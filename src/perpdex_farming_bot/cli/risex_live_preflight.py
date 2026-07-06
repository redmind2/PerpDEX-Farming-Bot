from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.error import HTTPError, URLError

from perpdex_farming_bot.connectors.risex_readonly import (
    RisexReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    nonce_state_path,
    normalize_risex_environment,
    read_only_get_json,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    read_risex_private_readonly_params,
    risex_available_private_readonly_env,
    risex_credential_env,
    risex_signing_missing,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


MAX_FIRST_LIVE_NOTIONAL_USD = Decimal("25")


@dataclass(frozen=True)
class RisexTinyOrderPlan:
    eligible: bool
    reason: str
    market_name: str
    best_bid: Decimal
    best_ask: Decimal
    spread_bps: Decimal
    step_size: Decimal
    step_price: Decimal
    min_order_size: Decimal
    planned_size: Decimal
    planned_size_steps: int
    planned_buy_price: Decimal
    planned_buy_price_ticks: int
    planned_sell_price: Decimal
    planned_sell_price_ticks: int
    one_side_notional_usd: Decimal
    gross_roundtrip_notional_usd: Decimal


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RiseX live-test preflight. Read-only by design: sends no orders and no cancels.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="RISEX",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="testnet", help="RiseX environment: testnet or production/mainnet.")
    parser.add_argument("--market-id", type=int, default=1, help="RiseX numeric market ID, e.g. 1 for BTC/USDC.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("10"))
    parser.add_argument("--network", action="store_true", help="Actually call RiseX public/private read-only REST.")
    parser.add_argument(
        "--allow-existing-position",
        action="store_true",
        help="Allow live-test planning when an existing position is present. Default blocks.",
    )
    args = parser.parse_args()

    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > MAX_FIRST_LIVE_NOTIONAL_USD:
        raise SystemExit(f"--max-notional-usd must be <= {MAX_FIRST_LIVE_NOTIONAL_USD} for the first RiseX live test")

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_risex_environment(args.environment)
    credential_env = risex_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("risex_live_preflight=read_only_no_orders")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"market_id={args.market_id}")
    print(f"max_notional_usd={fmt_decimal(args.max_notional_usd)}")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("orders_sent=False")
    print("fresh_orderbook_verify=required_before_live_test")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except RisexReadonlyConfigError as exc:
        print("preflight_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    available_env = risex_available_private_readonly_env(args.credential_prefix, environment)
    signing_missing = risex_signing_missing(args.credential_prefix, environment)
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.signer_address}={masked_env_status(credential_env.signer_address)}")
    print(f"primary_{credential_env.signer_private_key}={masked_env_status(credential_env.signer_private_key)}")
    print(f"private_readonly_env_ready={available_env is not None}")
    print(f"signing_env_ready={not signing_missing}")
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))
    print(f"eth_account_installed={importlib.util.find_spec('eth_account') is not None}")

    key_parse_status, key_matches_signer = _check_signer_private_key(
        signer_address=get_env(credential_env.signer_address),
        signer_private_key=get_env(credential_env.signer_private_key),
    )
    print(f"signer_private_key_parse={key_parse_status}")
    print(f"signer_address_matches_private_key={key_matches_signer}")

    if not args.network:
        print("network_skipped=pass_--network_to_run_risex_live_preflight")
        print("preflight_ready=False")
        return

    all_ok = True
    try:
        system_data = _data_payload(read_only_get_json(api_endpoint, "/v1/system/config", {}, args.timeout_seconds))
        maintenance_mode = _optional_bool(system_data.get("is_maintenance_mode"))
        print("system_config_ok=True")
        print(f"maintenance_mode={_fmt_optional_bool(maintenance_mode)}")
        if maintenance_mode is True:
            all_ok = False
            print("maintenance_check=blocked")
        else:
            print("maintenance_check=ok")

        market_payload = read_only_get_json(
            api_endpoint,
            "/v1/markets",
            {"market_ids": args.market_id},
            args.timeout_seconds,
        )
        market = _select_market(market_payload, args.market_id)
        orderbook_payload = read_only_get_json(
            api_endpoint,
            "/v1/orderbook",
            {"market_id": args.market_id, "limit": 1},
            args.timeout_seconds,
        )
        order_plan = build_tiny_order_plan(
            market=market,
            orderbook_payload=orderbook_payload,
            max_notional_usd=args.max_notional_usd,
        )
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexReadonlyConfigError) as exc:
        print("market_preflight_ok=False")
        print(f"market_preflight_error={exc.__class__.__name__}")
        print("preflight_ready=False")
        return

    print(f"market_preflight_ok={order_plan.eligible}")
    print(f"market_name={order_plan.market_name}")
    print(f"best_bid={fmt_decimal(order_plan.best_bid)}")
    print(f"best_ask={fmt_decimal(order_plan.best_ask)}")
    print(f"spread_bps={fmt_decimal(order_plan.spread_bps)}")
    print(f"step_size={fmt_decimal(order_plan.step_size)}")
    print(f"step_price={fmt_decimal(order_plan.step_price)}")
    print(f"min_order_size={fmt_decimal(order_plan.min_order_size)}")
    print(f"planned_size={fmt_decimal(order_plan.planned_size)}")
    print(f"planned_size_steps={order_plan.planned_size_steps}")
    print(f"planned_buy_price={fmt_decimal(order_plan.planned_buy_price)}")
    print(f"planned_buy_price_ticks={order_plan.planned_buy_price_ticks}")
    print(f"planned_sell_price={fmt_decimal(order_plan.planned_sell_price)}")
    print(f"planned_sell_price_ticks={order_plan.planned_sell_price_ticks}")
    print(f"planned_one_side_notional_usd={fmt_decimal(order_plan.one_side_notional_usd)}")
    print(f"planned_roundtrip_gross_notional_usd={fmt_decimal(order_plan.gross_roundtrip_notional_usd)}")
    print(f"market_preflight_reason={order_plan.reason}")
    all_ok = all_ok and order_plan.eligible

    account = ""
    if available_env is None:
        all_ok = False
        print("private_state_ok=False")
        print("private_readonly_skipped=missing_account_address_env")
        print("nonce_state_ok=False")
        print("position_check=skipped_missing_account")
    else:
        account = read_risex_private_readonly_params(args.credential_prefix, environment)["account"]
        private_ok = _print_private_state(
            api_endpoint=api_endpoint,
            account=account,
            market_id=args.market_id,
            timeout_seconds=args.timeout_seconds,
            allow_existing_position=args.allow_existing_position,
        )
        all_ok = all_ok and private_ok

    signing_ready = (
        not signing_missing
        and key_parse_status == "ok"
        and key_matches_signer == "True"
        and importlib.util.find_spec("eth_account") is not None
    )
    print(f"signing_runtime_ready={signing_ready}")
    all_ok = all_ok and signing_ready

    print("live_execution_ready=requires_risex_live_test_confirmation" if all_ok else "live_execution_ready=False")
    print(f"preflight_ready={all_ok}")


