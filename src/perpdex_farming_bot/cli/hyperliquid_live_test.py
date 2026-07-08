from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from perpdex_farming_bot.connectors.hyperliquid_readonly import (
    HyperliquidReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_hyperliquid_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    hyperliquid_available_private_readonly_env,
    hyperliquid_credential_env,
    hyperliquid_signing_missing,
    read_hyperliquid_credentials,
)
from perpdex_farming_bot.core.execution_models import ExecutionRequest, OrderKind, RoundtripMode
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.hyperliquid import HyperliquidAdapter
from perpdex_farming_bot.exchanges.hyperliquid_fees import load_hyperliquid_account_fee
from perpdex_farming_bot.gateway.live_preflight import (
    build_live_preflight_gateway,
    paired_live_trade_intent,
    run_live_gateway_preflight,
)
from perpdex_farming_bot.marketdata.hyperliquid import (
    HyperliquidMarketInfo,
    fetch_hyperliquid_rest_top_of_book,
    load_hyperliquid_market_info,
)


CONFIRM_TEXT = "LIVE_HYPERLIQUID_TINY_BTC_ROUNDTRIP"
MAX_FIRST_LIVE_NOTIONAL_USD = Decimal("25")
DEFAULT_MIN_ORDER_SIZE_USD = Decimal("10")


@dataclass(frozen=True)
class TinyOrderPlan:
    coin: str
    market_info: HyperliquidMarketInfo
    best_bid: Decimal
    best_ask: Decimal
    spread_bps: Decimal
    size: Decimal
    one_side_notional_usd: Decimal
    aggressive_buy_px: Decimal
    aggressive_sell_px: Decimal
    eligible: bool
    reason: str


