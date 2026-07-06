from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.error import HTTPError, URLError

from perpdex_farming_bot.cli.risex_live_preflight import (
    _data_payload,
    _extract_positions,
    _position_size,
    _select_market,
    _to_decimal,
    build_tiny_order_plan,
    fmt_decimal,
)
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
from perpdex_farming_bot.connectors.risex_trading import (
    RisexSignedPlaceOrder,
    RisexTradingConfigError,
    build_place_order_draft,
    extract_order_id,
    safe_signed_order_summary,
    sign_place_order,
    sign_place_order_verify_signature,
)
from perpdex_farming_bot.credentials import read_risex_credentials, risex_credential_env
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.base import AdapterError
from perpdex_farming_bot.exchanges.risex import RisexAdapter


CONFIRM_TEXT = "LIVE_RISEX_TINY_BTC_ROUNDTRIP"
MAX_FIRST_LIVE_NOTIONAL_USD = Decimal("25")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded RiseX tiny BTC roundtrip. Default is dry-run; live POST requires --execute-live "
            "and the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="RISEX",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="production", help="RiseX environment: production/mainnet or testnet.")
    parser.add_argument("--market-id", type=int, default=1, help="RiseX BTC market ID. Current BTC/USDC is 1.")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("10"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("25"))
    parser.add_argument("--deadline-seconds", type=int, default=300)
    parser.add_argument(
        "--signing-mode",
        choices=("verify-signature", "verify-witness"),
        default="verify-witness",
        help="RiseX order permit signing mode. Official RiseX docs use VerifyWitness.",
    )
    parser.add_argument("--post-order-wait-seconds", type=float, default=1.0)
    parser.add_argument("--position-settle-attempts", type=int, default=8)
    parser.add_argument("--position-settle-delay-seconds", type=float, default=0.7)
    parser.add_argument("--network", action="store_true", help="Actually call RiseX read-only REST and, if confirmed, live POST.")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--close-existing-only",
        action="store_true",
        help="Do not open a new entry. Close the existing BTC position with a reduce-only market sell.",
    )
    parser.add_argument(
        "--allow-existing-position",
        action="store_true",
        help="Allow starting while an existing BTC position is present. Default blocks.",
    )
    args = parser.parse_args()

    _validate_args(args)

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_risex_environment(args.environment)
    credential_env = risex_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("risex_live_test=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"market_id={args.market_id}")
    print(f"execute_live={args.execute_live}")
    print(f"close_existing_only={args.close_existing_only}")
    print(f"max_notional_usd={fmt_decimal(args.max_notional_usd)}")
    print(f"slippage_bps={fmt_decimal(args.slippage_bps)}")
    print(f"signing_mode={args.signing_mode}")
    print("entry_order_type=market_ioc_buy")
    print("close_order_type=market_ioc_reduce_only_sell")
    print("fresh_orderbook_verify=immediately_before_live_post")
    print(f"required_confirmation={CONFIRM_TEXT}")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except RisexReadonlyConfigError as exc:
        print("live_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.signer_address}={masked_env_status(credential_env.signer_address)}")
    print(f"primary_{credential_env.signer_private_key}={masked_env_status(credential_env.signer_private_key)}")

    if not args.network:
        print("network_skipped=pass_--network_to_run_live_test_preflight_or_post")
        print("live_ready=False")
        return

    credentials = read_risex_credentials(args.credential_prefix, environment)
    adapter = RisexAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=False,
    )
    signer_ok, signer_reason = adapter.signer_ready()
    print(f"signer_ready={signer_ok}")
    print(f"signer_ready_reason={signer_reason}")
    if not signer_ok:
        print("live_ready=False")
        return

    account = credentials["account_address"]
    signer = credentials["signer_address"]

    if not _read_only_state_is_ready(api_endpoint, account, signer, args):
        return

    if args.close_existing_only:
        _close_existing_position_only(api_endpoint, credentials, args)
        return

    initial_plan = _build_and_print_order_plan(api_endpoint, args, "initial")
    if initial_plan is None:
        return
    signed_entry = _build_signed_order(
        api_endpoint=api_endpoint,
        credentials=credentials,
        args=args,
        side=0,
        reduce_only=False,
        size_steps=initial_plan.planned_size_steps,
        price_ticks=_buy_price_ticks(initial_plan.best_ask, initial_plan.step_price, args.slippage_bps),
        step_size=initial_plan.step_size,
        step_price=initial_plan.step_price,
        label="dry_run_entry",
    )
    if signed_entry is None:
        return

    if not args.execute_live:
        print("live_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    fresh_plan = _build_and_print_order_plan(api_endpoint, args, "fresh")
    if fresh_plan is None:
        print("live_aborted=fresh_orderbook_verify_failed")
        return
    if fresh_plan.gross_roundtrip_notional_usd > args.max_notional_usd * Decimal("2"):
        print("live_aborted=notional_cap_changed")
        return

    signed_entry = _build_signed_order(
        api_endpoint=api_endpoint,
        credentials=credentials,
        args=args,
        side=0,
        reduce_only=False,
        size_steps=fresh_plan.planned_size_steps,
        price_ticks=_buy_price_ticks(fresh_plan.best_ask, fresh_plan.step_price, args.slippage_bps),
        step_size=fresh_plan.step_size,
        step_price=fresh_plan.step_price,
        label="live_entry",
    )
    if signed_entry is None:
        print("live_aborted=entry_signing_failed")
        return

    live_adapter = RisexAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=True,
    )
    print("live_entry_submitting=True")
    try:
        entry_result = live_adapter.submit_signed_place_order(signed_entry)
    except AdapterError as exc:
        print("live_aborted=adapter_rejected_entry_order")
        print(f"adapter_error={exc}")
        return
    _print_post_result("entry", entry_result)
    if not entry_result.ok:
        print("live_aborted=entry_order_failed")
        return
    entry_order_id = extract_order_id(entry_result.parsed)
    if entry_order_id:
        print(f"entry_order_id={entry_order_id}")

    if args.post_order_wait_seconds:
        time.sleep(args.post_order_wait_seconds)

    detected_steps = _settled_position_steps(api_endpoint, account, fresh_plan.step_size, args, label="post_entry")
    close_size_steps = detected_steps if detected_steps > 0 else fresh_plan.planned_size_steps
    print(f"close_size_steps={close_size_steps}")
    if close_size_steps <= 0:
        print("live_aborted=no_detected_or_planned_close_size")
        return

    close_plan = _build_and_print_order_plan(api_endpoint, args, "close_fresh")
    if close_plan is None:
        print("manual_review_required=True")
        print("reason=close_orderbook_verify_failed")
        return

    signed_close = _build_signed_order(
        api_endpoint=api_endpoint,
        credentials=credentials,
        args=args,
        side=1,
        reduce_only=True,
        size_steps=close_size_steps,
        price_ticks=_sell_price_ticks(close_plan.best_bid, close_plan.step_price, args.slippage_bps),
        step_size=close_plan.step_size,
        step_price=close_plan.step_price,
        label="live_close",
    )
    if signed_close is None:
        print("manual_review_required=True")
        print("reason=close_signing_failed")
        return

    print("live_close_submitting=True")
    try:
        close_result = live_adapter.submit_signed_place_order(signed_close)
    except AdapterError as exc:
        print("manual_review_required=True")
        print("live_aborted=adapter_rejected_close_order")
        print(f"adapter_error={exc}")
        return
    _print_post_result("close", close_result)
    if not close_result.ok:
        print("manual_review_required=True")
        return

    if args.post_order_wait_seconds:
        time.sleep(args.post_order_wait_seconds)
    final_steps = _settled_position_steps(api_endpoint, account, fresh_plan.step_size, args, label="final")
    print(f"final_position_steps={final_steps}")
    if final_steps == 0:
        print("live_test_status=closed_flat_or_not_detected")
    else:
        print("live_test_status=position_remains_manual_review_required")


