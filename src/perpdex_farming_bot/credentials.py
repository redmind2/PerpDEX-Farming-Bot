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
HOTSTUFF_SIGNING_FIELDS = ("SIGNER_ADDRESS", "SIGNER_PRIVATE_KEY")
RISEX_PRIVATE_READONLY_FIELDS = ("ACCOUNT_ADDRESS",)
RISEX_SIGNING_FIELDS = ("SIGNER_ADDRESS", "SIGNER_PRIVATE_KEY")
PACIFICA_PRIVATE_READONLY_FIELDS = ("ACCOUNT_ADDRESS",)
PACIFICA_SIGNING_FIELDS = ("API_AGENT_PUBLIC_KEY", "API_AGENT_PRIVATE_KEY")
HYPERLIQUID_PRIVATE_READONLY_FIELDS = ("ACCOUNT_ADDRESS",)
HYPERLIQUID_SIGNING_FIELDS = ("API_WALLET_ADDRESS", "API_WALLET_PRIVATE_KEY")
LIGHTER_PRIVATE_READONLY_FIELDS = ("L1_ADDRESS", "ACCOUNT_INDEX", "READ_ONLY_AUTH_TOKEN")
LIGHTER_SIGNING_FIELDS = ("ACCOUNT_INDEX", "API_KEY_INDEX", "API_PRIVATE_KEY")


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
        return (self.signer_address, self.signer_private_key)


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
    missing: list[str] = []
    if masked_env_status(names.signer_address) == "missing":
        missing.append(names.signer_address)
    if masked_env_status(names.signer_private_key) == "missing":
        missing.append(names.signer_private_key)
    return missing


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


@dataclass(frozen=True)
class RisexCredentialEnv:
    prefix: str
    environment: str
    account_address: str
    signer_address: str
    signer_private_key: str

    @property
    def private_readonly_names(self) -> tuple[str, ...]:
        return (self.account_address,)

    @property
    def signing_names(self) -> tuple[str, ...]:
        return (self.signer_address, self.signer_private_key)


def risex_credential_env(prefix: str, environment: str = "TESTNET") -> RisexCredentialEnv:
    canonical = normalize_env_prefix(prefix)
    env = _risex_credential_environment(environment)
    return RisexCredentialEnv(
        prefix=canonical,
        environment=env,
        account_address=env_name(canonical, "ACCOUNT_ADDRESS", env),
        signer_address=env_name(canonical, "SIGNER_ADDRESS", env),
        signer_private_key=env_name(canonical, "SIGNER_PRIVATE_KEY", env),
    )


def risex_available_private_readonly_env(prefix: str, environment: str = "TESTNET") -> RisexCredentialEnv | None:
    candidate = risex_credential_env(prefix, environment)
    if all(masked_env_status(name) != "missing" for name in candidate.private_readonly_names):
        return candidate
    return None


def risex_private_readonly_missing(prefix: str, environment: str = "TESTNET") -> list[str]:
    names = risex_credential_env(prefix, environment).private_readonly_names
    return [name for name in names if masked_env_status(name) == "missing"]


def risex_signing_missing(prefix: str, environment: str = "TESTNET") -> list[str]:
    names = risex_credential_env(prefix, environment)
    return [name for name in names.signing_names if masked_env_status(name) == "missing"]


def read_risex_private_readonly_params(prefix: str, environment: str = "TESTNET") -> dict[str, str]:
    names = risex_available_private_readonly_env(prefix, environment) or risex_credential_env(prefix, environment)
    return {"account": get_env(names.account_address)}


def read_risex_credentials(prefix: str, environment: str = "TESTNET") -> dict[str, str]:
    names = risex_credential_env(prefix, environment)
    return {
        "account_address": get_env(names.account_address),
        "signer_address": get_env(names.signer_address),
        "signer_private_key": get_env(names.signer_private_key),
    }


def _risex_credential_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    return normalized


