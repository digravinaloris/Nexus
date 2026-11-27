"""Tests de caractérisation de la REST API (/api/*) utilisée par l'app mobile.

Ils décrivent le comportement ACTUEL de l'API (statuts HTTP, JSON, effets en
base) via le client de test Flask, sans connaître l'endroit où vivent les
routes : ils passent donc avant ET après l'extraction de main.py vers
web/rest_api.py. Fausse base : mongomock. Faux Discord : petits objets maison
(un vrai event loop tourne dans un thread pour `run_coroutine`).
"""
import asyncio
import datetime
import os
import sys
import threading

import pytest

if os.getenv("CI"):
    import mongomock
else:
    mongomock = pytest.importorskip("mongomock")

for _mod in ("discord", "flask", "PIL", "matplotlib", "reportlab", "deep_translator",
             "sentry_sdk", "pyotp", "yaml", "pymongo"):
    pytest.importorskip(_mod)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUILD = 111
OTHER_GUILD = 222
MASTER = {"X-API-Key": "master-key"}

# Liste de référence des routes /api/* : si une route disparaît, change de
# méthode ou de chemin pendant un refactor, le test correspondant échoue.
GOLDEN_ROUTES = [
    ("/api/guilds", ("GET",)),
    ("/api/guilds/<guild_id>/appeals", ("GET",)),
    ("/api/guilds/<guild_id>/appeals/<token>/accept", ("POST",)),
    ("/api/guilds/<guild_id>/appeals/<token>/deny", ("POST",)),
    ("/api/guilds/<guild_id>/audit", ("GET",)),
    ("/api/guilds/<guild_id>/broadcast", ("POST",)),
    ("/api/guilds/<guild_id>/cases", ("GET",)),
    ("/api/guilds/<guild_id>/channels", ("GET",)),
    ("/api/guilds/<guild_id>/config", ("GET",)),
    ("/api/guilds/<guild_id>/config", ("POST",)),
    ("/api/guilds/<guild_id>/faq", ("GET",)),
    ("/api/guilds/<guild_id>/faq", ("POST",)),
    ("/api/guilds/<guild_id>/faq/<entry_id>", ("DELETE",)),
    ("/api/guilds/<guild_id>/faq/toggle", ("POST",)),
    ("/api/guilds/<guild_id>/features", ("GET",)),
    ("/api/guilds/<guild_id>/features/<feature_key>", ("POST",)),
    ("/api/guilds/<guild_id>/lock", ("POST",)),
    ("/api/guilds/<guild_id>/members", ("GET",)),
    ("/api/guilds/<guild_id>/members/<user_id>/ban", ("POST",)),
    ("/api/guilds/<guild_id>/members/<user_id>/kick", ("POST",)),
    ("/api/guilds/<guild_id>/members/<user_id>/mute", ("POST",)),
    ("/api/guilds/<guild_id>/members/<user_id>/unmute", ("POST",)),
    ("/api/guilds/<guild_id>/members/<user_id>/unwarn", ("POST",)),
    ("/api/guilds/<guild_id>/members/<user_id>/warn", ("POST",)),
    ("/api/guilds/<guild_id>/members/<user_id>/warnings", ("GET",)),
    ("/api/guilds/<guild_id>/roles", ("GET",)),
    ("/api/guilds/<guild_id>/stats", ("GET",)),
    ("/api/guilds/<guild_id>/unlock", ("POST",)),
    ("/api/guilds/<guild_id>/warnlist", ("GET",)),
    ("/api/health", ("GET",)),
]


# ------------------------------------------------------------- faux Discord

class FakeAvatar:
    url = "https://cdn.example/avatar.png"


class FakeMember:
    def __init__(self, uid, name="Bob", top_role=1, bot=False, timed_out=False):
        self.id = uid
        self.display_name = name
        self.name = name
        self.top_role = top_role
        self.bot = bot
        self.display_avatar = FakeAvatar()
        self._timed_out = timed_out
        self.timed_out_until = None
        self.calls = []

    def __str__(self):
        return self.name

    def is_timed_out(self):
        return self._timed_out

    async def ban(self, reason=None):
        self.calls.append(("ban", reason))

    async def kick(self, reason=None):
        self.calls.append(("kick", reason))

    async def timeout(self, duration, reason=None):
        self.calls.append(("timeout", duration, reason))


class FakeChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.sent = []

    async def send(self, content=None, embed=None):
        self.sent.append((content, embed))


class FakeRole:
    def __init__(self, rid, name, default=False):
        self.id = rid
        self.name = name
        self.mention = f"<@&{rid}>"
        self._default = default

    def is_default(self):
        return self._default


class FakeGuild:
    def __init__(self, gid, name="Test Guild"):
        self.id = gid
        self.name = name
        self.member_count = 3
        self.icon = None
        self.me = FakeMember(1, "Nexus", top_role=5)
        self.members = []
        self.text_channels = []
        self.roles = [FakeRole(gid, "@everyone", default=True), FakeRole(9, "Mods")]
        self.unbanned = []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    async def bans(self):
        for i in (1, 2):
            yield i

    async def unban(self, obj, reason=None):
        self.unbanned.append(obj.id)


class FakeUser:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


# ----------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def nexus():
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


@pytest.fixture(scope="module")
def event_loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


class Env:
    pass


@pytest.fixture
def env(nexus, monkeypatch, event_loop_thread):
    import core

    # Restaure tous les globals que init_mongo() réassigne.
    for name in [n for n in vars(nexus) if n.endswith("_col")] + ["mongo", "db"]:
        monkeypatch.setattr(nexus, name, getattr(nexus, name))
    monkeypatch.setattr(nexus, "MongoClient", lambda *a, **kw: mongomock.MongoClient())
    monkeypatch.setattr(nexus.bot, "db", None, raising=False)
    core.reset()
    nexus.init_mongo()

    # Clé maîtresse : à l'endroit où vit le décorateur (main.py avant l'extraction,
    # web.rest_api après).
    monkeypatch.setattr(nexus, "API_KEY", "master-key", raising=False)
    try:
        import web.rest_api as rest_module
        monkeypatch.setattr(rest_module, "API_KEY", "master-key")
    except ImportError:
        pass

    e = Env()
    e.nexus = nexus
    e.guild = FakeGuild(GUILD)
    e.guilds = {GUILD: e.guild}
    e.ready = True
    e.user = FakeUser()
    e.client = nexus.api.test_client()
    e.col = core.col

    monkeypatch.setattr(nexus.bot, "is_ready", lambda: e.ready)
    monkeypatch.setattr(nexus.bot, "get_guild", lambda gid: e.guilds.get(gid))
    monkeypatch.setattr(type(nexus.bot), "guilds", property(lambda self: list(e.guilds.values())))
    monkeypatch.setattr(nexus.bot, "locked_guilds", set())
    monkeypatch.setattr(nexus.bot, "loop", event_loop_thread, raising=False)

    async def fetch_user(uid):
        return e.user

    monkeypatch.setattr(nexus.bot, "fetch_user", fetch_user)
    yield e
    core.reset()


def url(path, guild=GUILD):
    return f"/api/guilds/{guild}{path}"


# -------------------------------------------------------------------- routes

def test_rest_routes_match_golden_list(nexus):
    rows = sorted(
        (r.rule, tuple(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))))
        for r in nexus.api.url_map.iter_rules() if r.rule.startswith("/api/")
    )
    assert rows == sorted(GOLDEN_ROUTES)


# ---------------------------------------------------------------------- auth

def test_missing_key_is_401(env):
    resp = env.client.get("/api/health")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Missing X-API-Key header"}


def test_invalid_guild_snowflakes_are_rejected_before_config_lookup(env, monkeypatch):
    import web.rest_api as rest_module

    def unexpected_lookup(_guild_id):
        raise AssertionError("invalid guild ID reached config lookup")

    class UnexpectedConfigService:
        def api_config_snapshot(self, guild_id):
            return unexpected_lookup(guild_id)

    monkeypatch.setattr(rest_module, "get_bot_service", lambda: UnexpectedConfigService())
    for invalid in ("abc", "0", str(2**64), "１２３"):
        response = env.client.get(url("/features", invalid), headers=MASTER)
        assert response.status_code == 400
        assert response.get_json() == {"error": "Invalid guild_id"}


