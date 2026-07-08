from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable


@dataclass(frozen=True)
class ExecutionEvent:
    exchange: str
    cycle_id: str
    environment: str
    status: str
    account_label: str | None = None
    wallet_label: str | None = None
    market: str | None = None
    fee_level: str | None = None
    maker_fee_bps: Decimal | None = None
    taker_fee_bps: Decimal | None = None
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    fee_source: str | None = None
    fee_multiplier: Decimal | None = None
    fee_multiplier_expires_at: str | None = None
    live_spread_bps: Decimal | None = None
    expected_loss_bps: Decimal | None = None
    planned_gross_volume_usd: Decimal | None = None
    filled_gross_volume_usd: Decimal | None = None
    estimated_fee_usd: Decimal | None = None
    estimated_loss_usd: Decimal | None = None
    realized_pnl_usd: Decimal | None = None
    points_estimate: Decimal | None = None
    start_position_count: int | None = None
    final_position_count: int | None = None
    start_open_order_count: int | None = None
    final_open_order_count: int | None = None
    final_all_flat: bool | None = None
    plan_latency_ms: Decimal | None = None
    entry_sign_latency_ms: Decimal | None = None
    close_sign_latency_ms: Decimal | None = None
    close_prebuild_sign_latency_ms: Decimal | None = None
    entry_post_latency_ms: Decimal | None = None
    close_post_latency_ms: Decimal | None = None
    entry_to_close_submit_gap_ms: Decimal | None = None
    cycle_total_latency_ms: Decimal | None = None
    adapter_submit_elapsed_ms: Decimal | None = None
    matched_trade_count: int | None = None
    matched_trade_gross_usd: Decimal | None = None
    matched_trade_fee_usd_estimate: Decimal | None = None
    order_ids: tuple[str, ...] = ()
    error_reason: str | None = None
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema": "perpdex.execution_event.v1",
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "exchange": self.exchange,
            "account_label": self.account_label,
            "wallet_label": self.wallet_label,
            "market": self.market,
            "cycle_id": self.cycle_id,
            "environment": self.environment,
            "fee_level": self.fee_level,
            "maker_fee_bps": _decimal_or_none(self.maker_fee_bps),
            "taker_fee_bps": _decimal_or_none(self.taker_fee_bps),
            "entry_fee_bps": _decimal_or_none(self.entry_fee_bps),
            "exit_fee_bps": _decimal_or_none(self.exit_fee_bps),
            "fee_source": self.fee_source,
            "fee_multiplier": _decimal_or_none(self.fee_multiplier),
            "fee_multiplier_expires_at": self.fee_multiplier_expires_at,
            "live_spread_bps": _decimal_or_none(self.live_spread_bps),
            "expected_loss_bps": _decimal_or_none(self.expected_loss_bps),
            "planned_gross_volume_usd": _decimal_or_none(self.planned_gross_volume_usd),
            "filled_gross_volume_usd": _decimal_or_none(self.filled_gross_volume_usd),
            "estimated_fee_usd": _decimal_or_none(self.estimated_fee_usd),
            "estimated_loss_usd": _decimal_or_none(self.estimated_loss_usd),
            "realized_pnl_usd": _decimal_or_none(self.realized_pnl_usd),
            "points_estimate": _decimal_or_none(self.points_estimate),
            "start_position_count": self.start_position_count,
            "final_position_count": self.final_position_count,
            "start_open_order_count": self.start_open_order_count,
            "final_open_order_count": self.final_open_order_count,
            "final_all_flat": self.final_all_flat,
            "plan_latency_ms": _decimal_or_none(self.plan_latency_ms),
            "entry_sign_latency_ms": _decimal_or_none(self.entry_sign_latency_ms),
            "close_sign_latency_ms": _decimal_or_none(self.close_sign_latency_ms),
            "close_prebuild_sign_latency_ms": _decimal_or_none(self.close_prebuild_sign_latency_ms),
            "entry_post_latency_ms": _decimal_or_none(self.entry_post_latency_ms),
            "close_post_latency_ms": _decimal_or_none(self.close_post_latency_ms),
            "entry_to_close_submit_gap_ms": _decimal_or_none(self.entry_to_close_submit_gap_ms),
            "cycle_total_latency_ms": _decimal_or_none(self.cycle_total_latency_ms),
            "adapter_submit_elapsed_ms": _decimal_or_none(self.adapter_submit_elapsed_ms),
            "matched_trade_count": self.matched_trade_count,
            "matched_trade_gross_usd": _decimal_or_none(self.matched_trade_gross_usd),
            "matched_trade_fee_usd_estimate": _decimal_or_none(self.matched_trade_fee_usd_estimate),
            "order_ids": [_sanitize_identifier(item) for item in self.order_ids],
            "error_reason": self.error_reason,
            "status": self.status,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def estimate_roundtrip_fee_usd(
    *,
    entry_notional_usd: Decimal,
    exit_notional_usd: Decimal,
    entry_fee_bps: Decimal | None,
    exit_fee_bps: Decimal | None,
) -> Decimal | None:
    if entry_fee_bps is None or exit_fee_bps is None:
        return None
    return (entry_notional_usd * entry_fee_bps / Decimal("10000")) + (
        exit_notional_usd * exit_fee_bps / Decimal("10000")
    )


def estimate_loss_usd(
    *,
    planned_gross_volume_usd: Decimal,
    expected_loss_bps: Decimal | None,
) -> Decimal | None:
    if expected_loss_bps is None:
        return None
    return planned_gross_volume_usd * expected_loss_bps / Decimal("10000")


def emit_execution_event(event: ExecutionEvent, emit: Callable[[str], None] = print) -> None:
    emit(f"execution_event_json={event.to_json()}")


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _sanitize_identifier(value: str) -> str:
    return re.sub(r"0x[a-fA-F0-9]{40,}", "0x[redacted]", value)
