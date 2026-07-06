from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from perpdex_farming_bot.connectors.risex_readonly import read_only_get_json
from perpdex_farming_bot.marketdata.spread_monitor import FetchResult, MarketSpec, RestBackoff, SpreadCache, TopOfBook


WAD = Decimal("1000000000000000000")


def fetch_risex_rest_top_of_book(
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
            "/v1/orderbook",
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
                exchange_id="risex",
                market=market,
                best_bid=_risex_decimal(bid["price"]),
                best_ask=_risex_decimal(ask["price"]),
                best_bid_size=_risex_decimal(bid["quantity"]),
                best_ask_size=_risex_decimal(ask["quantity"]),
                timestamp=_timestamp_from_payload(data),
                source="rest:orderbook",
            ),
        )
    except (KeyError, IndexError, TypeError, ValueError, TimeoutError, OSError) as exc:
        return FetchResult(False, f"rest_orderbook_error:{exc.__class__.__name__}")


def refresh_risex_spread_cache(
    *,
    cache: SpreadCache,
    specs: list[MarketSpec],
    api_endpoint: str,
    wss_endpoint: str,
    market_ids_by_market: dict[str, int],
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

    stale_before = cache.missing_or_stale(specs, cache_max_age_seconds)
    if source in {"auto", "websocket"} and stale_before:
        market_ids = [_market_id_for_spec(spec, market_ids_by_market) for spec in stale_before]
        market_ids = [market_id for market_id in market_ids if market_id is not None]
        snapshots, reason = collect_risex_orderbook_snapshots(
            wss_endpoint=wss_endpoint,
            market_ids=market_ids,
            market_names_by_id={value: key for key, value in market_ids_by_market.items()},
            timeout_seconds=websocket_timeout_seconds,
        )
        cache.update_many(snapshots)
        emit(f"risex_monitor_websocket_snapshots={len(snapshots)} reason={reason}")
        if source == "websocket" and cache.missing_or_stale(specs, cache_max_age_seconds):
            emit("risex_monitor_rest_backup_skipped=monitor_source_websocket")
            return

    stale_after_ws = cache.missing_or_stale(specs, cache_max_age_seconds)
    for spec in stale_after_ws:
        market_id = _market_id_for_spec(spec, market_ids_by_market)
        if market_id is None:
            emit(f"risex_monitor_rest_backup market={spec.market} ok=False reason=missing_market_id")
            continue
        rest_backoff.wait()
        result = fetch_risex_rest_top_of_book(
            api_endpoint,
            market_id=market_id,
            market=spec.market,
            timeout_seconds=timeout_seconds,
        )
        if result.ok and result.snapshot is not None:
            cache.update(result.snapshot)
            emit(f"risex_monitor_rest_backup market={spec.market} ok=True")
            continue
        if "rate" in result.reason.lower():
            rest_backoff.note_rate_limited()
            emit(f"risex_monitor_rest_backup market={spec.market} ok=False rate_limited=True reason={result.reason}")
        else:
            emit(f"risex_monitor_rest_backup market={spec.market} ok=False reason={result.reason}")


def collect_risex_orderbook_snapshots(
    *,
    wss_endpoint: str,
    market_ids: list[int],
    market_names_by_id: dict[int, str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    if not market_ids:
        return [], "no_markets"
    try:
        return asyncio.run(_collect_risex_orderbook_snapshots(wss_endpoint, market_ids, market_names_by_id, timeout_seconds))
    except ImportError:
        return [], "websockets_not_installed"
    except Exception as exc:
        return [], f"websocket_error:{exc.__class__.__name__}"


async def _collect_risex_orderbook_snapshots(
    wss_endpoint: str,
    market_ids: list[int],
    market_names_by_id: dict[int, str],
    timeout_seconds: float,
) -> tuple[list[TopOfBook], str]:
    import websockets

    snapshots: dict[int, TopOfBook] = {}
    async with websockets.connect(wss_endpoint, ping_interval=None) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {"channel": "orderbook", "market_ids": market_ids},
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
            snapshot = _parse_risex_orderbook_message(parsed, market_names_by_id)
            if snapshot is None:
                continue
            market_id = _market_id_from_message(parsed)
            if market_id in wanted:
                snapshots[market_id] = snapshot

    if set(snapshots) >= set(market_ids):
        reason = "websocket_all_markets"
    elif snapshots:
        reason = "websocket_partial"
    else:
        reason = "websocket_no_snapshots"
    return list(snapshots.values()), reason


def _parse_risex_orderbook_message(message: object, market_names_by_id: dict[int, str]) -> TopOfBook | None:
    if not isinstance(message, dict):
        return None
    market_id = _market_id_from_message(message)
    if market_id is None:
        return None
    data = _object_payload(message)
    if "bids" not in data or "asks" not in data:
        return None
    bid = _first_level(data, "bids")
    ask = _first_level(data, "asks")
    market = market_names_by_id.get(market_id, f"market_id:{market_id}")
    return TopOfBook(
        exchange_id="risex",
        market=market,
        best_bid=_risex_decimal(bid["price"]),
        best_ask=_risex_decimal(ask["price"]),
        best_bid_size=_risex_decimal(bid["quantity"]),
        best_ask_size=_risex_decimal(ask["quantity"]),
        timestamp=_timestamp_from_payload(message),
        source="ws:orderbook",
    )


def _object_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    nested = payload.get("result")
    if isinstance(nested, dict):
        data = nested.get("data")
        if isinstance(data, dict):
            return data
        return nested
    return payload


def _first_level(data: dict[str, object], side: str) -> dict[str, object]:
    levels = data[side]
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"{side} is empty")
    first = levels[0]
    if isinstance(first, dict):
        if "quantity" not in first and "size" in first:
            return {"price": first["price"], "quantity": first["size"]}
        return first
    if isinstance(first, list) and len(first) >= 2:
        return {"price": first[0], "quantity": first[1]}
    raise ValueError(f"{side}[0] is not a price level")


def _risex_decimal(value: object) -> Decimal:
    raw = str(value)
    number = Decimal(raw)
    if "." in raw:
        return number
    if abs(number) >= Decimal("1000000000000"):
        return number / WAD
    return number


def _timestamp_from_payload(payload: object) -> datetime:
    if isinstance(payload, dict):
        for key in ("timestamp", "block_timestamp", "worker_timestamp"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                raw = int(str(value))
            except ValueError:
                continue
            if raw > 10_000_000_000_000:
                return datetime.fromtimestamp(raw / 1_000_000_000, timezone.utc)
            return datetime.fromtimestamp(raw, timezone.utc)
    return datetime.now(timezone.utc)


def _market_id_from_message(message: dict[str, object]) -> int | None:
    for source in (message, _object_payload(message)):
        value = source.get("market_id")
        if value is None:
            continue
        try:
            return int(str(value))
        except ValueError:
            continue
    return None


def _market_id_for_spec(spec: MarketSpec, market_ids_by_market: dict[str, int]) -> int | None:
    if spec.market in market_ids_by_market:
        return market_ids_by_market[spec.market]
    if isinstance(spec.metadata, dict) and spec.metadata.get("market_id") is not None:
        try:
            return int(str(spec.metadata["market_id"]))
        except ValueError:
            return None
    return None
