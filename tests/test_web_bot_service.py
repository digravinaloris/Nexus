"""Tests for the web-to-bot service boundary."""
import core
from web.in_process_bot_service import InProcessBotService


def test_bot_service_interface_module_has_no_runtime_imports():
    import web.bot_service as interface_module

    assert "core" not in vars(interface_module)
    assert "discord" not in vars(interface_module)


def test_in_process_adapter_implements_every_service_contract_method():
    from web.bot_service import BotService

    interface_methods = {
        name for name, value in vars(BotService).items()
        if callable(value) and not name.startswith("_")
    }
    adapter_methods = {
        name for name, value in vars(InProcessBotService).items()
        if callable(value)
    }

    assert interface_methods <= adapter_methods


class FakeBot:
    def __init__(self, ready=True, guilds=None):
        self.ready = ready
        self.guilds = guilds or []

    def is_ready(self):
        return self.ready


class FakeMongoAdmin:
    def __init__(self, error=None):
        self.error = error

    def command(self, name):
        assert name == "ping"
        if self.error:
            raise self.error


class FakeMongo:
    def __init__(self, error=None):
        self.admin = FakeMongoAdmin(error)


def test_health_snapshot_reports_connected_dependencies(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(guilds=[1, 2]))
    monkeypatch.setattr(core, "mongo", FakeMongo())

    assert InProcessBotService().health_snapshot() == {
        "status": "alive",
        "discord_gateway": "connected",
        "mongodb": "connected",
        "guild_count": 2,
    }


def test_health_snapshot_keeps_liveness_when_dependencies_are_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False, guilds=[1]))
    monkeypatch.setattr(core, "mongo", FakeMongo(error=RuntimeError("database down")))

    assert InProcessBotService().health_snapshot() == {
        "status": "alive",
        "discord_gateway": "disconnected",
        "mongodb": "unreachable",
        "guild_count": 0,
    }


def test_health_snapshot_handles_uninitialized_runtime(monkeypatch):
    monkeypatch.setattr(core, "bot", None)
    monkeypatch.setattr(core, "mongo", None)

    assert InProcessBotService().health_snapshot() == {
        "status": "alive",
        "discord_gateway": "disconnected",
        "mongodb": "unreachable",
        "guild_count": 0,
    }


def test_guild_presence_reports_ready_and_present_guild(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(guilds=[1, 2]))
    monkeypatch.setattr(core.bot, "get_guild", lambda guild_id: object() if guild_id == 123 else None, raising=False)

    assert InProcessBotService().guild_presence("123") == {
        "bot_ready": True,
        "guild_present": True,
    }
    assert InProcessBotService().guild_presence("456") == {
        "bot_ready": True,
        "guild_present": False,
    }


def test_guild_presence_fails_closed_when_bot_is_not_ready_or_uninitialized(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False, guilds=[1]))
    assert InProcessBotService().guild_presence("123") == {
        "bot_ready": False,
        "guild_present": False,
    }

    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().guild_presence("123") == {
        "bot_ready": False,
        "guild_present": False,
    }


def test_available_guild_ids_filters_candidates_to_connected_bot(monkeypatch):
    class Guild:
        def __init__(self, guild_id):
            self.id = guild_id

    class ReadyBot:
        def is_ready(self):
            return True

        guilds = [Guild(123), Guild(456)]

    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().available_guild_ids(["456", "789", "123"]) == ["456", "123"]


def test_available_guild_ids_returns_empty_when_bot_is_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False, guilds=[1, 2]))
    assert InProcessBotService().available_guild_ids(["1", "2"]) == []

    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().available_guild_ids(["1", "2"]) == []


def test_is_guild_owner_checks_current_guild_owner(monkeypatch):
    class Guild:
        owner_id = 42

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, guild_id):
            return Guild() if guild_id == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    service = InProcessBotService()
    assert service.is_guild_owner("123", "42") is True
    assert service.is_guild_owner("123", "43") is False
    assert service.is_guild_owner("456", "42") is False


def test_is_guild_owner_fails_closed_when_bot_is_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False, guilds=[]))
    assert InProcessBotService().is_guild_owner("123", "42") is False
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().is_guild_owner("123", "42") is False



def test_public_status_snapshot_is_transport_neutral_and_handles_missing_runtime(monkeypatch):
    class StatusBot:
        def is_ready(self):
            return True

        guilds = [object(), object(), object()]
        start_time = __import__("time").time() - 3700
        latency = 0.125

    monkeypatch.setattr(core, "bot", StatusBot())
    snapshot = InProcessBotService().public_status_snapshot()
    assert snapshot["online"] is True
    assert snapshot["guild_count"] == 3
    assert snapshot["latency_ms"] == 125
    assert snapshot["uptime"].startswith("1h")

    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().public_status_snapshot() == {
        "online": False, "guild_count": 0, "latency_ms": "—", "uptime": "unknown"
    }