def build_tiny_order_plan(
    *,
    market: dict[str, object],
    orderbook_payload: object,
    max_notional_usd: Decimal,
) -> RisexTinyOrderPlan:
    config = market.get("config") if isinstance(market.get("config"), dict) else {}
    market_name = str(_first_value(config, market, keys=("name", "symbol", "market_name")) or "unknown")
    step_size = _to_decimal(_first_value(config, market, keys=("step_size", "stepSize")))
    step_price = _to_decimal(_first_value(config, market, keys=("step_price", "stepPrice", "tick_size", "tickSize")))
    min_order_size = _to_decimal(_first_value(config, market, keys=("min_order_size", "minOrderSize")))
    availability_values = [
        _optional_bool(_first_value(market, config, keys=(key,)))
        for key in ("available", "unlocked", "is_active", "active")
    ]

    orderbook = _data_payload(orderbook_payload)
    best_bid = _first_level_price(orderbook, ("bids", "bid_levels", "buy"), step_price)
    best_ask = _first_level_price(orderbook, ("asks", "ask_levels", "sell"), step_price)
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        raise ValueError("RiseX orderbook did not contain a usable bid/ask")
    if step_size <= 0 or step_price <= 0 or min_order_size <= 0:
        raise ValueError("RiseX market config did not contain usable size/price steps")

    planned_size = _round_up(min_order_size, step_size)
    max_size = _round_down(max_notional_usd / best_ask, step_size)
    buy_price = _round_down(best_bid, step_price)
    sell_price = _round_up(best_ask, step_price)
    size_steps = int(planned_size / step_size)
    buy_price_ticks = int(buy_price / step_price)
    sell_price_ticks = int(sell_price / step_price)
    one_side_notional = planned_size * best_ask
    mid = (best_bid + best_ask) / Decimal("2")
    spread_bps = (best_ask - best_bid) / mid * Decimal("10000")

    eligible = True
    reason = "ok"
    if any(value is False for value in availability_values):
        eligible = False
        reason = "market_not_available"
    elif max_size < planned_size:
        eligible = False
        reason = "min_order_size_exceeds_max_notional"
    elif buy_price_ticks <= 0 or sell_price_ticks <= 0 or size_steps <= 0:
        eligible = False
        reason = "planned_order_steps_invalid"

    return RisexTinyOrderPlan(
        eligible=eligible,
        reason=reason,
        market_name=market_name,
        best_bid=best_bid,
        best_ask=best_ask,
        spread_bps=spread_bps,
        step_size=step_size,
        step_price=step_price,
        min_order_size=min_order_size,
        planned_size=planned_size,
        planned_size_steps=size_steps,
        planned_buy_price=buy_price,
        planned_buy_price_ticks=buy_price_ticks,
        planned_sell_price=sell_price,
        planned_sell_price_ticks=sell_price_ticks,
        one_side_notional_usd=one_side_notional,
        gross_roundtrip_notional_usd=one_side_notional * Decimal("2"),
    )


