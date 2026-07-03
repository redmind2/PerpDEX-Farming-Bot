from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from perpdex_farming_bot.models import MarketSnapshot, QuoteLevel


@dataclass(frozen=True)
class HibachiPublicOrderbookResult:
    ok: bool
    reason: str
    snapshot: MarketSnapshot | None = None


def load_hibachi_orderbook_snapshot(
    symbol: str,
    *,
    average_spread_bps: float,
    depth: int,
    granularity: float,
) -> HibachiPublicOrderbookResult:
    try:
        from hibachi_xyz import HibachiApiClient
    except ImportError:
        return HibachiPublicOrderbookResult(False, "hibachi_xyz_not_installed")

    try:
        client = HibachiApiClient()
        if granularity <= 0:
            granularity = _resolve_granularity(client, symbol)
        orderbook = client.get_orderbook(symbol, depth=depth, granularity=granularity)
    except Exception as exc:
        return HibachiPublicOrderbookResult(False, f"hibachi_public_orderbook_error:{exc.__class__.__name__}")

    bids = [_quote_level(level) for level in getattr(orderbook, "bid", ())]
    asks = [_quote_level(level) for level in getattr(orderbook, "ask", ())]
    if not bids or not asks:
        return HibachiPublicOrderbookResult(False, "hibachi_public_orderbook_missing_best_bid_or_ask")

    return HibachiPublicOrderbookResult(
        True,
        "ok",
        MarketSnapshot(
            exchange_id="hibachi",
            market=symbol,
            best_bid=bids[0],
            best_ask=asks[0],
            second_bid=bids[1] if len(bids) > 1 else None,
            second_ask=asks[1] if len(asks) > 1 else None,
            average_spread_bps=average_spread_bps,
            tick_size=0.0,
            timestamp=datetime.now(timezone.utc),
        ),
    )


def _quote_level(level: object) -> QuoteLevel:
    return QuoteLevel(
        price=float(getattr(level, "price")),
        size=float(getattr(level, "quantity")),
    )


def _resolve_granularity(client: object, symbol: str) -> float:
    inventory = client.get_inventory()
    for market in getattr(inventory, "markets", ()):
        contract = getattr(market, "contract", None)
        if contract is None:
            continue
        if getattr(contract, "symbol", "") != symbol:
            continue
        granularities = getattr(contract, "orderbookGranularities", ())
        if granularities:
            return float(granularities[0])
    return 0.1
