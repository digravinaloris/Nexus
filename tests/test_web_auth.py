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
    assert query["code_challenge_method"] == ["S256"]

    with env.client.session_transaction() as sess:
        assert sess["oauth_state"] == query["state"][0]
        assert isinstance(sess["oauth_state_created_at"], (int, float))
        verifier = sess["oauth_code_verifier"]
        assert 43 <= len(verifier) <= 128
        import base64
        import hashlib
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        assert query["code_challenge"] == [expected]


def test_callback_rejects_expired_state_without_calling_discord(env, monkeypatch):
    called = []
    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: called.append(True))
    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "expired-state"
        sess["oauth_state_created_at"] = auth.time.time() - auth.OAUTH_STATE_MAX_AGE - 1
    response = env.client.get("/callback?code=authorization-code&state=expired-state")
    assert response.status_code == 403
    assert called == []
    with env.client.session_transaction() as sess:
        assert "oauth_state" not in sess and "oauth_state_created_at" not in sess


def test_callback_rejects_missing_pkce_verifier_without_calling_discord(env, monkeypatch):
    called = []
    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: called.append(True))
    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"
        sess["oauth_state_created_at"] = auth.time.time()
    response = env.client.get("/callback?code=authorization-code&state=valid-state")
    assert response.status_code == 403
    assert called == []


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
        sess["oauth_code_verifier"] = "A" * 43

        sess["oauth_state_created_at"] = auth.time.time()
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
        sess["csrf_token"] = "expected-csrf-token-" + "x" * 32

    response = env.client.post("/logout", data={"csrf_token": "wrong-token"})
    assert response.status_code == 403
    with env.client.session_transaction() as sess:
        assert sess["access_token"] == "valid"


@pytest.mark.parametrize("token", ["", "short-token", "x" * 31])
def test_logout_rejects_weak_csrf_tokens(env, token):
    with env.client.session_transaction() as sess:
        sess["access_token"] = "valid"
        sess["csrf_token"] = token

    response = env.client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 403
    with env.client.session_transaction() as sess:
        assert sess["access_token"] == "valid"


def test_logout_with_valid_csrf_clears_session(env):
    with env.client.session_transaction() as sess:
        sess["access_token"] = "valid"
        sess["csrf_token"] = "expected-csrf-token-" + "x" * 32

    response = env.client.post("/logout", data={"csrf_token": "expected-csrf-token-" + "x" * 32})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    with env.client.session_transaction() as sess:
        assert not sess


@pytest.mark.parametrize("access_token", [" ", "token\\nInjected: yes", "token\\rvalue", "x" * 4097])
def test_callback_rejects_malformed_access_tokens(env, monkeypatch, access_token):
    class TokenResponse:
        status_code = 200

        def json(self):
            return {"access_token": access_token, "expires_in": 3600}

    calls = []
    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: TokenResponse())
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: calls.append(True))

    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"
        sess["oauth_code_verifier"] = "A" * 43
        sess["oauth_state_created_at"] = auth.time.time()

    response = env.client.get("/callback?code=authorization-code&state=valid-state")
    assert response.status_code == 502
    assert calls == []
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess


def test_callback_rejects_missing_or_invalid_token_expiration(env, monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "access", "refresh_token": "refresh"}

    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: FakeResponse())

    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"
        sess["oauth_code_verifier"] = "A" * 43

        sess["oauth_state_created_at"] = auth.time.time()

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


def test_dashboard_permissions_are_refetched_instead_of_using_cached_access(env, monkeypatch):
    from flask import session

    class Response:
        status_code = 200

        def json(self):
            return []

    calls = []
    monkeypatch.setattr(auth.requests, "get", lambda *args, **kwargs: calls.append((args, kwargs)) or Response())

    with env.nexus.api.test_request_context("/dashboard"):
        session["access_token"] = "valid-access-token"
        session["admin_guilds_cache"] = [{"id": "123456789012345678", "permissions": "8"}]
        session["admin_guilds_cache_at"] = 4102444700
        assert auth.get_user_admin_guilds() == []

    assert len(calls) == 1


