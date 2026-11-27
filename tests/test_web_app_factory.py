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
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
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
