from __future__ import annotations

import argparse
import importlib.util

from perpdex_farming_bot.connectors.risex_readonly import (
    RisexReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    default_wss_endpoint,
    endpoint_from_env,
    normalize_risex_environment,
    validate_https_base_url,
    validate_wss_url,
    wss_endpoint_env_name,
)
from perpdex_farming_bot.credentials import (
    risex_available_private_readonly_env,
    risex_credential_env,
    risex_private_readonly_missing,
    risex_signing_missing,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(description="Check RiseX local env values without printing secrets.")
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="RISEX",
        help="Credential prefix/account id. Case-insensitive, e.g. risex_1 -> RISEX_1.",
    )
    parser.add_argument("--environment", default="testnet", help="RiseX environment: testnet or production/mainnet.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_risex_environment(args.environment)
    credential_env = risex_credential_env(args.credential_prefix, environment)
    available_env = risex_available_private_readonly_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    wss_name = wss_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    wss_endpoint = endpoint_from_env(get_env(wss_name), default_wss_endpoint(environment))

    print("risex_env_check=local_only_no_network")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print("sdk_package=risex-client")
    print("sdk_language=typescript")
    print("sdk_official=False")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")

    endpoint_errors: list[str] = []
    try:
        print(f"{api_name}={validate_https_base_url(api_name, api_endpoint)}")
    except RisexReadonlyConfigError as exc:
        print(f"{api_name}=invalid")
        print(f"{api_name}_error={exc}")
        endpoint_errors.append(api_name)
    try:
        print(f"{wss_name}={validate_wss_url(wss_name, wss_endpoint)}")
    except RisexReadonlyConfigError as exc:
        print(f"{wss_name}=invalid")
        print(f"{wss_name}_error={exc}")
        endpoint_errors.append(wss_name)

    print(f"primary_credential_prefix={credential_env.prefix}")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.signer_address}={masked_env_status(credential_env.signer_address)}")
    print(f"primary_{credential_env.signer_private_key}={masked_env_status(credential_env.signer_private_key)}")
    print(f"eth_account_installed={importlib.util.find_spec('eth_account') is not None}")

    key_parse_status, key_matches_signer = _check_signer_private_key(
        signer_address=get_env(credential_env.signer_address),
        signer_private_key=get_env(credential_env.signer_private_key),
    )
    print(f"signer_private_key_parse={key_parse_status}")
    print(f"signer_address_matches_private_key={key_matches_signer}")

    private_missing = risex_private_readonly_missing(args.credential_prefix, environment)
    signing_missing = risex_signing_missing(args.credential_prefix, environment)
    print("risex_public_readonly_env_ready=True")
    print(f"risex_private_readonly_env_ready={available_env is not None}")
    print(f"risex_signing_env_ready={not signing_missing}")
    if private_missing:
        print("private_readonly_missing_required=" + ",".join(private_missing))
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))

    print(f"risex_env_ready={not endpoint_errors and available_env is not None}")
    if endpoint_errors:
        print("invalid_endpoints=" + ",".join(endpoint_errors))


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


if __name__ == "__main__":
    main()
