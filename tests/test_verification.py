"""Vérification Cloudflare Turnstile : liens personnels, validation serveur, bouton Discord.

Cloudflare est mocké (`requests.post`) ; les clés ci-dessous sont les clés de TEST publiques de
Cloudflare (« toujours passe »), jamais de vraies clés.
"""
import asyncio
import datetime
import os
import sys
import types

import discord
import pytest
import requests as requests_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # `pytest tests/` (comme la CI) n'ajoute pas la racine du repo
    sys.path.insert(0, ROOT)

import verification  # noqa: E402

SITE = "1x00000000000000000000AA"
SECRET = "1x0000000000000000000000000000000AA"
BASE = "https://nexus.example"
GID = 111
UTC = datetime.timezone.utc


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", SITE)
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)


def fake_cloudflare(monkeypatch, result=None, exc=None):
    calls = []

    def post(url, data=None, timeout=None):
        calls.append({"url": url, "data": data, "timeout": timeout})
        if exc:
            raise exc
        return types.SimpleNamespace(json=lambda: result if result is not None else {"success": True})

    monkeypatch.setattr(verification.requests, "post", post)
    return calls


# ------------------------------------------------------------- configuration

def test_configured_needs_both_keys_and_a_public_url(monkeypatch):
    for var in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "PUBLIC_BASE_URL", "DISCORD_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    assert verification.turnstile_configured() is False
    monkeypatch.setenv("TURNSTILE_SITE_KEY", SITE)
    assert verification.turnstile_configured() is False
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", SECRET)
    assert verification.turnstile_configured() is False  # pas d'URL publique
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE + "/")
    assert verification.turnstile_configured() is True
    assert verification.verification_url("abc") == f"{BASE}/verify/abc"


