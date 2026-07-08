from __future__ import annotations

import argparse
import importlib.util
import json
import re
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Literal

from perpdex_farming_bot.cli.hotstuff_live_preflight import (
    MarketCandidate,
    _candidate_from_live_orderbook,
    _load_instrument_map,
)
from perpdex_farming_bot.connectors.hotstuff_readonly import (
    api_endpoint_env_name,
    default_api_endpoint,
    default_wss_endpoint,
    endpoint_from_env,
    info_post_json,
    normalize_hotstuff_environment,
    validate_https_base_url,
    validate_wss_url,
    wss_endpoint_env_name,
)
from perpdex_farming_bot.core.execution_cost import MarketCostInput, MarketCostResult, calculate_market_cost
from perpdex_farming_bot.core.execution_event import (
    ExecutionEvent,
    emit_execution_event,
    estimate_loss_usd,
    estimate_roundtrip_fee_usd,
)
from perpdex_farming_bot.core.live_volume import RoundtripPlan, VolumeRunConfig, run_paired_volume
from perpdex_farming_bot.credentials import (
    hotstuff_available_private_readonly_env,
    hotstuff_signing_missing,
    read_hotstuff_credentials,
    read_hotstuff_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present
from perpdex_farming_bot.exchanges.hotstuff import HotstuffAdapter, ensure_hotstuff_sdk_compat
from perpdex_farming_bot.exchanges.hotstuff_fees import (
    HotstuffAccountFee,
    HotstuffFeeProvider,
    hotstuff_fee_overrides_from_plan,
    hotstuff_market_fee_metadata_from_instruments,
    load_hotstuff_account_fee,
)
from perpdex_farming_bot.marketdata import MarketSpec, RestBackoff, SpreadCache, select_lowest_spread
from perpdex_farming_bot.marketdata.hotstuff import refresh_hotstuff_spread_cache


CONFIRM_TEXT = "LIVE_HOTSTUFF_100_USD_TO_1000"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Hotstuff mainnet live test. Re-checks spreads before every cycle and only sends "
            "orders with --execute-live plus the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hotstuff.live-test.json")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="HOTSTUFF")
    parser.add_argument("--market", default="", help="Optional single Hotstuff market filter, e.g. BTC-PERP.")
    parser.add_argument("--order-notional-usd", type=Decimal, default=None)
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=None)
    parser.add_argument("--level-size-fraction", type=Decimal, default=None)
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-spread-bps", type=Decimal, default=None)
    parser.add_argument(
        "--ignore-24h-spread-gate",
        action="store_true",
        help="Use only --max-spread-bps as the hard spread gate. Default keeps the 24h average gate.",
    )
    parser.add_argument("--min-entry-delay-seconds", type=float, default=None)
    parser.add_argument(
        "--loop-delay-seconds",
        type=float,
        default=1.0,
        help="Delay between completed cycles for fast-close mode.",
    )
    parser.add_argument("--monitor-source", choices=("auto", "websocket", "rest"), default="auto")
    parser.add_argument("--monitor-cache-max-age-seconds", type=float, default=2.0)
    parser.add_argument("--websocket-snapshot-timeout-seconds", type=float, default=0.8)
    parser.add_argument("--rest-poll-min-interval-seconds", type=float, default=0.1)
    parser.add_argument("--rate-limit-backoff-seconds", type=float, default=5.0)
    parser.add_argument("--reduce-only-settle-attempts", type=int, default=5)
    parser.add_argument("--reduce-only-settle-delay-seconds", type=float, default=0.5)
    parser.add_argument("--reduce-only-slippage-bps", type=Decimal, default=Decimal("100"))
    parser.add_argument(
        "--fast-close-on-fill",
        action="store_true",
        help="Submit a reduce-only close immediately after an entry fill response, without position REST polling.",
    )
    parser.add_argument(
        "--prebuild-close-order",
        action="store_true",
        help="Build the reduce-only close request before entry submission. Requires --fast-close-on-fill.",
    )
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.market:
        plan = _filter_plan_markets(plan, args.market)
    environment = normalize_hotstuff_environment(args.environment or str(plan.get("environment", "production")))
    api_name = api_endpoint_env_name(environment)
    api_endpoint = validate_https_base_url(
        api_name,
        endpoint_from_env(get_env(api_name), default_api_endpoint(environment)),
    )
    wss_name = wss_endpoint_env_name(environment)
    wss_endpoint = validate_wss_url(
        wss_name,
        endpoint_from_env(get_env(wss_name), default_wss_endpoint(environment)),
    )
    order_notional = args.order_notional_usd or Decimal(str(plan["order_notional_usd"]))
    target_gross_volume = args.target_gross_volume_usd or Decimal(str(plan["target_gross_volume_usd"]))
    max_spread_bps = args.max_spread_bps or Decimal(str(plan["max_spread_bps"]))
    if args.ignore_24h_spread_gate:
        plan = _override_plan_24h_spread_gate(plan, max_spread_bps)
    level_size_fraction = args.level_size_fraction or Decimal(str(plan.get("level_size_fraction", "0.5")))
    min_entry_delay_seconds = (
        args.min_entry_delay_seconds
        if args.min_entry_delay_seconds is not None
        else float(plan["min_entry_delay_seconds"])
    )

    _validate_live_args(order_notional, target_gross_volume, args.max_cycles, min_entry_delay_seconds)
    _validate_level_size_fraction(level_size_fraction)
    _validate_fast_close_args(args)

    print("hotstuff_live_test=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"api_endpoint={api_endpoint}")
    print(f"wss_endpoint={wss_endpoint}")
    print(f"config={args.config}")
    print(f"credential_prefix={args.credential_prefix}")
    print(f"execute_live={args.execute_live}")
    print(f"order_notional_usd={order_notional}")
    print(f"target_gross_volume_usd={target_gross_volume}")
    print(f"max_spread_bps={max_spread_bps}")
    print(f"ignore_24h_spread_gate={args.ignore_24h_spread_gate}")
    print(f"level_size_fraction={level_size_fraction}")
    print(
        "order_sizing_rule="
        "min(order_notional_usd, remaining_gross_volume_usd/2, smaller_best_bid_or_ask_level_notional*level_size_fraction)"
    )
    print(f"min_entry_delay_seconds={min_entry_delay_seconds}")
    print(f"loop_delay_seconds={args.loop_delay_seconds}")
    print(f"fast_close_on_fill={args.fast_close_on_fill}")
    print(f"prebuild_close_order={args.prebuild_close_order}")
    print(f"roundtrip_mode={'fast-reduce-only' if args.fast_close_on_fill else 'netting'}")
    print(f"monitor_source={args.monitor_source}")
    print(f"monitor_cache_max_age_seconds={args.monitor_cache_max_age_seconds}")
    print(f"websocket_snapshot_timeout_seconds={args.websocket_snapshot_timeout_seconds}")
    print("fresh_orderbook_verify=selected_market_only_before_order")
    print("market_selection=lowest_expected_loss_bps_then_live_spread_bps")
    print("latency_logging=plan,entry_sign,close_sign_or_prebuild,entry_post,entry_to_close_submit_gap,close_post,cycle_total")
    print("hotstuff_fast_close_signing_model=sdk_signs_inside_place_order_post; sign_latency_is_request_build_latency")
    print("orders_require_confirmation=True")
    print(f"required_confirmation={CONFIRM_TEXT}")

    account_env = hotstuff_available_private_readonly_env(args.credential_prefix, environment)
    print(f"private_readonly_env_ready={account_env is not None}")
    if account_env is None:
        print("live_ready=False")
        print("reason=missing_account_address_env")
        return

    credentials = read_hotstuff_credentials(args.credential_prefix, environment)
    signing_missing = hotstuff_signing_missing(args.credential_prefix, environment)
    print(f"signing_env_ready={not signing_missing}")
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))
        print("live_ready=False")
        print("reason=missing_signing_env")
        return

    sdk_installed = importlib.util.find_spec("hotstuff") is not None and importlib.util.find_spec("eth_account") is not None
    print(f"sdk_installed={sdk_installed}")
    print("sdk_package=hotstuff-python-sdk")

    account_ok = _account_summary_ok(api_endpoint, args.credential_prefix, environment, args.timeout_seconds)
    print(f"account_summary_private_readonly_ok={account_ok}")
    if not account_ok:
        print("live_ready=False")
        print("reason=account_summary_readonly_failed")
        return

    start_positions = _positions(api_endpoint, args.credential_prefix, environment, args.timeout_seconds)
    print(f"start_position_count={len(start_positions)}")
    if start_positions:
        print("live_ready=False")
        print("reason=existing_positions_detected")
        print("existing_position_markets=" + ",".join(sorted(_position_market(item) for item in start_positions)))
        return
    start_open_orders = _open_orders(api_endpoint, args.credential_prefix, environment, args.timeout_seconds)
    print(f"start_open_order_count={len(start_open_orders)}")
    if start_open_orders:
        print("live_ready=False")
        print("reason=existing_open_orders_detected")
        return

    instruments = _load_instrument_map(api_endpoint, args.timeout_seconds)
    print(f"instrument_count={len(instruments)}")
    fee_provider = _load_hotstuff_fee_provider(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        plan=plan,
        instruments=instruments,
        timeout_seconds=args.timeout_seconds,
        private_readonly_ready=True,
    )

    selected = _select_candidate(api_endpoint, plan, instruments, max_spread_bps, args.timeout_seconds, fee_provider)
    if selected is None:
        print("live_ready=False")
        print("reason=no_eligible_market")
        return
    selected_cost = _candidate_cost(selected, fee_provider)

    print(f"selected_market={selected.market}")
    print(f"selected_instrument_id={selected.instrument_id}")
    print(f"selected_live_spread_bps={selected.live_spread_bps:.4f}")
    print(f"selected_24h_threshold_bps={selected.provided_24h_spread_bps}")
    print(f"selected_entry_fee_bps={selected_cost.entry_fee_bps}")
    print(f"selected_exit_fee_bps={selected_cost.exit_fee_bps}")
    print(f"selected_slippage_buffer_bps={selected_cost.slippage_buffer_bps}")
    print(f"selected_fee_source={selected_cost.fee_source}")
    print(f"selected_expected_loss_bps={selected_cost.expected_loss_bps:.4f}")
    order_plan = _roundtrip_order_plan(
        selected,
        order_notional,
        target_gross_volume,
        level_size_fraction,
    )
    buy_qty = order_plan["buy_qty"]
    buy_price = order_plan["buy_price"]
    sell_qty = order_plan["sell_qty"]
    sell_price = order_plan["sell_price"]
    planned_gross = order_plan["planned_gross"]
    if args.fast_close_on_fill:
        matched_qty = min(buy_qty, sell_qty)
        buy_qty = matched_qty
        sell_qty = matched_qty
        planned_gross = (matched_qty * buy_price) + (matched_qty * sell_price)
    print(f"planned_per_side_cap_usd={order_plan['per_side_cap']:.4f}")
    print(f"planned_buy_size={_fmt_decimal(buy_qty)}")
    print(f"planned_sell_size={_fmt_decimal(sell_qty)}")
    print(f"planned_buy_price={_fmt_decimal(buy_price)}")
    print(f"planned_sell_price={_fmt_decimal(sell_price)}")
    print(f"planned_cycle_gross_volume_usd={planned_gross:.4f}")
    print(f"planned_cycles_to_target={_ceil_decimal(target_gross_volume / planned_gross) if planned_gross > 0 else 0}")
    _emit_hotstuff_execution_event(
        account_label=args.credential_prefix,
        environment=environment,
        market=selected.market,
        cycle_id="dry_run_plan",
        fee_provider=fee_provider,
        cost=selected_cost,
        entry_notional_usd=buy_qty * buy_price,
        exit_notional_usd=sell_qty * sell_price,
        planned_gross_volume_usd=planned_gross,
        start_position_count=len(start_positions),
        final_position_count=len(start_positions),
        start_open_order_count=len(start_open_orders),
        final_open_order_count=len(start_open_orders),
        status="dry_run_plan_ready",
    )

    if buy_qty <= 0 or sell_qty <= 0:
        print("live_ready=False")
        print("reason=quantity_zero")
        return
    if buy_qty * buy_price < selected.min_notional_usd or sell_qty * sell_price < selected.min_notional_usd:
        print("live_ready=False")
        print("reason=below_min_notional")
        return
    if args.fast_close_on_fill:
        _dry_run_fast_close_build(
            selected=selected,
            entry_price=buy_price,
            entry_size=buy_qty,
            close_size=buy_qty,
            args=args,
            sdk_installed=sdk_installed,
        )

    if not args.execute_live:
        print("live_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if not sdk_installed:
        print("live_ready=False")
        print("reason=hotstuff_python_sdk_not_installed_for_this_python")
        print("hint=use_the_bundled_python_where_hotstuff_python_sdk_was_installed")
        return
    if not credentials["signer_address"]:
        print("live_ready=False")
        print("reason=missing_signer_address_env_for_api_wallet_live_test")
        return
    if credentials["account_address"].casefold() == credentials["signer_address"].casefold():
        print("live_ready=False")
        print("reason=account_address_must_be_owner_not_signer_for_api_wallet_live_test")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    if args.fast_close_on_fill:
        _execute_fast_close_loop(
            args=args,
            plan=plan,
            api_endpoint=api_endpoint,
            wss_endpoint=wss_endpoint,
            environment=environment,
            order_notional=order_notional,
            target_gross_volume=target_gross_volume,
            max_spread_bps=max_spread_bps,
            level_size_fraction=level_size_fraction,
        )
        return

    _execute_live_loop(
        args=args,
        plan=plan,
        api_endpoint=api_endpoint,
        wss_endpoint=wss_endpoint,
        environment=environment,
        credentials=credentials,
        order_notional=order_notional,
        target_gross_volume=target_gross_volume,
        max_spread_bps=max_spread_bps,
        level_size_fraction=level_size_fraction,
        min_entry_delay_seconds=min_entry_delay_seconds,
    )


def _validate_live_args(
    order_notional: Decimal,
    target_gross_volume: Decimal,
    max_cycles: int,
    min_entry_delay_seconds: float,
) -> None:
    if order_notional <= 0:
        raise SystemExit("--order-notional-usd must be greater than zero")
    if order_notional > Decimal("100"):
        raise SystemExit("--order-notional-usd must be <= 100 for this guarded live test")
    if target_gross_volume <= 0:
        raise SystemExit("--target-gross-volume-usd must be greater than zero")
    if target_gross_volume > Decimal("1000"):
        raise SystemExit("--target-gross-volume-usd must be <= 1000 for this guarded live test")
    if max_cycles <= 0:
        raise SystemExit("--max-cycles must be greater than zero")
    if max_cycles > 20:
        raise SystemExit("--max-cycles must be <= 20 for this guarded live test")
    if min_entry_delay_seconds < 1:
        raise SystemExit("--min-entry-delay-seconds must be at least 1")


def _validate_level_size_fraction(level_size_fraction: Decimal) -> None:
    if level_size_fraction <= 0 or level_size_fraction > 1:
        raise SystemExit("level_size_fraction must be greater than 0 and <= 1")


def _validate_fast_close_args(args: argparse.Namespace) -> None:
    if args.loop_delay_seconds < 0 or args.loop_delay_seconds > 10:
        raise SystemExit("--loop-delay-seconds must be between 0 and 10")
    if args.prebuild_close_order and not args.fast_close_on_fill:
        raise SystemExit("--prebuild-close-order requires --fast-close-on-fill")


def _filter_plan_markets(plan: dict[str, object], market: str) -> dict[str, object]:
    target = market.upper()
    markets = [item for item in plan["markets"] if isinstance(item, dict) and str(item.get("market", "")).upper() == target]
    if not markets:
        raise SystemExit(f"--market {target} was not found in config markets")
    filtered = dict(plan)
    filtered["markets"] = markets
    return filtered


def _override_plan_24h_spread_gate(plan: dict[str, object], max_spread_bps: Decimal) -> dict[str, object]:
    filtered = dict(plan)
    markets: list[dict[str, object]] = []
    for item in plan["markets"]:
        if not isinstance(item, dict):
            continue
        market = dict(item)
        market["provided_24h_spread_bps"] = str(max_spread_bps)
        markets.append(market)
    filtered["markets"] = markets
    return filtered


def _load_hotstuff_fee_provider(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    plan: dict[str, object],
    instruments: dict[str, dict[str, object]],
    timeout_seconds: float,
    private_readonly_ready: bool,
) -> HotstuffFeeProvider:
    overrides = hotstuff_fee_overrides_from_plan(plan)
    metadata = hotstuff_market_fee_metadata_from_instruments(instruments)
    account_fee = _load_account_fee_or_none(
        api_endpoint=api_endpoint,
        credential_prefix=credential_prefix,
        environment=environment,
        timeout_seconds=timeout_seconds,
        private_readonly_ready=private_readonly_ready,
    )
    complete_overrides = [
        item
        for item in overrides.values()
        if item.entry_fee_bps is not None and item.exit_fee_bps is not None
    ]
    multiplier_overrides = [item for item in overrides.values() if item.fee_multiplier != Decimal("1")]
    print(f"fee_provider=hotstuff")
    print(f"fee_market_metadata_count={len(metadata)}")
    print(f"fee_config_exact_override_count={len(complete_overrides)}")
    print(f"fee_config_multiplier_count={len(multiplier_overrides)}")
    print("fee_unknown_policy=block")
    return HotstuffFeeProvider(
        metadata_by_market=metadata,
        override_by_market=overrides,
        account_fee=account_fee,
    )


def _load_account_fee_or_none(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
    private_readonly_ready: bool,
) -> HotstuffAccountFee | None:
    if not private_readonly_ready:
        print("account_fee_private_readonly_skipped=missing_account_address_env")
        return None
    try:
        account_fee = load_hotstuff_account_fee(api_endpoint, credential_prefix, environment, timeout_seconds)
    except Exception as exc:
        print("account_fee_private_readonly_ok=False")
        print(f"account_fee_error_type={exc.__class__.__name__}")
        return None

    print("account_fee_private_readonly_ok=True")
    print(f"account_fee_level={account_fee.fee_level or 'unknown'}")
    print(f"account_maker_fee_bps={account_fee.maker_fee_bps}")
    print(f"account_taker_fee_bps={account_fee.taker_fee_bps}")
    return account_fee


def _candidate_cost(candidate: MarketCandidate, fee_provider: HotstuffFeeProvider) -> MarketCostResult:
    return calculate_market_cost(
        MarketCostInput(
            exchange_id="hotstuff",
            market=candidate.market,
            live_spread_bps=candidate.live_spread_bps,
            fee=fee_provider.fee_for_market(candidate.market),
        )
    )


def _emit_hotstuff_execution_event(
    *,
    account_label: str,
    environment: str,
    market: str,
    cycle_id: str,
    fee_provider: HotstuffFeeProvider,
    cost: MarketCostResult,
    entry_notional_usd: Decimal,
    exit_notional_usd: Decimal,
    planned_gross_volume_usd: Decimal,
    start_position_count: int | None,
    final_position_count: int | None,
    start_open_order_count: int | None,
    final_open_order_count: int | None,
    status: str,
    filled_gross_volume_usd: Decimal | None = None,
    final_all_flat: bool | None = None,
    plan_latency_ms: Decimal | str | None = None,
    entry_sign_latency_ms: Decimal | str | None = None,
    close_sign_latency_ms: Decimal | str | None = None,
    close_prebuild_sign_latency_ms: Decimal | str | None = None,
    entry_post_latency_ms: Decimal | str | None = None,
    close_post_latency_ms: Decimal | str | None = None,
    entry_to_close_submit_gap_ms: Decimal | str | None = None,
    cycle_total_latency_ms: Decimal | str | None = None,
    order_ids: tuple[str, ...] = (),
    error_reason: str | None = None,
) -> None:
    account_fee = fee_provider.account_fee
    override = fee_provider.override_by_market.get(market)
    estimated_fee = estimate_roundtrip_fee_usd(
        entry_notional_usd=entry_notional_usd,
        exit_notional_usd=exit_notional_usd,
        entry_fee_bps=cost.entry_fee_bps if cost.fee_known else None,
        exit_fee_bps=cost.exit_fee_bps if cost.fee_known else None,
    )
    event = ExecutionEvent(
        exchange="hotstuff",
        account_label=account_label,
        wallet_label=None,
        market=market,
        cycle_id=cycle_id,
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
        planned_gross_volume_usd=planned_gross_volume_usd,
        filled_gross_volume_usd=filled_gross_volume_usd,
        estimated_fee_usd=estimated_fee,
        estimated_loss_usd=estimate_loss_usd(
            planned_gross_volume_usd=planned_gross_volume_usd,
            expected_loss_bps=cost.expected_loss_bps if cost.fee_known else None,
        ),
        realized_pnl_usd=None,
        points_estimate=None,
        start_position_count=start_position_count,
        final_position_count=final_position_count,
        start_open_order_count=start_open_order_count,
        final_open_order_count=final_open_order_count,
        final_all_flat=final_all_flat,
        plan_latency_ms=_optional_decimal(plan_latency_ms),
        entry_sign_latency_ms=_optional_decimal(entry_sign_latency_ms),
        close_sign_latency_ms=_optional_decimal(close_sign_latency_ms),
        close_prebuild_sign_latency_ms=_optional_decimal(close_prebuild_sign_latency_ms),
        entry_post_latency_ms=_optional_decimal(entry_post_latency_ms),
        close_post_latency_ms=_optional_decimal(close_post_latency_ms),
        entry_to_close_submit_gap_ms=_optional_decimal(entry_to_close_submit_gap_ms),
        cycle_total_latency_ms=_optional_decimal(cycle_total_latency_ms),
        order_ids=order_ids,
        error_reason=error_reason,
        status=status,
    )
    emit_execution_event(event)


def _select_candidate(
    api_endpoint: str,
    plan: dict[str, object],
    instruments: dict[str, dict[str, object]],
    max_spread_bps: Decimal,
    timeout_seconds: float,
    fee_provider: HotstuffFeeProvider,
) -> MarketCandidate | None:
    candidates = [
        _candidate_from_live_orderbook(api_endpoint, market, instruments, max_spread_bps, timeout_seconds)
        for market in plan["markets"]
    ]
    eligible: list[tuple[MarketCandidate, MarketCostResult]] = []
    print(f"candidate_market_count={len(candidates)}")
    for candidate in candidates:
        cost = _candidate_cost(candidate, fee_provider) if candidate.eligible else None
        cost_eligible = cost.eligible if cost is not None else False
        expected_loss = f"{cost.expected_loss_bps:.4f}" if cost is not None else "999999.0000"
        fee_source = cost.fee_source if cost is not None else "not_checked"
        reason = candidate.reason if not candidate.eligible else (cost.reason if cost is not None and not cost.eligible else candidate.reason)
        if cost is not None and cost_eligible:
            eligible.append((candidate, cost))
        print(
            f"candidate={candidate.market} eligible={candidate.eligible and cost_eligible} "
            f"spread_bps={candidate.live_spread_bps:.4f} expected_loss_bps={expected_loss} "
            f"fee_source={fee_source} reason={reason}"
        )
    print(f"eligible_market_count={len(eligible)}")
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (item[1].expected_loss_bps, item[0].live_spread_bps, item[0].market),
    )[0]


