from __future__ import annotations

from typing import Any


class SecretConfigError(ValueError):
    """Raised when a config appears to contain plaintext secrets."""


SECRET_KEY_NAMES = {
    "api_key",
    "api_secret",
    "private_key",
    "secret_key",
    "seed_phrase",
    "session_key",
    "signature",
}


def assert_no_plaintext_secrets(raw: dict[str, Any]) -> None:
    findings: list[str] = []
    _scan(raw, path="config", findings=findings)
    if findings:
        joined = ", ".join(findings)
        raise SecretConfigError(
            "Plaintext secret-like config values are not allowed. "
            f"Move them to local environment variables instead: {joined}"
        )


def _scan(value: Any, path: str, findings: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in SECRET_KEY_NAMES and _looks_like_plaintext_secret(child):
                findings.append(child_path)
            else:
                _scan(child, child_path, findings)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan(child, f"{path}[{index}]", findings)


def _looks_like_plaintext_secret(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith("${") and stripped.endswith("}"):
        return False
    if stripped.upper().startswith("ENV:"):
        return False
    return True

