from __future__ import annotations

import argparse
import importlib.util
import sys
import time
import uuid
from decimal import Decimal
from urllib.error import HTTPError, URLError

from perpdex_farming_bot.cli.pacifica_live_common import (
    build_tiny_order_plan,
    fmt_decimal,
    load_pacifica_open_orders,
    load_pacifica_positions,
    nonzero_pacifica_positions,
    pacifica_position_symbol,
    pacifica_signed_position_amount,
)
from perpdex_farming_bot.connectors.pacifica_readonly import (
    PacificaReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_pacifica_environment,
    read_only_get,
    validate_https_base_url,
)
from perpdex_farming_bot.connectors.pacifica_trading import (
    PacificaSigningError,
    PacificaTradingConfigError,
    extract_order_id,
    safe_signed_request_summary,
)
from perpdex_farming_bot.credentials import (
    pacifica_available_private_readonly_env,
    pacifica_credential_env,
    pacifica_signing_missing,
)
from perpdex_farming_bot.core.execution_models import OrderKind, RoundtripMode
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.base import AdapterError
from perpdex_farming_bot.exchanges.pacifica import PacificaAdapter
from perpdex_farming_bot.gateway.live_preflight import (
    build_live_preflight_gateway,
    paired_live_trade_intent,
    run_live_gateway_preflight,
)
from perpdex_farming_bot.gateway.live_action import GatewayLiveActionProxy