def test_invalid_user_snowflakes_are_rejected(env):
    for invalid in ("abc", "0", str(2**64), "１２３"):
        response = env.client.get(
            url(f"/members/{invalid}/warnings"), headers=MASTER
        )
        assert response.status_code == 400
        assert response.get_json() == {"error": "Invalid user_id"}


def test_oversized_api_key_is_rejected_before_guild_lookup(env, monkeypatch):
    import web.rest_api as rest_module

    def unexpected_lookup():
        raise AssertionError("oversized API key reached guild key lookup")

    monkeypatch.setattr(rest_module, "get_bot_service", unexpected_lookup)
    response = env.client.get(
        url("/features"), headers={"X-API-Key": "x" * 513}
    )
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_wrong_key_is_401(env):
    resp = env.client.get("/api/health", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Unauthorized"}


def test_master_key_grants_access(env):
    assert env.client.get("/api/health", headers=MASTER).status_code == 200


def test_guild_key_only_works_for_its_own_guild(env):
    env.guilds[OTHER_GUILD] = FakeGuild(OTHER_GUILD)
    from config_store import update_config
    update_config(GUILD, "api_key", "guild-key")
    h = {"X-API-Key": "guild-key"}
    assert env.client.get(url("/features"), headers=h).status_code == 200
    assert env.client.get(url("/features", OTHER_GUILD), headers=h).status_code == 401


def test_guild_key_without_guild_in_url_is_resolved_via_bot_guilds(env):
    from config_store import update_config
    update_config(GUILD, "api_key", "guild-key")
    resp = env.client.get("/api/guilds", headers={"X-API-Key": "guild-key"})
    assert resp.status_code == 200
    assert [g["id"] for g in resp.get_json()] == [str(GUILD)]  # jamais les autres serveurs


# -------------------------------------------------------------------- health

def test_health_master(env):
    body = env.client.get("/api/health", headers=MASTER).get_json()
    assert body == {"online": True, "locked": False, "guild_count": 1}
    env.ready = False
    body = env.client.get("/api/health", headers=MASTER).get_json()
    assert body == {"online": False, "locked": False, "guild_count": 0}


def test_health_guild_key_reports_only_its_guild(env):
    from config_store import update_config
    update_config(GUILD, "api_key", "guild-key")
    env.nexus.bot.locked_guilds.add(GUILD)
    body = env.client.get("/api/health", headers={"X-API-Key": "guild-key"}).get_json()
    assert body == {"online": True, "locked": True, "guild_count": 1}


# -------------------------------------------------------------------- guilds

def test_list_guilds(env):
    resp = env.client.get("/api/guilds", headers=MASTER)
    assert resp.get_json() == [
        {"id": str(GUILD), "name": "Test Guild", "member_count": 3, "icon_url": None}]
    env.ready = False
    assert env.client.get("/api/guilds", headers=MASTER).status_code == 503


def test_guild_not_found_and_bot_not_ready(env):
    assert env.client.get(url("/channels", 999), headers=MASTER).status_code == 404
    env.ready = False
    resp = env.client.get(url("/channels"), headers=MASTER)
    assert resp.status_code == 503 and resp.get_json() == {"error": "Bot not ready"}


def test_channels_roles_members(env):
    env.guild.text_channels = [FakeChannel(5, "general")]
    env.guild.members = [FakeMember(10, "Bob"), FakeMember(11, "BotUser", bot=True)]
    assert env.client.get(url("/channels"), headers=MASTER).get_json() == [{"id": "5", "name": "general"}]
    assert env.client.get(url("/roles"), headers=MASTER).get_json() == [{"id": "9", "name": "Mods"}]
    members = env.client.get(url("/members"), headers=MASTER).get_json()
    assert [m["id"] for m in members] == ["10"]  # les bots sont exclus
    assert members[0]["is_muted"] is False and members[0]["username"] == "Bob"


def test_stats(env):
    env.guild.members = [FakeMember(10, timed_out=True), FakeMember(11)]
    env.col.warns.insert_one({"guild_id": str(GUILD), "user_id": "10", "count": 2})
    env.col.warns.insert_one({"guild_id": str(GUILD), "user_id": "11", "count": 0})
    from sanctions_store import log_sanction
    log_sanction(GUILD, 10, "warn", "x", 7)
    body = env.client.get(url("/stats"), headers=MASTER).get_json()
    assert body["ban_count"] == 2 and body["muted_count"] == 1
    assert body["warned_count"] == 1 and body["warns_today"] == 1
    assert body["locked"] is False and len(body["activity_7d"]) == 7
    assert body["open_tickets_count"] == 0


# -------------------------------------------------------------------- config

def test_config_get_hides_secrets(env):
    from config_store import get_config, update_config
    get_config(GUILD)  # crée le doc avec ses valeurs par défaut (un upsert seul ne le fait pas)
    update_config(GUILD, "api_key", "secret")
    body = env.client.get(url("/config"), headers=MASTER).get_json()
    assert "api_key" not in body and "_id" not in body
    assert body["guild_id"] == str(GUILD) and body["logs_channel"] == "logs"


def test_config_post_validation_and_update(env):
    h = MASTER
    assert env.client.post(url("/config", 999), json={"language": "fr"}, headers=h).status_code == 404
    assert env.client.post(url("/config"), json={"nope": 1}, headers=h).status_code == 400
    for payload in ([], "invalid", 123):
        response = env.client.post(url("/config"), json=payload, headers=h)
        assert response.status_code == 400
        assert response.get_json() == {"error": "JSON body must be an object"}

    invalid_payloads = [
        {"language": "de"},
        {"language": 1},
        {"logs_channel": []},
        {"logs_channel": "not-a-channel-id"},
        {"autorole": "not-a-role-id"},
        {"allowed_roles": "5"},
        {"allowed_roles": ["5", None]},
        {"command_roles": []},
        {"command_roles": {"ban": "5"}},
        {"command_roles": {"ban": ["5", "bad"]}},
    ]
    from config_store import get_config
    for payload in invalid_payloads:
        before = dict(get_config(GUILD))
        response = env.client.post(url("/config"), json=payload, headers=h)
        assert response.status_code == 400
        assert get_config(GUILD) == before

    resp = env.client.post(url("/config"), json={
        "language": "fr", "nope": 1, "autorole": "5",
        "allowed_roles": ["5"], "command_roles": {"ban": ["5"]},
    }, headers=h)
    assert resp.status_code == 200
    assert resp.get_json() == {
        "success": True,
        "updated": {
            "language": "fr", "autorole": "5",
            "allowed_roles": ["5"], "command_roles": {"ban": ["5"]},
        },
    }
    assert get_config(GUILD)["language"] == "fr"
    assert "nope" not in get_config(GUILD)
    env.ready = False
    assert env.client.post(url("/config"), json={"language": "en"}, headers=h).status_code == 503


# ---------------------------------------------------------------------- lock

def test_lock_unlock(env):
    resp = env.client.post(url("/lock"), headers=MASTER)
    assert resp.get_json() == {"success": True, "locked": True}
    assert GUILD in env.nexus.bot.locked_guilds
    resp = env.client.post(url("/unlock"), headers=MASTER)
    assert resp.get_json() == {"success": True, "locked": False}
    assert GUILD not in env.nexus.bot.locked_guilds


# ---------------------------------------------------------------- moderation

def test_ban_kick_success_and_logging(env):
    member = FakeMember(10)
    env.guild.members = [member]
    resp = env.client.post(url("/members/10/ban"), json={"reason": "spam"}, headers=MASTER)
    assert resp.get_json() == {"success": True}
    assert member.calls == [("ban", "spam")]
    resp = env.client.post(url("/members/10/kick"), headers=MASTER)
    assert resp.status_code == 200
    assert member.calls[-1] == ("kick", "Kicked via mobile app")
    docs = list(env.col.sanctions.find({"guild_id": str(GUILD)}).sort("case_id", 1))
    assert [(d["type"], d["moderator_id"], d["case_id"]) for d in docs] == [
        ("ban", "mobile_app", 1), ("kick", "mobile_app", 2)]


def test_ban_errors(env):
    resp = env.client.post(url("/members/10/ban"), headers=MASTER)
    assert resp.status_code == 400 and resp.get_json() == {"error": "Member not found"}
    env.guild.members = [FakeMember(10, top_role=9)]
    resp = env.client.post(url("/members/10/ban"), headers=MASTER)
    assert resp.status_code == 400 and resp.get_json() == {"error": "Role too high"}
    assert env.col.sanctions.count_documents({}) == 0


def test_mobile_moderation_rejects_malformed_payloads_before_side_effects(env):
    member = FakeMember(10)
    env.guild.members = [member]
    for route in ("ban", "kick", "mute", "warn"):
        for payload in ([], "invalid", 123):
            response = env.client.post(
                url(f"/members/10/{route}"), json=payload, headers=MASTER
            )
            assert response.status_code == 400
            assert response.get_json() == {"error": "JSON body must be an object"}
        null_response = env.client.post(
            url(f"/members/10/{route}"), data="null",
            content_type="application/json", headers=MASTER,
        )
        assert null_response.status_code == 400
        assert null_response.get_json() == {"error": "JSON body must be an object"}
        for reason in (123, [], {}, "x" * 513):
            response = env.client.post(
                url(f"/members/10/{route}"), json={"reason": reason}, headers=MASTER
            )
            assert response.status_code == 400
            assert response.get_json() == {
                "error": "reason must be a string of at most 512 characters"
            }
    assert member.calls == []
    assert env.col.sanctions.count_documents({}) == 0
    assert env.col.warns.count_documents({}) == 0


def test_mute_and_unmute(env):
    member = FakeMember(10)
    env.guild.members = [member]
    resp = env.client.post(url("/members/10/mute"), json={"minutes": 30, "reason": "calm"}, headers=MASTER)
    assert resp.status_code == 200
    assert member.calls[0] == ("timeout", datetime.timedelta(minutes=30), "calm")
    doc = env.col.sanctions.find_one({"type": "mute"})
    assert doc["reason"] == "calm (30 min)"
    resp = env.client.post(url("/members/10/unmute"), headers=MASTER)
    assert resp.status_code == 200 and member.calls[-1][1] is None
    assert env.client.post(url("/members/99/unmute"), headers=MASTER).status_code == 400


def test_mute_rejects_invalid_duration_without_calling_discord(env):
    member = FakeMember(10)
    env.guild.members = [member]

    for minutes in ("abc", -1, 0, 40321, True, 30.5):
        response = env.client.post(
            url("/members/10/mute"),
            json={"minutes": minutes},
            headers=MASTER,
        )
        assert response.status_code == 400
        assert response.get_json() == {
            "error": "minutes must be an integer between 1 and 40320"
        }

    assert member.calls == []
    assert env.col.sanctions.count_documents({}) == 0


def test_warn_unwarn_and_listing(env):
    env.guild.members = [FakeMember(10)]
    resp = env.client.post(url("/members/10/warn"), json={"reason": "rude"}, headers=MASTER)
    assert resp.get_json() == {"success": True, "warnings": 1, "reason": "rude"}
    resp = env.client.post(url("/members/10/warn"), headers=MASTER)
    assert resp.get_json()["warnings"] == 2
    assert env.client.post(url("/members/77/warn"), headers=MASTER).status_code == 404
    assert env.client.get(url("/members/10/warnings"), headers=MASTER).get_json() == {
        "user_id": "10", "warnings": 2}
    assert env.client.get(url("/warnlist"), headers=MASTER).get_json() == [{"user_id": "10", "count": 2}]
    resp = env.client.post(url("/members/10/unwarn"), headers=MASTER)
    assert resp.get_json() == {"success": True, "warnings": 1}
    env.client.post(url("/members/10/unwarn"), headers=MASTER)
    resp = env.client.post(url("/members/10/unwarn"), headers=MASTER)
    assert resp.status_code == 400 and resp.get_json() == {"error": "No warnings to remove"}


def test_broadcast(env):
    channel = FakeChannel(5, "general")
    env.guild.text_channels = [channel]
    h = MASTER
    assert env.client.post(url("/broadcast"), json={"message": "hi"}, headers=h).status_code == 400
    resp = env.client.post(url("/broadcast"), json={"message": "hi", "channel_id": "404"}, headers=h)
    assert resp.status_code == 404
    resp = env.client.post(url("/broadcast"), json={"message": "hi", "channel_id": "5", "mention": "everyone"}, headers=h)
    assert resp.get_json() == {"success": True}
    content, embed = channel.sent[0]
    assert content == "@everyone" and embed.description == "hi"
    env.client.post(url("/broadcast"), json={"message": "yo", "channel_id": "5", "mention": "Mods"}, headers=h)
    assert channel.sent[1][0] == "<@&9>"


def test_broadcast_rejects_malformed_payloads_without_sending(env):
    channel = FakeChannel(5, "general")
    env.guild.text_channels = [channel]
    for payload in ([], "invalid", 123):
        response = env.client.post(url("/broadcast"), json=payload, headers=MASTER)
        assert response.status_code == 400
        assert response.get_json() == {"error": "JSON body must be an object"}
    null_response = env.client.post(
        url("/broadcast"), data="null", content_type="application/json", headers=MASTER
    )
    assert null_response.status_code == 400
    assert null_response.get_json() == {"error": "JSON body must be an object"}
    invalid_payloads = [
        {"message": 123, "channel_id": "5"},
        {"message": " ", "channel_id": "5"},
        {"message": "x" * 4097, "channel_id": "5"},
        {"message": "hi", "channel_id": 5},
        {"message": "hi", "channel_id": "invalid"},
        {"message": "hi", "channel_id": "5", "mention": []},
        {"message": "hi", "channel_id": "5", "mention": "x" * 101},
    ]
    for payload in invalid_payloads:
        response = env.client.post(url("/broadcast"), json=payload, headers=MASTER)
        assert response.status_code == 400
    assert channel.sent == []


# ------------------------------------------------------------ audit / cases

def test_audit_feed_and_cases(env):
    from audit import record_audit
    from sanctions_store import log_sanction
    record_audit(GUILD, 1, "Admin", "Changed config", "x")
    log_sanction(GUILD, 10, "ban", "spam", "mobile_app")
    log_sanction(GUILD, 11, "warn", "rude", "automod")
    # Les timestamps Mongo ont une précision de 1 ms : on écarte les deux cas
    # pour que l'ordre « plus récent d'abord » soit déterministe.
    env.col.sanctions.update_one(
        {"guild_id": str(GUILD), "case_id": 1},
        {"$set": {"timestamp": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)}})
    feed = env.client.get(url("/audit"), headers=MASTER).get_json()
    assert len(feed) == 3 and {"actor_name", "action", "details", "timestamp"} <= set(feed[0])
    cases = env.client.get(url("/cases"), headers=MASTER).get_json()
    assert [c["case_id"] for c in cases] == [2, 1]  # plus récent d'abord
    only_bans = env.client.get(url("/cases?type=ban"), headers=MASTER).get_json()
    assert [c["type"] for c in only_bans] == ["ban"]
    assert len(env.client.get(url("/cases?limit=1"), headers=MASTER).get_json()) == 1
    assert len(env.client.get(url("/cases?limit=0"), headers=MASTER).get_json()) == 1
    assert len(env.client.get(url("/cases?limit=-5"), headers=MASTER).get_json()) == 1
    assert len(env.client.get(url("/audit?limit=0"), headers=MASTER).get_json()) == 1
    assert len(env.client.get(url("/audit?limit=-5"), headers=MASTER).get_json()) == 1


