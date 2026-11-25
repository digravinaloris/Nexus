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
    assert "/dashboard/<guild_id>/calendar" in rules


def test_config_autorole_command_is_registered_and_optional(nexus):
    config = next(c for c in nexus.bot.tree.get_commands() if c.name == "config")
    command = next(command for command in config.commands if command.name == "autorole")
    assert command.parameters[0].required is False

def test_automod_exempt_roles_command_is_registered(nexus):
    config = next(c for c in nexus.bot.tree.get_commands() if c.name == "config")
    assert any(command.name == "automodexempt" for command in config.commands)


def test_automod_exempt_roles_bypass_only_when_configured(nexus):
    member = type("Member", (), {"guild_permissions": type("Permissions", (), {"manage_messages": False})(),
                                  "roles": [type("Role", (), {"id": 77})()]})()
    assert nexus.is_automod_exempt(member, {"automod_exempt_role_ids": ["77"]})
    assert not nexus.is_automod_exempt(member, {"automod_exempt_role_ids": ["78"]})
    assert not nexus.is_automod_exempt(member, {})


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
        # (faq / verification_challenges n'ont pas de variable dans main : elles passent par core.col.)
        main_only_in_core = {"faq", "verification_challenges"}  # accessibles via core.col uniquement
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


def test_extracted_helpers_are_reexported_by_main(nexus):
    """Étape 2a : les fonctions déplacées hors de main.py doivent rester
    accessibles sous les MÊMES noms (tout le code de main.py les appelle ainsi)."""
    import audit
    import config_store
    import core
    import i18n
    import sanctions_store

    for mod, names in {
        config_store: ["get_config", "update_config", "add_command_role", "remove_command_role",
                       "set_command_template", "get_global_lock", "set_global_lock"],
        sanctions_store: ["get_warns", "set_warns", "mark_channel_locked", "mark_channel_unlocked",
                          "get_locked_channels", "get_next_case_id", "log_sanction", "get_case",
                          "create_sanction_appeal", "get_ban_appeal", "get_ban_appeal_by_case",
                          "get_sanction_stats"],
        audit: ["record_audit", "build_sparkline_svg", "get_audit_log"],
        i18n: ["TRANSLATIONS", "t", "current_lang", "current_theme"],
    }.items():
        for name in names:
            assert getattr(nexus, name) is getattr(mod, name), f"main.{name} n'est plus {mod.__name__}.{name}"

    # core.bot doit être branché dès l'import (get_audit_log en dépend)
    assert core.bot is nexus.bot
    # et le moteur Jinja doit utiliser le t() de i18n
    assert nexus.api.jinja_env.globals["t"] is i18n.t


def test_rest_api_blueprint_is_registered_on_the_app(nexus):
    """Étape 2b-1 : la REST API vit dans web/rest_api.py (Blueprint `rest_api`)
    et main.py ne redéfinit plus ses routes."""
    assert "rest_api" in nexus.api.blueprints
    rest_rules = [r for r in nexus.api.url_map.iter_rules() if r.endpoint.startswith("rest_api.")]
    assert len(rest_rules) == 30
    assert {r.rule for r in rest_rules} == {r.rule for r in nexus.api.url_map.iter_rules()
                                            if r.rule.startswith("/api/")}
    for name in ("health", "list_guilds", "guild_stats", "ban_member_route", "guild_faq_toggle_route"):
        assert not hasattr(nexus, name), f"main.{name} aurait dû partir dans web/rest_api.py"
    # Les helpers déplacés restent accessibles sous le même nom dans main.py
    import bridge
    import discord_logging
    import features
    import moderation_actions
    assert nexus.run_coroutine is bridge.run_coroutine
    assert nexus.FEATURE_TOGGLES is features.FEATURE_TOGGLES
    assert nexus._send_log_embed is discord_logging._send_log_embed
    assert nexus.log_dashboard_action is discord_logging.log_dashboard_action
    assert nexus.reverse_sanction is moderation_actions.reverse_sanction


