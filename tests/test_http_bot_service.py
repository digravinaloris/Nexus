import datetime
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


def test_public_status_snapshot_uses_the_same_signed_transport():
    session = FakeSession(
        response=FakeResponse(
            payload={
                "ok": True,
                "result": {
                    "online": True,
                    "guild_count": 8,
                    "latency_ms": 42,
                    "uptime": "2h",
                },
            }
        )
    )
    result = _client(session).public_status_snapshot()

    assert result == {
        "online": True,
        "guild_count": 8,
        "latency_ms": 42,
        "uptime": "2h",
    }
    body = session.calls[0][1]["data"]
    assert json.loads(body) == {
        "method": "public_status_snapshot",
        "args": [],
        "kwargs": {},
    }
    claims = verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path="/internal/v1/dispatch",
        body=body,
        headers=session.calls[0][1]["headers"],
    )
    assert claims.nonce


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


def test_available_guild_ids_dispatches_and_enforces_candidate_scope():
    candidates = ["123456789012345678", "234567890123456789"]
    result = ["234567890123456789"]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result}))
    client = _client(session)
    assert client.available_guild_ids(candidates) == result
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {
        "method": "available_guild_ids",
        "args": [candidates],
        "kwargs": {},
    }

    for invalid in (
        ["345678901234567890"],
        ["123456789012345678", "123456789012345678"],
        [123],
    ):
        bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": invalid}))
        with pytest.raises(BotServiceTransportError, match="invalid response"):
            _client(bad).available_guild_ids(candidates)


def test_available_guild_ids_rejects_invalid_candidate_lists_before_transport():
    session = FakeSession()
    client = _client(session)
    for invalid in ("123", [123], ["bad"], ["123456789012345678"] * 2, ["1"] * 501):
        with pytest.raises(ValueError, match="candidate guild IDs"):
            client.available_guild_ids(invalid)
    assert session.calls == []


@pytest.mark.parametrize("method,args", [
    ("guild_presence", ["123456789012345678"]),
    ("guild_name", ["123456789012345678"]),
    ("is_guild_owner", ["123456789012345678", "234567890123456789"]),
    ("is_guild_admin", ["123456789012345678", "234567890123456789"]),
])
def test_guild_read_snapshots_send_bounded_signed_arguments(method, args):
    result_value = {
        "guild_presence": {"bot_ready": True, "guild_present": True},
        "guild_name": "Nexus Community",
        "is_guild_owner": True,
        "is_guild_admin": True,
    }[method]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result_value}))

    result = getattr(_client(session), method)(*args)

    assert result == result_value
    body = session.calls[0][1]["data"]
    assert json.loads(body) == {"method": method, "args": args, "kwargs": {}}
    verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path="/internal/v1/dispatch",
        body=body,
        headers=session.calls[0][1]["headers"],
    )


@pytest.mark.parametrize("guild_id", ["", "0", "-1", "abc", "1" * 21, 123])
def test_guild_read_snapshots_reject_invalid_guild_ids_before_transport(guild_id):
    session = FakeSession()
    client = _client(session)

    with pytest.raises(ValueError, match="guild ID"):
        client.guild_presence(guild_id)

    assert session.calls == []


@pytest.mark.parametrize("method", ["is_guild_owner", "is_guild_admin"])
def test_dashboard_authorization_rejects_invalid_guild_or_user_ids(method):
    session = FakeSession()
    client = _client(session)
    with pytest.raises(ValueError, match="guild ID"):
        getattr(client, method)("bad", "234567890123456789")
    with pytest.raises(ValueError, match="user ID"):
        getattr(client, method)("123456789012345678", "bad")
    assert session.calls == []


@pytest.mark.parametrize(
    "method,result",
    [
        ("is_guild_owner", 1),
        ("is_guild_admin", None),
        ("guild_presence", {"bot_ready": True}),
        ("guild_presence", {"bot_ready": 1, "guild_present": False}),
        ("guild_presence", {"bot_ready": True, "guild_present": False, "extra": False}),
        ("guild_name", ""),
        ("guild_name", "x" * 257),
        ("guild_name", None),
    ],
)
def test_guild_read_snapshots_reject_invalid_remote_dtos(method, result):
    session = FakeSession(
        response=FakeResponse(payload={"ok": True, "result": result})
    )

    call_args = (
        ("123456789012345678", "234567890123456789")
        if method in {"is_guild_owner", "is_guild_admin"}
        else ("123456789012345678",)
    )
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        getattr(_client(session), method)(*call_args)


