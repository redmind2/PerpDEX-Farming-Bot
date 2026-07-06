from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from perpdex_farming_bot.models import MarketSnapshot, QuoteLevel


@dataclass(frozen=True)
class TopOfBook:
    exchange_id: str
    market: str
    best_bid: Decimal
    best_ask: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    timestamp: datetime
    source: str

    @property
    def mid_price(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        if self.mid_price <= 0:
            return Decimal("999999")
        return ((self.best_ask - self.best_bid) / self.mid_price) * Decimal("10000")

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()

    def to_market_snapshot(self, *, average_spread_bps: Decimal, tick_size: Decimal = Decimal("0")) -> MarketSnapshot:
        return MarketSnapshot(
            exchange_id=self.exchange_id,
            market=self.market,
            best_bid=QuoteLevel(price=float(self.best_bid), size=float(self.best_bid_size)),
            best_ask=QuoteLevel(price=float(self.best_ask), size=float(self.best_ask_size)),
            second_bid=None,
            second_ask=None,
            average_spread_bps=float(average_spread_bps),
            tick_size=float(tick_size),
            timestamp=self.timestamp,
        )


@dataclass(frozen=True)
class MarketSpec:
    exchange_id: str
    market: str
    average_spread_bps: Decimal
    max_spread_bps: Decimal
    tick_size: Decimal = Decimal("0")
    metadata: Any = None


@dataclass(frozen=True)
class FetchResult:
    ok: bool
    reason: str
    snapshot: TopOfBook | None = None


@dataclass(frozen=True)
class SpreadSelection:
    selected: tuple[MarketSpec, TopOfBook] | None
    accepted: tuple[tuple[MarketSpec, TopOfBook], ...]
    rejected: tuple[tuple[MarketSpec, str], ...]


@dataclass
class SpreadCache:
    _items: dict[tuple[str, str], TopOfBook] = field(default_factory=dict)

    def update(self, snapshot: TopOfBook) -> None:
        self._items[(snapshot.exchange_id, snapshot.market)] = snapshot

    def update_many(self, snapshots: list[TopOfBook] | tuple[TopOfBook, ...]) -> None:
        for snapshot in snapshots:
            self.update(snapshot)

    def get(self, exchange_id: str, market: str) -> TopOfBook | None:
        return self._items.get((exchange_id, market))

    def fresh(self, exchange_id: str, market: str, max_age_seconds: float) -> TopOfBook | None:
        snapshot = self.get(exchange_id, market)
        if snapshot is None:
            return None
        if snapshot.age_seconds > max_age_seconds:
            return None
        return snapshot

    def missing_or_stale(self, specs: list[MarketSpec], max_age_seconds: float) -> list[MarketSpec]:
        return [
            spec
            for spec in specs
            if self.fresh(spec.exchange_id, spec.market, max_age_seconds) is None
        ]


@dataclass
class RestBackoff:
    min_interval_seconds: float = 0.1
    default_backoff_seconds: float = 5.0
    _next_allowed_at: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if self._next_allowed_at > now:
            time.sleep(self._next_allowed_at - now)
        self._next_allowed_at = time.monotonic() + max(0.0, self.min_interval_seconds)

    def note_rate_limited(self, retry_after_seconds: float | None = None) -> None:
        delay = retry_after_seconds if retry_after_seconds is not None else self.default_backoff_seconds
        self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + max(0.0, delay))


def check_spread_gate(spec: MarketSpec, snapshot: TopOfBook) -> tuple[bool, str]:
    spread_bps = snapshot.spread_bps
    if spread_bps > spec.average_spread_bps:
        return False, f"spread_above_average:{spread_bps:.4f}>{spec.average_spread_bps:.4f}"
    if spread_bps > spec.max_spread_bps:
        return False, f"spread_above_hard_cap:{spread_bps:.4f}>{spec.max_spread_bps:.4f}"
    return True, "spread_ok"


def select_lowest_spread(
    cache: SpreadCache,
    specs: list[MarketSpec],
    *,
    max_age_seconds: float,
) -> SpreadSelection:
    accepted: list[tuple[MarketSpec, TopOfBook]] = []
    rejected: list[tuple[MarketSpec, str]] = []

    for spec in specs:
        snapshot = cache.fresh(spec.exchange_id, spec.market, max_age_seconds)
        if snapshot is None:
            rejected.append((spec, "snapshot_missing_or_stale"))
            continue
        ok, reason = check_spread_gate(spec, snapshot)
        if not ok:
            rejected.append((spec, reason))
            continue
        accepted.append((spec, snapshot))

    if not accepted:
        return SpreadSelection(None, tuple(accepted), tuple(rejected))

    selected = min(accepted, key=lambda item: (item[1].spread_bps, item[0].market))
    return SpreadSelection(selected, tuple(accepted), tuple(rejected))
