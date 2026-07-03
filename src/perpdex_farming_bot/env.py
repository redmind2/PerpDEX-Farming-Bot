from __future__ import annotations

import os
import re
from pathlib import Path


def normalize_env_prefix(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_")
    return normalized.upper()


def env_name(prefix: str, name: str, environment: str = "PRODUCTION") -> str:
    return f"{normalize_env_prefix(prefix)}_{name}_{normalize_env_prefix(environment)}"


def load_dotenv_if_present(path: str | Path = ".env") -> bool:
    env_path = Path(path)
    if not env_path.exists():
        return False

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not has_env(key):
            os.environ[key] = value
    return True


def get_env(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]

    target = name.casefold()
    for key, value in os.environ.items():
        if key.casefold() == target:
            return value
    return default


def has_env(name: str) -> bool:
    return get_env(name) != ""


def masked_env_status(name: str) -> str:
    value = get_env(name)
    if not value:
        return "missing"
    return "present"
