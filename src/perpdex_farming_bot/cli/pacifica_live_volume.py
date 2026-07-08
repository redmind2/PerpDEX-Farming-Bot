from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError

from perpdex_farming_bot.cli.pacifica_live_common import (
    PacificaAccountFee,
    PacificaFeeOverride,
    PacificaFeeProvider,
    PacificaMarketInfo,
    fmt_decimal,
    load_all_pacifica_market_info,
    load_pacifica_account_fee,
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
from perpdex_farming_bot.core.execution_cost import (
    MarketCostInput,
    SizingInput,
    calculate_market_cost,
    calculate_sizing,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.base import AdapterError
from perpdex_farming_bot.exchanges.pacifica import PacificaAdapter
from perpdex_farming_bot.marketdata.pacifica import fetch_pacifica_rest_top_of_book
from perpdex_farming_bot.marketdata.spread_monitor import TopOfBook


CONFIRM_TEXT = "LIVE_PACIFICA_1000_VOLUME"
MAX_TARGET_GROSS_VOLUME_USD = Decimal("1000")
MAX_ORDER_NOTIONAL_USD = Decimal("100")
LEDGER_EVENT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class VolumeMarketConfig:
    symbol: str
    display_market: str
    current_spread_bps: Decimal
    spread_5m_bps: Decimal
    spread_1h_bps: Decimal
    spread_24h_bps: Decimal
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    fee_multiplier: Decimal = Decimal("1")
    fee_multiplier_expires_at: datetime | None = None
    slippage_buffer_bps: Decimal = Decimal("0")

    def threshold(self, source: str) -> Decimal:
        if source == "current":
            return self.current_spread_bps
        if source == "5m":
            return self.spread_5m_bps
        if source == "1h":
            return self.spread_1h_bps
        if source == "24h":
            return self.spread_24h_bps
        raise ValueError(f"unknown threshold source: {source}")


@dataclass(frozen=True)
class VolumeCandidate:
    config: VolumeMarketConfig
    market_info: PacificaMarketInfo
    top_of_book: TopOfBook
    threshold_bps: Decimal
    smaller_level_notional_usd: Decimal
    per_side_cap_usd: Decimal
    amount: Decimal
    entry_notional_usd: Decimal
    planned_gross_volume_usd: Decimal
    entry_fee_bps: Decimal
    exit_fee_bps: Decimal
    slippage_buffer_bps: Decimal
    fee_source: str
    fee_known: bool
    expected_loss_bps: Decimal
    eligible: bool
    reason: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Pacifica live volume loop. Re-checks live spreads, chooses the lowest expected loss "
            "market, and only sends orders with --execute-live plus the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/pacifica.live-volume.json")
    parser.add_argument("--environment", default=None)
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="PACIFICA")
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=None)
    parser.add_argument("--order-notional-usd", type=Decimal, default=None)
    parser.add_argument("--level-size-fraction", type=Decimal, default=None)
    parser.add_argument("--threshold-source", choices=("current", "5m", "1h", "24h"), default=None)
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument("--max-idle-cycles", type=int, default=20)
    parser.add_argument("--loop-delay-seconds", type=float, default=1.0)
    parser.add_argument("--min-entry-delay-seconds", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--agg-level", type=int, default=1)
    parser.add_argument("--slippage-percent", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--expiry-window-ms", type=int, default=5000)
    parser.add_argument("--post-order-wait-seconds", type=float, default=2.0)
    parser.add_argument("--position-settle-attempts", type=int, default=8)
    parser.add_argument("--position-settle-delay-seconds", type=float, default=0.5)
    parser.add_argument(
        "--fast-close-on-fill",
        action="store_true",
        help="Submit the reduce-only close immediately after a successful entry POST, without waiting for a position REST check.",
    )
    parser.add_argument(
        "--prebuild-close-order",
        action="store_true",
        help="Pre-sign the second-side order before entry submission. Requires a fast close mode.",
    )
    parser.add_argument(
        "--close-mode",
        choices=("confirmed", "fast-reduce-only", "netting"),
        default=None,
        help="confirmed waits for a position check; fast-reduce-only submits reduce-only close after entry; netting submits the opposite non-reduce-only order after entry.",
    )
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    _normalize_close_mode_args(args)

    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = normalize_pacifica_environment(args.environment or str(plan.get("environment", "production")))
    target_gross_volume_usd = args.target_gross_volume_usd or Decimal(str(plan["target_gross_volume_usd"]))
    order_notional_usd = args.order_notional_usd or Decimal(str(plan["order_notional_usd"]))
    level_size_fraction = args.level_size_fraction or Decimal(str(plan["level_size_fraction"]))
    threshold_source = args.threshold_source or str(plan.get("threshold_source", "24h"))
    markets = _load_market_configs(plan)
    _validate_args(args, target_gross_volume_usd, order_notional_usd, level_size_fraction)

    env_loaded = load_dotenv_if_present(args.env_file)
    credential_env = pacifica_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("pacifica_live_volume=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"config={args.config}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"execute_live={args.execute_live}")
    print(f"target_gross_volume_usd={target_gross_volume_usd}")
    print(f"order_notional_usd={order_notional_usd}")
    print(f"level_size_fraction={level_size_fraction}")
    print(f"threshold_source={threshold_source}")
    print(f"loop_delay_seconds={args.loop_delay_seconds}")
    print(f"fast_close_on_fill={args.fast_close_on_fill}")
    print(f"prebuild_close_order={args.prebuild_close_order}")
    print(f"close_mode={args.close_mode}")
    close_order_type = "market_ask" if args.close_mode == "netting" else "market_reduce_only_ask"
    print(f"close_order_type={close_order_type}")
    print("target_volume_accounting=planned_entry_notional_plus_planned_close_notional")
    print("order_sizing_rule=min(order_notional_usd, remaining_gross_volume_usd/2, smaller_best_bid_or_ask_level_notional*level_size_fraction)")
    print("fresh_orderbook_verify=selected_market_only_before_order")
    print("market_selection=lowest_expected_loss_bps_then_live_spread_bps")
    print("partial_fill_fast_close_policy=planned_amount_reduce_only_close_then_final_reconciliation")
    print(f"ledger_event_schema_version={LEDGER_EVENT_SCHEMA_VERSION}")
    print("ledger_event_output=key_value_lines")
    print("ledger_event_time_scopes=cycle,day_utc,iso_week_utc")
    print("ledger_event_fields=exchange,run_mode,status,market,symbol,cycle,day_utc,iso_week_utc,gross_volume_usd,expected_loss_bps,expected_loss_usd,expected_fee_usd,fee_source,fee_event_status,points_status,final_all_flat,entry_post_latency_ms,close_post_latency_ms,entry_to_close_submit_gap_ms,cycle_total_latency_ms,error_alert")
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
        print("network_skipped=pass_--network_to_run_volume_preflight_or_live_loop")
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
    if private_env is None or signing_missing or not dependencies_ready:
        print("live_ready=False")
        print("reason=missing_private_or_signing_setup")
        return

    account = get_env(credential_env.account_address)
    if not _private_state_is_clean(api_endpoint, account, args):
        return

    market_infos = load_all_pacifica_market_info(api_endpoint, args.timeout_seconds)
    account_fee = _load_account_fee_or_none(api_endpoint, account, args)
    fee_provider = PacificaFeeProvider(
        market_info_by_symbol=market_infos,
        override_by_symbol=_fee_overrides_by_symbol(markets),
        account_fee=account_fee,
    )
    print(f"configured_market_count={len(markets)}")
    print(f"exchange_market_info_count={len(market_infos)}")
    plan_started_ns = _now_ns()
    candidate = _select_candidate(
        api_endpoint=api_endpoint,
        markets=markets,
        market_infos=market_infos,
        fee_provider=fee_provider,
        threshold_source=threshold_source,
        remaining_gross_volume_usd=target_gross_volume_usd,
        order_notional_usd=order_notional_usd,
        level_size_fraction=level_size_fraction,
        timeout_seconds=args.timeout_seconds,
        agg_level=args.agg_level,
    )
    if candidate is None:
        print("live_ready=False")
        print("reason=no_eligible_market")
        _print_ledger_error_event(
            "dry_run_no_eligible",
            run_mode="dry_run",
            status="blocked",
            reason="no_eligible_market",
        )
        return
    print(f"dry_run_plan_latency_ms={_elapsed_ms(plan_started_ns)}")

    _print_candidate("dry_run_selected", candidate)
    dry_adapter = PacificaAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=False,
    )
    sign_entry_started_ns = _now_ns()
    signed_entry = _build_signed_market_order(
        adapter=dry_adapter,
        symbol=candidate.config.symbol,
        amount=candidate.amount,
        side="bid",
        reduce_only=False,
        slippage_percent=args.slippage_percent,
        expiry_window_ms=args.expiry_window_ms,
    )
    print(f"dry_run_entry_sign_latency_ms={_elapsed_ms(sign_entry_started_ns)}")
    if signed_entry is None:
        _print_ledger_error_event(
            "dry_run_entry_signing_failed",
            run_mode="dry_run",
            status="blocked",
            reason="entry_signing_failed",
            candidate=candidate,
        )
        return
    _print_signed_request_summary("dry_run_entry", signed_entry)
    dry_close_ready = False
    if args.close_mode in {"fast-reduce-only", "netting"} and args.prebuild_close_order:
        sign_close_started_ns = _now_ns()
        signed_dry_close = _build_signed_market_order(
            adapter=dry_adapter,
            symbol=candidate.config.symbol,
            amount=candidate.amount,
            side="ask",
            reduce_only=args.close_mode != "netting",
            slippage_percent=args.slippage_percent,
            expiry_window_ms=args.expiry_window_ms,
        )
        print(f"dry_run_close_prebuild_sign_latency_ms={_elapsed_ms(sign_close_started_ns)}")
        if signed_dry_close is None:
            _print_ledger_error_event(
                "dry_run_close_prebuild_signing_failed",
                run_mode="dry_run",
                status="blocked",
                reason="close_prebuild_signing_failed",
                candidate=candidate,
            )
            return
        _print_signed_request_summary("dry_run_close_prebuilt", signed_dry_close)
        print("dry_run_prebuild_close_without_order_post=True")
        dry_close_ready = True
    _print_ledger_cycle_event(
        "dry_run",
        candidate,
        run_mode="dry_run",
        status="dry_run_signed",
        cycle=0,
        order_submit_enabled=False,
        signed_entry_ready=True,
        signed_close_ready=dry_close_ready,
        final_all_flat=None,
    )

    if not args.execute_live:
        print("live_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    _run_live_loop(
        args=args,
        api_endpoint=api_endpoint,
        environment=environment,
        account=account,
        markets=markets,
        market_infos=market_infos,
        fee_provider=fee_provider,
        target_gross_volume_usd=target_gross_volume_usd,
        order_notional_usd=order_notional_usd,
        level_size_fraction=level_size_fraction,
        threshold_source=threshold_source,
    )


def _run_live_loop(
    *,
    args: argparse.Namespace,
    api_endpoint: str,
    environment: str,
    account: str,
    markets: list[VolumeMarketConfig],
    market_infos: dict[str, PacificaMarketInfo],
    fee_provider: PacificaFeeProvider,
    target_gross_volume_usd: Decimal,
    order_notional_usd: Decimal,
    level_size_fraction: Decimal,
    threshold_source: str,
) -> None:
    dry_adapter = PacificaAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=False,
    )
    live_adapter = PacificaAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=True,
    )

    planned_gross_volume = Decimal("0")
    idle_cycles = 0
    print("live_loop_start=True")

    for cycle in range(1, args.max_cycles + 1):
        cycle_started_ns = _now_ns()
        remaining = target_gross_volume_usd - planned_gross_volume
        if remaining <= Decimal("0"):
            print(f"stop_reason=target_volume_reached:{planned_gross_volume:.4f}>={target_gross_volume_usd:.4f}")
            break

        plan_started_ns = _now_ns()
        candidate = _select_candidate(
            api_endpoint=api_endpoint,
            markets=markets,
            market_infos=market_infos,
            fee_provider=fee_provider,
            threshold_source=threshold_source,
            remaining_gross_volume_usd=remaining,
            order_notional_usd=order_notional_usd,
            level_size_fraction=level_size_fraction,
            timeout_seconds=args.timeout_seconds,
            agg_level=args.agg_level,
        )
        if candidate is None:
            if _remaining_below_min_executable(remaining, markets, market_infos):
                print(f"stop_reason=remaining_below_min_executable:{remaining:.4f}")
                _print_ledger_error_event(
                    f"cycle_{cycle}",
                    run_mode="live",
                    status="stopped",
                    reason="remaining_below_min_executable",
                )
                break
            idle_cycles += 1
            print(f"cycle={cycle} eligible_market_count=0 idle_cycles={idle_cycles}")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="idle",
                reason="no_eligible_market",
            )
            if idle_cycles >= args.max_idle_cycles:
                print(f"stop_reason=max_idle_cycles_reached:{idle_cycles}")
                _print_ledger_error_event(
                    f"cycle_{cycle}",
                    run_mode="live",
                    status="stopped",
                    reason="max_idle_cycles_reached",
                )
                break
            if args.poll_seconds:
                time.sleep(args.poll_seconds)
            continue
        idle_cycles = 0

        fresh = _fresh_verify_candidate(
            api_endpoint=api_endpoint,
            candidate=candidate,
            fee_provider=fee_provider,
            threshold_source=threshold_source,
            remaining_gross_volume_usd=remaining,
            order_notional_usd=order_notional_usd,
            level_size_fraction=level_size_fraction,
            timeout_seconds=args.timeout_seconds,
            agg_level=args.agg_level,
        )
        if fresh is None:
            print(f"cycle={cycle} selected_plan_skipped=fresh_verify_failed")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="skipped",
                reason="fresh_verify_failed",
                candidate=candidate,
            )
            if args.poll_seconds:
                time.sleep(args.poll_seconds)
            continue
        print(f"cycle_{cycle}_plan_latency_ms={_elapsed_ms(plan_started_ns)}")

        print(f"cycle={cycle}")
        _print_candidate("live_selected", fresh)

        sign_entry_started_ns = _now_ns()
        signed_entry = _build_signed_market_order(
            adapter=dry_adapter,
            symbol=fresh.config.symbol,
            amount=fresh.amount,
            side="bid",
            reduce_only=False,
            slippage_percent=args.slippage_percent,
            expiry_window_ms=args.expiry_window_ms,
        )
        entry_sign_latency_ms = _elapsed_ms(sign_entry_started_ns)
        print(f"cycle_{cycle}_entry_sign_latency_ms={entry_sign_latency_ms}")
        if signed_entry is None:
            print("stop_reason=entry_signing_failed")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="blocked",
                reason="entry_signing_failed",
                candidate=fresh,
            )
            break

        prebuilt_close: object | None = None
        close_prebuild_sign_latency_ms: str | None = None
        if args.close_mode in {"fast-reduce-only", "netting"} and args.prebuild_close_order:
            sign_close_started_ns = _now_ns()
            prebuilt_close = _build_signed_market_order(
                adapter=dry_adapter,
                symbol=fresh.config.symbol,
                amount=fresh.amount,
                side="ask",
                reduce_only=args.close_mode != "netting",
                slippage_percent=args.slippage_percent,
                expiry_window_ms=args.expiry_window_ms,
            )
            close_prebuild_sign_latency_ms = _elapsed_ms(sign_close_started_ns)
            print(f"cycle_{cycle}_close_prebuild_sign_latency_ms={close_prebuild_sign_latency_ms}")
            if prebuilt_close is None:
                print("stop_reason=close_prebuild_signing_failed")
                _print_ledger_error_event(
                    f"cycle_{cycle}",
                    run_mode="live",
                    status="blocked",
                    reason="close_prebuild_signing_failed",
                    candidate=fresh,
                )
                break

        print("live_entry_submitting=True")
        entry_post_started_ns = _now_ns()
        try:
            entry_result = live_adapter.submit_signed_order_request(signed_entry)
        except AdapterError as exc:
            print("stop_reason=adapter_rejected_entry_order")
            print(f"adapter_error={exc}")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="error",
                reason="adapter_rejected_entry_order",
                candidate=fresh,
            )
            break
        entry_post_done_ns = _now_ns()
        entry_post_latency_ms = _elapsed_ms(entry_post_started_ns, entry_post_done_ns)
        print(f"cycle_{cycle}_entry_post_latency_ms={entry_post_latency_ms}")
        _print_post_result("entry", entry_result)
        if not entry_result.ok:
            print("stop_reason=entry_order_failed")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="error",
                reason="entry_order_failed",
                candidate=fresh,
            )
            break

        entry_order_id = extract_order_id(entry_result.parsed)
        if entry_order_id:
            print(f"entry_order_id={entry_order_id}")

        if args.close_mode in {"fast-reduce-only", "netting"}:
            print("fast_close_on_fill=True")
            print(f"close_mode={args.close_mode}")
            print("post_entry_position_check_skipped=True")
            close_source = "planned_entry_amount_netting" if args.close_mode == "netting" else "planned_entry_amount_reduce_only_fast"
            print(f"close_quantity_source={close_source}")
            close_amount = fresh.amount
            close_side = "ask"
            close_amount_abs = fresh.amount
            if prebuilt_close is not None:
                signed_close = prebuilt_close
                print("close_order_prebuilt=True")
                close_sign_latency_ms = None
            else:
                sign_close_started_ns = _now_ns()
                signed_close = _build_signed_market_order(
                    adapter=dry_adapter,
                    symbol=fresh.config.symbol,
                    amount=close_amount_abs,
                    side=close_side,
                    reduce_only=args.close_mode != "netting",
                    slippage_percent=args.slippage_percent,
                    expiry_window_ms=args.expiry_window_ms,
                )
                close_sign_latency_ms = _elapsed_ms(sign_close_started_ns)
                print(f"cycle_{cycle}_close_sign_latency_ms={close_sign_latency_ms}")
                if signed_close is None:
                    print("stop_reason=close_signing_failed")
                    _print_ledger_error_event(
                        f"cycle_{cycle}",
                        run_mode="live",
                        status="manual_review_required",
                        reason="close_signing_failed",
                        candidate=fresh,
                    )
                    break
        else:
            if args.post_order_wait_seconds:
                time.sleep(args.post_order_wait_seconds)

            close_amount = _settled_position_amount(api_endpoint, account, fresh.config.symbol, args)
            if close_amount == 0:
                print("post_entry_position_snapshot=flat_or_delayed")
                print("close_quantity_source=planned_entry_amount_reduce_only_rescue")
                close_amount = fresh.amount
            else:
                print("close_quantity_source=detected_position_amount")

            close_side = "ask" if close_amount > 0 else "bid"
            close_amount_abs = abs(close_amount)
            sign_close_started_ns = _now_ns()
            signed_close = _build_signed_market_order(
                adapter=dry_adapter,
                symbol=fresh.config.symbol,
                amount=close_amount_abs,
                side=close_side,
                reduce_only=True,
                slippage_percent=args.slippage_percent,
                expiry_window_ms=args.expiry_window_ms,
            )
            close_sign_latency_ms = _elapsed_ms(sign_close_started_ns)
            print(f"cycle_{cycle}_close_sign_latency_ms={close_sign_latency_ms}")
            if signed_close is None:
                print("stop_reason=close_signing_failed")
                _print_ledger_error_event(
                    f"cycle_{cycle}",
                    run_mode="live",
                    status="manual_review_required",
                    reason="close_signing_failed",
                    candidate=fresh,
                )
                break

        print("live_close_submitting=True")
        entry_to_close_submit_gap_ms = _elapsed_ms(entry_post_done_ns)
        print(f"cycle_{cycle}_entry_to_close_submit_gap_ms={entry_to_close_submit_gap_ms}")
        close_post_started_ns = _now_ns()
        try:
            close_result = live_adapter.submit_signed_order_request(signed_close)
        except AdapterError as exc:
            print("stop_reason=adapter_rejected_close_order")
            print(f"adapter_error={exc}")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="manual_review_required",
                reason="adapter_rejected_close_order",
                candidate=fresh,
            )
            break
        close_post_done_ns = _now_ns()
        close_post_latency_ms = _elapsed_ms(close_post_started_ns, close_post_done_ns)
        print(f"cycle_{cycle}_close_post_latency_ms={close_post_latency_ms}")
        _print_post_result("close", close_result)
        if not close_result.ok:
            print("stop_reason=close_order_failed_manual_review_required")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="manual_review_required",
                reason="close_order_failed",
                candidate=fresh,
            )
            break
        close_order_id = extract_order_id(close_result.parsed)
        if close_order_id:
            print(f"close_order_id={close_order_id}")

        if args.post_order_wait_seconds:
            time.sleep(args.post_order_wait_seconds)
        final_amount = _settled_position_amount(api_endpoint, account, fresh.config.symbol, args, label="final")
        print(f"final_position_amount={fmt_decimal(final_amount)}")
        if final_amount != 0 and args.close_mode == "netting":
            print("netting_residual_position_detected=True")
            rescue_side = "ask" if final_amount > 0 else "bid"
            rescue_amount_abs = abs(final_amount)
            sign_rescue_started_ns = _now_ns()
            signed_rescue = _build_signed_market_order(
                adapter=dry_adapter,
                symbol=fresh.config.symbol,
                amount=rescue_amount_abs,
                side=rescue_side,
                reduce_only=True,
                slippage_percent=args.slippage_percent,
                expiry_window_ms=args.expiry_window_ms,
            )
            print(f"cycle_{cycle}_rescue_close_sign_latency_ms={_elapsed_ms(sign_rescue_started_ns)}")
            if signed_rescue is None:
                print("stop_reason=netting_rescue_signing_failed_manual_review_required")
                _print_ledger_error_event(
                    f"cycle_{cycle}",
                    run_mode="live",
                    status="manual_review_required",
                    reason="netting_rescue_signing_failed",
                    candidate=fresh,
                )
                break
            rescue_post_started_ns = _now_ns()
            try:
                rescue_result = live_adapter.submit_signed_order_request(signed_rescue)
            except AdapterError as exc:
                print("stop_reason=adapter_rejected_netting_rescue_order")
                print(f"adapter_error={exc}")
                _print_ledger_error_event(
                    f"cycle_{cycle}",
                    run_mode="live",
                    status="manual_review_required",
                    reason="adapter_rejected_netting_rescue_order",
                    candidate=fresh,
                )
                break
            rescue_post_done_ns = _now_ns()
            print(f"cycle_{cycle}_rescue_close_post_latency_ms={_elapsed_ms(rescue_post_started_ns, rescue_post_done_ns)}")
            _print_post_result("netting_rescue_close", rescue_result)
            if not rescue_result.ok:
                print("stop_reason=netting_rescue_order_failed_manual_review_required")
                _print_ledger_error_event(
                    f"cycle_{cycle}",
                    run_mode="live",
                    status="manual_review_required",
                    reason="netting_rescue_order_failed",
                    candidate=fresh,
                )
                break
            close_post_done_ns = rescue_post_done_ns
            if args.post_order_wait_seconds:
                time.sleep(args.post_order_wait_seconds)
            final_amount = _settled_position_amount(
                api_endpoint,
                account,
                fresh.config.symbol,
                args,
                label="final_after_netting_rescue",
            )
            print(f"final_position_amount_after_netting_rescue={fmt_decimal(final_amount)}")
        if final_amount != 0:
            print("stop_reason=position_remains_manual_review_required")
            _print_ledger_error_event(
                f"cycle_{cycle}",
                run_mode="live",
                status="manual_review_required",
                reason="position_remains_after_roundtrip",
                candidate=fresh,
            )
            break

        round_gross = fresh.entry_notional_usd + (close_amount_abs * fresh.top_of_book.best_bid)
        planned_gross_volume += round_gross
        print(f"cycle_planned_gross_volume_usd={round_gross:.4f}")
        print(f"live_total_planned_gross_volume_usd={planned_gross_volume:.4f}")
        cycle_total_latency_ms = _elapsed_ms(cycle_started_ns, close_post_done_ns)
        print(f"cycle_{cycle}_total_latency_ms={cycle_total_latency_ms}")
        _print_ledger_cycle_event(
            f"cycle_{cycle}",
            fresh,
            run_mode="live",
            status="live_roundtrip_closed",
            cycle=cycle,
            order_submit_enabled=True,
            signed_entry_ready=True,
            signed_close_ready=True,
            final_all_flat=True,
            entry_order_id_present=bool(entry_order_id),
            close_order_id_present=bool(close_order_id),
            entry_sign_latency_ms=entry_sign_latency_ms,
            close_sign_latency_ms=close_sign_latency_ms,
            close_prebuild_sign_latency_ms=close_prebuild_sign_latency_ms,
            entry_post_latency_ms=entry_post_latency_ms,
            close_post_latency_ms=close_post_latency_ms,
            entry_to_close_submit_gap_ms=entry_to_close_submit_gap_ms,
            cycle_total_latency_ms=cycle_total_latency_ms,
        )
        if planned_gross_volume >= target_gross_volume_usd:
            print(f"stop_reason=target_volume_reached:{planned_gross_volume:.4f}>={target_gross_volume_usd:.4f}")
            break
        if args.loop_delay_seconds:
            time.sleep(args.loop_delay_seconds)
    else:
        print(f"stop_reason=max_cycles_reached:{args.max_cycles}")

    print(f"final_estimated_gross_volume_usd={planned_gross_volume:.4f}")
    print(f"final_planned_gross_volume_usd={planned_gross_volume:.4f}")
    print(f"target_gross_volume_usd={target_gross_volume_usd:.4f}")
    _print_final_state(api_endpoint, account, args, markets, market_infos)


