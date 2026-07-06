from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Protocol


FEE_SOURCE_PRIORITY = (
    "adapter_api",
    "market_metadata",
    "config_override",
    "config_multiplier",
    "unknown_policy",
)
UNKNOWN_EXPECTED_LOSS_BPS = Decimal("999999")


@dataclass(frozen=True)
class MarketFee:
    """Fee inputs after applying the project fee-source priority.

    Priority:
    1. adapter/API automatic lookup
    2. market metadata
    3. exact config override
    4. config multiplier with an explicit expiry
    5. block trading or use an explicit conservative default when fee is unknown
    """

    entry_fee_bps: Decimal | None
    exit_fee_bps: Decimal | None
    source: str
    slippage_buffer_bps: Decimal = Decimal("0")
    conservative_entry_fee_bps: Decimal = Decimal("0")
    conservative_exit_fee_bps: Decimal = Decimal("0")

    @property
    def known(self) -> bool:
        return self.entry_fee_bps is not None and self.exit_fee_bps is not None


class FeeProvider(Protocol):
    def fee_for_market(self, market: str) -> MarketFee:
        """Return the best-known fee for one market."""


@dataclass(frozen=True)
class MarketCostInput:
    exchange_id: str
    market: str
    live_spread_bps: Decimal
    fee: MarketFee


@dataclass(frozen=True)
class MarketCostResult:
    exchange_id: str
    market: str
    live_spread_bps: Decimal
    entry_fee_bps: Decimal
    exit_fee_bps: Decimal
    slippage_buffer_bps: Decimal
    expected_loss_bps: Decimal
    fee_source: str
    fee_known: bool
    eligible: bool
    reason: str


@dataclass(frozen=True)
class SizingInput:
    exchange_id: str
    market: str
    best_bid: Decimal
    best_ask: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    order_notional_usd: Decimal
    remaining_gross_volume_usd: Decimal
    level_size_fraction: Decimal
    lot_size: Decimal
    min_order_size_usd: Decimal


@dataclass(frozen=True)
class SizingResult:
    exchange_id: str
    market: str
    smaller_top_level_notional_usd: Decimal
    per_side_cap_usd: Decimal
    amount: Decimal
    entry_notional_usd: Decimal
    exit_notional_usd: Decimal
    planned_gross_volume_usd: Decimal
    eligible: bool
    reason: str


def expected_loss_bps(
    *,
    live_spread_bps: Decimal,
    entry_fee_bps: Decimal,
    exit_fee_bps: Decimal,
    slippage_buffer_bps: Decimal = Decimal("0"),
) -> Decimal:
    return live_spread_bps + entry_fee_bps + exit_fee_bps + slippage_buffer_bps


def calculate_market_cost(
    market: MarketCostInput,
    *,
    unknown_fee_policy: str = "block",
) -> MarketCostResult:
    entry_fee, exit_fee, fee_known = _resolve_fee(market.fee, unknown_fee_policy)
    if entry_fee is None or exit_fee is None:
        return MarketCostResult(
            exchange_id=market.exchange_id,
            market=market.market,
            live_spread_bps=market.live_spread_bps,
            entry_fee_bps=Decimal("0"),
            exit_fee_bps=Decimal("0"),
            slippage_buffer_bps=market.fee.slippage_buffer_bps,
            expected_loss_bps=UNKNOWN_EXPECTED_LOSS_BPS,
            fee_source=market.fee.source,
            fee_known=False,
            eligible=False,
            reason="fee_unknown",
        )

    expected = expected_loss_bps(
        live_spread_bps=market.live_spread_bps,
        entry_fee_bps=entry_fee,
        exit_fee_bps=exit_fee,
        slippage_buffer_bps=market.fee.slippage_buffer_bps,
    )
    return MarketCostResult(
        exchange_id=market.exchange_id,
        market=market.market,
        live_spread_bps=market.live_spread_bps,
        entry_fee_bps=entry_fee,
        exit_fee_bps=exit_fee,
        slippage_buffer_bps=market.fee.slippage_buffer_bps,
        expected_loss_bps=expected,
        fee_source=market.fee.source,
        fee_known=fee_known,
        eligible=True,
        reason="cost_ok",
    )


