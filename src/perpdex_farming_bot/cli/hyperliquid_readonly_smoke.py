from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from perpdex_farming_bot.connectors.hyperliquid_readonly import (
    HyperliquidReadonlyConfigError,
    api_endpoint_env_name,
    body_shape_from_payload,
    default_api_endpoint,
    endpoint_from_env,
    info_post,
    info_post_json,
    normalize_hyperliquid_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    hyperliquid_available_private_readonly_env,
    hyperliquid_credential_env,
    read_hyperliquid_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status
from perpdex_farming_bot.exchanges.hyperliquid_fees import load_hyperliquid_account_fee
from perpdex_farming_bot.marketdata.hyperliquid import fetch_hyperliquid_rest_top_of_book


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and optionally run Hyperliquid read-only smoke checks. This never sends orders.",
    )
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HYPERLIQUID",
        help="Credential prefix/account id. Case-insensitive, e.g. hyperliquid_1 -> HYPERLIQUID_1.",
    )
    parser.add_argument("--environment", default="production", help="Hyperliquid environment: production/mainnet.")
    parser.add_argument("--network", action="store_true", help="Actually call Hyperliquid read-only /info endpoints.")
    parser.add_argument("--public", action="store_true", help="Run public market-data checks.")
    parser.add_argument("--private-readonly", action="store_true", help="Run account-specific read-only checks.")
    parser.add_argument(
        "--public-check",
        choices=("all-mids", "meta", "l2-book"),
        default="all-mids",
        help="Public read-only /info request to call when --public is used.",
    )
    parser.add_argument(
        "--private-check",
        choices=("state", "open-orders", "user-fills", "user-fees"),
        default="state",
        help="Private read-only /info request to call when --private-readonly is used.",
    )
    parser.add_argument("--coin", default="BTC", help="HyperCore perp coin name, e.g. BTC.")
    parser.add_argument("--dex", default="", help="Optional Hyperliquid perp dex name. Empty means the default perp dex.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0, help="Timeout for each read-only request.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_hyperliquid_environment(args.environment)
    credential_env = hyperliquid_credential_env(args.credential_prefix, environment)
    available_env = hyperliquid_available_private_readonly_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("hyperliquid_readonly_smoke=prepared")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"coin={args.coin}")
    print(f"dex={args.dex or 'default'}")
    print("sdk_package=hyperliquid-python-sdk")
    print("sdk_official=True")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")
    print("http_method=POST")
    print("http_path=/info")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        print(f"{api_name}={api_endpoint}")
    except HyperliquidReadonlyConfigError as exc:
        print("hyperliquid_readonly_smoke_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    print(f"primary_credential_prefix={credential_env.prefix}")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_wallet_address}={masked_env_status(credential_env.api_wallet_address)}")
    print(f"primary_{credential_env.api_wallet_private_key}={masked_env_status(credential_env.api_wallet_private_key)}")
    print(f"optional_{credential_env.vault_address}={masked_env_status(credential_env.vault_address)}")
    print(f"private_readonly_env_ready={available_env is not None}")

    if not args.network:
        print("network_skipped=pass_--network_to_call_hyperliquid_readonly_info")
        print("hyperliquid_readonly_smoke_ready=True")
        return

    should_run_public = args.public or not args.private_readonly
    all_ok = True
    if should_run_public:
        all_ok = _run_public_check(api_endpoint, args) and all_ok
    if args.private_readonly:
        if available_env is None:
            print("private_readonly_skipped=missing_account_address_env")
            print("hyperliquid_readonly_smoke_ready=False")
            raise SystemExit(2)
        all_ok = _run_private_readonly_check(api_endpoint, args) and all_ok

    print(f"hyperliquid_readonly_smoke_ready={all_ok}")
    if not all_ok:
        raise SystemExit(1)


