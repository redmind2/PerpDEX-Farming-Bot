from __future__ import annotations

import argparse
from decimal import Decimal

from perpdex_farming_bot.core.execution_models import (
    ExecutionMode,
    ExecutionRequest,
    OrderIntent,
    OrderKind,
    OrderSide,
    TradeIntent,
)
from perpdex_farming_bot.gateway import (
    BUILTIN_GATEWAY_EXCHANGE_IDS,
    build_gateway_from_bindings,
    builtin_gateway_exchange_bindings,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a network-free Execution Gateway smoke test.")
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--exchange", choices=("all", *BUILTIN_GATEWAY_EXCHANGE_IDS), default="all")
    args = parser.parse_args()

    mode = ExecutionMode(args.mode)
    bindings = builtin_gateway_exchange_bindings()
    if args.exchange != "all":
        bindings = tuple(binding for binding in bindings if binding.exchange_id == args.exchange)
    gateway = build_gateway_from_bindings(bindings)

    print("gateway_smoke=True")
    print(f"mode={mode.value}")
    print(f"registered_exchange_count={len(bindings)}")

    ok = True
    for binding in bindings:
        order = OrderIntent(
            intent_id=f"{binding.exchange_id}-smoke-order-1",
            exchange_id=binding.exchange_id,
            market=binding.default_market,
            side=OrderSide.BUY,
            order_type=OrderKind.LIMIT,
            quantity=Decimal("0.001"),
            price=Decimal("50000"),
            reference_price=Decimal("50000"),
            time_in_force="ioc",
        )
        intent = TradeIntent(
            intent_id=f"{binding.exchange_id}-smoke-trade-1",
            strategy_id="gateway_smoke",
            account_alias=binding.account_alias,
            exchange_id=binding.exchange_id,
            market=binding.default_market,
            mode=mode,
            orders=(order,),
            max_gross_notional_usd=Decimal("100"),
        )
        result = gateway.execute(
            ExecutionRequest(
                request_id=f"gateway-smoke-{binding.exchange_id}-1",
                trade_intent=intent,
            )
        )
        fee_source = result.fee_quote.source if result.fee_quote is not None else "none"
        expected_loss = result.cost_quote.expected_loss_bps if result.cost_quote is not None else "none"
        ledger_schema = result.ledger_event.to_json_dict()["schema"] if result.ledger_event is not None else "none"
        print(
            f"exchange={binding.exchange_id} "
            f"market={binding.default_market} "
            f"accepted={result.accepted} "
            f"status={result.status} "
            f"live_order_submitted={result.live_order_submitted} "
            f"fee_source={fee_source} "
            f"expected_loss_bps={expected_loss} "
            f"ledger_schema={ledger_schema}"
        )
        ok = ok and result.accepted and not result.live_order_submitted

    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
