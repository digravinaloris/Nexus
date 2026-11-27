"""welcome -- message de bienvenue (texte) + bannière ne contenant QUE la photo de profil.

Le texte est un vrai message Discord (mention cliquable, modèle configurable par serveur) ;
la bannière est une image de fond avec uniquement l'avatar du membre, rond, au centre.
Aucun texte n'est dessiné dans l'image.

Modèle (config `welcome_message`, 300 caractères max) -- placeholders :
    {user}      mention du membre            {username}  pseudo affiché
    {server}    nom du serveur               {count}     nombre de membres

Ce module n'importe JAMAIS main.py.
"""
import asyncio
import io
import os

import discord
from PIL import Image, ImageDraw
import requests

from nexus_logging.jsonlog import log_json

BANNER_BG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "welcome_banner_bg.jpg")
BANNER_SIZE = (900, 300)
AVATAR_SIZE = 200
DEFAULT_WELCOME_MESSAGE = "{user} Welcome to {server}!"
MAX_WELCOME_MESSAGE_LENGTH = 300
MAX_AVATAR_DOWNLOAD_BYTES = 5 * 1024 * 1024
_AVATAR_CHUNK_SIZE = 64 * 1024
_DISCORD_MESSAGE_LIMIT = 2000


def download_avatar(url):
    """Télécharge un avatar avec une limite stricte de mémoire (5 Mio)."""
    response = requests.get(url, timeout=10, stream=True)
    try:
        response.raise_for_status()
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=_AVATAR_CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_AVATAR_DOWNLOAD_BYTES:
                raise ValueError("Avatar download exceeds the configured size limit")
            chunks.append(chunk)
        if not chunks:
            raise ValueError("Avatar download is empty")
        return b"".join(chunks)
    finally:
        response.close()


def compose_welcome_banner(avatar_bytes):
    """CPU-bound (Pillow) -- toujours appelée via asyncio.to_thread, jamais directement
    dans une coroutine. Retourne un BytesIO PNG : fond + avatar rond centré, rien d'autre."""
    bg = Image.open(BANNER_BG_PATH).convert("RGBA").resize(BANNER_SIZE)
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
    mask = Image.new("L", avatar.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0) + avatar.size, fill=255)
    position = ((BANNER_SIZE[0] - AVATAR_SIZE) // 2, (BANNER_SIZE[1] - AVATAR_SIZE) // 2)
    bg.paste(avatar, position, mask)
    buf = io.BytesIO()
    bg.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def format_welcome_message(template, member):
    """Remplace uniquement les 4 placeholders connus (pas de str.format : un `{truc}` inconnu
    ou un modèle malveillant ne peut ni planter ni lire d'attributs)."""
    text = template or DEFAULT_WELCOME_MESSAGE
    replacements = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{server}": member.guild.name,
        "{count}": str(member.guild.member_count),
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text[:_DISCORD_MESSAGE_LIMIT]


async def send_welcome(member, channel, template=None):
    """Poste le message de bienvenue (+ la bannière si elle a pu être générée). Ne lève jamais :
    une bannière ratée n'empêche pas le message, et rien ne doit empêcher le reste de
    on_member_join de tourner. Seule la mention du nouveau membre peut notifier (jamais
    @everyone ni un rôle, même si le nom du serveur ou le modèle en contient)."""
    text = format_welcome_message(template, member)
    banner = None
    try:
        avatar_bytes = await asyncio.to_thread(
            download_avatar, member.display_avatar.replace(size=256).url)
        banner = await asyncio.to_thread(compose_welcome_banner, avatar_bytes)
    except Exception as e:
        log_json("error", "welcome_banner_failed", user_id=member.id, guild_id=member.guild.id, error=str(e))
    kwargs = {"content": text,
              "allowed_mentions": discord.AllowedMentions(everyone=False, roles=False, users=[member])}
    if banner is not None:
        kwargs["file"] = discord.File(banner, filename="welcome.png")
    try:
        await channel.send(**kwargs)
    except Exception as e:
        log_json("error", "welcome_message_failed", user_id=member.id, guild_id=member.guild.id, error=str(e))
