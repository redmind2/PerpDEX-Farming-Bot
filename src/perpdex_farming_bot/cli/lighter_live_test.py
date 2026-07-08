from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from perpdex_farming_bot.connectors.lighter_readonly import (
    LighterReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_lighter_environment,
    read_only_get_json,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    lighter_credential_env,
    lighter_signing_missing,
    read_lighter_credentials,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.core.execution_event import ExecutionEvent, emit_execution_event
from perpdex_farming_bot.marketdata.lighter import (
    LighterMarketMetadata,
    fetch_lighter_rest_top_of_book,
    lighter_market_metadata_from_order_books,
)


CONFIRM_TEXT = "LIVE_LIGHTER_TINY_BTC_ROUNDTRIP"
MAX_FIRST_LIVE_NOTIONAL_USD = Decimal("30")
DEFAULT_BTC_MARKET_ID = 1
DEFAULT_BTC_SYMBOL = "BTC"


@dataclass(frozen=True)
class TinyOrderPlan:
    market_id: int
    symbol: str
    best_bid: Decimal
    best_ask: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    spread_bps: Decimal
    size_decimals: int
    price_decimals: int
    min_base_amount: Decimal
    min_quote_amount: Decimal
    planned_size: Decimal
    planned_base_amount: int
    planned_one_side_notional_usd: Decimal
    aggressive_buy_price: Decimal
    aggressive_sell_price: Decimal
    aggressive_buy_price_raw: int
    aggressive_sell_price_raw: int
    taker_fee_percent: Decimal | None
    maker_fee_percent: Decimal | None
    eligible: bool
    reason: str


@dataclass(frozen=True)
class PrivateState:
    account_ok: bool
    position_count: int
    selected_position_amount: Decimal
    open_order_count: int
    reason: str


@dataclass(frozen=True)
class SendTxSummary:
    ok: bool
    code: int | None
    message_present: bool
    tx_hash_present: bool
    predicted_execution_time_ms: int | None
    volume_quota_remaining: int | None
    error_present: bool
    elapsed_ms: Decimal | None = None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded Lighter tiny BTC roundtrip. Default is read-only dry-run; live orders require "
            "--execute-live and the exact confirmation string."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="LIGHTER",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="production", help="Lighter environment: production/mainnet only.")
    parser.add_argument("--symbol", default=DEFAULT_BTC_SYMBOL)
    parser.add_argument("--market-id", type=int, default=DEFAULT_BTC_MARKET_ID)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--quote-amount-usd", type=Decimal, default=Decimal("25"))
    parser.add_argument("--max-notional-usd", type=Decimal, default=MAX_FIRST_LIVE_NOTIONAL_USD)
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("50"))
    parser.add_argument("--max-spread-bps", type=Decimal, default=Decimal("20"))
    parser.add_argument("--orderbook-limit", type=int, default=20)
    parser.add_argument("--settle-attempts", type=int, default=20)
    parser.add_argument("--settle-delay-seconds", type=float, default=0.1)
    parser.add_argument("--trade-poll-attempts", type=int, default=6)
    parser.add_argument("--trade-poll-delay-seconds", type=float, default=0.5)
    parser.add_argument(
        "--close-mode",
        choices=("confirmed", "optimistic", "netting"),
        default="confirmed",
        help=(
            "confirmed waits for the entry position before close. optimistic sends a reduce-only close after a short "
            "delay. netting sends the opposite non-reduce-only order immediately, then rescues any residual position."
        ),
    )
    parser.add_argument("--optimistic-close-delay-seconds", type=float, default=0.15)
    parser.add_argument("--network", action="store_true", help="Call Lighter read-only REST and, if confirmed, live POST.")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--allow-existing-position",
        action="store_true",
        help="Allow starting while any existing Lighter position is present. Default blocks.",
    )
    args = parser.parse_args()

    _validate_args(args)
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_lighter_environment(args.environment)
    credential_env = lighter_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("lighter_live_test=explicit_confirm_required")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"symbol={args.symbol}")
    print(f"market_id={args.market_id}")
    print(f"execute_live={args.execute_live}")
    print(f"quote_amount_usd={fmt_decimal(args.quote_amount_usd)}")
    print(f"max_notional_usd={fmt_decimal(args.max_notional_usd)}")
    print(f"slippage_bps={fmt_decimal(args.slippage_bps)}")
    print(f"max_spread_bps={fmt_decimal(args.max_spread_bps)}")
    print("entry_order_type=market_ioc_buy")
    close_order_type = "market_ioc_sell" if args.close_mode == "netting" else "market_ioc_reduce_only_sell"
    print(f"close_order_type={close_order_type}")
    print(f"close_mode={args.close_mode}")
    print("fast_close_on_position_detected=True")
    print("fresh_orderbook_verify=immediately_before_each_live_order")
    print(f"required_confirmation={CONFIRM_TEXT}")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except LighterReadonlyConfigError as exc:
        print("live_ready=False")
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

    if not args.network:
        print("network_skipped=pass_--network_to_run_lighter_live_preflight_or_post")
        print("live_ready=False")
        return

    credentials = read_lighter_credentials(args.credential_prefix, environment)
    try:
        account_index = _required_int(credentials["account_index"], "LIGHTER_ACCOUNT_INDEX")
        api_key_index = _required_int(credentials["api_key_index"], "LIGHTER_API_KEY_INDEX")
    except ValueError as exc:
        print("live_ready=False")
        print(f"credential_parse_error={exc}")
        return

    if signing_missing:
        print("live_ready=False")
        print("reason=missing_lighter_signing_env")
        return

    signer = None
    auth_token = ""
    try:
        signer = _build_signer(
            api_endpoint=api_endpoint,
            account_index=account_index,
            api_key_index=api_key_index,
            api_private_key=credentials["api_private_key"],
        )
        auth_token, auth_error = signer.create_auth_token_with_expiry(api_key_index=api_key_index)
        print(f"auth_token_created={bool(auth_token) and not auth_error}")
        if auth_error:
            print("live_ready=False")
            print("reason=auth_token_signing_failed")
            return

        start_state = _load_and_print_private_state(
            api_endpoint=api_endpoint,
            account_index=account_index,
            auth_token=auth_token,
            market_id=args.market_id,
            symbol=args.symbol,
            timeout_seconds=args.timeout_seconds,
            label="start",
        )
        if not _start_state_is_safe(start_state, args):
            print("live_ready=False")
            return

        initial_plan = _build_and_print_plan(api_endpoint, args, "initial")
        if initial_plan is None or not initial_plan.eligible:
            print("live_ready=False")
            return

        if not args.execute_live:
            print("live_ready=True")
            print(f"live_skipped=pass_--execute-live_and_--confirm_{CONFIRM_TEXT}")
            return
        if args.confirm != CONFIRM_TEXT:
            print("live_ready=True")
            print("live_skipped=confirmation_mismatch")
            return

        await _execute_live_roundtrip(
            signer=signer,
            api_endpoint=api_endpoint,
            account_index=account_index,
            api_key_index=api_key_index,
            auth_token=auth_token,
            args=args,
        )
    finally:
        if signer is not None:
            await signer.close()


