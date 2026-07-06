from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from perpdex_farming_bot.connectors.pacifica_readonly import validate_https_base_url


SIGNED_POST_PATHS = {
    "/orders/create": "create_order",
    "/orders/create_market": "create_market_order",
    "/orders/cancel": "cancel_order",
}

SECRETISH_PAYLOAD_NAMES = {
    "api_key",
    "apikey",
    "key",
    "private_key",
    "privatekey",
    "secret",
    "secret_key",
    "session",
    "session_key",
    "signature",
    "token",
}

JSON_READ_LIMIT_BYTES = 1048576


class PacificaTradingConfigError(ValueError):
    """Raised when a Pacifica trading request would violate local safety rules."""


class PacificaSigningError(ValueError):
    """Raised when a Pacifica request cannot be signed safely."""


@dataclass(frozen=True)
class PacificaSignedRequest:
    path: str
    operation_type: str
    request: dict[str, object]
    safe_keys: tuple[str, ...]
    client_order_id: str | None
    signature_present: bool
    api_agent_public_key_present: bool
    message_length: int


@dataclass(frozen=True)
class PacificaPostResult:
    ok: bool
    status_code: int | None
    content_type: str
    body_shape: str
    parsed: object | None
    error: str


def build_signed_request(
    *,
    operation_type: str,
    payload: dict[str, object],
    account_address: str,
    api_agent_public_key: str,
    api_agent_private_key: str,
    expiry_window_ms: int = 5000,
    timestamp_ms: int | None = None,
) -> PacificaSignedRequest:
    operation_type = operation_type.strip()
    if operation_type not in SIGNED_POST_PATHS.values():
        allowed = ",".join(sorted(SIGNED_POST_PATHS.values()))
        raise PacificaTradingConfigError(f"operation_type must be one of {allowed}")
    if not account_address:
        raise PacificaSigningError("Pacifica account address is required for signing")
    if not api_agent_public_key:
        raise PacificaSigningError("Pacifica API Agent public key is required for signing")
    if not api_agent_private_key:
        raise PacificaSigningError("Pacifica API Agent private key is required for signing")
    if expiry_window_ms <= 0 or expiry_window_ms > 30_000:
        raise PacificaTradingConfigError("expiry_window_ms must be greater than 0 and <= 30000")

    _validate_operation_payload(payload)
    keypair, base58_module = _load_agent_keypair(api_agent_private_key)
    derived_api_agent_public_key = str(keypair.pubkey())
    if derived_api_agent_public_key != api_agent_public_key:
        raise PacificaSigningError("derived API Agent public key does not match PACIFICA_API_AGENT_PUBLIC_KEY")

    timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    signature_header = {
        "timestamp": timestamp,
        "expiry_window": expiry_window_ms,
        "type": operation_type,
    }
    message = pacifica_compact_message(signature_header, payload)
    signature = keypair.sign_message(message.encode("utf-8"))
    signature_b58 = base58_module.b58encode(bytes(signature)).decode("ascii")

    request: dict[str, object] = {
        "account": account_address,
        "agent_wallet": api_agent_public_key,
        "signature": signature_b58,
        "timestamp": timestamp,
        "expiry_window": expiry_window_ms,
        **payload,
    }
    return PacificaSignedRequest(
        path=_path_for_operation_type(operation_type),
        operation_type=operation_type,
        request=request,
        safe_keys=tuple(sorted(request.keys())),
        client_order_id=str(payload["client_order_id"]) if payload.get("client_order_id") else None,
        signature_present=bool(signature_b58),
        api_agent_public_key_present=bool(api_agent_public_key),
        message_length=len(message),
    )


def pacifica_compact_message(signature_header: dict[str, object], payload: dict[str, object]) -> str:
    data_to_sign = {**signature_header, "data": payload}
    sorted_message = _sort_json_keys(data_to_sign)
    return json.dumps(sorted_message, separators=(",", ":"))


