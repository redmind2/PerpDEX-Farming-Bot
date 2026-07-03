from __future__ import annotations

from perpdex_farming_bot.config import BotConfig
from perpdex_farming_bot.models import QuoteLevel


def capped_level_quantity(level: QuoteLevel, config: BotConfig) -> float:
    level_fraction_qty = level.size * config.strategy.level_size_fraction
    cap_qty = config.strategy.notional_cap_usd / level.price
    return max(0.0, min(level_fraction_qty, cap_qty))
