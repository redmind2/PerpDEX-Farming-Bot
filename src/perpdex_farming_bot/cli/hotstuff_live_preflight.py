from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from urllib.error import HTTPError, URLError

from perpdex_farming_bot.connectors.hotstuff_readonly import (
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    info_post_json,
    normalize_hotstuff_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import hotstuff_available_private_readonly_env, read_hotstuff_private_readonly_params
from perpdex_farming_bot.env import get_env, load_dotenv_if_present


@dataclass(frozen=True)
class MarketCandidate:
    market: str
    instrument_id: int
    lot_size: Decimal
    tick_size: Decimal
    min_notional_usd: Decimal
    provided_current_spread_bps: Decimal
    provided_24h_spread_bps: Decimal
    live_spread_bps: Decimal
    best_bid: Decimal
    best_ask: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    eligible: bool
    reason: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hotstuff mainnet live-test preflight. Read-only: selects a candidate market but sends no orders.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config", default="config/hotstuff.live-test.json")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--credential-prefix", "--account-id", dest="credential_prefix", default="HOTSTUFF")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    plan = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = normalize_hotstuff_environment(args.environment or str(plan.get("environment", "production")))
    api_name = api_endpoint_env_name(environment)
    api_endpoint = validate_https_base_url(
        api_name,
        endpoint_from_env(get_env(api_name), default_api_endpoint(environment)),
    )

    order_notional = Decimal(str(plan["order_notional_usd"]))
    target_gross_volume = Decimal(str(plan["target_gross_volume_usd"]))
    max_spread_bps = Decimal(str(plan["max_spread_bps"]))
    level_size_fraction = Decimal(str(plan.get("level_size_fraction", "0.5")))
    delay_seconds = Decimal(str(plan["min_entry_delay_seconds"]))
    if level_size_fraction <= 0 or level_size_fraction > 1:
        raise SystemExit("level_size_fraction must be greater than 0 and <= 1")

    print("hotstuff_live_preflight=read_only_no_orders")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"api_endpoint={api_endpoint}")
    print(f"config={args.config}")
    print(f"credential_prefix={args.credential_prefix}")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print(f"order_notional_usd={order_notional}")
    print(f"target_gross_volume_usd={target_gross_volume}")
    print(f"max_spread_bps={max_spread_bps}")
    print(f"level_size_fraction={level_size_fraction}")
    print(
        "order_sizing_rule="
        "min(order_notional_usd, remaining_gross_volume_usd/2, smaller_best_bid_or_ask_level_notional*level_size_fraction)"
    )
    print(f"min_entry_delay_seconds={delay_seconds}")

    account_env = hotstuff_available_private_readonly_env(args.credential_prefix, environment)
    print(f"private_readonly_env_ready={account_env is not None}")
    if account_env is not None:
        _print_account_summary_status(api_endpoint, args.credential_prefix, environment, args.timeout_seconds)
    else:
        print("account_summary_skipped=missing_account_address_env")

    instruments = _load_instrument_map(api_endpoint, args.timeout_seconds)
    print(f"instrument_count={len(instruments)}")

    candidates: list[MarketCandidate] = []
    for market in plan["markets"]:
        candidates.append(
            _candidate_from_live_orderbook(api_endpoint, market, instruments, max_spread_bps, args.timeout_seconds)
        )

    eligible = [candidate for candidate in candidates if candidate.eligible]
    for candidate in candidates:
        print(f"market={candidate.market}")
        print(f"  instrument_id={candidate.instrument_id}")
        print(f"  lot_size={candidate.lot_size}")
        print(f"  tick_size={candidate.tick_size}")
        print(f"  min_notional_usd={candidate.min_notional_usd}")
        print(f"  provided_current_spread_bps={candidate.provided_current_spread_bps}")
        print(f"  provided_24h_spread_bps={candidate.provided_24h_spread_bps}")
        print(f"  live_best_bid={candidate.best_bid}")
        print(f"  live_best_ask={candidate.best_ask}")
        print(f"  live_best_bid_size={candidate.best_bid_size}")
        print(f"  live_best_ask_size={candidate.best_ask_size}")
        print(f"  live_spread_bps={candidate.live_spread_bps:.4f}")
        print(f"  eligible={candidate.eligible}")
        print(f"  reason={candidate.reason}")

    print(f"eligible_market_count={len(eligible)}")
    if not eligible:
        print("selected_market=none")
        print("preflight_ready=False")
        return

    selected = sorted(eligible, key=lambda item: (item.live_spread_bps, item.provided_current_spread_bps, item.market))[0]
    order_plan = _roundtrip_order_plan(
        selected,
        order_notional,
        target_gross_volume,
        level_size_fraction,
    )
    one_way_cycles = int(math.ceil(target_gross_volume / order_notional)) if order_notional > 0 else 0
    planned_gross = order_plan["planned_gross"]
    roundtrip_cycles = int((target_gross_volume / planned_gross).to_integral_value(rounding=ROUND_UP)) if planned_gross > 0 else 0

    print("selected_market=" + selected.market)
    print(f"selected_instrument_id={selected.instrument_id}")
    print(f"selected_lot_size={selected.lot_size}")
    print(f"selected_tick_size={selected.tick_size}")
    print(f"selected_min_notional_usd={selected.min_notional_usd}")
    print(f"selected_live_spread_bps={selected.live_spread_bps:.4f}")
    print(f"selected_24h_threshold_bps={selected.provided_24h_spread_bps}")
    print(f"selected_best_bid={selected.best_bid}")
    print(f"selected_best_ask={selected.best_ask}")
    print(f"selected_best_bid_size={selected.best_bid_size}")
    print(f"selected_best_ask_size={selected.best_ask_size}")
    print(f"planned_order_notional_usd={order_notional}")
    print(f"planned_per_side_cap_usd={order_plan['per_side_cap']:.4f}")
    print(f"planned_buy_size={order_plan['buy_qty']}")
    print(f"planned_sell_size={order_plan['sell_qty']}")
    print(f"planned_cycle_gross_volume_usd={planned_gross:.4f}")
    print(f"planned_target_gross_volume_usd={target_gross_volume}")
    print(f"planned_one_way_order_count_to_target={one_way_cycles}")
    print(f"planned_roundtrip_cycles_to_target_if_buy_sell={roundtrip_cycles}")
    print("live_execution_ready=requires_hotstuff_live_test_confirmation")
    print("preflight_ready=True")


