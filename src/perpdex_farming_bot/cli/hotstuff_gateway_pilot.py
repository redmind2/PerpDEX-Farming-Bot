from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from perpdex_farming_bot.cli.hotstuff_live_test import (
    _candidate_cost,
    _filter_plan_markets,
    _fmt_decimal,
    _load_hotstuff_fee_provider,
    _override_plan_24h_spread_gate,
    _roundtrip_order_plan,
    _select_candidate,
    _validate_fast_close_args,
    _validate_level_size_fraction,
    _validate_live_args,
)
from perpdex_farming_bot.cli.hotstuff_live_preflight import _load_instrument_map
from perpdex_farming_bot.connectors.hotstuff_readonly import (
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_hotstuff_environment,
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
from perpdex_farming_bot.credentials import hotstuff_available_private_readonly_env, hotstuff_credential_env
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.hotstuff import HotstuffAdapter
from perpdex_farming_bot.gateway.exchange_registry import LazyExchangeAdapterBridge
from perpdex_farming_bot.gateway.execution_gateway import ExecutionGateway, StaticKillSwitch


ACCOUNT_ALIAS = "hotstuff_gateway"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Hotstuff Execution Gateway pilot. Selects a candidate and routes a TradeIntent "
            "through Gateway dry-run/paper checks. This CLI never sends live orders."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hotstuff.live-test.json")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="HOTSTUFF")
    parser.add_argument("--mode", choices=("dry_run", "paper"), default="dry_run")
    parser.add_argument("--network", action="store_true", help="Read live orderbook/account state. Never sends orders.")
    parser.add_argument("--read-only", action="store_true", help="Also run private read-only position/open-order checks through Gateway.")
    parser.add_argument("--market", default="", help="Optional single Hotstuff market filter, e.g. BTC-PERP.")
    parser.add_argument("--order-notional-usd", type=Decimal, default=None)
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=None)
    parser.add_argument("--level-size-fraction", type=Decimal, default=None)
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-spread-bps", type=Decimal, default=None)
    parser.add_argument("--ignore-24h-spread-gate", action="store_true")
    parser.add_argument("--min-entry-delay-seconds", type=float, default=None)
    parser.add_argument("--loop-delay-seconds", type=float, default=0.0)
    parser.add_argument("--fast-close-on-fill", action="store_true")
    parser.add_argument("--prebuild-close-order", action="store_true")
    parser.add_argument("--close-mode", choices=("confirmed", "fast-reduce-only", "netting"), default="netting")
    args = parser.parse_args()
    if args.close_mode == "fast-reduce-only":
        args.fast_close_on_fill = True

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.market:
        plan = _filter_plan_markets(plan, args.market)
    environment = normalize_hotstuff_environment(args.environment or str(plan.get("environment", "production")))
    api_name = api_endpoint_env_name(environment)
    api_endpoint = validate_https_base_url(
        api_name,
        endpoint_from_env(get_env(api_name), default_api_endpoint(environment)),
    )
    order_notional = args.order_notional_usd or Decimal(str(plan["order_notional_usd"]))
    target_gross_volume = args.target_gross_volume_usd or Decimal(str(plan["target_gross_volume_usd"]))
    max_spread_bps = args.max_spread_bps or Decimal(str(plan["max_spread_bps"]))
    if args.ignore_24h_spread_gate:
        plan = _override_plan_24h_spread_gate(plan, max_spread_bps)
    level_size_fraction = args.level_size_fraction or Decimal(str(plan.get("level_size_fraction", "0.5")))
    min_entry_delay_seconds = (
        args.min_entry_delay_seconds
        if args.min_entry_delay_seconds is not None
        else float(plan["min_entry_delay_seconds"])
    )
    _validate_live_args(order_notional, target_gross_volume, args.max_cycles, min_entry_delay_seconds)
    _validate_level_size_fraction(level_size_fraction)
    _validate_fast_close_args(args)

    mode = ExecutionMode(args.mode)
    roundtrip_mode = _roundtrip_mode(args.close_mode)
    credential_env = hotstuff_credential_env(args.credential_prefix, environment)
    account_env = hotstuff_available_private_readonly_env(args.credential_prefix, environment)

    print("hotstuff_gateway_pilot=True")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"mode={mode.value}")
    print(f"network={args.network}")
    print(f"include_read_only={args.read_only}")
    print(f"api_endpoint={api_endpoint}")
    print(f"config={args.config}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"order_notional_usd={order_notional}")
    print(f"target_gross_volume_usd={target_gross_volume}")
    print(f"max_spread_bps={max_spread_bps}")
    print(f"level_size_fraction={level_size_fraction}")
    print(f"close_mode={args.close_mode}")
    print("execute_live=False")
    print("live_order_submitted=False")
    print("gateway_live_orders_enabled=False")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.signer_address}={masked_env_status(credential_env.signer_address)}")
    print(f"primary_{credential_env.signer_private_key}={masked_env_status(credential_env.signer_private_key)}")
    print(f"legacy_{credential_env.legacy_private_key}={masked_env_status(credential_env.legacy_private_key)}")
    print(f"private_readonly_env_ready={account_env is not None}")

    if args.network:
        instruments = _load_instrument_map(api_endpoint, args.timeout_seconds)
        print(f"instrument_count={len(instruments)}")
        fee_provider = _load_hotstuff_fee_provider(
            api_endpoint=api_endpoint,
            credential_prefix=args.credential_prefix,
            environment=environment,
            plan=plan,
            instruments=instruments,
            timeout_seconds=args.timeout_seconds,
            private_readonly_ready=account_env is not None,
        )
        selected = _select_candidate(api_endpoint, plan, instruments, max_spread_bps, args.timeout_seconds, fee_provider)
        if selected is None:
            print("gateway_ready=False")
            print("reason=no_hotstuff_gateway_candidate")
            raise SystemExit(1)
        selected_cost = _candidate_cost(selected, fee_provider)
        order_plan = _roundtrip_order_plan(selected, order_notional, target_gross_volume, level_size_fraction)
        buy_qty = order_plan["buy_qty"]
        sell_qty = order_plan["sell_qty"]
        if roundtrip_mode is RoundtripMode.FAST_REDUCE_ONLY:
            matched_qty = min(buy_qty, sell_qty)
            buy_qty = matched_qty
            sell_qty = matched_qty
        planned_gross = (buy_qty * selected.best_ask) + (sell_qty * selected.best_bid)
        print(f"gateway_selected_market={selected.market}")
        print(f"gateway_selected_instrument_id={selected.instrument_id}")
        print(f"gateway_selected_live_spread_bps={selected.live_spread_bps:.4f}")
        print(f"gateway_selected_24h_threshold_bps={selected.provided_24h_spread_bps}")
        print(f"gateway_selected_entry_fee_bps={selected_cost.entry_fee_bps}")
        print(f"gateway_selected_exit_fee_bps={selected_cost.exit_fee_bps}")
        print(f"gateway_selected_slippage_buffer_bps={selected_cost.slippage_buffer_bps}")
        print(f"gateway_selected_fee_source={selected_cost.fee_source}")
        print(f"gateway_selected_expected_loss_bps={selected_cost.expected_loss_bps:.4f}")
        print(f"gateway_planned_buy_size={_fmt_decimal(buy_qty)}")
        print(f"gateway_planned_sell_size={_fmt_decimal(sell_qty)}")
        print(f"gateway_planned_gross_volume_usd={planned_gross:.4f}")
        if buy_qty <= 0 or sell_qty <= 0:
            print("gateway_ready=False")
            print("reason=quantity_zero")
            raise SystemExit(1)
        trade_intent = _trade_intent_from_selection(
            selected,
            mode=mode,
            roundtrip_mode=roundtrip_mode,
            buy_qty=buy_qty,
            sell_qty=sell_qty,
            planned_gross=planned_gross,
        )
        market = selected.market
        entry_fee_bps = selected_cost.entry_fee_bps
        exit_fee_bps = selected_cost.exit_fee_bps
        fee_source = selected_cost.fee_source
    else:
        trade_intent = _synthetic_trade_intent(mode=mode, roundtrip_mode=roundtrip_mode)
        market = trade_intent.market
        entry_fee_bps = Decimal("3")
        exit_fee_bps = Decimal("3")
        fee_source = "hotstuff_gateway_pilot_static_no_network"
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
        max_order_notional_usd=order_notional + Decimal("1"),
        max_gross_notional_usd=max(planned_gross, target_gross_volume),
        timeout_seconds=args.timeout_seconds,
    )
    preflight = gateway.preflight(
        ExecutionPreflightRequest(
            request_id="hotstuff-gateway-pilot-1",
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


def _trade_intent_from_selection(
    selected,
    *,
    mode: ExecutionMode,
    roundtrip_mode: RoundtripMode,
    buy_qty: Decimal,
    sell_qty: Decimal,
    planned_gross: Decimal,
) -> TradeIntent:
    buy = OrderIntent(
        intent_id="hotstuff-gateway-buy-1",
        exchange_id="hotstuff",
        market=selected.market,
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=buy_qty,
        reference_price=selected.best_ask,
        reduce_only=False,
        metadata={"source": "hotstuff_gateway_pilot", "instrument_id": selected.instrument_id},
    )
    sell = OrderIntent(
        intent_id="hotstuff-gateway-sell-1",
        exchange_id="hotstuff",
        market=selected.market,
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=sell_qty,
        reference_price=selected.best_bid,
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
        metadata={"source": "hotstuff_gateway_pilot", "instrument_id": selected.instrument_id},
    )
    return TradeIntent(
        intent_id="hotstuff-gateway-trade-1",
        strategy_id="hotstuff_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="hotstuff",
        market=selected.market,
        mode=mode,
        orders=(buy, sell),
        roundtrip_mode=roundtrip_mode,
        max_gross_notional_usd=planned_gross,
        metadata={
            "instrument_id": selected.instrument_id,
            "spread_bps": str(selected.live_spread_bps),
            "planned_gross_volume_usd": str(planned_gross),
            "gateway_pilot": True,
        },
    )


def _synthetic_trade_intent(*, mode: ExecutionMode, roundtrip_mode: RoundtripMode) -> TradeIntent:
    buy = OrderIntent(
        intent_id="hotstuff-gateway-synthetic-buy-1",
        exchange_id="hotstuff",
        market="BTC-PERP",
        side=OrderSide.BUY,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("60000"),
        reduce_only=False,
    )
    sell = OrderIntent(
        intent_id="hotstuff-gateway-synthetic-sell-1",
        exchange_id="hotstuff",
        market="BTC-PERP",
        side=OrderSide.SELL,
        order_type=OrderKind.MARKET,
        quantity=Decimal("0.0004"),
        reference_price=Decimal("59999"),
        reduce_only=roundtrip_mode is not RoundtripMode.NETTING,
    )
    return TradeIntent(
        intent_id="hotstuff-gateway-synthetic-trade-1",
        strategy_id="hotstuff_gateway_pilot",
        account_alias=ACCOUNT_ALIAS,
        exchange_id="hotstuff",
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
        exchange_id="hotstuff",
        adapter_factory=lambda: HotstuffAdapter(
            api_endpoint=api_endpoint,
            credential_prefix=credential_prefix,
            environment=environment,
            timeout_seconds=timeout_seconds,
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
            exchange_id="hotstuff",
            account_alias=account_alias,
            entry_fee_bps=entry_fee_bps,
            exit_fee_bps=exit_fee_bps,
            taker_fee_bps=entry_fee_bps,
            source=fee_source,
            markets=(market,),
        ),
        adapters={"hotstuff": adapter},
        kill_switch=StaticKillSwitch(enabled=False),
        live_orders_enabled=False,
    )


if __name__ == "__main__":
    main()