@pytest.mark.parametrize("method", ["api_text_channels_snapshot", "api_roles_snapshot"])
def test_dashboard_collection_snapshots_send_signed_guild_scoped_requests(method):
    result_value = [{"id": "123456789012345678", "name": "general"}]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result_value}))

    result = getattr(_client(session), method)("123456789012345678")

    assert result == result_value
    body = session.calls[0][1]["data"]
    assert json.loads(body) == {
        "method": method,
        "args": ["123456789012345678"],
        "kwargs": {},
    }
    verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path="/internal/v1/dispatch",
        body=body,
        headers=session.calls[0][1]["headers"],
    )


@pytest.mark.parametrize("method", ["api_text_channels_snapshot", "api_roles_snapshot"])
def test_dashboard_collection_snapshots_allow_offline_none(method):
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": None}))
    assert getattr(_client(session), method)("123456789012345678") is None


@pytest.mark.parametrize("method", ["api_text_channels_snapshot", "api_roles_snapshot"])
@pytest.mark.parametrize(
    "result",
    [
        "not-a-list",
        [{"id": "123456789012345678"}],
        [{"id": "abc", "name": "general"}],
        [{"id": "123456789012345678", "name": ""}],
        [{"id": "123456789012345678", "name": "x" * 101}],
        [{"id": "123456789012345678", "name": "general", "extra": True}],
        [{"id": "123456789012345678", "name": "general"}] * 501,
    ],
)
def test_dashboard_collection_snapshots_reject_malformed_dtos(method, result):
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        getattr(_client(session), method)("123456789012345678")


@pytest.mark.parametrize("method", ["api_text_channels_snapshot", "api_roles_snapshot"])
@pytest.mark.parametrize("guild_id", ["", "0", "-1", "abc", "1" * 21, 123])
def test_dashboard_collection_snapshots_reject_invalid_guild_ids(method, guild_id):
    session = FakeSession()
    with pytest.raises(ValueError, match="guild ID"):
        getattr(_client(session), method)(guild_id)
    assert session.calls == []


def test_remote_faq_snapshot_sends_signed_guild_scoped_request():
    result_value = {
        "enabled": True,
        "entries": [{"id": "faq_1", "keywords": ["help", "support"], "response": "Open a ticket."}],
    }
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result_value}))
    assert _client(session).api_faq_snapshot("123456789012345678") == result_value
    body = session.calls[0][1]["data"]
    assert json.loads(body) == {
        "method": "api_faq_snapshot",
        "args": ["123456789012345678"],
        "kwargs": {},
    }
    verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path="/internal/v1/dispatch",
        body=body,
        headers=session.calls[0][1]["headers"],
    )


def test_remote_faq_snapshot_allows_missing_guild_data():
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": None}))
    assert _client(session).api_faq_snapshot("123456789012345678") is None


@pytest.mark.parametrize(
    "result",
    [
        [],
        {"enabled": 1, "entries": []},
        {"enabled": True, "entries": "bad"},
        {"enabled": True, "entries": [{"id": "bad id", "keywords": ["help"], "response": "ok"}]},
        {"enabled": True, "entries": [{"id": "faq", "keywords": [], "response": "ok"}]},
        {"enabled": True, "entries": [{"id": "faq", "keywords": ["help"], "response": ""}]},
        {"enabled": True, "entries": [{"id": "faq", "keywords": ["x" * 101], "response": "ok"}]},
    ],
)
def test_remote_faq_snapshot_rejects_malformed_dtos(result):
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(session).api_faq_snapshot("123456789012345678")


