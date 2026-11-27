import json
import time

from flask import Flask

from web.bot_service_signing import sign_internal_request
from web.internal_bot_service import create_internal_bot_service_blueprint


SECRET = b"internal-bot-service-test-secret-32-bytes"
PATH = "/internal/v1/dispatch"
VALID_MEMBER = {
    "id": "123456789012345678",
    "name": "Sample Member",
    "username": "sample",
    "avatar_url": "https://cdn.discordapp.com/avatars/123456789012345678/sample.png",
    "is_muted": False,
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

    def api_members_snapshot(self, guild_id):
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
        {"method": "api_members_snapshot", "args": args, "kwargs": {}},
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


def test_signed_dispatch_returns_bounded_member_dto():
    client, service = _client([VALID_MEMBER])

    response = _post(client, ["123"], "a" * 24)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": [VALID_MEMBER]}
    assert service.calls == ["123"]


def test_signed_dispatch_allows_missing_members_snapshot():
    client, service = _client(None)

    response = _post(client, ["123"], "b" * 24)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": None}
    assert service.calls == ["123"]


def test_signed_dispatch_rejects_invalid_members_arguments():
    client, service = _client([VALID_MEMBER])

    for index, args in enumerate(([], ["invalid"], ["123", "456"], [123])):
        response = _post(client, args, chr(100 + index) * 24)
        assert response.status_code == 403

    assert service.calls == []


def test_signed_dispatch_rejects_malformed_member_dtos():
    invalid_members = []
    for field, value in (
        ("id", "not-decimal"),
        ("name", ""),
        ("username", ""),
        ("avatar_url", "http://cdn.discordapp.com/test"),
        ("avatar_url", "https://example.com/avatar"),
        ("is_muted", 0),
    ):
        invalid = dict(VALID_MEMBER)
        invalid[field] = value
        invalid_members.append([invalid])

    extra = dict(VALID_MEMBER, extra=True)
    invalid_members.extend(([extra], "not-a-list"))

    invalid_members.append([dict(VALID_MEMBER) for _ in range(501])

    for index, result in enumerate(invalid_members):
        client, service = _client(result)
        response = _post(client, ["123"], chr(110 + index) * 24)
        assert response.status_code == 503
        assert response.get_json() == {"error": "internal service unavailable"}
        assert service.calls == ["123"]
