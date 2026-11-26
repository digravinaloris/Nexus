"""Tests de caractérisation : pages /dashboard/* (HTML) du site web.

Écrits AVANT l'extraction vers web/dashboard.py : ils décrivent le comportement
existant (statuts, redirections, effets en base, entrées d'audit, messages flash)
et doivent passer à l'identique après. Session de dashboard factice (cookie Flask),
`requests` mocké pour la liste des serveurs Discord, mongomock, faux Discord.
"""
import datetime
import time
import types

import pytest
import requests as requests_module

from conftest import FakeChannel, FakeGuild, FakeRole

GID = "123456789012345678"  # snowflake plausible (le dashboard exige 17-19 chiffres)
OWNER_ID = "42"


class PermRole(FakeRole):
    def __init__(self, rid, name, admin=False, default=False):
        super().__init__(rid, name, default=default)
        self.permissions = types.SimpleNamespace(
            administrator=admin, manage_guild=False, ban_members=False,
            kick_members=False, manage_roles=False)


class DashGuild(FakeGuild):
    def __init__(self):
        super().__init__(int(GID), "Test Guild")
        self.owner_id = int(OWNER_ID)
        self.roles = [PermRole(int(GID), "@everyone", default=True), PermRole(9, "Mods"),
                      PermRole(10, "Admins", admin=True)]
        self.text_channels = [FakeChannel(55, "general"), FakeChannel(56, "logs")]
        self.voice_channels = [FakeChannel(66, "voice")]
        self.unbanned = []

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def unban(self, obj, reason=None):
        self.unbanned.append(obj.id)


@pytest.fixture
def dash(env, monkeypatch):
    env.guild = DashGuild()
    env.guilds.clear()
    env.guilds[int(GID)] = env.guild
    env.dm = []

    class User:
        async def send(self, text):
            env.dm.append(text)

    async def fetch_user(uid):
        return User()

    monkeypatch.setattr(env.nexus.bot, "fetch_user", fetch_user)
    monkeypatch.setattr(env.nexus.bot, "start_time", time.time(), raising=False)

    def fake_get(url, **kw):
        class R:
            status_code = 200

            def json(self_inner):
                return env.discord_guilds
        return R()

    env.discord_guilds = [{"id": GID, "name": "Managed Server", "permissions": "8"}]
    monkeypatch.setattr(requests_module, "get", fake_get)

    def login(user_id=OWNER_ID, name="bob"):
        with env.client.session_transaction() as sess:
            sess.clear()
            sess["access_token"] = "AT"
            sess["token_expires_at"] = 4102444800
            sess["dash_user"] = {"id": user_id, "username": name}
            sess["csrf_token"] = "csrf-test-token-012345678901234567890123456789"

    env.login = login
    env.url = lambda path="": f"/dashboard/{GID}{path}"
    env.cfg = lambda: __import__("config_store").get_config(GID)
    env.audit = lambda: [d["action"] for d in env.col.audit.find({"guild_id": GID})]
    # Existing tests simulate valid browser form submissions.
    original_post = env.client.post
    def post_with_csrf(*args, **kwargs):
        data = kwargs.get("data")
        if data is None:
            data = {}
        if isinstance(data, dict) and "csrf_token" not in data:
            data = dict(data)
            with env.client.session_transaction() as sess:
                data["csrf_token"] = sess.get("csrf_token", "")
            kwargs["data"] = data
        return original_post(*args, **kwargs)
    env.client.post = post_with_csrf
    env.raw_post = original_post
    login()
    return env


def flashes(client):
    with client.session_transaction() as sess:
        return list(sess.get("_flashes", []))


# ------------------------------------------------------------ langue / thème

