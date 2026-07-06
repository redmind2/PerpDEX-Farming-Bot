from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from perpdex_farming_bot.connectors.risex_readonly import read_only_get_json
from perpdex_farming_bot.connectors.risex_trading import RisexPostResult, RisexSignedPlaceOrder, post_place_order
from perpdex_farming_bot.credentials import read_risex_credentials, read_risex_private_readonly_params
from perpdex_farming_bot.exchanges.base import (
    AdapterError,
    ExchangeOrderResult,
    ExchangePosition,
    PairedRoundtripResult,
)


WAD = Decimal("1000000000000000000")


@dataclass(frozen=True)
class RisexAdapter:
    api_endpoint: str
    credential_prefix: str
    environment: str = "TESTNET"
    timeout_seconds: float = 10.0
    allow_live_orders: bool = False

    exchange_id: str = "risex"

    def submit_signed_place_order(self, signed_order: RisexSignedPlaceOrder) -> RisexPostResult:
        if not self.allow_live_orders:
            raise AdapterError("RiseX live orders are disabled; set allow_live_orders=True only for explicit live tests")
        return post_place_order(self.api_endpoint, signed_order, self.timeout_seconds)

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        params = read_risex_private_readonly_params(self.credential_prefix, self.environment)
        if not params["account"]:
            raise AdapterError("RiseX account address env is required for private read-only positions")
        payload = read_only_get_json(
            self.api_endpoint,
            "/v1/positions",
            {"account": params["account"], "page_size": 1000},
            self.timeout_seconds,
            private_readonly=True,
        )
        data = _object_payload(payload)
        raw_positions = data.get("positions", ())
        if not isinstance(raw_positions, list):
            return ()

        positions: list[ExchangePosition] = []
        for item in raw_positions:
            if not isinstance(item, dict):
                continue
            size = _signed_decimal(item.get("size", "0"))
            if size == 0:
                continue
            positions.append(
                ExchangePosition(
                    exchange_id=self.exchange_id,
                    market=_position_market(item),
                    size=size,
                    side=_position_side(item, size),
                )
            )
        return tuple(positions)

    def signer_ready(self) -> tuple[bool, str]:
        credentials = read_risex_credentials(self.credential_prefix, self.environment)
        if not credentials["account_address"]:
            return False, "missing_account_address"
        if not credentials["signer_address"]:
            return False, "missing_signer_address"
        if not credentials["signer_private_key"]:
            return False, "missing_signer_private_key"
        try:
            from eth_account import Account

            derived = Account.from_key(credentials["signer_private_key"]).address
        except Exception:
            return False, "invalid_signer_private_key"
        if derived.casefold() != credentials["signer_address"].casefold():
            return False, "signer_private_key_does_not_match_signer_address"
        return True, "signer_env_present_key_matches_signer_address"

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
            raise AdapterError("RiseX live orders are disabled in the Phase 0 adapter skeleton")
        raise AdapterError("RiseX live order submission is not implemented yet")

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
            raise AdapterError("RiseX reduce-only live orders are disabled in the Phase 0 adapter skeleton")
        raise AdapterError("RiseX reduce-only order submission is not implemented yet")


def _object_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise AdapterError("RiseX response was not a JSON object")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _position_market(position: dict[str, object]) -> str:
    market = position.get("market") or position.get("symbol") or position.get("market_name")
    if market:
        return str(market)
    market_id = position.get("market_id")
    if market_id is not None:
        return f"market_id:{market_id}"
    return "unknown"


def _position_side(position: dict[str, object], size: Decimal) -> str:
    side = position.get("side")
    if side:
        text = str(side).lower()
        if text == "buy":
            return "long"
        if text == "sell":
            return "short"
        return text
    return "long" if size > 0 else "short"


def _signed_decimal(value: object) -> Decimal:
    raw = str(value)
    number = Decimal(raw)
    if "." in raw:
        return number
    if abs(number) >= Decimal("1000000000000"):
        return number / WAD
    return number
