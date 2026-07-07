from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from perpdex_farming_bot.connectors.hyperliquid_readonly import info_post_json
from perpdex_farming_bot.marketdata.spread_monitor import FetchResult, TopOfBook


PERP_MAX_DECIMALS = 6


@dataclass(frozen=True)
class HyperliquidMarketInfo:
    coin: str
    asset_id: int
    sz_decimals: int
    lot_size: Decimal
    price_decimal_places: int
    min_order_size_usd: Decimal | None = None

    @property
    def min_order_known(self) -> bool:
        return self.min_order_size_usd is not None and self.min_order_size_usd > 0


def fetch_hyperliquid_rest_top_of_book(
    api_endpoint: str,
    *,
    coin: str,
    timeout_seconds: float,
    dex: str = "",
) -> FetchResult:
    body: dict[str, object] = {"type": "l2Book", "coin": coin}
    if dex:
        body["dex"] = dex
    try:
        orderbook = info_post_json(api_endpoint, body, timeout_seconds)
        levels = _levels_payload(orderbook)
        bid = _first_level(levels, 0)
        ask = _first_level(levels, 1)
        return FetchResult(
            True,
            "rest_l2_book",
            TopOfBook(
                exchange_id="hyperliquid",
                market=coin,
                best_bid=Decimal(str(bid["px"])),
                best_ask=Decimal(str(ask["px"])),
                best_bid_size=Decimal(str(bid["sz"])),
                best_ask_size=Decimal(str(ask["sz"])),
                timestamp=_timestamp_from_payload(orderbook),
                source="rest:info:l2Book",
            ),
        )
    except (KeyError, IndexError, TypeError, ValueError, TimeoutError, OSError) as exc:
        return FetchResult(False, f"rest_l2_book_error:{exc.__class__.__name__}")


def load_hyperliquid_market_info(
    api_endpoint: str,
    *,
    coin: str,
    timeout_seconds: float,
    dex: str = "",
    min_order_size_usd: Decimal | None = None,
) -> HyperliquidMarketInfo:
    metadata = load_all_hyperliquid_market_info(
        api_endpoint,
        timeout_seconds=timeout_seconds,
        dex=dex,
        min_order_size_by_coin={coin: min_order_size_usd} if min_order_size_usd is not None else {},
    )
    if coin not in metadata:
        raise ValueError(f"Hyperliquid market was not found in meta: {coin}")
    return metadata[coin]


def load_all_hyperliquid_market_info(
    api_endpoint: str,
    *,
    timeout_seconds: float,
    dex: str = "",
    min_order_size_by_coin: dict[str, Decimal | None] | None = None,
) -> dict[str, HyperliquidMarketInfo]:
    body: dict[str, object] = {"type": "meta"}
    if dex:
        body["dex"] = dex
    payload = info_post_json(api_endpoint, body, timeout_seconds)
    universe = _meta_universe(payload)
    min_by_coin = min_order_size_by_coin or {}
    result: dict[str, HyperliquidMarketInfo] = {}
    for asset_id, item in enumerate(universe):
        if not isinstance(item, dict):
            continue
        coin = str(item.get("name") or "")
        if not coin:
            continue
        sz_decimals = int(str(item.get("szDecimals", "0")))
        result[coin] = HyperliquidMarketInfo(
            coin=coin,
            asset_id=asset_id,
            sz_decimals=sz_decimals,
            lot_size=lot_size_from_sz_decimals(sz_decimals),
            price_decimal_places=perp_price_decimal_places(sz_decimals),
            min_order_size_usd=min_by_coin.get(coin),
        )
    return result


def round_size_down(value: Decimal, market_info: HyperliquidMarketInfo) -> Decimal:
    return round_down_to_step(value, market_info.lot_size)


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def lot_size_from_sz_decimals(sz_decimals: int) -> Decimal:
    if sz_decimals <= 0:
        return Decimal("1")
    return Decimal("1").scaleb(-sz_decimals)


def perp_price_decimal_places(sz_decimals: int) -> int:
    return max(0, PERP_MAX_DECIMALS - sz_decimals)


def _meta_universe(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise ValueError("Hyperliquid meta response was not an object")
    universe = payload.get("universe")
    if not isinstance(universe, list):
        raise ValueError("Hyperliquid meta response did not contain universe")
    return universe


def _levels_payload(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise ValueError("Hyperliquid l2Book response was not an object")
    levels = payload.get("levels")
    if not isinstance(levels, list):
        raise ValueError("Hyperliquid l2Book response did not contain levels")
    return levels


def _first_level(levels: list[object], side_index: int) -> dict[str, object]:
    side = levels[side_index]
    if not isinstance(side, list) or not side:
        side_name = "bids" if side_index == 0 else "asks"
        raise ValueError(f"Hyperliquid l2Book {side_name} side was empty")
    first = side[0]
    if not isinstance(first, dict):
        raise ValueError("Hyperliquid l2Book level was not an object")
    return first


def _timestamp_from_payload(payload: object) -> datetime:
    if isinstance(payload, dict) and payload.get("time") is not None:
        try:
            return datetime.fromtimestamp(int(str(payload["time"])) / 1000, timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)