def test_public_and_auth_routes_live_in_web_modules(nexus):
    """Étape 2b-2a : /, /status, kill-switch, appels publics et login/callback/logout
    vivent dans web/public.py et web/auth.py, enregistrés SANS préfixe de Blueprint
    (les templates font url_for('dash_home'), url_for('dash_guild_page')...)."""
    from web import auth, public
    endpoints = {r.endpoint for r in nexus.api.url_map.iter_rules()}
    for name in ("home", "favicon", "public_status_page", "killswitch_confirm_page", "killswitch_submit",
                 "appeal_form_page", "appeal_form_submit", "dash_login", "dash_callback", "dash_logout"):
        assert name in endpoints, f"endpoint {name} disparu (ou renommé avec un préfixe)"
        assert not hasattr(nexus, name) or getattr(nexus, name) is None, f"main.{name} aurait dû partir"
    assert nexus.api.view_functions["dash_login"] is auth.dash_login
    assert nexus.api.view_functions["home"] is public.home
    # Helpers déplacés, toujours accessibles sous le même nom
    import moderation_actions
    import owner_notify
    assert nexus.SANCTION_ICONS is moderation_actions.SANCTION_ICONS
    assert nexus.post_appeal_for_review is moderation_actions.post_appeal_for_review
    assert nexus.get_owner_user is owner_notify.get_owner_user


DASHBOARD_ENDPOINTS = [
    "dash_set_lang", "dash_presence_ping", "dash_presence_who", "dash_features_page", "dash_settings_page",
    "dash_settings_save", "dash_template_marketplace", "dash_template_import", "dash_set_theme", "dash_home",
    "dash_guild_page", "dash_automod", "dash_permission_add", "dash_permission_template",
    "dash_permission_remove", "dash_apikey_regen", "dash_export_audit_csv", "dash_export_audit_pdf",
    "dash_toggle_lock", "dash_cases_page", "dash_appeals_page", "dash_appeal_accept", "dash_appeal_deny",
]


def test_dashboard_pages_live_in_web_dashboard(nexus):
    """Étape 2b-2b : les 23 pages /dashboard/* vivent dans web/dashboard.py, enregistrées avec
    leurs noms d'endpoints d'origine (url_for('dash_guild_page')... dans les templates)."""
    from web import dashboard
    for name in DASHBOARD_ENDPOINTS:
        assert nexus.api.view_functions[name] is getattr(dashboard, name), name
        assert not hasattr(nexus, name), f"main.{name} aurait dû partir dans web/dashboard.py"
    import audit_export
    import command_catalog
    import template_ops
    # Données / helpers partagés avec les slash commands, ré-importés sous le même nom
    assert nexus.MODERATION_COMMANDS is command_catalog.MODERATION_COMMANDS
    assert nexus.COMMAND_TEMPLATES is command_catalog.COMMAND_TEMPLATES
    assert nexus._get_audit_records is audit_export._get_audit_records
    assert nexus._build_audit_csv_bytes is audit_export._build_audit_csv_bytes
    assert nexus._apply_template_base_config is template_ops._apply_template_base_config



def test_slash_command_groups_are_not_nested(nexus):
    """Discord allows one subgroup level, but never a group inside a subgroup."""
    discord = __import__("discord")
    for top_level in nexus.bot.tree.get_commands():
        if isinstance(top_level, discord.app_commands.Group):
            for child in top_level.commands:
                if isinstance(child, discord.app_commands.Group):
                    for grandchild in child.commands:
                        assert not isinstance(grandchild, discord.app_commands.Group), (
                            f"Too many slash-command group levels: /{top_level.name} {child.name} {grandchild.name}"
                        )
    config_group = next(c for c in nexus.bot.tree.get_commands() if c.name == "config")
    ticket_group = next(c for c in nexus.bot.tree.get_commands() if c.name == "ticket")
    ticket_names = {c.name for c in ticket_group.commands}
    assert {"setup", "disable", "panel-create", "panel-list", "panel-remove", "reopen", "assign", "unassign", "priority"} <= ticket_names
    assert not any(c.name == "ticket" for c in config_group.commands), "/ticket must not be nested under /config"
    for group_name in ("template", "rolemenu", "verification", "sticky", "badwords", "domains"):
        assert any(c.name == group_name for c in config_group.commands), f"/config {group_name} missing"


def test_ticket_topic_parsing(nexus):
    assert nexus._ticket_topic_parts("123456789") == ("123456789", None, None, None)
    assert nexus._ticket_topic_parts("nexus-ticket|opener:123456789|panel:abcd1234|support:222|archive:333") == ("123456789", "abcd1234", "222", "333")
    assert nexus._ticket_topic_parts(None) == (None, None, None, None)


def test_ticket_panel_lookup(nexus):
    cfg = {"ticket_panels": [{"id": "abcd1234", "name": "Support"}]}
    assert nexus._find_ticket_panel(cfg, "abcd1234")["name"] == "Support"
    assert nexus._find_ticket_panel(cfg, "missing") is None