async def _execute_live_roundtrip(
    *,
    signer: Any,
    api_endpoint: str,
    account_index: int,
    api_key_index: int,
    auth_token: str,
    args: argparse.Namespace,
) -> None:
    entry_plan = _build_and_print_plan(api_endpoint, args, "fresh_entry")
    if entry_plan is None or not entry_plan.eligible:
        print("live_aborted=fresh_entry_orderbook_verify_failed")
        return

    client_base = int(time.time() * 1000) % 2_000_000_000
    entry_client_order_index = client_base
    close_client_order_index = client_base + 1

    print("live_entry_submitting=True")
    order_start = time.perf_counter()
    entry_start = time.perf_counter()
    try:
        _, entry_response, entry_error = await signer.create_order(
            market_index=args.market_id,
            client_order_index=entry_client_order_index,
            base_amount=entry_plan.planned_base_amount,
            price=entry_plan.aggressive_buy_price_raw,
            is_ask=False,
            order_type=signer.ORDER_TYPE_MARKET,
            time_in_force=signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            reduce_only=False,
            order_expiry=signer.DEFAULT_IOC_EXPIRY,
            api_key_index=api_key_index,
        )
    except Exception as exc:
        print("live_aborted=entry_order_exception")
        print(f"entry_error_type={exc.__class__.__name__}")
        return
    entry_elapsed_ms = (time.perf_counter() - entry_start) * 1000
    entry_summary = _send_tx_summary(entry_response, entry_error, entry_elapsed_ms)
    _print_send_tx_summary("entry", entry_summary, entry_elapsed_ms)
    if not entry_summary.ok:
        print("live_aborted=entry_send_tx_not_ok")
        return

    client_order_indexes = {entry_client_order_index, close_client_order_index}
    close_summary: SendTxSummary | None
    roundtrip_order_elapsed_ms: Decimal | None = None
    if args.close_mode == "netting":
        print("netting_roundtrip_enabled=True")
        close_summary = await _submit_market_close(
            signer=signer,
            api_key_index=api_key_index,
            market_id=args.market_id,
            client_order_index=close_client_order_index,
            close_base_amount=entry_plan.planned_base_amount,
            close_price_raw=entry_plan.aggressive_sell_price_raw,
            close_is_ask=True,
            reduce_only=False,
            label="close",
        )
        roundtrip_order_elapsed_ms = _elapsed_decimal_from_perf(order_start)
        print(f"roundtrip_order_elapsed_ms={roundtrip_order_elapsed_ms}")
        netting_amount = _poll_selected_position(
            api_endpoint=api_endpoint,
            account_index=account_index,
            auth_token=auth_token,
            market_id=args.market_id,
            symbol=args.symbol,
            timeout_seconds=args.timeout_seconds,
            attempts=args.settle_attempts,
            delay_seconds=args.settle_delay_seconds,
            label="netting_post_close",
            want_flat=True,
        )
        if netting_amount != 0:
            rescue_client_order_index = close_client_order_index + 1
            client_order_indexes.add(rescue_client_order_index)
            print("netting_rescue_required=True")
            close_summary = await _submit_confirmed_close(
                signer=signer,
                api_endpoint=api_endpoint,
                api_key_index=api_key_index,
                auth_token=auth_token,
                account_index=account_index,
                client_order_index=rescue_client_order_index,
                entry_amount=netting_amount,
                args=args,
                label="rescue_close",
            )
        else:
            print("netting_rescue_required=False")
    elif args.close_mode == "optimistic":
        print("optimistic_close_enabled=True")
        print(f"optimistic_close_delay_seconds={args.optimistic_close_delay_seconds}")
        if args.optimistic_close_delay_seconds:
            time.sleep(args.optimistic_close_delay_seconds)
        close_summary = await _submit_market_close(
            signer=signer,
            api_key_index=api_key_index,
            market_id=args.market_id,
            client_order_index=close_client_order_index,
            close_base_amount=entry_plan.planned_base_amount,
            close_price_raw=entry_plan.aggressive_sell_price_raw,
            close_is_ask=True,
            reduce_only=True,
            label="close",
        )
        roundtrip_order_elapsed_ms = _elapsed_decimal_from_perf(order_start)
        print(f"roundtrip_order_elapsed_ms={roundtrip_order_elapsed_ms}")
        optimistic_amount = _poll_selected_position(
            api_endpoint=api_endpoint,
            account_index=account_index,
            auth_token=auth_token,
            market_id=args.market_id,
            symbol=args.symbol,
            timeout_seconds=args.timeout_seconds,
            attempts=args.settle_attempts,
            delay_seconds=args.settle_delay_seconds,
            label="optimistic_post_close",
            want_flat=True,
        )
        if optimistic_amount != 0:
            rescue_client_order_index = close_client_order_index + 1
            client_order_indexes.add(rescue_client_order_index)
            print("optimistic_close_rescue_required=True")
            close_summary = await _submit_confirmed_close(
                signer=signer,
                api_endpoint=api_endpoint,
                api_key_index=api_key_index,
                auth_token=auth_token,
                account_index=account_index,
                client_order_index=rescue_client_order_index,
                entry_amount=optimistic_amount,
                args=args,
                label="rescue_close",
            )
        else:
            print("optimistic_close_rescue_required=False")
    else:
        entry_amount = _poll_selected_position(
            api_endpoint=api_endpoint,
            account_index=account_index,
            auth_token=auth_token,
            market_id=args.market_id,
            symbol=args.symbol,
            timeout_seconds=args.timeout_seconds,
            attempts=args.settle_attempts,
            delay_seconds=args.settle_delay_seconds,
            label="post_entry",
        )
        if entry_amount == 0:
            print("manual_review_required=True")
            print("live_aborted=entry_position_not_detected")
            _print_active_orders(
                api_endpoint=api_endpoint,
                account_index=account_index,
                auth_token=auth_token,
                market_id=args.market_id,
                timeout_seconds=args.timeout_seconds,
                label="post_entry",
            )
            return
        close_summary = await _submit_confirmed_close(
            signer=signer,
            api_endpoint=api_endpoint,
            api_key_index=api_key_index,
            auth_token=auth_token,
            account_index=account_index,
            client_order_index=close_client_order_index,
            entry_amount=entry_amount,
            args=args,
            label="close",
        )
        roundtrip_order_elapsed_ms = _elapsed_decimal_from_perf(order_start)
        print(f"roundtrip_order_elapsed_ms={roundtrip_order_elapsed_ms}")

    if close_summary is None or (args.close_mode == "confirmed" and not close_summary.ok):
        print("manual_review_required=True")
        print("live_aborted=close_send_tx_not_ok")
        return

    final_amount = _poll_selected_position(
        api_endpoint=api_endpoint,
        account_index=account_index,
        auth_token=auth_token,
        market_id=args.market_id,
        symbol=args.symbol,
        timeout_seconds=args.timeout_seconds,
        attempts=args.settle_attempts,
        delay_seconds=args.settle_delay_seconds,
        label="final",
        want_flat=True,
    )
    final_state = _load_and_print_private_state(
        api_endpoint=api_endpoint,
        account_index=account_index,
        auth_token=auth_token,
        market_id=args.market_id,
        symbol=args.symbol,
        timeout_seconds=args.timeout_seconds,
        label="final",
    )
    trade_summary = _poll_and_print_trade_summary(
        api_endpoint=api_endpoint,
        account_index=account_index,
        auth_token=auth_token,
        market_id=args.market_id,
        client_order_indexes=client_order_indexes,
        timeout_seconds=args.timeout_seconds,
        attempts=args.trade_poll_attempts,
        delay_seconds=args.trade_poll_delay_seconds,
    )
    print("ledger_event_schema_version=1")
    final_all_flat = final_amount == 0 and final_state.open_order_count == 0
    emit_execution_event(
        ExecutionEvent(
            exchange="lighter",
            account_label=args.credential_prefix,
            wallet_label=args.credential_prefix,
            market=f"{args.symbol}-PERP",
            cycle_id="live_roundtrip",
            environment="PRODUCTION",
            status="closed_flat" if final_all_flat else "position_or_open_order_remains_manual_review_required",
            planned_gross_volume_usd=entry_plan.planned_one_side_notional_usd * Decimal("2"),
            filled_gross_volume_usd=_optional_decimal_value(trade_summary["matched_trade_gross_usd"]),
            start_position_count=0,
            final_position_count=0 if final_amount == 0 else 1,
            start_open_order_count=0,
            final_open_order_count=final_state.open_order_count,
            final_all_flat=final_all_flat,
            entry_post_latency_ms=entry_summary.elapsed_ms,
            close_post_latency_ms=close_summary.elapsed_ms,
            cycle_total_latency_ms=roundtrip_order_elapsed_ms,
            matched_trade_count=int(trade_summary["matched_trade_count"]),
            matched_trade_gross_usd=_optional_decimal_value(trade_summary["matched_trade_gross_usd"]),
            matched_trade_fee_usd_estimate=_optional_decimal_value(trade_summary["matched_trade_fee_usd_estimate"]),
        )
    )
    if final_all_flat:
        print("final_all_flat=True")
        print("live_test_status=closed_flat")
    else:
        print("final_all_flat=False")
        print("manual_review_required=True")
        print("live_test_status=position_or_open_order_remains_manual_review_required")


