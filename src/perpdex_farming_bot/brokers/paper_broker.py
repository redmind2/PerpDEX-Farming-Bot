from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from perpdex_farming_bot.models import MarketSnapshot, OrderIntent, OrderType, Side


@dataclass(frozen=True)
class PaperFill:
    market: str
    side: Side
    quantity: float
    fill_price: float
    notional_usd: float
    timestamp: datetime
    source_intent: OrderIntent


class PaperBroker:
    """Simulates fills without calling any exchange API."""

    def execute(self, snapshot: MarketSnapshot, intents: tuple[OrderIntent, ...], now: datetime) -> tuple[PaperFill, ...]:
        fills: list[PaperFill] = []
        for intent in intents:
            fill_price = self._paper_fill_price(snapshot, intent)
            fills.append(
                PaperFill(
                    market=intent.market,
                    side=intent.side,
                    quantity=intent.quantity,
                    fill_price=fill_price,
                    notional_usd=intent.quantity * fill_price,
                    timestamp=now,
                    source_intent=intent,
                )
            )
        return tuple(fills)

    def _paper_fill_price(self, snapshot: MarketSnapshot, intent: OrderIntent) -> float:
        if intent.order_type is OrderType.MARKET:
            return snapshot.best_ask.price if intent.side is Side.BUY else snapshot.best_bid.price
        if intent.price is None:
            return intent.reference_price
        return intent.price
