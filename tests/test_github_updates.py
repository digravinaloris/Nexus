import datetime as dt

import pytest

pytest.importorskip("discord")

from cogs import github_updates


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 404:
            raise RuntimeError("HTTP error")

    def json(self):
        return self.payload


def sample_release(**overrides):
    release = {
        "id": 123,
        "tag_name": "v2.0.0",
        "name": "Nexus 2.0",
        "html_url": "https://github.com/digravinaloris/Nexus/releases/tag/v2.0.0",
        "body": "New features and fixes.",
        "published_at": "2026-11-24T12:30:00Z",
        "draft": False,
        "prerelease": False,
    }
    release.update(overrides)
    return release


def test_build_release_embed_uses_release_metadata():
    embed = github_updates.build_release_embed(sample_release())

    assert embed.title == "🚀 Nexus 2.0"
    assert embed.url.endswith("/v2.0.0")
    assert embed.description == "New features and fixes."
    assert embed.fields[0].value == "v2.0.0"
    assert embed.timestamp == dt.datetime(2026, 11, 24, 12, 30, tzinfo=dt.timezone.utc)


def test_build_release_embed_handles_empty_and_long_notes():
    empty = github_updates.build_release_embed(sample_release(body=""))
    long = github_updates.build_release_embed(sample_release(body="x" * 5000, name="N" * 400))

    assert empty.description == "A new Nexus version is available."
    assert len(long.description) == 4000
    assert len(long.title) <= 256
    assert long.description.endswith("...")


def test_fetch_latest_release_uses_fixed_github_endpoint(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return FakeResponse(payload=sample_release())

    monkeypatch.setattr(github_updates.requests, "get", fake_get)
    release = github_updates.fetch_latest_release()

    assert release["id"] == 123
    assert seen["url"] == github_updates.RELEASE_API
    assert seen["kwargs"]["timeout"] == 10
    assert seen["kwargs"]["headers"]["User-Agent"] == "Nexus-Discord-Bot"


def test_fetch_latest_release_returns_none_when_no_release_exists(monkeypatch):
    monkeypatch.setattr(github_updates.requests, "get", lambda *a, **k: FakeResponse(status_code=404))
    assert github_updates.fetch_latest_release() is None


@pytest.mark.parametrize("flags", [{"draft": True}, {"prerelease": True}])
def test_fetch_latest_release_ignores_drafts_and_prereleases(monkeypatch, flags):
    monkeypatch.setattr(
        github_updates.requests,
        "get",
        lambda *a, **k: FakeResponse(payload=sample_release(**flags)),
    )
    assert github_updates.fetch_latest_release() is None


def test_fetch_latest_release_rejects_incomplete_github_payload(monkeypatch):
    monkeypatch.setattr(
        github_updates.requests,
        "get",
        lambda *a, **k: FakeResponse(payload={"id": 1}),
    )
    with pytest.raises(ValueError, match="missing required fields"):
        github_updates.fetch_latest_release()