async def _submit_confirmed_close(
    *,
    signer: Any,
    api_endpoint: str,
    api_key_index: int,
    auth_token: str,
    account_index: int,
    client_order_index: int,
    entry_amount: Decimal,
    args: argparse.Namespace,
    label: str,
) -> SendTxSummary | None:
    del auth_token, account_index
    close_plan = _build_and_print_plan(api_endpoint, args, f"fresh_{label}", close_size=abs(entry_amount))
    if close_plan is None or not close_plan.eligible:
        print("manual_review_required=True")
        print(f"live_aborted=fresh_{label}_orderbook_verify_failed")
        return None

    close_is_ask = entry_amount > 0
    close_price_raw = close_plan.aggressive_sell_price_raw if close_is_ask else close_plan.aggressive_buy_price_raw
    close_base_amount = _decimal_to_raw(abs(entry_amount), close_plan.size_decimals, ROUND_FLOOR)
    if close_base_amount <= 0:
        print("manual_review_required=True")
        print(f"live_aborted={label}_base_amount_rounded_to_zero")
        return None

    return await _submit_market_close(
        signer=signer,
        api_key_index=api_key_index,
        market_id=args.market_id,
        client_order_index=client_order_index,
        close_base_amount=close_base_amount,
        close_price_raw=close_price_raw,
        close_is_ask=close_is_ask,
        reduce_only=True,
        label=label,
    )


