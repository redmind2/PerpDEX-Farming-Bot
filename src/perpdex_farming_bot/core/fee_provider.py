from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Protocol

from perpdex_farming_bot.core.execution_cost import MarketFee
from perpdex_farming_bot.core.execution_models import FeeQuote, UnknownFeePolicy


@dataclass(frozen=True)
class FeeRequest:
    exchange_id: str
    account_alias: str
    market: str
    unknown_fee_policy: UnknownFeePolicy = UnknownFeePolicy.BLOCK


class CommonFeeProvider(Protocol):
    def quote_fee(self, request: FeeRequest) -> FeeQuote:
        """Return a normalized fee quote without exposing exchange-specific APIs."""


class LegacyMarketFeeProvider(Protocol):
    def fee_for_market(self, market: str) -> MarketFee:
        """Existing exchange fee providers already implement this smaller API."""


@dataclass(frozen=True)
class MarketFeeProviderAdapter:
    exchange_id: str
    account_alias: str
    provider: LegacyMarketFeeProvider
    conservative_entry_fee_bps: Decimal | None = None
    conservative_exit_fee_bps: Decimal | None = None

    def quote_fee(self, request: FeeRequest) -> FeeQuote:
        if request.exchange_id != self.exchange_id or request.account_alias != self.account_alias:
            return FeeQuote(
                exchange_id=request.exchange_id,
                account_alias=request.account_alias,
                market=request.market,
                source="fee_unknown",
                fee_known=False,
                unknown_fee_policy=request.unknown_fee_policy,
                blocked=True,
                block_reason="fee_provider_scope_mismatch",
            )
        market_fee = self.provider.fee_for_market(request.market)
        return fee_quote_from_market_fee(
            market_fee,
            request,
            conservative_entry_fee_bps=self.conservative_entry_fee_bps,
            conservative_exit_fee_bps=self.conservative_exit_fee_bps,
        )


@dataclass(frozen=True)
class StaticFeeProvider:
    exchange_id: str
    account_alias: str
    entry_fee_bps: Decimal
    exit_fee_bps: Decimal
    source: str = "static_config"
    maker_fee_bps: Decimal | None = None
    taker_fee_bps: Decimal | None = None
    slippage_buffer_bps: Decimal = Decimal("0")
    markets: tuple[str, ...] = ()

    def quote_fee(self, request: FeeRequest) -> FeeQuote:
        if request.exchange_id != self.exchange_id or request.account_alias != self.account_alias:
            return FeeQuote(
                exchange_id=request.exchange_id,
                account_alias=request.account_alias,
                market=request.market,
                source="fee_unknown",
                fee_known=False,
                unknown_fee_policy=request.unknown_fee_policy,
                blocked=True,
                block_reason="fee_provider_scope_mismatch",
            )
        if self.markets and request.market not in self.markets:
            return FeeQuote(
                exchange_id=request.exchange_id,
                account_alias=request.account_alias,
                market=request.market,
                source="fee_unknown",
                fee_known=False,
                unknown_fee_policy=request.unknown_fee_policy,
                blocked=True,
                block_reason="fee_market_not_configured",
            )
        return FeeQuote(
            exchange_id=request.exchange_id,
            account_alias=request.account_alias,
            market=request.market,
            source=self.source,
            fee_known=True,
            maker_fee_bps=self.maker_fee_bps,
            taker_fee_bps=self.taker_fee_bps,
            entry_fee_bps=self.entry_fee_bps,
            exit_fee_bps=self.exit_fee_bps,
            slippage_buffer_bps=self.slippage_buffer_bps,
            unknown_fee_policy=request.unknown_fee_policy,
        )


@dataclass(frozen=True)
class MultiExchangeFeeProvider:
    providers: Mapping[tuple[str, str], CommonFeeProvider]

    def quote_fee(self, request: FeeRequest) -> FeeQuote:
        provider = self.providers.get((request.exchange_id, request.account_alias))
        if provider is None:
            return FeeQuote(
                exchange_id=request.exchange_id,
                account_alias=request.account_alias,
                market=request.market,
                source="fee_unknown",
                fee_known=False,
                unknown_fee_policy=request.unknown_fee_policy,
                blocked=True,
                block_reason="fee_provider_missing_for_exchange_account",
            )
        return provider.quote_fee(request)


def fee_quote_from_market_fee(
    market_fee: MarketFee,
    request: FeeRequest,
    *,
    conservative_entry_fee_bps: Decimal | None = None,
    conservative_exit_fee_bps: Decimal | None = None,
) -> FeeQuote:
    entry_fee_bps = market_fee.entry_fee_bps
    exit_fee_bps = market_fee.exit_fee_bps
    fee_known = entry_fee_bps is not None and exit_fee_bps is not None

    if fee_known:
        return FeeQuote(
            exchange_id=request.exchange_id,
            account_alias=request.account_alias,
            market=request.market,
            source=market_fee.source,
            fee_known=True,
            entry_fee_bps=entry_fee_bps,
            exit_fee_bps=exit_fee_bps,
            slippage_buffer_bps=market_fee.slippage_buffer_bps,
            unknown_fee_policy=request.unknown_fee_policy,
        )

    if request.unknown_fee_policy is UnknownFeePolicy.BLOCK:
        return FeeQuote(
            exchange_id=request.exchange_id,
            account_alias=request.account_alias,
            market=request.market,
            source=market_fee.source,
            fee_known=False,
            slippage_buffer_bps=market_fee.slippage_buffer_bps,
            unknown_fee_policy=request.unknown_fee_policy,
            blocked=True,
            block_reason="fee_unknown",
        )

    entry_default = _explicit_default(conservative_entry_fee_bps, market_fee.conservative_entry_fee_bps)
    exit_default = _explicit_default(conservative_exit_fee_bps, market_fee.conservative_exit_fee_bps)
    if entry_default is None or exit_default is None:
        return FeeQuote(
            exchange_id=request.exchange_id,
            account_alias=request.account_alias,
            market=request.market,
            source=market_fee.source,
            fee_known=False,
            slippage_buffer_bps=market_fee.slippage_buffer_bps,
            unknown_fee_policy=request.unknown_fee_policy,
            blocked=True,
            block_reason="conservative_default_missing",
        )

    return FeeQuote(
        exchange_id=request.exchange_id,
        account_alias=request.account_alias,
        market=request.market,
        source=f"{market_fee.source}_conservative_default",
        fee_known=False,
        entry_fee_bps=entry_default,
        exit_fee_bps=exit_default,
        slippage_buffer_bps=market_fee.slippage_buffer_bps,
        unknown_fee_policy=request.unknown_fee_policy,
    )


def fee_quotes_by_market(
    provider: CommonFeeProvider,
    requests: tuple[FeeRequest, ...],
) -> Mapping[str, FeeQuote]:
    return {request.market: provider.quote_fee(request) for request in requests}


def _explicit_default(configured: Decimal | None, embedded: Decimal) -> Decimal | None:
    if configured is not None:
        return configured
    if embedded > 0:
        return embedded
    return None
