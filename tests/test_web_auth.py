from urllib.parse import parse_qs, urlparse

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
