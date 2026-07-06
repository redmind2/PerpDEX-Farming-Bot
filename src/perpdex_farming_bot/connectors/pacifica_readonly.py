from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from perpdex_farming_bot.env import normalize_env_prefix


DEFAULT_PACIFICA_PRODUCTION_API_ENDPOINT = "https://api.pacifica.fi/api/v1"
DEFAULT_PACIFICA_TESTNET_API_ENDPOINT = "https://test-api.pacifica.fi/api/v1"
DEFAULT_PACIFICA_PRODUCTION_WSS_ENDPOINT = "wss://ws.pacifica.fi/ws"
DEFAULT_PACIFICA_TESTNET_WSS_ENDPOINT = "wss://test-ws.pacifica.fi/ws"

PUBLIC_GET_PATHS = {
    "/info",
    "/book",
    "/prices",
}

PRIVATE_READONLY_GET_PATHS = {
    "/account",
    "/orders",
    "/orders/history",
    "/orders/history_by_id",
    "/positions",
}

ALLOWED_QUERY_PARAMS = {
    "/info": set(),
    "/prices": set(),
    "/book": {"symbol", "agg_level"},
    "/account": {"account"},
    "/orders": {"account"},
    "/orders/history": {"account", "limit", "cursor"},
    "/orders/history_by_id": {"order_id"},
    "/positions": {"account"},
}

ALLOWED_AGG_LEVELS = {1, 10, 100, 1000, 10000}

