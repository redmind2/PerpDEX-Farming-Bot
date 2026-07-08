from __future__ import annotations

import argparse
import importlib.util
from decimal import Decimal

from perpdex_farming_bot.cli.lighter_live_test import (
    DEFAULT_BTC_MARKET_ID,
    DEFAULT_BTC_SYMBOL,
    _build_and_print_plan,
    _validate_args,
    fmt_decimal,
)
from perpdex_farming_bot.connectors.lighter_readonly import (
    LighterReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_lighter_environment,
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
from perpdex_farming_bot.credentials import lighter_credential_env, lighter_signing_missing
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.lighter import LighterAdapter
from perpdex_farming_bot.gateway.exchange_registry import LazyExchangeAdapterBridge
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


ACCOUNT_ALIAS = "lighter_gateway"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Lighter Execution Gateway pilot. Builds a TradeIntent and routes it through "
            "Gateway dry-run/paper checks. This CLI never sends live orders."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="LIGHTER")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--network", action="store_true", help="Read live public orderbook data. Never sends orders.")
    parser.add_argument("--read-only", action="store_true", help="Also run private read-only position/open-order checks through Gateway.")
    parser.add_argument("--symbol", default=DEFAULT_BTC_SYMBOL)
    parser.add_argument("--market-id", type=int, default=DEFAULT_BTC_MARKET_ID)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--quote-amount-usd", type=Decimal, default=Decimal("25"))
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("30"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("50"))
    parser.add_argument("--max-spread-bps", type=Decimal, default=Decimal("20"))
    parser.add_argument("--orderbook-limit", type=int, default=20)
    parser.add_argument("--settle-attempts", type=int, default=20)
    parser.add_argument("--settle-delay-seconds", type=float, default=0.1)
    parser.add_argument("--trade-poll-attempts", type=int, default=6)
    parser.add_argument("--trade-poll-delay-seconds", type=float, default=0.5)
    parser.add_argument("--close-mode", choices=("confirmed", "optimistic", "netting"), default="netting")
    parser.add_argument("--optimistic-close-delay-seconds", type=float, default=0.15)
    args = parser.parse_args()
    _validate_args(args)
    if args.read_only and not args.network:
        raise SystemExit("--read-only requires --network because it calls private read-only APIs")

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_lighter_environment(args.environment)
    mode = ExecutionMode(args.mode)
    roundtrip_mode = _roundtrip_mode(args.close_mode)
    credential_env = lighter_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("lighter_gateway_pilot=True")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"mode={mode.value}")
    print(f"network={args.network}")
    print(f"include_read_only={args.read_only}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"symbol={args.symbol}")
    print(f"market_id={args.market_id}")
    print(f"quote_amount_usd={fmt_decimal(args.quote_amount_usd)}")
    print(f"max_notional_usd={fmt_decimal(args.max_notional_usd)}")
    print(f"close_mode={args.close_mode}")
    print("execute_live=False")
    print("live_order_submitted=False")
    print("gateway_live_orders_enabled=False")
    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except LighterReadonlyConfigError as exc:
        print("gateway_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    signing_missing = lighter_signing_missing(args.credential_prefix, environment)
    print(f"primary_{credential_env.l1_address}={masked_env_status(credential_env.l1_address)}")
    print(f"primary_{credential_env.account_index}={masked_env_status(credential_env.account_index)}")
    print(f"primary_{credential_env.api_key_index}={masked_env_status(credential_env.api_key_index)}")
    print(f"primary_{credential_env.api_private_key}={masked_env_status(credential_env.api_private_key)}")
    print(f"optional_{credential_env.read_only_auth_token}={masked_env_status(credential_env.read_only_auth_token)}")
    print(f"signing_env_ready={not signing_missing}")
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))
    print(f"lighter_sdk_installed={importlib.util.find_spec('lighter') is not None}")

    if args.network:
        if args.read_only and not get_env(credential_env.read_only_auth_token):
            print("gateway_ready=False")
            print("reason=lighter_read_only_auth_token_missing")
            raise SystemExit(1)
        plan = _build_and_print_plan(api_endpoint, args, "gateway_plan")
        if plan is None or not plan.eligible:
            print("gateway_ready=False")
            print("reason=no_lighter_gateway_candidate")
            raise SystemExit(1)
        fee_bps = _fee_bps_from_percent(plan.taker_fee_percent)
        if fee_bps is None:
            print("gateway_ready=False")
            print("reason=lighter_taker_fee_unknown")
            raise SystemExit(1)
        trade_intent = _trade_intent_from_plan(plan, mode=mode, roundtrip_mode=roundtrip_mode)
        market = trade_intent.market
        fee_source = "lighter_market_metadata_percentage_taker_fee"
        planned_gross = plan.planned_one_side_notional_usd * Decimal("2")
    else:
        trade_intent = _synthetic_trade_intent(mode=mode, roundtrip_mode=roundtrip_mode, symbol=args.symbol)
        market = trade_intent.market
        fee_bps = Decimal("3")
        fee_source = "lighter_gateway_pilot_static_no_network"
        planned_gross = trade_intent.planned_gross_notional_usd or Decimal("50")
        print("network_skipped=using_synthetic_trade_intent")

    gateway = _build_gateway(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        account_alias=ACCOUNT_ALIAS,
        market=market,
        fee_bps=fee_bps,
        fee_source=fee_source,
        max_order_notional_usd=args.max_notional_usd + Decimal("1"),
        max_gross_notional_usd=max(planned_gross, args.max_notional_usd * Decimal("2")),
        timeout_seconds=args.timeout_seconds,
    )
    preflight = gateway.preflight(
        ExecutionPreflightRequest(
            request_id="lighter-gateway-pilot-1",
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
        expected = fmt_decimal(cost.expected_loss_bps) if cost.expected_loss_bps is not None else "unknown"
        fee_usd = fmt_decimal(cost.estimated_fee_usd) if cost.estimated_fee_usd is not None else "unknown"
        print(f"gateway_expected_loss_bps={expected}")
        print(f"gateway_estimated_fee_usd={fee_usd}")
    if preflight.execution_result.ledger_event is not None:
        emit_execution_event(preflight.execution_result.ledger_event)
    if not preflight.ready:
        raise SystemExit(1)


def _trade_intent_from_plan(plan, *, mode: ExecutionMode, roundtrip_mode: RoundtripMode) -> TradeIntent:
    market = f"{plan.symbol}-PERP"
    buy = OrderIntent(
        intent_id="lighter-gateway-buy-1",
        exchange_id="lighter",
        market=market,
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=plan.planned_size,
        reference_price=plan.best_ask,
        reduce_only=False,
        metadata={"source": "lighter_gateway_pilot", "market_id": plan.market_id},
    )
    sell = OrderIntent(
        intent_id="lighter-gateway-sell-1",
        exchange_id="lighter",
        market=market,
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=plan.planned_size,
        reference_price=plan.best_bid,
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
        metadata={"source": "lighter_gateway_pilot", "market_id": plan.market_id},
    )
    return TradeIntent(
        intent_id="lighter-gateway-trade-1",
        strategy_id="lighter_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="lighter",
        market=market,
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=plan.planned_one_side_notional_usd * Decimal("2"),
        metadata={
            "market_id": plan.market_id,
            "spread_bps": str(plan.spread_bps),
            "planned_gross_volume_usd": str(plan.planned_one_side_notional_usd * Decimal("2")),
            "gateway_pilot": True,
        },
    )


def _synthetic_trade_intent(*, mode: ExecutionMode, roundtrip_mode: RoundtripMode, symbol: str) -> TradeIntent:
    market = f"{symbol}-PERP"
    buy = OrderIntent(
        intent_id="lighter-gateway-synthetic-buy-1",
        exchange_id="lighter",
        market=market,
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("60000"),
        reduce_only=False,
    )
    sell = OrderIntent(
        intent_id="lighter-gateway-synthetic-sell-1",
        exchange_id="lighter",
        market=market,
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("59999"),
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
    )
    return TradeIntent(
        intent_id="lighter-gateway-synthetic-trade-1",
        strategy_id="lighter_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="lighter",
        market=market,
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=Decimal("50"),
    )


def _roundtrip_mode(close_mode: str) -> RoundtripMode:
    if close_mode == "netting":
        return RoundtripMode.NETTING
    if close_mode == "optimistic":
        return RoundtripMode.FAST_REDUCE_ONLY
    return RoundtripMode.CONFIRMED


def _fee_bps_from_percent(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value * Decimal("100")


def _build_gateway(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    account_alias: str,
    market: str,
    fee_bps: Decimal,
    fee_source: str,
    max_order_notional_usd: Decimal,
    max_gross_notional_usd: Decimal,
    timeout_seconds: float,
) -> ExecutionGateway:
    adapter = LazyExchangeAdapterBridge(
        exchange_id="lighter",
        adapter_factory=lambda: LighterAdapter(
            api_endpoint=api_endpoint,
            credential_prefix=credential_prefix,
            environment=environment,
            timeout_seconds=timeout_seconds,
            allow_live_orders=False,
        ),
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
            exchange_id="lighter",
            account_alias=account_alias,
            entry_fee_bps=fee_bps,
            exit_fee_bps=fee_bps,
            taker_fee_bps=fee_bps,
            source=fee_source,
            markets=(market,),
        ),
        adapters={"lighter": adapter},
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=False,
    )


if __name__ == "__main__":
    main()
