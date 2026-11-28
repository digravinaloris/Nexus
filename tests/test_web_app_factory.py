"""Tests for the standalone Flask app factory used by the existing bot process."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _create_app(config, bot_service=None, **kwargs):
    # Import after test modules have populated their OAuth environment defaults.
    from web.app_factory import create_web_app

    return create_web_app(config=config, bot_service=bot_service, **kwargs)


def _config(**overrides):
    values = {
        "SECRET_KEY": "web-factory-test-secret",
        "DISCORD_CLIENT_ID": "factory-client-id",
        "DISCORD_CLIENT_SECRET": "factory-client-secret",
        "DISCORD_REDIRECT_URI": "http://localhost/callback",
    }
    values.update(overrides)
    return values


def test_factory_registers_existing_routes_and_security_hooks():
    app = _create_app(_config())
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert "/" in rules
    assert "/login" in rules
    assert "/callback" in rules
    assert "/dashboard/<guild_id>/calendar" in rules
    assert "/api/guilds" in rules
    assert app.config["SESSION_COOKIE_NAME"] == "__Host-nexus_session"
    assert app.config["SESSION_COOKIE_DOMAIN"] is None
    assert app.config["SESSION_COOKIE_PATH"] == "/"
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_REFRESH_EACH_REQUEST"] is False
    assert callable(app.jinja_env.globals["t"])
    assert callable(app.jinja_env.globals["lang"])
    assert callable(app.jinja_env.globals["theme"])

    response = app.test_client().get("/login")
    assert response.status_code == 302
    assert "factory-client-id" in response.headers["Location"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"




def test_factory_resolves_oauth_settings_per_app():
    first = _create_app(_config(
        DISCORD_CLIENT_ID="first-client",
        DISCORD_CLIENT_SECRET="first-secret",
        DISCORD_REDIRECT_URI="https://first.example/callback",
    ))
    second = _create_app(_config(
        DISCORD_CLIENT_ID="second-client",
        DISCORD_CLIENT_SECRET="second-secret",
        DISCORD_REDIRECT_URI="https://second.example/callback",
    ))

    from web.auth import _oauth_config

    with first.app_context():
        assert _oauth_config() == (
            "first-client", "first-secret", "https://first.example/callback"
        )
    with second.app_context():
        assert _oauth_config() == (
            "second-client", "second-secret", "https://second.example/callback"
        )

def test_factory_injects_flask_session_interface():
    class FakeSessionInterface:
        pass

    session_interface = FakeSessionInterface()
    app = _create_app(_config(), session_interface=session_interface)

    assert app.session_interface is session_interface


def test_factory_can_require_a_complete_server_side_session_backend():
    class CompleteServerSessionInterface:
        nexus_server_side_sessions = True

        def consume_oauth_state(self, session, expected_state):
            return True

        def rotate_session(self, session):
            return None

        def revoke_session(self, session):
            return None

    backend = CompleteServerSessionInterface()
    app = _create_app(
        _config(),
        session_interface=backend,
        require_server_side_sessions=True,
    )

    assert app.session_interface is backend


@pytest.mark.parametrize(
    "backend",
    [
        None,
        object(),
        type("PartialBackend", (), {
            "nexus_server_side_sessions": True,
            "consume_oauth_state": lambda self, session, state: True,
            "rotate_session": lambda self, session: None,
        })(),
    ],
)
def test_factory_rejects_missing_or_incomplete_server_side_session_backend(backend):
    with pytest.raises(RuntimeError, match="server-side session interface"):
        _create_app(
            _config(),
            session_interface=backend,
            require_server_side_sessions=True,
        )


def test_factory_keeps_legacy_session_mode_when_server_side_sessions_not_required():
    app = _create_app(_config())

    assert app.session_interface is not None
    assert app.test_client().get("/login").status_code == 302


def test_factory_installs_csrf_protection():
    app = _create_app(_config())
    response = app.test_client().post("/dashboard/lang/fr", data={"next": "/dashboard"})
    assert response.status_code == 400



def test_factory_injects_bot_service_per_app_without_global_leakage():
    class FakeBotService:
        def __init__(self, source):
            self.source = source

        def health_snapshot(self):
            return {"source": self.source}

    first_service = FakeBotService("first")
    second_service = FakeBotService("second")
    first_app = _create_app(_config(), bot_service=first_service)
    second_app = _create_app(_config(), bot_service=second_service)

    assert first_app.test_client().get("/").get_json() == {"source": "first"}
    assert second_app.test_client().get("/").get_json() == {"source": "second"}

    from web.bot_service import get_bot_service

    with first_app.app_context():
        assert get_bot_service() is first_service
    with second_app.app_context():
        assert get_bot_service() is second_service



def test_internal_dispatch_registration_is_explicit_and_all_or_nothing():
    from web.app_factory import create_web_app
    from web.bot_service_signing import sign_internal_request

    default_app = _create_app(_config())
    assert "/internal/v1/dispatch" not in {
        rule.rule for rule in default_app.url_map.iter_rules()
    }

    class FakeBotService:
        def health_snapshot(self):
            return {"bot": "online"}

    class FakeNonceStore:
        def __init__(self):
            self.seen = set()

        def consume(self, key_id, nonce, expires_at):
            marker = (key_id, nonce)
            if marker in self.seen:
                return False
            self.seen.add(marker)
            return True

    secret = b"app-factory-internal-key-material-32-bytes"
    app = create_web_app(
        config=_config(),
        internal_bot_service=FakeBotService(),
        internal_bot_service_keyring={"test": secret},
        internal_nonce_store=FakeNonceStore(),
    )
    body = b'{"method":"health_snapshot","args":[],"kwargs":{}}'
    headers = sign_internal_request(
        secret=secret,
        key_id="test",
        method="POST",
        path="/internal/v1/dispatch",
        body=body,
    )
    headers["Content-Type"] = "application/json"
    response = app.test_client().post(
        "/internal/v1/dispatch", data=body, headers=headers
    )
    assert response.status_code == 200
    assert response.get_json()["result"] == {"bot": "online"}

    with pytest.raises(ValueError, match="requires a service, keyring, and nonce store"):
        create_web_app(
            config=_config(),
            internal_bot_service=FakeBotService(),
        )


def test_factory_requires_a_secret_key():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _create_app(_config(SECRET_KEY=None))


def test_factory_imports_without_discord_bot_or_mongodb_runtime():
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys

real_import = builtins.__import__
blocked = {
    "main",
    "discord",
    "pymongo",
    "requests",
    "bson",
    "core",
    "bridge",
    "cogs",
    "audit",
    "audit_export",
    "calendar_store",
    "template_ops",
    "template_store",
    "killswitch_store",
    "verification",
    "config_store",
    "faq_store",
    "discord_logging",
    "sanctions_store",
    "moderation_actions",
    "owner_notify",
}

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    # Relative imports such as click.core pass only "core" as the name;
    # only block absolute imports of Nexus runtime modules.
    if level == 0 and name.split(".", 1)[0] in blocked:
        raise AssertionError("web app factory imported forbidden runtime module: " + name)
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
from web.app_factory import create_web_app

app = create_web_app(config={
    "SECRET_KEY": "isolated-factory-test",
    "DISCORD_CLIENT_ID": "client",
    "DISCORD_CLIENT_SECRET": "secret",
    "DISCORD_REDIRECT_URI": "http://localhost/callback",
})
assert app.test_client().get("/login").status_code == 302
assert "main" not in sys.modules
assert "discord" not in sys.modules
assert "pymongo" not in sys.modules
"""
    env = os.environ.copy()
    for name in (
        "FLASK_SECRET_KEY",
        "DISCORD_CLIENT_ID",
        "DISCORD_CLIENT_SECRET",
        "DISCORD_REDIRECT_URI",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_factory_accepts_explicit_environment_mapping():
    app = _create_app(
        {},
        environ={
            "FLASK_SECRET_KEY": "injected-secret",
            "DISCORD_CLIENT_ID": "injected-client",
            "DISCORD_CLIENT_SECRET": "injected-oauth-secret",
            "DISCORD_REDIRECT_URI": "https://injected.example/callback",
            "API_KEY": "injected-api-key",
        },
    )

    assert app.config["SECRET_KEY"] == "injected-secret"
    assert app.config["DISCORD_CLIENT_ID"] == "injected-client"
    assert app.config["DISCORD_CLIENT_SECRET"] == "injected-oauth-secret"
    assert app.config["DISCORD_REDIRECT_URI"] == "https://injected.example/callback"
    assert app.config["API_KEY"] == "injected-api-key"


def test_factory_config_overrides_injected_environment(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "process-secret")
    app = _create_app(
        {
            "SECRET_KEY": "config-secret",
            "DISCORD_CLIENT_ID": "config-client",
        },
        environ={
            "FLASK_SECRET_KEY": "mapping-secret",
            "DISCORD_CLIENT_ID": "mapping-client",
            "DISCORD_CLIENT_SECRET": "mapping-oauth-secret",
            "DISCORD_REDIRECT_URI": "https://mapping.example/callback",
        },
    )

    assert app.config["SECRET_KEY"] == "config-secret"
    assert app.config["DISCORD_CLIENT_ID"] == "config-client"
    assert app.config["DISCORD_CLIENT_SECRET"] == "mapping-oauth-secret"
    assert app.config["DISCORD_REDIRECT_URI"] == "https://mapping.example/callback"


def test_factory_empty_environment_mapping_does_not_read_process_secrets(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "process-secret")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "process-client")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "process-oauth-secret")
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://process.example/callback")
    monkeypatch.setenv("API_KEY", "process-api-key")

    app = _create_app({"SECRET_KEY": "config-secret"}, environ={})

    assert app.config["SECRET_KEY"] == "config-secret"
    assert app.config["DISCORD_CLIENT_ID"] == ""
    assert app.config["DISCORD_CLIENT_SECRET"] == ""
    assert app.config["DISCORD_REDIRECT_URI"] == ""
    assert app.config["API_KEY"] is None