SECRETISH_QUERY_NAMES = {
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

REDACTED_QUERY_NAMES = {
    "account",
    "signature",
    "token",
}

BODY_SHAPE_READ_LIMIT_BYTES = 65536
JSON_READ_LIMIT_BYTES = 1048576


class PacificaReadonlyConfigError(ValueError):
    """Raised when a Pacifica read-only check would exceed the current safety boundary."""


@dataclass(frozen=True)
class PacificaReadonlyHttpResult:
    url: str
    safe_url: str
    ok: bool
    status_code: int | None
    content_type: str
    body_shape: str
    error: str


def normalize_pacifica_environment(environment: str) -> str:
    normalized = normalize_env_prefix(environment)
    if normalized in {"MAINNET", "PROD", "PRODUCTION"}:
        return "PRODUCTION"
    if normalized in {"TEST", "TESTNET"}:
        return "TESTNET"
    raise PacificaReadonlyConfigError("Pacifica environment must be production/mainnet or testnet")


def default_api_endpoint(environment: str) -> str:
    if normalize_pacifica_environment(environment) == "TESTNET":
        return DEFAULT_PACIFICA_TESTNET_API_ENDPOINT
    return DEFAULT_PACIFICA_PRODUCTION_API_ENDPOINT


def default_wss_endpoint(environment: str) -> str:
    if normalize_pacifica_environment(environment) == "TESTNET":
        return DEFAULT_PACIFICA_TESTNET_WSS_ENDPOINT
    return DEFAULT_PACIFICA_PRODUCTION_WSS_ENDPOINT


def api_endpoint_env_name(environment: str) -> str:
    return f"PACIFICA_API_ENDPOINT_{normalize_pacifica_environment(environment)}"


def wss_endpoint_env_name(environment: str) -> str:
    return f"PACIFICA_WSS_ENDPOINT_{normalize_pacifica_environment(environment)}"


def endpoint_from_env(value: str | None, default: str) -> str:
    return value.strip() if value and value.strip() else default


def validate_https_base_url(name: str, value: str) -> str:
    if not value:
        raise PacificaReadonlyConfigError(f"{name} is required for this Pacifica environment")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise PacificaReadonlyConfigError(f"{name} must use https")
    if not parsed.netloc:
        raise PacificaReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise PacificaReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise PacificaReadonlyConfigError(f"{name} must not include query string or fragment")

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def validate_wss_url(name: str, value: str) -> str:
    if not value:
        raise PacificaReadonlyConfigError(f"{name} is required for this Pacifica environment")
    parsed = urlparse(value)
    if parsed.scheme != "wss":
        raise PacificaReadonlyConfigError(f"{name} must use wss")
    if not parsed.netloc:
        raise PacificaReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise PacificaReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise PacificaReadonlyConfigError(f"{name} must not include query string or fragment")
    return value.rstrip("/")


def validate_readonly_path(path: str, *, private_readonly: bool = False) -> str:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise PacificaReadonlyConfigError("Pacifica read-only paths must be relative paths, not full URLs")
    if not parsed.path.startswith("/"):
        raise PacificaReadonlyConfigError("Pacifica read-only paths must start with /")

    allowed = PRIVATE_READONLY_GET_PATHS if private_readonly else PUBLIC_GET_PATHS
    if parsed.path not in allowed:
        scope = "private read-only" if private_readonly else "public"
        raise PacificaReadonlyConfigError(f"Pacifica {scope} path is not allowlisted: {parsed.path}")
    if parsed.query or parsed.fragment:
        raise PacificaReadonlyConfigError("Pass query values separately, not inside the path")
    return parsed.path


def validate_query_params(path: str, query: dict[str, object]) -> None:
    allowed = ALLOWED_QUERY_PARAMS.get(path, set())
    for key, value in query.items():
        normalized_key = key.replace("-", "_").lower()
        if normalized_key in SECRETISH_QUERY_NAMES:
            raise PacificaReadonlyConfigError(f"query.{key} must not contain secret-like values")
        if key not in allowed:
            raise PacificaReadonlyConfigError(f"query.{key} is not allowlisted for {path}")
        _assert_query_value_is_simple(key, value)
        if key == "agg_level" and value not in ("", None):
            try:
                agg_level = int(str(value))
            except ValueError as exc:
                raise PacificaReadonlyConfigError("query.agg_level must be an integer") from exc
            if agg_level not in ALLOWED_AGG_LEVELS:
                allowed_text = ",".join(str(item) for item in sorted(ALLOWED_AGG_LEVELS))
                raise PacificaReadonlyConfigError(f"query.agg_level must be one of {allowed_text}")


def read_only_get(
    base_url: str,
    path: str,
    query: dict[str, object],
    timeout_seconds: float,
    *,
    private_readonly: bool = False,
) -> PacificaReadonlyHttpResult:
    base = validate_https_base_url("PACIFICA_API_ENDPOINT", base_url)
    path = validate_readonly_path(path, private_readonly=private_readonly)
    validate_query_params(path, query)
    url = _url_with_query(base, path, query)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "perpdex-farming-bot-pacifica-readonly-smoke",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(BODY_SHAPE_READ_LIMIT_BYTES)
            content_type = response.headers.get("Content-Type", "")
            status_code = response.getcode()
            return PacificaReadonlyHttpResult(
                url=url,
                safe_url=_safe_url(url),
                ok=200 <= status_code < 300,
                status_code=status_code,
                content_type=content_type,
                body_shape=_body_shape(body, content_type),
                error="",
            )
    except HTTPError as exc:
        return PacificaReadonlyHttpResult(
            url=url,
            safe_url=_safe_url(url),
            ok=False,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", ""),
            body_shape="not_read",
            error=exc.reason or "http_error",
        )
    except (TimeoutError, URLError) as exc:
        return PacificaReadonlyHttpResult(
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
) -> object:
    base = validate_https_base_url("PACIFICA_API_ENDPOINT", base_url)
    path = validate_readonly_path(path, private_readonly=private_readonly)
    validate_query_params(path, query)
    request = Request(
        _url_with_query(base, path, query),
        headers={
            "Accept": "application/json",
            "User-Agent": "perpdex-farming-bot-pacifica-readonly",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        response_body = response.read(JSON_READ_LIMIT_BYTES)
    return json.loads(response_body.decode("utf-8"))


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
    raise PacificaReadonlyConfigError(f"query.{key} must be a simple scalar or list of scalars")


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
