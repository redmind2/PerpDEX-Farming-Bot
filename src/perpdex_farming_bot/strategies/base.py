from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from perpdex_farming_bot.config import BotConfig
from perpdex_farming_bot.models import MarketSnapshot, StrategyDecision


class BaseStrategy(ABC):
    name: str

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.last_run_at: datetime | None = None

    def cooldown_active(self, now: datetime) -> bool:
        if self.last_run_at is None:
            return False
        elapsed = (now - self.last_run_at).total_seconds()
        return elapsed < self.config.cooldown_seconds

    def mark_run(self, now: datetime) -> None:
        self.last_run_at = now

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot, now: datetime) -> StrategyDecision:
        """Return paper order intents, never real exchange orders."""
