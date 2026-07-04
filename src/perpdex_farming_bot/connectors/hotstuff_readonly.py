from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from perpdex_farming_bot.env import normalize_env_prefix


DEFAULT_HOTSTUFF_API_ENDPOINT = "https://api.hotstuff.trade"
DEFAULT_HOTSTUFF_TESTNET_API_ENDPOINT = "https://testnet-api.hotstuff.trade"
DEFAULT_HOTSTUFF_WSS_ENDPOINT = "wss://api.hotstuff.trade/ws"
DEFAULT_HOTSTUFF_TESTNET_WSS_ENDPOINT = "wss://testnet-api.hotstuff.trade/ws"

PUBLIC_INFO_METHODS = {
    "instruments",
    "ticker",
    "orderbook",
}

PRIVATE_READONLY_INFO_METHODS = {
    "accountSummary",
    "accountInfo",
    "openOrders",
    "positions",
    "orderHistory",
    "fills",
    "fundingHistory",
    "transferHistory",
    "instrumentLeverage",
    "allAgents",
    "referralSummary",
    "userFees",
}

SECRETISH_PARAM_NAMES = {
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

BODY_SHAPE_READ_LIMIT_BYTES = 65536
JSON_READ_LIMIT_BYTES = 1048576


class HotstuffReadonlyConfigError(ValueError):
    """Raised when a Hotstuff smoke test would exceed the read-only boundary."""


@dataclass(frozen=True)
class HotstuffReadonlyHttpResult:
    url: str
    ok: bool
    status_code: int | None
    content_type: str
    body_shape: str
    error: str


def normalize_hotstuff_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    raise HotstuffReadonlyConfigError("Hotstuff environment must be production/mainnet or testnet")


def default_api_endpoint(environment: str) -> str:
    if normalize_hotstuff_environment(environment) == "TESTNET":
        return DEFAULT_HOTSTUFF_TESTNET_API_ENDPOINT
    return DEFAULT_HOTSTUFF_API_ENDPOINT


def default_wss_endpoint(environment: str) -> str:
    if normalize_hotstuff_environment(environment) == "TESTNET":
        return DEFAULT_HOTSTUFF_TESTNET_WSS_ENDPOINT
    return DEFAULT_HOTSTUFF_WSS_ENDPOINT


def api_endpoint_env_name(environment: str) -> str:
    return f"HOTSTUFF_API_ENDPOINT_{normalize_hotstuff_environment(environment)}"


def wss_endpoint_env_name(environment: str) -> str:
    return f"HOTSTUFF_WSS_ENDPOINT_{normalize_hotstuff_environment(environment)}"


def endpoint_from_env(value: str | None, default: str) -> str:
    return value.strip() if value and value.strip() else default


def validate_https_base_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise HotstuffReadonlyConfigError(f"{name} must use https")
    if not parsed.netloc:
        raise HotstuffReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise HotstuffReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise HotstuffReadonlyConfigError(f"{name} must not include query string or fragment")

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def validate_wss_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "wss":
        raise HotstuffReadonlyConfigError(f"{name} must use wss")
    if not parsed.netloc:
        raise HotstuffReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise HotstuffReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise HotstuffReadonlyConfigError(f"{name} must not include query string or fragment")
    return value.rstrip("/")


def validate_info_method(method: str, private_readonly: bool = False) -> None:
    allowed = PRIVATE_READONLY_INFO_METHODS if private_readonly else PUBLIC_INFO_METHODS
    if method not in allowed:
        scope = "private read-only" if private_readonly else "public"
        raise HotstuffReadonlyConfigError(f"Hotstuff {scope} info method is not allowlisted: {method}")


def info_post(
    base_url: str,
    method: str,
    params: dict[str, object],
    timeout_seconds: float,
    private_readonly: bool = False,
) -> HotstuffReadonlyHttpResult:
    base = validate_https_base_url("HOTSTUFF_API_ENDPOINT", base_url)
    validate_info_method(method, private_readonly=private_readonly)
    _assert_no_secretish_params(params)
    url = urljoin(base.rstrip("/") + "/", "info")
    body = json.dumps({"method": method, "params": params}, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "perpdex-farming-bot-hotstuff-readonly-smoke",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read(BODY_SHAPE_READ_LIMIT_BYTES)
            content_type = response.headers.get("Content-Type", "")
            status_code = response.getcode()
            return HotstuffReadonlyHttpResult(
                url=url,
                ok=200 <= status_code < 300,
                status_code=status_code,
                content_type=content_type,
                body_shape=_body_shape(response_body, content_type),
                error="",
            )
    except HTTPError as exc:
        return HotstuffReadonlyHttpResult(
            url=url,
            ok=False,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", ""),
            body_shape="not_read",
            error=exc.reason or "http_error",
        )
    except (TimeoutError, URLError) as exc:
        return HotstuffReadonlyHttpResult(
            url=url,
            ok=False,
            status_code=None,
            content_type="",
            body_shape="not_read",
            error=exc.__class__.__name__,
        )


def info_post_json(
    base_url: str,
    method: str,
    params: dict[str, object],
    timeout_seconds: float,
    private_readonly: bool = False,
) -> object:
    base = validate_https_base_url("HOTSTUFF_API_ENDPOINT", base_url)
    validate_info_method(method, private_readonly=private_readonly)
    _assert_no_secretish_params(params)
    url = urljoin(base.rstrip("/") + "/", "info")
    body = json.dumps({"method": method, "params": params}, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "perpdex-farming-bot-hotstuff-readonly-smoke",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        response_body = response.read(JSON_READ_LIMIT_BYTES)
    return json.loads(response_body.decode("utf-8"))


def _assert_no_secretish_params(value: object, path: str = "params") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).replace("-", "_").lower()
            if normalized_key in SECRETISH_PARAM_NAMES:
                raise HotstuffReadonlyConfigError(f"{path}.{key} must not be sent to /info")
            _assert_no_secretish_params(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secretish_params(child, f"{path}[{index}]")


def _body_shape(body: bytes, content_type: str) -> str:
    if not body:
        return "empty"
    if "json" not in content_type.lower():
        return f"non_json_bytes={len(body)}"

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        if len(body) >= BODY_SHAPE_READ_LIMIT_BYTES:
            return f"json_truncated_bytes={len(body)}"
        return f"json_parse_failed_bytes={len(body)}"

    if isinstance(payload, dict):
        keys = ",".join(sorted(str(key) for key in payload.keys())[:8])
        return f"json_object_keys={keys or 'none'}"
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            keys = ",".join(sorted(str(key) for key in payload[0].keys())[:8])
            return f"json_array_len={len(payload)} first_object_keys={keys or 'none'}"
        return f"json_array_len={len(payload)}"
    return f"json_{type(payload).__name__}"
