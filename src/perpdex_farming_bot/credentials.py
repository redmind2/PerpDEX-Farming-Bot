from __future__ import annotations

from dataclasses import dataclass

from perpdex_farming_bot.env import env_name, get_env, masked_env_status, normalize_env_prefix


HIBACHI_REQUIRED_FIELDS = (
    "API_KEY",
    "PUBLIC_KEY",
    "PRIVATE_KEY",
    "ACCOUNT_ID",
)

HOTSTUFF_PRIVATE_READONLY_FIELDS = ("ACCOUNT_ADDRESS",)
HOTSTUFF_SIGNING_FIELDS = ("SIGNER_PRIVATE_KEY",)


@dataclass(frozen=True)
class HibachiCredentialEnv:
    prefix: str
    api_key: str
    public_key: str
    private_key: str
    account_id: str

    @property
    def required_names(self) -> tuple[str, ...]:
        return (self.api_key, self.public_key, self.private_key, self.account_id)


def hibachi_credential_env(prefix: str) -> HibachiCredentialEnv:
    canonical = normalize_env_prefix(prefix)
    return HibachiCredentialEnv(
        prefix=canonical,
        api_key=env_name(canonical, "API_KEY"),
        public_key=env_name(canonical, "PUBLIC_KEY"),
        private_key=env_name(canonical, "PRIVATE_KEY"),
        account_id=env_name(canonical, "ACCOUNT_ID"),
    )


def hibachi_credential_env_candidates(prefix: str) -> tuple[HibachiCredentialEnv, ...]:
    canonical = normalize_env_prefix(prefix)
    candidates = [hibachi_credential_env(canonical)]
    if canonical.endswith("_CRYPTO"):
        legacy = canonical.removesuffix("_CRYPTO")
        candidates.append(hibachi_credential_env(legacy))
    return tuple(candidates)


def hibachi_available_credential_env(prefix: str) -> HibachiCredentialEnv | None:
    for candidate in hibachi_credential_env_candidates(prefix):
        if all(masked_env_status(name) != "missing" for name in candidate.required_names):
            return candidate
    return None


def hibachi_missing_required(prefix: str) -> list[str]:
    available = hibachi_available_credential_env(prefix)
    if available is not None:
        return []
    names = hibachi_credential_env(prefix).required_names
    return [name for name in names if masked_env_status(name) == "missing"]


def read_hibachi_credentials(prefix: str) -> dict[str, str]:
    names = hibachi_available_credential_env(prefix) or hibachi_credential_env(prefix)
    return {
        "api_key": get_env(names.api_key),
        "public_key": get_env(names.public_key),
        "private_key": get_env(names.private_key),
        "account_id": get_env(names.account_id),
    }


@dataclass(frozen=True)
class HotstuffCredentialEnv:
    prefix: str
    environment: str
    account_address: str
    signer_address: str
    signer_private_key: str
    legacy_private_key: str

    @property
    def private_readonly_names(self) -> tuple[str, ...]:
        return (self.account_address,)

    @property
    def signing_names(self) -> tuple[str, ...]:
        return (self.signer_private_key,)


def hotstuff_credential_env(prefix: str, environment: str = "PRODUCTION") -> HotstuffCredentialEnv:
    canonical = normalize_env_prefix(prefix)
    env = _hotstuff_credential_environment(environment)
    return HotstuffCredentialEnv(
        prefix=canonical,
        environment=env,
        account_address=env_name(canonical, "ACCOUNT_ADDRESS", env),
        signer_address=env_name(canonical, "SIGNER_ADDRESS", env),
        signer_private_key=env_name(canonical, "SIGNER_PRIVATE_KEY", env),
        legacy_private_key=env_name(canonical, "PRIVATE_KEY", env),
    )


def hotstuff_available_private_readonly_env(
    prefix: str,
    environment: str = "PRODUCTION",
) -> HotstuffCredentialEnv | None:
    candidate = hotstuff_credential_env(prefix, environment)
    if all(masked_env_status(name) != "missing" for name in candidate.private_readonly_names):
        return candidate
    return None


def hotstuff_private_readonly_missing(prefix: str, environment: str = "PRODUCTION") -> list[str]:
    names = hotstuff_credential_env(prefix, environment).private_readonly_names
    return [name for name in names if masked_env_status(name) == "missing"]


def hotstuff_signing_missing(prefix: str, environment: str = "PRODUCTION") -> list[str]:
    names = hotstuff_credential_env(prefix, environment)
    has_new_signer_key = masked_env_status(names.signer_private_key) != "missing"
    has_legacy_key = masked_env_status(names.legacy_private_key) != "missing"
    if has_new_signer_key or has_legacy_key:
        return []
    return [names.signer_private_key]


def read_hotstuff_private_readonly_params(prefix: str, environment: str = "PRODUCTION") -> dict[str, str]:
    names = hotstuff_available_private_readonly_env(prefix, environment) or hotstuff_credential_env(prefix, environment)
    return {"user": get_env(names.account_address)}


def read_hotstuff_credentials(prefix: str, environment: str = "PRODUCTION") -> dict[str, str]:
    names = hotstuff_credential_env(prefix, environment)
    signer_private_key = get_env(names.signer_private_key) or get_env(names.legacy_private_key)
    return {
        "account_address": get_env(names.account_address),
        "signer_address": get_env(names.signer_address),
        "signer_private_key": signer_private_key,
        "legacy_private_key": get_env(names.legacy_private_key),
    }


def _hotstuff_credential_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    return normalized
