from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_HIBACHI_API_ENDPOINT = "https://api.hibachi.xyz"
DEFAULT_HIBACHI_DATA_API_ENDPOINT = "https://data-api.hibachi.xyz"

FORBIDDEN_PATH_SEGMENTS = {
    "account",
    "balance",
    "cancel",
    "cancellations",
    "capital",
    "deposit",
    "deposits",
    "leverage",
    "margin",
    "modify",
    "order",
    "orders",
    "position",
    "positions",
    "rebalance",
    "replace",
    "session",
    "signature",
    "sign",
    "submit",
    "trade",
    "transfer",
    "transfers",
    "withdraw",
    "withdrawals",
}

ALLOWED_PUBLIC_PATHS = {
    "/market/exchange-info",
    "/market/inventory",
    "/market/data/prices",
    "/market/data/stats",
    "/market/data/trades",
    "/market/data/klines",
    "/market/data/open-interest",
    "/market/data/orderbook",
}


class HibachiReadonlyConfigError(ValueError):
    """Raised when a requested Hibachi smoke test is outside read-only bounds."""


@dataclass(frozen=True)
class ReadonlyHttpResult:
    url: str
    ok: bool
    status_code: int | None
    content_type: str
    body_shape: str
    error: str


def endpoint_from_env(value: str | None, default: str) -> str:
    return value.strip() if value and value.strip() else default


def validate_https_base_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise HibachiReadonlyConfigError(f"{name} must use https")
    if not parsed.netloc:
        raise HibachiReadonlyConfigError(f"{name} must include a host")
    if parsed.username or parsed.password:
        raise HibachiReadonlyConfigError(f"{name} must not include username or password")
    if parsed.query or parsed.fragment:
        raise HibachiReadonlyConfigError(f"{name} must not include query string or fragment")

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def parse_readonly_paths(raw: str) -> list[str]:
    paths: list[str] = []
    normalized = raw.replace(";", ",").replace("\n", ",")
    for chunk in normalized.split(","):
        path = chunk.strip()
        if not path:
            continue
        validate_readonly_path(path)
        paths.append(path)
    return paths


def validate_readonly_path(path: str) -> None:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        raise HibachiReadonlyConfigError("public read-only paths must be relative paths, not full URLs")
    if not path.startswith("/"):
        raise HibachiReadonlyConfigError("public read-only paths must start with /")
    if parsed.path not in ALLOWED_PUBLIC_PATHS:
        raise HibachiReadonlyConfigError(f"public path is not in the Hibachi read-only allowlist: {parsed.path}")

    for segment in parsed.path.lower().split("/"):
        if not segment:
            continue
        if segment in FORBIDDEN_PATH_SEGMENTS:
            raise HibachiReadonlyConfigError(f"path segment is blocked for the current smoke step: {segment}")


def public_get(base_url: str, path: str, timeout_seconds: float) -> ReadonlyHttpResult:
    base = validate_https_base_url("HIBACHI_DATA_API_ENDPOINT_PRODUCTION", base_url)
    validate_readonly_path(path)
    url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "perpdex-farming-bot-readonly-smoke",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(8192)
            content_type = response.headers.get("Content-Type", "")
            status_code = response.getcode()
            return ReadonlyHttpResult(
                url=url,
                ok=200 <= status_code < 300,
                status_code=status_code,
                content_type=content_type,
                body_shape=_body_shape(body, content_type),
                error="",
            )
    except HTTPError as exc:
        return ReadonlyHttpResult(
            url=url,
            ok=False,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", ""),
            body_shape="not_read",
            error=exc.reason or "http_error",
        )
    except (TimeoutError, URLError) as exc:
        return ReadonlyHttpResult(
            url=url,
            ok=False,
            status_code=None,
            content_type="",
            body_shape="not_read",
            error=exc.__class__.__name__,
        )


def _body_shape(body: bytes, content_type: str) -> str:
    if not body:
        return "empty"
    if "json" not in content_type.lower():
        return f"non_json_bytes={len(body)}"

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
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
