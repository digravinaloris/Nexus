"""Opt-in signed HTTP dispatch for the internal Nexus BotService boundary."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from flask import Blueprint, jsonify, request

from web.bot_service_signing import (
    BotServiceAuthError,
    CLOCK_SKEW_SECONDS,
    MAX_BODY_BYTES,
    parse_hmac_keyring,
    verify_internal_request,
)


class NonceStore(Protocol):
    def consume(self, key_id: str, nonce: str, expires_at: int) -> bool:
        """Atomically claim a nonce; return False when it was already used."""


class MongoNonceStore:
    """Durable replay store backed by a Mongo collection with unique and TTL indexes."""

    def __init__(self, collection_provider: Callable[[], Any]):
        self._collection_provider = collection_provider
        self._indexes_ready = False
        self._index_lock = threading.Lock()

    def _collection(self):
        collection = self._collection_provider()
        if collection is None:
            raise RuntimeError("BotService nonce collection is unavailable")
        if not self._indexes_ready:
            with self._index_lock:
                if not self._indexes_ready:
                    collection.create_index(
                        [("key_id", 1), ("nonce", 1)],
                        unique=True,
                        name="uniq_bot_service_nonce",
                    )
                    collection.create_index(
                        "expires_at",
                        expireAfterSeconds=0,
                        name="ttl_bot_service_nonce",
                    )
                    self._indexes_ready = True
        return collection

    def consume(self, key_id: str, nonce: str, expires_at: int) -> bool:
        from pymongo.errors import DuplicateKeyError

        collection = self._collection()
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        try:
            collection.insert_one(
                {
                    "key_id": key_id,
                    "nonce": nonce,
                    "expires_at": expiry,
                }
            )
        except DuplicateKeyError:
            return False
        return True


def create_internal_bot_service_blueprint(
    *,
    service,
    keyring: Mapping[str, bytes],
    nonce_store: NonceStore,
) -> Blueprint:
    """Create the opt-in v1 dispatch endpoint.

    Only explicit read-only snapshot methods are exposed. Mutating operations
    must be added explicitly with their own DTO and authorization rules;
    arbitrary method names are never dispatched.
    """
    blueprint = Blueprint("nexus_internal_bot_service", __name__)

    @blueprint.post("/internal/v1/dispatch")
    def dispatch():
        if request.query_string:
            return jsonify({"error": "invalid internal request"}), 400
        if request.content_length is None or request.content_length > MAX_BODY_BYTES:
            return jsonify({"error": "invalid internal request"}), 413
        if request.mimetype != "application/json":
            return jsonify({"error": "unsupported content type"}), 415

        body = request.get_data(cache=False)
        if len(body) > MAX_BODY_BYTES:
            return jsonify({"error": "invalid internal request"}), 413

        try:
            claims = verify_internal_request(
                keyring=keyring,
                method=request.method,
                path=request.path,
                body=body,
                headers=request.headers,
            )
        except BotServiceAuthError:
            return jsonify({"error": "unauthorized"}), 401

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"error": "invalid internal request"}), 400

        if (
            not isinstance(payload, dict)
            or set(payload) != {"method", "args", "kwargs"}
            or not isinstance(payload.get("kwargs"), dict)
            or payload.get("kwargs") != {}
            or not isinstance(payload.get("args"), list)
        ):
            return jsonify({"error": "unsupported internal operation"}), 403

        method_name = payload.get("method")
        args = payload["args"]
        if not isinstance(method_name, str):
            return jsonify({"error": "unsupported internal operation"}), 403

        operations = {
            "health_snapshot": (getattr(service, "health_snapshot", None), []),
            "public_status_snapshot": (getattr(service, "public_status_snapshot", None), []),
        }

        def valid_guild_id(value):
            return (
                isinstance(value, str)
                and re.fullmatch(r"[0-9]{1,20}", value) is not None
                and 0 < int(value) <= (2**64 - 1)
            )

        if method_name in {"is_guild_owner", "is_guild_admin"}:
            if len(args) != 2 or not all(valid_guild_id(value) for value in args):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name in {"api_claim_appeal_by_case", "api_release_appeal_claim_by_case"}:
            if len(args) != 2 or not valid_guild_id(args[0]) or type(args[1]) is not int or not 1 <= args[1] <= (2**63 - 1):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "api_resolve_appeal_by_case":
            if (len(args) != 3 or not valid_guild_id(args[0]) or type(args[1]) is not int
                or not 1 <= args[1] <= (2**63 - 1) or args[2] not in {"accepted", "denied"}):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "dashboard_submitted_appeals_snapshot":
            if len(args) != 2 or not valid_guild_id(args[0]) or type(args[1]) is not int or not 1 <= args[1] <= 100:
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "api_sanction_cases_snapshot":
            if (len(args) != 3 or not valid_guild_id(args[0])
                or not isinstance(args[1], str)
                or (args[1] and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", args[1]) is None)
                or type(args[2]) is not int or not 1 <= args[2] <= 200):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "dashboard_sanction_stats_snapshot":
            if (len(args) != 2 or not valid_guild_id(args[0])
                or type(args[1]) is not int or not 1 <= args[1] <= 30):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "dashboard_sanction_types_snapshot":
            if len(args) != 1 or not valid_guild_id(args[0]):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "dashboard_roles_snapshot":
            if len(args) != 1 or not valid_guild_id(args[0]):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "dashboard_settings_snapshot":
            if len(args) != 1 or not valid_guild_id(args[0]):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "dashboard_overview_snapshot":
            if (len(args) not in {1, 2} or not valid_guild_id(args[0])
                or (len(args) == 2 and args[1] is not None and not valid_guild_id(args[1]))):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "api_guild_stats_snapshot":
            if (
                len(args) not in {1, 2}
                or not valid_guild_id(args[0])
                or (
                    len(args) == 2
                    and args[1] is not None
                    and not (
                        valid_guild_id(args[1])
                        or (
                            type(args[1]) is int
                            and args[1] > 0
                            and re.fullmatch(r"[0-9]{1,20}", str(args[1]))
                        )
                    )
                )
            ):
                return jsonify({"error": "unsupported internal operation"}), 403
            operation_args = args
            if len(args) == 2 and type(args[1]) is int:
                operation_args = [args[0], str(args[1])]
            operations[method_name] = (
                getattr(service, method_name, None),
                operation_args,
            )
        elif method_name in {
            "guild_presence", "guild_name", "api_text_channels_snapshot",
            "api_roles_snapshot", "api_faq_snapshot", "api_members_snapshot",
        }:
            if len(args) != 1 or not valid_guild_id(args[0]):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name in {"api_health_snapshot", "api_guild_list_snapshot"}:
            if len(args) > 1 or (args and not valid_guild_id(args[0])):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "api_guild_stats_snapshot":
            if (
                len(args) not in {1, 2}
                or not valid_guild_id(args[0])
                or (len(args) == 2 and args[1] is not None and not valid_guild_id(args[1]))
            ):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "api_guild_id_candidates":
            if args != []:
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif method_name == "available_guild_ids":
            if (
                len(args) != 1
                or not isinstance(args[0], list)
                or len(args[0]) > 500
                or not all(valid_guild_id(value) for value in args[0])
            ):
                return jsonify({"error": "unsupported internal operation"}), 403
            operations[method_name] = (getattr(service, method_name, None), args)
        elif args != []:
            return jsonify({"error": "unsupported internal operation"}), 403

        operation_entry = operations.get(method_name)
        if operation_entry is None:
            return jsonify({"error": "unsupported internal operation"}), 403
        operation, operation_args = operation_entry
        if not callable(operation):
            return jsonify({"error": "unsupported internal operation"}), 403

        try:
            consumed = nonce_store.consume(
                claims.key_id,
                claims.nonce,
                claims.timestamp + CLOCK_SKEW_SECONDS,
            )
        except Exception:
            # Replay-store outages must never fall back to process memory.
            return jsonify({"error": "internal service unavailable"}), 503
        if not consumed:
            return jsonify({"error": "request already used"}), 409

        try:
            result = operation(*operation_args)
            if method_name in {"api_claim_appeal_by_case", "api_release_appeal_claim_by_case", "api_resolve_appeal_by_case"}:
                if not isinstance(result, bool):
                    return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "dashboard_submitted_appeals_snapshot":
                required = {"guild_id", "case_id", "sanction_type", "user_name", "user_id", "ban_reason", "appeal_text", "submitted_at", "status"}
                def appeal_dto(item):
                    if not isinstance(item, dict) or not required.issubset(item):
                        return None
                    stamp = item.get("submitted_at")
                    if not isinstance(stamp, datetime):
                        return None
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    stamp = stamp.astimezone(timezone.utc)
                    case_id, sanction_type = item.get("case_id"), item.get("sanction_type")
                    user_name, user_id = item.get("user_name"), item.get("user_id")
                    ban_reason, appeal_text = item.get("ban_reason"), item.get("appeal_text")
                    if (str(item.get("guild_id")) != str(operation_args[0]) or item.get("status") != "submitted"
                        or type(case_id) is not int or not 1 <= case_id <= (2**63 - 1)
                        or not isinstance(sanction_type, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", sanction_type) is None
                        or not isinstance(user_name, str) or not 1 <= len(user_name) <= 100
                        or not isinstance(user_id, str) or not valid_guild_id(user_id)
                        or not isinstance(ban_reason, str) or len(ban_reason) > 1000
                        or not isinstance(appeal_text, str) or not 1 <= len(appeal_text) <= 4000):
                        return None
                    return {"case_id": case_id, "sanction_type": sanction_type, "user_name": user_name,
                        "user_id": user_id, "ban_reason": ban_reason, "appeal_text": appeal_text,
                        "submitted_at": stamp.isoformat().replace("+00:00", "Z")}
                if not isinstance(result, list) or len(result) > operation_args[1]:
                    return jsonify({"error": "internal service unavailable"}), 503
                projected = [appeal_dto(item) for item in result]
                if (any(item is None for item in projected)
                    or len({item["case_id"] for item in projected}) != len(projected)
                    or any(projected[i]["submitted_at"] < projected[i + 1]["submitted_at"] for i in range(len(projected) - 1))):
                    return jsonify({"error": "internal service unavailable"}), 503
                result = projected
            if method_name == "api_sanction_cases_snapshot":
                def case_dto(item):
                    required = {"guild_id", "case_id", "type", "timestamp", "user_id", "reason"}
                    if not isinstance(item, dict) or not required.issubset(item):
                        return None
                    stamp = item.get("timestamp")
                    if not isinstance(stamp, datetime):
                        return None
                    if stamp.tzinfo is None:
                        # PyMongo commonly decodes BSON UTC datetimes as naive values.
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    stamp = stamp.astimezone(timezone.utc)
                    case_id, sanction_type = item.get("case_id"), item.get("type")
                    user_id, reason = item.get("user_id"), item.get("reason")
                    if (type(case_id) is not int or not 1 <= case_id <= (2**63 - 1)
                        or not isinstance(sanction_type, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", sanction_type) is None
                        or not isinstance(user_id, str) or not valid_guild_id(user_id)
                        or not isinstance(reason, str) or len(reason) > 1000
                        or str(item.get("guild_id")) != str(operation_args[0])):
                        return None
                    return {"case_id": case_id, "type": sanction_type, "timestamp": stamp.isoformat().replace("+00:00", "Z"), "user_id": user_id, "reason": reason}
                if not isinstance(result, list) or len(result) > operation_args[2]:
                    return jsonify({"error": "internal service unavailable"}), 503
                projected = [case_dto(item) for item in result]
                if (any(item is None for item in projected)
                    or len({item["case_id"] for item in projected}) != len(projected)
                    or any(projected[i]["timestamp"] < projected[i + 1]["timestamp"] for i in range(len(projected) - 1))):
                    return jsonify({"error": "internal service unavailable"}), 503
                result = projected
            if method_name == "dashboard_sanction_stats_snapshot":
                def valid_sanction_label(label):
                    if not isinstance(label, str) or re.fullmatch(r"[0-3][0-9]/[0-1][0-9]", label) is None:
                        return False
                    try:
                        datetime.strptime(label + "/2000", "%d/%m/%Y")
                        return True
                    except ValueError:
                        return False
                days = operation_args[1]
                if (not isinstance(result, list) or len(result) != days
                    or not all(isinstance(item, (tuple, list)) and len(item) == 2
                        and valid_sanction_label(item[0])
                        and type(item[1]) is int and 0 <= item[1] <= 100000
                        for item in result)
                    or len({item[0] for item in result}) != len(result)):
                    return jsonify({"error": "internal service unavailable"}), 503
                result = [{"label": label, "count": count} for label, count in result]
            if method_name == "dashboard_sanction_types_snapshot" and (
                not isinstance(result, list) or len(result) > 50
                or not all(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) for value in result)
                or len(set(result)) != len(result)
            ):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "dashboard_roles_snapshot" and result is not None and (
                not isinstance(result, list)
                or len(result) > 500
                or not all(
                    isinstance(item, dict)
                    and set(item) == {"id", "name"}
                    and valid_guild_id(item["id"])
                    and isinstance(item["name"], str)
                    and 1 <= len(item["name"]) <= 100
                    for item in result
                )
            ):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "dashboard_settings_snapshot" and result is not None:
                def valid_settings_item(item):
                    return (
                        isinstance(item, dict)
                        and set(item) == {"id", "name"}
                        and valid_guild_id(item["id"])
                        and isinstance(item["name"], str)
                        and 1 <= len(item["name"]) <= 100
                    )
                if (
                    not isinstance(result, dict)
                    or set(result) != {"id", "name", "owner_id", "text_channels", "voice_channels", "roles"}
                    or not valid_guild_id(result.get("id"))
                    or not valid_guild_id(result.get("owner_id"))
                    or not isinstance(result.get("name"), str)
                    or not 1 <= len(result["name"]) <= 256
                    or any(
                        not isinstance(result.get(key), list)
                        or len(result[key]) > 500
                        or not all(valid_settings_item(item) for item in result[key])
                        for key in ("text_channels", "voice_channels", "roles")
                    )
                ):
                    return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "dashboard_overview_snapshot" and result is not None:
                permission_keys = {"administrator", "manage_guild", "ban_members", "kick_members", "manage_roles"}
                def valid_item(item):
                    return (isinstance(item, dict) and set(item) == {"id", "name"}
                        and valid_guild_id(item["id"]) and isinstance(item["name"], str)
                        and 1 <= len(item["name"]) <= 100)
                if (not isinstance(result, dict)
                    or set(result) != {"id", "name", "owner_id", "text_channels", "roles", "role_names", "open_tickets"}
                    or not valid_guild_id(result.get("id")) or not valid_guild_id(result.get("owner_id"))
                    or not isinstance(result.get("name"), str) or not 1 <= len(result["name"]) <= 256
                    or not isinstance(result.get("text_channels"), list) or len(result["text_channels"]) > 500
                    or not all(valid_item(item) for item in result["text_channels"])
                    or not isinstance(result.get("open_tickets"), list) or len(result["open_tickets"]) > 500
                    or not all(valid_item(item) for item in result["open_tickets"])
                    or not isinstance(result.get("roles"), list) or len(result["roles"]) > 500
                    or not all(isinstance(role, dict) and set(role) == {"id", "name", "permissions"}
                        and valid_guild_id(role.get("id")) and isinstance(role.get("name"), str)
                        and 1 <= len(role["name"]) <= 100 and isinstance(role.get("permissions"), dict)
                        and set(role["permissions"]) == permission_keys
                        and all(type(value) is bool for value in role["permissions"].values())
                        for role in result["roles"])
                    or not isinstance(result.get("role_names"), dict) or len(result["role_names"]) > 500
                    or not all(valid_guild_id(key) and isinstance(value, str) and 1 <= len(value) <= 100
                        for key, value in result["role_names"].items())):
                    return jsonify({"error": "internal service unavailable"}), 503
            if method_name in {"is_guild_owner", "is_guild_admin"} and not isinstance(result, bool):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "guild_presence" and (
                not isinstance(result, dict)
                or set(result) != {"bot_ready", "guild_present"}
                or any(not isinstance(value, bool) for value in result.values())
            ):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "guild_name" and (
                not isinstance(result, str) or not result or len(result) > 256
            ):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name in {"api_guild_id_candidates", "available_guild_ids"} and (
                not isinstance(result, list)
                or len(result) > (2000 if method_name == "api_guild_id_candidates" else 500)
                or not all(
                    isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value)
                    for value in result
                )
            ):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "api_health_snapshot" and (
                not isinstance(result, dict)
                or set(result) != {"online", "locked", "guild_count"}
                or not isinstance(result["online"], bool)
                or not isinstance(result["locked"], bool)
                or type(result["guild_count"]) is not int
                or result["guild_count"] < 0
            ):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "api_guild_list_snapshot" and result is not None:
                if (
                    not isinstance(result, list)
                    or len(result) > 500
                    or not all(
                        isinstance(guild, dict)
                        and set(guild) == {"id", "name", "member_count", "icon_url"}
                        and valid_guild_id(guild["id"])
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
                    return jsonify({"error": "internal service unavailable"}), 503
                serialized_size = len(
                    json.dumps(
                        result, separators=(",", ":"), ensure_ascii=True
                    ).encode("utf-8")
                )
                if serialized_size > 60 * 1024:
                    return jsonify({"error": "internal service unavailable"}), 503
            if method_name in {"api_text_channels_snapshot", "api_roles_snapshot"} and (
                result is not None
                and (
                    not isinstance(result, list)
                    or len(result) > 500
                    or not all(
                        isinstance(item, dict)
                        and set(item) == {"id", "name"}
                        and isinstance(item["id"], str)
                        and re.fullmatch(r"[0-9]{1,20}", item["id"])
                        and isinstance(item["name"], str)
                        and 1 <= len(item["name"]) <= 100
                        for item in result
                    )
                )
            ):
                return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "api_members_snapshot" and result is not None:
                expected_member_fields = {
                    "id", "name", "username", "avatar_url", "is_muted"
                }
                if (
                    not isinstance(result, list)
                    or len(result) > 500
                    or not all(
                        isinstance(member, dict)
                        and set(member) == expected_member_fields
                        and valid_guild_id(member.get("id"))
                        and isinstance(member.get("name"), str)
                        and 1 <= len(member["name"]) <= 100
                        and isinstance(member.get("username"), str)
                        and 1 <= len(member["username"]) <= 100
                        and isinstance(member.get("avatar_url"), str)
                        and len(member["avatar_url"]) <= 2048
                        and member["avatar_url"].startswith("https://cdn.discordapp.com/")
                        and isinstance(member.get("is_muted"), bool)
                        for member in result
                    )
                ):
                    return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "api_faq_snapshot" and result is not None:
                if not isinstance(result, dict):
                    return jsonify({"error": "internal service unavailable"}), 503
                enabled = result.get("enabled", False)
                entries = result.get("entries", [])
                if (
                    not isinstance(enabled, bool)
                    or not isinstance(entries, list)
                    or len(entries) > 500
                ):
                    return jsonify({"error": "internal service unavailable"}), 503
                safe_entries = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        return jsonify({"error": "internal service unavailable"}), 503
                    entry_id = entry.get("id")
                    keywords = entry.get("keywords")
                    response_text = entry.get("response")
                    if (
                        not isinstance(entry_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry_id)
                        or not isinstance(keywords, list)
                        or not 1 <= len(keywords) <= 25
                        or any(
                            not isinstance(keyword, str)
                            or not keyword.strip()
                            or len(keyword) > 100
                            for keyword in keywords
                        )
                        or not isinstance(response_text, str)
                        or not response_text.strip()
                        or len(response_text) > 2000
                    ):
                        return jsonify({"error": "internal service unavailable"}), 503
                    safe_entries.append(
                        {
                            "id": entry_id,
                            "keywords": keywords,
                            "response": response_text,
                        }
                    )
                result = {"enabled": enabled, "entries": safe_entries}
                serialized_size = len(
                    json.dumps(
                        result, separators=(",", ":"), ensure_ascii=True
                    ).encode("utf-8")
                )
                if serialized_size > 60 * 1024:
                    return jsonify({"error": "internal service unavailable"}), 503
            if method_name == "api_guild_stats_snapshot" and result not in (None, {}):
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
                    or not valid_guild_id(result.get("guild_id"))
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
                    return jsonify({"error": "internal service unavailable"}), 503
            return jsonify({"ok": True, "result": result})
        except Exception:
            return jsonify({"error": "internal service unavailable"}), 503

    return blueprint


def internal_bot_service_app_options(
    *,
    environ: Mapping[str, str],
    service_factory: Callable[[], Any],
    nonce_collection_provider: Callable[[], Any],
) -> dict[str, Any]:
    """Build opt-in Flask factory kwargs; disabled unless explicitly enabled."""
    if environ.get("NEXUS_BOT_SERVICE_INTERNAL_ENABLED", "").strip().lower() != "true":
        return {}

    keyring = parse_hmac_keyring(environ.get("NEXUS_BOT_SERVICE_HMAC_KEYS", ""))
    service = service_factory()
    nonce_store = MongoNonceStore(nonce_collection_provider)
    return {
        "bot_service": service,
        "internal_bot_service": service,
        "internal_bot_service_keyring": keyring,
        "internal_nonce_store": nonce_store,
    }
