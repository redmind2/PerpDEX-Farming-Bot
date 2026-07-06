from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from perpdex_farming_bot.connectors.pacifica_readonly import read_only_get_json
from perpdex_farming_bot.marketdata.spread_monitor import FetchResult, MarketSpec, RestBackoff, SpreadCache, TopOfBook


def fetch_pacifica_rest_top_of_book(
    api_endpoint: str,
    *,
    symbol: str,
    timeout_seconds: float,
    agg_level: int = 1,
) -> FetchResult:
    try:
        orderbook = read_only_get_json(
            api_endpoint,
            "/book",
            {"symbol": symbol, "agg_level": agg_level},
            timeout_seconds,
        )
        data = _object_payload(orderbook)
        bid = _first_level(data, 0)
        ask = _first_level(data, 1)
        return FetchResult(
            True,
            "rest_orderbook",
            TopOfBook(
                exchange_id="pacifica",
                market=symbol,
                best_bid=Decimal(str(bid["p"])),
                best_ask=Decimal(str(ask["p"])),
                best_bid_size=Decimal(str(bid["a"])),
                best_ask_size=Decimal(str(ask["a"])),
                timestamp=_timestamp_from_payload(data),
                source="rest:book",
            ),
        )
    except (KeyError, IndexError, TypeError, ValueError, TimeoutError, OSError) as exc:
        return FetchResult(False, f"rest_orderbook_error:{exc.__class__.__name__}")


def refresh_pacifica_spread_cache(
    *,
    cache: SpreadCache,
    specs: list[MarketSpec],
    api_endpoint: str,
    wss_endpoint: str,
    monitor_source: str,
    cache_max_age_seconds: float,
    websocket_timeout_seconds: float,
    timeout_seconds: float,
    agg_level_by_market: dict[str, int],
    rest_backoff: RestBackoff,
    emit: Callable[[str], None] = print,
) -> None:
    source = monitor_source.lower()
    if source not in {"auto", "websocket", "rest"}:
        raise ValueError("monitor_source must be auto, websocket, or rest")

    stale_before = cache.missing_or_stale(specs, cache_max_age_seconds)
    if source in {"auto", "websocket"} and stale_before:
        snapshots, reason = collect_pacifica_bbo_snapshots(
            wss_endpoint=wss_endpoint,
            symbols=[spec.market for spec in stale_before],
            timeout_seconds=websocket_timeout_seconds,
        )
        cache.update_many(snapshots)
        emit(f"pacifica_monitor_websocket_snapshots={len(snapshots)} reason={reason}")
        if source == "websocket" and cache.missing_or_stale(specs, cache_max_age_seconds):
            emit("pacifica_monitor_rest_backup_skipped=monitor_source_websocket")
            return

    stale_after_ws = cache.missing_or_stale(specs, cache_max_age_seconds)
    for spec in stale_after_ws:
        rest_backoff.wait()
        result = fetch_pacifica_rest_top_of_book(
            api_endpoint,
            symbol=spec.market,
            timeout_seconds=timeout_seconds,
            agg_level=agg_level_by_market.get(spec.market, 1),
        )
        if result.ok and result.snapshot is not None:
            cache.update(result.snapshot)
            emit(f"pacifica_monitor_rest_backup market={spec.market} ok=True")
            continue
        if "rate" in result.reason.lower():
            rest_backoff.note_rate_limited()
            emit(f"pacifica_monitor_rest_backup market={spec.market} ok=False rate_limited=True reason={result.reason}")
        else:
            emit(f"pacifica_monitor_rest_backup market={spec.market} ok=False reason={result.reason}")


def collect_pacifica_bbo_snapshots(
    *,
    wss_endpoint: str,
    symbols: list[str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    if not symbols:
        return [], "no_markets"
    try:
        return asyncio.run(_collect_pacifica_bbo_snapshots(wss_endpoint, symbols, timeout_seconds))
    except ImportError:
        return [], "websockets_not_installed"
    except Exception as exc:
        return [], f"websocket_error:{exc.__class__.__name__}"


async def _collect_pacifica_bbo_snapshots(
    wss_endpoint: str,
    symbols: list[str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    import websockets

    snapshots: dict[str, TopOfBook] = {}
    async with websockets.connect(wss_endpoint, ping_interval=None) as websocket:
        for symbol in symbols:
            await websocket.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "params": {"source": "bbo", "symbol": symbol},
                    },
                    separators=(",", ":"),
                ),
            )

        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
        wanted = set(symbols)
        while wanted - set(snapshots):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            parsed = json.loads(raw)
            snapshot = _parse_pacifica_bbo_message(parsed)
            if snapshot is not None and snapshot.market in wanted:
                snapshots[snapshot.market] = snapshot

    if set(snapshots) >= set(symbols):
        reason = "websocket_all_markets"
    elif snapshots:
        reason = "websocket_partial"
    else:
        reason = "websocket_no_snapshots"
    return list(snapshots.values()), reason


def _parse_pacifica_bbo_message(message: object) -> TopOfBook | None:
    if not isinstance(message, dict):
        return None
    if message.get("channel") != "bbo":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    market = str(data.get("s") or data.get("symbol") or "")
    if not market:
        return None
    return TopOfBook(
        exchange_id="pacifica",
        market=market,
        best_bid=Decimal(str(data["b"])),
        best_ask=Decimal(str(data["a"])),
        best_bid_size=Decimal(str(data["B"])),
        best_ask_size=Decimal(str(data["A"])),
        timestamp=_timestamp_from_payload(data),
        source="ws:bbo",
    )


def _object_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _first_level(data: dict[str, object], index: int) -> dict[str, object]:
    levels = data["l"]
    if not isinstance(levels, list) or len(levels) <= index:
        raise ValueError("orderbook levels are missing")
    side = levels[index]
    if not isinstance(side, list) or not side:
        raise ValueError("orderbook side is empty")
    first = side[0]
    if not isinstance(first, dict):
        raise ValueError("orderbook level is not an object")
    return first


def _timestamp_from_payload(payload: object) -> datetime:
    if isinstance(payload, dict):
        for key in ("t", "timestamp"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                raw = int(str(value))
            except ValueError:
                continue
            if raw > 10_000_000_000:
                return datetime.fromtimestamp(raw / 1000, timezone.utc)
            return datetime.fromtimestamp(raw, timezone.utc)
    return datetime.now(timezone.utc)
