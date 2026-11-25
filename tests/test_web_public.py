"""Tests de caractérisation : routes publiques et authentification du site web.

/ (statut JSON), /favicon.ico, /status, login / callback / logout (OAuth Discord,
`requests` mocké), kill-switch par e-mail + TOTP, formulaire public d'appel.
Écrits AVANT l'extraction vers web/public.py et web/auth.py : ils décrivent le
comportement existant et doivent passer à l'identique après.
"""
import datetime
import os
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
import requests as requests_module

from conftest import GUILD, FakeChannel

# Endpoint Flask -> (règle, méthodes) de TOUTES les routes non-REST. Les templates
# font url_for('dash_guild_page'...) : ces noms ne doivent jamais changer.
GOLDEN_ENDPOINTS = [
    ('appeal_form_page', '/appeal/<token>', ('GET',)),
    ('appeal_form_submit', '/appeal/<token>', ('POST',)),
    ('dash_apikey_regen', '/dashboard/<guild_id>/apikey/regenerate', ('POST',)),
    ('dash_appeal_accept', '/dashboard/<guild_id>/appeals/<token>/accept', ('POST',)),
    ('dash_appeal_deny', '/dashboard/<guild_id>/appeals/<token>/deny', ('POST',)),
    ('dash_appeals_page', '/dashboard/<guild_id>/appeals', ('GET',)),
    ('dash_automod', '/dashboard/<guild_id>/automod', ('POST',)),
    ('dash_callback', '/callback', ('GET',)),
    ('dash_cases_page', '/dashboard/<guild_id>/cases', ('GET',)),
    ('dash_calendar_page', '/dashboard/<guild_id>/calendar', ('GET', 'POST')),
    ('dash_export_audit_csv', '/dashboard/<guild_id>/audit/export.csv', ('GET',)),
    ('dash_export_audit_pdf', '/dashboard/<guild_id>/audit/export.pdf', ('GET',)),
    ('dash_features_page', '/dashboard/<guild_id>/features', ('GET', 'POST')),
    ('dash_guild_page', '/dashboard/<guild_id>', ('GET', 'POST')),
    ('dash_home', '/dashboard', ('GET',)),
    ('dash_login', '/login', ('GET',)),
    ('dash_logout', '/logout', ('POST',)),
    ('dash_permission_add', '/dashboard/<guild_id>/permissions/add', ('POST',)),
    ('dash_permission_remove', '/dashboard/<guild_id>/permissions/remove', ('POST',)),
    ('dash_permission_template', '/dashboard/<guild_id>/permissions/template', ('POST',)),
    ('dash_presence_ping', '/dashboard/<guild_id>/presence/ping', ('POST',)),
    ('dash_presence_who', '/dashboard/<guild_id>/presence/who', ('GET',)),
    ('dash_set_lang', '/dashboard/lang/<lang_code>', ('GET',)),
    ('dash_set_theme', '/dashboard/theme/<theme_name>', ('GET',)),
    ('dash_settings_page', '/dashboard/<guild_id>/settings', ('GET',)),
    ('dash_settings_save', '/dashboard/<guild_id>/settings/<section>', ('POST',)),
    ('dash_template_import', '/dashboard/<guild_id>/templates/import/<code>', ('GET', 'POST')),
    ('dash_template_marketplace', '/dashboard/<guild_id>/templates', ('GET',)),
    ('dash_toggle_lock', '/dashboard/<guild_id>/lock', ('POST',)),
    ('favicon', '/favicon.ico', ('GET',)),
    ('home', '/', ('GET',)),
    ('killswitch_confirm_page', '/admin/kill-switch/<token>', ('GET',)),
    ('killswitch_submit', '/admin/kill-switch/<token>', ('POST',)),
    ('public_status_page', '/status', ('GET',)),
    ('verify_page', '/verify/<token>', ('GET',)),
    ('verify_submit', '/verify/<token>', ('POST',)),
]


def test_web_endpoint_names_and_rules_are_stable(nexus):
    rows = sorted(
        (r.endpoint, r.rule, tuple(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))))
        for r in nexus.api.url_map.iter_rules()
        if not r.endpoint.startswith("rest_api.") and r.endpoint != "static"
    )
    assert rows == sorted(GOLDEN_ENDPOINTS)


# -------------------------------------------------------------------- home

def test_home_without_mongo_is_still_200(nexus, monkeypatch):
    import core
    core.reset()
    monkeypatch.setattr(nexus, "mongo", None)
    resp = nexus.api.test_client().get("/")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "alive" and body["mongodb"] == "unreachable"
    assert body["discord_gateway"] == "disconnected" and body["guild_count"] == 0


