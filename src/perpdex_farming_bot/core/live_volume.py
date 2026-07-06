from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from perpdex_farming_bot.exchanges.base import ExchangeAdapter, PairedRoundtripResult


@dataclass(frozen=True)
class RoundtripPlan:
    market: str
    instrument_id: int
    buy_price: Decimal
    sell_price: Decimal
    buy_size: Decimal
    sell_size: Decimal
    planned_gross_volume_usd: Decimal
    first_side: str = "BUY"
    second_side: str = "SELL"
    reason: str = "selected"


@dataclass(frozen=True)
class VolumeRunConfig:
    target_gross_volume_usd: Decimal
    max_cycles: int
    min_entry_delay_seconds: float
    allow_existing_positions: bool = False


@dataclass(frozen=True)
class VolumeRunResult:
    status: str
    planned_gross_volume_usd: Decimal
    cycles: int
    results: tuple[PairedRoundtripResult, ...]


def run_paired_volume(
    *,
    adapter: ExchangeAdapter,
    config: VolumeRunConfig,
    select_plan: Callable[[Decimal], RoundtripPlan | None],
    emit: Callable[[str], None] = print,
) -> VolumeRunResult:
    live_gross_volume = Decimal("0")
    results: list[PairedRoundtripResult] = []

    start_positions = adapter.list_positions()
    emit(f"start_position_count={len(start_positions)}")
    if start_positions and not config.allow_existing_positions:
        emit("live_aborted=existing_positions_detected")
        return VolumeRunResult("existing_positions_detected", live_gross_volume, 0, tuple(results))

    for cycle in range(1, config.max_cycles + 1):
        if live_gross_volume >= config.target_gross_volume_usd:
            status = f"target_reached:{live_gross_volume:.4f}>={config.target_gross_volume_usd:.4f}"
            emit(f"stop_reason={status}")
            return VolumeRunResult(status, live_gross_volume, cycle - 1, tuple(results))

        if cycle > 1 and config.min_entry_delay_seconds:
            emit(f"cycle={cycle} delay_seconds={config.min_entry_delay_seconds}")
            time.sleep(config.min_entry_delay_seconds)

        positions = adapter.list_positions()
        if positions:
            emit(f"cycle={cycle} live_aborted=position_detected_before_cycle")
            emit("position_markets=" + ",".join(sorted(position.market for position in positions)))
            return VolumeRunResult("position_detected_before_cycle", live_gross_volume, cycle - 1, tuple(results))

        remaining = config.target_gross_volume_usd - live_gross_volume
        plan = select_plan(remaining)
        if plan is None:
            emit(f"cycle={cycle} live_idle=no_eligible_market")
            continue

        if plan.planned_gross_volume_usd <= 0:
            emit(f"cycle={cycle} live_idle=planned_gross_zero")
            continue
        if live_gross_volume + plan.planned_gross_volume_usd > config.target_gross_volume_usd:
            status = (
                "target_cap_would_be_exceeded:"
                f"{live_gross_volume:.4f}+{plan.planned_gross_volume_usd:.4f}>{config.target_gross_volume_usd:.4f}"
            )
            emit(f"cycle={cycle} live_idle=target_cap_would_be_exceeded")
            emit(f"stop_reason={status}")
            return VolumeRunResult(status, live_gross_volume, cycle - 1, tuple(results))

        emit(
            f"cycle={cycle} selected_market={plan.market} "
            f"buy_size={plan.buy_size} sell_size={plan.sell_size} "
            f"planned_gross_volume_usd={plan.planned_gross_volume_usd:.4f}"
        )
        emit(f"cycle={cycle} live_submit=True")
        result = execute_roundtrip_plan(adapter, plan)
        results.append(result)
        emit(f"cycle={cycle} adapter_status={result.status}")
        if not result.success:
            emit(f"stop_reason=adapter_error_cycle_{cycle}")
            return VolumeRunResult("adapter_error", live_gross_volume, cycle, tuple(results))

        residual_positions = adapter.list_positions()
        if residual_positions:
            emit(f"cycle={cycle} residual_position_detected=True")
            emit("residual_position_markets=" + ",".join(sorted(position.market for position in residual_positions)))
            return VolumeRunResult("residual_position_detected", live_gross_volume, cycle, tuple(results))

        live_gross_volume += plan.planned_gross_volume_usd
        emit(f"cycle={cycle} live_total_gross_volume_usd={live_gross_volume:.4f}")

    emit(f"stop_reason=max_cycles_reached:{config.max_cycles}")
    return VolumeRunResult("max_cycles_reached", live_gross_volume, config.max_cycles, tuple(results))


def execute_roundtrip_plan(adapter: ExchangeAdapter, plan: RoundtripPlan) -> PairedRoundtripResult:
    return adapter.execute_paired_notional_roundtrip(
        market=plan.market,
        instrument_id=plan.instrument_id,
        buy_price=plan.buy_price,
        sell_price=plan.sell_price,
        buy_size=plan.buy_size,
        sell_size=plan.sell_size,
        planned_gross_volume_usd=plan.planned_gross_volume_usd,
        first_side=plan.first_side,
        second_side=plan.second_side,
    )