def test_factory_uses_process_environment_when_mapping_is_not_injected(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "default-process-secret")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "default-process-client")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "default-process-oauth-secret")
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://default.example/callback")
    monkeypatch.setenv("API_KEY", "default-process-api-key")

    app = _create_app({})

    assert app.config["SECRET_KEY"] == "default-process-secret"
    assert app.config["DISCORD_CLIENT_ID"] == "default-process-client"
    assert app.config["DISCORD_CLIENT_SECRET"] == "default-process-oauth-secret"
    assert app.config["DISCORD_REDIRECT_URI"] == "https://default.example/callback"
    assert app.config["API_KEY"] == "default-process-api-key"



def test_factory_requires_explicit_secret_when_runtime_environment_is_empty(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "process-secret-must-not-fallback")

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _create_app({}, environ={})



def test_factory_injects_totp_secret_from_runtime_environment(monkeypatch):
    monkeypatch.setenv("TOTP_SECRET", "process-totp-secret")

    app = _create_app(_config(), environ={"TOTP_SECRET": "injected-totp-secret"})
    isolated_app = _create_app(_config(), environ={})

    assert app.config["TOTP_SECRET"] == "injected-totp-secret"
    assert isolated_app.config["TOTP_SECRET"] == ""


def test_factory_injects_turnstile_settings_and_derives_public_base_url(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "process-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "process-secret-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://process.example")

    app = _create_app(_config(), environ={
        "TURNSTILE_SITE_KEY": "injected-site-key",
        "TURNSTILE_SECRET_KEY": "injected-secret-key",
        "DISCORD_REDIRECT_URI": "https://injected.example/callback",
    })
    isolated_app = _create_app(_config(), environ={})

    assert app.config["TURNSTILE_SITE_KEY"] == "injected-site-key"
    assert app.config["TURNSTILE_SECRET_KEY"] == "injected-secret-key"
    assert app.config["PUBLIC_BASE_URL"] == "https://injected.example"
    assert isolated_app.config["TURNSTILE_SITE_KEY"] == ""
    assert isolated_app.config["TURNSTILE_SECRET_KEY"] == ""
    assert isolated_app.config["PUBLIC_BASE_URL"] == ""



def test_factory_injects_github_webhook_secret_without_process_fallback(monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", "process-webhook-secret")

    app = _create_app(_config(), environ={"GITHUB_RELEASE_WEBHOOK_SECRET": "injected-webhook-secret"})
    isolated_app = _create_app(_config(), environ={})

    assert app.config["GITHUB_RELEASE_WEBHOOK_SECRET"] == "injected-webhook-secret"
    assert isolated_app.config["GITHUB_RELEASE_WEBHOOK_SECRET"] == ""


def test_login_uses_injected_session_backend_without_setting_a_client_cookie():
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession()
            self.saved = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            self.saved += 1

    backend = FakeSessionInterface()
    app = _create_app(_config(), session_interface=backend)

    response = app.test_client().get("/login")

    assert response.status_code == 302
    assert "oauth_state" in backend.session
    assert "oauth_state_created_at" in backend.session
    assert "oauth_code_verifier" in backend.session
    assert "Set-Cookie" not in response.headers
    assert backend.saved == 1


def test_oauth_callback_keeps_discord_credentials_in_injected_session_backend():
    import time
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                oauth_state="server-side-state",
                oauth_state_created_at=time.time(),
                oauth_code_verifier="A" * 43,
            )
            self.saved = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            self.saved += 1

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class FakeDiscordHttpClient:
        def post(self, url, **kwargs):
            assert url.endswith("/oauth2/token")
            assert kwargs["data"]["code_verifier"] == "A" * 43
            return FakeResponse(200, {
                "access_token": "server-only-access-token",
                "refresh_token": "unused-refresh-token",
                "expires_in": 3600,
            })

        def get(self, url, **kwargs):
            assert url.endswith("/users/@me")
            assert kwargs["headers"]["Authorization"] == "Bearer server-only-access-token"
            return FakeResponse(200, {
                "id": "123456789012345678",
                "username": "server-session-user",
            })

    backend = FakeSessionInterface()
    app = _create_app(
        _config(DISCORD_HTTP_CLIENT=FakeDiscordHttpClient()),
        session_interface=backend,
    )

    response = app.test_client().get(
        "/callback?code=valid-authorization-code&state=server-side-state"
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    assert "Set-Cookie" not in response.headers
    assert backend.session["access_token"] == "server-only-access-token"
    assert backend.session["dash_user"]["username"] == "server-session-user"
    assert isinstance(backend.session["csrf_token"], str)
    assert "refresh_token" not in backend.session
    assert "oauth_state" not in backend.session
    assert "oauth_code_verifier" not in backend.session
    assert backend.saved == 1


def test_server_session_backend_rotates_after_successful_oauth_and_revokes_on_logout():
    import time
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                oauth_state="pre-auth-state",
                oauth_state_created_at=time.time(),
                oauth_code_verifier="A" * 43,
            )
            self.rotations = 0
            self.revocations = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            pass

        def rotate_session(self, session):
            assert dict(session) == dict(self.session)
            self.rotations += 1

        def revoke_session(self, session):
            assert dict(session) == dict(self.session)
            self.revocations += 1

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class FakeDiscordHttpClient:
        def post(self, url, **kwargs):
            return FakeResponse(200, {
                "access_token": "server-only-access-token",
                "expires_in": 3600,
            })

        def get(self, url, **kwargs):
            return FakeResponse(200, {
                "id": "123456789012345678",
                "username": "session-lifecycle-user",
            })

    backend = FakeSessionInterface()
    app = _create_app(
        _config(DISCORD_HTTP_CLIENT=FakeDiscordHttpClient()),
        session_interface=backend,
    )
    client = app.test_client()

    response = client.get(
        "/callback?code=valid-authorization-code&state=pre-auth-state"
    )
    assert response.status_code == 302
    assert backend.rotations == 1
    assert backend.revocations == 0
    assert backend.session["access_token"] == "server-only-access-token"
    assert "Set-Cookie" not in response.headers

    csrf_token = backend.session["csrf_token"]
    response = client.post("/logout", data={"csrf_token": csrf_token})
    assert response.status_code == 302
    assert backend.revocations == 1
    assert not backend.session
    assert "Set-Cookie" not in response.headers


def test_expired_oauth_session_is_revoked_by_injected_backend():
    import time
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                access_token="expired-server-only-token",
                token_expires_at=time.time() - 1,
                dash_user={"id": "123456789012345678"},
            )
            self.revocations = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            pass

        def revoke_session(self, session):
            assert dict(session) == dict(self.session)
            self.revocations += 1

    backend = FakeSessionInterface()
    app = _create_app(_config(), session_interface=backend)

    response = app.test_client().get("/dashboard")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    assert backend.revocations == 1
    assert not backend.session
    assert "Set-Cookie" not in response.headers


