from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
import re
import time
from typing import Callable, Generic, Mapping, Protocol, TypeVar

from perpdex_farming_bot.core.execution_event import ExecutionEvent
from perpdex_farming_bot.core.execution_adapter import GatewayOrderAdapter
from perpdex_farming_bot.core.execution_models import (
    AccountPolicy,
    ExecutionCostQuote,
    ExecutionMode,
    ExecutionPreflightRequest,
    ExecutionPreflightResult,
    ExecutionRequest,
    ExecutionResult,
    FeeQuote,
    OrderExecutionResult,
    ReadOnlyCheckResult,
    TradeIntent,
)
from perpdex_farming_bot.core.fee_provider import CommonFeeProvider, FeeRequest
from perpdex_farming_bot.exchanges.base import ExchangeOrderResult, PairedRoundtripResult


class KillSwitch(Protocol):
    def is_enabled(self) -> bool:
        """Return True when execution should be blocked."""


LedgerSink = Callable[[ExecutionEvent], None]
T = TypeVar("T")


@dataclass(frozen=True)
class StaticKillSwitch:
    enabled: bool = False

    def is_enabled(self) -> bool:
        return self.enabled


@dataclass(frozen=True)
class LiveAdapterActionResult(Generic[T]):
    accepted: bool
    status: str
    execution_result: ExecutionResult
    payload: T | None = None
    error: str | None = None


