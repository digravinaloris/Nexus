"""Cloudflare-native signed BotService transport using Workers Fetch via FFI.

This module is isolated to the local Worker spike. It deliberately avoids
requests/socket-based transports and uses the Workers Fetch API from inside a
request handler.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import math
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

def _valid_public_template_detail(item):
    required = {"code", "name", "created_guild_name", "created_at", "snapshot"}
    if (
        not isinstance(item, dict) or set(item) != required
        or not isinstance(item["code"], str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", item["code"]) is None
        or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 100
        or not isinstance(item["created_guild_name"], str)
        or not 1 <= len(item["created_guild_name"]) <= 100
        or not isinstance(item["created_at"], str)
    ):
        return False
    try:
        stamp = datetime.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None or stamp.utcoffset() != datetime.timedelta(0):
        return False
    snapshot = item["snapshot"]
    expected = {"feature_toggles", "automod", "warn_escalation_enabled", "command_roles_by_name"}
    if not isinstance(snapshot, dict) or set(snapshot) != expected or type(snapshot["warn_escalation_enabled"]) is not bool:
        return False
    features = snapshot["feature_toggles"]
    if (
        not isinstance(features, dict) or len(features) > 100
        or any(not isinstance(k, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", k) is None or type(v) is not bool for k, v in features.items())
    ):
        return False
    bounds = {
        "automod_spam_count": (1, 1000), "automod_spam_window": (1, 3600),
        "automod_caps_ratio": (0.1, 1), "automod_raid_count": (2, 1000),
        "automod_raid_window": (2, 3600),
    }
    automod = snapshot["automod"]
    if not isinstance(automod, dict) or not set(automod).issubset({"banned_words", "banned_domains", *bounds.keys()}):
        return False
    for key, value in automod.items():
        if key in {"banned_words", "banned_domains"}:
            if value is not None and (
                not isinstance(value, list) or len(value) > 200
                or any(not isinstance(entry, str) or len(entry) > 100 for entry in value)
            ):
                return False
        elif value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not bounds[key][0] <= value <= bounds[key][1]
        ):
            return False
    roles = snapshot["command_roles_by_name"]
    if not isinstance(roles, dict) or len(roles) > 100:
        return False
    for command, names in roles.items():
        if (
            not isinstance(command, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", command) is None
            or not isinstance(names, list) or len(names) > 50
            or any(not isinstance(name, str) or not 1 <= len(name) <= 100 for name in names)
        ):
            return False
    try:
        return len(json.dumps(item, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) <= 40 * 1024
    except (TypeError, ValueError):
        return False


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
        if method in {"health_snapshot", "public_status_snapshot", "api_guild_id_candidates", "public_global_lock_state"}:
            expected = bool if method == "public_global_lock_state" else (list if method == "api_guild_id_candidates" else dict)
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
        elif method == "dashboard_config_snapshot":
            if expected_type is not dict or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid dashboard config snapshot arguments")
        elif method == "dashboard_public_templates_snapshot":
            if expected_type is not list or args != []:
                raise ValueError("Invalid public template snapshot arguments")
        elif method == "dashboard_template_snapshot":
            if (
                expected_type != (dict, type(None)) or len(args) != 1
                or not isinstance(args[0], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args[0]) is None
            ):
                raise ValueError("Invalid public template detail arguments")
        elif method == "dashboard_presence_ping":
            if (
                expected_type is not type(None)
                or len(args) != 3
                or not valid_id(args[0])
                or not valid_id(args[1])
                or not isinstance(args[2], str)
                or not 1 <= len(args[2].strip()) <= 100
            ):
                raise ValueError("Invalid dashboard presence heartbeat arguments")
        elif method == "dashboard_presence_viewers":
            if expected_type is not list or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid dashboard presence viewer arguments")
        elif method in {"dashboard_roles_snapshot", "dashboard_settings_snapshot"}:
            expected = (list, type(None)) if method == "dashboard_roles_snapshot" else (dict, type(None))
            if expected_type != expected or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid dashboard snapshot arguments")
        elif method == "dashboard_overview_snapshot":
            if (
                expected_type != (dict, type(None))
                or len(args) not in {1, 2}
                or not valid_id(args[0])
                or (len(args) == 2 and not valid_id(args[1]))
            ):
                raise ValueError("Invalid dashboard overview arguments")
        elif method == "api_member_warnings":
            if expected_type is not int or len(args) != 2 or not all(valid_id(value) for value in args):
                raise ValueError("Invalid member warning count arguments")
        elif method == "dashboard_submitted_appeals_snapshot":
            if (
                expected_type is not list
                or len(args) != 2
                or not valid_id(args[0])
                or type(args[1]) is not int
                or not 1 <= args[1] <= 100
            ):
                raise ValueError("Invalid submitted appeals snapshot arguments")
        elif method == "api_warnlist_snapshot":
            if expected_type is not list or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid warning list snapshot arguments")
        elif method == "api_sanction_cases_snapshot":
            if (
                expected_type is not list
                or len(args) != 3
                or not valid_id(args[0])
                or not isinstance(args[1], str)
                or (args[1] and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", args[1]) is None)
                or type(args[2]) is not int
                or not 1 <= args[2] <= 200
            ):
                raise ValueError("Invalid sanction cases snapshot arguments")
        elif method == "api_audit_snapshot":
            if (
                expected_type is not list
                or len(args) != 2
                or not valid_id(args[0])
                or type(args[1]) is not int
                or not 1 <= args[1] <= 100
            ):
                raise ValueError("Invalid audit snapshot arguments")
        elif method == "dashboard_calendar_has_duplicate_event":
            if (
                expected_type is not bool
                or len(args) != 3
                or not valid_id(args[0])
                or not isinstance(args[1], str)
                or not 1 <= len(args[1]) <= 100
                or not isinstance(args[2], str)
                or len(args[2]) > 64
            ):
                raise ValueError("Invalid calendar duplicate-check arguments")
            try:
                requested_at = datetime.datetime.fromisoformat(args[2].replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Invalid calendar duplicate-check timestamp") from exc
            if requested_at.tzinfo is None:
                raise ValueError("Calendar duplicate-check timestamp must be timezone-aware")
        elif method == "dashboard_calendar_upcoming_events":
            if (
                expected_type is not list
                or len(args) != 3
                or not valid_id(args[0])
                or not isinstance(args[1], str)
                or len(args[1]) > 64
                or type(args[2]) is not int
                or not 1 <= args[2] <= 50
            ):
                raise ValueError("Invalid dashboard calendar arguments")
            try:
                requested_at = datetime.datetime.fromisoformat(args[1].replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Invalid dashboard calendar timestamp") from exc
            if requested_at.tzinfo is None:
                raise ValueError("Dashboard calendar timestamp must be timezone-aware")
        elif method == "dashboard_sanction_stats_snapshot":
            if (
                expected_type is not list
                or len(args) != 2
                or not valid_id(args[0])
                or type(args[1]) is not int
                or not 1 <= args[1] <= 30
            ):
                raise ValueError("Invalid dashboard sanction stats arguments")
        elif method == "dashboard_sanction_types_snapshot":
            if expected_type is not list or len(args) != 1 or not valid_id(args[0]):
                raise ValueError("Invalid dashboard sanction types arguments")
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
        if method == "health_snapshot" and (
            not isinstance(result, dict)
            or set(result) != {"status", "discord_gateway", "mongodb", "guild_count"}
            or result["status"] != "alive"
            or result["discord_gateway"] not in {"connected", "disconnected"}
            or result["mongodb"] not in {"connected", "unreachable"}
            or type(result["guild_count"]) is not int
            or not 0 <= result["guild_count"] <= 10_000_000
        ):
            raise CloudflareBotServiceError("Internal BotService returned an invalid health snapshot")
        if method == "public_status_snapshot" and (
            not isinstance(result, dict)
            or set(result) != {"online", "guild_count", "latency_ms", "uptime"}
            or type(result["online"]) is not bool
            or type(result["guild_count"]) is not int
            or not 0 <= result["guild_count"] <= 10_000_000
            or not (
                (type(result["latency_ms"]) is int and 0 <= result["latency_ms"] <= 600_000)
                or result["latency_ms"] == "—"
            )
            or not isinstance(result["uptime"], str)
            or not 1 <= len(result["uptime"]) <= 128
        ):
            raise CloudflareBotServiceError("Internal BotService returned an invalid public status snapshot")
        if method == "api_member_warnings" and (
            type(result) is not int or not 0 <= result <= 100000
        ):
            raise CloudflareBotServiceError(
                "Internal BotService returned an invalid member warning count"
            )
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
        if method == "api_guild_list_snapshot" and result is not None:
            valid_guilds = []
            for item in result if isinstance(result, list) else ():
                if not isinstance(item, dict) or set(item) != {"id", "name", "member_count", "icon_url"}:
                    valid_guilds = []
                    break
                guild_id = item["id"]
                name = item["name"]
                member_count = item["member_count"]
                icon_url = item["icon_url"]
                valid_icon = icon_url is None
                if isinstance(icon_url, str) and len(icon_url) <= 2048 and valid_id(guild_id):
                    parsed_icon = urlsplit(icon_url)
                    valid_icon = (
                        parsed_icon.scheme == "https"
                        and parsed_icon.netloc == "cdn.discordapp.com"
                        and parsed_icon.hostname == "cdn.discordapp.com"
                        and parsed_icon.username is None
                        and parsed_icon.password is None
                        and parsed_icon.fragment == ""
                        and parsed_icon.path.startswith(f"/icons/{guild_id}/")
                        and parsed_icon.path.rsplit("/", 1)[-1].rsplit(".", 1)[-1] in {"png", "webp", "gif"}
                        and re.fullmatch(
                            r"[A-Za-z0-9_-]{1,128}",
                            parsed_icon.path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                        ) is not None
                    )
                elif isinstance(icon_url, str):
                    valid_icon = False
                valid_guilds.append(
                    valid_id(guild_id)
                    and isinstance(name, str) and 1 <= len(name) <= 100
                    and (
                        member_count is None
                        or (
                            type(member_count) is int
                            and 0 <= member_count <= 10_000_000
                        )
                    )
                    and valid_icon
                )
            if (
                not isinstance(result, list)
                or len(result) > 500
                or len(valid_guilds) != len(result)
                or not all(valid_guilds)
                or len({item.get("id") for item in result if isinstance(item, dict)}) != len(result)
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
        if method == "dashboard_presence_viewers" and (
            len(result) > 50
            or not all(
                isinstance(name, str) and 1 <= len(name.strip()) <= 100
                for name in result
            )
            or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 8 * 1024
        ):
            raise CloudflareBotServiceError("Internal BotService returned invalid dashboard presence viewers")
        if method == "dashboard_template_snapshot" and result is not None and not _valid_public_template_detail(result):
            raise CloudflareBotServiceError("Internal BotService returned invalid public template detail")
        if method == "dashboard_public_templates_snapshot":
            required = {"code", "name", "created_guild_name", "created_at", "feature_toggle_count", "command_role_count"}
            def valid_public_template(item):
                if (
                    not isinstance(item, dict)
                    or set(item) != required
                    or not isinstance(item["code"], str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", item["code"]) is None
                    or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 100
                    or not isinstance(item["created_guild_name"], str) or not 1 <= len(item["created_guild_name"]) <= 100
                    or not isinstance(item["created_at"], str)
                    or type(item["feature_toggle_count"]) is not int or not 0 <= item["feature_toggle_count"] <= 500
                    or type(item["command_role_count"]) is not int or not 0 <= item["command_role_count"] <= 500
                ):
                    return False
                try:
                    stamp = datetime.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                except ValueError:
                    return False
                return stamp.tzinfo is not None and stamp.utcoffset() == datetime.timedelta(0)
            if (
                not isinstance(result, list)
                or len(result) > 50
                or not all(valid_public_template(item) for item in result)
                or len({item["code"] for item in result}) != len(result)
                or any(result[i]["created_at"] < result[i + 1]["created_at"] for i in range(len(result) - 1))
                or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 60 * 1024
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid public templates snapshot")
            result = [
                dict(item, created_at=datetime.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")))
                for item in result
            ]
        if method == "dashboard_sanction_stats_snapshot":
            def valid_sanction_stats_label(label):
                if not isinstance(label, str) or re.fullmatch(r"[0-3][0-9]/[0-1][0-9]", label) is None:
                    return False
                try:
                    time.strptime(label + "/2000", "%d/%m/%Y")
                    return True
                except ValueError:
                    return False
            if (
                not isinstance(result, list)
                or len(result) != args[1]
                or not all(
                    isinstance(item, dict)
                    and set(item) == {"label", "count"}
                    and valid_sanction_stats_label(item["label"])
                    and type(item["count"]) is int
                    and 0 <= item["count"] <= 100000
                    for item in result
                )
                or len({item["label"] for item in result}) != len(result)
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid dashboard sanction stats")
        if method == "dashboard_sanction_types_snapshot" and (
            len(result) > 50
            or not all(
                isinstance(value, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) is not None
                for value in result
            )
            or len(set(result)) != len(result)
        ):
            raise CloudflareBotServiceError("Internal BotService returned invalid dashboard sanction types")
        if method == "api_member_warnings" and (type(result) is not int or not 0 <= result <= 1000000):
            raise CloudflareBotServiceError("Internal BotService returned invalid member warning count")
        if method == "dashboard_submitted_appeals_snapshot":
            required = {"case_id", "sanction_type", "user_name", "user_id", "ban_reason", "appeal_text", "submitted_at"}
            def valid_appeal(item):
                if (
                    not isinstance(item, dict)
                    or set(item) != required
                    or type(item["case_id"]) is not int
                    or not 1 <= item["case_id"] <= (2**63 - 1)
                    or not isinstance(item["sanction_type"], str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item["sanction_type"]) is None
                    or not isinstance(item["user_name"], str)
                    or not 1 <= len(item["user_name"]) <= 100
                    or not valid_id(item["user_id"])
                    or not isinstance(item["ban_reason"], str)
                    or len(item["ban_reason"]) > 1000
                    or not isinstance(item["appeal_text"], str)
                    or not 1 <= len(item["appeal_text"]) <= 4000
                    or not isinstance(item["submitted_at"], str)
                ):
                    return False
                try:
                    stamp = datetime.datetime.fromisoformat(item["submitted_at"].replace("Z", "+00:00"))
                except ValueError:
                    return False
                return stamp.tzinfo is not None and stamp.utcoffset() == datetime.timedelta(0)
            if (
                not isinstance(result, list)
                or len(result) > args[1]
                or not all(valid_appeal(item) for item in result)
                or len({item["case_id"] for item in result}) != len(result)
                or any(result[i]["submitted_at"] < result[i + 1]["submitted_at"] for i in range(len(result) - 1))
                or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 60 * 1024
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid submitted appeals snapshot")
            result = [
                dict(item, submitted_at=datetime.datetime.fromisoformat(item["submitted_at"].replace("Z", "+00:00")))
                for item in result
            ]
        if method == "api_warnlist_snapshot":
            def valid_warning(item):
                return (
                    isinstance(item, dict)
                    and set(item) == {"user_id", "count"}
                    and isinstance(item["user_id"], str)
                    and re.fullmatch(r"[0-9]{1,20}", item["user_id"]) is not None
                    and type(item["count"]) is int
                    and 1 <= item["count"] <= 1000000
                )
            if (
                not isinstance(result, list)
                or len(result) > 1000
                or not all(valid_warning(item) for item in result)
                or len({item["user_id"] for item in result}) != len(result)
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid warning list snapshot")
        if method == "api_sanction_cases_snapshot":
            required = {"case_id", "type", "timestamp", "user_id", "reason"}
            def valid_sanction_case(item):
                if (
                    not isinstance(item, dict)
                    or set(item) != required
                    or type(item["case_id"]) is not int
                    or not 1 <= item["case_id"] <= (2**63 - 1)
                    or not isinstance(item["type"], str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item["type"]) is None
                    or not valid_id(item["user_id"])
                    or not isinstance(item["reason"], str)
                    or len(item["reason"]) > 1000
                    or not isinstance(item["timestamp"], str)
                ):
                    return False
                try:
                    stamp = datetime.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                except ValueError:
                    return False
                return stamp.tzinfo is not None and stamp.utcoffset() == datetime.timedelta(0)
            if (
                not isinstance(result, list)
                or len(result) > args[2]
                or not all(valid_sanction_case(item) for item in result)
                or len({item["case_id"] for item in result}) != len(result)
                or any(result[i]["timestamp"] < result[i + 1]["timestamp"] for i in range(len(result) - 1))
                or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 60 * 1024
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid sanction cases snapshot")
            result = [
                dict(item, timestamp=datetime.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")))
                for item in result
            ]
        if method == "api_audit_snapshot":
            required = {"actor_name", "action", "details", "timestamp"}
            def valid_audit_entry(item):
                if (
                    not isinstance(item, dict)
                    or set(item) != required
                    or not isinstance(item["actor_name"], str)
                    or not 1 <= len(item["actor_name"]) <= 128
                    or not isinstance(item["action"], str)
                    or not 1 <= len(item["action"]) <= 200
                    or not isinstance(item["details"], str)
                    or len(item["details"]) > 1000
                    or not isinstance(item["timestamp"], str)
                ):
                    return False
                try:
                    stamp = datetime.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                except ValueError:
                    return False
                return stamp.tzinfo is not None and stamp.utcoffset() == datetime.timedelta(0)
            if (
                not isinstance(result, list)
                or len(result) > args[1]
                or not all(valid_audit_entry(item) for item in result)
                or any(result[i]["timestamp"] < result[i + 1]["timestamp"] for i in range(len(result) - 1))
                or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 64 * 1024
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid audit snapshot")
        if method == "dashboard_calendar_upcoming_events":
            def valid_calendar_event(item):
                if (
                    not isinstance(item, dict)
                    or set(item) != {"_id", "guild_id", "title", "description", "role_id", "starts_at", "timezone", "time_set"}
                    or not isinstance(item["_id"], str)
                    or re.fullmatch(r"[0-9a-fA-F]{24}", item["_id"]) is None
                    or item["guild_id"] != args[0]
                    or not isinstance(item["title"], str)
                    or not 1 <= len(item["title"]) <= 100
                    or not isinstance(item["description"], str)
                    or len(item["description"]) > 1000
                    or not valid_id(item["role_id"])
                    or not isinstance(item["starts_at"], str)
                    or not isinstance(item["timezone"], str)
                    or not 1 <= len(item["timezone"]) <= 64
                    or type(item["time_set"]) is not bool
                ):
                    return False
                try:
                    stamp = datetime.datetime.fromisoformat(item["starts_at"].replace("Z", "+00:00"))
                except ValueError:
                    return False
                return stamp.tzinfo is not None
            if (
                not isinstance(result, list)
                or len(result) > args[2]
                or not all(valid_calendar_event(item) for item in result)
                or any(result[i]["starts_at"] > result[i + 1]["starts_at"] for i in range(len(result) - 1))
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid dashboard calendar events")
        if method == "dashboard_overview_snapshot" and result is not None:
            permission_keys = {"administrator", "manage_guild", "ban_members", "kick_members", "manage_roles"}
            def valid_overview_channel(item):
                return (
                    isinstance(item, dict)
                    and set(item) == {"id", "name"}
                    and valid_id(item["id"])
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                )
            def valid_overview_role(item):
                return (
                    isinstance(item, dict)
                    and set(item) == {"id", "name", "permissions"}
                    and valid_id(item["id"])
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                    and isinstance(item["permissions"], dict)
                    and set(item["permissions"]) == permission_keys
                    and all(type(value) is bool for value in item["permissions"].values())
                )
            if (
                not isinstance(result, dict)
                or set(result) != {"id", "name", "owner_id", "text_channels", "roles", "role_names", "open_tickets"}
                or not valid_id(result["id"])
                or not valid_id(result["owner_id"])
                or not isinstance(result["name"], str)
                or not 1 <= len(result["name"]) <= 256
                or any(
                    not isinstance(result[key], list)
                    or len(result[key]) > 500
                    or not all(validator(item) for item in result[key])
                    for key, validator in (
                        ("text_channels", valid_overview_channel),
                        ("roles", valid_overview_role),
                        ("open_tickets", valid_overview_channel),
                    )
                )
                or not isinstance(result["role_names"], dict)
                or len(result["role_names"]) > 500
                or not all(
                    valid_id(role_id)
                    and isinstance(role_name, str)
                    and 1 <= len(role_name) <= 100
                    for role_id, role_name in result["role_names"].items()
                )
                or len({item["id"] for item in result["text_channels"]}) != len(result["text_channels"])
                or len({item["id"] for item in result["roles"]}) != len(result["roles"])
                or len({item["id"] for item in result["open_tickets"]}) != len(result["open_tickets"])
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid dashboard overview")
        if method == "dashboard_roles_snapshot" and result is not None:
            if (
                not isinstance(result, list)
                or len(result) > 500
                or not all(
                    isinstance(item, dict)
                    and set(item) == {"id", "name"}
                    and valid_id(item["id"])
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                    for item in result
                )
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid dashboard roles")
        if method == "dashboard_settings_snapshot" and result is not None:
            def valid_setting_item(item):
                return (
                    isinstance(item, dict)
                    and set(item) == {"id", "name"}
                    and valid_id(item["id"])
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                )
            if (
                not isinstance(result, dict)
                or set(result) != {"id", "name", "owner_id", "text_channels", "voice_channels", "roles"}
                or not valid_id(result["id"])
                or not valid_id(result["owner_id"])
                or not isinstance(result["name"], str)
                or not 1 <= len(result["name"]) <= 256
                or any(
                    not isinstance(result[key], list)
                    or len(result[key]) > 500
                    or not all(valid_setting_item(item) for item in result[key])
                    for key in ("text_channels", "voice_channels", "roles")
                )
            ):
                raise CloudflareBotServiceError("Internal BotService returned invalid dashboard settings")
        if method == "dashboard_config_snapshot":
            allowed = {"guild_id", "logs_channel", "autorole", "language", "tickets_enabled", "ticket_category_id", "appeal_channel_id", "boost_channel_id", "member_count_channel_id", "welcome_channel_id", "welcome_banner_enabled", "verification_enabled", "verified_role_id", "unverified_role_id", "quarantine_enabled", "quarantine_role_id", "quarantine_channel_id", "trap_channel_enabled", "trap_channel_id", "jtc_channel_id", "detailed_logs_enabled", "antinuke_enabled", "weekly_digest_enabled", "log_dashboard_actions", "anti_link_global_enabled", "anti_mention_spam_enabled", "anti_media_flood_enabled", "auto_slowmode_enabled", "anti_doxx_enabled", "raid_pattern_alert_enabled", "auto_translate_enabled", "remindme_enabled", "serverinfo_visual_enabled", "automod_spam_count", "automod_spam_window", "automod_caps_ratio", "automod_raid_count", "automod_raid_window", "mention_spam_threshold", "media_flood_count", "media_flood_window", "auto_slowmode_threshold", "auto_slowmode_window", "auto_slowmode_seconds", "auto_slowmode_duration_minutes", "warn_escalation_enabled", "warn_mute_threshold", "warn_mute_minutes", "warn_kick_threshold", "min_account_age_days", "antinuke_threshold", "quarantine_suspicion_days", "quarantine_hold_minutes", "banned_words", "banned_domains", "calendar_timezone", "calendar_channel_id", "calendar_manager_role_ids", "command_roles", "command_templates"}
            forbidden = ("secret", "token", "webhook", "api_key", "password", "cookie", "credential")
            def safe_value(value, depth=0):
                if depth > 6: return False
                if value is None or type(value) in {bool, int, float, str}: return not (isinstance(value, str) and len(value) > 4096)
                if isinstance(value, list): return len(value) <= 1000 and all(safe_value(v, depth + 1) for v in value)
                if isinstance(value, dict): return len(value) <= 100 and all(isinstance(k, str) and len(k) <= 100 and not any(x in k.lower() for x in forbidden) and safe_value(v, depth + 1) for k, v in value.items())
                return False
            def valid_mapping(value, role_ids=False):
                return isinstance(value, dict) and len(value) <= 100 and all(isinstance(k, str) and 0 < len(k) <= 100 and isinstance(v, list) and len(v) <= 100 and all((isinstance(x, str) and len(x) <= 100) or (role_ids and type(x) is int and 0 < x < 2**64) for x in v) for k, v in value.items())
            lists = {"banned_words", "banned_domains", "calendar_manager_role_ids"}
            scalars = allowed - lists - {"guild_id", "command_roles", "command_templates"}
            if (not isinstance(result, dict) or not set(result).issubset(allowed) or result.get("guild_id") != args[0]
                or any(not isinstance(result[k], list) or len(result[k]) > 1000 or not all(isinstance(v, str) and len(v) <= 253 for v in result[k]) for k in lists if k in result)
                or ("command_roles" in result and not valid_mapping(result["command_roles"], True))
                or ("command_templates" in result and not valid_mapping(result["command_templates"]))
                or any(v is not None and type(v) not in {bool, int, float, str} for k, v in result.items() if k in scalars)
                or not safe_value(result) or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True)) > 65536):
                raise CloudflareBotServiceError("Internal BotService returned an invalid dashboard config snapshot")
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

    def public_global_lock_state(self) -> bool:
        return self._dispatch("public_global_lock_state", [], bool)

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

    def dashboard_config_snapshot(self, guild_id: str) -> dict:
        return self._dispatch("dashboard_config_snapshot", [guild_id], dict)

    def dashboard_public_templates_snapshot(self) -> list:
        return self._dispatch("dashboard_public_templates_snapshot", [], list)

    def dashboard_template_snapshot(self, code: str) -> dict | None:
        if not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code) is None:
            raise ValueError("Invalid public template code")
        return self._dispatch("dashboard_template_snapshot", [code], (dict, type(None)))


    def dashboard_presence_ping(self, guild_id: str, user_id: str, name: str) -> None:
        for value in (guild_id, user_id):
            if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,20}", value) is None:
                raise ValueError("Invalid dashboard presence heartbeat IDs")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("Invalid dashboard presence heartbeat name")
        return self._dispatch("dashboard_presence_ping", [guild_id, user_id, name], type(None))

    def dashboard_presence_viewers(self, guild_id: str) -> list:
        if not isinstance(guild_id, str) or re.fullmatch(r"[0-9]{1,20}", guild_id) is None:
            raise ValueError("Invalid guild ID")
        return self._dispatch("dashboard_presence_viewers", [guild_id], list)

    def dashboard_submitted_appeals_snapshot(self, guild_id: str, limit: int = 100) -> list:
        if not isinstance(guild_id, str) or re.fullmatch(r"[0-9]{1,20}", guild_id) is None:
            raise ValueError("Invalid guild ID")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid submitted appeals limit")
        return self._dispatch("dashboard_submitted_appeals_snapshot", [guild_id, limit], list)

    def api_member_warnings(self, guild_id: str, user_id: str) -> int:
        for value in (guild_id, user_id):
            if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,20}", value) is None:
                raise ValueError("Invalid member warning count IDs")
        return self._dispatch("api_member_warnings", [guild_id, user_id], int)

    def api_warnlist_snapshot(self, guild_id: str) -> list:
        if not isinstance(guild_id, str) or re.fullmatch(r"[0-9]{1,20}", guild_id) is None:
            raise ValueError("Invalid guild ID")
        return self._dispatch("api_warnlist_snapshot", [guild_id], list)

    def api_sanction_cases_snapshot(self, guild_id: str, filter_type: str = "", limit: int = 50) -> list:
        if not isinstance(guild_id, str) or re.fullmatch(r"[0-9]{1,20}", guild_id) is None:
            raise ValueError("Invalid guild ID")
        if (
            not isinstance(filter_type, str)
            or (filter_type and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", filter_type) is None)
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            raise ValueError("Invalid sanction cases arguments")
        return self._dispatch("api_sanction_cases_snapshot", [guild_id, filter_type, limit], list)

    def api_audit_snapshot(self, guild_id: str, limit: int = 20) -> list:
        if not isinstance(guild_id, str) or re.fullmatch(r"[0-9]{1,20}", guild_id) is None:
            raise ValueError("Invalid guild ID")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid audit snapshot limit")
        result = self._dispatch("api_audit_snapshot", [guild_id, limit], list)
        return [
            dict(item, timestamp=datetime.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")))
            for item in result
        ]

    def dashboard_calendar_has_duplicate_event(self, guild_id: str, title: str, starts_at) -> bool:
        if not isinstance(guild_id, str) or re.fullmatch(r"[0-9]{1,20}", guild_id) is None:
            raise ValueError("Invalid guild ID")
        if not isinstance(title, str) or not 1 <= len(title) <= 100:
            raise ValueError("Invalid calendar event title")
        if not isinstance(starts_at, datetime.datetime) or starts_at.tzinfo is None:
            raise ValueError("Calendar event time must be timezone-aware")
        timestamp = starts_at.astimezone(datetime.timezone.utc).isoformat()
        return self._dispatch("dashboard_calendar_has_duplicate_event", [guild_id, title, timestamp], bool)

    def dashboard_calendar_upcoming_events(self, guild_id: str, now, limit: int = 50) -> list:
        if not isinstance(guild_id, str) or re.fullmatch(r"[0-9]{1,20}", guild_id) is None:
            raise ValueError("Invalid guild ID")
        if not isinstance(now, datetime.datetime) or now.tzinfo is None:
            raise ValueError("Calendar snapshot time must be timezone-aware")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid calendar snapshot limit")
        timestamp = now.astimezone(datetime.timezone.utc).isoformat()
        result = self._dispatch("dashboard_calendar_upcoming_events", [guild_id, timestamp, limit], list)
        for item in result:
            item["starts_at"] = datetime.datetime.fromisoformat(item["starts_at"].replace("Z", "+00:00"))
        return result

    def dashboard_overview_snapshot(self, guild_id: str, ticket_category_id: str | None = None) -> dict | None:
        args = [guild_id] if ticket_category_id is None else [guild_id, ticket_category_id]
        return self._dispatch("dashboard_overview_snapshot", args, (dict, type(None)))

    def dashboard_sanction_stats_snapshot(self, guild_id: str, days: int = 30) -> list:
        result = self._dispatch("dashboard_sanction_stats_snapshot", [guild_id, days], list)
        return [(item["label"], item["count"]) for item in result]

    def dashboard_sanction_types_snapshot(self, guild_id: str) -> list:
        return self._dispatch("dashboard_sanction_types_snapshot", [guild_id], list)

    def dashboard_roles_snapshot(self, guild_id: str) -> list | None:
        return self._dispatch("dashboard_roles_snapshot", [guild_id], (list, type(None)))

    def dashboard_settings_snapshot(self, guild_id: str) -> dict | None:
        return self._dispatch("dashboard_settings_snapshot", [guild_id], (dict, type(None)))