def test_discord_401_revokes_injected_server_session():
    from flask.sessions import SessionInterface, SessionMixin
    from web.auth import get_user_admin_guilds

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(access_token="revoked-discord-token")
            self.revocations = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            pass

        def revoke_session(self, session):
            assert dict(session) == dict(self.session)
            self.revocations += 1

    class UnauthorizedResponse:
        status_code = 401

    class FakeDiscordHttpClient:
        def get(self, url, **kwargs):
            assert url.endswith("/users/@me/guilds")
            return UnauthorizedResponse()

    backend = FakeSessionInterface()
    app = _create_app(
        _config(DISCORD_HTTP_CLIENT=FakeDiscordHttpClient()),
        session_interface=backend,
    )

    with app.test_request_context("/dashboard"):
        assert get_user_admin_guilds() is None

    assert backend.revocations == 1
    assert not backend.session


def test_dashboard_request_redirects_and_revokes_after_discord_401():
    import time
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                access_token="discord-rejected-token",
                token_expires_at=time.time() + 3600,
                dash_user={"id": "123456789012345678"},
            )
            self.revocations = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            pass

        def revoke_session(self, session):
            assert dict(session) == dict(self.session)
            self.revocations += 1

    class UnauthorizedResponse:
        status_code = 401

    class FakeDiscordHttpClient:
        def get(self, url, **kwargs):
            assert url.endswith("/users/@me/guilds")
            return UnauthorizedResponse()

    backend = FakeSessionInterface()
    app = _create_app(
        _config(DISCORD_HTTP_CLIENT=FakeDiscordHttpClient()),
        session_interface=backend,
    )

    response = app.test_client().get("/dashboard/123456789012345678/calendar")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    assert backend.revocations == 1
    assert not backend.session
    assert "Set-Cookie" not in response.headers