def _load_market_configs(plan: dict[str, object]) -> list[VolumeMarketConfig]:
    markets: list[VolumeMarketConfig] = []
    for item in plan.get("markets", []):
        if not isinstance(item, dict):
            continue
        symbol = str(item["symbol"])
        markets.append(
            VolumeMarketConfig(
                symbol=symbol,
                display_market=str(item.get("display_market") or f"{symbol}-PERP"),
                current_spread_bps=Decimal(str(item["current_spread_bps"])),
                spread_5m_bps=Decimal(str(item["spread_5m_bps"])),
                spread_1h_bps=Decimal(str(item["spread_1h_bps"])),
                spread_24h_bps=Decimal(str(item["spread_24h_bps"])),
                entry_fee_bps=_optional_decimal(item, "entry_fee_bps"),
                exit_fee_bps=_optional_decimal(item, "exit_fee_bps"),
                fee_multiplier=Decimal(str(item.get("fee_multiplier", "1"))),
                fee_multiplier_expires_at=_optional_datetime(item, "fee_multiplier_expires_at"),
                slippage_buffer_bps=Decimal(str(item.get("slippage_buffer_bps", "0"))),
            )
        )
    return markets


def _load_account_fee_or_none(api_endpoint: str, account: str, args: argparse.Namespace) -> PacificaAccountFee | None:
    try:
        account_fee = load_pacifica_account_fee(api_endpoint, account, args.timeout_seconds)
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
        print(f"account_fee_lookup_error={exc.__class__.__name__}")
        return None
    print("account_fee_source=account_api")
    print(f"account_fee_level={account_fee.fee_level if account_fee.fee_level is not None else 'unknown'}")
    print(f"account_maker_fee_bps={account_fee.maker_fee_bps}")
    print(f"account_taker_fee_bps={account_fee.taker_fee_bps}")
    return account_fee


