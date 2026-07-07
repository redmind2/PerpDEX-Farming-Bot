from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from perpdex_farming_bot.connectors.hyperliquid_readonly import info_post_json
from perpdex_farming_bot.credentials import (
    read_hyperliquid_credentials,
    read_hyperliquid_private_readonly_params,
)
from perpdex_farming_bot.exchanges.base import (
    AdapterError,
    ExchangeOrderResult,
    ExchangePosition,
    PairedRoundtripResult,
)


@dataclass(frozen=True)
class HyperliquidAdapter:
    api_endpoint: str
    credential_prefix: str
    environment: str = "PRODUCTION"
    timeout_seconds: float = 10.0
    dex: str = ""
    perp_dexs: tuple[str, ...] = ()
    allow_live_orders: bool = False
    max_roundtrip_gross_volume_usd: Decimal | None = Decimal("250")

    exchange_id: str = "hyperliquid"

    def list_positions(self) -> tuple[ExchangePosition, ...]:
        params = read_hyperliquid_private_readonly_params(self.credential_prefix, self.environment)
        account = params["user"]
        if not account:
            raise AdapterError("Hyperliquid account address env is required for private read-only positions")

        positions: list[ExchangePosition] = []
        for dex in self._configured_perp_dexs():
            body: dict[str, object] = {"type": "clearinghouseState", "user": account}
            if dex:
                body["dex"] = dex
            payload = info_post_json(
                self.api_endpoint,
                body,
                self.timeout_seconds,
                private_readonly=True,
            )
            positions.extend(_positions_from_clearinghouse_state(payload, self.exchange_id, dex))
        return tuple(positions)

    def list_open_orders(self) -> tuple[dict[str, object], ...]:
        params = read_hyperliquid_private_readonly_params(self.credential_prefix, self.environment)
        account = params["user"]
        if not account:
            raise AdapterError("Hyperliquid account address env is required for private read-only open orders")

        orders: list[dict[str, object]] = []
        for dex in self._configured_perp_dexs():
            body: dict[str, object] = {"type": "openOrders", "user": account}
            if dex:
                body["dex"] = dex
            payload = info_post_json(
                self.api_endpoint,
                body,
                self.timeout_seconds,
                private_readonly=True,
            )
            if isinstance(payload, list):
                orders.extend(item for item in payload if isinstance(item, dict))
        return tuple(orders)

    def signer_ready(self) -> tuple[bool, str]:
        credentials = read_hyperliquid_credentials(self.credential_prefix, self.environment)
        if not credentials["account_address"]:
            return False, "missing_account_address"
        if not credentials["api_wallet_address"]:
            return False, "missing_api_wallet_address"
        if not credentials["api_wallet_private_key"]:
            return False, "missing_api_wallet_private_key"
        try:
            from eth_account import Account

            derived = Account.from_key(credentials["api_wallet_private_key"]).address
        except Exception:
            return False, "invalid_api_wallet_private_key"
        if derived.casefold() != credentials["api_wallet_address"].casefold():
            return False, "api_wallet_address_mismatch"
        return True, "api_wallet_env_present_key_matches_address"

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
        del instrument_id, second_side
        mode = _normalize_roundtrip_mode(roundtrip_mode)
        if not self.allow_live_orders:
            raise AdapterError("Hyperliquid live order submission is disabled")
        if self.max_roundtrip_gross_volume_usd is not None and planned_gross_volume_usd > self.max_roundtrip_gross_volume_usd:
            raise AdapterError(
                "Hyperliquid planned roundtrip gross volume exceeds adapter cap: "
                f"{planned_gross_volume_usd}>{self.max_roundtrip_gross_volume_usd}"
            )

        signer_ok, signer_reason = self.signer_ready()
        if not signer_ok:
            raise AdapterError(f"Hyperliquid signer is not ready: {signer_reason}")
        if self.list_open_orders():
            return PairedRoundtripResult(
                exchange_id=self.exchange_id,
                market=market,
                success=False,
                planned_gross_volume_usd=planned_gross_volume_usd,
                status="existing_open_orders_detected",
            )

        entry_is_buy, entry_price, entry_size, close_price = _roundtrip_sides(
            first_side=first_side,
            buy_price=buy_price,
            sell_price=sell_price,
            buy_size=buy_size,
            sell_size=sell_size,
        )
        if mode == "netting":
            netting_size = sell_size if entry_is_buy else buy_size
            entry_result, close_result = self._submit_ioc_orders_bulk(
                market=market,
                orders=(
                    _IocOrderIntent(
                        is_buy=entry_is_buy,
                        price=entry_price,
                        size=entry_size,
                        reduce_only=False,
                    ),
                    _IocOrderIntent(
                        is_buy=not entry_is_buy,
                        price=close_price,
                        size=netting_size,
                        reduce_only=False,
                    ),
                ),
            )
            residual = self._first_residual_position_for_market(market)
            status = "ok_flat_bulk_netting" if entry_result.success and close_result.success and residual is None else (
                f"bulk_netting_or_residual_check_failed:{entry_result.status}:{close_result.status}"
            )
            if residual is not None:
                rescue_is_buy = residual.size < 0
                rescue_result = self._submit_ioc_order(
                    market=market,
                    is_buy=rescue_is_buy,
                    price=buy_price if rescue_is_buy else sell_price,
                    size=abs(residual.size),
                    reduce_only=True,
                )
                residual = self._first_residual_position_for_market(market)
                if rescue_result.success and residual is None:
                    status = "ok_flat_bulk_netting_after_reduce_only_rescue"
                else:
                    status = f"bulk_netting_rescue_failed:{rescue_result.status}"
            success = entry_result.success and close_result.success and residual is None
            return PairedRoundtripResult(
                exchange_id=self.exchange_id,
                market=market,
                success=success,
                planned_gross_volume_usd=planned_gross_volume_usd,
                buy_result=entry_result if entry_is_buy else close_result,
                sell_result=close_result if entry_is_buy else entry_result,
                residual_position=residual,
                status=status,
            )

        entry_result = self._submit_ioc_order(
            market=market,
            is_buy=entry_is_buy,
            price=entry_price,
            size=entry_size,
            reduce_only=False,
        )
        if not entry_result.success or entry_result.filled_size <= 0:
            return PairedRoundtripResult(
                exchange_id=self.exchange_id,
                market=market,
                success=False,
                planned_gross_volume_usd=planned_gross_volume_usd,
                buy_result=entry_result if entry_is_buy else None,
                sell_result=None if entry_is_buy else entry_result,
                status=f"entry_not_filled:{entry_result.status}",
            )

        close_result = self._submit_ioc_order(
            market=market,
            is_buy=not entry_is_buy,
            price=close_price,
            size=entry_result.filled_size,
            reduce_only=True,
        )
        residual = self._first_residual_position_for_market(market)
        success = close_result.success and residual is None and close_result.filled_size >= entry_result.filled_size
        return PairedRoundtripResult(
            exchange_id=self.exchange_id,
            market=market,
            success=success,
            planned_gross_volume_usd=planned_gross_volume_usd,
            buy_result=entry_result if entry_is_buy else close_result,
            sell_result=close_result if entry_is_buy else entry_result,
            residual_position=residual,
            status="ok_flat" if success else f"close_or_residual_check_failed:{close_result.status}",
        )

    def _submit_ioc_orders_bulk(
        self,
        *,
        market: str,
        orders: tuple["_IocOrderIntent", ...],
    ) -> tuple[ExchangeOrderResult, ...]:
        if not orders:
            raise AdapterError("Hyperliquid bulk order list must not be empty")
        order_requests: list[dict[str, object]] = []
        for order in orders:
            if order.size <= 0:
                raise AdapterError("Hyperliquid order size must be greater than zero")
            if order.price <= 0:
                raise AdapterError("Hyperliquid order price must be greater than zero")
            order_requests.append(
                {
                    "coin": market,
                    "is_buy": order.is_buy,
                    "sz": float(order.size),
                    "limit_px": float(order.price),
                    "order_type": {"limit": {"tif": "Ioc"}},
                    "reduce_only": order.reduce_only,
                }
            )
        exchange = self._exchange_client(market)
        try:
            response = exchange.bulk_orders(order_requests)
        except Exception as exc:
            return tuple(
                ExchangeOrderResult(
                    exchange_id=self.exchange_id,
                    market=market,
                    success=False,
                    status="bulk_order_exception",
                    error=_exchange_error_reason(exc),
                )
                for _ in orders
            )
        results = _order_results_from_response(self.exchange_id, market, response)
        if len(results) >= len(orders):
            return results[: len(orders)]
        return results + tuple(
            ExchangeOrderResult(
                exchange_id=self.exchange_id,
                market=market,
                success=False,
                status="missing_bulk_order_status",
            )
            for _ in range(len(orders) - len(results))
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
        del instrument_id
        if not self.allow_live_orders:
            raise AdapterError("Hyperliquid reduce-only live orders are disabled")
        signer_ok, signer_reason = self.signer_ready()
        if not signer_ok:
            raise AdapterError(f"Hyperliquid signer is not ready: {signer_reason}")
        return self._submit_ioc_order(
            market=market,
            is_buy=_close_side_is_buy(side),
            price=price,
            size=size,
            reduce_only=True,
        )

    def _submit_ioc_order(
        self,
        *,
        market: str,
        is_buy: bool,
        price: Decimal,
        size: Decimal,
        reduce_only: bool,
    ) -> ExchangeOrderResult:
        if size <= 0:
            raise AdapterError("Hyperliquid order size must be greater than zero")
        if price <= 0:
            raise AdapterError("Hyperliquid order price must be greater than zero")
        exchange = self._exchange_client(market)
        try:
            response = exchange.order(
                market,
                is_buy,
                float(size),
                float(price),
                {"limit": {"tif": "Ioc"}},
                reduce_only=reduce_only,
            )
        except Exception as exc:
            return ExchangeOrderResult(
                exchange_id=self.exchange_id,
                market=market,
                success=False,
                status="order_exception",
                error=_exchange_error_reason(exc),
            )
        return _order_result_from_response(self.exchange_id, market, response)

    def _exchange_client(self, market: str):
        from eth_account import Account
        from hyperliquid.exchange import Exchange

        credentials = read_hyperliquid_credentials(self.credential_prefix, self.environment)
        wallet = Account.from_key(credentials["api_wallet_private_key"])
        return Exchange(
            wallet,
            base_url=self.api_endpoint,
            vault_address=credentials["vault_address"] or None,
            account_address=credentials["account_address"],
            perp_dexs=list(self._configured_perp_dexs(market)),
            timeout=self.timeout_seconds,
        )

    def _configured_perp_dexs(self, market: str = "") -> tuple[str, ...]:
        result: list[str] = [""]
        for dex in self.perp_dexs:
            if dex not in result:
                result.append(dex)
        if self.dex and self.dex not in result:
            result.append(self.dex)
        if ":" in market:
            market_dex = market.split(":", 1)[0]
            if market_dex and market_dex not in result:
                result.append(market_dex)
        return tuple(result)

    def _first_residual_position_for_market(self, market: str) -> ExchangePosition | None:
        normalized = market.casefold()
        short_name = market.split(":", 1)[-1].casefold()
        for position in self.list_positions():
            position_market = position.market.casefold()
            if position_market in {normalized, short_name}:
                return position
        return None


@dataclass(frozen=True)
class _IocOrderIntent:
    is_buy: bool
    price: Decimal
    size: Decimal
    reduce_only: bool


def _positions_from_clearinghouse_state(payload: object, exchange_id: str, dex: str = "") -> list[ExchangePosition]:
    if not isinstance(payload, dict):
        raise AdapterError("Hyperliquid clearinghouseState response was not a JSON object")
    raw_positions = payload.get("assetPositions")
    if not isinstance(raw_positions, list):
        return []

    positions: list[ExchangePosition] = []
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        if not isinstance(position, dict):
            continue
        amount = Decimal(str(position.get("szi", "0")))
        if amount == 0:
            continue
        market = str(position.get("coin") or "unknown")
        if dex and ":" not in market:
            market = f"{dex}:{market}"
        positions.append(
            ExchangePosition(
                exchange_id=exchange_id,
                market=market,
                size=amount,
                side="long" if amount > 0 else "short",
            )
        )
    return positions


def _roundtrip_sides(
    *,
    first_side: str,
    buy_price: Decimal,
    sell_price: Decimal,
    buy_size: Decimal,
    sell_size: Decimal,
) -> tuple[bool, Decimal, Decimal, Decimal]:
    side = first_side.strip().upper()
    if side in {"BUY", "B"}:
        return True, buy_price, buy_size, sell_price
    if side in {"SELL", "S"}:
        return False, sell_price, sell_size, buy_price
    raise AdapterError("Hyperliquid first_side must be BUY or SELL")


def _close_side_is_buy(side: str) -> bool:
    normalized = side.strip().lower()
    if normalized in {"buy", "b", "bid"}:
        return True
    if normalized in {"sell", "s", "ask"}:
        return False
    raise AdapterError("Hyperliquid close side must be buy/b or sell/s")


def _normalize_roundtrip_mode(mode: str) -> str:
    normalized = mode.strip().lower().replace("_", "-")
    if normalized in {"confirmed", "reduce-only", "reduce-only-confirmed"}:
        return "confirmed"
    if normalized == "netting":
        return "netting"
    raise AdapterError("Hyperliquid roundtrip_mode must be confirmed or netting")


def _order_result_from_response(exchange_id: str, market: str, response: object) -> ExchangeOrderResult:
    results = _order_results_from_response(exchange_id, market, response)
    if not results:
        return ExchangeOrderResult(exchange_id, market, False, "missing_order_status")
    return results[0]


def _order_results_from_response(exchange_id: str, market: str, response: object) -> tuple[ExchangeOrderResult, ...]:
    if not isinstance(response, dict):
        return (
            ExchangeOrderResult(exchange_id, market, False, "unexpected_response_type", error=type(response).__name__),
        )
    if response.get("status") != "ok":
        return (
            ExchangeOrderResult(
                exchange_id,
                market,
                False,
                str(response.get("status") or "exchange_error"),
                error=_safe_response_error(response),
            ),
        )
    statuses = _response_statuses(response)
    if not statuses:
        return (ExchangeOrderResult(exchange_id, market, False, "missing_order_status"),)
    return tuple(_order_result_from_status(exchange_id, market, status) for status in statuses)


def _order_result_from_status(exchange_id: str, market: str, status: object) -> ExchangeOrderResult:
    if not isinstance(status, dict):
        return ExchangeOrderResult(exchange_id, market, False, "order_status_not_object")
    if "filled" in status and isinstance(status["filled"], dict):
        filled = status["filled"]
        filled_size = Decimal(str(filled.get("totalSz", "0")))
        return ExchangeOrderResult(
            exchange_id=exchange_id,
            market=market,
            success=filled_size > 0,
            status="filled" if filled_size > 0 else "filled_zero",
            filled_size=filled_size,
            average_price=Decimal(str(filled.get("avgPx", "0"))),
            exchange_order_id=str(filled.get("oid", "")) or None,
        )
    if "resting" in status:
        return ExchangeOrderResult(exchange_id, market, False, "ioc_order_resting_unexpected")
    if "error" in status:
        return ExchangeOrderResult(exchange_id, market, False, "order_error", error=str(status.get("error") or "order_error"))
    return ExchangeOrderResult(exchange_id, market, False, "unknown_order_status")


def _response_statuses(response: dict[str, object]) -> list[object]:
    payload = response.get("response")
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    statuses = data.get("statuses")
    return statuses if isinstance(statuses, list) else []


def _safe_response_error(response: dict[str, object]) -> str:
    for key in ("error", "message"):
        value = response.get(key)
        if value:
            return str(value)[:240]
    return ""


def _exchange_error_reason(exc: Exception) -> str:
    text = str(exc)
    return text[:240] if text else exc.__class__.__name__
