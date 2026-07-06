from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from perpdex_farming_bot.connectors.hotstuff_readonly import info_post_json
from perpdex_farming_bot.core.execution_cost import MarketFee
from perpdex_farming_bot.credentials import read_hotstuff_private_readonly_params


@dataclass(frozen=True)
class HotstuffMarketFeeMetadata:
    market: str
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    source: str = "market_metadata"


@dataclass(frozen=True)
class HotstuffFeeOverride:
    market: str
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    fee_multiplier: Decimal = Decimal("1")
    fee_multiplier_expires_at: datetime | None = None
    slippage_buffer_bps: Decimal = Decimal("0")
    source: str = "config_exact_override"


@dataclass(frozen=True)
class HotstuffAccountFee:
    fee_level: str | None
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    source: str = "account_api_user_fees"


@dataclass(frozen=True)
class HotstuffFeeProvider:
    metadata_by_market: dict[str, HotstuffMarketFeeMetadata]
    override_by_market: dict[str, HotstuffFeeOverride]
    account_fee: HotstuffAccountFee | None = None

    def fee_for_market(self, market: str) -> MarketFee:
        override = self.override_by_market.get(market)
        slippage_buffer = override.slippage_buffer_bps if override is not None else Decimal("0")

        if self.account_fee is not None:
            multiplier, source = _active_fee_multiplier(override)
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

        if override is not None and override.entry_fee_bps is not None and override.exit_fee_bps is not None:
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


def load_hotstuff_account_fee(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
) -> HotstuffAccountFee:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    payload = info_post_json(api_endpoint, "user_fees", params, timeout_seconds, private_readonly=True)
    if not isinstance(payload, dict):
        raise ValueError("Hotstuff user_fees response was not an object")

    threshold = payload.get("total_volume_threshold")
    fee_level = f"total_volume_threshold={threshold}" if threshold not in (None, "") else None
    return HotstuffAccountFee(
        fee_level=fee_level,
        maker_fee_bps=_fee_rate_to_bps(_required_decimal_field(payload, "perp_maker_fee_rate")),
        taker_fee_bps=_fee_rate_to_bps(_required_decimal_field(payload, "perp_taker_fee_rate")),
    )


def hotstuff_fee_overrides_from_plan(plan: dict[str, object]) -> dict[str, HotstuffFeeOverride]:
    overrides: dict[str, HotstuffFeeOverride] = {}
    raw_markets = plan.get("markets", [])
    if not isinstance(raw_markets, list):
        return overrides

    for item in raw_markets:
        if not isinstance(item, dict):
            continue
        market = str(item.get("market") or "")
        if not market:
            continue
        overrides[market] = HotstuffFeeOverride(
            market=market,
            entry_fee_bps=_optional_decimal_field(item, ("entry_fee_bps", "entryFeeBps")),
            exit_fee_bps=_optional_decimal_field(item, ("exit_fee_bps", "exitFeeBps")),
            fee_multiplier=Decimal(str(item.get("fee_multiplier", "1"))),
            fee_multiplier_expires_at=_optional_datetime_field(item, "fee_multiplier_expires_at"),
            slippage_buffer_bps=Decimal(str(item.get("slippage_buffer_bps", "0"))),
        )
    return overrides


def hotstuff_market_fee_metadata_from_instruments(
    instruments: dict[str, dict[str, object]] | Iterable[dict[str, object]],
) -> dict[str, HotstuffMarketFeeMetadata]:
    if isinstance(instruments, dict):
        iterable = instruments.values()
    else:
        iterable = instruments

    result: dict[str, HotstuffMarketFeeMetadata] = {}
    for item in iterable:
        if not isinstance(item, dict):
            continue
        market = str(item.get("name") or item.get("symbol") or item.get("market") or "")
        if not market:
            continue
        entry_fee, exit_fee = _metadata_fee_bps(item)
        if entry_fee is None and exit_fee is None:
            continue
        result[market] = HotstuffMarketFeeMetadata(
            market=market,
            entry_fee_bps=entry_fee,
            exit_fee_bps=exit_fee,
        )
    return result


def _metadata_fee_bps(payload: dict[str, object]) -> tuple[Decimal | None, Decimal | None]:
    entry_fee = _optional_decimal_field(payload, ("entry_fee_bps", "entryFeeBps"))
    exit_fee = _optional_decimal_field(payload, ("exit_fee_bps", "exitFeeBps"))
    taker_fee_bps = _optional_decimal_field(
        payload,
        (
            "taker_fee_bps",
            "takerFeeBps",
            "perp_taker_fee_bps",
            "perpTakerFeeBps",
        ),
    )
    taker_fee_rate = _optional_decimal_field(
        payload,
        (
            "taker_fee_rate",
            "takerFeeRate",
            "perp_taker_fee_rate",
            "perpTakerFeeRate",
        ),
    )
    if taker_fee_bps is None and taker_fee_rate is not None:
        taker_fee_bps = _fee_rate_to_bps(taker_fee_rate)

    if entry_fee is None:
        entry_fee = taker_fee_bps
    if exit_fee is None:
        exit_fee = taker_fee_bps
    return entry_fee, exit_fee


def _required_decimal_field(payload: dict[str, object], name: str) -> Decimal:
    value = payload.get(name)
    if value is None or value == "":
        raise ValueError(f"missing Hotstuff account fee field: {name}")
    return Decimal(str(value))


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
    return datetime.fromisoformat(text)


def _fee_rate_to_bps(rate: Decimal) -> Decimal:
    return rate * Decimal("10000")


def _active_fee_multiplier(override: HotstuffFeeOverride | None) -> tuple[Decimal, str]:
    if override is None or override.fee_multiplier == Decimal("1"):
        return Decimal("1"), "account_taker_fee"
    if override.fee_multiplier_expires_at is None:
        return Decimal("1"), "account_taker_fee_config_multiplier_missing_expiry_ignored"

    expires_at = override.fee_multiplier_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc):
        return Decimal("1"), "account_taker_fee_config_multiplier_expired_ignored"
    return override.fee_multiplier, "account_taker_fee_config_multiplier"