async def _submit_market_close(
    *,
    signer: Any,
    api_key_index: int,
    market_id: int,
    client_order_index: int,
    close_base_amount: int,
    close_price_raw: int,
    close_is_ask: bool,
    reduce_only: bool,
    label: str,
) -> SendTxSummary | None:
    print(f"live_{label}_submitting=True")
    print(f"{label}_reduce_only={reduce_only}")
    close_start = time.perf_counter()
    try:
        _, close_response, close_error = await signer.create_order(
            market_index=market_id,
            client_order_index=client_order_index,
            base_amount=close_base_amount,
            price=close_price_raw,
            is_ask=close_is_ask,
            order_type=signer.ORDER_TYPE_MARKET,
            time_in_force=signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            reduce_only=reduce_only,
            order_expiry=signer.DEFAULT_IOC_EXPIRY,
            api_key_index=api_key_index,
        )
    except Exception as exc:
        print("manual_review_required=True")
        print(f"live_aborted={label}_order_exception")
        print(f"{label}_error_type={exc.__class__.__name__}")
        return None
    close_elapsed_ms = (time.perf_counter() - close_start) * 1000
    close_summary = _send_tx_summary(close_response, close_error, close_elapsed_ms)
    _print_send_tx_summary(label, close_summary, close_elapsed_ms)
    return close_summary