def test_remote_members_snapshot_sends_signed_guild_scoped_request():
    result_value = [{
        "id": "123456789012345678",
        "name": "Example User",
        "username": "example",
        "avatar_url": "https://cdn.discordapp.com/avatars/123456789012345678/hash.png",
        "is_muted": False,
    }]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result_value}))
    assert _client(session).api_members_snapshot("123456789012345678") == result_value
    body = session.calls[0][1]["data"]
    assert json.loads(body) == {
        "method": "api_members_snapshot",
        "args": ["123456789012345678"],
        "kwargs": {},
    }
    verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path="/internal/v1/dispatch",
        body=body,
        headers=session.calls[0][1]["headers"],
    )


@pytest.mark.parametrize(
    "result",
    [
        {},
        [{"id": "abc", "name": "Name", "username": "user", "avatar_url": "https://cdn.discordapp.com/a", "is_muted": False}],
        [{"id": "123", "name": "Name", "username": "user", "avatar_url": "http://cdn.discordapp.com/a", "is_muted": False}],
        [{"id": "123", "name": "Name", "username": "user", "avatar_url": "https://evil.example/a", "is_muted": False}],
        [{"id": "123", "name": "Name", "username": "user", "avatar_url": "https://cdn.discordapp.com/a", "is_muted": 0}],
        [{"id": "123", "name": "Name", "username": "user", "avatar_url": "https://cdn.discordapp.com/a", "is_muted": False, "extra": True}],
    ],
)
def test_remote_members_snapshot_rejects_malformed_dtos(result):
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(session).api_members_snapshot("123456789012345678")


@pytest.mark.parametrize("method", ["api_faq_snapshot", "api_members_snapshot"])
@pytest.mark.parametrize("guild_id", ["", "0", "-1", "abc", "1" * 21, 123])
def test_faq_and_member_snapshots_reject_invalid_guild_ids(method, guild_id):
    session = FakeSession()
    with pytest.raises(ValueError, match="guild ID"):
        getattr(_client(session), method)(guild_id)
    assert session.calls == []



def test_remote_guild_list_snapshot_supports_global_and_guild_scoped_requests():
    result_value = [{
        "id": "123456789012345678",
        "name": "Nexus Community",
        "member_count": 42,
        "icon_url": "https://cdn.discordapp.com/icons/123456789012345678/hash.png",
    }]
    for guild_id, args in [(None, []), ("123456789012345678", ["123456789012345678"])]:
        session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result_value}))
        assert _client(session).api_guild_list_snapshot(guild_id) == result_value
        body = session.calls[0][1]["data"]
        assert json.loads(body) == {
            "method": "api_guild_list_snapshot",
            "args": args,
            "kwargs": {},
        }
        verify_internal_request(
            keyring={"current": SECRET},
            method="POST",
            path="/internal/v1/dispatch",
            body=body,
            headers=session.calls[0][1]["headers"],
        )


def test_remote_guild_list_snapshot_allows_offline_none():
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": None}))
    assert _client(session).api_guild_list_snapshot() is None


@pytest.mark.parametrize(
    "result",
    [
        {},
        [{"id": "abc", "name": "Nexus", "member_count": 1, "icon_url": None}],
        [{"id": "123", "name": "", "member_count": 1, "icon_url": None}],
        [{"id": "123", "name": "Nexus", "member_count": True, "icon_url": None}],
        [{"id": "123", "name": "Nexus", "member_count": -1, "icon_url": None}],
        [{"id": "123", "name": "Nexus", "member_count": 1, "icon_url": "https://evil.example/icon.png"}],
        [{"id": "123", "name": "Nexus", "member_count": 1, "icon_url": "http://cdn.discordapp.com/icons/123/a.png"}],
        [{"id": "123", "name": "Nexus", "member_count": 1, "icon_url": None, "extra": True}],
        [{"id": "123456789012345678", "name": "Nexus", "member_count": 1, "icon_url": None}] * 501,
    ],
)
def test_remote_guild_list_snapshot_rejects_malformed_dtos(result):
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(session).api_guild_list_snapshot()


@pytest.mark.parametrize("guild_id", ["", "0", "-1", "abc", "1" * 21, 123])
def test_remote_guild_list_snapshot_rejects_invalid_optional_guild_id(guild_id):
    session = FakeSession()
    with pytest.raises(ValueError, match="guild ID"):
        _client(session).api_guild_list_snapshot(guild_id)
    assert session.calls == []



