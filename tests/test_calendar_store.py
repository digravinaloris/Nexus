"""Tests for shared calendar event persistence helpers."""
import datetime

import mongomock
import pytest

import core
import calendar_store


class FakeBot:
    def __init__(self):
        self.db = mongomock.MongoClient().nexus


@pytest.fixture
def events_store(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(core, "bot", bot)
    return bot.db["calendar_events"]


def test_insert_and_find_duplicate_event(events_store):
    starts_at = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)
    event = {"guild_id": "123", "title": "Launch", "starts_at": starts_at}

    event_id = calendar_store.insert_event(event)

    assert event_id is not None
    assert calendar_store.find_duplicate_event("123", "Launch", starts_at)
    assert not calendar_store.find_duplicate_event("456", "Launch", starts_at)


def test_delete_event_is_scoped_to_guild(events_store):
    from bson import ObjectId

    event_id = ObjectId()
    events_store.insert_one({"_id": event_id, "guild_id": "123"})

    assert not calendar_store.delete_event("456", event_id)
    assert calendar_store.delete_event("123", event_id)
    assert events_store.count_documents({}) == 0


def test_list_upcoming_events_is_sorted_and_limited(events_store):
    now = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)
    events_store.insert_many([
        {"guild_id": "123", "title": "Later", "starts_at": now + datetime.timedelta(days=2)},
        {"guild_id": "123", "title": "Sooner", "starts_at": now + datetime.timedelta(days=1)},
        {"guild_id": "123", "title": "Past", "starts_at": now - datetime.timedelta(days=1)},
        {"guild_id": "456", "title": "Other guild", "starts_at": now + datetime.timedelta(hours=1)},
    ])

    result = calendar_store.list_upcoming_events("123", now, limit=1)

    assert [event["title"] for event in result] == ["Sooner"]


def test_calendar_store_fails_clearly_without_database(monkeypatch):
    monkeypatch.setattr(core, "bot", None)
    with pytest.raises(RuntimeError, match="unavailable"):
        calendar_store.list_upcoming_events("123", datetime.datetime.now(datetime.timezone.utc))