def _validate_args(args: argparse.Namespace) -> None:
    if normalize_lighter_environment(args.environment) != "PRODUCTION":
        raise SystemExit("This live test is currently limited to Lighter production/mainnet")
    if args.market_id < 0:
        raise SystemExit("--market-id must be zero or greater")
    if not args.symbol:
        raise SystemExit("--symbol is required")
    if args.quote_amount_usd <= 0:
        raise SystemExit("--quote-amount-usd must be greater than zero")
    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > MAX_FIRST_LIVE_NOTIONAL_USD:
        raise SystemExit(f"--max-notional-usd must be <= {MAX_FIRST_LIVE_NOTIONAL_USD} for this guarded live test")
    if args.quote_amount_usd > args.max_notional_usd:
        raise SystemExit("--quote-amount-usd must be <= --max-notional-usd")
    if args.slippage_bps <= 0 or args.slippage_bps > Decimal("100"):
        raise SystemExit("--slippage-bps must be > 0 and <= 100")
    if args.max_spread_bps <= 0 or args.max_spread_bps > Decimal("100"):
        raise SystemExit("--max-spread-bps must be > 0 and <= 100")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be greater than zero")
    if args.orderbook_limit <= 0:
        raise SystemExit("--orderbook-limit must be greater than zero")
    if args.settle_attempts <= 0:
        raise SystemExit("--settle-attempts must be greater than zero")
    if args.settle_delay_seconds < 0:
        raise SystemExit("--settle-delay-seconds must be zero or greater")
    if args.trade_poll_attempts <= 0:
        raise SystemExit("--trade-poll-attempts must be greater than zero")
    if args.trade_poll_delay_seconds < 0:
        raise SystemExit("--trade-poll-delay-seconds must be zero or greater")
    if args.optimistic_close_delay_seconds < 0 or args.optimistic_close_delay_seconds > 2:
        raise SystemExit("--optimistic-close-delay-seconds must be >= 0 and <= 2")


def _build_signer(*, api_endpoint: str, account_index: int, api_key_index: int, api_private_key: str) -> Any:
    from lighter.signer_client import SignerClient

    return SignerClient(
        url=api_endpoint,
        account_index=account_index,
        api_private_keys={api_key_index: api_private_key},
    )


def _build_and_print_plan(
    api_endpoint: str,
    args: argparse.Namespace,
    label: str,
    *,
    close_size: Decimal | None = None,
) -> TinyOrderPlan | None:
    try:
        metadata = _load_market_metadata(
            api_endpoint=api_endpoint,
            market_id=args.market_id,
            symbol=args.symbol,
            timeout_seconds=args.timeout_seconds,
        )
        orderbook = fetch_lighter_rest_top_of_book(
            api_endpoint,
            market_id=args.market_id,
            market=args.symbol,
            timeout_seconds=args.timeout_seconds,
            limit=args.orderbook_limit,
        )
    except (TimeoutError, OSError, ValueError, LighterReadonlyConfigError) as exc:
        print(f"{label}_market_preflight_ok=False")
        print(f"{label}_market_preflight_error={exc.__class__.__name__}")
        return None

    if not orderbook.ok or orderbook.snapshot is None:
        print(f"{label}_market_preflight_ok=False")
        print(f"{label}_market_preflight_reason={orderbook.reason}")
        return None

    try:
        plan = _plan_from_market_data(metadata, orderbook.snapshot, args, close_size=close_size)
    except ValueError as exc:
        print(f"{label}_market_preflight_ok=False")
        print(f"{label}_market_preflight_reason={exc}")
        return None

    print(f"{label}_market_preflight_ok={plan.eligible}")
    print(f"{label}_symbol={plan.symbol}")
    print(f"{label}_market_type={metadata.market_type or 'unknown'}")
    print(f"{label}_size_decimals={plan.size_decimals}")
    print(f"{label}_price_decimals={plan.price_decimals}")
    print(f"{label}_min_base_amount={fmt_decimal(plan.min_base_amount)}")
    print(f"{label}_min_quote_amount={fmt_decimal(plan.min_quote_amount)}")
    print(f"{label}_maker_fee_percent={_fmt_optional_decimal(plan.maker_fee_percent)}")
    print(f"{label}_taker_fee_percent={_fmt_optional_decimal(plan.taker_fee_percent)}")
    print(f"{label}_best_bid={fmt_decimal(plan.best_bid)}")
    print(f"{label}_best_ask={fmt_decimal(plan.best_ask)}")
    print(f"{label}_spread_bps={plan.spread_bps:.4f}")
    print(f"{label}_planned_size={fmt_decimal(plan.planned_size)}")
    print(f"{label}_planned_base_amount_raw={plan.planned_base_amount}")
    print(f"{label}_planned_one_side_notional_usd={plan.planned_one_side_notional_usd:.4f}")
    print(f"{label}_aggressive_buy_price={fmt_decimal(plan.aggressive_buy_price)}")
    print(f"{label}_aggressive_sell_price={fmt_decimal(plan.aggressive_sell_price)}")
    print(f"{label}_market_preflight_reason={plan.reason}")
    return plan


def _load_market_metadata(
    *,
    api_endpoint: str,
    market_id: int,
    symbol: str,
    timeout_seconds: float,
) -> LighterMarketMetadata:
    payload = read_only_get_json(
        api_endpoint,
        "/api/v1/orderBooks",
        {"filter": "perp", "market_id": market_id},
        timeout_seconds,
    )
    by_market = lighter_market_metadata_from_order_books(payload)
    if str(market_id) in by_market:
        return by_market[str(market_id)]
    for metadata in by_market.values():
        if metadata.symbol.upper() == symbol.upper():
            return metadata
    raise ValueError(f"Lighter market metadata not found for market_id={market_id} symbol={symbol}")