def calculate_sizing(size: SizingInput) -> SizingResult:
    if size.best_bid <= 0 or size.best_ask <= 0:
        return _empty_sizing(size, "invalid_best_bid_or_ask")
    if size.best_bid_size < 0 or size.best_ask_size < 0:
        return _empty_sizing(size, "invalid_top_level_size")
    if size.order_notional_usd <= 0:
        return _empty_sizing(size, "invalid_order_notional")
    if size.remaining_gross_volume_usd <= 0:
        return _empty_sizing(size, "remaining_gross_zero")
    if size.level_size_fraction <= 0 or size.level_size_fraction > 1:
        return _empty_sizing(size, "invalid_level_size_fraction")
    if size.lot_size <= 0:
        return _empty_sizing(size, "missing_lot_size")

    smaller_top_level_notional = min(
        size.best_bid * size.best_bid_size,
        size.best_ask * size.best_ask_size,
    )
    per_side_cap = min(
        size.order_notional_usd,
        size.remaining_gross_volume_usd / Decimal("2"),
        smaller_top_level_notional * size.level_size_fraction,
    )
    if per_side_cap <= 0:
        return SizingResult(
            exchange_id=size.exchange_id,
            market=size.market,
            smaller_top_level_notional_usd=smaller_top_level_notional,
            per_side_cap_usd=per_side_cap,
            amount=Decimal("0"),
            entry_notional_usd=Decimal("0"),
            exit_notional_usd=Decimal("0"),
            planned_gross_volume_usd=Decimal("0"),
            eligible=False,
            reason="per_side_cap_zero",
        )

    amount = round_down_to_lot(per_side_cap / size.best_ask, size.lot_size)
    entry_notional = amount * size.best_ask
    exit_notional = amount * size.best_bid
    planned_gross = entry_notional + exit_notional
    min_side_notional = min(entry_notional, exit_notional)

    eligible = True
    reason = "size_ok"
    if amount <= 0:
        eligible = False
        reason = "quantity_zero"
    elif min_side_notional < size.min_order_size_usd:
        eligible = False
        reason = f"below_min_order_size:{min_side_notional:.4f}<{size.min_order_size_usd}"

    return SizingResult(
        exchange_id=size.exchange_id,
        market=size.market,
        smaller_top_level_notional_usd=smaller_top_level_notional,
        per_side_cap_usd=per_side_cap,
        amount=amount,
        entry_notional_usd=entry_notional,
        exit_notional_usd=exit_notional,
        planned_gross_volume_usd=planned_gross,
        eligible=eligible,
        reason=reason,
    )


def round_down_to_lot(value: Decimal, lot_size: Decimal) -> Decimal:
    if lot_size <= 0:
        return value
    return (value / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size


def _resolve_fee(fee: MarketFee, unknown_fee_policy: str) -> tuple[Decimal | None, Decimal | None, bool]:
    if fee.known:
        return fee.entry_fee_bps, fee.exit_fee_bps, True
    if unknown_fee_policy == "block":
        return None, None, False
    if unknown_fee_policy == "conservative_default":
        return fee.conservative_entry_fee_bps, fee.conservative_exit_fee_bps, False
    raise ValueError("unknown_fee_policy must be block or conservative_default")


def _empty_sizing(size: SizingInput, reason: str) -> SizingResult:
    return SizingResult(
        exchange_id=size.exchange_id,
        market=size.market,
        smaller_top_level_notional_usd=Decimal("0"),
        per_side_cap_usd=Decimal("0"),
        amount=Decimal("0"),
        entry_notional_usd=Decimal("0"),
        exit_notional_usd=Decimal("0"),
        planned_gross_volume_usd=Decimal("0"),
        eligible=False,
        reason=reason,
    )
