from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

from perpdex_farming_bot.cli.risex_live_preflight import (
    _data_payload,
    _extract_positions,
    _position_size,
    _select_market,
    _to_decimal,
    fmt_decimal,
)
from perpdex_farming_bot.cli.risex_live_test import (
    CONFIRM_TEXT,
    _build_signed_order,
    _extract_open_orders,
    _print_post_result,
    _read_only_state_is_ready,
    _settled_position_steps,
)
from perpdex_farming_bot.connectors.risex_readonly import (
    RisexReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    nonce_state_path,
    normalize_risex_environment,
    read_only_get_json,
    validate_https_base_url,
)
from perpdex_farming_bot.connectors.risex_trading import (
    RisexSignedPlaceOrder,
    RisexTradingConfigError,
    build_place_order_draft,
    safe_signed_order_summary,
    sign_place_order,
)
from perpdex_farming_bot.credentials import read_risex_credentials, risex_credential_env
from perpdex_farming_bot.core.execution_cost import MarketCostInput, calculate_market_cost
from perpdex_farming_bot.core.execution_event import (
    ExecutionEvent,
    emit_execution_event,
    estimate_roundtrip_fee_usd,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.base import AdapterError
from perpdex_farming_bot.exchanges.risex import RisexAdapter
from perpdex_farming_bot.exchanges.risex_fees import (
    RisexAccountFee,
    RisexFeeProvider,
    load_risex_account_fee_from_trade_history,
    risex_fee_overrides_from_config,
    risex_market_fee_metadata_from_markets,
)


MAX_LIVE_TARGET_USD = Decimal("1000")
MAX_LEG_NOTIONAL_USD = Decimal("100")
DEFAULT_MARKET_IDS = (1, 2, 4, 5)  # BTC, ETH, SOL, HYPE


@dataclass(frozen=True)
class MarketRuntime:
    market_id: int
    name: str
    step_size: Decimal
    step_price: Decimal
    min_order_size: Decimal


@dataclass(frozen=True)
class TopBookPlan:
    market: MarketRuntime
    best_bid: Decimal
    best_bid_qty: Decimal
    best_ask: Decimal
    best_ask_qty: Decimal
    spread_bps: Decimal
    entry_fee_bps: Decimal
    exit_fee_bps: Decimal
    slippage_buffer_bps: Decimal
    fee_source: str
    fee_known: bool
    expected_loss_bps: Decimal
    side_notional: Decimal
    size_steps: int
    size: Decimal

    @property
    def estimated_roundtrip_notional(self) -> Decimal:
        return self.side_notional * Decimal("2")


@dataclass(frozen=True)
class MarketStateSummary:
    open_order_count: int
    position_count: int
    all_flat: bool


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded RiseX live volume test. Places market buy then reduce-only market sell while spread <= threshold."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="RISEX")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--market-ids", default="1,2,4,5", help="Comma-separated RiseX market IDs.")
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=Decimal("1000"))
    parser.add_argument("--max-leg-notional-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--spread-bps", type=Decimal, default=Decimal("1"))
    parser.add_argument("--max-expected-loss-bps", type=Decimal, default=None)
    parser.add_argument("--book-fraction", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--fee-config", default="config/risex.live-volume.json")
    parser.add_argument("--fee-history-limit", type=int, default=100)
    parser.add_argument("--loop-delay-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--deadline-seconds", type=int, default=300)
    parser.add_argument("--position-settle-attempts", type=int, default=8)
    parser.add_argument("--position-settle-delay-seconds", type=float, default=0.7)
    parser.add_argument(
        "--fast-close-on-fill",
        action="store_true",
        help="Submit the reduce-only close immediately after a successful entry POST, without waiting for a position REST check.",
    )
    parser.add_argument(
        "--prebuild-close-order",
        action="store_true",
        help="Pre-sign the reduce-only close before entry submission. Requires --fast-close-on-fill.",
    )
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    _validate_args(args)

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_risex_environment(args.environment)
    credential_env = risex_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    market_ids = _parse_market_ids(args.market_ids)

    print("risex_live_volume=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"market_ids={','.join(str(item) for item in market_ids)}")
    print(f"target_gross_volume_usd={fmt_decimal(args.target_gross_volume_usd)}")
    print(f"max_leg_notional_usd={fmt_decimal(args.max_leg_notional_usd)}")
    print(f"spread_bps_threshold={fmt_decimal(args.spread_bps)}")
    print(f"max_expected_loss_bps={fmt_decimal(args.max_expected_loss_bps) if args.max_expected_loss_bps is not None else 'none'}")
    print(f"fee_config={args.fee_config}")
    print(f"fee_history_limit={args.fee_history_limit}")
    print(f"book_fraction={fmt_decimal(args.book_fraction)}")
    print(f"loop_delay_seconds={args.loop_delay_seconds}")
    print(f"fast_close_on_fill={args.fast_close_on_fill}")
    print(f"prebuild_close_order={args.prebuild_close_order}")
    print(f"execute_live={args.execute_live}")
    print(f"required_confirmation={CONFIRM_TEXT}")
    print("market_selection=lowest_expected_loss_bps_then_live_spread_bps")
    print("entry_order_type=market_ioc_buy")
    print("close_order_type=market_ioc_reduce_only_sell")
    print("volume_counter=estimated_gross_notional_buy_plus_sell")
    print("execution_event_schema=perpdex.execution_event.v1")
    print("execution_event_output=execution_event_json")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except RisexReadonlyConfigError as exc:
        print("volume_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.signer_address}={masked_env_status(credential_env.signer_address)}")
    print(f"primary_{credential_env.signer_private_key}={masked_env_status(credential_env.signer_private_key)}")

    if not args.network:
        print("network_skipped=pass_--network_to_run_preflight_or_live_volume")
        print("volume_ready=False")
        return

    credentials = read_risex_credentials(args.credential_prefix, environment)
    common_args = _common_args(args, market_ids[0])
    adapter = RisexAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=False,
    )
    signer_ok, signer_reason = adapter.signer_ready()
    print(f"signer_ready={signer_ok}")
    print(f"signer_ready_reason={signer_reason}")
    if not signer_ok:
        print("volume_ready=False")
        return

    if not _system_and_session_ready(api_endpoint, credentials, common_args):
        return
    start_state = _all_markets_flat(api_endpoint, credentials["account_address"], market_ids, common_args)
    if not start_state.all_flat:
        _emit_blocked_execution_event(
            environment=environment,
            account_label=credential_env.prefix,
            start_state=start_state,
            reason="existing_open_orders_or_positions_detected",
        )
        return

    markets = _load_markets(api_endpoint, market_ids, args.timeout_seconds)
    if not markets:
        print("volume_ready=False")
        print("reason=no_target_markets_loaded")
        return

    market_payload = read_only_get_json(api_endpoint, "/v1/markets", {}, args.timeout_seconds)
    account_fee = _load_account_fee_or_none(api_endpoint, credentials["account_address"], args)
    fee_provider = RisexFeeProvider(
        override_by_market=risex_fee_overrides_from_config(args.fee_config),
        metadata_by_market=risex_market_fee_metadata_from_markets(market_payload),
        account_fee=account_fee,
    )

    dry_plan = _choose_plan(api_endpoint, markets, args, args.target_gross_volume_usd, fee_provider)
    if dry_plan is None:
        print("volume_ready=False")
        print("reason=no_market_passed_spread_or_size_gate")
        _emit_blocked_execution_event(
            environment=environment,
            account_label=credential_env.prefix,
            start_state=start_state,
            reason="no_market_passed_spread_or_size_gate",
        )
        return
    _print_plan("dry_run", dry_plan)
    signed_entry = _build_signed_order(
        api_endpoint=api_endpoint,
        credentials=credentials,
        args=_common_args(args, dry_plan.market.market_id),
        side=0,
        reduce_only=False,
        size_steps=dry_plan.size_steps,
        price_ticks=0,
        step_size=dry_plan.market.step_size,
        step_price=dry_plan.market.step_price,
        label="dry_run_entry",
    )
    if signed_entry is None:
        print("volume_ready=False")
        return
    if args.fast_close_on_fill and args.prebuild_close_order:
        try:
            dry_close_nonce_data = _next_nonce_after_signed(signed_entry)
        except RisexTradingConfigError as exc:
            print("volume_ready=False")
            print(f"dry_run_close_prebuild_nonce_error={exc.__class__.__name__}")
            return
        signed_dry_close = _build_signed_order_with_nonce(
            api_endpoint=api_endpoint,
            credentials=credentials,
            args=_common_args(args, dry_plan.market.market_id),
            side=1,
            reduce_only=True,
            size_steps=dry_plan.size_steps,
            price_ticks=0,
            step_size=dry_plan.market.step_size,
            step_price=dry_plan.market.step_price,
            nonce_data=dry_close_nonce_data,
            label="dry_run_close_prebuilt",
        )
        if signed_dry_close is None:
            print("volume_ready=False")
            return

    if not args.execute_live:
        _emit_dry_run_execution_event(
            environment=environment,
            account_label=credential_env.prefix,
            plan=dry_plan,
            account_fee=account_fee,
            fee_provider=fee_provider,
            start_state=start_state,
        )
        print("volume_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if args.confirm != CONFIRM_TEXT:
        print("volume_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    live_adapter = RisexAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=True,
    )
    _run_volume_loop(
        api_endpoint=api_endpoint,
        credentials=credentials,
        markets=markets,
        adapter=live_adapter,
        fee_provider=fee_provider,
        args=args,
    )


def _validate_args(args: argparse.Namespace) -> None:
    if args.target_gross_volume_usd <= 0 or args.target_gross_volume_usd > MAX_LIVE_TARGET_USD:
        raise SystemExit(f"--target-gross-volume-usd must be > 0 and <= {MAX_LIVE_TARGET_USD}")
    if args.max_leg_notional_usd <= 0 or args.max_leg_notional_usd > MAX_LEG_NOTIONAL_USD:
        raise SystemExit(f"--max-leg-notional-usd must be > 0 and <= {MAX_LEG_NOTIONAL_USD}")
    if args.spread_bps < 0 or args.spread_bps > Decimal("5"):
        raise SystemExit("--spread-bps must be between 0 and 5")
    if args.max_expected_loss_bps is not None and args.max_expected_loss_bps < 0:
        raise SystemExit("--max-expected-loss-bps must be zero or greater")
    if args.book_fraction <= 0 or args.book_fraction > Decimal("0.5"):
        raise SystemExit("--book-fraction must be > 0 and <= 0.5")
    if args.fee_history_limit <= 0 or args.fee_history_limit > 500:
        raise SystemExit("--fee-history-limit must be > 0 and <= 500")
    if args.loop_delay_seconds < 0 or args.loop_delay_seconds > 10:
        raise SystemExit("--loop-delay-seconds must be between 0 and 10")
    if args.deadline_seconds <= 0 or args.deadline_seconds > 1800:
        raise SystemExit("--deadline-seconds must be > 0 and <= 1800")
    if args.position_settle_attempts <= 0:
        raise SystemExit("--position-settle-attempts must be greater than zero")
    if args.position_settle_delay_seconds < 0:
        raise SystemExit("--position-settle-delay-seconds must be zero or greater")
    if args.prebuild_close_order and not args.fast_close_on_fill:
        raise SystemExit("--prebuild-close-order requires --fast-close-on-fill")


def _parse_market_ids(raw: str) -> tuple[int, ...]:
    result: list[int] = []
    for chunk in raw.split(","):
        text = chunk.strip()
        if not text:
            continue
        value = int(text)
        if value <= 0:
            raise SystemExit("--market-ids must contain positive integers")
        result.append(value)
    if not result:
        raise SystemExit("--market-ids must include at least one market")
    return tuple(dict.fromkeys(result))


def _common_args(args: argparse.Namespace, market_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        credential_prefix=args.credential_prefix,
        environment=normalize_risex_environment(args.environment),
        market_id=market_id,
        timeout_seconds=args.timeout_seconds,
        deadline_seconds=args.deadline_seconds,
        position_settle_attempts=args.position_settle_attempts,
        position_settle_delay_seconds=args.position_settle_delay_seconds,
        signing_mode="verify-witness",
        allow_existing_position=False,
        close_existing_only=False,
        execute_live=args.execute_live,
        confirm=args.confirm,
        post_order_wait_seconds=args.loop_delay_seconds,
        max_notional_usd=args.max_leg_notional_usd,
        slippage_bps=Decimal("25"),
    )


def _system_and_session_ready(api_endpoint: str, credentials: dict[str, str], args: SimpleNamespace) -> bool:
    return _read_only_state_is_ready(
        api_endpoint,
        credentials["account_address"],
        credentials["signer_address"],
        args,
    )


def _all_markets_flat(api_endpoint: str, account: str, market_ids: tuple[int, ...], args: SimpleNamespace) -> MarketStateSummary:
    all_ok = True
    total_open_orders = 0
    total_nonzero_positions = 0
    for market_id in market_ids:
        open_orders_payload = read_only_get_json(
            api_endpoint,
            "/v1/orders/open",
            {"account": account, "market_id": market_id, "limit": 25},
            args.timeout_seconds,
            private_readonly=True,
        )
        open_orders = _extract_open_orders(open_orders_payload)
        positions_payload = read_only_get_json(
            api_endpoint,
            "/v1/positions",
            {"account": account, "market_id": market_id, "page_size": 100},
            args.timeout_seconds,
            private_readonly=True,
        )
        positions = _extract_positions(positions_payload)
        nonzero_positions = [position for position in positions if _position_size(position) != 0]
        print(f"market_{market_id}_start_open_order_count={len(open_orders)}")
        print(f"market_{market_id}_start_position_count={len(nonzero_positions)}")
        total_open_orders += len(open_orders)
        total_nonzero_positions += len(nonzero_positions)
        if open_orders or nonzero_positions:
            all_ok = False
    if not all_ok:
        print("volume_ready=False")
        print("reason=existing_open_orders_or_positions_detected")
    return MarketStateSummary(
        open_order_count=total_open_orders,
        position_count=total_nonzero_positions,
        all_flat=all_ok,
    )


def _load_markets(api_endpoint: str, market_ids: tuple[int, ...], timeout_seconds: float) -> tuple[MarketRuntime, ...]:
    payload = read_only_get_json(api_endpoint, "/v1/markets", {}, timeout_seconds)
    markets: list[MarketRuntime] = []
    for market_id in market_ids:
        try:
            raw = _select_market(payload, market_id)
        except (ValueError, KeyError, IndexError):
            print(f"market_{market_id}_loaded=False")
            continue
        config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
        name = str(config.get("name") or raw.get("display_name") or raw.get("base_asset_symbol") or market_id)
        available = bool(raw.get("available", False)) and bool(raw.get("active", False))
        unlocked = bool(config.get("unlocked", False))
        if not available or not unlocked:
            print(f"market_{market_id}_loaded=False")
            print(f"market_{market_id}_reason=not_available_or_locked")
            continue
        markets.append(
            MarketRuntime(
                market_id=market_id,
                name=name,
                step_size=_to_decimal(config.get("step_size")),
                step_price=_to_decimal(config.get("step_price")),
                min_order_size=_to_decimal(config.get("min_order_size")),
            )
        )
        print(f"market_{market_id}_loaded=True")
        print(f"market_{market_id}_name={name}")
    return tuple(markets)


def _load_account_fee_or_none(api_endpoint: str, account: str, args: argparse.Namespace) -> RisexAccountFee | None:
    try:
        account_fee = load_risex_account_fee_from_trade_history(
            api_endpoint,
            account,
            args.timeout_seconds,
            limit=args.fee_history_limit,
        )
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexReadonlyConfigError) as exc:
        print(f"account_fee_lookup_error={exc.__class__.__name__}")
        print("account_fee_source=unavailable")
        return None

    print(f"account_fee_source={account_fee.source}")
    print(f"account_fee_level={account_fee.fee_level if account_fee.fee_level is not None else 'unknown'}")
    print(
        "account_maker_fee_bps="
        f"{fmt_decimal(account_fee.maker_fee_bps) if account_fee.maker_fee_bps is not None else 'unknown'}"
    )
    print(f"account_taker_fee_bps={fmt_decimal(account_fee.taker_fee_bps)}")
    return account_fee


def _run_volume_loop(
    *,
    api_endpoint: str,
    credentials: dict[str, str],
    markets: tuple[MarketRuntime, ...],
    adapter: RisexAdapter,
    fee_provider: RisexFeeProvider,
    args: argparse.Namespace,
) -> None:
    total = Decimal("0")
    cycle = 0
    while total < args.target_gross_volume_usd:
        cycle_started_ns = _now_ns()
        remaining = args.target_gross_volume_usd - total
        plan_started_ns = _now_ns()
        plan = _choose_plan(api_endpoint, markets, args, remaining, fee_provider)
        print(f"cycle_{cycle + 1}_plan_latency_ms={_elapsed_ms(plan_started_ns)}")
        if plan is None:
            print("volume_loop_stopped=no_eligible_market")
            break
        cycle += 1
        _print_plan(f"cycle_{cycle}", plan)
        cycle_args = _common_args(args, plan.market.market_id)

        sign_entry_started_ns = _now_ns()
        signed_entry = _build_signed_order(
            api_endpoint=api_endpoint,
            credentials=credentials,
            args=cycle_args,
            side=0,
            reduce_only=False,
            size_steps=plan.size_steps,
            price_ticks=0,
            step_size=plan.market.step_size,
            step_price=plan.market.step_price,
            label=f"cycle_{cycle}_entry",
        )
        print(f"cycle_{cycle}_entry_sign_latency_ms={_elapsed_ms(sign_entry_started_ns)}")
        if signed_entry is None:
            print("volume_loop_stopped=entry_signing_failed")
            break

        prebuilt_close: RisexSignedPlaceOrder | None = None
        if args.fast_close_on_fill and args.prebuild_close_order:
            sign_close_started_ns = _now_ns()
            try:
                close_nonce_data = _next_nonce_after_signed(signed_entry)
            except RisexTradingConfigError as exc:
                print("volume_loop_stopped=close_prebuild_nonce_error")
                print(f"cycle_{cycle}_close_prebuild_nonce_error={exc.__class__.__name__}")
                break
            prebuilt_close = _build_signed_order_with_nonce(
                api_endpoint=api_endpoint,
                credentials=credentials,
                args=cycle_args,
                side=1,
                reduce_only=True,
                size_steps=plan.size_steps,
                price_ticks=0,
                step_size=plan.market.step_size,
                step_price=plan.market.step_price,
                nonce_data=close_nonce_data,
                label=f"cycle_{cycle}_close_prebuilt",
            )
            print(f"cycle_{cycle}_close_prebuild_sign_latency_ms={_elapsed_ms(sign_close_started_ns)}")
            if prebuilt_close is None:
                print("volume_loop_stopped=close_prebuild_signing_failed")
                break

        print(f"cycle_{cycle}_entry_submitting=True")
        entry_post_started_ns = _now_ns()
        try:
            entry_result = adapter.submit_signed_place_order(signed_entry)
        except AdapterError as exc:
            print("volume_loop_stopped=entry_adapter_error")
            print(f"cycle_{cycle}_entry_adapter_error={exc}")
            break
        entry_post_done_ns = _now_ns()
        print(f"cycle_{cycle}_entry_post_latency_ms={_elapsed_ms(entry_post_started_ns, entry_post_done_ns)}")
        _print_post_result(f"cycle_{cycle}_entry", entry_result)
        if not entry_result.ok:
            print("volume_loop_stopped=entry_order_failed")
            break

        if args.fast_close_on_fill:
            close_steps = plan.size_steps
            print(f"cycle_{cycle}_fast_close_on_fill=True")
            print(f"cycle_{cycle}_post_entry_position_check_skipped=True")
            print(f"cycle_{cycle}_close_market_check_skipped=True")
            if prebuilt_close is not None:
                signed_close = prebuilt_close
                print(f"cycle_{cycle}_close_order_prebuilt=True")
            else:
                sign_close_started_ns = _now_ns()
                signed_close = _build_signed_order(
                    api_endpoint=api_endpoint,
                    credentials=credentials,
                    args=cycle_args,
                    side=1,
                    reduce_only=True,
                    size_steps=close_steps,
                    price_ticks=0,
                    step_size=plan.market.step_size,
                    step_price=plan.market.step_price,
                    label=f"cycle_{cycle}_close",
                )
                print(f"cycle_{cycle}_close_sign_latency_ms={_elapsed_ms(sign_close_started_ns)}")
                if signed_close is None:
                    print("manual_review_required=True")
                    print("volume_loop_stopped=close_signing_failed")
                    break
        else:
            time.sleep(args.loop_delay_seconds)
            position_check_started_ns = _now_ns()
            close_steps = _settled_position_steps(
                api_endpoint,
                credentials["account_address"],
                plan.market.step_size,
                cycle_args,
                label=f"cycle_{cycle}_post_entry",
            )
            print(f"cycle_{cycle}_post_entry_position_check_latency_ms={_elapsed_ms(position_check_started_ns)}")
            if close_steps <= 0:
                print("volume_loop_stopped=no_position_detected_after_entry")
                break

            close_plan_started_ns = _now_ns()
            close_plan = _plan_for_market(api_endpoint, plan.market, args, remaining, fee_provider)
            print(f"cycle_{cycle}_close_market_check_latency_ms={_elapsed_ms(close_plan_started_ns)}")
            if close_plan is None:
                print(f"cycle_{cycle}_close_market_check=failed_or_spread_wide")
            else:
                _print_plan(f"cycle_{cycle}_close_check", close_plan)

            sign_close_started_ns = _now_ns()
            signed_close = _build_signed_order(
                api_endpoint=api_endpoint,
                credentials=credentials,
                args=cycle_args,
                side=1,
                reduce_only=True,
                size_steps=close_steps,
                price_ticks=0,
                step_size=plan.market.step_size,
                step_price=plan.market.step_price,
                label=f"cycle_{cycle}_close",
            )
            print(f"cycle_{cycle}_close_sign_latency_ms={_elapsed_ms(sign_close_started_ns)}")
            if signed_close is None:
                print("manual_review_required=True")
                print("volume_loop_stopped=close_signing_failed")
                break

        print(f"cycle_{cycle}_close_submitting=True")
        print(f"cycle_{cycle}_entry_to_close_submit_gap_ms={_elapsed_ms(entry_post_done_ns)}")
        close_post_started_ns = _now_ns()
        try:
            close_result = adapter.submit_signed_place_order(signed_close)
        except AdapterError as exc:
            print("manual_review_required=True")
            print("volume_loop_stopped=close_adapter_error")
            print(f"cycle_{cycle}_close_adapter_error={exc}")
            break
        close_post_done_ns = _now_ns()
        print(f"cycle_{cycle}_close_post_latency_ms={_elapsed_ms(close_post_started_ns, close_post_done_ns)}")
        _print_post_result(f"cycle_{cycle}_close", close_result)
        if not close_result.ok:
            print("manual_review_required=True")
            print("volume_loop_stopped=close_order_failed")
            break

        total += plan.estimated_roundtrip_notional
        print(f"cycle_{cycle}_estimated_gross_volume_usd={fmt_decimal(plan.estimated_roundtrip_notional)}")
        print(f"volume_progress_usd={fmt_decimal(total)}")
        print(f"cycle_{cycle}_total_latency_ms={_elapsed_ms(cycle_started_ns, close_post_done_ns)}")
        time.sleep(args.loop_delay_seconds)

    _print_final_state(api_endpoint, credentials["account_address"], markets, args, total)


def _choose_plan(
    api_endpoint: str,
    markets: tuple[MarketRuntime, ...],
    args: argparse.Namespace,
    remaining_gross_volume: Decimal,
    fee_provider: RisexFeeProvider,
) -> TopBookPlan | None:
    plans = [
        plan
        for market in markets
        if (plan := _plan_for_market(api_endpoint, market, args, remaining_gross_volume, fee_provider)) is not None
    ]
    if not plans:
        return None
    plans.sort(key=lambda item: (item.expected_loss_bps, item.spread_bps, -item.side_notional, item.market.market_id))
    return plans[0]


def _plan_for_market(
    api_endpoint: str,
    market: MarketRuntime,
    args: argparse.Namespace,
    remaining_gross_volume: Decimal,
    fee_provider: RisexFeeProvider,
) -> TopBookPlan | None:
    try:
        payload = read_only_get_json(
            api_endpoint,
            "/v1/orderbook",
            {"market_id": market.market_id, "limit": 1},
            args.timeout_seconds,
        )
        book = _data_payload(payload)
        bid_price, bid_qty = _first_level(book, "bids")
        ask_price, ask_qty = _first_level(book, "asks")
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexReadonlyConfigError) as exc:
        print(f"market_{market.market_id}_plan_error={exc.__class__.__name__}")
        return None

    if bid_price <= 0 or ask_price <= 0 or ask_price < bid_price:
        return None
    mid = (bid_price + ask_price) / Decimal("2")
    spread_bps = (ask_price - bid_price) / mid * Decimal("10000")
    fee = fee_provider.fee_for_market(str(market.market_id))
    cost = calculate_market_cost(
        MarketCostInput(
            exchange_id="risex",
            market=str(market.market_id),
            live_spread_bps=spread_bps,
            fee=fee,
        )
    )
    bid_half_notional = bid_price * bid_qty * args.book_fraction
    ask_half_notional = ask_price * ask_qty * args.book_fraction
    max_side_notional = min(
        args.max_leg_notional_usd,
        bid_half_notional,
        ask_half_notional,
        remaining_gross_volume / Decimal("2"),
    )
    size = _round_down(max_side_notional / ask_price, market.step_size)
    if size < market.min_order_size:
        print(f"market_{market.market_id}_eligible=False")
        print(f"market_{market.market_id}_reason=size_below_min_order")
        return None
    size_steps = int((size / market.step_size).to_integral_value(rounding=ROUND_FLOOR))
    side_notional = size * ask_price
    print(f"market_{market.market_id}_spread_bps={fmt_decimal(spread_bps)}")
    entry_fee_text = fmt_decimal(cost.entry_fee_bps) if cost.fee_known else "unknown"
    exit_fee_text = fmt_decimal(cost.exit_fee_bps) if cost.fee_known else "unknown"
    print(f"market_{market.market_id}_entry_fee_bps={entry_fee_text}")
    print(f"market_{market.market_id}_exit_fee_bps={exit_fee_text}")
    print(f"market_{market.market_id}_slippage_buffer_bps={fmt_decimal(cost.slippage_buffer_bps)}")
    print(f"market_{market.market_id}_fee_source={cost.fee_source}")
    print(f"market_{market.market_id}_expected_loss_bps={fmt_decimal(cost.expected_loss_bps)}")
    print(f"market_{market.market_id}_side_notional_usd={fmt_decimal(side_notional)}")
    if not cost.eligible:
        print(f"market_{market.market_id}_eligible=False")
        print(f"market_{market.market_id}_reason={cost.reason}")
        return None
    if spread_bps > args.spread_bps:
        print(f"market_{market.market_id}_eligible=False")
        print(f"market_{market.market_id}_reason=spread_above_threshold")
        return None
    if args.max_expected_loss_bps is not None and cost.expected_loss_bps > args.max_expected_loss_bps:
        print(f"market_{market.market_id}_eligible=False")
        print(f"market_{market.market_id}_reason=expected_loss_above_threshold")
        return None
    if side_notional <= 0 or size_steps <= 0:
        return None
    return TopBookPlan(
        market=market,
        best_bid=bid_price,
        best_bid_qty=bid_qty,
        best_ask=ask_price,
        best_ask_qty=ask_qty,
        spread_bps=spread_bps,
        entry_fee_bps=cost.entry_fee_bps,
        exit_fee_bps=cost.exit_fee_bps,
        slippage_buffer_bps=cost.slippage_buffer_bps,
        fee_source=cost.fee_source,
        fee_known=cost.fee_known,
        expected_loss_bps=cost.expected_loss_bps,
        side_notional=side_notional,
        size_steps=size_steps,
        size=size,
    )