def _validate_args(args: argparse.Namespace) -> None:
    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > MAX_FIRST_LIVE_NOTIONAL_USD:
        raise SystemExit(f"--max-notional-usd must be <= {MAX_FIRST_LIVE_NOTIONAL_USD} for this guarded live test")
    if args.slippage_bps <= 0 or args.slippage_bps > Decimal("100"):
        raise SystemExit("--slippage-bps must be > 0 and <= 100")
    if args.deadline_seconds <= 0 or args.deadline_seconds > 1800:
        raise SystemExit("--deadline-seconds must be > 0 and <= 1800")
    if args.post_order_wait_seconds < 0:
        raise SystemExit("--post-order-wait-seconds must be zero or greater")
    if args.position_settle_attempts <= 0:
        raise SystemExit("--position-settle-attempts must be greater than zero")
    if args.position_settle_delay_seconds < 0:
        raise SystemExit("--position-settle-delay-seconds must be zero or greater")


def _read_only_state_is_ready(api_endpoint: str, account: str, signer: str, args: argparse.Namespace) -> bool:
    try:
        system_data = _data_payload(read_only_get_json(api_endpoint, "/v1/system/config", {}, args.timeout_seconds))
        maintenance = system_data.get("is_maintenance_mode") if isinstance(system_data, dict) else None
        print("system_config_ok=True")
        print(f"maintenance_mode={maintenance}")
        if maintenance is True:
            print("live_ready=False")
            print("reason=maintenance_mode")
            return False

        session_payload = read_only_get_json(
            api_endpoint,
            "/v1/auth/session-key-status",
            {"account": account, "signer": signer},
            args.timeout_seconds,
            private_readonly=True,
        )
        session_data = _data_payload(session_payload)
        status = session_data.get("status") if isinstance(session_data, dict) else None
        description = session_data.get("status_description") if isinstance(session_data, dict) else None
        print("session_key_status_ok=True")
        print(f"session_key_status={status}")
        print(f"session_key_status_description={description or 'unknown'}")
        if str(status) != "1":
            print("live_ready=False")
            print("reason=session_key_not_active")
            return False

        open_orders_payload = read_only_get_json(
            api_endpoint,
            "/v1/orders/open",
            {"account": account, "market_id": args.market_id, "limit": 25},
            args.timeout_seconds,
            private_readonly=True,
        )
        open_orders = _extract_open_orders(open_orders_payload)
        print(f"start_open_order_count={len(open_orders)}")
        if open_orders:
            print("live_ready=False")
            print("reason=existing_open_orders_detected")
            return False

        positions_payload = read_only_get_json(
            api_endpoint,
            "/v1/positions",
            {"account": account, "market_id": args.market_id, "page_size": 100},
            args.timeout_seconds,
            private_readonly=True,
        )
        positions = _extract_positions(positions_payload)
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexReadonlyConfigError) as exc:
        print("live_ready=False")
        print(f"read_only_state_error={exc.__class__.__name__}")
        return False

    nonzero_positions = [position for position in positions if _position_size(position) != 0]
    print(f"start_position_count={len(nonzero_positions)}")
    if nonzero_positions and not args.allow_existing_position and not args.close_existing_only:
        print("live_ready=False")
        print("reason=existing_positions_detected")
        return False
    return True


