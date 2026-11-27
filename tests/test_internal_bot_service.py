import json
import time
from threading import Lock

from flask import Flask
from pymongo.errors import DuplicateKeyError

from web.bot_service_signing import sign_internal_request
from web.internal_bot_service import (
    MongoNonceStore,
    create_internal_bot_service_blueprint,
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