@dataclass
class ExecutionGateway:
    account_policies: Mapping[str, AccountPolicy]
    fee_provider: CommonFeeProvider | None = None
    adapters: Mapping[str, GatewayOrderAdapter] = field(default_factory=dict)
    kill_switch: KillSwitch | None = None
    ledger_sink: LedgerSink | None = None
    live_orders_enabled: bool = False

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return self._evaluate_request(request, allow_live_without_submission=False)

    def execute_paired_roundtrip(
        self,
        request: ExecutionRequest,
        *,
        instrument_id: int = 0,
        first_side: str | None = None,
        second_side: str | None = None,
    ) -> PairedRoundtripResult:
        execution_result = self._evaluate_request(request, allow_live_without_submission=False)
        intent = request.trade_intent
        if not execution_result.accepted:
            return PairedRoundtripResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                planned_gross_volume_usd=intent.planned_gross_notional_usd or Decimal("0"),
                status=execution_result.status,
            )
        if intent.mode is not ExecutionMode.LIVE:
            return PairedRoundtripResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                planned_gross_volume_usd=intent.planned_gross_notional_usd or Decimal("0"),
                status="gateway_roundtrip_requires_live_mode",
            )

        roundtrip = _roundtrip_submit_args(intent, instrument_id=instrument_id, first_side=first_side, second_side=second_side)
        if isinstance(roundtrip, str):
            return PairedRoundtripResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                planned_gross_volume_usd=intent.planned_gross_notional_usd or Decimal("0"),
                status=roundtrip,
            )

        adapter = self._registered_adapter(intent.exchange_id)
        method = getattr(adapter, "execute_paired_notional_roundtrip", None)
        if method is None:
            return PairedRoundtripResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                planned_gross_volume_usd=roundtrip["planned_gross_volume_usd"],
                status="gateway_adapter_roundtrip_submit_missing",
            )
        try:
            return method(**roundtrip)
        except Exception as exc:
            return PairedRoundtripResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                planned_gross_volume_usd=roundtrip["planned_gross_volume_usd"],
                status=f"gateway_adapter_roundtrip_exception:{exc.__class__.__name__}",
            )

    def execute_reduce_only_close(
        self,
        request: ExecutionRequest,
        *,
        instrument_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> ExchangeOrderResult:
        execution_result = self._evaluate_request(request, allow_live_without_submission=False)
        intent = request.trade_intent
        if not execution_result.accepted:
            return ExchangeOrderResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                status=execution_result.status,
            )
        if intent.mode is not ExecutionMode.LIVE:
            return ExchangeOrderResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                status="gateway_close_requires_live_mode",
            )
        adapter = self._registered_adapter(intent.exchange_id)
        method = getattr(adapter, "close_position_reduce_only", None)
        if method is None:
            return ExchangeOrderResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                status="gateway_adapter_reduce_only_close_missing",
            )
        try:
            return method(
                market=intent.market,
                instrument_id=instrument_id,
                side=side,
                price=price,
                size=size,
            )
        except Exception as exc:
            return ExchangeOrderResult(
                exchange_id=intent.exchange_id,
                market=intent.market,
                success=False,
                status=f"gateway_adapter_reduce_only_close_exception:{exc.__class__.__name__}",
            )

    def run_live_adapter_action(
        self,
        request: ExecutionRequest,
        action: Callable[[], T],
    ) -> LiveAdapterActionResult[T]:
        execution_result = self._evaluate_request(request, allow_live_without_submission=False)
        if not execution_result.accepted:
            return LiveAdapterActionResult(False, execution_result.status, execution_result)
        if request.trade_intent.mode is not ExecutionMode.LIVE:
            return LiveAdapterActionResult(False, "gateway_live_action_requires_live_mode", execution_result)
        started = time.perf_counter()
        try:
            payload = action()
        except Exception as exc:
            action_result = self._action_execution_result(
                request,
                execution_result,
                status=f"gateway_live_action_exception:{exc.__class__.__name__}",
                adapter_submit_elapsed_ms=_elapsed_ms(started),
                error_reason=_safe_error(exc),
                live_order_submitted=False,
            )
            return LiveAdapterActionResult(
                False,
                f"gateway_live_action_exception:{exc.__class__.__name__}",
                action_result,
                error=_safe_error(exc),
            )
        action_result = self._action_execution_result(
            request,
            execution_result,
            status="gateway_live_action_executed",
            adapter_submit_elapsed_ms=_elapsed_ms(started),
            live_order_submitted=True,
        )
        return LiveAdapterActionResult(True, "gateway_live_action_executed", action_result, payload=payload)

    async def run_live_adapter_action_async(
        self,
        request: ExecutionRequest,
        action: Callable[[], object],
    ) -> LiveAdapterActionResult[object]:
        execution_result = self._evaluate_request(request, allow_live_without_submission=False)
        if not execution_result.accepted:
            return LiveAdapterActionResult(False, execution_result.status, execution_result)
        if request.trade_intent.mode is not ExecutionMode.LIVE:
            return LiveAdapterActionResult(False, "gateway_live_action_requires_live_mode", execution_result)
        started = time.perf_counter()
        try:
            payload = await action()
        except Exception as exc:
            action_result = self._action_execution_result(
                request,
                execution_result,
                status=f"gateway_live_action_exception:{exc.__class__.__name__}",
                adapter_submit_elapsed_ms=_elapsed_ms(started),
                error_reason=_safe_error(exc),
                live_order_submitted=False,
            )
            return LiveAdapterActionResult(
                False,
                f"gateway_live_action_exception:{exc.__class__.__name__}",
                action_result,
                error=_safe_error(exc),
            )
        action_result = self._action_execution_result(
            request,
            execution_result,
            status="gateway_live_action_executed",
            adapter_submit_elapsed_ms=_elapsed_ms(started),
            live_order_submitted=True,
        )
        return LiveAdapterActionResult(True, "gateway_live_action_executed", action_result, payload=payload)

    def record_observation(
        self,
        request: ExecutionRequest,
        *,
        status: str,
        metadata: Mapping[str, object] | None = None,
        error_reason: str | None = None,
        live_order_submitted: bool = True,
    ) -> ExecutionEvent:
        policy = self.account_policies.get(request.trade_intent.account_alias)
        fee_quote = self._quote_fee(request.trade_intent, policy) if policy is not None else None
        cost_quote = self._build_cost_quote(request.trade_intent, fee_quote)
        event = self._ledger_event(
            request,
            status,
            fee_quote,
            cost_quote,
            metadata=metadata,
            error_reason=error_reason,
        )
        self._emit_ledger(event)
        del live_order_submitted
        return event

    def _action_execution_result(
        self,
        request: ExecutionRequest,
        execution_result: ExecutionResult,
        *,
        status: str,
        adapter_submit_elapsed_ms: Decimal,
        error_reason: str | None = None,
        live_order_submitted: bool,
    ) -> ExecutionResult:
        event = self._ledger_event(
            request,
            status,
            execution_result.fee_quote,
            execution_result.cost_quote,
            metadata={"adapter_submit_elapsed_ms": adapter_submit_elapsed_ms},
            error_reason=error_reason,
        )
        self._emit_ledger(event)
        return replace(
            execution_result,
            status=status,
            reason="gateway live adapter action completed" if error_reason is None else error_reason,
            ledger_event=event,
            live_order_submitted=live_order_submitted,
        )

    def _evaluate_request(
        self,
        request: ExecutionRequest,
        *,
        allow_live_without_submission: bool,
    ) -> ExecutionResult:
        intent = request.trade_intent
        policy = self.account_policies.get(intent.account_alias)
        if policy is None:
            return self._reject(request, "account_policy_missing", "account policy was not configured")

        try:
            self._registered_adapter(intent.exchange_id)
        except ValueError as exc:
            return self._reject(request, exc.args[0], exc.args[1])

        if not policy.allows_mode(intent.mode):
            return self._reject(request, "mode_blocked_by_account_policy", f"mode {intent.mode.value} is not allowed")

        if policy.kill_switch_required and self.kill_switch is None and intent.mode is not ExecutionMode.DRY_RUN:
            return self._reject(request, "kill_switch_missing", "account policy requires a kill switch")

        if self._kill_switch_enabled() and intent.mode is not ExecutionMode.DRY_RUN:
            return self._reject(request, "kill_switch_enabled", "kill switch is enabled")

        if intent.mode is ExecutionMode.LIVE and not self.live_orders_enabled and not allow_live_without_submission:
            return self._reject(request, "live_mode_not_connected", "live orders are not wired in this skeleton")

        limit_error = self._validate_notional_limits(intent, policy)
        if limit_error is not None:
            return self._reject(request, limit_error, limit_error)

        fee_quote = self._quote_fee(intent, policy)
        if fee_quote is None and policy.require_fee_quote:
            return self._reject(request, "fee_provider_missing", "fee provider is required by account policy")
        if fee_quote is not None and fee_quote.blocked and policy.require_fee_quote:
            return self._reject(request, fee_quote.block_reason or "fee_quote_blocked", "fee quote blocked execution", fee_quote)

        cost_quote = self._build_cost_quote(intent, fee_quote)
        order_results = tuple(
            OrderExecutionResult(
                order_intent_id=order.intent_id,
                accepted=True,
                status=f"{intent.mode.value}_planned_no_exchange_call",
                live_order_submitted=False,
            )
            for order in intent.orders
        )
        status = _accepted_status(intent.mode, allow_live_without_submission=allow_live_without_submission)
        result = ExecutionResult(
            request_id=request.request_id,
            mode=intent.mode,
            account_alias=intent.account_alias,
            exchange_id=intent.exchange_id,
            market=intent.market,
            accepted=True,
            status=status,
            reason="gateway skeleton accepted without live exchange submission",
            order_results=order_results,
            fee_quote=fee_quote,
            cost_quote=cost_quote,
            ledger_event=self._ledger_event(request, status, fee_quote, cost_quote),
            live_order_submitted=False,
        )
        self._emit_ledger(result.ledger_event)
        return result

    def preflight(self, request: ExecutionPreflightRequest) -> ExecutionPreflightResult:
        execution_result = self._evaluate_request(
            ExecutionRequest(
                request_id=request.request_id,
                trade_intent=request.trade_intent,
            ),
            allow_live_without_submission=request.allow_live_without_submission,
        )
        if not execution_result.accepted:
            return ExecutionPreflightResult(
                request_id=request.request_id,
                mode=request.trade_intent.mode,
                account_alias=request.trade_intent.account_alias,
                exchange_id=request.trade_intent.exchange_id,
                market=request.trade_intent.market,
                ready=False,
                status=execution_result.status,
                reason=execution_result.reason,
                execution_result=execution_result,
                live_order_submitted=False,
            )

        checks: list[ReadOnlyCheckResult] = []
        if not request.include_read_only:
            checks.append(ReadOnlyCheckResult("read_only", False, True, "read_only_checks_skipped"))
            return ExecutionPreflightResult(
                request_id=request.request_id,
                mode=request.trade_intent.mode,
                account_alias=request.trade_intent.account_alias,
                exchange_id=request.trade_intent.exchange_id,
                market=request.trade_intent.market,
                ready=True,
                status="preflight_ready_no_read_only",
                reason="policy, adapter, and fee checks passed; private read-only checks were not requested",
                execution_result=execution_result,
                checks=tuple(checks),
                live_order_submitted=False,
            )

        adapter = self._registered_adapter(request.trade_intent.exchange_id)
        if request.check_positions:
            checks.append(self._check_positions(adapter, request.trade_intent.account_alias))
        if request.check_open_orders:
            checks.append(self._check_open_orders(adapter, request.trade_intent.account_alias, request.trade_intent.market))

        failed = tuple(check for check in checks if not check.ok)
        ready = not failed
        status = "preflight_ready" if ready else "preflight_blocked_by_read_only_check"
        reason = "read-only checks passed" if ready else ",".join(check.status for check in failed)
        return ExecutionPreflightResult(
            request_id=request.request_id,
            mode=request.trade_intent.mode,
            account_alias=request.trade_intent.account_alias,
            exchange_id=request.trade_intent.exchange_id,
            market=request.trade_intent.market,
            ready=ready,
            status=status,
            reason=reason,
            execution_result=execution_result,
            checks=tuple(checks),
            live_order_submitted=False,
        )

    def _registered_adapter(self, exchange_id: str) -> GatewayOrderAdapter:
        adapter = self.adapters.get(exchange_id)
        if adapter is None:
            raise ValueError("exchange_adapter_not_registered", "exchange adapter was not registered")
        if adapter.exchange_id != exchange_id:
            raise ValueError("exchange_adapter_id_mismatch", "registered adapter exchange_id mismatched request")
        return adapter

    def _check_positions(self, adapter: GatewayOrderAdapter, account_alias: str) -> ReadOnlyCheckResult:
        try:
            positions = adapter.list_positions(account_alias)
        except Exception as exc:
            return ReadOnlyCheckResult("positions", True, False, "positions_read_failed", error=_safe_error(exc))
        nonzero_count = sum(0 if position.is_flat else 1 for position in positions)
        if nonzero_count:
            return ReadOnlyCheckResult("positions", True, False, "positions_nonzero", count=nonzero_count)
        return ReadOnlyCheckResult("positions", True, True, "positions_flat", count=0)

    def _check_open_orders(self, adapter: GatewayOrderAdapter, account_alias: str, market: str) -> ReadOnlyCheckResult:
        try:
            open_orders = adapter.list_open_orders(account_alias, market)
        except Exception as exc:
            return ReadOnlyCheckResult("open_orders", True, False, "open_orders_read_failed", error=_safe_error(exc))
        order_count = len(open_orders)
        if order_count:
            return ReadOnlyCheckResult("open_orders", True, False, "open_orders_detected", count=order_count)
        return ReadOnlyCheckResult("open_orders", True, True, "open_orders_empty", count=0)

    def _quote_fee(self, intent: TradeIntent, policy: AccountPolicy) -> FeeQuote | None:
        if self.fee_provider is None:
            return None
        return self.fee_provider.quote_fee(
            FeeRequest(
                exchange_id=intent.exchange_id,
                account_alias=intent.account_alias,
                market=intent.market,
                unknown_fee_policy=policy.unknown_fee_policy,
            )
        )

    def _build_cost_quote(self, intent: TradeIntent, fee_quote: FeeQuote | None) -> ExecutionCostQuote | None:
        if fee_quote is None:
            return None
        expected_loss_bps = None
        if fee_quote.can_estimate_cost:
            expected_loss_bps = (
                fee_quote.entry_fee_bps
                + fee_quote.exit_fee_bps
                + fee_quote.slippage_buffer_bps
            )
        return ExecutionCostQuote(
            exchange_id=intent.exchange_id,
            market=intent.market,
            fee_quote=fee_quote,
            expected_loss_bps=expected_loss_bps,
            estimated_fee_usd=_estimate_fee_usd(intent, fee_quote),
            eligible=not fee_quote.blocked,
            reason="cost_estimated" if expected_loss_bps is not None else "cost_not_estimated",
        )

    def _validate_notional_limits(self, intent: TradeIntent, policy: AccountPolicy) -> str | None:
        if policy.max_order_notional_usd is not None:
            for order in intent.orders:
                notional = order.notional_usd
                if notional is not None and notional > policy.max_order_notional_usd:
                    return "order_notional_exceeds_account_policy"

        planned_gross = intent.planned_gross_notional_usd
        max_gross = intent.max_gross_notional_usd
        if policy.max_gross_notional_usd is not None:
            max_gross = policy.max_gross_notional_usd if max_gross is None else min(max_gross, policy.max_gross_notional_usd)
        if planned_gross is not None and max_gross is not None and planned_gross > max_gross:
            return "gross_notional_exceeds_account_policy"
        return None

    def _reject(
        self,
        request: ExecutionRequest,
        status: str,
        reason: str,
        fee_quote: FeeQuote | None = None,
    ) -> ExecutionResult:
        cost_quote = self._build_cost_quote(request.trade_intent, fee_quote)
        result = ExecutionResult(
            request_id=request.request_id,
            mode=request.trade_intent.mode,
            account_alias=request.trade_intent.account_alias,
            exchange_id=request.trade_intent.exchange_id,
            market=request.trade_intent.market,
            accepted=False,
            status=status,
            reason=reason,
            fee_quote=fee_quote,
            cost_quote=cost_quote,
            ledger_event=self._ledger_event(request, status, fee_quote, cost_quote, error_reason=reason),
            live_order_submitted=False,
        )
        self._emit_ledger(result.ledger_event)
        return result

    def _ledger_event(
        self,
        request: ExecutionRequest,
        status: str,
        fee_quote: FeeQuote | None,
        cost_quote: ExecutionCostQuote | None,
        *,
        metadata: Mapping[str, object] | None = None,
        error_reason: str | None = None,
    ) -> ExecutionEvent:
        intent = request.trade_intent
        event_metadata = _event_metadata(intent, metadata)
        return ExecutionEvent(
            exchange=intent.exchange_id,
            cycle_id=request.request_id,
            environment=intent.mode.value,
            status=status,
            account_label=intent.account_alias,
            wallet_label=_metadata_str(event_metadata, "wallet_label"),
            market=intent.market,
            fee_level=_metadata_str(event_metadata, "fee_level"),
            maker_fee_bps=fee_quote.maker_fee_bps if fee_quote is not None else None,
            taker_fee_bps=fee_quote.taker_fee_bps if fee_quote is not None else None,
            entry_fee_bps=fee_quote.entry_fee_bps if fee_quote is not None else None,
            exit_fee_bps=fee_quote.exit_fee_bps if fee_quote is not None else None,
            fee_source=fee_quote.source if fee_quote is not None else None,
            fee_multiplier=(
                fee_quote.fee_multiplier
                if fee_quote is not None and fee_quote.fee_multiplier is not None
                else _metadata_decimal(event_metadata, "fee_multiplier")
            ),
            fee_multiplier_expires_at=(
                fee_quote.fee_multiplier_expires_at.isoformat()
                if fee_quote is not None and fee_quote.fee_multiplier_expires_at is not None
                else _metadata_str(event_metadata, "fee_multiplier_expires_at")
            ),
            live_spread_bps=_metadata_decimal(event_metadata, "live_spread_bps", "spread_bps"),
            expected_loss_bps=cost_quote.expected_loss_bps if cost_quote is not None else None,
            planned_gross_volume_usd=(
                _metadata_decimal(event_metadata, "planned_gross_volume_usd")
                or intent.planned_gross_notional_usd
            ),
            filled_gross_volume_usd=_metadata_decimal(event_metadata, "filled_gross_volume_usd"),
            estimated_fee_usd=cost_quote.estimated_fee_usd if cost_quote is not None else None,
            estimated_loss_usd=_metadata_decimal(event_metadata, "estimated_loss_usd"),
            realized_pnl_usd=_metadata_decimal(event_metadata, "realized_pnl_usd"),
            points_estimate=_metadata_decimal(event_metadata, "points_estimate"),
            start_position_count=_metadata_int(event_metadata, "start_position_count"),
            final_position_count=_metadata_int(event_metadata, "final_position_count"),
            start_open_order_count=_metadata_int(event_metadata, "start_open_order_count"),
            final_open_order_count=_metadata_int(event_metadata, "final_open_order_count"),
            final_all_flat=_metadata_bool(event_metadata, "final_all_flat"),
            plan_latency_ms=_metadata_decimal(event_metadata, "plan_latency_ms"),
            entry_sign_latency_ms=_metadata_decimal(event_metadata, "entry_sign_latency_ms"),
            close_sign_latency_ms=_metadata_decimal(event_metadata, "close_sign_latency_ms"),
            close_prebuild_sign_latency_ms=_metadata_decimal(event_metadata, "close_prebuild_sign_latency_ms"),
            entry_post_latency_ms=_metadata_decimal(event_metadata, "entry_post_latency_ms"),
            close_post_latency_ms=_metadata_decimal(event_metadata, "close_post_latency_ms"),
            entry_to_close_submit_gap_ms=_metadata_decimal(event_metadata, "entry_to_close_submit_gap_ms"),
            cycle_total_latency_ms=_metadata_decimal(event_metadata, "cycle_total_latency_ms"),
            adapter_submit_elapsed_ms=_metadata_decimal(event_metadata, "adapter_submit_elapsed_ms"),
            matched_trade_count=_metadata_int(event_metadata, "matched_trade_count"),
            matched_trade_gross_usd=_metadata_decimal(event_metadata, "matched_trade_gross_usd"),
            matched_trade_fee_usd_estimate=_metadata_decimal(event_metadata, "matched_trade_fee_usd_estimate"),
            order_ids=_metadata_order_ids(event_metadata, "order_ids"),
            error_reason=error_reason,
        )

    def _emit_ledger(self, event: ExecutionEvent | None) -> None:
        if event is not None and self.ledger_sink is not None:
            self.ledger_sink(event)

    def _kill_switch_enabled(self) -> bool:
        return self.kill_switch is not None and self.kill_switch.is_enabled()