def test_public_status_snapshot_sanitizes_non_finite_latency(monkeypatch):
    class StatusBot:
        def is_ready(self):
            return True

        guilds = []
        start_time = 0
        latency = float("nan")

    monkeypatch.setattr(core, "bot", StatusBot())
    assert InProcessBotService().public_status_snapshot()["latency_ms"] == "—"


def test_guild_name_returns_name_for_ready_guild(monkeypatch):
    class Guild:
        name = "Test server"

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, guild_id):
            return Guild() if guild_id == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    service = InProcessBotService()
    assert service.guild_name("123") == "Test server"
    assert service.guild_name("456") == "the server"


def test_guild_name_falls_back_when_bot_is_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().guild_name("123") == "the server"
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().guild_name("123") == "the server"


def test_api_health_snapshot_for_master_key(monkeypatch):
    class HealthBot:
        def is_ready(self):
            return True

        guilds = [object(), object(), object()]
        locked_guilds = {123}

    monkeypatch.setattr(core, "bot", HealthBot())
    assert InProcessBotService().api_health_snapshot() == {
        "online": True, "locked": True, "guild_count": 3
    }


def test_api_health_snapshot_for_guild_key(monkeypatch):
    class HealthBot:
        def is_ready(self):
            return False

        guilds = [object(), object()]
        locked_guilds = {123}

    monkeypatch.setattr(core, "bot", HealthBot())
    assert InProcessBotService().api_health_snapshot("123") == {
        "online": False, "locked": True, "guild_count": 1
    }
    assert InProcessBotService().api_health_snapshot("456") == {
        "online": False, "locked": False, "guild_count": 1
    }


def test_api_health_snapshot_handles_uninitialized_bot(monkeypatch):
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_health_snapshot() == {
        "online": False, "locked": False, "guild_count": 0
    }
    assert InProcessBotService().api_health_snapshot("123") == {
        "online": False, "locked": False, "guild_count": 1
    }


def test_api_guild_list_snapshot_returns_serializable_guild_fields(monkeypatch):
    class Icon:
        url = "https://cdn.example/icon.png"

    class Guild:
        def __init__(self, gid, name, members, icon=None):
            self.id = gid
            self.name = name
            self.member_count = members
            self.icon = icon

    guild_a = Guild(123, "Alpha", 10, Icon())
    guild_b = Guild(456, "Beta", 20)

    class ReadyBot:
        def is_ready(self):
            return True

        guilds = [guild_a, guild_b]

        def get_guild(self, guild_id):
            return next((g for g in self.guilds if g.id == guild_id), None)

    monkeypatch.setattr(core, "bot", ReadyBot())
    service = InProcessBotService()
    assert service.api_guild_list_snapshot() == [
        {"id": "123", "name": "Alpha", "member_count": 10, "icon_url": "https://cdn.example/icon.png"},
        {"id": "456", "name": "Beta", "member_count": 20, "icon_url": None},
    ]
    assert service.api_guild_list_snapshot("123") == [
        {"id": "123", "name": "Alpha", "member_count": 10, "icon_url": "https://cdn.example/icon.png"}
    ]
    assert service.api_guild_list_snapshot("999") == []


def test_api_guild_list_snapshot_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_guild_list_snapshot() is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_guild_list_snapshot("123") is None


def test_api_text_channels_snapshot(monkeypatch):
    class Channel:
        def __init__(self, cid, name):
            self.id, self.name = cid, name

    class Guild:
        text_channels = [Channel(5, "general"), Channel(6, "staff")]

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return Guild() if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    service = InProcessBotService()
    assert service.api_text_channels_snapshot("123") == [
        {"id": "5", "name": "general"}, {"id": "6", "name": "staff"}
    ]
    assert service.api_text_channels_snapshot("456") == []


def test_api_roles_snapshot_excludes_default_role(monkeypatch):
    class Role:
        def __init__(self, rid, name, default):
            self.id, self.name, self.default = rid, name, default

        def is_default(self):
            return self.default

    class Guild:
        roles = [Role(123, "@everyone", True), Role(9, "Mods", False)]

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return Guild() if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    service = InProcessBotService()
    assert service.api_roles_snapshot("123") == [{"id": "9", "name": "Mods"}]
    assert service.api_roles_snapshot("456") == []


