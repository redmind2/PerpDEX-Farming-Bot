from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from perpdex_farming_bot.security.secrets import assert_no_plaintext_secrets


class ConfigValidationError(ValueError):
    """Raised when config structure would allow unsafe orchestration."""


@dataclass(frozen=True)
class ExecutionContextConfig:
    exchange_id: str
    account_id: str
    wallet_id: str
    market: str


@dataclass(frozen=True)
class ExchangeConfig:
    exchange_id: str
    connector: str
    points_source: str


@dataclass(frozen=True)
class AccountConfig:
    account_id: str
    exchange_id: str
    wallet_id: str
    credential_env_prefix: str


@dataclass(frozen=True)
class StrategyAssignmentConfig:
    assignment_id: str
    exchange_id: str
    account_id: str
    wallet_id: str
    paired_account_id: str | None
    paired_wallet_id: str | None
    market: str
    strategy: str
    enabled: bool
    data_hub_symbol: str | None = None


@dataclass(frozen=True)
class StrategyConfig:
    notional_cap_usd: float
    level_size_fraction: float
    spread_vs_average_ratio: float
    max_spread_bps: float
    max_gap_to_spread_ratio: float
    trade_tape_window_seconds: int
    min_repeating_trade_count: int
    delta_neutral_total_collateral_usd: float
    delta_neutral_notional_cap_usd: float
    delta_neutral_notional_pct_of_collateral: float | None
    delta_neutral_max_pair_position_usd: float
    delta_neutral_max_pair_position_pct_of_collateral: float | None
    delta_neutral_min_hold_seconds: int
    delta_neutral_exit_after_seconds: int


@dataclass(frozen=True)
class RiskConfig:
    max_order_notional_usd: float
    max_position_notional_usd: float
    max_daily_loss_usd: float
    max_orders_per_run: int
    max_price_age_seconds: int
    kill_switch_enabled: bool


@dataclass(frozen=True)
class BudgetConfig:
    period_name: str
    period_start_weekday_utc: str
    max_period_loss_usd: float
    max_period_volume_usd: float
    max_round_loss_usd: float
    max_round_volume_usd: float


@dataclass(frozen=True)
class PointsConfig:
    enabled: bool
    source: str
    assumed_points_per_usd_volume: float | None


@dataclass(frozen=True)
class ComplianceConfig:
    allow_market_taker_round_trip: bool
    prohibit_self_cross: bool
    prohibit_wash_trading: bool
    require_external_counterparty: bool
    notes: str


@dataclass(frozen=True)
class SecretsConfig:
    env_file: str
    required_env_vars: list[str]
    forbidden_plaintext_keys: list[str]


@dataclass(frozen=True)
class BotConfig:
    mode: str
    market: str
    execution_context: ExecutionContextConfig
    exchanges: list[ExchangeConfig]
    accounts: list[AccountConfig]
    strategy_assignments: list[StrategyAssignmentConfig]
    cooldown_seconds: int
    strategy: StrategyConfig
    risk: RiskConfig
    budget: BudgetConfig
    points: PointsConfig
    compliance: ComplianceConfig
    secrets: SecretsConfig


def load_config(path: str | Path) -> BotConfig:
    raw: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    assert_no_plaintext_secrets(raw)
    config = BotConfig(
        mode=raw["mode"],
        market=raw["market"],
        execution_context=ExecutionContextConfig(**raw["execution_context"]),
        exchanges=[ExchangeConfig(**item) for item in raw["exchanges"]],
        accounts=[AccountConfig(**item) for item in raw["accounts"]],
        strategy_assignments=[StrategyAssignmentConfig(**item) for item in raw["strategy_assignments"]],
        cooldown_seconds=int(raw["cooldown_seconds"]),
        strategy=StrategyConfig(**raw["strategy"]),
        risk=RiskConfig(**raw["risk"]),
        budget=BudgetConfig(**raw["budget"]),
        points=PointsConfig(**raw["points"]),
        compliance=ComplianceConfig(**raw["compliance"]),
        secrets=SecretsConfig(**raw["secrets"]),
    )
    _validate_strategy_assignments(config.strategy_assignments)
    _validate_strategy_config(config.strategy)
    return config


def _validate_strategy_config(strategy: StrategyConfig) -> None:
    if strategy.level_size_fraction < 0 or strategy.level_size_fraction > 1:
        raise ConfigValidationError("level_size_fraction must be between 0 and 1")

    if strategy.max_spread_bps < 0:
        raise ConfigValidationError("max_spread_bps must be zero or greater")

    if strategy.delta_neutral_total_collateral_usd <= 0:
        raise ConfigValidationError("delta_neutral_total_collateral_usd must be greater than zero")

    _validate_optional_pct(
        "delta_neutral_notional_pct_of_collateral",
        strategy.delta_neutral_notional_pct_of_collateral,
    )
    _validate_optional_pct(
        "delta_neutral_max_pair_position_pct_of_collateral",
        strategy.delta_neutral_max_pair_position_pct_of_collateral,
    )


def _validate_optional_pct(name: str, value: float | None) -> None:
    if value is None:
        return
    if value < 0 or value > 1:
        raise ConfigValidationError(f"{name} must be between 0 and 1")


def _validate_strategy_assignments(assignments: list[StrategyAssignmentConfig]) -> None:
    active_market_owners: dict[tuple[str, str], str] = {}

    for assignment in assignments:
        if not assignment.enabled:
            continue

        market_key = (assignment.exchange_id, assignment.market)
        existing_owner = active_market_owners.get(market_key)
        if existing_owner is not None:
            raise ConfigValidationError(
                "Active strategy assignments must not overlap on the same exchange market: "
                f"{existing_owner} and {assignment.assignment_id}"
            )
        active_market_owners[market_key] = assignment.assignment_id
