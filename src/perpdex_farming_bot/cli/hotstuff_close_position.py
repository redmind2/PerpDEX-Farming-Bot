from __future__ import annotations

import argparse
import importlib.util
import time
from decimal import Decimal
from typing import Literal

from perpdex_farming_bot.cli.hotstuff_live_preflight import _first_level, _load_instrument_map
from perpdex_farming_bot.cli.hotstuff_live_test import _position_market, _positions
from perpdex_farming_bot.connectors.hotstuff_readonly import (
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    info_post_json,
    normalize_hotstuff_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.core.execution_event import emit_execution_event
from perpdex_farming_bot.core.execution_models import ExecutionMode, ExecutionRequest, OrderIntent, OrderKind, OrderSide, TradeIntent
from perpdex_farming_bot.credentials import read_hotstuff_credentials
from perpdex_farming_bot.env import get_env, load_dotenv_if_present
from perpdex_farming_bot.exchanges.hotstuff import HotstuffAdapter, hotstuff_close_price, round_down_to_step
from perpdex_farming_bot.gateway.live_preflight import build_live_preflight_gateway, run_live_gateway_preflight


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Guarded Hotstuff reduce-only position close. Requires explicit confirmation.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="HOTSTUFF")
    parser.add_argument("--market", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--close-slippage-bps", type=Decimal, default=Decimal("100"))
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_hotstuff_environment(args.environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = validate_https_base_url(
        api_name,
        endpoint_from_env(get_env(api_name), default_api_endpoint(environment)),
    )
    market = args.market.upper()
    confirm_text = _confirm_text(market)

    print("hotstuff_close_position=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"api_endpoint={api_endpoint}")
    print(f"credential_prefix={args.credential_prefix}")
    print(f"market={market}")
    print(f"execute_live={args.execute_live}")
    print("close_mode=reduce_only_ioc_market")
    print(f"close_slippage_bps={args.close_slippage_bps}")
    print(f"required_confirmation={confirm_text}")

    sdk_installed = importlib.util.find_spec("hotstuff") is not None and importlib.util.find_spec("eth_account") is not None
    print(f"sdk_installed={sdk_installed}")
    if not sdk_installed:
        print("close_ready=False")
        print("reason=hotstuff_python_sdk_not_installed_for_this_python")
        return

    credentials = read_hotstuff_credentials(args.credential_prefix, environment)
    if not credentials["account_address"]:
        print("close_ready=False")
        print("reason=missing_account_address_env")
        return
    if not credentials["signer_private_key"]:
        print("close_ready=False")
        print("reason=missing_signer_private_key_env")
        return

    position = _find_position(api_endpoint, args.credential_prefix, environment, market, args.timeout_seconds)
    if position is None:
        print("close_ready=False")
        print("reason=no_position_for_market")
        return

    raw_size = Decimal(str(position.get("size", "0")))
    print(f"position_market={market}")
    print(f"position_size={raw_size}")
    if raw_size == 0:
        print("close_ready=False")
        print("reason=zero_position_size")
        return

    if not args.execute_live:
        print("close_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{confirm_text}")
        return
    if args.confirm != confirm_text:
        print("close_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    adapter = HotstuffAdapter(
        api_endpoint,
        args.credential_prefix,
        environment,
        args.timeout_seconds,
    )
    signer_ready, signer_reason = adapter.signer_ready()
    print(f"signer_ready={signer_ready}")
    print(f"signer_status={signer_reason}")
    if not signer_ready:
        print(f"live_aborted={signer_reason}")
        return

    instruments = _load_instrument_map(api_endpoint, args.timeout_seconds)
    instrument = instruments.get(market, {})
    instrument_id = int(instrument.get("id", 0) or 0)
    lot_size = Decimal(str(instrument.get("lot_size", "0")))
    tick_size = Decimal(str(instrument.get("tick_size", "0")))
    if instrument_id <= 0 or lot_size <= 0 or tick_size <= 0:
        print("close_ready=False")
        print("reason=instrument_or_lot_or_tick_size_missing")
        return

    orderbook = info_post_json(api_endpoint, "orderbook", {"symbol": market}, args.timeout_seconds)
    if not isinstance(orderbook, dict):
        print("close_ready=False")
        print("reason=orderbook_response_not_object")
        return
    best_bid = Decimal(str(_first_level(orderbook, "bids")["price"]))
    best_ask = Decimal(str(_first_level(orderbook, "asks")["price"]))

    side: Literal["b", "s"] = "s" if raw_size > 0 else "b"
    price = hotstuff_close_price(side, best_bid, best_ask, tick_size, args.close_slippage_bps)
    size = round_down_to_step(abs(raw_size), lot_size)
    if size <= 0:
        print("close_ready=False")
        print("reason=rounded_size_zero")
        return

    gateway_preflight = _run_gateway_close_preflight(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        account_alias=f"{args.credential_prefix}_gateway",
        market=market,
        side=side,
        price=price,
        size=size,
        max_notional_usd=abs(size * price) + Decimal("1"),
        timeout_seconds=args.timeout_seconds,
    )
    if not gateway_preflight.ready:
        print("close_ready=False")
        print("reason=gateway_preflight_not_ready")
        return

    print("live_submit=True")
    live_gateway, live_trade_intent = _build_gateway_close_context(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        account_alias=f"{args.credential_prefix}_gateway",
        market=market,
        side=side,
        price=price,
        size=size,
        max_notional_usd=abs(size * price) + Decimal("1"),
        timeout_seconds=args.timeout_seconds,
        live_orders_enabled=True,
    )
    print("live_submit_route=execution_gateway_reduce_only_close")
    live_request = ExecutionRequest(
        request_id="hotstuff-close-position-gateway-submit",
        trade_intent=live_trade_intent,
    )
    result = live_gateway.execute_reduce_only_close(
        live_request,
        instrument_id=instrument_id,
        side=side,
        price=price,
        size=size,
    )
    print(f"reduce_only_close_success={result.success}")
    print(f"reduce_only_close_status={result.status}")
    if result.filled_size:
        print(f"reduce_only_close_filled_size={result.filled_size}")
    if result.average_price is not None:
        print(f"reduce_only_close_average_price={result.average_price}")
    if result.error:
        print(f"reduce_only_close_error={result.error}")
    if not result.success:
        print("stop_reason=adapter_error")
        return

    time.sleep(1)
    remaining = _find_position(api_endpoint, args.credential_prefix, environment, market, args.timeout_seconds)
    remaining_size = Decimal("0") if remaining is None else Decimal(str(remaining.get("size", "0")))
    print(f"remaining_position_size={remaining_size}")
    emit_execution_event(
        live_gateway.record_observation(
            live_request,
            status=result.status,
            metadata={
                "filled_gross_volume_usd": (
                    abs(result.filled_size * result.average_price)
                    if result.average_price is not None and result.filled_size
                    else None
                ),
                "final_position_count": 0 if remaining_size == 0 else 1,
                "final_all_flat": remaining_size == 0,
                "order_ids": (result.exchange_order_id,) if result.exchange_order_id else (),
            },
            error_reason=None if result.success else result.status,
        )
    )
    print("stop_reason=reduce_only_close_submitted")


def _find_position(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    market: str,
    timeout_seconds: float,
) -> dict[str, object] | None:
    for position in _positions(api_endpoint, credential_prefix, environment, timeout_seconds):
        if _position_market(position).upper() == market:
            return position
    return None


def _run_gateway_close_preflight(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    account_alias: str,
    market: str,
    side: str,
    price: Decimal,
    size: Decimal,
    max_notional_usd: Decimal,
    timeout_seconds: float,
):
    gateway, trade_intent = _build_gateway_close_context(
        api_endpoint=api_endpoint,
        credential_prefix=credential_prefix,
        environment=environment,
        account_alias=account_alias,
        market=market,
        side=side,
        price=price,
        size=size,
        max_notional_usd=max_notional_usd,
        timeout_seconds=timeout_seconds,
        live_orders_enabled=False,
    )
    return run_live_gateway_preflight(
        gateway=gateway,
        trade_intent=trade_intent,
        request_id="hotstuff-close-position-gateway-preflight",
        include_read_only=True,
        check_positions=False,
        check_open_orders=True,
    )


def _build_gateway_close_context(
    *,
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    account_alias: str,
    market: str,
    side: str,
    price: Decimal,
    size: Decimal,
    max_notional_usd: Decimal,
    timeout_seconds: float,
    live_orders_enabled: bool,
):
    order_side = OrderSide.BUY if side == "b" else OrderSide.SELL
    trade_intent = TradeIntent(
        intent_id="hotstuff-close-position-gateway-trade-1",
        strategy_id="hotstuff_close_position",
        account_alias=account_alias,
        exchange_id="hotstuff",
        market=market,
        mode=ExecutionMode.LIVE,
        orders=(
            OrderIntent(
                intent_id="hotstuff-close-position-gateway-order-1",
                exchange_id="hotstuff",
                market=market,
                side=order_side,
                order_type=OrderKind.MARKET,
                quantity=size,
                reference_price=price,
                reduce_only=True,
                metadata={"source": "hotstuff_close_position"},
            ),
        ),
        max_gross_notional_usd=max_notional_usd,
        metadata={"close_position": True},
    )
    gateway = build_live_preflight_gateway(
        exchange_id="hotstuff",
        account_alias=account_alias,
        market=market,
        adapter_factory=lambda: HotstuffAdapter(
            api_endpoint,
            credential_prefix,
            environment,
            timeout_seconds,
        ),
        entry_fee_bps=Decimal("3"),
        exit_fee_bps=Decimal("3"),
        fee_source="hotstuff_close_position_conservative_default",
        max_order_notional_usd=max_notional_usd,
        max_gross_notional_usd=max_notional_usd,
        open_orders_supported=True,
        live_orders_enabled=live_orders_enabled,
    )
    return gateway, trade_intent


def _confirm_text(market: str) -> str:
    base = market.removesuffix("-PERP").replace("-", "_")
    return f"CLOSE_HOTSTUFF_{base}_REDUCE_ONLY"


if __name__ == "__main__":
    main()
