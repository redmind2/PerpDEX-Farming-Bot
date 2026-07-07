from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from perpdex_farming_bot.connectors.lighter_readonly import read_only_get_json
from perpdex_farming_bot.marketdata.spread_monitor import FetchResult, TopOfBook


@dataclass(frozen=True)
class LighterMarketMetadata:
    market_id: int
    symbol: str
    market_type: str = ""
    price_decimals: int | None = None
    size_decimals: int | None = None
    quote_decimals: int | None = None
    min_base_amount: Decimal | None = None
    min_quote_amount: Decimal | None = None
    maker_fee_percent: Decimal | None = None
    taker_fee_percent: Decimal | None = None
    source: str = "orderBooks"


def fetch_lighter_rest_top_of_book(
    api_endpoint: str,
    *,
    market_id: int,
    market: str,
    timeout_seconds: float,
    limit: int = 5,
) -> FetchResult:
    try:
        orderbook = read_only_get_json(
            api_endpoint,
            "/api/v1/orderBookOrders",
            {"market_id": market_id, "limit": limit},
            timeout_seconds,
        )
        data = _object_payload(orderbook)
        bid = _first_level(data, "bids")
        ask = _first_level(data, "asks")
        return FetchResult(
            True,
            "rest_orderbook",
            TopOfBook(
                exchange_id="lighter",
                market=market,
                best_bid=Decimal(str(bid["price"])),
                best_ask=Decimal(str(ask["price"])),
                best_bid_size=Decimal(str(bid["size"])),
                best_ask_size=Decimal(str(ask["size"])),
                timestamp=_timestamp_from_payload(data),
                source="rest:orderBookOrders",
            ),
        )
    except (KeyError, IndexError, TypeError, ValueError, TimeoutError, OSError) as exc:
        return FetchResult(False, f"rest_orderbook_error:{exc.__class__.__name__}")