def _run_public_check(api_endpoint: str, args: argparse.Namespace) -> bool:
    if args.public_check == "all-mids":
        body: dict[str, object] = {"type": "allMids"}
        if args.dex:
            body["dex"] = args.dex
        result = info_post(api_endpoint, body, args.timeout_seconds)
        print("public_check=all-mids")
        _print_result("public_post", result)
        return result.ok

    if args.public_check == "meta":
        body = {"type": "meta"}
        if args.dex:
            body["dex"] = args.dex
        try:
            payload = info_post_json(api_endpoint, body, args.timeout_seconds)
        except (TimeoutError, OSError, ValueError, HyperliquidReadonlyConfigError) as exc:
            print("public_check=meta")
            print("public_post_ok=False")
            print(f"public_post_error={exc.__class__.__name__}")
            return False
        print("public_check=meta")
        print("public_post_ok=True")
        print(f"public_post_body_shape={body_shape_from_payload(payload)}")
        print(f"meta_market_count={_meta_market_count(payload)}")
        return True

    if args.public_check == "l2-book":
        result = fetch_hyperliquid_rest_top_of_book(
            api_endpoint,
            coin=args.coin,
            timeout_seconds=args.timeout_seconds,
            dex=args.dex,
        )
        print("public_check=l2-book")
        print(f"public_post_ok={result.ok}")
        print(f"public_post_reason={result.reason}")
        if result.snapshot is not None:
            snapshot = result.snapshot
            print(f"best_bid={_fmt_decimal(snapshot.best_bid)}")
            print(f"best_ask={_fmt_decimal(snapshot.best_ask)}")
            print(f"best_bid_size={_fmt_decimal(snapshot.best_bid_size)}")
            print(f"best_ask_size={_fmt_decimal(snapshot.best_ask_size)}")
            print(f"spread_bps={snapshot.spread_bps:.4f}")
        return result.ok

    raise ValueError(f"unknown public check: {args.public_check}")


def _run_private_readonly_check(api_endpoint: str, args: argparse.Namespace) -> bool:
    params = read_hyperliquid_private_readonly_params(args.credential_prefix, args.environment)
    user = params["user"]
    if args.private_check == "state":
        body: dict[str, object] = {"type": "clearinghouseState", "user": user}
        if args.dex:
            body["dex"] = args.dex
        try:
            payload = info_post_json(api_endpoint, body, args.timeout_seconds, private_readonly=True)
        except (TimeoutError, OSError, ValueError, HyperliquidReadonlyConfigError) as exc:
            print("private_readonly_check=state")
            print("private_post_ok=False")
            print(f"private_post_error={exc.__class__.__name__}")
            return False
        print("private_readonly_check=state")
        print("private_post_ok=True")
        print(f"private_post_body_shape={body_shape_from_payload(payload)}")
        print(f"position_count={_position_count(payload)}")
        return True

    if args.private_check == "open-orders":
        body = {"type": "openOrders", "user": user}
        if args.dex:
            body["dex"] = args.dex
        result = info_post(api_endpoint, body, args.timeout_seconds, private_readonly=True)
        print("private_readonly_check=open-orders")
        _print_result("private_post", result)
        return result.ok

    if args.private_check == "user-fills":
        result = info_post(
            api_endpoint,
            {"type": "userFills", "user": user},
            args.timeout_seconds,
            private_readonly=True,
        )
        print("private_readonly_check=user-fills")
        _print_result("private_post", result)
        return result.ok

    if args.private_check == "user-fees":
        try:
            fee = load_hyperliquid_account_fee(
                api_endpoint,
                user,
                args.timeout_seconds,
            )
        except (TimeoutError, OSError, ValueError, ImportError, HyperliquidReadonlyConfigError) as exc:
            print("private_readonly_check=user-fees")
            print("private_post_ok=False")
            print(f"private_post_error={exc.__class__.__name__}")
            return False
        print("private_readonly_check=user-fees")
        print("private_post_ok=True")
        print(f"account_fee_level={fee.fee_level or 'unknown'}")
        print(f"account_maker_fee_bps={fee.maker_fee_bps}")
        print(f"account_taker_fee_bps={fee.taker_fee_bps}")
        return True

    raise ValueError(f"unknown private read-only check: {args.private_check}")


def _print_result(prefix: str, result: object) -> None:
    print(f"{prefix}_url={getattr(result, 'url')}")
    print(f"{prefix}_ok={getattr(result, 'ok')}")
    print(f"{prefix}_status_code={getattr(result, 'status_code')}")
    print(f"{prefix}_content_type={getattr(result, 'content_type') or 'unknown'}")
    print(f"{prefix}_body_shape={getattr(result, 'body_shape')}")
    if getattr(result, "error"):
        print(f"{prefix}_error={getattr(result, 'error')}")


def _meta_market_count(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    universe = payload.get("universe")
    if not isinstance(universe, list):
        return 0
    return len(universe)


def _position_count(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("assetPositions")
    if not isinstance(raw, list):
        return 0
    count = 0
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("position"), dict):
            continue
        size = Decimal(str(item["position"].get("szi", "0")))
        if size != 0:
            count += 1
    return count


def _fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
