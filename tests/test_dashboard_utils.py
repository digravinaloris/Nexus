"""Tests for dashboard form number parsing, independent from Flask routes."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_utils import (
    read_bounded_form_numbers, validate_channel_selection, validate_role_selection,
    is_safe_relative_redirect, build_feature_toggle_updates,
)

FIELDS = [
    ("count", int, 1, 100),
    ("ratio", float, 0.1, 1.0),
]


def test_parses_int_and_decimal_comma():
    assert read_bounded_form_numbers({"count": "8", "ratio": "0,75"}, FIELDS) == {
        "count": 8, "ratio": 0.75
    }


def test_blank_fields_are_omitted():
    assert read_bounded_form_numbers({"count": "", "ratio": "  "}, FIELDS) == {}


def test_values_are_clamped_to_bounds():
    assert read_bounded_form_numbers({"count": "0", "ratio": "2"}, FIELDS) == {
        "count": 1, "ratio": 1.0
    }


def test_invalid_field_identifies_key():
    try:
        read_bounded_form_numbers({"count": "nope", "ratio": "0.5"}, FIELDS)
    except ValueError as exc:
        assert str(exc) == "count"
    else:
        raise AssertionError("Expected invalid numeric input to raise ValueError")



def test_channel_selection_blank_none_and_valid():
    channels = [{"id": "101"}, {"id": 202}]
    assert validate_channel_selection("", channels) is None
    assert validate_channel_selection(" none ", channels) is None
    assert validate_channel_selection("202", channels) == 202


def test_channel_selection_rejects_unknown_and_non_numeric():
    channels = [{"id": "101"}]
    for raw in ("202", "general", "-1"):
        try:
            validate_channel_selection(raw, channels)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected invalid channel: {raw}")


def test_role_selection_dict_rejects_unknown_and_accepts_real_role():
    guild = {"roles": [{"id": "10"}, {"id": "20"}]}
    assert validate_role_selection("20", guild) == 20
    assert validate_role_selection("", guild) is None
    try:
        validate_role_selection("99", guild)
    except ValueError:
        pass
    else:
        raise AssertionError("Expected unknown role to be rejected")


class FakeRole:
    def __init__(self, role_id, is_default=False):
        self.id = role_id
        self._default = is_default

    def is_default(self):
        return self._default


class FakeGuild:
    def __init__(self, roles):
        self.roles = roles

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)


def test_role_selection_object_rejects_default_role():
    guild = FakeGuild([FakeRole(1, is_default=True), FakeRole(2)])
    assert validate_role_selection("2", guild) == 2
    try:
        validate_role_selection("1", guild)
    except ValueError:
        pass
    else:
        raise AssertionError("@everyone/default role must not be selectable")



def test_safe_relative_redirect_accepts_local_paths_and_queries():
    assert is_safe_relative_redirect("/dashboard")
    assert is_safe_relative_redirect("/dashboard/lang/fr?next=%2Fsettings")


def test_safe_relative_redirect_rejects_external_and_ambiguous_targets():
    for target in (
        "https://evil.example", "//evil.example", "/\\\\evil.example",
        "/\\t/evil.example", "/path\\\\evil", "dashboard", "",
    ):
        assert not is_safe_relative_redirect(target), target


def test_safe_relative_redirect_rejects_non_string_values():
    assert not is_safe_relative_redirect(None)
    assert not is_safe_relative_redirect(123)



def test_feature_toggle_updates_write_missing_values_using_effective_defaults():
    toggles = {
        "remindme": {"config_key": "remindme_enabled"},
        "tickets": {"config_key": "tickets_enabled"},
    }
    updates, enabled, disabled = build_feature_toggle_updates(
        toggles, {}, {"tickets"}, {"remindme_enabled": True}
    )
    assert updates == [("remindme_enabled", False), ("tickets_enabled", True)]
    assert enabled == ["tickets"]
    assert disabled == ["remindme"]


def test_feature_toggle_updates_skip_values_already_stored():
    toggles = {"tickets": {"config_key": "tickets_enabled"}}
    updates, enabled, disabled = build_feature_toggle_updates(
        toggles, {"tickets_enabled": True}, {"tickets"}
    )
    assert updates == []
    assert enabled == []
    assert disabled == []


def test_feature_toggle_updates_audit_only_effective_changes():
    toggles = {
        "remindme": {"config_key": "remindme_enabled"},
        "tickets": {"config_key": "tickets_enabled"},
    }
    updates, enabled, disabled = build_feature_toggle_updates(
        toggles,
        {"remindme_enabled": True, "tickets_enabled": False},
        {"tickets"},
        {"remindme_enabled": True},
    )
    assert updates == [("tickets_enabled", True)]
    assert enabled == ["tickets"]
    assert disabled == []


def test_feature_toggle_updates_preserve_explicit_stored_values_over_defaults():
    toggles = {"remindme": {"config_key": "remindme_enabled"}}
    updates, enabled, disabled = build_feature_toggle_updates(
        toggles, {"remindme_enabled": False}, set(), {"remindme_enabled": True}
    )
    assert updates == []
    assert enabled == []
    assert disabled == []