def _close_existing_position_only(api_endpoint: str, credentials: dict[str, str], args: argparse.Namespace) -> None:
    close_plan = _build_and_print_order_plan(api_endpoint, args, "close_existing")
    if close_plan is None:
        print("manual_review_required=True")
        print("reason=close_existing_orderbook_verify_failed")
        return

    close_size_steps = _settled_position_steps(
        api_endpoint,
        credentials["account_address"],
        close_plan.step_size,
        args,
        label="close_existing",
    )
    print(f"close_existing_size_steps={close_size_steps}")
    if close_size_steps <= 0:
        print("manual_review_required=True")
        print("reason=no_existing_position_detected")
        return

    signed_close = _build_signed_order(
        api_endpoint=api_endpoint,
        credentials=credentials,
        args=args,
        side=1,
        reduce_only=True,
        size_steps=close_size_steps,
        price_ticks=_sell_price_ticks(close_plan.best_bid, close_plan.step_price, args.slippage_bps),
        step_size=close_plan.step_size,
        step_price=close_plan.step_price,
        label="close_existing",
    )
    if signed_close is None:
        print("manual_review_required=True")
        print("reason=close_existing_signing_failed")
        return

    if not args.execute_live:
        print("live_ready=True")
        print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
        return
    if args.confirm != CONFIRM_TEXT:
        print("live_ready=True")
        print("live_skipped=confirmation_mismatch")
        return

    live_adapter = RisexAdapter(
        api_endpoint=api_endpoint,
        credential_prefix=args.credential_prefix,
        environment=args.environment,
        timeout_seconds=args.timeout_seconds,
        allow_live_orders=True,
    )
    print("close_existing_submitting=True")
    try:
        close_result = live_adapter.submit_signed_place_order(signed_close)
    except AdapterError as exc:
        print("manual_review_required=True")
        print("live_aborted=adapter_rejected_close_existing_order")
        print(f"adapter_error={exc}")
        return
    _print_post_result("close_existing", close_result)
    if not close_result.ok:
        print("manual_review_required=True")
        return

    if args.post_order_wait_seconds:
        time.sleep(args.post_order_wait_seconds)
    final_steps = _settled_position_steps(
        api_endpoint,
        credentials["account_address"],
        close_plan.step_size,
        args,
        label="final",
    )
    print(f"final_position_steps={final_steps}")
    if final_steps == 0:
        print("live_test_status=closed_flat_or_not_detected")
    else:
        print("live_test_status=position_remains_manual_review_required")


