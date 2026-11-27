"""Tests des modules extraits de main.py à l'étape 2a :
config_store, sanctions_store, audit, i18n.

Ils tournent sur une fausse base MongoDB en mémoire (mongomock) branchée via
`core.init_db()`, donc ils testent les VRAIES requêtes ($set, $addToSet, $pull,
$inc, tri...) sans aucune connexion réseau.
"""
import ast
import datetime
import os
import sys

import pytest

if os.getenv("CI"):
    import mongomock  # en CI : échec franc si la dépendance manque (pas de skip silencieux)
else:
    mongomock = pytest.importorskip("mongomock")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import audit  # noqa: E402
import config_store  # noqa: E402
import core  # noqa: E402
import sanctions_store  # noqa: E402

UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def mongo_db():
    client = mongomock.MongoClient()
    database = client["discordbot"]
    core.init_db(client, database)
    old_bot = core.bot
    core.bot = None
    yield database
    core.bot = old_bot
    core.reset()


# ---------------------------------------------------------------- hygiène

MODULES = ["core", "config_store", "sanctions_store", "killswitch_store", "faq_store", "audit", "i18n", "utils"]


@pytest.mark.parametrize("module", MODULES)
def test_extracted_modules_never_import_main(module):
    """Sous `python main.py`, le module s'appelle __main__ : un `import main`
    le rechargerait une 2e fois (2e bot, commandes en double)."""
    with open(os.path.join(ROOT, f"{module}.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "main" for a in node.names), module
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "main", module


# ----------------------------------------------------------- config_store

def test_get_config_creates_defaults_once(mongo_db):
    cfg = config_store.get_config(123)
    assert cfg["guild_id"] == "123"
    assert cfg["logs_channel"] == "logs"
    assert cfg["quarantine_hold_minutes"] == 30
    assert cfg["remindme_enabled"] is True
    assert cfg["banned_words"] == []
    assert mongo_db["config"].count_documents({"guild_id": "123"}) == 1
    config_store.get_config(123)  # 2e appel : pas de doublon
    assert mongo_db["config"].count_documents({"guild_id": "123"}) == 1


def test_get_config_is_per_guild(mongo_db):
    config_store.update_config(1, "logs_channel", "mod-logs")
    assert config_store.get_config(1)["logs_channel"] == "mod-logs"
    assert config_store.get_config(2)["logs_channel"] == "logs"


def test_update_config_upserts(mongo_db):
    config_store.update_config(5, "autorole", "999")
    doc = mongo_db["config"].find_one({"guild_id": "5"})
    assert doc["autorole"] == "999"


def test_update_config_fields_applies_all_fields_in_one_update(mongo_db, monkeypatch):
    calls = []
    original_update_one = mongo_db["config"].update_one

    def track_update_one(*args, **kwargs):
        calls.append((args, kwargs))
        return original_update_one(*args, **kwargs)

    monkeypatch.setattr(config_store.col.config, "update_one", track_update_one)
    config_store.update_config_fields(6, {"language": "fr", "autorole": "123"})

    assert len(calls) == 1
    assert calls[0][0][1] == {"$set": {"language": "fr", "autorole": "123"}}
    assert config_store.get_config(6)["language"] == "fr"
    assert config_store.get_config(6)["autorole"] == "123"


def test_command_roles_add_remove(mongo_db):
    config_store.get_config(7)
    config_store.add_command_role(7, "ban", "10")
    config_store.add_command_role(7, "ban", "10")  # $addToSet : pas de doublon
    config_store.add_command_role(7, "ban", "11")
    assert config_store.get_config(7)["command_roles"]["ban"] == ["10", "11"]
    config_store.remove_command_role(7, "ban", "10")
    assert config_store.get_config(7)["command_roles"]["ban"] == ["11"]


def test_set_command_template(mongo_db):
    config_store.get_config(8)
    config_store.set_command_template(8, "trial", ["warn", "mute"])
    assert config_store.get_config(8)["command_templates"]["trial"] == ["warn", "mute"]


def test_global_lock_defaults_to_false_and_toggles():
    assert config_store.get_global_lock() is False
    config_store.set_global_lock(True)
    assert config_store.get_global_lock() is True
    config_store.set_global_lock(False)
    assert config_store.get_global_lock() is False


# -------------------------------------------------------- sanctions_store

def test_warns_default_zero_and_set():
    assert sanctions_store.get_warns(1, 42) == 0
    sanctions_store.set_warns(1, 42, 3)
    assert sanctions_store.get_warns(1, 42) == 3
    assert sanctions_store.get_warns(2, 42) == 0  # autre serveur
    sanctions_store.set_warns(1, 42, 0)
    assert sanctions_store.get_warns(1, 42) == 0

def test_dashboard_warning_counts_are_scoped_and_time_bounded():
    since = datetime.datetime(2026, 11, 27, tzinfo=UTC)
    sanctions_store.set_warns(1, 42, 2)
    sanctions_store.set_warns(1, 43, 0)
    sanctions_store.set_warns(2, 44, 5)
    core.col.sanctions.insert_one({
        "guild_id": "1",
        "type": "warn",
        "timestamp": since + datetime.timedelta(hours=1),
    })
    core.col.sanctions.insert_one({
        "guild_id": "1",
        "type": "ban",
        "timestamp": since + datetime.timedelta(hours=1),
    })
    core.col.sanctions.insert_one({
        "guild_id": "1",
        "type": "warn",
        "timestamp": since - datetime.timedelta(seconds=1),
    })
    core.col.sanctions.insert_one({
        "guild_id": "2",
        "type": "warn",
        "timestamp": since + datetime.timedelta(hours=1),
    })

    assert sanctions_store.get_dashboard_warning_counts(1, since) == {
        "warned_count": 1,
        "warns_today": 1,
    }


def test_locked_channels_lifecycle():
    sanctions_store.mark_channel_locked(1, 100, "general", 7)
    sanctions_store.mark_channel_locked(1, 101, "voice", 7, channel_type="voice")
    locked = sanctions_store.get_locked_channels(1)
    assert {c["channel_id"] for c in locked} == {"100", "101"}
    assert {c["channel_type"] for c in locked} == {"text", "voice"}
    sanctions_store.mark_channel_unlocked(1, 100)
    assert [c["channel_id"] for c in sanctions_store.get_locked_channels(1)] == ["101"]
    assert sanctions_store.get_locked_channels(2) == []


def test_case_ids_increment_per_guild():
    assert sanctions_store.get_next_case_id(1) == 1
    assert sanctions_store.get_next_case_id(1) == 2
    assert sanctions_store.get_next_case_id(2) == 1  # compteur indépendant


def test_log_sanction_and_get_case():
    case_id = sanctions_store.log_sanction(1, 42, "ban", "spam", 7)
    assert case_id == 1
    doc = sanctions_store.get_case(1, case_id)
    assert doc["user_id"] == "42" and doc["type"] == "ban"
    assert doc["reason"] == "spam" and doc["moderator_id"] == "7"
    assert sanctions_store.get_case(2, case_id) is None  # scopé par serveur
    second = sanctions_store.log_sanction(1, 43, "warn", "", "automod")
    assert second == 2
    assert sanctions_store.get_case(1, second)["reason"] == "No reason provided"


def test_sanction_appeals_roundtrip():
    sanctions_store.create_sanction_appeal(1, 5, "ban", 42, "Bob", "spam", "tok123")
    appeal = sanctions_store.get_ban_appeal("tok123")
    assert appeal["status"] == "pending" and appeal["user_name"] == "Bob"
    assert appeal["ban_reason"] == "spam"
    assert appeal["appeal_text"] is None
    assert sanctions_store.get_ban_appeal_by_case(1, 5)["token"] == "tok123"
    assert sanctions_store.get_ban_appeal("nope") is None


def test_sanction_stats_are_zero_filled():
    sanctions_store.log_sanction(1, 42, "warn", "a", 7)
    sanctions_store.log_sanction(1, 43, "ban", "b", 7)
    sanctions_store.log_sanction(2, 44, "warn", "c", 7)  # autre serveur : ignoré
    series = sanctions_store.get_sanction_stats(1, days=7)
    assert len(series) == 7  # un point par jour, même les jours calmes
    assert [v for _, v in series][-1] == 2  # aujourd'hui
    assert sum(v for _, v in series) == 2


# ------------------------------------------------------------------ audit

def test_record_audit_truncates_details(mongo_db):
    audit.record_audit(1, 42, "Bob", "config change", "x" * 900)
    doc = mongo_db["audit_log"].find_one({"guild_id": "1"})
    assert doc["actor_id"] == "42" and doc["actor_name"] == "Bob"
    assert len(doc["details"]) == 500


def test_record_audit_never_raises_when_mongo_not_ready(capsys):
    core.reset()
    audit.record_audit(1, None, "Web", "x")  # ne doit pas lever
    assert "Failed to record audit entry" in capsys.readouterr().out


class FakeBot:
    def get_user(self, user_id):
        return "CachedUser" if user_id == 7 else None


def test_get_audit_log_merges_sources_sorted_desc(mongo_db):
    core.bot = FakeBot()
    now = datetime.datetime.now(UTC)
    mongo_db["audit_log"].insert_one({
        "guild_id": "1", "actor_name": "Admin", "action": "config",
        "details": "d", "timestamp": now - datetime.timedelta(minutes=10)})
    for mod, minutes in (("mobile_app", 5), ("automod", 4), ("7", 3), ("999", 2)):
        mongo_db["sanctions"].insert_one({
            "guild_id": "1", "moderator_id": mod, "type": "mute", "case_id": minutes,
            "reason": "r", "timestamp": now - datetime.timedelta(minutes=minutes)})
    entries = audit.get_audit_log(1, limit=10)
    assert [e["actor_name"] for e in entries] == [
        "Moderator 999", "CachedUser", "🤖 AutoMod", "📱 Mobile App", "Admin"]
    assert entries[0]["action"] == "Mute (case #2)"
    assert len(audit.get_audit_log(1, limit=2)) == 2
    assert audit.get_audit_log(2) == []


def test_sparkline_svg():
    assert audit.build_sparkline_svg([]) == ""
    svg = audit.build_sparkline_svg([("01/01", 0), ("02/01", 5)])
    assert svg.startswith("<svg") and svg.count("<rect") == 2
    assert "02/01: 5" in svg


# ------------------------------------------------------------------- i18n

def test_i18n_loads_both_languages():
    import i18n
    assert set(i18n.TRANSLATIONS) == {"en", "fr"}
    assert i18n.TRANSLATIONS["en"] and i18n.TRANSLATIONS["fr"]


def test_i18n_t_follows_session_language_with_fallbacks():
    flask = pytest.importorskip("flask")
    import i18n
    app = flask.Flask("t")
    app.secret_key = "x"
    en_key = next(k for k, v in i18n.TRANSLATIONS["en"].items() if isinstance(v, str))
    with app.test_request_context():
        assert i18n.current_lang() == "en" and i18n.current_theme() == "dark"
        assert i18n.t(en_key) == i18n.TRANSLATIONS["en"][en_key]
        assert i18n.t("cle_inexistante_xyz") == "cle_inexistante_xyz"
        flask.session["lang"] = "fr"
        flask.session["theme"] = "light"
        assert i18n.current_lang() == "fr" and i18n.current_theme() == "light"
        assert i18n.t(en_key) == i18n.TRANSLATIONS["fr"].get(en_key, i18n.TRANSLATIONS["en"][en_key])



def test_rest_sanction_query_helpers_are_guild_scoped_and_sorted(mongo_db):
    sanctions_store.set_warns(1, 42, 2)
    sanctions_store.set_warns(1, 43, 0)
    sanctions_store.set_warns(2, 44, 5)
    assert [(d["user_id"], d["count"]) for d in sanctions_store.get_warnlist(1)] == [("42", 2)]

    older = sanctions_store.log_sanction(1, 42, "warn", "older", 7)
    newer = sanctions_store.log_sanction(1, 43, "ban", "newer", 7)
    sanctions_store.log_sanction(2, 44, "warn", "other guild", 7)
    mongo_db["sanctions"].update_one({"guild_id": "1", "case_id": older}, {"$set": {"timestamp": datetime.datetime.now(UTC) - datetime.timedelta(days=1)}})
    mongo_db["sanctions"].update_one({"guild_id": "1", "case_id": newer}, {"$set": {"timestamp": datetime.datetime.now(UTC)}})
    assert sanctions_store.get_sanction_cases(1, limit=1)[0]["case_id"] == newer
    assert [c["case_id"] for c in sanctions_store.get_sanction_cases(1, "warn", 10)] == [older]

    sanctions_store.create_sanction_appeal(1, 1, "ban", 42, "Bob", "r", "pending")
    mongo_db["ban_appeals"].update_one({"token": "pending"}, {"$set": {"status": "submitted", "submitted_at": datetime.datetime.now(UTC)}})
    sanctions_store.create_sanction_appeal(1, 2, "ban", 43, "Alice", "r", "other")
    mongo_db["ban_appeals"].update_one({"token": "other"}, {"$set": {"status": "submitted", "submitted_at": datetime.datetime.now(UTC)}})
    sanctions_store.create_sanction_appeal(2, 3, "ban", 44, "Carol", "r", "foreign")
    mongo_db["ban_appeals"].update_one({"token": "foreign"}, {"$set": {"status": "submitted", "submitted_at": datetime.datetime.now(UTC)}})
    assert {a["token"] for a in sanctions_store.get_submitted_appeals(1)} == {"pending", "other"}



def test_get_sanction_types_is_guild_scoped(mongo_db):
    sanctions_store.log_sanction(1, 42, "warn", "warning", 7)
    sanctions_store.log_sanction(1, 43, "ban", "ban", 7)
    sanctions_store.log_sanction(2, 44, "mute", "other guild", 7)
    assert set(sanctions_store.get_sanction_types(1)) == {"warn", "ban"}
    assert set(sanctions_store.get_sanction_types(2)) == {"mute"}



def test_faq_store_add_remove_toggle_lifecycle(mongo_db):
    import faq_store

    assert faq_store.get_faq(1) is None
    faq_store.add_faq_entry(1, {"id": "abc", "keywords": ["help"], "response": "answer"})
    doc = faq_store.get_faq(1)
    assert doc.get("enabled", False) is False and doc["entries"] == [{"id": "abc", "keywords": ["help"], "response": "answer"}]
    faq_store.add_faq_entry(1, {"id": "def", "keywords": ["rules"], "response": "rules answer"})
    assert len(faq_store.get_faq(1)["entries"]) == 2
    assert faq_store.remove_faq_entry(1, "abc") is True
    assert faq_store.remove_faq_entry(1, "missing") is False
    faq_store.set_faq_enabled(1, True)
    faq_store.set_faq_enabled(2, False)
    assert faq_store.get_faq(1)["enabled"] is True
    assert faq_store.get_faq(2)["enabled"] is False
    assert faq_store.get_faq(1)["entries"] == [{"id": "def", "keywords": ["rules"], "response": "rules answer"}]



def test_faq_store_get_or_create_and_save_preserve_enabled(mongo_db):
    import faq_store

    doc = faq_store.get_or_create_faq(5)
    assert {key: doc[key] for key in ("guild_id", "enabled", "entries")} == {"guild_id": "5", "enabled": False, "entries": []}
    faq_store.set_faq_enabled(5, True)
    faq_store.save_faq_entries(5, [{"id": "x", "keywords": ["test"], "response": "answer"}])
    assert faq_store.get_faq(5)["enabled"] is True
    assert faq_store.get_faq(5)["entries"][0]["id"] == "x"
    assert mongo_db["faq"].count_documents({"guild_id": "5"}) == 1



def test_sanctions_store_submits_only_pending_appeals(mongo_db):
    sanctions_store.create_sanction_appeal(1, 7, "ban", 42, "Bob", "reason", "appeal-token")
    submitted_at = datetime.datetime.now(UTC)
    assert sanctions_store.submit_ban_appeal("appeal-token", "please review", submitted_at) is True
    doc = mongo_db["ban_appeals"].find_one({"token": "appeal-token"})
    assert doc["status"] == "submitted"
    assert doc["appeal_text"] == "please review"
    stored_at = doc["submitted_at"].replace(tzinfo=UTC)
    expected_at = submitted_at.replace(microsecond=(submitted_at.microsecond // 1000) * 1000)
    assert stored_at == expected_at
    assert sanctions_store.submit_ban_appeal("appeal-token", "duplicate", submitted_at) is False


def test_killswitch_store_token_lifecycle(mongo_db):
    import killswitch_store

    mongo_db["killswitch_tokens"].insert_one({"token": "one-time", "used": False, "attempts": 0})
    assert killswitch_store.get_killswitch_token("one-time")["attempts"] == 0
    assert killswitch_store.increment_killswitch_attempts("one-time") == 1
    assert killswitch_store.mark_killswitch_token_used("one-time") is True
    assert killswitch_store.mark_killswitch_token_used("one-time") is False
    doc = killswitch_store.get_killswitch_token("one-time")
    assert doc["attempts"] == 1 and doc["used"] is True
    assert killswitch_store.get_killswitch_token("missing") is None


def test_killswitch_store_stops_attempts_at_limit(mongo_db):
    import killswitch_store

    mongo_db["killswitch_tokens"].insert_one({"token": "limited", "used": False, "attempts": 4})
    assert killswitch_store.increment_killswitch_attempts("limited") == 5
    assert killswitch_store.increment_killswitch_attempts("limited") is None
    assert killswitch_store.mark_killswitch_token_used("limited") is False
