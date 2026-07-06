from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from perpdex_farming_bot.connectors.pacifica_readonly import read_only_get_json
from perpdex_farming_bot.core.execution_cost import MarketFee
from perpdex_farming_bot.marketdata.pacifica import fetch_pacifica_rest_top_of_book
from perpdex_farming_bot.marketdata.spread_monitor import TopOfBook


@dataclass(frozen=True)
class PacificaMarketInfo:
    symbol: str
    tick_size: Decimal
    lot_size: Decimal
    min_order_size_usd: Decimal
    raw_keys: tuple[str, ...]


@dataclass(frozen=True)
class PacificaFeeOverride:
    symbol: str
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    fee_multiplier: Decimal = Decimal("1")
    fee_multiplier_expires_at: datetime | None = None
    slippage_buffer_bps: Decimal = Decimal("0")
    source: str = "config_override"

    @property
    def has_exact_override(self) -> bool:
        return self.entry_fee_bps is not None and self.exit_fee_bps is not None


@dataclass(frozen=True)
class PacificaAccountFee:
    fee_level: int | None
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    source: str = "account_api"


@dataclass(frozen=True)
class PacificaFeeProvider:
    market_info_by_symbol: dict[str, PacificaMarketInfo]
    override_by_symbol: dict[str, PacificaFeeOverride]
    account_fee: PacificaAccountFee | None = None

    def fee_for_market(self, market: str) -> MarketFee:
        override = self.override_by_symbol.get(market)
        slippage_buffer = override.slippage_buffer_bps if override is not None else Decimal("0")

        if self.account_fee is not None:
            if override is not None and override.has_exact_override:
                return MarketFee(
                    entry_fee_bps=override.entry_fee_bps,
                    exit_fee_bps=override.exit_fee_bps,
                    slippage_buffer_bps=slippage_buffer,
                    source=override.source,
                )
            multiplier, source = _active_fee_multiplier(override)
            return MarketFee(
                entry_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                exit_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                slippage_buffer_bps=slippage_buffer,
                source=source,
            )

        if override is not None and override.has_exact_override:
            return MarketFee(
                entry_fee_bps=override.entry_fee_bps,
                exit_fee_bps=override.exit_fee_bps,
                slippage_buffer_bps=slippage_buffer,
                source=override.source,
            )

        return MarketFee(
            entry_fee_bps=None,
            exit_fee_bps=None,
            slippage_buffer_bps=slippage_buffer,
            source="fee_unknown",
        )


@dataclass(frozen=True)
class PacificaTinyOrderPlan:
    symbol: str
    amount: Decimal
    one_side_notional_usd: Decimal
    gross_roundtrip_notional_usd: Decimal
    best_bid: Decimal
    best_ask: Decimal
    spread_bps: Decimal
    min_order_size_usd: Decimal
    lot_size: Decimal
    tick_size: Decimal
    eligible: bool
    reason: str


def load_pacifica_market_info(api_endpoint: str, symbol: str, timeout_seconds: float) -> PacificaMarketInfo:
    payload = read_only_get_json(api_endpoint, "/info", {}, timeout_seconds)
    market = _find_market_info(payload, symbol)
    if market is None:
        raise ValueError(f"market not found in Pacifica /info: {symbol}")
    return _market_info_from_payload(symbol, market)


def load_all_pacifica_market_info(api_endpoint: str, timeout_seconds: float) -> dict[str, PacificaMarketInfo]:
    payload = read_only_get_json(api_endpoint, "/info", {}, timeout_seconds)
    result: dict[str, PacificaMarketInfo] = {}
    for market in _walk_market_objects(payload):
        symbol = str(market.get("symbol") or market.get("s") or market.get("market") or market.get("name") or "")
        if not symbol or symbol in result:
            continue
        result[symbol] = _market_info_from_payload(symbol, market)
    return result


