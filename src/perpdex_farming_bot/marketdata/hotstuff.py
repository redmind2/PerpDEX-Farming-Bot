from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from perpdex_farming_bot.connectors.hotstuff_readonly import info_post_json
from perpdex_farming_bot.marketdata.spread_monitor import FetchResult, MarketSpec, RestBackoff, SpreadCache, TopOfBook


def fetch_hotstuff_rest_top_of_book(
    api_endpoint: str,
    market: str,
    timeout_seconds: float,
) -> FetchResult:
    try:
        orderbook = info_post_json(api_endpoint, "orderbook", {"symbol": market}, timeout_seconds)
        if not isinstance(orderbook, dict):
            return FetchResult(False, "orderbook_response_not_object")
        bid = _first_level(orderbook, "bids")
        ask = _first_level(orderbook, "asks")
        return FetchResult(
            True,
            "rest_orderbook",
            TopOfBook(
                exchange_id="hotstuff",
                market=market,
                best_bid=Decimal(str(bid["price"])),
                best_ask=Decimal(str(ask["price"])),
                best_bid_size=Decimal(str(bid["size"])),
                best_ask_size=Decimal(str(ask["size"])),
                timestamp=datetime.now(timezone.utc),
                source="rest:orderbook",
            ),
        )
    except (KeyError, IndexError, TypeError, ValueError, TimeoutError, OSError) as exc:
        return FetchResult(False, f"rest_orderbook_error:{exc.__class__.__name__}")


def refresh_hotstuff_spread_cache(
    *,
    cache: SpreadCache,
    specs: list[MarketSpec],
    api_endpoint: str,
    wss_endpoint: str,
    monitor_source: str,
    cache_max_age_seconds: float,
    websocket_timeout_seconds: float,
    timeout_seconds: float,
    rest_backoff: RestBackoff,
    emit: Callable[[str], None] = print,
) -> None:
    source = monitor_source.lower()
    if source not in {"auto", "websocket", "rest"}:
        raise ValueError("monitor_source must be auto, websocket, or rest")

    if source in {"auto", "websocket"}:
        stale_before = cache.missing_or_stale(specs, cache_max_age_seconds)
        if stale_before:
            snapshots, reason = collect_hotstuff_bbo_snapshots(
                wss_endpoint=wss_endpoint,
                markets=[spec.market for spec in stale_before],
                timeout_seconds=websocket_timeout_seconds,
            )
            cache.update_many(snapshots)
            emit(f"hotstuff_monitor_websocket_snapshots={len(snapshots)} reason={reason}")
            if source == "websocket" and cache.missing_or_stale(specs, cache_max_age_seconds):
                emit("hotstuff_monitor_rest_backup_skipped=monitor_source_websocket")
                return

    stale_after_ws = cache.missing_or_stale(specs, cache_max_age_seconds)
    if not stale_after_ws:
        return

    for spec in stale_after_ws:
        rest_backoff.wait()
        result = fetch_hotstuff_rest_top_of_book(api_endpoint, spec.market, timeout_seconds)
        if result.ok and result.snapshot is not None:
            cache.update(result.snapshot)
            emit(f"hotstuff_monitor_rest_backup market={spec.market} ok=True")
            continue
        if "rate" in result.reason.lower():
            rest_backoff.note_rate_limited()
            emit(f"hotstuff_monitor_rest_backup market={spec.market} ok=False rate_limited=True reason={result.reason}")
        else:
            emit(f"hotstuff_monitor_rest_backup market={spec.market} ok=False reason={result.reason}")


def collect_hotstuff_bbo_snapshots(
    *,
    wss_endpoint: str,
    markets: list[str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    if not markets:
        return [], "no_markets"
    try:
        return asyncio.run(_collect_hotstuff_bbo_snapshots(wss_endpoint, markets, timeout_seconds))
    except ImportError:
        return [], "websockets_not_installed"
    except Exception as exc:
        return [], f"websocket_error:{exc.__class__.__name__}"


async def _collect_hotstuff_bbo_snapshots(
    wss_endpoint: str,
    markets: list[str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    import websockets

    snapshots: dict[str, TopOfBook] = {}
    async with websockets.connect(wss_endpoint, ping_interval=None) as websocket:
        for index, market in enumerate(markets, start=1):
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": str(index),
                        "method": "subscribe",
                        "params": {"channel": "bbo", "symbol": market},
                    },
                    separators=(",", ":"),
                ),
            )

        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
        wanted = set(markets)
        while wanted - set(snapshots):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            parsed = json.loads(raw)
            snapshot = _parse_hotstuff_bbo_message(parsed)
            if snapshot is not None and snapshot.market in wanted:
                snapshots[snapshot.market] = snapshot

    if set(snapshots) >= set(markets):
        reason = "websocket_all_markets"
    elif snapshots:
        reason = "websocket_partial"
    else:
        reason = "websocket_no_snapshots"
    return list(snapshots.values()), reason


def _parse_hotstuff_bbo_message(message: object) -> TopOfBook | None:
    if not isinstance(message, dict):
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    data = params.get("data")
    if not isinstance(data, dict):
        return None
    market = str(data.get("symbol") or "")
    if not market:
        return None
    return TopOfBook(
        exchange_id="hotstuff",
        market=market,
        best_bid=Decimal(str(data["best_bid_price"])),
        best_ask=Decimal(str(data["best_ask_price"])),
        best_bid_size=Decimal(str(data["best_bid_size"])),
        best_ask_size=Decimal(str(data["best_ask_size"])),
        timestamp=datetime.now(timezone.utc),
        source="ws:bbo",
    )


def _first_level(orderbook: dict[str, object], side: str) -> dict[str, object]:
    levels = orderbook[side]
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"{side} is empty")
    first = levels[0]
    if not isinstance(first, dict):
        raise ValueError(f"{side}[0] is not an object")
    return first