def _execute_live_loop(
    *,
    args: argparse.Namespace,
    plan: dict[str, object],
    api_endpoint: str,
    wss_endpoint: str,
    environment: str,
    credentials: dict[str, str],
    order_notional: Decimal,
    target_gross_volume: Decimal,
    max_spread_bps: Decimal,
    level_size_fraction: Decimal,
    min_entry_delay_seconds: float,
) -> None:
    adapter = HotstuffAdapter(
        api_endpoint,
        args.credential_prefix,
        environment,
        args.timeout_seconds,
    )
    signer_ready, signer_reason = adapter.signer_ready()
    print(f"signer_ready={signer_ready}")
    print(f"signer_status={signer_reason}")
    if not signer_ready:
        print(f"live_aborted={signer_reason}")
        return

    print("live_loop_start=True")
    print("live_volume_accounting=planned_buy_notional_plus_planned_sell_notional")
    print("live_market_monitor=websocket_cache_with_rest_backup")

    instruments = _load_instrument_map(api_endpoint, args.timeout_seconds)
    fee_provider = _load_hotstuff_fee_provider(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        plan=plan,
        instruments=instruments,
        timeout_seconds=args.timeout_seconds,
        private_readonly_ready=True,
    )
    specs = _hotstuff_market_specs(plan, max_spread_bps)
    cache = SpreadCache()
    rest_backoff = RestBackoff(
        min_interval_seconds=args.rest_poll_min_interval_seconds,
        default_backoff_seconds=args.rate_limit_backoff_seconds,
    )

    def select_plan(remaining_gross_volume_usd: Decimal) -> RoundtripPlan | None:
        selected = _select_candidate_from_monitor(
            args=args,
            plan=plan,
            api_endpoint=api_endpoint,
            wss_endpoint=wss_endpoint,
            instruments=instruments,
            specs=specs,
            cache=cache,
            rest_backoff=rest_backoff,
            max_spread_bps=max_spread_bps,
            fee_provider=fee_provider,
        )
        if selected is None:
            return None
        selected_cost = _candidate_cost(selected, fee_provider)

        order_plan = _roundtrip_order_plan(
            selected,
            order_notional,
            remaining_gross_volume_usd,
            level_size_fraction,
        )
        buy_qty = order_plan["buy_qty"]
        buy_price = order_plan["buy_price"]
        sell_qty = order_plan["sell_qty"]
        sell_price = order_plan["sell_price"]
        planned_gross = order_plan["planned_gross"]
        print(
            f"selected_market={selected.market} spread_bps={selected.live_spread_bps:.4f} "
            f"expected_loss_bps={selected_cost.expected_loss_bps:.4f} "
            f"fee_source={selected_cost.fee_source} "
            f"per_side_cap_usd={order_plan['per_side_cap']:.4f} "
            f"buy_size={_fmt_decimal(buy_qty)} sell_size={_fmt_decimal(sell_qty)} "
            f"planned_gross_volume_usd={planned_gross:.4f}"
        )
        if buy_qty <= 0 or sell_qty <= 0:
            print("selected_plan_skipped=quantity_zero")
            return None
        if planned_gross <= 0:
            print("selected_plan_skipped=planned_gross_zero")
            return None
        if planned_gross > remaining_gross_volume_usd:
            print(
                "selected_plan_skipped=target_cap_would_be_exceeded:"
                f"{planned_gross:.4f}>{remaining_gross_volume_usd:.4f}"
            )
            return None
        _emit_hotstuff_execution_event(
            account_label=args.credential_prefix,
            environment=environment,
            market=selected.market,
            cycle_id="live_plan",
            fee_provider=fee_provider,
            cost=selected_cost,
            entry_notional_usd=buy_qty * buy_price,
            exit_notional_usd=sell_qty * sell_price,
            planned_gross_volume_usd=planned_gross,
            start_position_count=None,
            final_position_count=None,
            start_open_order_count=None,
            final_open_order_count=None,
            status="live_plan_selected",
        )
        return RoundtripPlan(
            market=selected.market,
            instrument_id=selected.instrument_id,
            buy_price=buy_price,
            sell_price=sell_price,
            buy_size=buy_qty,
            sell_size=sell_qty,
            planned_gross_volume_usd=planned_gross,
            roundtrip_mode="netting",
            reason="lowest_expected_loss_eligible_market",
        )

    result = run_paired_volume(
        adapter=adapter,
        config=VolumeRunConfig(
            target_gross_volume_usd=target_gross_volume,
            max_cycles=args.max_cycles,
            min_entry_delay_seconds=min_entry_delay_seconds,
        ),
        select_plan=select_plan,
    )
    print(f"live_result_status={result.status}")
    print(f"live_result_planned_gross_volume_usd={result.planned_gross_volume_usd:.4f}")
    print(f"live_result_cycles={result.cycles}")


