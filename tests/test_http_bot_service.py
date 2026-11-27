import json

import pytest
import requests

from web.bot_service_signing import verify_internal_request
from web.http_bot_service import (
    BotServiceTransportError,
    SignedHttpBotService,
)


SECRET = b"signed-http-bot-service-test-secret-32-bytes"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, error=None, body=None, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.error = error
        self.headers = headers or {}
        self.body = body if body is not None else (
            json.dumps(payload).encode("utf-8") if error is None else b""
        )
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse(
            payload={"ok": True, "result": {"status": "alive", "guild_count": 3}}
        )
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _client(session=None, **kwargs):
    return SignedHttpBotService(
        base_url="https://bot.internal.example",
        key_id="current",
        secret=SECRET,
        session=session or FakeSession(),
        **kwargs,
    )


def test_health_snapshot_sends_signed_exact_body_and_returns_result():
    session = FakeSession()
    client = _client(session, timeout=3.5)

    result = client.health_snapshot()

    assert result == {"status": "alive", "guild_count": 3}
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url == "https://bot.internal.example/internal/v1/dispatch"
    assert options["timeout"] == 3.5
    assert options["allow_redirects"] is False
    assert options["stream"] is True
    assert options["headers"]["Content-Type"] == "application/json"

    body = options["data"]
    assert json.loads(body) == {
        "method": "health_snapshot",
        "args": [],
        "kwargs": {},
    }
    claims = verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path="/internal/v1/dispatch",
        body=body,
        headers=options["headers"],
    )
    assert claims.key_id == "current"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://bot.internal.example",
        "https://user:password@bot.internal.example",
        "https://bot.internal.example/path",
        "https://bot.internal.example?token=secret",
        "https://bot.internal.example#fragment",
        "",
    ],
)
def test_client_rejects_non_origin_or_insecure_urls(base_url):
    with pytest.raises(ValueError, match="HTTPS origin"):
        SignedHttpBotService(
            base_url=base_url,
            key_id="current",
            secret=SECRET,
            session=FakeSession(),
        )


def test_client_rejects_short_secrets_and_invalid_timeouts():
    with pytest.raises(ValueError, match="secret"):
        SignedHttpBotService(
            base_url="https://bot.internal.example",
            key_id="current",
            secret=b"short",
            session=FakeSession(),
        )
    with pytest.raises(ValueError, match="timeout"):
        _client(timeout=0)


@pytest.mark.parametrize("status", [301, 401, 409, 503])
def test_non_200_responses_fail_without_following_redirects(status):
    session = FakeSession(response=FakeResponse(status_code=status))
    with pytest.raises(BotServiceTransportError):
        _client(session).health_snapshot()
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(payload=[]),
        FakeResponse(payload={"ok": False, "error": "details"}),
        FakeResponse(payload={"ok": True, "result": []}),
        FakeResponse(error=ValueError("not JSON")),
    ],
)
def test_malformed_remote_responses_fail_closed(response):
    with pytest.raises(BotServiceTransportError):
        _client(FakeSession(response=response)).health_snapshot()


def test_network_errors_are_normalized_without_leaking_transport_details():
    session = FakeSession(error=requests.Timeout("private host detail"))
    with pytest.raises(BotServiceTransportError, match="request failed") as raised:
        _client(session).health_snapshot()
    assert "private host detail" not in str(raised.value)



def test_response_body_size_is_bounded_and_response_is_closed():
    response = FakeResponse(
        body=b"x" * (65_536 + 1),
        headers={"Content-Length": str(65_537)},
    )
    session = FakeSession(response=response)
    with pytest.raises(BotServiceTransportError, match="size limit"):
        _client(session).health_snapshot()
    assert response.closed is True


def test_response_is_closed_after_success():
    response = FakeResponse(payload={"ok": True, "result": {"status": "alive"}})
    result = _client(FakeSession(response=response)).health_snapshot()
    assert result == {"status": "alive"}
    assert response.closed is True
