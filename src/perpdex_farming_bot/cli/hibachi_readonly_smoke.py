from __future__ import annotations

import argparse
import os
import sys

from perpdex_farming_bot.connectors.hibachi_readonly import (
    DEFAULT_HIBACHI_API_ENDPOINT,
    DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    HibachiReadonlyConfigError,
    endpoint_from_env,
    parse_readonly_paths,
    public_get,
    validate_https_base_url,
)
from perpdex_farming_bot.credentials import (
    hibachi_available_credential_env,
    hibachi_credential_env,
    hibachi_credential_env_candidates,
)
from perpdex_farming_bot.env import get_env, load_dotenv_if_present, masked_env_status


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and optionally run Hibachi read-only smoke checks. This never sends orders.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Local env file to load. Secret values are never printed.",
    )
    parser.add_argument(
        "--credential-prefix",
        "--account-id",
        dest="credential_prefix",
        default="HIBACHI",
        help="Credential prefix/account id. Case-insensitive, e.g. hibachi_1_crypto -> HIBACHI_1_CRYPTO.",
    )
    parser.add_argument(
        "--public-path",
        action="append",
        default=[],
        help="Relative public read-only path to test with GET, for example /... after official docs confirm it.",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="Actually send public GET requests. Without this flag, the command is local-only.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
        help="Timeout for each public GET request when --network is used.",
    )
    args = parser.parse_args()

    env_loaded = load_dotenv_if_present(args.env_file)
    credential_env = hibachi_credential_env(args.credential_prefix)
    available_env = hibachi_available_credential_env(args.credential_prefix)
    data_endpoint = endpoint_from_env(
        get_env("HIBACHI_DATA_API_ENDPOINT_PRODUCTION"),
        DEFAULT_HIBACHI_DATA_API_ENDPOINT,
    )
    api_endpoint = endpoint_from_env(
        get_env("HIBACHI_API_ENDPOINT_PRODUCTION"),
        DEFAULT_HIBACHI_API_ENDPOINT,
    )

    print("hibachi_readonly_smoke=prepared")
    print(f"env_file_loaded={env_loaded}")
    print(f"credential_prefix={credential_env.prefix}")
    if available_env is not None and available_env.prefix != credential_env.prefix:
        print(f"credential_prefix_status=usable_via_legacy:{available_env.prefix}")
    elif available_env is not None:
        print("credential_prefix_status=usable")
    else:
        print("credential_prefix_status=missing")
    print("orders_enabled=False")
    print("cancel_enabled=False")
    print("position_change_enabled=False")
    print("private_network_enabled=False")
    print("public_http_method=GET")

    try:
        print(f"api_endpoint={validate_https_base_url('HIBACHI_API_ENDPOINT_PRODUCTION', api_endpoint)}")
        print(
            "data_api_endpoint="
            f"{validate_https_base_url('HIBACHI_DATA_API_ENDPOINT_PRODUCTION', data_endpoint)}"
        )
        public_paths = _configured_public_paths(args.public_path)
    except HibachiReadonlyConfigError as exc:
        print("hibachi_readonly_smoke_ready=False")
        print(f"config_error={exc}")
        raise SystemExit(2) from exc

    missing = _print_credential_env_status(args.credential_prefix)
    print(f"private_readonly_env_ready={available_env is not None}")
    if missing and available_env is None:
        print("missing_required=" + ",".join(missing))
    elif missing:
        print("primary_missing_required=" + ",".join(missing))
    print("private_readonly_network_skipped=use_hibachi_sdk_smoke_for_private_readonly")

    print(f"public_path_count={len(public_paths)}")
    if not args.network:
        print("public_network_skipped=pass_--network_after_confirming_official_readonly_paths")
        print("hibachi_readonly_smoke_ready=True")
        return

    if not public_paths:
        print("hibachi_readonly_smoke_ready=False")
        print("public_network_skipped=no_public_paths_configured")
        raise SystemExit(2)

    all_ok = True
    for index, path in enumerate(public_paths, start=1):
        result = public_get(data_endpoint, path, args.timeout_seconds)
        all_ok = all_ok and result.ok
        print(f"public_get_{index}_url={result.url}")
        print(f"public_get_{index}_ok={result.ok}")
        print(f"public_get_{index}_status_code={result.status_code}")
        print(f"public_get_{index}_content_type={result.content_type or 'unknown'}")
        print(f"public_get_{index}_body_shape={result.body_shape}")
        if result.error:
            print(f"public_get_{index}_error={result.error}")

    print(f"hibachi_readonly_smoke_ready={all_ok}")
    if not all_ok:
        raise SystemExit(1)


def _configured_public_paths(cli_paths: list[str]) -> list[str]:
    raw_env_paths = os.environ.get("HIBACHI_PUBLIC_READONLY_PATHS_PRODUCTION", "")
    paths = parse_readonly_paths(raw_env_paths)
    for path in cli_paths:
        paths.extend(parse_readonly_paths(path))
    return paths


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