def _first_level(book: object, side: str) -> tuple[Decimal, Decimal]:
    if not isinstance(book, dict):
        raise ValueError("orderbook response was not an object")
    levels = book.get(side)
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"orderbook {side} was empty")
    first = levels[0]
    if not isinstance(first, dict):
        raise ValueError(f"orderbook {side} level was not an object")
    price = _to_decimal(first.get("price"))
    quantity = _to_decimal(first.get("quantity") or first.get("size") or first.get("qty"))
    return price, quantity


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _print_plan(label: str, plan: TopBookPlan) -> None:
    print(f"{label}_market_id={plan.market.market_id}")
    print(f"{label}_market_name={plan.market.name}")
    print(f"{label}_best_bid={fmt_decimal(plan.best_bid)}")
    print(f"{label}_best_bid_qty={fmt_decimal(plan.best_bid_qty)}")
    print(f"{label}_best_ask={fmt_decimal(plan.best_ask)}")
    print(f"{label}_best_ask_qty={fmt_decimal(plan.best_ask_qty)}")
    print(f"{label}_spread_bps={fmt_decimal(plan.spread_bps)}")
    print(f"{label}_entry_fee_bps={fmt_decimal(plan.entry_fee_bps)}")
    print(f"{label}_exit_fee_bps={fmt_decimal(plan.exit_fee_bps)}")
    print(f"{label}_slippage_buffer_bps={fmt_decimal(plan.slippage_buffer_bps)}")
    print(f"{label}_fee_source={plan.fee_source}")
    print(f"{label}_fee_known={plan.fee_known}")
    print(f"{label}_expected_loss_bps={fmt_decimal(plan.expected_loss_bps)}")
    print(f"{label}_size={fmt_decimal(plan.size)}")
    print(f"{label}_size_steps={plan.size_steps}")
    print(f"{label}_side_notional_usd={fmt_decimal(plan.side_notional)}")
    print(f"{label}_estimated_roundtrip_notional_usd={fmt_decimal(plan.estimated_roundtrip_notional)}")


