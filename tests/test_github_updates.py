import asyncio

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


def test_build_release_message_contains_plain_text_details():
    message = github_updates.build_release_message(sample_release())

    assert message == (
        "📢 New Nexus release!\nNexus 2.0\nVersion: v2.0.0\n\n"
        "New features and fixes.\n\n"
        "https://github.com/digravinaloris/Nexus/releases/tag/v2.0.0"
    )
    assert "**" not in message
    assert "`" not in message


def test_build_release_message_handles_empty_and_long_notes():
    empty = github_updates.build_release_message(sample_release(body=""))
    long = github_updates.build_release_message(sample_release(body="x" * 5000, name="N" * 400))

    assert "No release notes provided." in empty
    assert len(long) <= github_updates.MAX_DISCORD_MESSAGE_LENGTH
    assert long.endswith(sample_release()["html_url"])
    assert "Version: v2.0.0" in long
    assert long.endswith("..." + "\n\n" + sample_release()["html_url"])


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


def test_fetch_latest_release_rejects_external_release_url(monkeypatch):
    monkeypatch.setattr(
        github_updates.requests,
        "get",
        lambda *a, **k: FakeResponse(payload=sample_release(html_url="https://example.com/fake-release")),
    )
    with pytest.raises(ValueError, match="URL is invalid"):
        github_updates.fetch_latest_release()


def test_post_release_sends_plain_text_without_embed_and_records_state(monkeypatch):
    class FakeTextChannel:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class FakeCollection:
        def __init__(self):
            self.updates = []

        def update_one(self, query, update):
            self.updates.append((query, update))

    class FakeGuild:
        def __init__(self, channel):
            self.channel = channel

        def get_channel(self, channel_id):
            return self.channel if channel_id == 2 else None

    class FakeBot:
        def __init__(self, guild, collection):
            self.guild = guild
            self.db = {"bot_state": collection}

        def get_guild(self, guild_id):
            return self.guild if guild_id == 1 else None

    monkeypatch.setattr(github_updates.discord, "TextChannel", FakeTextChannel)
    channel = FakeTextChannel()
    collection = FakeCollection()
    bot = FakeBot(FakeGuild(channel), collection)
    cog = github_updates.GitHubUpdates(bot)

    sent = asyncio.run(cog.post_release("1", "2", sample_release()))

    assert sent is True
    assert len(channel.sent) == 1
    payload = channel.sent[0]
    assert "embed" not in payload
    assert payload["content"] == github_updates.build_release_message(sample_release())
    assert payload["allowed_mentions"].everyone is False
    assert payload["allowed_mentions"].roles is False
    assert payload["allowed_mentions"].users is False
    assert len(collection.updates) == 1
    assert collection.updates[0][1]["$set"]["last_release_id"] == "123"


def test_poll_once_skips_already_announced_release(monkeypatch):
    class FakeCollection:
        def find_one(self, query):
            return {"_id": github_updates.STATE_ID, "guild_id": "1", "channel_id": "2", "last_release_id": "123"}

    class FakeBot:
        db = {"bot_state": FakeCollection()}

    async def unexpected_post(*args, **kwargs):
        pytest.fail("Already announced release should not be posted again.")

    cog = github_updates.GitHubUpdates(FakeBot())
    monkeypatch.setattr(github_updates, "fetch_latest_release", lambda: sample_release())
    monkeypatch.setattr(cog, "post_release", unexpected_post)

    asyncio.run(cog.poll_once())
