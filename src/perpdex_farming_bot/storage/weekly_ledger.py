from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from perpdex_farming_bot.analytics import PerformanceMetrics
from perpdex_farming_bot.budget import BudgetState, PeriodWindow


LIVE_RUN_EVENT_COLUMNS = (
    "exchange",
    "account_label",
    "wallet_label",
    "market",
    "cycle_id",
    "environment",
    "fee_level",
    "maker_fee_bps",
    "taker_fee_bps",
    "entry_fee_bps",
    "exit_fee_bps",
    "fee_source",
    "fee_multiplier",
    "fee_multiplier_expires_at",
    "live_spread_bps",
    "expected_loss_bps",
    "planned_gross_volume_usd",
    "filled_gross_volume_usd",
    "estimated_fee_usd",
    "estimated_loss_usd",
    "realized_pnl_usd",
    "points_estimate",
    "start_position_count",
    "final_position_count",
    "start_open_order_count",
    "final_open_order_count",
    "order_ids",
    "error_reason",
    "status",
)


_LIVE_RUN_EXTRA_COLUMNS = {
    "environment": "TEXT",
    "cycle_id": "TEXT",
    "fee_level": "TEXT",
    "maker_fee_bps": "REAL",
    "taker_fee_bps": "REAL",
    "entry_fee_bps": "REAL",
    "exit_fee_bps": "REAL",
    "fee_source": "TEXT",
    "fee_multiplier": "REAL",
    "fee_multiplier_expires_at": "TEXT",
    "live_spread_bps": "REAL",
    "expected_loss_bps": "REAL",
    "filled_gross_volume_usd": "REAL",
    "estimated_fee_usd": "REAL",
    "estimated_loss_usd": "REAL",
    "realized_pnl_usd": "REAL",
    "points_estimate": "REAL",
    "start_position_count": "INTEGER",
    "final_position_count": "INTEGER",
    "start_open_order_count": "INTEGER",
    "final_open_order_count": "INTEGER",
    "order_ids": "TEXT",
    "error_reason": "TEXT",
}


@dataclass(frozen=True)
class WeeklyTotals:
    gross_volume_usd: float
    realized_pnl_usd: float
    realized_loss_usd: float
    run_count: int


@dataclass(frozen=True)
class LiveWeeklyTotals:
    planned_gross_volume_usd: float
    run_count: int


@dataclass(frozen=True)
class LiveWeeklyMarketTotal:
    exchange_id: str
    account_group_key: str
    wallet_role: str
    wallet_key: str
    credential_prefix: str
    market: str
    planned_gross_volume_usd: float
    run_count: int


