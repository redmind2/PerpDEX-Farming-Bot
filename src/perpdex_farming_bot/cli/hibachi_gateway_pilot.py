from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from perpdex_farming_bot.cli.hibachi_live_roundtrip import (
    _config_for_assignment,
    _enabled_assignment,
    _live_spread_allowed,
    _load_snapshot,
    _round_plan,
)
from perpdex_farming_bot.config import load_config
from perpdex_farming_bot.connectors import DataHubReadonlyConnector
from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    endpoint_from_env,
    validate_https_base_url,
)
from perpdex_farming_bot.core.execution_event import emit_execution_event
from perpdex_farming_bot.core.execution_models import (
    AccountPolicy,
    ExecutionMode,
    ExecutionPreflightRequest,
    OrderIntent,
    OrderKind,
    OrderSide,
    RoundtripMode,
    TradeIntent,
)
from perpdex_farming_bot.core.fee_provider import StaticFeeProvider
from perpdex_farming_bot.credentials import hibachi_available_credential_env, hibachi_credential_env
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.hibachi import HibachiAdapter
from perpdex_farming_bot.gateway.exchange_registry import LazyExchangeAdapterBridge
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


ACCOUNT_ALIAS = "hibachi_gateway"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Hibachi Execution Gateway pilot. Builds a TradeIntent from the existing Hibachi "
            "market-market preflight path and routes it through Gateway dry-run/paper checks. "
            "This CLI never sends live orders."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hibachi.paper.json")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="HIBACHI_1_CRYPTO")
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--network", action="store_true", help="Read live public/private state. Never sends orders.")
    parser.add_argument("--read-only", action="store_true", help="Also run private read-only position/open-order checks through Gateway.")
    parser.add_argument("--market", default="BTC/USDT-P")
    parser.add_argument("--data-source", choices=("data-hub-window-min", "market-config"), default="market-config")
    parser.add_argument("--data-hub-db", default=get_env("PERPDEX_DATA_HUB_DB") or "")
    parser.add_argument("--data-hub-immutable", action="store_true")
    parser.add_argument("--markets-config", default=get_env("PERPDEX_DATA_HUB_MARKETS_CONFIG") or "config/markets.json")
    parser.add_argument("--orderbook-source", choices=("hibachi-sdk",), default="hibachi-sdk")
    parser.add_argument("--orderbook-depth", type=int, default=5)
    parser.add_argument("--orderbook-granularity", type=float, default=0.0)
    parser.add_argument("--average-spread-samples", type=int, default=12)
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("5"))
    parser.add_argument("--max-fees-percent", type=Decimal, default=Decimal("0.0005"))
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=Decimal("0"))
    parser.add_argument("--close-mode", choices=("confirmed", "fast-reduce-only", "netting"), default="netting")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    if args.read_only and not args.network:
        raise SystemExit("--read-only requires --network because it calls private read-only APIs")
    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > Decimal("100"):
        raise SystemExit("--max-notional-usd must be <= 100 for this guarded pilot")
    if args.target_gross_volume_usd < 0:
        raise SystemExit("--target-gross-volume-usd must be zero or greater")
    if args.target_gross_volume_usd > Decimal("100"):
        raise SystemExit("--target-gross-volume-usd must be <= 100 for this guarded pilot")

    config = load_config(Path(args.config))
    assignment = _enabled_assignment(config, args.market)
    run_config = _config_for_assignment(config, assignment)
    mode = ExecutionMode(args.mode)
    roundtrip_mode = _roundtrip_mode(args.close_mode)
    credential_env = hibachi_credential_env(args.credential_prefix)
    available_env = hibachi_available_credential_env(args.credential_prefix)
    api_endpoint = validate_https_base_url(
        "HIBACHI_API_ENDPOINT_PRODUCTION",
        endpoint_from_env(get_env("HIBACHI_API_ENDPOINT_PRODUCTION"), DEFAULT_HIBACHI_API_ENDPOINT),
    )
    data_endpoint = validate_https_base_url(
        "HIBACHI_DATA_API_ENDPOINT_PRODUCTION",
        endpoint_from_env(get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"), DEFAULT_HIBACHI_DATA_API_ENDPOINT),
    )

    print("hibachi_gateway_pilot=True")
    print(f"env_file_loaded={env_loaded}")
    print(f"mode={mode.value}")
    print(f"network={args.network}")
    print(f"include_read_only={args.read_only}")
    print(f"api_endpoint={api_endpoint}")
    print(f"data_api_endpoint={data_endpoint}")
    print(f"config={args.config}")
    print(f"credential_prefix={credential_env.prefix}")
    if available_env is not None and available_env.prefix != credential_env.prefix:
        print(f"credential_prefix_status=usable_via_legacy:{available_env.prefix}")
    elif available_env is not None:
        print("credential_prefix_status=usable")
    else:
        print("credential_prefix_status=missing")
    print(f"market={assignment.market}")
    print(f"data_source={args.data_source}")
    print(f"max_notional_usd={args.max_notional_usd}")
    print(f"max_fees_percent={args.max_fees_percent}")
    print(f"close_mode={args.close_mode}")
    print("execute_live=False")
    print("live_order_submitted=False")
    print("gateway_live_orders_enabled=False")
    for candidate in (credential_env,):
        for name in candidate.required_names:
            print(f"primary_{name}={masked_env_status(name)}")

    if args.network:
        data_hub = (
            DataHubReadonlyConnector(args.data_hub_db, immutable=args.data_hub_immutable)
            if args.data_source == "data-hub-window-min"
            else None
        )
        snapshot_result = _load_snapshot(args, run_config, assignment, data_hub)
        if not snapshot_result.ok or snapshot_result.snapshot is None:
            print("gateway_ready=False")
            print(f"reason={snapshot_result.reason}")
            raise SystemExit(1)
        snapshot = snapshot_result.snapshot
        spread_allowed, spread_reason = _live_spread_allowed(config, snapshot)
        print(f"gateway_market_data_reason={snapshot_result.reason}")
        print(f"gateway_spread_allowed={spread_allowed}")
        print(f"gateway_spread_reason={spread_reason}")
        print(f"gateway_spread_bps={snapshot.spread_bps:.4f}")
        print(f"gateway_threshold_bps={snapshot.average_spread_bps:.4f}")
        if not spread_allowed:
            print("gateway_ready=False")
            print(f"reason={spread_reason}")
            raise SystemExit(1)
        quantity, notional, first_side, second_side = _round_plan(args, config, snapshot)
        if quantity <= 0:
            print("gateway_ready=False")
            print("reason=quantity_zero")
            raise SystemExit(1)
        planned_gross = notional * Decimal("2")
        trade_intent = _trade_intent_from_plan(
            market=assignment.market,
            mode=mode,
            roundtrip_mode=roundtrip_mode,
            quantity=quantity,
            first_side=first_side,
            second_side=second_side,
            best_bid=Decimal(str(snapshot.best_bid.price)),
            best_ask=Decimal(str(snapshot.best_ask.price)),
            planned_gross=planned_gross,
        )
        print(f"gateway_planned_quantity={quantity}")
        print(f"gateway_planned_one_side_notional_usd={notional:.4f}")
        print(f"gateway_planned_gross_volume_usd={planned_gross:.4f}")
        print(f"gateway_planned_first_side={first_side}")
        print(f"gateway_planned_second_side={second_side}")
    else:
        trade_intent = _synthetic_trade_intent(mode=mode, roundtrip_mode=roundtrip_mode, market=assignment.market)
        planned_gross = trade_intent.planned_gross_notional_usd or Decimal("50")
        print("network_skipped=using_synthetic_trade_intent")

    fee_bps = args.max_fees_percent * Decimal("10000")
    gateway = _build_gateway(
        credential_prefix=args.credential_prefix,
        account_alias=ACCOUNT_ALIAS,
        market=trade_intent.market,
        fee_bps=fee_bps,
        fee_source="hibachi_max_fees_percent_conservative",
        max_order_notional_usd=args.max_notional_usd + Decimal("1"),
        max_gross_notional_usd=max(planned_gross, args.target_gross_volume_usd, Decimal("50")),
    )
    preflight = gateway.preflight(
        ExecutionPreflightRequest(
            request_id="hibachi-gateway-pilot-1",
            trade_intent=trade_intent,
            include_read_only=args.read_only,
            check_positions=args.read_only,
            check_open_orders=args.read_only,
        )
    )
    print(f"gateway_ready={preflight.ready}")
    print(f"gateway_status={preflight.status}")
    print(f"gateway_reason={preflight.reason}")
    print(f"gateway_accepted={preflight.execution_result.accepted}")
    print(f"gateway_execution_status={preflight.execution_result.status}")
    print(f"gateway_live_order_submitted={preflight.live_order_submitted}")
    for check in preflight.checks:
        print(f"gateway_check={check.name}:{check.status}:ok={check.ok}:count={check.count}")
    if preflight.execution_result.cost_quote is not None:
        cost = preflight.execution_result.cost_quote
        expected = _fmt_decimal(cost.expected_loss_bps) if cost.expected_loss_bps is not None else "unknown"
        fee_usd = _fmt_decimal(cost.estimated_fee_usd) if cost.estimated_fee_usd is not None else "unknown"
        print(f"gateway_expected_loss_bps={expected}")
        print(f"gateway_estimated_fee_usd={fee_usd}")
    if preflight.execution_result.ledger_event is not None:
        emit_execution_event(preflight.execution_result.ledger_event)
    if not preflight.ready:
        raise SystemExit(1)


def _trade_intent_from_plan(
    *,
    market: str,
    mode: ExecutionMode,
    roundtrip_mode: RoundtripMode,
    quantity: Decimal,
    first_side: str,
    second_side: str,
    best_bid: Decimal,
    best_ask: Decimal,
    planned_gross: Decimal,
) -> TradeIntent:
    first = _order_intent("hibachi-gateway-first-1", market, first_side, quantity, best_bid, best_ask, False)
    second = _order_intent(
        "hibachi-gateway-second-1",
        market,
        second_side,
        quantity,
        best_bid,
        best_ask,
        roundtrip_mode is not RoundtripMode.NETTING,
    )
    return TradeIntent(
        intent_id="hibachi-gateway-trade-1",
        strategy_id="hibachi_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="hibachi",
        market=market,
        mode=mode,
        orders=(first, second),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=planned_gross,
        metadata={"planned_gross_volume_usd": str(planned_gross), "gateway_pilot": True},
    )


def _synthetic_trade_intent(*, mode: ExecutionMode, roundtrip_mode: RoundtripMode, market: str) -> TradeIntent:
    first = _order_intent(
        "hibachi-gateway-synthetic-buy-1",
        market,
        "BUY",
        Decimal("0.00005"),
        Decimal("59999"),
        Decimal("60000"),
        False,
    )
    second = _order_intent(
        "hibachi-gateway-synthetic-sell-1",
        market,
        "SELL",
        Decimal("0.00005"),
        Decimal("59999"),
        Decimal("60000"),
        roundtrip_mode is not RoundtripMode.NETTING,
    )
    return TradeIntent(
        intent_id="hibachi-gateway-synthetic-trade-1",
        strategy_id="hibachi_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="hibachi",
        market=market,
        mode=mode,
        orders=(first, second),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=Decimal("50"),
    )


def _order_intent(
    intent_id: str,
    market: str,
    side: str,
    quantity: Decimal,
    best_bid: Decimal,
    best_ask: Decimal,
    reduce_only: bool,
) -> OrderIntent:
    order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
    reference_price = best_ask if order_side is OrderSide.BUY else best_bid
    return OrderIntent(
        intent_id=intent_id,
        exchange_id="hibachi",
        market=market,
        side=order_side,
        order_type=OrderKind.MARKET,
        quantity=quantity,
        reference_price=reference_price,
        reduce_only=reduce_only,
        metadata={"source": "hibachi_gateway_pilot"},
    )


def _roundtrip_mode(close_mode: str) -> RoundtripMode:
    if close_mode == "netting":
        return RoundtripMode.NETTING
    if close_mode == "fast-reduce-only":
        return RoundtripMode.FAST_REDUCE_ONLY
    return RoundtripMode.CONFIRMED


def _build_gateway(
    *,
    credential_prefix: str,
    account_alias: str,
    market: str,
    fee_bps: Decimal,
    fee_source: str,
    max_order_notional_usd: Decimal,
    max_gross_notional_usd: Decimal,
) -> ExecutionGateway:
    adapter = LazyExchangeAdapterBridge(
        exchange_id="hibachi",
        adapter_factory=lambda: HibachiAdapter(credential_prefix=credential_prefix),
        open_orders_supported=True,
    )
    return ExecutionGateway(
        account_policies={
            account_alias: AccountPolicy(
                account_alias=account_alias,
                allowed_modes=(ExecutionMode.DRY_RUN, ExecutionMode.PAPER),
                allow_live=False,
                require_fee_quote=True,
                max_order_notional_usd=max_order_notional_usd,
                max_gross_notional_usd=max_gross_notional_usd,
                kill_switch_required=True,
            )
        },
        fee_provider=StaticFeeProvider(
            exchange_id="hibachi",
            account_alias=account_alias,
            entry_fee_bps=fee_bps,
            exit_fee_bps=fee_bps,
            taker_fee_bps=fee_bps,
            source=fee_source,
            markets=(market,),
        ),
        adapters={"hibachi": adapter},
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=False,
    )


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


if __name__ == "__main__":
    main()