def test_api_channel_and_role_snapshots_return_none_when_offline(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    service = InProcessBotService()
    assert service.api_text_channels_snapshot("123") is None
    assert service.api_roles_snapshot("123") is None
    monkeypatch.setattr(core, "bot", None)
    assert service.api_text_channels_snapshot("123") is None
    assert service.api_roles_snapshot("123") is None


def test_api_members_snapshot_filters_bots_and_serializes_fields(monkeypatch):
    class Avatar:
        url = "https://cdn.example/avatar.png"

    class Member:
        def __init__(self, uid, name, bot=False, muted=False):
            self.id = uid
            self.display_name = name
            self.bot = bot
            self.avatar = Avatar()
            self.muted = muted

        def __str__(self):
            return "user-" + self.display_name.lower()

        @property
        def display_avatar(self):
            return self.avatar

        def is_timed_out(self):
            return self.muted

    class Guild:
        members = [Member(10, "Alice", muted=True), Member(11, "Nexus", bot=True)]

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return Guild() if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().api_members_snapshot("123") == [
        {"id": "10", "name": "Alice", "username": "user-alice",
         "avatar_url": "https://cdn.example/avatar.png", "is_muted": True}
    ]
    assert InProcessBotService().api_members_snapshot("456") == []


def test_api_members_snapshot_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_members_snapshot("123") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_members_snapshot("123") is None


def test_set_guild_locked_adds_and_removes_ids(monkeypatch):
    class LockBot:
        def is_ready(self):
            return True

        locked_guilds = set()

    bot = LockBot()
    monkeypatch.setattr(core, "bot", bot)
    service = InProcessBotService()
    assert service.set_guild_locked("123", True) is True
    assert bot.locked_guilds == {123}
    assert service.set_guild_locked("123", False) is True
    assert bot.locked_guilds == set()
    assert service.set_guild_locked("456", False) is True


def test_set_guild_locked_fails_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().set_guild_locked("123", True) is False
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().set_guild_locked("123", False) is False


def test_verification_challenge_operations_are_exposed_by_bot_service(monkeypatch):
    import web.in_process_bot_service as service_module

    challenge = {"token": "verify-token", "guild_id": "123", "user_id": "456"}
    calls = []
    monkeypatch.setattr(service_module, "get_valid_challenge", lambda token: challenge if token == "verify-token" else None)
    monkeypatch.setattr(service_module, "record_failed_attempt", lambda token: calls.append(("failed", token)) or 2)
    monkeypatch.setattr(service_module, "mark_used", lambda token: calls.append(("used", token)) or True)

    service = InProcessBotService()
    assert service.verify_get_valid_challenge("verify-token") == challenge
    assert service.verify_get_valid_challenge("invalid") is None
    assert service.verify_record_failed_attempt("verify-token") == 2
    assert service.verify_mark_used("verify-token") is True
    assert calls == [("failed", "verify-token"), ("used", "verify-token")]


def test_api_config_snapshot_and_updates_use_config_store(monkeypatch):
    import web.in_process_bot_service as service_module

    calls = []
    monkeypatch.setattr(service_module, "get_config", lambda guild_id: {"guild_id": guild_id, "api_key": "secret"})

    def fake_update_fields(guild_id, updates):
        calls.append((guild_id, updates))

    monkeypatch.setattr(service_module, "update_config_fields", fake_update_fields)
    service = InProcessBotService()
    assert service.api_config_snapshot("123") == {"guild_id": "123", "api_key": "secret"}
    updates = {"language": "fr", "autorole": 9}
    service.api_update_config("123", updates)
    assert calls == [("123", updates)]


def test_api_guild_stats_snapshot_serializes_guild_metrics(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        def __init__(self, timed_out=False):
            self.timed_out = timed_out

        def is_timed_out(self):
            return self.timed_out

    class Guild:
        id = 123
        name = "Alpha"
        member_count = 25
        members = [Member(True), Member(False)]
        locked_channels = []
        channels = []

        async def bans(self):
            yield object()
            yield object()

        def get_channel(self, channel_id):
            return None

    guild = Guild()

    class ReadyBot:
        def is_ready(self):
            return True

        locked_guilds = {123}

        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(
        service_module,
        "get_dashboard_warning_counts",
        lambda guild_id, since: {"warned_count": 1, "warns_today": 2},
    )
    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(service_module, "get_sanction_stats", lambda guild_id, days: [(str(i), i) for i in range(days)])
    snapshot = InProcessBotService().api_guild_stats_snapshot("123")
    assert snapshot == {
        "guild_id": "123", "name": "Alpha", "member_count": 25,
        "ban_count": 2, "muted_count": 1, "open_tickets_count": 0,
        "warned_count": 1, "warns_today": 2, "activity_7d": [0, 1, 2, 3, 4, 5, 6],
        "locked": True,
    }
    assert InProcessBotService().api_guild_stats_snapshot("456") == {}


def test_api_guild_stats_snapshot_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_guild_stats_snapshot("123") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_guild_stats_snapshot("123") is None


def test_api_unmute_member_uses_service_and_logs(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        def __init__(self):
            self.calls = []

        async def timeout(self, duration):
            self.calls.append(duration)

    member = Member()
    logged = []

    class Guild:
        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    async def fake_log_unmute(g, m):
        logged.append((g, m))
    monkeypatch.setattr(service_module, "log_unmute", fake_log_unmute)
    assert InProcessBotService().api_unmute_member("123", "10") == (True, None)
    assert member.calls == [None]
    assert logged == [(guild, member)]


def test_api_unmute_member_reports_missing_member_or_guild(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Guild:
        def get_member(self, uid):
            return None

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return Guild() if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    assert InProcessBotService().api_unmute_member("123", "10") == (False, "Member not found")
    assert InProcessBotService().api_unmute_member("456", "10") == (False, "Guild not found")


def test_api_unmute_member_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_unmute_member("123", "10") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_unmute_member("123", "10") is None


def test_api_broadcast_service_sends_embed_and_mention(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content=None, embed=None):
            self.sent.append((content, embed))

    class Role:
        name = "Mods"
        mention = "<@&9>"

    channel = Channel()

    class Guild:
        roles = [Role()]

        def get_channel(self, cid):
            return channel if cid == 5 else None

    guild = Guild()

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    assert InProcessBotService().api_broadcast("123", "5", "Hello staff", "Mods") == (True, None)
    content, embed = channel.sent[0]
    assert content == "<@&9>"
    assert embed.description == "Hello staff"
    assert embed.footer.text == "📢 Sent from the mobile app"


def test_api_broadcast_service_returns_not_found_for_missing_channel_or_guild(monkeypatch):
    class Guild:
        roles = []

        def get_channel(self, cid):
            return None

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return Guild() if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    service = InProcessBotService()
    assert service.api_broadcast("123", "5", "hi") == (False, "Channel not found")
    assert service.api_broadcast("456", "5", "hi") == (False, "Guild not found")


def test_api_broadcast_service_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_broadcast("123", "5", "hi") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_broadcast("123", "5", "hi") is None


def test_api_mute_member_applies_timeout_and_logs(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        top_role = 2

        def __init__(self):
            self.calls = []

        async def timeout(self, duration, reason=None):
            self.calls.append((duration, reason))

    member = Member()
    sanctions = []
    muted_logs = []

    class Guild:
        me = type("BotMember", (), {"top_role": 5})()

        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(service_module, "log_sanction", lambda *args: sanctions.append(args))
    async def fake_log_mute(g, m, minutes, reason):
        muted_logs.append((g, m, minutes, reason))
    monkeypatch.setattr(service_module, "log_mute", fake_log_mute)

    import datetime
    assert InProcessBotService().api_mute_member("123", "10", 30, "test reason") == (True, None)
    assert member.calls == [(datetime.timedelta(minutes=30), "test reason")]
    assert sanctions == [("123", "10", "mute", "test reason (30 min)", "mobile_app")]
    assert muted_logs == [(guild, member, 30, "test reason")]


def test_api_mute_member_checks_member_and_role_hierarchy(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        top_role = 9

        async def timeout(self, duration, reason=None):
            raise AssertionError("too-high member must not be timed out")

    member = Member()
    class Guild:
        me = type("BotMember", (), {"top_role": 5})()
        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    assert InProcessBotService().api_mute_member("123", "99", 10, "x") == (False, "Member not found")
    assert InProcessBotService().api_mute_member("123", "10", 10, "x") == (False, "Role too high")


def test_api_mute_member_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_mute_member("123", "10", 10, "x") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_mute_member("123", "10", 10, "x") is None


def test_api_ban_member_applies_ban_and_logs(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        top_role = 2

        def __init__(self):
            self.calls = []

        async def ban(self, reason=None):
            self.calls.append(reason)

    member = Member()
    sanctions = []
    ban_logs = []

    class Guild:
        me = type("BotMember", (), {"top_role": 5})()

        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(service_module, "log_sanction", lambda *args: sanctions.append(args))
    async def fake_log_ban(g, m, reason):
        ban_logs.append((g, m, reason))
    monkeypatch.setattr(service_module, "log_ban", fake_log_ban)

    assert InProcessBotService().api_ban_member("123", "10", "bad behavior") == (True, None)
    assert member.calls == ["bad behavior"]
    assert sanctions == [("123", "10", "ban", "bad behavior", "mobile_app")]
    assert ban_logs == [(guild, member, "bad behavior")]


def test_api_ban_member_checks_member_and_role_hierarchy(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        top_role = 10

        async def ban(self, reason=None):
            raise AssertionError("too-high member must not be banned")

    member = Member()
    class Guild:
        me = type("BotMember", (), {"top_role": 5})()
        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    assert InProcessBotService().api_ban_member("123", "99", "x") == (False, "Member not found")
    assert InProcessBotService().api_ban_member("123", "10", "x") == (False, "Role too high")


def test_api_ban_member_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_ban_member("123", "10", "x") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_ban_member("123", "10", "x") is None


def test_api_kick_member_applies_kick_and_logs(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        top_role = 2

        def __init__(self):
            self.calls = []

        async def kick(self, reason=None):
            self.calls.append(reason)

    member = Member()
    sanctions = []
    kick_logs = []

    class Guild:
        me = type("BotMember", (), {"top_role": 5})()

        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(service_module, "log_sanction", lambda *args: sanctions.append(args))
    async def fake_log_kick(g, m, reason):
        kick_logs.append((g, m, reason))
    monkeypatch.setattr(service_module, "log_kick", fake_log_kick)

    assert InProcessBotService().api_kick_member("123", "10", "bad behavior") == (True, None)
    assert member.calls == ["bad behavior"]
    assert sanctions == [("123", "10", "kick", "bad behavior", "mobile_app")]
    assert kick_logs == [(guild, member, "bad behavior")]


def test_api_kick_member_checks_member_and_role_hierarchy(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        top_role = 10

        async def kick(self, reason=None):
            raise AssertionError("too-high member must not be kicked")

    member = Member()
    class Guild:
        me = type("BotMember", (), {"top_role": 5})()
        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    assert InProcessBotService().api_kick_member("123", "99", "x") == (False, "Member not found")
    assert InProcessBotService().api_kick_member("123", "10", "x") == (False, "Role too high")


def test_api_kick_member_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_kick_member("123", "10", "x") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_kick_member("123", "10", "x") is None


def test_api_warn_member_records_warning_and_attempts_discord_log(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Member:
        pass

    member = Member()
    records = []
    logs = []

    class Guild:
        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "get_warns", lambda gid, uid: 3)
    monkeypatch.setattr(service_module, "set_warns", lambda gid, uid, count: records.append((gid, uid, count)))
    monkeypatch.setattr(service_module, "log_sanction", lambda *args: records.append(args))
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    async def fake_log_warn(g, m, reason, count):
        logs.append((g, m, reason, count))
    monkeypatch.setattr(service_module, "log_warn", fake_log_warn)

    assert InProcessBotService().api_warn_member("123", "10", "spam") == (True, 4)
    assert records == [("123", "10", 4), ("123", "10", "warn", "spam", "mobile_app")]
    assert logs == [(guild, member, "spam", 4)]


def test_api_warn_member_handles_missing_member_and_guild(monkeypatch):
    class Guild:
        def get_member(self, uid):
            return None

    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return Guild() if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().api_warn_member("123", "10", "x") == (False, "Member not found")
    assert InProcessBotService().api_warn_member("456", "10", "x") == (False, "Guild not found")


def test_api_warn_member_returns_none_when_bot_unavailable(monkeypatch):
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_warn_member("123", "10", "x") is None
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_warn_member("123", "10", "x") is None


def test_api_unwarn_member_decrements_and_best_effort_logs(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    warnings = {"count": 2}
    records = []
    logs = []

    class Member:
        pass

    member = Member()
    class Guild:
        def get_member(self, uid):
            return member if uid == 10 else None

    guild = Guild()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "get_warns", lambda gid, uid: warnings["count"])
    def fake_set_warns(gid, uid, count):
        warnings["count"] = count
        records.append((gid, uid, count))
    monkeypatch.setattr(service_module, "set_warns", fake_set_warns)
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    async def fake_log_unwarn(g, m, count):
        logs.append((g, m, count))
    monkeypatch.setattr(service_module, "log_unwarn", fake_log_unwarn)

    assert InProcessBotService().api_unwarn_member("123", "10") == (True, 1)
    assert warnings["count"] == 1
    assert records == [("123", "10", 1)]
    assert logs == [(guild, member, 1)]


def test_api_unwarn_member_handles_zero_and_bot_offline(monkeypatch):
    import web.in_process_bot_service as service_module

    monkeypatch.setattr(service_module, "get_warns", lambda gid, uid: 0)
    assert InProcessBotService().api_unwarn_member("123", "10") == (False, "No warnings to remove")
    monkeypatch.setattr(service_module, "get_warns", lambda gid, uid: 1)
    records = []
    monkeypatch.setattr(service_module, "set_warns", lambda gid, uid, count: records.append(count))
    monkeypatch.setattr(core, "bot", FakeBot(ready=False))
    assert InProcessBotService().api_unwarn_member("123", "10") == (True, 0)
    assert records == [0]
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_unwarn_member("123", "10") == (True, 0)



def test_api_accept_appeal_reverses_and_best_effort_dms(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    class Guild:
        name = "Test server"

    guild = Guild()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return guild if gid == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    calls = []
    async def fake_reverse(g, sanction_type, user_id):
        calls.append(("reverse", g, sanction_type, user_id))
        return "restored"
    async def fake_dm(user_id, guild_name, case_id, accepted, result):
        calls.append(("dm", user_id, guild_name, case_id, accepted, result))
    monkeypatch.setattr(service_module, "reverse_sanction", fake_reverse)
    monkeypatch.setattr(service_module, "_dm_user_appeal_result", fake_dm)
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))

    assert InProcessBotService().api_accept_appeal("123", "10", "ban") == (True, "restored")
    assert calls == [("reverse", guild, "ban", "10")]


def test_api_accept_appeal_handles_missing_runtime_and_guild(monkeypatch):
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_accept_appeal("123", "10", "ban") == (False, "Bot not ready")
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return None
    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().api_accept_appeal("123", "10", "ban") == (False, "Guild not found")


def test_api_accept_appeal_returns_failure_when_sanction_reversal_fails(monkeypatch):
    import web.in_process_bot_service as service_module
    class Guild:
        name = "Test"
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return Guild()
    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "reverse_sanction", lambda *args: (_ for _ in ()).throw(RuntimeError("failure")))
    assert InProcessBotService().api_accept_appeal("123", "10", "ban") == (False, "Unable to resolve appeal")


def test_api_deny_appeal_best_effort_dm(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module
    class Guild:
        name = "Server"
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, gid):
            return Guild()
    monkeypatch.setattr(core, "bot", ReadyBot())
    calls = []
    async def fake_dm(*args):
        calls.append(args)
    monkeypatch.setattr(service_module, "_dm_user_appeal_result", fake_dm)
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    assert InProcessBotService().api_notify_appeal_result("123", "10", "5", False) == (True, None)
    assert calls == [(10, "Server", "5", False, None)]



def test_api_guild_id_candidates_returns_string_ids_for_ready_bot(monkeypatch):
    class Guild:
        def __init__(self, guild_id):
            self.id = guild_id

    guilds = [Guild(123), Guild(456)]
    class ReadyBot:
        def is_ready(self):
            return True
        @property
        def guilds(self):
            return guilds
    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().api_guild_id_candidates() == ["123", "456"]


def test_api_guild_id_candidates_fails_closed_when_unavailable(monkeypatch):
    class OfflineBot:
        def is_ready(self):
            return False
    monkeypatch.setattr(core, "bot", OfflineBot())
    assert InProcessBotService().api_guild_id_candidates() == []
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_guild_id_candidates() == []

def test_set_guild_locked_updates_runtime_state(monkeypatch):
    class LockingBot(FakeBot):
        def __init__(self, ready=True):
            super().__init__(ready=ready)
            self.locked_guilds = set()

    bot = LockingBot()
    monkeypatch.setattr(core, "bot", bot)
    service = InProcessBotService()

    assert service.set_guild_locked("123", True) is True
    assert bot.locked_guilds == {123}
    assert service.api_health_snapshot("123")["locked"] is True

    assert service.set_guild_locked("123", False) is True
    assert bot.locked_guilds == set()
    assert service.api_health_snapshot("123")["locked"] is False


def test_set_guild_locked_fails_closed_when_runtime_is_unavailable(monkeypatch):
    bot = FakeBot(ready=False)
    monkeypatch.setattr(core, "bot", bot)
    assert InProcessBotService().set_guild_locked("123", True) is False

    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().set_guild_locked("123", True) is False


def test_api_announce_calendar_event_sends_role_limited_announcement(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module
    from web.in_process_bot_service import InProcessBotService

    sent = []
    class Role:
        id = 7
        mention = "<@&7>"
        def is_default(self):
            return False
    class Channel:
        async def send(self, **kwargs):
            sent.append(kwargs)
    class Guild:
        def get_channel(self, channel_id):
            return Channel() if channel_id == 8 else None
        def get_role(self, role_id):
            return Role() if role_id == 7 else None
    guild = Guild()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, guild_id):
            return guild if guild_id == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr("cogs.calendar.make_embed", lambda event: "calendar-embed")
    event = {"role_id": "7", "title": "Maintenance"}
    assert InProcessBotService().api_announce_calendar_event("123", "8", event) == (True, None)
    assert sent[0]["content"] == "<@&7>"
    assert sent[0]["embed"] == "calendar-embed"
    assert [role.id for role in sent[0]["allowed_mentions"].roles] == [7]


def test_api_announce_calendar_event_fails_closed_for_missing_runtime_targets(monkeypatch):
    from web.in_process_bot_service import InProcessBotService

    service = InProcessBotService()
    monkeypatch.setattr(core, "bot", None)
    assert service.api_announce_calendar_event("123", "8", {"role_id": "7"}) == (False, "Bot not ready")

    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, guild_id):
            return None
    monkeypatch.setattr(core, "bot", ReadyBot())
    assert service.api_announce_calendar_event("123", "8", {"role_id": "7"}) == (False, "Guild not found")


def test_api_grant_verified_applies_verification_through_service(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module
    import verification

    guild = object()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, guild_id):
            return guild if guild_id == 123 else None

    calls = []
    async def fake_grant(target_guild, user_id):
        calls.append((target_guild, user_id))
        return True, None

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(verification, "grant_verified", fake_grant)
    assert InProcessBotService().api_grant_verified("123", "456") == (True, None)
    assert calls == [(guild, "456")]


def test_api_grant_verified_fails_closed_when_runtime_or_guild_is_missing(monkeypatch):
    service = InProcessBotService()
    monkeypatch.setattr(core, "bot", None)
    assert service.api_grant_verified("123", "456") == (False, "Bot not ready")

    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, guild_id):
            return None
    monkeypatch.setattr(core, "bot", ReadyBot())
    assert service.api_grant_verified("123", "456") == (False, "Guild not found")


def test_api_post_appeal_for_review_uses_bot_runtime(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    guild = object()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, guild_id):
            return guild if guild_id == 123 else None

    calls = []
    async def fake_post(target_guild, appeal, appeal_text):
        calls.append((target_guild, appeal, appeal_text))

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(service_module, "post_appeal_for_review", fake_post)
    appeal = {"case_id": 7}
    assert InProcessBotService().api_post_appeal_for_review("123", appeal, "details") == (True, None)
    assert calls == [(guild, appeal, "details")]


def test_api_post_appeal_for_review_fails_closed_without_runtime(monkeypatch):
    service = InProcessBotService()
    monkeypatch.setattr(core, "bot", None)
    assert service.api_post_appeal_for_review("123", {}, "details") == (False, "Bot not ready")


def test_killswitch_owner_notifications_are_delegated_to_runtime(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    calls = []
    async def fake_bruteforce(ip):
        calls.append(("bruteforce", ip))
    async def fake_killswitch(locked):
        calls.append(("killswitch", locked))

    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(service_module, "notify_owner_bruteforce", fake_bruteforce)
    monkeypatch.setattr(service_module, "notify_owner_killswitch", fake_killswitch)
    service = InProcessBotService()
    service.notify_owner_bruteforce("203.0.113.5")
    service.notify_owner_killswitch(True)
    assert calls == [("bruteforce", "203.0.113.5"), ("killswitch", True)]


def test_api_log_dashboard_action_uses_guild_runtime(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    guild = object()
    class ReadyBot:
        def is_ready(self):
            return True
        def get_guild(self, guild_id):
            return guild if guild_id == 123 else None

    calls = []
    async def fake_log(target_guild, actor_name, action, details):
        calls.append((target_guild, actor_name, action, details))

    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(service_module, "log_dashboard_action", fake_log)
    service = InProcessBotService()
    assert service.api_log_dashboard_action("123", "Admin", "Updated settings", "x") == (True, None)
    assert calls == [(guild, "Admin", "Updated settings", "x")]


def test_api_log_dashboard_action_fails_closed_when_runtime_missing(monkeypatch):
    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().api_log_dashboard_action("123", "Admin", "Action") == (False, "Bot not ready")


def test_dashboard_settings_snapshot_serializes_guild_data(monkeypatch):
    class Channel:
        def __init__(self, channel_id, name):
            self.id = channel_id
            self.name = name

    class Role(Channel):
        def __init__(self, role_id, name, default=False):
            super().__init__(role_id, name)
            self.default = default

        def is_default(self):
            return self.default

    class Guild:
        id = 123
        name = "Test server"
        owner_id = 456
        text_channels = [Channel(10, "general")]
        voice_channels = [Channel(11, "voice")]
        roles = [Role(1, "@everyone", True), Role(2, "Moderator")]

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, guild_id):
            return Guild() if guild_id == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().dashboard_settings_snapshot("123") == {
        "id": "123",
        "name": "Test server",
        "owner_id": "456",
        "text_channels": [{"id": "10", "name": "general"}],
        "voice_channels": [{"id": "11", "name": "voice"}],
        "roles": [{"id": "2", "name": "Moderator"}],
    }


def test_dashboard_overview_snapshot_serializes_main_dashboard_data(monkeypatch):
    from types import SimpleNamespace
    from web.in_process_bot_service import InProcessBotService

    class Role:
        def __init__(self, role_id, name, default=False, administrator=False):
            self.id = role_id
            self.name = name
            self.permissions = SimpleNamespace(
                administrator=administrator, manage_guild=False, ban_members=False,
                kick_members=False, manage_roles=False,
            )
            self.default = default

        def is_default(self):
            return self.default

    class Channel:
        def __init__(self, channel_id, name):
            self.id = channel_id
            self.name = name

    class Guild:
        id = 123
        name = "Test server"
        owner_id = 456
        text_channels = [Channel(10, "logs")]
        roles = [Role(1, "@everyone", default=True), Role(2, "Admins", administrator=True)]

        def get_channel(self, channel_id):
            return None

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, guild_id):
            return Guild() if guild_id == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().dashboard_overview_snapshot("123") == {
        "id": "123",
        "name": "Test server",
        "owner_id": "456",
        "text_channels": [{"id": "10", "name": "logs"}],
        "roles": [{
            "id": "2", "name": "Admins",
            "permissions": {
                "administrator": True, "manage_guild": False, "ban_members": False,
                "kick_members": False, "manage_roles": False,
            },
        }],
        "role_names": {"1": "@everyone", "2": "Admins"},
        "open_tickets": [],
    }


def test_dashboard_overview_snapshot_fails_closed_when_runtime_unavailable(monkeypatch):
    from web.in_process_bot_service import InProcessBotService

    monkeypatch.setattr(core, "bot", None)
    assert InProcessBotService().dashboard_overview_snapshot("123") is None


def test_api_handle_github_release_dispatches_and_deduplicates(monkeypatch):
    import asyncio
    import web.in_process_bot_service as service_module

    deliveries = []

    class DeliveryCollection:
        def find_one(self, query):
            return next((item for item in deliveries if item["_id"] == query["_id"]), None)

        def insert_one(self, document):
            deliveries.append(document)

    class FakeDB:
        def __getitem__(self, name):
            assert name == "github_webhook_deliveries"
            return DeliveryCollection()

    calls = []

    class FakeCog:
        async def handle_webhook_release(self, release):
            calls.append(release["tag_name"])
            return True

    class ReadyBot:
        def is_ready(self):
            return True

        def get_cog(self, name):
            assert name == "GitHubUpdates"
            return FakeCog()

    monkeypatch.setattr(core, "db", FakeDB())
    monkeypatch.setattr(core, "bot", ReadyBot())
    monkeypatch.setattr(service_module, "run_coroutine", lambda coroutine: asyncio.run(coroutine))
    service = InProcessBotService()
    release = {"tag_name": "v2.1.0"}

    assert service.api_handle_github_release("delivery-1", release) == "sent"
    assert service.api_handle_github_release("delivery-1", release) == "duplicate"
    assert calls == ["v2.1.0"]
    assert deliveries == [{"_id": "delivery-1"}]


def test_api_handle_github_release_reports_runtime_and_handler_failures(monkeypatch):
    import web.in_process_bot_service as service_module

    service = InProcessBotService()
    monkeypatch.setattr(core, "db", None)
    monkeypatch.setattr(core, "bot", None)
    assert service.api_handle_github_release("delivery-1", {}) == "unavailable"

    class ReadyBot:
        def is_ready(self):
            return True

        def get_cog(self, name):
            return None

    class EmptyDB:
        def __getitem__(self, name):
            assert name == "github_webhook_deliveries"
            return type("EmptyCollection", (), {"find_one": lambda self, query: None})()

    monkeypatch.setattr(core, "db", EmptyDB())
    monkeypatch.setattr(core, "bot", ReadyBot())
    assert service.api_handle_github_release("delivery-1", {}) == "handler_unavailable"


def test_dashboard_roles_snapshot_includes_everyone_and_serializes_roles(monkeypatch):
    class Role:
        def __init__(self, role_id, name):
            self.id = role_id
            self.name = name

    class Guild:
        roles = [Role(1, "@everyone"), Role(2, "Moderator")]

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, guild_id):
            return Guild() if guild_id == 123 else None

    monkeypatch.setattr(core, "bot", ReadyBot())
    assert InProcessBotService().dashboard_roles_snapshot("123") == [
        {"id": "1", "name": "@everyone"},
        {"id": "2", "name": "Moderator"},
    ]


def test_dashboard_roles_snapshot_fails_closed_when_guild_unavailable(monkeypatch):
    service = InProcessBotService()
    monkeypatch.setattr(core, "bot", None)
    assert service.dashboard_roles_snapshot("123") is None


def test_dashboard_settings_snapshot_fails_closed_when_guild_unavailable(monkeypatch):
    service = InProcessBotService()
    monkeypatch.setattr(core, "bot", None)
    assert service.dashboard_settings_snapshot("123") is None

    class ReadyBot:
        def is_ready(self):
            return True

        def get_guild(self, guild_id):
            return None

    monkeypatch.setattr(core, "bot", ReadyBot())
    assert service.dashboard_settings_snapshot("123") is None



def test_get_bot_service_uses_lazy_cached_adapter_factory(monkeypatch):
    import sys
    from types import SimpleNamespace
    import web.bot_service as service_module


    class FakeService:
        instances = 0


        def __init__(self):
            type(self).instances += 1


    monkeypatch.setattr(service_module, "_service_instance", None)
    monkeypatch.setitem(sys.modules, "web.in_process_bot_service", SimpleNamespace(InProcessBotService=FakeService))


    first = service_module.get_bot_service()
    second = service_module.get_bot_service()


    assert isinstance(first, FakeService)
    assert first is second
    assert FakeService.instances == 1



def test_in_process_adapter_implements_every_bot_service_contract_method():
    from web.bot_service import BotService
    from web.in_process_bot_service import InProcessBotService

    interface_methods = {
        name for name, member in BotService.__dict__.items()
        if callable(member) and not name.startswith("__")
    }
    adapter_methods = {
        name for name, member in InProcessBotService.__dict__.items()
        if callable(member) and not name.startswith("__")
    }

    assert interface_methods <= adapter_methods