def _emit_dry_run_execution_event(
    *,
    environment: str,
    account_label: str,
    plan: TopBookPlan,
    account_fee: RisexAccountFee | None,
    fee_provider: RisexFeeProvider,
    start_state: MarketStateSummary,
) -> None:
    override = fee_provider.override_by_market.get(str(plan.market.market_id))
    event = ExecutionEvent(
        exchange="risex",
        account_label=account_label,
        wallet_label=account_label,
        market=plan.market.name,
        cycle_id="dry_run",
        environment=environment,
        status="dry_run_ready",
        fee_level=account_fee.fee_level if account_fee is not None else None,
        maker_fee_bps=account_fee.maker_fee_bps if account_fee is not None else None,
        taker_fee_bps=account_fee.taker_fee_bps if account_fee is not None else None,
        entry_fee_bps=plan.entry_fee_bps,
        exit_fee_bps=plan.exit_fee_bps,
        fee_source=plan.fee_source,
        fee_multiplier=override.fee_multiplier if override is not None else Decimal("1"),
        fee_multiplier_expires_at=(
            override.fee_multiplier_expires_at.isoformat()
            if override is not None and override.fee_multiplier_expires_at is not None
            else None
        ),
        live_spread_bps=plan.spread_bps,
        expected_loss_bps=plan.expected_loss_bps,
        planned_gross_volume_usd=plan.estimated_roundtrip_notional,
        filled_gross_volume_usd=Decimal("0"),
        estimated_fee_usd=estimate_roundtrip_fee_usd(
            entry_notional_usd=plan.side_notional,
            exit_notional_usd=plan.side_notional,
            entry_fee_bps=plan.entry_fee_bps,
            exit_fee_bps=plan.exit_fee_bps,
        ),
        estimated_loss_usd=_estimate_roundtrip_loss_usd(
            plan.side_notional,
            plan.expected_loss_bps,
        ),
        points_estimate=None,
        start_position_count=start_state.position_count,
        final_position_count=start_state.position_count,
        start_open_order_count=start_state.open_order_count,
        final_open_order_count=start_state.open_order_count,
    )
    emit_execution_event(event)


