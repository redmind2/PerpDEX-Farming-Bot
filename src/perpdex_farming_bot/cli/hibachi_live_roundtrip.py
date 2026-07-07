from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Literal

from perpdex_farming_bot.cli.hibachi_paper_cycle import _config_for_assignment, _load_snapshot
from perpdex_farming_bot.config import load_config
from perpdex_farming_bot.connectors import DataHubReadonlyConnector
from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    endpoint_from_env,
)
from perpdex_farming_bot.credentials import read_hibachi_credentials
from perpdex_farming_bot.env import get_env, load_dotenv_if_present


CONFIRM_TEXT = "LIVE_HIBACHI_BTC_MARKET_ROUNDTRIP"
TARGET_VOLUME_TOLERANCE_USD = Decimal("0.01")


@dataclass(frozen=True)
class HibachiFastCloseResult:
    success: bool
    status: str
    estimated_gross_volume_usd: Decimal
    entry_order_id: str | None = None
    close_order_id: str | None = None
    final_open_order_count: int | None = None
    final_position_size: Decimal = Decimal("0")
    final_all_flat: bool = False


def main() -> None:
    load_dotenv_if_present(".env")
    parser = argparse.ArgumentParser(
        description="One explicitly confirmed Hibachi live market roundtrip test.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hibachi.paper.json")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default=get_env("HIBACHI_LIVE_CREDENTIAL_PREFIX") or "HIBACHI_1_CRYPTO",
        help="Local env prefix for the Hibachi credential set to use. Secret values are never printed.",
    )
    parser.add_argument("--market", default="BTC/USDT-P")
    parser.add_argument("--data-source", choices=("data-hub-window-min", "market-config"), default="market-config")
    parser.add_argument("--data-hub-db", default=get_env("PERPDEX_DATA_HUB_DB") or "")
    parser.add_argument("--data-hub-immutable", action="store_true")
    parser.add_argument("--markets-config", default=get_env("PERPDEX_DATA_HUB_MARKETS_CONFIG") or "config/markets.json")
    parser.add_argument("--orderbook-source", choices=("hibachi-sdk",), default="hibachi-sdk")
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--orderbook-depth", type=int, default=5)
    parser.add_argument("--orderbook-granularity", type=float, default=0.0)
    parser.add_argument("--average-spread-samples", type=int, default=12)
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("5"))
    parser.add_argument("--max-fees-percent", type=Decimal, default=Decimal("0.0005"))
    parser.add_argument(
        "--target-gross-volume-usd",
        type=Decimal,
        default=Decimal("0"),
        help="Optional live loop target. Counts both sides, so a $5 paired BUY+SELL round counts as about $10.",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument(
        "--min-entry-delay-seconds",
        type=float,
        default=1.0,
        help=(
            "Minimum delay between live entry batches. The next cycle checks spread again after this delay. "
            "Residual ReduceOnly rescue closes are still sent immediately."
        ),
    )
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
            "Submit the reduce-only close immediately after a successful entry market POST, "
            "without waiting for a position REST check between orders."
        ),
    )
    parser.add_argument(
        "--prebuild-close-order",
        action="store_true",
        help=(
            "Request close-order prebuild before entry. Hibachi SDK market orders sign inside POST, "
            "so this currently logs unsupported and still uses SDK-internal signing."
        ),
    )
    parser.add_argument("--fill-lookup-attempts", type=int, default=5)
    parser.add_argument("--fill-lookup-delay-seconds", type=float, default=0.25)
    parser.add_argument(
        "--residual-settle-attempts",
        type=int,
        default=5,
        help="Poll the position this many times before sending a reduce-only rescue close.",
    )
    parser.add_argument(
        "--residual-settle-delay-seconds",
        type=float,
        default=0.25,
        help="Delay between residual-position settle checks.",
    )
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument("--max-idle-cycles", type=int, default=20)
    parser.add_argument(
        "--allow-existing-position",
        action="store_true",
        help="Allow starting while a market position already exists. Default aborts if the market is not flat.",
    )
    parser.add_argument(
        "--entry-mode",
        choices=("paired-market-batch", "buy-then-close"),
        default="paired-market-batch",
        help=(
            "paired-market-batch submits equal market buy/sell orders in one batch, "
            "then closes any residual position. buy-then-close is the earlier single-side test flow."
        ),
    )
    parser.add_argument(
        "--close-mode",
        choices=("immediate-reduce-only", "verified-position"),
        default="verified-position",
        help=(
            "verified-position reads the account position before closing. "
            "immediate-reduce-only closes the same token quantity immediately with ReduceOnly."
        ),
    )
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    load_dotenv_if_present(args.env_file)
    if not args.network:
        raise SystemExit("--network is required for live preflight because Hibachi public orderbook is needed")
    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > Decimal("100"):
        raise SystemExit("--max-notional-usd must be <= 100 for this first live test CLI")
    if args.target_gross_volume_usd < 0:
        raise SystemExit("--target-gross-volume-usd must be zero or greater")
    if args.target_gross_volume_usd > Decimal("100"):
        raise SystemExit("--target-gross-volume-usd must be <= 100 for this first live loop CLI")
    if args.poll_seconds < 0:
        raise SystemExit("--poll-seconds must be zero or greater")
    if args.min_entry_delay_seconds < 1:
        raise SystemExit("--min-entry-delay-seconds must be at least 1 for live safety")
    if args.loop_delay_seconds is not None and args.loop_delay_seconds < 0:
        raise SystemExit("--loop-delay-seconds must be zero or greater")
    if args.prebuild_close_order and not args.fast_close_on_fill:
        raise SystemExit("--prebuild-close-order requires --fast-close-on-fill")
    if args.fill_lookup_attempts <= 0:
        raise SystemExit("--fill-lookup-attempts must be greater than zero")
    if args.fill_lookup_delay_seconds < 0:
        raise SystemExit("--fill-lookup-delay-seconds must be zero or greater")
    if args.residual_settle_attempts <= 0:
        raise SystemExit("--residual-settle-attempts must be greater than zero")
    if args.residual_settle_delay_seconds < 0:
        raise SystemExit("--residual-settle-delay-seconds must be zero or greater")
    if args.max_cycles <= 0:
        raise SystemExit("--max-cycles must be greater than zero")
    if args.max_idle_cycles <= 0:
        raise SystemExit("--max-idle-cycles must be greater than zero")
    if args.data_source == "data-hub-window-min" and not args.data_hub_db:
        raise SystemExit("--data-hub-db or PERPDEX_DATA_HUB_DB is required for data-hub-window-min")

    config = load_config(Path(args.config))
    assignment = _enabled_assignment(config, args.market)
    run_config = _config_for_assignment(config, assignment)
    data_hub = (
        DataHubReadonlyConnector(args.data_hub_db, immutable=args.data_hub_immutable)
        if args.data_source == "data-hub-window-min"
        else None
    )
    snapshot_result = _load_snapshot(args, run_config, assignment, data_hub)

    print("hibachi_live_roundtrip=explicit_confirm_required")
    print("live_orders_possible=True")
    print(f"execute_live={args.execute_live}")
    print(f"market={assignment.market}")
    print(f"credential_prefix={args.credential_prefix}")
    print(f"data_source={args.data_source}")
    print(f"max_notional_usd={args.max_notional_usd}")
    print(f"target_gross_volume_usd={args.target_gross_volume_usd}")
    print(f"max_fees_percent={args.max_fees_percent}")
    print(f"min_entry_delay_seconds={args.min_entry_delay_seconds}")
    print(f"loop_delay_seconds={_entry_delay_seconds(args):.2f}")
    print(f"fast_close_on_fill={args.fast_close_on_fill}")
    print(f"prebuild_close_order={args.prebuild_close_order}")
    print("close_prebuild_supported=False")
    print("hibachi_signing_mode=sdk_internal_at_post")
    print(f"entry_mode={args.entry_mode}")
    print(f"close_mode={args.close_mode}")
    print(f"roundtrip_mode={'netting' if args.entry_mode == 'paired-market-batch' else args.close_mode}")

    if not snapshot_result.ok or snapshot_result.snapshot is None:
        print("preflight_ok=False")
        print(f"reason={snapshot_result.reason}")
        return

    snapshot = snapshot_result.snapshot
    spread_allowed, spread_reason = _live_spread_allowed(config, snapshot)
    if not spread_allowed and args.target_gross_volume_usd <= Decimal("0"):
        print("preflight_ok=False")
        print(f"reason={spread_reason}")
        return

    smaller_level = snapshot.best_bid if snapshot.best_bid.size <= snapshot.best_ask.size else snapshot.best_ask
    level_fraction_qty = Decimal(str(smaller_level.size)) * Decimal(str(config.strategy.level_size_fraction))
    cap_qty = args.max_notional_usd / Decimal(str(snapshot.best_ask.price))
    quantity = min(level_fraction_qty, cap_qty).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    notional = quantity * Decimal(str(snapshot.best_ask.price))
    first_side, second_side = _paired_market_sides(snapshot)

    print("preflight_ok=True")
    print(f"preflight_trade_allowed={spread_allowed}")
    if not spread_allowed:
        print(f"preflight_trade_block_reason={spread_reason}")
    print(f"spread_bps={snapshot.spread_bps:.4f}")
    print(f"threshold_bps={snapshot.average_spread_bps:.4f}")
    print(f"best_bid={snapshot.best_bid.price}")
    print(f"best_ask={snapshot.best_ask.price}")
    print(f"planned_quantity_btc={quantity}")
    print(f"planned_one_side_notional_usd={notional:.4f}")
    if args.fast_close_on_fill:
        print(f"planned_entry_side={first_side}")
        print(f"planned_close_side={_opposite_side(first_side)}")
        print("planned_entry=single_market_order")
        print("planned_close=immediate_reduce_only_market_close_same_quantity_without_position_read")
        print("planned_final_check=position_and_open_orders_after_roundtrip")
    elif args.entry_mode == "paired-market-batch":
        print(f"planned_first_market_side={first_side}")
        print(f"planned_second_market_side={second_side}")
        print("planned_entry=market_buy_and_market_sell_same_quantity_in_one_batch")
        print("planned_residual_close=read_position_after_batch_then_reduce_only_market_close_residual")
    else:
        print(f"planned_buy_quantity={quantity}")
        print(f"planned_buy_notional_usd={notional:.4f}")
        if args.close_mode == "immediate-reduce-only":
            print("planned_close=reduce_only_market_sell_same_quantity_without_position_read")
        else:
            print("planned_close=reduce_only_market_sell_after_position_read")

    if quantity <= 0:
        print("live_skipped=quantity_zero")
        return
    if not args.execute_live:
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_skipped=confirmation_mismatch")
        return

    from hibachi_xyz import HibachiApiClient
    from hibachi_xyz.types import (
        CreateOrder,
        CreateOrderBatchResponse,
        ErrorBatchResponse,
        OrderFlags,
        Side,
    )

    credentials = read_hibachi_credentials(args.credential_prefix)
    api_endpoint = endpoint_from_env(get_env("HIBACHI_API_ENDPOINT_PRODUCTION"), DEFAULT_HIBACHI_API_ENDPOINT)
    data_endpoint = endpoint_from_env(
        get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"),
        DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    )
    client = HibachiApiClient(
        api_url=api_endpoint,
        data_api_url=data_endpoint,
        api_key=credentials["api_key"],
        account_id=credentials["account_id"],
        private_key=credentials["private_key"],
    )

    start_direction, start_qty = _position_state(client.get_account_info(), assignment.market)
    print(f"start_position_direction={start_direction or 'flat'}")
    print(f"start_position_quantity={start_qty}")
    if start_qty > Decimal("0") and not args.allow_existing_position:
        print("live_aborted=existing_position_detected")
        return

    if args.target_gross_volume_usd > Decimal("0"):
        _run_live_volume_loop(args, config, run_config, assignment, data_hub, client)
        return

    if args.fast_close_on_fill:
        _execute_fast_close_market_roundtrip(
            client,
            assignment,
            args,
            quantity,
            first_side,
            one_side_notional_usd=notional,
        )
        return

    if args.entry_mode == "paired-market-batch":
        side_lookup = {"BUY": Side.BUY, "SELL": Side.SELL}
        paired_orders = [
            CreateOrder(
                assignment.market,
                side_lookup[first_side],
                str(quantity),
                args.max_fees_percent,
            ),
            CreateOrder(
                assignment.market,
                side_lookup[second_side],
                str(quantity),
                args.max_fees_percent,
            ),
        ]
        print("live_batch_submitting=True")
        response = client.batch_orders(paired_orders)
        print("live_batch_submitted=True")
        for index, order in enumerate(response.orders, start=1):
            if isinstance(order, CreateOrderBatchResponse):
                print(f"batch_order_{index}_status=success")
                print(f"batch_order_{index}_order_id={order.orderId}")
                print(f"batch_order_{index}_nonce={order.nonce}")
            elif isinstance(order, ErrorBatchResponse):
                print(f"batch_order_{index}_status=error")
                print(f"batch_order_{index}_error_code={order.errorCode}")
                print(f"batch_order_{index}_error_status={order.status}")
                print(f"batch_order_{index}_error_message={order.message}")
            else:
                print(f"batch_order_{index}_status=unexpected_response_type")
                print(f"batch_order_{index}_response_type={type(order).__name__}")

        residual_direction, residual_qty = _position_state(client.get_account_info(), assignment.market)
        print(f"residual_position_direction={residual_direction or 'flat'}")
        print(f"residual_position_quantity={residual_qty}")
        if residual_qty <= Decimal("0"):
            print("residual_close_skipped=flat")
            return

        close_side = Side.SELL if residual_direction == "long" else Side.BUY
        print(f"residual_close_side={'SELL' if close_side == Side.SELL else 'BUY'}")
        print("residual_close_submitting=True")
        close_nonce, close_order_id = client.place_market_order(
            assignment.market,
            str(residual_qty),
            close_side,
            args.max_fees_percent,
            order_flags=OrderFlags.ReduceOnly,
        )
        print("residual_close_submitted=True")
        print(f"residual_close_nonce={close_nonce}")
        print(f"residual_close_order_id={close_order_id}")
        return

    print("live_buy_submitting=True")
    buy_nonce, buy_order_id = client.place_market_order(
        assignment.market,
        str(quantity),
        Side.BUY,
        args.max_fees_percent,
    )
    print("live_buy_submitted=True")
    print(f"buy_nonce={buy_nonce}")
    print(f"buy_order_id={buy_order_id}")

    if args.close_mode == "immediate-reduce-only":
        close_qty = quantity
        print(f"close_quantity_source=planned_buy_quantity")
    else:
        close_direction, close_qty = _position_state(client.get_account_info(), assignment.market)
        if close_qty > Decimal("0") and close_direction != "long":
            print(f"live_close_skipped=detected_non_long_position_{close_direction}")
            return
        print(f"position_quantity_after_buy={close_qty}")
    if close_qty <= Decimal("0"):
        print("live_close_skipped=no_long_position_detected")
        return

    print("live_close_submitting=True")
    close_nonce, close_order_id = client.place_market_order(
        assignment.market,
        str(close_qty),
        Side.SELL,
        args.max_fees_percent,
        order_flags=OrderFlags.ReduceOnly,
    )
    print("live_close_submitted=True")
    print(f"close_nonce={close_nonce}")
    print(f"close_order_id={close_order_id}")


