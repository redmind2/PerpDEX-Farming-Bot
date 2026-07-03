from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from perpdex_farming_bot.analytics import calculate_metrics
from perpdex_farming_bot.brokers import PaperBroker
from perpdex_farming_bot.budget import BudgetState, current_weekly_window
from perpdex_farming_bot.cli.hibachi_paper_cycle import _config_for_assignment, _empty_metrics, _load_snapshot
from perpdex_farming_bot.config import load_config
from perpdex_farming_bot.connectors import DataHubReadonlyConnector
from perpdex_farming_bot.env import load_dotenv_if_present
from perpdex_farming_bot.risk import RiskDecision, RiskEngine
from perpdex_farming_bot.runtime_control import DEFAULT_RUNTIME_CONTROL_PATH, control_decision, load_runtime_control
from perpdex_farming_bot.storage import WeeklyLedger
from perpdex_farming_bot.strategies import MarketMarketStrategy


def main() -> None:
    load_dotenv_if_present(".env")
    parser = argparse.ArgumentParser(
        description="Run a repeated Hibachi paper-only monitor loop. This never sends real orders.",
    )
    parser.add_argument("--config", default="config/hibachi.paper.json")
    parser.add_argument("--db", default=os.environ.get("PERPDEX_FARMING_BOT_DB", "data/hibachi_paper.sqlite"))
    parser.add_argument("--market", default="BTC/USDT-P")
    parser.add_argument("--data-source", choices=("data-hub-window-min", "market-config"), default="data-hub-window-min")
    parser.add_argument("--data-hub-db", default=os.environ.get("PERPDEX_DATA_HUB_DB") or "")
    parser.add_argument("--data-hub-immutable", action="store_true")
    parser.add_argument("--markets-config", default=os.environ.get("PERPDEX_DATA_HUB_MARKETS_CONFIG") or "config/markets.json")
    parser.add_argument("--orderbook-source", choices=("hibachi-sdk", "mock"), default="hibachi-sdk")
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--orderbook-depth", type=int, default=5)
    parser.add_argument("--orderbook-granularity", type=float, default=0.0)
    parser.add_argument("--average-spread-samples", type=int, default=12)
    parser.add_argument("--target-volume-usd", type=float, default=1000.0)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--max-cycles", type=int, default=200)
    parser.add_argument("--control-file", default=os.environ.get("PERPDEX_RUNTIME_CONTROL_FILE") or DEFAULT_RUNTIME_CONTROL_PATH)
    parser.add_argument("--no-record", action="store_true")
    args = parser.parse_args()

    if args.target_volume_usd <= 0:
        raise SystemExit("--target-volume-usd must be greater than zero")
    if args.poll_seconds < 0:
        raise SystemExit("--poll-seconds must be zero or greater")
    if args.max_cycles <= 0:
        raise SystemExit("--max-cycles must be greater than zero")
    if args.orderbook_source == "hibachi-sdk" and not args.network:
        raise SystemExit("--network is required when --orderbook-source hibachi-sdk")
    if args.data_source == "data-hub-window-min" and not args.data_hub_db:
        raise SystemExit("--data-hub-db or PERPDEX_DATA_HUB_DB is required for data-hub-window-min")

    config = load_config(Path(args.config))
    now = datetime.now(timezone.utc)
    week = current_weekly_window(now, config.budget.period_start_weekday_utc)
    ledger = WeeklyLedger(args.db)
    if not args.no_record:
        ledger.init()

    data_hub = None
    if args.data_source == "data-hub-window-min":
        data_hub = DataHubReadonlyConnector(args.data_hub_db, immutable=args.data_hub_immutable)

    assignments = [
        assignment
        for assignment in config.strategy_assignments
        if assignment.enabled
        and assignment.exchange_id == "hibachi"
        and assignment.strategy == "market-market"
        and assignment.market == args.market
    ]
    if len(assignments) != 1:
        raise SystemExit(f"expected exactly one enabled Hibachi market-market assignment for {args.market}")

    assignment = assignments[0]
    run_config = _config_for_assignment(config, assignment)
    paper_volume_usd = 0.0
    paper_loss_usd = 0.0

    print("hibachi_paper_monitor=paper_only_no_orders")
    print(f"market={assignment.market}")
    print(f"data_source={args.data_source}")
    if args.data_source == "data-hub-window-min":
        print(f"data_hub_symbol={assignment.data_hub_symbol or assignment.market}")
        print(f"data_hub_db={args.data_hub_db}")
        print(f"data_hub_immutable={args.data_hub_immutable}")
        print("spread_threshold_source=min_1d_7d_average_bps")
    else:
        print(f"markets_config={args.markets_config}")
        print("spread_threshold_source=markets_config_average_bps")
    print(f"orderbook_source={args.orderbook_source}")
    print(f"public_network_enabled={args.network}")
    print(f"target_volume_usd={args.target_volume_usd:.2f}")
    print(f"max_order_notional_usd={config.risk.max_order_notional_usd:.2f}")
    print(f"level_size_fraction={config.strategy.level_size_fraction:.4f}")
    print(f"poll_seconds={args.poll_seconds:.2f}")
    print(f"control_file={args.control_file}")
    print("orders_sent=False")

    for cycle in range(1, args.max_cycles + 1):
        now = datetime.now(timezone.utc)
        control_state = load_runtime_control(args.control_file)
        control = control_decision(
            control_state,
            exchange_id=assignment.exchange_id,
            wallet_id=assignment.wallet_id,
            market=assignment.market,
        )
        if not control.enabled:
            print(f"cycle={cycle} runtime_control_enabled=False reason={control.reason}")
            if args.poll_seconds:
                time.sleep(args.poll_seconds)
            continue

        budget_state = (
            ledger.load_budget_state(week, assignment.exchange_id, assignment.account_id, assignment.wallet_id)
            if not args.no_record
            else BudgetState(period_volume_usd=paper_volume_usd, period_realized_loss_usd=paper_loss_usd)
        )
        if budget_state.period_volume_usd >= args.target_volume_usd:
            print(f"stop_reason=target_volume_reached:{budget_state.period_volume_usd:.2f}>={args.target_volume_usd:.2f}")
            break

        snapshot_result = _load_snapshot(args, run_config, assignment, data_hub)
        if not snapshot_result.ok or snapshot_result.snapshot is None:
            print(f"cycle={cycle} market_data_ok=False reason={snapshot_result.reason}")
            if args.poll_seconds:
                time.sleep(args.poll_seconds)
            continue

        snapshot = snapshot_result.snapshot
        decision = MarketMarketStrategy(run_config).evaluate(snapshot, now)
        risk_decision = (
            RiskEngine(run_config).review(snapshot, decision.intents, budget_state)
            if decision.intents
            else RiskDecision(False, "strategy produced no paper intents", ())
        )
        fills = ()
        metrics = _empty_metrics(run_config)
        if risk_decision.approved:
            fills = PaperBroker().execute(snapshot, risk_decision.approved_intents, now)
            metrics = calculate_metrics(run_config, snapshot, fills)

        if not args.no_record:
            ledger.record_paper_run(
                week,
                now,
                assignment.exchange_id,
                assignment.account_id,
                assignment.wallet_id,
                assignment.market,
                assignment.strategy,
                decision.reason,
                risk_decision.approved,
                risk_decision.reason,
                len(fills),
                metrics,
            )
        else:
            paper_volume_usd += metrics.gross_volume_usd
            paper_loss_usd += metrics.realized_loss_usd

        total_volume = budget_state.period_volume_usd + metrics.gross_volume_usd
        print(
            f"cycle={cycle} market_data_ok=True spread_bps={snapshot.spread_bps:.4f} "
            f"threshold_bps={snapshot.average_spread_bps:.4f} decision={decision.reason} "
            f"risk_approved={risk_decision.approved} paper_fill_count={len(fills)} "
            f"paper_volume_usd={metrics.gross_volume_usd:.2f} total_volume_usd={total_volume:.2f}"
        )
        if total_volume >= args.target_volume_usd:
            print(f"stop_reason=target_volume_reached:{total_volume:.2f}>={args.target_volume_usd:.2f}")
            break
        if args.poll_seconds:
            time.sleep(args.poll_seconds)
    else:
        print(f"stop_reason=max_cycles_reached:{args.max_cycles}")


if __name__ == "__main__":
    main()