def collect_lighter_ticker_snapshots(
    *,
    wss_endpoint: str,
    market_ids: list[int],
    market_names_by_id: dict[int, str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    if not market_ids:
        return [], "no_markets"
    try:
        return asyncio.run(_collect_lighter_ticker_snapshots(wss_endpoint, market_ids, market_names_by_id, timeout_seconds))
    except ImportError:
        return [], "websockets_not_installed"
    except Exception as exc:
        return [], f"websocket_error:{exc.__class__.__name__}"


def lighter_market_metadata_from_order_books(payload: object) -> dict[str, LighterMarketMetadata]:
    result: dict[str, LighterMarketMetadata] = {}
    for market in _walk_market_objects(payload):
        market_id = _optional_int_field(market, ("market_id", "market_index", "id", "index"))
        if market_id is None:
            continue
        symbol = str(market.get("symbol") or market.get("name") or f"market_id:{market_id}")
        result[str(market_id)] = LighterMarketMetadata(
            market_id=market_id,
            symbol=symbol,
            market_type=str(market.get("market_type") or market.get("type") or ""),
            price_decimals=_optional_int_field(
                market,
                ("supported_price_decimals", "price_decimals", "priceDecimals"),
            ),
            size_decimals=_optional_int_field(
                market,
                ("supported_size_decimals", "size_decimals", "sizeDecimals"),
            ),
            quote_decimals=_optional_int_field(
                market,
                ("supported_quote_decimals", "quote_decimals", "quoteDecimals"),
            ),
            min_base_amount=_optional_decimal_field(
                market,
                ("min_base_amount", "minBaseAmount", "min_size", "minSize"),
            ),
            min_quote_amount=_optional_decimal_field(
                market,
                ("min_quote_amount", "minQuoteAmount", "min_order_size", "minOrderSize"),
            ),
            maker_fee_percent=_optional_decimal_field(
                market,
                ("maker_fee", "makerFee", "maker_fee_percent", "makerFeePercent"),
            ),
            taker_fee_percent=_optional_decimal_field(
                market,
                ("taker_fee", "takerFee", "taker_fee_percent", "takerFeePercent"),
            ),
        )
    return result


async def _collect_lighter_ticker_snapshots(
    wss_endpoint: str,
    market_ids: list[int],
    market_names_by_id: dict[int, str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    import websockets

    snapshots: dict[int, TopOfBook] = {}
    async with websockets.connect(wss_endpoint, ping_interval=60) as websocket:
        for market_id in market_ids:
            await websocket.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "channel": f"ticker/{market_id}",
                    },
                    separators=(",", ":"),
                ),
            )

        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
        wanted = set(market_ids)
        while wanted - set(snapshots):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            parsed = json.loads(raw)
            snapshot = _parse_lighter_ticker_message(parsed, market_names_by_id)
            if snapshot is None:
                continue
            market_id = _market_id_from_channel(parsed)
            if market_id in wanted:
                snapshots[market_id] = snapshot

    if set(snapshots) >= set(market_ids):
        reason = "websocket_all_markets"
    elif snapshots:
        reason = "websocket_partial"
    else:
        reason = "websocket_no_snapshots"
    return list(snapshots.values()), reason


def _parse_lighter_ticker_message(message: object, market_names_by_id: dict[int, str]) -> TopOfBook | None:
    if not isinstance(message, dict):
        return None
    if str(message.get("type") or "") not in {"update/ticker", "subscribed/ticker"}:
        return None
    ticker = message.get("ticker")
    if not isinstance(ticker, dict):
        return None
    ask = ticker.get("a")
    bid = ticker.get("b")
    if not isinstance(ask, dict) or not isinstance(bid, dict):
        return None
    market_id = _market_id_from_channel(message)
    market = market_names_by_id.get(market_id, str(ticker.get("s") or f"market_id:{market_id}"))
    return TopOfBook(
        exchange_id="lighter",
        market=market,
        best_bid=Decimal(str(bid["price"])),
        best_ask=Decimal(str(ask["price"])),
        best_bid_size=Decimal(str(bid["size"])),
        best_ask_size=Decimal(str(ask["size"])),
        timestamp=_timestamp_from_payload(message),
        source="ws:ticker",
    )


def _object_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    order_book = payload.get("order_book")
    if isinstance(order_book, dict):
        return order_book
    return payload


def _first_level(data: dict[str, object], side: str) -> dict[str, object]:
    levels = data.get(side)
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"{side} is empty")
    first = levels[0]
    if isinstance(first, dict):
        if "size" not in first and "base_amount" in first:
            return {"price": first["price"], "size": first["base_amount"]}
        if "size" not in first and "remaining_base_amount" in first:
            return {"price": first["price"], "size": first["remaining_base_amount"]}
        return first
    if isinstance(first, list) and len(first) >= 2:
        return {"price": first[0], "size": first[1]}
    raise ValueError(f"{side}[0] is not a price level")


def _walk_market_objects(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("order_books", "orderBooks", "markets", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    for key in ("order_books", "orderBooks", "markets", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if any(key in payload for key in ("market_id", "market_index", "symbol")):
        return [payload]
    return []


def _optional_decimal_field(payload: dict[str, object], names: tuple[str, ...]) -> Decimal | None:
    for name in names:
        value = payload.get(name)
        if value is None or value == "":
            continue
        return Decimal(str(value))
    return None


def _optional_int_field(payload: dict[str, object], names: tuple[str, ...]) -> int | None:
    for name in names:
        value = payload.get(name)
        if value is None or value == "":
            continue
        return int(str(value))
    return None


def _timestamp_from_payload(payload: object) -> datetime:
    if isinstance(payload, dict):
        for key in ("timestamp", "last_updated_at", "transaction_time"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                raw = int(str(value))
            except ValueError:
                continue
            if raw > 10_000_000_000_000:
                return datetime.fromtimestamp(raw / 1_000_000, timezone.utc)
            if raw > 10_000_000_000:
                return datetime.fromtimestamp(raw / 1000, timezone.utc)
            return datetime.fromtimestamp(raw, timezone.utc)
    return datetime.now(timezone.utc)


def _market_id_from_channel(message: dict[str, object]) -> int:
    channel = str(message.get("channel") or "")
    _, _, raw = channel.partition(":")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return -1
    return -1
