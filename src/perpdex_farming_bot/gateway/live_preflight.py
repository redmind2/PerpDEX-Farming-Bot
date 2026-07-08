from __future__ import annotations

from decimal import Decimal
from typing import Callable, Mapping

from perpdex_farming_bot.core.execution_event import emit_execution_event
from perpdex_farming_bot.core.execution_models import (
    AccountPolicy,
    ExecutionMode,
    ExecutionPreflightRequest,
    ExecutionPreflightResult,
    OrderIntent,
    OrderKind,
    OrderSide,
    RoundtripMode,
    TradeIntent,
)
from perpdex_farming_bot.core.fee_provider import StaticFeeProvider
from perpdex_farming_bot.exchanges.base import ExchangeAdapter
from perpdex_farming_bot.gateway.exchange_registry import LazyExchangeAdapterBridge
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


def paired_live_trade_intent(
    *,
    exchange_id: str,
    account_alias: str,
    strategy_id: str,
    market: str,
    roundtrip_mode: RoundtripMode,
    quantity: Decimal | None = None,
    buy_quantity: Decimal | None = None,
    sell_quantity: Decimal | None = None,
    buy_reference_price: Decimal | None = None,
    sell_reference_price: Decimal | None = None,
    buy_price: Decimal | None = None,
    sell_price: Decimal | None = None,
    buy_order_type: OrderKind = OrderKind.MARKET,
    sell_order_type: OrderKind = OrderKind.MARKET,
    time_in_force: str | None = None,
    max_gross_notional_usd: Decimal | None = None,
    sell_reduce_only: bool | None = None,
    metadata: Mapping[str, object] | None = None,
    buy_metadata: Mapping[str, object] | None = None,
    sell_metadata: Mapping[str, object] | None = None,
) -> TradeIntent:
    resolved_buy_quantity = buy_quantity if buy_quantity is not None else quantity
    resolved_sell_quantity = sell_quantity if sell_quantity is not None else quantity
    if resolved_buy_quantity is None or resolved_sell_quantity is None:
        raise ValueError("quantity or both buy_quantity/sell_quantity are required")

    reduce_only = roundtrip_mode is not RoundtripMode.NETTING if sell_reduce_only is None else sell_reduce_only
    buy = OrderIntent(
        intent_id=f"{exchange_id}-live-gateway-buy-1",
        exchange_id=exchange_id,
        market=market,
        side=OrderSide.BUY,
        order_type=buy_order_type,
        quantity=resolved_buy_quantity,
        price=buy_price,
        reference_price=buy_reference_price,
        time_in_force=time_in_force,
        reduce_only=False,
        metadata=dict(buy_metadata or {}),
    )
    sell = OrderIntent(
        intent_id=f"{exchange_id}-live-gateway-sell-1",
        exchange_id=exchange_id,
        market=market,
        side=OrderSide.SELL,
        order_type=sell_order_type,
        quantity=resolved_sell_quantity,
        price=sell_price,
        reference_price=sell_reference_price,
        time_in_force=time_in_force,
        reduce_only=reduce_only,
        metadata=dict(sell_metadata or {}),
    )
    return TradeIntent(
        intent_id=f"{exchange_id}-live-gateway-trade-1",
        strategy_id=strategy_id,
        account_alias=account_alias,
        exchange_id=exchange_id,
        market=market,
        mode=ExecutionMode.LIVE,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=max_gross_notional_usd,
        metadata=dict(metadata or {}),
    )


def build_live_preflight_gateway(
    *,
    exchange_id: str,
    account_alias: str,
    market: str,
    adapter_factory: Callable[[], ExchangeAdapter],
    entry_fee_bps: Decimal,
    exit_fee_bps: Decimal,
    fee_source: str,
    max_order_notional_usd: Decimal | None,
    max_gross_notional_usd: Decimal | None,
    slippage_buffer_bps: Decimal = Decimal("0"),
    open_orders_supported: bool = True,
    live_orders_enabled: bool = False,
) -> ExecutionGateway:
    taker_fee_bps = max(entry_fee_bps, exit_fee_bps)
    return ExecutionGateway(
        account_policies={
            account_alias: AccountPolicy(
                account_alias=account_alias,
                allowed_modes=(ExecutionMode.DRY_RUN, ExecutionMode.PAPER, ExecutionMode.LIVE),
                allow_live=True,
                require_fee_quote=True,
                max_order_notional_usd=max_order_notional_usd,
                max_gross_notional_usd=max_gross_notional_usd,
                kill_switch_required=True,
            )
        },
        fee_provider=StaticFeeProvider(
            exchange_id=exchange_id,
            account_alias=account_alias,
            entry_fee_bps=entry_fee_bps,
            exit_fee_bps=exit_fee_bps,
            taker_fee_bps=taker_fee_bps,
            source=fee_source,
            slippage_buffer_bps=slippage_buffer_bps,
            markets=(market,),
        ),
        adapters={
            exchange_id: LazyExchangeAdapterBridge(
                exchange_id=exchange_id,
                adapter_factory=adapter_factory,
                open_orders_supported=open_orders_supported,
            )
        },
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=live_orders_enabled,
    )


def run_live_gateway_preflight(
    *,
    gateway: ExecutionGateway,
    trade_intent: TradeIntent,
    request_id: str,
    include_read_only: bool,
    check_positions: bool = True,
    check_open_orders: bool = True,
    print_prefix: str = "gateway",
    emit_ledger: bool = True,
) -> ExecutionPreflightResult:
    preflight = gateway.preflight(
        ExecutionPreflightRequest(
            request_id=request_id,
            trade_intent=trade_intent,
            include_read_only=include_read_only,
            check_positions=check_positions,
            check_open_orders=check_open_orders,
            allow_live_without_submission=True,
        )
    )
    print_gateway_preflight_result(preflight, prefix=print_prefix)
    if emit_ledger and preflight.execution_result.ledger_event is not None:
        emit_execution_event(preflight.execution_result.ledger_event)
    return preflight


def print_gateway_preflight_result(preflight: ExecutionPreflightResult, *, prefix: str = "gateway") -> None:
    clean_prefix = prefix.rstrip("_")
    print(f"{clean_prefix}_ready={preflight.ready}")
    print(f"{clean_prefix}_status={preflight.status}")
    print(f"{clean_prefix}_reason={preflight.reason}")
    print(f"{clean_prefix}_accepted={preflight.execution_result.accepted}")
    print(f"{clean_prefix}_execution_status={preflight.execution_result.status}")
    print(f"{clean_prefix}_live_order_submitted={preflight.live_order_submitted}")
    for check in preflight.checks:
        print(f"{clean_prefix}_check={check.name}:{check.status}:ok={check.ok}:count={check.count}")
    cost = preflight.execution_result.cost_quote
    if cost is not None:
        expected = cost.expected_loss_bps if cost.expected_loss_bps is not None else "unknown"
        fee_usd = cost.estimated_fee_usd if cost.estimated_fee_usd is not None else "unknown"
        print(f"{clean_prefix}_expected_loss_bps={expected}")
        print(f"{clean_prefix}_estimated_fee_usd={fee_usd}")