def test_remote_guild_list_snapshot_rejects_non_sequence_internal_args():
    with pytest.raises(ValueError, match="internal BotService arguments"):
        _client()._dispatch_snapshot("api_guild_list_snapshot", None)



def test_remote_api_health_snapshot_supports_global_and_guild_scoped_requests():
    result_value = {"online": True, "locked": False, "guild_count": 3}
    for guild_id, args in [(None, []), ("123456789012345678", ["123456789012345678"])]:
        session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result_value}))
        assert _client(session).api_health_snapshot(guild_id) == result_value
        body = session.calls[0][1]["data"]
        assert json.loads(body) == {
            "method": "api_health_snapshot",
            "args": args,
            "kwargs": {},
        }
        verify_internal_request(
            keyring={"current": SECRET},
            method="POST",
            path="/internal/v1/dispatch",
            body=body,
            headers=session.calls[0][1]["headers"],
        )


@pytest.mark.parametrize(
    "result",
    [
        [],
        {"online": 1, "locked": False, "guild_count": 1},
        {"online": True, "locked": 0, "guild_count": 1},
        {"online": True, "locked": False, "guild_count": True},
        {"online": True, "locked": False, "guild_count": -1},
        {"online": True, "locked": False, "guild_count": 1, "extra": True},
    ],
)
def test_remote_api_health_snapshot_rejects_malformed_dtos(result):
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(session).api_health_snapshot()


@pytest.mark.parametrize("guild_id", ["", "0", "-1", "abc", "1" * 21, 123])
def test_remote_api_health_snapshot_rejects_invalid_optional_guild_id(guild_id):
    session = FakeSession()
    with pytest.raises(ValueError, match="guild ID"):
        _client(session).api_health_snapshot(guild_id)
    assert session.calls == []


def test_remote_guild_stats_snapshot_validates_dto_and_signed_arguments():
    result_value = {
        "guild_id": "123456789012345678", "name": "Nexus Community",
        "member_count": 42, "ban_count": 2, "muted_count": 1,
        "open_tickets_count": 3, "warned_count": 4, "warns_today": 1,
        "activity_7d": [0, 1, 2, 0, 1, 0, 3], "locked": False,
    }
    for args in (["123456789012345678"], ["123456789012345678", "234567890123456789"]):
        session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result_value}))
        assert _client(session).api_guild_stats_snapshot(*args) == result_value
        body = session.calls[0][1]["data"]
        assert json.loads(body)["method"] == "api_guild_stats_snapshot"
        assert json.loads(body)["args"] == args
        verify_internal_request(keyring={"current": SECRET}, method="POST",
            path="/internal/v1/dispatch", body=body,
            headers=session.calls[0][1]["headers"])


@pytest.mark.parametrize("result", [
    {},
    {"guild_id":"123456789012345678", "name":"Nexus", "member_count":-1, "ban_count":0,
     "muted_count":0, "open_tickets_count":0, "warned_count":0, "warns_today":0,
     "activity_7d":[0]*7, "locked":False},
    {"guild_id":"123456789012345678", "name":"Nexus", "member_count":1, "ban_count":0,
     "muted_count":0, "open_tickets_count":0, "warned_count":0, "warns_today":0,
     "activity_7d":[0]*6, "locked":False},
    {"guild_id":"123456789012345678", "name":"Nexus", "member_count":1, "ban_count":0,
     "muted_count":0, "open_tickets_count":0, "warned_count":0, "warns_today":0,
     "activity_7d":[0]*7, "locked":0},
])
def test_remote_guild_stats_snapshot_rejects_malformed_dtos(result):
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": result}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(session).api_guild_stats_snapshot("123456789012345678")


@pytest.mark.parametrize("args", [
    ("abc",), ("123456789012345678", "abc"),
    ("123456789012345678", "1" * 21),
])
def test_remote_guild_stats_snapshot_rejects_invalid_ids(args):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).api_guild_stats_snapshot(*args)
    assert session.calls == []