def _plan_from_market_data(
    metadata: LighterMarketMetadata,
    top_of_book: Any,
    args: argparse.Namespace,
    *,
    close_size: Decimal | None,
) -> TinyOrderPlan:
    if metadata.size_decimals is None:
        raise ValueError("missing_size_decimals")
    if metadata.price_decimals is None:
        raise ValueError("missing_price_decimals")
    size_decimals = metadata.size_decimals
    price_decimals = metadata.price_decimals
    lot_size = Decimal("1").scaleb(-size_decimals)
    min_base = metadata.min_base_amount or Decimal("0")
    min_quote = metadata.min_quote_amount or Decimal("0")

    if close_size is None:
        requested_size = args.quote_amount_usd / top_of_book.best_ask
        planned_size = _round_up_to_step(max(requested_size, min_base), lot_size)
    else:
        planned_size = _round_down_to_step(close_size, lot_size)

    notional = planned_size * top_of_book.best_ask
    mid = (top_of_book.best_bid + top_of_book.best_ask) / Decimal("2")
    spread_bps = ((top_of_book.best_ask - top_of_book.best_bid) / mid) * Decimal("10000") if mid > 0 else Decimal("999999")
    slippage_fraction = args.slippage_bps / Decimal("10000")
    aggressive_buy_price = _round_up_to_step(
        top_of_book.best_ask * (Decimal("1") + slippage_fraction),
        Decimal("1").scaleb(-price_decimals),
    )
    aggressive_sell_price = _round_down_to_step(
        top_of_book.best_bid * (Decimal("1") - slippage_fraction),
        Decimal("1").scaleb(-price_decimals),
    )
    planned_base_amount = _decimal_to_raw(planned_size, size_decimals, ROUND_CEILING)
    aggressive_buy_price_raw = _decimal_to_raw(aggressive_buy_price, price_decimals, ROUND_CEILING)
    aggressive_sell_price_raw = _decimal_to_raw(aggressive_sell_price, price_decimals, ROUND_FLOOR)

    eligible = True
    reason = "ok"
    if planned_size <= 0 or planned_base_amount <= 0:
        eligible = False
        reason = "size_rounded_to_zero"
    elif min_base > 0 and planned_size < min_base:
        eligible = False
        reason = "below_min_base_amount"
    elif min_quote > 0 and notional < min_quote:
        eligible = False
        reason = "below_min_quote_amount"
    elif notional > args.max_notional_usd:
        eligible = False
        reason = "notional_above_cap"
    elif spread_bps > args.max_spread_bps:
        eligible = False
        reason = "spread_above_cap"
    elif close_size is None and planned_size > top_of_book.best_ask_size:
        eligible = False
        reason = "ask_top_level_size_too_small"
    elif close_size is not None and planned_size > top_of_book.best_bid_size:
        eligible = False
        reason = "bid_top_level_size_too_small"

    return TinyOrderPlan(
        market_id=metadata.market_id,
        symbol=metadata.symbol,
        best_bid=top_of_book.best_bid,
        best_ask=top_of_book.best_ask,
        best_bid_size=top_of_book.best_bid_size,
        best_ask_size=top_of_book.best_ask_size,
        spread_bps=spread_bps,
        size_decimals=size_decimals,
        price_decimals=price_decimals,
        min_base_amount=min_base,
        min_quote_amount=min_quote,
        planned_size=planned_size,
        planned_base_amount=planned_base_amount,
        planned_one_side_notional_usd=notional,
        aggressive_buy_price=aggressive_buy_price,
        aggressive_sell_price=aggressive_sell_price,
        aggressive_buy_price_raw=aggressive_buy_price_raw,
        aggressive_sell_price_raw=aggressive_sell_price_raw,
        taker_fee_percent=metadata.taker_fee_percent,
        maker_fee_percent=metadata.maker_fee_percent,
        eligible=eligible,
        reason=reason,
    )


def _load_and_print_private_state(
    *,
    api_endpoint: str,
    account_index: int,
    auth_token: str,
    market_id: int,
    symbol: str,
    timeout_seconds: float,
    label: str,
) -> PrivateState:
    try:
        account = read_only_get_json(
            api_endpoint,
            "/api/v1/account",
            {"by": "index", "value": str(account_index), "active_only": True},
            timeout_seconds,
            private_readonly=True,
        )
        positions = _walk_position_objects(account)
        selected_position_amount = _selected_position_amount(positions, market_id, symbol)
        open_orders = _load_active_orders(api_endpoint, account_index, auth_token, market_id, timeout_seconds)
    except Exception as exc:
        print(f"{label}_private_state_ok=False")
        print(f"{label}_private_state_error={exc.__class__.__name__}")
        return PrivateState(False, 0, Decimal("0"), 0, exc.__class__.__name__)

    position_count = sum(1 for item in positions if _signed_position_amount(item) != 0)
    open_order_count = len(open_orders)
    print(f"{label}_private_state_ok=True")
    print(f"{label}_position_count={position_count}")
    print(f"{label}_selected_market_position_amount={fmt_decimal(selected_position_amount)}")
    print(f"{label}_open_order_count={open_order_count}")
    return PrivateState(True, position_count, selected_position_amount, open_order_count, "ok")