def load_pacifica_account_fee(api_endpoint: str, account: str, timeout_seconds: float) -> PacificaAccountFee:
    payload = read_only_get_json(
        api_endpoint,
        "/account",
        {"account": account},
        timeout_seconds,
        private_readonly=True,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Pacifica account fee response was not an object")
    data = payload["data"]
    return PacificaAccountFee(
        fee_level=_optional_int_field(data, "fee_level"),
        maker_fee_bps=_fee_rate_to_bps(_required_decimal_field(data, "maker_fee")),
        taker_fee_bps=_fee_rate_to_bps(_required_decimal_field(data, "taker_fee")),
    )


def _market_info_from_payload(symbol: str, market: dict[str, object]) -> PacificaMarketInfo:
    tick_size = _decimal_field(market, ("tick_size", "tickSize", "px_tick", "price_tick", "price_tick_size"))
    lot_size = _decimal_field(market, ("lot_size", "lotSize", "sz_step", "amount_step", "quantity_step"))
    min_order_size = _decimal_field(
        market,
        ("min_order_size", "minOrderSize", "min_notional", "min_notional_usd", "min_order_notional"),
    )
    return PacificaMarketInfo(
        symbol=symbol,
        tick_size=tick_size,
        lot_size=lot_size,
        min_order_size_usd=min_order_size,
        raw_keys=tuple(sorted(str(key) for key in market.keys())),
    )


def build_tiny_order_plan(
    *,
    api_endpoint: str,
    symbol: str,
    max_notional_usd: Decimal,
    timeout_seconds: float,
    agg_level: int,
) -> PacificaTinyOrderPlan:
    market_info = load_pacifica_market_info(api_endpoint, symbol, timeout_seconds)
    tob_result = fetch_pacifica_rest_top_of_book(
        api_endpoint,
        symbol=symbol,
        timeout_seconds=timeout_seconds,
        agg_level=agg_level,
    )
    if not tob_result.ok or tob_result.snapshot is None:
        return PacificaTinyOrderPlan(
            symbol=symbol,
            amount=Decimal("0"),
            one_side_notional_usd=Decimal("0"),
            gross_roundtrip_notional_usd=Decimal("0"),
            best_bid=Decimal("0"),
            best_ask=Decimal("0"),
            spread_bps=Decimal("999999"),
            min_order_size_usd=market_info.min_order_size_usd,
            lot_size=market_info.lot_size,
            tick_size=market_info.tick_size,
            eligible=False,
            reason=tob_result.reason,
        )
    return tiny_order_plan_from_top_of_book(
        market_info=market_info,
        top_of_book=tob_result.snapshot,
        max_notional_usd=max_notional_usd,
    )


def tiny_order_plan_from_top_of_book(
    *,
    market_info: PacificaMarketInfo,
    top_of_book: TopOfBook,
    max_notional_usd: Decimal,
) -> PacificaTinyOrderPlan:
    if market_info.lot_size <= 0:
        return PacificaTinyOrderPlan(
            symbol=market_info.symbol,
            amount=Decimal("0"),
            one_side_notional_usd=Decimal("0"),
            gross_roundtrip_notional_usd=Decimal("0"),
            best_bid=top_of_book.best_bid,
            best_ask=top_of_book.best_ask,
            spread_bps=Decimal("999999"),
            min_order_size_usd=market_info.min_order_size_usd,
            lot_size=market_info.lot_size,
            tick_size=market_info.tick_size,
            eligible=False,
            reason="missing_lot_size",
        )

    amount = _round_down_to_step(max_notional_usd / top_of_book.best_ask, market_info.lot_size)
    one_side_notional = amount * top_of_book.best_ask
    mid = (top_of_book.best_bid + top_of_book.best_ask) / Decimal("2")
    spread_bps = ((top_of_book.best_ask - top_of_book.best_bid) / mid) * Decimal("10000") if mid > 0 else Decimal("999999")
    eligible = amount > 0 and one_side_notional >= market_info.min_order_size_usd
    if amount <= 0:
        reason = "quantity_zero"
    elif one_side_notional < market_info.min_order_size_usd:
        reason = f"below_min_order_size:{one_side_notional:.4f}<{market_info.min_order_size_usd}"
    else:
        reason = "ok"
    return PacificaTinyOrderPlan(
        symbol=market_info.symbol,
        amount=amount,
        one_side_notional_usd=one_side_notional,
        gross_roundtrip_notional_usd=one_side_notional * Decimal("2"),
        best_bid=top_of_book.best_bid,
        best_ask=top_of_book.best_ask,
        spread_bps=spread_bps,
        min_order_size_usd=market_info.min_order_size_usd,
        lot_size=market_info.lot_size,
        tick_size=market_info.tick_size,
        eligible=eligible,
        reason=reason,
    )


def load_pacifica_positions(api_endpoint: str, account: str, timeout_seconds: float) -> list[dict[str, object]]:
    payload = read_only_get_json(
        api_endpoint,
        "/positions",
        {"account": account},
        timeout_seconds,
        private_readonly=True,
    )
    return [item for item in _array_payload(payload) if isinstance(item, dict)]


def load_pacifica_open_orders(api_endpoint: str, account: str, timeout_seconds: float) -> tuple[list[dict[str, object]], str]:
    payload = read_only_get_json(
        api_endpoint,
        "/orders",
        {"account": account},
        timeout_seconds,
        private_readonly=True,
    )
    orders = [item for item in _array_payload(payload) if isinstance(item, dict)]
    last_order_id = ""
    if isinstance(payload, dict) and payload.get("last_order_id") is not None:
        last_order_id = str(payload["last_order_id"])
    return orders, last_order_id


def nonzero_pacifica_positions(positions: list[dict[str, object]], symbol: str | None = None) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for position in positions:
        if symbol is not None and pacifica_position_symbol(position) != symbol:
            continue
        if abs(pacifica_signed_position_amount(position)) > 0:
            result.append(position)
    return result


def pacifica_signed_position_amount(position: dict[str, object]) -> Decimal:
    raw_amount = position.get("amount", position.get("a", position.get("quantity", position.get("size", "0"))))
    amount = Decimal(str(raw_amount))
    side = str(position.get("side") or position.get("d") or position.get("direction") or "").lower()
    if side in {"ask", "short", "sell"}:
        return -abs(amount)
    if side in {"bid", "long", "buy"}:
        return abs(amount)
    return amount


def pacifica_position_symbol(position: dict[str, object]) -> str:
    return str(position.get("symbol") or position.get("s") or position.get("market") or "unknown")


def fmt_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _find_market_info(payload: object, symbol: str) -> dict[str, object] | None:
    for item in _walk_market_objects(payload):
        item_symbol = str(item.get("symbol") or item.get("s") or item.get("market") or item.get("name") or "")
        if item_symbol == symbol:
            return item
    return None


def _walk_market_objects(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    candidates: list[dict[str, object]] = []
    data = payload.get("data")
    if isinstance(data, list):
        candidates.extend(item for item in data if isinstance(item, dict))
    if isinstance(data, dict):
        candidates.extend(_walk_market_objects(data))
    for key in ("markets", "perps", "instruments", "symbols"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    if any(key in payload for key in ("symbol", "s", "market", "name")):
        candidates.append(payload)
    return candidates


def _decimal_field(payload: dict[str, object], names: tuple[str, ...]) -> Decimal:
    for name in names:
        value = payload.get(name)
        if value is None or value == "":
            continue
        return Decimal(str(value))
    return Decimal("0")


def _required_decimal_field(payload: dict[str, object], name: str) -> Decimal:
    value = payload.get(name)
    if value is None or value == "":
        raise ValueError(f"missing Pacifica account fee field: {name}")
    return Decimal(str(value))


def _optional_int_field(payload: dict[str, object], name: str) -> int | None:
    value = payload.get(name)
    if value is None or value == "":
        return None
    return int(str(value))


def _fee_rate_to_bps(rate: Decimal) -> Decimal:
    return rate * Decimal("10000")


def _active_fee_multiplier(override: PacificaFeeOverride | None) -> tuple[Decimal, str]:
    if override is None or override.fee_multiplier == Decimal("1"):
        return Decimal("1"), "account_taker_fee"
    if override.fee_multiplier_expires_at is None:
        return Decimal("1"), "account_taker_fee_config_multiplier_missing_expiry_ignored"

    expires_at = override.fee_multiplier_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc):
        return Decimal("1"), "account_taker_fee_config_multiplier_expired_ignored"
    return override.fee_multiplier, "account_taker_fee_config_multiplier"


def _array_payload(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("positions", "orders", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    for key in ("positions", "orders", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
