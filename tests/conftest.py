"""Fixtures partagées par les tests web (étape 2b-2).

`nexus`  : le module main importé une seule fois (sans lancer bot ni Flask).
`env`    : base mongomock branchée via init_mongo() + faux Discord (guilde, salons,
           utilisateur), un vrai event loop dans un thread pour run_coroutine().
Les fichiers de tests plus anciens (test_rest_api, test_app_smoke...) gardent
leurs propres fixtures du même nom, qui priment sur celles-ci.
"""
import asyncio
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPAT = os.path.join(ROOT, "compat")
for import_path in (ROOT, COMPAT):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)
GUILD = 111


class FakeAvatar:
    url = "https://cdn.example/avatar.png"


class FakeMember:
    def __init__(self, uid, name="Bob", top_role=1, bot=False):
        self.id = uid
        self.display_name = name
        self.name = name
        self.top_role = top_role
        self.bot = bot
        self.display_avatar = FakeAvatar()

    def __str__(self):
        return self.name


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
        self.voice_channels = []
        self.categories = []
        self.roles = [FakeRole(gid, "@everyone", default=True), FakeRole(9, "Mods")]

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class FakeOwner:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class Env:
    pass


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


@pytest.fixture
def env(nexus, monkeypatch, event_loop_thread):
    import core
    if os.getenv("CI"):
        import mongomock
    else:
        mongomock = pytest.importorskip("mongomock")

    for name in [n for n in vars(nexus) if n.endswith("_col")] + ["mongo", "db"]:
        monkeypatch.setattr(nexus, name, getattr(nexus, name))
    monkeypatch.setattr(nexus, "MongoClient", lambda *a, **kw: mongomock.MongoClient())
    monkeypatch.setattr(nexus.bot, "db", None, raising=False)
    core.reset()
    nexus.init_mongo()

    e = Env()
    e.nexus = nexus
    e.guild = FakeGuild(GUILD)
    e.guilds = {GUILD: e.guild}
    e.ready = True
    e.owner = FakeOwner()
    e.client = nexus.api.test_client()
    e.col = core.col

    monkeypatch.setattr(nexus.bot, "is_ready", lambda: e.ready)
    monkeypatch.setattr(nexus.bot, "get_guild", lambda gid: e.guilds.get(gid))
    monkeypatch.setattr(type(nexus.bot), "guilds", property(lambda self: list(e.guilds.values())))
    monkeypatch.setattr(nexus.bot, "locked_guilds", set())
    monkeypatch.setattr(nexus.bot, "loop", event_loop_thread, raising=False)

    class AppInfo:
        owner = e.owner
        team = None

    async def application_info():
        return AppInfo()

    monkeypatch.setattr(nexus.bot, "application_info", application_info)
    yield e
    core.reset()
