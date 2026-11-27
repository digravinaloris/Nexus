import json
import time

from flask import Flask

from web.bot_service_signing import sign_internal_request
from web.internal_bot_service import create_internal_bot_service_blueprint


SECRET = b"internal-bot-service-test-secret-32-bytes"
PATH = "/internal/v1/dispatch"
VALID_STATS = {
    "guild_id": "123",
    "name": "Alpha",
    "member_count": 25,
    "ban_count": 2,
    "muted_count": 1,
    "open_tickets_count": 3,
    "warned_count": 4,
    "warns_today": 1,
    "activity_7d": [0, 1, 2, 3, 4, 5, 6],
    "locked": False,
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

    def api_guild_stats_snapshot(self, guild_id, ticket_category_id=None):
        self.calls.append((guild_id, ticket_category_id))
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
        {"method": "api_guild_stats_snapshot", "args": args, "kwargs": {}},
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


def test_signed_dispatch_returns_bounded_guild_stats():
    client, service = _client(VALID_STATS)

    response = _post(client, ["123", "456"], "a" * 24)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "result": VALID_STATS}
    assert service.calls == [("123", "456")]


def test_signed_dispatch_allows_missing_guild_and_offline_stats():
    for result, args, expected_call, nonce in (
        ({}, ["123"], ("123", None), "b" * 24),
        (None, ["123", None], ("123", None), "c" * 24),
    ):
        client, service = _client(result)
        response = _post(client, args, nonce)
        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "result": result}
        assert service.calls == [expected_call]


def test_signed_dispatch_rejects_invalid_guild_stats_arguments():
    client, service = _client(VALID_STATS)

    for index, args in enumerate(
        ([], ["invalid"], ["123", "not-decimal"], ["123", "456", "789"], [123])
    ):
        response = _post(client, args, chr(100 + index) * 24)
        assert response.status_code == 403

    assert service.calls == []


def test_signed_dispatch_rejects_malformed_guild_stats():
    invalid_results = []
    for key, value in (
        ("guild_id", "not-decimal"),
        ("name", ""),
        ("member_count", True),
        ("ban_count", -1),
        ("locked", 1),
        ("activity_7d", [0, 1, 2]),
    ):
        invalid = dict(VALID_STATS)
        invalid[key] = value
        invalid_results.append(invalid)

    extra = dict(VALID_STATS, extra=True)
    invalid_results.extend(("not-a-dict", extra))

    for index, result in enumerate(invalid_results):
        client, service = _client(result)
        response = _post(client, ["123"], chr(110 + index) * 24)
        assert response.status_code == 503
        assert response.get_json() == {"error": "internal service unavailable"}
        assert service.calls == [("123", None)]
