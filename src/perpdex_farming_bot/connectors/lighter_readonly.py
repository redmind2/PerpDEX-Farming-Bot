from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from perpdex_farming_bot.env import normalize_env_prefix


DEFAULT_LIGHTER_PRODUCTION_API_ENDPOINT = "https://mainnet.zklighter.elliot.ai"
DEFAULT_LIGHTER_TESTNET_API_ENDPOINT = ""
DEFAULT_LIGHTER_PRODUCTION_WSS_ENDPOINT = "wss://mainnet.zklighter.elliot.ai/stream"
DEFAULT_LIGHTER_TESTNET_WSS_ENDPOINT = "wss://testnet.zklighter.elliot.ai/stream"

PUBLIC_GET_PATHS = {
    "/",
    "/api/v1/assetDetails",
    "/api/v1/currentHeight",
    "/api/v1/exchangeStats",
    "/api/v1/funding-rates",
    "/api/v1/orderBookDetails",
    "/api/v1/orderBookOrders",
    "/api/v1/orderBooks",
    "/api/v1/recentTrades",
}

PRIVATE_READONLY_GET_PATHS = {
    "/api/v1/account",
    "/api/v1/accountActiveOrders",
    "/api/v1/accountInactiveOrders",
    "/api/v1/accountLimits",
    "/api/v1/accountMetadata",
    "/api/v1/accountTxs",
    "/api/v1/accountsByL1Address",
    "/api/v1/apikeys",
    "/api/v1/pnl",
    "/api/v1/positionFunding",
    "/api/v1/trades",
}

ALLOWED_QUERY_PARAMS = {
    "/": set(),
    "/api/v1/account": {"active_only", "by", "cursor", "value"},
    "/api/v1/accountActiveOrders": {"account_index", "market_id", "market_type"},
    "/api/v1/accountInactiveOrders": {
        "account_index",
        "ask_filter",
        "between_timestamps",
        "cursor",
        "limit",
        "market_id",
        "market_type",
    },
    "/api/v1/accountLimits": {"account_index"},
    "/api/v1/accountMetadata": {"by", "cursor", "value"},
    "/api/v1/accountTxs": {"by", "index", "limit", "types", "value"},
    "/api/v1/accountsByL1Address": {"cursor", "l1_address"},
    "/api/v1/apikeys": {"account_index", "api_key_index"},
    "/api/v1/assetDetails": {"asset_id"},
    "/api/v1/currentHeight": set(),
    "/api/v1/exchangeStats": set(),
    "/api/v1/funding-rates": set(),
    "/api/v1/orderBookDetails": {"filter", "market_id"},
    "/api/v1/orderBookOrders": {"limit", "market_id"},
    "/api/v1/orderBooks": {"filter", "market_id"},
    "/api/v1/pnl": {
        "by",
        "count_back",
        "end_timestamp",
        "ignore_transfers",
        "resolution",
        "start_timestamp",
        "value",
    },
    "/api/v1/positionFunding": {
        "account_index",
        "cursor",
        "end_timestamp",
        "limit",
        "market_id",
        "side",
        "start_timestamp",
    },
    "/api/v1/recentTrades": {"limit", "market_id"},
    "/api/v1/trades": {
        "account_index",
        "aggregate",
        "ask_filter",
        "cursor",
        "from",
        "limit",
        "market_id",
        "market_type",
        "order_index",
        "role",
        "skip_ask_order_id",
        "skip_bid_order_id",
        "sort_by",
        "sort_dir",
        "type",
    },
}

SECRETISH_QUERY_NAMES = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "key",
    "private_key",
    "privatekey",
    "secret",
    "secret_key",
    "session",
    "session_key",
    "signature",
    "signer_private_key",
    "token",
}

REDACTED_QUERY_NAMES = {
    "account_index",
    "l1_address",
    "value",
}

BODY_SHAPE_READ_LIMIT_BYTES = 65536
JSON_READ_LIMIT_BYTES = 1048576


class LighterReadonlyConfigError(ValueError):
    """Raised when a Lighter read-only call would exceed the safe Phase 0 boundary."""


@dataclass(frozen=True)
class LighterReadonlyHttpResult:
    url: str
    safe_url: str
    ok: bool
    status_code: int | None
    content_type: str
    body_shape: str
    error: str


def normalize_lighter_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    raise LighterReadonlyConfigError("Lighter environment must be production/mainnet or testnet")


def default_api_endpoint(environment: str) -> str:
    if normalize_lighter_environment(environment) == "TESTNET":
        return DEFAULT_LIGHTER_TESTNET_API_ENDPOINT
    return DEFAULT_LIGHTER_PRODUCTION_API_ENDPOINT


def default_wss_endpoint(environment: str) -> str:
    if normalize_lighter_environment(environment) == "TESTNET":
        return DEFAULT_LIGHTER_TESTNET_WSS_ENDPOINT
    return DEFAULT_LIGHTER_PRODUCTION_WSS_ENDPOINT


def api_endpoint_env_name(environment: str) -> str:
    return f"LIGHTER_API_ENDPOINT_{normalize_lighter_environment(environment)}"


def wss_endpoint_env_name(environment: str) -> str:
    return f"LIGHTER_WSS_ENDPOINT_{normalize_lighter_environment(environment)}"


def endpoint_from_env(value: str | None, default: str) -> str:
    return value.strip() if value and value.strip() else default