def test_oauth_callback_fails_closed_when_session_rotation_fails():
    import time
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                oauth_state="rotation-failure-state",
                oauth_state_created_at=time.time(),
                oauth_code_verifier="B" * 43,
            )

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            pass

        def rotate_session(self, session):
            raise RuntimeError("session backend rotation unavailable")

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class FakeDiscordHttpClient:
        def post(self, url, **kwargs):
            return FakeResponse(200, {"access_token": "never-persist-this-token", "expires_in": 3600})

        def get(self, url, **kwargs):
            return FakeResponse(200, {"id": "123456789012345678", "username": "rotation-failure-user"})



    backend = FakeSessionInterface()
    app = _create_app(
        _config(DISCORD_HTTP_CLIENT=FakeDiscordHttpClient()),
        session_interface=backend,
    )


    response = app.test_client().get(
        "/callback?code=valid-authorization-code&state=rotation-failure-state"
    )


    assert response.status_code == 503
    assert response.get_json() == {"error": "OAuth session storage unavailable"}
    # Rotation failure must leave neither pre-authentication nor authenticated
    # session data behind, and must not issue a browser cookie.
    assert not backend.session
    assert "access_token" not in backend.session
    assert "csrf_token" not in backend.session
    assert "dash_user" not in backend.session
    assert "Set-Cookie" not in response.headers



