from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping

from perpdex_farming_bot.core.execution_event import ExecutionEvent


class ExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"


class RoundtripMode(str, Enum):
    CONFIRMED = "confirmed"
    FAST_REDUCE_ONLY = "fast_reduce_only"
    NETTING = "netting"


class UnknownFeePolicy(str, Enum):
    BLOCK = "block"
    CONSERVATIVE_DEFAULT = "conservative_default"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderKind(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"


LedgerEvent = ExecutionEvent


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    exchange_id: str
    market: str
    side: OrderSide
    order_type: OrderKind
    quantity: Decimal
    price: Decimal | None = None
    reference_price: Decimal | None = None
    time_in_force: str | None = None
    reduce_only: bool = False
    post_only: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def notional_usd(self) -> Decimal | None:
        price = self.price if self.price is not None else self.reference_price
        if price is None:
            return None
        return abs(self.quantity * price)


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    strategy_id: str
    account_alias: str
    exchange_id: str
    market: str
    mode: ExecutionMode
    orders: tuple[OrderIntent, ...]
    roundtrip_mode: RoundtripMode | None = None
    max_gross_notional_usd: Decimal | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def planned_gross_notional_usd(self) -> Decimal | None:
        total = Decimal("0")
        for order in self.orders:
            notional = order.notional_usd
            if notional is None:
                return None
            total += notional
        return total


@dataclass(frozen=True)
class FeeQuote:
    exchange_id: str
    account_alias: str
    market: str
    source: str
    fee_known: bool
    maker_fee_bps: Decimal | None = None
    taker_fee_bps: Decimal | None = None
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    slippage_buffer_bps: Decimal = Decimal("0")
    fee_multiplier: Decimal | None = None
    fee_multiplier_expires_at: datetime | None = None
    unknown_fee_policy: UnknownFeePolicy = UnknownFeePolicy.BLOCK
    blocked: bool = False
    block_reason: str | None = None

    @property
    def can_estimate_cost(self) -> bool:
        return self.entry_fee_bps is not None and self.exit_fee_bps is not None


@dataclass(frozen=True)
class ExecutionCostQuote:
    exchange_id: str
    market: str
    fee_quote: FeeQuote
    expected_loss_bps: Decimal | None
    estimated_fee_usd: Decimal | None
    eligible: bool
    reason: str


@dataclass(frozen=True)
class AccountPolicy:
    account_alias: str
    allowed_modes: tuple[ExecutionMode, ...] = (ExecutionMode.DRY_RUN, ExecutionMode.PAPER)
    allow_live: bool = False
    require_fee_quote: bool = True
    unknown_fee_policy: UnknownFeePolicy = UnknownFeePolicy.BLOCK
    max_order_notional_usd: Decimal | None = None
    max_gross_notional_usd: Decimal | None = None
    kill_switch_required: bool = True

    def allows_mode(self, mode: ExecutionMode) -> bool:
        if mode is ExecutionMode.LIVE and not self.allow_live:
            return False
        return mode in self.allowed_modes


@dataclass(frozen=True)
class ExecutionRequest:
    request_id: str
    trade_intent: TradeIntent
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class OrderExecutionResult:
    order_intent_id: str
    accepted: bool
    status: str
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    error: str | None = None
    live_order_submitted: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    request_id: str
    mode: ExecutionMode
    account_alias: str
    exchange_id: str
    market: str
    accepted: bool
    status: str
    reason: str
    order_results: tuple[OrderExecutionResult, ...] = ()
    fee_quote: FeeQuote | None = None
    cost_quote: ExecutionCostQuote | None = None
    ledger_event: LedgerEvent | None = None
    live_order_submitted: bool = False


@dataclass(frozen=True)
class RoundtripResult:
    request_id: str
    mode: ExecutionMode
    roundtrip_mode: RoundtripMode
    success: bool
    status: str
    entry_result: OrderExecutionResult | None = None
    exit_result: OrderExecutionResult | None = None
    filled_gross_volume_usd: Decimal = Decimal("0")
    residual_position_size: Decimal | None = None
