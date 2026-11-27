import services.template_ops as template_ops


def test_template_apply_updates_only_whitelisted_configuration(monkeypatch):
    updates = []
    monkeypatch.setattr(template_ops, "update_config", lambda guild_id, key, value: updates.append((guild_id, key, value)))

    template_ops._apply_template_base_config("123", {
        "feature_toggles": {
            "antinuke": True,
            "unknown_feature": True,
            "tickets": "yes",
        },
        "automod": {
            "automod_spam_count": 25,
            "banned_words": ["spam"],
            "api_key": "attacker-controlled",
            "owner_id": "999",
            "command_roles": {"ban": [999]},
            "automod_raid_count": None,
        },
        "warn_escalation_enabled": True,
    })

    assert ("123", "antinuke_enabled", True) in updates
    assert ("123", "automod_spam_count", 25) in updates
    assert ("123", "banned_words", ["spam"]) in updates
    assert ("123", "warn_escalation_enabled", True) in updates
    assert not any(key in {"api_key", "owner_id", "command_roles", "unknown_feature"} for _, key, _ in updates)
    assert not any(key == "automod_raid_count" for _, key, _ in updates)


def test_template_apply_ignores_malformed_sections_and_values(monkeypatch):
    updates = []
    monkeypatch.setattr(template_ops, "update_config", lambda guild_id, key, value: updates.append((key, value)))

    template_ops._apply_template_base_config("123", {
        "feature_toggles": ["antinuke"],
        "automod": "not-a-mapping",
        "warn_escalation_enabled": "true",
    })
    assert ("warn_escalation_enabled", "true") not in updates
    updates.clear()
    template_ops._apply_template_base_config("123", {
        "automod": {
            "banned_domains": "example.com",
            "automod_spam_count": True,
            "automod_raid_window": 10,
        }
    })

    assert updates == [("automod_raid_window", 10), ("warn_escalation_enabled", False)]


def test_template_apply_rejects_out_of_range_automod_numbers(monkeypatch):
    updates = []
    monkeypatch.setattr(template_ops, "update_config", lambda guild_id, key, value: updates.append((key, value)))

    template_ops._apply_template_base_config("123", {
        "automod": {
            "automod_spam_count": -1,
            "automod_spam_window": 999999,
            "automod_caps_ratio": 5,
            "automod_raid_count": 1,
            "automod_raid_window": float("inf"),
        }
    })

    assert updates == [("warn_escalation_enabled", False)]



def test_template_snapshot_builds_portable_settings_and_role_names(monkeypatch):
    monkeypatch.setattr(template_ops, "FEATURE_TOGGLES", {
        "tickets": {"config_key": "tickets_enabled"},
        "antinuke": {"config_key": "antinuke_enabled"},
    })

    class Guild:
        roles = {10: type("Role", (), {"name": "Moderators"})(), 11: None, 12: type("Role", (), {"name": "Helpers"})()}

        def get_role(self, role_id):
            return self.roles.get(role_id)

    snapshot = template_ops._build_template_snapshot(Guild(), {
        "tickets_enabled": True,
        "antinuke_enabled": False,
        "banned_words": ["spam"],
        "automod_spam_count": 8,
        "command_roles": {"ban": ["10", "11"], "kick": ["12"], "empty": ["999"]},
        "warn_escalation_enabled": True,
    })

    assert snapshot["feature_toggles"] == {"tickets": True, "antinuke": False}
    assert snapshot["automod"]["banned_words"] == ["spam"]
    assert snapshot["automod"]["automod_spam_count"] == 8
    assert snapshot["command_roles_by_name"] == {"ban": ["Moderators"], "kick": ["Helpers"]}
    assert snapshot["warn_escalation_enabled"] is True


def test_template_snapshot_uses_defaults_for_missing_values(monkeypatch):
    monkeypatch.setattr(template_ops, "FEATURE_TOGGLES", {"tickets": {"config_key": "tickets_enabled"}})

    class Guild:
        def get_role(self, role_id):
            return None

    snapshot = template_ops._build_template_snapshot(Guild(), {})

    assert snapshot["feature_toggles"] == {"tickets": False}
    assert snapshot["warn_escalation_enabled"] is False
    assert snapshot["command_roles_by_name"] == {}
