import json
import time

from flask import Flask

from web.bot_service_signing import sign_internal_request
from web.internal_bot_service import create_internal_bot_service_blueprint


SECRET = b"internal-bot-service-test-secret-32-bytes"
PATH = "/internal/v1/dispatch"
VALID_DOC = {
    "_id": object(),
    "guild_id": "123",
    "enabled": True,
    "entries": [
        {
            "id": "a" * 32,
            "keywords": ["help", "support"],
            "response": "Contact the support team.",
            "legacy_metadata": "must not cross the boundary",
        }
    ],
}


class MemoryNonceStore:
    def __init__(self):
        self.seen = set()

    def consume(self, key_id, nonce, expires_at):
        key = (key_id, nonce)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


class FakeBotService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def api_faq_snapshot(self, guild_id):
        self.calls.append(guild_id)
        return self.result


def _client(result):
    service = FakeBotService(result)
    app = Flask(__name__)
    app.register_blueprint(
        create_internal_bot_service_blueprint(
            service=service,
            keyring={"current": SECRET},
            nonce_store=MemoryNonceStore(),
        )
    )
    return app.test_client(), service


def _post(client, args, nonce):
    body = json.dumps(
        {"method": "api_faq_snapshot", "args": args, "kwargs": {}},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = sign_internal_request(
        secret=SECRET,
        key_id="current",
        method="POST",
        path=PATH,
        body=body,
        timestamp=int(time.time()),
        nonce=nonce,
    )
    headers["Content-Type"] = "application/json"
    return client.post(PATH, data=body, headers=headers)


def test_signed_dispatch_returns_sanitized_faq_snapshot():
    client, service = _client(VALID_DOC)

    response = _post(client, ["123"], "a" * 24)

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "result": {
            "enabled": True,
            "entries": [
                {
                    "id": "a" * 32,
                    "keywords": ["help", "support"],
                    "response": "Contact the support team.",
                }
            ],
        },
    }
    assert service.calls == ["123"]


def test_signed_dispatch_allows_missing_faq_document():
    client, service = _client(None)

    response = _post(client, ["123"], "b" * 24)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": None}
    assert service.calls == ["123"]


def test_signed_dispatch_rejects_invalid_faq_snapshot_arguments():
    client, service = _client(VALID_DOC)

    for index, args in enumerate(([], ["not-decimal"], ["123", "456"], [123])):
        response = _post(client, args, chr(100 + index) * 24)
        assert response.status_code == 403

    assert service.calls == []


def test_signed_dispatch_rejects_malformed_faq_entries():
    invalid_docs = []
    for field, value in (
        ("enabled", "true"),
        ("entries", "not-a-list"),
    ):
        invalid = dict(VALID_DOC)
        invalid[field] = value
        invalid_docs.append(invalid)

    malformed_entries = [
        {"id": "invalid id", "keywords": ["help"], "response": "valid"},
        {"id": "a" * 32, "keywords": [], "response": "valid"},
        {"id": "a" * 32, "keywords": [" "], "response": "valid"},
        {"id": "a" * 32, "keywords": ["x" * 101], "response": "valid"},
        {"id": "a" * 32, "keywords": ["help"], "response": " "},
        {"id": "a" * 32, "keywords": ["help"], "response": "x" * 2001},
    ]
    for entry in malformed_entries:
        invalid_docs.append({"enabled": True, "entries": [entry]})

    invalid_docs.append(
        {
            "enabled": True,
            "entries": [
                {
                    "id": "a" * 32,
                    "keywords": ["x" * 100 for _ in range(25)],
                    "response": "y" * 2000,
                }
                for _ in range(15)
            ],
        }
    )

    for index, result in enumerate(invalid_docs):
        client, service = _client(result)
        response = _post(client, ["123"], chr(110 + index) * 24)
        assert response.status_code == 503
        assert response.get_json() == {"error": "internal service unavailable"}
        assert service.calls == ["123"]