CONFIRM_TEXT = "LIVE_PACIFICA_TINY_MARKET_ROUNDTRIP"
MAX_FIRST_LIVE_NOTIONAL_USD = Decimal("25")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Pacifica tiny market roundtrip. Default is dry-run; live POST requires --execute-live "
            "and the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="PACIFICA",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="testnet", help="Pacifica environment: testnet or production/mainnet.")
    parser.add_argument("--symbol", default="BTC", help="Pacifica case-sensitive market symbol, e.g. BTC.")
    parser.add_argument("--agg-level", type=int, default=1, help="Orderbook aggregation level for fresh verification.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("15"))
    parser.add_argument("--slippage-percent", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--expiry-window-ms", type=int, default=5000)
    parser.add_argument("--network", action="store_true", help="Actually call Pacifica read-only REST and, if confirmed, live POST.")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--post-order-wait-seconds", type=float, default=1.0)
    parser.add_argument("--position-settle-attempts", type=int, default=8)
    parser.add_argument("--position-settle-delay-seconds", type=float, default=0.5)
    parser.add_argument(
        "--allow-existing-position",
        action="store_true",
        help="Allow starting while any existing position is present. Default blocks.",
    )
    args = parser.parse_args()

    _validate_args(args)

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_pacifica_environment(args.environment)
    credential_env = pacifica_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("pacifica_live_test=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"symbol={args.symbol}")
    print(f"execute_live={args.execute_live}")
    print(f"max_notional_usd={args.max_notional_usd}")
    print(f"slippage_percent={args.slippage_percent}")
    print(f"expiry_window_ms={args.expiry_window_ms}")
    print("entry_order_type=market_bid")
    print("close_order_type=market_reduce_only")
    print("fresh_orderbook_verify=immediately_before_live_post")
    print(f"required_confirmation={CONFIRM_TEXT}")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except PacificaReadonlyConfigError as exc:
        print("live_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_agent_public_key}={masked_env_status(credential_env.api_agent_public_key)}")
    print(f"primary_{credential_env.api_agent_private_key}={masked_env_status(credential_env.api_agent_private_key)}")

    if not args.network:
        print("network_skipped=pass_--network_to_run_live_test_preflight_or_post")
        print("live_ready=False")
        return

    private_env = pacifica_available_private_readonly_env(args.credential_prefix, environment)
    signing_missing = pacifica_signing_missing(args.credential_prefix, environment)
    dependencies_ready = _dependencies_ready()
    print(f"private_readonly_env_ready={private_env is not None}")
    print(f"signing_env_ready={not signing_missing}")
    print(f"signing_runtime_ready={dependencies_ready}")
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))

    if private_env is None:
        print("live_ready=False")
        print("reason=missing_account_address_env")
        return
    if signing_missing:
        print("live_ready=False")
        print("reason=missing_signing_env")
        return
    if not dependencies_ready:
        print("live_ready=False")
        print("reason=missing_pacifica_signing_dependencies")
        print("hint=install_requirements_in_the_python_runtime_before_live_testing")
        return

    account = get_env(credential_env.account_address)
    dry_adapter = PacificaAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=False,
    )

    if not _private_state_is_clean(api_endpoint, account, args):
        return

    order_plan = _build_and_print_order_plan(api_endpoint, args, "initial")
    if order_plan is None:
        return

    signed_entry = _build_signed_market_order(
        adapter=dry_adapter,
        symbol=args.symbol,
        amount=order_plan.amount,
        side="bid",
        reduce_only=False,
        slippage_percent=args.slippage_percent,
        expiry_window_ms=args.expiry_window_ms,
    )
    if signed_entry is None:
        return
    _print_signed_request_summary("dry_run_entry", signed_entry)

    gateway_preflight = _run_gateway_preflight(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        account_alias=f"{credential_env.prefix}_gateway",
        symbol=args.symbol,
        amount=order_plan.amount,
        best_bid=order_plan.best_bid,
        best_ask=order_plan.best_ask,
        max_notional_usd=args.max_notional_usd,
        timeout_seconds=args.timeout_seconds,
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

    fresh_plan = _build_and_print_order_plan(api_endpoint, args, "fresh")
    if fresh_plan is None:
        print("live_aborted=fresh_orderbook_verify_failed")
        return

    signed_entry = _build_signed_market_order(
        adapter=dry_adapter,
        symbol=args.symbol,
        amount=fresh_plan.amount,
        side="bid",
        reduce_only=False,
        slippage_percent=args.slippage_percent,
        expiry_window_ms=args.expiry_window_ms,
    )
    if signed_entry is None:
        print("live_aborted=entry_signing_failed")
        return

    live_adapter = PacificaAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=True,
    )
    live_gateway, live_trade_intent = _build_gateway_context(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        account_alias=f"{credential_env.prefix}_gateway",
        symbol=args.symbol,
        amount=fresh_plan.amount,
        best_bid=fresh_plan.best_bid,
        best_ask=fresh_plan.best_ask,
        max_notional_usd=args.max_notional_usd,
        timeout_seconds=args.timeout_seconds,
        live_orders_enabled=True,
    )
    live_adapter = GatewayLiveActionProxy(
        target=live_adapter,
        gateway=live_gateway,
        trade_intent=live_trade_intent,
        request_id_prefix="pacifica-live-test-gateway-submit",
    )
    print("live_submit_route=execution_gateway_live_action_proxy")
    print("live_entry_submitting=True")
    try:
        entry_result = live_adapter.submit_signed_order_request(signed_entry)
    except AdapterError as exc:
        print("live_aborted=adapter_rejected_entry_order")
        print(f"adapter_error={exc}")
        return
    _print_post_result("entry", entry_result)
    if not entry_result.ok:
        print("live_aborted=entry_order_failed")
        return

    entry_order_id = extract_order_id(entry_result.parsed)
    if entry_order_id:
        print(f"entry_order_id={entry_order_id}")
    if args.post_order_wait_seconds:
        time.sleep(args.post_order_wait_seconds)

    close_amount = _settled_position_amount(api_endpoint, account, args.symbol, args)
    if close_amount == 0:
        print("post_entry_position_snapshot=flat_or_delayed")
        print("close_quantity_source=planned_entry_amount_reduce_only_rescue")
        close_amount = fresh_plan.amount
        if entry_order_id:
            _print_order_history_check(api_endpoint, entry_order_id, args.timeout_seconds)
    else:
        print("close_quantity_source=detected_position_amount")

    close_side = "ask" if close_amount > 0 else "bid"
    close_amount_abs = abs(close_amount)
    signed_close = _build_signed_market_order(
        adapter=dry_adapter,
        symbol=args.symbol,
        amount=close_amount_abs,
        side=close_side,
        reduce_only=True,
        slippage_percent=args.slippage_percent,
        expiry_window_ms=args.expiry_window_ms,
    )
    if signed_close is None:
        print("live_aborted=close_signing_failed")
        return
    _print_signed_request_summary("live_close", signed_close)

    print("live_close_submitting=True")
    try:
        close_result = live_adapter.submit_signed_order_request(signed_close)
    except AdapterError as exc:
        print("live_aborted=adapter_rejected_close_order")
        print(f"adapter_error={exc}")
        return
    _print_post_result("close", close_result)
    if not close_result.ok:
        print("manual_review_required=True")
        return

    if args.post_order_wait_seconds:
        time.sleep(args.post_order_wait_seconds)
    final_amount = _settled_position_amount(api_endpoint, account, args.symbol, args, label="final")
    print(f"final_position_amount={fmt_decimal(final_amount)}")
    if final_amount == 0:
        print("live_test_status=closed_flat_or_not_detected")
    else:
        print("live_test_status=position_remains_manual_review_required")


