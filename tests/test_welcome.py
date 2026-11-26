"""Message de bienvenue (texte séparé) + bannière ne contenant que la photo de profil."""
import asyncio
import datetime
import io
import json
import os
import sys
import types

import discord
import pytest
from PIL import Image, ImageChops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # `pytest tests/` (comme la CI) n'ajoute pas la racine du repo
    sys.path.insert(0, ROOT)

import welcome  # noqa: E402
from jsonlog import log_json  # noqa: E402


def png_bytes(color=(255, 0, 0), size=64):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeAvatarAsset:
    url = "https://cdn.example/avatar.png?size=256"

    def replace(self, **kw):
        return self


class FakeGuild:
    id = 111
    name = "Test Guild"
    member_count = 42


class FakeMember:
    id = 7
    mention = "<@7>"
    display_name = "Bob"
    guild = FakeGuild()
    display_avatar = FakeAvatarAsset()


class FakeChannel:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def send(self, **kwargs):
        if self.fail:
            raise discord.HTTPException(types.SimpleNamespace(status=403, reason="Forbidden"), "nope")
        self.calls.append(kwargs)


@pytest.fixture
def avatar_download(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            data = png_bytes()
            return iter(data[i:i + 11] for i in range(0, len(data), 11))

        def close(self):
            pass

    monkeypatch.setattr(welcome.requests, "get", lambda *a, **kw: FakeResponse())


# ---------------------------------------------------------------- bannière

def test_avatar_download_rejects_oversized_response(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            return iter([b"x" * welcome.MAX_AVATAR_DOWNLOAD_BYTES, b"x"])

        def close(self):
            self.closed = True

    response = FakeResponse()
    monkeypatch.setattr(welcome.requests, "get", lambda *a, **kw: response)
    with pytest.raises(ValueError, match="size limit"):
        welcome.download_avatar("https://cdn.example/avatar.png")
    assert response.closed


def test_avatar_download_rejects_http_errors(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            raise RuntimeError("HTTP error")

        def close(self):
            pass

    monkeypatch.setattr(welcome.requests, "get", lambda *a, **kw: FakeResponse())
    with pytest.raises(RuntimeError, match="HTTP error"):
        welcome.download_avatar("https://cdn.example/avatar.png")


def test_banner_is_png_with_only_a_centered_round_avatar():
    banner = Image.open(welcome.compose_welcome_banner(png_bytes((255, 0, 0)))).convert("RGB")
    assert banner.size == welcome.BANNER_SIZE == (900, 300)
    assert banner.getpixel((450, 150)) == (255, 0, 0)  # centre = couleur de l'avatar
    bg = Image.open(welcome.BANNER_BG_PATH).convert("RGB").resize(welcome.BANNER_SIZE)
    left, top = (900 - 200) // 2, (300 - 200) // 2
    # Tout ce qui est en dehors du carré de l'avatar est EXACTEMENT le fond : aucun texte dessiné.
    box = ImageChops.difference(banner, bg).getbbox()
    assert box is not None and box[0] >= left and box[1] >= top and box[2] <= left + 200 and box[3] <= top + 200
    # Avatar rond : le coin du carré reste le fond.
    assert banner.getpixel((left + 1, top + 1)) == bg.getpixel((left + 1, top + 1))


def test_banner_output_is_a_png_stream():
    buf = welcome.compose_welcome_banner(png_bytes())
    assert buf.read(8) == b"\x89PNG\r\n\x1a\n"


# ----------------------------------------------------------------- message

def test_default_message_and_placeholders():
    m = FakeMember()
    assert welcome.format_welcome_message(None, m) == "<@7> Welcome to Test Guild!"
    assert welcome.format_welcome_message("", m) == "<@7> Welcome to Test Guild!"
    assert welcome.format_welcome_message("Hey {username} ({user}) — #{count} on {server}", m) == \
        "Hey Bob (<@7>) — #42 on Test Guild"


def test_unknown_braces_are_left_alone_and_never_evaluated():
    m = FakeMember()
    assert welcome.format_welcome_message("{foo} {0} {user.id} {__class__}", m) == "{foo} {0} {user.id} {__class__}"


def test_message_is_capped_to_discord_limit():
    assert len(welcome.format_welcome_message("x" * 5000, FakeMember())) == 2000


# ------------------------------------------------------------------- envoi

def test_send_welcome_posts_text_and_banner_in_one_message(avatar_download):
    channel = FakeChannel()
    asyncio.run(welcome.send_welcome(FakeMember(), channel))
    assert len(channel.calls) == 1
    call = channel.calls[0]
    assert call["content"] == "<@7> Welcome to Test Guild!"
    assert call["file"].filename == "welcome.png"
    mentions = call["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is False
    assert [u.id for u in mentions.users] == [7]  # seul le nouveau membre peut être notifié


def test_custom_template_is_used(avatar_download):
    channel = FakeChannel()
    asyncio.run(welcome.send_welcome(FakeMember(), channel, "Bienvenue {username} sur {server} !"))
    assert channel.calls[0]["content"] == "Bienvenue Bob sur Test Guild !"


def test_text_is_still_sent_when_the_banner_fails(monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("avatar download failed")

    monkeypatch.setattr(welcome.requests, "get", boom)
    channel = FakeChannel()
    asyncio.run(welcome.send_welcome(FakeMember(), channel))
    assert len(channel.calls) == 1 and "file" not in channel.calls[0]
    assert channel.calls[0]["content"] == "<@7> Welcome to Test Guild!"
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["event"] == "welcome_banner_failed" and log["level"] == "error"


def test_send_failure_never_raises(avatar_download, capsys):
    asyncio.run(welcome.send_welcome(FakeMember(), FakeChannel(fail=True)))  # ne lève pas
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["event"] == "welcome_message_failed"


# ---------------------------------------------------------------- jsonlog

def test_log_json_prints_one_json_line(capsys):
    log_json("info", "something_happened", guild_id=1, extra={"a": datetime.date(2026, 1, 2)})
    line = capsys.readouterr().out
    assert line.count("\n") == 1
    payload = json.loads(line)
    assert payload["level"] == "info" and payload["event"] == "something_happened"
    assert payload["guild_id"] == 1 and payload["extra"] == {"a": "2026-01-02"} and "ts" in payload


# ------------------------------------------------- câblage dans main.py

def test_main_uses_the_new_welcome_module(nexus):
    import jsonlog
    assert nexus.send_welcome is welcome.send_welcome
    assert nexus.log_json is jsonlog.log_json
    assert not hasattr(nexus, "send_welcome_banner") and not hasattr(nexus, "_compose_welcome_banner")


class FakeInteraction:
    guild_id = 111

    def __init__(self):
        self.sent = []
        self.response = types.SimpleNamespace(send_message=self._send)

    async def _send(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))


def test_config_welcome_command_stores_channel_and_message(env):
    from config_store import get_config
    channel = types.SimpleNamespace(id=55, mention="<#55>")
    cb = env.nexus.config_welcome.callback

    def run(message=None):
        inter = FakeInteraction()
        asyncio.run(cb(inter, channel, message)) if message is not None else asyncio.run(cb(inter, channel))
        return inter.sent[0]

    text, ephemeral = run()  # sans message : le message existant n'est pas touché
    assert ephemeral is True and "<#55>" in text and get_config(111)["welcome_channel_id"] == 55
    assert get_config(111).get("welcome_message") is None
    text, _ = run("  Hello {user}!  ")
    assert get_config(111)["welcome_message"] == "Hello {user}!" and "Hello {user}!" in text
    run("y" * 999)
    assert len(get_config(111)["welcome_message"]) == welcome.MAX_WELCOME_MESSAGE_LENGTH == 300
    text, _ = run("default")
    assert get_config(111)["welcome_message"] is None and welcome.DEFAULT_WELCOME_MESSAGE in text
    run("custom again")
    run("   ")  # vide = retour au défaut
    assert get_config(111)["welcome_message"] is None


def test_on_member_join_posts_the_welcome_when_enabled(env, monkeypatch):
    """Câblage réel : feature activée + salon configuré -> send_welcome(member, salon, modèle du serveur)."""
    from unittest.mock import AsyncMock, MagicMock
    from config_store import update_config
    posted = []

    async def fake_send_welcome(member, channel, template=None):
        posted.append((member, channel, template))

    async def no_log(guild, embed):
        pass

    monkeypatch.setattr(env.nexus, "send_welcome", fake_send_welcome)
    monkeypatch.setattr(env.nexus, "_send_log_embed", no_log)  # le log « Member Joined » n'est pas le sujet
    channel = FakeChannel()
    guild = MagicMock()
    guild.id = 111
    guild.get_channel = lambda cid: channel if cid == 55 else None
    guild.invites = AsyncMock(return_value=[])
    member = MagicMock()
    member.guild = guild
    member.bot = False
    member.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=900)
    update_config(111, "welcome_banner_enabled", True)
    update_config(111, "welcome_channel_id", 55)
    update_config(111, "welcome_message", "Yo {user}")

    async def run():
        await env.nexus.on_member_join(member)
        await asyncio.sleep(0.05)  # laisse tourner la tâche créée par asyncio.create_task

    asyncio.run(run())
    assert posted == [(member, channel, "Yo {user}")]
    # désactivée -> rien n'est posté
    posted.clear()
    update_config(111, "welcome_banner_enabled", False)
    asyncio.run(run())
    assert posted == []