def _execute_fast_close_loop(
    *,
    args: argparse.Namespace,
    plan: dict[str, object],
    api_endpoint: str,
    wss_endpoint: str,
    environment: str,
    order_notional: Decimal,
    target_gross_volume: Decimal,
    max_spread_bps: Decimal,
    level_size_fraction: Decimal,
) -> None:
    adapter = HotstuffAdapter(
        api_endpoint,
        args.credential_prefix,
        environment,
        args.timeout_seconds,
    )
    signer_ready, signer_reason = adapter.signer_ready()
    print(f"signer_ready={signer_ready}")
    print(f"signer_status={signer_reason}")
    if not signer_ready:
        print(f"live_aborted={signer_reason}")
        return

    print("live_loop_start=True")
    print("live_loop_mode=fast_close_on_fill")
    print("live_volume_accounting=estimated_entry_notional_plus_estimated_close_notional")
    print("live_market_monitor=websocket_cache_with_rest_backup")
    print("post_entry_position_check=final_only")
    print("post_entry_open_order_check=final_only")

    instruments = _load_instrument_map(api_endpoint, args.timeout_seconds)
    fee_provider = _load_hotstuff_fee_provider(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        plan=plan,
        instruments=instruments,
        timeout_seconds=args.timeout_seconds,
        private_readonly_ready=True,
    )
    specs = _hotstuff_market_specs(plan, max_spread_bps)
    cache = SpreadCache()
    rest_backoff = RestBackoff(
        min_interval_seconds=args.rest_poll_min_interval_seconds,
        default_backoff_seconds=args.rate_limit_backoff_seconds,
    )

    total = Decimal("0")
    cycle = 0
    while total < target_gross_volume and cycle < args.max_cycles:
        cycle_started_ns = _now_ns()
        plan_started_ns = _now_ns()
        selected = _select_candidate_from_monitor(
            args=args,
            plan=plan,
            api_endpoint=api_endpoint,
            wss_endpoint=wss_endpoint,
            instruments=instruments,
            specs=specs,
            cache=cache,
            rest_backoff=rest_backoff,
            max_spread_bps=max_spread_bps,
            fee_provider=fee_provider,
        )
        plan_latency_ms = _elapsed_ms(plan_started_ns)
        print(f"cycle_{cycle + 1}_plan_latency_ms={plan_latency_ms}")
        print(f"plan_latency_ms={plan_latency_ms}")
        if selected is None:
            print("volume_loop_stopped=no_eligible_market")
            break
        selected_cost = _candidate_cost(selected, fee_provider)

        remaining = target_gross_volume - total
        order_plan = _roundtrip_order_plan(
            selected,
            order_notional,
            remaining,
            level_size_fraction,
        )
        buy_qty = order_plan["buy_qty"]
        sell_qty = order_plan["sell_qty"]
        buy_price = order_plan["buy_price"]
        sell_price = order_plan["sell_price"]
        matched_qty = min(buy_qty, sell_qty)
        buy_qty = matched_qty
        planned_gross = (matched_qty * buy_price) + (matched_qty * sell_price)
        if buy_qty <= 0 or planned_gross <= 0:
            print("volume_loop_stopped=planned_size_zero")
            break
        if planned_gross > remaining:
            print("volume_loop_stopped=target_cap_would_be_exceeded")
            break

        cycle += 1
        print(f"cycle_{cycle}_selected_market={selected.market}")
        print(f"cycle_{cycle}_selected_spread_bps={selected.live_spread_bps:.4f}")
        print(f"cycle_{cycle}_selected_expected_loss_bps={selected_cost.expected_loss_bps:.4f}")
        print(f"cycle_{cycle}_selected_fee_source={selected_cost.fee_source}")
        print(f"cycle_{cycle}_entry_size={_fmt_decimal(buy_qty)}")
        print(f"cycle_{cycle}_entry_price={_fmt_decimal(buy_price)}")
        print(f"cycle_{cycle}_planned_gross_volume_usd={planned_gross:.4f}")
        _emit_hotstuff_execution_event(
            account_label=args.credential_prefix,
            environment=environment,
            market=selected.market,
            cycle_id=f"cycle_{cycle}",
            fee_provider=fee_provider,
            cost=selected_cost,
            entry_notional_usd=buy_qty * buy_price,
            exit_notional_usd=matched_qty * sell_price,
            planned_gross_volume_usd=planned_gross,
            start_position_count=None,
            final_position_count=None,
            start_open_order_count=None,
            final_open_order_count=None,
            plan_latency_ms=plan_latency_ms,
            status="fast_close_plan_selected",
        )

        entry_sign_started_ns = _now_ns()
        try:
            entry_params = _build_entry_order_params(selected, buy_price, buy_qty)
        except ImportError:
            print("volume_loop_stopped=hotstuff_python_sdk_not_installed_for_order_build")
            break
        entry_sign_latency_ms = _elapsed_ms(entry_sign_started_ns)
        print(f"cycle_{cycle}_entry_sign_latency_ms={entry_sign_latency_ms}")
        print(f"entry_sign_latency_ms={entry_sign_latency_ms}")

        prebuilt_close: object | None = None
        prebuilt_close_price = _aggressive_close_price(
            "s",
            selected.best_bid,
            selected.best_ask,
            selected.tick_size,
            args.reduce_only_slippage_bps,
        )
        if args.prebuild_close_order:
            close_prebuild_started_ns = _now_ns()
            try:
                prebuilt_close = _build_close_order_params(selected, prebuilt_close_price, buy_qty)
            except ImportError:
                print("volume_loop_stopped=hotstuff_python_sdk_not_installed_for_close_prebuild")
                break
            close_prebuild_sign_latency_ms = _elapsed_ms(close_prebuild_started_ns)
            print(f"cycle_{cycle}_close_prebuild_sign_latency_ms={close_prebuild_sign_latency_ms}")
            print(f"close_prebuild_sign_latency_ms={close_prebuild_sign_latency_ms}")
            print(f"cycle_{cycle}_close_prebuilt_size={_fmt_decimal(buy_qty)}")
            print(f"cycle_{cycle}_close_prebuilt_price={_fmt_decimal(prebuilt_close_price)}")

        print(f"cycle_{cycle}_entry_submitting=True")
        entry_post_started_ns = _now_ns()
        entry_result = adapter.submit_place_order_params(market=selected.market, params=entry_params)
        entry_post_done_ns = _now_ns()
        entry_post_latency_ms = _elapsed_ms(entry_post_started_ns, entry_post_done_ns)
        print(f"cycle_{cycle}_entry_post_latency_ms={entry_post_latency_ms}")
        print(f"entry_post_latency_ms={entry_post_latency_ms}")
        _print_order_result(f"cycle_{cycle}_entry", entry_result)
        if not entry_result.success:
            print("volume_loop_stopped=entry_order_failed")
            break

        entry_filled_size = _round_down_to_step(entry_result.filled_size, selected.lot_size)
        print(f"cycle_{cycle}_entry_filled_size={_fmt_decimal(entry_filled_size)}")
        if entry_filled_size <= 0:
            print("volume_loop_stopped=entry_not_filled_no_fast_close_submitted")
            break
        if entry_filled_size < buy_qty:
            print(f"cycle_{cycle}_entry_partial_fill=True")
            prebuilt_close = None
        else:
            print(f"cycle_{cycle}_entry_fully_filled=True")

        close_price = prebuilt_close_price
        close_size = entry_filled_size
        if prebuilt_close is not None:
            close_params = prebuilt_close
            print(f"cycle_{cycle}_close_order_prebuilt=True")
        else:
            close_sign_started_ns = _now_ns()
            try:
                close_params = _build_close_order_params(selected, close_price, close_size)
            except ImportError:
                print("manual_review_required=True")
                print("volume_loop_stopped=hotstuff_python_sdk_not_installed_for_close_build")
                break
            close_sign_latency_ms = _elapsed_ms(close_sign_started_ns)
            print(f"cycle_{cycle}_close_sign_latency_ms={close_sign_latency_ms}")
            print(f"close_sign_latency_ms={close_sign_latency_ms}")
            print(f"cycle_{cycle}_close_order_prebuilt=False")

        print(f"cycle_{cycle}_close_size={_fmt_decimal(close_size)}")
        print(f"cycle_{cycle}_close_price={_fmt_decimal(close_price)}")
        print(f"cycle_{cycle}_close_submitting=True")
        entry_to_close_submit_gap_ms = _elapsed_ms(entry_post_done_ns)
        print(f"cycle_{cycle}_entry_to_close_submit_gap_ms={entry_to_close_submit_gap_ms}")
        print(f"entry_to_close_submit_gap_ms={entry_to_close_submit_gap_ms}")
        close_post_started_ns = _now_ns()
        close_result = adapter.submit_place_order_params(market=selected.market, params=close_params)
        close_post_done_ns = _now_ns()
        close_post_latency_ms = _elapsed_ms(close_post_started_ns, close_post_done_ns)
        print(f"cycle_{cycle}_close_post_latency_ms={close_post_latency_ms}")
        print(f"close_post_latency_ms={close_post_latency_ms}")
        _print_order_result(f"cycle_{cycle}_close", close_result)
        if not close_result.success or close_result.filled_size <= 0:
            print("manual_review_required=True")
            print("volume_loop_stopped=close_order_failed_or_unfilled")
            break

        entry_price = entry_result.average_price or buy_price
        close_average_price = close_result.average_price or close_price
        cycle_gross = (entry_filled_size * entry_price) + (close_size * close_average_price)
        total += cycle_gross
        print(f"cycle_{cycle}_estimated_gross_volume_usd={cycle_gross:.4f}")
        print(f"volume_progress_usd={total:.4f}")
        order_ids = tuple(
            item
            for item in (entry_result.exchange_order_id, close_result.exchange_order_id)
            if item
        )
        cycle_total_latency_ms = _elapsed_ms(cycle_started_ns, close_post_done_ns)
        _emit_hotstuff_execution_event(
            account_label=args.credential_prefix,
            environment=environment,
            market=selected.market,
            cycle_id=f"cycle_{cycle}",
            fee_provider=fee_provider,
            cost=selected_cost,
            entry_notional_usd=entry_filled_size * entry_price,
            exit_notional_usd=close_size * close_average_price,
            planned_gross_volume_usd=planned_gross,
            filled_gross_volume_usd=cycle_gross,
            start_position_count=None,
            final_position_count=None,
            start_open_order_count=None,
            final_open_order_count=None,
            entry_sign_latency_ms=entry_sign_latency_ms,
            close_sign_latency_ms=None if prebuilt_close is not None else close_sign_latency_ms,
            close_prebuild_sign_latency_ms=close_prebuild_sign_latency_ms if prebuilt_close is not None else None,
            entry_post_latency_ms=entry_post_latency_ms,
            close_post_latency_ms=close_post_latency_ms,
            entry_to_close_submit_gap_ms=entry_to_close_submit_gap_ms,
            cycle_total_latency_ms=cycle_total_latency_ms,
            order_ids=order_ids,
            status="fast_close_cycle_completed",
        )
        print(f"cycle_{cycle}_total_latency_ms={cycle_total_latency_ms}")
        print(f"cycle_total_latency_ms={cycle_total_latency_ms}")
        if total < target_gross_volume:
            print(f"cycle_{cycle}_loop_delay_seconds={args.loop_delay_seconds}")
            time.sleep(args.loop_delay_seconds)

    if cycle >= args.max_cycles and total < target_gross_volume:
        print(f"volume_loop_stopped=max_cycles_reached:{args.max_cycles}")
    _print_final_state(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        instruments=instruments,
        target_markets=[str(item["market"]) for item in plan["markets"] if isinstance(item, dict)],
        timeout_seconds=args.timeout_seconds,
        total=total,
    )


