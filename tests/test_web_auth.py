import pytest
from urllib.parse import parse_qs, urlparse

import os

# web.auth reads OAuth configuration at import time, before pytest fixtures run.
os.environ.setdefault("DISCORD_CLIENT_ID", "test-client-id")
os.environ.setdefault("DISCORD_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("DISCORD_REDIRECT_URI", "http://localhost/callback")

import web.auth as auth


def test_login_encodes_oauth_parameters_and_sets_state(env):
    response = env.client.get("/login")
    assert response.status_code == 302

    location = urlparse(response.headers["Location"])
    query = parse_qs(location.query)
    assert location.netloc == "discord.com"
    assert query["client_id"] == [auth.DASH_CLIENT_ID]
    assert query["redirect_uri"] == [auth.DASH_REDIRECT_URI]
    assert query["scope"] == ["identify guilds"]
    assert len(query["state"][0]) >= 32

    with env.client.session_transaction() as sess:
        assert sess["oauth_state"] == query["state"][0]


def test_callback_rejects_invalid_state_without_calling_discord(env, monkeypatch):
    called = []
    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: called.append(True))
    response = env.client.get("/callback?code=authorization-code&state=wrong")
    assert response.status_code == 403
    assert called == []



def test_callback_rejects_missing_state_without_calling_discord(env, monkeypatch):
    called = []
    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: called.append(True))
    response = env.client.get("/callback?code=authorization-code")
    assert response.status_code == 403
    assert called == []


def test_callback_does_not_create_session_when_identity_lookup_fails(env, monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

    class FailedUserResponse:
        status_code = 503

    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: FailedUserResponse())

    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"
        sess["old_data"] = "must be cleared"

    response = env.client.get("/callback?code=authorization-code&state=valid-state")
    assert response.status_code == 502
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess
        assert "old_data" not in sess


def test_expired_oauth_session_is_cleared_and_redirected(env):
    with env.client.session_transaction() as sess:
        sess["access_token"] = "expired"
        sess["token_expires_at"] = 1

    response = env.client.get("/dashboard/111/calendar")
    assert response.status_code in (301, 302)
    assert response.headers["Location"].endswith("/login")
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess



def test_logout_rejects_get_requests(env):
    response = env.client.get("/logout")
    assert response.status_code == 405


def test_logout_rejects_missing_or_invalid_csrf(env):
    with env.client.session_transaction() as sess:
        sess["access_token"] = "valid"
        sess["csrf_token"] = "expected-token"

    response = env.client.post("/logout", data={"csrf_token": "wrong-token"})
    assert response.status_code == 403
    with env.client.session_transaction() as sess:
        assert sess["access_token"] == "valid"


def test_logout_with_valid_csrf_clears_session(env):
    with env.client.session_transaction() as sess:
        sess["access_token"] = "valid"
        sess["csrf_token"] = "expected-token"

    response = env.client.post("/logout", data={"csrf_token": "expected-token"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    with env.client.session_transaction() as sess:
        assert not sess


def test_callback_rejects_missing_or_invalid_token_expiration(env, monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "access", "refresh_token": "refresh"}

    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: FakeResponse())

    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"

    response = env.client.get("/callback?code=authorization-code&state=valid-state")
    assert response.status_code == 502
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess
        assert "token_expires_at" not in sess


@pytest.mark.parametrize("expires_at", [None, "never", True, float("nan"), float("inf"), 0])
def test_dashboard_rejects_missing_or_invalid_session_expiry(env, expires_at):
    with env.client.session_transaction() as sess:
        sess["access_token"] = "must-not-authenticate"
        if expires_at is not None:
            sess["token_expires_at"] = expires_at

    response = env.client.get("/dashboard/111/calendar")
    assert response.status_code in (301, 302)
    assert response.headers["Location"].endswith("/login")
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess
        assert "token_expires_at" not in sess
