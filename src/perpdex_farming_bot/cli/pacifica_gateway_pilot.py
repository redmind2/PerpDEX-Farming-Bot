from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from perpdex_farming_bot.cli.pacifica_live_common import (
    PacificaFeeProvider,
    fmt_decimal,
    load_all_pacifica_market_info,
)
from perpdex_farming_bot.cli.pacifica_live_volume import (
    _fee_overrides_by_symbol,
    _load_account_fee_or_none,
    _load_market_configs,
    _normalize_close_mode_args,
    _print_candidate,
    _select_candidate,
    _validate_args,
)
from perpdex_farming_bot.connectors.pacifica_readonly import (
    PacificaReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_pacifica_environment,
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
from perpdex_farming_bot.credentials import pacifica_credential_env
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.pacifica import PacificaAdapter
from perpdex_farming_bot.gateway.exchange_registry import LazyExchangeAdapterBridge
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


ACCOUNT_ALIAS = "pacifica_gateway"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Pacifica Execution Gateway pilot. Selects a volume candidate and routes a TradeIntent "
            "through Gateway dry-run/paper checks. This CLI never sends live orders."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/pacifica.live-volume.json")
    parser.add_argument("--environment", default=None)
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="PACIFICA")
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--network", action="store_true", help="Read live public/private read-only data. Never sends orders.")
    parser.add_argument("--read-only", action="store_true", help="Also run private read-only position/open-order checks through Gateway.")
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=None)
    parser.add_argument("--order-notional-usd", type=Decimal, default=None)
    parser.add_argument("--level-size-fraction", type=Decimal, default=None)
    parser.add_argument("--threshold-source", choices=("current", "5m", "1h", "24h"), default=None)
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--max-idle-cycles", type=int, default=20)
    parser.add_argument("--loop-delay-seconds", type=float, default=0.0)
    parser.add_argument("--min-entry-delay-seconds", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--poll-seconds", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--agg-level", type=int, default=1)
    parser.add_argument("--slippage-percent", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--expiry-window-ms", type=int, default=5000)
    parser.add_argument("--post-order-wait-seconds", type=float, default=0.0)
    parser.add_argument("--position-settle-attempts", type=int, default=8)
    parser.add_argument("--position-settle-delay-seconds", type=float, default=0.5)
    parser.add_argument("--fast-close-on-fill", action="store_true")
    parser.add_argument("--prebuild-close-order", action="store_true")
    parser.add_argument("--close-mode", choices=("confirmed", "fast-reduce-only", "netting"), default="netting")
    args = parser.parse_args()
    _normalize_close_mode_args(args)

    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = normalize_pacifica_environment(args.environment or str(plan.get("environment", "production")))
    target_gross_volume_usd = args.target_gross_volume_usd or Decimal(str(plan["target_gross_volume_usd"]))
    order_notional_usd = args.order_notional_usd or Decimal(str(plan["order_notional_usd"]))
    level_size_fraction = args.level_size_fraction or Decimal(str(plan["level_size_fraction"]))
    threshold_source = args.threshold_source or str(plan.get("threshold_source", "24h"))
    markets = _load_market_configs(plan)
    _validate_args(args, target_gross_volume_usd, order_notional_usd, level_size_fraction)

    env_loaded = load_dotenv_if_present(args.env_file)
    mode = ExecutionMode(args.mode)
    roundtrip_mode = _roundtrip_mode(args.close_mode)
    credential_env = pacifica_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("pacifica_gateway_pilot=True")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"mode={mode.value}")
    print(f"network={args.network}")
    print(f"include_read_only={args.read_only}")
    print(f"config={args.config}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"target_gross_volume_usd={target_gross_volume_usd}")
    print(f"order_notional_usd={order_notional_usd}")
    print(f"level_size_fraction={level_size_fraction}")
    print(f"threshold_source={threshold_source}")
    print(f"close_mode={args.close_mode}")
    print("execute_live=False")
    print("live_order_submitted=False")
    print("gateway_live_orders_enabled=False")
    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except PacificaReadonlyConfigError as exc:
        print("gateway_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_agent_public_key}={masked_env_status(credential_env.api_agent_public_key)}")
    print(f"primary_{credential_env.api_agent_private_key}={masked_env_status(credential_env.api_agent_private_key)}")

    if args.network:
        account = get_env(credential_env.account_address)
        account_fee = _load_account_fee_or_none(api_endpoint, account, args) if account else None
        if account_fee is None:
            print("account_fee_available=False")
        market_infos = load_all_pacifica_market_info(api_endpoint, args.timeout_seconds)
        fee_provider = PacificaFeeProvider(
            market_info_by_symbol=market_infos,
            override_by_symbol=_fee_overrides_by_symbol(markets),
            account_fee=account_fee,
        )
        print(f"configured_market_count={len(markets)}")
        print(f"exchange_market_info_count={len(market_infos)}")
        candidate = _select_candidate(
            api_endpoint=api_endpoint,
            markets=markets,
            market_infos=market_infos,
            fee_provider=fee_provider,
            threshold_source=threshold_source,
            remaining_gross_volume_usd=target_gross_volume_usd,
            order_notional_usd=order_notional_usd,
            level_size_fraction=level_size_fraction,
            timeout_seconds=args.timeout_seconds,
            agg_level=args.agg_level,
        )
        if candidate is None:
            print("gateway_ready=False")
            print("reason=no_pacifica_gateway_candidate")
            raise SystemExit(1)
        _print_candidate("gateway_selected", candidate)
        trade_intent = _trade_intent_from_candidate(candidate, mode=mode, roundtrip_mode=roundtrip_mode)
        market = trade_intent.market
        entry_fee_bps = candidate.entry_fee_bps
        exit_fee_bps = candidate.exit_fee_bps
        fee_source = candidate.fee_source
        planned_gross = candidate.planned_gross_volume_usd
    else:
        trade_intent = _synthetic_trade_intent(mode=mode, roundtrip_mode=roundtrip_mode)
        market = trade_intent.market
        entry_fee_bps = Decimal("3")
        exit_fee_bps = Decimal("3")
        fee_source = "pacifica_gateway_pilot_static_no_network"
        planned_gross = trade_intent.planned_gross_notional_usd or Decimal("50")
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
        max_order_notional_usd=order_notional_usd + Decimal("1"),
        max_gross_notional_usd=max(planned_gross, target_gross_volume_usd),
        timeout_seconds=args.timeout_seconds,
    )
    preflight = gateway.preflight(
        ExecutionPreflightRequest(
            request_id="pacifica-gateway-pilot-1",
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


def _trade_intent_from_candidate(candidate, *, mode: ExecutionMode, roundtrip_mode: RoundtripMode) -> TradeIntent:
    market = candidate.config.display_market
    buy = OrderIntent(
        intent_id="pacifica-gateway-buy-1",
        exchange_id="pacifica",
        market=market,
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=candidate.amount,
        reference_price=candidate.top_of_book.best_ask,
        reduce_only=False,
        metadata={"source": "pacifica_gateway_pilot", "symbol": candidate.config.symbol},
    )
    sell = OrderIntent(
        intent_id="pacifica-gateway-sell-1",
        exchange_id="pacifica",
        market=market,
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=candidate.amount,
        reference_price=candidate.top_of_book.best_bid,
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
        metadata={"source": "pacifica_gateway_pilot", "symbol": candidate.config.symbol},
    )
    return TradeIntent(
        intent_id="pacifica-gateway-trade-1",
        strategy_id="pacifica_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="pacifica",
        market=market,
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=candidate.planned_gross_volume_usd,
        metadata={
            "symbol": candidate.config.symbol,
            "spread_bps": str(candidate.top_of_book.spread_bps),
            "planned_gross_volume_usd": str(candidate.planned_gross_volume_usd),
            "gateway_pilot": True,
        },
    )


def _synthetic_trade_intent(*, mode: ExecutionMode, roundtrip_mode: RoundtripMode) -> TradeIntent:
    buy = OrderIntent(
        intent_id="pacifica-gateway-synthetic-buy-1",
        exchange_id="pacifica",
        market="BTC-PERP",
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("60000"),
        reduce_only=False,
    )
    sell = OrderIntent(
        intent_id="pacifica-gateway-synthetic-sell-1",
        exchange_id="pacifica",
        market="BTC-PERP",
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("59999"),
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
    )
    return TradeIntent(
        intent_id="pacifica-gateway-synthetic-trade-1",
        strategy_id="pacifica_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="pacifica",
        market="BTC-PERP",
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
        exchange_id="pacifica",
        adapter_factory=lambda: PacificaAdapter(
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
            exchange_id="pacifica",
            account_alias=account_alias,
            entry_fee_bps=entry_fee_bps,
            exit_fee_bps=exit_fee_bps,
            taker_fee_bps=entry_fee_bps,
            source=fee_source,
            markets=(market,),
        ),
        adapters={"pacifica": adapter},
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=False,
    )


if __name__ == "__main__":
    main()