def _dry_run_fast_close_build(
    *,
    selected: MarketCandidate,
    entry_price: Decimal,
    entry_size: Decimal,
    close_size: Decimal,
    args: argparse.Namespace,
    sdk_installed: bool,
) -> None:
    print("fast_close_dry_run=True")
    print("fast_close_post_entry_position_check_skipped=True")
    if not sdk_installed:
        print("fast_close_prebuild_available=False")
        print("fast_close_prebuild_reason=hotstuff_python_sdk_not_installed_for_this_python")
        return

    entry_started_ns = _now_ns()
    _build_entry_order_params(selected, entry_price, entry_size)
    print(f"dry_run_entry_sign_latency_ms={_elapsed_ms(entry_started_ns)}")
    if not args.prebuild_close_order:
        print("dry_run_close_prebuild_skipped=prebuild_close_order_false")
        return

    close_price = _aggressive_close_price(
        "s",
        selected.best_bid,
        selected.best_ask,
        selected.tick_size,
        args.reduce_only_slippage_bps,
    )
    close_started_ns = _now_ns()
    _build_close_order_params(selected, close_price, close_size)
    print(f"dry_run_close_prebuild_sign_latency_ms={_elapsed_ms(close_started_ns)}")
    print("dry_run_close_prebuilt=True")
    print(f"dry_run_close_prebuilt_size={_fmt_decimal(close_size)}")
    print(f"dry_run_close_prebuilt_price={_fmt_decimal(close_price)}")


