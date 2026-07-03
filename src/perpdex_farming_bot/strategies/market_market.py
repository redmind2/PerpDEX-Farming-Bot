from __future__ import annotations

from datetime import datetime

from perpdex_farming_bot.models import (
    MarketSnapshot,
    OrderIntent,
    OrderType,
    Side,
    StrategyDecision,
    TimeInForce,
)
from perpdex_farming_bot.strategies.base import BaseStrategy
from perpdex_farming_bot.strategies.sizing import capped_level_quantity


class MarketMarketStrategy(BaseStrategy):
    name = "market-market"
    _BPS_TOLERANCE = 1e-6

    def evaluate(self, snapshot: MarketSnapshot, now: datetime) -> StrategyDecision:
        if self.cooldown_active(now):
            return StrategyDecision(self.name, True, "cooldown active")

        max_spread = snapshot.average_spread_bps * self.config.strategy.spread_vs_average_ratio
        if snapshot.spread_bps - max_spread > self._BPS_TOLERANCE:
            return StrategyDecision(
                self.name,
                True,
                f"spread {snapshot.spread_bps:.4f} bps is above threshold {max_spread:.4f} bps",
            )

        if self.config.strategy.max_spread_bps > 0:
            configured_max_spread = self.config.strategy.max_spread_bps
            if snapshot.spread_bps - configured_max_spread > self._BPS_TOLERANCE:
                return StrategyDecision(
                    self.name,
                    True,
                    f"spread {snapshot.spread_bps:.4f} bps is above configured cap {configured_max_spread:.4f} bps",
                )

        smaller_level = (
            snapshot.best_bid
            if snapshot.best_bid.size <= snapshot.best_ask.size
            else snapshot.best_ask
        )
        quantity = capped_level_quantity(smaller_level, self.config)
        if quantity <= 0:
            return StrategyDecision(self.name, True, "quantity is zero after cap")

        self.mark_run(now)
        intents = (
            OrderIntent(
                strategy_name=self.name,
                exchange_id=self.config.execution_context.exchange_id,
                account_id=self.config.execution_context.account_id,
                wallet_id=self.config.execution_context.wallet_id,
                market=snapshot.market,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=quantity,
                time_in_force=TimeInForce.IOC,
                metadata={
                    "reference_price": snapshot.best_ask.price,
                    "paper_only": True,
                    "reason": "current spread is below average spread threshold",
                },
            ),
            OrderIntent(
                strategy_name=self.name,
                exchange_id=self.config.execution_context.exchange_id,
                account_id=self.config.execution_context.account_id,
                wallet_id=self.config.execution_context.wallet_id,
                market=snapshot.market,
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=quantity,
                time_in_force=TimeInForce.IOC,
                metadata={
                    "reference_price": snapshot.best_bid.price,
                    "paper_only": True,
                    "reason": "paired market close intent for paper simulation",
                },
            ),
        )
        return StrategyDecision(self.name, False, "paper market-market intents created", intents)
