from __future__ import annotations

import argparse
from decimal import Decimal

from perpdex_farming_bot.cli.risex_live_volume import (
    _all_markets_flat,
    _choose_plan,
    _common_args,
    _load_account_fee_or_none,
    _load_markets,
    _normalize_close_mode_args,
    _parse_market_ids,
    _print_plan,
    _system_and_session_ready,
    _validate_args,
    fmt_decimal,
)
from perpdex_farming_bot.connectors.risex_readonly import (
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_risex_environment,
    read_only_get_json,
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
from perpdex_farming_bot.credentials import read_risex_credentials, risex_credential_env
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.risex import RisexAdapter
from perpdex_farming_bot.exchanges.risex_fees import (
    RisexFeeProvider,
    risex_fee_overrides_from_config,
    risex_market_fee_metadata_from_markets,
)
from perpdex_farming_bot.gateway.exchange_registry import LazyExchangeAdapterBridge
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


ACCOUNT_ALIAS = "risex_gateway"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "RiseX Execution Gateway pilot. Builds a TradeIntent and routes it through "
            "Gateway dry-run/paper checks. This CLI never sends live orders."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="RISEX")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--network", action="store_true", help="Read live orderbook/account fee/read-only state. Never sends orders.")
    parser.add_argument("--read-only", action="store_true", help="Also run private read-only position/open-order checks through Gateway.")
    parser.add_argument("--market-ids", default="1,2,4,5")
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=Decimal("1000"))
    parser.add_argument("--max-leg-notional-usd", type=Decimal, default=Decimal("100"))
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--spread-bps", type=Decimal, default=Decimal("1"))
    parser.add_argument("--max-expected-loss-bps", type=Decimal, default=None)
    parser.add_argument("--book-fraction", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--fee-config", default="config/risex.live-volume.json")
    parser.add_argument("--fee-history-limit", type=int, default=100)
    parser.add_argument("--loop-delay-seconds", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--deadline-seconds", type=int, default=300)
    parser.add_argument("--position-settle-attempts", type=int, default=8)
    parser.add_argument("--position-settle-delay-seconds", type=float, default=0.7)
    parser.add_argument("--fast-close-on-fill", action="store_true")
    parser.add_argument("--prebuild-close-order", action="store_true")
    parser.add_argument("--close-mode", choices=("confirmed", "fast-reduce-only", "netting"), default="netting")
    args = parser.parse_args()
    args.execute_live = False
    args.confirm = ""
    _normalize_close_mode_args(args)
    _validate_args(args)

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_risex_environment(args.environment)
    mode = ExecutionMode(args.mode)
    market_ids = _parse_market_ids(args.market_ids)
    credential_env = risex_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    api_endpoint = validate_https_base_url(api_name, api_endpoint)

    print("risex_gateway_pilot=True")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"mode={mode.value}")
    print(f"network={args.network}")
    print(f"include_read_only={args.read_only}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"market_ids={','.join(str(item) for item in market_ids)}")
    print(f"target_gross_volume_usd={fmt_decimal(args.target_gross_volume_usd)}")
    print(f"max_leg_notional_usd={fmt_decimal(args.max_leg_notional_usd)}")
    print(f"close_mode={args.close_mode}")
    print("execute_live=False")
    print("live_order_submitted=False")
    print("gateway_live_orders_enabled=False")
    print(f"{api_name}={api_endpoint}")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.signer_address}={masked_env_status(credential_env.signer_address)}")
    print(f"primary_{credential_env.signer_private_key}={masked_env_status(credential_env.signer_private_key)}")

    if args.network:
        credentials = read_risex_credentials(args.credential_prefix, environment)
        common_args = _common_args(args, market_ids[0])
        adapter_probe = RisexAdapter(
            api_endpoint=api_endpoint,
            credential_prefix=args.credential_prefix,
            environment=environment,
            timeout_seconds=args.timeout_seconds,
            allow_live_orders=False,
        )
        signer_ok, signer_reason = adapter_probe.signer_ready()
        print(f"signer_ready={signer_ok}")
        print(f"signer_ready_reason={signer_reason}")
        if not signer_ok:
            print("gateway_ready=False")
            print("reason=risex_signer_not_ready")
            raise SystemExit(1)
        if not _system_and_session_ready(api_endpoint, credentials, common_args):
            print("gateway_ready=False")
            print("reason=risex_system_or_session_not_ready")
            raise SystemExit(1)
        if args.read_only:
            start_state = _all_markets_flat(api_endpoint, credentials["account_address"], market_ids, common_args)
            if not start_state.all_flat:
                print("gateway_ready=False")
                print("reason=existing_open_orders_or_positions_detected")
                raise SystemExit(1)
        plan, fee_provider = _network_plan_and_fee_provider(
            api_endpoint=api_endpoint,
            account=credentials["account_address"],
            market_ids=market_ids,
            args=args,
        )
        _print_plan("gateway_plan", plan)
        trade_intent = _trade_intent_from_plan(plan, mode=mode, roundtrip_mode=_roundtrip_mode(args.close_mode))
        market = str(plan.market.market_id)
        entry_fee_bps = plan.entry_fee_bps
        exit_fee_bps = plan.exit_fee_bps
        fee_source = plan.fee_source
        del fee_provider
    else:
        trade_intent = _synthetic_trade_intent(mode=mode, roundtrip_mode=_roundtrip_mode(args.close_mode))
        market = trade_intent.market
        entry_fee_bps = Decimal("3")
        exit_fee_bps = Decimal("3")
        fee_source = "risex_gateway_pilot_static_no_network"
        print("network_skipped=using_synthetic_trade_intent")

    gateway = _build_gateway(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        account_alias=ACCOUNT_ALIAS,
        market=market,
        entry_fee_bps=entry_fee_bps,
        exit_fee_bps=exit_fee_bps,
        fee_source=fee_source,
        max_order_notional_usd=args.max_leg_notional_usd + Decimal("1"),
        max_gross_notional_usd=args.target_gross_volume_usd,
        timeout_seconds=args.timeout_seconds,
    )
    preflight = gateway.preflight(
        ExecutionPreflightRequest(
            request_id="risex-gateway-pilot-1",
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
        print(f"gateway_expected_loss_bps={fmt_decimal(cost.expected_loss_bps) if cost.expected_loss_bps is not None else 'unknown'}")
        print(f"gateway_estimated_fee_usd={fmt_decimal(cost.estimated_fee_usd) if cost.estimated_fee_usd is not None else 'unknown'}")
    if preflight.execution_result.ledger_event is not None:
        emit_execution_event(preflight.execution_result.ledger_event)
    if not preflight.ready:
        raise SystemExit(1)


def _network_plan_and_fee_provider(
    *,
    api_endpoint: str,
    account: str,
    market_ids: tuple[int, ...],
    args: argparse.Namespace,
) -> tuple[object, RisexFeeProvider]:
    markets = _load_markets(api_endpoint, market_ids, args.timeout_seconds)
    if not markets:
        print("gateway_ready=False")
        print("reason=no_risex_markets_loaded")
        raise SystemExit(1)
    market_payload = read_only_get_json(api_endpoint, "/v1/markets", {}, args.timeout_seconds)
    account_fee = _load_account_fee_or_none(api_endpoint, account, args)
    fee_provider = RisexFeeProvider(
        override_by_market=risex_fee_overrides_from_config(args.fee_config),
        metadata_by_market=risex_market_fee_metadata_from_markets(market_payload),
        account_fee=account_fee,
    )
    plan = _choose_plan(api_endpoint, markets, args, args.target_gross_volume_usd, fee_provider)
    if plan is None:
        print("gateway_ready=False")
        print("reason=no_risex_gateway_candidate")
        raise SystemExit(1)
    return plan, fee_provider


def _trade_intent_from_plan(plan, *, mode: ExecutionMode, roundtrip_mode: RoundtripMode) -> TradeIntent:
    market = str(plan.market.market_id)
    buy = OrderIntent(
        intent_id="risex-gateway-buy-1",
        exchange_id="risex",
        market=market,
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=plan.size,
        reference_price=plan.best_ask,
        reduce_only=False,
        metadata={"source": "risex_gateway_pilot", "market_name": plan.market.name},
    )
    sell = OrderIntent(
        intent_id="risex-gateway-sell-1",
        exchange_id="risex",
        market=market,
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=plan.size,
        reference_price=plan.best_bid,
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
        metadata={"source": "risex_gateway_pilot", "market_name": plan.market.name},
    )
    return TradeIntent(
        intent_id="risex-gateway-trade-1",
        strategy_id="risex_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="risex",
        market=market,
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=plan.estimated_roundtrip_notional,
        metadata={
            "market_name": plan.market.name,
            "spread_bps": str(plan.spread_bps),
            "planned_gross_volume_usd": str(plan.estimated_roundtrip_notional),
            "gateway_pilot": True,
        },
    )


def _synthetic_trade_intent(*, mode: ExecutionMode, roundtrip_mode: RoundtripMode) -> TradeIntent:
    buy = OrderIntent(
        intent_id="risex-gateway-synthetic-buy-1",
        exchange_id="risex",
        market="1",
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("60000"),
        reduce_only=False,
    )
    sell = OrderIntent(
        intent_id="risex-gateway-synthetic-sell-1",
        exchange_id="risex",
        market="1",
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("59999"),
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
    )
    return TradeIntent(
        intent_id="risex-gateway-synthetic-trade-1",
        strategy_id="risex_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="risex",
        market="1",
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=Decimal("50"),
    )


def _roundtrip_mode(close_mode: str) -> RoundtripMode:
    if close_mode == "netting":
        return RoundtripMode.NETTING
    if close_mode == "fast-reduce-only":
        return RoundtripMode.FAST_REDUCE_ONLY
    return RoundtripMode.CONFIRMED


def _build_gateway(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    account_alias: str,
    market: str,
    entry_fee_bps: Decimal,
    exit_fee_bps: Decimal,
    fee_source: str,
    max_order_notional_usd: Decimal,
    max_gross_notional_usd: Decimal,
    timeout_seconds: float,
) -> ExecutionGateway:
    adapter = LazyExchangeAdapterBridge(
        exchange_id="risex",
        adapter_factory=lambda: RisexAdapter(
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
            exchange_id="risex",
            account_alias=account_alias,
            entry_fee_bps=entry_fee_bps,
            exit_fee_bps=exit_fee_bps,
            taker_fee_bps=entry_fee_bps,
            source=fee_source,
            markets=(market,),
        ),
        adapters={"risex": adapter},
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=False,
    )


if __name__ == "__main__":
    main()
