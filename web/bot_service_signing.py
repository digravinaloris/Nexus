"""Versioned HMAC authentication primitives for the internal BotService transport.

Nonce replay prevention is intentionally a separate durable-store operation. A
caller must atomically consume the returned (key_id, nonce) claim before dispatch.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Mapping


SIGNATURE_VERSION = "NEXUS-BOT-SERVICE-V1"
HEADER_KEY_ID = "X-Nexus-Key-Id"
HEADER_TIMESTAMP = "X-Nexus-Timestamp"
HEADER_NONCE = "X-Nexus-Nonce"
HEADER_SIGNATURE = "X-Nexus-Signature"
MAX_BODY_BYTES = 1_048_576
CLOCK_SKEW_SECONDS = 60

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_SIGNATURE_RE = re.compile(r"^[a-f0-9]{64}$")


class BotServiceAuthError(ValueError):
    """Raised when an internal BotService request cannot be authenticated."""


@dataclass(frozen=True)
class SignedRequestClaims:
    key_id: str
    timestamp: int
    nonce: str


def _validate_secret(secret: bytes) -> bytes:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("BotService HMAC secrets must contain at least 32 bytes")
    return secret


def _validate_target(method: str, path: str) -> tuple[str, str]:
    if not isinstance(method, str) or method.upper() != "POST":
        raise BotServiceAuthError("Invalid internal service authentication")
    if (
        not isinstance(path, str)
        or not path.startswith("/internal/v1/")
        or "?" in path
        or "#" in path
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise BotServiceAuthError("Invalid internal service authentication")
    return "POST", path


def _validate_body(body: bytes) -> bytes:
    if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
        raise BotServiceAuthError("Invalid internal service authentication")
    return body


def _canonical_message(
    key_id: str,
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
) -> bytes:
    body_digest = hashlib.sha256(body).hexdigest()
    fields = (
        SIGNATURE_VERSION,
        key_id,
        method,
        path,
        str(timestamp),
        nonce,
        body_digest,
    )
    return "\n".join(fields).encode("utf-8")


def sign_internal_request(
    *,
    secret: bytes,
    key_id: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build signed headers for one bounded POST request."""
    secret = _validate_secret(secret)
    method, path = _validate_target(method, path)
    body = _validate_body(body)
    if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
        raise ValueError("Invalid BotService key ID")

    issued_at = int(time.time()) if timestamp is None else timestamp
    if not isinstance(issued_at, int) or issued_at < 0:
        raise ValueError("Invalid BotService timestamp")
    request_nonce = secrets.token_urlsafe(24) if nonce is None else nonce
    if not isinstance(request_nonce, str) or not _NONCE_RE.fullmatch(request_nonce):
        raise ValueError("Invalid BotService nonce")

    canonical = _canonical_message(
        key_id, method, path, issued_at, request_nonce, body
    )
    signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return {
        HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: str(issued_at),
        HEADER_NONCE: request_nonce,
        HEADER_SIGNATURE: signature,
    }


def verify_internal_request(
    *,
    keyring: Mapping[str, bytes],
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    now: int | None = None,
    clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
) -> SignedRequestClaims:
    """Verify a request and return claims for a separate atomic replay check.

    The caller must persistently consume (key_id, nonce) before dispatching the
    operation. This function deliberately does not use process memory as a
    replay store.
    """
    method, path = _validate_target(method, path)
    body = _validate_body(body)
    if not isinstance(clock_skew_seconds, int) or clock_skew_seconds < 0:
        raise ValueError("Invalid BotService clock skew")

    key_id = headers.get(HEADER_KEY_ID, "")
    timestamp_text = headers.get(HEADER_TIMESTAMP, "")
    nonce = headers.get(HEADER_NONCE, "")
    signature = headers.get(HEADER_SIGNATURE, "")
    if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
        raise BotServiceAuthError("Invalid internal service authentication")
    if not isinstance(timestamp_text, str) or not re.fullmatch(r"0|[1-9][0-9]{0,11}", timestamp_text):
        raise BotServiceAuthError("Invalid internal service authentication")
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise BotServiceAuthError("Invalid internal service authentication")
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        raise BotServiceAuthError("Invalid internal service authentication")

    secret = keyring.get(key_id)
    if secret is None:
        raise BotServiceAuthError("Invalid internal service authentication")
    try:
        secret = _validate_secret(secret)
    except ValueError as exc:
        raise BotServiceAuthError("Invalid internal service authentication") from exc

    timestamp = int(timestamp_text)
    current_time = int(time.time()) if now is None else now
    if abs(current_time - timestamp) > clock_skew_seconds:
        raise BotServiceAuthError("Invalid internal service authentication")

    canonical = _canonical_message(key_id, method, path, timestamp, nonce, body)
    expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise BotServiceAuthError("Invalid internal service authentication")

    return SignedRequestClaims(key_id=key_id, timestamp=timestamp, nonce=nonce)
