"""Tests for the in-memory AutoMod and anti-raid checks."""
from types import SimpleNamespace

from services import automod_checks


def test_automod_exempt_checks_permissions_and_configured_roles():
    moderator = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_messages=True),
        roles=[],
    )
    assert automod_checks.is_automod_exempt(moderator, {}) is True

    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_messages=False),
        roles=[SimpleNamespace(id=42)],
    )
    assert automod_checks.is_automod_exempt(member, {"allowed_roles": [42]}) is True
    assert automod_checks.is_automod_exempt(member, {"automod_exempt_role_ids": ["42"]}) is True
    assert automod_checks.is_automod_exempt(member, {}) is False


def test_spam_and_raid_checks_use_configured_thresholds(monkeypatch):
    monkeypatch.setattr(automod_checks.time, "time", lambda: 100.0)
    cfg = {"automod_spam_count": 2, "automod_spam_window": 5}
    assert automod_checks.check_spam(8001, 1, cfg) is False
    assert automod_checks.check_spam(8001, 1, cfg) is False
    assert automod_checks.check_spam(8001, 1, cfg) is True

    raid_cfg = {"automod_raid_count": 2, "automod_raid_window": 10}
    assert automod_checks.check_raid(8002, raid_cfg) is False
    assert automod_checks.check_raid(8002, raid_cfg) is True


def test_media_flood_and_channel_burst_thresholds(monkeypatch):
    monkeypatch.setattr(automod_checks.time, "time", lambda: 200.0)
    media_cfg = {"media_flood_count": 1, "media_flood_window": 5}
    assert automod_checks.check_media_flood(8003, 1, media_cfg) is False
    assert automod_checks.check_media_flood(8003, 1, media_cfg) is True

    burst_cfg = {"auto_slowmode_threshold": 1, "auto_slowmode_window": 5}
    assert automod_checks.check_channel_burst(8004, burst_cfg) is False
    assert automod_checks.check_channel_burst(8004, burst_cfg) is True


def test_join_name_pattern_and_invite_detection(monkeypatch):
    monkeypatch.setattr(automod_checks.time, "time", lambda: 300.0)
    guild_id = 8005
    assert automod_checks.record_join_name_and_check_pattern(guild_id, "raiduser-one") is False
    assert automod_checks.record_join_name_and_check_pattern(guild_id, "raiduser-two") is False
    assert automod_checks.record_join_name_and_check_pattern(guild_id, "raiduser-three") is True

    assert automod_checks.check_invite_link("Join discord.gg/example") is True
    assert automod_checks.check_invite_link("No invite here") is False
