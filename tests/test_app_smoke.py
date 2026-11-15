"""Smoke tests : main.py doit pouvoir être importé SANS lancer le bot ni Flask.

Ces tests ne parlent ni à Discord ni à MongoDB. Ils servent de filet de
sécurité pour le découpage de main.py : si un refactor casse un import, une
route ou un décorateur, ça pète ici avant d'arriver sur Render.

Ils sont ignorés (skip) si les dépendances lourdes ne sont pas installées.
"""
import os
import sys
import pytest

pytest.importorskip("discord")
pytest.importorskip("flask")
pytest.importorskip("PIL")
pytest.importorskip("matplotlib")
pytest.importorskip("reportlab")
pytest.importorskip("deep_translator")
pytest.importorskip("sentry_sdk")
pytest.importorskip("pyotp")
pytest.importorskip("yaml")
pytest.importorskip("pymongo")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def nexus(tmp_path_factory):
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
    os.environ.setdefault("DISCORD_CLIENT_ID", "0")
    os.environ.setdefault("DISCORD_CLIENT_SECRET", "test")
    os.environ.setdefault("DISCORD_REDIRECT_URI", "http://localhost/callback")
    os.environ.pop("SENTRY_DSN", None)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    old_cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        import main
        yield main
    finally:
        os.chdir(old_cwd)


def test_import_has_no_side_effects(nexus):
    # Rien ne doit tourner à l'import : ni serveur Flask, ni bot connecté.
    assert nexus.bot.is_closed() is False  # objet créé mais jamais lancé
    assert not nexus.bot.is_ready()
    assert nexus.db is None  # init_mongo() n'est appelé que dans on_ready


def test_main_entrypoint_exists(nexus):
    assert callable(nexus.main)


def test_slash_commands_are_registered(nexus):
    names = {c.name for c in nexus.bot.tree.get_commands()}
    # Un échantillon de commandes critiques qui ne doivent jamais disparaître.
    for expected in ("ban", "kick", "config", "feature", "backup", "template"):
        assert expected in names, f"commande /{expected} manquante"
    assert len(names) >= 50


def test_api_routes_are_registered(nexus):
    rules = {r.rule for r in nexus.api.url_map.iter_rules()}
    assert "/" in rules
    assert any(r.startswith("/api/") for r in rules)
    assert len(rules) >= 60


def test_all_feature_toggles_present(nexus):
    assert len(nexus.FEATURE_TOGGLES) >= 17


def test_status_endpoint_returns_json_without_mongo(nexus):
    client = nexus.api.test_client()
    resp = client.get("/")
    assert resp.status_code == 200  # toujours 200 (UptimeRobot)
    assert resp.is_json


def test_api_requires_key(nexus):
    client = nexus.api.test_client()
    resp = client.get("/api/guilds")
    assert resp.status_code in (401, 403)


def test_dashboard_redirects_when_logged_out(nexus):
    client = nexus.api.test_client()
    resp = client.get("/dashboard")
    assert resp.status_code in (301, 302, 303, 307, 308, 401, 403)
