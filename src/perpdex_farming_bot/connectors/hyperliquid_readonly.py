from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from perpdex_farming_bot.env import normalize_env_prefix


DEFAULT_HYPERLIQUID_PRODUCTION_API_ENDPOINT = "https://api.hyperliquid.xyz"
DEFAULT_HYPERLIQUID_TESTNET_API_ENDPOINT = "https://api.hyperliquid-testnet.xyz"

PUBLIC_INFO_TYPES = {
    "allMids",
    "l2Book",
    "meta",
    "metaAndAssetCtxs",
    "perpDexs",
}

PRIVATE_READONLY_INFO_TYPES = {
    "clearinghouseState",
    "frontendOpenOrders",
    "historicalOrders",
    "openOrders",
    "orderStatus",
    "portfolio",
    "subAccounts",
    "userFees",
    "userFills",
    "userFillsByTime",
    "userRateLimit",
    "userRole",
    "userVaultEquities",
}

SECRETISH_FIELD_NAMES = {
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

ADDRESS_FIELD_NAMES = {
    "user",
    "vaultAddress",
    "vault_address",
}

BODY_SHAPE_READ_LIMIT_BYTES = 65536
JSON_READ_LIMIT_BYTES = 1048576


class HyperliquidReadonlyConfigError(ValueError):
    """Raised when a Hyperliquid read-only call would exceed the safe boundary."""


@dataclass(frozen=True)
class HyperliquidReadonlyHttpResult:
    url: str
    ok: bool
    status_code: int | None
    content_type: str
    body_shape: str
    error: str


def normalize_hyperliquid_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    raise HyperliquidReadonlyConfigError("Hyperliquid environment must be production/mainnet or testnet")


def default_api_endpoint(environment: str) -> str:
    if normalize_hyperliquid_environment(environment) == "TESTNET":
        return DEFAULT_HYPERLIQUID_TESTNET_API_ENDPOINT
    return DEFAULT_HYPERLIQUID_PRODUCTION_API_ENDPOINT


def api_endpoint_env_name(environment: str) -> str:
    return f"HYPERLIQUID_API_ENDPOINT_{normalize_hyperliquid_environment(environment)}"


def endpoint_from_env(value: str | None, default: str) -> str:
    return value.strip() if value and value.strip() else default


def validate_https_base_url(name: str, value: str) -> str:
    if not value:
        raise HyperliquidReadonlyConfigError(f"{name} is required for this Hyperliquid environment")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise HyperliquidReadonlyConfigError(f"{name} must use https")
    if not parsed.netloc:
        raise HyperliquidReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise HyperliquidReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise HyperliquidReadonlyConfigError(f"{name} must not include query string or fragment")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def validate_info_body(body: dict[str, object], *, private_readonly: bool = False) -> None:
    info_type = str(body.get("type") or "")
    allowed = PRIVATE_READONLY_INFO_TYPES if private_readonly else PUBLIC_INFO_TYPES
    if info_type not in allowed:
        scope = "private read-only" if private_readonly else "public"
        raise HyperliquidReadonlyConfigError(f"Hyperliquid {scope} info type is not allowlisted: {info_type}")
    _assert_no_secretish_fields(body)


def info_post(
    base_url: str,
    body: dict[str, object],
    timeout_seconds: float,
    *,
    private_readonly: bool = False,
) -> HyperliquidReadonlyHttpResult:
    base = validate_https_base_url("HYPERLIQUID_API_ENDPOINT", base_url)
    validate_info_body(body, private_readonly=private_readonly)
    url = urljoin(base.rstrip("/") + "/", "info")
    request = Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "perpdex-farming-bot-hyperliquid-readonly-smoke",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read(BODY_SHAPE_READ_LIMIT_BYTES)
            content_type = response.headers.get("Content-Type", "")
            status_code = response.getcode()
            return HyperliquidReadonlyHttpResult(
                url=url,
                ok=200 <= status_code < 300,
                status_code=status_code,
                content_type=content_type,
                body_shape=_body_shape(response_body, content_type),
                error="",
            )
    except HTTPError as exc:
        return HyperliquidReadonlyHttpResult(
            url=url,
            ok=False,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", ""),
            body_shape="not_read",
            error=exc.reason or "http_error",
        )
    except (TimeoutError, URLError) as exc:
        return HyperliquidReadonlyHttpResult(
            url=url,
            ok=False,
            status_code=None,
            content_type="",
            body_shape="not_read",
            error=exc.__class__.__name__,
        )


def info_post_json(
    base_url: str,
    body: dict[str, object],
    timeout_seconds: float,
    *,
    private_readonly: bool = False,
) -> object:
    base = validate_https_base_url("HYPERLIQUID_API_ENDPOINT", base_url)
    validate_info_body(body, private_readonly=private_readonly)
    request = Request(
        urljoin(base.rstrip("/") + "/", "info"),
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "perpdex-farming-bot-hyperliquid-readonly",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        response_body = response.read(JSON_READ_LIMIT_BYTES)
    return json.loads(response_body.decode("utf-8"))


def body_shape_from_payload(payload: object) -> str:
    if isinstance(payload, dict):
        keys = ",".join(sorted(str(key) for key in payload.keys())[:8])
        return f"json_object_keys={keys or 'none'}"
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            keys = ",".join(sorted(str(key) for key in payload[0].keys())[:8])
            return f"json_array_len={len(payload)} first_object_keys={keys or 'none'}"
        return f"json_array_len={len(payload)}"
    return f"json_{type(payload).__name__}"


def safe_info_body_shape(body: dict[str, object]) -> str:
    copied = _redact_addresses(body)
    return body_shape_from_payload(copied)


def _assert_no_secretish_fields(value: object, path: str = "body") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).replace("-", "_").lower()
            if normalized_key in SECRETISH_FIELD_NAMES:
                raise HyperliquidReadonlyConfigError(f"{path}.{key} must not be sent to /info")
            _assert_no_secretish_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secretish_fields(child, f"{path}[{index}]")


def _redact_addresses(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, child in value.items():
            if str(key) in ADDRESS_FIELD_NAMES and child:
                result[str(key)] = "[redacted]"
            else:
                result[str(key)] = _redact_addresses(child)
        return result
    if isinstance(value, list):
        return [_redact_addresses(item) for item in value]
    return value


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
    return body_shape_from_payload(payload)