def _enabled_assignment(config: object, market: str) -> object:
    matches = [
        assignment
        for assignment in config.strategy_assignments
        if assignment.enabled
        and assignment.exchange_id == "hibachi"
        and assignment.strategy == "market-market"
        and assignment.market == market
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one enabled Hibachi market-market assignment for {market}")
    return matches[0]


def _run_live_volume_loop(
    args: argparse.Namespace,
    config: object,
    run_config: object,
    assignment: object,
    data_hub: DataHubReadonlyConnector | None,
    client: object,
) -> None:
    if args.entry_mode != "paired-market-batch" and not args.fast_close_on_fill:
        print("live_loop_aborted=target_volume_requires_paired_market_batch")
        return

    live_gross_volume = Decimal("0")
    idle_cycles = 0
    print("live_volume_loop_start=True")
    print("target_volume_counts=both_market_sides")
    print(f"entry_delay_seconds={_entry_delay_seconds(args):.2f}")
    print("spread_check_timing=after_entry_delay_before_each_batch")

    for cycle in range(1, args.max_cycles + 1):
        if _target_reached(live_gross_volume, args.target_gross_volume_usd):
            print(f"stop_reason=target_volume_reached:{live_gross_volume:.4f}>={args.target_gross_volume_usd:.4f}")
            print(f"final_estimated_gross_volume_usd={live_gross_volume:.4f}")
            return

        cycle_started_ns = _now_ns()
        plan_started_ns = _now_ns()

        snapshot_result = _load_snapshot(args, run_config, assignment, data_hub)
        print(f"cycle={cycle} plan_latency_ms={_elapsed_ms(plan_started_ns)}")
        if not snapshot_result.ok or snapshot_result.snapshot is None:
            idle_cycles += 1
            print(f"cycle={cycle} market_data_ok=False reason={snapshot_result.reason}")
            if idle_cycles >= args.max_idle_cycles:
                print(f"stop_reason=max_idle_cycles_reached:{idle_cycles}")
                return
            if args.poll_seconds:
                time.sleep(args.poll_seconds)
            continue

        snapshot = snapshot_result.snapshot
        spread_allowed, spread_reason = _live_spread_allowed(run_config, snapshot)
        if not spread_allowed:
            idle_cycles += 1
            print(
                f"cycle={cycle} market_data_ok=True trade_allowed=False "
                f"spread_bps={snapshot.spread_bps:.4f} threshold_bps={snapshot.average_spread_bps:.4f} "
                f"reason={spread_reason}"
            )
            if idle_cycles >= args.max_idle_cycles:
                print(f"stop_reason=max_idle_cycles_reached:{idle_cycles}")
                return
            if args.poll_seconds:
                time.sleep(args.poll_seconds)
            continue

        idle_cycles = 0
        quantity, notional, first_side, second_side = _round_plan(args, config, snapshot)
        print(
            f"cycle={cycle} market_data_ok=True spread_bps={snapshot.spread_bps:.4f} "
            f"threshold_bps={snapshot.average_spread_bps:.4f} quantity_btc={quantity} "
            f"one_side_notional_usd={notional:.4f} first_side={first_side} second_side={second_side}"
        )

        if quantity <= Decimal("0"):
            idle_cycles += 1
            print(f"cycle={cycle} live_skipped=quantity_zero")
            if idle_cycles >= args.max_idle_cycles:
                print(f"stop_reason=max_idle_cycles_reached:{idle_cycles}")
                return
            if args.poll_seconds:
                time.sleep(args.poll_seconds)
            continue

        if args.fast_close_on_fill:
            result = _execute_fast_close_market_roundtrip(
                client,
                assignment,
                args,
                quantity,
                first_side,
                one_side_notional_usd=notional,
                label_prefix=f"cycle={cycle}",
                cycle_started_ns=cycle_started_ns,
                plan_latency_already_logged=True,
            )
            status = result.status
        else:
            status = _execute_paired_market_batch(client, assignment, args, quantity, first_side, second_side)
        if status != "ok_flat":
            print(f"stop_reason={status}")
            print(f"final_estimated_gross_volume_usd={live_gross_volume:.4f}")
            return

        round_gross_volume = notional * Decimal("2")
        live_gross_volume += round_gross_volume
        print(f"cycle={cycle} live_round_gross_volume_usd={round_gross_volume:.4f}")
        print(f"live_total_gross_volume_usd={live_gross_volume:.4f}")

        if _target_reached(live_gross_volume, args.target_gross_volume_usd):
            print(f"stop_reason=target_volume_reached:{live_gross_volume:.4f}>={args.target_gross_volume_usd:.4f}")
            print(f"final_estimated_gross_volume_usd={live_gross_volume:.4f}")
            return
        entry_delay = _entry_delay_seconds(args)
        if entry_delay:
            print(f"next_loop_delay_seconds={entry_delay:.2f}")
            time.sleep(entry_delay)

    print(f"stop_reason=max_cycles_reached:{args.max_cycles}")
    print(f"final_estimated_gross_volume_usd={live_gross_volume:.4f}")


def _entry_delay_seconds(args: argparse.Namespace) -> float:
    if getattr(args, "loop_delay_seconds", None) is not None:
        return float(args.loop_delay_seconds)
    return max(float(args.poll_seconds), float(args.min_entry_delay_seconds))


def _target_reached(volume_usd: Decimal, target_usd: Decimal) -> bool:
    return volume_usd + TARGET_VOLUME_TOLERANCE_USD >= target_usd


def _live_spread_allowed(config: object, snapshot: object) -> tuple[bool, str]:
    spread_bps = Decimal(str(snapshot.spread_bps))
    average_threshold = Decimal(str(snapshot.average_spread_bps)) * Decimal(str(config.strategy.spread_vs_average_ratio))
    if spread_bps > average_threshold:
        return (
            False,
            f"live_spread_above_average:{spread_bps:.4f}>{average_threshold:.4f}",
        )

    configured_cap = Decimal(str(config.strategy.max_spread_bps))
    if configured_cap > Decimal("0") and spread_bps > configured_cap:
        return (
            False,
            f"live_spread_above_configured_cap:{spread_bps:.4f}>{configured_cap:.4f}",
        )

    return True, "live_spread_ok"


def _round_plan(
    args: argparse.Namespace,
    config: object,
    snapshot: object,
) -> tuple[Decimal, Decimal, Literal["BUY", "SELL"], Literal["BUY", "SELL"]]:
    smaller_level = snapshot.best_bid if snapshot.best_bid.size <= snapshot.best_ask.size else snapshot.best_ask
    level_fraction_qty = Decimal(str(smaller_level.size)) * Decimal(str(config.strategy.level_size_fraction))
    cap_qty = args.max_notional_usd / Decimal(str(snapshot.best_ask.price))
    quantity = min(level_fraction_qty, cap_qty).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    notional = quantity * Decimal(str(snapshot.best_ask.price))
    first_side, second_side = _paired_market_sides(snapshot)
    return quantity, notional, first_side, second_side


def _execute_paired_market_batch(
    client: object,
    assignment: object,
    args: argparse.Namespace,
    quantity: Decimal,
    first_side: Literal["BUY", "SELL"],
    second_side: Literal["BUY", "SELL"],
) -> str:
    from hibachi_xyz.types import (
        CreateOrder,
        CreateOrderBatchResponse,
        ErrorBatchResponse,
        OrderFlags,
        Side,
    )

    side_lookup = {"BUY": Side.BUY, "SELL": Side.SELL}
    paired_orders = [
        CreateOrder(
            assignment.market,
            side_lookup[first_side],
            str(quantity),
            args.max_fees_percent,
        ),
        CreateOrder(
            assignment.market,
            side_lookup[second_side],
            str(quantity),
            args.max_fees_percent,
        ),
    ]
    print("live_batch_submitting=True")
    response = client.batch_orders(paired_orders)
    print("live_batch_submitted=True")

    success_count = 0
    buy_order_ids: list[int] = []
    sell_order_ids: list[int] = []
    planned_sides = (first_side, second_side)
    for index, order in enumerate(response.orders, start=1):
        if isinstance(order, CreateOrderBatchResponse):
            success_count += 1
            order_id = int(order.orderId)
            if planned_sides[index - 1] == "BUY":
                buy_order_ids.append(order_id)
            else:
                sell_order_ids.append(order_id)
            print(f"batch_order_{index}_status=success")
            print(f"batch_order_{index}_order_id={order_id}")
            print(f"batch_order_{index}_nonce={order.nonce}")
        elif isinstance(order, ErrorBatchResponse):
            print(f"batch_order_{index}_status=error")
            print(f"batch_order_{index}_error_code={order.errorCode}")
            print(f"batch_order_{index}_error_status={order.status}")
            print(f"batch_order_{index}_error_message={order.message}")
        else:
            print(f"batch_order_{index}_status=unexpected_response_type")
            print(f"batch_order_{index}_response_type={type(order).__name__}")

    if getattr(args, "skip_fill_spread_lookup", False):
        print("actual_fill_spread_status=skipped")
    else:
        _print_actual_fill_spread(client, assignment.market, buy_order_ids, sell_order_ids, args)

    residual_direction, residual_qty = _settled_position_state(client, assignment.market, args)
    if residual_qty <= Decimal("0"):
        if success_count == 2:
            print("residual_close_skipped=flat")
            return "ok_flat"
        print("residual_close_skipped=flat_but_batch_not_all_success")
        return "batch_not_all_success"

    close_side = Side.SELL if residual_direction == "long" else Side.BUY
    print(f"residual_close_side={'SELL' if close_side == Side.SELL else 'BUY'}")
    print("residual_close_submitting=True")
    close_nonce, close_order_id = client.place_market_order(
        assignment.market,
        str(residual_qty),
        close_side,
        args.max_fees_percent,
        order_flags=OrderFlags.ReduceOnly,
    )
    print("residual_close_submitted=True")
    print(f"residual_close_nonce={close_nonce}")
    print(f"residual_close_order_id={close_order_id}")

    final_direction, final_qty = _settled_position_state(client, assignment.market, args, label="final")
    if final_qty <= Decimal("0"):
        return "residual_closed_stop_for_review"
    return "residual_close_failed_position_remains"


def _execute_fast_close_market_roundtrip(
    client: object,
    assignment: object,
    args: argparse.Namespace,
    quantity: Decimal,
    entry_side: Literal["BUY", "SELL"],
    *,
    one_side_notional_usd: Decimal | None = None,
    label_prefix: str = "",
    cycle_started_ns: int | None = None,
    plan_latency_already_logged: bool = False,
) -> HibachiFastCloseResult:
    from hibachi_xyz.types import OrderFlags, Side

    prefix = f"{label_prefix} " if label_prefix else ""
    cycle_started = cycle_started_ns or _now_ns()
    plan_started_ns = _now_ns()
    close_side_name = _opposite_side(entry_side)
    side_lookup = {"BUY": Side.BUY, "SELL": Side.SELL}
    estimated_gross = (one_side_notional_usd or Decimal("0")) * Decimal("2")

    if not plan_latency_already_logged:
        print(f"{prefix}plan_latency_ms={_elapsed_ms(plan_started_ns)}")
    print(f"{prefix}fast_close_on_fill=True")
    print(f"{prefix}entry_side={entry_side}")
    print(f"{prefix}close_side={close_side_name}")
    print(f"{prefix}close_quantity_source=planned_entry_quantity")
    print(f"{prefix}partial_fill_handling=reduce_only_close_uses_planned_quantity_without_fill_ws")

    if getattr(args, "prebuild_close_order", False):
        close_prebuild_started_ns = _now_ns()
        print(f"{prefix}close_order_prebuild_requested=True")
        print(f"{prefix}close_order_prebuild_supported=False")
        print(f"{prefix}close_prebuild_reason=hibachi_sdk_market_orders_sign_inside_post")
        print(f"{prefix}close_prebuild_sign_latency_ms={_elapsed_ms(close_prebuild_started_ns)}")

    entry_sign_started_ns = _now_ns()
    print(f"{prefix}entry_sign_latency_ms={_elapsed_ms(entry_sign_started_ns)}")
    print(f"{prefix}entry_sign_latency_source=sdk_internal_not_separately_measured")
    print(f"{prefix}entry_submitting=True")
    entry_post_started_ns = _now_ns()
    try:
        entry_nonce, entry_order_id = client.place_market_order(
            assignment.market,
            str(quantity),
            side_lookup[entry_side],
            args.max_fees_percent,
        )
    except Exception as exc:
        entry_post_done_ns = _now_ns()
        print(f"{prefix}entry_post_latency_ms={_elapsed_ms(entry_post_started_ns, entry_post_done_ns)}")
        print(f"{prefix}entry_order_failed=True")
        print(f"{prefix}entry_error_type={exc.__class__.__name__}")
        print(f"{prefix}cycle_total_latency_ms={_elapsed_ms(cycle_started, entry_post_done_ns)}")
        return HibachiFastCloseResult(False, "entry_order_failed", estimated_gross)

    entry_post_done_ns = _now_ns()
    print(f"{prefix}entry_submitted=True")
    print(f"{prefix}entry_nonce={entry_nonce}")
    print(f"{prefix}entry_order_id={entry_order_id}")
    print(f"{prefix}entry_post_latency_ms={_elapsed_ms(entry_post_started_ns, entry_post_done_ns)}")
    print(f"{prefix}entry_fill_confirmation=market_post_success_no_private_fill_ws")
    print(f"{prefix}post_entry_position_check_skipped=True")

    if not getattr(args, "prebuild_close_order", False):
        close_sign_started_ns = _now_ns()
        print(f"{prefix}close_sign_latency_ms={_elapsed_ms(close_sign_started_ns)}")
        print(f"{prefix}close_sign_latency_source=sdk_internal_not_separately_measured")

    print(f"{prefix}close_submitting=True")
    print(f"{prefix}entry_to_close_submit_gap_ms={_elapsed_ms(entry_post_done_ns)}")
    close_post_started_ns = _now_ns()
    try:
        close_nonce, close_order_id = client.place_market_order(
            assignment.market,
            str(quantity),
            side_lookup[close_side_name],
            args.max_fees_percent,
            order_flags=OrderFlags.ReduceOnly,
        )
    except Exception as exc:
        close_post_done_ns = _now_ns()
        print(f"{prefix}close_post_latency_ms={_elapsed_ms(close_post_started_ns, close_post_done_ns)}")
        print(f"{prefix}close_order_failed=True")
        print(f"{prefix}close_error_type={exc.__class__.__name__}")
        print(f"{prefix}manual_review_required=True")
        print(f"{prefix}cycle_total_latency_ms={_elapsed_ms(cycle_started, close_post_done_ns)}")
        return HibachiFastCloseResult(
            False,
            "close_order_failed",
            estimated_gross,
            entry_order_id=str(entry_order_id),
        )

    close_post_done_ns = _now_ns()
    print(f"{prefix}close_submitted=True")
    print(f"{prefix}close_nonce={close_nonce}")
    print(f"{prefix}close_order_id={close_order_id}")
    print(f"{prefix}close_post_latency_ms={_elapsed_ms(close_post_started_ns, close_post_done_ns)}")

    final_state = _print_final_market_state(client, assignment.market, args, label_prefix=label_prefix)
    print(f"{prefix}cycle_total_latency_ms={_elapsed_ms(cycle_started, close_post_done_ns)}")
    status = "ok_flat" if final_state.final_all_flat else "fast_close_final_not_flat"
    return HibachiFastCloseResult(
        final_state.final_all_flat,
        status,
        estimated_gross,
        entry_order_id=str(entry_order_id),
        close_order_id=str(close_order_id),
        final_open_order_count=final_state.final_open_order_count,
        final_position_size=final_state.final_position_size,
        final_all_flat=final_state.final_all_flat,
    )


def _opposite_side(side: Literal["BUY", "SELL"]) -> Literal["BUY", "SELL"]:
    return "SELL" if side == "BUY" else "BUY"


def _print_final_market_state(
    client: object,
    market: str,
    args: argparse.Namespace,
    *,
    label_prefix: str = "",
) -> HibachiFastCloseResult:
    prefix = f"{label_prefix} " if label_prefix else ""
    direction, quantity = _settled_position_state(client, market, args, label="final")
    open_order_count = _open_order_count(client, market)
    all_flat = quantity <= Decimal("0") and open_order_count == 0
    print(f"{prefix}final_open_order_count={open_order_count if open_order_count is not None else 'unknown'}")
    print(f"{prefix}final_position_direction={direction or 'flat'}")
    print(f"{prefix}final_position_size={quantity}")
    print(f"{prefix}final_position_steps=not_available_for_hibachi_sdk")
    print(f"{prefix}final_all_flat={all_flat}")
    return HibachiFastCloseResult(
        all_flat,
        "ok_flat" if all_flat else "final_not_flat",
        Decimal("0"),
        final_open_order_count=open_order_count,
        final_position_size=quantity,
        final_all_flat=all_flat,
    )


def _open_order_count(client: object, market: str) -> int | None:
    try:
        response = client.get_pending_orders()
    except Exception as exc:
        print(f"final_open_order_lookup_error={exc.__class__.__name__}")
        return None
    return sum(1 for order in getattr(response, "orders", ()) if getattr(order, "symbol", "") == market)


def _now_ns() -> int:
    return time.perf_counter_ns()


def _elapsed_ms(start_ns: int, end_ns: int | None = None) -> str:
    end = end_ns if end_ns is not None else _now_ns()
    return f"{(end - start_ns) / 1_000_000:.3f}"


def _print_actual_fill_spread(
    client: object,
    market: str,
    buy_order_ids: list[int],
    sell_order_ids: list[int],
    args: argparse.Namespace,
) -> None:
    if not buy_order_ids or not sell_order_ids:
        print("actual_fill_spread_status=unavailable_missing_order_id")
        return

    last_buy_qty = Decimal("0")
    last_sell_qty = Decimal("0")
    for attempt in range(1, args.fill_lookup_attempts + 1):
        try:
            buy_fills, sell_fills = _collect_order_fills(client, market, buy_order_ids, sell_order_ids)
        except Exception as exc:
            print("actual_fill_spread_status=lookup_error")
            print(f"actual_fill_spread_error_type={exc.__class__.__name__}")
            return

        buy_qty, buy_vwap = _vwap(buy_fills)
        sell_qty, sell_vwap = _vwap(sell_fills)
        last_buy_qty = buy_qty
        last_sell_qty = sell_qty
        if buy_qty > Decimal("0") and sell_qty > Decimal("0"):
            mid = (buy_vwap + sell_vwap) / Decimal("2")
            spread_usd = abs(buy_vwap - sell_vwap)
            spread_bps = (spread_usd / mid) * Decimal("10000") if mid > Decimal("0") else Decimal("0")
            signed_buy_minus_sell = buy_vwap - sell_vwap
            print("actual_fill_spread_status=found")
            print(f"actual_buy_vwap={buy_vwap:.4f}")
            print(f"actual_sell_vwap={sell_vwap:.4f}")
            print(f"actual_buy_quantity={buy_qty}")
            print(f"actual_sell_quantity={sell_qty}")
            print(f"actual_spread_usd={spread_usd:.4f}")
            print(f"actual_spread_bps={spread_bps:.4f}")
            print(f"actual_buy_minus_sell_usd={signed_buy_minus_sell:.4f}")
            return

        if attempt < args.fill_lookup_attempts and args.fill_lookup_delay_seconds:
            time.sleep(args.fill_lookup_delay_seconds)

    print("actual_fill_spread_status=unavailable_not_found")
    print(f"actual_buy_quantity_found={last_buy_qty}")
    print(f"actual_sell_quantity_found={last_sell_qty}")


def _collect_order_fills(
    client: object,
    market: str,
    buy_order_ids: list[int],
    sell_order_ids: list[int],
) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
    buy_set = set(buy_order_ids)
    sell_set = set(sell_order_ids)
    buy_fills: list[tuple[Decimal, Decimal]] = []
    sell_fills: list[tuple[Decimal, Decimal]] = []

    account_trades = client.get_account_trades()
    for trade in getattr(account_trades, "trades", ()):
        if getattr(trade, "symbol", "") != market:
            continue
        price = Decimal(str(getattr(trade, "price", "0")))
        quantity = Decimal(str(getattr(trade, "quantity", "0")))
        bid_order_id = int(getattr(trade, "bidOrderId", 0) or 0)
        ask_order_id = int(getattr(trade, "askOrderId", 0) or 0)
        if bid_order_id in buy_set or ask_order_id in buy_set:
            buy_fills.append((price, quantity))
        if ask_order_id in sell_set or bid_order_id in sell_set:
            sell_fills.append((price, quantity))

    return buy_fills, sell_fills


def _vwap(fills: list[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal]:
    quantity = sum((fill_qty for _, fill_qty in fills), Decimal("0"))
    if quantity <= Decimal("0"):
        return Decimal("0"), Decimal("0")
    notional = sum((price * fill_qty for price, fill_qty in fills), Decimal("0"))
    return quantity, notional / quantity


def _settled_position_state(
    client: object,
    market: str,
    args: argparse.Namespace,
    *,
    label: str = "residual",
) -> tuple[Literal["long", "short", ""], Decimal]:
    attempts = max(1, int(getattr(args, "residual_settle_attempts", 1)))
    delay_seconds = max(0.0, float(getattr(args, "residual_settle_delay_seconds", 0.0)))

    direction, quantity = _position_state(client.get_account_info(), market)
    print(f"{label}_position_direction={direction or 'flat'}")
    print(f"{label}_position_quantity={quantity}")
    if quantity <= Decimal("0"):
        return direction, quantity

    for attempt in range(2, attempts + 1):
        if delay_seconds:
            time.sleep(delay_seconds)
        direction, quantity = _position_state(client.get_account_info(), market)
        print(f"{label}_settle_attempt={attempt}")
        print(f"{label}_settle_position_direction={direction or 'flat'}")
        print(f"{label}_settle_position_quantity={quantity}")
        if quantity <= Decimal("0"):
            print(f"{label}_settle_resolved=flat")
            return direction, quantity

    print(f"{label}_settle_resolved=False")
    return direction, quantity


def _paired_market_sides(snapshot: object) -> tuple[Literal["BUY", "SELL"], Literal["BUY", "SELL"]]:
    best_bid_size = Decimal(str(snapshot.best_bid.size))
    best_ask_size = Decimal(str(snapshot.best_ask.size))
    if best_bid_size < best_ask_size:
        return ("SELL", "BUY")
    return ("BUY", "SELL")


def _position_state(account_info: object, market: str) -> tuple[Literal["long", "short", ""], Decimal]:
    for position in getattr(account_info, "positions", ()):
        if getattr(position, "symbol", "") != market:
            continue
        quantity = Decimal(str(getattr(position, "quantity", "0")))
        direction = str(getattr(position, "direction", "")).lower()
        if quantity > 0 and direction in {"long", "bid", "buy"}:
            return ("long", quantity)
        if quantity > 0 and direction in {"short", "ask", "sell"}:
            return ("short", quantity)
        if quantity > 0 and not direction:
            return ("long", quantity)
    return ("", Decimal("0"))


if __name__ == "__main__":
    main()
