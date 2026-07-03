from __future__ import annotations

from dataclasses import dataclass

from perpdex_farming_bot.brokers import PaperFill
from perpdex_farming_bot.config import BotConfig
from perpdex_farming_bot.models import MarketSnapshot, Side


@dataclass(frozen=True)
class PerformanceMetrics:
    gross_volume_usd: float
    realized_pnl_usd: float
    realized_loss_usd: float
    loss_per_volume: float | None
    points_estimate: float | None
    points_status: str
    points_per_volume: float | None
    points_per_loss: float | None


def calculate_metrics(
    config: BotConfig,
    snapshot: MarketSnapshot,
    fills: tuple[PaperFill, ...],
) -> PerformanceMetrics:
    gross_volume = sum(fill.notional_usd for fill in fills)
    cash = 0.0
    position_qty = 0.0

    for fill in fills:
        if fill.side is Side.BUY:
            cash -= fill.notional_usd
            position_qty += fill.quantity
        else:
            cash += fill.notional_usd
            position_qty -= fill.quantity

    mark_value = position_qty * snapshot.mid_price
    realized_pnl = cash + mark_value
    realized_loss = max(0.0, -realized_pnl)
    loss_per_volume = _safe_divide(realized_loss, gross_volume)

    points_estimate, points_status = _estimate_points(config, gross_volume)
    points_per_volume = None
    points_per_loss = None
    if points_estimate is not None:
        points_per_volume = _safe_divide(points_estimate, gross_volume)
        points_per_loss = _safe_divide(points_estimate, realized_loss)

    return PerformanceMetrics(
        gross_volume_usd=gross_volume,
        realized_pnl_usd=realized_pnl,
        realized_loss_usd=realized_loss,
        loss_per_volume=loss_per_volume,
        points_estimate=points_estimate,
        points_status=points_status,
        points_per_volume=points_per_volume,
        points_per_loss=points_per_loss,
    )


def _estimate_points(config: BotConfig, gross_volume: float) -> tuple[float | None, str]:
    if not config.points.enabled:
        return None, "disabled"

    if config.points.assumed_points_per_usd_volume is not None:
        return gross_volume * config.points.assumed_points_per_usd_volume, "estimated_from_config"

    # Later this can call an exchange-specific points API adapter if one exists.
    if config.points.source == "unavailable":
        return None, "unavailable"

    return None, f"unsupported_source:{config.points.source}"


def _safe_divide(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator

