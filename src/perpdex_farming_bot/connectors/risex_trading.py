from __future__ import annotations

import json
import base64
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from perpdex_farming_bot.connectors.risex_readonly import validate_https_base_url


JSON_READ_LIMIT_BYTES = 1048576


class RisexTradingConfigError(ValueError):
    """Raised when a RiseX live order request is unsafe or malformed."""


@dataclass(frozen=True)
class RisexPlaceOrderDraft:
    market_id: int
    size_steps: int
    price_ticks: int
    size_wad: int
    price_wad: int
    side: int
    post_only: bool
    reduce_only: bool
    stp_mode: int
    order_type: int
    time_in_force: int
    builder_id: int
    client_order_id: str
    ttl_units: int
    expiry: int = 0


@dataclass(frozen=True)
class RisexSignedPlaceOrder:
    body: dict[str, object]
    encoded_data_hash: str
    deadline: int
    nonce_anchor: str = ""
    nonce_bitmap_index: int = -1
    nonce: str = ""
    signing_mode: str = "verify_witness"


@dataclass(frozen=True)
class RisexPostResult:
    ok: bool
    status_code: int | None
    parsed: object | None
    body_shape: str
    error: str


def build_place_order_draft(
    *,
    market_id: int,
    size_steps: int,
    price_ticks: int,
    size_wad: int,
    price_wad: int,
    side: int,
    reduce_only: bool,
    client_order_id: str,
    post_only: bool = False,
    stp_mode: int = 0,
    order_type: int = 0,
    time_in_force: int = 3,
    builder_id: int = 0,
    ttl_units: int = 0,
    expiry: int = 0,
) -> RisexPlaceOrderDraft:
    if market_id <= 0:
        raise RisexTradingConfigError("market_id must be greater than zero")
    if size_steps <= 0:
        raise RisexTradingConfigError("size_steps must be greater than zero")
    if size_steps > 4_294_967_295:
        raise RisexTradingConfigError("size_steps exceeds RiseX uint32 limit")
    if price_ticks < 0:
        raise RisexTradingConfigError("price_ticks must be zero or greater")
    if size_wad <= 0:
        raise RisexTradingConfigError("size_wad must be greater than zero")
    if price_wad < 0:
        raise RisexTradingConfigError("price_wad must be zero or greater")
    if size_wad > 2**128 - 1:
        raise RisexTradingConfigError("size_wad exceeds RiseX uint128 limit")
    if price_wad > 2**128 - 1:
        raise RisexTradingConfigError("price_wad exceeds RiseX uint128 limit")
    if price_ticks > 16_777_215:
        raise RisexTradingConfigError("price_ticks exceeds RiseX uint24 limit")
    if side not in {0, 1}:
        raise RisexTradingConfigError("side must be 0=buy or 1=sell")
    if stp_mode not in {0, 1, 2}:
        raise RisexTradingConfigError("stp_mode must be 0, 1, or 2")
    if order_type not in {0, 1}:
        raise RisexTradingConfigError("order_type must be 0=market or 1=limit")
    if order_type == 1 and price_ticks <= 0:
        raise RisexTradingConfigError("limit orders require price_ticks greater than zero")
    if order_type == 1 and price_wad <= 0:
        raise RisexTradingConfigError("limit orders require price_wad greater than zero")
    if time_in_force not in {0, 1, 2, 3}:
        raise RisexTradingConfigError("time_in_force must be 0, 1, 2, or 3")
    if builder_id < 0 or builder_id > 65_535:
        raise RisexTradingConfigError("builder_id must fit uint16")
    if ttl_units < 0 or ttl_units > 65_535:
        raise RisexTradingConfigError("ttl_units must fit uint16")
    if expiry < 0 or expiry > 4_294_967_295:
        raise RisexTradingConfigError("expiry must fit uint32")
    if not client_order_id.isdigit():
        raise RisexTradingConfigError("client_order_id must be a uint64 decimal string")
    if int(client_order_id) > 18_446_744_073_709_551_615:
        raise RisexTradingConfigError("client_order_id must fit uint64")
    return RisexPlaceOrderDraft(
        market_id=market_id,
        size_steps=size_steps,
        price_ticks=price_ticks,
        size_wad=size_wad,
        price_wad=price_wad,
        side=side,
        post_only=post_only,
        reduce_only=reduce_only,
        stp_mode=stp_mode,
        order_type=order_type,
        time_in_force=time_in_force,
        builder_id=builder_id,
        client_order_id=client_order_id,
        ttl_units=ttl_units,
        expiry=expiry,
    )


def sign_place_order(
    *,
    draft: RisexPlaceOrderDraft,
    account: str,
    signer: str,
    signer_private_key: str,
    eip712_domain: dict[str, object],
    target_contract: str,
    nonce_anchor: str,
    nonce_bitmap_index: int,
    deadline_seconds: int | None = None,
) -> RisexSignedPlaceOrder:
    _assert_evm_address("account", account)
    _assert_evm_address("signer", signer)
    _assert_evm_address("target_contract", target_contract)
    if not signer_private_key:
        raise RisexTradingConfigError("signer_private_key is required")
    derived = Account.from_key(signer_private_key).address
    if derived.casefold() != signer.casefold():
        raise RisexTradingConfigError("signer private key does not match signer address")
    if nonce_bitmap_index < 0 or nonce_bitmap_index > 207:
        raise RisexTradingConfigError("nonce_bitmap_index must be between 0 and 207")

    deadline = deadline_seconds if deadline_seconds is not None else int(time.time()) + 300
    if deadline <= int(time.time()):
        raise RisexTradingConfigError("deadline must be in the future")

    encoded_hash = hash_place_order_action(draft)
    domain = _normalize_domain(eip712_domain)
    signable = encode_typed_data(
        domain_data=domain,
        message_types={
            "VerifyWitness": [
                {"name": "account", "type": "address"},
                {"name": "target", "type": "address"},
                {"name": "hash", "type": "bytes32"},
                {"name": "nonceAnchor", "type": "uint48"},
                {"name": "nonceBitmap", "type": "uint8"},
                {"name": "deadline", "type": "uint32"},
            ],
        },
        message_data={
            "account": account,
            "target": target_contract,
            "hash": encoded_hash,
            "nonceAnchor": int(nonce_anchor),
            "nonceBitmap": nonce_bitmap_index,
            "deadline": deadline,
        },
    )
    signature_bytes = Account.sign_message(signable, signer_private_key).signature
    signature = base64.b64encode(_signature_to_eip2098_compact(signature_bytes)).decode("ascii")

    body = {
        "market_id": draft.market_id,
        "size_steps": draft.size_steps,
        "price_ticks": draft.price_ticks,
        "side": draft.side,
        "post_only": draft.post_only,
        "reduce_only": draft.reduce_only,
        "stp_mode": draft.stp_mode,
        "order_type": draft.order_type,
        "time_in_force": draft.time_in_force,
        "builder_id": draft.builder_id,
        "client_order_id": draft.client_order_id,
        "ttl_units": draft.ttl_units,
        "permit": {
            "account": account,
            "signer": signer,
            "nonce_anchor": str(nonce_anchor),
            "nonce_bitmap_index": nonce_bitmap_index,
            "deadline": deadline,
            "signature": signature,
        },
        "no_retry": True,
    }
    return RisexSignedPlaceOrder(
        body=body,
        encoded_data_hash="0x" + encoded_hash.hex(),
        deadline=deadline,
        nonce_anchor=str(nonce_anchor),
        nonce_bitmap_index=nonce_bitmap_index,
        signing_mode="verify_witness_flat_v3",
    )


