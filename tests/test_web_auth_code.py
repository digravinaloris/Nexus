from urllib.parse import urlencode

import web.auth as auth


def _prepare_oauth_session(env):
    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "valid-state"
        sess["oauth_state_created_at"] = auth.time.time()
        sess["oauth_code_verifier"] = "A" * 43


def test_callback_rejects_malformed_authorization_codes_before_token_exchange(env, monkeypatch):
    codes = ["bad code", "bad\\ncode", "bad\\x7fcode", "x" * 2049]
    calls = []
    monkeypatch.setattr(auth.requests, "post", lambda *args, **kwargs: calls.append(True))

    for code in codes:
        _prepare_oauth_session(env)
        query = urlencode({"state": "valid-state", "code": code})
        response = env.client.get(f"/callback?{query}")
        assert response.status_code == 400
        assert calls == []
