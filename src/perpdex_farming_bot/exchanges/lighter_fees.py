from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json

from perpdex_farming_bot.connectors.lighter_readonly import read_only_get_json
from perpdex_farming_bot.core.execution_cost import MarketFee
from perpdex_farming_bot.marketdata.lighter import (
    LighterMarketMetadata,
    lighter_market_metadata_from_order_books,
)


@dataclass(frozen=True)
class LighterFeeOverride:
    market: str
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    fee_multiplier: Decimal = Decimal("1")
    fee_multiplier_expires_at: datetime | None = None
    slippage_buffer_bps: Decimal = Decimal("0")
    source: str = "config_exact_override"

    @property
    def has_exact_override(self) -> bool:
        return self.entry_fee_bps is not None and self.exit_fee_bps is not None


@dataclass(frozen=True)
class LighterFeeProvider:
    override_by_market: dict[str, LighterFeeOverride]
    metadata_by_market: dict[str, LighterMarketMetadata]

    def fee_for_market(self, market: str) -> MarketFee:
        override = self.override_by_market.get(market)
        slippage_buffer = override.slippage_buffer_bps if override is not None else Decimal("0")

        metadata = self.metadata_by_market.get(market)
        if metadata is not None and metadata.taker_fee_percent is not None:
            multiplier, source = _active_fee_multiplier(override, "market_metadata_percentage_taker_fee")
            taker_fee_bps = metadata.taker_fee_percent * Decimal("100") * multiplier
            return MarketFee(
                entry_fee_bps=taker_fee_bps,
                exit_fee_bps=taker_fee_bps,
                slippage_buffer_bps=slippage_buffer,
                source=source,
            )

        if override is not None and override.has_exact_override:
            return MarketFee(
                entry_fee_bps=override.entry_fee_bps,
                exit_fee_bps=override.exit_fee_bps,
                slippage_buffer_bps=slippage_buffer,
                source=override.source,
            )

        return MarketFee(
            entry_fee_bps=None,
            exit_fee_bps=None,
            slippage_buffer_bps=slippage_buffer,
            source="fee_unknown",
        )


def load_lighter_market_fee_metadata(
    api_endpoint: str,
    timeout_seconds: float,
    *,
    market_id: int | None = None,
    market_filter: str = "perp",
) -> dict[str, LighterMarketMetadata]:
    query: dict[str, object] = {"filter": market_filter}
    if market_id is not None:
        query["market_id"] = market_id
    payload = read_only_get_json(api_endpoint, "/api/v1/orderBooks", query, timeout_seconds)
    return lighter_market_metadata_from_order_books(payload)


def lighter_fee_overrides_from_config(path: str | Path | None) -> dict[str, LighterFeeOverride]:
    if path is None:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    markets = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(markets, list):
        return {}

    result: dict[str, LighterFeeOverride] = {}
    for item in markets:
        if not isinstance(item, dict):
            continue
        market = item.get("market_id", item.get("market", item.get("symbol")))
        if market is None or market == "":
            continue
        market_key = str(market)
        result[market_key] = LighterFeeOverride(
            market=market_key,
            entry_fee_bps=_optional_decimal_field(item, ("entry_fee_bps", "entryFeeBps")),
            exit_fee_bps=_optional_decimal_field(item, ("exit_fee_bps", "exitFeeBps")),
            fee_multiplier=Decimal(str(item.get("fee_multiplier", "1"))),
            fee_multiplier_expires_at=_optional_datetime_field(item, "fee_multiplier_expires_at"),
            slippage_buffer_bps=Decimal(str(item.get("slippage_buffer_bps", "0"))),
        )
    return result


def _optional_decimal_field(payload: dict[str, object], names: tuple[str, ...]) -> Decimal | None:
    for name in names:
        value = payload.get(name)
        if value is None or value == "":
            continue
        return Decimal(str(value))
    return None


def _optional_datetime_field(payload: dict[str, object], name: str) -> datetime | None:
    value = payload.get(name)
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _active_fee_multiplier(
    override: LighterFeeOverride | None,
    base_source: str,
) -> tuple[Decimal, str]:
    if override is None or override.fee_multiplier == Decimal("1"):
        return Decimal("1"), base_source
    if override.fee_multiplier_expires_at is None:
        return Decimal("1"), f"{base_source}_config_multiplier_missing_expiry_ignored"

    expires_at = override.fee_multiplier_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc):
        return Decimal("1"), f"{base_source}_config_multiplier_expired_ignored"
    return override.fee_multiplier, f"{base_source}_config_multiplier"