def _fee_overrides_by_symbol(markets: list[VolumeMarketConfig]) -> dict[str, PacificaFeeOverride]:
    return {
        market.symbol: PacificaFeeOverride(
            symbol=market.symbol,
            entry_fee_bps=market.entry_fee_bps,
            exit_fee_bps=market.exit_fee_bps,
            fee_multiplier=market.fee_multiplier,
            fee_multiplier_expires_at=market.fee_multiplier_expires_at,
            slippage_buffer_bps=market.slippage_buffer_bps,
            source="config_override",
        )
        for market in markets
    }


def _optional_decimal(payload: dict[str, object], key: str) -> Decimal | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _optional_datetime(payload: dict[str, object], key: str) -> datetime | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _validate_args(
    args: argparse.Namespace,
    target_gross_volume_usd: Decimal,
    order_notional_usd: Decimal,
    level_size_fraction: Decimal,
) -> None:
    if target_gross_volume_usd <= 0:
        raise SystemExit("--target-gross-volume-usd must be greater than zero")
    if target_gross_volume_usd > MAX_TARGET_GROSS_VOLUME_USD:
        raise SystemExit(f"--target-gross-volume-usd must be <= {MAX_TARGET_GROSS_VOLUME_USD} for this guarded live CLI")
    if order_notional_usd <= 0:
        raise SystemExit("--order-notional-usd must be greater than zero")
    if order_notional_usd > MAX_ORDER_NOTIONAL_USD:
        raise SystemExit(f"--order-notional-usd must be <= {MAX_ORDER_NOTIONAL_USD} for this guarded live CLI")
    if level_size_fraction <= 0 or level_size_fraction > 1:
        raise SystemExit("--level-size-fraction must be greater than 0 and <= 1")
    if args.max_cycles <= 0 or args.max_cycles > 20:
        raise SystemExit("--max-cycles must be greater than 0 and <= 20")
    if args.max_idle_cycles <= 0:
        raise SystemExit("--max-idle-cycles must be greater than zero")
    if args.loop_delay_seconds < 0 or args.loop_delay_seconds > 10:
        raise SystemExit("--loop-delay-seconds must be between 0 and 10")
    if args.min_entry_delay_seconds is not None and args.min_entry_delay_seconds < 0:
        raise SystemExit("--min-entry-delay-seconds is deprecated; use --loop-delay-seconds")
    if args.poll_seconds < 0:
        raise SystemExit("--poll-seconds must be zero or greater")
    if args.slippage_percent <= 0 or args.slippage_percent > Decimal("2"):
        raise SystemExit("--slippage-percent must be > 0 and <= 2")
    if args.expiry_window_ms <= 0 or args.expiry_window_ms > 30_000:
        raise SystemExit("--expiry-window-ms must be > 0 and <= 30000")
    if args.position_settle_attempts <= 0:
        raise SystemExit("--position-settle-attempts must be greater than zero")
    if args.position_settle_delay_seconds < 0:
        raise SystemExit("--position-settle-delay-seconds must be zero or greater")
    if args.prebuild_close_order and args.close_mode == "confirmed":
        raise SystemExit("--prebuild-close-order requires --fast-close-on-fill or --close-mode netting")


