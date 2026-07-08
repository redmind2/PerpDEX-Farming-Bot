from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal

from perpdex_farming_bot.core.execution_models import ExecutionRequest, OrderKind, RoundtripMode
from perpdex_farming_bot.exchanges.base import ExchangeOrderResult, PairedRoundtripResult
from perpdex_farming_bot.gateway import BUILTIN_GATEWAY_EXCHANGE_IDS
from perpdex_farming_bot.gateway.live_action import GatewayLiveActionProxy
from perpdex_farming_bot.gateway.live_preflight import build_live_preflight_gateway, paired_live_trade_intent


DEFAULT_MARKETS = {
    "hibachi": "BTC/USDT-P",
    "hotstuff": "BTC-PERP",
    "hyperliquid": "BTC",
    "lighter": "BTC-PERP",
    "pacifica": "BTC",
    "risex": "1",
}


@dataclass(frozen=True)
class NoNetworkRoundtripAdapter:
    exchange_id: str

    def list_positions(self) -> tuple[object, ...]:
        return ()

    def list_open_orders(self) -> tuple[dict[str, object], ...]:
        return ()

    def execute_paired_notional_roundtrip(
        self,
        *,
        market: str,
        instrument_id: int,
        buy_price: Decimal,
        sell_price: Decimal,
        buy_size: Decimal,
        sell_size: Decimal,
        planned_gross_volume_usd: Decimal,
        first_side: str = "BUY",
        second_side: str = "SELL",
        roundtrip_mode: str = "confirmed",
    ) -> PairedRoundtripResult:
        del instrument_id, buy_price, sell_price, buy_size, sell_size, first_side, second_side, roundtrip_mode
        order = ExchangeOrderResult(self.exchange_id, market, True, "fake_filled", filled_size=Decimal("0.001"))
        return PairedRoundtripResult(
            exchange_id=self.exchange_id,
            market=market,
            success=True,
            planned_gross_volume_usd=planned_gross_volume_usd,
            buy_result=order,
            sell_result=order,
            status="fake_gateway_submit_ok",
        )


@dataclass(frozen=True)
class NoNetworkLiveActionTarget:
    exchange_id: str

    def submit_signed_place_order(self, signed_order: object) -> dict[str, object]:
        return {
            "exchange_id": self.exchange_id,
            "signed_order": signed_order,
            "submitted": True,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Network-free live submit smoke for the Execution Gateway. Uses a fake adapter and never sends orders.",
    )
    parser.add_argument("--exchange", choices=("all", *BUILTIN_GATEWAY_EXCHANGE_IDS), default="all")
    args = parser.parse_args()

    exchange_ids = BUILTIN_GATEWAY_EXCHANGE_IDS if args.exchange == "all" else (args.exchange,)
    print("gateway_live_submit_smoke=True")
    print("network=False")
    print("real_order_submitted=False")

    all_ok = True
    for exchange_id in exchange_ids:
        market = DEFAULT_MARKETS[exchange_id]
        account_alias = f"{exchange_id}_gateway_submit_smoke"
        trade_intent = paired_live_trade_intent(
            exchange_id=exchange_id,
            account_alias=account_alias,
            strategy_id="gateway_live_submit_smoke",
            market=market,
            roundtrip_mode=RoundtripMode.NETTING,
            quantity=Decimal("0.001"),
            buy_price=Decimal("50000"),
            sell_price=Decimal("49999"),
            buy_reference_price=Decimal("50000"),
            sell_reference_price=Decimal("49999"),
            buy_order_type=OrderKind.LIMIT,
            sell_order_type=OrderKind.LIMIT,
            time_in_force="ioc",
            max_gross_notional_usd=Decimal("100"),
        )
        gateway = build_live_preflight_gateway(
            exchange_id=exchange_id,
            account_alias=account_alias,
            market=market,
            adapter_factory=lambda exchange_id=exchange_id: NoNetworkRoundtripAdapter(exchange_id),
            entry_fee_bps=Decimal("3"),
            exit_fee_bps=Decimal("3"),
            fee_source="gateway_live_submit_smoke_static",
            max_order_notional_usd=Decimal("100"),
            max_gross_notional_usd=Decimal("100"),
            open_orders_supported=True,
            live_orders_enabled=True,
        )
        result = gateway.execute_paired_roundtrip(
            ExecutionRequest(
                request_id=f"{exchange_id}-live-submit-smoke-1",
                trade_intent=trade_intent,
            )
        )
        print(f"exchange={exchange_id} success={result.success} status={result.status}")
        all_ok = all_ok and result.success and result.status == "fake_gateway_submit_ok"

        proxy = GatewayLiveActionProxy(
            target=NoNetworkLiveActionTarget(exchange_id),
            gateway=gateway,
            trade_intent=trade_intent,
            request_id_prefix=f"{exchange_id}-live-action-proxy-smoke",
        )
        proxy_result = proxy.submit_signed_place_order({"fake_signed_order": True})
        proxy_ok = isinstance(proxy_result, dict) and proxy_result.get("submitted") is True
        print(f"exchange={exchange_id} proxy_action_ok={proxy_ok}")
        all_ok = all_ok and proxy_ok

    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