def validate_https_base_url(name: str, value: str) -> str:
    if not value:
        raise LighterReadonlyConfigError(f"{name} is required for this Lighter environment")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise LighterReadonlyConfigError(f"{name} must use https")
    if not parsed.netloc:
        raise LighterReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise LighterReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise LighterReadonlyConfigError(f"{name} must not include query string or fragment")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def validate_wss_url(name: str, value: str) -> str:
    if not value:
        raise LighterReadonlyConfigError(f"{name} is required for this Lighter environment")
    parsed = urlparse(value)
    if parsed.scheme != "wss":
        raise LighterReadonlyConfigError(f"{name} must use wss")
    if not parsed.netloc:
        raise LighterReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise LighterReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise LighterReadonlyConfigError(f"{name} must not include query string or fragment")
    return value.rstrip("/")


def validate_readonly_path(path: str, *, private_readonly: bool = False) -> str:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise LighterReadonlyConfigError("Lighter read-only paths must be relative paths, not full URLs")
    if not parsed.path.startswith("/"):
        raise LighterReadonlyConfigError("Lighter read-only paths must start with /")
    if parsed.query or parsed.fragment:
        raise LighterReadonlyConfigError("Pass query values separately, not inside the path")

    allowed = PRIVATE_READONLY_GET_PATHS if private_readonly else PUBLIC_GET_PATHS
    if parsed.path not in allowed:
        scope = "private read-only" if private_readonly else "public"
        raise LighterReadonlyConfigError(f"Lighter {scope} path is not allowlisted: {parsed.path}")
    return parsed.path


def validate_query_params(path: str, query: dict[str, object]) -> None:
    allowed = ALLOWED_QUERY_PARAMS.get(path, set())
    for key, value in query.items():
        normalized_key = key.replace("-", "_").lower()
        if normalized_key in SECRETISH_QUERY_NAMES:
            raise LighterReadonlyConfigError(f"query.{key} must not contain secret-like values")
        if key not in allowed:
            raise LighterReadonlyConfigError(f"query.{key} is not allowlisted for {path}")
        _assert_query_value_is_simple(key, value)


def read_only_get(
    base_url: str,
    path: str,
    query: dict[str, object],
    timeout_seconds: float,
    *,
    private_readonly: bool = False,
    read_only_auth_token: str = "",
) -> LighterReadonlyHttpResult:
    base = validate_https_base_url("LIGHTER_API_ENDPOINT", base_url)
    path = validate_readonly_path(path, private_readonly=private_readonly)
    validate_query_params(path, query)
    url = _url_with_query(base, path, query)
    request = Request(
        url,
        headers=_headers(read_only_auth_token, "perpdex-farming-bot-lighter-readonly-smoke"),
        method="GET",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(BODY_SHAPE_READ_LIMIT_BYTES)
            content_type = response.headers.get("Content-Type", "")
            status_code = response.getcode()
            return LighterReadonlyHttpResult(
                url=url,
                safe_url=_safe_url(url),
                ok=200 <= status_code < 300,
                status_code=status_code,
                content_type=content_type,
                body_shape=_body_shape(body, content_type),
                error="",
            )
    except HTTPError as exc:
        return LighterReadonlyHttpResult(
            url=url,
            safe_url=_safe_url(url),
            ok=False,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", ""),
            body_shape="not_read",
            error=exc.reason or "http_error",
        )
    except (TimeoutError, URLError) as exc:
        return LighterReadonlyHttpResult(
            url=url,
            safe_url=_safe_url(url),
            ok=False,
            status_code=None,
            content_type="",
            body_shape="not_read",
            error=exc.__class__.__name__,
        )


def read_only_get_json(
    base_url: str,
    path: str,
    query: dict[str, object],
    timeout_seconds: float,
    *,
    private_readonly: bool = False,
    read_only_auth_token: str = "",
) -> object:
    base = validate_https_base_url("LIGHTER_API_ENDPOINT", base_url)
    path = validate_readonly_path(path, private_readonly=private_readonly)
    validate_query_params(path, query)
    request = Request(
        _url_with_query(base, path, query),
        headers=_headers(read_only_auth_token, "perpdex-farming-bot-lighter-readonly"),
        method="GET",
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


def _headers(read_only_auth_token: str, user_agent: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    token = read_only_auth_token.strip()
    if token:
        if "\n" in token or "\r" in token:
            raise LighterReadonlyConfigError("LIGHTER_READ_ONLY_AUTH_TOKEN must be a single line")
        headers["Authorization"] = token
    return headers


def _url_with_query(base_url: str, path: str, query: dict[str, object]) -> str:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    normalized: list[tuple[str, str]] = []
    for key, value in query.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            normalized.extend((key, str(item)) for item in value)
        else:
            normalized.append((key, str(value)))
    if not normalized:
        return url
    return f"{url}?{urlencode(normalized)}"


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    safe_pairs: list[tuple[str, str]] = []
    for chunk in parsed.query.split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        if key in REDACTED_QUERY_NAMES and value:
            safe_pairs.append((key, "[redacted]"))
        else:
            safe_pairs.append((key, value))
    safe_query = "&".join(f"{key}={value}" for key, value in safe_pairs)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", safe_query, ""))


def _assert_query_value_is_simple(key: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, (list, tuple)) and all(isinstance(item, (str, int, float, bool)) for item in value):
        return
    raise LighterReadonlyConfigError(f"query.{key} must be a simple scalar or list of scalars")


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
