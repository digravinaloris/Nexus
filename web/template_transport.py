"""Allowlisted transport DTOs for public marketplace templates."""
import datetime
import json
import math
import re


_FEATURE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_COMMAND_KEY = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_CODE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_AUTOMOD_BOUNDS = {
    "automod_spam_count": (1, 1000),
    "automod_spam_window": (1, 3600),
    "automod_caps_ratio": (0.1, 1),
    "automod_raid_count": (2, 1000),
    "automod_raid_window": (2, 3600),
}
_AUTOMOD_KEYS = frozenset({
    "banned_words", "banned_domains", *_AUTOMOD_BOUNDS.keys(),
})


def _portable_snapshot(value):
    if not isinstance(value, dict) or set(value) != {
        "feature_toggles", "automod", "warn_escalation_enabled",
        "command_roles_by_name",
    }:
        raise ValueError("Invalid public template snapshot")
    features = value["feature_toggles"]
    if (
        not isinstance(features, dict) or len(features) > 100
        or any(not isinstance(k, str) or not _FEATURE_KEY.fullmatch(k) or type(v) is not bool
               for k, v in features.items())
    ):
        raise ValueError("Invalid public template feature toggles")
    automod = value["automod"]
    if not isinstance(automod, dict) or not set(automod).issubset(_AUTOMOD_KEYS):
        raise ValueError("Invalid public template automod settings")
    clean_automod = {}
    for key, item in automod.items():
        if key in {"banned_words", "banned_domains"}:
            if (
                item is not None and (
                    not isinstance(item, list) or len(item) > 200
                    or any(not isinstance(entry, str) or len(entry) > 100 for entry in item)
                )
            ):
                raise ValueError("Invalid public template automod list")
        elif item is not None:
            low, high = _AUTOMOD_BOUNDS[key]
            if (
                isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or not low <= item <= high
            ):
                raise ValueError("Invalid public template automod value")
        clean_automod[key] = item
    roles = value["command_roles_by_name"]
    if not isinstance(roles, dict) or len(roles) > 100:
        raise ValueError("Invalid public template role mapping")
    clean_roles = {}
    for command, names in roles.items():
        if (
            not isinstance(command, str) or not _COMMAND_KEY.fullmatch(command)
            or not isinstance(names, list) or len(names) > 50
            or any(not isinstance(name, str) or not 1 <= len(name) <= 100 for name in names)
        ):
            raise ValueError("Invalid public template role mapping")
        clean_roles[command] = list(names)
    snapshot = {
        "feature_toggles": dict(features),
        "automod": clean_automod,
        "warn_escalation_enabled": value["warn_escalation_enabled"],
        "command_roles_by_name": clean_roles,
    }
    if type(snapshot["warn_escalation_enabled"]) is not bool:
        raise ValueError("Invalid public template warning setting")
    if len(json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 32 * 1024:
        raise ValueError("Public template snapshot exceeded size limit")
    return snapshot


def project_public_template(document):
    """Project one public template to a bounded, creator-free DTO."""
    if not isinstance(document, dict) or document.get("public") is not True:
        raise ValueError("Template is not public")
    code = document.get("code")
    name = document.get("name")
    guild_name = document.get("created_guild_name")
    created_at = document.get("created_at")
    if (
        not isinstance(code, str) or not _CODE.fullmatch(code)
        or not isinstance(name, str) or not 1 <= len(name) <= 100
        or not isinstance(guild_name, str) or not 1 <= len(guild_name) <= 100
        or not isinstance(created_at, datetime.datetime)
    ):
        raise ValueError("Invalid public template metadata")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    created_at = created_at.astimezone(datetime.timezone.utc)
    result = {
        "code": code,
        "name": name,
        "created_guild_name": guild_name,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "snapshot": _portable_snapshot(document.get("snapshot")),
    }
    if len(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 40 * 1024:
        raise ValueError("Public template response exceeded size limit")
    return result


def validate_public_template_dto(item):
    """Validate the serialized public-template DTO received over signed transport."""
    required = {"code", "name", "created_guild_name", "created_at", "snapshot"}
    if not isinstance(item, dict) or set(item) != required:
        return False
    try:
        if (
            not isinstance(item["code"], str) or not _CODE.fullmatch(item["code"])
            or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 100
            or not isinstance(item["created_guild_name"], str)
            or not 1 <= len(item["created_guild_name"]) <= 100
            or not isinstance(item["created_at"], str)
        ):
            return False
        stamp = datetime.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() != datetime.timedelta(0):
            return False
        snapshot = _portable_snapshot(item["snapshot"])
        normalized = dict(item, snapshot=snapshot)
        return len(json.dumps(normalized, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) <= 40 * 1024
    except (TypeError, ValueError, OverflowError):
        return False