def test_dashboard_overview_snapshot_dispatch_and_validates_dto():
    dto = {
        "id": "123456789012345678", "name": "Nexus", "owner_id": "234567890123456789",
        "text_channels": [{"id": "345678901234567890", "name": "general"}],
        "roles": [{"id": "456789012345678901", "name": "Admin",
            "permissions": {"administrator": True, "manage_guild": False, "ban_members": False,
                "kick_members": False, "manage_roles": False}}],
        "role_names": {"456789012345678901": "Admin"},
        "open_tickets": [{"id": "567890123456789012", "name": "ticket-1"}],
    }
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    assert client.dashboard_overview_snapshot("123456789012345678", "234567890123456789") == dto
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["method"] == "dashboard_overview_snapshot"
    assert payload["args"] == ["123456789012345678", "234567890123456789"]

    malformed = dict(dto)
    malformed["roles"] = [{"id": "456789012345678901", "name": "Admin", "permissions": {"administrator": "yes"}}]
    bad_session = FakeSession(response=FakeResponse(payload={"ok": True, "result": malformed}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad_session).dashboard_overview_snapshot("123456789012345678")



def test_dashboard_settings_snapshot_dispatch_and_validates_dto():
    dto = {
        "id": "123456789012345678", "name": "Nexus", "owner_id": "234567890123456789",
        "text_channels": [{"id": "345678901234567890", "name": "general"}],
        "voice_channels": [{"id": "456789012345678901", "name": "General"}],
        "roles": [{"id": "567890123456789012", "name": "Moderator"}],
    }
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    assert client.dashboard_settings_snapshot("123456789012345678") == dto
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["method"] == "dashboard_settings_snapshot"
    assert payload["args"] == ["123456789012345678"]

    malformed = dict(dto)
    malformed["voice_channels"] = [{"id": "not-a-snowflake", "name": "General"}]
    bad_session = FakeSession(response=FakeResponse(payload={"ok": True, "result": malformed}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad_session).dashboard_settings_snapshot("123456789012345678")


@pytest.mark.parametrize("guild_id", ["abc", "0", "1" * 21])
def test_dashboard_settings_snapshot_rejects_invalid_guild_ids(guild_id):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).dashboard_settings_snapshot(guild_id)
    assert session.calls == []



def test_dashboard_sanction_stats_snapshot_dispatches_bounded_dto_and_returns_tuples():
    dto = [
        {"label": "01/11", "count": 0},
        {"label": "02/11", "count": 3},
    ]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    assert client.dashboard_sanction_stats_snapshot("345678901234567890", days=2) == [("01/11", 0), ("02/11", 3)]
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["method"] == "dashboard_sanction_stats_snapshot"
    assert payload["args"] == ["345678901234567890", 2]

    malformed = [dict(dto[0]), {"label": "31/02", "count": 1}]
    bad_session = FakeSession(response=FakeResponse(payload={"ok": True, "result": malformed}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad_session).dashboard_sanction_stats_snapshot("345678901234567890", days=2)


@pytest.mark.parametrize("days", [0, 31, -1, "2", 2.0, True, False])
def test_dashboard_sanction_stats_snapshot_rejects_invalid_day_ranges_without_network(days):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).dashboard_sanction_stats_snapshot("345678901234567890", days=days)
    assert session.calls == []


@pytest.mark.parametrize("guild_id", ["abc", "0", "1" * 21])
def test_dashboard_sanction_stats_snapshot_rejects_invalid_guild_ids_without_network(guild_id):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).dashboard_sanction_stats_snapshot(guild_id)
    assert session.calls == []


def test_dashboard_sanction_types_snapshot_dispatch_and_validates_bounded_values():
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": ["ban", "kick", "soft_ban"]}))
    client = _client(session)
    assert client.dashboard_sanction_types_snapshot("345678901234567890") == ["ban", "kick", "soft_ban"]
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["method"] == "dashboard_sanction_types_snapshot"
    assert payload["args"] == ["345678901234567890"]

    for malformed in [["ban", "bad value"], ["ban", "ban"], ["x" * 33], ["ban"] * 51, [1]]:
        bad_session = FakeSession(response=FakeResponse(payload={"ok": True, "result": malformed}))
        with pytest.raises(BotServiceTransportError, match="invalid response"):
            _client(bad_session).dashboard_sanction_types_snapshot("345678901234567890")


