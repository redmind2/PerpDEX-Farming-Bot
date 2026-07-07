from __future__ import annotations

import argparse
import sys

from perpdex_farming_bot.connectors.lighter_readonly import (
    LighterReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    default_wss_endpoint,
    endpoint_from_env,
    normalize_lighter_environment,
    read_only_get,
    validate_https_base_url,
    validate_wss_url,
    wss_endpoint_env_name,
)
from perpdex_farming_bot.credentials import (
    lighter_available_private_readonly_env,
    lighter_credential_env,
    read_lighter_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and optionally run Lighter read-only smoke checks. This never sends orders.",
    )
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="LIGHTER",
        help="Credential prefix/account id. Case-insensitive, e.g. lighter_1 -> LIGHTER_1.",
    )
    parser.add_argument("--environment", default="production", help="Lighter environment: production/mainnet or testnet.")
    parser.add_argument("--network", action="store_true", help="Actually call Lighter read-only REST endpoints.")
    parser.add_argument("--public", action="store_true", help="Run public market-data checks.")
    parser.add_argument("--private-readonly", action="store_true", help="Run account-specific read-only checks.")
    parser.add_argument(
        "--public-check",
        choices=("status", "height", "markets", "market-details", "orderbook", "recent-trades"),
        default="markets",
        help="Public read-only endpoint to call when --public is used.",
    )
    parser.add_argument(
        "--private-check",
        choices=("accounts-by-l1", "account", "metadata", "active-orders", "account-limits", "apikeys", "trades"),
        default="account",
        help="Private read-only endpoint to call when --private-readonly is used.",
    )
    parser.add_argument("--market-id", type=int, default=0, help="Lighter market id/index, e.g. 0.")
    parser.add_argument("--filter", default="perp", choices=("all", "spot", "perp"), help="Market filter.")
    parser.add_argument("--limit", type=int, default=5, help="Small read-only result limit.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0, help="Timeout for each read-only request.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_lighter_environment(args.environment)
    credential_env = lighter_credential_env(args.credential_prefix, environment)
    available_env = lighter_available_private_readonly_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    wss_name = wss_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    wss_endpoint = endpoint_from_env(get_env(wss_name), default_wss_endpoint(environment))

    print("lighter_readonly_smoke=prepared")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print("http_method=GET")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        wss_endpoint = validate_wss_url(wss_name, wss_endpoint)
        print(f"{api_name}={api_endpoint}")
        print(f"{wss_name}={wss_endpoint}")
    except LighterReadonlyConfigError as exc:
        print("lighter_readonly_smoke_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    print(f"primary_credential_prefix={credential_env.prefix}")
    print(f"primary_{credential_env.l1_address}={masked_env_status(credential_env.l1_address)}")
    print(f"primary_{credential_env.account_index}={masked_env_status(credential_env.account_index)}")
    print(f"primary_{credential_env.api_key_index}={masked_env_status(credential_env.api_key_index)}")
    print(f"primary_{credential_env.api_private_key}={masked_env_status(credential_env.api_private_key)}")
    print(f"optional_{credential_env.read_only_auth_token}={masked_env_status(credential_env.read_only_auth_token)}")
    print(f"private_readonly_env_ready={available_env is not None}")

    if not args.network:
        print("network_skipped=pass_--network_to_call_lighter_readonly_rest")
        print("lighter_readonly_smoke_ready=True")
        return

    should_run_public = args.public or not args.private_readonly
    all_ok = True
    if should_run_public:
        result = _run_public_check(api_endpoint, args)
        all_ok = all_ok and result

    if args.private_readonly:
        if available_env is None:
            print("private_readonly_skipped=missing_account_index_or_l1_address_env")
            print("lighter_readonly_smoke_ready=False")
            raise SystemExit(2)
        result = _run_private_readonly_check(api_endpoint, args)
        all_ok = all_ok and result

    print(f"lighter_readonly_smoke_ready={all_ok}")
    if not all_ok:
        raise SystemExit(1)


def _run_public_check(api_endpoint: str, args: argparse.Namespace) -> bool:
    if args.public_check == "status":
        path = "/"
        query: dict[str, object] = {}
    elif args.public_check == "height":
        path = "/api/v1/currentHeight"
        query = {}
    elif args.public_check == "markets":
        path = "/api/v1/orderBooks"
        query = {"filter": args.filter}
    elif args.public_check == "market-details":
        path = "/api/v1/orderBookDetails"
        query = {"market_id": args.market_id, "filter": args.filter}
    elif args.public_check == "orderbook":
        path = "/api/v1/orderBookOrders"
        query = {"market_id": args.market_id, "limit": args.limit}
    elif args.public_check == "recent-trades":
        path = "/api/v1/recentTrades"
        query = {"market_id": args.market_id, "limit": args.limit}
    else:
        raise ValueError(f"unknown public check: {args.public_check}")

    result = read_only_get(api_endpoint, path, query, args.timeout_seconds)
    print(f"public_check={args.public_check}")
    _print_result("public_get", result.safe_url, result.ok, result.status_code, result.content_type, result.body_shape, result.error)
    return result.ok


def _run_private_readonly_check(api_endpoint: str, args: argparse.Namespace) -> bool:
    params = read_lighter_private_readonly_params(args.credential_prefix, args.environment)
    account_index = params["account_index"]
    l1_address = params["l1_address"]
    auth_token = params["read_only_auth_token"]

    if args.private_check == "accounts-by-l1":
        if not l1_address:
            print("private_readonly_skipped=missing_l1_address_env")
            return False
        path = "/api/v1/accountsByL1Address"
        query: dict[str, object] = {"l1_address": l1_address}
        auth_required = False
    elif args.private_check in {"account", "metadata"}:
        path = "/api/v1/account" if args.private_check == "account" else "/api/v1/accountMetadata"
        if account_index:
            query = {"by": "index", "value": account_index, "active_only": True}
        elif l1_address:
            query = {"by": "l1_address", "value": l1_address, "active_only": True}
        else:
            print("private_readonly_skipped=missing_account_index_or_l1_address_env")
            return False
        if args.private_check == "metadata":
            query.pop("active_only", None)
        auth_required = False
    elif args.private_check == "active-orders":
        if not account_index:
            print("private_readonly_skipped=missing_account_index_env")
            return False
        path = "/api/v1/accountActiveOrders"
        query = {"account_index": account_index, "market_id": args.market_id, "market_type": args.filter}
        auth_required = True
    elif args.private_check == "account-limits":
        if not account_index:
            print("private_readonly_skipped=missing_account_index_env")
            return False
        path = "/api/v1/accountLimits"
        query = {"account_index": account_index}
        auth_required = True
    elif args.private_check == "apikeys":
        if not account_index:
            print("private_readonly_skipped=missing_account_index_env")
            return False
        path = "/api/v1/apikeys"
        query = {"account_index": account_index, "api_key_index": 255}
        auth_required = False
    elif args.private_check == "trades":
        if not account_index:
            print("private_readonly_skipped=missing_account_index_env")
            return False
        path = "/api/v1/trades"
        query = {
            "account_index": account_index,
            "market_id": args.market_id,
            "market_type": args.filter,
            "sort_by": "timestamp",
            "limit": args.limit,
        }
        auth_required = True
    else:
        raise ValueError(f"unknown private read-only check: {args.private_check}")

    if auth_required and not auth_token:
        print("private_readonly_skipped=missing_read_only_auth_token_env")
        return False

    result = read_only_get(
        api_endpoint,
        path,
        query,
        args.timeout_seconds,
        private_readonly=True,
        read_only_auth_token=auth_token,
    )
    print(f"private_readonly_check={args.private_check}")
    print(f"private_readonly_auth_header_present={bool(auth_token)}")
    _print_result(
        "private_get",
        result.safe_url,
        result.ok,
        result.status_code,
        result.content_type,
        result.body_shape,
        result.error,
    )
    return result.ok


def _print_result(
    prefix: str,
    safe_url: str,
    ok: bool,
    status_code: int | None,
    content_type: str,
    body_shape: str,
    error: str,
) -> None:
    print(f"{prefix}_url={safe_url}")
    print(f"{prefix}_ok={ok}")
    print(f"{prefix}_status_code={status_code}")
    print(f"{prefix}_content_type={content_type or 'unknown'}")
    print(f"{prefix}_body_shape={body_shape}")
    if error:
        print(f"{prefix}_error={error}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
