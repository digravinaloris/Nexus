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
