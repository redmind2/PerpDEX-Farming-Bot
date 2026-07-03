from __future__ import annotations

from dataclasses import dataclass

from perpdex_farming_bot.env import env_name, get_env, masked_env_status, normalize_env_prefix


HIBACHI_REQUIRED_FIELDS = (
    "API_KEY",
    "PUBLIC_KEY",
    "PRIVATE_KEY",
    "ACCOUNT_ID",
)


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
