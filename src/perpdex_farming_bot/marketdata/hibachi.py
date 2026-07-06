from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from perpdex_farming_bot.connectors.hibachi_sdk_public import load_hibachi_orderbook_snapshot
from perpdex_farming_bot.marketdata.spread_monitor import FetchResult, MarketSpec, RestBackoff, SpreadCache, TopOfBook


def fetch_hibachi_rest_top_of_book(
    market: str,
    *,
    average_spread_bps: Decimal,
    depth: int,
    granularity: float,
) -> FetchResult:
    result = load_hibachi_orderbook_snapshot(
        market,
        average_spread_bps=float(average_spread_bps),
        depth=depth,
        granularity=granularity,
    )
    if not result.ok or result.snapshot is None:
        return FetchResult(False, result.reason)
    snapshot = result.snapshot
    return FetchResult(
        True,
        "rest_orderbook",
        TopOfBook(
            exchange_id="hibachi",
            market=market,
            best_bid=Decimal(str(snapshot.best_bid.price)),
            best_ask=Decimal(str(snapshot.best_ask.price)),
            best_bid_size=Decimal(str(snapshot.best_bid.size)),
            best_ask_size=Decimal(str(snapshot.best_ask.size)),
            timestamp=snapshot.timestamp,
            source="rest:orderbook",
        ),
    )


def refresh_hibachi_spread_cache(
    *,
    cache: SpreadCache,
    specs: list[MarketSpec],
    data_api_endpoint: str,
    monitor_source: str,
    cache_max_age_seconds: float,
    websocket_timeout_seconds: float,
    depth: int,
    granularity_by_market: dict[str, float],
    rest_backoff: RestBackoff,
    emit: Callable[[str], None] = print,
) -> None:
    source = monitor_source.lower()
    if source not in {"auto", "websocket", "rest"}:
        raise ValueError("monitor_source must be auto, websocket, or rest")

    if source in {"auto", "websocket"}:
        stale_before = cache.missing_or_stale(specs, cache_max_age_seconds)
        if stale_before:
            snapshots, reason = collect_hibachi_ask_bid_snapshots(
                data_api_endpoint=data_api_endpoint,
                markets=[spec.market for spec in stale_before],
                timeout_seconds=websocket_timeout_seconds,
            )
            cache.update_many(snapshots)
            emit(f"hibachi_monitor_websocket_snapshots={len(snapshots)} reason={reason}")
            if source == "websocket" and cache.missing_or_stale(specs, cache_max_age_seconds):
                emit("hibachi_monitor_rest_backup_skipped=monitor_source_websocket")
                return

    stale_after_ws = cache.missing_or_stale(specs, cache_max_age_seconds)
    for spec in stale_after_ws:
        rest_backoff.wait()
        result = fetch_hibachi_rest_top_of_book(
            spec.market,
            average_spread_bps=spec.average_spread_bps,
            depth=depth,
            granularity=granularity_by_market.get(spec.market, 0.0),
        )
        if result.ok and result.snapshot is not None:
            cache.update(result.snapshot)
            emit(f"hibachi_monitor_rest_backup market={spec.market} ok=True")
            continue
        if "ratelimited" in result.reason.lower() or "rate limit" in result.reason.lower():
            rest_backoff.note_rate_limited()
            emit(f"hibachi_monitor_rest_backup market={spec.market} ok=False rate_limited=True reason={result.reason}")
        else:
            emit(f"hibachi_monitor_rest_backup market={spec.market} ok=False reason={result.reason}")


def collect_hibachi_ask_bid_snapshots(
    *,
    data_api_endpoint: str,
    markets: list[str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    if not markets:
        return [], "no_markets"
    try:
        return asyncio.run(_collect_hibachi_ask_bid_snapshots(data_api_endpoint, markets, timeout_seconds))
    except ImportError:
        return [], "hibachi_xyz_not_installed"
    except Exception as exc:
        return [], f"websocket_error:{exc.__class__.__name__}"


async def _collect_hibachi_ask_bid_snapshots(
    data_api_endpoint: str,
    markets: list[str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    import json

    import websockets

    snapshots: dict[str, TopOfBook] = {}
    endpoint = data_api_endpoint.replace("https://", "wss://").rstrip("/") + "/ws/market?hibachiClient=perpdex-farming-bot"
    async with websockets.connect(endpoint) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "parameters": {
                        "subscriptions": [
                            {"symbol": market, "topic": "ask_bid_price"}
                            for market in markets
                        ],
                    },
                },
                separators=(",", ":"),
            ),
        )
        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
        while set(snapshots) < set(markets):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            parsed = json.loads(raw)
            snapshot = _parse_hibachi_ask_bid_message(parsed)
            if snapshot is not None and snapshot.market in markets:
                snapshots[snapshot.market] = snapshot

    if set(snapshots) >= set(markets):
        reason = "websocket_all_markets"
    elif snapshots:
        reason = "websocket_partial"
    else:
        reason = "websocket_no_snapshots"
    return list(snapshots.values()), reason


def _parse_hibachi_ask_bid_message(message: dict[str, object]) -> TopOfBook | None:
    data = message.get("data") or message.get("payload") or message
    if not isinstance(data, dict):
        return None
    market = _first_string(message, "symbol", "market", "instrument", "instrument_name")
    if not market:
        market = _first_string(data, "symbol", "market", "instrument", "instrument_name")
    if not market:
        parameters = message.get("parameters")
        if isinstance(parameters, dict):
            market = _first_string(parameters, "symbol", "market")
    if not market:
        return None

    bid = _first_decimal(data, "best_bid_price", "bid_price", "bidPrice", "bestBidPrice", "bid")
    ask = _first_decimal(data, "best_ask_price", "ask_price", "askPrice", "bestAskPrice", "ask")
    bid_size = _first_decimal(data, "best_bid_size", "bid_size", "bidSize", "bestBidSize", "bid_quantity", "bidQuantity")
    ask_size = _first_decimal(data, "best_ask_size", "ask_size", "askSize", "bestAskSize", "ask_quantity", "askQuantity")
    if bid is None or ask is None or bid_size is None or ask_size is None:
        return None
    return TopOfBook(
        exchange_id="hibachi",
        market=market,
        best_bid=bid,
        best_ask=ask,
        best_bid_size=bid_size,
        best_ask_size=ask_size,
        timestamp=datetime.now(timezone.utc),
        source="ws:ask_bid_price",
    )


def _first_string(data: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


def _first_decimal(data: dict[str, object], *keys: str) -> Decimal | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None