def _normalize_close_mode_args(args: argparse.Namespace) -> None:
    if args.close_mode is None:
        args.close_mode = "fast-reduce-only" if args.fast_close_on_fill else "confirmed"
    if args.close_mode in {"fast-reduce-only", "netting"}:
        args.fast_close_on_fill = True


def _dependencies_ready() -> bool:
    return importlib.util.find_spec("base58") is not None and importlib.util.find_spec("solders") is not None


def _remaining_below_min_executable(
    remaining_gross_volume_usd: Decimal,
    markets: list[VolumeMarketConfig],
    market_infos: dict[str, PacificaMarketInfo],
) -> bool:
    min_order_sizes = [
        market_infos[market.symbol].min_order_size_usd
        for market in markets
        if market.symbol in market_infos and market_infos[market.symbol].min_order_size_usd > 0
    ]
    if not min_order_sizes:
        return False
    return remaining_gross_volume_usd / Decimal("2") < min(min_order_sizes)


def _private_state_is_clean(api_endpoint: str, account: str, args: argparse.Namespace) -> bool:
    try:
        positions = load_pacifica_positions(api_endpoint, account, args.timeout_seconds)
        nonzero_positions = nonzero_pacifica_positions(positions)
        open_orders, last_order_id = load_pacifica_open_orders(api_endpoint, account, args.timeout_seconds)
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
        print("live_ready=False")
        print(f"private_state_error={exc.__class__.__name__}")
        return False

    print(f"start_position_count={len(nonzero_positions)}")
    print(f"start_open_order_count={len(open_orders)}")
    if last_order_id:
        print(f"last_order_id={last_order_id}")
    if nonzero_positions:
        print("live_ready=False")
        print("reason=existing_positions_detected")
        print("existing_position_markets=" + ",".join(sorted({pacifica_position_symbol(item) for item in nonzero_positions})))
        return False
    if open_orders:
        print("live_ready=False")
        print("reason=existing_open_orders_detected")
        return False
    return True


