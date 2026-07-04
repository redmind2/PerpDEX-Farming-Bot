from __future__ import annotations

import argparse
import importlib.util
import json
import re
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Literal

from perpdex_farming_bot.cli.hotstuff_live_preflight import (
    MarketCandidate,
    _candidate_from_live_orderbook,
    _load_instrument_map,
)
from perpdex_farming_bot.connectors.hotstuff_readonly import (
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    info_post_json,
    normalize_hotstuff_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.core.live_volume import RoundtripPlan, VolumeRunConfig, run_paired_volume
from perpdex_farming_bot.credentials import (
    hotstuff_available_private_readonly_env,
    read_hotstuff_credentials,
    read_hotstuff_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present
from perpdex_farming_bot.exchanges.hotstuff import HotstuffAdapter


CONFIRM_TEXT = "LIVE_HOTSTUFF_100_USD_TO_1000"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Hotstuff mainnet live test. Re-checks spreads before every cycle and only sends "
            "orders with --execute-live plus the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hotstuff.live-test.json")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="HOTSTUFF")
    parser.add_argument("--order-notional-usd", type=Decimal, default=None)
    parser.add_argument("--target-gross-volume-usd", type=Decimal, default=None)
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-spread-bps", type=Decimal, default=None)
    parser.add_argument("--min-entry-delay-seconds", type=float, default=None)
    parser.add_argument("--reduce-only-settle-attempts", type=int, default=5)
    parser.add_argument("--reduce-only-settle-delay-seconds", type=float, default=0.5)
    parser.add_argument("--reduce-only-slippage-bps", type=Decimal, default=Decimal("100"))
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = normalize_hotstuff_environment(args.environment or str(plan.get("environment", "production")))
    api_name = api_endpoint_env_name(environment)
    api_endpoint = validate_https_base_url(
        api_name,
        endpoint_from_env(get_env(api_name), default_api_endpoint(environment)),
    )
    order_notional = args.order_notional_usd or Decimal(str(plan["order_notional_usd"]))
    target_gross_volume = args.target_gross_volume_usd or Decimal(str(plan["target_gross_volume_usd"]))
    max_spread_bps = args.max_spread_bps or Decimal(str(plan["max_spread_bps"]))
    min_entry_delay_seconds = (
        args.min_entry_delay_seconds
        if args.min_entry_delay_seconds is not None
        else float(plan["min_entry_delay_seconds"])
    )

    _validate_live_args(order_notional, target_gross_volume, args.max_cycles, min_entry_delay_seconds)

    print("hotstuff_live_test=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"api_endpoint={api_endpoint}")
    print(f"config={args.config}")
    print(f"credential_prefix={args.credential_prefix}")
    print(f"execute_live={args.execute_live}")
    print(f"order_notional_usd={order_notional}")
    print(f"target_gross_volume_usd={target_gross_volume}")
    print(f"max_spread_bps={max_spread_bps}")
    print(f"min_entry_delay_seconds={min_entry_delay_seconds}")
    print("orders_require_confirmation=True")
    print(f"required_confirmation={CONFIRM_TEXT}")

    account_env = hotstuff_available_private_readonly_env(args.credential_prefix, environment)
    print(f"private_readonly_env_ready={account_env is not None}")
    if account_env is None:
        print("live_ready=False")
        print("reason=missing_account_address_env")
        return

    credentials = read_hotstuff_credentials(args.credential_prefix, environment)
    private_key_present = bool(credentials["signer_private_key"])
    print(f"signing_env_ready={private_key_present}")
    if not private_key_present:
        print("live_ready=False")
        print("reason=missing_signer_private_key_env")
        return

    sdk_installed = importlib.util.find_spec("hotstuff") is not None and importlib.util.find_spec("eth_account") is not None
    print(f"sdk_installed={sdk_installed}")
    print("sdk_package=hotstuff-python-sdk")

    account_ok = _account_summary_ok(api_endpoint, args.credential_prefix, environment, args.timeout_seconds)
    print(f"account_summary_private_readonly_ok={account_ok}")
    if not account_ok:
        print("live_ready=False")
        print("reason=account_summary_readonly_failed")
        return

    start_positions = _positions(api_endpoint, args.credential_prefix, environment, args.timeout_seconds)
    print(f"start_position_count={len(start_positions)}")
    if start_positions:
        print("live_ready=False")
        print("reason=existing_positions_detected")
        print("existing_position_markets=" + ",".join(sorted(_position_market(item) for item in start_positions)))
        return

    selected = _select_candidate(api_endpoint, plan, max_spread_bps, args.timeout_seconds)
    if selected is None:
        print("live_ready=False")
        print("reason=no_eligible_market")
        return

    print(f"selected_market={selected.market}")
    print(f"selected_instrument_id={selected.instrument_id}")
    print(f"selected_live_spread_bps={selected.live_spread_bps:.4f}")
    print(f"selected_24h_threshold_bps={selected.provided_24h_spread_bps}")
    buy_qty, buy_price = _quantity_for_notional(order_notional, selected.best_ask, selected.lot_size)
    sell_qty, sell_price = _quantity_for_notional(order_notional, selected.best_bid, selected.lot_size)
    planned_gross = (buy_qty * buy_price) + (sell_qty * sell_price)
    print(f"planned_buy_size={_fmt_decimal(buy_qty)}")
    print(f"planned_sell_size={_fmt_decimal(sell_qty)}")
    print(f"planned_buy_price={_fmt_decimal(buy_price)}")
    print(f"planned_sell_price={_fmt_decimal(sell_price)}")
    print(f"planned_cycle_gross_volume_usd={planned_gross:.4f}")
    print(f"planned_cycles_to_target={_ceil_decimal(target_gross_volume / planned_gross) if planned_gross > 0 else 0}")

    if buy_qty <= 0 or sell_qty <= 0:
        print("live_ready=False")
        print("reason=quantity_zero")
        return
    if buy_qty * buy_price < selected.min_notional_usd or sell_qty * sell_price < selected.min_notional_usd:
        print("live_ready=False")
        print("reason=below_min_notional")
        return

    if not args.execute_live:
        print("live_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if not sdk_installed:
        print("live_ready=False")
        print("reason=hotstuff_python_sdk_not_installed_for_this_python")
        print("hint=use_the_bundled_python_where_hotstuff_python_sdk_was_installed")
        return
    if not credentials["signer_address"]:
        print("live_ready=False")
        print("reason=missing_signer_address_env_for_api_wallet_live_test")
        return
    if credentials["account_address"].casefold() == credentials["signer_address"].casefold():
        print("live_ready=False")
        print("reason=account_address_must_be_owner_not_signer_for_api_wallet_live_test")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    _execute_live_loop(
        args=args,
        plan=plan,
        api_endpoint=api_endpoint,
        environment=environment,
        credentials=credentials,
        order_notional=order_notional,
        target_gross_volume=target_gross_volume,
        max_spread_bps=max_spread_bps,
        min_entry_delay_seconds=min_entry_delay_seconds,
    )


def _validate_live_args(
    order_notional: Decimal,
    target_gross_volume: Decimal,
    max_cycles: int,
    min_entry_delay_seconds: float,
) -> None:
    if order_notional <= 0:
        raise SystemExit("--order-notional-usd must be greater than zero")
    if order_notional > Decimal("100"):
        raise SystemExit("--order-notional-usd must be <= 100 for this guarded live test")
    if target_gross_volume <= 0:
        raise SystemExit("--target-gross-volume-usd must be greater than zero")
    if target_gross_volume > Decimal("1000"):
        raise SystemExit("--target-gross-volume-usd must be <= 1000 for this guarded live test")
    if max_cycles <= 0:
        raise SystemExit("--max-cycles must be greater than zero")
    if max_cycles > 20:
        raise SystemExit("--max-cycles must be <= 20 for this guarded live test")
    if min_entry_delay_seconds < 1:
        raise SystemExit("--min-entry-delay-seconds must be at least 1")


def _select_candidate(
    api_endpoint: str,
    plan: dict[str, object],
    max_spread_bps: Decimal,
    timeout_seconds: float,
) -> MarketCandidate | None:
    instruments = _load_instrument_map(api_endpoint, timeout_seconds)
    candidates = [
        _candidate_from_live_orderbook(api_endpoint, market, instruments, max_spread_bps, timeout_seconds)
        for market in plan["markets"]
    ]
    eligible = [candidate for candidate in candidates if candidate.eligible]
    print(f"eligible_market_count={len(eligible)}")
    for candidate in candidates:
        print(
            f"candidate={candidate.market} eligible={candidate.eligible} "
            f"spread_bps={candidate.live_spread_bps:.4f} reason={candidate.reason}"
        )
    if not eligible:
        return None
    return sorted(eligible, key=lambda item: (item.live_spread_bps, item.provided_current_spread_bps, item.market))[0]


def _execute_live_loop(
    *,
    args: argparse.Namespace,
    plan: dict[str, object],
    api_endpoint: str,
    environment: str,
    credentials: dict[str, str],
    order_notional: Decimal,
    target_gross_volume: Decimal,
    max_spread_bps: Decimal,
    min_entry_delay_seconds: float,
) -> None:
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

    print("live_loop_start=True")
    print("live_volume_accounting=planned_buy_notional_plus_planned_sell_notional")

    def select_plan(remaining_gross_volume_usd: Decimal) -> RoundtripPlan | None:
        selected = _select_candidate(api_endpoint, plan, max_spread_bps, args.timeout_seconds)
        if selected is None:
            return None

        buy_qty, buy_price = _quantity_for_notional(order_notional, selected.best_ask, selected.lot_size)
        sell_qty, sell_price = _quantity_for_notional(order_notional, selected.best_bid, selected.lot_size)
        planned_gross = (buy_qty * buy_price) + (sell_qty * sell_price)
        print(
            f"selected_market={selected.market} spread_bps={selected.live_spread_bps:.4f} "
            f"buy_size={_fmt_decimal(buy_qty)} sell_size={_fmt_decimal(sell_qty)} "
            f"planned_gross_volume_usd={planned_gross:.4f}"
        )
        if buy_qty <= 0 or sell_qty <= 0:
            print("selected_plan_skipped=quantity_zero")
            return None
        if planned_gross <= 0:
            print("selected_plan_skipped=planned_gross_zero")
            return None
        if planned_gross > remaining_gross_volume_usd:
            print(
                "selected_plan_skipped=target_cap_would_be_exceeded:"
                f"{planned_gross:.4f}>{remaining_gross_volume_usd:.4f}"
            )
            return None
        return RoundtripPlan(
            market=selected.market,
            instrument_id=selected.instrument_id,
            buy_price=buy_price,
            sell_price=sell_price,
            buy_size=buy_qty,
            sell_size=sell_qty,
            planned_gross_volume_usd=planned_gross,
            reason="lowest_spread_eligible_market",
        )

    result = run_paired_volume(
        adapter=adapter,
        config=VolumeRunConfig(
            target_gross_volume_usd=target_gross_volume,
            max_cycles=args.max_cycles,
            min_entry_delay_seconds=min_entry_delay_seconds,
        ),
        select_plan=select_plan,
    )
    print(f"live_result_status={result.status}")
    print(f"live_result_planned_gross_volume_usd={result.planned_gross_volume_usd:.4f}")
    print(f"live_result_cycles={result.cycles}")


def _account_summary_ok(api_endpoint: str, credential_prefix: str, environment: str, timeout_seconds: float) -> bool:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    try:
        payload = info_post_json(api_endpoint, "accountSummary", params, timeout_seconds, private_readonly=True)
    except Exception:
        return False
    return isinstance(payload, dict)


def _positions(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
) -> list[dict[str, object]]:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    payload = info_post_json(api_endpoint, "positions", params, timeout_seconds, private_readonly=True)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and abs(Decimal(str(item.get("size", "0")))) > 0]


def _selected_position(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    market: str,
    timeout_seconds: float,
) -> dict[str, object] | None:
    for position in _positions(api_endpoint, credential_prefix, environment, timeout_seconds):
        if _position_market(position) == market:
            return position
    return None


def _position_market(position: dict[str, object]) -> str:
    return str(position.get("instrument") or position.get("symbol") or position.get("instrument_name") or "unknown")


def _signer_registered_for_account(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
    signer_address: str,
) -> bool:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    try:
        payload = info_post_json(api_endpoint, "allAgents", params, timeout_seconds, private_readonly=True)
    except Exception:
        return False
    return _payload_contains_address(payload, signer_address.casefold())


def _payload_contains_address(payload: object, address: str) -> bool:
    if isinstance(payload, dict):
        return any(_payload_contains_address(value, address) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains_address(value, address) for value in payload)
    if isinstance(payload, str):
        return payload.casefold() == address
    return False


def _send_reduce_only_close(client: object, selected: MarketCandidate, residual: dict[str, object], args: argparse.Namespace) -> bool:
    from hotstuff import PlaceOrderParams, UnitOrder

    raw_size = Decimal(str(residual.get("size", "0")))
    if raw_size == 0:
        return True
    side: Literal["b", "s"] = "s" if raw_size > 0 else "b"
    price = _aggressive_close_price(
        side,
        selected.best_bid,
        selected.best_ask,
        selected.tick_size,
        args.reduce_only_slippage_bps,
    )
    size = _round_down_to_step(abs(raw_size), selected.lot_size)
    if size <= 0:
        return False
    order = UnitOrder(
        instrumentId=selected.instrument_id,
        side=side,
        positionSide="BOTH",
        price=_fmt_decimal(price),
        size=_fmt_decimal(size),
        tif="IOC",
        ro=True,
        po=False,
        isMarket=True,
    )
    response = _safe_place_order(client, PlaceOrderParams(orders=[order], expiresAfter=_now_ms() + 60_000), "reduce_only_close")
    if response is None:
        return False
    _print_exchange_response(response, "reduce_only_close")
    return not _response_has_error(response)


def _quantity_for_notional(notional: Decimal, price: Decimal, lot_size: Decimal) -> tuple[Decimal, Decimal]:
    if price <= 0 or lot_size <= 0:
        return Decimal("0"), price
    return _round_down_to_step(notional / price, lot_size), price


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def _aggressive_close_price(
    side: Literal["b", "s"],
    best_bid: Decimal,
    best_ask: Decimal,
    tick_size: Decimal,
    slippage_bps: Decimal,
) -> Decimal:
    factor = slippage_bps / Decimal("10000")
    if side == "s":
        return _round_down_to_step(best_bid * (Decimal("1") - factor), tick_size)
    return _round_up_to_step(best_ask * (Decimal("1") + factor), tick_size)


def _ceil_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_UP))


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _response_has_error(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    error = response.get("error")
    if error:
        return True
    status = response.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failed", "rejected"}:
        return True
    return False


def _safe_place_order(client: object, params: object, prefix: str) -> object | None:
    try:
        return client.place_order(params)
    except Exception as exc:
        print(f"{prefix}_exchange_exception_type={type(exc).__name__}")
        reason = _exchange_error_reason(exc)
        if reason:
            print(f"{prefix}_exchange_exception_reason={reason}")
        return None


def _exchange_error_reason(exc: Exception) -> str:
    text = str(exc)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if error:
                return str(error)
            status = payload.get("status")
            if status:
                return str(status)

    sanitized = re.sub(r"0x[a-fA-F0-9]{40,64}", "0x[redacted]", text)
    return sanitized[:240]


def _print_exchange_response(response: object, prefix: str) -> None:
    print(f"{prefix}_response_type={type(response).__name__}")
    if not isinstance(response, dict):
        return
    safe_keys = [key for key in ("success", "tx_type", "error") if key in response]
    print(f"{prefix}_keys=" + ",".join(sorted(str(key) for key in response.keys())[:12]))
    for key in safe_keys:
        print(f"{prefix}_{key}={response[key]}")
    data = response.get("data")
    if isinstance(data, dict):
        print(f"{prefix}_data_keys=" + ",".join(sorted(str(key) for key in data.keys())[:12]))
        status = data.get("status")
        if status is not None:
            print(f"{prefix}_data_status={status}")


if __name__ == "__main__":
    main()