def test_oauth_callback_fails_closed_when_session_save_fails():
    import time
    from flask.sessions import SessionInterface, SessionMixin


    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                oauth_state="save-failure-state",
                oauth_state_created_at=time.time(),
                oauth_code_verifier="C" * 43,
            )
            self.persisted = {}
            self.rotations = 0
            self.save_attempts = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            self.save_attempts += 1
            raise RuntimeError("session backend write unavailable")

        def rotate_session(self, session):
            self.rotations += 1

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class FakeDiscordHttpClient:
        def post(self, url, **kwargs):
            return FakeResponse(200, {"access_token": "write-failure-token", "expires_in": 3600})

        def get(self, url, **kwargs):
            return FakeResponse(200, {"id": "123456789012345678", "username": "write-failure-user"})



    backend = FakeSessionInterface()
    app = _create_app(
        _config(DISCORD_HTTP_CLIENT=FakeDiscordHttpClient()),
        session_interface=backend,
    )

    response = app.test_client().get(
        "/callback?code=valid-authorization-code&state=save-failure-state"
    )



    assert response.status_code == 500
    assert backend.rotations == 1
    assert backend.save_attempts == 2  # Flask attempts to save both the original and error response
    assert backend.persisted == {}
    assert "Set-Cookie" not in response.headers



