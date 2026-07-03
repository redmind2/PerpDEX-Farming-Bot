from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from perpdex_farming_bot.analytics import calculate_metrics
from perpdex_farming_bot.brokers import PaperBroker
from perpdex_farming_bot.budget import BudgetState
from perpdex_farming_bot.config import load_config
from perpdex_farming_bot.connectors import mock_snapshot
from perpdex_farming_bot.risk import RiskEngine
from perpdex_farming_bot.strategies import (
    LimitMarketStrategy,
    LimitStoplossStrategy,
    MarketMarketStrategy,
    PairedDeltaNeutralStrategy,
)


def _format_optional(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.8f}"


def _configured_notional(
    total_collateral_usd: float,
    fixed_usd: float,
    pct_of_collateral: float | None,
) -> float:
    candidates = [fixed_usd]
    if pct_of_collateral is not None:
        candidates.append(total_collateral_usd * pct_of_collateral)
    return min(candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a paper-only PerpDEX strategy skeleton.")
    parser.add_argument(
        "--config",
        default="config/default.paper.json",
        help="Path to paper config JSON.",
    )
    parser.add_argument(
        "--strategy",
        choices=("market-market", "limit-market", "limit-stoploss", "paired-delta-neutral"),
        default="market-market",
        help="Paper strategy skeleton to evaluate.",
    )
    parser.add_argument(
        "--paired-phase",
        choices=("entry", "exit"),
        default="entry",
        help="Entry or exit phase for paired delta-neutral paper strategy.",
    )
    parser.add_argument(
        "--held-seconds",
        type=int,
        default=0,
        help="Current hold time for paired delta-neutral exit checks.",
    )
    parser.add_argument(
        "--current-pair-position-usd",
        type=float,
        default=0.0,
        help="Current paired position notional for paper cap checks.",
    )
    parser.add_argument(
        "--period-volume-usd",
        type=float,
        default=0.0,
        help="Already used period volume. This is a paper budget input.",
    )
    parser.add_argument(
        "--period-loss-usd",
        type=float,
        default=0.0,
        help="Already realized period loss. This is a paper budget input.",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    snapshot = mock_snapshot(config.execution_context.exchange_id, config.execution_context.market)
    strategy = {
        "market-market": MarketMarketStrategy,
        "limit-market": LimitMarketStrategy,
        "limit-stoploss": LimitStoplossStrategy,
        "paired-delta-neutral": PairedDeltaNeutralStrategy,
    }[args.strategy](config)
    if isinstance(strategy, PairedDeltaNeutralStrategy):
        strategy.set_runtime_state(
            phase=args.paired_phase,
            held_seconds=args.held_seconds,
            current_pair_position_usd=args.current_pair_position_usd,
        )

    now = datetime.now(timezone.utc)
    print(f"exchange_id={config.execution_context.exchange_id}")
    print(f"account_id={config.execution_context.account_id}")
    print(f"wallet_id={config.execution_context.wallet_id}")
    print(f"market={config.execution_context.market}")
    if isinstance(strategy, PairedDeltaNeutralStrategy):
        print(f"delta_neutral_total_collateral_usd={config.strategy.delta_neutral_total_collateral_usd:.2f}")
        print(
            "delta_neutral_round_notional_usd="
            f"{_configured_notional(config.strategy.delta_neutral_total_collateral_usd, config.strategy.delta_neutral_notional_cap_usd, config.strategy.delta_neutral_notional_pct_of_collateral):.2f}"
        )
        print(
            "delta_neutral_max_pair_position_usd="
            f"{_configured_notional(config.strategy.delta_neutral_total_collateral_usd, config.strategy.delta_neutral_max_pair_position_usd, config.strategy.delta_neutral_max_pair_position_pct_of_collateral):.2f}"
        )
    decision = strategy.evaluate(snapshot, now)
    print(f"strategy={decision.strategy_name}")
    print(f"decision={decision.reason}")
    print(f"intent_count={len(decision.intents)}")

    budget_state = BudgetState(
        period_volume_usd=args.period_volume_usd,
        period_realized_loss_usd=args.period_loss_usd,
    )
    risk_decision = RiskEngine(config).review(snapshot, decision.intents, budget_state)
    print(f"risk_approved={risk_decision.approved}")
    print(f"risk_reason={risk_decision.reason}")
    print(f"budget_period={config.budget.period_name}")
    print(
        "budget_used "
        f"volume={budget_state.period_volume_usd:.2f}/"
        f"{config.budget.max_period_volume_usd:.2f} "
        f"loss={budget_state.period_realized_loss_usd:.2f}/"
        f"{config.budget.max_period_loss_usd:.2f}"
    )

    if not risk_decision.approved:
        return

    fills = PaperBroker().execute(snapshot, risk_decision.approved_intents, now)
    print(f"paper_fill_count={len(fills)}")
    for fill in fills:
        print(
            "paper_fill "
            f"account={fill.source_intent.account_id} "
            f"wallet={fill.source_intent.wallet_id} "
            f"side={fill.side.value} "
            f"type={fill.source_intent.order_type.value} "
            f"qty={fill.quantity:.6f} "
            f"price={fill.fill_price:.6f} "
            f"notional={fill.notional_usd:.2f}"
        )

    metrics = calculate_metrics(config, snapshot, fills)
    print(f"metrics_gross_volume_usd={metrics.gross_volume_usd:.2f}")
    print(f"metrics_realized_pnl_usd={metrics.realized_pnl_usd:.6f}")
    print(f"metrics_realized_loss_usd={metrics.realized_loss_usd:.6f}")
    print(f"metrics_loss_per_volume={_format_optional(metrics.loss_per_volume)}")
    print(f"metrics_points_status={metrics.points_status}")
    print(f"metrics_points_estimate={_format_optional(metrics.points_estimate)}")
    print(f"metrics_points_per_volume={_format_optional(metrics.points_per_volume)}")
    print(f"metrics_points_per_loss={_format_optional(metrics.points_per_loss)}")
    print(
        "budget_round "
        f"volume={metrics.gross_volume_usd:.2f}/"
        f"{config.budget.max_round_volume_usd:.2f} "
        f"loss={metrics.realized_loss_usd:.6f}/"
        f"{config.budget.max_round_loss_usd:.6f}"
    )
    print(
        "budget_round_ok="
        f"{metrics.gross_volume_usd <= config.budget.max_round_volume_usd and metrics.realized_loss_usd <= config.budget.max_round_loss_usd}"
    )
    projected_period_loss = budget_state.period_realized_loss_usd + metrics.realized_loss_usd
    projected_period_volume = budget_state.period_volume_usd + metrics.gross_volume_usd
    print(
        "budget_projected_period "
        f"volume={projected_period_volume:.2f}/"
        f"{config.budget.max_period_volume_usd:.2f} "
        f"loss={projected_period_loss:.6f}/"
        f"{config.budget.max_period_loss_usd:.6f}"
    )


if __name__ == "__main__":
    main()
