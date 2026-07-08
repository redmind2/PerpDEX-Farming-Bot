from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal

from perpdex_farming_bot.core.execution_adapter import OpenOrderSnapshot, PositionSnapshot
from perpdex_farming_bot.core.execution_models import ExecutionMode, OrderExecutionResult, OrderIntent, OrderKind, RoundtripMode
from perpdex_farming_bot.gateway import BUILTIN_GATEWAY_EXCHANGE_IDS
from perpdex_farming_bot.gateway.live_preflight import (
    build_live_preflight_gateway,
    paired_live_trade_intent,
    run_live_gateway_preflight,
)


DEFAULT_MARKETS = {
    "hibachi": "BTC/USDT-P",
    "hotstuff": "BTC-PERP",
    "hyperliquid": "BTC",
    "lighter": "BTC-PERP",
    "pacifica": "BTC",
    "risex": "1",
}


@dataclass(frozen=True)
class NoNetworkAdapter:
    exchange_id: str

    def list_positions(self) -> tuple[PositionSnapshot, ...]:
        return ()

    def list_open_orders(self) -> tuple[OpenOrderSnapshot, ...]:
        return ()

    def submit_order(self, order: OrderIntent, *, mode: ExecutionMode) -> OrderExecutionResult:
        return OrderExecutionResult(
            order_intent_id=order.intent_id,
            accepted=False,
            status=f"{self.exchange_id}_{mode.value}_submit_not_called_in_smoke",
            live_order_submitted=False,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Network-free live preflight smoke for the Execution Gateway. Never sends orders.",
    )
    parser.add_argument("--exchange", choices=("all", *BUILTIN_GATEWAY_EXCHANGE_IDS), default="all")
    args = parser.parse_args()

    exchange_ids = BUILTIN_GATEWAY_EXCHANGE_IDS if args.exchange == "all" else (args.exchange,)
    print("gateway_live_preflight_smoke=True")
    print(f"registered_exchange_count={len(exchange_ids)}")
    print("live_order_submitted=False")

    all_ready = True
    for exchange_id in exchange_ids:
        market = DEFAULT_MARKETS[exchange_id]
        account_alias = f"{exchange_id}_gateway_smoke"
        trade_intent = paired_live_trade_intent(
            exchange_id=exchange_id,
            account_alias=account_alias,
            strategy_id="gateway_live_preflight_smoke",
            market=market,
            roundtrip_mode=RoundtripMode.NETTING,
            quantity=Decimal("0.001"),
            buy_reference_price=Decimal("50000"),
            sell_reference_price=Decimal("49999"),
            buy_order_type=OrderKind.MARKET,
            sell_order_type=OrderKind.MARKET,
            max_gross_notional_usd=Decimal("100"),
        )
        gateway = build_live_preflight_gateway(
            exchange_id=exchange_id,
            account_alias=account_alias,
            market=market,
            adapter_factory=lambda exchange_id=exchange_id: NoNetworkAdapter(exchange_id),
            entry_fee_bps=Decimal("3"),
            exit_fee_bps=Decimal("3"),
            fee_source="gateway_live_preflight_smoke_static",
            max_order_notional_usd=Decimal("100"),
            max_gross_notional_usd=Decimal("100"),
            open_orders_supported=True,
        )
        result = run_live_gateway_preflight(
            gateway=gateway,
            trade_intent=trade_intent,
            request_id=f"{exchange_id}-live-preflight-smoke-1",
            include_read_only=True,
            print_prefix=f"{exchange_id}_gateway",
            emit_ledger=False,
        )
        all_ready = (
            all_ready
            and result.ready
            and not result.live_order_submitted
            and result.execution_result.status == "live_preflight_accepted_no_exchange_call"
        )

    raise SystemExit(0 if all_ready else 1)


if __name__ == "__main__":
    main()