def _print_private_state(
    *,
    api_endpoint: str,
    account: str,
    market_id: int,
    timeout_seconds: float,
    allow_existing_position: bool,
) -> bool:
    try:
        nonce_payload = read_only_get_json(
            api_endpoint,
            nonce_state_path(account),
            {},
            timeout_seconds,
            private_readonly=True,
        )
        nonce_data = _data_payload(nonce_payload)
        print("nonce_state_ok=True")
        print(f"nonce_anchor_present={nonce_data.get('nonce_anchor') is not None}")
        print(f"current_bitmap_index_present={nonce_data.get('current_bitmap_index') is not None}")

        positions_payload = read_only_get_json(
            api_endpoint,
            "/v1/positions",
            {"account": account, "market_id": market_id, "page_size": 100},
            timeout_seconds,
            private_readonly=True,
        )
        positions = _extract_positions(positions_payload)
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexReadonlyConfigError) as exc:
        print("private_state_ok=False")
        print(f"private_state_error={exc.__class__.__name__}")
        return False

    nonzero_positions = [position for position in positions if _position_size(position) != 0]
    print("private_state_ok=True")
    print(f"position_count={len(nonzero_positions)}")
    if nonzero_positions and not allow_existing_position:
        print("position_check=blocked_existing_position")
        return False
    print("position_check=ok")
    return True


def _select_market(payload: object, market_id: int) -> dict[str, object]:
    data = _data_payload(payload)
    if isinstance(data, dict):
        raw_markets = data.get("markets") or data.get("items") or data.get("results") or data.get("data")
        if raw_markets is None and _matches_market_id(data, market_id):
            return data
    else:
        raw_markets = data

    if not isinstance(raw_markets, list):
        raise ValueError("RiseX markets response did not contain a market list")
    for item in raw_markets:
        if isinstance(item, dict) and _matches_market_id(item, market_id):
            return item
    raise ValueError("RiseX target market was not found")


def _matches_market_id(market: dict[str, object], market_id: int) -> bool:
    raw = market.get("market_id") or market.get("marketId") or market.get("id")
    return str(raw) == str(market_id)


def _extract_positions(payload: object) -> list[dict[str, object]]:
    data = _data_payload(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    raw = data.get("positions") or data.get("items") or data.get("results") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _position_size(position: dict[str, object]) -> Decimal:
    raw = position.get("size") or position.get("position_size") or position.get("base_size") or "0"
    text = str(raw)
    number = Decimal(text)
    if "." not in text and abs(number) >= Decimal("1000000000000"):
        return number / Decimal("1000000000000000000")
    return number


def _data_payload(payload: object) -> object:
    if isinstance(payload, dict) and "data" in payload:
        data = payload["data"]
        if data is not None:
            return data
    return payload


def _first_value(*containers: object, keys: tuple[str, ...]) -> object:
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_level_price(orderbook: object, side_names: tuple[str, ...], step_price: Decimal) -> Decimal:
    if not isinstance(orderbook, dict):
        raise ValueError("RiseX orderbook response was not a JSON object")
    levels = None
    for name in side_names:
        levels = orderbook.get(name)
        if levels:
            break
    if not isinstance(levels, list) or not levels:
        raise ValueError("RiseX orderbook side was empty")

    first = levels[0]
    if isinstance(first, dict):
        price = first.get("price") or first.get("p")
        if price is not None:
            return _to_decimal(price)
        price_ticks = first.get("price_ticks") or first.get("priceTicks")
        if price_ticks is not None:
            return _to_decimal(price_ticks) * step_price
    if isinstance(first, (list, tuple)) and first:
        return _to_decimal(first[0])
    raise ValueError("RiseX orderbook level did not contain a price")


def _to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def _fmt_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return str(value)


def _check_signer_private_key(*, signer_address: str, signer_private_key: str) -> tuple[str, str]:
    if not signer_private_key:
        return "skipped_missing", "skipped"
    try:
        from eth_account import Account

        derived = Account.from_key(signer_private_key).address
    except Exception:
        return "error", "skipped"

    if not signer_address:
        return "ok", "skipped_missing_signer_address"
    return "ok", str(derived.casefold() == signer_address.casefold())


def fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