def _validate_args(args: argparse.Namespace) -> None:
    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > MAX_FIRST_LIVE_NOTIONAL_USD:
        raise SystemExit(f"--max-notional-usd must be <= {MAX_FIRST_LIVE_NOTIONAL_USD} for the first Pacifica live test")
    if args.slippage_percent <= 0 or args.slippage_percent > Decimal("2"):
        raise SystemExit("--slippage-percent must be > 0 and <= 2 for this guarded live test")
    if args.expiry_window_ms <= 0 or args.expiry_window_ms > 30_000:
        raise SystemExit("--expiry-window-ms must be > 0 and <= 30000")
    if args.post_order_wait_seconds < 0:
        raise SystemExit("--post-order-wait-seconds must be zero or greater")
    if args.position_settle_attempts <= 0:
        raise SystemExit("--position-settle-attempts must be greater than zero")
    if args.position_settle_delay_seconds < 0:
        raise SystemExit("--position-settle-delay-seconds must be zero or greater")


def _run_gateway_preflight(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    account_alias: str,
    symbol: str,
    amount: Decimal,
    best_bid: Decimal,
    best_ask: Decimal,
    max_notional_usd: Decimal,
    timeout_seconds: float,
):
    gateway, trade_intent = _build_gateway_context(
        api_endpoint=api_endpoint,
        credential_prefix=credential_prefix,
        environment=environment,
        account_alias=account_alias,
        symbol=symbol,
        amount=amount,
        best_bid=best_bid,
        best_ask=best_ask,
        max_notional_usd=max_notional_usd,
        timeout_seconds=timeout_seconds,
        live_orders_enabled=False,
    )
    return run_live_gateway_preflight(
        gateway=gateway,
        trade_intent=trade_intent,
        request_id="pacifica-live-test-gateway-preflight",
        include_read_only=True,
    )


def _build_gateway_context(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    account_alias: str,
    symbol: str,
    amount: Decimal,
    best_bid: Decimal,
    best_ask: Decimal,
    max_notional_usd: Decimal,
    timeout_seconds: float,
    live_orders_enabled: bool,
):
    trade_intent = paired_live_trade_intent(
        exchange_id="pacifica",
        account_alias=account_alias,
        strategy_id="pacifica_live_test",
        market=symbol,
        roundtrip_mode=RoundtripMode.CONFIRMED,
        quantity=amount,
        buy_reference_price=best_ask,
        sell_reference_price=best_bid,
        buy_order_type=OrderKind.MARKET,
        sell_order_type=OrderKind.MARKET,
        max_gross_notional_usd=max_notional_usd * Decimal("2"),
        metadata={"planned_gross_volume_usd": str(max_notional_usd * Decimal("2"))},
    )
    gateway = build_live_preflight_gateway(
        exchange_id="pacifica",
        account_alias=account_alias,
        market=symbol,
        adapter_factory=lambda: PacificaAdapter(
            api_endpoint=api_endpoint,
            credential_prefix=credential_prefix,
            environment=environment,
            timeout_seconds=timeout_seconds,
            allow_live_orders=False,
        ),
        entry_fee_bps=Decimal("3"),
        exit_fee_bps=Decimal("3"),
        fee_source="pacifica_tiny_live_test_conservative_default",
        max_order_notional_usd=max_notional_usd + Decimal("1"),
        max_gross_notional_usd=max_notional_usd * Decimal("2"),
        open_orders_supported=True,
        live_orders_enabled=live_orders_enabled,
    )
    return gateway, trade_intent


def _dependencies_ready() -> bool:
    return importlib.util.find_spec("base58") is not None and importlib.util.find_spec("solders") is not None


def _private_state_is_clean(api_endpoint: str, account: str, args: argparse.Namespace) -> bool:
    try:
        positions = load_pacifica_positions(api_endpoint, account, args.timeout_seconds)
        nonzero_positions = nonzero_pacifica_positions(positions)
        selected_positions = nonzero_pacifica_positions(positions, args.symbol)
        open_orders, last_order_id = load_pacifica_open_orders(api_endpoint, account, args.timeout_seconds)
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
        print("live_ready=False")
        print(f"private_state_error={exc.__class__.__name__}")
        return False

    print(f"start_position_count={len(nonzero_positions)}")
    print(f"start_selected_symbol_position_count={len(selected_positions)}")
    print(f"start_open_order_count={len(open_orders)}")
    if last_order_id:
        print(f"last_order_id={last_order_id}")
    if nonzero_positions and not args.allow_existing_position:
        print("live_ready=False")
        print("reason=existing_positions_detected")
        print("existing_position_markets=" + ",".join(sorted({pacifica_position_symbol(item) for item in nonzero_positions})))
        return False
    if open_orders:
        print("live_ready=False")
        print("reason=existing_open_orders_detected")
        return False
    return True


