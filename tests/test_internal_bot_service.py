import json
import time

import pytest
from threading import Lock

from flask import Flask
from pymongo.errors import DuplicateKeyError

from web.bot_service_signing import sign_internal_request
from web.internal_bot_service import (
    MongoNonceStore,
    create_internal_bot_service_blueprint,
    internal_bot_service_app_options,
)


SECRET = b"internal-bot-service-test-secret-32-bytes"
KEYRING = {"current": SECRET}
PATH = "/internal/v1/dispatch"


class MemoryNonceStore:
    def __init__(self, fail=False):
        self.seen = set()
        self.lock = Lock()
        self.fail = fail

    def consume(self, key_id, nonce, expires_at):
        if self.fail:
            raise RuntimeError("store unavailable")
        with self.lock:
            key = (key_id, nonce)
            if key in self.seen:
                return False
            self.seen.add(key)
            return True


class FakeBotService:
    def __init__(self):
        self.calls = []

    def health_snapshot(self):
        self.calls.append("health_snapshot")
        return {"bot": "online", "guild_count": 4}

    def public_status_snapshot(self):
        self.calls.append("public_status_snapshot")
        return {"online": True, "guild_count": 4, "latency_ms": 23, "uptime": "1h"}

    def is_guild_owner(self, guild_id, user_id):
        self.calls.append(("is_guild_owner", guild_id, user_id))
        return guild_id == "123" and user_id == "42"

    def is_guild_admin(self, guild_id, user_id):
        self.calls.append(("is_guild_admin", guild_id, user_id))
        return guild_id == "123" and user_id in {"42", "43"}


    def guild_presence(self, guild_id):
        self.calls.append(("guild_presence", guild_id))
        return {"bot_ready": True, "guild_present": guild_id == "123"}

    def guild_name(self, guild_id):
        self.calls.append(("guild_name", guild_id))
        return "Test Server" if guild_id == "123" else "the server"

    def api_health_snapshot(self, guild_id=None):
        self.calls.append(("api_health_snapshot", guild_id))
        return {"online": True, "locked": False, "guild_count": 1 if guild_id else 4}

    def api_guild_list_snapshot(self, guild_id=None):
        self.calls.append(("api_guild_list_snapshot", guild_id))
        if guild_id:
            return [{
                "id": guild_id,
                "name": "Test Server",
                "member_count": 42,
                "icon_url": None,
            }]
        return [{
            "id": "123",
            "name": "Test Server",
            "member_count": 42,
            "icon_url": None,
        }]

    def api_guild_id_candidates(self):
        self.calls.append("api_guild_id_candidates")
        return ["123", "456"]

    def available_guild_ids(self, candidate_ids):
        self.calls.append(("available_guild_ids", candidate_ids))
        return [guild_id for guild_id in candidate_ids if guild_id == "123"]



def _request_body(payload=None):
    if payload is None:
        payload = {"method": "health_snapshot", "args": [], "kwargs": {}}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _signed_headers(body, *, timestamp=None, nonce="n" * 24):
    issued_at = int(time.time()) if timestamp is None else timestamp
    headers = sign_internal_request(
        secret=SECRET,
        key_id="current",
        method="POST",
        path=PATH,
        body=body,
        timestamp=issued_at,
        nonce=nonce,
    )
    headers["Content-Type"] = "application/json"
    return headers


def _client(nonce_store=None):
    service = FakeBotService()
    app = Flask(__name__)
    app.register_blueprint(
        create_internal_bot_service_blueprint(
            service=service,
            keyring=KEYRING,
            nonce_store=nonce_store or MemoryNonceStore(),
        )
    )
    return app.test_client(), service


def test_signed_dispatch_returns_health_snapshot():
    client, service = _client()
    body = _request_body()
    response = client.post(PATH, data=body, headers=_signed_headers(body))

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "result": {"bot": "online", "guild_count": 4},
    }
    assert service.calls == ["health_snapshot"]


def test_signed_dispatch_allows_public_status_snapshot():
    client, service = _client()
    body = _request_body(
        {"method": "public_status_snapshot", "args": [], "kwargs": {}}
    )
    response = client.post(PATH, data=body, headers=_signed_headers(body))

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "result": {
            "online": True,
            "guild_count": 4,
            "latency_ms": 23,
            "uptime": "1h",
        },
    }
    assert service.calls == ["public_status_snapshot"]


