import template_ops


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

    assert updates == [("automod_raid_window", 10)]



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
