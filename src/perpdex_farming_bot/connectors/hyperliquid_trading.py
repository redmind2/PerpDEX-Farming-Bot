from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


VALID_TIFS = {"Alo", "Ioc", "Gtc"}


@dataclass(frozen=True)
class HyperliquidOrderIntent:
    asset_id: int
    coin: str
    is_buy: bool
    price: Decimal
    size: Decimal
    tif: str
    reduce_only: bool
    cloid: str | None = None

    def to_order_wire_preview(self) -> dict[str, object]:
        if self.tif not in VALID_TIFS:
            raise ValueError("Hyperliquid tif must be one of Alo, Ioc, or Gtc")
        if self.size <= 0:
            raise ValueError("Hyperliquid order size must be greater than zero")
        if self.price <= 0:
            raise ValueError("Hyperliquid order price must be greater than zero")
        order: dict[str, object] = {
            "a": self.asset_id,
            "b": self.is_buy,
            "p": _fmt_decimal(self.price),
            "s": _fmt_decimal(self.size),
            "r": self.reduce_only,
            "t": {"limit": {"tif": self.tif}},
        }
        if self.cloid:
            order["c"] = self.cloid
        return order


@dataclass(frozen=True)
class HyperliquidCloseRequestPrebuild:
    close_order: HyperliquidOrderIntent
    pre_signed: bool
    requires_entry_fill_confirmation: bool
    requires_final_position_reconciliation: bool
    reason: str

    def to_action_preview(self) -> dict[str, object]:
        return {
            "type": "order",
            "orders": [self.close_order.to_order_wire_preview()],
            "grouping": "na",
        }


def build_close_request_prebuild(
    *,
    asset_id: int,
    coin: str,
    entry_is_buy: bool,
    filled_size: Decimal,
    close_price: Decimal,
    tif: str = "Ioc",
    cloid: str | None = None,
) -> HyperliquidCloseRequestPrebuild:
    """Build an unsigned reduce-only close request preview.

    This intentionally does not produce a nonce, signature, or request payload
    for /exchange. Hyperliquid nonces and expiresAfter are signing-sensitive, so
    this project should confirm the entry fill and size before any future live
    close signing path is enabled.
    """

    close_intent = HyperliquidOrderIntent(
        asset_id=asset_id,
        coin=coin,
        is_buy=not entry_is_buy,
        price=close_price,
        size=filled_size,
        tif=tif,
        reduce_only=True,
        cloid=cloid,
    )
    return HyperliquidCloseRequestPrebuild(
        close_order=close_intent,
        pre_signed=False,
        requires_entry_fill_confirmation=True,
        requires_final_position_reconciliation=True,
        reason="close_request_prebuild_only_nonce_and_expires_after_not_signed",
    )


def _fmt_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")
