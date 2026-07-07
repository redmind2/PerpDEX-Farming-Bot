from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from perpdex_farming_bot.cli.hyperliquid_live_test import (
    _check_api_wallet_private_key,
    _position_count,
    _round_down_to_step,
    _sdk_style_slippage_price,
    fmt_decimal,
)
from perpdex_farming_bot.connectors.hyperliquid_readonly import (
    HyperliquidReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    info_post_json,
    normalize_hyperliquid_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.core.live_volume import RoundtripPlan, VolumeRunConfig, run_paired_volume
from perpdex_farming_bot.credentials import (
    hyperliquid_available_private_readonly_env,
    hyperliquid_credential_env,
    hyperliquid_signing_missing,
    read_hyperliquid_credentials,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.base import ExchangeOrderResult
from perpdex_farming_bot.exchanges.hyperliquid import HyperliquidAdapter
from perpdex_farming_bot.exchanges.hyperliquid_fees import load_hyperliquid_account_fee
from perpdex_farming_bot.marketdata.hyperliquid import (
    HyperliquidMarketInfo,
    fetch_hyperliquid_rest_top_of_book,
    load_all_hyperliquid_market_info,
)


CONFIRM_TEXT = "LIVE_HYPERLIQUID_1000_SPREAD_VOLUME"
MAX_LIVE_TARGET_GROSS_VOLUME_USD = Decimal("1200")
MAX_LIVE_LEG_NOTIONAL_USD = Decimal("100")


@dataclass(frozen=True)
class MarketSpreadConfig:
    coin: str
    api_coin: str
    dex: str
    display_market: str
    average_spread_bps: Decimal | None
    enabled: bool
    disabled_reason: str


@dataclass(frozen=True)
class VolumeCandidate:
    config: MarketSpreadConfig
    market_info: HyperliquidMarketInfo
    best_bid: Decimal
    best_ask: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    spread_bps: Decimal
    spread_threshold_bps: Decimal
    size: Decimal
    planned_entry_notional_usd: Decimal
    planned_close_notional_usd: Decimal
    planned_gross_volume_usd: Decimal
    aggressive_buy_px: Decimal
    aggressive_sell_px: Decimal
    eligible: bool
    reason: str


@dataclass(frozen=True)
class PrivateStateSnapshot:
    position_count: int
    open_order_count: int
    account_value: Decimal | None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Hyperliquid spread-gated live volume test. Default is dry-run; live POST requires "
            "--execute-live and the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hyperliquid.spread-volume-test.json")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HYPERLIQUID",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="", help="Override config environment: production/mainnet.")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--network", action="store_true", help="Actually call Hyperliquid read-only and, if confirmed, live POST.")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=None)
    parser.add_argument("--max-leg-notional-usd", type=Decimal, default=None)
    parser.add_argument("--book-fraction", type=Decimal, default=None)
    parser.add_argument("--spread-cap-bps", type=Decimal, default=None)
    parser.add_argument("--min-order-size-usd", type=Decimal, default=None)
    parser.add_argument("--slippage-bps", type=Decimal, default=None)
    parser.add_argument("--cycle-delay-seconds", type=float, default=1.0)
    parser.add_argument("--target-tolerance-usd", type=Decimal, default=Decimal("2"))
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument("--max-scan-attempts", type=int, default=80)
    parser.add_argument("--no-candidate-delay-seconds", type=float, default=1.0)
    parser.add_argument("--final-state-delay-seconds", type=float, default=1.0)
    parser.add_argument(
        "--roundtrip-mode",
        "--close-mode",
        dest="roundtrip_mode",
        choices=("confirmed", "netting"),
        default="confirmed",
        help="confirmed waits for the entry fill then sends reduce-only close; netting sends the opposite non-reduce-only order immediately after entry.",
    )
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = normalize_hyperliquid_environment(args.environment or str(plan.get("environment", "production")))
    dex = str(plan.get("dex", ""))
    target_gross = args.target_gross_volume_usd or Decimal(str(plan.get("target_gross_volume_usd", "1000")))
    max_leg_notional = args.max_leg_notional_usd or Decimal(str(plan.get("max_leg_notional_usd", "100")))
    book_fraction = args.book_fraction or Decimal(str(plan.get("book_fraction", "0.5")))
    spread_cap_bps = args.spread_cap_bps or Decimal(str(plan.get("spread_cap_bps", "1")))
    min_order_size_usd = args.min_order_size_usd or Decimal(str(plan.get("min_order_size_usd", "10")))
    slippage_bps = args.slippage_bps or Decimal(str(plan.get("slippage_bps", "25")))
    allow_live_orders = _bool_config(plan.get("allow_live_orders", True), "allow_live_orders")
    markets = _market_configs(plan)
    perp_dexs = _configured_perp_dexs(markets)

    _validate_args(
        environment=environment,
        target_gross=target_gross,
        max_leg_notional=max_leg_notional,
        book_fraction=book_fraction,
        spread_cap_bps=spread_cap_bps,
        min_order_size_usd=min_order_size_usd,
        slippage_bps=slippage_bps,
        args=args,
    )

    credential_env = hyperliquid_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("hyperliquid_live_volume_test=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"dex={dex or 'default'}")
    print(f"configured_perp_dexs={_fmt_dex_list(perp_dexs)}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"config={args.config}")
    print(f"average_window={plan.get('average_window', '5m')}")
    print(f"execute_live={args.execute_live}")
    print(f"target_gross_volume_usd={fmt_decimal(target_gross)}")
    print(f"max_leg_notional_usd={fmt_decimal(max_leg_notional)}")
    print(f"book_fraction={fmt_decimal(book_fraction)}")
    print(f"spread_rule=current_spread_bps_lte_min_{fmt_decimal(spread_cap_bps)}bps_and_market_average")
    print(f"min_order_size_usd={fmt_decimal(min_order_size_usd)}")
    print(f"slippage_bps={fmt_decimal(slippage_bps)}")
    print(f"adapter_allow_live_orders_config={allow_live_orders}")
    print(f"cycle_delay_seconds={args.cycle_delay_seconds}")
    print(f"roundtrip_mode={args.roundtrip_mode}")
    print("entry_order_type=limit_ioc_buy")
    close_order_type = "limit_ioc_sell" if args.roundtrip_mode == "netting" else "limit_ioc_reduce_only_sell"
    close_price_mode = "adapter_bulk_netting_close_sized_from_plan" if args.roundtrip_mode == "netting" else "adapter_reduce_only_close_sized_from_actual_fill"
    print(f"close_order_type={close_order_type}")
    print(f"close_price_mode={close_price_mode}")
    if args.roundtrip_mode == "netting":
        print("netting_submit_mode=bulk_orders_single_request")
    print("live_runner=core.live_volume.run_paired_volume")
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
        print("network_skipped=pass_--network_to_run_live_volume_preflight_or_post")
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
        info = _build_volume_sdk_info(api_endpoint, args, perp_dexs)
    except Exception as exc:
        print("live_ready=False")
        print(f"sdk_info_client_error={exc.__class__.__name__}")
        return

    start_private = _fetch_private_state_snapshot(info, credentials["account_address"], perp_dexs, "start")
    if start_private is None:
        print("live_ready=False")
        return
    print(f"start_position_count={start_private.position_count}")
    print(f"start_open_order_count={start_private.open_order_count}")
    if start_private.position_count != 0 or start_private.open_order_count != 0:
        print("live_ready=False")
        print("reason=existing_position_or_open_order_detected")
        return
    start_account_value = start_private.account_value
    print(f"start_account_value_usd={fmt_optional_decimal(start_account_value)}")

    try:
        market_info_by_coin = _load_market_info_by_api_coin(
            api_endpoint,
            markets=markets,
            timeout_seconds=args.timeout_seconds,
            min_order_size_usd=min_order_size_usd,
        )
    except (TimeoutError, OSError, ValueError, HyperliquidReadonlyConfigError) as exc:
        print("market_metadata_ok=False")
        print(f"market_metadata_error={exc.__class__.__name__}")
        print("live_ready=False")
        return
    print("market_metadata_ok=True")
    print(f"market_metadata_count={len(market_info_by_coin)}")

    account_fee_bps = _load_taker_fee_bps(api_endpoint, credentials["account_address"], args.timeout_seconds)
    print(f"account_taker_fee_bps={fmt_optional_decimal(account_fee_bps)}")
    if account_fee_bps is None:
        print("live_ready=False")
        print("reason=account_fee_unknown")
        return

    first_candidates = _discover_candidates(
        api_endpoint=api_endpoint,
        markets=markets,
        market_info_by_coin=market_info_by_coin,
        timeout_seconds=args.timeout_seconds,
        target_gross=target_gross,
        filled_gross=Decimal("0"),
        max_leg_notional=max_leg_notional,
        book_fraction=book_fraction,
        spread_cap_bps=spread_cap_bps,
        min_order_size_usd=min_order_size_usd,
        slippage_bps=slippage_bps,
    )
    _print_candidate_snapshot("initial", first_candidates)
    selected = _select_candidate(first_candidates)
    if selected is None:
        print("live_ready=False")
        print("reason=no_eligible_market_initially")
        return
    print(f"initial_selected_market={selected.config.display_market}")
    print(f"initial_selected_spread_bps={selected.spread_bps:.4f}")
    print(f"initial_selected_planned_gross_volume_usd={selected.planned_gross_volume_usd:.4f}")

    if not args.execute_live:
        print("live_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_ready=True")
        print("live_skipped=confirmation_mismatch")
        return
    if not allow_live_orders:
        print("live_ready=False")
        print("reason=adapter_allow_live_orders_config_false")
        return

    adapter = HyperliquidAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        perp_dexs=tuple(perp_dexs),
        allow_live_orders=allow_live_orders,
        max_roundtrip_gross_volume_usd=(max_leg_notional * Decimal("2")) + Decimal("5"),
    )
    adapter_signer_ready, adapter_signer_status = adapter.signer_ready()
    print(f"adapter_signer_ready={adapter_signer_ready}")
    print(f"adapter_signer_status={adapter_signer_status}")
    if not adapter_signer_ready:
        print("live_ready=False")
        print("reason=adapter_signer_not_ready")
        return

    run_start_monotonic = time.perf_counter()
    fills_start_ms = int(time.time() * 1000) - 2000
    scan_attempts = 0
    selected_plans: list[VolumeCandidate] = []

    def select_plan(remaining_gross_volume_usd: Decimal) -> RoundtripPlan | None:
        nonlocal scan_attempts
        filled_for_planning = max(Decimal("0"), target_gross - remaining_gross_volume_usd)
        while scan_attempts < args.max_scan_attempts:
            scan_attempts += 1
            candidates = _discover_candidates(
                api_endpoint=api_endpoint,
                markets=markets,
                market_info_by_coin=market_info_by_coin,
                timeout_seconds=args.timeout_seconds,
                target_gross=target_gross,
                filled_gross=filled_for_planning,
                max_leg_notional=max_leg_notional,
                book_fraction=book_fraction,
                spread_cap_bps=spread_cap_bps,
                min_order_size_usd=min_order_size_usd,
                slippage_bps=slippage_bps,
            )
            candidate = _select_candidate(candidates)
            if candidate is None:
                print(f"scan_{scan_attempts}_eligible_market=none")
                if args.no_candidate_delay_seconds:
                    time.sleep(args.no_candidate_delay_seconds)
                continue

            cycle = len(selected_plans) + 1
            print(f"cycle_{cycle}_selected_market={candidate.config.display_market}")
            print(f"cycle_{cycle}_api_coin={candidate.config.api_coin}")
            print(f"cycle_{cycle}_spread_bps={candidate.spread_bps:.4f}")
            print(f"cycle_{cycle}_spread_threshold_bps={candidate.spread_threshold_bps:.4f}")
            print(f"cycle_{cycle}_planned_size={fmt_decimal(candidate.size)}")
            print(f"cycle_{cycle}_planned_gross_volume_usd={candidate.planned_gross_volume_usd:.4f}")
            selected_plans.append(candidate)
            return RoundtripPlan(
                market=candidate.config.api_coin,
                instrument_id=candidate.market_info.asset_id,
                buy_price=candidate.aggressive_buy_px,
                sell_price=candidate.aggressive_sell_px,
                buy_size=candidate.size,
                sell_size=candidate.size,
                planned_gross_volume_usd=candidate.planned_gross_volume_usd,
                first_side="BUY",
                second_side="SELL",
                roundtrip_mode=args.roundtrip_mode,
                reason="hyperliquid_lowest_spread_core_adapter",
            )
        print("selected_plan_skipped=max_scan_attempts_reached")
        return None

    core_result = run_paired_volume(
        adapter=adapter,
        config=VolumeRunConfig(
            target_gross_volume_usd=target_gross,
            max_cycles=args.max_cycles,
            min_entry_delay_seconds=args.cycle_delay_seconds,
        ),
        select_plan=select_plan,
    )
    cycle_results = list(core_result.results)
    filled_gross = sum(
        (
            _order_result_notional(result.buy_result) + _order_result_notional(result.sell_result)
            for result in cycle_results
        ),
        Decimal("0"),
    )
    stop_reason = core_result.status

    if args.final_state_delay_seconds:
        time.sleep(args.final_state_delay_seconds)
    final_private = _fetch_private_state_snapshot(info, credentials["account_address"], perp_dexs, "final")
    final_position_count = final_private.position_count if final_private is not None else -1
    final_open_order_count = final_private.open_order_count if final_private is not None else -1
    final_account_value = final_private.account_value if final_private is not None else None

    fills_end_ms = int(time.time() * 1000) + 2000
    order_ids = {
        order.exchange_order_id
        for item in cycle_results
        for order in (item.buy_result, item.sell_result)
        if order is not None and order.exchange_order_id
    }
    fill_fee_usd = _sum_fees_from_user_fills(
        api_endpoint=api_endpoint,
        account=credentials["account_address"],
        start_ms=fills_start_ms,
        end_ms=fills_end_ms,
        timeout_seconds=args.timeout_seconds,
        order_ids=order_ids,
    )

    estimated_spread_loss = sum((_roundtrip_spread_loss(item) for item in cycle_results), Decimal("0"))
    estimated_fee = filled_gross * account_fee_bps / Decimal("10000")
    estimated_total_loss = estimated_spread_loss + estimated_fee
    fill_based_loss = estimated_spread_loss + (fill_fee_usd if fill_fee_usd is not None else estimated_fee)
    account_value_loss = None
    account_value_loss_reliable = False
    if start_account_value is not None and final_account_value is not None:
        account_value_loss = start_account_value - final_account_value
        account_value_loss_reliable = bool(filled_gross == 0 or start_account_value != 0 or final_account_value != 0)
    realized_loss = account_value_loss if account_value_loss_reliable else fill_based_loss

    elapsed_seconds = time.perf_counter() - run_start_monotonic
    print("summary_begin=True")
    print(f"stop_reason={stop_reason}")
    print(f"cycle_count={len(cycle_results)}")
    print(f"scan_attempt_count={scan_attempts}")
    print(f"core_planned_gross_volume_usd={core_result.planned_gross_volume_usd:.6f}")
    print(f"filled_gross_volume_usd={filled_gross:.6f}")
    print(f"target_gross_volume_usd={target_gross:.6f}")
    print(f"estimated_spread_loss_usd={estimated_spread_loss:.6f}")
    print(f"estimated_fee_usd={estimated_fee:.6f}")
    print(f"estimated_total_loss_usd={estimated_total_loss:.6f}")
    print(f"fill_fee_usd_from_user_fills={fmt_optional_decimal(fill_fee_usd)}")
    print(f"fill_based_loss_usd={fmt_optional_decimal(fill_based_loss)}")
    print(f"start_account_value_usd={fmt_optional_decimal(start_account_value)}")
    print(f"final_account_value_usd={fmt_optional_decimal(final_account_value)}")
    print(f"account_value_delta_loss_usd={fmt_optional_decimal(account_value_loss)}")
    print(f"account_value_delta_reliable={account_value_loss_reliable}")
    print(f"realized_loss_usd={fmt_optional_decimal(realized_loss)}")
    print(f"cpm_usd_per_1000_volume={fmt_optional_decimal(_cpm(realized_loss, filled_gross, Decimal('1000')))}")
    print(f"cost_usd_per_1m_volume={fmt_optional_decimal(_cpm(realized_loss, filled_gross, Decimal('1000000')))}")
    print(f"total_elapsed_seconds={elapsed_seconds:.2f}")
    print(f"final_position_count={final_position_count}")
    print(f"final_open_order_count={final_open_order_count}")
    print("summary_end=True")
    if final_position_count == 0 and final_open_order_count == 0:
        print("live_test_status=closed_flat_or_not_detected")
    else:
        print("live_test_status=position_or_open_order_manual_review_required")


def _validate_args(
    *,
    environment: str,
    target_gross: Decimal,
    max_leg_notional: Decimal,
    book_fraction: Decimal,
    spread_cap_bps: Decimal,
    min_order_size_usd: Decimal,
    slippage_bps: Decimal,
    args: argparse.Namespace,
) -> None:
    if environment != "PRODUCTION":
        raise SystemExit("This live volume test is currently limited to Hyperliquid production/mainnet")
    if target_gross <= 0 or target_gross > MAX_LIVE_TARGET_GROSS_VOLUME_USD:
        raise SystemExit(f"--target-gross-volume-usd must be > 0 and <= {MAX_LIVE_TARGET_GROSS_VOLUME_USD}")
    if max_leg_notional <= 0 or max_leg_notional > MAX_LIVE_LEG_NOTIONAL_USD:
        raise SystemExit(f"--max-leg-notional-usd must be > 0 and <= {MAX_LIVE_LEG_NOTIONAL_USD}")
    if book_fraction <= 0 or book_fraction > 1:
        raise SystemExit("--book-fraction must be > 0 and <= 1")
    if spread_cap_bps <= 0:
        raise SystemExit("--spread-cap-bps must be greater than zero")
    if min_order_size_usd <= 0 or min_order_size_usd > max_leg_notional:
        raise SystemExit("--min-order-size-usd must be > 0 and <= max leg notional")
    if slippage_bps <= 0 or slippage_bps > Decimal("100"):
        raise SystemExit("--slippage-bps must be > 0 and <= 100")
    if args.max_cycles <= 0:
        raise SystemExit("--max-cycles must be greater than zero")
    if args.max_scan_attempts <= 0:
        raise SystemExit("--max-scan-attempts must be greater than zero")
    if args.target_tolerance_usd < 0:
        raise SystemExit("--target-tolerance-usd must be zero or greater")
    if args.cycle_delay_seconds < 0 or args.no_candidate_delay_seconds < 0 or args.final_state_delay_seconds < 0:
        raise SystemExit("delay arguments must be zero or greater")


def _bool_config(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise SystemExit(f"{name} must be true or false")


def _market_configs(plan: dict[str, object]) -> list[MarketSpreadConfig]:
    raw_markets = plan.get("markets")
    if not isinstance(raw_markets, list):
        return []
    markets: list[MarketSpreadConfig] = []
    for item in raw_markets:
        if not isinstance(item, dict):
            continue
        coin = str(item.get("coin") or item.get("market") or "").strip()
        if not coin:
            continue
        api_coin = str(item.get("api_coin") or coin).strip()
        dex = str(item.get("dex") or "").strip()
        display = str(item.get("display_market") or f"{coin}-PERP")
        raw_average = item.get("average_spread_bps")
        average = Decimal(str(raw_average)) if raw_average not in (None, "", "n/a") else None
        enabled = bool(item.get("enabled", True))
        disabled_reason = str(item.get("disabled_reason") or "config_disabled")
        markets.append(
            MarketSpreadConfig(
                coin=coin,
                api_coin=api_coin,
                dex=dex,
                display_market=display,
                average_spread_bps=average,
                enabled=enabled,
                disabled_reason=disabled_reason,
            )
        )
    return markets


def _configured_perp_dexs(markets: list[MarketSpreadConfig]) -> list[str]:
    result = [""]
    for market in markets:
        if market.dex and market.dex not in result:
            result.append(market.dex)
    return result


def _load_market_info_by_api_coin(
    api_endpoint: str,
    *,
    markets: list[MarketSpreadConfig],
    timeout_seconds: float,
    min_order_size_usd: Decimal,
) -> dict[str, HyperliquidMarketInfo]:
    result: dict[str, HyperliquidMarketInfo] = {}
    dexes = _configured_perp_dexs(markets)
    for dex in dexes:
        min_order_size_by_coin = {
            market.api_coin: min_order_size_usd
            for market in markets
            if market.dex == dex
        }
        metadata = load_all_hyperliquid_market_info(
            api_endpoint,
            timeout_seconds=timeout_seconds,
            dex=dex,
            min_order_size_by_coin=min_order_size_by_coin,
        )
        result.update(metadata)
    return result


def _discover_candidates(
    *,
    api_endpoint: str,
    markets: list[MarketSpreadConfig],
    market_info_by_coin: dict[str, HyperliquidMarketInfo],
    timeout_seconds: float,
    target_gross: Decimal,
    filled_gross: Decimal,
    max_leg_notional: Decimal,
    book_fraction: Decimal,
    spread_cap_bps: Decimal,
    min_order_size_usd: Decimal,
    slippage_bps: Decimal,
) -> list[VolumeCandidate]:
    remaining_gross = max(Decimal("0"), target_gross - filled_gross)
    if remaining_gross / Decimal("2") >= min_order_size_usd:
        remaining_side_cap = remaining_gross / Decimal("2")
    else:
        remaining_side_cap = min_order_size_usd
    result: list[VolumeCandidate] = []
    for market in markets:
        if not market.enabled:
            result.append(_empty_candidate(market, market.disabled_reason))
            continue
        market_info = market_info_by_coin.get(market.api_coin)
        if market_info is None:
            result.append(_empty_candidate(market, "market_not_found_in_meta"))
            continue
        orderbook = fetch_hyperliquid_rest_top_of_book(
            api_endpoint,
            coin=market.api_coin,
            timeout_seconds=timeout_seconds,
            dex=market.dex,
        )
        if not orderbook.ok or orderbook.snapshot is None:
            result.append(_empty_candidate(market, orderbook.reason))
            continue
        snapshot = orderbook.snapshot
        threshold = min(spread_cap_bps, market.average_spread_bps) if market.average_spread_bps is not None else spread_cap_bps
        book_cap = min(
            snapshot.best_ask * snapshot.best_ask_size,
            snapshot.best_bid * snapshot.best_bid_size,
        ) * book_fraction
        leg_cap = min(max_leg_notional, book_cap, remaining_side_cap)
        size = _round_down_to_step(leg_cap / snapshot.best_ask, market_info.lot_size)
        entry_notional = size * snapshot.best_ask
        close_notional = size * snapshot.best_bid
        gross = entry_notional + close_notional
        eligible = True
        reason = "ok"
        if snapshot.spread_bps > threshold:
            eligible = False
            reason = f"spread_above_threshold:{snapshot.spread_bps:.4f}>{threshold:.4f}"
        elif size <= 0:
            eligible = False
            reason = "size_rounded_to_zero"
        elif entry_notional < min_order_size_usd or close_notional < min_order_size_usd:
            eligible = False
            reason = "notional_below_min_order_size"
        elif size > snapshot.best_ask_size or size > snapshot.best_bid_size:
            eligible = False
            reason = "top_level_size_too_small"
        elif market.average_spread_bps is None:
            reason = "ok_average_spread_missing_using_spread_cap"

        slippage_fraction = slippage_bps / Decimal("10000")
        result.append(
            VolumeCandidate(
                config=market,
                market_info=market_info,
                best_bid=snapshot.best_bid,
                best_ask=snapshot.best_ask,
                best_bid_size=snapshot.best_bid_size,
                best_ask_size=snapshot.best_ask_size,
                spread_bps=snapshot.spread_bps,
                spread_threshold_bps=threshold,
                size=size,
                planned_entry_notional_usd=entry_notional,
                planned_close_notional_usd=close_notional,
                planned_gross_volume_usd=gross,
                aggressive_buy_px=_sdk_style_slippage_price(
                    snapshot.best_ask,
                    is_buy=True,
                    slippage_fraction=slippage_fraction,
                    price_decimal_places=market_info.price_decimal_places,
                ),
                aggressive_sell_px=_sdk_style_slippage_price(
                    snapshot.best_bid,
                    is_buy=False,
                    slippage_fraction=slippage_fraction,
                    price_decimal_places=market_info.price_decimal_places,
                ),
                eligible=eligible,
                reason=reason,
            )
        )
    return result


def _empty_candidate(market: MarketSpreadConfig, reason: str) -> VolumeCandidate:
    return VolumeCandidate(
        config=market,
        market_info=HyperliquidMarketInfo(
            coin=market.api_coin,
            asset_id=-1,
            sz_decimals=0,
            lot_size=Decimal("0"),
            price_decimal_places=0,
            min_order_size_usd=None,
        ),
        best_bid=Decimal("0"),
        best_ask=Decimal("0"),
        best_bid_size=Decimal("0"),
        best_ask_size=Decimal("0"),
        spread_bps=Decimal("999999"),
        spread_threshold_bps=Decimal("0"),
        size=Decimal("0"),
        planned_entry_notional_usd=Decimal("0"),
        planned_close_notional_usd=Decimal("0"),
        planned_gross_volume_usd=Decimal("0"),
        aggressive_buy_px=Decimal("0"),
        aggressive_sell_px=Decimal("0"),
        eligible=False,
        reason=reason,
    )


def _select_candidate(candidates: list[VolumeCandidate]) -> VolumeCandidate | None:
    eligible = [candidate for candidate in candidates if candidate.eligible]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            item.spread_bps,
            -item.planned_gross_volume_usd,
            item.config.display_market,
        ),
    )