def _build_and_print_order_plan(api_endpoint: str, args: argparse.Namespace, label: str):
    try:
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
        print(f"{label}_market_preflight_ok=False")
        print(f"{label}_market_preflight_error={exc.__class__.__name__}")
        print("live_ready=False")
        return None

    print(f"{label}_market_preflight_ok={order_plan.eligible}")
    print(f"{label}_market_name={order_plan.market_name}")
    print(f"{label}_best_bid={fmt_decimal(order_plan.best_bid)}")
    print(f"{label}_best_ask={fmt_decimal(order_plan.best_ask)}")
    print(f"{label}_spread_bps={fmt_decimal(order_plan.spread_bps)}")
    print(f"{label}_planned_size={fmt_decimal(order_plan.planned_size)}")
    print(f"{label}_planned_size_steps={order_plan.planned_size_steps}")
    print(f"{label}_planned_one_side_notional_usd={fmt_decimal(order_plan.one_side_notional_usd)}")
    print(f"{label}_planned_roundtrip_gross_notional_usd={fmt_decimal(order_plan.gross_roundtrip_notional_usd)}")
    print(f"{label}_market_preflight_reason={order_plan.reason}")
    if not order_plan.eligible:
        print("live_ready=False")
        print(f"reason={label}_market_not_eligible")
        return None
    return order_plan


def _build_signed_order(
    *,
    api_endpoint: str,
    credentials: dict[str, str],
    args: argparse.Namespace,
    side: int,
    reduce_only: bool,
    size_steps: int,
    price_ticks: int,
    step_size: Decimal,
    step_price: Decimal,
    label: str,
) -> RisexSignedPlaceOrder | None:
    try:
        domain_payload = read_only_get_json(api_endpoint, "/v1/auth/eip712-domain", {}, args.timeout_seconds)
        domain_data = _data_payload(domain_payload)
        nonce_data = _next_nonce_data(api_endpoint, credentials["account_address"], args.timeout_seconds)
        client_order_id = "0"
        draft = build_place_order_draft(
            market_id=args.market_id,
            size_steps=size_steps,
            price_ticks=0,
            size_wad=_decimal_to_wad(Decimal(size_steps) * step_size),
            price_wad=0,
            side=side,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            post_only=False,
            stp_mode=0,
            order_type=0,
            time_in_force=3,
            ttl_units=0,
            expiry=0,
        )
        if args.signing_mode == "verify-signature":
            nonce_data = _next_nonce_data(api_endpoint, credentials["account_address"], args.timeout_seconds)
            composite_nonce = _composite_nonce(
                str(nonce_data["nonce_anchor"]),
                int(nonce_data["nonce_bitmap_index"]),
            )
            system_payload = read_only_get_json(api_endpoint, "/v1/system/config", {}, args.timeout_seconds)
            system_data = _data_payload(system_payload)
            addresses = system_data.get("addresses", {}) if isinstance(system_data, dict) else {}
            target_contract = addresses.get("perps_manager")
            if not target_contract:
                raise RisexTradingConfigError("system config did not include perps_manager target contract")
            signed = sign_place_order_verify_signature(
                draft=draft,
                account=credentials["account_address"],
                signer=credentials["signer_address"],
                signer_private_key=credentials["signer_private_key"],
                eip712_domain=domain_data,
                target_contract=str(target_contract),
                nonce=composite_nonce,
                nonce_anchor=str(nonce_data["nonce_anchor"]),
                nonce_bitmap_index=int(nonce_data["nonce_bitmap_index"]),
                deadline_seconds=int(time.time()) + args.deadline_seconds,
                request_shape="flat",
            )
        else:
            nonce_data = _next_nonce_data(api_endpoint, credentials["account_address"], args.timeout_seconds)
            system_payload = read_only_get_json(api_endpoint, "/v1/system/config", {}, args.timeout_seconds)
            system_data = _data_payload(system_payload)
            addresses = system_data.get("addresses", {}) if isinstance(system_data, dict) else {}
            target_contract = addresses.get("router") or addresses.get("auth")
            if not target_contract:
                raise RisexTradingConfigError("system config did not include router/auth target contract")
            signed = sign_place_order(
                draft=draft,
                account=credentials["account_address"],
                signer=credentials["signer_address"],
                signer_private_key=credentials["signer_private_key"],
                eip712_domain=domain_data,
                target_contract=str(target_contract),
                nonce_anchor=nonce_data["nonce_anchor"],
                nonce_bitmap_index=nonce_data["nonce_bitmap_index"],
                deadline_seconds=int(time.time()) + args.deadline_seconds,
            )
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexTradingConfigError) as exc:
        print(f"{label}_signed_order_ready=False")
        print(f"{label}_signing_error={exc.__class__.__name__}")
        return None

    _print_signed_order_summary(label, signed)
    return signed