def _estimate_roundtrip_loss_usd(one_side_notional_usd: Decimal, expected_loss_bps: Decimal | None) -> Decimal | None:
    if expected_loss_bps is None:
        return None
    return one_side_notional_usd * expected_loss_bps / Decimal("10000")


def _emit_blocked_execution_event(
    *,
    environment: str,
    account_label: str,
    start_state: MarketStateSummary,
    reason: str,
) -> None:
    event = ExecutionEvent(
        exchange="risex",
        account_label=account_label,
        wallet_label=account_label,
        cycle_id="dry_run",
        environment=environment,
        status="blocked",
        start_position_count=start_state.position_count,
        final_position_count=start_state.position_count,
        start_open_order_count=start_state.open_order_count,
        final_open_order_count=start_state.open_order_count,
        error_reason=reason,
    )
    emit_execution_event(event)


def _print_final_state(
    api_endpoint: str,
    account: str,
    markets: tuple[MarketRuntime, ...],
    args: argparse.Namespace,
    total: Decimal,
) -> None:
    print(f"final_estimated_gross_volume_usd={fmt_decimal(total)}")
    all_flat = True
    for market in markets:
        cycle_args = _common_args(args, market.market_id)
        steps = _settled_position_steps(
            api_endpoint,
            account,
            market.step_size,
            cycle_args,
            label=f"final_market_{market.market_id}",
        )
        open_orders_payload = read_only_get_json(
            api_endpoint,
            "/v1/orders/open",
            {"account": account, "market_id": market.market_id, "limit": 25},
            args.timeout_seconds,
            private_readonly=True,
        )
        open_orders = _extract_open_orders(open_orders_payload)
        print(f"final_market_{market.market_id}_open_order_count={len(open_orders)}")
        print(f"final_market_{market.market_id}_position_steps={steps}")
        if steps != 0 or open_orders:
            all_flat = False
    print(f"final_all_flat={all_flat}")


