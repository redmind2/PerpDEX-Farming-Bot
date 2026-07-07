from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping, Protocol

from perpdex_farming_bot.core.execution_event import ExecutionEvent
from perpdex_farming_bot.core.execution_adapter import GatewayOrderAdapter
from perpdex_farming_bot.core.execution_models import (
    AccountPolicy,
    ExecutionCostQuote,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    FeeQuote,
    OrderExecutionResult,
    TradeIntent,
)
from perpdex_farming_bot.core.fee_provider import CommonFeeProvider, FeeRequest


class KillSwitch(Protocol):
    def is_enabled(self) -> bool:
        """Return True when execution should be blocked."""


LedgerSink = Callable[[ExecutionEvent], None]


@dataclass(frozen=True)
class StaticKillSwitch:
    enabled: bool = False

    def is_enabled(self) -> bool:
        return self.enabled


@dataclass
class ExecutionGateway:
    account_policies: Mapping[str, AccountPolicy]
    fee_provider: CommonFeeProvider | None = None
    adapters: Mapping[str, GatewayOrderAdapter] = field(default_factory=dict)
    kill_switch: KillSwitch | None = None
    ledger_sink: LedgerSink | None = None
    live_orders_enabled: bool = False

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        intent = request.trade_intent
        policy = self.account_policies.get(intent.account_alias)
        if policy is None:
            return self._reject(request, "account_policy_missing", "account policy was not configured")

        adapter = self.adapters.get(intent.exchange_id)
        if adapter is None:
            return self._reject(request, "exchange_adapter_not_registered", "exchange adapter was not registered")
        if adapter.exchange_id != intent.exchange_id:
            return self._reject(request, "exchange_adapter_id_mismatch", "registered adapter exchange_id mismatched request")

        if not policy.allows_mode(intent.mode):
            return self._reject(request, "mode_blocked_by_account_policy", f"mode {intent.mode.value} is not allowed")

        if policy.kill_switch_required and self.kill_switch is None and intent.mode is not ExecutionMode.DRY_RUN:
            return self._reject(request, "kill_switch_missing", "account policy requires a kill switch")

        if self._kill_switch_enabled() and intent.mode is not ExecutionMode.DRY_RUN:
            return self._reject(request, "kill_switch_enabled", "kill switch is enabled")

        if intent.mode is ExecutionMode.LIVE and not self.live_orders_enabled:
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
        status = "dry_run_accepted" if intent.mode is ExecutionMode.DRY_RUN else "paper_accepted_no_exchange_call"
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
        error_reason: str | None = None,
    ) -> ExecutionEvent:
        intent = request.trade_intent
        return ExecutionEvent(
            exchange=intent.exchange_id,
            cycle_id=request.request_id,
            environment=intent.mode.value,
            status=status,
            account_label=intent.account_alias,
            market=intent.market,
            maker_fee_bps=fee_quote.maker_fee_bps if fee_quote is not None else None,
            taker_fee_bps=fee_quote.taker_fee_bps if fee_quote is not None else None,
            entry_fee_bps=fee_quote.entry_fee_bps if fee_quote is not None else None,
            exit_fee_bps=fee_quote.exit_fee_bps if fee_quote is not None else None,
            fee_source=fee_quote.source if fee_quote is not None else None,
            fee_multiplier=fee_quote.fee_multiplier if fee_quote is not None else None,
            fee_multiplier_expires_at=(
                fee_quote.fee_multiplier_expires_at.isoformat()
                if fee_quote is not None and fee_quote.fee_multiplier_expires_at is not None
                else None
            ),
            expected_loss_bps=cost_quote.expected_loss_bps if cost_quote is not None else None,
            planned_gross_volume_usd=intent.planned_gross_notional_usd,
            estimated_fee_usd=cost_quote.estimated_fee_usd if cost_quote is not None else None,
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
