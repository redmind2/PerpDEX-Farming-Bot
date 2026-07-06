from __future__ import annotations

import argparse
import sys

from perpdex_farming_bot.connectors.pacifica_readonly import (
    PacificaReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    default_wss_endpoint,
    endpoint_from_env,
    normalize_pacifica_environment,
    read_only_get,
    validate_https_base_url,
    validate_wss_url,
    wss_endpoint_env_name,
)
from perpdex_farming_bot.credentials import (
    pacifica_available_private_readonly_env,
    pacifica_credential_env,
    read_pacifica_private_readonly_params,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and optionally run Pacifica read-only smoke checks. This never sends orders.",
    )
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="PACIFICA",
        help="Credential prefix/account id. Case-insensitive, e.g. pacifica_1 -> PACIFICA_1.",
    )
    parser.add_argument("--environment", default="testnet", help="Pacifica environment: testnet or production/mainnet.")
    parser.add_argument("--network", action="store_true", help="Actually call Pacifica read-only REST endpoints.")
    parser.add_argument("--public", action="store_true", help="Run public market-data checks.")
    parser.add_argument("--private-readonly", action="store_true", help="Run account-specific read-only checks.")
    parser.add_argument(
        "--public-check",
        choices=("markets", "orderbook", "prices"),
        default="markets",
        help="Public read-only endpoint to call when --public is used.",
    )
    parser.add_argument(
        "--private-check",
        choices=("account", "positions"),
        default="positions",
        help="Private read-only endpoint to call when --private-readonly is used.",
    )
    parser.add_argument("--symbol", default="BTC", help="Pacifica case-sensitive market symbol, e.g. BTC.")
    parser.add_argument("--agg-level", type=int, default=1, help="Orderbook aggregation level.")
    parser.add_argument("--timeout-seconds", type=float, default=5.0, help="Timeout for each read-only request.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_pacifica_environment(args.environment)
    credential_env = pacifica_credential_env(args.credential_prefix, environment)
    available_env = pacifica_available_private_readonly_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    wss_name = wss_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    wss_endpoint = endpoint_from_env(get_env(wss_name), default_wss_endpoint(environment))

    print("pacifica_readonly_smoke=prepared")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print("sdk_repository=https://github.com/pacifica-fi/python-sdk")
    print("sdk_language=python")
    print("sdk_official=True")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")
    print("http_method=GET")

    try:
        api_endpoint = validate_https_base_url(api_name, api_endpoint)
        wss_endpoint = validate_wss_url(wss_name, wss_endpoint)
        print(f"{api_name}={api_endpoint}")
        print(f"{wss_name}={wss_endpoint}")
    except PacificaReadonlyConfigError as exc:
        print("pacifica_readonly_smoke_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    print(f"primary_credential_prefix={credential_env.prefix}")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_agent_public_key}={masked_env_status(credential_env.api_agent_public_key)}")
    print(f"primary_{credential_env.api_agent_private_key}={masked_env_status(credential_env.api_agent_private_key)}")
    print(f"private_readonly_env_ready={available_env is not None}")

    if not args.network:
        print("network_skipped=pass_--network_to_call_pacifica_readonly_rest")
        print("pacifica_readonly_smoke_ready=True")
        return

    should_run_public = args.public or not args.private_readonly
    all_ok = True
    if should_run_public:
        result = _run_public_check(api_endpoint, args)
        all_ok = all_ok and result

    if args.private_readonly:
        if available_env is None:
            print("private_readonly_skipped=missing_account_address_env")
            print("pacifica_readonly_smoke_ready=False")
            raise SystemExit(2)
        result = _run_private_readonly_check(api_endpoint, args)
        all_ok = all_ok and result

    print(f"pacifica_readonly_smoke_ready={all_ok}")
    if not all_ok:
        raise SystemExit(1)


def _run_public_check(api_endpoint: str, args: argparse.Namespace) -> bool:
    if args.public_check == "markets":
        path = "/info"
        query: dict[str, object] = {}
    elif args.public_check == "orderbook":
        path = "/book"
        query = {"symbol": args.symbol, "agg_level": args.agg_level}
    elif args.public_check == "prices":
        path = "/prices"
        query = {}
    else:
        raise ValueError(f"unknown public check: {args.public_check}")

    result = read_only_get(api_endpoint, path, query, args.timeout_seconds)
    print(f"public_check={args.public_check}")
    print(f"public_get_url={result.safe_url}")
    print(f"public_get_ok={result.ok}")
    print(f"public_get_status_code={result.status_code}")
    print(f"public_get_content_type={result.content_type or 'unknown'}")
    print(f"public_get_body_shape={result.body_shape}")
    if result.error:
        print(f"public_get_error={result.error}")
    return result.ok


def _run_private_readonly_check(api_endpoint: str, args: argparse.Namespace) -> bool:
    params = read_pacifica_private_readonly_params(args.credential_prefix, args.environment)
    if args.private_check == "account":
        path = "/account"
        query: dict[str, object] = {"account": params["account"]}
    elif args.private_check == "positions":
        path = "/positions"
        query = {"account": params["account"]}
    else:
        raise ValueError(f"unknown private read-only check: {args.private_check}")

    result = read_only_get(
        api_endpoint,
        path,
        query,
        args.timeout_seconds,
        private_readonly=True,
    )
    print(f"private_readonly_check={args.private_check}")
    print(f"private_get_url={result.safe_url}")
    print(f"private_get_ok={result.ok}")
    print(f"private_get_status_code={result.status_code}")
    print(f"private_get_content_type={result.content_type or 'unknown'}")
    print(f"private_get_body_shape={result.body_shape}")
    if result.error:
        print(f"private_get_error={result.error}")
    return result.ok


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