def test_api_sanction_cases_snapshot_dispatches_and_validates_dto():
    dto = [{"case_id": 7, "type": "ban", "timestamp": "2026-11-28T09:30:00Z", "user_id": "123456789012345678", "reason": "test"}]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    result = client.api_sanction_cases_snapshot("345678901234567890", "ban", 10)
    assert result[0]["timestamp"].isoformat() == "2026-11-28T09:30:00+00:00"
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["method"] == "api_sanction_cases_snapshot"
    assert payload["args"] == ["345678901234567890", "ban", 10]

    malformed = [dict(dto[0], reason="x" * 1001)]
    bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": malformed}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad).api_sanction_cases_snapshot("345678901234567890", "ban", 10)


@pytest.mark.parametrize(("guild_id", "filter_type", "limit"), [("bad", "", 10), ("123", "Bad Type", 10), ("123", "", True), ("123", "", 201)])
def test_api_sanction_cases_snapshot_rejects_invalid_arguments(guild_id, filter_type, limit):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).api_sanction_cases_snapshot(guild_id, filter_type, limit)
    assert session.calls == []


def test_api_authenticate_key_dispatches_without_returning_the_secret():
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": "345678901234567890"}))
    result = _client(session).api_authenticate_key("guild-secret", "345678901234567890")
    assert result == "345678901234567890"
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {"method": "api_authenticate_key", "args": ["guild-secret", "345678901234567890"], "kwargs": {}}
    assert "guild-secret" not in json.dumps(session.calls[0][1]["headers"])

    bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": "456789012345678901"}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad).api_authenticate_key("guild-secret", "345678901234567890")


def test_api_authenticate_key_supports_global_guild_lookup_and_rejects_bad_keys():
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": "345678901234567890"}))
    assert _client(session).api_authenticate_key("guild-secret") == "345678901234567890"
    assert json.loads(session.calls[0][1]["data"])["args"] == ["guild-secret"]
    bad = FakeSession()
    assert _client(bad).api_authenticate_key("clé") is None
    assert _client(bad).api_authenticate_key("x" * 513) is None
    assert bad.calls == []


def test_api_member_warnings_dispatches_and_validates_result():
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": 3}))
    assert _client(session).api_member_warnings("345678901234567890", "456789012345678901") == 3
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {"method": "api_member_warnings", "args": ["345678901234567890", "456789012345678901"], "kwargs": {}}

    bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": True}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad).api_member_warnings("345678901234567890", "456789012345678901")


def test_api_warnlist_snapshot_dispatches_and_validates_dto():
    dto = [{"user_id": "456789012345678901", "count": 2}]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    assert _client(session).api_warnlist_snapshot("345678901234567890") == dto
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {"method": "api_warnlist_snapshot", "args": ["345678901234567890"], "kwargs": {}}

    bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": [{"user_id": "bad", "count": 2}]}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad).api_warnlist_snapshot("345678901234567890")


def test_api_member_warnings_rejects_invalid_ids_before_transport():
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).api_member_warnings("bad", "456789012345678901")
    with pytest.raises(ValueError):
        _client(session).api_member_warnings("345678901234567890", "bad")
    assert session.calls == []


def test_api_audit_snapshot_dispatches_and_validates_dto():
    dto = [{
        "actor_name": "Moderator", "action": "Banned member", "details": "spam",
        "timestamp": "2026-11-28T09:30:00Z",
    }]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    result = _client(session).api_audit_snapshot("345678901234567890", 10)
    assert result[0]["timestamp"].isoformat() == "2026-11-28T09:30:00+00:00"
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {"method": "api_audit_snapshot", "args": ["345678901234567890", 10], "kwargs": {}}

    malformed = [dict(dto[0], actor_name="x" * 129)]
    bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": malformed}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad).api_audit_snapshot("345678901234567890", 10)


@pytest.mark.parametrize(("guild_id", "limit"), [("bad", 10), ("123", True), ("123", 101), ("123", 0)])
def test_api_audit_snapshot_rejects_invalid_arguments(guild_id, limit):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).api_audit_snapshot(guild_id, limit)
    assert session.calls == []


