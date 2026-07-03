from __future__ import annotations

from dataclasses import dataclass

from perpdex_farming_bot.budget import BudgetState
from perpdex_farming_bot.config import BotConfig
from perpdex_farming_bot.models import MarketSnapshot, OrderIntent


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    approved_intents: tuple[OrderIntent, ...]


class RiskEngine:
    _FLOAT_TOLERANCE = 1e-9

    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def review(
        self,
        snapshot: MarketSnapshot,
        intents: tuple[OrderIntent, ...],
        budget_state: BudgetState | None = None,
    ) -> RiskDecision:
        budget_state = budget_state or BudgetState()

        if self.config.mode != "paper":
            return RiskDecision(False, "only paper mode is allowed in this skeleton", ())

        if self.config.risk.kill_switch_enabled:
            return RiskDecision(False, "kill switch is enabled", ())

        if snapshot.age_seconds > self.config.risk.max_price_age_seconds:
            return RiskDecision(False, "market snapshot is stale", ())

        if len(intents) > self.config.risk.max_orders_per_run:
            return RiskDecision(False, "too many order intents for one run", ())

        if budget_state.period_realized_loss_usd >= self.config.budget.max_period_loss_usd:
            return RiskDecision(False, "period loss budget is already exhausted", ())

        if budget_state.period_volume_usd >= self.config.budget.max_period_volume_usd:
            return RiskDecision(False, "period volume budget is already exhausted", ())

        round_volume = sum(intent.notional_usd for intent in intents)
        if round_volume - self.config.budget.max_round_volume_usd > self._FLOAT_TOLERANCE:
            return RiskDecision(False, "round volume exceeds budget limit", ())

        projected_period_volume = budget_state.period_volume_usd + round_volume
        if projected_period_volume - self.config.budget.max_period_volume_usd > self._FLOAT_TOLERANCE:
            return RiskDecision(False, "projected period volume exceeds budget limit", ())

        approved: list[OrderIntent] = []
        for intent in intents:
            excess = intent.notional_usd - self.config.risk.max_order_notional_usd
            if excess > self._FLOAT_TOLERANCE:
                return RiskDecision(
                    False,
                    f"order notional {intent.notional_usd:.2f} exceeds risk limit",
                    tuple(approved),
                )
            approved.append(intent)

        return RiskDecision(True, "approved for paper broker only", tuple(approved))