def post_signed_json(
    base_url: str,
    signed_request: PacificaSignedRequest,
    timeout_seconds: float,
) -> PacificaPostResult:
    base = validate_https_base_url("PACIFICA_API_ENDPOINT", base_url)
    path = _validate_signed_post_path(signed_request.path)
    expected_type = SIGNED_POST_PATHS[path]
    if signed_request.operation_type != expected_type:
        raise PacificaTradingConfigError(f"{path} must use operation_type={expected_type}")

    url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    body = json.dumps(signed_request.request, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "perpdex-farming-bot-pacifica-live-test",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read(JSON_READ_LIMIT_BYTES)
            content_type = response.headers.get("Content-Type", "")
            status_code = response.getcode()
            parsed = _parse_json_or_none(response_body)
            return PacificaPostResult(
                ok=200 <= status_code < 300,
                status_code=status_code,
                content_type=content_type,
                body_shape=_body_shape(parsed, response_body, content_type),
                parsed=parsed,
                error="",
            )
    except HTTPError as exc:
        response_body = exc.read(JSON_READ_LIMIT_BYTES)
        parsed = _parse_json_or_none(response_body)
        return PacificaPostResult(
            ok=False,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", ""),
            body_shape=_body_shape(parsed, response_body, exc.headers.get("Content-Type", "")),
            parsed=parsed,
            error=_sanitize_error(exc.reason or "http_error"),
        )
    except (TimeoutError, URLError) as exc:
        return PacificaPostResult(
            ok=False,
            status_code=None,
            content_type="",
            body_shape="not_read",
            parsed=None,
            error=exc.__class__.__name__,
        )


def safe_signed_request_summary(signed_request: PacificaSignedRequest) -> dict[str, object]:
    return {
        "path": signed_request.path,
        "operation_type": signed_request.operation_type,
        "safe_keys": ",".join(signed_request.safe_keys),
        "client_order_id": signed_request.client_order_id or "",
        "signature_present": signed_request.signature_present,
        "api_agent_public_key_present": signed_request.api_agent_public_key_present,
        "message_length": signed_request.message_length,
    }


def extract_order_id(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("order_id", "id"):
            value = payload.get(key)
            if value is not None:
                return str(value)
        data = payload.get("data")
        if isinstance(data, dict):
            return extract_order_id(data)
        if isinstance(data, list) and data:
            return extract_order_id(data[0])
    return ""


def _load_agent_keypair(api_agent_private_key: str) -> tuple[Any, Any]:
    try:
        import base58
        from solders.keypair import Keypair
    except ImportError as exc:
        raise PacificaSigningError(
            "Pacifica signing requires base58 and solders. Install project requirements before live testing."
        ) from exc

    try:
        return Keypair.from_bytes(base58.b58decode(api_agent_private_key)), base58
    except Exception as exc:
        raise PacificaSigningError("Pacifica agent private key could not be decoded as base58 Ed25519 key") from exc


def _validate_operation_payload(payload: dict[str, object]) -> None:
    if not payload:
        raise PacificaTradingConfigError("operation payload is required")
    for key, value in payload.items():
        normalized = key.replace("-", "_").lower()
        if normalized in SECRETISH_PAYLOAD_NAMES:
            raise PacificaTradingConfigError(f"payload.{key} must not contain secret-like values")
        if value is None:
            raise PacificaTradingConfigError(f"payload.{key} must not be null")


def _path_for_operation_type(operation_type: str) -> str:
    for path, candidate in SIGNED_POST_PATHS.items():
        if candidate == operation_type:
            return path
    raise PacificaTradingConfigError(f"unsupported operation_type={operation_type}")


def _validate_signed_post_path(path: str) -> str:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise PacificaTradingConfigError("Pacifica signed POST paths must be relative paths, not full URLs")
    if parsed.path not in SIGNED_POST_PATHS:
        allowed = ",".join(sorted(SIGNED_POST_PATHS))
        raise PacificaTradingConfigError(f"Pacifica signed POST path must be one of {allowed}")
    if parsed.query or parsed.fragment:
        raise PacificaTradingConfigError("Pass request fields in JSON payload, not inside the path")
    return parsed.path


def _sort_json_keys(value: object) -> object:
    if isinstance(value, dict):
        return {key: _sort_json_keys(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_json_keys(item) for item in value]
    return value


def _parse_json_or_none(response_body: bytes) -> object | None:
    if not response_body:
        return None
    try:
        return json.loads(response_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _body_shape(parsed: object | None, raw: bytes, content_type: str) -> str:
    if not raw:
        return "empty"
    if parsed is None:
        if "json" not in content_type.lower():
            return f"non_json_bytes={len(raw)}"
        return f"json_parse_failed_bytes={len(raw)}"
    if isinstance(parsed, dict):
        keys = ",".join(sorted(str(key) for key in parsed.keys())[:8])
        return f"json_object_keys={keys or 'none'}"
    if isinstance(parsed, list):
        return f"json_array_len={len(parsed)}"
    return f"json_{type(parsed).__name__}"


def _sanitize_error(error: str) -> str:
    for marker in ("signature", "private", "token", "secret"):
        if marker in error.lower():
            return "redacted_exchange_error"
    return error[:240]
