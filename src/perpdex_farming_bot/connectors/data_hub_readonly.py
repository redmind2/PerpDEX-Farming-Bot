from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from perpdex_farming_bot.models import MarketSnapshot


@dataclass(frozen=True)
class DataHubSnapshotResult:
    ok: bool
    reason: str
    snapshot: MarketSnapshot | None = None


@dataclass(frozen=True)
class DataHubSpreadSignal:
    exchange_id: str
    symbol: str
    timestamp: datetime
    spread_bps: float
    average_spread_bps: float

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()


@dataclass(frozen=True)
class DataHubSpreadResult:
    ok: bool
    reason: str
    signal: DataHubSpreadSignal | None = None


@dataclass(frozen=True)
class DataHubWindowAverage:
    label: str
    average_spread_bps: float
    sample_count: int


@dataclass(frozen=True)
class DataHubWindowSpreadResult:
    ok: bool
    reason: str
    signal: DataHubSpreadSignal | None = None
    averages: tuple[DataHubWindowAverage, ...] = ()


class DataHubReadonlyConnector:
    """Reads public market data from the Data Hub SQLite DB using mode=ro."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout_seconds: float = 30.0,
        locked_retries: int = 3,
        locked_retry_sleep_seconds: float = 0.25,
        immutable: bool = False,
    ) -> None:
        self.db_path = str(db_path)
        self.timeout_seconds = timeout_seconds
        self.locked_retries = locked_retries
        self.locked_retry_sleep_seconds = locked_retry_sleep_seconds
        self.immutable = immutable

    def latest_spread_signal(
        self,
        exchange_id: str,
        symbol: str,
        *,
        average_spread_samples: int = 12,
    ) -> DataHubSpreadResult:
        try:
            with self._connect() as connection:
                row = self._fetch_one(
                    connection,
                    """
                    SELECT *
                    FROM market_snapshots
                    WHERE exchange_id = ? COLLATE NOCASE
                        AND symbol = ? COLLATE NOCASE
                        AND spread_bps IS NOT NULL
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (exchange_id, symbol),
                )
                if row is None:
                    return DataHubSpreadResult(False, "spread_snapshot_missing")

                average_spread_bps = self._average_spread_bps(
                    connection,
                    exchange_id,
                    symbol,
                    max(1, average_spread_samples),
                )
                if average_spread_bps is None:
                    average_spread_bps = float(row["spread_bps"])

                return DataHubSpreadResult(
                    True,
                    "ok",
                    DataHubSpreadSignal(
                        exchange_id=str(row["exchange_id"]),
                        symbol=str(row["symbol"]),
                        timestamp=_parse_timestamp(row["timestamp"]),
                        spread_bps=float(row["spread_bps"]),
                        average_spread_bps=average_spread_bps,
                    ),
                )
        except sqlite3.OperationalError as exc:
            return DataHubSpreadResult(False, f"sqlite_operational_error:{exc.__class__.__name__}")

    def latest_spread_signal_with_window_min(
        self,
        exchange_id: str,
        symbol: str,
        *,
        windows: tuple[tuple[str, int], ...] = (("1d", 1), ("7d", 7)),
    ) -> DataHubWindowSpreadResult:
        try:
            with self._connect() as connection:
                row = self._fetch_one(
                    connection,
                    """
                    SELECT *
                    FROM market_snapshots
                    WHERE exchange_id = ? COLLATE NOCASE
                        AND symbol = ? COLLATE NOCASE
                        AND spread_bps IS NOT NULL
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (exchange_id, symbol),
                )
                if row is None:
                    return DataHubWindowSpreadResult(False, "spread_snapshot_missing")

                latest_timestamp = _parse_timestamp(row["timestamp"])
                averages: list[DataHubWindowAverage] = []
                for label, days in windows:
                    average = self._average_spread_bps_since(
                        connection,
                        exchange_id,
                        symbol,
                        latest_timestamp - timedelta(days=days),
                    )
                    if average is not None:
                        averages.append(
                            DataHubWindowAverage(
                                label=label,
                                average_spread_bps=average[0],
                                sample_count=average[1],
                            )
                        )

                positive_averages = [item for item in averages if item.average_spread_bps > 0]
                if not positive_averages:
                    return DataHubWindowSpreadResult(False, "positive_window_spread_samples_missing")

                selected_average = min(item.average_spread_bps for item in positive_averages)
                return DataHubWindowSpreadResult(
                    True,
                    "ok_positive_window_min",
                    DataHubSpreadSignal(
                        exchange_id=str(row["exchange_id"]),
                        symbol=str(row["symbol"]),
                        timestamp=latest_timestamp,
                        spread_bps=float(row["spread_bps"]),
                        average_spread_bps=selected_average,
                    ),
                    tuple(averages),
                )
        except sqlite3.OperationalError as exc:
            return DataHubWindowSpreadResult(False, f"sqlite_operational_error:{exc.__class__.__name__}")

    def _average_spread_bps(
        self,
        connection: sqlite3.Connection,
        exchange_id: str,
        symbol: str,
        sample_count: int,
    ) -> float | None:
        rows = self._fetch_all(
            connection,
            """
            SELECT spread_bps
            FROM market_snapshots
            WHERE exchange_id = ? COLLATE NOCASE
                AND symbol = ? COLLATE NOCASE
                AND spread_bps IS NOT NULL
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (exchange_id, symbol, sample_count),
        )
        values = [float(row["spread_bps"]) for row in rows]
        if not values:
            return None
        return sum(values) / len(values)

    def _average_spread_bps_since(
        self,
        connection: sqlite3.Connection,
        exchange_id: str,
        symbol: str,
        since: datetime,
    ) -> tuple[float, int] | None:
        rows = self._fetch_all(
            connection,
            """
            SELECT spread_bps
            FROM market_snapshots
            WHERE exchange_id = ? COLLATE NOCASE
                AND symbol = ? COLLATE NOCASE
                AND spread_bps IS NOT NULL
                AND timestamp >= ?
            ORDER BY timestamp DESC, id DESC
            """,
            (exchange_id, symbol, since.astimezone(timezone.utc).isoformat()),
        )
        values = [float(row["spread_bps"]) for row in rows]
        if not values:
            return None
        return sum(values) / len(values), len(values)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            _readonly_uri(self.db_path, immutable=self.immutable),
            uri=True,
            timeout=self.timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _fetch_one(
        self,
        connection: sqlite3.Connection,
        query: str,
        params: tuple[object, ...],
    ) -> sqlite3.Row | None:
        return _with_locked_retry(
            lambda: connection.execute(query, params).fetchone(),
            self.locked_retries,
            self.locked_retry_sleep_seconds,
        )

    def _fetch_all(
        self,
        connection: sqlite3.Connection,
        query: str,
        params: tuple[object, ...],
    ) -> list[sqlite3.Row]:
        return _with_locked_retry(
            lambda: connection.execute(query, params).fetchall(),
            self.locked_retries,
            self.locked_retry_sleep_seconds,
        )


def _with_locked_retry(
    operation: Callable[[], object],
    retries: int,
    sleep_seconds: float,
) -> object:
    for attempt in range(retries + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= retries:
                raise
            time.sleep(sleep_seconds * (attempt + 1))
    raise AssertionError("unreachable")


def _readonly_uri(path: str, *, immutable: bool = False) -> str:
    if path.startswith("file:"):
        uri = path
        separator = "&" if "?" in uri else "?"
        if "mode=" in uri and "mode=ro" not in uri:
            raise ValueError("Data Hub SQLite URI must use mode=ro")
        if "mode=" not in uri:
            uri = f"{uri}{separator}mode=ro"
            separator = "&"
        if immutable and "immutable=" not in uri:
            uri = f"{uri}{separator}immutable=1"
        return uri

    uri = Path(path).resolve().as_uri()
    query = "mode=ro"
    if immutable:
        query = f"{query}&immutable=1"
    return f"{uri}?{query}"


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)

    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(float(text), timezone.utc)

    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