def test_logout_clears_session_when_backend_revocation_fails():
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                access_token="logout-revocation-failure-token",
                token_expires_at=9999999999,
                dash_user={"id": "123456789012345678"},
                csrf_token="Z" * 32,
            )

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            pass

        def revoke_session(self, session):
            raise RuntimeError("session backend revoke unavailable")

    backend = FakeSessionInterface()
    app = _create_app(_config(), session_interface=backend)
    response = app.test_client().post("/logout", data={"csrf_token": "Z" * 32})

    assert response.status_code == 503
    assert response.get_json() == {"error": "Unable to revoke session. Please retry."}
    assert not backend.session
    assert "Set-Cookie" not in response.headers


def test_oauth_callback_rejects_state_already_consumed_by_another_request():
    import time
    from flask.sessions import SessionInterface, SessionMixin

    class BackendSession(dict, SessionMixin):
        new = False
        modified = False
        accessed = False

    class FakeSessionInterface(SessionInterface):
        def __init__(self):
            self.session = BackendSession(
                oauth_state="already-consumed-state",
                oauth_state_created_at=time.time(),
                oauth_code_verifier="C" * 43,
            )
            self.consume_calls = 0

        def open_session(self, app, request):
            return self.session

        def save_session(self, app, session, response):
            pass

        def consume_oauth_state(self, session, expected_state):
            self.consume_calls += 1
            assert expected_state == "already-consumed-state"
            return False

    class DiscordMustNotBeCalled:
        def post(self, *args, **kwargs):
            raise AssertionError("OAuth token exchange ran after state consumption failed")

        def get(self, *args, **kwargs):
            raise AssertionError("Discord identity lookup ran after state consumption failed")

    backend = FakeSessionInterface()
    app = _create_app(
        _config(DISCORD_HTTP_CLIENT=DiscordMustNotBeCalled()),
        session_interface=backend,
    )

    response = app.test_client().get(
        "/callback?code=valid-authorization-code&state=already-consumed-state"
    )

    assert response.status_code == 403
    assert response.get_json() == {"error": "Invalid state"}
    assert backend.consume_calls == 1
    assert backend.session["oauth_state"] == "already-consumed-state"
    assert "Set-Cookie" not in response.headers
