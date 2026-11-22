"""Réglages de la vérification web : durée du lien, essais, âge minimal, signaux suspects, auto-kick."""
import asyncio
import datetime
import os
import sys
import types

import discord
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # `pytest tests/` (comme la CI) n'ajoute pas la racine du repo
    sys.path.insert(0, ROOT)

import verification  # noqa: E402
from test_verification import (BASE, GID, FakeResponse, VMember, fake_cloudflare, keys, press_verify,  # noqa: E402,F401
                               vguild)

UTC = datetime.timezone.utc


def now():
    return datetime.datetime.now(UTC)


# ---------------------------------------------------------------- réglages

def test_settings_have_safe_defaults_and_are_clamped(env):
    from config_store import get_config, update_config
    assert verification.get_verification_settings(GID) == {
        "verification_link_minutes": 10, "verification_max_attempts": 3,
        "verification_kick_hours": 0, "verification_min_account_days": 0}
    get_config(GID)
    update_config(GID, "verification_link_minutes", 9999)   # hors bornes -> plafonné
    update_config(GID, "verification_max_attempts", -4)     # hors bornes -> relevé
    update_config(GID, "verification_kick_hours", "abc")    # type invalide -> défaut
    update_config(GID, "verification_min_account_days", True)  # booléen refusé -> défaut
    assert verification.get_verification_settings(GID) == {
        "verification_link_minutes": 60, "verification_max_attempts": 1,
        "verification_kick_hours": 0, "verification_min_account_days": 0}


def test_link_lifetime_and_attempts_follow_the_settings(env):
    from config_store import update_config
    update_config(GID, "verification_link_minutes", 30)
    update_config(GID, "verification_max_attempts", 5)
    token = verification.create_challenge(GID, 7)
    doc = env.col.verification_challenges.find_one({"token": token})
    assert doc["expires_at"] - doc["created_at"] == datetime.timedelta(minutes=30) and doc["max_attempts"] == 5
    for _ in range(4):
        verification.record_failed_attempt(token)
    assert verification.get_valid_challenge(token) is not None  # 4 échecs sur 5 : encore vivant
    verification.record_failed_attempt(token)
    assert verification.get_valid_challenge(token) is None


def test_custom_max_attempts_through_the_web_page(vguild, keys, monkeypatch):
    from config_store import update_config
    update_config(GID, "verification_max_attempts", 2)
    fake_cloudflare(monkeypatch, {"success": False, "error-codes": ["invalid-input-response"]})
    token = verification.create_challenge(GID, 7)
    assert "Attempts left: 2" in vguild.client.get(f"/verify/{token}").get_data(as_text=True)
    codes = [vguild.client.post(f"/verify/{token}", data={"cf-turnstile-response": "bad"}).status_code for _ in range(2)]
    assert codes == [400, 410]  # 2 essais seulement


# ------------------------------------------------------------ âge minimal

def test_min_account_age_blocks_new_accounts_at_the_button(vguild, keys):
    from config_store import update_config
    update_config(GID, "verification_method", "turnstile")
    update_config(GID, "verification_min_account_days", 5)
    vguild.vguild.member.created_at = now() - datetime.timedelta(days=2)
    reply = press_verify(vguild)
    assert "too new" in reply["text"] and "3 day(s)" in reply["text"] and reply["view"] is None
    assert vguild.col.verification_challenges.count_documents({}) == 0  # aucun lien créé
    vguild.vguild.member.created_at = now() - datetime.timedelta(days=30)
    assert press_verify(vguild)["view"] is not None  # assez ancien : le lien est envoyé
    update_config(GID, "verification_min_account_days", 0)
    vguild.vguild.member.created_at = now() - datetime.timedelta(hours=1)
    assert press_verify(vguild)["view"] is not None  # 0 = aucune limite


def test_account_days_left_math():
    user = types.SimpleNamespace(created_at=now() - datetime.timedelta(days=2, hours=1))
    assert verification.account_days_left(user, 5) == 3
    assert verification.account_days_left(user, 0) == 0
    assert verification.account_days_left(types.SimpleNamespace(), 5) == 0  # âge inconnu : pas de blocage


# ---------------------------------------------------------- signaux suspects

def test_suspicious_signals():
    young = types.SimpleNamespace(created_at=now() - datetime.timedelta(days=2), avatar=None)
    assert verification.suspicious_signals(young) == ["account created 2 day(s) ago", "default avatar"]
    old = types.SimpleNamespace(created_at=now() - datetime.timedelta(days=400), avatar=object())
    assert verification.suspicious_signals(old) == []
    assert verification.suspicious_signals(types.SimpleNamespace()) == []  # attributs inconnus : rien


def test_signals_are_logged_after_verification_and_never_block_it(vguild, monkeypatch):
    logged = []

    async def record(guild, embed):
        logged.append(embed)

    monkeypatch.setattr(verification, "_send_log_embed", record)
    from config_store import update_config
    update_config(GID, "verified_role_id", 900)
    member = vguild.vguild.member
    member.created_at, member.avatar = now() - datetime.timedelta(days=1), None
    assert asyncio.run(verification.grant_verified(vguild.vguild, 7)) == (True, None)
    assert "default avatar" in logged[0].fields[1].value and "<@7>" in logged[0].fields[0].value
    # un log qui plante ne casse pas la vérification
    async def boom(guild, embed):
        raise RuntimeError("logs down")

    monkeypatch.setattr(verification, "_send_log_embed", boom)
    vguild.vguild.member = VMember(7, [vguild.vguild.unverified])
    vguild.vguild.member.created_at, vguild.vguild.member.avatar = now() - datetime.timedelta(days=1), None
    assert asyncio.run(verification.grant_verified(vguild.vguild, 7)) == (True, None)