def _select_candidate(
    *,
    api_endpoint: str,
    markets: list[VolumeMarketConfig],
    market_infos: dict[str, PacificaMarketInfo],
    fee_provider: PacificaFeeProvider,
    threshold_source: str,
    remaining_gross_volume_usd: Decimal,
    order_notional_usd: Decimal,
    level_size_fraction: Decimal,
    timeout_seconds: float,
    agg_level: int,
) -> VolumeCandidate | None:
    candidates: list[VolumeCandidate] = []
    for market in markets:
        market_info = market_infos.get(market.symbol)
        if market_info is None:
            candidate = _empty_candidate(market, Decimal("0"), "market_info_missing")
        else:
            result = fetch_pacifica_rest_top_of_book(
                api_endpoint,
                symbol=market.symbol,
                timeout_seconds=timeout_seconds,
                agg_level=agg_level,
            )
            if not result.ok or result.snapshot is None:
                candidate = _empty_candidate(market, market.threshold(threshold_source), result.reason)
            else:
                candidate = _candidate_from_snapshot(
                    market=market,
                    market_info=market_info,
                    fee_provider=fee_provider,
                    top_of_book=result.snapshot,
                    threshold_source=threshold_source,
                    remaining_gross_volume_usd=remaining_gross_volume_usd,
                    order_notional_usd=order_notional_usd,
                    level_size_fraction=level_size_fraction,
                )
        candidates.append(candidate)

    eligible = [candidate for candidate in candidates if candidate.eligible]
    print(f"eligible_market_count={len(eligible)}")
    for candidate in candidates:
        print(
            f"candidate={candidate.config.display_market} eligible={candidate.eligible} "
            f"live_spread_bps={candidate.top_of_book.spread_bps:.4f} "
            f"threshold_bps={candidate.threshold_bps:.4f} "
            f"expected_loss_bps={candidate.expected_loss_bps:.4f} "
            f"fee_source={candidate.fee_source} "
            f"amount={fmt_decimal(candidate.amount)} reason={candidate.reason}"
        )
    if not eligible:
        return None
    return min(eligible, key=lambda item: (item.expected_loss_bps, item.top_of_book.spread_bps, item.config.symbol))