# ------------------------------------------------------------------- appeals

def _submitted_appeal(env, sanction_type="ban", token="tok"):
    from sanctions_store import create_sanction_appeal
    create_sanction_appeal(GUILD, 5, sanction_type, 42, "Bob", "spam", token)
    env.col.ban_appeals.update_one(
        {"token": token},
        {"$set": {"status": "submitted", "appeal_text": "sorry",
                  "submitted_at": datetime.datetime.now(datetime.timezone.utc)}})


def test_appeals_list_accept_deny(env):
    _submitted_appeal(env, "ban", "t1")
    _submitted_appeal(env, "warn", "t2")
    listed = env.client.get(url("/appeals"), headers=MASTER).get_json()
    assert {a["token"] for a in listed} == {"t1", "t2"}
    assert listed[0]["original_reason"] == "spam" and listed[0]["appeal_text"] == "sorry"

    resp = env.client.post(url("/appeals/t1/accept"), headers=MASTER)
    assert resp.get_json() == {"success": True, "result": "user unbanned"}
    assert env.guild.unbanned == [42]
    assert env.col.ban_appeals.find_one({"token": "t1"})["status"] == "accepted"
    assert "accepted" in env.user.sent[0]

    resp = env.client.post(url("/appeals/t2/deny"), headers=MASTER)
    assert resp.get_json() == {"success": True}
    assert env.col.ban_appeals.find_one({"token": "t2"})["status"] == "denied"
    assert "denied" in env.user.sent[1]
    # Déjà résolu -> 404 ; token inconnu -> 404
    assert env.client.post(url("/appeals/t1/accept"), headers=MASTER).status_code == 404
    assert env.client.post(url("/appeals/zzz/deny"), headers=MASTER).status_code == 404
    audit_actions = [d["action"] for d in env.col.audit.find({"guild_id": str(GUILD)})]
    assert "Accepted appeal (via mobile app)" in audit_actions
    assert "Denied appeal (via mobile app)" in audit_actions