def _print_candidate_snapshot(label: str, candidates: list[VolumeCandidate]) -> None:
    eligible = [candidate for candidate in candidates if candidate.eligible]
    print(f"{label}_eligible_market_count={len(eligible)}")
    for candidate in sorted(candidates, key=lambda item: (not item.eligible, item.spread_bps, item.config.coin)):
        print(
            f"{label}_market={candidate.config.display_market} "
            f"api_coin={candidate.config.api_coin} "
            f"dex={candidate.config.dex or 'default'} "
            f"spread_bps={candidate.spread_bps:.4f} "
            f"threshold_bps={candidate.spread_threshold_bps:.4f} "
            f"planned_gross_usd={candidate.planned_gross_volume_usd:.4f} "
            f"eligible={candidate.eligible} "
            f"reason={candidate.reason}"
        )


def _build_volume_sdk_info(
    api_endpoint: str,
    args: argparse.Namespace,
    perp_dexs: list[str],
):
    from hyperliquid.info import Info

    return Info(api_endpoint, skip_ws=True, perp_dexs=perp_dexs, timeout=args.timeout_seconds)


def _fetch_private_state_snapshot(
    info,
    account: str,
    perp_dexs: list[str],
    label: str,
) -> PrivateStateSnapshot | None:
    total_positions = 0
    total_open_orders = 0
    account_value: Decimal | None = None
    for dex in perp_dexs:
        dex_label = dex or "default"
        state = _fetch_user_state(info, account, dex, f"{label}_{dex_label}")
        if state is None:
            return None
        open_orders = _fetch_open_orders(info, account, dex, f"{label}_{dex_label}")
        if open_orders is None:
            return None
        position_count = _position_count(state)
        open_order_count = len(open_orders)
        print(f"{label}_{dex_label}_position_count={position_count}")
        print(f"{label}_{dex_label}_open_order_count={open_order_count}")
        total_positions += position_count
        total_open_orders += open_order_count
        if dex == "":
            account_value = _account_value(state)
    return PrivateStateSnapshot(
        position_count=total_positions,
        open_order_count=total_open_orders,
        account_value=account_value,
    )


