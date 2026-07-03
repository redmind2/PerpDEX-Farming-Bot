from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from perpdex_farming_bot.analytics import PerformanceMetrics, calculate_metrics
from perpdex_farming_bot.brokers import PaperBroker
from perpdex_farming_bot.budget import current_weekly_window
from perpdex_farming_bot.config import BotConfig, ExecutionContextConfig, StrategyAssignmentConfig, load_config
from perpdex_farming_bot.connectors import (
    DataHubReadonlyConnector,
    DataHubSnapshotResult,
    load_hibachi_orderbook_snapshot,
    mock_snapshot,
)
from perpdex_farming_bot.env import load_dotenv_if_present
from perpdex_farming_bot.risk import RiskDecision, RiskEngine
from perpdex_farming_bot.storage import WeeklyLedger
from perpdex_farming_bot.strategies import MarketMarketStrategy


def main() -> None:
    load_dotenv_if_present(".env")
    parser = argparse.ArgumentParser(
        description="Run one Hibachi paper-only market-market scan cycle across configured markets.",
    )
    parser.add_argument(
        "--config",
        default="config/hibachi.paper.json",
        help="Path to Hibachi paper config JSON.",
    )
    parser.add_argument(
        "--db",
        default="data/hibachi_paper.sqlite",
        help="Local SQLite file for weekly paper metrics.",
    )
    parser.add_argument(
        "--market",
        action="append",
        default=[],
        help="Optional market filter. Repeat for multiple markets.",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Evaluate the cycle without writing SQLite records.",
    )
    parser.add_argument(
        "--max-spread-bps",
        type=float,
        default=None,
        help="Override config strategy.max_spread_bps for this run. Use 0 to disable the configured cap.",
    )
    parser.add_argument(
        "--data-source",
        choices=("mock", "data-hub", "data-hub-window-min", "market-config"),
        default="mock",
        help=(
            "Spread signal source. data-hub-window-min uses the smaller 1D/7D Data Hub average; "
            "market-config uses config/markets.json."
        ),
    )
    parser.add_argument(
        "--orderbook-source",
        choices=("mock", "hibachi-sdk"),
        default="mock",
        help="Orderbook source for best bid/ask size. hibachi-sdk uses public get_orderbook.",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Allow public network calls for --orderbook-source hibachi-sdk.",
    )
    parser.add_argument(
        "--data-hub-db",
        default=os.environ.get("PERPDEX_DATA_HUB_DB", ""),
        help="Dashboard/Data Hub SQLite path. Required when using Data Hub sources.",
    )
    parser.add_argument(
        "--markets-config",
        default=os.environ.get("PERPDEX_DATA_HUB_MARKETS_CONFIG", "config/markets.json"),
        help="Market catalog JSON used when --data-source market-config.",
    )
    parser.add_argument(
        "--data-hub-immutable",
        action="store_true",
        help="Open the Data Hub DB as a read-only immutable snapshot. Use only for local copied/static DB files.",
    )
    parser.add_argument(
        "--average-spread-samples",
        type=int,
        default=12,
        help="Number of latest Data Hub snapshots used for average spread.",
    )
    parser.add_argument(
        "--orderbook-depth",
        type=int,
        default=5,
        help="Depth for real-time Hibachi public orderbook reads.",
    )
    parser.add_argument(
        "--orderbook-granularity",
        type=float,
        default=0.0,
        help="Granularity for real-time Hibachi public orderbook reads. Use 0 for auto.",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    if args.max_spread_bps is not None:
        if args.max_spread_bps < 0:
            raise SystemExit("--max-spread-bps must be zero or greater")
        config = replace(config, strategy=replace(config.strategy, max_spread_bps=args.max_spread_bps))

    now = datetime.now(timezone.utc)
    week = current_weekly_window(now, config.budget.period_start_weekday_utc)
    ledger = WeeklyLedger(args.db)
    if not args.no_record:
        ledger.init()
    data_hub = None
    if args.data_source in {"data-hub", "data-hub-window-min"}:
        if not args.data_hub_db:
            raise SystemExit("--data-hub-db or PERPDEX_DATA_HUB_DB is required for Data Hub sources")
        data_hub = DataHubReadonlyConnector(args.data_hub_db, immutable=args.data_hub_immutable)

    print("hibachi_paper_cycle=paper_only_no_orders")
    print(f"config={args.config}")
    print(f"db={args.db}")
    print(f"data_source={args.data_source}")
    print(f"orderbook_source={args.orderbook_source}")
    print(f"public_network_enabled={args.network}")
    if args.data_source in {"data-hub", "data-hub-window-min"}:
        print(f"data_hub_db={args.data_hub_db}")
        print(f"data_hub_immutable={args.data_hub_immutable}")
    if args.data_source == "market-config":
        print(f"markets_config={args.markets_config}")
    print(f"period_name={config.budget.period_name}")
    print(f"period_start_utc={week.start_utc.isoformat()}")
    print(f"period_end_utc={week.end_utc.isoformat()}")
    print(f"max_weekly_volume_usd={config.budget.max_period_volume_usd:.2f}")
    print(f"max_weekly_loss_usd={config.budget.max_period_loss_usd:.2f}")
    print(f"max_spread_bps={config.strategy.max_spread_bps:.4f}")
    print("orders_sent=False")

    market_filter = set(args.market)
    assignments = [
        assignment
        for assignment in config.strategy_assignments
        if assignment.enabled
        and assignment.exchange_id == "hibachi"
        and assignment.strategy == "market-market"
        and (not market_filter or assignment.market in market_filter)
    ]
    print(f"assignment_count={len(assignments)}")

    for assignment in assignments:
        run_config = _config_for_assignment(config, assignment)
        budget_state = (
            ledger.load_budget_state(week, assignment.exchange_id, assignment.account_id, assignment.wallet_id)
            if not args.no_record
            else None
        )
        snapshot_result = _load_snapshot(args, run_config, assignment, data_hub)
        if not snapshot_result.ok or snapshot_result.snapshot is None:
            metrics = _empty_metrics(run_config)
            decision_reason = f"market_data_no_trade:{snapshot_result.reason}"
            if not args.no_record:
                ledger.record_paper_run(
                    week,
                    now,
                    assignment.exchange_id,
                    assignment.account_id,
                    assignment.wallet_id,
                    assignment.market,
                    assignment.strategy,
                    decision_reason,
                    False,
                    "market data rejected before strategy",
                    0,
                    metrics,
                )
            print(f"market={assignment.market}")
            if args.data_source in {"data-hub", "data-hub-window-min"}:
                print(f"  data_hub_symbol={assignment.data_hub_symbol or assignment.market}")
            print(f"  market_data_ok=False")
            print(f"  market_data_reason={snapshot_result.reason}")
            print(f"  decision={decision_reason}")
            print("  risk_approved=False")
            print("  paper_fill_count=0")
            print("  paper_volume_usd=0.00")
            print("  paper_realized_loss_usd=0.000000")
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

        print(f"market={assignment.market}")
        if args.data_source in {"data-hub", "data-hub-window-min"}:
            print(f"  data_hub_symbol={assignment.data_hub_symbol or assignment.market}")
        print(f"  market_data_ok=True")
        print(f"  market_data_reason={snapshot_result.reason}")
        print(f"  data_age_seconds={snapshot.age_seconds:.0f}")
        print(f"  spread_bps={snapshot.spread_bps:.4f}")
        print(f"  average_spread_bps={snapshot.average_spread_bps:.4f}")
        print(f"  decision={decision.reason}")
        print(f"  risk_approved={risk_decision.approved}")
        print(f"  risk_reason={risk_decision.reason}")
        print(f"  paper_fill_count={len(fills)}")
        print(f"  paper_volume_usd={metrics.gross_volume_usd:.2f}")
        print(f"  paper_realized_loss_usd={metrics.realized_loss_usd:.6f}")

    if not args.no_record and assignments:
        first = assignments[0]
        totals = ledger.weekly_totals(week, first.exchange_id, first.account_id, first.wallet_id)
        print("weekly_totals_scope=first_account_wallet")
        print(f"weekly_volume_usd={totals.gross_volume_usd:.2f}")
        print(f"weekly_realized_loss_usd={totals.realized_loss_usd:.6f}")
        print(f"weekly_run_count={totals.run_count}")


def _config_for_assignment(config: BotConfig, assignment: StrategyAssignmentConfig) -> BotConfig:
    return replace(
        config,
        execution_context=ExecutionContextConfig(
            exchange_id=assignment.exchange_id,
            account_id=assignment.account_id,
            wallet_id=assignment.wallet_id,
            market=assignment.market,
        ),
    )


def _load_snapshot(
    args: argparse.Namespace,
    config: BotConfig,
    assignment: StrategyAssignmentConfig,
    data_hub: DataHubReadonlyConnector | None,
) -> DataHubSnapshotResult:
    if args.data_source == "mock":
        return DataHubSnapshotResult(True, "mock_snapshot", mock_snapshot(assignment.exchange_id, assignment.market))
    if args.data_source == "market-config":
        return _load_market_config_snapshot(args, assignment)
    if data_hub is None:
        return DataHubSnapshotResult(False, "data_hub_connector_missing")

    data_hub_symbol = assignment.data_hub_symbol or assignment.market
    if args.data_source == "data-hub-window-min":
        spread_result = data_hub.latest_spread_signal_with_window_min(
            assignment.exchange_id,
            data_hub_symbol,
        )
    else:
        spread_result = data_hub.latest_spread_signal(
            assignment.exchange_id,
            data_hub_symbol,
            average_spread_samples=args.average_spread_samples,
        )
    if not spread_result.ok or spread_result.signal is None:
        return DataHubSnapshotResult(False, spread_result.reason)

    signal = spread_result.signal
    if signal.age_seconds > config.risk.max_price_age_seconds:
        return DataHubSnapshotResult(
            False,
            f"data_hub_spread_stale:{signal.age_seconds:.0f}>{config.risk.max_price_age_seconds}",
        )

    average_threshold = signal.average_spread_bps * config.strategy.spread_vs_average_ratio
    if signal.spread_bps > average_threshold:
        return DataHubSnapshotResult(
            False,
            f"data_hub_spread_above_average:{signal.spread_bps:.4f}>{average_threshold:.4f}",
        )
    if config.strategy.max_spread_bps > 0 and signal.spread_bps > config.strategy.max_spread_bps:
        return DataHubSnapshotResult(
            False,
            f"data_hub_spread_above_configured_cap:{signal.spread_bps:.4f}>{config.strategy.max_spread_bps:.4f}",
        )

    if args.orderbook_source == "mock":
        snapshot = mock_snapshot(assignment.exchange_id, assignment.market)
        return DataHubSnapshotResult(
            True,
            f"data_hub_spread_ok_mock_orderbook:age_seconds={signal.age_seconds:.0f}",
            replace(snapshot, average_spread_bps=signal.average_spread_bps),
        )

    if not args.network:
        return DataHubSnapshotResult(False, "hibachi_sdk_orderbook_network_disabled")

    orderbook_result = load_hibachi_orderbook_snapshot(
        assignment.market,
        average_spread_bps=signal.average_spread_bps,
        depth=args.orderbook_depth,
        granularity=args.orderbook_granularity,
    )
    if not orderbook_result.ok or orderbook_result.snapshot is None:
        return DataHubSnapshotResult(False, orderbook_result.reason)
    return DataHubSnapshotResult(
        True,
        f"data_hub_spread_ok_hibachi_orderbook:age_seconds={signal.age_seconds:.0f}",
        orderbook_result.snapshot,
    )


def _load_market_config_snapshot(
    args: argparse.Namespace,
    assignment: StrategyAssignmentConfig,
) -> DataHubSnapshotResult:
    market = _market_config_entry(Path(args.markets_config), assignment.exchange_id, assignment.market)
    if market is None:
        return DataHubSnapshotResult(False, "market_config_entry_missing")
    if not bool(market.get("enabled_for_paper", False)):
        return DataHubSnapshotResult(False, "market_config_paper_disabled")

    average_spread_bps = _market_config_average_spread_bps(market)
    if args.orderbook_source == "mock":
        snapshot = mock_snapshot(assignment.exchange_id, assignment.market)
        return DataHubSnapshotResult(
            True,
            "market_config_ok_mock_orderbook",
            replace(snapshot, average_spread_bps=average_spread_bps),
        )

    if not args.network:
        return DataHubSnapshotResult(False, "hibachi_sdk_orderbook_network_disabled")

    orderbook_result = load_hibachi_orderbook_snapshot(
        assignment.market,
        average_spread_bps=average_spread_bps,
        depth=args.orderbook_depth,
        granularity=args.orderbook_granularity,
    )
    if not orderbook_result.ok or orderbook_result.snapshot is None:
        return DataHubSnapshotResult(False, orderbook_result.reason)
    return DataHubSnapshotResult(True, "market_config_ok_hibachi_orderbook", orderbook_result.snapshot)


def _market_config_entry(path: Path, exchange_id: str, market: str) -> dict[str, object] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None

    for item in raw.get("markets", ()):
        if not isinstance(item, dict):
            continue
        if str(item.get("exchange_id", "")).casefold() == exchange_id.casefold() and item.get("market") == market:
            return item
    return None


def _market_config_average_spread_bps(market: dict[str, object]) -> float:
    spread_reference = market.get("spread_reference", {})
    if isinstance(spread_reference, dict) and spread_reference.get("average_spread_bps") is not None:
        return float(spread_reference["average_spread_bps"])

    paper_thresholds = market.get("paper_thresholds", {})
    if isinstance(paper_thresholds, dict) and paper_thresholds.get("max_spread_bps") is not None:
        return float(paper_thresholds["max_spread_bps"])

    raise SystemExit("markets config entry must include spread_reference.average_spread_bps")


def _empty_metrics(config: BotConfig) -> PerformanceMetrics:
    return PerformanceMetrics(
        gross_volume_usd=0.0,
        realized_pnl_usd=0.0,
        realized_loss_usd=0.0,
        loss_per_volume=None,
        points_estimate=None,
        points_status="not_evaluated",
        points_per_volume=None,
        points_per_loss=None,
    )


if __name__ == "__main__":
    main()