def _estimate_fee_usd(intent: TradeIntent, fee_quote: FeeQuote) -> Decimal | None:
    if not fee_quote.can_estimate_cost:
        return None
    fees = [fee_quote.entry_fee_bps, fee_quote.exit_fee_bps]
    total = Decimal("0")
    for index, order in enumerate(intent.orders):
        notional = order.notional_usd
        if notional is None:
            return None
        fee_bps = fees[index] if index < len(fees) else fee_quote.exit_fee_bps
        if fee_bps is None:
            return None
        total += notional * fee_bps / Decimal("10000")
    return total


def _accepted_status(mode: ExecutionMode, *, allow_live_without_submission: bool) -> str:
    if mode is ExecutionMode.DRY_RUN:
        return "dry_run_accepted"
    if mode is ExecutionMode.PAPER:
        return "paper_accepted_no_exchange_call"
    if mode is ExecutionMode.LIVE and allow_live_without_submission:
        return "live_preflight_accepted_no_exchange_call"
    return "live_submit_authorized"


def _roundtrip_submit_args(
    intent: TradeIntent,
    *,
    instrument_id: int,
    first_side: str | None,
    second_side: str | None,
) -> dict[str, object] | str:
    if len(intent.orders) != 2:
        return "gateway_roundtrip_requires_two_orders"
    buy_orders = [order for order in intent.orders if order.side.value == "buy"]
    sell_orders = [order for order in intent.orders if order.side.value == "sell"]
    if len(buy_orders) != 1 or len(sell_orders) != 1:
        return "gateway_roundtrip_requires_one_buy_one_sell"

    buy = buy_orders[0]
    sell = sell_orders[0]
    buy_price = buy.price if buy.price is not None else buy.reference_price
    sell_price = sell.price if sell.price is not None else sell.reference_price
    if buy_price is None or sell_price is None:
        return "gateway_roundtrip_requires_prices"
    planned_gross = intent.planned_gross_notional_usd
    if planned_gross is None:
        return "gateway_roundtrip_requires_notional"
    mode = intent.roundtrip_mode.value if intent.roundtrip_mode is not None else "confirmed"
    ordered_first = first_side or intent.orders[0].side.value.upper()
    ordered_second = second_side or intent.orders[1].side.value.upper()
    return {
        "market": intent.market,
        "instrument_id": instrument_id,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "buy_size": buy.quantity,
        "sell_size": sell.quantity,
        "planned_gross_volume_usd": planned_gross,
        "first_side": ordered_first,
        "second_side": ordered_second,
        "roundtrip_mode": mode,
    }


def _safe_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    text = re.sub(r"0x[a-fA-F0-9]{40,}", "0x[redacted]", text)
    return text[:240]


def _elapsed_ms(started: float) -> Decimal:
    return Decimal(str((time.perf_counter() - started) * 1000))


def _event_metadata(intent: TradeIntent, metadata: Mapping[str, object] | None) -> dict[str, object]:
    values = dict(intent.metadata)
    if metadata:
        values.update(metadata)
    return values


def _metadata_decimal(metadata: Mapping[str, object], *keys: str) -> Decimal | None:
    for key in keys:
        value = metadata.get(key)
        if value in (None, "") or isinstance(value, bool):
            continue
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None


def _metadata_int(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _metadata_bool(metadata: Mapping[str, object], key: str) -> bool | None:
    value = metadata.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _metadata_order_ids(metadata: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = metadata.get(key)
    if value in (None, ""):
        return ()
    if isinstance(value, (tuple, list, set)):
        return tuple(str(item) for item in value if item not in (None, ""))
    return tuple(item.strip() for item in str(value).split(",") if item.strip())