# ------------------------------------------------------------------ auto-kick

class KMember:
    def __init__(self, uid, hours_ago, bot=False, fail=False):
        self.id, self.bot, self.fail = uid, bot, fail
        self.joined_at = None if hours_ago is None else now() - datetime.timedelta(hours=hours_ago)
        self.kicked = None

    async def kick(self, reason=None):
        if self.fail:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="no"), "missing perms")
        self.kicked = reason


class KGuild:
    def __init__(self, members, default_role=False):
        self.id = GID
        self.role = types.SimpleNamespace(id=901, members=members, is_default=lambda: default_role)
        self.text_channels = []

    def get_role(self, rid):
        return self.role if rid == 901 else None


@pytest.fixture
def kick_env(env, monkeypatch):
    from config_store import update_config
    logged = []

    async def record(guild, embed):
        logged.append(embed)

    monkeypatch.setattr(verification, "_send_log_embed", record)
    update_config(GID, "unverified_role_id", 901)
    env.logged = logged
    return env


def test_kicks_only_members_unverified_for_too_long(kick_env):
    old, young, bot, unknown, failing = KMember(1, 30), KMember(2, 5), KMember(3, 99, bot=True), KMember(4, None), KMember(5, 50, fail=True)
    guild = KGuild([old, young, bot, unknown, failing])
    kicked = asyncio.run(verification.kick_unverified_members(guild, 24))
    assert kicked == [old] and old.kicked == "Did not verify within 24h"
    assert young.kicked is None and bot.kicked is None and unknown.kicked is None and failing.kicked is None
    case = kick_env.col.sanctions.find_one({"user_id": "1"})
    assert case["type"] == "kick" and case["moderator_id"] == "automod" and case["reason"] == "Did not verify within 24h"
    assert kick_env.col.sanctions.count_documents({}) == 1  # l'échec (Forbidden) n'est pas journalisé comme un kick
    assert len(kick_env.logged) == 1 and "<@1>" in kick_env.logged[0].fields[1].value


def test_kick_is_disabled_without_hours_role_or_with_everyone_role(kick_env):
    members = [KMember(1, 500)]
    assert asyncio.run(verification.kick_unverified_members(KGuild(members), 0)) == []
    assert asyncio.run(verification.kick_unverified_members(KGuild(members, default_role=True), 24)) == []
    from config_store import update_config
    update_config(GID, "unverified_role_id", None)
    assert asyncio.run(verification.kick_unverified_members(KGuild(members), 24)) == []
    assert members[0].kicked is None


def test_kick_batch_is_capped(kick_env):
    members = [KMember(i, 100) for i in range(1, 31)]
    kicked = asyncio.run(verification.kick_unverified_members(KGuild(members), 24))
    assert len(kicked) == verification.KICK_BATCH_LIMIT == 25
    assert sum(m.kicked is not None for m in members) == 25  # les 5 restants attendent le prochain passage


def test_kick_pass_needs_verification_enabled_and_uses_the_setting(kick_env):
    from config_store import update_config
    member = KMember(1, 80)
    guild = KGuild([member])
    update_config(GID, "verification_kick_hours", 48)
    assert asyncio.run(verification.kick_pass(guild)) == []  # vérification pas activée sur ce serveur
    update_config(GID, "verification_enabled", True)
    assert asyncio.run(verification.kick_pass(guild)) == [member] and member.kicked == "Did not verify within 48h"
    update_config(GID, "verification_kick_hours", 0)  # 0 = jamais
    other = KMember(2, 999)
    assert asyncio.run(verification.kick_pass(KGuild([other]))) == [] and other.kicked is None


def test_background_loop_runs_a_pass_per_guild(env, monkeypatch):
    nexus = env.nexus
    passes = []

    async def fake_pass(guild):
        passes.append(guild)

    async def ready():
        return None

    async def fake_sleep(seconds):
        closed["v"] = True
        passes.append(("sleep", seconds))

    closed = {"v": False}
    monkeypatch.setattr(nexus, "kick_pass", fake_pass)
    monkeypatch.setattr(nexus.bot, "wait_until_ready", ready)
    monkeypatch.setattr(nexus.bot, "is_closed", lambda: closed["v"])
    monkeypatch.setattr(nexus.asyncio, "sleep", fake_sleep)
    asyncio.run(nexus.unverified_kick_loop())
    assert passes == [env.guild, ("sleep", 900)]


# ------------------------------------------------------------- /verification settings

def run_settings(env, **kwargs):
    inter = types.SimpleNamespace(guild_id=GID, response=FakeResponse())
    asyncio.run(env.nexus.verification_settings.callback(inter, **kwargs))
    return inter.response.sent[0]["text"]


def test_settings_command_saves_valid_values_and_shows_the_summary(env):
    text = run_settings(env, link_minutes=15, kick_hours=48)
    assert "Link lifetime (minutes): **15**" in text and "Auto-kick after (hours, 0 = never): **48**" in text
    assert "Max attempts: **3**" in text  # non fourni : valeur actuelle conservée
    assert verification.get_verification_settings(GID)["verification_link_minutes"] == 15
    run_settings(env, kick_hours=0, max_attempts=10, link_minutes=2, min_account_days=365)  # bornes incluses
    assert verification.get_verification_settings(GID) == {
        "verification_link_minutes": 2, "verification_max_attempts": 10,
        "verification_kick_hours": 0, "verification_min_account_days": 365}


def test_settings_command_is_all_or_nothing(env):
    run_settings(env, link_minutes=20)
    text = run_settings(env, link_minutes=30, max_attempts=99, kick_hours=500)
    assert text.startswith("❌") and "between 1 and 10" in text and "between 0 and 168" in text
    assert verification.get_verification_settings(GID)["verification_link_minutes"] == 20  # rien d'enregistré