def test_mobile_failed_appeal_reversal_releases_claim(env, monkeypatch):
    import web.in_process_bot_service as service_module

    _submitted_appeal(env, "ban", "failed-accept")

    def fail_reversal(coroutine):
        coroutine.close()
        raise RuntimeError("Discord temporarily unavailable")

    monkeypatch.setattr(service_module, "run_coroutine", fail_reversal)
    response = env.client.post(
        url("/appeals/failed-accept/accept"), headers=MASTER
    )
    assert response.status_code == 500
    assert response.get_json() == {"error": "Unable to resolve appeal"}

    appeal = env.col.ban_appeals.find_one({"token": "failed-accept"})
    assert appeal["status"] == "submitted"
    assert "processing_at" not in appeal


@pytest.mark.parametrize("action", ["accept", "deny"])
def test_mobile_appeal_claim_prevents_concurrent_resolution(env, monkeypatch, action):
    import web.rest_api as rest_module
    import web.in_process_bot_service as service_module

    token = f"race-{action}"
    _submitted_appeal(env, "ban", token)
    stale_appeal = dict(env.col.ban_appeals.find_one({"token": token}))
    env.col.ban_appeals.update_one(
        {"token": token}, {"$set": {"status": "processing"}}
    )
    appeal_service = rest_module.get_bot_service()
    monkeypatch.setattr(
        appeal_service, "api_get_appeal",
        lambda _token: {**stale_appeal, "status": "submitted"},
    )
    monkeypatch.setattr(rest_module, "get_bot_service", lambda: appeal_service)
    side_effects = []
    monkeypatch.setattr(
        service_module, "run_coroutine",
        lambda coroutine: side_effects.append(coroutine) or "unexpected",
    )

    response = env.client.post(
        url(f"/appeals/{token}/{action}"), headers=MASTER
    )
    assert response.status_code == 404
    assert env.col.ban_appeals.find_one({"token": token})["status"] == "processing"
    assert side_effects == []