def _build_signed_order_with_nonce(
    *,
    api_endpoint: str,
    credentials: dict[str, str],
    args: SimpleNamespace,
    side: int,
    reduce_only: bool,
    size_steps: int,
    price_ticks: int,
    step_size: Decimal,
    step_price: Decimal,
    nonce_data: dict[str, object],
    label: str,
) -> RisexSignedPlaceOrder | None:
    try:
        domain_payload = read_only_get_json(api_endpoint, "/v1/auth/eip712-domain", {}, args.timeout_seconds)
        domain_data = _data_payload(domain_payload)
        system_payload = read_only_get_json(api_endpoint, "/v1/system/config", {}, args.timeout_seconds)
        system_data = _data_payload(system_payload)
        addresses = system_data.get("addresses", {}) if isinstance(system_data, dict) else {}
        target_contract = addresses.get("router") or addresses.get("auth")
        if not target_contract:
            raise RisexTradingConfigError("system config did not include router/auth target contract")
        draft = build_place_order_draft(
            market_id=args.market_id,
            size_steps=size_steps,
            price_ticks=price_ticks,
            size_wad=_decimal_to_wad(Decimal(size_steps) * step_size),
            price_wad=_decimal_to_wad(Decimal(price_ticks) * step_price) if price_ticks else 0,
            side=side,
            reduce_only=reduce_only,
            client_order_id="0",
            post_only=False,
            stp_mode=0,
            order_type=0,
            time_in_force=3,
            ttl_units=0,
            expiry=0,
        )
        signed = sign_place_order(
            draft=draft,
            account=credentials["account_address"],
            signer=credentials["signer_address"],
            signer_private_key=credentials["signer_private_key"],
            eip712_domain=domain_data,
            target_contract=str(target_contract),
            nonce_anchor=nonce_data["nonce_anchor"],
            nonce_bitmap_index=nonce_data["nonce_bitmap_index"],
            deadline_seconds=int(time.time()) + args.deadline_seconds,
        )
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexTradingConfigError) as exc:
        print(f"{label}_signed_order_ready=False")
        print(f"{label}_signing_error={exc.__class__.__name__}")
        return None

    _print_signed_order_summary(label, signed)
    return signed


