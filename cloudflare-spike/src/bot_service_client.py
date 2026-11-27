"""Cloudflare-native signed BotService transport using Workers Fetch via FFI.

This module is isolated to the local Worker spike. It deliberately avoids
requests/socket-based transports and uses the Workers Fetch API from inside a
request handler.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from js import Uint8Array
from pyodide.ffi import run_sync
from workers import fetch


INTERNAL_DISPATCH_PATH = "/internal/v1/dispatch"
MAX_RESPONSE_BYTES = 65_536
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")


class CloudflareBotServiceError(RuntimeError):
    """Raised when a signed Worker-to-BotService request fails closed."""


def _read_bounded_response(response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise CloudflareBotServiceError("BotService response exceeded the size limit")
        except ValueError:
            pass

    body = response.body
    if body is None:
        raise CloudflareBotServiceError("BotService returned an empty response")
    reader = body.getReader()
    chunks = []
    total = 0
    while True:
        part = run_sync(reader.read())
        if bool(part.done):
            break
        chunk = Uint8Array.new(part.value).to_bytes()
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            run_sync(reader.cancel())
            raise CloudflareBotServiceError("BotService response exceeded the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


class CloudflareFetchBotService:
    """Partial Cloudflare Worker client for explicit read-only snapshots."""

    def __init__(
        self,
        *,
        base_url: str,
        key_id: str,
        secret: bytes,
        allow_local_http: bool = False,
    ):
        parsed = urlsplit(base_url) if isinstance(base_url, str) else None
        local_http = (
            parsed is not None
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and allow_local_http
        )
        if (
            parsed is None
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not local_http)
        ):
            raise ValueError("BotService URL must be an HTTPS origin")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("BotService URL must be an HTTPS origin") from exc
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise ValueError("Invalid BotService key ID")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("BotService HMAC secret must contain at least 32 bytes")

        self._base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._key_id = key_id
        self._secret = secret

    def _dispatch(self, method: str, args: list, expected_type: type):
        if method in {"health_snapshot", "public_status_snapshot"}:
            if args != [] or expected_type is not dict:
                raise ValueError("Invalid snapshot BotService arguments")
        elif method in {"is_guild_owner", "is_guild_admin"}:
            if (
                expected_type is not bool
                or len(args) != 2
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) for value in args)
            ):
                raise ValueError("Invalid guild owner check arguments")
        else:
            raise ValueError("Unsupported internal BotService operation")

        body = json.dumps(
            {"method": method, "args": args, "kwargs": {}},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        if not _NONCE_RE.fullmatch(nonce):
            raise CloudflareBotServiceError("Could not create an internal request nonce")

        canonical = "\n".join(
            (
                "NEXUS-BOT-SERVICE-V1",
                self._key_id,
                "POST",
                INTERNAL_DISPATCH_PATH,
                timestamp,
                nonce,
                hashlib.sha256(body).hexdigest(),
            )
        ).encode("utf-8")
        signature = hmac.new(self._secret, canonical, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Nexus-Key-Id": self._key_id,
            "X-Nexus-Timestamp": timestamp,
            "X-Nexus-Nonce": nonce,
            "X-Nexus-Signature": signature,
        }

        try:
            response = run_sync(
                fetch(
                    f"{self._base_url}{INTERNAL_DISPATCH_PATH}",
                    method="POST",
                    headers=headers,
                    body=body.decode("utf-8"),
                    redirect="manual",
                    cache="no-store",
                )
            )
            if int(response.status) != 200:
                raise CloudflareBotServiceError("Internal BotService returned an error")
            raw_payload = _read_bounded_response(response)
            payload = json.loads(raw_payload.decode("utf-8"))
        except CloudflareBotServiceError:
            raise
        except Exception as exc:
            raise CloudflareBotServiceError("Internal BotService request failed") from exc

        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("result"), expected_type)
        ):
            raise CloudflareBotServiceError("Internal BotService returned an invalid response")
        return payload["result"]

    def _dispatch_snapshot(self, method: str) -> dict:
        return self._dispatch(method, [], dict)

    def health_snapshot(self) -> dict:
        return self._dispatch_snapshot("health_snapshot")

    def public_status_snapshot(self) -> dict:
        return self._dispatch_snapshot("public_status_snapshot")

    def is_guild_owner(self, guild_id: str, user_id: str) -> bool:
        return self._dispatch("is_guild_owner", [guild_id, user_id], bool)
