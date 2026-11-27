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
            return isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) is not None

        if method_name in {"is_guild_owner", "is_guild_admin"}:
            if len(args) != 2 or not all(valid_guild_id(value) for value in args):
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
            if method_name == "api_health_snapshot" and not isinstance(result, dict):
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
