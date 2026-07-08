from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable

from perpdex_farming_bot.core.execution_event import emit_execution_event
from perpdex_farming_bot.core.execution_models import ExecutionRequest, TradeIntent
from perpdex_farming_bot.exchanges.base import AdapterError
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway


@dataclass
class GatewayLiveActionProxy:
    target: object
    gateway: ExecutionGateway
    trade_intent: TradeIntent
    request_id_prefix: str
    emit_events: bool = True
    _sequence: int = field(default=0, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    def submit_signed_place_order(self, signed_order: object) -> object:
        return self._run_sync(
            "submit_signed_place_order",
            lambda: self.target.submit_signed_place_order(signed_order),  # type: ignore[attr-defined]
        )

    def submit_signed_order_request(self, signed_request: object) -> object:
        return self._run_sync(
            "submit_signed_order_request",
            lambda: self.target.submit_signed_order_request(signed_request),  # type: ignore[attr-defined]
        )

    def submit_place_order_params(self, *args: object, **kwargs: object) -> object:
        return self._run_sync(
            "submit_place_order_params",
            lambda: self.target.submit_place_order_params(*args, **kwargs),  # type: ignore[attr-defined]
        )

    def batch_orders(self, orders: object) -> object:
        return self._run_sync(
            "batch_orders",
            lambda: self.target.batch_orders(orders),  # type: ignore[attr-defined]
        )

    def place_market_order(self, *args: object, **kwargs: object) -> object:
        return self._run_sync(
            "place_market_order",
            lambda: self.target.place_market_order(*args, **kwargs),  # type: ignore[attr-defined]
        )

    async def create_order(self, *args: object, **kwargs: object) -> object:
        return await self._run_async(
            "create_order",
            lambda: self.target.create_order(*args, **kwargs),  # type: ignore[attr-defined]
        )

    def _run_sync(self, action_name: str, action: Callable[[], object]) -> object:
        result = self.gateway.run_live_adapter_action(
            ExecutionRequest(
                request_id=self._next_request_id(action_name),
                trade_intent=self.trade_intent,
            ),
            action,
        )
        self._emit_result_event(result)
        if not result.accepted:
            raise AdapterError(result.error or result.status)
        return result.payload

    async def _run_async(self, action_name: str, action: Callable[[], object]) -> object:
        result = await self.gateway.run_live_adapter_action_async(
            ExecutionRequest(
                request_id=self._next_request_id(action_name),
                trade_intent=self.trade_intent,
            ),
            action,
        )
        self._emit_result_event(result)
        if not result.accepted:
            raise AdapterError(result.error or result.status)
        return result.payload

    def _next_request_id(self, action_name: str) -> str:
        clean_action = action_name.replace("_", "-")
        with self._lock:
            self._sequence += 1
            return f"{self.request_id_prefix}-{self._sequence}-{clean_action}"

    def _emit_result_event(self, result: object) -> None:
        if not self.emit_events:
            return
        execution_result = getattr(result, "execution_result", None)
        event = getattr(execution_result, "ledger_event", None)
        if event is not None:
            emit_execution_event(event)
