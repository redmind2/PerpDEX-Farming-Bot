from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json

from perpdex_farming_bot.connectors.risex_readonly import read_only_get_json
from perpdex_farming_bot.core.execution_cost import MarketFee


@dataclass(frozen=True)
class RisexMarketFeeMetadata:
    market: str
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    source: str = "market_metadata"


@dataclass(frozen=True)
class RisexFeeOverride:
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
class RisexAccountFee:
    fee_level: str | None
    maker_fee_bps: Decimal | None
    taker_fee_bps: Decimal
    source: str = "account_trade_history_taker_fee"


@dataclass(frozen=True)
class RisexFeeProvider:
    override_by_market: dict[str, RisexFeeOverride]
    metadata_by_market: dict[str, RisexMarketFeeMetadata]
    account_fee: RisexAccountFee | None = None

    def fee_for_market(self, market: str) -> MarketFee:
        override = self.override_by_market.get(market)
        slippage_buffer = override.slippage_buffer_bps if override is not None else Decimal("0")

        if self.account_fee is not None:
            multiplier, source = _active_fee_multiplier(override, "account_taker_fee")
            return MarketFee(
                entry_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                exit_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                slippage_buffer_bps=slippage_buffer,
                source=source,
            )

        metadata = self.metadata_by_market.get(market)
        if metadata is not None and metadata.entry_fee_bps is not None and metadata.exit_fee_bps is not None:
            return MarketFee(
                entry_fee_bps=metadata.entry_fee_bps,
                exit_fee_bps=metadata.exit_fee_bps,
                slippage_buffer_bps=slippage_buffer,
                source=metadata.source,
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


def load_risex_account_fee_from_trade_history(
    api_endpoint: str,
    account: str,
    timeout_seconds: float,
    *,
    market_id: int | None = None,
    limit: int = 100,
) -> RisexAccountFee:
    query: dict[str, object] = {
        "account": account,
        "limit": limit,
        "page": 1,
        "sorted_by": "-time",
    }
    if market_id is not None:
        query["market_id"] = market_id
    payload = read_only_get_json(
        api_endpoint,
        "/v1/trade-history",
        query,
        timeout_seconds,
        private_readonly=True,
    )
    trades = _trades_payload(payload)
    fee_bps_values = [
        _trade_fee_bps(trade)
        for trade in trades
        if str(trade.get("liquidity_indicator") or "").upper() == "TAKER"
    ]
    fee_bps_values = [fee for fee in fee_bps_values if fee is not None]
    if not fee_bps_values:
        raise ValueError("RiseX trade-history did not include taker fee samples")

    taker_fee_bps = _median(fee_bps_values)
    sample_scope = f"market_id={market_id}" if market_id is not None else "all_markets"
    return RisexAccountFee(
        fee_level=f"inferred_from_recent_trades:{sample_scope}:samples={len(fee_bps_values)}",
        maker_fee_bps=None,
        taker_fee_bps=taker_fee_bps,
    )


def risex_market_fee_metadata_from_markets(payload: object) -> dict[str, RisexMarketFeeMetadata]:
    result: dict[str, RisexMarketFeeMetadata] = {}
    for market in _walk_market_objects(payload):
        market_id = market.get("market_id")
        if market_id is None:
            continue
        entry_fee, exit_fee = _metadata_fee_bps(market)
        config = market.get("config") if isinstance(market.get("config"), dict) else {}
        config_entry_fee, config_exit_fee = _metadata_fee_bps(config)
        if entry_fee is None:
            entry_fee = config_entry_fee
        if exit_fee is None:
            exit_fee = config_exit_fee
        if entry_fee is None and exit_fee is None:
            continue
        market_key = str(market_id)
        result[market_key] = RisexMarketFeeMetadata(
            market=market_key,
            entry_fee_bps=entry_fee,
            exit_fee_bps=exit_fee,
        )
    return result


def risex_fee_overrides_from_config(path: str | Path | None) -> dict[str, RisexFeeOverride]:
    if path is None:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    markets = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(markets, list):
        return {}

    result: dict[str, RisexFeeOverride] = {}
    for item in markets:
        if not isinstance(item, dict):
            continue
        market = item.get("market_id", item.get("market", item.get("symbol")))
        if market is None or market == "":
            continue
        market_key = str(market)
        result[market_key] = RisexFeeOverride(
            market=market_key,
            entry_fee_bps=_optional_decimal_field(item, ("entry_fee_bps", "entryFeeBps")),
            exit_fee_bps=_optional_decimal_field(item, ("exit_fee_bps", "exitFeeBps")),
            fee_multiplier=Decimal(str(item.get("fee_multiplier", "1"))),
            fee_multiplier_expires_at=_optional_datetime_field(item, "fee_multiplier_expires_at"),
            slippage_buffer_bps=Decimal(str(item.get("slippage_buffer_bps", "0"))),
        )
    return result


def _trades_payload(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        raw = data.get("trades")
    else:
        raw = payload.get("trades")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _trade_fee_bps(trade: dict[str, object]) -> Decimal | None:
    fee = _optional_decimal_field(trade, ("fee", "fee_usd", "feeUsd"))
    price = _optional_decimal_field(trade, ("price",))
    size = _optional_decimal_field(trade, ("size", "quantity", "qty"))
    if fee is None or price is None or size is None:
        return None
    notional = abs(price * size)
    if notional <= 0:
        return None
    return abs(fee) / notional * Decimal("10000")


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


def _walk_market_objects(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("markets", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    for key in ("markets", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if "market_id" in payload:
        return [payload]
    return []


def _metadata_fee_bps(payload: dict[str, object]) -> tuple[Decimal | None, Decimal | None]:
    entry_fee = _optional_decimal_field(payload, ("entry_fee_bps", "entryFeeBps"))
    exit_fee = _optional_decimal_field(payload, ("exit_fee_bps", "exitFeeBps"))
    taker_fee_bps = _optional_decimal_field(
        payload,
        ("taker_fee_bps", "takerFeeBps", "perp_taker_fee_bps", "perpTakerFeeBps"),
    )
    taker_fee_rate = _optional_decimal_field(
        payload,
        ("taker_fee_rate", "takerFeeRate", "perp_taker_fee_rate", "perpTakerFeeRate"),
    )
    if taker_fee_bps is None and taker_fee_rate is not None:
        taker_fee_bps = _fee_rate_to_bps(taker_fee_rate)
    if entry_fee is None:
        entry_fee = taker_fee_bps
    if exit_fee is None:
        exit_fee = taker_fee_bps
    return entry_fee, exit_fee


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


def _fee_rate_to_bps(rate: Decimal) -> Decimal:
    return rate * Decimal("10000")


def _active_fee_multiplier(
    override: RisexFeeOverride | None,
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
