from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone


WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class BudgetState:
    period_volume_usd: float = 0.0
    period_realized_loss_usd: float = 0.0

    @property
    def period_realized_pnl_usd(self) -> float:
        return -self.period_realized_loss_usd


@dataclass(frozen=True)
class PeriodWindow:
    start_utc: datetime
    end_utc: datetime

    @property
    def label(self) -> str:
        return self.start_utc.strftime("%Y-%m-%d")


def current_weekly_window(now: datetime, start_weekday_utc: str) -> PeriodWindow:
    weekday = _parse_weekday(start_weekday_utc)
    now_utc = _ensure_utc(now)
    today_midnight = datetime.combine(now_utc.date(), time.min, tzinfo=timezone.utc)
    days_since_start = (today_midnight.weekday() - weekday) % 7
    start = today_midnight - timedelta(days=days_since_start)
    end = start + timedelta(days=7)
    return PeriodWindow(start, end)


def _parse_weekday(value: str) -> int:
    normalized = value.strip().lower()
    if normalized.isdigit():
        index = int(normalized)
        if 0 <= index <= 6:
            return index
    if normalized in WEEKDAY_INDEX:
        return WEEKDAY_INDEX[normalized]
    raise ValueError("period_start_weekday_utc must be monday..sunday or 0..6")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
