"""Signed HTTP client for the opt-in internal Nexus BotService endpoint.

Only explicit read-only BotService snapshots are transported. Additional
methods must be added alongside endpoint allowlisting and DTO tests.
"""

from __future__ import annotations

import datetime
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
            "api_guild_list_snapshot": (0, 1),
            "api_health_snapshot": (0, 1),
            "api_guild_stats_snapshot": (1, 2),
            "dashboard_overview_snapshot": (1, 2),
            "dashboard_settings_snapshot": 1,
            "dashboard_roles_snapshot": 1,
            "dashboard_sanction_stats_snapshot": 2,
            "dashboard_sanction_types_snapshot": 1,
            "api_sanction_cases_snapshot": 3,
            "api_audit_snapshot": 2,
            "api_member_warnings": 2,
            "api_warnlist_snapshot": 1,
            "api_authenticate_key": (1, 2),
            "api_rest_config_snapshot": 1,
            "dashboard_config_snapshot": 1,
            "dashboard_calendar_upcoming_events": 3,
            "is_guild_owner": 2,
            "is_guild_admin": 2,
            "available_guild_ids": 1,
            "dashboard_submitted_appeals_snapshot": 2,
            "api_claim_appeal_by_case": 2,
            "api_release_appeal_claim_by_case": 2,
            "api_resolve_appeal_by_case": 3,
        }
        if method not in allowed_argument_counts:
            raise ValueError("Unsupported internal BotService operation")
        if not isinstance(args, (list, tuple)):
            raise ValueError("Invalid internal BotService arguments")
        expected_counts = allowed_argument_counts[method]
        valid_count = (
            len(args) in expected_counts
            if isinstance(expected_counts, tuple)
            else len(args) == expected_counts
        )
        if not valid_count:
            raise ValueError("Invalid internal BotService arguments")
        if method == "api_authenticate_key":
            if (not isinstance(args[0], str) or not args[0].isascii() or not 1 <= len(args[0]) <= 512
                or (len(args) == 2 and not self._valid_guild_id(args[1]))):
                raise ValueError("Invalid API key authentication arguments")
        if method in {"guild_presence", "guild_name", "api_text_channels_snapshot", "api_roles_snapshot", "api_faq_snapshot", "api_members_snapshot", "api_guild_stats_snapshot", "dashboard_overview_snapshot", "dashboard_settings_snapshot", "dashboard_roles_snapshot", "dashboard_sanction_stats_snapshot", "dashboard_sanction_types_snapshot", "api_sanction_cases_snapshot", "dashboard_submitted_appeals_snapshot", "api_claim_appeal_by_case", "api_release_appeal_claim_by_case", "api_resolve_appeal_by_case", "api_audit_snapshot", "api_member_warnings", "api_warnlist_snapshot", "api_rest_config_snapshot", "dashboard_config_snapshot", "dashboard_calendar_upcoming_events", "is_guild_owner", "is_guild_admin"} and not self._valid_guild_id(args[0]):
            raise ValueError("Invalid guild ID")
        if method in {"api_guild_stats_snapshot", "dashboard_overview_snapshot"} and len(args) == 2 and args[1] is not None and not (
            isinstance(args[1], str)
            and re.fullmatch(r"[0-9]{1,20}", args[1]) is not None
            and 0 < int(args[1]) <= (2**64 - 1)
        ):
            raise ValueError("Invalid ticket category ID")

        if method == "available_guild_ids" and (
            not isinstance(args[0], list)
            or len(args[0]) > 500
            or not all(self._valid_guild_id(value) for value in args[0])
            or len(set(args[0])) != len(args[0])
        ):
            raise ValueError("Invalid candidate guild IDs")
        if method in {"is_guild_owner", "is_guild_admin"} and not self._valid_guild_id(args[1]):
            raise ValueError("Invalid user ID")
        if method == "dashboard_calendar_upcoming_events":
            if (
                not isinstance(args[1], str)
                or len(args[1]) > 64
                or type(args[2]) is not int
                or not 1 <= args[2] <= 50
            ):
                raise ValueError("Invalid calendar snapshot arguments")
            try:
                requested_at = datetime.datetime.fromisoformat(args[1].replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Invalid calendar snapshot timestamp") from exc
            if requested_at.tzinfo is None:
                raise ValueError("Invalid calendar snapshot timestamp")

        if method == "api_member_warnings" and not self._valid_guild_id(args[1]):
            raise ValueError("Invalid member ID")
        if method == "api_audit_snapshot" and (type(args[1]) is not int or not 1 <= args[1] <= 100):
            raise ValueError("Invalid audit limit")
        if method == "dashboard_sanction_stats_snapshot" and (type(args[1]) is not int or not 1 <= args[1] <= 30):
            raise ValueError("Invalid sanction statistics day range")
        if method == "api_sanction_cases_snapshot" and (
            not isinstance(args[1], str)
            or (args[1] and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", args[1]) is None)
            or type(args[2]) is not int or not 1 <= args[2] <= 200
        ):
            raise ValueError("Invalid sanction cases arguments")
        if method == "dashboard_submitted_appeals_snapshot" and (type(args[1]) is not int or not 1 <= args[1] <= 100):
            raise ValueError("Invalid submitted appeals limit")
        if method in {"api_claim_appeal_by_case", "api_release_appeal_claim_by_case"} and (type(args[1]) is not int or not 1 <= args[1] <= (2**63 - 1)):
            raise ValueError("Invalid appeal case ID")
        if method == "api_resolve_appeal_by_case" and (type(args[1]) is not int or not 1 <= args[1] <= (2**63 - 1) or not isinstance(args[2], str) or args[2] not in {"accepted", "denied"}):
            raise ValueError("Invalid appeal resolution arguments")

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
        if method == "available_guild_ids":
            if (
                not isinstance(result, list)
                or len(result) > 500
                or not all(self._valid_guild_id(value) for value in result)
                or len(set(result)) != len(result)
                or not set(result).issubset(set(args[0]))
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method in {"is_guild_owner", "is_guild_admin"}:
            if not isinstance(result, bool):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method in {"api_claim_appeal_by_case", "api_release_appeal_claim_by_case", "api_resolve_appeal_by_case"}:
            if not isinstance(result, bool):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_authenticate_key":
            if result is not None and (
                not self._valid_guild_id(result)
                or (len(args) == 2 and result != args[1])
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method in {"health_snapshot", "public_status_snapshot"}:
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
        elif method == "api_guild_list_snapshot":
            if result is not None and (
                not isinstance(result, list)
                or len(result) > 500
                or not all(
                    isinstance(guild, dict)
                    and set(guild) == {"id", "name", "member_count", "icon_url"}
                    and self._valid_guild_id(guild["id"])
                    and isinstance(guild["name"], str)
                    and 1 <= len(guild["name"]) <= 100
                    and (
                        guild["member_count"] is None
                        or (type(guild["member_count"]) is int and guild["member_count"] >= 0)
                    )
                    and (
                        guild["icon_url"] is None
                        or (
                            isinstance(guild["icon_url"], str)
                            and len(guild["icon_url"]) <= 2048
                            and guild["icon_url"].startswith("https://cdn.discordapp.com/icons/")
                        )
                    )
                    for guild in result
                )
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_health_snapshot":
            if (
                not isinstance(result, dict)
                or set(result) != {"online", "locked", "guild_count"}
                or not isinstance(result["online"], bool)
                or not isinstance(result["locked"], bool)
                or type(result["guild_count"]) is not int
                or result["guild_count"] < 0
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_guild_stats_snapshot":
            if result is not None and (
                not isinstance(result, dict)
                or set(result) != {
                    "guild_id", "name", "member_count", "ban_count", "muted_count",
                    "open_tickets_count", "warned_count", "warns_today", "activity_7d", "locked",
                }
                or not self._valid_guild_id(result.get("guild_id"))
                or not isinstance(result.get("name"), str)
                or not 1 <= len(result["name"]) <= 256
                or (
                    result["member_count"] is not None
                    and (type(result["member_count"]) is not int or result["member_count"] < 0)
                )
                or any(
                    type(result[key]) is not int or result[key] < 0
                    for key in ("ban_count", "muted_count", "open_tickets_count", "warned_count", "warns_today")
                )
                or not isinstance(result["activity_7d"], list)
                or len(result["activity_7d"]) != 7
                or any(type(count) is not int or count < 0 for count in result["activity_7d"])
                or not isinstance(result["locked"], bool)
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_member_warnings":
            if type(result) is not int or not 0 <= result <= 100000:
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_warnlist_snapshot":
            if (not isinstance(result, list) or len(result) > 1000
                or not all(isinstance(item, dict) and set(item) == {"user_id", "count"}
                    and self._valid_guild_id(item["user_id"])
                    and type(item["count"]) is int and 1 <= item["count"] <= 100000
                    for item in result)
                or len({item["user_id"] for item in result}) != len(result)):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_audit_snapshot":
            required = {"actor_name", "action", "details", "timestamp"}
            def valid_audit_entry(item):
                if not isinstance(item, dict) or set(item) != required or not all(isinstance(item[key], str) for key in required - {"timestamp"}):
                    return False
                stamp = item["timestamp"]
                if not isinstance(stamp, str):
                    return False
                try:
                    parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    return False
                return (
                    1 <= len(item["actor_name"]) <= 128 and 1 <= len(item["action"]) <= 200
                    and len(item["details"]) <= 1000
                    and parsed.tzinfo is not None and parsed.utcoffset() == datetime.timedelta(0)
                )
            if (not isinstance(result, list) or len(result) > args[1]
                or not all(valid_audit_entry(item) for item in result)
                or any(result[i]["timestamp"] < result[i + 1]["timestamp"] for i in range(len(result) - 1))):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
            result = [dict(item, timestamp=datetime.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))) for item in result]
        elif method == "dashboard_sanction_stats_snapshot":
            def valid_label(label):
                if not isinstance(label, str) or re.fullmatch(r"[0-3][0-9]/[0-1][0-9]", label) is None:
                    return False
                try:
                    datetime.datetime.strptime(label + "/2000", "%d/%m/%Y")
                    return True
                except ValueError:
                    return False
            if (
                not isinstance(result, list)
                or len(result) != args[1]
                or not all(
                    isinstance(item, dict)
                    and set(item) == {"label", "count"}
                    and valid_label(item["label"])
                    and type(item["count"]) is int
                    and 0 <= item["count"] <= 100000
                    for item in result
                )
                or len({item["label"] for item in result}) != len(result)
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "dashboard_submitted_appeals_snapshot":
            required = {"case_id", "sanction_type", "user_name", "user_id", "ban_reason", "appeal_text", "submitted_at"}
            def valid_appeal(item):
                if not isinstance(item, dict) or set(item) != required:
                    return False
                stamp = item["submitted_at"]
                if not isinstance(stamp, str):
                    return False
                try:
                    parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    return False
                return (
                    type(item["case_id"]) is int and 1 <= item["case_id"] <= (2**63 - 1)
                    and isinstance(item["sanction_type"], str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item["sanction_type"]) is not None
                    and isinstance(item["user_name"], str) and 1 <= len(item["user_name"]) <= 100
                    and self._valid_guild_id(item["user_id"])
                    and isinstance(item["ban_reason"], str) and len(item["ban_reason"]) <= 1000
                    and isinstance(item["appeal_text"], str) and 1 <= len(item["appeal_text"]) <= 4000
                    and parsed.tzinfo is not None and parsed.utcoffset() == datetime.timedelta(0)
                )
            if (not isinstance(result, list) or len(result) > args[1]
                or not all(valid_appeal(item) for item in result)
                or len({item["case_id"] for item in result}) != len(result)
                or any(result[i]["submitted_at"] < result[i + 1]["submitted_at"] for i in range(len(result) - 1))):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
            result = [dict(item, submitted_at=datetime.datetime.fromisoformat(item["submitted_at"].replace("Z", "+00:00"))) for item in result]
        elif method == "api_sanction_cases_snapshot":
            def valid_case(item):
                if not isinstance(item, dict) or set(item) != {"case_id", "type", "timestamp", "user_id", "reason"}:
                    return False
                stamp = item["timestamp"]
                if not isinstance(stamp, str):
                    return False
                try:
                    parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    return False
                return (
                    type(item["case_id"]) is int and 1 <= item["case_id"] <= (2**63 - 1)
                    and isinstance(item["type"], str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item["type"]) is not None
                    and parsed.tzinfo is not None and parsed.utcoffset() == datetime.timedelta(0)
                    and self._valid_guild_id(item["user_id"])
                    and isinstance(item["reason"], str) and len(item["reason"]) <= 1000
                )
            if (not isinstance(result, list) or len(result) > args[2]
                or not all(valid_case(item) for item in result)
                or len({item["case_id"] for item in result}) != len(result)
                or any(result[i]["timestamp"] < result[i + 1]["timestamp"] for i in range(len(result) - 1))):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
            result = [dict(item, timestamp=datetime.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))) for item in result]
        elif method == "dashboard_sanction_types_snapshot":
            if (not isinstance(result, list) or len(result) > 50
                or not all(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) for value in result)
                or len(set(result)) != len(result)):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "dashboard_roles_snapshot":
            if result is not None and (
                not isinstance(result, list)
                or len(result) > 500
                or not all(
                    isinstance(item, dict)
                    and set(item) == {"id", "name"}
                    and self._valid_guild_id(item["id"])
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                    for item in result
                )
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "dashboard_settings_snapshot":
            if result is not None:
                def valid_item(item):
                    return (
                        isinstance(item, dict)
                        and set(item) == {"id", "name"}
                        and self._valid_guild_id(item["id"])
                        and isinstance(item["name"], str)
                        and 1 <= len(item["name"]) <= 100
                    )
                if (
                    not isinstance(result, dict)
                    or set(result) != {"id", "name", "owner_id", "text_channels", "voice_channels", "roles"}
                    or not self._valid_guild_id(result["id"])
                    or not self._valid_guild_id(result["owner_id"])
                    or not isinstance(result["name"], str)
                    or not 1 <= len(result["name"]) <= 256
                    or any(
                        not isinstance(result[key], list)
                        or len(result[key]) > 500
                        or not all(valid_item(item) for item in result[key])
                        for key in ("text_channels", "voice_channels", "roles")
                    )
                ):
                    raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "dashboard_overview_snapshot":
            if result is not None:
                permission_keys = {"administrator", "manage_guild", "ban_members", "kick_members", "manage_roles"}
                def valid_item(item):
                    return (isinstance(item, dict) and set(item) == {"id", "name"}
                        and self._valid_guild_id(item["id"])
                        and isinstance(item["name"], str) and 1 <= len(item["name"]) <= 100)
                if (not isinstance(result, dict)
                    or set(result) != {"id", "name", "owner_id", "text_channels", "roles", "role_names", "open_tickets"}
                    or not self._valid_guild_id(result["id"])
                    or not self._valid_guild_id(result["owner_id"])
                    or not isinstance(result["name"], str) or not 1 <= len(result["name"]) <= 256
                    or not isinstance(result["text_channels"], list) or len(result["text_channels"]) > 500
                    or not all(valid_item(item) for item in result["text_channels"])
                    or not isinstance(result["open_tickets"], list) or len(result["open_tickets"]) > 500
                    or not all(valid_item(item) for item in result["open_tickets"])
                    or not isinstance(result["roles"], list) or len(result["roles"]) > 500
                    or not all(isinstance(role, dict) and set(role) == {"id", "name", "permissions"}
                        and self._valid_guild_id(role["id"])
                        and isinstance(role["name"], str) and 1 <= len(role["name"]) <= 100
                        and isinstance(role["permissions"], dict) and set(role["permissions"]) == permission_keys
                        and all(type(value) is bool for value in role["permissions"].values())
                        for role in result["roles"])
                    or not isinstance(result["role_names"], dict) or len(result["role_names"]) > 500
                    or not all(self._valid_guild_id(key) and isinstance(value, str) and 1 <= len(value) <= 100
                        for key, value in result["role_names"].items())):
                    raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "api_rest_config_snapshot":
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
            def safe_json_value(value, depth=0):
                if depth > 8:
                    return False
                if value is None or type(value) in {bool, int, float, str}:
                    return not (isinstance(value, str) and len(value) > 4096)
                if isinstance(value, list):
                    return len(value) <= 500 and all(safe_json_value(item, depth + 1) for item in value)
                if isinstance(value, dict):
                    return len(value) <= 500 and all(
                        isinstance(key, str) and len(key) <= 100
                        and not any(fragment in key.lower() for fragment in forbidden_fragments)
                        and safe_json_value(item, depth + 1)
                        for key, item in value.items()
                    )
                return False
            if (not isinstance(result, dict)
                or not set(result).issubset(allowed_fields)
                or result.get("guild_id") != args[0]
                or not safe_json_value(result)
                or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True)) > 65536):
                raise BotServiceTransportError("Internal BotService returned an invalid response")

        elif method == "dashboard_config_snapshot":
            allowed_fields = {
                "guild_id", "logs_channel", "autorole", "language", "tickets_enabled",
                "ticket_category_id", "appeal_channel_id", "boost_channel_id",
                "member_count_channel_id", "welcome_channel_id", "welcome_banner_enabled",
                "verification_enabled", "verified_role_id", "unverified_role_id",
                "quarantine_enabled", "quarantine_role_id", "quarantine_channel_id",
                "trap_channel_enabled", "trap_channel_id", "jtc_channel_id",
                "detailed_logs_enabled", "antinuke_enabled", "weekly_digest_enabled",
                "log_dashboard_actions", "anti_link_global_enabled",
                "anti_mention_spam_enabled", "anti_media_flood_enabled",
                "auto_slowmode_enabled", "anti_doxx_enabled", "raid_pattern_alert_enabled",
                "auto_translate_enabled", "remindme_enabled", "serverinfo_visual_enabled",
                "automod_spam_count", "automod_spam_window", "automod_caps_ratio",
                "automod_raid_count", "automod_raid_window", "mention_spam_threshold",
                "media_flood_count", "media_flood_window", "auto_slowmode_threshold",
                "auto_slowmode_window", "auto_slowmode_seconds",
                "auto_slowmode_duration_minutes", "warn_escalation_enabled",
                "warn_mute_threshold", "warn_mute_minutes", "warn_kick_threshold",
                "min_account_age_days", "antinuke_threshold",
                "quarantine_suspicion_days", "quarantine_hold_minutes",
                "banned_words", "banned_domains", "calendar_timezone",
                "calendar_channel_id", "calendar_manager_role_ids",
                "command_roles", "command_templates",
            }
            forbidden_fragments = ("secret", "token", "webhook", "api_key", "password", "cookie", "credential")

            def safe_dashboard_value(value, depth=0):
                if depth > 6:
                    return False
                if value is None or type(value) in {bool, int, float, str}:
                    return not (isinstance(value, str) and len(value) > 4096)
                if isinstance(value, list):
                    return len(value) <= 1000 and all(safe_dashboard_value(item, depth + 1) for item in value)
                if isinstance(value, dict):
                    return len(value) <= 100 and all(
                        isinstance(key, str) and len(key) <= 100
                        and not any(fragment in key.lower() for fragment in forbidden_fragments)
                        and safe_dashboard_value(item, depth + 1)
                        for key, item in value.items()
                    )
                return False

            def valid_string_list(value):
                return (
                    isinstance(value, list)
                    and len(value) <= 1000
                    and all(isinstance(item, str) and len(item) <= 253 for item in value)
                )

            def valid_command_mapping(value, *, allow_role_ids=False):
                return (
                    isinstance(value, dict)
                    and len(value) <= 100
                    and all(
                        isinstance(name, str) and 0 < len(name) <= 100
                        and isinstance(items, list) and len(items) <= 100
                        and all(
                            (isinstance(item, str) and len(item) <= 100)
                            or (allow_role_ids and type(item) is int and 0 < item < 2**64)
                            for item in items
                        )
                        for name, items in value.items()
                    )
                )

            list_fields = ("banned_words", "banned_domains", "calendar_manager_role_ids")
            scalar_fields = allowed_fields - set(list_fields) - {"command_roles", "command_templates", "guild_id"}
            if (
                not isinstance(result, dict)
                or not set(result).issubset(allowed_fields)
                or result.get("guild_id") != args[0]
                or any(not valid_string_list(result[key]) for key in list_fields if key in result)
                or ("command_roles" in result and not valid_command_mapping(result["command_roles"], allow_role_ids=True))
                or ("command_templates" in result and not valid_command_mapping(result["command_templates"]))
                or any(
                    value is not None and type(value) not in {bool, int, float, str}
                    for key, value in result.items() if key in scalar_fields
                )
                or not safe_dashboard_value(result)
                or len(json.dumps(result, separators=(",", ":"), ensure_ascii=True)) > 65536
            ):
                raise BotServiceTransportError("Internal BotService returned an invalid response")
        elif method == "dashboard_calendar_upcoming_events":
            required = {"_id", "guild_id", "title", "description", "role_id", "starts_at", "timezone", "time_set"}
            if result is not None:
                if not isinstance(result, list) or len(result) > args[2]:
                    raise BotServiceTransportError("Internal BotService returned an invalid response")
                projected = []
                for item in result:
                    if (
                        not isinstance(item, dict)
                        or set(item) != required
                        or item.get("guild_id") != args[0]
                        or not isinstance(item.get("_id"), str)
                        or re.fullmatch(r"[0-9a-fA-F]{24}", item["_id"]) is None
                        or not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 100
                        or not isinstance(item.get("description"), str) or len(item["description"]) > 1000
                        or not self._valid_guild_id(item.get("role_id"))
                        or not isinstance(item.get("starts_at"), str)
                        or not isinstance(item.get("timezone"), str) or not 1 <= len(item["timezone"]) <= 64
                        or not isinstance(item.get("time_set"), bool)
                    ):
                        raise BotServiceTransportError("Internal BotService returned an invalid response")
                    try:
                        starts_at = datetime.datetime.fromisoformat(item["starts_at"].replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise BotServiceTransportError("Internal BotService returned an invalid response") from exc
                    if starts_at.tzinfo is None:
                        raise BotServiceTransportError("Internal BotService returned an invalid response")
                    projected.append({**item, "starts_at": starts_at})
                if any(projected[i]["starts_at"] > projected[i + 1]["starts_at"] for i in range(len(projected) - 1)):
                    raise BotServiceTransportError("Internal BotService returned an invalid response")
                result = projected

        return result

    def health_snapshot(self):
        return self._dispatch_snapshot("health_snapshot")

    def public_status_snapshot(self):
        return self._dispatch_snapshot("public_status_snapshot")

    def available_guild_ids(self, candidate_ids):
        return self._dispatch_snapshot("available_guild_ids", (candidate_ids,))

    def is_guild_owner(self, guild_id: str, user_id: str) -> bool:
        return self._dispatch_snapshot("is_guild_owner", (guild_id, user_id))

    def is_guild_admin(self, guild_id: str, user_id: str) -> bool:
        return self._dispatch_snapshot("is_guild_admin", (guild_id, user_id))

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

    def api_guild_list_snapshot(self, guild_id: str = None):
        args = () if guild_id is None else (guild_id,)
        if guild_id is not None and not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        return self._dispatch_snapshot("api_guild_list_snapshot", args)

    def dashboard_sanction_stats_snapshot(self, guild_id: str, days: int = 30):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        if type(days) is not int or not 1 <= days <= 30:
            raise ValueError("Invalid sanction statistics day range")
        result = self._dispatch_snapshot("dashboard_sanction_stats_snapshot", (guild_id, days))
        return [(item["label"], item["count"]) for item in result]

    def dashboard_sanction_types_snapshot(self, guild_id: str):
        return self._dispatch_snapshot("dashboard_sanction_types_snapshot", (guild_id,))

    def api_rest_config_snapshot(self, guild_id: str):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        return self._dispatch_snapshot("api_rest_config_snapshot", (guild_id,))

    def dashboard_config_snapshot(self, guild_id: str):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        return self._dispatch_snapshot("dashboard_config_snapshot", (guild_id,))

    def dashboard_calendar_upcoming_events(self, guild_id: str, now, limit: int = 50):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        if not isinstance(now, datetime.datetime) or now.tzinfo is None:
            raise ValueError("Calendar snapshot time must be timezone-aware")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid calendar snapshot limit")
        timestamp = now.astimezone(datetime.timezone.utc).isoformat()
        return self._dispatch_snapshot("dashboard_calendar_upcoming_events", (guild_id, timestamp, limit))

    def api_authenticate_key(self, provided_key: str, guild_id: str = None):
        if not isinstance(provided_key, str) or not provided_key.isascii() or not 1 <= len(provided_key) <= 512:
            return None
        args = (provided_key,) if guild_id is None else (provided_key, guild_id)
        return self._dispatch_snapshot("api_authenticate_key", args)

    def api_member_warnings(self, guild_id: str, user_id: str):
        if not self._valid_guild_id(guild_id) or not self._valid_guild_id(user_id):
            raise ValueError("Invalid guild or member ID")
        return self._dispatch_snapshot("api_member_warnings", (guild_id, user_id))

    def api_warnlist_snapshot(self, guild_id: str):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        return self._dispatch_snapshot("api_warnlist_snapshot", (guild_id,))

    def api_audit_snapshot(self, guild_id: str, limit: int = 20):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid audit limit")
        return self._dispatch_snapshot("api_audit_snapshot", (guild_id, limit))

    def dashboard_submitted_appeals_snapshot(self, guild_id: str, limit: int = 100):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid submitted appeals limit")
        return self._dispatch_snapshot("dashboard_submitted_appeals_snapshot", (guild_id, limit))

    def api_claim_appeal_by_case(self, guild_id: str, case_id: int) -> bool:
        return self._dispatch_appeal_case_action("api_claim_appeal_by_case", guild_id, case_id)

    def api_release_appeal_claim_by_case(self, guild_id: str, case_id: int) -> bool:
        return self._dispatch_appeal_case_action("api_release_appeal_claim_by_case", guild_id, case_id)

    def api_resolve_appeal_by_case(self, guild_id: str, case_id: int, status: str) -> bool:
        if not isinstance(status, str) or status not in {"accepted", "denied"}:
            raise ValueError("Invalid appeal resolution status")
        return self._dispatch_appeal_case_action("api_resolve_appeal_by_case", guild_id, case_id, status)

    def _dispatch_appeal_case_action(self, method: str, guild_id: str, case_id: int, *extra) -> bool:
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        if type(case_id) is not int or not 1 <= case_id <= (2**63 - 1):
            raise ValueError("Invalid appeal case ID")
        return self._dispatch_snapshot(method, (guild_id, case_id, *extra))

    def api_sanction_cases_snapshot(self, guild_id: str, filter_type: str = "", limit: int = 50):
        if not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        if (not isinstance(filter_type, str)
            or (filter_type and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", filter_type) is None)
            or type(limit) is not int or not 1 <= limit <= 200):
            raise ValueError("Invalid sanction cases arguments")
        return self._dispatch_snapshot("api_sanction_cases_snapshot", (guild_id, filter_type, limit))

    def dashboard_roles_snapshot(self, guild_id: str):
        return self._dispatch_snapshot("dashboard_roles_snapshot", (guild_id,))

    def dashboard_settings_snapshot(self, guild_id: str):
        return self._dispatch_snapshot("dashboard_settings_snapshot", (guild_id,))

    def dashboard_overview_snapshot(self, guild_id: str, ticket_category_id: str = None):
        args = (guild_id,) if ticket_category_id is None else (guild_id, ticket_category_id)
        return self._dispatch_snapshot("dashboard_overview_snapshot", args)

    def api_guild_stats_snapshot(self, guild_id: str, ticket_category_id: str = None):
        args = (guild_id,) if ticket_category_id is None else (guild_id, ticket_category_id)
        return self._dispatch_snapshot("api_guild_stats_snapshot", args)

    def api_health_snapshot(self, guild_id: str = None):
        args = () if guild_id is None else (guild_id,)
        if guild_id is not None and not self._valid_guild_id(guild_id):
            raise ValueError("Invalid guild ID")
        return self._dispatch_snapshot("api_health_snapshot", args)
