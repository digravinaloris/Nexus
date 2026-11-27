"""Tests for the standalone Flask app factory used by the existing bot process."""

import os
import subprocess
import sys
from pathlib import Path

import pytest




def _create_app(config):
    # Import after test modules have populated their OAuth environment defaults.
    from web.app_factory import create_web_app

    return create_web_app(config=config)

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


def test_factory_installs_csrf_protection():
    app = create_web_app(config=_config())
    response = app.test_client().post("/dashboard/lang/fr", data={"next": "/dashboard"})
    assert response.status_code == 400


def test_factory_requires_a_secret_key():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _create_app(_config(SECRET_KEY=None))


def test_factory_imports_without_discord_bot_or_mongodb_runtime():
    root = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys

real_import = builtins.__import__
blocked = {"main", "discord", "pymongo"}

def guarded_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in blocked:
        raise AssertionError("web app factory imported forbidden runtime module: " + name)
    return real_import(name, *args, **kwargs)

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