def test_home_reports_components(env):
    body = env.client.get("/").get_json()
    assert body == {"status": "alive", "discord_gateway": "connected",
                    "mongodb": "connected", "guild_count": 1}
    env.ready = False
    body = env.client.get("/").get_json()
    assert body["discord_gateway"] == "disconnected" and body["guild_count"] == 0


def test_favicon_is_served_from_repo_root(env):
    resp = env.client.get("/favicon.ico")
    assert resp.status_code == 200 and resp.mimetype == "image/png"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert len(resp.data) == os.path.getsize(os.path.join(root, "Nexus.png"))
    resp.close()


# ------------------------------------------------------------------ status

def test_status_page_offline_and_online(env, monkeypatch):
    env.ready = False
    resp = env.client.get("/status")
    assert resp.status_code == 503 and "Bot is offline" in resp.get_data(as_text=True)
    env.ready = True
    monkeypatch.setattr(env.nexus.bot, "start_time", __import__("time").time() - 3700, raising=False)
    resp = env.client.get("/status")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "All systems operational" in text
    assert "1h" in text  # uptime formaté (3700 s)


# ------------------------------------------------------------------- login

def test_login_redirects_to_discord_with_state(env):
    resp = env.client.get("/login")
    assert resp.status_code == 302
    loc = urlparse(resp.headers["Location"])
    assert loc.netloc == "discord.com" and loc.path == "/api/oauth2/authorize"
    q = parse_qs(loc.query)
    assert q["scope"] == ["identify guilds"] and q["response_type"] == ["code"]
    with env.client.session_transaction() as sess:
        assert sess["oauth_state"] == q["state"][0] and len(q["state"][0]) >= 32


def _fake_requests(monkeypatch, token_status=200, guilds=None, user=None):
    calls = []

    class Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    def post(url, **kw):
        calls.append(("POST", url))
        return Resp(token_status, {"access_token": "AT", "refresh_token": "RT"})

    def get(url, **kw):
        calls.append(("GET", url))
        if url.endswith("/users/@me/guilds"):
            return Resp(200, guilds if guilds is not None else [])
        return Resp(200, user or {"id": "42", "username": "bob"})

    monkeypatch.setattr(requests_module, "post", post)
    monkeypatch.setattr(requests_module, "get", get)
    return calls


def test_callback_rejects_bad_state_and_missing_code(env, monkeypatch):
    _fake_requests(monkeypatch)
    resp = env.client.get("/callback?state=x&code=c")
    assert resp.status_code == 403 and resp.get_json() == {"error": "Invalid state"}
    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "good"
    resp = env.client.get("/callback?state=good")
    assert resp.status_code == 400 and resp.get_json() == {"error": "Missing code"}


def test_callback_token_exchange_failure(env, monkeypatch):
    _fake_requests(monkeypatch, token_status=400)
    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "s"
    resp = env.client.get("/callback?state=s&code=c")
    assert resp.status_code == 400 and resp.get_json() == {"error": "Token exchange failed"}


def test_callback_success_stores_session_and_redirects(env, monkeypatch):
    calls = _fake_requests(monkeypatch, user={"id": "42", "username": "bob"})
    with env.client.session_transaction() as sess:
        sess["oauth_state"] = "s"
    resp = env.client.get("/callback?state=s&code=c")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard")
    with env.client.session_transaction() as sess:
        assert sess["access_token"] == "AT" and sess["refresh_token"] == "RT"
        assert sess["dash_user"] == {"id": "42", "username": "bob"}
        assert "oauth_state" not in sess  # état consommé (usage unique)
    assert [c[0] for c in calls] == ["POST", "GET"]