# ------------------------------------------------------------------ features

def test_features_list_and_toggle(env):
    feats = env.client.get(url("/features"), headers=MASTER).get_json()
    assert len(feats) >= 17
    assert {"key", "label", "enabled"} == set(feats[0])
    assert all(f["enabled"] is False or f["key"] == "remindme" for f in feats)
    resp = env.client.post(url("/features/antinuke"), json={"enabled": True}, headers=MASTER)
    assert resp.get_json() == {"success": True, "key": "antinuke", "enabled": True}
    from config_store import get_config
    assert get_config(GUILD)["antinuke_enabled"] is True
    feats = {f["key"]: f["enabled"] for f in env.client.get(url("/features"), headers=MASTER).get_json()}
    assert feats["antinuke"] is True
    assert env.client.post(url("/features/does_not_exist"), headers=MASTER).status_code == 404
    for invalid in ("false", "true", 0, 1, None, [], {}):
        response = env.client.post(url("/features/antinuke"), json={"enabled": invalid}, headers=MASTER)
        assert response.status_code == 400
    assert {item["key"]: item["enabled"] for item in env.client.get(url("/features"), headers=MASTER).get_json()}["antinuke"] is True


# ----------------------------------------------------------------------- faq

def test_faq_crud_and_toggle(env):
    h = MASTER
    assert env.client.get(url("/faq"), headers=h).get_json() == {"enabled": False, "entries": []}
    invalid_payloads = [
        {"keywords": "rules", "response": "answer"},
        {"keywords": ["rules", None], "response": "answer"},
        {"keywords": [], "response": "answer"},
        {"keywords": [" "], "response": "answer"},
        {"keywords": ["x" * 101], "response": "answer"},
        {"keywords": ["rules"], "response": 123},
        {"keywords": ["rules"], "response": " "},
        {"keywords": ["rules"], "response": "x" * 2001},
        {"keywords": ["rules"] * 26, "response": "answer"},
    ]
    for payload in invalid_payloads:
        response = env.client.post(url("/faq"), json=payload, headers=h)
        assert response.status_code == 400
    assert env.col.faq.count_documents({"guild_id": str(GUILD)}) == 0
    resp = env.client.post(url("/faq"), json={"keywords": [" rules ", "règles"], "response": " Read #rules "}, headers=h)
    entry_id = resp.get_json()["id"]
    assert len(entry_id) == 32
    body = env.client.get(url("/faq"), headers=h).get_json()
    assert body["entries"] == [{"id": entry_id, "keywords": ["rules", "règles"], "response": "Read #rules"}]
    assert env.client.post(url("/faq/toggle"), json={"enabled": True}, headers=h).get_json() == {
        "success": True, "enabled": True}
    assert env.client.get(url("/faq"), headers=h).get_json()["enabled"] is True
    assert env.client.delete(url("/faq/zzzz"), headers=h).status_code == 404
    assert env.client.delete(url(f"/faq/{entry_id}"), headers=h).get_json() == {"success": True}
    assert env.client.get(url("/faq"), headers=h).get_json()["entries"] == []
    for invalid in ("false", "true", 0, 1, None, [], {}):
        response = env.client.post(url("/faq/toggle"), json={"enabled": invalid}, headers=h)
        assert response.status_code == 400
    assert env.client.get(url("/faq"), headers=h).get_json()["enabled"] is True
    assert env.client.delete(url("/faq/x", OTHER_GUILD), headers=h).status_code == 404


