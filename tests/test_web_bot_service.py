"""Tests for the web-to-bot service boundary."""
import core
from web.bot_service import InProcessBotService


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
