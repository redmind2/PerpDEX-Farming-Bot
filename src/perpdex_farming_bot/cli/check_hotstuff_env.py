from __future__ import annotations

import argparse

from perpdex_farming_bot.connectors.hotstuff_readonly import (
    HotstuffReadonlyConfigError,
    api_endpoint_env_name,
    default_api_endpoint,
    default_wss_endpoint,
    endpoint_from_env,
    normalize_hotstuff_environment,
    validate_https_base_url,
    validate_wss_url,
    wss_endpoint_env_name,
)
from perpdex_farming_bot.credentials import (
    hotstuff_available_private_readonly_env,
    hotstuff_credential_env,
    hotstuff_private_readonly_missing,
    hotstuff_signing_missing,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Hotstuff local env values without printing secrets.")
    parser.add_argument("--env-file", default=".env", help="Local env file. Secret values are never printed.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HOTSTUFF",
        help="Credential prefix/account id. Case-insensitive, e.g. hotstuff_1 -> HOTSTUFF_1.",
    )
    parser.add_argument(
        "--environment",
        default="production",
        help="Hotstuff environment: production/mainnet or testnet.",
    )
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    environment = normalize_hotstuff_environment(args.environment)
    credential_env = hotstuff_credential_env(args.credential_prefix, environment)
    available_env = hotstuff_available_private_readonly_env(args.credential_prefix, environment)
    api_name = api_endpoint_env_name(environment)
    wss_name = wss_endpoint_env_name(environment)
    api_endpoint = endpoint_from_env(get_env(api_name), default_api_endpoint(environment))
    wss_endpoint = endpoint_from_env(get_env(wss_name), default_wss_endpoint(environment))

    print("hotstuff_env_check=local_only_no_network")
    print(f"env_file_loaded={env_loaded}")
    print(f"environment={environment}")
    print(f"credential_prefix={credential_env.prefix}")
    print("sdk_package=hotstuff-python-sdk")
    print("sdk_import=hotstuff")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("transfer_enabled=False")
    print("withdraw_enabled=False")

    endpoint_errors: list[str] = []
    try:
        print(f"{api_name}={validate_https_base_url(api_name, api_endpoint)}")
    except HotstuffReadonlyConfigError as exc:
        print(f"{api_name}=invalid")
        print(f"{api_name}_error={exc}")
        endpoint_errors.append(api_name)
    try:
        print(f"{wss_name}={validate_wss_url(wss_name, wss_endpoint)}")
    except HotstuffReadonlyConfigError as exc:
        print(f"{wss_name}=invalid")
        print(f"{wss_name}_error={exc}")
        endpoint_errors.append(wss_name)

    print(f"primary_credential_prefix={credential_env.prefix}")
    print(f"primary_{credential_env.account_address}={masked_env_status(credential_env.account_address)}")
    print(f"primary_{credential_env.signer_address}={masked_env_status(credential_env.signer_address)}")
    print(f"primary_{credential_env.signer_private_key}={masked_env_status(credential_env.signer_private_key)}")
    print(f"legacy_{credential_env.legacy_private_key}={masked_env_status(credential_env.legacy_private_key)}")

    private_missing = hotstuff_private_readonly_missing(args.credential_prefix, environment)
    signing_missing = hotstuff_signing_missing(args.credential_prefix, environment)
    print("hotstuff_public_readonly_env_ready=True")
    print(f"hotstuff_private_readonly_env_ready={available_env is not None}")
    print(f"hotstuff_signing_env_ready={not signing_missing}")
    if private_missing:
        print("private_readonly_missing_required=" + ",".join(private_missing))
    if signing_missing:
        print("signing_missing_required=" + ",".join(signing_missing))

    print(f"hotstuff_env_ready={not endpoint_errors and available_env is not None}")
    if endpoint_errors:
        print("invalid_endpoints=" + ",".join(endpoint_errors))


if __name__ == "__main__":
    main()
