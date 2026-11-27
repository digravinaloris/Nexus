"""Signed HTTP client for the opt-in internal Nexus BotService endpoint.

Only explicit read-only BotService snapshots are transported. Additional
methods must be added alongside endpoint allowlisting and DTO tests.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import requests

from web.bot_service import BotService
from web.bot_service_signing import sign_internal_request


INTERNAL_DISPATCH_PATH = "/internal/v1/dispatch"
DEFAULT_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 65_536


class BotServiceTransportError(RuntimeError):
    """Raised when the signed internal BotService cannot be reached safely."""


class SignedHttpBotService(BotService):
    """Partial remote BotService adapter for allowlisted read-only snapshots."""

    def __init__(
        self,
        *,
        base_url: str,
        key_id: str,
        secret: bytes,
        session=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        parsed = urlsplit(base_url) if isinstance(base_url, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("BotService URL must be an HTTPS origin")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("BotService key ID is required")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("BotService HMAC secret must contain at least 32 bytes")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("BotService timeout must be positive")

        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("BotService URL must be an HTTPS origin") from exc
        self._base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._key_id = key_id
        self._secret = secret
        self._session = session if session is not None else requests.Session()
        self._timeout = float(timeout)

    def _dispatch_snapshot(self, method: str):
        if method not in {"health_snapshot", "public_status_snapshot"}:
            raise ValueError("Unsupported internal BotService operation")
        body = json.dumps(
            {"method": method, "args": [], "kwargs": {}},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        headers = sign_internal_request(
            secret=self._secret,
            key_id=self._key_id,
            method="POST",
            path=INTERNAL_DISPATCH_PATH,
            body=body,
        )
        headers["Content-Type"] = "application/json"

        try:
            response = self._session.post(
                f"{self._base_url}{INTERNAL_DISPATCH_PATH}",
                data=body,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise BotServiceTransportError(
                "Internal BotService request failed"
            ) from exc
        except Exception as exc:
            raise BotServiceTransportError(
                "Internal BotService request failed"
            ) from exc

        if getattr(response, "status_code", None) != 200:
            raise BotServiceTransportError("Internal BotService returned an error")

        try:
            content_length = getattr(response, "headers", {}).get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        raise BotServiceTransportError(
                            "Internal BotService response exceeded the size limit"
                        )
                except ValueError:
                    pass

            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise BotServiceTransportError(
                        "Internal BotService response exceeded the size limit"
                    )
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
        except BotServiceTransportError:
            raise
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            raise BotServiceTransportError(
                "Internal BotService returned an invalid response"
            ) from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("result"), dict)
        ):
            raise BotServiceTransportError(
                "Internal BotService returned an invalid response"
            )
        return payload["result"]


    def health_snapshot(self):
        return self._dispatch_snapshot("health_snapshot")

    def public_status_snapshot(self):
        return self._dispatch_snapshot("public_status_snapshot")
