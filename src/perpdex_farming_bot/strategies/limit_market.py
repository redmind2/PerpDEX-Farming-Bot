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


class LimitMarketStrategy(BaseStrategy):
    name = "limit-market"

    def evaluate(self, snapshot: MarketSnapshot, now: datetime) -> StrategyDecision:
        if self.cooldown_active(now):
            return StrategyDecision(self.name, True, "cooldown active")

        intents: list[OrderIntent] = []
        max_gap = snapshot.spread * self.config.strategy.max_gap_to_spread_ratio

        if snapshot.second_bid is not None:
            bid_gap = snapshot.best_bid.price - snapshot.second_bid.price
            if 0 <= bid_gap <= max_gap:
                quantity = capped_level_quantity(snapshot.best_bid, self.config)
                intents.append(
                    OrderIntent(
                        strategy_name=self.name,
                        exchange_id=self.config.execution_context.exchange_id,
                        account_id=self.config.execution_context.account_id,
                        wallet_id=self.config.execution_context.wallet_id,
                        market=snapshot.market,
                        side=Side.BUY,
                        order_type=OrderType.LIMIT,
                        quantity=quantity,
                        price=snapshot.best_bid.price,
                        time_in_force=TimeInForce.GTC,
                        metadata={
                            "paper_only": True,
                            "exit_plan": "market sell immediately after simulated fill",
                            "bid_gap": bid_gap,
                        },
                    )
                )

        if snapshot.second_ask is not None:
            ask_gap = snapshot.second_ask.price - snapshot.best_ask.price
            if 0 <= ask_gap <= max_gap:
                quantity = capped_level_quantity(snapshot.best_ask, self.config)
                intents.append(
                    OrderIntent(
                        strategy_name=self.name,
                        exchange_id=self.config.execution_context.exchange_id,
                        account_id=self.config.execution_context.account_id,
                        wallet_id=self.config.execution_context.wallet_id,
                        market=snapshot.market,
                        side=Side.SELL,
                        order_type=OrderType.LIMIT,
                        quantity=quantity,
                        price=snapshot.best_ask.price,
                        time_in_force=TimeInForce.GTC,
                        metadata={
                            "paper_only": True,
                            "exit_plan": "market buy immediately after simulated fill",
                            "ask_gap": ask_gap,
                        },
                    )
                )

        intents = [intent for intent in intents if intent.quantity > 0]
        if not intents:
            return StrategyDecision(self.name, True, "no best-level gap matched the threshold")

        self.mark_run(now)
        return StrategyDecision(self.name, False, "paper limit-market entry intents created", tuple(intents))