@dataclass(frozen=True)
class FillSummary:
    ok: bool
    status: str
    filled_size: Decimal
    average_price: Decimal
    order_id: str
    reason: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Hyperliquid tiny BTC roundtrip. Default is dry-run; live POST requires --execute-live "
            "and the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HYPERLIQUID",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="production", help="Hyperliquid environment: production/mainnet.")
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--dex", default="", help="Optional Hyperliquid perp dex name. Empty means default perp dex.")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("12.5"))
    parser.add_argument("--min-order-size-usd", type=Decimal, default=DEFAULT_MIN_ORDER_SIZE_USD)
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("25"))
    parser.add_argument("--final-state-delay-seconds", type=float, default=0.5)
    parser.add_argument("--network", action="store_true", help="Actually call Hyperliquid read-only and, if confirmed, live POST.")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    _validate_args(args)

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_hyperliquid_environment(args.environment)
    credential_env = hyperliquid_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("hyperliquid_live_test=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"dex={args.dex or 'default'}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"coin={args.coin}")
    print(f"execute_live={args.execute_live}")
    print(f"max_notional_usd={fmt_decimal(args.max_notional_usd)}")
    print(f"min_order_size_usd={fmt_decimal(args.min_order_size_usd)}")
    print(f"slippage_bps={fmt_decimal(args.slippage_bps)}")
    print("entry_order_type=limit_ioc_buy")
    print("close_order_type=limit_ioc_reduce_only_sell")
    print("fresh_orderbook_verify=immediately_before_live_post")
    print(f"required_confirmation={CONFIRM_TEXT}")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except HyperliquidReadonlyConfigError as exc:
        print("live_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    available_env = hyperliquid_available_private_readonly_env(args.credential_prefix, environment)
    signing_missing = hyperliquid_signing_missing(args.credential_prefix, environment)
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_wallet_address}={masked_env_status(credential_env.api_wallet_address)}")
    print(f"primary_{credential_env.api_wallet_private_key}={masked_env_status(credential_env.api_wallet_private_key)}")
    print(f"optional_{credential_env.vault_address}={masked_env_status(credential_env.vault_address)}")
    print(f"private_readonly_env_ready={available_env is not None}")
    print(f"signing_env_ready={not signing_missing}")
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))
    print(f"hyperliquid_python_sdk_installed={importlib.util.find_spec('hyperliquid') is not None}")
    print(f"eth_account_installed={importlib.util.find_spec('eth_account') is not None}")

    if not args.network:
        print("network_skipped=pass_--network_to_run_live_test_preflight_or_post")
        print("live_ready=False")
        return

    credentials = read_hyperliquid_credentials(args.credential_prefix, environment)
    key_parse_status, key_matches_signer = _check_api_wallet_private_key(
        api_wallet_address=credentials["api_wallet_address"],
        api_wallet_private_key=credentials["api_wallet_private_key"],
    )
    print(f"api_wallet_private_key_parse={key_parse_status}")
    print(f"api_wallet_address_matches_private_key={key_matches_signer}")
    if key_parse_status != "ok" or key_matches_signer not in {"True", "skipped_missing_api_wallet_address"}:
        print("live_ready=False")
        print("reason=api_wallet_key_not_ready")
        return

    try:
        info, exchange = _build_sdk_clients(api_endpoint, credentials, args)
    except Exception as exc:
        print("live_ready=False")
        print(f"sdk_client_error={exc.__class__.__name__}")
        return

    if not _print_private_state(info, credentials["account_address"], args.dex, "start"):
        print("live_ready=False")
        return

    initial_plan = _build_and_print_plan(api_endpoint, args, "initial")
    if initial_plan is None or not initial_plan.eligible:
        print("live_ready=False")
        return

    try:
        account_fee = load_hyperliquid_account_fee(api_endpoint, credentials["account_address"], args.timeout_seconds)
    except Exception as exc:
        print("live_ready=False")
        print(f"account_fee_error={exc.__class__.__name__}")
        return
    print(f"account_taker_fee_bps={fmt_decimal(account_fee.taker_fee_bps)}")

    gateway_account_alias = f"{credential_env.prefix}_gateway"
    gateway_trade_intent = _gateway_trade_intent_from_plan(
        initial_plan,
        account_alias=gateway_account_alias,
        max_gross_notional_usd=args.max_notional_usd * Decimal("2"),
    )
    gateway = _build_hyperliquid_gateway(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        dex=args.dex,
        account_alias=gateway_account_alias,
        market=initial_plan.coin,
        fee_bps=account_fee.taker_fee_bps,
        max_order_notional_usd=args.max_notional_usd + Decimal("1"),
        max_gross_notional_usd=args.max_notional_usd * Decimal("2"),
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=False,
        live_orders_enabled=False,
    )
    gateway_preflight = run_live_gateway_preflight(
        gateway=gateway,
        trade_intent=gateway_trade_intent,
        request_id="hyperliquid-live-test-gateway-preflight",
        include_read_only=True,
    )
    if not gateway_preflight.ready:
        print("live_ready=False")
        print("reason=gateway_preflight_not_ready")
        return

    if not args.execute_live:
        print("live_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    fresh_plan = _build_and_print_plan(api_endpoint, args, "fresh")
    if fresh_plan is None or not fresh_plan.eligible:
        print("live_aborted=fresh_orderbook_verify_failed")
        return

    order_start = time.perf_counter()
    live_gateway = _build_hyperliquid_gateway(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        dex=args.dex,
        account_alias=gateway_account_alias,
        market=fresh_plan.coin,
        fee_bps=account_fee.taker_fee_bps,
        max_order_notional_usd=args.max_notional_usd + Decimal("1"),
        max_gross_notional_usd=args.max_notional_usd * Decimal("2"),
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=True,
        live_orders_enabled=True,
    )
    print("live_submit_route=execution_gateway_roundtrip_adapter")
    result = live_gateway.execute_paired_roundtrip(
        ExecutionRequest(
            request_id="hyperliquid-live-test-gateway-submit",
            trade_intent=_gateway_trade_intent_from_plan(
                fresh_plan,
                account_alias=gateway_account_alias,
                max_gross_notional_usd=args.max_notional_usd * Decimal("2"),
            ),
        ),
        instrument_id=fresh_plan.market_info.asset_id,
        first_side="BUY",
        second_side="SELL",
    )
    roundtrip_elapsed_ms = (time.perf_counter() - order_start) * 1000
    _print_exchange_order_result("entry", result.buy_result)
    _print_exchange_order_result("close", result.sell_result)
    print(f"gateway_roundtrip_success={result.success}")
    print(f"gateway_roundtrip_status={result.status}")
    print(f"roundtrip_order_elapsed_ms={roundtrip_elapsed_ms:.2f}")

    if args.final_state_delay_seconds:
        time.sleep(float(args.final_state_delay_seconds))
    final_ok = _print_private_state(info, credentials["account_address"], args.dex, "final")
    if result.success and final_ok:
        print("live_test_status=closed_flat_or_not_detected")
    else:
        print("live_test_status=position_or_close_fill_manual_review_required")


def _validate_args(args: argparse.Namespace) -> None:
    if normalize_hyperliquid_environment(args.environment) != "PRODUCTION":
        raise SystemExit("This live test is currently limited to Hyperliquid production/mainnet")
    if not args.coin:
        raise SystemExit("--coin is required")
    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > MAX_FIRST_LIVE_NOTIONAL_USD:
        raise SystemExit(f"--max-notional-usd must be <= {MAX_FIRST_LIVE_NOTIONAL_USD} for this guarded live test")
    if args.min_order_size_usd <= 0:
        raise SystemExit("--min-order-size-usd must be greater than zero")
    if args.min_order_size_usd > args.max_notional_usd:
        raise SystemExit("--min-order-size-usd must be <= --max-notional-usd")
    if args.slippage_bps <= 0 or args.slippage_bps > Decimal("100"):
        raise SystemExit("--slippage-bps must be > 0 and <= 100")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be greater than zero")
    if args.final_state_delay_seconds < 0:
        raise SystemExit("--final-state-delay-seconds must be zero or greater")


def _gateway_trade_intent_from_plan(
    plan: TinyOrderPlan,
    *,
    account_alias: str,
    max_gross_notional_usd: Decimal,
):
    return paired_live_trade_intent(
        exchange_id="hyperliquid",
        account_alias=account_alias,
        strategy_id="hyperliquid_live_test",
        market=plan.coin,
        roundtrip_mode=RoundtripMode.CONFIRMED,
        quantity=plan.size,
        buy_price=plan.aggressive_buy_px,
        sell_price=plan.aggressive_sell_px,
        buy_reference_price=plan.best_ask,
        sell_reference_price=plan.best_bid,
        buy_order_type=OrderKind.LIMIT,
        sell_order_type=OrderKind.LIMIT,
        time_in_force="ioc",
        max_gross_notional_usd=max_gross_notional_usd,
        metadata={
            "spread_bps": str(plan.spread_bps),
            "planned_gross_volume_usd": str(plan.one_side_notional_usd * Decimal("2")),
        },
        buy_metadata={"source": "hyperliquid_live_test"},
        sell_metadata={"source": "hyperliquid_live_test"},
    )


def _build_hyperliquid_gateway(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    dex: str,
    account_alias: str,
    market: str,
    fee_bps: Decimal,
    max_order_notional_usd: Decimal,
    max_gross_notional_usd: Decimal,
    timeout_seconds: float,
    allow_live_orders: bool,
    live_orders_enabled: bool,
):
    return build_live_preflight_gateway(
        exchange_id="hyperliquid",
        account_alias=account_alias,
        market=market,
        adapter_factory=lambda: HyperliquidAdapter(
            api_endpoint=api_endpoint,
            credential_prefix=credential_prefix,
            environment=environment,
            timeout_seconds=timeout_seconds,
            dex=dex,
            perp_dexs=(dex,) if dex else (),
            allow_live_orders=allow_live_orders,
            max_roundtrip_gross_volume_usd=max_gross_notional_usd + Decimal("5"),
        ),
        entry_fee_bps=fee_bps,
        exit_fee_bps=fee_bps,
        fee_source="hyperliquid_account_user_fees",
        max_order_notional_usd=max_order_notional_usd,
        max_gross_notional_usd=max_gross_notional_usd,
        open_orders_supported=True,
        live_orders_enabled=live_orders_enabled,
    )


def _print_exchange_order_result(label: str, result: object | None) -> None:
    if result is None:
        print(f"{label}_result_present=False")
        return
    print(f"{label}_result_present=True")
    print(f"{label}_success={getattr(result, 'success', False)}")
    print(f"{label}_status={getattr(result, 'status', 'unknown')}")
    print(f"{label}_filled_size={getattr(result, 'filled_size', Decimal('0'))}")
    average_price = getattr(result, "average_price", None)
    if average_price is not None:
        print(f"{label}_average_price={average_price}")
    order_id = getattr(result, "exchange_order_id", None)
    if order_id:
        print(f"{label}_order_id={order_id}")
    error = getattr(result, "error", "")
    if error:
        print(f"{label}_error={error}")


def _build_sdk_clients(api_endpoint: str, credentials: dict[str, str], args: argparse.Namespace):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    wallet = Account.from_key(credentials["api_wallet_private_key"])
    vault_address = credentials["vault_address"] or None
    info = Info(api_endpoint, skip_ws=True, timeout=args.timeout_seconds)
    exchange = Exchange(
        wallet,
        base_url=api_endpoint,
        vault_address=vault_address,
        account_address=credentials["account_address"],
        timeout=args.timeout_seconds,
    )
    return info, exchange


def _build_and_print_plan(
    api_endpoint: str,
    args: argparse.Namespace,
    label: str,
    *,
    close_size: Decimal | None = None,
) -> TinyOrderPlan | None:
    try:
        market_info = load_hyperliquid_market_info(
            api_endpoint,
            coin=args.coin,
            timeout_seconds=args.timeout_seconds,
            dex=args.dex,
            min_order_size_usd=args.min_order_size_usd,
        )
        orderbook = fetch_hyperliquid_rest_top_of_book(
            api_endpoint,
            coin=args.coin,
            timeout_seconds=args.timeout_seconds,
            dex=args.dex,
        )
    except (TimeoutError, OSError, ValueError, HyperliquidReadonlyConfigError) as exc:
        print(f"{label}_market_preflight_ok=False")
        print(f"{label}_market_preflight_error={exc.__class__.__name__}")
        return None

    if not orderbook.ok or orderbook.snapshot is None:
        print(f"{label}_market_preflight_ok=False")
        print(f"{label}_market_preflight_reason={orderbook.reason}")
        return None

    snapshot = orderbook.snapshot
    if close_size is None:
        size = _round_up_to_step(args.min_order_size_usd / snapshot.best_ask, market_info.lot_size)
    else:
        size = _round_down_to_step(close_size, market_info.lot_size)
    notional = size * snapshot.best_ask
    eligible = True
    reason = "ok"
    if size <= 0:
        eligible = False
        reason = "size_rounded_to_zero"
    elif close_size is None and notional < args.min_order_size_usd:
        eligible = False
        reason = "notional_below_min_order_size"
    elif notional > args.max_notional_usd:
        eligible = False
        reason = "notional_above_cap"
    elif size > snapshot.best_ask_size:
        eligible = False
        reason = "ask_top_level_size_too_small"
    elif close_size is not None and size > snapshot.best_bid_size:
        eligible = False
        reason = "bid_top_level_size_too_small"

    slippage_fraction = args.slippage_bps / Decimal("10000")
    aggressive_buy_px = _sdk_style_slippage_price(
        snapshot.best_ask,
        is_buy=True,
        slippage_fraction=slippage_fraction,
        price_decimal_places=market_info.price_decimal_places,
    )
    aggressive_sell_px = _sdk_style_slippage_price(
        snapshot.best_bid,
        is_buy=False,
        slippage_fraction=slippage_fraction,
        price_decimal_places=market_info.price_decimal_places,
    )

    print(f"{label}_market_preflight_ok={eligible}")
    print(f"{label}_coin={args.coin}")
    print(f"{label}_asset_id={market_info.asset_id}")
    print(f"{label}_sz_decimals={market_info.sz_decimals}")
    print(f"{label}_lot_size={fmt_decimal(market_info.lot_size)}")
    print(f"{label}_price_decimal_places={market_info.price_decimal_places}")
    print(f"{label}_best_bid={fmt_decimal(snapshot.best_bid)}")
    print(f"{label}_best_ask={fmt_decimal(snapshot.best_ask)}")
    print(f"{label}_spread_bps={snapshot.spread_bps:.4f}")
    print(f"{label}_planned_size={fmt_decimal(size)}")
    print(f"{label}_planned_one_side_notional_usd={notional:.4f}")
    print(f"{label}_aggressive_buy_px={fmt_decimal(aggressive_buy_px)}")
    print(f"{label}_aggressive_sell_px={fmt_decimal(aggressive_sell_px)}")
    print(f"{label}_market_preflight_reason={reason}")

    return TinyOrderPlan(
        coin=args.coin,
        market_info=market_info,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        spread_bps=snapshot.spread_bps,
        size=size,
        one_side_notional_usd=notional,
        aggressive_buy_px=aggressive_buy_px,
        aggressive_sell_px=aggressive_sell_px,
        eligible=eligible,
        reason=reason,
    )


def _print_private_state(info, account_address: str, dex: str, label: str) -> bool:
    try:
        state = info.user_state(account_address, dex=dex)
        open_orders = info.open_orders(account_address, dex=dex)
    except Exception as exc:
        print(f"{label}_private_state_ok=False")
        print(f"{label}_private_state_error={exc.__class__.__name__}")
        return False

    position_count = _position_count(state)
    open_order_count = len(open_orders) if isinstance(open_orders, list) else 0
    print(f"{label}_private_state_ok=True")
    print(f"{label}_position_count={position_count}")
    print(f"{label}_open_order_count={open_order_count}")
    if position_count != 0:
        print(f"{label}_state_check=blocked_existing_position")
        return False
    if open_order_count != 0:
        print(f"{label}_state_check=blocked_existing_open_orders")
        return False
    print(f"{label}_state_check=ok")
    return True


def _position_count(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("assetPositions")
    if not isinstance(raw, list):
        return 0
    count = 0
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("position"), dict):
            continue
        if Decimal(str(item["position"].get("szi", "0"))) != 0:
            count += 1
    return count


def _parse_single_order_fill(result: object) -> FillSummary:
    if not isinstance(result, dict):
        return FillSummary(False, "unknown", Decimal("0"), Decimal("0"), "", "response_not_object")
    if result.get("status") != "ok":
        return FillSummary(False, str(result.get("status") or "error"), Decimal("0"), Decimal("0"), "", "status_not_ok")
    response = result.get("response")
    if not isinstance(response, dict):
        return FillSummary(False, "ok", Decimal("0"), Decimal("0"), "", "missing_response")
    data = response.get("data")
    if not isinstance(data, dict):
        return FillSummary(False, "ok", Decimal("0"), Decimal("0"), "", "missing_data")
    statuses = data.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        return FillSummary(False, "ok", Decimal("0"), Decimal("0"), "", "missing_statuses")
    first = statuses[0]
    if not isinstance(first, dict):
        return FillSummary(False, "ok", Decimal("0"), Decimal("0"), "", "status_not_object")
    if "filled" in first and isinstance(first["filled"], dict):
        filled = first["filled"]
        return FillSummary(
            True,
            "filled",
            Decimal(str(filled.get("totalSz", "0"))),
            Decimal(str(filled.get("avgPx", "0"))),
            str(filled.get("oid", "")),
            "filled",
        )
    if "resting" in first:
        return FillSummary(False, "resting", Decimal("0"), Decimal("0"), "", "ioc_order_resting_unexpected")
    if "error" in first:
        return FillSummary(False, "error", Decimal("0"), Decimal("0"), "", str(first.get("error") or "order_error"))
    return FillSummary(False, "unknown", Decimal("0"), Decimal("0"), "", "unknown_order_status")


def _print_order_result(label: str, result: object, fill: FillSummary, elapsed_ms: float) -> None:
    response_type = "unknown"
    status_count = 0
    if isinstance(result, dict):
        response_type = str(result.get("status") or "unknown")
        response = result.get("response")
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict) and isinstance(data.get("statuses"), list):
                status_count = len(data["statuses"])
    print(f"{label}_response_status={response_type}")
    print(f"{label}_response_status_count={status_count}")
    print(f"{label}_fill_status={fill.status}")
    print(f"{label}_filled_size={fmt_decimal(fill.filled_size)}")
    print(f"{label}_average_price={fmt_decimal(fill.average_price)}")
    print(f"{label}_order_id={fill.order_id or 'unknown'}")
    print(f"{label}_elapsed_ms={elapsed_ms:.2f}")
    print(f"{label}_reason={fill.reason}")


def _check_api_wallet_private_key(*, api_wallet_address: str, api_wallet_private_key: str) -> tuple[str, str]:
    if not api_wallet_private_key:
        return "skipped_missing", "skipped"
    try:
        from eth_account import Account

        derived = Account.from_key(api_wallet_private_key).address
    except Exception:
        return "error", "skipped"

    if not api_wallet_address:
        return "ok", "skipped_missing_api_wallet_address"
    return "ok", str(derived.casefold() == api_wallet_address.casefold())


def _round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _sdk_style_slippage_price(
    value: Decimal,
    *,
    is_buy: bool,
    slippage_fraction: Decimal,
    price_decimal_places: int,
) -> Decimal:
    adjusted = value * (Decimal("1") + slippage_fraction if is_buy else Decimal("1") - slippage_fraction)
    rounded = round(float(f"{float(adjusted):.5g}"), price_decimal_places)
    return Decimal(str(rounded))


def fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