def _candidate_from_live_orderbook(
    api_endpoint: str,
    market: dict[str, object],
    instruments: dict[str, dict[str, object]],
    max_spread_bps: Decimal,
    timeout_seconds: float,
) -> MarketCandidate:
    symbol = str(market["market"])
    provided_current = Decimal(str(market["provided_current_spread_bps"]))
    provided_24h = Decimal(str(market["provided_24h_spread_bps"]))
    instrument = instruments.get(symbol, {})
    instrument_id = int(instrument.get("id", 0) or 0)
    lot_size = Decimal(str(instrument.get("lot_size", "0")))
    tick_size = Decimal(str(instrument.get("tick_size", "0")))
    min_notional_usd = Decimal(str(instrument.get("min_notional_usd", "0")))
    try:
        if instrument_id <= 0:
            raise ValueError("instrument missing")
        orderbook = info_post_json(api_endpoint, "orderbook", {"symbol": symbol}, timeout_seconds)
        if not isinstance(orderbook, dict):
            raise ValueError("orderbook response is not an object")
        bid = _first_level(orderbook, "bids")
        ask = _first_level(orderbook, "asks")
        best_bid = Decimal(str(bid["price"]))
        best_ask = Decimal(str(ask["price"]))
        best_bid_size = Decimal(str(bid["size"]))
        best_ask_size = Decimal(str(ask["size"]))
        mid = (best_bid + best_ask) / Decimal("2")
        live_spread_bps = ((best_ask - best_bid) / mid) * Decimal("10000") if mid > 0 else Decimal("999999")
        eligible = live_spread_bps <= max_spread_bps and live_spread_bps <= provided_24h
        if eligible:
            reason = "spread_ok"
        elif live_spread_bps > max_spread_bps:
            reason = f"live_spread_above_max:{live_spread_bps:.4f}>{max_spread_bps}"
        else:
            reason = f"live_spread_above_24h:{live_spread_bps:.4f}>{provided_24h}"
        return MarketCandidate(
            market=symbol,
            instrument_id=instrument_id,
            lot_size=lot_size,
            tick_size=tick_size,
            min_notional_usd=min_notional_usd,
            provided_current_spread_bps=provided_current,
            provided_24h_spread_bps=provided_24h,
            live_spread_bps=live_spread_bps,
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            eligible=eligible,
            reason=reason,
        )
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
        return MarketCandidate(
            market=symbol,
            instrument_id=instrument_id,
            lot_size=lot_size,
            tick_size=tick_size,
            min_notional_usd=min_notional_usd,
            provided_current_spread_bps=provided_current,
            provided_24h_spread_bps=provided_24h,
            live_spread_bps=Decimal("999999"),
            best_bid=Decimal("0"),
            best_ask=Decimal("0"),
            best_bid_size=Decimal("0"),
            best_ask_size=Decimal("0"),
            eligible=False,
            reason=f"orderbook_error:{exc.__class__.__name__}",
        )


