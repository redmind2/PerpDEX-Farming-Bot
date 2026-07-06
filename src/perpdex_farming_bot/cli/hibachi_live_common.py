from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from perpdex_farming_bot.core.execution_cost import MarketFee


@dataclass(frozen=True)
class HibachiFeeOverride:
    market: str
    entry_fee_bps: Decimal | None = None
    exit_fee_bps: Decimal | None = None
    fee_multiplier: Decimal = Decimal("1")
    fee_multiplier_expires_at: datetime | None = None
    slippage_buffer_bps: Decimal = Decimal("0")
    source: str = "config_override"

    @property
    def has_exact_override(self) -> bool:
        return self.entry_fee_bps is not None and self.exit_fee_bps is not None


@dataclass(frozen=True)
class HibachiAccountFee:
    fee_level: str | None
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    source: str


@dataclass(frozen=True)
class HibachiFeeProvider:
    override_by_market: dict[str, HibachiFeeOverride]
    account_fee: HibachiAccountFee | None = None
    metadata_fee: HibachiAccountFee | None = None

    def fee_for_market(self, market: str) -> MarketFee:
        override = self.override_by_market.get(market)
        slippage_buffer = override.slippage_buffer_bps if override is not None else Decimal("0")

        if self.account_fee is not None:
            if override is not None and override.has_exact_override:
                return MarketFee(
                    entry_fee_bps=override.entry_fee_bps,
                    exit_fee_bps=override.exit_fee_bps,
                    slippage_buffer_bps=slippage_buffer,
                    source=override.source,
                )
            multiplier, multiplier_source = _active_fee_multiplier(override, "account_taker_fee")
            return MarketFee(
                entry_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                exit_fee_bps=self.account_fee.taker_fee_bps * multiplier,
                slippage_buffer_bps=slippage_buffer,
                source=multiplier_source,
            )

        if self.metadata_fee is not None:
            if override is not None and override.has_exact_override:
                return MarketFee(
                    entry_fee_bps=override.entry_fee_bps,
                    exit_fee_bps=override.exit_fee_bps,
                    slippage_buffer_bps=slippage_buffer,
                    source=override.source,
                )
            multiplier, multiplier_source = _active_fee_multiplier(override, "metadata_taker_fee")
            return MarketFee(
                entry_fee_bps=self.metadata_fee.taker_fee_bps * multiplier,
                exit_fee_bps=self.metadata_fee.taker_fee_bps * multiplier,
                slippage_buffer_bps=slippage_buffer,
                source=multiplier_source,
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


def load_hibachi_account_fee(client: object) -> HibachiAccountFee:
    account_info = client.get_account_info()
    return HibachiAccountFee(
        fee_level=None,
        maker_fee_bps=fee_rate_to_bps(Decimal(str(getattr(account_info, "tradeMakerFeeRate")))),
        taker_fee_bps=fee_rate_to_bps(Decimal(str(getattr(account_info, "tradeTakerFeeRate")))),
        source="account_api",
    )


def load_hibachi_metadata_fee(client: object) -> HibachiAccountFee:
    inventory = client.get_inventory()
    fee_config = getattr(inventory, "feeConfig")
    return HibachiAccountFee(
        fee_level=None,
        maker_fee_bps=fee_rate_to_bps(Decimal(str(getattr(fee_config, "tradeMakerFeeRate")))),
        taker_fee_bps=fee_rate_to_bps(Decimal(str(getattr(fee_config, "tradeTakerFeeRate")))),
        source="market_metadata",
    )


def fee_rate_to_bps(rate: Decimal) -> Decimal:
    return rate * Decimal("10000")


def _active_fee_multiplier(
    override: HibachiFeeOverride | None,
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
