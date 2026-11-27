import json
import time

from flask import Flask

from web.bot_service_signing import sign_internal_request
from web.internal_bot_service import create_internal_bot_service_blueprint


SECRET = b"internal-bot-service-test-secret-32-bytes"
PATH = "/internal/v1/dispatch"


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

    def api_text_channels_snapshot(self, guild_id):
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
        {"method": "api_text_channels_snapshot", "args": args, "kwargs": {}},
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


def test_signed_dispatch_returns_bounded_text_channel_dtos():
    client, service = _client([
        {"id": "123456789012345678", "name": "general"},
        {"id": "987654321098765432", "name": "staff-chat"},
    ])

    response = _post(client, ["123456789012345678"], "a" * 24)

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "result": [
            {"id": "123456789012345678", "name": "general"},
            {"id": "987654321098765432", "name": "staff-chat"},
        ],
    }
    assert service.calls == ["123456789012345678"]


def test_signed_dispatch_allows_unavailable_text_channel_snapshot():
    client, service = _client(None)

    response = _post(client, ["123"], "b" * 24)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": None}
    assert service.calls == ["123"]


def test_signed_dispatch_rejects_invalid_text_channel_arguments():
    client, service = _client([])

    for index, args in enumerate(([], ["invalid"], ["123", "456"], [123])):
        response = _post(client, args, chr(99 + index) * 24)
        assert response.status_code == 403

    assert service.calls == []


def test_signed_dispatch_rejects_malformed_text_channel_dtos():
    invalid_results = [
        "not-a-list",
        [{"id": "123", "name": "general", "extra": True}],
        [{"id": "not-decimal", "name": "general"}],
        [{"id": "123", "name": ""}],
        [{"id": "123", "name": "x" * 101}],
        [{"id": "123", "name": "general"}] * 501,
    ]

    for index, result in enumerate(invalid_results):
        client, service = _client(result)
        response = _post(client, ["123"], chr(100 + index) * 24)
        assert response.status_code == 503
        assert response.get_json() == {"error": "internal service unavailable"}
        assert service.calls == ["123"]