def _build_and_print_order_plan(api_endpoint: str, args: argparse.Namespace, label: str) -> object | None:
    try:
        order_plan = build_tiny_order_plan(
            api_endpoint=api_endpoint,
            symbol=args.symbol,
            max_notional_usd=args.max_notional_usd,
            timeout_seconds=args.timeout_seconds,
            agg_level=args.agg_level,
        )
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
        print(f"{label}_market_preflight_ok=False")
        print(f"{label}_market_preflight_error={exc.__class__.__name__}")
        print("live_ready=False")
        return None

    print(f"{label}_market_preflight_ok={order_plan.eligible}")
    print(f"{label}_best_bid={fmt_decimal(order_plan.best_bid)}")
    print(f"{label}_best_ask={fmt_decimal(order_plan.best_ask)}")
    print(f"{label}_spread_bps={order_plan.spread_bps:.4f}")
    print(f"{label}_planned_amount={fmt_decimal(order_plan.amount)}")
    print(f"{label}_planned_one_side_notional_usd={order_plan.one_side_notional_usd:.4f}")
    print(f"{label}_planned_roundtrip_gross_notional_usd={order_plan.gross_roundtrip_notional_usd:.4f}")
    print(f"{label}_market_preflight_reason={order_plan.reason}")
    if not order_plan.eligible:
        print("live_ready=False")
        print(f"reason={label}_market_not_eligible")
        return None
    return order_plan


def _build_signed_market_order(
    *,
    adapter: PacificaAdapter,
    symbol: str,
    amount: Decimal,
    side: str,
    reduce_only: bool,
    slippage_percent: Decimal,
    expiry_window_ms: int,
) -> object | None:
    try:
        return adapter.build_market_order_request(
            symbol=symbol,
            amount=amount,
            side=side,
            slippage_percent=slippage_percent,
            reduce_only=reduce_only,
            client_order_id=str(uuid.uuid4()),
            expiry_window_ms=expiry_window_ms,
        )
    except (PacificaSigningError, PacificaTradingConfigError) as exc:
        print("signing_ok=False")
        print(f"signing_error={exc}")
        return None


def _print_signed_request_summary(prefix: str, signed_request: object) -> None:
    summary = safe_signed_request_summary(signed_request)
    print(f"{prefix}_request_ready=True")
    for key, value in summary.items():
        print(f"{prefix}_{key}={value}")


def _print_post_result(prefix: str, result: object) -> None:
    print(f"{prefix}_post_ok={result.ok}")
    print(f"{prefix}_post_status_code={result.status_code}")
    print(f"{prefix}_post_body_shape={result.body_shape}")
    if result.error:
        print(f"{prefix}_post_error={result.error}")
    if isinstance(result.parsed, dict):
        safe_keys = ",".join(sorted(str(key) for key in result.parsed.keys())[:8])
        print(f"{prefix}_post_response_keys={safe_keys}")


def _settled_position_amount(
    api_endpoint: str,
    account: str,
    symbol: str,
    args: argparse.Namespace,
    *,
    label: str = "post_entry",
) -> Decimal:
    amount = Decimal("0")
    for attempt in range(1, args.position_settle_attempts + 1):
        try:
            positions = load_pacifica_positions(api_endpoint, account, args.timeout_seconds)
        except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
            print(f"{label}_position_read_error={exc.__class__.__name__}")
            return Decimal("0")
        amount = sum(
            (
                pacifica_signed_position_amount(position)
                for position in positions
                if pacifica_position_symbol(position) == symbol
            ),
            Decimal("0"),
        )
        print(f"{label}_position_settle_attempt={attempt}")
        print(f"{label}_position_amount={fmt_decimal(amount)}")
        if amount != 0:
            return amount
        if attempt < args.position_settle_attempts and args.position_settle_delay_seconds:
            time.sleep(args.position_settle_delay_seconds)
    return amount


def _print_order_history_check(api_endpoint: str, order_id: str, timeout_seconds: float) -> None:
    result = read_only_get(
        api_endpoint,
        "/orders/history_by_id",
        {"order_id": order_id},
        timeout_seconds,
        private_readonly=True,
    )
    print(f"entry_history_readonly_ok={result.ok}")
    print(f"entry_history_status_code={result.status_code}")
    print(f"entry_history_body_shape={result.body_shape}")
    if result.error:
        print(f"entry_history_error={result.error}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
