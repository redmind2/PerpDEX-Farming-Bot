from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Protocol

from perpdex_farming_bot.core.execution_models import ExecutionMode, OrderExecutionResult, OrderIntent


@dataclass(frozen=True)
class TopOfBook:
    exchange_id: str
    market: str
    best_bid_price: Decimal
    best_bid_size: Decimal
    best_ask_price: Decimal
    best_ask_size: Decimal
    timestamp_utc: datetime | None = None


@dataclass(frozen=True)
class PositionSnapshot:
    exchange_id: str
    account_alias: str
    market: str
    size: Decimal
    side: str
    entry_price: Decimal | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_flat(self) -> bool:
        return self.size == 0


@dataclass(frozen=True)
class OpenOrderSnapshot:
    exchange_id: str
    account_alias: str
    market: str
    order_id: str
    side: str
    price: Decimal | None
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    reduce_only: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeFillSnapshot:
    exchange_id: str
    account_alias: str
    market: str
    trade_id: str
    order_id: str | None
    side: str
    price: Decimal
    quantity: Decimal
    fee_usd: Decimal | None = None
    timestamp_utc: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


class ReadOnlyExecutionAdapter(Protocol):
    exchange_id: str

    def get_top_of_book(self, market: str) -> TopOfBook:
        """Return current best bid/ask without private side effects."""

    def list_positions(self, account_alias: str) -> tuple[PositionSnapshot, ...]:
        """Return current account positions using private read-only access."""

    def list_open_orders(self, account_alias: str, market: str | None = None) -> tuple[OpenOrderSnapshot, ...]:
        """Return open orders using private read-only access."""

    def list_order_history(self, account_alias: str, market: str | None = None) -> tuple[OpenOrderSnapshot, ...]:
        """Return recent order history using private read-only access."""

    def list_trade_fills(self, account_alias: str, market: str | None = None) -> tuple[TradeFillSnapshot, ...]:
        """Return recent fills/trades using private read-only access."""


class GatewayOrderAdapter(ReadOnlyExecutionAdapter, Protocol):
    def submit_order(self, order: OrderIntent, *, mode: ExecutionMode) -> OrderExecutionResult:
        """Gateway-only order submission hook. The skeleton does not call this for live mode."""