def test_dashboard_submitted_appeals_snapshot_dispatches_and_validates_dto():
    dto = [{
        "case_id": 12, "sanction_type": "ban", "user_name": "ExampleUser",
        "user_id": "123456789012345678", "ban_reason": "original reason",
        "appeal_text": "Please review", "submitted_at": "2026-11-28T09:30:00Z",
    }]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    result = client.dashboard_submitted_appeals_snapshot("345678901234567890", 10)
    assert result[0]["submitted_at"].isoformat() == "2026-11-28T09:30:00+00:00"
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["method"] == "dashboard_submitted_appeals_snapshot"
    assert payload["args"] == ["345678901234567890", 10]

    malformed = [dict(dto[0], appeal_text="x" * 4001)]
    bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": malformed}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad).dashboard_submitted_appeals_snapshot("345678901234567890", 10)


def test_signed_appeal_case_actions_dispatch_without_tokens():
    for method, args in [
        ("api_claim_appeal_by_case", ["345678901234567890", 12]),
        ("api_release_appeal_claim_by_case", ["345678901234567890", 12]),
        ("api_resolve_appeal_by_case", ["345678901234567890", 12, "denied"]),
    ]:
        session = FakeSession(response=FakeResponse(payload={"ok": True, "result": True}))
        client = _client(session)
        if method == "api_resolve_appeal_by_case":
            result = client.api_resolve_appeal_by_case(*args)
        else:
            result = getattr(client, method)(*args)
        assert result is True
        payload = json.loads(session.calls[0][1]["data"])
        assert payload["method"] == method and payload["args"] == args
        assert "token" not in json.dumps(payload)


@pytest.mark.parametrize(("method", "args"), [
    ("api_claim_appeal_by_case", ("bad", 1)),
    ("api_claim_appeal_by_case", ("123", True)),
    ("api_release_appeal_claim_by_case", ("123", 0)),
    ("api_resolve_appeal_by_case", ("123", 1, "processing")),
])
def test_signed_appeal_case_actions_reject_invalid_arguments(method, args):
    session = FakeSession()
    with pytest.raises(ValueError):
        getattr(_client(session), method)(*args)
    assert session.calls == []


def test_signed_appeal_case_actions_reject_non_boolean_remote_result():
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": 1}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(session).api_claim_appeal_by_case("123", 1)


@pytest.mark.parametrize(("guild_id", "limit"), [("bad", 10), ("123", True), ("123", 101), ("123", 0)])
def test_dashboard_submitted_appeals_snapshot_rejects_invalid_arguments(guild_id, limit):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).dashboard_submitted_appeals_snapshot(guild_id, limit)
    assert session.calls == []


def test_dashboard_roles_snapshot_dispatch_and_validates_dto():
    dto = [
        {"id": "123456789012345678", "name": "@everyone"},
        {"id": "234567890123456789", "name": "Moderator"},
    ]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    assert client.dashboard_roles_snapshot("345678901234567890") == dto
    payload = json.loads(session.calls[0][1]["data"])
    assert payload["method"] == "dashboard_roles_snapshot"
    assert payload["args"] == ["345678901234567890"]

    bad = [{"id": "not-a-snowflake", "name": "Moderator"}]
    bad_session = FakeSession(response=FakeResponse(payload={"ok": True, "result": bad}))
    with pytest.raises(BotServiceTransportError, match="invalid response"):
        _client(bad_session).dashboard_roles_snapshot("345678901234567890")


@pytest.mark.parametrize("guild_id", ["abc", "0", "1" * 21])
def test_dashboard_roles_snapshot_rejects_invalid_guild_ids(guild_id):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).dashboard_roles_snapshot(guild_id)
    assert session.calls == []


def test_api_rest_config_snapshot_dispatches_and_rejects_unreviewed_or_secret_fields():
    dto = {"guild_id": "345678901234567890", "logs_channel": "logs", "tickets_enabled": True}
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    assert client.api_rest_config_snapshot("345678901234567890") == dto
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {
        "method": "api_rest_config_snapshot",
        "args": ["345678901234567890"],
        "kwargs": {},
    }

    for invalid in (
        {**dto, "log_webhook_url": "https://hooks.example/secret"},
        {**dto, "future_unreviewed_secret": "nope"},
        {**dto, "guild_id": "456789012345678901"},
        {**dto, "command_templates": {"access_token": "secret"}},
    ):
        bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": invalid}))
        with pytest.raises(BotServiceTransportError, match="invalid response"):
            _client(bad).api_rest_config_snapshot("345678901234567890")