def _fresh_verify_candidate(
    *,
    api_endpoint: str,
    candidate: VolumeCandidate,
    fee_provider: PacificaFeeProvider,
    threshold_source: str,
    remaining_gross_volume_usd: Decimal,
    order_notional_usd: Decimal,
    level_size_fraction: Decimal,
    timeout_seconds: float,
    agg_level: int,
) -> VolumeCandidate | None:
    result = fetch_pacifica_rest_top_of_book(
        api_endpoint,
        symbol=candidate.config.symbol,
        timeout_seconds=timeout_seconds,
        agg_level=agg_level,
    )
    if not result.ok or result.snapshot is None:
        print(f"fresh_verify_error={result.reason}")
        return None
    fresh = _candidate_from_snapshot(
        market=candidate.config,
        market_info=candidate.market_info,
        fee_provider=fee_provider,
        top_of_book=result.snapshot,
        threshold_source=threshold_source,
        remaining_gross_volume_usd=remaining_gross_volume_usd,
        order_notional_usd=order_notional_usd,
        level_size_fraction=level_size_fraction,
    )
    _print_candidate("fresh_verify", fresh)
    if not fresh.eligible:
        return None
    return fresh


def _candidate_from_snapshot(
    *,
    market: VolumeMarketConfig,
    market_info: PacificaMarketInfo,
    fee_provider: PacificaFeeProvider,
    top_of_book: TopOfBook,
    threshold_source: str,
    remaining_gross_volume_usd: Decimal,
    order_notional_usd: Decimal,
    level_size_fraction: Decimal,
) -> VolumeCandidate:
    threshold = market.threshold(threshold_source)
    fee = fee_provider.fee_for_market(market.symbol)
    cost = calculate_market_cost(
        MarketCostInput(
            exchange_id="pacifica",
            market=market.symbol,
            live_spread_bps=top_of_book.spread_bps,
            fee=fee,
        )
    )
    sizing = calculate_sizing(
        SizingInput(
            exchange_id="pacifica",
            market=market.symbol,
            best_bid=top_of_book.best_bid,
            best_ask=top_of_book.best_ask,
            best_bid_size=top_of_book.best_bid_size,
            best_ask_size=top_of_book.best_ask_size,
            order_notional_usd=order_notional_usd,
            remaining_gross_volume_usd=remaining_gross_volume_usd,
            level_size_fraction=level_size_fraction,
            lot_size=market_info.lot_size,
            min_order_size_usd=market_info.min_order_size_usd,
        )
    )
    eligible = True
    reason = "spread_and_size_ok"
    if not cost.eligible:
        eligible = False
        reason = cost.reason
    elif top_of_book.spread_bps > threshold:
        eligible = False
        reason = f"spread_above_{threshold_source}:{top_of_book.spread_bps:.4f}>{threshold:.4f}"
    elif not sizing.eligible:
        eligible = False
        reason = sizing.reason
    return VolumeCandidate(
        config=market,
        market_info=market_info,
        top_of_book=top_of_book,
        threshold_bps=threshold,
        smaller_level_notional_usd=sizing.smaller_top_level_notional_usd,
        per_side_cap_usd=sizing.per_side_cap_usd,
        amount=sizing.amount,
        entry_notional_usd=sizing.entry_notional_usd,
        planned_gross_volume_usd=sizing.planned_gross_volume_usd,
        entry_fee_bps=cost.entry_fee_bps,
        exit_fee_bps=cost.exit_fee_bps,
        slippage_buffer_bps=cost.slippage_buffer_bps,
        fee_source=cost.fee_source,
        fee_known=cost.fee_known,
        expected_loss_bps=cost.expected_loss_bps,
        eligible=eligible,
        reason=reason,
    )


