from __future__ import annotations

import argparse
import importlib.util
import sys
from decimal import Decimal
from urllib.error import HTTPError, URLError

from perpdex_farming_bot.cli.pacifica_live_common import (
    build_tiny_order_plan,
    fmt_decimal,
    load_pacifica_open_orders,
    load_pacifica_positions,
    nonzero_pacifica_positions,
)
from perpdex_farming_bot.connectors.pacifica_readonly import (
    PacificaReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_pacifica_environment,
    read_only_get,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    pacifica_available_private_readonly_env,
    pacifica_credential_env,
    pacifica_signing_missing,
    read_pacifica_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


MAX_FIRST_LIVE_NOTIONAL_USD = Decimal("25")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pacifica live-test preflight. Read-only by design: sends no orders and no cancels.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="PACIFICA",
        help="Credential prefix/account id. Secret values are never printed.",
    )
    parser.add_argument("--environment", default="testnet", help="Pacifica environment: testnet or production/mainnet.")
    parser.add_argument("--symbol", default="BTC", help="Pacifica case-sensitive market symbol, e.g. BTC.")
    parser.add_argument("--agg-level", type=int, default=1, help="Orderbook aggregation level for fresh verification.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-notional-usd", type=Decimal, default=Decimal("15"))
    parser.add_argument("--network", action="store_true", help="Actually call Pacifica public/private read-only REST.")
    parser.add_argument(
        "--allow-existing-position",
        action="store_true",
        help="Allow live-test planning when an existing position is present. Default blocks.",
    )
    args = parser.parse_args()

    if args.max_notional_usd <= 0:
        raise SystemExit("--max-notional-usd must be greater than zero")
    if args.max_notional_usd > MAX_FIRST_LIVE_NOTIONAL_USD:
        raise SystemExit(f"--max-notional-usd must be <= {MAX_FIRST_LIVE_NOTIONAL_USD} for the first Pacifica live test")

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_pacifica_environment(args.environment)
    credential_env = pacifica_credential_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("pacifica_live_preflight=read_only_no_orders")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"symbol={args.symbol}")
    print(f"max_notional_usd={args.max_notional_usd}")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("fresh_orderbook_verify=required_before_live_test")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except PacificaReadonlyConfigError as exc:
        print("preflight_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    available_env = pacifica_available_private_readonly_env(args.credential_prefix, environment)
    signing_missing = pacifica_signing_missing(args.credential_prefix, environment)
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_agent_public_key}={masked_env_status(credential_env.api_agent_public_key)}")
    print(f"primary_{credential_env.api_agent_private_key}={masked_env_status(credential_env.api_agent_private_key)}")
    print(f"private_readonly_env_ready={available_env is not None}")
    print(f"signing_env_ready={not signing_missing}")
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))
    print(f"base58_installed={importlib.util.find_spec('base58') is not None}")
    print(f"solders_installed={importlib.util.find_spec('solders') is not None}")

    if not args.network:
        print("network_skipped=pass_--network_to_run_pacifica_live_preflight")
        print("preflight_ready=False")
        return

    all_ok = True

    account = ""
    if available_env is None:
        all_ok = False
        print("private_readonly_skipped=missing_account_address_env")
    else:
        account = read_pacifica_private_readonly_params(args.credential_prefix, environment)["account"]
        account_ok = _print_private_account_check(api_endpoint, account, args.timeout_seconds)
        all_ok = all_ok and account_ok

    try:
        order_plan = build_tiny_order_plan(
            api_endpoint=api_endpoint,
            symbol=args.symbol,
            max_notional_usd=args.max_notional_usd,
            timeout_seconds=args.timeout_seconds,
            agg_level=args.agg_level,
        )
    except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
        print("market_preflight_ok=False")
        print(f"market_preflight_error={exc.__class__.__name__}")
        print("preflight_ready=False")
        return

    print(f"market_preflight_ok={order_plan.eligible}")
    print(f"best_bid={fmt_decimal(order_plan.best_bid)}")
    print(f"best_ask={fmt_decimal(order_plan.best_ask)}")
    print(f"spread_bps={order_plan.spread_bps:.4f}")
    print(f"lot_size={fmt_decimal(order_plan.lot_size)}")
    print(f"tick_size={fmt_decimal(order_plan.tick_size)}")
    print(f"min_order_size_usd={fmt_decimal(order_plan.min_order_size_usd)}")
    print(f"planned_amount={fmt_decimal(order_plan.amount)}")
    print(f"planned_one_side_notional_usd={order_plan.one_side_notional_usd:.4f}")
    print(f"planned_roundtrip_gross_notional_usd={order_plan.gross_roundtrip_notional_usd:.4f}")
    print(f"market_preflight_reason={order_plan.reason}")
    all_ok = all_ok and order_plan.eligible

    if account:
        try:
            positions = load_pacifica_positions(api_endpoint, account, args.timeout_seconds)
            nonzero_positions = nonzero_pacifica_positions(positions)
            selected_nonzero = nonzero_pacifica_positions(positions, args.symbol)
            open_orders, last_order_id = load_pacifica_open_orders(api_endpoint, account, args.timeout_seconds)
        except (HTTPError, TimeoutError, URLError, ValueError, KeyError, IndexError) as exc:
            print("private_state_ok=False")
            print(f"private_state_error={exc.__class__.__name__}")
            print("preflight_ready=False")
            return

        print("private_state_ok=True")
        print(f"position_count={len(nonzero_positions)}")
        print(f"selected_symbol_position_count={len(selected_nonzero)}")
        print(f"open_order_count={len(open_orders)}")
        if last_order_id:
            print(f"last_order_id={last_order_id}")

        if nonzero_positions and not args.allow_existing_position:
            all_ok = False
            print("position_check=blocked_existing_position")
        else:
            print("position_check=ok")

        if open_orders:
            all_ok = False
            print("open_order_check=blocked_existing_open_orders")
        else:
            print("open_order_check=ok")
    else:
        print("private_state_ok=False")
        print("position_check=skipped_missing_account")
        print("open_order_check=skipped_missing_account")

    dependencies_ready = importlib.util.find_spec("base58") is not None and importlib.util.find_spec("solders") is not None
    signing_ready = not signing_missing and dependencies_ready
    print(f"signing_runtime_ready={signing_ready}")
    all_ok = all_ok and signing_ready

    print("live_execution_ready=requires_pacifica_live_test_confirmation" if all_ok else "live_execution_ready=False")
    print(f"preflight_ready={all_ok}")


def _print_private_account_check(api_endpoint: str, account: str, timeout_seconds: float) -> bool:
    result = read_only_get(
        api_endpoint,
        "/account",
        {"account": account},
        timeout_seconds,
        private_readonly=True,
    )
    print(f"account_private_readonly_ok={result.ok}")
    print(f"account_private_readonly_status_code={result.status_code}")
    print(f"account_private_readonly_body_shape={result.body_shape}")
    if result.error:
        print(f"account_private_readonly_error={result.error}")
    return result.ok


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
