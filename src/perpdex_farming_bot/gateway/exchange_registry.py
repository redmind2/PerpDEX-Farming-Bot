from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping

from perpdex_farming_bot.core.execution_adapter import (
    OpenOrderSnapshot,
    PositionSnapshot,
    TopOfBook,
    TradeFillSnapshot,
)
from perpdex_farming_bot.core.execution_models import (
    AccountPolicy,
    ExecutionMode,
    OrderExecutionResult,
    OrderIntent,
)
from perpdex_farming_bot.core.fee_provider import CommonFeeProvider, MultiExchangeFeeProvider, StaticFeeProvider
from perpdex_farming_bot.exchanges.base import AdapterError, ExchangeAdapter
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


BUILTIN_GATEWAY_EXCHANGE_IDS = (
    "hibachi",
    "hotstuff",
    "hyperliquid",
    "lighter",
    "pacifica",
    "risex",
)


@dataclass
class LazyExchangeAdapterBridge:
    exchange_id: str
    adapter_factory: Callable[[], ExchangeAdapter]
    _adapter: ExchangeAdapter | None = field(default=None, init=False, repr=False)

    def get_top_of_book(self, market: str) -> TopOfBook:
        raise AdapterError(f"{self.exchange_id} top-of-book is not connected to Gateway yet for {market}")

    def list_positions(self, account_alias: str) -> tuple[PositionSnapshot, ...]:
        return tuple(
            PositionSnapshot(
                exchange_id=position.exchange_id,
                account_alias=account_alias,
                market=position.market,
                size=position.size,
                side=position.side,
            )
            for position in self._adapter_instance().list_positions()
        )

    def list_open_orders(self, account_alias: str, market: str | None = None) -> tuple[OpenOrderSnapshot, ...]:
        method = getattr(self._adapter_instance(), "list_open_orders", None)
        if method is None:
            return ()
        raw_orders = _call_list_open_orders(method, market)
        return tuple(_open_order_snapshot(self.exchange_id, account_alias, item) for item in raw_orders)

    def list_order_history(self, account_alias: str, market: str | None = None) -> tuple[OpenOrderSnapshot, ...]:
        del account_alias, market
        return ()

    def list_trade_fills(self, account_alias: str, market: str | None = None) -> tuple[TradeFillSnapshot, ...]:
        del account_alias, market
        return ()

    def submit_order(self, order: OrderIntent, *, mode: ExecutionMode) -> OrderExecutionResult:
        return OrderExecutionResult(
            order_intent_id=order.intent_id,
            accepted=False,
            status=f"{self.exchange_id}_{mode.value}_submit_blocked_gateway_skeleton",
            error="Gateway exchange bridge does not submit live orders in this phase",
            live_order_submitted=False,
        )

    def _adapter_instance(self) -> ExchangeAdapter:
        if self._adapter is None:
            self._adapter = self.adapter_factory()
        return self._adapter


@dataclass(frozen=True)
class GatewayExchangeBinding:
    exchange_id: str
    account_alias: str
    default_market: str
    adapter: LazyExchangeAdapterBridge
    fee_provider: CommonFeeProvider
    account_policy: AccountPolicy


def builtin_gateway_exchange_bindings() -> tuple[GatewayExchangeBinding, ...]:
    return (
        _binding("hibachi", "BTC/USDT-P", _create_hibachi_adapter),
        _binding("hotstuff", "BTC-PERP", _create_hotstuff_adapter),
        _binding("hyperliquid", "BTC", _create_hyperliquid_adapter),
        _binding("lighter", "BTC-PERP", _create_lighter_adapter),
        _binding("pacifica", "BTC", _create_pacifica_adapter),
        _binding("risex", "1", _create_risex_adapter),
    )


def build_gateway_from_bindings(bindings: tuple[GatewayExchangeBinding, ...]) -> ExecutionGateway:
    return ExecutionGateway(
        account_policies={binding.account_alias: binding.account_policy for binding in bindings},
        fee_provider=MultiExchangeFeeProvider(
            {
                (binding.exchange_id, binding.account_alias): binding.fee_provider
                for binding in bindings
            }
        ),
        adapters={binding.exchange_id: binding.adapter for binding in bindings},
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=False,
    )