def _build_entry_order_params(selected: MarketCandidate, price: Decimal, size: Decimal) -> object:
    return _build_hotstuff_order_params(
        instrument_id=selected.instrument_id,
        side="b",
        price=price,
        size=size,
        reduce_only=False,
    )


def _build_close_order_params(selected: MarketCandidate, price: Decimal, size: Decimal) -> object:
    return _build_hotstuff_order_params(
        instrument_id=selected.instrument_id,
        side="s",
        price=price,
        size=size,
        reduce_only=True,
    )


def _build_hotstuff_order_params(
    *,
    instrument_id: int,
    side: Literal["b", "s"],
    price: Decimal,
    size: Decimal,
    reduce_only: bool,
) -> object:
    ensure_hotstuff_sdk_compat()
    from hotstuff import PlaceOrderParams, UnitOrder

    order = UnitOrder(
        instrumentId=instrument_id,
        side=side,
        positionSide="BOTH",
        price=_fmt_decimal(price),
        size=_fmt_decimal(size),
        tif="IOC",
        ro=reduce_only,
        po=False,
        isMarket=True,
    )
    return PlaceOrderParams(orders=[order], expiresAfter=_now_ms() + 60_000)


def _print_order_result(prefix: str, result: object) -> None:
    success = getattr(result, "success", False)
    status = getattr(result, "status", "")
    filled_size = getattr(result, "filled_size", Decimal("0"))
    average_price = getattr(result, "average_price", None)
    exchange_order_id = getattr(result, "exchange_order_id", None)
    error = getattr(result, "error", "")
    print(f"{prefix}_success={success}")
    print(f"{prefix}_status={status}")
    print(f"{prefix}_filled_size={_fmt_decimal(filled_size)}")
    if average_price is not None:
        print(f"{prefix}_average_price={_fmt_decimal(average_price)}")
    if exchange_order_id:
        print(f"{prefix}_exchange_order_id={exchange_order_id}")
    if error:
        print(f"{prefix}_error={error}")