def test_ticket_topic_update_preserves_panel_metadata(nexus):
    topic = "nexus-ticket|opener:123|panel:abc|support:222|archive:333"
    updated = nexus._ticket_topic_update(topic, "priority", "urgent")
    assert updated == topic + "|priority:urgent"
    assert nexus._ticket_topic_parts(updated) == ("123", "abc", "222", "333")
    assert "priority:urgent" in updated


def test_ticket_topic_update_replaces_existing_priority(nexus):
    topic = "nexus-ticket|opener:123|priority:low"
    updated = nexus._ticket_topic_update(topic, "priority", "high")
    assert updated == "nexus-ticket|opener:123|priority:high"


def test_ticket_topic_update_migrates_legacy_topic(nexus):
    assert nexus._ticket_topic_update("123456789", "priority", "normal") == (
        "nexus-ticket|opener:123456789|priority:normal"
    )


def test_ticket_topic_value_reads_assignment(nexus):
    topic = "nexus-ticket|opener:123|panel:abc|assignee:456"
    assert nexus._ticket_topic_value(topic, "assignee") == "456"
    assert nexus._ticket_topic_value(topic, "missing") is None



def test_ticket_topic_status_reads_closed_and_open(nexus):
    topic = "nexus-ticket|opener:123|panel:abc|status:closed"
    assert nexus._ticket_topic_value(topic, "status") == "closed"
    assert nexus._ticket_topic_update(topic, "status", "open") == (
        "nexus-ticket|opener:123|panel:abc|status:open"
    )

def test_ticket_reopen_uses_member_guild_permissions(nexus):
    import inspect

    source = inspect.getsource(nexus.config_ticket_reopen.callback)
    assert "interaction.user.guild_permissions.manage_channels" in source
    assert "interaction.guild_permissions.manage_channels" not in source


def test_ticket_assignment_commands_are_registered(nexus):
    ticket_group = next(c for c in nexus.bot.tree.get_commands() if c.name == "ticket")
    assert {"assign", "unassign"} <= {command.name for command in ticket_group.commands}


def test_ticket_priority_command_is_registered(nexus):
    ticket_group = next(c for c in nexus.bot.tree.get_commands() if c.name == "ticket")
    assert "priority" in {command.name for command in ticket_group.commands}



def test_ticket_transcript_escapes_user_content_and_renders_attachments(nexus):
    from types import SimpleNamespace
    from datetime import datetime, timezone

    author = SimpleNamespace(
        display_name='<script>alert("x")</script>',
        display_avatar=SimpleNamespace(url='https://cdn.example/avatar.png?a=1&b=2'),
        bot=False,
    )
    message = SimpleNamespace(
        author=author,
        created_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        edited_at=None,
        content='<img src=x onerror=alert(1)>\nHello & welcome',
        attachments=[
            SimpleNamespace(
                filename='proof <1>.png',
                url='https://cdn.example/proof.png?a=1&b=2',
                content_type='image/png',
            ),
            SimpleNamespace(
                filename='report.pdf',
                url='https://cdn.example/report.pdf',
                content_type='application/pdf',
            ),
        ],
        embeds=[],
    )

    rendered = nexus._render_ticket_transcript('support<&', [message])
    assert '<script>' not in rendered
    assert '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;' in rendered
    assert '&lt;img src=x onerror=alert(1)&gt;' in rendered
    assert 'support&lt;&amp;' in rendered
    assert 'proof &lt;1&gt;.png' in rendered
    assert 'report.pdf' in rendered
    assert 'image-attachment' in rendered
    assert 'rel="noopener noreferrer"' in rendered



def test_ticket_panel_questions_command_is_registered(nexus):
    ticket_group = next(c for c in nexus.bot.tree.get_commands() if c.name == "ticket")
    assert "panel-questions" in {command.name for command in ticket_group.commands}


def test_ticket_modal_supports_five_custom_questions(nexus):
    questions = ["Reason", "Platform", "Issue", "Steps", "Extra details"]
    modal = nexus.TicketOpenModal(panel_config={"form_questions": questions})
    text_inputs = [item for item in modal.children if isinstance(item, __import__("discord").ui.TextInput)]
    assert len(text_inputs) == 5
    assert [item.label for item in text_inputs] == questions
    assert all(item.required for item in text_inputs)
