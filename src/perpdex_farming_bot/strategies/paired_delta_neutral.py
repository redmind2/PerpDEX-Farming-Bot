from __future__ import annotations

from datetime import datetime

from perpdex_farming_bot.config import BotConfig
from perpdex_farming_bot.config import StrategyAssignmentConfig
from perpdex_farming_bot.models import (
    MarketSnapshot,
    OrderIntent,
    OrderType,
    Side,
    StrategyDecision,
    TimeInForce,
)
from perpdex_farming_bot.strategies.base import BaseStrategy


class PairedDeltaNeutralStrategy(BaseStrategy):
    name = "paired-delta-neutral"

    def __init__(self, config: BotConfig) -> None:
        super().__init__(config)
        self.phase = "entry"
        self.held_seconds = 0
        self.current_pair_position_usd = 0.0

    def set_runtime_state(
        self,
        phase: str,
        held_seconds: int,
        current_pair_position_usd: float,
    ) -> None:
        self.phase = phase
        self.held_seconds = held_seconds
        self.current_pair_position_usd = current_pair_position_usd

    def evaluate(self, snapshot: MarketSnapshot, now: datetime) -> StrategyDecision:
        if self.cooldown_active(now):
            return StrategyDecision(self.name, True, "cooldown active")

        assignment = self._assignment()
        if assignment is None:
            return StrategyDecision(self.name, True, "paired-delta-neutral assignment is missing")

        max_spread = snapshot.average_spread_bps * self.config.strategy.spread_vs_average_ratio
        if snapshot.spread_bps > max_spread:
            return StrategyDecision(
                self.name,
                True,
                f"spread {snapshot.spread_bps:.4f} bps is above threshold {max_spread:.4f} bps",
            )

        if self.phase == "exit":
            return self._exit(snapshot, now, assignment)
        return self._entry(snapshot, now, assignment)

    def _entry(
        self,
        snapshot: MarketSnapshot,
        now: datetime,
        assignment: StrategyAssignmentConfig,
    ) -> StrategyDecision:
        remaining_pair_capacity = (
            self._max_pair_position_usd()
            - self.current_pair_position_usd
        )
        notional = min(
            self._round_notional_usd(),
            remaining_pair_capacity,
        )
        if notional <= 0:
            return StrategyDecision(self.name, True, "paired position cap is already full")

        self.mark_run(now)
        intents = (
            self._intent(
                assignment=assignment,
                account_id=assignment.account_id,
                wallet_id=assignment.wallet_id,
                side=Side.BUY,
                quantity=notional / snapshot.best_ask.price,
                reference_price=snapshot.best_ask.price,
                reduce_only=False,
                phase="entry_long",
            ),
            self._intent(
                assignment=assignment,
                account_id=assignment.paired_account_id or "",
                wallet_id=assignment.paired_wallet_id or "",
                side=Side.SELL,
                quantity=notional / snapshot.best_bid.price,
                reference_price=snapshot.best_bid.price,
                reduce_only=False,
                phase="entry_short",
            ),
        )
        return StrategyDecision(self.name, False, "paper delta-neutral entry intents created", intents)

    def _exit(
        self,
        snapshot: MarketSnapshot,
        now: datetime,
        assignment: StrategyAssignmentConfig,
    ) -> StrategyDecision:
        if self.held_seconds < self.config.strategy.delta_neutral_min_hold_seconds:
            return StrategyDecision(self.name, True, "minimum hold time is not reached")

        notional = min(
            self._round_notional_usd(),
            max(self.current_pair_position_usd, 0.0),
        )
        if notional <= 0:
            return StrategyDecision(self.name, True, "no paired position to close")

        self.mark_run(now)
        intents = (
            self._intent(
                assignment=assignment,
                account_id=assignment.account_id,
                wallet_id=assignment.wallet_id,
                side=Side.SELL,
                quantity=notional / snapshot.best_bid.price,
                reference_price=snapshot.best_bid.price,
                reduce_only=True,
                phase="exit_long",
            ),
            self._intent(
                assignment=assignment,
                account_id=assignment.paired_account_id or "",
                wallet_id=assignment.paired_wallet_id or "",
                side=Side.BUY,
                quantity=notional / snapshot.best_ask.price,
                reference_price=snapshot.best_ask.price,
                reduce_only=True,
                phase="exit_short",
            ),
        )
        return StrategyDecision(self.name, False, "paper delta-neutral exit intents created", intents)

    def _intent(
        self,
        assignment: StrategyAssignmentConfig,
        account_id: str,
        wallet_id: str,
        side: Side,
        quantity: float,
        reference_price: float,
        reduce_only: bool,
        phase: str,
    ) -> OrderIntent:
        return OrderIntent(
            strategy_name=self.name,
            exchange_id=assignment.exchange_id,
            account_id=account_id,
            wallet_id=wallet_id,
            market=assignment.market,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
            metadata={
                "reference_price": reference_price,
                "paper_only": True,
                "paired_phase": phase,
                "delta_neutral_pair": True,
                "round_notional_usd": quantity * reference_price,
                "max_pair_position_usd": self._max_pair_position_usd(),
                "total_collateral_usd": self.config.strategy.delta_neutral_total_collateral_usd,
            },
        )

    def _round_notional_usd(self) -> float:
        return self._smaller_configured_notional(
            fixed_usd=self.config.strategy.delta_neutral_notional_cap_usd,
            pct_of_collateral=self.config.strategy.delta_neutral_notional_pct_of_collateral,
        )

    def _max_pair_position_usd(self) -> float:
        return self._smaller_configured_notional(
            fixed_usd=self.config.strategy.delta_neutral_max_pair_position_usd,
            pct_of_collateral=self.config.strategy.delta_neutral_max_pair_position_pct_of_collateral,
        )

    def _smaller_configured_notional(
        self,
        fixed_usd: float,
        pct_of_collateral: float | None,
    ) -> float:
        candidates = [fixed_usd]
        if pct_of_collateral is not None:
            candidates.append(self.config.strategy.delta_neutral_total_collateral_usd * pct_of_collateral)
        return max(0.0, min(candidates))

    def _assignment(self) -> StrategyAssignmentConfig | None:
        for assignment in self.config.strategy_assignments:
            if assignment.strategy != self.name:
                continue
            if assignment.paired_account_id and assignment.paired_wallet_id:
                return assignment
        return None
