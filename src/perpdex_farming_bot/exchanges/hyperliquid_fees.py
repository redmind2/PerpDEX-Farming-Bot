from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from perpdex_farming_bot.connectors.hyperliquid_readonly import info_post_json
from perpdex_farming_bot.core.execution_cost import MarketFee


@dataclass(frozen=True)
class HyperliquidMarketFeeMetadata:
    market: str
    maker_fee_bps: Decimal | None = None
    taker_fee_bps: Decimal | None = None
    source: str = "market_metadata"


@dataclass(frozen=True)
class HyperliquidFeeOverride:
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
class HyperliquidAccountFee:
    fee_level: str | None
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    source: str = "account_api_user_fees"


@dataclass(frozen=True)
class HyperliquidFeeProvider:
    metadata_by_market: dict[str, HyperliquidMarketFeeMetadata]
    override_by_market: dict[str, HyperliquidFeeOverride]
    account_fee: HyperliquidAccountFee | None = None

    def fee_for_market(self, market: str) -> MarketFee:
        override = self.override_by_market.get(market)
        slippage_buffer = override.slippage_buffer_bps if override is not None else Decimal("0")

        if self.account_fee is not None:
            multiplier, source = _active_fee_multiplier(override, "account_user_fees_taker_fee")
            return MarketFee(
                entry_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                exit_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                slippage_buffer_bps=slippage_buffer,
                source=source,
            )

        metadata = self.metadata_by_market.get(market)
        if metadata is not None and metadata.taker_fee_bps is not None:
            multiplier, source = _active_fee_multiplier(override, metadata.source)
            return MarketFee(
                entry_fee_bps=metadata.taker_fee_bps * multiplier,
                exit_fee_bps=metadata.taker_fee_bps * multiplier,
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


def load_hyperliquid_account_fee(
    api_endpoint: str,
    account_address: str,
    timeout_seconds: float,
    *,
    prefer_sdk: bool = True,
) -> HyperliquidAccountFee:
    if prefer_sdk:
        try:
            payload = _load_user_fees_with_sdk(api_endpoint, account_address, timeout_seconds)
        except ImportError:
            payload = info_post_json(
                api_endpoint,
                {"type": "userFees", "user": account_address},
                timeout_seconds,
                private_readonly=True,
            )
    else:
        payload = info_post_json(
            api_endpoint,
            {"type": "userFees", "user": account_address},
            timeout_seconds,
            private_readonly=True,
        )
    if not isinstance(payload, dict):
        raise ValueError("Hyperliquid userFees response was not an object")
    return _account_fee_from_user_fees(payload)


def hyperliquid_fee_overrides_from_config(path: str | Path | None) -> dict[str, HyperliquidFeeOverride]:
    if path is None:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    markets = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(markets, list):
        return {}

    result: dict[str, HyperliquidFeeOverride] = {}
    for item in markets:
        if not isinstance(item, dict):
            continue
        market = str(item.get("coin") or item.get("market") or item.get("display_market") or "")
        if not market:
            continue
        result[market] = HyperliquidFeeOverride(
            market=market,
            entry_fee_bps=_optional_decimal_field(item, ("entry_fee_bps", "entryFeeBps")),
            exit_fee_bps=_optional_decimal_field(item, ("exit_fee_bps", "exitFeeBps")),
            fee_multiplier=Decimal(str(item.get("fee_multiplier", "1"))),
            fee_multiplier_expires_at=_optional_datetime_field(item, "fee_multiplier_expires_at"),
            slippage_buffer_bps=Decimal(str(item.get("slippage_buffer_bps", "0"))),
        )
    return result


def hyperliquid_market_fee_metadata_from_meta(payload: object) -> dict[str, HyperliquidMarketFeeMetadata]:
    if not isinstance(payload, dict):
        return {}
    universe = payload.get("universe")
    if not isinstance(universe, list):
        return {}

    result: dict[str, HyperliquidMarketFeeMetadata] = {}
    for item in universe:
        if not isinstance(item, dict):
            continue
        market = str(item.get("name") or "")
        if not market:
            continue
        maker_fee_bps = _metadata_fee_bps(item, ("maker_fee_bps", "makerFeeBps", "add_fee_bps", "addFeeBps"))
        taker_fee_bps = _metadata_fee_bps(item, ("taker_fee_bps", "takerFeeBps", "cross_fee_bps", "crossFeeBps"))
        if maker_fee_bps is None and taker_fee_bps is None:
            continue
        result[market] = HyperliquidMarketFeeMetadata(
            market=market,
            maker_fee_bps=maker_fee_bps,
            taker_fee_bps=taker_fee_bps,
        )
    return result


def _load_user_fees_with_sdk(api_endpoint: str, account_address: str, timeout_seconds: float) -> object:
    from hyperliquid.info import Info

    info = Info(api_endpoint, skip_ws=True, timeout=timeout_seconds)
    return info.user_fees(account_address)


def _account_fee_from_user_fees(payload: dict[str, object]) -> HyperliquidAccountFee:
    maker_rate = _optional_decimal_field(payload, ("userAddRate", "add", "maker", "makerFeeRate"))
    taker_rate = _optional_decimal_field(payload, ("userCrossRate", "cross", "taker", "takerFeeRate"))
    fee_schedule = payload.get("feeSchedule")
    if isinstance(fee_schedule, dict):
        if maker_rate is None:
            maker_rate = _optional_decimal_field(fee_schedule, ("add", "maker", "makerFeeRate"))
        if taker_rate is None:
            taker_rate = _optional_decimal_field(fee_schedule, ("cross", "taker", "takerFeeRate"))
    if maker_rate is None or taker_rate is None:
        raise ValueError("Hyperliquid userFees response did not contain userAddRate/userCrossRate")

    active_referral = payload.get("activeReferralDiscount")
    active_staking = payload.get("activeStakingDiscount")
    fee_level_parts = []
    if active_referral not in (None, ""):
        fee_level_parts.append(f"activeReferralDiscount={active_referral}")
    if isinstance(active_staking, dict) and active_staking.get("discount") not in (None, ""):
        fee_level_parts.append(f"activeStakingDiscount={active_staking.get('discount')}")

    return HyperliquidAccountFee(
        fee_level=";".join(fee_level_parts) if fee_level_parts else None,
        maker_fee_bps=_fee_rate_to_bps(maker_rate),
        taker_fee_bps=_fee_rate_to_bps(taker_rate),
    )


def _metadata_fee_bps(payload: dict[str, object], names: tuple[str, ...]) -> Decimal | None:
    bps = _optional_decimal_field(payload, names)
    if bps is not None:
        return bps
    rate_names = tuple(name.removesuffix("_bps").removesuffix("Bps") + "_rate" for name in names)
    rate = _optional_decimal_field(payload, rate_names)
    if rate is None:
        return None
    return _fee_rate_to_bps(rate)


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
    override: HyperliquidFeeOverride | None,
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