def _print_final_state(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    instruments: dict[str, dict[str, object]],
    target_markets: list[str],
    timeout_seconds: float,
    total: Decimal,
) -> None:
    positions = _positions(api_endpoint, credential_prefix, environment, timeout_seconds)
    open_orders = _open_orders(api_endpoint, credential_prefix, environment, timeout_seconds)
    positions_by_market = {_position_market(position): Decimal(str(position.get("size", "0"))) for position in positions}
    print(f"final_estimated_gross_volume_usd={total:.4f}")
    print(f"final_open_order_count={len(open_orders)}")
    print(f"final_position_count={len(positions)}")
    for market in target_markets:
        size = positions_by_market.get(market, Decimal("0"))
        instrument = instruments.get(market, {})
        lot_size = Decimal(str(instrument.get("lot_size", "0")))
        steps = Decimal("0") if lot_size <= 0 else _round_down_to_step(abs(size) / lot_size, Decimal("1"))
        label = _safe_label(market)
        print(f"final_{label}_position_size={_fmt_decimal(size)}")
        print(f"final_{label}_position_steps={_fmt_decimal(steps)}")
    all_flat = not positions and not open_orders
    print(f"final_all_flat={all_flat}")


def _hotstuff_market_specs(plan: dict[str, object], max_spread_bps: Decimal) -> list[MarketSpec]:
    specs: list[MarketSpec] = []
    for market in plan["markets"]:
        if not isinstance(market, dict):
            continue
        symbol = str(market["market"])
        specs.append(
            MarketSpec(
                exchange_id="hotstuff",
                market=symbol,
                average_spread_bps=Decimal(str(market["provided_24h_spread_bps"])),
                max_spread_bps=max_spread_bps,
                metadata=market,
            ),
        )
    return specs