def _first_level(orderbook: dict[str, object], side: str) -> dict[str, object]:
    levels = orderbook[side]
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"{side} is empty")
    first = levels[0]
    if not isinstance(first, dict):
        raise ValueError(f"{side}[0] is not an object")
    return first


def _load_instrument_map(api_endpoint: str, timeout_seconds: float) -> dict[str, dict[str, object]]:
    payload = info_post_json(api_endpoint, "instruments", {"type": "perps"}, timeout_seconds)
    if not isinstance(payload, dict):
        return {}
    perps = payload.get("perps", [])
    if not isinstance(perps, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for item in perps:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if name:
            result[name] = item
    return result


def _roundtrip_order_plan(
    selected: MarketCandidate,
    order_notional: Decimal,
    remaining_gross_volume_usd: Decimal,
    level_size_fraction: Decimal,
) -> dict[str, Decimal]:
    best_bid_notional = selected.best_bid * selected.best_bid_size
    best_ask_notional = selected.best_ask * selected.best_ask_size
    liquidity_cap = min(best_bid_notional, best_ask_notional) * level_size_fraction
    per_side_cap = min(order_notional, remaining_gross_volume_usd / Decimal("2"), liquidity_cap)
    if per_side_cap <= 0:
        return {
            "per_side_cap": Decimal("0"),
            "buy_qty": Decimal("0"),
            "sell_qty": Decimal("0"),
            "planned_gross": Decimal("0"),
        }
    buy_qty = _round_down_to_step(per_side_cap / selected.best_ask, selected.lot_size)
    sell_qty = _round_down_to_step(per_side_cap / selected.best_bid, selected.lot_size)
    return {
        "per_side_cap": per_side_cap,
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "planned_gross": (buy_qty * selected.best_ask) + (sell_qty * selected.best_bid),
    }


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _print_account_summary_status(
    api_endpoint: str,
    credential_prefix: str,
    environment: str,
    timeout_seconds: float,
) -> None:
    params = read_hotstuff_private_readonly_params(credential_prefix, environment)
    try:
        payload = info_post_json(api_endpoint, "accountSummary", params, timeout_seconds, private_readonly=True)
    except (HTTPError, TimeoutError, URLError, ValueError) as exc:
        print("account_summary_private_readonly_ok=False")
        print(f"account_summary_error_type={exc.__class__.__name__}")
        return
    print("account_summary_private_readonly_ok=True")
    if isinstance(payload, dict):
        print("account_summary_keys=" + ",".join(sorted(str(key) for key in payload.keys())[:12]))
    else:
        print(f"account_summary_type={type(payload).__name__}")


if __name__ == "__main__":
    main()
