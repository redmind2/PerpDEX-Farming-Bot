from __future__ import annotations

import argparse
import importlib.util

from perpdex_farming_bot.connectors.lighter_readonly import (
    LighterReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    default_wss_endpoint,
    endpoint_from_env,
    normalize_lighter_environment,
    validate_https_base_url,
    validate_wss_url,
    wss_endpoint_env_name,
)
from perpdex_farming_bot.credentials import (
    lighter_available_private_readonly_env,
    lighter_credential_env,
    lighter_private_readonly_missing,
    lighter_signing_missing,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Lighter local env values without printing secrets.")
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="LIGHTER",
        help="Credential prefix/account id. Case-insensitive, e.g. lighter_1 -> LIGHTER_1.",
    )
    parser.add_argument("--environment", default="production", help="Lighter environment: production/mainnet or testnet.")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_lighter_environment(args.environment)
    credential_env = lighter_credential_env(args.credential_prefix, environment)
    available_env = lighter_available_private_readonly_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    wss_name = wss_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    wss_endpoint = endpoint_from_env(get_env(wss_name), default_wss_endpoint(environment))

    print("lighter_env_check=local_only_no_network")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print("api_docs=https://apidocs.lighter.xyz/docs/get-started")
    print("sdk_repository=https://github.com/elliottech/lighter-python")
    print("sdk_package=lighter-sdk")
    print("sdk_import_name=lighter")
    print("sdk_official=True")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")

    endpoint_errors: list[str] = []
    try:
        print(f"{api_name}={validate_https_base_url(api_name, api_endpoint)}")
    except LighterReadonlyConfigError as exc:
        print(f"{api_name}=invalid")
        print(f"{api_name}_error={exc}")
        endpoint_errors.append(api_name)
    try:
        print(f"{wss_name}={validate_wss_url(wss_name, wss_endpoint)}")
    except LighterReadonlyConfigError as exc:
        print(f"{wss_name}=invalid")
        print(f"{wss_name}_error={exc}")
        endpoint_errors.append(wss_name)

    print(f"primary_credential_prefix={credential_env.prefix}")
    print(f"primary_{credential_env.l1_address}={masked_env_status(credential_env.l1_address)}")
    print(f"primary_{credential_env.account_index}={masked_env_status(credential_env.account_index)}")
    print(f"primary_{credential_env.api_key_index}={masked_env_status(credential_env.api_key_index)}")
    print(f"primary_{credential_env.api_private_key}={masked_env_status(credential_env.api_private_key)}")
    print(f"optional_{credential_env.read_only_auth_token}={masked_env_status(credential_env.read_only_auth_token)}")

    private_missing = lighter_private_readonly_missing(args.credential_prefix, environment)
    signing_missing = lighter_signing_missing(args.credential_prefix, environment)
    print("lighter_public_readonly_env_ready=True")
    print(f"lighter_private_readonly_env_ready={available_env is not None}")
    print(f"lighter_private_auth_env_ready={_has_account_index(credential_env) and _has_read_only_auth_token(credential_env)}")
    print(f"lighter_signing_env_ready={not signing_missing}")
    if private_missing:
        print("private_readonly_missing_one_of=" + ",".join(private_missing))
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))

    print(f"account_index_parse={_int_parse_status(get_env(credential_env.account_index))}")
    print(f"api_key_index_parse={_int_parse_status(get_env(credential_env.api_key_index))}")
    print(f"lighter_python_sdk_installed={importlib.util.find_spec('lighter') is not None}")
    print("api_private_key_parse=not_checked_phase0_no_signer_init")
    print(f"lighter_env_ready={not endpoint_errors and available_env is not None}")
    if endpoint_errors:
        print("invalid_endpoints=" + ",".join(endpoint_errors))


def _has_account_index(credential_env: object) -> bool:
    return masked_env_status(getattr(credential_env, "account_index")) != "missing"


def _has_read_only_auth_token(credential_env: object) -> bool:
    return masked_env_status(getattr(credential_env, "read_only_auth_token")) != "missing"


def _int_parse_status(value: str) -> str:
    if not value:
        return "skipped_missing"
    try:
        int(value)
    except ValueError:
        return "error"
    return "ok"


if __name__ == "__main__":
    main()
