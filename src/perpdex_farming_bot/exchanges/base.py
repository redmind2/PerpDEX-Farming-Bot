from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class AdapterError(RuntimeError):
    """Raised when an exchange-specific adapter cannot safely complete an action."""


@dataclass(frozen=True)
class ExchangePosition:
    exchange_id: str
    market: str
    size: Decimal
    side: str

    @property
    def is_flat(self) -> bool:
        return self.size == 0


@dataclass(frozen=True)
class ExchangeOrderResult:
    exchange_id: str
    market: str
    success: bool
    status: str
    filled_size: Decimal = Decimal("0")
    average_price: Decimal | None = None
    exchange_order_id: str | None = None
    error: str = ""


@dataclass(frozen=True)
class PairedRoundtripResult:
    exchange_id: str
    market: str
    success: bool
    planned_gross_volume_usd: Decimal
    buy_result: ExchangeOrderResult | None = None
    sell_result: ExchangeOrderResult | None = None
    residual_position: ExchangePosition | None = None
    status: str = ""


class ExchangeAdapter(Protocol):
    exchange_id: str

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        """Return non-zero positions for the configured account."""

    def execute_paired_notional_roundtrip(
        self,
        *,
        market: str,
        instrument_id: int,
        buy_price: Decimal,
        sell_price: Decimal,
        buy_size: Decimal,
        sell_size: Decimal,
        planned_gross_volume_usd: Decimal,
        first_side: str = "BUY",
        second_side: str = "SELL",
        roundtrip_mode: str = "confirmed",
    ) -> PairedRoundtripResult:
        """Place one paired buy/sell roundtrip through the exchange-specific API."""

    def close_position_reduce_only(
        self,
        *,
        market: str,
        instrument_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> ExchangeOrderResult:
        """Close an existing position through a reduce-only exchange-specific order."""