def _empty_candidate(market: VolumeMarketConfig, threshold: Decimal, reason: str) -> VolumeCandidate:
    empty_top = TopOfBook(
        exchange_id="pacifica",
        market=market.symbol,
        best_bid=Decimal("0"),
        best_ask=Decimal("0"),
        best_bid_size=Decimal("0"),
        best_ask_size=Decimal("0"),
        timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        source="missing",
    )
    return VolumeCandidate(
        config=market,
        market_info=PacificaMarketInfo(market.symbol, Decimal("0"), Decimal("0"), Decimal("0"), ()),
        top_of_book=empty_top,
        threshold_bps=threshold,
        smaller_level_notional_usd=Decimal("0"),
        per_side_cap_usd=Decimal("0"),
        amount=Decimal("0"),
        entry_notional_usd=Decimal("0"),
        planned_gross_volume_usd=Decimal("0"),
        entry_fee_bps=Decimal("0"),
        exit_fee_bps=Decimal("0"),
        slippage_buffer_bps=market.slippage_buffer_bps,
        fee_source="fee_unknown",
        fee_known=False,
        expected_loss_bps=Decimal("999999"),
        eligible=False,
        reason=reason,
    )


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


def _print_candidate(prefix: str, candidate: VolumeCandidate) -> None:
    print(f"{prefix}_market={candidate.config.display_market}")
    print(f"{prefix}_symbol={candidate.config.symbol}")
    print(f"{prefix}_live_spread_bps={candidate.top_of_book.spread_bps:.4f}")
    print(f"{prefix}_threshold_bps={candidate.threshold_bps:.4f}")
    print(f"{prefix}_entry_fee_bps={candidate.entry_fee_bps}")
    print(f"{prefix}_exit_fee_bps={candidate.exit_fee_bps}")
    print(f"{prefix}_slippage_buffer_bps={candidate.slippage_buffer_bps}")
    print(f"{prefix}_fee_source={candidate.fee_source}")
    print(f"{prefix}_fee_known={candidate.fee_known}")
    print(f"{prefix}_expected_loss_bps={candidate.expected_loss_bps:.4f}")
    print(f"{prefix}_best_bid={fmt_decimal(candidate.top_of_book.best_bid)}")
    print(f"{prefix}_best_ask={fmt_decimal(candidate.top_of_book.best_ask)}")
    print(f"{prefix}_best_bid_size={fmt_decimal(candidate.top_of_book.best_bid_size)}")
    print(f"{prefix}_best_ask_size={fmt_decimal(candidate.top_of_book.best_ask_size)}")
    print(f"{prefix}_smaller_level_notional_usd={candidate.smaller_level_notional_usd:.4f}")
    print(f"{prefix}_per_side_cap_usd={candidate.per_side_cap_usd:.4f}")
    print(f"{prefix}_amount={fmt_decimal(candidate.amount)}")
    print(f"{prefix}_entry_notional_usd={candidate.entry_notional_usd:.4f}")
    print(f"{prefix}_planned_gross_volume_usd={candidate.planned_gross_volume_usd:.4f}")
    print(f"{prefix}_eligible={candidate.eligible}")
    print(f"{prefix}_reason={candidate.reason}")


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


def _print_final_state(
    api_endpoint: str,
    account: str,
    args: argparse.Namespace,
    markets: list[VolumeMarketConfig],
    market_infos: dict[str, PacificaMarketInfo],
) -> None:
    try:
        positions = load_pacifica_positions(api_endpoint, account, args.timeout_seconds)
        open_orders, last_order_id = load_pacifica_open_orders(api_endpoint, account, args.timeout_seconds)
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
        print(f"final_state_error={exc.__class__.__name__}")
        return
    nonzero_positions = nonzero_pacifica_positions(positions)
    print(f"final_position_count={len(nonzero_positions)}")
    print(f"final_open_order_count={len(open_orders)}")
    if last_order_id:
        print(f"final_last_order_id={last_order_id}")
    position_amounts: dict[str, Decimal] = {}
    for position in nonzero_positions:
        symbol = pacifica_position_symbol(position)
        position_amounts[symbol] = position_amounts.get(symbol, Decimal("0")) + pacifica_signed_position_amount(position)
    symbols = sorted({market.symbol for market in markets} | set(position_amounts))
    for symbol in symbols:
        amount = position_amounts.get(symbol, Decimal("0"))
        lot_size = market_infos[symbol].lot_size if symbol in market_infos else Decimal("0")
        print(f"final_market_{symbol}_position_size={fmt_decimal(amount)}")
        print(f"final_market_{symbol}_position_steps={_position_steps(amount, lot_size)}")
    all_flat = not nonzero_positions and not open_orders
    print(f"final_all_flat={all_flat}")
    _print_ledger_event(
        "final",
        {
            "event_type": "run_final_state",
            "exchange": "pacifica",
            "run_mode": "live",
            "status": "flat" if all_flat else "manual_review_required",
            "position_count": len(nonzero_positions),
            "open_order_count": len(open_orders),
            "all_flat": all_flat,
            "error_alert": not all_flat,
            "error_reason": "" if all_flat else "final_position_or_open_order_remaining",
            **_ledger_period_fields(),
        },
    )