def test_logout_clears_session(env):
    with env.client.session_transaction() as sess:
        sess["access_token"] = "AT"
        sess["csrf_token"] = "test-token"
    resp = env.client.post("/logout", data={"csrf_token": "test-token"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess


# --------------------------------------- garde-fous du dashboard (auth.py)

def test_dashboard_guards(env, monkeypatch):
    guild = f"{GUILD}"
    page = f"/dashboard/{guild}/cases"
    # pas connecté -> redirigé vers /login
    resp = env.client.get(page)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    with env.client.session_transaction() as sess:
        sess["access_token"] = "AT"
        sess["dash_user"] = {"id": "42", "username": "bob"}
    # identifiant de serveur invalide -> 400
    assert env.client.get("/dashboard/12/cases").status_code == 400
    # connecté mais pas admin de ce serveur -> 403
    _fake_requests(monkeypatch, guilds=[{"id": "999999999999999999", "permissions": "8"}])
    resp = env.client.get(f"/dashboard/{'1' * 18}/cases")
    assert resp.status_code == 403 and resp.get_json() == {"error": "Forbidden"}


def test_dashboard_guard_admin_but_bot_absent_or_not_ready(env, monkeypatch):
    gid = str(GUILD).ljust(18, "0")  # snowflake plausible : 18 chiffres
    env.guilds.clear()
    env.guilds[int(gid)] = env.guild
    _fake_requests(monkeypatch, guilds=[{"id": gid, "permissions": "8"}])
    with env.client.session_transaction() as sess:
        sess["access_token"] = "AT"
    env.ready = False
    resp = env.client.get(f"/dashboard/{gid}/cases")
    assert resp.status_code == 403 and resp.get_json() == {"error": "Bot not present on this server"}
    env.ready = True
    env.guilds.clear()
    resp = env.client.get(f"/dashboard/{gid}/cases")
    assert resp.status_code == 403
    # non-admin (permission 0x8 absente) -> 403 Forbidden
    _fake_requests(monkeypatch, guilds=[{"id": gid, "permissions": "4"}])
    with env.client.session_transaction() as sess:
        sess.pop("admin_guilds_cache", None)
        sess.pop("admin_guilds_cache_at", None)
    assert env.client.get(f"/dashboard/{gid}/cases").get_json() == {"error": "Forbidden"}


def test_dashboard_guard_discord_401_forces_new_login(env, monkeypatch):
    class R:
        status_code = 401

    monkeypatch.setattr(requests_module, "get", lambda *a, **k: R())
    with env.client.session_transaction() as sess:
        sess["access_token"] = "AT"
    resp = env.client.get(f"/dashboard/{'1' * 18}/cases")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    with env.client.session_transaction() as sess:
        assert "access_token" not in sess  # session purgée


def test_dashboard_page_renders_for_admin(env, monkeypatch):
    gid = "1" * 18
    env.guilds.clear()
    env.guilds[int(gid)] = env.guild
    _fake_requests(monkeypatch, guilds=[{"id": gid, "permissions": "8"}])
    with env.client.session_transaction() as sess:
        sess["access_token"] = "AT"
        sess["dash_user"] = {"id": "42", "username": "bob"}
    from sanctions_store import log_sanction
    log_sanction(int(gid), 10, "ban", "spam-reason-xyz", "mobile_app")
    resp = env.client.get(f"/dashboard/{gid}/cases")
    assert resp.status_code == 200
    assert "spam-reason-xyz" in resp.get_data(as_text=True)


# -------------------------------------------------------------- kill-switch

def _token(env, token="tok", used=False, attempts=0, expires_in=600):
    env.col.killswitch_tokens.insert_one({
        "token": token, "used": used, "attempts": attempts,
        "expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)})


def test_killswitch_invalid_expired_used_or_locked_out_tokens_are_410(env):
    assert env.client.get("/admin/kill-switch/nope").status_code == 410
    _token(env, "expired", expires_in=-5)
    _token(env, "used", used=True)
    _token(env, "five", attempts=5)
    for t in ("expired", "used", "five"):
        assert env.client.get(f"/admin/kill-switch/{t}").status_code == 410
        assert env.client.post(f"/admin/kill-switch/{t}", data={"code": "1"}).status_code == 410
    actions = [d["action"] for d in env.col.audit.find({"guild_id": "global"})]
    assert actions.count("Viewed an invalid/expired kill-switch link") == 4


def test_killswitch_confirm_page_has_no_side_effect(env):
    _token(env)
    resp = env.client.get("/admin/kill-switch/tok", headers={"X-Forwarded-For": "1.2.3.4, 9.9.9.9"})
    assert resp.status_code == 200 and "Lock the bot down?" in resp.get_data(as_text=True)
    from config_store import get_global_lock
    assert get_global_lock() is False
    assert env.col.killswitch_tokens.find_one({"token": "tok"})["used"] is False
    doc = env.col.audit.find_one({"guild_id": "global"})
    assert doc["actor_name"] == "Unknown (IP 1.2.3.4)"  # 1re IP de X-Forwarded-For


def test_killswitch_wrong_code_counts_attempts_then_dies(env, monkeypatch):
    monkeypatch.setenv("TOTP_SECRET", pyotp.random_base32())
    _token(env)
    for i in range(4):
        resp = env.client.post("/admin/kill-switch/tok", data={"code": "000000"})
        assert resp.status_code == 200
    assert env.col.killswitch_tokens.find_one({"token": "tok"})["attempts"] == 4
    assert env.owner.sent == []
    env.client.post("/admin/kill-switch/tok", data={"code": "000000"})  # 5e essai
    assert env.col.killswitch_tokens.find_one({"token": "tok"})["attempts"] == 5
    assert len(env.owner.sent) == 1 and "Possible intrusion attempt" in env.owner.sent[0]
    assert env.client.get("/admin/kill-switch/tok").status_code == 410  # lien mort
    from config_store import get_global_lock
    assert get_global_lock() is False


def test_killswitch_no_totp_secret_never_validates(env, monkeypatch):
    monkeypatch.delenv("TOTP_SECRET", raising=False)
    _token(env)
    env.client.post("/admin/kill-switch/tok", data={"code": "123456"})
    from config_store import get_global_lock
    assert get_global_lock() is False
    assert env.col.killswitch_tokens.find_one({"token": "tok"})["attempts"] == 1


def test_killswitch_correct_code_toggles_global_lock_once(env, monkeypatch):
    secret = pyotp.random_base32()
    monkeypatch.setenv("TOTP_SECRET", secret)
    _token(env)
    code = pyotp.TOTP(secret).now()
    resp = env.client.post("/admin/kill-switch/tok", data={"code": code})
    assert resp.status_code == 200 and "Bot locked down" in resp.get_data(as_text=True)
    from config_store import get_global_lock
    assert get_global_lock() is True
    assert env.col.killswitch_tokens.find_one({"token": "tok"})["used"] is True
    assert "locked down" in env.owner.sent[0]
    actions = [d["action"] for d in env.col.audit.find({"guild_id": "global"})]
    assert "Locked bot globally" in actions
    # Usage unique : le même lien ne sert plus
    assert env.client.post("/admin/kill-switch/tok", data={"code": code}).status_code == 410
    # Un 2e lien déverrouille
    _token(env, "tok2")
    env.client.post("/admin/kill-switch/tok2", data={"code": pyotp.TOTP(secret).now()})
    assert get_global_lock() is False and "unlocked" in env.owner.sent[1]


# ------------------------------------------------------------------ appeals

def _appeal(env, status="pending", token="ap", sanction="ban"):
    from sanctions_store import create_sanction_appeal
    create_sanction_appeal(GUILD, 7, sanction, 42, "Bob", "reason-abc", token)
    if status != "pending":
        env.col.ban_appeals.update_one({"token": token}, {"$set": {"status": status}})


def test_appeal_form_states(env):
    assert env.client.get("/appeal/unknown").status_code == 404
    _appeal(env)
    resp = env.client.get("/appeal/ap")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "reason-abc" in text and "Test Guild" in text
    for status, title in (("submitted", "Already submitted"), ("accepted", "Appeal accepted"),
                          ("denied", "Appeal denied")):
        env.col.ban_appeals.update_one({"token": "ap"}, {"$set": {"status": status}})
        resp = env.client.get("/appeal/ap")
        assert resp.status_code == 200 and title in resp.get_data(as_text=True)


def test_appeal_submit_validates_saves_and_posts_for_review(env):
    from config_store import update_config
    channel = FakeChannel(55, "appeals")
    env.guild.text_channels = [channel]
    update_config(GUILD, "appeal_channel_id", "55")
    assert env.client.post("/appeal/unknown", data={"appeal_text": "x"}).status_code == 404
    _appeal(env)
    # Texte vide : on réaffiche le formulaire, rien n'est enregistré
    resp = env.client.post("/appeal/ap", data={"appeal_text": "   "})
    assert resp.status_code == 200 and "reason-abc" in resp.get_data(as_text=True)
    assert env.col.ban_appeals.find_one({"token": "ap"})["status"] == "pending"
    # Texte valide (tronqué à 1000 caractères)
    resp = env.client.post("/appeal/ap", data={"appeal_text": "s" * 1500})
    assert resp.status_code == 200
    doc = env.col.ban_appeals.find_one({"token": "ap"})
    assert doc["status"] == "submitted" and len(doc["appeal_text"]) == 1000
    assert doc["submitted_at"] is not None
    _, embed = channel.sent[0]
    assert "Appeal" in embed.title and "Case #7" in embed.title and "🔨" in embed.title
    # Déjà soumis -> plus de nouvelle soumission
    assert env.client.post("/appeal/ap", data={"appeal_text": "again"}).status_code == 404
    assert len(channel.sent) == 1


def test_appeal_submit_works_without_review_channel(env):
    _appeal(env, sanction="warn", token="w")
    resp = env.client.post("/appeal/w", data={"appeal_text": "please"})
    assert resp.status_code == 200
    assert env.col.ban_appeals.find_one({"token": "w"})["status"] == "submitted"


def test_public_responses_include_baseline_security_headers(env):
    response = env.client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