@dataclass(frozen=True)
class PacificaCredentialEnv:
    prefix: str
    environment: str
    account_address: str
    api_agent_public_key: str
    api_agent_private_key: str

    @property
    def private_readonly_names(self) -> tuple[str, ...]:
        return (self.account_address,)

    @property
    def signing_names(self) -> tuple[str, ...]:
        return (self.api_agent_public_key, self.api_agent_private_key)


def pacifica_credential_env(prefix: str, environment: str = "TESTNET") -> PacificaCredentialEnv:
    canonical = normalize_env_prefix(prefix)
    env = _pacifica_credential_environment(environment)
    return PacificaCredentialEnv(
        prefix=canonical,
        environment=env,
        account_address=env_name(canonical, "ACCOUNT_ADDRESS", env),
        api_agent_public_key=env_name(canonical, "API_AGENT_PUBLIC_KEY", env),
        api_agent_private_key=env_name(canonical, "API_AGENT_PRIVATE_KEY", env),
    )


def pacifica_available_private_readonly_env(prefix: str, environment: str = "TESTNET") -> PacificaCredentialEnv | None:
    candidate = pacifica_credential_env(prefix, environment)
    if all(masked_env_status(name) != "missing" for name in candidate.private_readonly_names):
        return candidate
    return None


def pacifica_private_readonly_missing(prefix: str, environment: str = "TESTNET") -> list[str]:
    names = pacifica_credential_env(prefix, environment).private_readonly_names
    return [name for name in names if masked_env_status(name) == "missing"]


def pacifica_signing_missing(prefix: str, environment: str = "TESTNET") -> list[str]:
    names = pacifica_credential_env(prefix, environment)
    return [name for name in names.signing_names if masked_env_status(name) == "missing"]


def read_pacifica_private_readonly_params(prefix: str, environment: str = "TESTNET") -> dict[str, str]:
    names = pacifica_available_private_readonly_env(prefix, environment) or pacifica_credential_env(prefix, environment)
    return {"account": get_env(names.account_address)}


def read_pacifica_credentials(prefix: str, environment: str = "TESTNET") -> dict[str, str]:
    names = pacifica_credential_env(prefix, environment)
    return {
        "account_address": get_env(names.account_address),
        "api_agent_public_key": get_env(names.api_agent_public_key),
        "api_agent_private_key": get_env(names.api_agent_private_key),
    }


def _pacifica_credential_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    return normalized


@dataclass(frozen=True)
class HyperliquidCredentialEnv:
    prefix: str
    environment: str
    account_address: str
    api_wallet_address: str
    api_wallet_private_key: str
    vault_address: str

    @property
    def private_readonly_names(self) -> tuple[str, ...]:
        return (self.account_address,)

    @property
    def signing_names(self) -> tuple[str, ...]:
        return (self.api_wallet_address, self.api_wallet_private_key)


def hyperliquid_credential_env(prefix: str, environment: str = "PRODUCTION") -> HyperliquidCredentialEnv:
    canonical = normalize_env_prefix(prefix)
    env = _hyperliquid_credential_environment(environment)
    return HyperliquidCredentialEnv(
        prefix=canonical,
        environment=env,
        account_address=env_name(canonical, "ACCOUNT_ADDRESS", env),
        api_wallet_address=env_name(canonical, "API_WALLET_ADDRESS", env),
        api_wallet_private_key=env_name(canonical, "API_WALLET_PRIVATE_KEY", env),
        vault_address=env_name(canonical, "VAULT_ADDRESS", env),
    )


def hyperliquid_available_private_readonly_env(
    prefix: str,
    environment: str = "PRODUCTION",
) -> HyperliquidCredentialEnv | None:
    candidate = hyperliquid_credential_env(prefix, environment)
    if all(masked_env_status(name) != "missing" for name in candidate.private_readonly_names):
        return candidate
    return None


def hyperliquid_private_readonly_missing(prefix: str, environment: str = "PRODUCTION") -> list[str]:
    names = hyperliquid_credential_env(prefix, environment).private_readonly_names
    return [name for name in names if masked_env_status(name) == "missing"]


