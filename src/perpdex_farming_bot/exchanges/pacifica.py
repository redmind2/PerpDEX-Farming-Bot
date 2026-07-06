from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from perpdex_farming_bot.connectors.pacifica_readonly import read_only_get_json
from perpdex_farming_bot.connectors.pacifica_trading import (
    PacificaPostResult,
    PacificaSignedRequest,
    build_signed_request,
    extract_order_id,
    post_signed_json,
)
from perpdex_farming_bot.credentials import read_pacifica_credentials, read_pacifica_private_readonly_params
from perpdex_farming_bot.exchanges.base import (
    AdapterError,
    ExchangeOrderResult,
    ExchangePosition,
    PairedRoundtripResult,
)


@dataclass(frozen=True)
class PacificaAdapter:
    api_endpoint: str
    credential_prefix: str
    environment: str = "TESTNET"
    timeout_seconds: float = 10.0
    allow_live_orders: bool = False

    exchange_id: str = "pacifica"

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        params = read_pacifica_private_readonly_params(self.credential_prefix, self.environment)
        if not params["account"]:
            raise AdapterError("Pacifica account address env is required for private read-only positions")
        payload = read_only_get_json(
            self.api_endpoint,
            "/positions",
            {"account": params["account"]},
            self.timeout_seconds,
            private_readonly=True,
        )
        raw_positions = _array_payload(payload)

        positions: list[ExchangePosition] = []
        for item in raw_positions:
            if not isinstance(item, dict):
                continue
            amount = _signed_position_amount(item)
            if amount == 0:
                continue
            positions.append(
                ExchangePosition(
                    exchange_id=self.exchange_id,
                    market=_position_market(item),
                    size=amount,
                    side="long" if amount > 0 else "short",
                )
            )
        return tuple(positions)

    def signer_ready(self) -> tuple[bool, str]:
        credentials = read_pacifica_credentials(self.credential_prefix, self.environment)
        if not credentials["account_address"]:
            return False, "missing_account_address"
        if not credentials["api_agent_public_key"]:
            return False, "missing_api_agent_public_key"
        if not credentials["api_agent_private_key"]:
            return False, "missing_api_agent_private_key"
        return True, "api_agent_key_env_present_not_bound_verified"

    def build_market_order_request(
        self,
        *,
        symbol: str,
        amount: Decimal,
        side: str,
        slippage_percent: Decimal,
        reduce_only: bool,
        client_order_id: str,
        expiry_window_ms: int,
    ) -> PacificaSignedRequest:
        credentials = read_pacifica_credentials(self.credential_prefix, self.environment)
        return build_signed_request(
            operation_type="create_market_order",
            payload={
                "symbol": symbol,
                "amount": _fmt_decimal(amount),
                "side": side,
                "slippage_percent": _fmt_decimal(slippage_percent),
                "reduce_only": reduce_only,
                "client_order_id": client_order_id,
            },
            account_address=credentials["account_address"],
            api_agent_public_key=credentials["api_agent_public_key"],
            api_agent_private_key=credentials["api_agent_private_key"],
            expiry_window_ms=expiry_window_ms,
        )

    def submit_signed_order_request(self, signed_request: PacificaSignedRequest) -> PacificaPostResult:
        if not self.allow_live_orders:
            raise AdapterError("Pacifica live order submission is disabled")
        if signed_request.path not in {"/orders/create", "/orders/create_market"}:
            raise AdapterError(f"Pacifica adapter refuses non-order signed path: {signed_request.path}")
        return post_signed_json(self.api_endpoint, signed_request, self.timeout_seconds)

    def create_market_order(
        self,
        *,
        symbol: str,
        amount: Decimal,
        side: str,
        slippage_percent: Decimal,
        reduce_only: bool,
        client_order_id: str,
        expiry_window_ms: int = 5000,
    ) -> ExchangeOrderResult:
        signed_request = self.build_market_order_request(
            symbol=symbol,
            amount=amount,
            side=side,
            slippage_percent=slippage_percent,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            expiry_window_ms=expiry_window_ms,
        )
        result = self.submit_signed_order_request(signed_request)
        return ExchangeOrderResult(
            exchange_id=self.exchange_id,
            market=symbol,
            success=result.ok,
            status=f"http_{result.status_code}" if result.status_code is not None else "network_error",
            exchange_order_id=extract_order_id(result.parsed) or None,
            error=result.error,
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
    ) -> PairedRoundtripResult:
        del instrument_id, buy_price, sell_price, buy_size, sell_size, first_side, second_side
        if not self.allow_live_orders:
            raise AdapterError("Pacifica live orders are disabled in the Phase 0 adapter skeleton")
        raise AdapterError("Pacifica live order submission is not implemented yet")

    def close_position_reduce_only(
        self,
        *,
        market: str,
        instrument_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> ExchangeOrderResult:
        del market, instrument_id, side, price, size
        if not self.allow_live_orders:
            raise AdapterError("Pacifica reduce-only live orders are disabled in the Phase 0 adapter skeleton")
        raise AdapterError("Pacifica reduce-only order submission is not implemented yet")


def _array_payload(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise AdapterError("Pacifica response was not a JSON object")
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        positions = data.get("positions")
        if isinstance(positions, list):
            return positions
    return []


def _position_market(position: dict[str, object]) -> str:
    market = position.get("symbol") or position.get("s") or position.get("market")
    return str(market) if market else "unknown"


def _signed_position_amount(position: dict[str, object]) -> Decimal:
    raw_amount = position.get("amount", position.get("a", "0"))
    amount = Decimal(str(raw_amount))
    side = str(position.get("side") or position.get("d") or "").lower()
    if side in {"ask", "short", "sell"}:
        return -abs(amount)
    if side in {"bid", "long", "buy"}:
        return abs(amount)
    return amount


def _fmt_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")