def _fetch_user_state(info, account: str, dex: str, label: str) -> object | None:
    try:
        state = info.user_state(account, dex=dex)
    except Exception as exc:
        print(f"{label}_private_state_ok=False")
        print(f"{label}_private_state_error={exc.__class__.__name__}")
        return None
    print(f"{label}_private_state_ok=True")
    return state


def _fetch_open_orders(info, account: str, dex: str, label: str) -> list[object] | None:
    try:
        open_orders = info.open_orders(account, dex=dex)
    except Exception as exc:
        print(f"{label}_open_orders_ok=False")
        print(f"{label}_open_orders_error={exc.__class__.__name__}")
        return None
    if not isinstance(open_orders, list):
        return []
    print(f"{label}_open_orders_ok=True")
    return open_orders


def _account_value(state: object | None) -> Decimal | None:
    if not isinstance(state, dict):
        return None
    for key in ("marginSummary", "crossMarginSummary"):
        summary = state.get(key)
        if isinstance(summary, dict) and summary.get("accountValue") not in (None, ""):
            return Decimal(str(summary["accountValue"]))
    return None


def _load_taker_fee_bps(api_endpoint: str, account: str, timeout_seconds: float) -> Decimal | None:
    try:
        account_fee = load_hyperliquid_account_fee(api_endpoint, account, timeout_seconds)
    except Exception as exc:
        print(f"account_fee_error_type={exc.__class__.__name__}")
        return None
    print(f"account_fee_level={account_fee.fee_level or 'unknown'}")
    print(f"account_maker_fee_bps={account_fee.maker_fee_bps}")
    return account_fee.taker_fee_bps