def _binding(
    exchange_id: str,
    default_market: str,
    adapter_factory: Callable[[], ExchangeAdapter],
) -> GatewayExchangeBinding:
    account_alias = f"{exchange_id}_gateway"
    return GatewayExchangeBinding(
        exchange_id=exchange_id,
        account_alias=account_alias,
        default_market=default_market,
        adapter=LazyExchangeAdapterBridge(exchange_id=exchange_id, adapter_factory=adapter_factory),
        fee_provider=StaticFeeProvider(
            exchange_id=exchange_id,
            account_alias=account_alias,
            entry_fee_bps=Decimal("3"),
            exit_fee_bps=Decimal("3"),
            taker_fee_bps=Decimal("3"),
            source="gateway_connection_smoke_fee_not_live",
            markets=(default_market,),
        ),
        account_policy=AccountPolicy(
            account_alias=account_alias,
            allowed_modes=(ExecutionMode.DRY_RUN, ExecutionMode.PAPER),
            allow_live=False,
            require_fee_quote=True,
            max_order_notional_usd=Decimal("100"),
            max_gross_notional_usd=Decimal("200"),
            kill_switch_required=True,
        ),
    )


def _create_hibachi_adapter() -> ExchangeAdapter:
    from perpdex_farming_bot.exchanges.hibachi import HibachiAdapter

    return HibachiAdapter(credential_prefix="HIBACHI_1_CRYPTO")


def _create_hotstuff_adapter() -> ExchangeAdapter:
    from perpdex_farming_bot.connectors.hotstuff_readonly import default_api_endpoint
    from perpdex_farming_bot.exchanges.hotstuff import HotstuffAdapter

    environment = "PRODUCTION"
    return HotstuffAdapter(
        api_endpoint=default_api_endpoint(environment),
        credential_prefix="HOTSTUFF",
        environment=environment,
    )


def _create_hyperliquid_adapter() -> ExchangeAdapter:
    from perpdex_farming_bot.connectors.hyperliquid_readonly import default_api_endpoint
    from perpdex_farming_bot.exchanges.hyperliquid import HyperliquidAdapter

    environment = "PRODUCTION"
    return HyperliquidAdapter(
        api_endpoint=default_api_endpoint(environment),
        credential_prefix="HYPERLIQUID",
        environment=environment,
        allow_live_orders=False,
    )


def _create_lighter_adapter() -> ExchangeAdapter:
    from perpdex_farming_bot.connectors.lighter_readonly import default_api_endpoint
    from perpdex_farming_bot.exchanges.lighter import LighterAdapter

    environment = "PRODUCTION"
    return LighterAdapter(
        api_endpoint=default_api_endpoint(environment),
        credential_prefix="LIGHTER",
        environment=environment,
        allow_live_orders=False,
    )


def _create_pacifica_adapter() -> ExchangeAdapter:
    from perpdex_farming_bot.connectors.pacifica_readonly import default_api_endpoint
    from perpdex_farming_bot.exchanges.pacifica import PacificaAdapter

    environment = "TESTNET"
    return PacificaAdapter(
        api_endpoint=default_api_endpoint(environment),
        credential_prefix="PACIFICA",
        environment=environment,
        allow_live_orders=False,
    )


def _create_risex_adapter() -> ExchangeAdapter:
    from perpdex_farming_bot.connectors.risex_readonly import default_api_endpoint
    from perpdex_farming_bot.exchanges.risex import RisexAdapter

    environment = "TESTNET"
    return RisexAdapter(
        api_endpoint=default_api_endpoint(environment),
        credential_prefix="RISEX",
        environment=environment,
        allow_live_orders=False,
    )


def _call_list_open_orders(method: object, market: str | None) -> tuple[Mapping[str, object], ...]:
    try:
        payload = method()
    except TypeError:
        if market is None:
            return ()
        try:
            payload = method(market=market)
        except TypeError:
            try:
                payload = method(market_id=int(market))
            except (TypeError, ValueError):
                return ()
    if not isinstance(payload, tuple):
        return ()
    return tuple(item for item in payload if isinstance(item, dict))


def _open_order_snapshot(exchange_id: str, account_alias: str, item: Mapping[str, object]) -> OpenOrderSnapshot:
    return OpenOrderSnapshot(
        exchange_id=exchange_id,
        account_alias=account_alias,
        market=_string_field(item, ("market", "coin", "symbol", "market_id", "market_index"), "unknown"),
        order_id=_string_field(item, ("order_id", "oid", "id", "order_index"), "unknown"),
        side=_string_field(item, ("side", "is_buy"), "unknown"),
        price=_optional_decimal(item, ("price", "limit_px", "px")),
        quantity=_optional_decimal(item, ("quantity", "size", "sz", "remaining_base_amount")) or Decimal("0"),
        filled_quantity=_optional_decimal(item, ("filled_quantity", "filled", "filled_size")) or Decimal("0"),
        reduce_only=bool(item.get("reduce_only", False)),
        metadata=dict(item),
    )


def _string_field(item: Mapping[str, object], names: tuple[str, ...], default: str) -> str:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return str(value)
    return default


def _optional_decimal(item: Mapping[str, object], names: tuple[str, ...]) -> Decimal | None:
    for name in names:
        value = item.get(name)
        if value in (None, ""):
            continue
        try:
            return Decimal(str(value))
        except Exception:
            return None
    return None
