"""Local-only Flask/D1 runtime spike. Never import Nexus bot modules here."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time

from flask import Flask, Response, jsonify, request, session
from pyodide.ffi import run_sync
from workers import wsgi

from crypto import aes_gcm_decrypt, aes_gcm_encrypt, sha256 as webcrypto_sha256
from d1_session_interface import D1SessionInterface, SessionStorageUnavailable
from bot_service_client import (
    CloudflareBotServiceError,
    CloudflareFetchBotService,
    MAX_RESPONSE_BYTES,
)


app = Flask(__name__)


def _db():
    """Return the D1 binding supplied to this Worker request."""
    return request.environ["workers.env"].DB


def _await(promise):
    """Bridge a Workers async binding call into a synchronous Flask view."""
    return run_sync(promise)


app.config.update(SESSION_COOKIE_NAME="nexus_spike_session", SESSION_COOKIE_SECURE=False)
app.session_interface = D1SessionInterface(
    db_getter=_db,
    awaiter=_await,
    key_getter=lambda version: _test_encryption_key(version),
)


@app.before_request
def reject_unavailable_session_storage():
    if getattr(session, "unavailable", False):
        return jsonify({"error": "session storage unavailable"}), 503


@app.errorhandler(SessionStorageUnavailable)
def handle_session_storage_unavailable(_error):
    return jsonify({"error": "session storage unavailable"}), 503


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_body():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def _test_bot_service_secret() -> bytes:
    raw = str(request.environ["workers.env"].SPIKE_TEST_BOT_SERVICE_HMAC_SECRET)
    secret = bytes.fromhex(raw)
    if len(secret) < 32:
        raise RuntimeError("local BotService test key must contain at least 32 bytes")
    return secret


@app.post("/internal/v1/dispatch")
def mock_internal_bot_service():
    """Local-only mock of the remote bot host; verifies the real signed wire format."""
    body = request.get_data(cache=False)
    headers = request.headers
    key_id = headers.get("X-Nexus-Key-Id", "")
    timestamp_text = headers.get("X-Nexus-Timestamp", "")
    nonce = headers.get("X-Nexus-Nonce", "")
    signature = headers.get("X-Nexus-Signature", "")
    if (
        key_id not in {
            "spike-test",
            "spike-redirect",
            "spike-unavailable",
            "spike-malformed",
            "spike-oversized",
            "spike-channel-malformed",
            "spike-role-malformed",
            "spike-stats-malformed",
            "spike-faq-malformed",
            "spike-members-malformed",
            "spike-dashboard-sanction-stats-malformed",
            "spike-dashboard-overview-malformed",
            "spike-public-templates-malformed",
            "spike-public-templates-duplicate",
            "spike-audit-malformed",
            "spike-sanction-cases-malformed",
        }
        or not re.fullmatch(r"0|[1-9][0-9]{0,11}", timestamp_text)
        or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", nonce)
        or not re.fullmatch(r"[a-f0-9]{64}", signature)
        or abs(int(time.time()) - int(timestamp_text)) > 60
        or len(body) > 1_048_576
    ):
        return jsonify({"error": "unauthorized"}), 401

    canonical = "\n".join(
        (
            "NEXUS-BOT-SERVICE-V1",
            key_id,
            request.method,
            "/internal/v1/dispatch",
            timestamp_text,
            nonce,
            hashlib.sha256(body).hexdigest(),
        )
    ).encode("utf-8")
    expected = hmac.new(_test_bot_service_secret(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "unauthorized"}), 401

    # These response modes are disposable local fixtures used only by the smoke test.
    if key_id == "spike-redirect":
        return Response("", status=302, headers={"Location": "https://bot.example/"})
    if key_id == "spike-unavailable":
        return jsonify({"error": "unavailable"}), 503
    if key_id == "spike-malformed":
        return Response("{malformed-json", status=200, mimetype="application/json")
    if key_id == "spike-oversized":
        oversized = '{"result":"' + ("x" * (MAX_RESPONSE_BYTES + 32)) + '"}'
        return Response(oversized, status=200, mimetype="application/json")

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "invalid request"}), 400
    if (
        not isinstance(payload, dict)
        or set(payload) != {"method", "args", "kwargs"}
        or not isinstance(payload.get("args"), list)
        or not isinstance(payload.get("kwargs"), dict)
        or payload.get("kwargs") != {}
    ):
        return jsonify({"error": "unsupported operation"}), 403

    results = {
        "health_snapshot": {"status": "alive", "guild_count": 12},
        "public_global_lock_state": False,
        "public_status_snapshot": {
            "online": True,
            "guild_count": 12,
            "latency_ms": 31,
            "uptime": "3h 12m",
        },
        "api_health_snapshot": {"online": True, "locked": False, "guild_count": 1},
        "api_guild_list_snapshot": [{"id": "123", "name": "Spike Test Server"}],
        "api_guild_id_candidates": ["123", "456"],
        "api_text_channels_snapshot": [
            {"id": "456", "name": "general"},
            {"id": "789", "name": "staff-chat"},
        ],
        "api_roles_snapshot": [
            {"id": "321", "name": "Moderator"},
            {"id": "654", "name": "Member"},
        ],
        "api_guild_stats_snapshot": {
            "guild_id": "123",
            "name": "Spike Test Server",
            "member_count": 25,
            "ban_count": 2,
            "muted_count": 1,
            "open_tickets_count": 3,
            "warned_count": 4,
            "warns_today": 1,
            "activity_7d": [0, 1, 2, 3, 4, 5, 6],
            "locked": False,
        },
        "api_faq_snapshot": {
            "enabled": True,
            "entries": [
                {
                    "id": "a" * 32,
                    "keywords": ["help", "support"],
                    "response": "Contact the support team.",
                }
            ],
        },
        "dashboard_config_snapshot": {"guild_id": "123", "language": "en", "tickets_enabled": True, "banned_words": ["spam"], "command_roles": {"ban": [321]}},
        "api_warnlist_snapshot": [{"user_id": "123456789012345678", "count": 2}],
        "dashboard_submitted_appeals_snapshot": [{
            "case_id": 12,
            "sanction_type": "ban",
            "user_name": "ExampleUser",
            "user_id": "123456789012345678",
            "ban_reason": "spam",
            "appeal_text": "Please review this ban.",
            "submitted_at": "2026-11-28T09:30:00Z",
        }],
        "api_member_warnings": 2,
        "api_audit_snapshot": [{
            "actor_name": "Moderator",
            "action": "Banned member",
            "details": "spam",
            "timestamp": "2026-11-28T09:30:00Z",
        }],
        "dashboard_calendar_has_duplicate_event": False,
        "dashboard_calendar_upcoming_events": [{
            "_id": "0123456789abcdef01234567",
            "guild_id": "123",
            "title": "Community event",
            "description": "A safe event",
            "role_id": "456",
            "starts_at": "2026-12-01T18:00:00Z",
            "timezone": "Europe/Paris",
            "time_set": True,
        }],
        "dashboard_sanction_types_snapshot": ["ban", "kick", "mute", "warn"],
        "dashboard_overview_snapshot": {
            "id": "123",
            "name": "Spike Test Server",
            "owner_id": "42",
            "text_channels": [{"id": "456", "name": "general"}],
            "roles": [{
                "id": "321",
                "name": "Moderator",
                "permissions": {
                    "administrator": False,
                    "manage_guild": True,
                    "ban_members": False,
                    "kick_members": False,
                    "manage_roles": False,
                },
            }],
            "role_names": {"321": "Moderator", "654": "Member"},
            "open_tickets": [{"id": "789", "name": "ticket-1"}],
        },
        "dashboard_roles_snapshot": [
            {"id": "321", "name": "Moderator"},
            {"id": "654", "name": "Member"},
        ],
        "dashboard_settings_snapshot": {
            "id": "123",
            "name": "Spike Test Server",
            "owner_id": "42",
            "text_channels": [{"id": "456", "name": "general"}],
            "voice_channels": [{"id": "789", "name": "Lobby"}],
            "roles": [{"id": "321", "name": "Moderator"}],
        },
        "dashboard_public_templates_snapshot": [{
            "code": "AbC_123-x",
            "name": "Welcome Pack",
            "created_guild_name": "Spike Test Server",
            "created_at": "2026-11-28T09:30:00Z",
            "feature_toggle_count": 4,
            "command_role_count": 2,
        }],
        "dashboard_template_snapshot": {
            "code": "AbC_123-x",
            "name": "Welcome Pack",
            "created_guild_name": "Spike Test Server",
            "created_at": "2026-11-28T09:30:00Z",
            "snapshot": {
                "feature_toggles": {"tickets_enabled": True},
                "automod": {"automod_spam_count": 10, "banned_words": ["spam"]},
                "warn_escalation_enabled": False,
                "command_roles_by_name": {"ban": ["Moderators"]},
            },
        },
        "api_rest_config_snapshot": {
            "guild_id": "123",
            "language": "en",
            "tickets_enabled": True,
            "welcome_banner_enabled": False,
            "command_roles": {"ban": ["321"]},
        },
        "api_members_snapshot": [
            {
                "id": "123456789012345678",
                "name": "Sample Member",
                "username": "sample",
                "avatar_url": "https://cdn.discordapp.com/avatars/123456789012345678/sample.png",
                "is_muted": False,
            }
        ],
    }
    method = payload.get("method")
    args = payload["args"]
    if isinstance(method, str) and method in results and args == [] and method != "dashboard_public_templates_snapshot":
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_health_snapshot" and args == ["123"]:
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_guild_list_snapshot" and args == ["123"]:
        return jsonify({"ok": True, "result": results[method]})
    if method == "guild_presence" and args == ["123"]:
        return jsonify({"ok": True, "result": {"bot_ready": True, "guild_present": True}})
    if method == "guild_name" and args == ["123"]:
        return jsonify({"ok": True, "result": "Spike Test Server"})
    if method == "available_guild_ids" and args == [["123", "456"]]:
        return jsonify({"ok": True, "result": ["123"]})
    if method == "api_text_channels_snapshot" and args == ["123"]:
        if key_id == "spike-channel-malformed":
            return jsonify({"ok": True, "result": [{"id": "not-decimal", "name": "general"}]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_roles_snapshot" and args == ["123"]:
        if key_id == "spike-role-malformed":
            return jsonify({"ok": True, "result": [{"id": "not-decimal", "name": "Moderator"}]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_sanction_stats_snapshot" and args == ["123", 2]:
        if key_id == "spike-dashboard-sanction-stats-malformed":
            return jsonify({"ok": True, "result": [{"label": "27/11", "count": -1}, {"label": "28/11", "count": 3}]})
        return jsonify({"ok": True, "result": [{"label": "27/11", "count": 0}, {"label": "28/11", "count": 3}]})
    if method == "dashboard_sanction_types_snapshot" and args == ["123"]:
        if key_id == "spike-dashboard-sanction-types-malformed":
            return jsonify({"ok": True, "result": ["ban", "not valid!"]})
        if key_id == "spike-dashboard-sanction-types-duplicate":
            return jsonify({"ok": True, "result": ["ban", "ban"]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_member_warnings" and args == ["123", "123456789012345678"]:
        if key_id == "spike-member-warnings-malformed":
            return jsonify({"ok": True, "result": "2"})
        if key_id == "spike-member-warnings-out-of-range":
            return jsonify({"ok": True, "result": -1})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_submitted_appeals_snapshot" and args == ["123", 10]:
        if key_id == "spike-appeals-malformed":
            malformed = dict(results[method][0])
            malformed["token"] = "must-never-be-transported"
            return jsonify({"ok": True, "result": [malformed]})
        if key_id == "spike-appeals-duplicate":
            return jsonify({"ok": True, "result": [results[method][0], results[method][0]]})
        if key_id == "spike-appeals-order":
            older = dict(results[method][0], case_id=13, submitted_at="2026-11-27T09:30:00Z")
            return jsonify({"ok": True, "result": [older, results[method][0]]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_warnlist_snapshot" and args == ["123"]:
        if key_id == "spike-warnlist-malformed":
            return jsonify({"ok": True, "result": [{"user_id": "not-decimal", "count": 2}]})
        if key_id == "spike-warnlist-duplicate":
            return jsonify({"ok": True, "result": [
                {"user_id": "123456789012345678", "count": 2},
                {"user_id": "123456789012345678", "count": 3},
            ]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_sanction_cases_snapshot" and args == ["123", "ban", 10]:
        cases = [{
            "case_id": 7,
            "type": "ban",
            "timestamp": "2026-11-28T09:30:00Z",
            "user_id": "123456789012345678",
            "reason": "spam",
        }]
        if key_id == "spike-sanction-cases-malformed":
            malformed = dict(cases[0])
            malformed["user_id"] = "not-decimal"
            return jsonify({"ok": True, "result": [malformed]})
        return jsonify({"ok": True, "result": cases})
    if method == "api_audit_snapshot" and args == ["123", 10]:
        if key_id == "spike-audit-malformed":
            malformed = dict(results[method][0])
            malformed["actor_name"] = "x" * 129
            return jsonify({"ok": True, "result": [malformed]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_calendar_has_duplicate_event" and args == ["123", "Community event", "2026-11-28T12:00:00+00:00"]:
        if key_id == "spike-calendar-duplicate-malformed":
            return jsonify({"ok": True, "result": "false"})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_calendar_upcoming_events" and args == ["123", "2026-11-28T12:00:00+00:00", 10]:
        if key_id == "spike-dashboard-calendar-malformed":
            malformed = dict(results[method][0])
            malformed["role_id"] = "not-decimal"
            return jsonify({"ok": True, "result": [malformed]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_overview_snapshot" and args in (["123"], ["123", "789"]):
        if key_id == "spike-dashboard-overview-malformed":
            malformed = dict(results[method])
            malformed["roles"] = [{
                "id": "321",
                "name": "Moderator",
                "permissions": {"administrator": "yes"},
            }]
            return jsonify({"ok": True, "result": malformed})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_presence_viewers" and args == ["123"]:
        if key_id == "spike-presence-viewers-malformed":
            return jsonify({"ok": True, "result": ["Loris", "x" * 101]})
        return jsonify({"ok": True, "result": ["Loris", "Alex"]})
    if method == "dashboard_roles_snapshot" and args == ["123"]:
        if key_id == "spike-dashboard-roles-malformed":
            return jsonify({"ok": True, "result": [{"id": "not-decimal", "name": "Moderator"}]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_settings_snapshot" and args == ["123"]:
        if key_id == "spike-dashboard-settings-malformed":
            malformed = dict(results[method])
            malformed["owner_id"] = "not-decimal"
            return jsonify({"ok": True, "result": malformed})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_public_templates_snapshot" and args == []:
        if key_id == "spike-public-templates-malformed":
            malformed = dict(results[method][0])
            malformed["snapshot"] = {"private_config": "must-not-cross-worker-boundary"}
            return jsonify({"ok": True, "result": [malformed]})
        if key_id == "spike-public-templates-duplicate":
            return jsonify({"ok": True, "result": [results[method][0], results[method][0]]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_template_snapshot" and args == ["AbC_123-x"]:
        if key_id == "spike-template-detail-malformed":
            malformed = dict(results[method])
            malformed["snapshot"] = {"private_config": "must-not-cross-worker-boundary"}
            return jsonify({"ok": True, "result": malformed})
        return jsonify({"ok": True, "result": results[method]})
    if method == "dashboard_config_snapshot" and args == ["123"]:
        if key_id == "spike-config-malformed":
            malformed = dict(results[method]); malformed["log_webhook_url"] = "https://example.invalid/secret"
            return jsonify({"ok": True, "result": malformed})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_rest_config_snapshot" and args == ["123"]:
        if key_id == "spike-config-malformed":
            malformed = dict(results[method])
            malformed["api_key"] = "must-not-cross-worker-boundary"
            return jsonify({"ok": True, "result": malformed})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_members_snapshot" and args == ["123"]:
        if key_id == "spike-members-malformed":
            malformed = dict(results[method][0])
            malformed["is_muted"] = 0
            return jsonify({"ok": True, "result": [malformed]})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_faq_snapshot" and args == ["123"]:
        if key_id == "spike-faq-malformed":
            malformed = dict(results[method])
            malformed["entries"] = [{"id": "invalid", "keywords": [], "response": ""}]
            return jsonify({"ok": True, "result": malformed})
        return jsonify({"ok": True, "result": results[method]})
    if method == "api_guild_stats_snapshot" and args in (["123"], ["123", "456"]):
        if key_id == "spike-stats-malformed":
            malformed = dict(results[method])
            malformed["locked"] = "false"
            return jsonify({"ok": True, "result": malformed})
        return jsonify({"ok": True, "result": results[method]})
    if (
        isinstance(method, str)
        and method in {"is_guild_owner", "is_guild_admin"}
        and len(args) == 2
        and all(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) for value in args)
    ):
        if method == "is_guild_owner":
            allowed = args == ["123", "42"]
        else:
            allowed = args[0] == "123" and args[1] in {"42", "43"}
        return jsonify({"ok": True, "result": allowed})
    return jsonify({"error": "unsupported operation"}), 403


@app.post("/__spike/fetch-client/<method>")
def exercise_fetch_bot_service_client(method):
    """Call the local mock bot host through Workers Fetch from inside the Worker."""
    try:
        env = request.environ["workers.env"]
        client = CloudflareFetchBotService(
            base_url="http://127.0.0.1:8787",
            key_id="spike-test",
            secret=_test_bot_service_secret(),
            allow_local_http=True,
        )
        if method == "health_snapshot":
            result = client.health_snapshot()
        elif method == "public_status_snapshot":
            result = client.public_status_snapshot()
        elif method == "public_global_lock_state":
            result = client.public_global_lock_state()
        elif method == "is_guild_owner":
            result = client.is_guild_owner("123", "42")
        elif method == "is_guild_owner_denied":
            result = client.is_guild_owner("123", "43")
        elif method == "is_guild_admin":
            result = client.is_guild_admin("123", "43")
        elif method == "is_guild_admin_denied":
            result = client.is_guild_admin("123", "44")
        elif method == "guild_presence":
            result = client.guild_presence("123")
        elif method == "guild_name":
            result = client.guild_name("123")
        elif method == "api_health_snapshot":
            result = client.api_health_snapshot("123")
        elif method == "api_guild_list_snapshot":
            result = client.api_guild_list_snapshot("123")
        elif method == "api_guild_id_candidates":
            result = client.api_guild_id_candidates()
        elif method == "available_guild_ids":
            result = client.available_guild_ids(["123", "456"])
        elif method == "api_text_channels_snapshot":
            result = client.api_text_channels_snapshot("123")
        elif method == "api_roles_snapshot":
            result = client.api_roles_snapshot("123")
        elif method == "api_roles_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id="spike-role-malformed",
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            invalid_client.api_roles_snapshot("123")
        elif method == "api_guild_stats_snapshot":
            result = client.api_guild_stats_snapshot("123")
        elif method == "api_guild_stats_snapshot_with_ticket_category":
            result = client.api_guild_stats_snapshot("123", 456)
        elif method == "api_guild_stats_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id="spike-stats-malformed",
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            invalid_client.api_guild_stats_snapshot("123")
        elif method == "api_guild_stats_snapshot_invalid_args":
            try:
                client.api_guild_stats_snapshot("123", "not-decimal")
            except ValueError:
                result = {"rejected": True}
            else:
                result = {"rejected": False}
        elif method == "api_faq_snapshot":
            result = client.api_faq_snapshot("123")
        elif method == "api_faq_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id="spike-faq-malformed",
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            invalid_client.api_faq_snapshot("123")
        elif method == "api_faq_snapshot_invalid_args":
            try:
                client.api_faq_snapshot("not-decimal")
            except ValueError:
                result = {"rejected": True}
            else:
                result = {"rejected": False}
        elif method == "dashboard_config_snapshot":
            result = client.dashboard_config_snapshot("123")
        elif method == "dashboard_sanction_stats_snapshot":
            result = client.dashboard_sanction_stats_snapshot("123", days=2)
        elif method == "dashboard_sanction_stats_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-dashboard-sanction-stats-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_sanction_stats_snapshot("123", days=2)
        elif method == "dashboard_sanction_stats_snapshot_invalid_args":
            try: client.dashboard_sanction_stats_snapshot("123", days=True)
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_sanction_types_snapshot":
            result = client.dashboard_sanction_types_snapshot("123")
        elif method == "dashboard_sanction_types_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-dashboard-sanction-types-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_sanction_types_snapshot("123")
        elif method == "dashboard_sanction_types_snapshot_duplicate":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-dashboard-sanction-types-duplicate", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_sanction_types_snapshot("123")
        elif method == "dashboard_sanction_types_snapshot_invalid_args":
            try: client.dashboard_sanction_types_snapshot("not-decimal")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_submitted_appeals_snapshot":
            result = client.dashboard_submitted_appeals_snapshot("123", limit=10)
        elif method == "dashboard_submitted_appeals_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-appeals-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_submitted_appeals_snapshot("123", limit=10)
        elif method == "dashboard_submitted_appeals_snapshot_duplicate":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-appeals-duplicate", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_submitted_appeals_snapshot("123", limit=10)
        elif method == "dashboard_submitted_appeals_snapshot_order":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-appeals-order", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_submitted_appeals_snapshot("123", limit=10)
        elif method == "dashboard_submitted_appeals_snapshot_invalid_args":
            try: client.dashboard_submitted_appeals_snapshot("not-decimal", limit=10)
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "api_member_warnings":
            result = client.api_member_warnings("123", "123456789012345678")
        elif method == "api_member_warnings_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-member-warnings-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.api_member_warnings("123", "123456789012345678")
        elif method == "api_member_warnings_out_of_range":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-member-warnings-out-of-range", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.api_member_warnings("123", "123456789012345678")
        elif method == "api_member_warnings_invalid_args":
            try: client.api_member_warnings("not-decimal", "123456789012345678")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "api_warnlist_snapshot":
            result = client.api_warnlist_snapshot("123")
        elif method == "api_warnlist_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-warnlist-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.api_warnlist_snapshot("123")
        elif method == "api_warnlist_snapshot_duplicate":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-warnlist-duplicate", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.api_warnlist_snapshot("123")
        elif method == "api_warnlist_snapshot_invalid_args":
            try: client.api_warnlist_snapshot("not-decimal")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "api_sanction_cases_snapshot":
            result = client.api_sanction_cases_snapshot("123", "ban", limit=10)
        elif method == "api_sanction_cases_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-sanction-cases-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.api_sanction_cases_snapshot("123", "ban", limit=10)
        elif method == "api_sanction_cases_snapshot_invalid_args":
            try: client.api_sanction_cases_snapshot("not-decimal", "ban", limit=10)
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "api_audit_snapshot":
            result = client.api_audit_snapshot("123", limit=10)
        elif method == "api_audit_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-audit-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.api_audit_snapshot("123", limit=10)
        elif method == "api_audit_snapshot_invalid_args":
            try: client.api_audit_snapshot("not-decimal", limit=10)
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_calendar_has_duplicate_event":
            from datetime import datetime, timezone
            result = client.dashboard_calendar_has_duplicate_event("123", "Community event", datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))
        elif method == "dashboard_calendar_has_duplicate_event_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-calendar-duplicate-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            from datetime import datetime, timezone
            invalid_client.dashboard_calendar_has_duplicate_event("123", "Community event", datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))
        elif method == "dashboard_calendar_has_duplicate_event_invalid_args":
            from datetime import datetime
            try: client.dashboard_calendar_has_duplicate_event("not-decimal", "Community event", datetime(2026, 11, 28))
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_calendar_upcoming_events":
            from datetime import datetime, timezone
            result = client.dashboard_calendar_upcoming_events("123", datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc), limit=10)
        elif method == "dashboard_calendar_upcoming_events_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-dashboard-calendar-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            from datetime import datetime, timezone
            invalid_client.dashboard_calendar_upcoming_events("123", datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc), limit=10)
        elif method == "dashboard_calendar_upcoming_events_invalid_args":
            from datetime import datetime
            try: client.dashboard_calendar_upcoming_events("not-decimal", datetime(2026, 11, 28))
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_overview_snapshot":
            result = client.dashboard_overview_snapshot("123", "789")
        elif method == "dashboard_overview_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-dashboard-overview-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_overview_snapshot("123", "789")
        elif method == "dashboard_overview_snapshot_invalid_args":
            try: client.dashboard_overview_snapshot("not-decimal")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_presence_viewers":
            result = client.dashboard_presence_viewers("123")
        elif method == "dashboard_presence_viewers_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-presence-viewers-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_presence_viewers("123")
        elif method == "dashboard_presence_viewers_invalid_args":
            try: client.dashboard_presence_viewers("not-decimal")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_roles_snapshot":
            result = client.dashboard_roles_snapshot("123")
        elif method == "dashboard_roles_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-dashboard-roles-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_roles_snapshot("123")
        elif method == "dashboard_roles_snapshot_invalid_args":
            try: client.dashboard_roles_snapshot("not-decimal")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_public_templates_snapshot":
            result = client.dashboard_public_templates_snapshot()
        elif method == "dashboard_public_templates_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-public-templates-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_public_templates_snapshot()
        elif method == "dashboard_public_templates_snapshot_duplicate":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-public-templates-duplicate", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_public_templates_snapshot()
        elif method == "dashboard_public_templates_snapshot_invalid_args":
            try: client._dispatch("dashboard_public_templates_snapshot", ["unexpected"], list)
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_template_snapshot":
            result = client.dashboard_template_snapshot("AbC_123-x")
        elif method == "dashboard_template_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-template-detail-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_template_snapshot("AbC_123-x")
        elif method == "dashboard_template_snapshot_invalid_args":
            try: client.dashboard_template_snapshot("unsafe/code")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_settings_snapshot":
            result = client.dashboard_settings_snapshot("123")
        elif method == "dashboard_settings_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-dashboard-settings-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_settings_snapshot("123")
        elif method == "dashboard_settings_snapshot_invalid_args":
            try: client.dashboard_settings_snapshot("not-decimal")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "dashboard_config_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(base_url="http://127.0.0.1:8787", key_id="spike-config-malformed", secret=_test_bot_service_secret(), allow_local_http=True)
            invalid_client.dashboard_config_snapshot("123")
        elif method == "dashboard_config_snapshot_invalid_args":
            try: client.dashboard_config_snapshot("not-decimal")
            except ValueError: result = {"rejected": True}
            else: result = {"rejected": False}
        elif method == "api_rest_config_snapshot":
            result = client.api_rest_config_snapshot("123")
        elif method == "api_rest_config_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id="spike-config-malformed",
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            invalid_client.api_rest_config_snapshot("123")
        elif method == "api_rest_config_snapshot_invalid_args":
            try:
                client.api_rest_config_snapshot("not-decimal")
            except ValueError:
                result = {"rejected": True}
            else:
                result = {"rejected": False}
        elif method == "api_members_snapshot":
            result = client.api_members_snapshot("123")
        elif method == "api_members_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id="spike-members-malformed",
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            invalid_client.api_members_snapshot("123")
        elif method == "api_members_snapshot_invalid_args":
            try:
                client.api_members_snapshot("not-decimal")
            except ValueError:
                result = {"rejected": True}
            else:
                result = {"rejected": False}
        elif method == "api_text_channels_snapshot_invalid":
            invalid_client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id="spike-channel-malformed",
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            invalid_client.api_text_channels_snapshot("123")
        else:
            return jsonify({"error": "unsupported operation"}), 403
    except Exception as exc:
        print(f"Local Worker Fetch BotService probe failed: {type(exc).__name__}", flush=True)
        return jsonify({"error": "local Worker Fetch transport unavailable"}), 503
    return jsonify({"result": result})


@app.post("/__spike/fetch-client-response-validation")
def validate_fetch_bot_service_error_responses():
    """Exercise rejected HTTP responses through the actual Workers Fetch adapter."""
    checks = {}
    for key_id, name in (
        ("spike-redirect", "rejects_redirect"),
        ("spike-unavailable", "rejects_non_200"),
        ("spike-malformed", "rejects_malformed_json"),
        ("spike-oversized", "rejects_oversized_body"),
    ):
        try:
            client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id=key_id,
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            client.health_snapshot()
            checks[name] = False
        except CloudflareBotServiceError:
            checks[name] = True
        except Exception as exc:
            print(
                f"Unexpected local Fetch rejection type: {type(exc).__name__}",
                flush=True,
            )
            checks[name] = False
    return jsonify({"checks": checks, "all_passed": all(checks.values())})


@app.post("/__spike/fetch-client-validation")
def validate_fetch_bot_service_configuration():
    """Exercise fail-closed client configuration checks in the Worker runtime."""
    valid = {
        "key_id": "spike-test",
        "secret": b"x" * 32,
    }
    checks = {}

    def rejected(name, **overrides):
        options = {**valid, **overrides}
        try:
            CloudflareFetchBotService(
                base_url=options.pop("base_url", "https://bot.example"),
                **options,
            )
        except ValueError:
            checks[name] = True
        else:
            checks[name] = False

    rejected("rejects_plain_http_origin", base_url="http://bot.example")
    rejected("rejects_origin_credentials", base_url="https://user:pass@bot.example")
    rejected("rejects_origin_path", base_url="https://bot.example/internal")
    rejected("rejects_origin_query", base_url="https://bot.example?debug=1")
    rejected("rejects_origin_fragment", base_url="https://bot.example#fragment")
    rejected("rejects_invalid_key_id", key_id="invalid/key")
    rejected("rejects_short_secret", secret=b"x" * 31)
    rejected("rejects_local_http_without_opt_in", base_url="http://127.0.0.1:8787")
    rejected(
        "rejects_nonlocal_http_with_opt_in",
        base_url="http://bot.example",
        allow_local_http=True,
    )

    try:
        client = CloudflareFetchBotService(
            base_url="https://bot.example",
            key_id=valid["key_id"],
            secret=valid["secret"],
        )
        client._dispatch_snapshot("api_ban_member")
        checks["rejects_unallowlisted_operation"] = False
    except ValueError:
        checks["rejects_unallowlisted_operation"] = True

    return jsonify({"checks": checks, "all_passed": all(checks.values())})


@app.get("/")
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "nexus-cloudflare-spike"})


@app.post("/__spike/setup")
def setup_schema():
    """Create disposable local-only tables used by the smoke test."""
    schema = [
        """
        CREATE TABLE IF NOT EXISTS spike_oauth_states (
            state_hash TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_sessions (
            session_hash TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            rotated_to TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_batch_probe (
            probe_key TEXT PRIMARY KEY
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_flask_sessions (
            session_hash TEXT PRIMARY KEY,
            ciphertext_hex TEXT NOT NULL,
            nonce_hex TEXT NOT NULL,
            key_version INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            idle_expires_at INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            revoked_at INTEGER,
            rotated_to TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_encrypted_sessions (
            session_hash TEXT PRIMARY KEY,
            ciphertext_hex TEXT NOT NULL,
            nonce_hex TEXT NOT NULL,
            key_version INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            idle_expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        )
        """,
    ]
    try:
        db = _db()
        for statement in schema:
            _await(db.prepare(statement).run())
    except Exception as exc:
        print(f"Local D1 schema setup failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": "local D1 setup failed", "cause": type(exc).__name__}), 503
    return jsonify({"ready": True})



@app.post("/__spike/flask-session/invalid-payload/<kind>")
def create_invalid_flask_session_payload(kind):
    """Exercise fail-closed serialization and payload-size limits."""
    if kind == "unserializable":
        session["unsupported_value"] = object()
    elif kind == "oversized":
        session["oversized_value"] = "x" * 16_385
    else:
        return jsonify({"error": "unknown test payload"}), 400
    return jsonify({"prepared": True})


@app.post("/__spike/flask-session/create-legacy-key")
def create_legacy_key_flask_session():
    """Insert a test session encrypted with the previous D1 key version."""
    sid = secrets.token_urlsafe(32)
    session_hash = app.session_interface._digest(sid)
    active_version = app.session_interface.active_key_version
    try:
        app.session_interface.active_key_version = 1
        _, ciphertext_hex, nonce_hex, key_version = app.session_interface._encrypt(
            sid,
            {
                "access_token": "spike-legacy-key-access-token",
                "csrf_token": "spike-legacy-key-csrf-secret",
            },
        )
    finally:
        app.session_interface.active_key_version = active_version
    now = int(time.time())
    try:
        _await(
            _db().prepare(
                """
                INSERT INTO spike_flask_sessions
                    (session_hash, ciphertext_hex, nonce_hex, key_version,
                     created_at, expires_at, idle_expires_at, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """
            ).bind(
                session_hash,
                ciphertext_hex,
                nonce_hex,
                key_version,
                now,
                now + app.session_interface.absolute_timeout,
                now + app.session_interface.idle_timeout,
            ).run()
        )
    except Exception:
        return jsonify({"error": "legacy session fixture unavailable"}), 503
    response = jsonify({"created": True, "key_version": key_version})
    response.set_cookie(
        app.config["SESSION_COOKIE_NAME"],
        sid,
        httponly=True,
        secure=app.config["SESSION_COOKIE_SECURE"],
        samesite="Lax",
        path="/",
    )
    return response


@app.post("/__spike/flask-session/touch-key-version")
def touch_key_version_flask_session():
    session["key_rotation_probe"] = True
    return jsonify({"touched": True})


@app.get("/__spike/flask-session/key-version")
def inspect_flask_session_key_version():
    sid = request.cookies.get(app.config["SESSION_COOKIE_NAME"], "")
    if not sid:
        return jsonify({"key_version": None}), 400
    try:
        row = _await(
            _db().prepare(
                "SELECT key_version FROM spike_flask_sessions WHERE session_hash = ? AND revoked_at IS NULL"
            ).bind(app.session_interface._digest(sid)).first()
        )
    except Exception:
        return jsonify({"error": "session key lookup unavailable"}), 503
    return jsonify({
        "key_version": int(_row_value(row, "key_version")) if row else None
    })


@app.post("/__spike/flask-session/login")
def create_flask_session():
    """Create a disposable Flask session containing a fake bearer credential."""
    session["access_token"] = "spike-only-discord-access-token"
    session["csrf_token"] = "spike-only-csrf-secret"
    return jsonify({"authenticated": True})


@app.post("/__spike/flask-session/oauth-login")
def create_oauth_state_session():
    """Create a disposable session for atomic one-time OAuth-state tests."""
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session["oauth_state_created_at"] = time.time()
    session["oauth_code_verifier"] = secrets.token_urlsafe(48)
    return jsonify({"state": state})


@app.post("/__spike/flask-session/oauth-consume")
def consume_oauth_state_session():
    state = _json_body().get("state")
    consumed = app.session_interface.consume_oauth_state(session, state)
    return jsonify({"consumed": consumed})


@app.get("/__spike/flask-session/me")
def inspect_flask_session():
    return jsonify({
        "authenticated": bool(session.get("access_token")),
        "access_token": session.get("access_token"),
        "csrf_token": session.get("csrf_token"),
    })


@app.post("/__spike/flask-session/rotate")
def rotate_flask_session():
    app.session_interface.rotate_session(session)
    session["access_token"] = "spike-rotated-discord-access-token"
    return jsonify({"rotated": True})



@app.post("/__spike/flask-session/tamper")
def tamper_flask_session():
    sid = request.cookies.get(app.config["SESSION_COOKIE_NAME"], "")
    if not sid:
        return jsonify({"tampered": False}), 400
    try:
        _await(
            _db()
            .prepare(
                "UPDATE spike_flask_sessions SET ciphertext_hex = '00' WHERE session_hash = ?"
            )
            .bind(_digest(sid))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"tampered": True})



def _expire_flask_session(field):
    """Expire one D1-backed Flask session without deleting its record."""
    sid = request.cookies.get(app.config["SESSION_COOKIE_NAME"], "")
    if not sid or field not in {"idle_expires_at", "expires_at"}:
        return jsonify({"expired": False}), 400
    try:
        _await(
            _db()
            .prepare(
                f"UPDATE spike_flask_sessions SET {field} = ? WHERE session_hash = ?"
            )
            .bind(int(time.time()) - 1, _digest(sid))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"expired": True})


@app.post("/__spike/flask-session/expire-idle")
def expire_flask_session_idle():
    return _expire_flask_session("idle_expires_at")


@app.post("/__spike/flask-session/expire-absolute")
def expire_flask_session_absolute():
    return _expire_flask_session("expires_at")


@app.post("/__spike/flask-session/stale-write")
def force_stale_flask_session_write():
    """Force a revision conflict after this request loaded its session."""
    sid = request.cookies.get(app.config["SESSION_COOKIE_NAME"], "")
    if not sid:
        return jsonify({"stale": False}), 400
    try:
        _await(
            _db()
            .prepare(
                """
                UPDATE spike_flask_sessions
                SET revision = revision + 1
                WHERE session_hash = ? AND revoked_at IS NULL
                """
            )
            .bind(_digest(sid))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    session["stale_write_probe"] = "must-not-persist"
    return jsonify({"stale": True})


@app.post("/__spike/flask-session/storage-failure")
def fail_flask_session_storage():
    original_getter = app.session_interface._db_getter
    app.session_interface._db_getter = lambda: (_ for _ in ()).throw(
        RuntimeError("disposable D1 outage")
    )
    try:
        app.session_interface.revoke_session(session)
    finally:
        app.session_interface._db_getter = original_getter
    return jsonify({"unexpected": True}), 500


@app.post("/__spike/flask-session/logout")
def revoke_flask_session():
    app.session_interface.revoke_session(session)
    session.clear()
    return jsonify({"logged_out": True})


@app.post("/__spike/state")
def create_state():
    raw_state = secrets.token_urlsafe(32)
    now = int(time.time())
    try:
        _await(
            _db()
            .prepare(
                "INSERT INTO spike_oauth_states (state_hash, expires_at) VALUES (?, ?)"
            )
            .bind(_digest(raw_state), now + 120)
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"state": raw_state})


@app.post("/__spike/state/consume")
def consume_state():
    raw_state = _json_body().get("state")
    if not isinstance(raw_state, str) or not raw_state or len(raw_state) > 256:
        return jsonify({"error": "invalid state"}), 400

    now = int(time.time())
    try:
        row = _await(
            _db()
            .prepare(
                """
                UPDATE spike_oauth_states
                SET consumed_at = ?
                WHERE state_hash = ? AND consumed_at IS NULL AND expires_at > ?
                RETURNING state_hash
                """
            )
            .bind(now, _digest(raw_state), now)
            .first()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"consumed": row is not None})


@app.post("/__spike/session")
def create_session():
    raw_id = secrets.token_urlsafe(32)
    now = int(time.time())
    try:
        _await(
            _db()
            .prepare(
                """
                INSERT INTO spike_sessions (session_hash, payload, expires_at)
                VALUES (?, ?, ?)
                """
            )
            .bind(_digest(raw_id), "non-sensitive-spike-fixture", now + 3600)
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"session_id": raw_id})


@app.post("/__spike/session/rotate")
def rotate_session():
    old_id = _json_body().get("session_id")
    if not isinstance(old_id, str) or not old_id or len(old_id) > 256:
        return jsonify({"error": "invalid session"}), 400

    old_hash = _digest(old_id)
    new_id = secrets.token_urlsafe(32)
    new_hash = _digest(new_id)
    now = int(time.time())
    db = _db()
    try:
        # The conditional update claims the old record for this unique new ID.
        # The following INSERT is conditional on that exact claim. D1 batch
        # makes both statements one transaction, so partial rotation rolls back.
        _await(
            db.batch(
                [
                    db.prepare(
                        """
                        UPDATE spike_sessions
                        SET revoked_at = ?, rotated_to = ?
                        WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                        """
                    ).bind(now, new_hash, old_hash, now),
                    db.prepare(
                        """
                        INSERT INTO spike_sessions (session_hash, payload, expires_at)
                        SELECT ?, ?, ?
                        WHERE EXISTS (
                            SELECT 1 FROM spike_sessions
                            WHERE session_hash = ? AND rotated_to = ?
                        )
                        """
                    ).bind(
                        new_hash,
                        "non-sensitive-spike-fixture",
                        now + 3600,
                        old_hash,
                        new_hash,
                    ),
                ]
            )
        )
        created = _await(
            db.prepare(
                """
                SELECT session_hash FROM spike_sessions
                WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                LIMIT 1
                """
            )
            .bind(new_hash, now)
            .first()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503

    if created is None:
        return jsonify({"error": "session could not be rotated"}), 409
    return jsonify({"session_id": new_id})


@app.get("/__spike/session/<session_id>")
def read_session(session_id):
    if not isinstance(session_id, str) or len(session_id) > 256:
        return jsonify({"active": False})
    now = int(time.time())
    try:
        row = _await(
            _db()
            .prepare(
                """
                SELECT session_hash FROM spike_sessions
                WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                LIMIT 1
                """
            )
            .bind(_digest(session_id), now)
            .first()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"active": row is not None})


@app.post("/__spike/batch-rollback")
def batch_rollback():
    """Prove that a failed D1 batch does not leave its first write committed."""
    marker = secrets.token_hex(16)
    db = _db()
    try:
        _await(
            db.batch(
                [
                    db.prepare(
                        "INSERT INTO spike_batch_probe (probe_key) VALUES (?)"
                    ).bind(marker),
                    db.prepare(
                        "INSERT INTO spike_batch_probe (probe_key) VALUES (?)"
                    ).bind(marker),
                ]
            )
        )
        return jsonify({"rolled_back": False}), 500
    except Exception:
        try:
            count = _await(
                db.prepare(
                    "SELECT COUNT(*) AS count FROM spike_batch_probe WHERE probe_key = ?"
                )
                .bind(marker)
                .first("count")
            )
        except Exception:
            return jsonify({"error": "local D1 unavailable"}), 503
        return jsonify({"rolled_back": count == 0})


def _test_encryption_key(key_version: int) -> bytes:
    """Read one disposable versioned key supplied by the local smoke workflow."""
    env = request.environ["workers.env"]
    raw_key = str(getattr(env, f"SPIKE_TEST_ENCRYPTION_KEY_V{key_version}"))
    try:
        key = bytes.fromhex(raw_key)
    except ValueError as exc:
        raise RuntimeError("local test encryption key is malformed") from exc
    if len(key) != 32:
        raise RuntimeError("local test encryption key must be 32 bytes")
    return key

def _session_aad(session_hash: str, key_version: int) -> bytes:
    return f"nexus-spike:session:v{key_version}:{session_hash}".encode("ascii")


def _row_value(row, name):
    try:
        return row[name]
    except (KeyError, TypeError):
        return getattr(row, name)


def _create_encrypted_session(key_version: int):
    raw_id = secrets.token_urlsafe(32)
    session_hash = _digest(raw_id)
    nonce = secrets.token_bytes(12)
    fixture = b"nexus-cloudflare-spike-persisted-non-sensitive-fixture"
    now = int(time.time())
    ciphertext = aes_gcm_encrypt(
        _test_encryption_key(key_version),
        nonce,
        fixture,
        _session_aad(session_hash, key_version),
    )
    _await(
        _db()
        .prepare(
            """
            INSERT INTO spike_encrypted_sessions
                (session_hash, ciphertext_hex, nonce_hex, key_version,
                 expires_at, idle_expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """
        )
        .bind(
            session_hash,
            ciphertext.hex(),
            nonce.hex(),
            key_version,
            now + 3600,
            now + 900,
        )
        .run()
    )
    return raw_id


@app.post("/__spike/encrypted-session")
def create_encrypted_session():
    """Create new records with the active local test key (version 2)."""
    try:
        raw_id = _create_encrypted_session(2)
    except Exception as exc:
        print(f"Local encrypted D1 session creation failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": "local encrypted session unavailable"}), 503
    return jsonify({"session_id": raw_id, "key_version": 2})


@app.post("/__spike/encrypted-session/legacy")
def create_legacy_encrypted_session():
    """Create a version-1 fixture to exercise a version-1 to version-2 rotation."""
    try:
        raw_id = _create_encrypted_session(1)
    except Exception:
        return jsonify({"error": "local encrypted session unavailable"}), 503
    return jsonify({"session_id": raw_id, "key_version": 1})

@app.get("/__spike/encrypted-session/<session_id>")
def read_encrypted_session(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"active": False})
    now = int(time.time())
    session_hash = _digest(session_id)
    try:
        row = _await(
            _db()
            .prepare(
                """
                UPDATE spike_encrypted_sessions
                SET idle_expires_at = MIN(expires_at, ?)
                WHERE session_hash = ? AND revoked_at IS NULL
                  AND expires_at > ? AND idle_expires_at > ?
                RETURNING ciphertext_hex, nonce_hex, key_version
                """
            )
            .bind(now + 900, session_hash, now, now)
            .first()
        )
        if row is None:
            return jsonify({"active": False})
        key_version = int(_row_value(row, "key_version"))
        ciphertext = bytes.fromhex(_row_value(row, "ciphertext_hex"))
        nonce = bytes.fromhex(_row_value(row, "nonce_hex"))
        plaintext = aes_gcm_decrypt(
            _test_encryption_key(key_version),
            nonce,
            ciphertext,
            _session_aad(session_hash, key_version),
        )
    except Exception:
        # Missing keys, malformed rows, expiry, and authentication failures fail closed.
        return jsonify({"active": False})
    fixture = b"nexus-cloudflare-spike-persisted-non-sensitive-fixture"
    return jsonify({"active": plaintext == fixture})


@app.post("/__spike/encrypted-session/<session_id>/rotate-key")
def rotate_encrypted_session_key(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"rotated": False}), 400
    now = int(time.time())
    session_hash = _digest(session_id)
    db = _db()
    try:
        row = _await(
            db.prepare(
                """
                SELECT ciphertext_hex, nonce_hex, key_version
                FROM spike_encrypted_sessions
                WHERE session_hash = ? AND revoked_at IS NULL
                  AND expires_at > ? AND idle_expires_at > ?
                LIMIT 1
                """
            )
            .bind(session_hash, now, now)
            .first()
        )
        if row is None:
            return jsonify({"rotated": False}), 404
        old_version = int(_row_value(row, "key_version"))
        if old_version == 2:
            return jsonify({"rotated": False, "key_version": 2})
        ciphertext = bytes.fromhex(_row_value(row, "ciphertext_hex"))
        old_nonce = bytes.fromhex(_row_value(row, "nonce_hex"))
        plaintext = aes_gcm_decrypt(
            _test_encryption_key(old_version),
            old_nonce,
            ciphertext,
            _session_aad(session_hash, old_version),
        )
        new_version = 2
        new_nonce = secrets.token_bytes(12)
        new_ciphertext = aes_gcm_encrypt(
            _test_encryption_key(new_version),
            new_nonce,
            plaintext,
            _session_aad(session_hash, new_version),
        )
        _await(
            db.prepare(
                """
                UPDATE spike_encrypted_sessions
                SET ciphertext_hex = ?, nonce_hex = ?, key_version = ?
                WHERE session_hash = ? AND key_version = ? AND revoked_at IS NULL
                  AND expires_at > ? AND idle_expires_at > ?
                """
            )
            .bind(
                new_ciphertext.hex(),
                new_nonce.hex(),
                new_version,
                session_hash,
                old_version,
                now,
                now,
            )
            .run()
        )
        current = _await(
            db.prepare(
                "SELECT key_version FROM spike_encrypted_sessions WHERE session_hash = ?"
            )
            .bind(session_hash)
            .first()
        )
        current_version = int(_row_value(current, "key_version")) if current else None
    except Exception:
        return jsonify({"error": "local key rotation unavailable"}), 503
    if current_version != new_version:
        return jsonify({"rotated": False}), 409
    return jsonify({"rotated": True, "key_version": new_version})


@app.post("/__spike/encrypted-session/<session_id>/revoke")
def revoke_encrypted_session(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"revoked": False}), 400
    try:
        _await(
            _db()
            .prepare(
                "UPDATE spike_encrypted_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL"
            )
            .bind(int(time.time()), _digest(session_id))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"revoked": True})


@app.post("/__spike/encrypted-session/<session_id>/expire-idle")
def expire_encrypted_session_idle(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"expired": False}), 400
    try:
        _await(
            _db()
            .prepare(
                "UPDATE spike_encrypted_sessions SET idle_expires_at = ? WHERE session_hash = ?"
            )
            .bind(int(time.time()) - 1, _digest(session_id))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"expired": True})

@app.post("/__spike/encrypted-session/<session_id>/tamper")
def tamper_encrypted_session(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"updated": False}), 400
    try:
        result = _await(
            _db()
            .prepare(
                """
                UPDATE spike_encrypted_sessions
                SET ciphertext_hex = '00'
                WHERE session_hash = ? AND revoked_at IS NULL
                """
            )
            .bind(_digest(session_id))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"updated": True})


@app.post("/__spike/crypto")
def crypto_compatibility():
    """Exercise Workers Web Crypto through Python FFI with disposable fixtures."""
    import hashlib

    fixture = b"nexus-cloudflare-spike-non-sensitive-fixture"
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    aad = b"nexus-spike:v1:session-digest-fixture"
    try:
        digest_matches = webcrypto_sha256(fixture) == hashlib.sha256(fixture).digest()
        ciphertext = aes_gcm_encrypt(key, nonce, fixture, aad)
        round_trip = aes_gcm_decrypt(key, nonce, ciphertext, aad) == fixture

        tampered = bytearray(ciphertext)
        tampered[-1] ^= 1
        try:
            aes_gcm_decrypt(key, nonce, bytes(tampered), aad)
            tampered_rejected = False
        except Exception:
            tampered_rejected = True

        wrong_key = bytearray(key)
        wrong_key[0] ^= 1
        try:
            aes_gcm_decrypt(bytes(wrong_key), nonce, ciphertext, aad)
            wrong_key_rejected = False
        except Exception:
            wrong_key_rejected = True

        try:
            aes_gcm_decrypt(key, nonce, ciphertext, aad + b"-wrong")
            wrong_aad_rejected = False
        except Exception:
            wrong_aad_rejected = True
    except Exception as exc:
        print(f"Local Web Crypto compatibility failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": "local Web Crypto unavailable", "cause": type(exc).__name__}), 503

    return jsonify(
        {
            "sha256_matches": digest_matches,
            "round_trip": round_trip,
            "tampered_ciphertext_rejected": tampered_rejected,
            "wrong_key_rejected": wrong_key_rejected,
            "wrong_aad_rejected": wrong_aad_rejected,
        }
    )


Default = wsgi.entrypoint(app)