def _sum_fees_from_user_fills(
    *,
    api_endpoint: str,
    account: str,
    start_ms: int,
    end_ms: int,
    timeout_seconds: float,
    order_ids: set[str],
) -> Decimal | None:
    if not order_ids:
        return None
    try:
        payload = info_post_json(
            api_endpoint,
            {
                "type": "userFillsByTime",
                "user": account,
                "startTime": start_ms,
                "endTime": end_ms,
            },
            timeout_seconds,
            private_readonly=True,
        )
    except Exception as exc:
        print(f"user_fills_fee_lookup_error={exc.__class__.__name__}")
        return None
    if not isinstance(payload, list):
        return None
    fee = Decimal("0")
    matched = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("oid", ""))
        if oid not in order_ids:
            continue
        raw_fee = item.get("fee")
        if raw_fee in (None, ""):
            continue
        fee += abs(Decimal(str(raw_fee)))
        matched += 1
    print(f"user_fills_matched_count={matched}")
    return fee


def _order_result_notional(result: ExchangeOrderResult | None) -> Decimal:
    if result is None or result.average_price is None:
        return Decimal("0")
    return result.filled_size * result.average_price


def _roundtrip_spread_loss(result: object) -> Decimal:
    buy_result = getattr(result, "buy_result", None)
    sell_result = getattr(result, "sell_result", None)
    if buy_result is None or sell_result is None:
        return Decimal("0")
    return _order_result_notional(buy_result) - _order_result_notional(sell_result)


def _cpm(loss: Decimal | None, gross: Decimal, scale: Decimal) -> Decimal | None:
    if loss is None or gross <= 0:
        return None
    return loss / gross * scale


def fmt_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return fmt_decimal(value)


def _fmt_dex_list(values: list[str]) -> str:
    labels = [value or "default" for value in values]
    return ",".join(labels)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