def test_api_rest_config_snapshot_rejects_invalid_guild_before_transport():
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).api_rest_config_snapshot("bad")
    assert session.calls == []


def test_dashboard_config_snapshot_dispatches_and_rejects_unsafe_dto_fields():
    dto = {
        "guild_id": "345678901234567890",
        "welcome_channel_id": "456789012345678901",
        "warn_mute_threshold": 3,
        "command_templates": {"moderation": ["ban", "kick"]},
    }
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    assert client.dashboard_config_snapshot("345678901234567890") == dto
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {
        "method": "dashboard_config_snapshot",
        "args": ["345678901234567890"],
        "kwargs": {},
    }

    invalid_results = (
        {**dto, "log_webhook_url": "https://hooks.example/secret"},
        {**dto, "api_key": "secret"},
        {**dto, "future_unreviewed_field": "nope"},
        {**dto, "guild_id": "456789012345678901"},
        {**dto, "command_templates": {"access_token": ["secret"]}},
        {**dto, "command_roles": {"ban": {"role": 123}}},
        {**dto, "welcome_channel_id": {"unexpected": "object"}},
    )
    for invalid in invalid_results:
        bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": invalid}))
        with pytest.raises(BotServiceTransportError, match="invalid response"):
            _client(bad).dashboard_config_snapshot("345678901234567890")


def test_dashboard_calendar_upcoming_events_dispatches_and_validates_dto():
    now = datetime.datetime(2026, 11, 28, 12, 0, tzinfo=datetime.timezone.utc)
    dto = [{
        "_id": "0123456789abcdef01234567",
        "guild_id": "345678901234567890",
        "title": "Community event",
        "description": "A safe event",
        "role_id": "456789012345678901",
        "starts_at": "2026-12-01T18:00:00Z",
        "timezone": "Europe/Paris",
        "time_set": True,
    }]
    session = FakeSession(response=FakeResponse(payload={"ok": True, "result": dto}))
    client = _client(session)
    result = client.dashboard_calendar_upcoming_events("345678901234567890", now, limit=25)
    assert result[0]["starts_at"] == datetime.datetime(2026, 12, 1, 18, 0, tzinfo=datetime.timezone.utc)
    payload = json.loads(session.calls[0][1]["data"])
    assert payload == {
        "method": "dashboard_calendar_upcoming_events",
        "args": ["345678901234567890", "2026-11-28T12:00:00+00:00", 25],
        "kwargs": {},
    }

    invalid_results = (
        [{**dto[0], "guild_id": "123"}],
        [{**dto[0], "created_by": "private-user-id"}],
        [{**dto[0], "role_id": {"unexpected": "object"}}],
        [{**dto[0], "starts_at": "not-a-date"}],
        [{**dto[0], "time_set": 1}],
    )
    for invalid in invalid_results:
        bad = FakeSession(response=FakeResponse(payload={"ok": True, "result": invalid}))
        with pytest.raises(BotServiceTransportError, match="invalid response"):
            _client(bad).dashboard_calendar_upcoming_events("345678901234567890", now, limit=25)


def test_dashboard_calendar_upcoming_events_rejects_invalid_arguments_before_transport():
    session = FakeSession()
    client = _client(session)
    now = datetime.datetime(2026, 11, 28, 12, 0, tzinfo=datetime.timezone.utc)
    with pytest.raises(ValueError):
        client.dashboard_calendar_upcoming_events("bad", now)
    with pytest.raises(ValueError):
        client.dashboard_calendar_upcoming_events("123", datetime.datetime(2026, 11, 28))
    with pytest.raises(ValueError):
        client.dashboard_calendar_upcoming_events("123", now, limit=True)
    assert session.calls == []


@pytest.mark.parametrize("guild_id", ["bad", "0", "1" * 21])
def test_dashboard_config_snapshot_rejects_invalid_guild_ids(guild_id):
    session = FakeSession()
    with pytest.raises(ValueError):
        _client(session).dashboard_config_snapshot(guild_id)
    assert session.calls == []