def _print_ledger_cycle_event(
    prefix: str,
    candidate: VolumeCandidate,
    *,
    run_mode: str,
    status: str,
    cycle: int,
    order_submit_enabled: bool,
    signed_entry_ready: bool,
    signed_close_ready: bool,
    final_all_flat: bool | None,
    entry_order_id_present: bool | None = None,
    close_order_id_present: bool | None = None,
    entry_sign_latency_ms: str | None = None,
    close_sign_latency_ms: str | None = None,
    close_prebuild_sign_latency_ms: str | None = None,
    entry_post_latency_ms: str | None = None,
    close_post_latency_ms: str | None = None,
    entry_to_close_submit_gap_ms: str | None = None,
    cycle_total_latency_ms: str | None = None,
) -> None:
    entry_notional = candidate.entry_notional_usd
    exit_notional = candidate.planned_gross_volume_usd - candidate.entry_notional_usd
    one_side_notional = candidate.planned_gross_volume_usd / Decimal("2")
    expected_fee_usd = (
        entry_notional * candidate.entry_fee_bps / Decimal("10000")
        + exit_notional * candidate.exit_fee_bps / Decimal("10000")
    )
    expected_spread_loss_usd = entry_notional - exit_notional
    if expected_spread_loss_usd < 0:
        expected_spread_loss_usd = Decimal("0")
    expected_slippage_buffer_usd = one_side_notional * candidate.slippage_buffer_bps / Decimal("10000")
    expected_loss_usd = one_side_notional * candidate.expected_loss_bps / Decimal("10000")
    fee_event = _fee_event_fields(candidate)
    fields: dict[str, object] = {
        "event_type": "cycle_result",
        "exchange": "pacifica",
        "run_mode": run_mode,
        "status": status,
        "market": candidate.config.display_market,
        "symbol": candidate.config.symbol,
        "cycle": cycle,
        "gross_volume_usd": candidate.planned_gross_volume_usd,
        "cycle_gross_volume_usd": candidate.planned_gross_volume_usd,
        "cycle_volume_usd_delta": candidate.planned_gross_volume_usd,
        "day_volume_usd_delta": candidate.planned_gross_volume_usd,
        "week_volume_usd_delta": candidate.planned_gross_volume_usd,
        "volume_status": "planned",
        "expected_loss_bps": candidate.expected_loss_bps,
        "loss_status": "expected",
        "loss_usd": expected_loss_usd,
        "cycle_expected_loss_usd": expected_loss_usd,
        "day_expected_loss_usd_delta": expected_loss_usd,
        "week_expected_loss_usd_delta": expected_loss_usd,
        "expected_fee_usd": expected_fee_usd,
        "expected_spread_loss_usd": expected_spread_loss_usd,
        "expected_slippage_buffer_usd": expected_slippage_buffer_usd,
        "entry_fee_bps": candidate.entry_fee_bps,
        "exit_fee_bps": candidate.exit_fee_bps,
        "slippage_buffer_bps": candidate.slippage_buffer_bps,
        "fee_source": candidate.fee_source,
        "fee_known": candidate.fee_known,
        "fee_event_source": candidate.fee_source,
        "points_status": "not_available",
        "points_source": "not_available",
        "cycle_points_delta": "unknown",
        "day_points_delta": "unknown",
        "week_points_delta": "unknown",
        "order_submit_enabled": order_submit_enabled,
        "signed_entry_ready": signed_entry_ready,
        "signed_close_ready": signed_close_ready,
        "final_all_flat": "unknown" if final_all_flat is None else final_all_flat,
        "entry_order_id_present": "unknown" if entry_order_id_present is None else entry_order_id_present,
        "close_order_id_present": "unknown" if close_order_id_present is None else close_order_id_present,
        "entry_sign_latency_ms": entry_sign_latency_ms or "unknown",
        "close_sign_latency_ms": close_sign_latency_ms or "unknown",
        "close_prebuild_sign_latency_ms": close_prebuild_sign_latency_ms or "unknown",
        "entry_post_latency_ms": entry_post_latency_ms or "unknown",
        "close_post_latency_ms": close_post_latency_ms or "unknown",
        "entry_to_close_submit_gap_ms": entry_to_close_submit_gap_ms or "unknown",
        "cycle_total_latency_ms": cycle_total_latency_ms or "unknown",
        "error_alert": False,
        "error_reason": "",
        **fee_event,
        **_ledger_period_fields(),
    }
    _print_ledger_event(prefix, fields)


def _print_ledger_error_event(
    prefix: str,
    *,
    run_mode: str,
    status: str,
    reason: str,
    candidate: VolumeCandidate | None = None,
) -> None:
    fields: dict[str, object] = {
        "event_type": "error_alert",
        "exchange": "pacifica",
        "run_mode": run_mode,
        "status": status,
        "market": candidate.config.display_market if candidate is not None else "",
        "symbol": candidate.config.symbol if candidate is not None else "",
        "cycle": "unknown",
        "gross_volume_usd": Decimal("0"),
        "cycle_gross_volume_usd": Decimal("0"),
        "cycle_volume_usd_delta": Decimal("0"),
        "day_volume_usd_delta": Decimal("0"),
        "week_volume_usd_delta": Decimal("0"),
        "planned_candidate_gross_volume_usd": candidate.planned_gross_volume_usd if candidate is not None else Decimal("0"),
        "volume_status": "not_counted_error",
        "expected_loss_bps": candidate.expected_loss_bps if candidate is not None else Decimal("999999"),
        "loss_status": "not_counted_error",
        "loss_usd": Decimal("0"),
        "cycle_expected_loss_usd": Decimal("0"),
        "day_expected_loss_usd_delta": Decimal("0"),
        "week_expected_loss_usd_delta": Decimal("0"),
        "fee_source": candidate.fee_source if candidate is not None else "unknown",
        "fee_event_status": _fee_event_status(candidate) if candidate is not None else "unknown",
        "fee_event_source": candidate.fee_source if candidate is not None else "unknown",
        "points_status": "not_available",
        "points_source": "not_available",
        "cycle_points_delta": "unknown",
        "day_points_delta": "unknown",
        "week_points_delta": "unknown",
        "error_alert": True,
        "error_reason": reason,
        **_ledger_period_fields(),
    }
    _print_ledger_event(prefix, fields)


def _fee_event_fields(candidate: VolumeCandidate) -> dict[str, object]:
    expires_at = candidate.config.fee_multiplier_expires_at
    return {
        "fee_event_status": _fee_event_status(candidate),
        "fee_event_multiplier": candidate.config.fee_multiplier,
        "fee_event_expires_at": expires_at.isoformat() if expires_at is not None else "",
    }


def _fee_event_status(candidate: VolumeCandidate) -> str:
    if candidate.config.entry_fee_bps is not None and candidate.config.exit_fee_bps is not None:
        return "exact_override"
    if candidate.config.fee_multiplier == Decimal("1"):
        return "none"
    if candidate.fee_source.endswith("_config_multiplier"):
        return "active"
    if candidate.fee_source.endswith("_config_multiplier_missing_expiry_ignored"):
        return "missing_expiry_ignored"
    if candidate.fee_source.endswith("_config_multiplier_expired_ignored"):
        return "expired_ignored"
    return "configured_but_not_applied"


def _ledger_period_fields() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    return {
        "event_time_utc": now.isoformat(),
        "day_utc": now.date().isoformat(),
        "iso_week_utc": f"{iso_year}-W{iso_week:02d}",
    }


def _print_ledger_event(prefix: str, fields: dict[str, object]) -> None:
    print(f"{prefix}_ledger_event_schema_version={LEDGER_EVENT_SCHEMA_VERSION}")
    for key, value in fields.items():
        print(f"{prefix}_ledger_{key}={_ledger_value(value)}")


def _ledger_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return fmt_decimal(value)
    if value is None:
        return ""
    text = str(value)
    return text.replace("\r", " ").replace("\n", " ")


def _position_steps(amount: Decimal, lot_size: Decimal) -> str:
    if lot_size <= 0:
        return "unknown"
    return fmt_decimal(amount / lot_size)


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
