from __future__ import annotations

import os

from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    HibachiReadonlyConfigError,
    endpoint_from_env,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import hibachi_available_credential_env, hibachi_credential_env, hibachi_credential_env_candidates
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status

OPTIONAL_HIBACHI_ENV_VARS = (
    "HIBACHI_API_ENDPOINT_PRODUCTION",
    "HIBACHI_DATA_API_ENDPOINT_PRODUCTION",
)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Check Hibachi local env values without printing secrets.")
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HIBACHI",
        help="Credential prefix/account id. Case-insensitive, e.g. hibachi_1_crypto -> HIBACHI_1_CRYPTO.",
    )
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(".env")
    credential_env = hibachi_credential_env(args.credential_prefix)
    available_env = hibachi_available_credential_env(args.credential_prefix)

    print("hibachi_env_check=local_only_no_network")
    print(f"env_file_loaded={env_loaded}")
    print(f"credential_prefix={credential_env.prefix}")
    if available_env is not None and available_env.prefix != credential_env.prefix:
        print(f"credential_prefix_status=usable_via_legacy:{available_env.prefix}")
    elif available_env is not None:
        print("credential_prefix_status=usable")
    else:
        print("credential_prefix_status=missing")
    missing: list[str] = []
    endpoint_errors: list[str] = []

    for candidate in hibachi_credential_env_candidates(args.credential_prefix):
        label = "primary" if candidate.prefix == credential_env.prefix else "legacy"
        candidate_missing: list[str] = []
        print(f"{label}_credential_prefix={candidate.prefix}")
        for name in candidate.required_names:
            status = masked_env_status(name)
            print(f"{label}_{name}={status}")
            if status == "missing":
                candidate_missing.append(name)
        if candidate.prefix == credential_env.prefix:
            missing = candidate_missing

    for name in OPTIONAL_HIBACHI_ENV_VARS:
        default = _default_endpoint(name)
        value = endpoint_from_env(get_env(name), default)
        try:
            print(f"{name}={validate_https_base_url(name, value)}")
        except HibachiReadonlyConfigError as exc:
            print(f"{name}=invalid")
            print(f"{name}_error={exc}")
            endpoint_errors.append(name)

    if available_env is None and missing:
        print("hibachi_env_ready=False")
        print("missing_required=" + ",".join(missing))
        return
    if available_env is not None and available_env.prefix != credential_env.prefix and missing:
        print("primary_missing_required=" + ",".join(missing))
    if endpoint_errors:
        print("hibachi_env_ready=False")
        print("invalid_endpoints=" + ",".join(endpoint_errors))
        return

    print("hibachi_env_ready=True")


def _default_endpoint(name: str) -> str:
    if name == "HIBACHI_API_ENDPOINT_PRODUCTION":
        return DEFAULT_HIBACHI_API_ENDPOINT
    if name == "HIBACHI_DATA_API_ENDPOINT_PRODUCTION":
        return DEFAULT_HIBACHI_DATA_API_ENDPOINT
    raise ValueError(f"unknown endpoint env var: {name}")


if __name__ == "__main__":
    main()