def test_lang_and_theme_switch(env):
    c = env.client
    with c.session_transaction() as sess:
        sess["csrf_token"] = "csrf-test-token-012345678901234567890123456789"
    resp = c.post("/dashboard/lang/fr?next=/status", data={"csrf_token": "csrf-test-token-012345678901234567890123456789"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/status")
    with c.session_transaction() as sess:
        assert sess["lang"] == "fr"
    c.post("/dashboard/lang/zz", data={"csrf_token": "csrf-test-token-012345678901234567890123456789"})  # langue inconnue : ignorée
    with c.session_transaction() as sess:
        assert sess["lang"] == "fr"
    resp = c.post("/dashboard/theme/light", data={"csrf_token": "csrf-test-token-012345678901234567890123456789"})
    assert resp.headers["Location"].endswith("/dashboard")  # sans ?next -> dashboard
    c.post("/dashboard/theme/pink", data={"csrf_token": "csrf-test-token-012345678901234567890123456789"})
    with c.session_transaction() as sess:
        assert sess["theme"] == "light"


def test_dashboard_mutations_reject_weak_session_csrf_token(dash):
    with dash.client.session_transaction() as sess:
        sess["csrf_token"] = "short-token"
    response = dash.raw_post(dash.url("/features"), data={
        "feat__antinuke": "on", "csrf_token": "short-token"
    })
    assert response.status_code == 400


def test_dashboard_mutations_reject_missing_or_invalid_csrf_tokens(dash):
    before = dash.cfg().copy()
    missing = dash.raw_post(dash.url("/features"), data={"feat__antinuke": "on"})
    invalid = dash.raw_post(dash.url("/features"), data={
        "feat__antinuke": "on", "csrf_token": "attacker-controlled-token"
    })
    assert missing.status_code == 400
    assert invalid.status_code == 400
    assert dash.cfg() == before


# ------------------------------------------------------------- liste serveurs

def test_dashboard_home_lists_only_manageable_guilds(dash):
    dash.discord_guilds.append({"id": "999999999999999999", "name": "Not Mine", "permissions": "8"})
    dash.discord_guilds.append({"id": "888888888888888888", "name": "No Admin", "permissions": "4"})
    text = dash.client.get("/dashboard").get_data(as_text=True)
    assert "Managed Server" in text
    assert "Not Mine" not in text and "No Admin" not in text  # bot absent / pas admin


def test_dashboard_home_requires_login(env):
    resp = env.client.get("/dashboard")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


# ------------------------------------------------------------------ présence

def test_presence_ping_and_who_with_ttl(dash, monkeypatch):
    assert dash.client.get(dash.url("/presence/who")).get_json() == {"viewers": []}
    assert dash.client.post(dash.url("/presence/ping")).get_json() == {"ok": True}
    assert dash.client.get(dash.url("/presence/who")).get_json() == {"viewers": ["bob"]}
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 30)  # > 25 s de TTL
    assert dash.client.get(dash.url("/presence/who")).get_json() == {"viewers": []}


# ------------------------------------------------------------ page principale

def test_guild_page_renders_and_audit_is_owner_only(dash):
    from audit import record_audit
    record_audit(GID, "1", "Admin", "ZetaAction", "d")
    resp = dash.client.get(dash.url())
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Test Guild" in text and "ZetaAction" in text
    dash.login(user_id="777", name="notowner")  # admin mais pas propriétaire
    text = dash.client.get(dash.url()).get_data(as_text=True)
    assert "Test Guild" in text and "ZetaAction" not in text


def test_guild_page_applies_server_language_when_session_has_none(dash):
    from config_store import update_config
    update_config(GID, "language", "fr")
    dash.client.get(dash.url())
    with dash.client.session_transaction() as sess:
        assert sess["lang"] == "fr"


def test_guild_page_post_updates_general_config(dash):
    resp = dash.client.post(dash.url(), data={
        "logs_channel": "56", "autorole": "9", "log_dashboard_actions": "on"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/dashboard/{GID}")
    cfg = dash.cfg()
    assert cfg["logs_channel"] == "logs" and cfg["autorole"] == 9
    assert cfg["log_dashboard_actions"] is True
    assert "Updated general config" in dash.audit()
    entry = dash.col.audit.find_one({"action": "Updated general config"})
    assert "logs channel → #logs" in entry["details"] and "autorole → @Mods" in entry["details"]
    # valeur invalide (id inconnu) : rien ne change, autorole "none" efface
    dash.client.post(dash.url(), data={"logs_channel": "99999", "autorole": "none"})
    cfg = dash.cfg()
    assert cfg["logs_channel"] == "logs" and cfg["autorole"] is None
    assert cfg["log_dashboard_actions"] is False  # case décochée


def test_guild_page_post_warns_about_risky_autorole(dash):
    dash.client.post(dash.url(), data={"autorole": "10"})
    messages = [m for _, m in flashes(dash.client)]
    assert any("Admins" in m and "sensitive permissions" in m for m in messages)
    assert dash.cfg()["autorole"] == 10


def test_automod_exempt_roles_are_filtered_to_real_roles(dash):
    resp = dash.client.post(dash.url("/automod"), data={"allowed_roles": ["9", "424242", "abc"]})
    assert resp.status_code == 302
    assert dash.cfg()["allowed_roles"] == [9]
    assert "Updated anti-raid exemptions" in dash.audit()


# ----------------------------------------------------------------- permissions

def test_permission_routes_require_server_owner(dash):
    dash.login(user_id="777")
    for path, data in (("/permissions/add", {"commands": "ban", "role_id": "9"}),
                       ("/permissions/remove", {"command": "ban", "role_id": "9"}),
                       ("/permissions/template", {"template_id": "x", "role_id": "9"}),
                       ("/apikey/regenerate", {}), ("/lock", {})):
        resp = dash.client.post(dash.url(path), data=data)
        assert resp.status_code == 403 and resp.get_json() == {"error": "Server owner required"}, path
    assert dash.client.get(dash.url("/audit/export.csv")).status_code == 403


def test_permission_add_remove_and_template(dash):
    nexus = dash.nexus
    cmds = list(nexus.MODERATION_COMMANDS)[:2]
    resp = dash.client.post(dash.url("/permissions/add"), data={
        "commands": [cmds[0], cmds[1], "bogus"], "role_id": "9"})
    assert resp.status_code == 302
    roles = dash.cfg()["command_roles"]
    assert roles[cmds[0]] == [9] and roles[cmds[1]] == [9] and "bogus" not in roles
    assert "Added command permissions" in dash.audit()
    # role inconnu -> aucune écriture
    dash.client.post(dash.url("/permissions/add"), data={"commands": cmds[0], "role_id": "31337"})
    assert dash.cfg()["command_roles"][cmds[0]] == [9]
    # retrait
    dash.client.post(dash.url("/permissions/remove"), data={"command": cmds[0], "role_id": "9"})
    assert dash.cfg()["command_roles"][cmds[0]] == []
    assert "Removed command permission" in dash.audit()
    # template intégré
    key = next(iter(nexus.COMMAND_TEMPLATES))
    dash.client.post(dash.url("/permissions/template"), data={"template_id": f"builtin:{key}", "role_id": "10"})
    for cmd in nexus.COMMAND_TEMPLATES[key]["commands"]:
        assert 10 in dash.cfg()["command_roles"][cmd]
    assert "Applied permission template" in dash.audit()
    # template perso
    from config_store import set_command_template
    set_command_template(GID, "mine", ["zzz_custom_cmd"])
    dash.client.post(dash.url("/permissions/template"), data={"template_id": "custom:mine", "role_id": "9"})
    assert dash.cfg()["command_roles"]["zzz_custom_cmd"] == [9]
    # role invalide -> redirection sans effet
    resp = dash.client.post(dash.url("/permissions/template"), data={"template_id": "custom:mine", "role_id": "x"})
    assert resp.status_code == 302


def test_apikey_regenerate_and_lock_toggle(dash):
    resp = dash.client.post(dash.url("/apikey/regenerate"))
    assert resp.status_code == 302
    key = dash.cfg()["api_key"]
    assert len(key) == 32 and int(key, 16) >= 0
    assert "Regenerated mobile API key" in dash.audit()
    dash.client.post(dash.url("/apikey/regenerate"))
    assert dash.cfg()["api_key"] != key
    dash.client.post(dash.url("/lock"))
    assert int(GID) in dash.nexus.bot.locked_guilds and "Locked the bot" in dash.audit()
    dash.client.post(dash.url("/lock"))
    assert int(GID) not in dash.nexus.bot.locked_guilds and "Unlocked the bot" in dash.audit()


# -------------------------------------------------------------------- exports

def test_audit_exports(dash):
    from audit import record_audit
    record_audit(GID, "42", "Bob", "Did a thing", "some details")
    resp = dash.client.get(dash.url("/audit/export.csv"))
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    assert resp.headers["Content-Disposition"] == f"attachment; filename=audit_{GID}.csv"
    assert "Did a thing" in resp.get_data(as_text=True)
    resp = dash.client.get(dash.url("/audit/export.pdf"))
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")
    assert resp.headers["Content-Disposition"] == f"attachment; filename=audit_{GID}.pdf"


# ------------------------------------------------------------------- features

def test_features_page_and_toggle_audit(dash):
    resp = dash.client.get(dash.url("/features"))
    assert resp.status_code == 200 and "feat__antinuke" in resp.get_data(as_text=True)
    resp = dash.client.post(dash.url("/features"), data={"feat__antinuke": "on"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/dashboard/{GID}/features")
    cfg = dash.cfg()
    assert cfg["antinuke_enabled"] is True and cfg["remindme_enabled"] is False
    entry = dash.col.audit.find_one({"action": "Updated feature toggles"})
    assert entry["details"] == "enabled: antinuke | disabled: remindme"
    count = len(dash.audit())
    dash.client.post(dash.url("/features"), data={"feat__antinuke": "on"})  # inchangé
    assert len(dash.audit()) == count  # rien à auditer


# ------------------------------------------------------------------- settings

def test_settings_page_renders(dash):
    resp = dash.client.get(dash.url("/settings"))
    assert resp.status_code == 200 and "automod_spam_count" in resp.get_data(as_text=True)


def _post(dash, section, data):
    return dash.client.post(dash.url(f"/settings/{section}"), data=data)


def test_settings_automod_valid_and_invalid(dash):
    resp = _post(dash, "automod", {"automod_spam_count": "20", "automod_caps_ratio": "0.5", "automod_raid_count": ""})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#sec-automod")
    cfg = dash.cfg()
    assert cfg["automod_spam_count"] == 20 and cfg["automod_caps_ratio"] == 0.5
    assert cfg["automod_raid_count"] == 5  # champ vide ignoré (valeur par défaut conservée)
    assert "Updated settings (automod)" in dash.audit()
    _post(dash, "automod", {"automod_spam_count": "abc"})  # non numérique : rejeté, rien n'est écrit
    assert dash.cfg()["automod_spam_count"] == 20
    assert "error-automod" in [c for c, _ in flashes(dash.client)]
    # Hors bornes : la valeur est PLAFONNÉE (clamp) à [min, max], pas rejetée
    _post(dash, "automod", {"automod_spam_count": "5000"})
    assert dash.cfg()["automod_spam_count"] == 1000
    _post(dash, "automod", {"automod_spam_count": "0"})
    assert dash.cfg()["automod_spam_count"] == 1


def test_settings_protection_kick_always_after_mute(dash):
    _post(dash, "protection", {"warn_mute_threshold": "5", "warn_kick_threshold": "3"})
    cfg = dash.cfg()
    assert cfg["warn_mute_threshold"] == 5 and cfg["warn_kick_threshold"] == 6
    assert cfg["warn_escalation_enabled"] is False
    _post(dash, "protection", {"warn_escalation_enabled": "on"})
    assert dash.cfg()["warn_escalation_enabled"] is True


def test_settings_channels_and_roles_are_validated_against_the_server(dash):
    _post(dash, "channels", {"welcome_channel_id": "55", "jtc_channel_id": "66", "boost_channel_id": "none"})
    cfg = dash.cfg()
    assert cfg["welcome_channel_id"] == 55 and cfg["jtc_channel_id"] == 66
    assert cfg["boost_channel_id"] is None
    _post(dash, "channels", {"welcome_channel_id": "99999"})  # salon inexistant : tout est rejeté
    assert dash.cfg()["welcome_channel_id"] == 55
    _post(dash, "channels", {"welcome_channel_id": "66"})  # un salon vocal n'est pas un salon texte
    assert dash.cfg()["welcome_channel_id"] == 55
    _post(dash, "quarantine", {"quarantine_role_id": "9", "quarantine_channel_id": "56",
                                "quarantine_hold_minutes": "45"})
    cfg = dash.cfg()
    assert cfg["quarantine_role_id"] == 9 and cfg["quarantine_channel_id"] == 56
    assert cfg["quarantine_hold_minutes"] == 45
    _post(dash, "quarantine", {"quarantine_role_id": GID})  # @everyone refusé
    assert dash.cfg()["quarantine_role_id"] == 9
    _post(dash, "verification", {"verified_role_id": "9", "unverified_role_id": "9"})  # même rôle : refusé
    assert dash.cfg().get("verified_role_id") is None
    _post(dash, "verification", {"verified_role_id": "9", "unverified_role_id": "10"})
    assert dash.cfg()["verified_role_id"] == 9 and dash.cfg()["unverified_role_id"] == 10


def test_settings_lists_and_language(dash):
    _post(dash, "lists", {"banned_words": "Foo\n\nbar\nFOO", "banned_domains": "Example.COM"})
    cfg = dash.cfg()
    assert len(cfg["banned_words"]) >= 2 and all(w == w.lower() for w in cfg["banned_words"])
    assert cfg["banned_domains"] == ["example.com"]
    _post(dash, "language", {"language": "fr"})
    assert dash.cfg()["language"] == "fr"
    _post(dash, "language", {"language": "xx"})
    assert dash.cfg()["language"] == "fr"
    dash.login(user_id="777")
    resp = _post(dash, "language", {"language": "en"})
    assert resp.status_code == 403 and resp.get_json() == {"error": "Server owner required"}
    resp = _post(dash, "nope", {})
    assert resp.status_code == 404 and resp.get_json() == {"error": "Unknown settings section"}


# -------------------------------------------------------------------- templates

def _template(dash, code="abc12345", public=True):
    dash.col.templates.insert_one({
        "code": code, "name": "My preset", "public": public, "created_guild_name": "Origin",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "snapshot": {"command_roles_by_name": {"ban": ["Mods"], "kick": ["Mods", "Ghost"]},
                     "feature_toggles": {"antinuke": True}, "automod": {"automod_spam_count": 7, "automod_raid_count": None},
                     "warn_escalation_enabled": True}})


def test_template_marketplace_lists_public_only(dash):
    _template(dash)
    dash.col.templates.insert_one({"code": "priv0000", "name": "SecretPreset", "public": False,
                                   "created_guild_name": "X", "created_at": datetime.datetime.now(datetime.timezone.utc),
                                   "snapshot": {}})
    text = dash.client.get(dash.url("/templates")).get_data(as_text=True)
    assert "My preset" in text and "SecretPreset" not in text


def test_template_import_unknown_get_and_apply(dash):
    resp = dash.client.get(dash.url("/templates/import/nope"))
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/dashboard/{GID}/templates")
    _template(dash)
    resp = dash.client.get(dash.url("/templates/import/abc12345"))
    assert resp.status_code == 200 and "My preset" in resp.get_data(as_text=True)
    resp = dash.client.post(dash.url("/templates/import/abc12345"), data={"map__0": "10", "map__1": "9"})
    # noms triés : ["Ghost", "Mods"] -> Ghost -> 10, Mods -> 9
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/dashboard/{GID}")
    cfg = dash.cfg()
    assert cfg["command_roles"]["ban"] == [9] and cfg["command_roles"]["kick"] == [9, 10] or \
        sorted(cfg["command_roles"]["kick"]) == [9, 10]
    assert cfg["antinuke_enabled"] is True and cfg["automod_spam_count"] == 7
    assert cfg["warn_escalation_enabled"] is True
    assert cfg.get("automod_raid_count") is None  # valeur None du snapshot ignorée (jamais écrite)
    assert any(a.startswith("Imported template 'My preset' from Origin") for a in dash.audit())


# ------------------------------------------------------------- cases / appeals

def test_cases_page_filters_by_type(dash):
    from sanctions_store import log_sanction
    log_sanction(GID, 10, "ban", "reason-ban-xyz", "mobile_app")
    log_sanction(GID, 11, "warn", "reason-warn-xyz", "automod")
    text = dash.client.get(dash.url("/cases")).get_data(as_text=True)
    assert "reason-ban-xyz" in text and "reason-warn-xyz" in text
    text = dash.client.get(dash.url("/cases?type=warn")).get_data(as_text=True)
    assert "reason-warn-xyz" in text and "reason-ban-xyz" not in text


def _submitted(dash, token, sanction="ban"):
    from sanctions_store import create_sanction_appeal
    create_sanction_appeal(GID, 7, sanction, 42, "Bob", "why-banned", token)
    dash.col.ban_appeals.update_one({"token": token}, {"$set": {
        "status": "submitted", "appeal_text": "appeal-body-xyz",
        "submitted_at": datetime.datetime.now(datetime.timezone.utc)}})


def test_appeals_review_accept_and_deny(dash):
    _submitted(dash, "t1")
    _submitted(dash, "t2", sanction="warn")
    text = dash.client.get(dash.url("/appeals")).get_data(as_text=True)
    assert "appeal-body-xyz" in text
    resp = dash.client.post(dash.url("/appeals/t1/accept"))
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/dashboard/{GID}/appeals")
    assert dash.col.ban_appeals.find_one({"token": "t1"})["status"] == "accepted"
    assert dash.guild.unbanned == [42]
    assert "Accepted appeal (via dashboard)" in dash.audit()
    assert any("accepted" in m for m in dash.dm)
    dash.client.post(dash.url("/appeals/t2/deny"))
    assert dash.col.ban_appeals.find_one({"token": "t2"})["status"] == "denied"
    assert "Denied appeal (via dashboard)" in dash.audit()
    assert any("denied" in m for m in dash.dm)
    # déjà résolu -> flash "plus disponible", aucun changement
    resp = dash.client.post(dash.url("/appeals/t1/accept"))
    assert resp.status_code == 302
    assert any("no longer available" in m for _, m in flashes(dash.client))


def test_dashboard_failed_appeal_reversal_releases_claim(dash, monkeypatch):
    import web.dashboard as dashboard_module

    _submitted(dash, "failed-accept")

    class FailedAppealService:
        def api_accept_appeal(self, *_args):
            return False, "Discord temporarily unavailable"

    monkeypatch.setattr(dashboard_module, "get_bot_service", lambda: FailedAppealService())
    response = dash.client.post(dash.url("/appeals/failed-accept/accept"))
    assert response.status_code == 500

    appeal = dash.col.ban_appeals.find_one({"token": "failed-accept"})
    assert appeal["status"] == "submitted"
    assert "processing_at" not in appeal


@pytest.mark.parametrize("action", ["accept", "deny"])
def test_dashboard_appeal_claim_prevents_concurrent_resolution(dash, monkeypatch, action):
    import web.dashboard as dashboard_module

    token = f"race-{action}"
    _submitted(dash, token)
    stale_appeal = dict(dash.col.ban_appeals.find_one({"token": token}))
    dash.col.ban_appeals.update_one(
        {"token": token}, {"$set": {"status": "processing"}}
    )
    monkeypatch.setattr(
        dashboard_module, "get_ban_appeal",
        lambda _token: {**stale_appeal, "status": "submitted"},
    )
    side_effects = []
    monkeypatch.setattr(
        dashboard_module, "run_coroutine",
        lambda coroutine: side_effects.append(coroutine) or "unexpected",
    )

    response = dash.client.post(dash.url(f"/appeals/{token}/{action}"))
    assert response.status_code == 302
    assert dash.col.ban_appeals.find_one({"token": token})["status"] == "processing"
    assert side_effects == []


@pytest.mark.parametrize("session_token, supplied_token", [
    ("csrf-test-token-012345678901234567890123456789", "é" * 40),
    ("é" * 40, "é" * 40),
    ("csrf-test-token-012345678901234567890123456789", "x" * 257),
])
def test_dashboard_mutations_reject_malformed_csrf_strings_without_500(dash, session_token, supplied_token):
    with dash.client.session_transaction() as sess:
        sess["csrf_token"] = session_token

    response = dash.client.post(
        dash.url("/lang/fr"),
        data={"csrf_token": supplied_token},
    )
    assert response.status_code == 400



def test_dashboard_home_uses_bot_service_for_available_guild_ids(dash, monkeypatch):
    from web import dashboard

    dash.discord_guilds.append({
        "id": "999999999999999999", "name": "Not In Bot", "permissions": "8"
    })

    class FakeBotService:
        def available_guild_ids(self, candidate_ids):
            assert candidate_ids == [GID, "999999999999999999"]
            return [GID]

    monkeypatch.setattr(dashboard, "get_bot_service", lambda: FakeBotService())
    response = dash.client.get("/dashboard")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Managed Server" in body
    assert "Not In Bot" not in body



@pytest.mark.parametrize("is_owner", [True, False])
def test_dashboard_owner_guard_uses_bot_service(dash, monkeypatch, is_owner):
    from flask import session
    from web import dashboard

    class FakeBotService:
        def is_guild_owner(self, guild_id, user_id):
            assert guild_id == GID
            assert user_id == OWNER_ID
            return is_owner

    monkeypatch.setattr(dashboard, "get_bot_service", lambda: FakeBotService())

    @dashboard.dash_owner_required
    def protected(guild_id):
        return "allowed"

    with dash.nexus.api.test_request_context(dash.url()):
        session["dash_user"] = {"id": OWNER_ID}
        result = protected(GID)
        if is_owner:
            assert result == "allowed"
        else:
            response, status = result
            assert status == 403
            assert response.get_json() == {"error": "Server owner required"}


def test_read_only_dashboard_pages_use_guild_name_service(dash, monkeypatch):
    import web.dashboard as dashboard_module

    class GuildNameService:
        def guild_name(self, guild_id):
            assert guild_id == GID
            return "Service Guild Name"

    monkeypatch.setattr(dashboard_module, "get_bot_service", lambda: GuildNameService())
    cases = dash.client.get(dash.url("/cases"))
    appeals = dash.client.get(dash.url("/appeals"))
    assert cases.status_code == 200 and "Service Guild Name" in cases.get_data(as_text=True)
    assert appeals.status_code == 200 and "Service Guild Name" in appeals.get_data(as_text=True)


def test_feature_and_template_marketplace_pages_use_guild_name_service(dash, monkeypatch):
    import web.dashboard as dashboard_module

    class GuildNameService:
        def guild_name(self, guild_id):
            assert guild_id == GID
            return "Service Guild Name"

    monkeypatch.setattr(dashboard_module, "get_bot_service", lambda: GuildNameService())
    features = dash.client.get(dash.url("/features"))
    marketplace = dash.client.get(dash.url("/templates"))
    assert features.status_code == 200 and "Service Guild Name" in features.get_data(as_text=True)
    assert marketplace.status_code == 200 and "Service Guild Name" in marketplace.get_data(as_text=True)