def _select_candidate_from_monitor(
    *,
    args: argparse.Namespace,
    plan: dict[str, object],
    api_endpoint: str,
    wss_endpoint: str,
    instruments: dict[str, dict[str, object]],
    specs: list[MarketSpec],
    cache: SpreadCache,
    rest_backoff: RestBackoff,
    max_spread_bps: Decimal,
    fee_provider: HotstuffFeeProvider,
) -> MarketCandidate | None:
    refresh_hotstuff_spread_cache(
        cache=cache,
        specs=specs,
        api_endpoint=api_endpoint,
        wss_endpoint=wss_endpoint,
        monitor_source=args.monitor_source,
        cache_max_age_seconds=args.monitor_cache_max_age_seconds,
        websocket_timeout_seconds=args.websocket_snapshot_timeout_seconds,
        timeout_seconds=args.timeout_seconds,
        rest_backoff=rest_backoff,
    )
    selection = select_lowest_spread(cache, specs, max_age_seconds=args.monitor_cache_max_age_seconds)
    cost_eligible: list[tuple[MarketSpec, object, MarketCostResult]] = []
    for spec, snapshot in selection.accepted:
        fee = fee_provider.fee_for_market(spec.market)
        cost = calculate_market_cost(
            MarketCostInput(
                exchange_id=spec.exchange_id,
                market=spec.market,
                live_spread_bps=snapshot.spread_bps,
                fee=fee,
            )
        )
        if cost.eligible:
            cost_eligible.append((spec, snapshot, cost))
        print(
            f"candidate={spec.market} eligible={cost.eligible} source={snapshot.source} "
            f"spread_bps={snapshot.spread_bps:.4f} expected_loss_bps={cost.expected_loss_bps:.4f} "
            f"fee_source={cost.fee_source} reason={cost.reason if not cost.eligible else 'spread_and_fee_ok'}"
        )
    for spec, reason in selection.rejected:
        snapshot = cache.get(spec.exchange_id, spec.market)
        spread = f"{snapshot.spread_bps:.4f}" if snapshot is not None else "unknown"
        print(f"candidate={spec.market} eligible=False source=cache spread_bps={spread} reason={reason}")
    print(f"eligible_market_count={len(cost_eligible)}")
    if not cost_eligible:
        return None

    selected_spec, cached_snapshot, selected_cost = min(
        cost_eligible,
        key=lambda item: (item[2].expected_loss_bps, item[1].spread_bps, item[0].market),
    )
    print(
        f"selected_market_from_cache={selected_spec.market} "
        f"source={cached_snapshot.source} spread_bps={cached_snapshot.spread_bps:.4f} "
        f"expected_loss_bps={selected_cost.expected_loss_bps:.4f} fee_source={selected_cost.fee_source}"
    )

    fresh_market = dict(selected_spec.metadata)
    fresh = _candidate_from_live_orderbook(api_endpoint, fresh_market, instruments, max_spread_bps, args.timeout_seconds)
    fresh_cost = _candidate_cost(fresh, fee_provider) if fresh.eligible else None
    fresh_expected_loss = f"{fresh_cost.expected_loss_bps:.4f}" if fresh_cost is not None else "999999.0000"
    fresh_reason = fresh.reason if fresh_cost is None or fresh_cost.eligible else fresh_cost.reason
    print(
        f"fresh_verify_market={fresh.market} eligible={fresh.eligible} "
        f"spread_bps={fresh.live_spread_bps:.4f} "
        f"expected_loss_bps={fresh_expected_loss} "
        f"reason={fresh_reason}"
    )
    if not fresh.eligible or fresh_cost is None or not fresh_cost.eligible:
        print("selected_plan_skipped=fresh_verify_failed")
        return None
    return fresh


