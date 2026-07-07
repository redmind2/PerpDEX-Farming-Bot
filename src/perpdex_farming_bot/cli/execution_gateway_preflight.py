from __future__ import annotations

import argparse
from decimal import Decimal

from perpdex_farming_bot.core.execution_models import (
    ExecutionMode,
    ExecutionPreflightRequest,
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
    parser = argparse.ArgumentParser(
        description="Run Execution Gateway preflight checks without sending live orders.",
    )
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--exchange", choices=("all", *BUILTIN_GATEWAY_EXCHANGE_IDS), default="all")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Also run private read-only position/open-order checks. This may call exchange APIs but never sends orders.",
    )
    args = parser.parse_args()

    mode = ExecutionMode(args.mode)
    bindings = builtin_gateway_exchange_bindings()
    if args.exchange != "all":
        bindings = tuple(binding for binding in bindings if binding.exchange_id == args.exchange)
    gateway = build_gateway_from_bindings(bindings)

    print("gateway_preflight=True")
    print(f"mode={mode.value}")
    print(f"include_read_only={args.read_only}")
    print(f"registered_exchange_count={len(bindings)}")

    all_ready = True
    for binding in bindings:
        request = ExecutionPreflightRequest(
            request_id=f"gateway-preflight-{binding.exchange_id}-1",
            trade_intent=_trade_intent(binding.exchange_id, binding.account_alias, binding.default_market, mode),
            include_read_only=args.read_only,
            check_positions=args.read_only,
            check_open_orders=args.read_only and binding.open_orders_supported,
        )
        result = gateway.preflight(request)
        checks = ",".join(
            f"{check.name}:{check.status}:count={check.count}"
            for check in result.checks
        ) or "none"
        open_order_check = "enabled" if args.read_only and binding.open_orders_supported else (
            "skipped_unsupported" if args.read_only else "skipped"
        )
        print(
            f"exchange={binding.exchange_id} "
            f"market={binding.default_market} "
            f"ready={result.ready} "
            f"status={result.status} "
            f"reason={result.reason} "
            f"open_orders_check={open_order_check} "
            f"live_order_submitted={result.live_order_submitted} "
            f"checks={checks}"
        )
        all_ready = all_ready and result.ready and not result.live_order_submitted

    raise SystemExit(0 if all_ready else 1)


def _trade_intent(exchange_id: str, account_alias: str, market: str, mode: ExecutionMode) -> TradeIntent:
    order = OrderIntent(
        intent_id=f"{exchange_id}-preflight-order-1",
        exchange_id=exchange_id,
        market=market,
        side=OrderSide.BUY,
        order_type=OrderKind.LIMIT,
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        reference_price=Decimal("50000"),
        time_in_force="ioc",
    )
    return TradeIntent(
        intent_id=f"{exchange_id}-preflight-trade-1",
        strategy_id="gateway_preflight",
        account_alias=account_alias,
        exchange_id=exchange_id,
        market=market,
        mode=mode,
        orders=(order,),
        max_gross_notional_usd=Decimal("100"),
    )


if __name__ == "__main__":
    main()
