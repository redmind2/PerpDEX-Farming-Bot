from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from perpdex_farming_bot.connectors.lighter_readonly import read_only_get_json
from perpdex_farming_bot.credentials import read_lighter_credentials, read_lighter_private_readonly_params
from perpdex_farming_bot.exchanges.base import (
    AdapterError,
    ExchangeOrderResult,
    ExchangePosition,
    PairedRoundtripResult,
)


@dataclass(frozen=True)
class LighterAdapter:
    api_endpoint: str
    credential_prefix: str
    environment: str = "PRODUCTION"
    timeout_seconds: float = 10.0
    allow_live_orders: bool = False

    exchange_id: str = "lighter"

    def account_snapshot(self) -> object:
        params = read_lighter_private_readonly_params(self.credential_prefix, self.environment)
        query = _account_query(params)
        return read_only_get_json(
            self.api_endpoint,
            "/api/v1/account",
            query,
            self.timeout_seconds,
            private_readonly=True,
            read_only_auth_token=params["read_only_auth_token"],
        )

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        payload = self.account_snapshot()
        positions: list[ExchangePosition] = []
        for item in _walk_position_objects(payload):
            size = _signed_position_amount(item)
            if size == 0:
                continue
            positions.append(
                ExchangePosition(
                    exchange_id=self.exchange_id,
                    market=_position_market(item),
                    size=size,
                    side="long" if size > 0 else "short",
                )
            )
        return tuple(positions)

    def list_open_orders(self, *, market_id: int | None = None) -> tuple[dict[str, object], ...]:
        params = read_lighter_private_readonly_params(self.credential_prefix, self.environment)
        account_index = params["account_index"]
        auth_token = params["read_only_auth_token"]
        if not account_index:
            raise AdapterError("Lighter account index env is required for read-only open orders")
        if not auth_token:
            raise AdapterError("Lighter read-only auth token env is required for read-only open orders")
        query: dict[str, object] = {"account_index": account_index, "market_type": "all"}
        if market_id is not None:
            query["market_id"] = market_id
        payload = read_only_get_json(
            self.api_endpoint,
            "/api/v1/accountActiveOrders",
            query,
            self.timeout_seconds,
            private_readonly=True,
            read_only_auth_token=auth_token,
        )
        return tuple(_walk_order_objects(payload))

    def list_trade_fills(self, *, market_id: int | None = None, limit: int = 100) -> tuple[dict[str, object], ...]:
        params = read_lighter_private_readonly_params(self.credential_prefix, self.environment)
        account_index = params["account_index"]
        auth_token = params["read_only_auth_token"]
        if not account_index:
            raise AdapterError("Lighter account index env is required for read-only trades")
        if not auth_token:
            raise AdapterError("Lighter read-only auth token env is required for read-only trades")
        query: dict[str, object] = {
            "account_index": account_index,
            "market_type": "all",
            "limit": limit,
            "sort_by": "timestamp",
            "sort_dir": "desc",
        }
        if market_id is not None:
            query["market_id"] = market_id
            query["market_type"] = "perp"
        payload = read_only_get_json(
            self.api_endpoint,
            "/api/v1/trades",
            query,
            self.timeout_seconds,
            private_readonly=True,
            read_only_auth_token=auth_token,
        )
        return tuple(_walk_trade_objects(payload))

    def signer_ready(self) -> tuple[bool, str]:
        credentials = read_lighter_credentials(self.credential_prefix, self.environment)
        if not credentials["account_index"]:
            return False, "missing_account_index"
        if not credentials["api_key_index"]:
            return False, "missing_api_key_index"
        if not credentials["api_private_key"]:
            return False, "missing_api_private_key"
        return True, "api_private_key_env_present_not_signer_initialized_in_phase0"

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
        del instrument_id, buy_price, sell_price, buy_size, sell_size, planned_gross_volume_usd
        del first_side, second_side, roundtrip_mode
        if not self.allow_live_orders:
            raise AdapterError("Lighter live orders are disabled in the Phase 0 adapter skeleton")
        raise AdapterError("Lighter live order submission is not implemented in Phase 0")

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
            raise AdapterError("Lighter reduce-only live orders are disabled in the Phase 0 adapter skeleton")
        raise AdapterError("Lighter reduce-only order submission is not implemented in Phase 0")

    def create_market_order(self, **_: object) -> ExchangeOrderResult:
        raise AdapterError("Lighter create_market_order is blocked in Phase 0")

    def cancel_order(self, **_: object) -> ExchangeOrderResult:
        raise AdapterError("Lighter cancel_order is blocked in Phase 0")


def _account_query(params: dict[str, str]) -> dict[str, object]:
    if params["account_index"]:
        return {"by": "index", "value": params["account_index"], "active_only": True}
    if params["l1_address"]:
        return {"by": "l1_address", "value": params["l1_address"], "active_only": True}
    raise AdapterError("Lighter account index or L1 address env is required for private read-only account data")


def _walk_position_objects(payload: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    _walk_objects_with_keys(payload, {"position", "position_value", "avg_entry_price"}, result)
    return result


def _walk_order_objects(payload: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    _walk_objects_with_keys(payload, {"order_index", "market_index", "remaining_base_amount"}, result)
    return result


def _walk_trade_objects(payload: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    _walk_objects_with_keys(payload, {"trade_id", "price", "size"}, result)
    if not result:
        _walk_objects_with_keys(payload, {"price", "size", "market_id"}, result)
    return result


def _walk_objects_with_keys(payload: object, keys: set[str], result: list[dict[str, object]]) -> None:
    if isinstance(payload, dict):
        if keys <= set(str(key) for key in payload.keys()):
            result.append(payload)
            return
        for value in payload.values():
            _walk_objects_with_keys(value, keys, result)
    elif isinstance(payload, list):
        for item in payload:
            _walk_objects_with_keys(item, keys, result)


def _position_market(position: dict[str, object]) -> str:
    market = position.get("symbol") or position.get("market") or position.get("market_name")
    if market:
        return str(market)
    market_id = position.get("market_id")
    if market_id is not None:
        return f"market_id:{market_id}"
    return "unknown"


def _signed_position_amount(position: dict[str, object]) -> Decimal:
    amount = Decimal(str(position.get("position", "0")))
    sign = position.get("sign")
    if sign is not None:
        try:
            sign_number = int(str(sign))
        except ValueError:
            sign_number = 0
        if sign_number < 0:
            return -abs(amount)
        if sign_number > 0:
            return abs(amount)
    side = str(position.get("side") or position.get("position_side") or "").lower()
    if side in {"ask", "short", "sell"}:
        return -abs(amount)
    if side in {"bid", "long", "buy"}:
        return abs(amount)
    return amount
