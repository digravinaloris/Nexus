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
        valid_id = lambda value: (
            isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) is not None
        )
        if method in {"health_snapshot", "public_status_snapshot", "api_guild_id_candidates"}:
            expected = dict if method != "api_guild_id_candidates" else list
            if args != [] or expected_type != expected:
                raise ValueError("Invalid snapshot BotService arguments")
        elif method in {"is_guild_owner", "is_guild_admin"}:
            if (
                expected_type is not bool
                or len(args) != 2
                or not all(valid_id(value) for value in args)
            ):
                raise ValueError("Invalid guild owner check arguments")
        elif method in {"guild_presence", "guild_name"}:
            expected = dict if method == "guild_presence" else str
            if expected_type != expected or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid guild read arguments")
        elif method in {"api_health_snapshot", "api_guild_list_snapshot"}:
            expected = dict if method == "api_health_snapshot" else (list, type(None))
            if expected_type != expected or len(args) > 1 or (args and not valid_id(args[0])):
                raise ValueError("Invalid API snapshot arguments")
        elif method == "available_guild_ids":
            if (
                expected_type is not list
                or len(args) != 1
                or not isinstance(args[0], list)
                or len(args[0]) > 500
                or not all(valid_id(value) for value in args[0])
            ):
                raise ValueError("Invalid available guild ID arguments")
        elif method in {"api_text_channels_snapshot", "api_roles_snapshot"}:
            if (
                expected_type != (list, type(None))
                or len(args) != 1
                or not valid_id(args[0])
            ):
                raise ValueError("Invalid guild collection snapshot arguments")
        elif method == "api_guild_stats_snapshot":
            if (
                expected_type != (dict, type(None))
                or len(args) not in {1, 2}
                or not valid_id(args[0])
                or (
                    len(args) == 2
                    and args[1] is not None
                    and not valid_id(args[1])
                )
            ):
                raise ValueError("Invalid guild stats snapshot arguments")
        elif method == "api_faq_snapshot":
            if expected_type != (dict, type(None)) or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid FAQ snapshot arguments")
        elif method in {"api_members_snapshot", "api_rest_config_snapshot"}:
            expected = (list, type(None)) if method == "api_members_snapshot" else dict
            if expected_type != expected or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid snapshot arguments")
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
        result = payload["result"]
        if method in {"api_guild_id_candidates", "available_guild_ids"} and (
            len(result) > (2000 if method == "api_guild_id_candidates" else 500)
            or not all(valid_id(value) for value in result)
        ):
            raise CloudflareBotServiceError("Internal BotService returned invalid guild IDs")
        if method == "guild_presence" and (
            set(result) != {"bot_ready", "guild_present"}
            or any(not isinstance(value, bool) for value in result.values())
        ):
            raise CloudflareBotServiceError("Internal BotService returned invalid guild presence")
        if method == "guild_name" and (not result or len(result) > 256):
            raise CloudflareBotServiceError("Internal BotService returned an invalid guild name")
        if method == "api_guild_list_snapshot" and result is not None and (
            len(result) > 500 or not all(isinstance(value, dict) for value in result)
        ):
            raise CloudflareBotServiceError("Internal BotService returned an invalid guild list")
        if (
            method in {"api_text_channels_snapshot", "api_roles_snapshot"}
            and result is not None
            and (
                len(result) > 500
                or not all(
                    isinstance(item, dict)
                    and set(item) == {"id", "name"}
                    and valid_id(item["id"])
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                    for item in result
                )
            )
        ):
            raise CloudflareBotServiceError(
                "Internal BotService returned invalid guild collection"
            )
        if method == "api_members_snapshot" and result is not None:
            expected_member_fields = {"id", "name", "username", "avatar_url", "is_muted"}
            if (
                not isinstance(result, list)
                or len(result) > 500
                or not all(
                    isinstance(member, dict)
                    and set(member) == expected_member_fields
                    and valid_id(member["id"])
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
                raise CloudflareBotServiceError(
                    "Internal BotService returned invalid members snapshot"
                )
        if method == "api_faq_snapshot" and result is not None:
            if (
                not isinstance(result, dict)
                or set(result) != {"enabled", "entries"}
                or not isinstance(result.get("enabled"), bool)
                or not isinstance(result.get("entries"), list)
                or len(result["entries"]) > 500
                or not all(
                    isinstance(entry, dict)
                    and set(entry) == {"id", "keywords", "response"}
                    and isinstance(entry["id"], str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry["id"])
                    and isinstance(entry["keywords"], list)
                    and 1 <= len(entry["keywords"]) <= 25
                    and all(
                        isinstance(keyword, str)
                        and keyword.strip()
                        and len(keyword) <= 100
                        for keyword in entry["keywords"]
                    )
                    and isinstance(entry["response"], str)
                    and entry["response"].strip()
                    and len(entry["response"]) <= 2000
                    for entry in result["entries"]
                )
                or len(
                    json.dumps(
                        result, separators=(",", ":"), ensure_ascii=True
                    ).encode("utf-8")
                ) > 60 * 1024
            ):
                raise CloudflareBotServiceError(
                    "Internal BotService returned invalid FAQ snapshot"
                )
        if method == "api_rest_config_snapshot":
            allowed_fields = {
                "guild_id", "logs_channel", "autorole", "allowed_roles",
                "automod_exempt_role_ids", "command_roles", "command_templates",
                "language", "log_dashboard_actions", "log_webhook_channel_id",
                "tickets_enabled", "ticket_category_id", "ticket_support_role_id",
                "ticket_archive_category_id", "ticket_panels", "appeal_channel_id",
                "boost_channel_id", "detailed_logs_enabled", "banned_words",
                "antinuke_enabled", "antinuke_threshold", "weekly_digest_enabled",
                "last_digest_sent_at", "member_count_channel_id", "min_account_age_days",
                "banned_domains", "anti_link_global_enabled", "welcome_channel_id",
                "welcome_banner_enabled", "verification_enabled", "verified_role_id",
                "verification_autorole_id", "unverified_role_id", "quarantine_enabled",
                "quarantine_role_id", "quarantine_channel_id", "quarantine_hold_minutes",
                "quarantine_suspicion_days", "anti_mention_spam_enabled",
                "mention_spam_threshold", "anti_media_flood_enabled", "media_flood_count",
                "media_flood_window", "auto_slowmode_enabled", "auto_slowmode_threshold",
                "auto_slowmode_window", "auto_slowmode_seconds",
                "auto_slowmode_duration_minutes", "anti_doxx_enabled",
                "raid_pattern_alert_enabled", "auto_translate_enabled", "remindme_enabled",
                "serverinfo_visual_enabled", "trap_channel_enabled", "trap_channel_id",
                "automod_spam_count", "automod_spam_window", "automod_caps_ratio",
                "automod_raid_count", "automod_raid_window", "warn_escalation_enabled",
                "warn_mute_threshold", "warn_mute_minutes", "warn_kick_threshold",
                "jtc_channel_id", "jtc_category_id", "pending_role_menu_items",
            }
            forbidden_fragments = ("secret", "token", "webhook", "api_key", "password", "cookie", "credential")
            def safe_config_value(value, depth=0):
                if depth > 8:
                    return False
                if value is None or type(value) in {bool, int, float, str}:
                    return not (isinstance(value, str) and len(value) > 4096)
                if isinstance(value, list):
                    return len(value) <= 500 and all(safe_config_value(item, depth + 1) for item in value)
                if isinstance(value, dict):
                    return len(value) <= 500 and all(
                        isinstance(key, str) and len(key) <= 100
                        and (depth == 0 or not any(fragment in key.lower() for fragment in forbidden_fragments))
                        and safe_config_value(item, depth + 1)
                        for key, item in value.items()
                    )
                return False
            if (
                not isinstance(result, dict)
                or not set(result).issubset(allowed_fields)
                or result.get("guild_id") != args[0]
                or not safe_config_value(result)
                or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 65_536
            ):
                raise CloudflareBotServiceError("Internal BotService returned an invalid REST config snapshot")
        if method == "api_guild_stats_snapshot" and result not in (None, {}):
            count_fields = {
                "ban_count", "muted_count", "open_tickets_count",
                "warned_count", "warns_today",
            }
            expected_fields = {
                "guild_id", "name", "member_count", *count_fields,
                "activity_7d", "locked",
            }
            if (
                not isinstance(result, dict)
                or set(result) != expected_fields
                or not valid_id(result.get("guild_id"))
                or not isinstance(result.get("name"), str)
                or not 1 <= len(result["name"]) <= 100
                or (
                    result.get("member_count") is not None
                    and (
                        type(result["member_count"]) is not int
                        or result["member_count"] < 0
                    )
                )
                or any(
                    type(result.get(field)) is not int or result[field] < 0
                    for field in count_fields
                )
                or not isinstance(result.get("activity_7d"), list)
                or len(result["activity_7d"]) != 7
                or any(
                    type(value) is not int or value < 0
                    for value in result["activity_7d"]
                )
                or not isinstance(result.get("locked"), bool)
            ):
                raise CloudflareBotServiceError(
                    "Internal BotService returned invalid guild stats"
                )
        return result

    def _dispatch_snapshot(self, method: str) -> dict:
        return self._dispatch(method, [], dict)

    def health_snapshot(self) -> dict:
        return self._dispatch_snapshot("health_snapshot")

    def public_status_snapshot(self) -> dict:
        return self._dispatch_snapshot("public_status_snapshot")

    def is_guild_owner(self, guild_id: str, user_id: str) -> bool:
        return self._dispatch("is_guild_owner", [guild_id, user_id], bool)

    def is_guild_admin(self, guild_id: str, user_id: str) -> bool:
        return self._dispatch("is_guild_admin", [guild_id, user_id], bool)

    def guild_presence(self, guild_id: str) -> dict:
        return self._dispatch("guild_presence", [guild_id], dict)

    def guild_name(self, guild_id: str) -> str:
        return self._dispatch("guild_name", [guild_id], str)

    def api_health_snapshot(self, guild_id: str | None = None) -> dict:
        args = [] if guild_id is None else [guild_id]
        return self._dispatch("api_health_snapshot", args, dict)

    def api_guild_list_snapshot(self, guild_id: str | None = None):
        args = [] if guild_id is None else [guild_id]
        return self._dispatch("api_guild_list_snapshot", args, (list, type(None)))

    def api_guild_id_candidates(self) -> list:
        return self._dispatch("api_guild_id_candidates", [], list)

    def available_guild_ids(self, candidate_ids: list[str]) -> list:
        return self._dispatch("available_guild_ids", [candidate_ids], list)

    def api_text_channels_snapshot(self, guild_id: str) -> list | None:
        return self._dispatch("api_text_channels_snapshot", [guild_id], (list, type(None)))

    def api_roles_snapshot(self, guild_id: str) -> list | None:
        return self._dispatch("api_roles_snapshot", [guild_id], (list, type(None)))

    def api_guild_stats_snapshot(
        self, guild_id: str, ticket_category_id: str | int | None = None
    ) -> dict | None:
        if type(ticket_category_id) is int and ticket_category_id > 0:
            ticket_category_id = str(ticket_category_id)
        args = [guild_id] if ticket_category_id is None else [guild_id, ticket_category_id]
        return self._dispatch("api_guild_stats_snapshot", args, (dict, type(None)))

    def api_faq_snapshot(self, guild_id: str) -> dict | None:
        return self._dispatch("api_faq_snapshot", [guild_id], (dict, type(None)))

    def api_members_snapshot(self, guild_id: str) -> list | None:
        return self._dispatch("api_members_snapshot", [guild_id], (list, type(None)))

    def api_rest_config_snapshot(self, guild_id: str) -> dict:
        return self._dispatch("api_rest_config_snapshot", [guild_id], dict)