def _start_state_is_safe(state: PrivateState, args: argparse.Namespace) -> bool:
    if not state.account_ok:
        return False
    if state.position_count and not args.allow_existing_position:
        print("reason=existing_positions_detected")
        return False
    if state.open_order_count:
        print("reason=existing_open_orders_detected")
        return False
    return True


def _load_active_orders(
    api_endpoint: str,
    account_index: int,
    auth_token: str,
    market_id: int,
    timeout_seconds: float,
) -> list[dict[str, object]]:
    try:
        payload = read_only_get_json(
            api_endpoint,
            "/api/v1/accountActiveOrders",
            {"account_index": account_index, "market_type": "all"},
            timeout_seconds,
            private_readonly=True,
            read_only_auth_token=auth_token,
        )
    except Exception:
        payload = read_only_get_json(
            api_endpoint,
            "/api/v1/accountActiveOrders",
            {"account_index": account_index, "market_id": market_id, "market_type": "perp"},
            timeout_seconds,
            private_readonly=True,
            read_only_auth_token=auth_token,
        )
    return _walk_order_objects(payload)


def _print_active_orders(
    *,
    api_endpoint: str,
    account_index: int,
    auth_token: str,
    market_id: int,
    timeout_seconds: float,
    label: str,
) -> None:
    try:
        open_orders = _load_active_orders(api_endpoint, account_index, auth_token, market_id, timeout_seconds)
    except Exception as exc:
        print(f"{label}_active_orders_ok=False")
        print(f"{label}_active_orders_error={exc.__class__.__name__}")
        return
    print(f"{label}_active_orders_ok=True")
    print(f"{label}_open_order_count={len(open_orders)}")


def _poll_selected_position(
    *,
    api_endpoint: str,
    account_index: int,
    auth_token: str,
    market_id: int,
    symbol: str,
    timeout_seconds: float,
    attempts: int,
    delay_seconds: float,
    label: str,
    want_flat: bool = False,
) -> Decimal:
    amount = Decimal("0")
    for attempt in range(1, attempts + 1):
        state = _load_and_print_private_state(
            api_endpoint=api_endpoint,
            account_index=account_index,
            auth_token=auth_token,
            market_id=market_id,
            symbol=symbol,
            timeout_seconds=timeout_seconds,
            label=f"{label}_{attempt}",
        )
        amount = state.selected_position_amount
        if want_flat and state.account_ok and amount == 0:
            return amount
        if not want_flat and amount != 0:
            return amount
        if attempt < attempts and delay_seconds:
            time.sleep(delay_seconds)
    return amount


def _poll_and_print_trade_summary(
    *,
    api_endpoint: str,
    account_index: int,
    auth_token: str,
    market_id: int,
    client_order_indexes: set[int],
    timeout_seconds: float,
    attempts: int,
    delay_seconds: float,
) -> dict[str, str | int]:
    summary: dict[str, str | int] = {
        "matched_trade_count": 0,
        "matched_trade_gross_usd": "0",
        "matched_trade_fee_usd_estimate": "0",
    }
    for attempt in range(1, attempts + 1):
        try:
            trades_payload = read_only_get_json(
                api_endpoint,
                "/api/v1/trades",
                {
                    "account_index": account_index,
                    "market_id": market_id,
                    "market_type": "perp",
                    "sort_by": "timestamp",
                    "sort_dir": "desc",
                    "limit": 20,
                },
                timeout_seconds,
                private_readonly=True,
                read_only_auth_token=auth_token,
            )
        except Exception as exc:
            print(f"trade_summary_ok=False")
            print(f"trade_summary_error={exc.__class__.__name__}")
            return summary

        matched = [
            trade for trade in _walk_trade_objects(trades_payload) if _trade_matches_client_ids(trade, client_order_indexes)
        ]
        gross_usd = sum((_decimal_field(trade, "usd_amount") for trade in matched), Decimal("0"))
        fee_usd = sum((_account_fee_usd_estimate(trade, account_index) for trade in matched), Decimal("0"))
        print(f"trade_summary_attempt={attempt}")
        print(f"matched_trade_count={len(matched)}")
        print(f"matched_trade_gross_usd={gross_usd:.6f}")
        print(f"matched_trade_fee_usd_estimate={fee_usd:.6f}")
        summary = {
            "matched_trade_count": len(matched),
            "matched_trade_gross_usd": f"{gross_usd:.6f}",
            "matched_trade_fee_usd_estimate": f"{fee_usd:.6f}",
        }
        if len(matched) >= 2:
            return summary
        if attempt < attempts and delay_seconds:
            time.sleep(delay_seconds)
    return summary


