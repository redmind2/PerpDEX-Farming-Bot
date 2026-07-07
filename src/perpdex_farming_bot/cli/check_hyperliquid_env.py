from __future__ import annotations

import argparse
import importlib.util

from perpdex_farming_bot.connectors.hyperliquid_readonly import (
    HyperliquidReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    endpoint_from_env,
    normalize_hyperliquid_environment,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    hyperliquid_available_private_readonly_env,
    hyperliquid_credential_env,
    hyperliquid_private_readonly_missing,
    hyperliquid_signing_missing,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Hyperliquid local env values without printing secrets.")
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HYPERLIQUID",
        help="Credential prefix/account id. Case-insensitive, e.g. hyperliquid_1 -> HYPERLIQUID_1.",
    )
    parser.add_argument("--environment", default="production", help="Hyperliquid environment: production/mainnet.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_hyperliquid_environment(args.environment)
    credential_env = hyperliquid_credential_env(args.credential_prefix, environment)
    available_env = hyperliquid_available_private_readonly_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))

    print("hyperliquid_env_check=local_only_no_network")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print("api_docs=https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api")
    print("sdk_package=hyperliquid-python-sdk")
    print("sdk_official=True")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")
    print("query_address_rule=use_master_or_subaccount_address_not_api_wallet_address")

    endpoint_errors: list[str] = []
    try:
        print(f"{api_name}={validate_https_base_url(api_name, api_endpoint)}")
    except HyperliquidReadonlyConfigError as exc:
        print(f"{api_name}=invalid")
        print(f"{api_name}_error={exc}")
        endpoint_errors.append(api_name)

    print(f"primary_credential_prefix={credential_env.prefix}")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.api_wallet_address}={masked_env_status(credential_env.api_wallet_address)}")
    print(f"primary_{credential_env.api_wallet_private_key}={masked_env_status(credential_env.api_wallet_private_key)}")
    print(f"optional_{credential_env.vault_address}={masked_env_status(credential_env.vault_address)}")

    private_missing = hyperliquid_private_readonly_missing(args.credential_prefix, environment)
    signing_missing = hyperliquid_signing_missing(args.credential_prefix, environment)
    print("hyperliquid_public_readonly_env_ready=True")
    print(f"hyperliquid_private_readonly_env_ready={available_env is not None}")
    print(f"hyperliquid_signing_env_ready={not signing_missing}")
    if private_missing:
        print("private_readonly_missing_required=" + ",".join(private_missing))
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))

    print(f"hyperliquid_python_sdk_installed={importlib.util.find_spec('hyperliquid') is not None}")
    print(f"eth_account_installed={importlib.util.find_spec('eth_account') is not None}")
    key_parse_status, key_matches_signer = _check_api_wallet_private_key(
        api_wallet_address=get_env(credential_env.api_wallet_address),
        api_wallet_private_key=get_env(credential_env.api_wallet_private_key),
    )
    print(f"api_wallet_private_key_parse={key_parse_status}")
    print(f"api_wallet_address_matches_private_key={key_matches_signer}")

    print(f"hyperliquid_env_ready={not endpoint_errors and available_env is not None}")
    if endpoint_errors:
        print("invalid_endpoints=" + ",".join(endpoint_errors))


def _check_api_wallet_private_key(*, api_wallet_address: str, api_wallet_private_key: str) -> tuple[str, str]:
    if not api_wallet_private_key:
        return "skipped_missing", "skipped"
    try:
        from eth_account import Account

        derived = Account.from_key(api_wallet_private_key).address
    except Exception:
        return "error", "skipped"

    if not api_wallet_address:
        return "ok", "skipped_missing_api_wallet_address"
    return "ok", str(derived.casefold() == api_wallet_address.casefold())


if __name__ == "__main__":
    main()
