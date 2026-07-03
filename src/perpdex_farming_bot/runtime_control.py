from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_RUNTIME_CONTROL_PATH = "data/runtime_control.json"


@dataclass(frozen=True)
class ControlDecision:
    enabled: bool
    reason: str


def load_runtime_control(path: str | Path = DEFAULT_RUNTIME_CONTROL_PATH) -> dict[str, object]:
    control_path = Path(path)
    if not control_path.exists():
        return _default_state()
    try:
        raw = json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state()
    return _merge_default(raw)


def save_runtime_control(state: dict[str, object], path: str | Path = DEFAULT_RUNTIME_CONTROL_PATH) -> None:
    control_path = Path(path)
    control_path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    control_path.write_text(json.dumps(_merge_default(state), indent=2, sort_keys=True), encoding="utf-8")


def set_enabled(
    path: str | Path,
    scope: str,
    key: str,
    enabled: bool,
) -> dict[str, object]:
    state = load_runtime_control(path)
    normalized_scope = scope.strip().lower()
    if normalized_scope in {"all", "global"}:
        state["global_enabled"] = enabled
    elif normalized_scope in {"exchange", "exchanges"}:
        _set_override(state, "exchange_enabled", key, enabled)
    elif normalized_scope in {"wallet", "wallets"}:
        _set_override(state, "wallet_enabled", key, enabled)
    elif normalized_scope in {"market", "markets"}:
        _set_override(state, "market_enabled", key, enabled)
    else:
        raise ValueError("scope must be all, exchange, wallet, or market")
    save_runtime_control(state, path)
    return state


def control_decision(
    state: dict[str, object],
    *,
    exchange_id: str,
    wallet_id: str,
    market: str,
) -> ControlDecision:
    if not bool(state.get("global_enabled", True)):
        return ControlDecision(False, "global_disabled")

    for label, mapping, key in (
        ("exchange", state.get("exchange_enabled", {}), exchange_id),
        ("wallet", state.get("wallet_enabled", {}), wallet_id),
        ("market", state.get("market_enabled", {}), market),
    ):
        if isinstance(mapping, dict) and key in mapping and not bool(mapping[key]):
            return ControlDecision(False, f"{label}_disabled:{key}")

    return ControlDecision(True, "enabled")


def format_control_state(state: dict[str, object]) -> str:
    lines = [f"global_enabled={bool(state.get('global_enabled', True))}"]
    for name in ("exchange_enabled", "wallet_enabled", "market_enabled"):
        mapping = state.get(name, {})
        if isinstance(mapping, dict) and mapping:
            formatted = ", ".join(f"{key}:{value}" for key, value in sorted(mapping.items()))
        else:
            formatted = "none"
        lines.append(f"{name}={formatted}")
    return "\n".join(lines)


def _set_override(state: dict[str, object], field: str, key: str, enabled: bool) -> None:
    clean_key = key.strip()
    if not clean_key:
        raise ValueError("key is required for this scope")
    mapping = state.setdefault(field, {})
    if not isinstance(mapping, dict):
        mapping = {}
        state[field] = mapping
    mapping[clean_key] = enabled


def _default_state() -> dict[str, object]:
    return {
        "global_enabled": True,
        "exchange_enabled": {},
        "wallet_enabled": {},
        "market_enabled": {},
        "updated_at_utc": None,
    }


def _merge_default(raw: dict[str, object]) -> dict[str, object]:
    state = _default_state()
    state.update(raw)
    for field in ("exchange_enabled", "wallet_enabled", "market_enabled"):
        if not isinstance(state.get(field), dict):
            state[field] = {}
    return state