def hyperliquid_signing_missing(prefix: str, environment: str = "PRODUCTION") -> list[str]:
    names = hyperliquid_credential_env(prefix, environment)
    return [name for name in names.signing_names if masked_env_status(name) == "missing"]


def read_hyperliquid_private_readonly_params(prefix: str, environment: str = "PRODUCTION") -> dict[str, str]:
    names = hyperliquid_available_private_readonly_env(prefix, environment) or hyperliquid_credential_env(
        prefix,
        environment,
    )
    return {
        "user": get_env(names.account_address),
        "vault_address": get_env(names.vault_address),
    }


def read_hyperliquid_credentials(prefix: str, environment: str = "PRODUCTION") -> dict[str, str]:
    names = hyperliquid_credential_env(prefix, environment)
    return {
        "account_address": get_env(names.account_address),
        "api_wallet_address": get_env(names.api_wallet_address),
        "api_wallet_private_key": get_env(names.api_wallet_private_key),
        "vault_address": get_env(names.vault_address),
    }


def _hyperliquid_credential_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    return normalized


@dataclass(frozen=True)
class LighterCredentialEnv:
    prefix: str
    environment: str
    l1_address: str
    account_index: str
    api_key_index: str
    api_private_key: str
    read_only_auth_token: str

    @property
    def private_readonly_names(self) -> tuple[str, ...]:
        return (self.l1_address, self.account_index, self.read_only_auth_token)

    @property
    def signing_names(self) -> tuple[str, ...]:
        return (self.account_index, self.api_key_index, self.api_private_key)


def lighter_credential_env(prefix: str, environment: str = "PRODUCTION") -> LighterCredentialEnv:
    canonical = normalize_env_prefix(prefix)
    env = _lighter_credential_environment(environment)
    return LighterCredentialEnv(
        prefix=canonical,
        environment=env,
        l1_address=env_name(canonical, "L1_ADDRESS", env),
        account_index=env_name(canonical, "ACCOUNT_INDEX", env),
        api_key_index=env_name(canonical, "API_KEY_INDEX", env),
        api_private_key=env_name(canonical, "API_PRIVATE_KEY", env),
        read_only_auth_token=env_name(canonical, "READ_ONLY_AUTH_TOKEN", env),
    )


def lighter_available_private_readonly_env(
    prefix: str,
    environment: str = "PRODUCTION",
) -> LighterCredentialEnv | None:
    candidate = lighter_credential_env(prefix, environment)
    if masked_env_status(candidate.account_index) != "missing":
        return candidate
    if masked_env_status(candidate.l1_address) != "missing":
        return candidate
    return None


def lighter_private_readonly_missing(prefix: str, environment: str = "PRODUCTION") -> list[str]:
    names = lighter_credential_env(prefix, environment)
    if lighter_available_private_readonly_env(prefix, environment) is not None:
        return []
    return [names.account_index, names.l1_address]


def lighter_signing_missing(prefix: str, environment: str = "PRODUCTION") -> list[str]:
    names = lighter_credential_env(prefix, environment)
    return [name for name in names.signing_names if masked_env_status(name) == "missing"]


def read_lighter_private_readonly_params(prefix: str, environment: str = "PRODUCTION") -> dict[str, str]:
    names = lighter_available_private_readonly_env(prefix, environment) or lighter_credential_env(prefix, environment)
    return {
        "l1_address": get_env(names.l1_address),
        "account_index": get_env(names.account_index),
        "read_only_auth_token": get_env(names.read_only_auth_token),
    }


def read_lighter_credentials(prefix: str, environment: str = "PRODUCTION") -> dict[str, str]:
    names = lighter_credential_env(prefix, environment)
    return {
        "l1_address": get_env(names.l1_address),
        "account_index": get_env(names.account_index),
        "api_key_index": get_env(names.api_key_index),
        "api_private_key": get_env(names.api_private_key),
        "read_only_auth_token": get_env(names.read_only_auth_token),
    }


def _lighter_credential_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    return normalized