def _next_nonce_data(api_endpoint: str, account: str, timeout_seconds: float) -> dict[str, object]:
    nonce_payload = read_only_get_json(
        api_endpoint,
        nonce_state_path(account),
        {},
        timeout_seconds,
        private_readonly=True,
    )
    data = _data_payload(nonce_payload)
    if not isinstance(data, dict):
        raise RisexTradingConfigError("nonce-state response was not an object")
    nonce_anchor = int(data["nonce_anchor"])
    bitmap_index = int(data["current_bitmap_index"])
    if bitmap_index >= 208:
        nonce_anchor += 1
        bitmap_index = 0
    return {"nonce_anchor": str(nonce_anchor), "nonce_bitmap_index": bitmap_index}


def _composite_nonce(nonce_anchor: str, nonce_bitmap_index: int) -> str:
    return str((int(nonce_anchor) << 8) | int(nonce_bitmap_index))


def _decimal_to_wad(value: Decimal) -> int:
    return int((value * Decimal("1000000000000000000")).to_integral_value(rounding=ROUND_FLOOR))


def _settled_position_steps(
    api_endpoint: str,
    account: str,
    step_size: Decimal,
    args: argparse.Namespace,
    *,
    label: str,
) -> int:
    for attempt in range(1, args.position_settle_attempts + 1):
        try:
            positions_payload = read_only_get_json(
                api_endpoint,
                "/v1/positions",
                {"account": account, "market_id": args.market_id, "page_size": 100},
                args.timeout_seconds,
                private_readonly=True,
            )
            positions = _extract_positions(positions_payload)
            sizes = [_position_size(position) for position in positions]
            nonzero = [size for size in sizes if size != 0]
        except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError, RisexReadonlyConfigError) as exc:
            print(f"{label}_position_check_error={exc.__class__.__name__}")
            return 0

        print(f"{label}_position_check_attempt={attempt}")
        print(f"{label}_position_count={len(nonzero)}")
        if nonzero:
            steps = _size_to_steps(abs(nonzero[0]), step_size)
            print(f"{label}_detected_position_steps={steps}")
            return steps
        if args.position_settle_delay_seconds:
            time.sleep(args.position_settle_delay_seconds)
    return 0


def _extract_open_orders(payload: object) -> list[dict[str, object]]:
    data = _data_payload(payload)
    if not isinstance(data, dict):
        return []
    raw = data.get("orders") or data.get("items") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _size_to_steps(size: Decimal, step_size: Decimal) -> int:
    if size <= 0:
        return 0
    if size >= 1 and size == size.to_integral_value():
        return int(size)
    return int((size / step_size).to_integral_value(rounding=ROUND_CEILING))


def _buy_price_ticks(best_ask: Decimal, step_price: Decimal, slippage_bps: Decimal) -> int:
    price = best_ask * (Decimal("1") + slippage_bps / Decimal("10000"))
    return int((price / step_price).to_integral_value(rounding=ROUND_CEILING))


def _sell_price_ticks(best_bid: Decimal, step_price: Decimal, slippage_bps: Decimal) -> int:
    price = best_bid * (Decimal("1") - slippage_bps / Decimal("10000"))
    return int((price / step_price).to_integral_value(rounding=ROUND_FLOOR))


def _client_order_id(salt: int) -> str:
    return str((time.time_ns() // 1000 + salt) % 18_446_744_073_709_551_615)


def _print_signed_order_summary(label: str, signed: RisexSignedPlaceOrder) -> None:
    summary = safe_signed_order_summary(signed)
    print(f"{label}_signed_order_ready=True")
    for key, value in summary.items():
        print(f"{label}_{key}={value}")


def _print_post_result(label: str, result) -> None:
    print(f"{label}_post_ok={result.ok}")
    print(f"{label}_post_status_code={result.status_code}")
    print(f"{label}_post_body_shape={result.body_shape}")
    if result.error:
        print(f"{label}_post_error={result.error}")
    data = _data_payload(result.parsed)
    if isinstance(data, dict):
        for key in ("order_id", "tx_hash", "block_number", "sc_order_id", "success"):
            if data.get(key) is not None:
                print(f"{label}_{key}={data.get(key)}")
        message = data.get("message") or data.get("error")
        if isinstance(message, dict):
            message = message.get("message") or message.get("reason") or str(message)
        if message:
            print(f"{label}_post_message={message}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