def sign_place_order_verify_signature(
    *,
    draft: RisexPlaceOrderDraft,
    account: str,
    signer: str,
    signer_private_key: str,
    eip712_domain: dict[str, object],
    target_contract: str,
    nonce: str,
    nonce_anchor: str = "",
    nonce_bitmap_index: int | None = None,
    deadline_seconds: int | None = None,
    request_shape: str = "flat",
) -> RisexSignedPlaceOrder:
    _assert_evm_address("account", account)
    _assert_evm_address("signer", signer)
    _assert_evm_address("target_contract", target_contract)
    if not signer_private_key:
        raise RisexTradingConfigError("signer_private_key is required")
    derived = Account.from_key(signer_private_key).address
    if derived.casefold() != signer.casefold():
        raise RisexTradingConfigError("signer private key does not match signer address")
    deadline = deadline_seconds if deadline_seconds is not None else int(time.time()) + 300
    if deadline <= int(time.time()):
        raise RisexTradingConfigError("deadline must be in the future")

    encoded_hash = hash_place_order_action(draft)
    domain = _normalize_domain(eip712_domain)
    signable = encode_typed_data(
        domain_data=domain,
        message_types={
            "VerifySignature": [
                {"name": "account", "type": "address"},
                {"name": "target", "type": "address"},
                {"name": "hash", "type": "bytes32"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        message_data={
            "account": account,
            "target": target_contract,
            "hash": encoded_hash,
            "nonce": int(nonce, 16) if nonce.startswith(("0x", "0X")) else int(nonce),
            "deadline": deadline,
        },
    )
    signature_bytes = Account.sign_message(signable, signer_private_key).signature

    if request_shape == "legacy":
        signature = signature_bytes.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        body = {
            "order_params": {
                "market_id": draft.market_id,
                "size": str(draft.size_wad),
                "price": str(draft.price_wad),
                "side": draft.side,
                "stp_mode": draft.stp_mode,
                "order_type": draft.order_type,
                "post_only": draft.post_only,
                "reduce_only": draft.reduce_only,
                "tif": draft.time_in_force,
                "time_in_force": draft.time_in_force,
                "timeInForce": draft.time_in_force,
                "expiry": draft.expiry,
            },
            "permit_params": {
                "account": account,
                "signer": signer,
                "deadline": str(deadline),
                "signature": signature,
                "nonce": str(nonce),
            },
        }
    elif request_shape == "flat":
        import base64

        signature = base64.b64encode(signature_bytes).decode("ascii")
        permit: dict[str, object] = {
            "account": account,
            "signer": signer,
            "nonce": str(nonce),
            "deadline": deadline,
            "signature": signature,
        }
        if nonce_anchor:
            permit["nonce_anchor"] = str(nonce_anchor)
        if nonce_bitmap_index is not None:
            permit["nonce_bitmap_index"] = nonce_bitmap_index
        body = {
            "market_id": draft.market_id,
            "size_steps": draft.size_steps,
            "price_ticks": draft.price_ticks,
            "side": draft.side,
            "post_only": draft.post_only,
            "reduce_only": draft.reduce_only,
            "stp_mode": draft.stp_mode,
            "order_type": draft.order_type,
            "time_in_force": draft.time_in_force,
            "builder_id": draft.builder_id,
            "client_order_id": draft.client_order_id,
            "ttl_units": draft.ttl_units,
            "permit": permit,
            "no_retry": True,
        }
    else:
        raise RisexTradingConfigError("request_shape must be flat or legacy")

    return RisexSignedPlaceOrder(
        body=body,
        encoded_data_hash="0x" + encoded_hash.hex(),
        deadline=deadline,
        nonce_anchor=str(nonce_anchor),
        nonce_bitmap_index=nonce_bitmap_index if nonce_bitmap_index is not None else -1,
        nonce=str(nonce),
        signing_mode=f"verify_signature_{request_shape}",
    )


def encode_place_order_data(draft: RisexPlaceOrderDraft) -> bytes:
    return encode_legacy_place_order_data(draft)


def encode_legacy_place_order_data(draft: RisexPlaceOrderDraft) -> bytes:
    flags = 0
    if draft.side == 1:
        flags |= 0x01
    if draft.post_only:
        flags |= 0x02
    if draft.reduce_only:
        flags |= 0x04
    flags |= draft.stp_mode << 3
    return b"".join(
        (
            draft.market_id.to_bytes(8, "big"),
            draft.size_wad.to_bytes(16, "big"),
            draft.price_wad.to_bytes(16, "big"),
            flags.to_bytes(1, "big"),
            draft.order_type.to_bytes(1, "big"),
            draft.time_in_force.to_bytes(1, "big"),
            draft.expiry.to_bytes(4, "big"),
        )
    )


def hash_place_order_action(draft: RisexPlaceOrderDraft) -> bytes:
    type_hash = keccak(text="RISE_PERPS_PLACE_ORDER_V1")
    side_flag = 1 if draft.side == 1 else 0
    order_type_flag = 1 if draft.order_type == 1 else 0
    flags = (
        side_flag
        | (2 if draft.post_only else 0)
        | (4 if draft.reduce_only else 0)
        | ((draft.stp_mode & 3) << 3)
        | ((order_type_flag & 1) << 5)
        | ((draft.time_in_force & 3) << 6)
    )
    order_data = (
        (int(draft.market_id) << 70)
        | (int(draft.size_steps) << 38)
        | (int(draft.price_ticks) << 14)
        | (int(flags) << 6)
        | (1 << 1)
    )
    encoded = b"".join(
        (
            _abi_bytes32(type_hash),
            _abi_uint(1, 8),
            _abi_uint(order_data, 88),
            _abi_uint(draft.builder_id, 16),
            _abi_uint(int(draft.client_order_id), 64),
            _abi_uint(draft.ttl_units, 16),
        )
    )
    return keccak(encoded)


def post_place_order(base_url: str, signed_order: RisexSignedPlaceOrder, timeout_seconds: float) -> RisexPostResult:
    base = validate_https_base_url("RISEX_API_ENDPOINT", base_url)
    url = urljoin(base.rstrip("/") + "/", "v1/orders/place")
    body = json.dumps(signed_order.body, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "perpdex-farming-bot-risex-live-test",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read(JSON_READ_LIMIT_BYTES)
            status_code = response.getcode()
            parsed = _parse_json_or_none(response_body)
            return RisexPostResult(
                ok=200 <= status_code < 300,
                status_code=status_code,
                parsed=parsed,
                body_shape=_body_shape(response_body),
                error="",
            )
    except HTTPError as exc:
        response_body = exc.read(JSON_READ_LIMIT_BYTES)
        return RisexPostResult(
            ok=False,
            status_code=exc.code,
            parsed=_parse_json_or_none(response_body),
            body_shape=_body_shape(response_body),
            error=exc.reason or "http_error",
        )
    except (TimeoutError, URLError) as exc:
        return RisexPostResult(
            ok=False,
            status_code=None,
            parsed=None,
            body_shape="not_read",
            error=exc.__class__.__name__,
        )


def safe_signed_order_summary(signed_order: RisexSignedPlaceOrder) -> dict[str, object]:
    body = signed_order.body
    order_params = body.get("order_params") if isinstance(body.get("order_params"), dict) else body
    permit = body.get("permit") if isinstance(body.get("permit"), dict) else body.get("permit_params")
    permit = permit if isinstance(permit, dict) else {}
    return {
        "signing_mode": signed_order.signing_mode,
        "request_shape": "official_docs" if "order_params" in body else "flat",
        "market_id": order_params.get("market_id"),
        "size_steps": order_params.get("size_steps"),
        "price_ticks": order_params.get("price_ticks"),
        "size_present": order_params.get("size") is not None,
        "price_present": order_params.get("price") is not None,
        "side": order_params.get("side"),
        "reduce_only": order_params.get("reduce_only"),
        "order_type": order_params.get("order_type"),
        "time_in_force": order_params.get("time_in_force") or order_params.get("tif"),
        "client_order_id": order_params.get("client_order_id"),
        "deadline": permit.get("deadline"),
        "nonce_anchor_present": permit.get("nonce_anchor") is not None,
        "nonce_bitmap_index": permit.get("nonce_bitmap_index"),
        "nonce_present": permit.get("nonce") is not None,
        "signature_present": bool(permit.get("signature")),
        "encoded_data_hash_present": bool(signed_order.encoded_data_hash),
    }


def extract_order_id(parsed: object | None) -> str:
    data = _data_payload(parsed)
    if not isinstance(data, dict):
        return ""
    for key in ("order_id", "orderId", "sc_order_id", "tx_hash"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _normalize_domain(domain: dict[str, object]) -> dict[str, object]:
    name = str(domain.get("name") or "")
    version = str(domain.get("version") or "")
    chain_id = domain.get("chain_id") or domain.get("chainId")
    verifying_contract = domain.get("verifying_contract") or domain.get("verifyingContract")
    if not name or not version or chain_id is None or not verifying_contract:
        raise RisexTradingConfigError("EIP712 domain is missing required fields")
    _assert_evm_address("verifying_contract", str(verifying_contract))
    return {
        "name": name,
        "version": version,
        "chainId": int(chain_id),
        "verifyingContract": str(verifying_contract),
    }


def _abi_bytes32(value: bytes) -> bytes:
    if len(value) != 32:
        raise RisexTradingConfigError("bytes32 value must be 32 bytes")
    return value


def _abi_uint(value: int, bits: int) -> bytes:
    if value < 0 or value >= 2**bits:
        raise RisexTradingConfigError(f"uint{bits} value out of range")
    return value.to_bytes(32, "big")


def _signature_to_eip2098_compact(signature: bytes) -> bytes:
    if len(signature) != 65:
        raise RisexTradingConfigError("expected 65-byte ECDSA signature")
    r = signature[:32]
    s_value = int.from_bytes(signature[32:64], "big")
    v = signature[64]
    y_parity = v - 27 if v >= 27 else v
    if y_parity not in {0, 1}:
        raise RisexTradingConfigError("signature v must be 27/28 or 0/1")
    y_parity_and_s = s_value | (y_parity << 255)
    return r + y_parity_and_s.to_bytes(32, "big")


def _assert_evm_address(name: str, value: str) -> None:
    if len(value) != 42 or not value.startswith(("0x", "0X")):
        raise RisexTradingConfigError(f"{name} must look like a 0x EVM address")
    if not all(character in "0123456789abcdefABCDEF" for character in value[2:]):
        raise RisexTradingConfigError(f"{name} must be hex")


def _parse_json_or_none(body: bytes) -> object | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _body_shape(body: bytes) -> str:
    if not body:
        return "empty"
    parsed = _parse_json_or_none(body)
    if isinstance(parsed, dict):
        keys = ",".join(sorted(str(key) for key in parsed.keys())[:8])
        return f"json_object_keys={keys or 'none'}"
    if isinstance(parsed, list):
        return f"json_array_len={len(parsed)}"
    return f"bytes={len(body)}"


def _data_payload(payload: object | None) -> object | None:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload
