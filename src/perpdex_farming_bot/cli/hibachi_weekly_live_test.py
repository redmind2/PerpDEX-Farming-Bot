from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Literal

from perpdex_farming_bot.budget import current_weekly_window
from perpdex_farming_bot.cli.hibachi_live_common import (
    HibachiAccountFee,
    HibachiFeeOverride,
    HibachiFeeProvider,
    load_hibachi_account_fee,
    load_hibachi_metadata_fee,
)
from perpdex_farming_bot.cli.hibachi_live_roundtrip import (
    _elapsed_ms,
    _execute_fast_close_market_roundtrip,
    _now_ns,
    _open_order_count,
    _opposite_side,
    _paired_market_sides,
    _position_state,
)
from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    endpoint_from_env,
)
from perpdex_farming_bot.connectors.hibachi_sdk_public import load_hibachi_orderbook_snapshot
from perpdex_farming_bot.core import MarketCostInput, MarketCostResult, RoundtripPlan, calculate_market_cost, execute_roundtrip_plan
from perpdex_farming_bot.credentials import (
    hibachi_available_credential_env,
    hibachi_missing_required,
    read_hibachi_credentials,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present
from perpdex_farming_bot.marketdata import MarketSpec, RestBackoff, SpreadCache
from perpdex_farming_bot.marketdata.hibachi import refresh_hibachi_spread_cache
from perpdex_farming_bot.exchanges.hibachi import HibachiAdapter
from perpdex_farming_bot.security.secrets import assert_no_plaintext_secrets
from perpdex_farming_bot.storage import LIVE_RUN_EVENT_COLUMNS, WeeklyLedger


CONFIRM_TEXT = "LIVE_HIBACHI_WEEKLY_TEST"
TARGET_VOLUME_TOLERANCE_USD = Decimal("0.01")


@dataclass(frozen=True)
class ExecutionSizing:
    max_order_notional_usd: Decimal
    market_order_safety_max_notional_usd: Decimal
    level_size_fraction: Decimal
    market_order_liquidity_safety_fraction: Decimal


@dataclass(frozen=True)
class WalletRef:
    group_key: str
    role: Literal["fx", "crypto"]
    wallet_key: str
    credential_prefix: str


@dataclass(frozen=True)
class MarketRun:
    phase_key: Literal["fx_multiplier", "crypto_volume"]
    phase_label: str
    exchange_id: str
    wallet: WalletRef
    market: str
    average_spread_bps: Decimal
    max_allowed_spread_bps: Decimal
    orderbook_granularity: float
    market_order_safety_max_notional_usd: Decimal | None = None
    market_order_liquidity_safety_fraction: Decimal | None = None
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    fee_multiplier: Decimal = Decimal("1")
    fee_multiplier_expires_at: datetime | None = None
    slippage_buffer_bps: Decimal = Decimal("0")


@dataclass(frozen=True)
class PhaseTargetTotals:
    fx_volume_usd: Decimal
    crypto_volume_usd: Decimal

    @property
    def combined_volume_usd(self) -> Decimal:
        return self.fx_volume_usd + self.crypto_volume_usd


@dataclass(frozen=True)
class MarketCandidate:
    run: MarketRun
    snapshot: object
    plan: "RoundPlan"
    spread_ratio: Decimal
    cost: MarketCostResult


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly confirmed Hibachi weekly live test: fill FX test volume first, "
            "then crypto/BTC test volume. Real orders require --execute-live and --confirm."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--weekly-config", default="config/hibachi.weekly.test.json")
    parser.add_argument("--accounts-config", default="config/accounts.json")
    parser.add_argument("--markets-config", default="config/markets.json")
    parser.add_argument(
        "--live-ledger-db",
        default=os.environ.get("PERPDEX_HIBACHI_LIVE_LEDGER_DB", "data/hibachi_live_ledger.sqlite"),
        help="Local SQLite file for confirmed live-test planned volume logs.",
    )
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--orderbook-depth", type=int, default=5)
    parser.add_argument(
        "--orderbook-granularity",
        type=float,
        default=0.0,
        help="Override granularity. Default 0 uses per-market config or Hibachi inventory.",
    )
    parser.add_argument("--max-fees-percent", type=Decimal, default=Decimal("0.0005"))
    parser.add_argument("--min-entry-delay-seconds", type=float, default=1.0)
    parser.add_argument(
        "--loop-delay-seconds",
        type=float,
        default=None,
        help=(
            "Delay before the next live cycle. If omitted, the legacy --min-entry-delay-seconds value is used."
        ),
    )
    parser.add_argument(
        "--fast-close-on-fill",
        action="store_true",
        help=(
            "After a successful entry market POST, submit the reduce-only close immediately "
            "without waiting for a position REST check between orders."
        ),
    )
    parser.add_argument(
        "--prebuild-close-order",
        action="store_true",
        help=(
            "Request close-order prebuild before entry. Hibachi SDK market orders sign inside POST, "
            "so this currently logs unsupported and uses SDK-internal signing."
        ),
    )
    parser.add_argument("--monitor-source", choices=("auto", "websocket", "rest"), default="auto")
    parser.add_argument("--monitor-cache-max-age-seconds", type=float, default=2.0)
    parser.add_argument("--websocket-snapshot-timeout-seconds", type=float, default=0.8)
    parser.add_argument("--rest-poll-min-interval-seconds", type=float, default=0.1)
    parser.add_argument("--rate-limit-backoff-seconds", type=float, default=5.0)
    parser.add_argument("--max-cycles-per-phase", type=int, default=150)
    parser.add_argument("--max-idle-cycles", type=int, default=120)
    parser.add_argument("--fill-lookup-attempts", type=int, default=5)
    parser.add_argument("--fill-lookup-delay-seconds", type=float, default=0.25)
    parser.add_argument("--residual-settle-attempts", type=int, default=5)
    parser.add_argument("--residual-settle-delay-seconds", type=float, default=0.25)
    parser.add_argument("--skip-fill-spread-lookup", action="store_true")
    parser.add_argument(
        "--continue-after-residual-close",
        action="store_true",
        help=(
            "If a residual position is closed successfully and the account is flat, "
            "count the planned paired round and continue instead of stopping for review."
        ),
    )
    parser.add_argument(
        "--resume-completed-phase",
        action="append",
        choices=("fx", "crypto"),
        default=[],
        help="Treat a phase as already completed in a prior run. The runner still checks the wallet is flat.",
    )
    parser.add_argument(
        "--max-live-target-usd-per-phase",
        type=Decimal,
        default=Decimal("10000"),
        help=(
            "Deprecated compatibility option. FX/Crypto phase caps are enforced by "
            "--max-live-fx-volume-usd and --max-live-crypto-volume-usd."
        ),
    )
    parser.add_argument(
        "--max-live-fx-volume-usd",
        type=Decimal,
        default=Decimal("10000"),
        help="Safety cap for the configured FX weekly target in this test runner.",
    )
    parser.add_argument(
        "--max-live-crypto-volume-usd",
        type=Decimal,
        default=Decimal("10000"),
        help="Safety cap for the configured Crypto weekly target in this test runner.",
    )
    parser.add_argument(
        "--max-live-total-volume-usd",
        type=Decimal,
        default=Decimal("20000"),
        help="Safety cap for combined FX + Crypto planned gross volume in this runner.",
    )
    parser.add_argument("--allow-existing-position", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    load_dotenv_if_present(args.env_file)
    _validate_args(args)

    plan = _load_plan(Path(args.weekly_config))
    accounts = _load_json(Path(args.accounts_config))
    markets = _load_json(Path(args.markets_config))
    sizing = _execution_sizing(plan)
    market_catalog = _market_catalog(markets)
    runs = _build_runs(plan, accounts, market_catalog)
    target_totals = phase_target_totals(plan, runs)
    completed_phases = set(args.resume_completed_phase or [])
    week = current_weekly_window(
        datetime.now(timezone.utc),
        str(_dict(plan.get("week")).get("start_weekday_utc", "monday")),
    )
    ledger = WeeklyLedger(args.live_ledger_db)
    ledger.init()

    print("hibachi_weekly_live_test=explicit_confirm_required")
    print("real_orders_possible=True")
    print(f"execute_live={args.execute_live}")
    print(f"confirm_required={CONFIRM_TEXT}")
    print(f"weekly_config={args.weekly_config}")
    print(f"live_ledger_db={args.live_ledger_db}")
    print("live_ledger_recording=successful_confirmed_rounds_only")
    print(f"period_start_utc={week.start_utc.isoformat()}")
    print(f"period_end_utc={week.end_utc.isoformat()}")
    print("volume_counter_source=pre_trade_best_ask_notional")
    print("volume_counter_counts=both_market_sides")
    print(f"min_entry_delay_seconds={args.min_entry_delay_seconds:.2f}")
    print(f"loop_delay_seconds={_loop_delay_seconds(args):.2f}")
    print(f"fast_close_on_fill={args.fast_close_on_fill}")
    print(f"prebuild_close_order={args.prebuild_close_order}")
    print("hibachi_close_prebuild_supported=False")
    print("hibachi_signing_mode=sdk_internal_at_post")
    print(f"max_order_notional_usd={sizing.max_order_notional_usd}")
    print(f"market_order_safety_max_notional_usd={sizing.market_order_safety_max_notional_usd}")
    print(f"level_size_fraction={sizing.level_size_fraction}")
    print(f"market_order_liquidity_safety_fraction={sizing.market_order_liquidity_safety_fraction}")
    print("live_orderbook_check=before_every_live_batch")
    print("live_orderbook_source=websocket_cache_with_rest_backup")
    print(f"monitor_source={args.monitor_source}")
    print(f"monitor_cache_max_age_seconds={args.monitor_cache_max_age_seconds}")
    print(f"websocket_snapshot_timeout_seconds={args.websocket_snapshot_timeout_seconds}")
    print("fresh_orderbook_verify=selected_market_only_before_order")
    print(f"continue_after_residual_close={args.continue_after_residual_close}")
    print("operation_event_fields=" + ",".join(LIVE_RUN_EVENT_COLUMNS))

    if not runs:
        raise SystemExit("no enabled Hibachi weekly test markets found")

    phase_runs = _phase_runs(runs)
    print(f"enabled_phase_count={sum(1 for items in phase_runs.values() if items)}")
    print(f"enabled_market_count={len(runs)}")
    print(f"fx_target_volume_usd={target_totals.fx_volume_usd}")
    print(f"crypto_target_volume_usd={target_totals.crypto_volume_usd}")
    print(f"combined_target_volume_usd={target_totals.combined_volume_usd}")
    for run in runs:
        missing = hibachi_missing_required(run.wallet.credential_prefix)
        credential_status = "present" if not missing else "missing"
        print(
            "phase_plan "
            f"phase={run.phase_label} wallet={run.wallet.wallet_key} market={run.market} "
            f"phase_target_volume_usd={_phase_target_for_run(target_totals, run)} "
            f"average_spread_bps={run.average_spread_bps} "
            f"max_allowed_spread_bps={run.max_allowed_spread_bps} "
            f"credential_prefix={run.wallet.credential_prefix} credential_status={credential_status} "
            f"resume_completed={run.phase_label in completed_phases} "
            f"market_order_safety_max_notional_usd={_effective_safety_max_notional(sizing, run)} "
            f"market_order_liquidity_safety_fraction={_effective_liquidity_safety_fraction(sizing, run)} "
            f"entry_fee_bps={run.entry_fee_bps if run.entry_fee_bps is not None else 'auto'} "
            f"exit_fee_bps={run.exit_fee_bps if run.exit_fee_bps is not None else 'auto'} "
            f"fee_multiplier={run.fee_multiplier} "
            f"fee_multiplier_expires_at={run.fee_multiplier_expires_at.isoformat() if run.fee_multiplier_expires_at else 'none'} "
            f"slippage_buffer_bps={run.slippage_buffer_bps}"
        )
        if missing:
            print(f"phase_blocked_missing_env={','.join(missing)}")

    if not args.network:
        print("preflight_ok=False")
        print("reason=--network_required_for_hibachi_orderbook_and_private_readonly")
        return

    if args.execute_live:
        _validate_live_caps(args, runs, target_totals)
        if args.confirm != CONFIRM_TEXT:
            print("live_skipped=confirmation_mismatch")
            return

    clients: dict[str, object] = {}
    for run in runs:
        clients.setdefault(run.wallet.credential_prefix, _hibachi_client(run.wallet.credential_prefix))

    first_client = next(iter(clients.values()))
    metadata_fee = _load_metadata_fee_or_none(first_client)
    account_fee_by_prefix = {
        prefix: _load_account_fee_or_none(client, prefix)
        for prefix, client in clients.items()
    }
    fee_providers = {
        prefix: _fee_provider_for_prefix(
            credential_prefix=prefix,
            runs=runs,
            account_fee_by_prefix=account_fee_by_prefix,
            metadata_fee=metadata_fee,
        )
        for prefix in clients
    }
    print("fee_source_priority=account_api_then_market_metadata_then_config_exact_override_then_config_multiplier_then_fee_unknown_block")
    print("fee_order_type=market_taker_entry_and_exit")

    preflight_ok = True
    for run in runs:
        if run.phase_label in completed_phases:
            ok = _print_completed_phase_preflight(args, run, clients[run.wallet.credential_prefix])
        else:
            ok = _print_phase_preflight(
                args,
                sizing,
                run,
                clients[run.wallet.credential_prefix],
                fee_providers[run.wallet.credential_prefix],
                _phase_target_for_run(target_totals, run),
            )
        preflight_ok = preflight_ok and ok

    print(f"preflight_ok={preflight_ok}")
    if not preflight_ok:
        print("live_skipped=preflight_failed")
        return
    if not args.execute_live:
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return

    combined_live_volume = Decimal("0")
    ordered_phases: list[tuple[Literal["fx", "crypto"], Decimal]] = [
        ("fx", target_totals.fx_volume_usd),
        ("crypto", target_totals.crypto_volume_usd),
    ]
    active_phase_index = 0
    for phase_label, phase_target in ordered_phases:
        phase_markets = phase_runs[phase_label]
        if not phase_markets:
            continue
        active_phase_index += 1
        if phase_label in completed_phases:
            print(f"phase_skipped phase={phase_label} reason=resume_completed")
            combined_live_volume += phase_target
            print(f"combined_live_planned_gross_volume_usd={combined_live_volume:.4f}")
            continue
        if active_phase_index > 1:
            print(f"phase_transition_delay_seconds={_loop_delay_seconds(args):.2f}")
            time.sleep(_loop_delay_seconds(args))
        phase_volume = _run_live_phase(
            args,
            sizing,
            phase_label,
            phase_target,
            phase_markets,
            clients,
            fee_providers,
            ledger,
            week,
        )
        combined_live_volume += phase_volume
        print(f"phase_complete phase={phase_label} planned_gross_volume_usd={phase_volume:.4f}")
        print(f"combined_live_planned_gross_volume_usd={combined_live_volume:.4f}")
        if not _target_reached(phase_volume, phase_target):
            print(f"weekly_live_test_stopped=phase_target_not_reached:{phase_label}")
            _print_weekly_final_state(runs, clients, combined_live_volume)
            return

    print("weekly_live_test_complete=True")
    print(f"combined_live_planned_gross_volume_usd={combined_live_volume:.4f}")
    _print_weekly_final_state(runs, clients, combined_live_volume)


def _validate_args(args: argparse.Namespace) -> None:
    if args.orderbook_depth <= 0:
        raise SystemExit("--orderbook-depth must be greater than zero")
    if args.orderbook_granularity < 0:
        raise SystemExit("--orderbook-granularity must be zero or greater")
    if args.max_fees_percent <= 0:
        raise SystemExit("--max-fees-percent must be greater than zero")
    if args.min_entry_delay_seconds < 1:
        raise SystemExit("--min-entry-delay-seconds must be at least 1 for live safety")
    if args.loop_delay_seconds is not None and args.loop_delay_seconds < 0:
        raise SystemExit("--loop-delay-seconds must be zero or greater")
    if args.prebuild_close_order and not args.fast_close_on_fill:
        raise SystemExit("--prebuild-close-order requires --fast-close-on-fill")
    if args.max_cycles_per_phase <= 0:
        raise SystemExit("--max-cycles-per-phase must be greater than zero")
    if args.max_idle_cycles <= 0:
        raise SystemExit("--max-idle-cycles must be greater than zero")
    if args.fill_lookup_attempts <= 0:
        raise SystemExit("--fill-lookup-attempts must be greater than zero")
    if args.fill_lookup_delay_seconds < 0:
        raise SystemExit("--fill-lookup-delay-seconds must be zero or greater")
    if args.residual_settle_attempts <= 0:
        raise SystemExit("--residual-settle-attempts must be greater than zero")
    if args.residual_settle_delay_seconds < 0:
        raise SystemExit("--residual-settle-delay-seconds must be zero or greater")
    if args.max_live_target_usd_per_phase <= 0:
        raise SystemExit("--max-live-target-usd-per-phase must be greater than zero")
    if args.max_live_fx_volume_usd <= 0:
        raise SystemExit("--max-live-fx-volume-usd must be greater than zero")
    if args.max_live_crypto_volume_usd <= 0:
        raise SystemExit("--max-live-crypto-volume-usd must be greater than zero")
    if args.max_live_total_volume_usd <= 0:
        raise SystemExit("--max-live-total-volume-usd must be greater than zero")


def _validate_live_caps(args: argparse.Namespace, runs: list[MarketRun], target_totals: PhaseTargetTotals) -> None:
    if target_totals.fx_volume_usd > args.max_live_fx_volume_usd:
        raise SystemExit(
            "configured FX weekly target exceeds safety cap: "
            f"{target_totals.fx_volume_usd}>{args.max_live_fx_volume_usd}"
        )
    if target_totals.crypto_volume_usd > args.max_live_crypto_volume_usd:
        raise SystemExit(
            "configured Crypto weekly target exceeds safety cap: "
            f"{target_totals.crypto_volume_usd}>{args.max_live_crypto_volume_usd}"
        )
    if target_totals.combined_volume_usd > args.max_live_total_volume_usd:
        raise SystemExit(
            "combined live target exceeds safety cap: "
            f"{target_totals.combined_volume_usd}>{args.max_live_total_volume_usd}"
        )


def phase_target_totals(plan: dict[str, Any], runs: list[MarketRun]) -> PhaseTargetTotals:
    return PhaseTargetTotals(
        fx_volume_usd=_phase_target_total(plan, runs, "fx_multiplier", "fx"),
        crypto_volume_usd=_phase_target_total(plan, runs, "crypto_volume", "crypto"),
    )


def _phase_target_total(
    plan: dict[str, Any],
    runs: list[MarketRun],
    phase_key: Literal["fx_multiplier", "crypto_volume"],
    phase_label: Literal["fx", "crypto"],
) -> Decimal:
    phase = _dict(plan.get(phase_key))
    configured = phase.get("target_weekly_volume_usd")
    if configured is not None:
        target = _decimal(configured)
        if target < Decimal("0"):
            raise SystemExit(f"{phase_key}.target_weekly_volume_usd must be zero or greater")
        if target <= Decimal("0") and any(run.phase_label == phase_label for run in runs):
            raise SystemExit(f"{phase_key}.target_weekly_volume_usd must be greater than zero")
        return target
    legacy_sum = _legacy_market_target_sum(plan, phase_key)
    if legacy_sum > Decimal("0"):
        return legacy_sum
    if any(run.phase_label == phase_label for run in runs):
        raise SystemExit(f"{phase_key}.target_weekly_volume_usd is required when the phase has enabled markets")
    return Decimal("0")


def _legacy_market_target_sum(
    plan: dict[str, Any],
    phase_key: Literal["fx_multiplier", "crypto_volume"],
) -> Decimal:
    phase = _dict(plan.get(phase_key))
    return sum(
        (
            _decimal(market.get("target_weekly_volume_usd"), "0")
            for market in phase.get("markets", ())
            if isinstance(market, dict) and bool(market.get("enabled", False))
        ),
        Decimal("0"),
    )


def _phase_runs(runs: list[MarketRun]) -> dict[Literal["fx", "crypto"], list[MarketRun]]:
    return {
        "fx": [run for run in runs if run.phase_label == "fx"],
        "crypto": [run for run in runs if run.phase_label == "crypto"],
    }


def _phase_target_for_run(target_totals: PhaseTargetTotals, run: MarketRun) -> Decimal:
    if run.phase_label == "fx":
        return target_totals.fx_volume_usd
    return target_totals.crypto_volume_usd


def _load_plan(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    assert_no_plaintext_secrets(raw)
    return raw


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _decimal(value: object, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _optional_positive_decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    decimal_value = Decimal(str(value))
    if decimal_value <= Decimal("0"):
        raise SystemExit(f"{label} must be greater than zero")
    return decimal_value


def _optional_fraction_decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    decimal_value = Decimal(str(value))
    if decimal_value <= Decimal("0") or decimal_value > Decimal("1"):
        raise SystemExit(f"{label} must be greater than 0 and <= 1")
    return decimal_value


def _optional_nonnegative_decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    decimal_value = Decimal(str(value))
    if decimal_value < Decimal("0"):
        raise SystemExit(f"{label} must be zero or greater")
    return decimal_value


def _optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _execution_sizing(plan: dict[str, Any]) -> ExecutionSizing:
    sizing = _dict(plan.get("execution_sizing"))
    max_order = _decimal(sizing.get("max_order_notional_usd"), "0")
    safety_max_order = _decimal(sizing.get("market_order_safety_max_notional_usd"), str(max_order))
    fraction = _decimal(sizing.get("level_size_fraction"), "0")
    safety_fraction = _decimal(sizing.get("market_order_liquidity_safety_fraction"), "1")
    if max_order <= Decimal("0"):
        raise SystemExit("execution_sizing.max_order_notional_usd must be greater than zero")
    if max_order > Decimal("100"):
        raise SystemExit("execution_sizing.max_order_notional_usd must be <= 100 for this live test runner")
    if safety_max_order <= Decimal("0"):
        raise SystemExit("execution_sizing.market_order_safety_max_notional_usd must be greater than zero")
    if safety_max_order > max_order:
        raise SystemExit(
            "execution_sizing.market_order_safety_max_notional_usd must be <= max_order_notional_usd"
        )
    if fraction <= Decimal("0") or fraction > Decimal("1"):
        raise SystemExit("execution_sizing.level_size_fraction must be greater than 0 and <= 1")
    if safety_fraction <= Decimal("0") or safety_fraction > Decimal("1"):
        raise SystemExit("execution_sizing.market_order_liquidity_safety_fraction must be greater than 0 and <= 1")
    return ExecutionSizing(
        max_order_notional_usd=max_order,
        market_order_safety_max_notional_usd=safety_max_order,
        level_size_fraction=fraction,
        market_order_liquidity_safety_fraction=safety_fraction,
    )


def _market_catalog(markets: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("exchange_id")), str(item.get("market"))): item
        for item in markets.get("markets", ())
        if isinstance(item, dict) and item.get("exchange_id") and item.get("market")
    }


def _build_runs(
    plan: dict[str, Any],
    accounts: dict[str, Any],
    market_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[MarketRun]:
    exchange_id = str(plan.get("exchange_id", "hibachi"))
    account_group = _single_enabled_account_group(plan, accounts)
    fx_wallet = _wallet_ref(account_group, "fx")
    crypto_wallet = _wallet_ref(account_group, "crypto")

    runs: list[MarketRun] = []
    fx = _dict(plan.get("fx_multiplier"))
    if bool(fx.get("enabled", False)):
        fx_markets = _enabled_markets(fx)
        for market in fx_markets:
            symbol = str(market.get("market", ""))
            runs.append(
                _market_run(
                    phase_key="fx_multiplier",
                    phase_label="fx",
                    wallet=fx_wallet,
                    exchange_id=exchange_id,
                    market=market,
                    symbol=symbol,
                    market_catalog=market_catalog,
                ),
            )

    crypto = _dict(plan.get("crypto_volume"))
    if bool(crypto.get("enabled", False)):
        crypto_markets = _enabled_markets(crypto)
        for market in crypto_markets:
            symbol = str(market.get("market", ""))
            runs.append(
                _market_run(
                    phase_key="crypto_volume",
                    phase_label="crypto",
                    wallet=crypto_wallet,
                    exchange_id=exchange_id,
                    market=market,
                    symbol=symbol,
                    market_catalog=market_catalog,
                ),
            )
    return runs


def _single_enabled_account_group(plan: dict[str, Any], accounts: dict[str, Any]) -> dict[str, Any]:
    account_by_key = {
        str(item.get("account_group_key")): item
        for item in accounts.get("account_groups", ())
        if isinstance(item, dict) and item.get("account_group_key")
    }
    enabled_groups: list[dict[str, Any]] = []
    for group_ref in plan.get("account_groups", ()):
        if not isinstance(group_ref, dict) or not bool(group_ref.get("enabled", False)):
            continue
        group_key = str(group_ref.get("account_group_key", ""))
        account_group = account_by_key.get(group_key)
        if not account_group:
            raise SystemExit(f"enabled account group missing from accounts config: {group_key}")
        if bool(account_group.get("enabled", False)):
            enabled_groups.append(account_group)

    if len(enabled_groups) != 1:
        raise SystemExit(
            f"expected exactly one enabled account group for this live test runner, got {len(enabled_groups)}"
        )
    return enabled_groups[0]


def _wallet_ref(account_group: dict[str, Any], role: Literal["fx", "crypto"]) -> WalletRef:
    wallets = _dict(account_group.get("wallets"))
    wallet = _dict(wallets.get(role))
    if not wallet:
        raise SystemExit(f"{role} wallet is missing in enabled account group")
    if not bool(wallet.get("enabled", False)):
        raise SystemExit(f"{role} wallet is disabled in enabled account group")
    prefix = str(wallet.get("credential_env_prefix", ""))
    if not prefix:
        raise SystemExit(f"{role} wallet credential_env_prefix is missing")
    return WalletRef(
        group_key=str(account_group.get("account_group_key", "")),
        role=role,
        wallet_key=str(wallet.get("wallet_key", role)),
        credential_prefix=prefix,
    )


def _enabled_markets(phase: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in phase.get("markets", ())
        if isinstance(item, dict) and bool(item.get("enabled", False)) and item.get("market")
    ]


def _market_run(
    *,
    phase_key: Literal["fx_multiplier", "crypto_volume"],
    phase_label: str,
    wallet: WalletRef,
    exchange_id: str,
    market: dict[str, Any],
    symbol: str,
    market_catalog: dict[tuple[str, str], dict[str, Any]],
) -> MarketRun:
    catalog = market_catalog.get((exchange_id, symbol), {})
    average = _decimal(
        market.get("fallback_average_spread_bps", market.get("fallback_max_spread_bps")),
        "0",
    )
    hard_cap = _decimal(market.get("max_allowed_spread_bps"), str(average))
    granularity = float(catalog.get("orderbook_granularity", 0.0) or 0.0)
    if average <= Decimal("0"):
        raise SystemExit(f"fallback_average_spread_bps must be greater than zero for {phase_label}/{symbol}")
    if hard_cap <= Decimal("0"):
        raise SystemExit(f"max_allowed_spread_bps must be greater than zero for {phase_label}/{symbol}")
    fee_multiplier = _decimal(market.get("fee_multiplier"), "1")
    if fee_multiplier <= Decimal("0"):
        raise SystemExit(f"fee_multiplier must be greater than zero for {phase_label}/{symbol}")
    return MarketRun(
        phase_key=phase_key,
        phase_label=phase_label,
        exchange_id=exchange_id,
        wallet=wallet,
        market=symbol,
        average_spread_bps=average,
        max_allowed_spread_bps=hard_cap,
        orderbook_granularity=granularity,
        market_order_safety_max_notional_usd=_optional_positive_decimal(
            market.get("market_order_safety_max_notional_usd"),
            f"{phase_label}/{symbol}.market_order_safety_max_notional_usd",
        ),
        market_order_liquidity_safety_fraction=_optional_fraction_decimal(
            market.get("market_order_liquidity_safety_fraction"),
            f"{phase_label}/{symbol}.market_order_liquidity_safety_fraction",
        ),
        entry_fee_bps=_optional_nonnegative_decimal(
            market.get("entry_fee_bps"),
            f"{phase_label}/{symbol}.entry_fee_bps",
        ),
        exit_fee_bps=_optional_nonnegative_decimal(
            market.get("exit_fee_bps"),
            f"{phase_label}/{symbol}.exit_fee_bps",
        ),
        fee_multiplier=fee_multiplier,
        fee_multiplier_expires_at=_optional_datetime(market.get("fee_multiplier_expires_at")),
        slippage_buffer_bps=_optional_nonnegative_decimal(
            market.get("slippage_buffer_bps"),
            f"{phase_label}/{symbol}.slippage_buffer_bps",
        )
        or Decimal("0"),
    )


def _hibachi_client(credential_prefix: str) -> object:
    if hibachi_available_credential_env(credential_prefix) is None:
        raise SystemExit(f"missing required env vars for credential prefix: {credential_prefix}")

    from hibachi_xyz import HibachiApiClient

    credentials = read_hibachi_credentials(credential_prefix)
    api_endpoint = endpoint_from_env(get_env("HIBACHI_API_ENDPOINT_PRODUCTION"), DEFAULT_HIBACHI_API_ENDPOINT)
    data_endpoint = endpoint_from_env(
        get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"),
        DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    )
    return HibachiApiClient(
        api_url=api_endpoint,
        data_api_url=data_endpoint,
        api_key=credentials["api_key"],
        account_id=credentials["account_id"],
        private_key=credentials["private_key"],
    )


def _load_account_fee_or_none(client: object, credential_prefix: str) -> HibachiAccountFee | None:
    try:
        fee = load_hibachi_account_fee(client)
    except Exception as exc:
        print(f"fee_account_lookup_error prefix={credential_prefix} error={exc.__class__.__name__}")
        return None
    print(f"fee_account_source prefix={credential_prefix} source={fee.source}")
    print(f"fee_account_level prefix={credential_prefix} value={fee.fee_level or 'unknown'}")
    print(f"fee_account_maker_fee_bps prefix={credential_prefix} value={fee.maker_fee_bps}")
    print(f"fee_account_taker_fee_bps prefix={credential_prefix} value={fee.taker_fee_bps}")
    return fee


def _load_metadata_fee_or_none(client: object) -> HibachiAccountFee | None:
    try:
        fee = load_hibachi_metadata_fee(client)
    except Exception as exc:
        print(f"fee_metadata_lookup_error={exc.__class__.__name__}")
        return None
    print(f"fee_metadata_source={fee.source}")
    print(f"fee_metadata_level={fee.fee_level or 'unknown'}")
    print(f"fee_metadata_maker_fee_bps={fee.maker_fee_bps}")
    print(f"fee_metadata_taker_fee_bps={fee.taker_fee_bps}")
    return fee


def _fee_provider_for_prefix(
    *,
    credential_prefix: str,
    runs: list[MarketRun],
    account_fee_by_prefix: dict[str, HibachiAccountFee | None],
    metadata_fee: HibachiAccountFee | None,
) -> HibachiFeeProvider:
    return HibachiFeeProvider(
        override_by_market=_fee_overrides_by_market(runs),
        account_fee=account_fee_by_prefix.get(credential_prefix),
        metadata_fee=metadata_fee,
    )


def _fee_overrides_by_market(runs: list[MarketRun]) -> dict[str, HibachiFeeOverride]:
    return {
        run.market: HibachiFeeOverride(
            market=run.market,
            entry_fee_bps=run.entry_fee_bps,
            exit_fee_bps=run.exit_fee_bps,
            fee_multiplier=run.fee_multiplier,
            fee_multiplier_expires_at=run.fee_multiplier_expires_at,
            slippage_buffer_bps=run.slippage_buffer_bps,
        )
        for run in runs
    }


def _print_phase_preflight(
    args: argparse.Namespace,
    sizing: ExecutionSizing,
    run: MarketRun,
    client: object,
    fee_provider: HibachiFeeProvider,
    phase_target_volume_usd: Decimal,
) -> bool:
    print(f"phase_preflight_start={run.phase_label}")
    position_ok, direction, quantity, position_reason = _read_position(client, run.market)
    if not position_ok:
        print(f"phase={run.phase_label} preflight_blocked={position_reason}")
        return False
    print(f"phase={run.phase_label} market={run.market} start_position_direction={direction or 'flat'}")
    print(f"phase={run.phase_label} market={run.market} start_position_quantity={quantity}")
    if quantity > Decimal("0") and not args.allow_existing_position:
        print(f"phase={run.phase_label} preflight_blocked=existing_position_detected")
        return False

    snapshot_result = _load_snapshot(args, run)
    if not snapshot_result.ok or snapshot_result.snapshot is None:
        print(f"phase={run.phase_label} preflight_blocked={snapshot_result.reason}")
        return False

    snapshot = snapshot_result.snapshot
    allowed, reason = _spread_allowed(run, snapshot)
    plan = _round_plan(sizing, run, snapshot, phase_target_volume_usd)
    cost = _market_cost(run, snapshot, fee_provider)
    if not cost.eligible:
        allowed = False
        reason = cost.reason
    print(
        f"phase={run.phase_label} market={run.market} spread_bps={Decimal(str(snapshot.spread_bps)):.4f} "
        f"average_spread_bps={run.average_spread_bps:.4f} "
        f"max_allowed_spread_bps={run.max_allowed_spread_bps:.4f} "
        f"expected_loss_bps={cost.expected_loss_bps:.4f} "
        f"entry_fee_bps={cost.entry_fee_bps} exit_fee_bps={cost.exit_fee_bps} "
        f"slippage_buffer_bps={cost.slippage_buffer_bps} fee_source={cost.fee_source} "
        f"fee_known={cost.fee_known} trade_allowed={allowed} reason={reason}"
    )
    print(f"phase={run.phase_label} preflight_orderbook_checked_at_utc={_snapshot_timestamp(snapshot)}")
    print(
        f"phase={run.phase_label} planned_first_quantity={plan.quantity} "
        f"planned_one_side_notional_usd={plan.notional_usd:.4f} "
        f"planned_first_side={plan.first_side} planned_second_side={plan.second_side}"
    )
    fee_base = _fee_base_for_event(fee_provider)
    planned_gross_volume = plan.notional_usd * Decimal("2")
    print(
        "operation_event_preview "
        f"exchange={run.exchange_id} account_label={run.wallet.group_key} "
        f"wallet_label={run.wallet.wallet_key} market={run.market} "
        f"cycle_id={run.phase_label}:preflight environment=production "
        f"fee_level={fee_base.fee_level or 'unknown'} "
        f"maker_fee_bps={fee_base.maker_fee_bps} taker_fee_bps={fee_base.taker_fee_bps} "
        f"entry_fee_bps={cost.entry_fee_bps} exit_fee_bps={cost.exit_fee_bps} "
        f"fee_source={cost.fee_source} fee_multiplier={run.fee_multiplier} "
        f"fee_multiplier_expires_at={run.fee_multiplier_expires_at.isoformat() if run.fee_multiplier_expires_at else 'none'} "
        f"live_spread_bps={Decimal(str(snapshot.spread_bps)):.4f} "
        f"expected_loss_bps={cost.expected_loss_bps:.4f} "
        f"planned_gross_volume_usd={planned_gross_volume:.4f} "
        f"filled_gross_volume_usd=not_available_until_live_fill "
        f"estimated_fee_usd={_estimated_fee_usd(plan.notional_usd, cost):.8f} "
        f"estimated_loss_usd={_estimated_loss_usd(plan.notional_usd, cost):.8f} "
        f"realized_pnl_usd=not_available points_estimate=not_available "
        f"order_ids=redacted error_reason={'' if allowed else reason} status=preflight"
    )
    if args.fast_close_on_fill:
        print(f"phase={run.phase_label} planned_fast_close=True")
        print(f"phase={run.phase_label} planned_entry_side={plan.first_side}")
        print(f"phase={run.phase_label} planned_close_side={_opposite_side(plan.first_side)}")
        print(f"phase={run.phase_label} post_entry_position_check_skipped=True")
        if args.prebuild_close_order:
            print(f"phase={run.phase_label} close_order_prebuild_requested=True")
            print(f"phase={run.phase_label} close_order_prebuild_supported=False")
            print(f"phase={run.phase_label} close_prebuild_reason=hibachi_sdk_market_orders_sign_inside_post")
    if plan.quantity <= Decimal("0"):
        print(f"phase={run.phase_label} preflight_warning=planned_quantity_zero")
    return True


def _print_completed_phase_preflight(
    args: argparse.Namespace,
    run: MarketRun,
    client: object,
) -> bool:
    print(f"phase_preflight_start={run.phase_label}")
    print(f"phase={run.phase_label} resume_completed=True")
    position_ok, direction, quantity, position_reason = _read_position(client, run.market)
    if not position_ok:
        print(f"phase={run.phase_label} preflight_blocked={position_reason}")
        return False
    print(f"phase={run.phase_label} market={run.market} start_position_direction={direction or 'flat'}")
    print(f"phase={run.phase_label} market={run.market} start_position_quantity={quantity}")
    if quantity > Decimal("0") and not args.allow_existing_position:
        print(f"phase={run.phase_label} preflight_blocked=existing_position_detected")
        return False
    print(f"phase={run.phase_label} preflight_ok=resume_completed_phase_flat")
    return True


@dataclass(frozen=True)
class RoundPlan:
    quantity: Decimal
    notional_usd: Decimal
    first_side: Literal["BUY", "SELL"]
    second_side: Literal["BUY", "SELL"]


def _load_snapshot(args: argparse.Namespace, run: MarketRun) -> object:
    granularity = args.orderbook_granularity if args.orderbook_granularity > 0 else run.orderbook_granularity
    return load_hibachi_orderbook_snapshot(
        run.market,
        average_spread_bps=float(run.average_spread_bps),
        depth=args.orderbook_depth,
        granularity=float(granularity),
    )


def _spread_allowed(run: MarketRun, snapshot: object) -> tuple[bool, str]:
    spread_bps = Decimal(str(snapshot.spread_bps))
    average_spread_bps = Decimal(str(snapshot.average_spread_bps))
    if spread_bps > average_spread_bps:
        return False, f"spread_above_average:{spread_bps:.4f}>{average_spread_bps:.4f}"
    if spread_bps > run.max_allowed_spread_bps:
        return False, f"spread_above_hard_cap:{spread_bps:.4f}>{run.max_allowed_spread_bps:.4f}"
    return True, "spread_ok"


def _market_cost(run: MarketRun, snapshot: object, fee_provider: HibachiFeeProvider) -> MarketCostResult:
    return calculate_market_cost(
        MarketCostInput(
            exchange_id=run.exchange_id,
            market=run.market,
            live_spread_bps=Decimal(str(snapshot.spread_bps)),
            fee=fee_provider.fee_for_market(run.market),
        ),
    )


def _fee_base_for_event(fee_provider: HibachiFeeProvider) -> HibachiAccountFee:
    if fee_provider.account_fee is not None:
        return fee_provider.account_fee
    if fee_provider.metadata_fee is not None:
        return fee_provider.metadata_fee
    return HibachiAccountFee(
        fee_level=None,
        maker_fee_bps=Decimal("0"),
        taker_fee_bps=Decimal("0"),
        source="not_available",
    )


def _estimated_fee_usd(one_side_notional_usd: Decimal, cost: MarketCostResult) -> Decimal:
    return one_side_notional_usd * (cost.entry_fee_bps + cost.exit_fee_bps) / Decimal("10000")


def _estimated_loss_usd(one_side_notional_usd: Decimal, cost: MarketCostResult) -> Decimal:
    return one_side_notional_usd * cost.expected_loss_bps / Decimal("10000")


def _effective_spread_cap(run: MarketRun, snapshot: object) -> Decimal:
    return min(Decimal(str(snapshot.average_spread_bps)), run.max_allowed_spread_bps)


def _round_plan(
    sizing: ExecutionSizing,
    run: MarketRun,
    snapshot: object,
    remaining_target_usd: Decimal,
) -> RoundPlan:
    per_side_cap = min(
        sizing.max_order_notional_usd,
        _effective_safety_max_notional(sizing, run),
        remaining_target_usd / Decimal("2"),
    )
    if per_side_cap <= Decimal("0"):
        return RoundPlan(Decimal("0"), Decimal("0"), "BUY", "SELL")

    smaller_level = snapshot.best_bid if snapshot.best_bid.size <= snapshot.best_ask.size else snapshot.best_ask
    level_fraction_qty = (
        Decimal(str(smaller_level.size))
        * sizing.level_size_fraction
        * _effective_liquidity_safety_fraction(sizing, run)
    )
    cap_qty = per_side_cap / Decimal(str(snapshot.best_ask.price))
    quantity = min(level_fraction_qty, cap_qty).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    notional = quantity * Decimal(str(snapshot.best_ask.price))
    first_side, second_side = _paired_market_sides(snapshot)
    return RoundPlan(quantity, notional, first_side, second_side)


def _effective_safety_max_notional(sizing: ExecutionSizing, run: MarketRun) -> Decimal:
    value = run.market_order_safety_max_notional_usd or sizing.market_order_safety_max_notional_usd
    return min(value, sizing.max_order_notional_usd)


def _effective_liquidity_safety_fraction(sizing: ExecutionSizing, run: MarketRun) -> Decimal:
    return run.market_order_liquidity_safety_fraction or sizing.market_order_liquidity_safety_fraction


def _market_spec_for_run(run: MarketRun) -> MarketSpec:
    return MarketSpec(
        exchange_id=run.exchange_id,
        market=run.market,
        average_spread_bps=run.average_spread_bps,
        max_spread_bps=run.max_allowed_spread_bps,
        metadata=run,
    )


def _refresh_hibachi_monitor_cache(
    args: argparse.Namespace,
    phase_markets: list[MarketRun],
    specs: list[MarketSpec],
    cache: SpreadCache,
    rest_backoff: RestBackoff,
) -> None:
    data_api_endpoint = endpoint_from_env(
        get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"),
        DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    )
    granularity_by_market = {
        run.market: args.orderbook_granularity if args.orderbook_granularity > 0 else run.orderbook_granularity
        for run in phase_markets
    }
    refresh_hibachi_spread_cache(
        cache=cache,
        specs=specs,
        data_api_endpoint=data_api_endpoint,
        monitor_source=args.monitor_source,
        cache_max_age_seconds=args.monitor_cache_max_age_seconds,
        websocket_timeout_seconds=args.websocket_snapshot_timeout_seconds,
        depth=args.orderbook_depth,
        granularity_by_market=granularity_by_market,
        rest_backoff=rest_backoff,
    )


def _run_live_phase(
    args: argparse.Namespace,
    sizing: ExecutionSizing,
    phase_label: Literal["fx", "crypto"],
    phase_target_volume_usd: Decimal,
    phase_markets: list[MarketRun],
    clients: dict[str, object],
    fee_providers: dict[str, HibachiFeeProvider],
    ledger: WeeklyLedger,
    week: object,
) -> Decimal:
    live_gross_volume = Decimal("0")
    idle_cycles = 0
    cache = SpreadCache()
    specs = [_market_spec_for_run(run) for run in phase_markets]
    adapters = {
        prefix: HibachiAdapter(prefix, args.max_fees_percent, client)
        for prefix, client in clients.items()
    }
    rest_backoff = RestBackoff(
        min_interval_seconds=args.rest_poll_min_interval_seconds,
        default_backoff_seconds=args.rate_limit_backoff_seconds,
    )

    print(f"phase_live_start={phase_label}")
    print(f"phase={phase_label} target_volume_usd={phase_target_volume_usd:.4f}")
    print(f"phase={phase_label} monitored_market_count={len(phase_markets)}")
    print(f"phase={phase_label} loop_delay_seconds={_loop_delay_seconds(args):.2f}")
    print(f"phase={phase_label} fast_close_on_fill={args.fast_close_on_fill}")

    for cycle in range(1, args.max_cycles_per_phase + 1):
        if _target_reached(live_gross_volume, phase_target_volume_usd):
            live_gross_volume = phase_target_volume_usd
            print(
                f"phase={phase_label} stop_reason=target_volume_reached:"
                f"{live_gross_volume:.4f}>={phase_target_volume_usd:.4f}"
            )
            return live_gross_volume

        if cycle > 1 and _loop_delay_seconds(args):
            print(f"phase={phase_label} cycle={cycle} loop_delay_seconds={_loop_delay_seconds(args):.2f}")
            time.sleep(_loop_delay_seconds(args))

        cycle_started_ns = _now_ns()
        remaining = phase_target_volume_usd - live_gross_volume
        plan_started_ns = _now_ns()
        _refresh_hibachi_monitor_cache(args, phase_markets, specs, cache, rest_backoff)
        candidates: list[MarketCandidate] = []
        for run in phase_markets:
            cached = cache.fresh(run.exchange_id, run.market, args.monitor_cache_max_age_seconds)
            if cached is None:
                print(
                    f"phase={phase_label} cycle={cycle} market={run.market} "
                    f"market_data_ok=False reason=monitor_cache_missing_or_stale"
                )
                continue

            snapshot = cached.to_market_snapshot(average_spread_bps=run.average_spread_bps)
            allowed, reason = _spread_allowed(run, snapshot)
            cost = _market_cost(run, snapshot, fee_providers[run.wallet.credential_prefix])
            if not cost.eligible:
                allowed = False
                reason = cost.reason
            round_plan = _round_plan(sizing, run, snapshot, remaining)
            print(
                f"phase={phase_label} cycle={cycle} market={run.market} market_data_ok=True "
                f"trade_allowed={allowed} orderbook_source={cached.source} "
                f"orderbook_checked_at_utc={_snapshot_timestamp(snapshot)} "
                f"spread_bps={Decimal(str(snapshot.spread_bps)):.4f} "
                f"average_spread_bps={Decimal(str(snapshot.average_spread_bps)):.4f} "
                f"max_allowed_spread_bps={run.max_allowed_spread_bps:.4f} "
                f"expected_loss_bps={cost.expected_loss_bps:.4f} "
                f"entry_fee_bps={cost.entry_fee_bps} exit_fee_bps={cost.exit_fee_bps} "
                f"slippage_buffer_bps={cost.slippage_buffer_bps} fee_source={cost.fee_source} "
                f"fee_known={cost.fee_known} reason={reason} "
                f"quantity={round_plan.quantity} one_side_notional_usd={round_plan.notional_usd:.4f}"
            )

            if not allowed or round_plan.quantity <= Decimal("0"):
                if round_plan.quantity <= Decimal("0"):
                    print(f"phase={phase_label} cycle={cycle} market={run.market} live_skipped=quantity_zero")
                continue

            spread_bps = Decimal(str(snapshot.spread_bps))
            effective_cap = _effective_spread_cap(run, snapshot)
            spread_ratio = spread_bps / effective_cap if effective_cap > Decimal("0") else spread_bps
            candidates.append(MarketCandidate(run, snapshot, round_plan, spread_ratio, cost))

        if not candidates:
            idle_cycles += 1
            if idle_cycles >= args.max_idle_cycles:
                print(f"phase={phase_label} stop_reason=max_idle_cycles_reached:{idle_cycles}")
                return live_gross_volume
            continue

        idle_cycles = 0
        selected = min(
            candidates,
            key=lambda item: (item.cost.expected_loss_bps, Decimal(str(item.snapshot.spread_bps)), str(item.run.market)),
        )
        run = selected.run
        snapshot = selected.snapshot
        round_plan = selected.plan
        print(
            f"phase={phase_label} cycle={cycle} selected_market={run.market} "
            f"selection_rule=lowest_expected_loss_bps_then_live_spread_bps "
            f"expected_loss_bps={selected.cost.expected_loss_bps:.4f} "
            f"fee_source={selected.cost.fee_source} "
            f"spread_ratio={selected.spread_ratio:.6f} "
            f"orderbook_check_timing=cache_then_selected_market_fresh_verify "
            f"first_side={round_plan.first_side} second_side={round_plan.second_side}"
        )

        fresh_result = _load_snapshot(args, run)
        if not fresh_result.ok or fresh_result.snapshot is None:
            idle_cycles += 1
            print(
                f"phase={phase_label} cycle={cycle} selected_market={run.market} "
                f"fresh_verify_ok=False reason={fresh_result.reason}"
            )
            if _rate_limited(fresh_result.reason):
                rest_backoff.note_rate_limited()
                print(f"phase={phase_label} cycle={cycle} fresh_verify_backoff=True")
            continue
        snapshot = fresh_result.snapshot
        allowed, reason = _spread_allowed(run, snapshot)
        cost = _market_cost(run, snapshot, fee_providers[run.wallet.credential_prefix])
        if not cost.eligible:
            allowed = False
            reason = cost.reason
        round_plan = _round_plan(sizing, run, snapshot, remaining)
        print(
            f"phase={phase_label} cycle={cycle} selected_market={run.market} "
            f"fresh_verify_ok=True spread_bps={Decimal(str(snapshot.spread_bps)):.4f} "
            f"expected_loss_bps={cost.expected_loss_bps:.4f} "
            f"entry_fee_bps={cost.entry_fee_bps} exit_fee_bps={cost.exit_fee_bps} "
            f"slippage_buffer_bps={cost.slippage_buffer_bps} fee_source={cost.fee_source} "
            f"reason={reason} quantity={round_plan.quantity} "
            f"one_side_notional_usd={round_plan.notional_usd:.4f}"
        )
        if not allowed or round_plan.quantity <= Decimal("0"):
            idle_cycles += 1
            print(f"phase={phase_label} cycle={cycle} selected_plan_skipped=fresh_verify_failed_or_quantity_zero")
            continue

        round_gross_volume = round_plan.notional_usd * Decimal("2")
        estimated_fee_usd = _estimated_fee_usd(round_plan.notional_usd, cost)
        estimated_loss_usd = _estimated_loss_usd(round_plan.notional_usd, cost)
        fee_base = _fee_base_for_event(fee_providers[run.wallet.credential_prefix])
        print(
            "operation_event_preview "
            f"exchange={run.exchange_id} account_label={run.wallet.group_key} "
            f"wallet_label={run.wallet.wallet_key} market={run.market} "
            f"cycle_id={phase_label}:{cycle} environment=production "
            f"fee_level={fee_base.fee_level or 'unknown'} "
            f"maker_fee_bps={fee_base.maker_fee_bps} taker_fee_bps={fee_base.taker_fee_bps} "
            f"entry_fee_bps={cost.entry_fee_bps} exit_fee_bps={cost.exit_fee_bps} "
            f"fee_source={cost.fee_source} fee_multiplier={run.fee_multiplier} "
            f"fee_multiplier_expires_at={run.fee_multiplier_expires_at.isoformat() if run.fee_multiplier_expires_at else 'none'} "
            f"live_spread_bps={Decimal(str(snapshot.spread_bps)):.4f} "
            f"expected_loss_bps={cost.expected_loss_bps:.4f} "
            f"planned_gross_volume_usd={round_gross_volume:.4f} "
            f"filled_gross_volume_usd=not_available_until_live_fill "
            f"estimated_fee_usd={estimated_fee_usd:.8f} estimated_loss_usd={estimated_loss_usd:.8f} "
            f"realized_pnl_usd=not_available points_estimate=not_available "
            f"order_ids=redacted status=planned"
        )

        print(f"phase={phase_label} cycle={cycle} plan_latency_ms={_elapsed_ms(plan_started_ns)}")
        try:
            if args.fast_close_on_fill:
                result = _execute_fast_close_market_roundtrip(
                    clients[run.wallet.credential_prefix],
                    run,
                    args,
                    round_plan.quantity,
                    round_plan.first_side,
                    one_side_notional_usd=round_plan.notional_usd,
                    label_prefix=f"phase={phase_label} cycle={cycle}",
                    cycle_started_ns=cycle_started_ns,
                    plan_latency_already_logged=True,
                )
            else:
                result = execute_roundtrip_plan(
                    adapters[run.wallet.credential_prefix],
                    RoundtripPlan(
                        market=run.market,
                        instrument_id=0,
                        buy_price=Decimal(str(snapshot.best_ask.price)),
                        sell_price=Decimal(str(snapshot.best_bid.price)),
                        buy_size=round_plan.quantity,
                        sell_size=round_plan.quantity,
                        planned_gross_volume_usd=round_plan.notional_usd * Decimal("2"),
                        first_side=round_plan.first_side,
                        second_side=round_plan.second_side,
                        reason="hibachi_weekly_lowest_live_spread",
                    ),
                )
        except Exception as exc:
            print(f"phase={phase_label} stop_reason=live_execution_exception:{exc.__class__.__name__}")
            return live_gross_volume
        status = result.status
        recorded_status = status
        if status == "residual_closed_stop_for_review" and args.continue_after_residual_close:
            print(f"phase={phase_label} cycle={cycle} residual_closed_continue=True")
            recorded_status = "residual_closed_continue"
        elif status != "ok_flat":
            print(f"phase={phase_label} stop_reason={status}")
            return live_gross_volume

        ledger.record_live_round(
            period=week,
            timestamp=datetime.now(timezone.utc),
            exchange_id=run.exchange_id,
            account_group_key=run.wallet.group_key,
            wallet_role=run.wallet.role,
            wallet_key=run.wallet.wallet_key,
            credential_prefix=run.wallet.credential_prefix,
            phase=phase_label,
            market=run.market,
            cycle=cycle,
            spread_bps=float(Decimal(str(snapshot.spread_bps))),
            threshold_bps=float(_effective_spread_cap(run, snapshot)),
            quantity=str(round_plan.quantity),
            one_side_notional_usd=float(round_plan.notional_usd),
            planned_gross_volume_usd=float(round_gross_volume),
            first_side=round_plan.first_side,
            second_side=round_plan.second_side,
            status=recorded_status,
            environment="production",
            cycle_id=f"{phase_label}:{cycle}",
            fee_level=fee_base.fee_level,
            maker_fee_bps=float(fee_base.maker_fee_bps),
            taker_fee_bps=float(fee_base.taker_fee_bps),
            entry_fee_bps=float(cost.entry_fee_bps),
            exit_fee_bps=float(cost.exit_fee_bps),
            fee_source=cost.fee_source,
            fee_multiplier=float(run.fee_multiplier),
            fee_multiplier_expires_at=(
                run.fee_multiplier_expires_at.isoformat() if run.fee_multiplier_expires_at else None
            ),
            live_spread_bps=float(Decimal(str(snapshot.spread_bps))),
            expected_loss_bps=float(cost.expected_loss_bps),
            filled_gross_volume_usd=float(result.estimated_gross_volume_usd)
            if hasattr(result, "estimated_gross_volume_usd")
            else None,
            estimated_fee_usd=float(estimated_fee_usd),
            estimated_loss_usd=float(estimated_loss_usd),
            realized_pnl_usd=None,
            points_estimate=None,
            start_position_count=None,
            final_position_count=(0 if getattr(result, "final_all_flat", False) else None),
            start_open_order_count=None,
            final_open_order_count=getattr(result, "final_open_order_count", None),
            order_ids="redacted",
            error_reason="" if recorded_status in {"ok_flat", "residual_closed_continue"} else recorded_status,
        )
        print(
            f"phase={phase_label} cycle={cycle} market={run.market} "
            f"live_ledger_recorded_planned_gross_volume_usd={round_gross_volume:.4f}"
        )
        live_gross_volume += round_gross_volume
        if _target_reached(live_gross_volume, phase_target_volume_usd):
            live_gross_volume = phase_target_volume_usd
        print(f"phase={phase_label} cycle={cycle} live_round_gross_volume_usd={round_gross_volume:.4f}")
        print(f"phase={phase_label} live_total_gross_volume_usd={live_gross_volume:.4f}")
        if _target_reached(live_gross_volume, phase_target_volume_usd):
            print(
                f"phase={phase_label} stop_reason=target_volume_reached:"
                f"{live_gross_volume:.4f}>={phase_target_volume_usd:.4f}"
            )
            return live_gross_volume

    print(f"phase={phase_label} stop_reason=max_cycles_reached:{args.max_cycles_per_phase}")
    return live_gross_volume


def _loop_delay_seconds(args: argparse.Namespace) -> float:
    if args.loop_delay_seconds is not None:
        return float(args.loop_delay_seconds)
    return float(args.min_entry_delay_seconds)


def _print_weekly_final_state(
    runs: list[MarketRun],
    clients: dict[str, object],
    estimated_gross_volume_usd: Decimal,
) -> None:
    print(f"final_estimated_gross_volume_usd={estimated_gross_volume_usd:.4f}")
    all_flat = True
    seen: set[tuple[str, str]] = set()
    for run in runs:
        state_key = (run.wallet.credential_prefix, run.market)
        if state_key in seen:
            continue
        seen.add(state_key)
        client = clients[run.wallet.credential_prefix]
        ok, direction, quantity, reason = _read_position(client, run.market)
        open_order_count = _open_order_count(client, run.market)
        if not ok or quantity > Decimal("0") or open_order_count != 0:
            all_flat = False
        print(
            "final_market_state "
            f"phase={run.phase_label} wallet={run.wallet.wallet_key} market={run.market} "
            f"position_ok={ok} position_reason={reason} "
            f"final_open_order_count={open_order_count if open_order_count is not None else 'unknown'} "
            f"final_position_direction={direction or 'flat'} "
            f"final_position_size={quantity} "
            f"final_position_steps=not_available_for_hibachi_sdk"
        )
    print(f"final_all_flat={all_flat}")


def _target_reached(volume_usd: Decimal, target_usd: Decimal) -> bool:
    return volume_usd + TARGET_VOLUME_TOLERANCE_USD >= target_usd


def _snapshot_timestamp(snapshot: object) -> str:
    timestamp = getattr(snapshot, "timestamp", None)
    if timestamp is None:
        return "unknown"
    isoformat = getattr(timestamp, "isoformat", None)
    if not callable(isoformat):
        return str(timestamp)
    return isoformat()


def _rate_limited(reason: str) -> bool:
    return "RateLimited" in reason or "rate limit" in reason.lower()


def _read_position(
    client: object,
    market: str,
) -> tuple[bool, Literal["long", "short", ""], Decimal, str]:
    try:
        direction, quantity = _position_state(client.get_account_info(), market)
    except Exception as exc:
        return False, "", Decimal("0"), f"account_info_error:{exc.__class__.__name__}"
    return True, direction, quantity, "ok"


if __name__ == "__main__":
    main()
