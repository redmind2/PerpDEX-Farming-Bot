from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import time

from perpdex_farming_bot.core.execution_event import emit_execution_event
from perpdex_farming_bot.core.execution_models import ExecutionRequest, OrderKind, RoundtripMode
from perpdex_farming_bot.exchanges.base import ExchangeOrderResult, ExchangePosition, PairedRoundtripResult
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway
from perpdex_farming_bot.gateway.live_preflight import paired_live_trade_intent


@dataclass
class GatewayRoundtripAdapter:
    gateway: ExecutionGateway
    exchange_id: str
    account_alias: str
    request_id_prefix: str = "gateway-roundtrip"
    _sequence: int = field(default=0, init=False)

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        adapter = self.gateway._registered_adapter(self.exchange_id)
        return tuple(
            ExchangePosition(
                exchange_id=position.exchange_id,
                market=position.market,
                size=position.size,
                side=position.side,
            )
            for position in adapter.list_positions(self.account_alias)
        )

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
        self._sequence += 1
        intent = paired_live_trade_intent(
            exchange_id=self.exchange_id,
            account_alias=self.account_alias,
            strategy_id=f"{self.exchange_id}_gateway_roundtrip_adapter",
            market=market,
            roundtrip_mode=_roundtrip_mode(roundtrip_mode),
            buy_quantity=buy_size,
            sell_quantity=sell_size,
            buy_price=buy_price,
            sell_price=sell_price,
            buy_reference_price=buy_price,
            sell_reference_price=sell_price,
            buy_order_type=OrderKind.LIMIT,
            sell_order_type=OrderKind.LIMIT,
            time_in_force="ioc",
            max_gross_notional_usd=planned_gross_volume_usd,
            metadata={"planned_gross_volume_usd": str(planned_gross_volume_usd)},
        )
        request = ExecutionRequest(
            request_id=f"{self.request_id_prefix}-{self._sequence}",
            trade_intent=intent,
        )
        started = time.perf_counter()
        result = self.gateway.execute_paired_roundtrip(
            request,
            instrument_id=instrument_id,
            first_side=first_side,
            second_side=second_side,
        )
        emit_execution_event(
            self.gateway.record_observation(
                request,
                status=result.status or ("ok" if result.success else "failed"),
                metadata=_roundtrip_observation_metadata(result, started),
                error_reason=None if result.success else result.status,
            )
        )
        return result

    def close_position_reduce_only(
        self,
        *,
        market: str,
        instrument_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> ExchangeOrderResult:
        self._sequence += 1
        intent = paired_live_trade_intent(
            exchange_id=self.exchange_id,
            account_alias=self.account_alias,
            strategy_id=f"{self.exchange_id}_gateway_reduce_only_close",
            market=market,
            roundtrip_mode=RoundtripMode.CONFIRMED,
            quantity=size,
            buy_price=price,
            sell_price=price,
            buy_reference_price=price,
            sell_reference_price=price,
            buy_order_type=OrderKind.LIMIT,
            sell_order_type=OrderKind.LIMIT,
            max_gross_notional_usd=abs(size * price) * Decimal("2"),
        )
        request = ExecutionRequest(
            request_id=f"{self.request_id_prefix}-close-{self._sequence}",
            trade_intent=intent,
        )
        started = time.perf_counter()
        result = self.gateway.execute_reduce_only_close(
            request,
            instrument_id=instrument_id,
            side=side,
            price=price,
            size=size,
        )
        emit_execution_event(
            self.gateway.record_observation(
                request,
                status=result.status,
                metadata=_close_observation_metadata(result, started),
                error_reason=None if result.success else result.status,
            )
        )
        return result


def _roundtrip_mode(raw: str) -> RoundtripMode:
    normalized = raw.strip().lower().replace("_", "-")
    if normalized == "netting":
        return RoundtripMode.NETTING
    if normalized in {"fast-reduce-only", "fast_reduce_only"}:
        return RoundtripMode.FAST_REDUCE_ONLY
    return RoundtripMode.CONFIRMED


def _roundtrip_observation_metadata(result: PairedRoundtripResult, started: float) -> dict[str, object]:
    filled_gross = Decimal("0")
    for order in (result.buy_result, result.sell_result):
        if order is None or order.average_price is None:
            continue
        filled_gross += abs(order.filled_size * order.average_price)
    order_ids = tuple(
        order.exchange_order_id
        for order in (result.buy_result, result.sell_result)
        if order is not None and order.exchange_order_id
    )
    residual_size = result.residual_position.size if result.residual_position is not None else Decimal("0")
    final_flat = _result_is_flat(result, residual_size)
    return {
        "adapter_submit_elapsed_ms": _elapsed_ms(started),
        "filled_gross_volume_usd": filled_gross if filled_gross > 0 else None,
        "final_position_count": 0 if final_flat else None,
        "final_all_flat": final_flat,
        "order_ids": order_ids,
    }


def _close_observation_metadata(result: ExchangeOrderResult, started: float) -> dict[str, object]:
    filled_gross = None
    if result.average_price is not None and result.filled_size:
        filled_gross = abs(result.filled_size * result.average_price)
    return {
        "adapter_submit_elapsed_ms": _elapsed_ms(started),
        "filled_gross_volume_usd": filled_gross,
        "final_position_count": 0 if result.success else None,
        "final_all_flat": True if result.success else None,
        "order_ids": (result.exchange_order_id,) if result.exchange_order_id else (),
    }


def _result_is_flat(result: PairedRoundtripResult, residual_size: Decimal) -> bool | None:
    if result.residual_position is not None:
        return residual_size == 0
    if "flat" in result.status:
        return True
    return None


def _elapsed_ms(started: float) -> Decimal:
    return Decimal(str((time.perf_counter() - started) * 1000))