def _account_summary_ok(api_endpoint: str, credential_prefix: str, environment: str, timeout_seconds: float) -> bool:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    try:
        payload = info_post_json(api_endpoint, "accountSummary", params, timeout_seconds, private_readonly=True)
    except Exception:
        return False
    return isinstance(payload, dict)


def _positions(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
) -> list[dict[str, object]]:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    payload = info_post_json(api_endpoint, "positions", params, timeout_seconds, private_readonly=True)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and abs(Decimal(str(item.get("size", "0")))) > 0]


def _open_orders(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
) -> list[dict[str, object]]:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    payload = info_post_json(api_endpoint, "openOrders", params, timeout_seconds, private_readonly=True)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("orders", "open_orders", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _selected_position(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    market: str,
    timeout_seconds: float,
) -> dict[str, object] | None:
    for position in _positions(api_endpoint, credential_prefix, environment, timeout_seconds):
        if _position_market(position) == market:
            return position
    return None


def _position_market(position: dict[str, object]) -> str:
    return str(position.get("instrument") or position.get("symbol") or position.get("instrument_name") or "unknown")


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower() or "market"


def _optional_decimal(value: Decimal | str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _signer_registered_for_account(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
    signer_address: str,
) -> bool:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    try:
        payload = info_post_json(api_endpoint, "allAgents", params, timeout_seconds, private_readonly=True)
    except Exception:
        return False
    return _payload_contains_address(payload, signer_address.casefold())


def _payload_contains_address(payload: object, address: str) -> bool:
    if isinstance(payload, dict):
        return any(_payload_contains_address(value, address) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains_address(value, address) for value in payload)
    if isinstance(payload, str):
        return payload.casefold() == address
    return False


def _send_reduce_only_close(client: object, selected: MarketCandidate, residual: dict[str, object], args: argparse.Namespace) -> bool:
    from hotstuff import PlaceOrderParams, UnitOrder

    raw_size = Decimal(str(residual.get("size", "0")))
    if raw_size == 0:
        return True
    side: Literal["b", "s"] = "s" if raw_size > 0 else "b"
    price = _aggressive_close_price(
        side,
        selected.best_bid,
        selected.best_ask,
        selected.tick_size,
        args.reduce_only_slippage_bps,
    )
    size = _round_down_to_step(abs(raw_size), selected.lot_size)
    if size <= 0:
        return False
    order = UnitOrder(
        instrumentId=selected.instrument_id,
        side=side,
        positionSide="BOTH",
        price=_fmt_decimal(price),
        size=_fmt_decimal(size),
        tif="IOC",
        ro=True,
        po=False,
        isMarket=True,
    )
    response = _safe_place_order(client, PlaceOrderParams(orders=[order], expiresAfter=_now_ms() + 60_000), "reduce_only_close")
    if response is None:
        return False
    _print_exchange_response(response, "reduce_only_close")
    return not _response_has_error(response)


def _quantity_for_notional(notional: Decimal, price: Decimal, lot_size: Decimal) -> tuple[Decimal, Decimal]:
    if price <= 0 or lot_size <= 0:
        return Decimal("0"), price
    return _round_down_to_step(notional / price, lot_size), price


def _roundtrip_order_plan(
    selected: MarketCandidate,
    order_notional: Decimal,
    remaining_gross_volume_usd: Decimal,
    level_size_fraction: Decimal,
) -> dict[str, Decimal]:
    best_bid_notional = selected.best_bid * selected.best_bid_size
    best_ask_notional = selected.best_ask * selected.best_ask_size
    liquidity_cap = min(best_bid_notional, best_ask_notional) * level_size_fraction
    per_side_cap = min(order_notional, remaining_gross_volume_usd / Decimal("2"), liquidity_cap)
    if per_side_cap <= 0:
        return {
            "per_side_cap": Decimal("0"),
            "buy_qty": Decimal("0"),
            "buy_price": selected.best_ask,
            "sell_qty": Decimal("0"),
            "sell_price": selected.best_bid,
            "planned_gross": Decimal("0"),
        }
    buy_qty, buy_price = _quantity_for_notional(per_side_cap, selected.best_ask, selected.lot_size)
    sell_qty, sell_price = _quantity_for_notional(per_side_cap, selected.best_bid, selected.lot_size)
    return {
        "per_side_cap": per_side_cap,
        "buy_qty": buy_qty,
        "buy_price": buy_price,
        "sell_qty": sell_qty,
        "sell_price": sell_price,
        "planned_gross": (buy_qty * buy_price) + (sell_qty * sell_price),
    }


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def _aggressive_close_price(
    side: Literal["b", "s"],
    best_bid: Decimal,
    best_ask: Decimal,
    tick_size: Decimal,
    slippage_bps: Decimal,
) -> Decimal:
    factor = slippage_bps / Decimal("10000")
    if side == "s":
        return _round_down_to_step(best_bid * (Decimal("1") - factor), tick_size)
    return _round_up_to_step(best_ask * (Decimal("1") + factor), tick_size)


def _ceil_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_UP))


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_ns() -> int:
    return time.perf_counter_ns()


def _elapsed_ms(start_ns: int, end_ns: int | None = None) -> str:
    finish_ns = _now_ns() if end_ns is None else end_ns
    return f"{(finish_ns - start_ns) / 1_000_000:.3f}"


def _response_has_error(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    error = response.get("error")
    if error:
        return True
    status = response.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failed", "rejected"}:
        return True
    return False


def _safe_place_order(client: object, params: object, prefix: str) -> object | None:
    try:
        return client.place_order(params)
    except Exception as exc:
        print(f"{prefix}_exchange_exception_type={type(exc).__name__}")
        reason = _exchange_error_reason(exc)
        if reason:
            print(f"{prefix}_exchange_exception_reason={reason}")
        return None


def _exchange_error_reason(exc: Exception) -> str:
    text = str(exc)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if error:
                return str(error)
            status = payload.get("status")
            if status:
                return str(status)

    sanitized = re.sub(r"0x[a-fA-F0-9]{40,64}", "0x[redacted]", text)
    return sanitized[:240]


def _print_exchange_response(response: object, prefix: str) -> None:
    print(f"{prefix}_response_type={type(response).__name__}")
    if not isinstance(response, dict):
        return
    safe_keys = [key for key in ("success", "tx_type", "error") if key in response]
    print(f"{prefix}_keys=" + ",".join(sorted(str(key) for key in response.keys())[:12]))
    for key in safe_keys:
        print(f"{prefix}_{key}={response[key]}")
    data = response.get("data")
    if isinstance(data, dict):
        print(f"{prefix}_data_keys=" + ",".join(sorted(str(key) for key in data.keys())[:12]))
        status = data.get("status")
        if status is not None:
            print(f"{prefix}_data_status={status}")


if __name__ == "__main__":
    main()