def test_dashboard_permissions_fail_closed_when_discord_is_unavailable(env, monkeypatch):
    from flask import session

    class Response:
        status_code = 503

    calls = []
    monkeypatch.setattr(auth.requests, "get", lambda *args, **kwargs: calls.append(True) or Response())

    with env.nexus.api.test_request_context("/dashboard"):
        session["access_token"] = "valid-access-token"
        session["admin_guilds_cache"] = [{"id": "123456789012345678", "permissions": "8"}]
        session["admin_guilds_cache_at"] = 4102444700
        assert auth.get_user_admin_guilds() is None

    assert calls == [True]


def test_dashboard_permissions_ignore_malformed_discord_guild_entries(env, monkeypatch):
    from flask import session

    class Response:
        status_code = 200

        def json(self):
            return [
                {"id": "123456789012345678", "permissions": "not-a-number"},
                {"id": "not-a-snowflake", "permissions": "8"},
                {"id": "123456789012345679", "permissions": "8"},
            ]

    monkeypatch.setattr(auth.requests, "get", lambda *args, **kwargs: Response())

    with env.nexus.api.test_request_context("/dashboard"):
        session["access_token"] = "valid-access-token"
        assert auth.get_user_admin_guilds() == [
            {"id": "123456789012345679", "permissions": "8"}
        ]


def test_dashboard_request_returns_temporary_error_when_discord_is_unavailable(env, monkeypatch):
    class Response:
        status_code = 503

    monkeypatch.setattr(auth.requests, "get", lambda *args, **kwargs: Response())
    with env.client.session_transaction() as sess:
        sess["access_token"] = "valid-access-token"
        sess["token_expires_at"] = 4102444800
        sess["admin_guilds_cache"] = [{"id": "123456789012345678", "permissions": "8"}]
        sess["admin_guilds_cache_at"] = 4102444700

    response = env.client.get("/dashboard/123456789012345678/calendar")
    assert response.status_code == 503
    assert response.get_json() == {
        "error": "Unable to verify Discord server permissions. Please try again shortly."
    }


@pytest.mark.parametrize("endpoint", ["token", "identity"])
def test_callback_rejects_malformed_oauth_json_without_session(env, monkeypatch, endpoint):
    class MalformedResponse:
        status_code = 200

        def json(self):
            raise ValueError("invalid JSON")

    class ValidTokenResponse:
        status_code = 200

        def json(self):
            return {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
            }

    if endpoint == "token":
        monkeypatch.setattr(auth.requests, "post", lambda *a, **k: MalformedResponse())
        monkeypatch.setattr(auth.requests, "get", lambda *a, **k: pytest.fail("identity lookup must not run"))
    else:
        monkeypatch.setattr(auth.requests, "post", lambda *a, **k: ValidTokenResponse())
        monkeypatch.setattr(auth.requests, "get", lambda *a, **k: MalformedResponse())

    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"
        sess["oauth_code_verifier"] = "A" * 43

        sess["oauth_state_created_at"] = auth.time.time()

    response = env.client.get("/callback?code=authorization-code&state=valid-state")
    assert response.status_code == 502
    assert response.get_json() == {
        "error": "Discord authentication is temporarily unavailable"
    }
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess
        assert "refresh_token" not in sess
        assert "token_expires_at" not in sess


@pytest.mark.parametrize("user_id", [None, "", "123", "not-a-snowflake", "1234567890123456", "123456789012345678901", 123456789012345678])
def test_callback_rejects_invalid_discord_user_id(env, monkeypatch, user_id):
    class TokenResponse:
        status_code = 200


        def json(self):
            return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

    class UserResponse:
        status_code = 200

        def json(self):
            return {"id": user_id}

    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: TokenResponse())
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: UserResponse())
    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"
        sess["oauth_code_verifier"] = "A" * 43

        sess["oauth_state_created_at"] = auth.time.time()

    response = env.client.get("/callback?code=authorization-code&state=valid-state")
    assert response.status_code == 502
    assert response.get_json() == {"error": "Invalid Discord user response"}
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess
        assert "refresh_token" not in sess
        assert "token_expires_at" not in sess


@pytest.mark.parametrize("guild_id", ["12345678901234567", "1234567890123456789", "12345678901234567890"])
def test_guild_id_validation_accepts_supported_snowflake_lengths(guild_id):
    assert auth.GUILD_ID_RE.fullmatch(guild_id)


@pytest.mark.parametrize("guild_id", ["1234567890123456", "123456789012345678901", "invalid", ""])
def test_guild_id_validation_rejects_invalid_snowflake_lengths(guild_id):
    assert not auth.GUILD_ID_RE.fullmatch(guild_id)