def _next_nonce_after_signed(signed: RisexSignedPlaceOrder) -> dict[str, object]:
    if not signed.nonce_anchor or signed.nonce_bitmap_index < 0:
        raise RisexTradingConfigError("signed order did not include nonce data")
    nonce_anchor = int(signed.nonce_anchor)
    bitmap_index = int(signed.nonce_bitmap_index) + 1
    if bitmap_index >= 208:
        nonce_anchor += 1
        bitmap_index = 0
    return {"nonce_anchor": str(nonce_anchor), "nonce_bitmap_index": bitmap_index}


def _next_nonce_data(api_endpoint: str, account: str, timeout_seconds: float) -> dict[str, object]:
    nonce_payload = read_only_get_json(
        api_endpoint,
        nonce_state_path(account),
        {},
        timeout_seconds,
        private_readonly=True,
    )
    data = _data_payload(nonce_payload)
    if not isinstance(data, dict):
        raise RisexTradingConfigError("nonce-state response was not an object")
    nonce_anchor = int(data["nonce_anchor"])
    bitmap_index = int(data["current_bitmap_index"])
    if bitmap_index >= 208:
        nonce_anchor += 1
        bitmap_index = 0
    return {"nonce_anchor": str(nonce_anchor), "nonce_bitmap_index": bitmap_index}


def _decimal_to_wad(value: Decimal) -> int:
    return int((value * Decimal("1000000000000000000")).to_integral_value(rounding=ROUND_FLOOR))


def _print_signed_order_summary(label: str, signed: RisexSignedPlaceOrder) -> None:
    summary = safe_signed_order_summary(signed)
    print(f"{label}_signed_order_ready=True")
    for key, value in summary.items():
        print(f"{label}_{key}={value}")


def _now_ns() -> int:
    return time.perf_counter_ns()


def _elapsed_ms(start_ns: int, end_ns: int | None = None) -> str:
    finish_ns = _now_ns() if end_ns is None else end_ns
    return f"{(finish_ns - start_ns) / 1_000_000:.3f}"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
