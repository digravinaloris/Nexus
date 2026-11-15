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


def test_core_stays_in_sync_with_main_collections(nexus, monkeypatch):
    """Anti-dérive : core.col et les variables xxx_col de main.py doivent
    pointer vers les MÊMES collections. Si quelqu'un ajoute une collection dans
    init_mongo() sans la déclarer dans core.COLLECTION_NAMES (ou l'inverse),
    ce test échoue."""
    import core

    class FakeCollection:
        def __init__(self, name):
            self.name = name

    class FakeDB:
        def __init__(self):
            self._cols = {}

        def __getitem__(self, name):
            return self._cols.setdefault(name, FakeCollection(name))

    class FakeMongo:
        def __init__(self, *a, **kw):
            self._dbs = {}

        def __getitem__(self, name):
            return self._dbs.setdefault(name, FakeDB())

        def close(self):
            pass

    # monkeypatch.setattr(x, valeur_actuelle) = on mémorise pour tout restaurer après.
    col_globals = [n for n in vars(nexus) if n.endswith("_col")]
    for name in col_globals + ["mongo", "db"]:
        monkeypatch.setattr(nexus, name, getattr(nexus, name))
    monkeypatch.setattr(nexus, "MongoClient", FakeMongo)
    monkeypatch.setattr(nexus.bot, "db", None, raising=False)

    core.reset()
    try:
        nexus.init_mongo()

        assert core.is_ready()
        assert core.db is nexus.db

        # core -> main : chaque collection de core a sa variable xxx_col identique.
        # (faq n'a pas de variable dans main : elle passe par bot.db["faq"].)
        main_only_in_core = {"faq"}
        for attr in core.COLLECTION_NAMES:
            if attr in main_only_in_core:
                continue
            main_var = getattr(nexus, f"{attr}_col", None)
            assert main_var is not None, f"main.{attr}_col manquante"
            assert main_var is getattr(core.col, attr), f"core.col.{attr} != main.{attr}_col"

        # main -> core : toute variable xxx_col de main est déclarée dans core.
        declared = {f"{a}_col" for a in core.COLLECTION_NAMES}
        for name in col_globals:
            assert name in declared, f"{name} (main.py) absente de core.COLLECTION_NAMES"

        assert core.col.faq is nexus.db["faq"]
    finally:
        core.reset()
