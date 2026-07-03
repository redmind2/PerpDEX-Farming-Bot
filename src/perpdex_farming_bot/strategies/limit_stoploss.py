from __future__ import annotations

from datetime import datetime, timedelta

from perpdex_farming_bot.models import (
    MarketSnapshot,
    OrderIntent,
    OrderType,
    Side,
    StrategyDecision,
    TimeInForce,
    TradePrint,
)
from perpdex_farming_bot.strategies.base import BaseStrategy
from perpdex_farming_bot.strategies.sizing import capped_level_quantity


class LimitStoplossStrategy(BaseStrategy):
    name = "limit-stoploss"

    def evaluate(self, snapshot: MarketSnapshot, now: datetime) -> StrategyDecision:
        if self.cooldown_active(now):
            return StrategyDecision(self.name, True, "cooldown active")

        trades = self._recent_repeating_trades(snapshot, now)
        if len(trades) < self.config.strategy.min_repeating_trade_count:
            return StrategyDecision(self.name, True, "not enough repeating trade-tape activity")

        buy_count = sum(1 for trade in trades if trade.side is Side.BUY)
        sell_count = len(trades) - buy_count

        if buy_count >= sell_count:
            entry_side = Side.SELL
            entry_level = snapshot.best_ask
            stop_side = Side.BUY
            stop_price = self._short_stop_price(snapshot)
        else:
            entry_side = Side.BUY
            entry_level = snapshot.best_bid
            stop_side = Side.SELL
            stop_price = self._long_stop_price(snapshot)

        quantity = capped_level_quantity(entry_level, self.config)
        if quantity <= 0:
            return StrategyDecision(self.name, True, "quantity is zero after cap")

        self.mark_run(now)
        entry = OrderIntent(
            strategy_name=self.name,
            exchange_id=self.config.execution_context.exchange_id,
            account_id=self.config.execution_context.account_id,
            wallet_id=self.config.execution_context.wallet_id,
            market=snapshot.market,
            side=entry_side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=entry_level.price,
            time_in_force=TimeInForce.GTC,
            metadata={
                "paper_only": True,
                "trigger": "repeating trade-tape activity",
                "recent_trade_count": len(trades),
            },
        )
        stop = OrderIntent(
            strategy_name=self.name,
            exchange_id=self.config.execution_context.exchange_id,
            account_id=self.config.execution_context.account_id,
            wallet_id=self.config.execution_context.wallet_id,
            market=snapshot.market,
            side=stop_side,
            order_type=OrderType.STOP_MARKET,
            quantity=quantity,
            price=stop_price,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            metadata={
                "paper_only": True,
                "protects_entry_side": entry_side.value,
                "reason": "paper stoploss derived from second-best level and tick size",
            },
        )
        return StrategyDecision(self.name, False, "paper limit entry and stoploss intents created", (entry, stop))

    def _recent_repeating_trades(self, snapshot: MarketSnapshot, now: datetime) -> tuple[TradePrint, ...]:
        window_start = now - timedelta(seconds=self.config.strategy.trade_tape_window_seconds)
        recent = tuple(trade for trade in snapshot.recent_trades if trade.timestamp >= window_start)
        if len(recent) < 2:
            return ()

        alternating_pairs = 0
        for previous, current in zip(recent, recent[1:]):
            if previous.side is not current.side:
                alternating_pairs += 1
        if alternating_pairs == 0:
            return ()
        return recent

    def _short_stop_price(self, snapshot: MarketSnapshot) -> float:
        if snapshot.second_ask is None:
            return snapshot.best_ask.price + snapshot.tick_size
        candidate = snapshot.second_ask.price - snapshot.tick_size
        if candidate <= snapshot.best_ask.price:
            return snapshot.second_ask.price
        return candidate

    def _long_stop_price(self, snapshot: MarketSnapshot) -> float:
        if snapshot.second_bid is None:
            return snapshot.best_bid.price - snapshot.tick_size
        candidate = snapshot.second_bid.price + snapshot.tick_size
        if candidate >= snapshot.best_bid.price:
            return snapshot.second_bid.price
        return candidate