def test_signed_dispatch_checks_guild_owner_with_server_side_bot_service():
    client, service = _client()
    body = _request_body({"method": "is_guild_owner", "args": ["123", "42"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": True}
    assert service.calls == [("is_guild_owner", "123", "42")]


def test_signed_dispatch_returns_false_for_non_owner():
    client, service = _client()
    body = _request_body({"method": "is_guild_owner", "args": ["123", "43"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": False}
    assert service.calls == [("is_guild_owner", "123", "43")]


@pytest.mark.parametrize("args", [[], ["123"], ["123", "42", "extra"], [123, "42"], ["123", 42], ["guild", "42"], ["123", "42\\n"]])
def test_signed_dispatch_rejects_invalid_guild_owner_arguments(args):
    client, service = _client()
    body = _request_body({"method": "is_guild_owner", "args": args, "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


def test_signed_dispatch_checks_guild_administrator_permission():
    client, service = _client()
    body = _request_body({"method": "is_guild_admin", "args": ["123", "43"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200 and response.get_json() == {"ok": True, "result": True}
    assert service.calls == [("is_guild_admin", "123", "43")]


def test_signed_dispatch_denies_non_administrator_member():
    client, service = _client()
    body = _request_body({"method": "is_guild_admin", "args": ["123", "44"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200 and response.get_json() == {"ok": True, "result": False}
    assert service.calls == [("is_guild_admin", "123", "44")]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["123"],
        ["123", "43", "extra"],
        [123, "43"],
        ["123", 43],
        ["guild", "43"],
        ["123", "43\\n"],
    ],
)
def test_signed_dispatch_rejects_invalid_guild_admin_arguments(args):
    client, service = _client()
    body = _request_body({"method": "is_guild_admin", "args": args, "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("is_guild_owner", ["123", "42"]),
        ("is_guild_admin", ["123", "43"]),
    ],
)
def test_signed_dispatch_fails_closed_when_authorization_result_is_not_boolean(method, args):
    client, service = _client()
    setattr(service, method, lambda guild_id, user_id: "true")
    body = _request_body({"method": method, "args": args, "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body, nonce="m" * 24))
    assert response.status_code == 503
    assert response.get_json() == {"error": "internal service unavailable"}


def test_replayed_nonce_is_rejected_before_second_dispatch():
    store = MemoryNonceStore()
    client, service = _client(store)
    body = _request_body()
    headers = _signed_headers(body)

    first = client.post(PATH, data=body, headers=headers)
    second = client.post(PATH, data=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 409
    assert service.calls == ["health_snapshot"]


def test_invalid_signature_and_expired_timestamp_are_rejected():
    client, service = _client()
    body = _request_body()
    bad_signature = _signed_headers(body)
    bad_signature["X-Nexus-Signature"] = "0" * 64
    assert client.post(PATH, data=body, headers=bad_signature).status_code == 401

    expired = _signed_headers(body, timestamp=int(time.time()) - 61, nonce="e" * 24)
    assert client.post(PATH, data=body, headers=expired).status_code == 401
    assert service.calls == []


def test_only_allowlisted_read_method_is_dispatchable():
    client, service = _client()
    body = _request_body({"method": "ban_member", "args": [], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


@pytest.mark.parametrize("method", [["health_snapshot"], {"name": "health_snapshot"}, None])
def test_non_string_operation_names_are_rejected(method):
    client, service = _client()
    body = _request_body({"method": method, "args": [], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


def test_invalid_payload_query_and_content_type_are_rejected():
    client, service = _client()
    body = b"not-json"
    assert client.post(PATH, data=body, headers=_signed_headers(body)).status_code == 400

    valid_body = _request_body()
    headers = _signed_headers(valid_body)
    headers["Content-Type"] = "text/plain"
    assert client.post(PATH, data=valid_body, headers=headers).status_code == 415
    assert client.post(PATH + "?debug=1", data=valid_body, headers=_signed_headers(valid_body)).status_code == 400
    assert service.calls == []


def test_oversized_body_and_nonce_store_outage_fail_closed():
    client, service = _client()
    oversized = b"x" * (1_048_577)
    assert client.post(
        PATH,
        data=oversized,
        headers={"Content-Type": "application/json"},
    ).status_code == 413
    assert service.calls == []

    failing_client, failing_service = _client(MemoryNonceStore(fail=True))
    body = _request_body()
    response = failing_client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503
    assert failing_service.calls == []



def test_signed_dispatch_returns_guild_presence_snapshot():
    client, service = _client()
    body = _request_body({"method": "guild_presence", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "result": {"bot_ready": True, "guild_present": True},
    }
    assert service.calls == [("guild_presence", "123")]


def test_signed_dispatch_returns_guild_name():
    client, service = _client()
    body = _request_body({"method": "guild_name", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": "Test Server"}
    assert service.calls == [("guild_name", "123")]


def test_signed_dispatch_returns_api_key_lookup_candidates():
    client, service = _client()
    body = _request_body({"method": "api_guild_id_candidates", "args": [], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": ["123", "456"]}
    assert service.calls == ["api_guild_id_candidates"]


def test_signed_dispatch_filters_available_guild_ids():
    client, service = _client()
    body = _request_body({
        "method": "available_guild_ids",
        "args": [["123", "456"]],
        "kwargs": {},
    })
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": ["123"]}
    assert service.calls == [("available_guild_ids", ["123", "456"])]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("guild_presence", []),
        ("guild_presence", ["invalid"]),
        ("guild_name", ["123", "456"]),
        ("api_guild_id_candidates", ["123"]),
        ("available_guild_ids", []),
        ("available_guild_ids", [["123", "invalid"]]),
        ("available_guild_ids", [["123"] * 501]),
        ("api_health_snapshot", ["invalid"]),
        ("api_guild_list_snapshot", ["invalid"]),
    ],
)
def test_signed_dispatch_rejects_invalid_read_operation_arguments(method, args):
    client, service = _client()
    body = _request_body({"method": method, "args": args, "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


def test_signed_dispatch_returns_optional_api_snapshots():
    client, service = _client()
    health_body = _request_body({"method": "api_health_snapshot", "args": ["123"], "kwargs": {}})
    health = client.post(PATH, data=health_body, headers=_signed_headers(health_body))
    assert health.status_code == 200
    assert health.get_json() == {"ok": True, "result": {"online": True, "locked": False, "guild_count": 1}}

    guilds_body = _request_body({"method": "api_guild_list_snapshot", "args": ["123"], "kwargs": {}})
    guilds = client.post(PATH, data=guilds_body, headers=_signed_headers(guilds_body, nonce="g" * 24))
    assert guilds.status_code == 200
    assert guilds.get_json() == {
        "ok": True,
        "result": [{
            "id": "123",
            "name": "Test Server",
            "member_count": 42,
            "icon_url": None,
        }],
    }


def test_signed_dispatch_fails_closed_on_malformed_read_operation_results():
    client, service = _client()
    service.guild_presence = lambda _guild_id: {"bot_ready": "yes", "guild_present": True}
    body = _request_body({"method": "guild_presence", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503
    assert response.get_json() == {"error": "internal service unavailable"}


class FakeMongoCollection:
    def __init__(self):
        self.indexes = []
        self.documents = set()

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def insert_one(self, document):
        key = (document["key_id"], document["nonce"])
        if key in self.documents:
            raise DuplicateKeyError("duplicate nonce")
        self.documents.add(key)


def test_mongo_nonce_store_creates_unique_and_ttl_indexes_and_rejects_replay():
    collection = FakeMongoCollection()
    store = MongoNonceStore(lambda: collection)

    assert store.consume("current", "n" * 24, int(time.time()) + 60) is True
    assert store.consume("current", "n" * 24, int(time.time()) + 60) is False

    assert len(collection.indexes) == 2
    assert collection.indexes[0][1]["unique"] is True
    assert collection.indexes[1][1]["expireAfterSeconds"] == 0
    assert collection.indexes[1][0] == "expires_at"


def test_mongo_nonce_store_fails_closed_when_collection_is_unavailable():
    store = MongoNonceStore(lambda: None)
    try:
        store.consume("current", "n" * 24, int(time.time()) + 60)
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("Unavailable Mongo collection must fail closed")


def test_internal_service_options_are_disabled_by_default():
    service_calls = []
    options = internal_bot_service_app_options(
        environ={},
        service_factory=lambda: service_calls.append("created"),
        nonce_collection_provider=lambda: None,
    )
    assert options == {}
    assert service_calls == []


def test_internal_service_options_require_valid_keyring_and_wire_dependencies():
    service = FakeBotService()
    collection = FakeMongoCollection()
    options = internal_bot_service_app_options(
        environ={
            "NEXUS_BOT_SERVICE_INTERNAL_ENABLED": "true",
            "NEXUS_BOT_SERVICE_HMAC_KEYS": json.dumps({"current": SECRET.hex()}),
        },
        service_factory=lambda: service,
        nonce_collection_provider=lambda: collection,
    )

    assert options["bot_service"] is service
    assert options["internal_bot_service"] is service
    assert options["internal_bot_service_keyring"] == {"current": SECRET}
    assert isinstance(options["internal_nonce_store"], MongoNonceStore)


def test_internal_service_enabled_with_missing_keyring_fails_closed():
    service_calls = []
    with pytest.raises(ValueError, match="keyring"):
        internal_bot_service_app_options(
            environ={"NEXUS_BOT_SERVICE_INTERNAL_ENABLED": "true"},
            service_factory=lambda: service_calls.append("created"),
            nonce_collection_provider=lambda: None,
        )
    assert service_calls == []


def test_signed_dispatch_allows_dashboard_overview_snapshot_with_valid_dto():
    dto = {
        "id": "123", "name": "Test Server", "owner_id": "42",
        "text_channels": [{"id": "111", "name": "general"}],
        "roles": [{"id": "222", "name": "Admin",
            "permissions": {"administrator": True, "manage_guild": False, "ban_members": False,
                "kick_members": False, "manage_roles": False}}],
        "role_names": {"222": "Admin"}, "open_tickets": [],
    }
    client, service = _client()
    service.dashboard_overview_snapshot = lambda guild_id, category=None: dto
    body = _request_body({"method": "dashboard_overview_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": dto}


def test_signed_dispatch_rejects_malformed_dashboard_overview_dto():
    client, service = _client()
    service.dashboard_overview_snapshot = lambda _guild_id: {"id": "123", "name": "Bad"}
    body = _request_body({"method": "dashboard_overview_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503
    assert response.get_json() == {"error": "internal service unavailable"}



def test_signed_dispatch_allows_dashboard_settings_snapshot_with_valid_dto():
    dto = {
        "id": "123", "name": "Test Server", "owner_id": "42",
        "text_channels": [{"id": "111", "name": "general"}],
        "voice_channels": [{"id": "112", "name": "General"}],
        "roles": [{"id": "222", "name": "Admin"}],
    }
    client, service = _client()
    service.dashboard_settings_snapshot = lambda guild_id: dto
    body = _request_body({"method": "dashboard_settings_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": dto}


def test_signed_dispatch_rejects_malformed_dashboard_settings_snapshot():
    client, service = _client()
    service.dashboard_settings_snapshot = lambda _guild_id: {
        "id": "123", "name": "Bad", "owner_id": "42",
        "text_channels": [], "voice_channels": [{"id": "not-valid", "name": "General"}],
        "roles": [],
    }
    body = _request_body({"method": "dashboard_settings_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503
    assert response.get_json() == {"error": "internal service unavailable"}



@pytest.mark.parametrize("days", [0, 31, -1, "2", 2.0, True, False])
def test_signed_dispatch_rejects_invalid_dashboard_sanction_stats_arguments(days):
    client, service = _client()
    body = _request_body({"method": "dashboard_sanction_stats_snapshot", "args": ["123", days], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


def test_signed_dispatch_serializes_dashboard_sanction_stats_as_validated_dto():
    client, service = _client()
    service.dashboard_sanction_stats_snapshot = lambda guild_id, days: [("01/11", 0), ("02/11", 3)]
    body = _request_body({"method": "dashboard_sanction_stats_snapshot", "args": ["123", 2], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": [
        {"label": "01/11", "count": 0}, {"label": "02/11", "count": 3}
    ]}
    assert service.calls == []


def test_signed_dispatch_rejects_malformed_dashboard_sanction_stats_series():
    client, service = _client()
    service.dashboard_sanction_stats_snapshot = lambda guild_id, days: [("31/02", 1), ("02/11", 3)]
    body = _request_body({"method": "dashboard_sanction_stats_snapshot", "args": ["123", 2], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503
    assert response.get_json() == {"error": "internal service unavailable"}


def test_signed_dispatch_allows_dashboard_sanction_types_snapshot_and_rejects_bad_values():
    client, service = _client()
    service.dashboard_sanction_types_snapshot = lambda guild_id: ["ban", "kick", "soft_ban"]
    body = _request_body({"method": "dashboard_sanction_types_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": ["ban", "kick", "soft_ban"]}

    service.dashboard_sanction_types_snapshot = lambda guild_id: ["ban", "not valid"]
    body = _request_body({"method": "dashboard_sanction_types_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body, nonce="s" * 24))
    assert response.status_code == 503


def test_signed_dispatch_allows_dashboard_roles_snapshot_and_rejects_bad_dto():
    dto = [
        {"id": "123", "name": "@everyone"},
        {"id": "234", "name": "Moderator"},
    ]
    client, service = _client()
    service.dashboard_roles_snapshot = lambda guild_id: dto
    body = _request_body({"method": "dashboard_roles_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": dto}

    service.dashboard_roles_snapshot = lambda guild_id: [{"id": "bad", "name": "Moderator"}]
    body = _request_body({"method": "dashboard_roles_snapshot", "args": ["123"], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body, nonce="r" * 24))
    assert response.status_code == 503



def test_signed_dispatch_allows_guild_scoped_case_appeal_actions():
    for method, args in [
        ("api_claim_appeal_by_case", ["123", 12]),
        ("api_release_appeal_claim_by_case", ["123", 12]),
        ("api_resolve_appeal_by_case", ["123", 12, "accepted"]),
    ]:
        client, service = _client()
        setattr(service, method, lambda *values: True)
        body = _request_body({"method": method, "args": args, "kwargs": {}})
        response = client.post(PATH, data=body, headers=_signed_headers(body, nonce=(method + "nonce").ljust(24, "x")))
        assert response.status_code == 200 and response.get_json() == {"ok": True, "result": True}


@pytest.mark.parametrize(("method", "args"), [
    ("api_claim_appeal_by_case", ["123", True]),
    ("api_release_appeal_claim_by_case", ["bad", 1]),
    ("api_resolve_appeal_by_case", ["123", 1, "processing"]),
    ("api_resolve_appeal_by_case", ["123", 1]),
])
def test_signed_dispatch_rejects_invalid_case_appeal_action_arguments(method, args):
    client, service = _client()
    body = _request_body({"method": method, "args": args, "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body, nonce=(method + "invalid").ljust(24, "x")))
    assert response.status_code == 403
    assert service.calls == []


def test_signed_dispatch_fails_closed_for_non_boolean_case_appeal_action_result():
    client, service = _client()
    service.api_claim_appeal_by_case = lambda guild_id, case_id: 1
    body = _request_body({"method": "api_claim_appeal_by_case", "args": ["123", 1], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body, nonce="caseactionresultnoncexxx"))
    assert response.status_code == 503


def test_signed_dispatch_projects_audit_entries_to_safe_dto():
    from datetime import datetime

    client, service = _client()
    service.api_audit_snapshot = lambda guild_id, limit: [{
        "_id": "mongo-secret", "guild_id": guild_id, "actor_id": "private-actor-id",
        "actor_name": "Moderator", "action": "Banned member", "details": "spam",
        "timestamp": datetime(2026, 11, 28, 9, 30),
    }]
    body = _request_body({"method": "api_audit_snapshot", "args": ["123", 10], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result == [{"actor_name": "Moderator", "action": "Banned member", "details": "spam",
        "timestamp": "2026-11-28T09:30:00Z"}]
    assert "actor_id" not in result[0] and "_id" not in result[0] and "guild_id" not in result[0]


def test_signed_dispatch_rejects_invalid_audit_snapshot_arguments():
    client, service = _client()
    body = _request_body({"method": "api_audit_snapshot", "args": ["123", True], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


def test_signed_dispatch_fails_closed_for_malformed_audit_snapshot():
    from datetime import datetime

    client, service = _client()
    service.api_audit_snapshot = lambda guild_id, limit: [{
        "guild_id": guild_id, "actor_name": "Moderator", "action": "Banned member",
        "details": "x" * 1001, "timestamp": datetime(2026, 11, 28, 9, 30),
    }]
    body = _request_body({"method": "api_audit_snapshot", "args": ["123", 10], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503


def test_signed_dispatch_projects_submitted_appeals_without_tokens_or_mongo_fields():
    from datetime import datetime

    client, service = _client()
    service.dashboard_submitted_appeals_snapshot = lambda guild_id, limit: [{
        "_id": "mongo-secret", "token": "public-appeal-token", "guild_id": guild_id,
        "case_id": 12, "sanction_type": "ban", "user_name": "ExampleUser",
        "user_id": "123456789012345678", "ban_reason": "original reason",
        "appeal_text": "Please review", "submitted_at": datetime(2026, 11, 28, 9, 30),
        "status": "submitted", "processing_at": None,
    }]
    body = _request_body({"method": "dashboard_submitted_appeals_snapshot", "args": ["123", 10], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result == [{
        "case_id": 12, "sanction_type": "ban", "user_name": "ExampleUser",
        "user_id": "123456789012345678", "ban_reason": "original reason",
        "appeal_text": "Please review", "submitted_at": "2026-11-28T09:30:00Z",
    }]
    assert "token" not in result[0] and "_id" not in result[0] and "guild_id" not in result[0]


@pytest.mark.parametrize("args", [["bad", 10], ["123", True], ["123", 101], ["123"], ["123", 10, "extra"]])
def test_signed_dispatch_rejects_invalid_submitted_appeals_arguments(args):
    client, service = _client()
    body = _request_body({"method": "dashboard_submitted_appeals_snapshot", "args": args, "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


def test_signed_dispatch_fails_closed_for_malformed_submitted_appeals():
    from datetime import datetime

    client, service = _client()
    service.dashboard_submitted_appeals_snapshot = lambda guild_id, limit: [{
        "guild_id": guild_id, "case_id": True, "sanction_type": "ban",
        "user_name": "ExampleUser", "user_id": "123456789012345678",
        "ban_reason": "reason", "appeal_text": "appeal",
        "submitted_at": datetime(2026, 11, 28, 9, 30), "status": "submitted",
    }]
    body = _request_body({"method": "dashboard_submitted_appeals_snapshot", "args": ["123", 10], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503
    assert response.get_json() == {"error": "internal service unavailable"}


def test_signed_dispatch_projects_sanction_case_documents_to_safe_dto():
    from datetime import datetime

    client, service = _client()
    service.api_sanction_cases_snapshot = lambda guild_id, filter_type, limit: [{
        "_id": "mongo-secret",
        "guild_id": guild_id,
        "case_id": 9,
        "type": "ban",
        "timestamp": datetime(2026, 11, 28, 9, 30),
        "user_id": "123456789012345678",
        "reason": "bounded reason",
        "moderator_id": "987654321098765432",
        "edited_by_name": "Private Moderator",
    }]
    body = _request_body({"method": "api_sanction_cases_snapshot", "args": ["123", "ban", 10], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": [{
        "case_id": 9,
        "type": "ban",
        "timestamp": "2026-11-28T09:30:00Z",
        "user_id": "123456789012345678",
        "reason": "bounded reason",
    }]}


@pytest.mark.parametrize("args", [
    ["bad", "", 10], ["123", "Bad Type", 10], ["123", "", True],
    ["123", "", 201], ["123", ""], ["123", "", 10, "extra"],
])
def test_signed_dispatch_rejects_invalid_sanction_case_arguments(args):
    client, service = _client()
    body = _request_body({"method": "api_sanction_cases_snapshot", "args": args, "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 403
    assert service.calls == []


def test_signed_dispatch_fails_closed_for_malformed_sanction_case_documents():
    from datetime import datetime

    client, service = _client()
    service.api_sanction_cases_snapshot = lambda guild_id, filter_type, limit: [{
        "guild_id": guild_id, "case_id": True, "type": "ban",
        "timestamp": datetime(2026, 11, 28, 9, 30), "user_id": "123456789012345678",
        "reason": "reason",
    }]
    body = _request_body({"method": "api_sanction_cases_snapshot", "args": ["123", "", 10], "kwargs": {}})
    response = client.post(PATH, data=body, headers=_signed_headers(body))
    assert response.status_code == 503
    assert response.get_json() == {"error": "internal service unavailable"}