def _send_tx_summary(response: object, error: object, elapsed_ms: float) -> SendTxSummary:
    code = _object_attr(response, "code")
    return SendTxSummary(
        ok=code == 200 and not error,
        code=int(code) if code is not None else None,
        message_present=bool(_object_attr(response, "message")),
        tx_hash_present=bool(_object_attr(response, "tx_hash")),
        predicted_execution_time_ms=_optional_int(_object_attr(response, "predicted_execution_time_ms")),
        volume_quota_remaining=_optional_int(_object_attr(response, "volume_quota_remaining")),
        error_present=bool(error),
        elapsed_ms=_decimal_ms(elapsed_ms),
    )


def _print_send_tx_summary(label: str, summary: SendTxSummary, elapsed_ms: float) -> None:
    print(f"{label}_send_tx_ok={summary.ok}")
    print(f"{label}_send_tx_code={summary.code}")
    print(f"{label}_message_present={summary.message_present}")
    print(f"{label}_tx_hash_present={summary.tx_hash_present}")
    print(f"{label}_predicted_execution_time_ms={summary.predicted_execution_time_ms}")
    print(f"{label}_volume_quota_remaining={summary.volume_quota_remaining}")
    print(f"{label}_error_present={summary.error_present}")
    print(f"{label}_elapsed_ms={elapsed_ms:.2f}")


def _elapsed_decimal_from_perf(start: float) -> Decimal:
    return _decimal_ms((time.perf_counter() - start) * 1000)


def _decimal_ms(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


def _optional_decimal_value(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _walk_position_objects(payload: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    _walk_objects_with_keys(payload, {"position", "position_value", "avg_entry_price"}, result)
    return result


def _walk_order_objects(payload: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    _walk_objects_with_keys(payload, {"order_index", "market_index", "remaining_base_amount"}, result)
    return result


def _walk_trade_objects(payload: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    _walk_objects_with_keys(payload, {"trade_id", "market_id", "usd_amount"}, result)
    return result


def _walk_objects_with_keys(payload: object, keys: set[str], result: list[dict[str, object]]) -> None:
    if isinstance(payload, dict):
        if keys <= set(str(key) for key in payload.keys()):
            result.append(payload)
            return
        for value in payload.values():
            _walk_objects_with_keys(value, keys, result)
    elif isinstance(payload, list):
        for item in payload:
            _walk_objects_with_keys(item, keys, result)


def _selected_position_amount(positions: list[dict[str, object]], market_id: int, symbol: str) -> Decimal:
    amount = Decimal("0")
    for position in positions:
        raw_market_id = position.get("market_id")
        raw_symbol = str(position.get("symbol") or "")
        if raw_market_id is not None and str(raw_market_id) == str(market_id):
            amount += _signed_position_amount(position)
        elif raw_symbol.upper() == symbol.upper():
            amount += _signed_position_amount(position)
    return amount


def _signed_position_amount(position: dict[str, object]) -> Decimal:
    amount = Decimal(str(position.get("position", "0")))
    sign = position.get("sign")
    if sign is not None:
        sign_number = int(str(sign))
        if sign_number < 0:
            return -abs(amount)
        if sign_number > 0:
            return abs(amount)
    return amount


def _trade_matches_client_ids(trade: dict[str, object], client_order_indexes: set[int]) -> bool:
    for key in ("ask_client_id", "bid_client_id"):
        value = trade.get(key)
        if value is None or value == "":
            continue
        try:
            if int(str(value)) in client_order_indexes:
                return True
        except ValueError:
            continue
    return False


def _account_fee_usd_estimate(trade: dict[str, object], account_index: int) -> Decimal:
    ask_account_id = _optional_int(trade.get("ask_account_id"))
    bid_account_id = _optional_int(trade.get("bid_account_id"))
    is_maker_ask = bool(trade.get("is_maker_ask"))
    account_is_ask = ask_account_id == account_index
    account_is_bid = bid_account_id == account_index
    account_is_taker = (account_is_ask and not is_maker_ask) or (account_is_bid and is_maker_ask)
    fee_field = "taker_fee" if account_is_taker else "maker_fee"
    raw_fee = _optional_int(trade.get(fee_field)) or 0
    return Decimal(raw_fee) / Decimal("1000000")


def _required_int(value: str, name: str) -> int:
    if not value:
        raise ValueError(f"missing {name}")
    return int(value)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(str(value))


def _object_attr(value: object, name: str) -> object:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _decimal_field(payload: dict[str, object], name: str) -> Decimal:
    value = payload.get(name)
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _decimal_to_raw(value: Decimal, decimals: int, rounding: str) -> int:
    return int((value * (Decimal(10) ** decimals)).to_integral_value(rounding=rounding))


def fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _fmt_optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return fmt_decimal(value)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
