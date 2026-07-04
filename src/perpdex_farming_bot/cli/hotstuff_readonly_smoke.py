from __future__ import annotations

import argparse
import importlib.util
import sys

from perpdex_farming_bot.connectors.hotstuff_readonly import (
    HotstuffReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    default_wss_endpoint,
    endpoint_from_env,
    info_post,
    normalize_hotstuff_environment,
    validate_https_base_url,
    validate_wss_url,
    wss_endpoint_env_name,
)
from perpdex_farming_bot.credentials import (
    hotstuff_available_private_readonly_env,
    hotstuff_credential_env,
    read_hotstuff_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


PRIVATE_METHOD_ALIASES = {
    "account-summary": "accountSummary",
    "account-info": "accountInfo",
    "open-orders": "openOrders",
    "positions": "positions",
    "order-history": "orderHistory",
    "fills": "fills",
    "funding-history": "fundingHistory",
    "transfer-history": "transferHistory",
    "instrument-leverage": "instrumentLeverage",
    "all-agents": "allAgents",
    "referral-summary": "referralSummary",
    "user-fees": "userFees",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and optionally run Hotstuff read-only smoke checks. This never sends orders.",
    )
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HOTSTUFF",
        help="Credential prefix/account id. Case-insensitive, e.g. hotstuff_1 -> HOTSTUFF_1.",
    )
    parser.add_argument("--environment", default="production", help="Hotstuff environment: production/mainnet or testnet.")
    parser.add_argument("--network", action="store_true", help="Actually call Hotstuff /info.")
    parser.add_argument("--public", action="store_true", help="Run public market-data checks.")
    parser.add_argument("--private-readonly", action="store_true", help="Run private account read-only checks.")
    parser.add_argument(
        "--public-method",
        choices=("instruments", "ticker", "orderbook"),
        default="ticker",
        help="Public /info method to call when --public is used.",
    )
    parser.add_argument(
        "--private-method",
        choices=tuple(PRIVATE_METHOD_ALIASES.keys()),
        default="account-summary",
        help="Private read-only /info method to call when --private-readonly is used.",
    )
    parser.add_argument("--symbol", default="all", help="Symbol for ticker/orderbook, e.g. BTC-PERP. Ticker can use all.")
    parser.add_argument("--instrument-type", default="perps", help="Instrument type for instruments, usually perps.")
    parser.add_argument("--limit", type=int, default=10, help="Optional small page limit for history-like private reads.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0, help="Timeout for each /info request.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_hotstuff_environment(args.environment)
    credential_env = hotstuff_credential_env(args.credential_prefix, environment)
    available_env = hotstuff_available_private_readonly_env(args.credential_prefix, environment)
    sdk_installed = importlib.util.find_spec("hotstuff") is not None
    api_name = api_endpoint_env_name(environment)
    wss_name = wss_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    wss_endpoint = endpoint_from_env(get_env(wss_name), default_wss_endpoint(environment))

    print("hotstuff_readonly_smoke=prepared")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print(f"sdk_installed={sdk_installed}")
    print("sdk_package=hotstuff-python-sdk")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")
    print("http_endpoint=/info")
    print("http_method=POST")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        wss_endpoint = validate_wss_url(wss_name, wss_endpoint)
        print(f"{api_name}={api_endpoint}")
        print(f"{wss_name}={wss_endpoint}")
    except HotstuffReadonlyConfigError as exc:
        print("hotstuff_readonly_smoke_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    _print_credential_env_status(credential_env)
    print(f"private_readonly_env_ready={available_env is not None}")

    if not args.network:
        print("network_skipped=pass_--network_to_call_hotstuff_info")
        print("hotstuff_readonly_smoke_ready=True")
        return

    should_run_public = args.public or not args.private_readonly
    all_ok = True
    if should_run_public:
        public_params = _public_params(args.public_method, args.symbol, args.instrument_type)
        result = info_post(api_endpoint, args.public_method, public_params, args.timeout_seconds)
        all_ok = all_ok and result.ok
        print(f"public_info_method={args.public_method}")
        print(f"public_post_url={result.url}")
        print(f"public_post_ok={result.ok}")
        print(f"public_post_status_code={result.status_code}")
        print(f"public_post_content_type={result.content_type or 'unknown'}")
        print(f"public_post_body_shape={result.body_shape}")
        if result.error:
            print(f"public_post_error={result.error}")

    if args.private_readonly:
        if available_env is None:
            print("private_readonly_skipped=missing_account_address_env")
            print("hotstuff_readonly_smoke_ready=False")
            raise SystemExit(2)
        private_method = PRIVATE_METHOD_ALIASES[args.private_method]
        private_params = _private_params(args, private_method)
        result = info_post(
            api_endpoint,
            private_method,
            private_params,
            args.timeout_seconds,
            private_readonly=True,
        )
        all_ok = all_ok and result.ok
        print(f"private_info_method={private_method}")
        print(f"private_post_url={result.url}")
        print(f"private_post_ok={result.ok}")
        print(f"private_post_status_code={result.status_code}")
        print(f"private_post_content_type={result.content_type or 'unknown'}")
        print(f"private_post_body_shape={result.body_shape}")
        if result.error:
            print(f"private_post_error={result.error}")

    print(f"hotstuff_readonly_smoke_ready={all_ok}")
    if not all_ok:
        raise SystemExit(1)


def _public_params(method: str, symbol: str, instrument_type: str) -> dict[str, object]:
    if method == "instruments":
        return {"type": instrument_type}
    if method == "ticker":
        return {"symbol": symbol}
    if method == "orderbook":
        if symbol == "all":
            raise SystemExit("--symbol must be a concrete market, e.g. BTC-PERP, when --public-method orderbook")
        return {"symbol": symbol}
    raise ValueError(f"unknown public method: {method}")


def _private_params(args: argparse.Namespace, method: str) -> dict[str, object]:
    params: dict[str, object] = read_hotstuff_private_readonly_params(args.credential_prefix, args.environment)
    if method == "instrumentLeverage":
        if args.symbol == "all":
            raise SystemExit("--symbol must be a concrete market, e.g. BTC-PERP, for instrument-leverage")
        params["symbol"] = args.symbol
    if method in {"openOrders", "orderHistory", "fills"} and args.limit > 0:
        params["limit"] = args.limit
    return params


def _print_credential_env_status(credential_env: object) -> None:
    account_address = getattr(credential_env, "account_address")
    signer_address = getattr(credential_env, "signer_address")
    signer_private_key = getattr(credential_env, "signer_private_key")
    legacy_private_key = getattr(credential_env, "legacy_private_key")
    print(f"primary_credential_prefix={getattr(credential_env, 'prefix')}")
    print(f"primary_{account_address}={masked_env_status(account_address)}")
    print(f"primary_{signer_address}={masked_env_status(signer_address)}")
    print(f"primary_{signer_private_key}={masked_env_status(signer_private_key)}")
    print(f"legacy_{legacy_private_key}={masked_env_status(legacy_private_key)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
