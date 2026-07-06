from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Literal

from perpdex_farming_bot.connectors.hotstuff_readonly import info_post_json
from perpdex_farming_bot.credentials import read_hotstuff_credentials, read_hotstuff_private_readonly_params
from perpdex_farming_bot.exchanges.base import (
    AdapterError,
    ExchangeOrderResult,
    ExchangePosition,
    PairedRoundtripResult,
)


@dataclass(frozen=True)
class HotstuffAdapter:
    api_endpoint: str
    credential_prefix: str
    environment: str
    timeout_seconds: float = 10.0

    exchange_id: str = "hotstuff"

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        params = read_hotstuff_private_readonly_params(self.credential_prefix, self.environment)
        payload = info_post_json(
            self.api_endpoint,
            "positions",
            params,
            self.timeout_seconds,
            private_readonly=True,
        )
        if not isinstance(payload, list):
            return ()

        positions: list[ExchangePosition] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            size = Decimal(str(item.get("size", "0")))
            if size == 0:
                continue
            market = _position_market(item)
            positions.append(
                ExchangePosition(
                    exchange_id=self.exchange_id,
                    market=market,
                    size=size,
                    side="long" if size > 0 else "short",
                )
            )
        return tuple(positions)

    def signer_ready(self) -> tuple[bool, str]:
        from eth_account import Account

        credentials = read_hotstuff_credentials(self.credential_prefix, self.environment)
        if not credentials["account_address"]:
            return False, "missing_account_address"
        if not credentials["signer_private_key"]:
            return False, "missing_signer_private_key"
        if not credentials["signer_address"]:
            return False, "missing_signer_address"
        if credentials["account_address"].casefold() == credentials["signer_address"].casefold():
            return False, "account_address_must_be_owner_not_signer"

        wallet = Account.from_key(credentials["signer_private_key"])
        if wallet.address.casefold() != credentials["signer_address"].casefold():
            return False, "signer_address_mismatch"
        if self._signer_registered(wallet.address):
            return True, "signer_registered_for_account"
        return False, "signer_not_registered_for_account"

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
        from hotstuff import PlaceOrderParams, UnitOrder

        del first_side, second_side
        client = self._exchange_client()
        orders = [
            UnitOrder(
                instrumentId=instrument_id,
                side="b",
                positionSide="BOTH",
                price=_fmt_decimal(buy_price),
                size=_fmt_decimal(buy_size),
                tif="IOC",
                ro=False,
                po=False,
                isMarket=True,
            ),
            UnitOrder(
                instrumentId=instrument_id,
                side="s",
                positionSide="BOTH",
                price=_fmt_decimal(sell_price),
                size=_fmt_decimal(sell_size),
                tif="IOC",
                ro=False,
                po=False,
                isMarket=True,
            ),
        ]
        response = _safe_place_order(client, PlaceOrderParams(orders=orders, expiresAfter=_now_ms() + 60_000))
        result = _order_result_from_response(self.exchange_id, market, response)
        return PairedRoundtripResult(
            exchange_id=self.exchange_id,
            market=market,
            success=result.success,
            planned_gross_volume_usd=planned_gross_volume_usd,
            buy_result=result,
            sell_result=result,
            status=result.status,
        )

    def close_position_reduce_only(
        self,
        *,
        market: str,
        instrument_id: int,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> ExchangeOrderResult:
        from hotstuff import PlaceOrderParams, UnitOrder

        if side not in {"b", "s"}:
            raise AdapterError("Hotstuff close side must be b or s")
        client = self._exchange_client()
        order = UnitOrder(
            instrumentId=instrument_id,
            side=side,
            positionSide="BOTH",
            price=_fmt_decimal(price),
            size=_fmt_decimal(size),
            tif="IOC",
            ro=True,
            po=False,
            isMarket=True,
        )
        response = _safe_place_order(client, PlaceOrderParams(orders=[order], expiresAfter=_now_ms() + 60_000))
        return _order_result_from_response(self.exchange_id, market, response)

    def _exchange_client(self) -> object:
        from eth_account import Account
        from hotstuff import ExchangeClient

        credentials = read_hotstuff_credentials(self.credential_prefix, self.environment)
        wallet = Account.from_key(credentials["signer_private_key"])
        return ExchangeClient(wallet=wallet, is_testnet=(self.environment == "TESTNET"))

    def _signer_registered(self, signer_address: str) -> bool:
        params = read_hotstuff_private_readonly_params(self.credential_prefix, self.environment)
        try:
            payload = info_post_json(
                self.api_endpoint,
                "allAgents",
                params,
                self.timeout_seconds,
                private_readonly=True,
            )
        except Exception:
            return False
        return _payload_contains_address(payload, signer_address.casefold())


def hotstuff_close_price(
    side: Literal["b", "s"],
    best_bid: Decimal,
    best_ask: Decimal,
    tick_size: Decimal,
    slippage_bps: Decimal,
) -> Decimal:
    factor = slippage_bps / Decimal("10000")
    if side == "s":
        return round_down_to_step(best_bid * (Decimal("1") - factor), tick_size)
    return round_up_to_step(best_ask * (Decimal("1") + factor), tick_size)


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def _position_market(position: dict[str, object]) -> str:
    return str(position.get("instrument") or position.get("symbol") or position.get("instrument_name") or "unknown")


def _safe_place_order(client: object, params: object) -> object:
    try:
        return client.place_order(params)
    except Exception as exc:
        return {
            "success": False,
            "error": _exchange_error_reason(exc),
            "exception_type": type(exc).__name__,
        }


def _order_result_from_response(exchange_id: str, market: str, response: object) -> ExchangeOrderResult:
    if not isinstance(response, dict):
        return ExchangeOrderResult(exchange_id, market, False, "unexpected_response_type", error=type(response).__name__)

    error = response.get("error")
    if error:
        return ExchangeOrderResult(exchange_id, market, False, "exchange_error", error=str(error))

    data = response.get("data")
    filled_size = Decimal("0")
    average_price: Decimal | None = None
    exchange_order_id: str | None = None
    status = "accepted"

    if isinstance(data, dict):
        status_payload = data.get("status")
        if isinstance(status_payload, list):
            for item in status_payload:
                if not isinstance(item, dict):
                    continue
                filled = item.get("filled")
                if isinstance(filled, dict):
                    status = "filled"
                    filled_size += Decimal(str(filled.get("total_size", "0")))
                    average_price = Decimal(str(filled.get("average_price", "0")))
                    if filled.get("order_id") is not None:
                        exchange_order_id = str(filled.get("order_id"))

    success = bool(response.get("success", False)) or status in {"accepted", "filled"}
    return ExchangeOrderResult(
        exchange_id=exchange_id,
        market=market,
        success=success,
        status=status,
        filled_size=filled_size,
        average_price=average_price,
        exchange_order_id=exchange_order_id,
    )


def _exchange_error_reason(exc: Exception) -> str:
    text = str(exc)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if error:
                return str(error)
            status = payload.get("status")
            if status:
                return str(status)

    sanitized = re.sub(r"0x[a-fA-F0-9]{40,64}", "0x[redacted]", text)
    return sanitized[:240]


def _payload_contains_address(payload: object, address: str) -> bool:
    if isinstance(payload, dict):
        return any(_payload_contains_address(value, address) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains_address(value, address) for value in payload)
    if isinstance(payload, str):
        return payload.casefold() == address
    return False


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _now_ms() -> int:
    return int(time.time() * 1000)