def test_master_api_key_uses_constant_time_comparison(env, monkeypatch):
    import web.rest_api as rest_module

    original = rest_module.secrets.compare_digest
    calls = []

    def tracked(provided, expected):
        calls.append((provided, expected))
        return original(provided, expected)

    monkeypatch.setattr(rest_module.secrets, "compare_digest", tracked)
    response = env.client.get("/api/health", headers=MASTER)
    assert response.status_code == 200
    assert ("master-key", "master-key") in calls


def test_guild_api_key_uses_constant_time_comparison(env, monkeypatch):
    import web.rest_api as rest_module
    from config_store import update_config

    update_config(GUILD, "api_key", "guild-key")
    original = rest_module.secrets.compare_digest
    calls = []

    def tracked(provided, expected):
        calls.append((provided, expected))
        return original(provided, expected)

    monkeypatch.setattr(rest_module.secrets, "compare_digest", tracked)
    response = env.client.get(url("/features"), headers={"X-API-Key": "guild-key"})
    assert response.status_code == 200
    assert ("guild-key", "guild-key") in calls



def test_guild_key_fallback_uses_constant_time_comparison(env, monkeypatch):
    import web.rest_api as rest_module
    from config_store import update_config

    update_config(GUILD, "api_key", "guild-key")
    original = rest_module.secrets.compare_digest
    calls = []

    def tracked(provided, expected):
        calls.append((provided, expected))
        return original(provided, expected)

    monkeypatch.setattr(rest_module.secrets, "compare_digest", tracked)
    response = env.client.get("/api/guilds", headers={"X-API-Key": "guild-key"})
    assert response.status_code == 200
    assert ("guild-key", "guild-key") in calls



def test_broadcast_hides_internal_exception_details(env, monkeypatch):
    import web.rest_api as rest_module

    service = rest_module.get_bot_service()

    def fail_broadcast(*args, **kwargs):
        raise RuntimeError("database password leaked")

    monkeypatch.setattr(service, "api_broadcast", fail_broadcast)
    response = env.client.post(
        url("/broadcast"),
        json={"message": "hello", "channel_id": "5"},
        headers=MASTER,
    )

    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal server error"}
    assert b"database password leaked" not in response.data
