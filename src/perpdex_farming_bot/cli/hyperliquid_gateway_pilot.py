from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from perpdex_farming_bot.cli.hyperliquid_live_volume_test import (
    _bool_config,
    _configured_perp_dexs,
    _discover_candidates,
    _fmt_dex_list,
    _load_market_info_by_api_coin,
    _load_taker_fee_bps,
    _market_configs,
    _print_candidate_snapshot,
    _select_candidate,
    _validate_args,
    fmt_decimal,
)
from perpdex_farming_bot.connectors.hyperliquid_readonly import (
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_hyperliquid_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.core.execution_event import emit_execution_event
from perpdex_farming_bot.core.execution_models import (
    AccountPolicy,
    ExecutionMode,
    ExecutionPreflightRequest,
    ExecutionRequest,
    OrderIntent,
    OrderKind,
    OrderSide,
    RoundtripMode,
    TradeIntent,
)
from perpdex_farming_bot.core.fee_provider import StaticFeeProvider
from perpdex_farming_bot.credentials import (
    hyperliquid_credential_env,
    read_hyperliquid_credentials,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.hyperliquid import HyperliquidAdapter
from perpdex_farming_bot.gateway.exchange_registry import LazyExchangeAdapterBridge
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


ACCOUNT_ALIAS = "hyperliquid_gateway"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Hyperliquid Execution Gateway pilot. Builds a TradeIntent and routes it through "
            "Gateway dry-run/paper checks. This CLI never sends live orders."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hyperliquid.spread-volume-test.json")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="HYPERLIQUID")
    parser.add_argument("--environment", default="")
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--network", action="store_true", help="Read live orderbook/account fee/read-only state. Never sends orders.")
    parser.add_argument("--read-only", action="store_true", help="Also run private read-only position/open-order checks through Gateway.")
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=None)
    parser.add_argument("--max-leg-notional-usd", type=Decimal, default=None)
    parser.add_argument("--book-fraction", type=Decimal, default=None)
    parser.add_argument("--spread-cap-bps", type=Decimal, default=None)
    parser.add_argument("--min-order-size-usd", type=Decimal, default=None)
    parser.add_argument("--slippage-bps", type=Decimal, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--roundtrip-mode", "--close-mode", dest="roundtrip_mode", choices=("confirmed", "netting"), default="netting")
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--max-scan-attempts", type=int, default=1)
    parser.add_argument("--target-tolerance-usd", type=Decimal, default=Decimal("2"))
    parser.add_argument("--cycle-delay-seconds", type=float, default=0.0)
    parser.add_argument("--no-candidate-delay-seconds", type=float, default=0.0)
    parser.add_argument("--final-state-delay-seconds", type=float, default=0.0)
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = normalize_hyperliquid_environment(args.environment or str(plan.get("environment", "production")))
    target_gross = args.target_gross_volume_usd or Decimal(str(plan.get("target_gross_volume_usd", "1000")))
    max_leg_notional = args.max_leg_notional_usd or Decimal(str(plan.get("max_leg_notional_usd", "100")))
    book_fraction = args.book_fraction or Decimal(str(plan.get("book_fraction", "0.5")))
    spread_cap_bps = args.spread_cap_bps or Decimal(str(plan.get("spread_cap_bps", "1")))
    min_order_size_usd = args.min_order_size_usd or Decimal(str(plan.get("min_order_size_usd", "10")))
    slippage_bps = args.slippage_bps or Decimal(str(plan.get("slippage_bps", "25")))
    allow_live_orders_config = _bool_config(plan.get("allow_live_orders", True), "allow_live_orders")
    markets = _market_configs(plan)
    perp_dexs = _configured_perp_dexs(markets)
    mode = ExecutionMode(args.mode)

    _validate_args(
        environment=environment,
        target_gross=target_gross,
        max_leg_notional=max_leg_notional,
        book_fraction=book_fraction,
        spread_cap_bps=spread_cap_bps,
        min_order_size_usd=min_order_size_usd,
        slippage_bps=slippage_bps,
        args=args,
    )

    credential_env = hyperliquid_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    api_endpoint = validate_https_base_url(api_name, api_endpoint)

    print("hyperliquid_gateway_pilot=True")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"mode={mode.value}")
    print(f"network={args.network}")
    print(f"include_read_only={args.read_only}")
    print(f"configured_perp_dexs={_fmt_dex_list(perp_dexs)}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"target_gross_volume_usd={fmt_decimal(target_gross)}")
    print(f"max_leg_notional_usd={fmt_decimal(max_leg_notional)}")
    print(f"roundtrip_mode={args.roundtrip_mode}")
    print("execute_live=False")
    print("live_order_submitted=False")
    print("gateway_live_orders_enabled=False")
    print(f"adapter_allow_live_orders_config={allow_live_orders_config}")
    print(f"{api_name}={api_endpoint}")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_wallet_address}={masked_env_status(credential_env.api_wallet_address)}")
    print(f"primary_{credential_env.api_wallet_private_key}={masked_env_status(credential_env.api_wallet_private_key)}")
    print(f"optional_{credential_env.vault_address}={masked_env_status(credential_env.vault_address)}")

    if args.network:
        credentials = read_hyperliquid_credentials(args.credential_prefix, environment)
        if not credentials["account_address"]:
            print("gateway_ready=False")
            print("reason=missing_hyperliquid_account_address")
            raise SystemExit(1)
        candidate, taker_fee_bps = _network_candidate_and_fee(
            api_endpoint=api_endpoint,
            account=credentials["account_address"],
            markets=markets,
            timeout_seconds=args.timeout_seconds,
            target_gross=target_gross,
            max_leg_notional=max_leg_notional,
            book_fraction=book_fraction,
            spread_cap_bps=spread_cap_bps,
            min_order_size_usd=min_order_size_usd,
            slippage_bps=slippage_bps,
        )
        trade_intent = _trade_intent_from_candidate(
            candidate,
            mode=mode,
            roundtrip_mode=RoundtripMode(args.roundtrip_mode),
            max_gross_notional_usd=target_gross,
        )
        fee_bps = taker_fee_bps
        fee_source = "hyperliquid_account_user_fees"
        market = candidate.config.api_coin
    else:
        trade_intent = _synthetic_trade_intent(mode=mode, roundtrip_mode=RoundtripMode(args.roundtrip_mode))
        fee_bps = Decimal("4.5")
        fee_source = "hyperliquid_gateway_pilot_static_no_network"
        market = trade_intent.market
        print("network_skipped=using_synthetic_trade_intent")

    gateway = _build_gateway(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        perp_dexs=tuple(perp_dexs),
        account_alias=ACCOUNT_ALIAS,
        market=market,
        fee_bps=fee_bps,
        fee_source=fee_source,
        max_order_notional_usd=_order_policy_cap(max_leg_notional, slippage_bps),
        max_gross_notional_usd=target_gross,
        timeout_seconds=args.timeout_seconds,
    )

    request = ExecutionPreflightRequest(
        request_id="hyperliquid-gateway-pilot-1",
        trade_intent=trade_intent,
        include_read_only=args.read_only,
        check_positions=args.read_only,
        check_open_orders=args.read_only,
    )
    preflight = gateway.preflight(request)
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


def _network_candidate_and_fee(
    *,
    api_endpoint: str,
    account: str,
    markets: list[object],
    timeout_seconds: float,
    target_gross: Decimal,
    max_leg_notional: Decimal,
    book_fraction: Decimal,
    spread_cap_bps: Decimal,
    min_order_size_usd: Decimal,
    slippage_bps: Decimal,
):
    market_info_by_coin = _load_market_info_by_api_coin(
        api_endpoint,
        markets=markets,
        timeout_seconds=timeout_seconds,
        min_order_size_usd=min_order_size_usd,
    )
    candidates = _discover_candidates(
        api_endpoint=api_endpoint,
        markets=markets,
        market_info_by_coin=market_info_by_coin,
        timeout_seconds=timeout_seconds,
        target_gross=target_gross,
        filled_gross=Decimal("0"),
        max_leg_notional=max_leg_notional,
        book_fraction=book_fraction,
        spread_cap_bps=spread_cap_bps,
        min_order_size_usd=min_order_size_usd,
        slippage_bps=slippage_bps,
    )
    _print_candidate_snapshot("gateway_initial", candidates)
    candidate = _select_candidate(candidates)
    if candidate is None:
        print("gateway_ready=False")
        print("reason=no_hyperliquid_gateway_candidate")
        raise SystemExit(1)
    print(f"gateway_selected_market={candidate.config.display_market}")
    print(f"gateway_selected_api_coin={candidate.config.api_coin}")
    print(f"gateway_selected_spread_bps={candidate.spread_bps:.4f}")
    print(f"gateway_selected_planned_gross_volume_usd={candidate.planned_gross_volume_usd:.4f}")

    taker_fee_bps = _load_taker_fee_bps(api_endpoint, account, timeout_seconds)
    if taker_fee_bps is None:
        print("gateway_ready=False")
        print("reason=hyperliquid_account_fee_unknown")
        raise SystemExit(1)
    return candidate, taker_fee_bps


def _trade_intent_from_candidate(
    candidate,
    *,
    mode: ExecutionMode,
    roundtrip_mode: RoundtripMode,
    max_gross_notional_usd: Decimal,
) -> TradeIntent:
    market = candidate.config.api_coin
    buy = OrderIntent(
        intent_id="hyperliquid-gateway-buy-1",
        exchange_id="hyperliquid",
        market=market,
        side=OrderSide.BUY,
        order_type=OrderKind.LIMIT,
        quantity=candidate.size,
        price=candidate.aggressive_buy_px,
        reference_price=candidate.best_ask,
        time_in_force="ioc",
        reduce_only=False,
        metadata={"source": "hyperliquid_gateway_pilot", "display_market": candidate.config.display_market},
    )
    sell = OrderIntent(
        intent_id="hyperliquid-gateway-sell-1",
        exchange_id="hyperliquid",
        market=market,
        side=OrderSide.SELL,
        order_type=OrderKind.LIMIT,
        quantity=candidate.size,
        price=candidate.aggressive_sell_px,
        reference_price=candidate.best_bid,
        time_in_force="ioc",
        reduce_only=roundtrip_mode is RoundtripMode.CONFIRMED,
        metadata={"source": "hyperliquid_gateway_pilot", "display_market": candidate.config.display_market},
    )
    return TradeIntent(
        intent_id="hyperliquid-gateway-trade-1",
        strategy_id="hyperliquid_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="hyperliquid",
        market=market,
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=max_gross_notional_usd,
        metadata={
            "spread_bps": str(candidate.spread_bps),
            "planned_gross_volume_usd": str(candidate.planned_gross_volume_usd),
            "gateway_pilot": True,
        },
    )


def _synthetic_trade_intent(*, mode: ExecutionMode, roundtrip_mode: RoundtripMode) -> TradeIntent:
    buy = OrderIntent(
        intent_id="hyperliquid-gateway-synthetic-buy-1",
        exchange_id="hyperliquid",
        market="BTC",
        side=OrderSide.BUY,
        order_type=OrderKind.LIMIT,
        quantity=Decimal("0.0004"),
        price=Decimal("50000"),
        reference_price=Decimal("50000"),
        time_in_force="ioc",
        reduce_only=False,
    )
    sell = OrderIntent(
        intent_id="hyperliquid-gateway-synthetic-sell-1",
        exchange_id="hyperliquid",
        market="BTC",
        side=OrderSide.SELL,
        order_type=OrderKind.LIMIT,
        quantity=Decimal("0.0004"),
        price=Decimal("49999"),
        reference_price=Decimal("49999"),
        time_in_force="ioc",
        reduce_only=roundtrip_mode is RoundtripMode.CONFIRMED,
    )
    return TradeIntent(
        intent_id="hyperliquid-gateway-synthetic-trade-1",
        strategy_id="hyperliquid_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="hyperliquid",
        market="BTC",
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=Decimal("50"),
    )


def _order_policy_cap(max_leg_notional: Decimal, slippage_bps: Decimal) -> Decimal:
    return max_leg_notional * (Decimal("1") + slippage_bps / Decimal("10000")) + Decimal("1")


def _build_gateway(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    perp_dexs: tuple[str, ...],
    account_alias: str,
    market: str,
    fee_bps: Decimal,
    fee_source: str,
    max_order_notional_usd: Decimal,
    max_gross_notional_usd: Decimal,
    timeout_seconds: float,
) -> ExecutionGateway:
    adapter = LazyExchangeAdapterBridge(
        exchange_id="hyperliquid",
        adapter_factory=lambda: HyperliquidAdapter(
            api_endpoint=api_endpoint,
            credential_prefix=credential_prefix,
            environment=environment,
            timeout_seconds=timeout_seconds,
            perp_dexs=perp_dexs,
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
            exchange_id="hyperliquid",
            account_alias=account_alias,
            entry_fee_bps=fee_bps,
            exit_fee_bps=fee_bps,
            taker_fee_bps=fee_bps,
            source=fee_source,
            markets=(market,),
        ),
        adapters={"hyperliquid": adapter},
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=False,
    )


if __name__ == "__main__":
    main()
