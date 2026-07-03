from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"


class TimeInForce(str, Enum):
    IOC = "ioc"
    GTC = "gtc"


@dataclass(frozen=True)
class QuoteLevel:
    price: float
    size: float

    @property
    def notional_usd(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class TradePrint:
    side: Side
    price: float
    size: float
    timestamp: datetime


@dataclass(frozen=True)
class MarketSnapshot:
    exchange_id: str
    market: str
    best_bid: QuoteLevel
    best_ask: QuoteLevel
    second_bid: QuoteLevel | None
    second_ask: QuoteLevel | None
    average_spread_bps: float
    tick_size: float
    timestamp: datetime
    recent_trades: tuple[TradePrint, ...] = ()

    @property
    def mid_price(self) -> float:
        return (self.best_bid.price + self.best_ask.price) / 2

    @property
    def spread(self) -> float:
        return self.best_ask.price - self.best_bid.price

    @property
    def spread_bps(self) -> float:
        return (self.spread / self.mid_price) * 10_000

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()


@dataclass(frozen=True)
class OrderIntent:
    strategy_name: str
    exchange_id: str
    account_id: str
    wallet_id: str
    market: str
    side: Side
    order_type: OrderType
    quantity: float
    price: float | None = None
    time_in_force: TimeInForce | None = None
    reduce_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def reference_price(self) -> float:
        if self.price is None:
            return float(self.metadata["reference_price"])
        return self.price

    @property
    def notional_usd(self) -> float:
        return self.quantity * self.reference_price


@dataclass(frozen=True)
class StrategyDecision:
    strategy_name: str
    should_wait: bool
    reason: str
    intents: tuple[OrderIntent, ...] = ()