def test_public_url_is_derived_from_the_oauth_redirect_uri(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://nexus.example/callback")
    assert verification.public_base_url() == BASE
    monkeypatch.setenv("DISCORD_REDIRECT_URI", "https://nexus.example/elsewhere")
    assert verification.public_base_url() == ""


# ---------------------------------------------------------------- liens (Mongo)

def test_challenge_lifecycle(env):
    now = datetime.datetime.now(UTC)
    token = verification.create_challenge(GID, 7, now=now)
    assert len(token) >= 40
    doc = verification.get_valid_challenge(token, now=now)
    assert doc["guild_id"] == str(GID) and doc["user_id"] == "7" and doc["attempts"] == 0
    # double-clic < 30 s : même lien
    assert verification.create_challenge(GID, 7, now=now + datetime.timedelta(seconds=10)) == token
    # clic plus tard : nouveau lien, l'ancien meurt (un seul lien actif par membre)
    later = now + datetime.timedelta(seconds=45)
    token2 = verification.create_challenge(GID, 7, now=later)
    assert token2 != token and verification.get_valid_challenge(token, now=later) is None
    assert env.col.verification_challenges.count_documents({"user_id": "7"}) == 1
    # un autre membre a son propre lien
    assert verification.create_challenge(GID, 8, now=later) != token2


def test_challenge_validity_rules(env):
    now = datetime.datetime.now(UTC)
    token = verification.create_challenge(GID, 7, now=now)
    assert verification.get_valid_challenge(token, now=now + datetime.timedelta(minutes=9)) is not None
    assert verification.get_valid_challenge(token, now=now + datetime.timedelta(minutes=10, seconds=1)) is None  # expiré
    assert verification.get_valid_challenge("inconnu") is None
    for _ in range(verification.MAX_ATTEMPTS):
        verification.record_failed_attempt(token)
    assert verification.get_valid_challenge(token, now=now) is None  # trop d'échecs
    other = verification.create_challenge(GID, 9, now=now)
    assert verification.mark_used(other) is True
    assert verification.mark_used(other) is False  # usage unique
    assert verification.get_valid_challenge(other, now=now) is None


def test_expired_challenges_are_cleaned_up(env):
    old = datetime.datetime.now(UTC) - datetime.timedelta(hours=5)
    verification.create_challenge(GID, 1, now=old)
    verification.create_challenge(GID, 2)  # déclenche le ménage
    assert env.col.verification_challenges.count_documents({"user_id": "1"}) == 0


# --------------------------------------------------------------- Cloudflare

def test_check_turnstile_success_sends_secret_response_and_ip(keys, monkeypatch):
    calls = fake_cloudflare(monkeypatch, {"success": True})
    assert verification.check_turnstile("tok", "1.2.3.4") == (True, None)
    assert calls[0]["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert calls[0]["data"] == {"secret": SECRET, "response": "tok", "remoteip": "1.2.3.4"}
    assert calls[0]["timeout"] == 10
    calls.clear()
    verification.check_turnstile("tok", None)
    assert "remoteip" not in calls[0]["data"]


def test_check_turnstile_uses_injected_http_client(keys):
    calls = []

    class InjectedClient:
        def post(self, url, data=None, timeout=None):
            calls.append({"url": url, "data": data, "timeout": timeout})
            return types.SimpleNamespace(json=lambda: {"success": True})

    assert verification.check_turnstile("worker-token", "1.2.3.4", http_client=InjectedClient()) == (True, None)
    assert calls == [{
        "url": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        "data": {"secret": SECRET, "response": "worker-token", "remoteip": "1.2.3.4"},
        "timeout": 10,
    }]


def test_check_turnstile_failure_modes(keys, monkeypatch):
    fake_cloudflare(monkeypatch, {"success": False, "error-codes": ["invalid-input-response", "timeout-or-duplicate"]})
    assert verification.check_turnstile("tok") == (False, "invalid-input-response,timeout-or-duplicate")
    fake_cloudflare(monkeypatch, {"success": "true"})  # pas un vrai booléen : refusé
    assert verification.check_turnstile("tok")[0] is False
    fake_cloudflare(monkeypatch, exc=requests_module.ConnectionError("down"))
    assert verification.check_turnstile("tok") == (False, "service_unavailable")  # échoue FERMÉ
    monkeypatch.delenv("TURNSTILE_SECRET_KEY")
    assert verification.check_turnstile("tok") == (False, "not_configured")


# ------------------------------------------------------------------ faux Discord

class VMember:
    def __init__(self, uid, roles=()):
        self.id = uid
        self.roles = list(roles)
        self.calls = []

    async def add_roles(self, role, reason=None):
        self.roles.append(role)
        self.calls.append(("add", role.id))

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)
        self.calls.append(("remove", role.id))


class VRole:
    def __init__(self, rid):
        self.id = rid
        self.mention = f"<@&{rid}>"


class VGuild:
    def __init__(self):
        self.id = GID
        self.name = "Test Guild"
        self.verified, self.unverified = VRole(900), VRole(901)
        self.autorole = VRole(902)
        self.member = VMember(7, [self.unverified])
        self.fetchable = None

    def get_role(self, rid):
        return {900: self.verified, 901: self.unverified, 902: self.autorole}.get(rid)

    def get_member(self, uid):
        return self.member if uid == 7 else None

    async def fetch_member(self, uid):
        if self.fetchable is not None and uid == self.fetchable.id:
            return self.fetchable
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"), "unknown member")


@pytest.fixture
def vguild(env):
    from config_store import update_config
    guild = VGuild()
    env.guilds.clear()
    env.guilds[GID] = guild
    env.vguild = guild
    update_config(GID, "verified_role_id", 900)
    update_config(GID, "unverified_role_id", 901)
    return env


# ------------------------------------------------------------------- pages web

def _link(env):
    return verification.create_challenge(GID, 7)


def test_page_unavailable_without_configuration(vguild, monkeypatch):
    for var in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "PUBLIC_BASE_URL", "DISCORD_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    assert vguild.client.get("/verify/whatever").status_code == 503
    assert vguild.client.post("/verify/whatever", data={"cf-turnstile-response": "x"}).status_code == 503


def test_page_shows_widget_with_privacy_headers(vguild, keys):
    token = _link(vguild)
    resp = vguild.client.get(f"/verify/{token}")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200 and SITE in text and "Test Guild" in text
    assert "challenges.cloudflare.com/turnstile/v0/api.js" in text and "cf-turnstile" in text
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    assert SECRET not in text  # la clé secrète n'apparaît jamais côté navigateur
    assert vguild.client.get("/verify/does-not-exist").status_code == 410


def test_submit_without_widget_response_does_not_consume_an_attempt(vguild, keys, monkeypatch):
    calls = fake_cloudflare(monkeypatch)
    token = _link(vguild)
    resp = vguild.client.post(f"/verify/{token}", data={})
    assert resp.status_code == 400 and "complete the check" in resp.get_data(as_text=True)
    assert calls == [] and vguild.col.verification_challenges.find_one({"token": token})["attempts"] == 0


def test_three_failures_kill_the_link(vguild, keys, monkeypatch):
    fake_cloudflare(monkeypatch, {"success": False, "error-codes": ["invalid-input-response"]})
    token = _link(vguild)
    codes = [vguild.client.post(f"/verify/{token}", data={"cf-turnstile-response": "bad"}).status_code for _ in range(3)]
    assert codes == [400, 400, 410]
    assert vguild.client.get(f"/verify/{token}").status_code == 410
    assert vguild.vguild.member.calls == []  # personne n'a été vérifié


def test_cloudflare_outage_is_not_the_members_fault(vguild, keys, monkeypatch):
    fake_cloudflare(monkeypatch, exc=requests_module.Timeout("slow"))
    token = _link(vguild)
    resp = vguild.client.post(f"/verify/{token}", data={"cf-turnstile-response": "x"})
    assert resp.status_code == 503 and "unreachable" in resp.get_data(as_text=True)
    assert vguild.col.verification_challenges.find_one({"token": token})["attempts"] == 0
    assert vguild.vguild.member.calls == []  # échoue fermé


def test_optional_autorole_is_added_after_verification(vguild):
    from config_store import update_config
    update_config(GID, "verification_autorole_id", 902)
    result = asyncio.run(verification.grant_verified(vguild.vguild, 7))
    assert result == (True, None)
    assert vguild.vguild.member.calls == [("remove", 901), ("add", 900), ("add", 902)]
    assert vguild.vguild.autorole in vguild.vguild.member.roles


def test_successful_check_grants_roles_audits_and_burns_the_link(vguild, keys, monkeypatch):
    calls = fake_cloudflare(monkeypatch, {"success": True})
    token = _link(vguild)
    resp = vguild.client.post(f"/verify/{token}", data={"cf-turnstile-response": "good"},
                              headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"})
    assert resp.status_code == 200 and "verified ✅" in resp.get_data(as_text=True)
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    guild = vguild.vguild
    assert guild.member.calls == [("remove", 901), ("add", 900)]
    assert guild.verified in guild.member.roles and guild.unverified not in guild.member.roles
    assert calls[0]["data"]["remoteip"] == "9.9.9.9"  # vraie IP du visiteur (derrière le proxy Render)
    doc = vguild.col.audit.find_one({"guild_id": str(GID), "action": "Verified via Cloudflare Turnstile"})
    assert doc["actor_id"] == "7" and doc["actor_name"] == "Member (web verification)"
    # usage unique : le même lien ne sert plus, même avec un token Cloudflare valide
    assert vguild.client.post(f"/verify/{token}", data={"cf-turnstile-response": "good"}).status_code == 410
    assert vguild.client.get(f"/verify/{token}").status_code == 410


def test_role_problems_are_reported_not_hidden(vguild, keys, monkeypatch):
    fake_cloudflare(monkeypatch, {"success": True})
    from config_store import update_config
    update_config(GID, "verified_role_id", 12345)  # rôle supprimé
    resp = vguild.client.post(f"/verify/{_link(vguild)}", data={"cf-turnstile-response": "ok"})
    assert resp.status_code == 500 and "Verified role no longer exists" in resp.get_data(as_text=True)
    assert vguild.vguild.member.calls == []
    # le membre est parti : message clair
    update_config(GID, "verified_role_id", 900)
    vguild.vguild.member = VMember(8)  # get_member(7) renverra None via uid != 7
    vguild.vguild.get_member = lambda uid: None
    resp = vguild.client.post(f"/verify/{verification.create_challenge(GID, 7, now=datetime.datetime.now(UTC) + datetime.timedelta(minutes=1))}",
                              data={"cf-turnstile-response": "ok"})
    assert resp.status_code == 500 and "no longer a member" in resp.get_data(as_text=True)


# ----------------------------------------------------------- bouton / commande

class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, view=None, ephemeral=False):
        self.sent.append({"text": text, "view": view, "ephemeral": ephemeral})


def interaction(guild, user):
    return types.SimpleNamespace(guild_id=GID, guild=guild, user=user, response=FakeResponse())


def press_verify(env):
    inter = interaction(env.vguild, env.vguild.member)
    asyncio.run(env.nexus.VerificationView().verify.callback(inter))
    return inter.response.sent[0]


def test_button_default_method_is_unchanged(vguild):
    reply = press_verify(vguild)
    assert "You're verified" in reply["text"] and reply["view"] is None
    assert vguild.vguild.member.calls == [("remove", 901), ("add", 900)]
    assert "already verified" in press_verify(vguild)["text"]  # anti-spam inchangé


def test_button_with_turnstile_sends_a_personal_link_and_grants_nothing(vguild, keys):
    from config_store import update_config
    update_config(GID, "verification_method", "turnstile")
    reply = press_verify(vguild)
    assert reply["ephemeral"] is True and vguild.vguild.member.calls == []
    button = reply["view"].children[0]
    assert button.style == discord.ButtonStyle.link and button.label == "Open verification page"
    token = button.url.rsplit("/", 1)[1]
    assert button.url == f"{BASE}/verify/{token}"
    assert verification.get_valid_challenge(token)["user_id"] == "7"
    assert press_verify(vguild)["view"].children[0].url == button.url  # double-clic : même lien


def test_button_with_turnstile_but_no_keys_refuses_instead_of_bypassing(vguild, monkeypatch):
    for var in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "PUBLIC_BASE_URL", "DISCORD_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    from config_store import update_config
    update_config(GID, "verification_method", "turnstile")
    reply = press_verify(vguild)
    assert "temporarily unavailable" in reply["text"] and reply["view"] is None
    assert vguild.vguild.member.calls == []  # surtout PAS de rôle donné sans contrôle


def test_config_verification_method_command(vguild, monkeypatch):
    from config_store import get_config
    cb = vguild.nexus.verification_method.callback

    def run(value):
        inter = types.SimpleNamespace(guild_id=GID, response=FakeResponse())
        asyncio.run(cb(inter, types.SimpleNamespace(value=value)))
        return inter.response.sent[0]

    for var in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "PUBLIC_BASE_URL", "DISCORD_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    refused = run("turnstile")
    assert "isn't configured" in refused["text"] and get_config(GID).get("verification_method") is None
    monkeypatch.setenv("TURNSTILE_SITE_KEY", SITE)
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    assert "Cloudflare Turnstile" in run("turnstile")["text"]
    assert get_config(GID)["verification_method"] == "turnstile"
    assert "one-click button" in run("button")["text"]
    assert get_config(GID)["verification_method"] == "button"
