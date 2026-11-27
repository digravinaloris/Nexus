"""Signed HTTP client for the opt-in internal Nexus BotService endpoint.

Only explicit read-only BotService snapshots are transported. Additional
methods must be added alongside endpoint allowlisting and DTO tests.
"""

from __future__ import annotations

import json
import re
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

    @staticmethod
    def _valid_guild_id(guild_id):
        return (
            isinstance(guild_id, str)
            and re.fullmatch(r"[0-9]{1,20}", guild_id) is not None
            and int(guild_id) > 0
            and int(guild_id) <= (2**64 - 1)
        )

    def _dispatch_snapshot(self, method: str, args=()):
        allowed_argument_counts = {
            "health_snapshot": 0,
            "public_status_snapshot": 0,
            "guild_presence": 1,
            "guild_name": 1,
            "api_text_channels_snapshot": 1,
            "api_roles_snapshot": 1,
            "api_faq_snapshot": 1,
            "api_members_snapshot": 1,
        }
        if method not in allowed_argument_counts:
            raise ValueError("Unsupported internal BotService operation")
        if not isinstance(args, (list, tuple)) or len(args) != allowed_argument_counts[method]:
            raise ValueError("Invalid internal BotService arguments")
        if method in {"guild_presence", "guild_name", "api_text_channels_snapshot", "api_roles_snapshot", "api_faq_snapshot", "api_members_snapshot"} and not self._valid_guild_id(args[0]):
            raise ValueError("Invalid guild ID")

        body = json.dumps(
            {"method": method, "args": list(args), "kwargs": {}},
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
            or "result" not in payload
        ):
            raise BotServiceTransportError(
                "Internal BotService returned an invalid response"
            )

        result = payload["result"]
        if method in {"health_snapshot", "public_status_snapshot"}:
            if not isinstance(result, dict):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "guild_presence":
            if (
                not isinstance(result, dict)
                or set(result) != {"bot_ready", "guild_present"}
                or any(not isinstance(value, bool) for value in result.values())
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "guild_name":
            if not isinstance(result, str) or not result or len(result) > 256:
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method in {"api_text_channels_snapshot", "api_roles_snapshot"}:
            if result is not None and (
                not isinstance(result, list)
                or len(result) > 500
                or not all(
                    isinstance(item, dict)
                    and set(item) == {"id", "name"}
                    and isinstance(item["id"], str)
                    and re.fullmatch(r"[0-9]{1,20}", item["id"])
                    and 0 < int(item["id"]) <= (2**64 - 1)
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                    for item in result
                )
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_faq_snapshot":
            if result is not None:
                if (
                    not isinstance(result, dict)
                    or set(result) != {"enabled", "entries"}
                    or not isinstance(result["enabled"], bool)
                    or not isinstance(result["entries"], list)
                    or len(result["entries"]) > 500
                ):
                    raise BotServiceTransportError("Internal BotService returned an invalid response")
                for entry in result["entries"]:
                    if (
                        not isinstance(entry, dict)
                        or set(entry) != {"id", "keywords", "response"}
                        or not isinstance(entry["id"], str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry["id"]) is None
                        or not isinstance(entry["keywords"], list)
                        or not 1 <= len(entry["keywords"]) <= 25
                        or any(not isinstance(word, str) or not word.strip() or len(word) > 100 for word in entry["keywords"])
                        or not isinstance(entry["response"], str)
                        or not entry["response"].strip()
                        or len(entry["response"]) > 2000
                    ):
                        raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_members_snapshot":
            if result is not None and (
                not isinstance(result, list)
                or len(result) > 500
                or not all(
                    isinstance(member, dict)
                    and set(member) == {"id", "name", "username", "avatar_url", "is_muted"}
                    and self._valid_guild_id(member["id"])
                    and isinstance(member["name"], str)
                    and 1 <= len(member["name"]) <= 100
                    and isinstance(member["username"], str)
                    and 1 <= len(member["username"]) <= 100
                    and isinstance(member["avatar_url"], str)
                    and len(member["avatar_url"]) <= 2048
                    and member["avatar_url"].startswith("https://cdn.discordapp.com/")
                    and isinstance(member["is_muted"], bool)
                    for member in result
                )
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        return result

    def health_snapshot(self):
        return self._dispatch_snapshot("health_snapshot")

    def public_status_snapshot(self):
        return self._dispatch_snapshot("public_status_snapshot")

    def guild_presence(self, guild_id: str):
        return self._dispatch_snapshot("guild_presence", (guild_id,))

    def guild_name(self, guild_id: str):
        return self._dispatch_snapshot("guild_name", (guild_id,))

    def api_text_channels_snapshot(self, guild_id: str):
        return self._dispatch_snapshot("api_text_channels_snapshot", (guild_id,))

    def api_roles_snapshot(self, guild_id: str):
        return self._dispatch_snapshot("api_roles_snapshot", (guild_id,))

    def api_faq_snapshot(self, guild_id: str):
        return self._dispatch_snapshot("api_faq_snapshot", (guild_id,))

    def api_members_snapshot(self, guild_id: str):
        return self._dispatch_snapshot("api_members_snapshot", (guild_id,))