class WeeklyLedger:
    """Stores local weekly strategy results in SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS weekly_market_metrics (
                    period_start_utc TEXT NOT NULL,
                    period_end_utc TEXT NOT NULL,
                    exchange_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    wallet_id TEXT NOT NULL,
                    market TEXT NOT NULL,
                    gross_volume_usd REAL NOT NULL DEFAULT 0,
                    realized_pnl_usd REAL NOT NULL DEFAULT 0,
                    realized_loss_usd REAL NOT NULL DEFAULT 0,
                    balance_start_usd REAL,
                    balance_end_usd REAL,
                    balance_loss_usd REAL,
                    points_start REAL,
                    points_end REAL,
                    points_earned REAL,
                    total_points REAL,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (
                        period_start_utc,
                        exchange_id,
                        account_id,
                        wallet_id,
                        market
                    )
                );

                CREATE TABLE IF NOT EXISTS paper_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    period_start_utc TEXT NOT NULL,
                    period_end_utc TEXT NOT NULL,
                    exchange_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    wallet_id TEXT NOT NULL,
                    market TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    decision_reason TEXT NOT NULL,
                    risk_approved INTEGER NOT NULL,
                    risk_reason TEXT NOT NULL,
                    fill_count INTEGER NOT NULL,
                    gross_volume_usd REAL NOT NULL,
                    realized_pnl_usd REAL NOT NULL,
                    realized_loss_usd REAL NOT NULL,
                    points_status TEXT NOT NULL,
                    points_estimate REAL
                );

                CREATE TABLE IF NOT EXISTS live_weekly_market_metrics (
                    period_start_utc TEXT NOT NULL,
                    period_end_utc TEXT NOT NULL,
                    exchange_id TEXT NOT NULL,
                    account_group_key TEXT NOT NULL,
                    wallet_role TEXT NOT NULL,
                    wallet_key TEXT NOT NULL,
                    credential_prefix TEXT NOT NULL,
                    market TEXT NOT NULL,
                    planned_gross_volume_usd REAL NOT NULL DEFAULT 0,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (
                        period_start_utc,
                        exchange_id,
                        account_group_key,
                        wallet_key,
                        market
                    )
                );

                CREATE TABLE IF NOT EXISTS live_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    period_start_utc TEXT NOT NULL,
                    period_end_utc TEXT NOT NULL,
                    exchange_id TEXT NOT NULL,
                    account_group_key TEXT NOT NULL,
                    wallet_role TEXT NOT NULL,
                    wallet_key TEXT NOT NULL,
                    credential_prefix TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    market TEXT NOT NULL,
                    cycle INTEGER NOT NULL,
                    spread_bps REAL NOT NULL,
                    threshold_bps REAL NOT NULL,
                    quantity TEXT NOT NULL,
                    one_side_notional_usd REAL NOT NULL,
                    planned_gross_volume_usd REAL NOT NULL,
                    first_side TEXT NOT NULL,
                    second_side TEXT NOT NULL,
                    environment TEXT,
                    cycle_id TEXT,
                    fee_level TEXT,
                    maker_fee_bps REAL,
                    taker_fee_bps REAL,
                    entry_fee_bps REAL,
                    exit_fee_bps REAL,
                    fee_source TEXT,
                    fee_multiplier REAL,
                    fee_multiplier_expires_at TEXT,
                    live_spread_bps REAL,
                    expected_loss_bps REAL,
                    filled_gross_volume_usd REAL,
                    estimated_fee_usd REAL,
                    estimated_loss_usd REAL,
                    realized_pnl_usd REAL,
                    points_estimate REAL,
                    start_position_count INTEGER,
                    final_position_count INTEGER,
                    start_open_order_count INTEGER,
                    final_open_order_count INTEGER,
                    order_ids TEXT,
                    error_reason TEXT,
                    status TEXT NOT NULL
                );
                """
            )
            self._ensure_live_run_extra_columns(connection)

    def load_budget_state(
        self,
        period: PeriodWindow,
        exchange_id: str,
        account_id: str,
        wallet_id: str,
    ) -> BudgetState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(gross_volume_usd), 0) AS gross_volume_usd,
                    COALESCE(SUM(realized_loss_usd), 0) AS realized_loss_usd
                FROM weekly_market_metrics
                WHERE period_start_utc = ?
                    AND exchange_id = ?
                    AND account_id = ?
                    AND wallet_id = ?
                """,
                (
                    _format_ts(period.start_utc),
                    exchange_id,
                    account_id,
                    wallet_id,
                ),
            ).fetchone()

        return BudgetState(
            period_volume_usd=float(row["gross_volume_usd"]),
            period_realized_loss_usd=float(row["realized_loss_usd"]),
        )

    def record_paper_run(
        self,
        period: PeriodWindow,
        timestamp: datetime,
        exchange_id: str,
        account_id: str,
        wallet_id: str,
        market: str,
        strategy: str,
        decision_reason: str,
        risk_approved: bool,
        risk_reason: str,
        fill_count: int,
        metrics: PerformanceMetrics,
    ) -> None:
        now_text = _format_ts(timestamp)
        period_start = _format_ts(period.start_utc)
        period_end = _format_ts(period.end_utc)

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_runs (
                    timestamp_utc,
                    period_start_utc,
                    period_end_utc,
                    exchange_id,
                    account_id,
                    wallet_id,
                    market,
                    strategy,
                    decision_reason,
                    risk_approved,
                    risk_reason,
                    fill_count,
                    gross_volume_usd,
                    realized_pnl_usd,
                    realized_loss_usd,
                    points_status,
                    points_estimate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_text,
                    period_start,
                    period_end,
                    exchange_id,
                    account_id,
                    wallet_id,
                    market,
                    strategy,
                    decision_reason,
                    int(risk_approved),
                    risk_reason,
                    fill_count,
                    metrics.gross_volume_usd,
                    metrics.realized_pnl_usd,
                    metrics.realized_loss_usd,
                    metrics.points_status,
                    metrics.points_estimate,
                ),
            )

            connection.execute(
                """
                INSERT INTO weekly_market_metrics (
                    period_start_utc,
                    period_end_utc,
                    exchange_id,
                    account_id,
                    wallet_id,
                    market,
                    gross_volume_usd,
                    realized_pnl_usd,
                    realized_loss_usd,
                    run_count,
                    updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT (
                    period_start_utc,
                    exchange_id,
                    account_id,
                    wallet_id,
                    market
                ) DO UPDATE SET
                    period_end_utc = excluded.period_end_utc,
                    gross_volume_usd = gross_volume_usd + excluded.gross_volume_usd,
                    realized_pnl_usd = realized_pnl_usd + excluded.realized_pnl_usd,
                    realized_loss_usd = realized_loss_usd + excluded.realized_loss_usd,
                    run_count = run_count + 1,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    period_start,
                    period_end,
                    exchange_id,
                    account_id,
                    wallet_id,
                    market,
                    metrics.gross_volume_usd,
                    metrics.realized_pnl_usd,
                    metrics.realized_loss_usd,
                    now_text,
                ),
            )

    def weekly_totals(
        self,
        period: PeriodWindow,
        exchange_id: str,
        account_id: str,
        wallet_id: str,
    ) -> WeeklyTotals:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(gross_volume_usd), 0) AS gross_volume_usd,
                    COALESCE(SUM(realized_pnl_usd), 0) AS realized_pnl_usd,
                    COALESCE(SUM(realized_loss_usd), 0) AS realized_loss_usd,
                    COALESCE(SUM(run_count), 0) AS run_count
                FROM weekly_market_metrics
                WHERE period_start_utc = ?
                    AND exchange_id = ?
                    AND account_id = ?
                    AND wallet_id = ?
                """,
                (
                    _format_ts(period.start_utc),
                    exchange_id,
                    account_id,
                    wallet_id,
                ),
            ).fetchone()

        return WeeklyTotals(
            gross_volume_usd=float(row["gross_volume_usd"]),
            realized_pnl_usd=float(row["realized_pnl_usd"]),
            realized_loss_usd=float(row["realized_loss_usd"]),
            run_count=int(row["run_count"]),
        )

    def record_live_round(
        self,
        *,
        period: PeriodWindow,
        timestamp: datetime,
        exchange_id: str,
        account_group_key: str,
        wallet_role: str,
        wallet_key: str,
        credential_prefix: str,
        phase: str,
        market: str,
        cycle: int,
        spread_bps: float,
        threshold_bps: float,
        quantity: str,
        one_side_notional_usd: float,
        planned_gross_volume_usd: float,
        first_side: str,
        second_side: str,
        status: str,
        environment: str = "production",
        cycle_id: str | None = None,
        fee_level: str | None = None,
        maker_fee_bps: float | None = None,
        taker_fee_bps: float | None = None,
        entry_fee_bps: float | None = None,
        exit_fee_bps: float | None = None,
        fee_source: str | None = None,
        fee_multiplier: float | None = None,
        fee_multiplier_expires_at: str | None = None,
        live_spread_bps: float | None = None,
        expected_loss_bps: float | None = None,
        filled_gross_volume_usd: float | None = None,
        estimated_fee_usd: float | None = None,
        estimated_loss_usd: float | None = None,
        realized_pnl_usd: float | None = None,
        points_estimate: float | None = None,
        start_position_count: int | None = None,
        final_position_count: int | None = None,
        start_open_order_count: int | None = None,
        final_open_order_count: int | None = None,
        order_ids: str | None = None,
        error_reason: str | None = None,
    ) -> None:
        now_text = _format_ts(timestamp)
        period_start = _format_ts(period.start_utc)
        period_end = _format_ts(period.end_utc)
        event_cycle_id = cycle_id or f"{phase}:{cycle}"
        event_live_spread_bps = live_spread_bps if live_spread_bps is not None else spread_bps

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO live_runs (
                    timestamp_utc,
                    period_start_utc,
                    period_end_utc,
                    exchange_id,
                    account_group_key,
                    wallet_role,
                    wallet_key,
                    credential_prefix,
                    phase,
                    market,
                    cycle,
                    spread_bps,
                    threshold_bps,
                    quantity,
                    one_side_notional_usd,
                    planned_gross_volume_usd,
                    first_side,
                    second_side,
                    environment,
                    cycle_id,
                    fee_level,
                    maker_fee_bps,
                    taker_fee_bps,
                    entry_fee_bps,
                    exit_fee_bps,
                    fee_source,
                    fee_multiplier,
                    fee_multiplier_expires_at,
                    live_spread_bps,
                    expected_loss_bps,
                    filled_gross_volume_usd,
                    estimated_fee_usd,
                    estimated_loss_usd,
                    realized_pnl_usd,
                    points_estimate,
                    start_position_count,
                    final_position_count,
                    start_open_order_count,
                    final_open_order_count,
                    order_ids,
                    error_reason,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_text,
                    period_start,
                    period_end,
                    exchange_id,
                    account_group_key,
                    wallet_role,
                    wallet_key,
                    credential_prefix,
                    phase,
                    market,
                    cycle,
                    spread_bps,
                    threshold_bps,
                    quantity,
                    one_side_notional_usd,
                    planned_gross_volume_usd,
                    first_side,
                    second_side,
                    environment,
                    event_cycle_id,
                    fee_level,
                    maker_fee_bps,
                    taker_fee_bps,
                    entry_fee_bps,
                    exit_fee_bps,
                    fee_source,
                    fee_multiplier,
                    fee_multiplier_expires_at,
                    event_live_spread_bps,
                    expected_loss_bps,
                    filled_gross_volume_usd,
                    estimated_fee_usd,
                    estimated_loss_usd,
                    realized_pnl_usd,
                    points_estimate,
                    start_position_count,
                    final_position_count,
                    start_open_order_count,
                    final_open_order_count,
                    order_ids,
                    error_reason,
                    status,
                ),
            )

            connection.execute(
                """
                INSERT INTO live_weekly_market_metrics (
                    period_start_utc,
                    period_end_utc,
                    exchange_id,
                    account_group_key,
                    wallet_role,
                    wallet_key,
                    credential_prefix,
                    market,
                    planned_gross_volume_usd,
                    run_count,
                    updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT (
                    period_start_utc,
                    exchange_id,
                    account_group_key,
                    wallet_key,
                    market
                ) DO UPDATE SET
                    period_end_utc = excluded.period_end_utc,
                    wallet_role = excluded.wallet_role,
                    credential_prefix = excluded.credential_prefix,
                    planned_gross_volume_usd =
                        planned_gross_volume_usd + excluded.planned_gross_volume_usd,
                    run_count = run_count + 1,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    period_start,
                    period_end,
                    exchange_id,
                    account_group_key,
                    wallet_role,
                    wallet_key,
                    credential_prefix,
                    market,
                    planned_gross_volume_usd,
                    now_text,
                ),
            )

    def live_weekly_totals(
        self,
        period: PeriodWindow,
        exchange_id: str | None = None,
        account_group_key: str | None = None,
        wallet_key: str | None = None,
        market: str | None = None,
    ) -> LiveWeeklyTotals:
        where, params = self._live_where(period, exchange_id, account_group_key, wallet_key, market)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COALESCE(SUM(planned_gross_volume_usd), 0) AS planned_gross_volume_usd,
                    COALESCE(SUM(run_count), 0) AS run_count
                FROM live_weekly_market_metrics
                WHERE {where}
                """,
                params,
            ).fetchone()

        return LiveWeeklyTotals(
            planned_gross_volume_usd=float(row["planned_gross_volume_usd"]),
            run_count=int(row["run_count"]),
        )

    def live_weekly_market_totals(
        self,
        period: PeriodWindow,
        exchange_id: str | None = None,
        account_group_key: str | None = None,
    ) -> list[LiveWeeklyMarketTotal]:
        where, params = self._live_where(period, exchange_id, account_group_key, None, None)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    exchange_id,
                    account_group_key,
                    wallet_role,
                    wallet_key,
                    credential_prefix,
                    market,
                    planned_gross_volume_usd,
                    run_count
                FROM live_weekly_market_metrics
                WHERE {where}
                ORDER BY exchange_id, account_group_key, wallet_role, market
                """,
                params,
            ).fetchall()

        return [
            LiveWeeklyMarketTotal(
                exchange_id=str(row["exchange_id"]),
                account_group_key=str(row["account_group_key"]),
                wallet_role=str(row["wallet_role"]),
                wallet_key=str(row["wallet_key"]),
                credential_prefix=str(row["credential_prefix"]),
                market=str(row["market"]),
                planned_gross_volume_usd=float(row["planned_gross_volume_usd"]),
                run_count=int(row["run_count"]),
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_live_run_extra_columns(self, connection: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(live_runs)").fetchall()
        }
        for name, column_type in _LIVE_RUN_EXTRA_COLUMNS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE live_runs ADD COLUMN {name} {column_type}")

    def _live_where(
        self,
        period: PeriodWindow,
        exchange_id: str | None,
        account_group_key: str | None,
        wallet_key: str | None,
        market: str | None,
    ) -> tuple[str, tuple[str, ...]]:
        clauses = ["period_start_utc = ?"]
        params: list[str] = [_format_ts(period.start_utc)]
        if exchange_id is not None:
            clauses.append("exchange_id = ?")
            params.append(exchange_id)
        if account_group_key is not None:
            clauses.append("account_group_key = ?")
            params.append(account_group_key)
        if wallet_key is not None:
            clauses.append("wallet_key = ?")
            params.append(wallet_key)
        if market is not None:
            clauses.append("market = ?")
            params.append(market)
        return " AND ".join(clauses), tuple(params)


def _format_ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
