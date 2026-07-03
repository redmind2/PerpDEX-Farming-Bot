from __future__ import annotations

import argparse
import importlib.util
import os
import sys

from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    endpoint_from_env,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    hibachi_available_credential_env,
    hibachi_credential_env,
    hibachi_credential_env_candidates,
    read_hibachi_credentials,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Use the official hibachi-xyz SDK for safe read-only smoke tests.",
    )
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HIBACHI",
        help="Credential prefix/account id. Case-insensitive, e.g. hibachi_1_crypto -> HIBACHI_1_CRYPTO.",
    )
    parser.add_argument("--network", action="store_true", help="Actually call Hibachi through the SDK.")
    parser.add_argument("--public", action="store_true", help="Run public SDK checks.")
    parser.add_argument("--private-readonly", action="store_true", help="Run private read-only account checks.")
    parser.add_argument("--symbol", default="BTC/USDT-P", help="Hibachi SDK symbol, for example BTC/USDT-P.")
    parser.add_argument("--depth", type=int, default=5, help="Orderbook depth for public SDK smoke.")
    parser.add_argument(
        "--granularity",
        type=float,
        default=0.0,
        help="Orderbook granularity for public SDK smoke. Use 0 for auto.",
    )
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    credential_env = hibachi_credential_env(args.credential_prefix)
    available_env = hibachi_available_credential_env(args.credential_prefix)
    sdk_installed = importlib.util.find_spec("hibachi_xyz") is not None

    print("hibachi_sdk_smoke=read_only")
    print(f"env_file_loaded={env_loaded}")
    print(f"credential_prefix={credential_env.prefix}")
    if available_env is not None and available_env.prefix != credential_env.prefix:
        print(f"credential_prefix_status=usable_via_legacy:{available_env.prefix}")
    elif available_env is not None:
        print("credential_prefix_status=usable")
    else:
        print("credential_prefix_status=missing")
    print(f"sdk_installed={sdk_installed}")
    print("sdk_package=hibachi-xyz")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")

    api_endpoint = endpoint_from_env(
        get_env("HIBACHI_API_ENDPOINT_PRODUCTION"),
        DEFAULT_HIBACHI_API_ENDPOINT,
    )
    data_endpoint = endpoint_from_env(
        get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"),
        DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    )
    print(f"api_endpoint={validate_https_base_url('HIBACHI_API_ENDPOINT_PRODUCTION', api_endpoint)}")
    print(f"data_api_endpoint={validate_https_base_url('HIBACHI_DATA_API_ENDPOINT_PRODUCTION', data_endpoint)}")

    missing = _print_credential_env_status(args.credential_prefix)
    print(f"private_readonly_env_ready={available_env is not None}")
    if missing and available_env is None:
        print("missing_required=" + ",".join(missing))
    elif missing:
        print("primary_missing_required=" + ",".join(missing))

    if not args.network:
        print("network_skipped=pass_--network_to_call_official_sdk")
        print("hibachi_sdk_smoke_ready=True")
        return

    if not sdk_installed:
        print("hibachi_sdk_smoke_ready=False")
        print("install_required=python -m pip install hibachi-xyz")
        raise SystemExit(2)

    from hibachi_xyz import HibachiApiClient

    should_run_public = args.public or not args.private_readonly
    if should_run_public:
        _run_public_smoke(HibachiApiClient, args.symbol, args.depth, args.granularity)

    if args.private_readonly:
        if available_env is None:
            print("private_readonly_skipped=missing_required_env")
            raise SystemExit(2)
        _run_private_readonly_smoke(HibachiApiClient, api_endpoint, data_endpoint, credential_env.prefix)

    print("hibachi_sdk_smoke_ready=True")


def _run_public_smoke(api_client_type: object, symbol: str, depth: int, granularity: float) -> None:
    client = api_client_type()
    print("public_sdk_network=True")
    try:
        exchange_info = client.get_exchange_info()
        contracts = getattr(exchange_info, "futureContracts", ())
        status = getattr(exchange_info, "status", "unknown")
        print("public_exchange_info_ok=True")
        print(f"public_exchange_status={status}")
        print(f"public_contract_count={len(contracts)}")

        if granularity <= 0:
            granularity = _resolve_granularity(client, symbol)
        orderbook = client.get_orderbook(symbol, depth=depth, granularity=granularity)
        ask = orderbook.ask[0] if getattr(orderbook, "ask", None) else None
        bid = orderbook.bid[0] if getattr(orderbook, "bid", None) else None
        print("public_orderbook_ok=True")
        print(f"public_orderbook_symbol={symbol}")
        print(f"public_orderbook_granularity={granularity}")
        print(f"public_best_ask={getattr(ask, 'price', 'missing')}")
        print(f"public_best_bid={getattr(bid, 'price', 'missing')}")
    except Exception as exc:
        print("public_sdk_network=False")
        print(f"public_sdk_error_type={exc.__class__.__name__}")
        raise SystemExit(1) from exc


def _run_private_readonly_smoke(
    api_client_type: object,
    api_endpoint: str,
    data_endpoint: str,
    credential_prefix: str,
) -> None:
    credentials = read_hibachi_credentials(credential_prefix)
    client = api_client_type(
        api_url=api_endpoint,
        data_api_url=data_endpoint,
        api_key=credentials["api_key"],
        account_id=credentials["account_id"],
        private_key=credentials["private_key"],
    )
    print("private_readonly_network=True")
    try:
        account_info = client.get_account_info()
        capital_balance = client.get_capital_balance()
        print("private_account_info_ok=True")
        print(f"private_assets_count={len(getattr(account_info, 'assets', ()))}")
        print(f"private_positions_count={len(getattr(account_info, 'positions', ()))}")
        print(f"private_balance_present={bool(getattr(account_info, 'balance', ''))}")
        print(f"private_capital_balance_present={bool(getattr(capital_balance, 'balance', ''))}")
    except Exception as exc:
        print("private_readonly_network=False")
        print(f"private_readonly_error_type={exc.__class__.__name__}")
        raise SystemExit(1) from exc


def _resolve_granularity(client: object, symbol: str) -> float:
    inventory = client.get_inventory()
    for market in getattr(inventory, "markets", ()):
        contract = getattr(market, "contract", None)
        if contract is None:
            continue
        if getattr(contract, "symbol", "") != symbol:
            continue
        granularities = getattr(contract, "orderbookGranularities", ())
        if granularities:
            return float(granularities[0])
    return 0.1


def _print_credential_env_status(prefix: str) -> list[str]:
    primary = hibachi_credential_env(prefix)
    primary_missing: list[str] = []
    for candidate in hibachi_credential_env_candidates(prefix):
        label = "primary" if candidate.prefix == primary.prefix else "legacy"
        print(f"{label}_credential_prefix={candidate.prefix}")
        for name in candidate.required_names:
            status = masked_env_status(name)
            print(f"{label}_{name}={status}")
            if candidate.prefix == primary.prefix and status == "missing":
                primary_missing.append(name)
    return primary_missing


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted=True")
        sys.exit(130)
