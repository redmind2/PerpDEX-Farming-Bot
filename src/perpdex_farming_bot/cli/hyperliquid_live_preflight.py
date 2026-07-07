from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from perpdex_farming_bot.connectors.hyperliquid_readonly import (
    HyperliquidReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    info_post_json,
    normalize_hyperliquid_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.core.execution_cost import (
    MarketCostInput,
    MarketCostResult,
    SizingInput,
    SizingResult,
    calculate_market_cost,
    calculate_sizing,
)
from perpdex_farming_bot.core.execution_event import (
    ExecutionEvent,
    emit_execution_event,
    estimate_loss_usd,
    estimate_roundtrip_fee_usd,
)
from perpdex_farming_bot.credentials import (
    hyperliquid_available_private_readonly_env,
    hyperliquid_credential_env,
    hyperliquid_signing_missing,
    read_hyperliquid_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.hyperliquid_fees import (
    HyperliquidAccountFee,
    HyperliquidFeeProvider,
    hyperliquid_fee_overrides_from_config,
    hyperliquid_market_fee_metadata_from_meta,
    load_hyperliquid_account_fee,
)
from perpdex_farming_bot.marketdata.hyperliquid import (
    HyperliquidMarketInfo,
    fetch_hyperliquid_rest_top_of_book,
    load_all_hyperliquid_market_info,
)


MAX_FIRST_LIVE_NOTIONAL_USD = Decimal("25")


@dataclass(frozen=True)
class HyperliquidMarketCandidate:
    coin: str
    display_market: str
    market_info: HyperliquidMarketInfo
    average_spread_bps: Decimal
    max_spread_bps: Decimal
    best_bid: Decimal
    best_ask: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    live_spread_bps: Decimal
    eligible: bool
    reason: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hyperliquid live preflight. Read-only by design: sends no orders and no cancels.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hyperliquid.live-volume.json")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HYPERLIQUID",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="", help="Override config environment: production/mainnet.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--network", action="store_true", help="Actually call Hyperliquid public/private read-only /info.")
    parser.add_argument("--max-notional-usd", type=Decimal, default=None)
    parser.add_argument(
        "--allow-existing-position",
        action="store_true",
        help="Allow planning when an existing non-zero position is present. Default blocks.",
    )
    parser.add_argument(
        "--allow-open-orders",
        action="store_true",
        help="Allow planning when existing open orders are present. Default blocks.",
    )
    parser.add_argument("--fast-close-on-fill", action="store_true", help="Review fast close path. Sends no close order.")
    parser.add_argument("--prebuild-close-order", action="store_true", help="Defer unsigned close-request prebuild until fill.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = normalize_hyperliquid_environment(args.environment or str(plan.get("environment", "production")))
    dex = str(plan.get("dex", ""))
    credential_env = hyperliquid_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    max_notional = args.max_notional_usd or Decimal(str(plan.get("max_leg_notional_usd", "15")))
    target_gross = Decimal(str(plan.get("target_gross_volume_usd", "20")))
    level_size_fraction = Decimal(str(plan.get("level_size_fraction", "0.5")))
    default_max_spread_bps = Decimal(str(plan.get("max_spread_bps", "2")))
    unknown_fee_policy = str(plan.get("unknown_fee_policy", "block"))

    if max_notional <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if max_notional > MAX_FIRST_LIVE_NOTIONAL_USD:
        raise SystemExit(f"--max-notional-usd must be <= {MAX_FIRST_LIVE_NOTIONAL_USD} for the first Hyperliquid live test")
    if level_size_fraction <= 0 or level_size_fraction > 1:
        raise SystemExit("level_size_fraction must be greater than 0 and <= 1")
    if unknown_fee_policy != "block":
        raise SystemExit("Hyperliquid preflight only allows unknown_fee_policy=block in this safety pass")

    print("hyperliquid_live_preflight=read_only_no_orders")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"dex={dex or 'default'}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"config={args.config}")
    print(f"max_notional_usd={fmt_decimal(max_notional)}")
    print(f"target_gross_volume_usd={fmt_decimal(target_gross)}")
    print(f"level_size_fraction={fmt_decimal(level_size_fraction)}")
    print(f"default_max_spread_bps={fmt_decimal(default_max_spread_bps)}")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("orders_sent=False")
    print("cancel_sent=False")
    print("fee_unknown_policy=block")
    print("market_selection=lowest_expected_loss_bps_then_live_spread_bps_then_coin")
    print("fresh_orderbook_verify=required_before_any_future_live_order")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except HyperliquidReadonlyConfigError as exc:
        print("preflight_ready=False")
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
    key_parse_status, key_matches_signer = _check_api_wallet_private_key(
        api_wallet_address=get_env(credential_env.api_wallet_address),
        api_wallet_private_key=get_env(credential_env.api_wallet_private_key),
    )
    print(f"api_wallet_private_key_parse={key_parse_status}")
    print(f"api_wallet_address_matches_private_key={key_matches_signer}")

    if args.fast_close_on_fill or args.prebuild_close_order:
        print(f"fast_close_on_fill_requested={args.fast_close_on_fill}")
        print(f"prebuild_close_order_requested={args.prebuild_close_order}")
        if args.fast_close_on_fill and args.prebuild_close_order:
            print("fast_close_mode=close_request_prebuild_only")
            print("fast_close_pre_signed=False")
            print("fast_close_size_rule=defer_until_entry_fill_actual_filled_size")
            print("fast_close_reconciliation=required_final_position_and_open_order_check")
        else:
            print("fast_close_mode=disabled_requires_both_flags")

    if not args.network:
        print("network_skipped=pass_--network_to_run_hyperliquid_live_preflight")
        print("preflight_ready=False")
        return

    account_fee = _load_account_fee_or_none(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        private_readonly_ready=available_env is not None,
    )

    try:
        meta_body: dict[str, object] = {"type": "meta"}
        if dex:
            meta_body["dex"] = dex
        meta_payload = info_post_json(api_endpoint, meta_body, args.timeout_seconds)
        market_info_by_coin = load_all_hyperliquid_market_info(
            api_endpoint,
            timeout_seconds=args.timeout_seconds,
            dex=dex,
            min_order_size_by_coin=_min_order_size_by_coin(plan),
        )
    except (TimeoutError, OSError, ValueError, HyperliquidReadonlyConfigError) as exc:
        print("market_metadata_ok=False")
        print(f"market_metadata_error={exc.__class__.__name__}")
        print("preflight_ready=False")
        return

    fee_provider = HyperliquidFeeProvider(
        metadata_by_market=hyperliquid_market_fee_metadata_from_meta(meta_payload),
        override_by_market=hyperliquid_fee_overrides_from_config(args.config),
        account_fee=account_fee,
    )
    print("market_metadata_ok=True")
    print(f"market_metadata_count={len(market_info_by_coin)}")
    print("fee_provider=hyperliquid")
    print(f"fee_market_metadata_count={len(fee_provider.metadata_by_market)}")
    print(f"fee_config_override_count={len(fee_provider.override_by_market)}")
    print(f"account_fee_auto_lookup_ok={account_fee is not None}")

    private_state_ok = _print_private_state(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        dex=dex,
        timeout_seconds=args.timeout_seconds,
        private_readonly_ready=available_env is not None,
        allow_existing_position=args.allow_existing_position,
        allow_open_orders=args.allow_open_orders,
    )

    candidate_rows: list[tuple[HyperliquidMarketCandidate, MarketCostResult, SizingResult]] = []
    for market_config in _market_configs(plan):
        candidate = _candidate_from_live_orderbook(
            api_endpoint=api_endpoint,
            market_config=market_config,
            market_info_by_coin=market_info_by_coin,
            timeout_seconds=args.timeout_seconds,
            dex=dex,
            default_max_spread_bps=default_max_spread_bps,
        )
        cost = _candidate_cost(candidate, fee_provider) if candidate.eligible else _blocked_cost(candidate, fee_provider)
        sizing = _candidate_sizing(candidate, max_notional, target_gross, level_size_fraction)
        if candidate.eligible and cost.eligible and sizing.eligible:
            candidate_rows.append((candidate, cost, sizing))
        _print_candidate(candidate, cost, sizing)

    print(f"eligible_market_count={len(candidate_rows)}")
    if not candidate_rows:
        print("selected_market=none")
        print("preflight_ready=False")
        return

    selected, selected_cost, selected_sizing = min(
        candidate_rows,
        key=lambda item: (item[1].expected_loss_bps, item[0].live_spread_bps, item[0].coin),
    )
    print(f"selected_market={selected.display_market}")
    print(f"selected_coin={selected.coin}")
    print(f"selected_asset_id={selected.market_info.asset_id}")
    print(f"selected_lot_size={fmt_decimal(selected.market_info.lot_size)}")
    print(f"selected_price_decimal_places={selected.market_info.price_decimal_places}")
    print(f"selected_min_order_size_usd={fmt_optional_decimal(selected.market_info.min_order_size_usd)}")
    print(f"selected_best_bid={fmt_decimal(selected.best_bid)}")
    print(f"selected_best_ask={fmt_decimal(selected.best_ask)}")
    print(f"selected_best_bid_size={fmt_decimal(selected.best_bid_size)}")
    print(f"selected_best_ask_size={fmt_decimal(selected.best_ask_size)}")
    print(f"selected_live_spread_bps={selected.live_spread_bps:.4f}")
    print(f"selected_entry_fee_bps={selected_cost.entry_fee_bps}")
    print(f"selected_exit_fee_bps={selected_cost.exit_fee_bps}")
    print(f"selected_slippage_buffer_bps={selected_cost.slippage_buffer_bps}")
    print(f"selected_fee_source={selected_cost.fee_source}")
    print(f"selected_expected_loss_bps={selected_cost.expected_loss_bps:.4f}")
    print(f"planned_size={fmt_decimal(selected_sizing.amount)}")
    print(f"planned_entry_notional_usd={selected_sizing.entry_notional_usd:.4f}")
    print(f"planned_exit_notional_usd={selected_sizing.exit_notional_usd:.4f}")
    print(f"planned_cycle_gross_volume_usd={selected_sizing.planned_gross_volume_usd:.4f}")

    if args.fast_close_on_fill and args.prebuild_close_order:
        print("fast_close_prebuild_ready=False")
        print("fast_close_prebuild_reason=requires_entry_fill_before_close_size_and_signature")
        print("fast_close_close_order_reduce_only=True")
        print("fast_close_cleanup_tools_to_review=scheduleCancel,cancelByCloid,final_position_reconciliation")

    _emit_hyperliquid_execution_event(
        account_label=args.credential_prefix,
        environment=environment,
        market=selected.display_market,
        fee_provider=fee_provider,
        cost=selected_cost,
        sizing=selected_sizing,
        status="preflight_ready" if private_state_ok else "preflight_blocked_private_state",
    )
    signing_ready = (
        not signing_missing
        and key_parse_status == "ok"
        and key_matches_signer in {"True", "skipped_missing_api_wallet_address"}
        and importlib.util.find_spec("eth_account") is not None
    )
    live_ready = private_state_ok and signing_ready
    print("live_execution_ready=requires_future_hyperliquid_live_confirmation" if live_ready else "live_execution_ready=False")
    print(f"preflight_ready={private_state_ok}")


def _market_configs(plan: dict[str, object]) -> list[dict[str, object]]:
    markets = plan.get("markets")
    if not isinstance(markets, list):
        return []
    return [item for item in markets if isinstance(item, dict)]


def _min_order_size_by_coin(plan: dict[str, object]) -> dict[str, Decimal | None]:
    result: dict[str, Decimal | None] = {}
    for item in _market_configs(plan):
        coin = str(item.get("coin") or item.get("market") or "")
        if not coin:
            continue
        raw = item.get("min_order_size_usd")
        result[coin] = Decimal(str(raw)) if raw not in (None, "") else None
    return result


def _candidate_from_live_orderbook(
    *,
    api_endpoint: str,
    market_config: dict[str, object],
    market_info_by_coin: dict[str, HyperliquidMarketInfo],
    timeout_seconds: float,
    dex: str,
    default_max_spread_bps: Decimal,
) -> HyperliquidMarketCandidate:
    coin = str(market_config.get("coin") or market_config.get("market") or "")
    display_market = str(market_config.get("display_market") or f"{coin}-PERP")
    max_spread_bps = Decimal(str(market_config.get("max_spread_bps", default_max_spread_bps)))
    average_spread_bps = Decimal(str(market_config.get("average_spread_bps", max_spread_bps)))
    market_info = market_info_by_coin.get(
        coin,
        HyperliquidMarketInfo(
            coin=coin or "unknown",
            asset_id=-1,
            sz_decimals=0,
            lot_size=Decimal("0"),
            price_decimal_places=0,
            min_order_size_usd=None,
        ),
    )
    if not coin:
        return _empty_candidate("unknown", display_market, market_info, average_spread_bps, max_spread_bps, "missing_coin")
    if market_info.asset_id < 0:
        return _empty_candidate(coin, display_market, market_info, average_spread_bps, max_spread_bps, "market_not_found_in_meta")
    if not market_info.min_order_known:
        return _empty_candidate(coin, display_market, market_info, average_spread_bps, max_spread_bps, "min_order_size_unknown")

    result = fetch_hyperliquid_rest_top_of_book(
        api_endpoint,
        coin=coin,
        timeout_seconds=timeout_seconds,
        dex=dex,
    )
    if not result.ok or result.snapshot is None:
        return _empty_candidate(coin, display_market, market_info, average_spread_bps, max_spread_bps, result.reason)
    snapshot = result.snapshot
    eligible = snapshot.spread_bps <= average_spread_bps and snapshot.spread_bps <= max_spread_bps
    if snapshot.spread_bps > average_spread_bps:
        reason = f"spread_above_average:{snapshot.spread_bps:.4f}>{average_spread_bps:.4f}"
    elif snapshot.spread_bps > max_spread_bps:
        reason = f"spread_above_hard_cap:{snapshot.spread_bps:.4f}>{max_spread_bps:.4f}"
    else:
        reason = "spread_ok"
    return HyperliquidMarketCandidate(
        coin=coin,
        display_market=display_market,
        market_info=market_info,
        average_spread_bps=average_spread_bps,
        max_spread_bps=max_spread_bps,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        best_bid_size=snapshot.best_bid_size,
        best_ask_size=snapshot.best_ask_size,
        live_spread_bps=snapshot.spread_bps,
        eligible=eligible,
        reason=reason,
    )


def _empty_candidate(
    coin: str,
    display_market: str,
    market_info: HyperliquidMarketInfo,
    average_spread_bps: Decimal,
    max_spread_bps: Decimal,
    reason: str,
) -> HyperliquidMarketCandidate:
    return HyperliquidMarketCandidate(
        coin=coin,
        display_market=display_market,
        market_info=market_info,
        average_spread_bps=average_spread_bps,
        max_spread_bps=max_spread_bps,
        best_bid=Decimal("0"),
        best_ask=Decimal("0"),
        best_bid_size=Decimal("0"),
        best_ask_size=Decimal("0"),
        live_spread_bps=Decimal("999999"),
        eligible=False,
        reason=reason,
    )


def _candidate_cost(candidate: HyperliquidMarketCandidate, fee_provider: HyperliquidFeeProvider) -> MarketCostResult:
    return calculate_market_cost(
        MarketCostInput(
            exchange_id="hyperliquid",
            market=candidate.coin,
            live_spread_bps=candidate.live_spread_bps,
            fee=fee_provider.fee_for_market(candidate.coin),
        )
    )


def _blocked_cost(candidate: HyperliquidMarketCandidate, fee_provider: HyperliquidFeeProvider) -> MarketCostResult:
    return calculate_market_cost(
        MarketCostInput(
            exchange_id="hyperliquid",
            market=candidate.coin,
            live_spread_bps=candidate.live_spread_bps,
            fee=fee_provider.fee_for_market(candidate.coin),
        )
    )


def _candidate_sizing(
    candidate: HyperliquidMarketCandidate,
    max_notional: Decimal,
    remaining_gross_volume_usd: Decimal,
    level_size_fraction: Decimal,
) -> SizingResult:
    return calculate_sizing(
        SizingInput(
            exchange_id="hyperliquid",
            market=candidate.coin,
            best_bid=candidate.best_bid,
            best_ask=candidate.best_ask,
            best_bid_size=candidate.best_bid_size,
            best_ask_size=candidate.best_ask_size,
            order_notional_usd=max_notional,
            remaining_gross_volume_usd=remaining_gross_volume_usd,
            level_size_fraction=level_size_fraction,
            lot_size=candidate.market_info.lot_size,
            min_order_size_usd=candidate.market_info.min_order_size_usd or Decimal("999999999"),
        )
    )


def _print_candidate(
    candidate: HyperliquidMarketCandidate,
    cost: MarketCostResult,
    sizing: SizingResult,
) -> None:
    print(f"market={candidate.display_market}")
    print(f"  coin={candidate.coin}")
    print(f"  asset_id={candidate.market_info.asset_id}")
    print(f"  sz_decimals={candidate.market_info.sz_decimals}")
    print(f"  lot_size={fmt_decimal(candidate.market_info.lot_size)}")
    print(f"  price_decimal_places={candidate.market_info.price_decimal_places}")
    print(f"  min_order_size_usd={fmt_optional_decimal(candidate.market_info.min_order_size_usd)}")
    print(f"  average_spread_bps={fmt_decimal(candidate.average_spread_bps)}")
    print(f"  max_spread_bps={fmt_decimal(candidate.max_spread_bps)}")
    print(f"  live_best_bid={fmt_decimal(candidate.best_bid)}")
    print(f"  live_best_ask={fmt_decimal(candidate.best_ask)}")
    print(f"  live_best_bid_size={fmt_decimal(candidate.best_bid_size)}")
    print(f"  live_best_ask_size={fmt_decimal(candidate.best_ask_size)}")
    print(f"  live_spread_bps={candidate.live_spread_bps:.4f}")
    print(f"  entry_fee_bps={cost.entry_fee_bps if cost.fee_known else 'unknown'}")
    print(f"  exit_fee_bps={cost.exit_fee_bps if cost.fee_known else 'unknown'}")
    print(f"  slippage_buffer_bps={cost.slippage_buffer_bps}")
    print(f"  fee_source={cost.fee_source}")
    print(f"  expected_loss_bps={cost.expected_loss_bps if cost.fee_known else 'unknown'}")
    print(f"  planned_size={fmt_decimal(sizing.amount)}")
    print(f"  planned_gross_volume_usd={fmt_decimal(sizing.planned_gross_volume_usd)}")
    print(f"  eligible={candidate.eligible and cost.eligible and sizing.eligible}")
    if not candidate.eligible:
        reason = candidate.reason
    elif not cost.eligible:
        reason = cost.reason
    else:
        reason = sizing.reason
    print(f"  reason={reason}")


def _load_account_fee_or_none(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
    private_readonly_ready: bool,
) -> HyperliquidAccountFee | None:
    if not private_readonly_ready:
        print("account_fee_private_readonly_skipped=missing_account_address_env")
        return None
    account = read_hyperliquid_private_readonly_params(credential_prefix, environment)["user"]
    try:
        account_fee = load_hyperliquid_account_fee(api_endpoint, account, timeout_seconds)
    except Exception as exc:
        print("account_fee_private_readonly_ok=False")
        print(f"account_fee_error_type={exc.__class__.__name__}")
        return None

    print("account_fee_private_readonly_ok=True")
    print(f"account_fee_level={account_fee.fee_level or 'unknown'}")
    print(f"account_maker_fee_bps={account_fee.maker_fee_bps}")
    print(f"account_taker_fee_bps={account_fee.taker_fee_bps}")
    return account_fee


def _print_private_state(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    dex: str,
    timeout_seconds: float,
    private_readonly_ready: bool,
    allow_existing_position: bool,
    allow_open_orders: bool,
) -> bool:
    if not private_readonly_ready:
        print("private_state_ok=False")
        print("private_readonly_skipped=missing_account_address_env")
        print("position_check=skipped_missing_account")
        print("open_order_check=skipped_missing_account")
        return False

    user = read_hyperliquid_private_readonly_params(credential_prefix, environment)["user"]
    state_body: dict[str, object] = {"type": "clearinghouseState", "user": user}
    orders_body: dict[str, object] = {"type": "openOrders", "user": user}
    if dex:
        state_body["dex"] = dex
        orders_body["dex"] = dex
    try:
        state = info_post_json(api_endpoint, state_body, timeout_seconds, private_readonly=True)
        open_orders = info_post_json(api_endpoint, orders_body, timeout_seconds, private_readonly=True)
    except (TimeoutError, OSError, ValueError, HyperliquidReadonlyConfigError) as exc:
        print("private_state_ok=False")
        print(f"private_state_error={exc.__class__.__name__}")
        return False

    position_count = _position_count(state)
    open_order_count = len(open_orders) if isinstance(open_orders, list) else 0
    print("private_state_ok=True")
    print(f"position_count={position_count}")
    print(f"open_order_count={open_order_count}")
    position_ok = position_count == 0 or allow_existing_position
    orders_ok = open_order_count == 0 or allow_open_orders
    print("position_check=ok" if position_ok else "position_check=blocked_existing_position")
    print("open_order_check=ok" if orders_ok else "open_order_check=blocked_existing_open_orders")
    return position_ok and orders_ok


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


def _emit_hyperliquid_execution_event(
    *,
    account_label: str,
    environment: str,
    market: str,
    fee_provider: HyperliquidFeeProvider,
    cost: MarketCostResult,
    sizing: SizingResult,
    status: str,
) -> None:
    account_fee = fee_provider.account_fee
    override = fee_provider.override_by_market.get(cost.market)
    estimated_fee = estimate_roundtrip_fee_usd(
        entry_notional_usd=sizing.entry_notional_usd,
        exit_notional_usd=sizing.exit_notional_usd,
        entry_fee_bps=cost.entry_fee_bps if cost.fee_known else None,
        exit_fee_bps=cost.exit_fee_bps if cost.fee_known else None,
    )
    emit_execution_event(
        ExecutionEvent(
            exchange="hyperliquid",
            account_label=account_label,
            wallet_label="api_wallet",
            market=market,
            cycle_id="preflight",
            environment=environment,
            fee_level=account_fee.fee_level if account_fee is not None else None,
            maker_fee_bps=account_fee.maker_fee_bps if account_fee is not None else None,
            taker_fee_bps=account_fee.taker_fee_bps if account_fee is not None else None,
            entry_fee_bps=cost.entry_fee_bps if cost.fee_known else None,
            exit_fee_bps=cost.exit_fee_bps if cost.fee_known else None,
            fee_source=cost.fee_source,
            fee_multiplier=override.fee_multiplier if override is not None else Decimal("1"),
            fee_multiplier_expires_at=(
                override.fee_multiplier_expires_at.isoformat()
                if override is not None and override.fee_multiplier_expires_at is not None
                else None
            ),
            live_spread_bps=cost.live_spread_bps,
            expected_loss_bps=cost.expected_loss_bps if cost.fee_known else None,
            planned_gross_volume_usd=sizing.planned_gross_volume_usd,
            filled_gross_volume_usd=None,
            estimated_fee_usd=estimated_fee,
            estimated_loss_usd=estimate_loss_usd(
                planned_gross_volume_usd=sizing.planned_gross_volume_usd,
                expected_loss_bps=cost.expected_loss_bps if cost.fee_known else None,
            ),
            realized_pnl_usd=None,
            points_estimate=None,
            start_position_count=None,
            final_position_count=None,
            start_open_order_count=None,
            final_open_order_count=None,
            order_ids=(),
            error_reason=None,
            status=status,
        )
    )


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


def fmt_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return fmt_decimal(value)


def fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
