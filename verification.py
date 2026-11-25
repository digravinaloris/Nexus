"""verification -- vérification des nouveaux membres par Cloudflare Turnstile.

Principe : le bouton « Verify » ne donne plus le rôle directement. Il génère un lien PERSONNEL
(un jeton aléatoire lié à un serveur + un membre, valable 10 minutes, utilisable une seule fois)
vers `/verify/<jeton>`. Cette page affiche le widget Turnstile ; le serveur valide la réponse
auprès de Cloudflare (`siteverify`) et SEULEMENT si elle est valide, le bot donne le rôle Verified.

Configuration (variables d'environnement, jamais en base ni dans le repo) :
    TURNSTILE_SITE_KEY      clé publique du widget
    TURNSTILE_SECRET_KEY    clé secrète (validation côté serveur)
    PUBLIC_BASE_URL         (optionnel) URL publique du site ; sinon déduite de DISCORD_REDIRECT_URI

Ce module n'importe JAMAIS main.py : le bot et la base passent par `core`.
"""
import datetime
import math
import os
import secrets

import discord
import requests
from pymongo import ReturnDocument

from config_store import get_config
from core import col
from discord_logging import _send_log_embed
from jsonlog import log_json
from sanctions_store import log_sanction

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
LINK_TTL = datetime.timedelta(minutes=10)
MAX_ATTEMPTS = 3                              # échecs Turnstile avant que le lien meure
REUSE_WINDOW = datetime.timedelta(seconds=30)  # un double-clic réutilise le même lien
METHODS = ("button", "turnstile")

# Réglages par serveur (clé de config -> défaut) et bornes acceptées. Les valeurs lues en base sont
# toujours ramenées dans ces bornes : une config corrompue ne peut pas désactiver la protection.
SETTINGS_DEFAULTS = {
    "verification_link_minutes": 10,     # durée de vie du lien personnel
    "verification_max_attempts": 3,      # échecs Turnstile avant que le lien meure
    "verification_kick_hours": 0,        # kick des membres restés Unverified (0 = jamais)
    "verification_min_account_days": 0,  # âge minimal du compte Discord pour vérifier (0 = aucun)
}
SETTINGS_LIMITS = {
    "verification_link_minutes": (2, 60),
    "verification_max_attempts": (1, 10),
    "verification_kick_hours": (0, 168),
    "verification_min_account_days": (0, 365),
}
KICK_BATCH_LIMIT = 25  # garde-fou : jamais plus de 25 kicks automatiques par serveur et par passage
SUSPICIOUS_ACCOUNT_DAYS = 7


def get_verification_settings(guild_id):
    cfg = get_config(guild_id)
    settings = {}
    for key, default in SETTINGS_DEFAULTS.items():
        value = cfg.get(key)
        lo, hi = SETTINGS_LIMITS[key]
        settings[key] = max(lo, min(hi, value)) if isinstance(value, int) and not isinstance(value, bool) else default
    return settings


def site_key():
    return os.getenv("TURNSTILE_SITE_KEY", "").strip()


def _secret_key():
    return os.getenv("TURNSTILE_SECRET_KEY", "").strip()


def public_base_url():
    base = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not base:
        redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "").strip()
        if redirect_uri.endswith("/callback"):
            base = redirect_uri[: -len("/callback")]
    return base.rstrip("/")


def turnstile_configured():
    """Faux si une clé ou l'URL publique manque : le bouton refuse alors de vérifier (il ne
    retombe JAMAIS silencieusement sur « donne le rôle sans contrôle »)."""
    return bool(site_key() and _secret_key() and public_base_url())


def verification_url(token):
    return f"{public_base_url()}/verify/{token}"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(dt):
    # pymongo relit les dates sans fuseau : on remet UTC avant de comparer.
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def create_challenge(guild_id, user_id, now=None):
    """Crée (ou réutilise, si demandé il y a < 30 s) le lien de vérification d'un membre."""
    now = now or _now()
    challenges = col.verification_challenges
    challenges.delete_many({"expires_at": {"$lt": now - datetime.timedelta(hours=1)}})  # ménage
    pair = {"guild_id": str(guild_id), "user_id": str(user_id), "used": False}
    existing = challenges.find_one(pair, sort=[("created_at", -1)])
    if (existing and _aware(existing["expires_at"]) > now
            and existing.get("attempts", 0) < existing.get("max_attempts", MAX_ATTEMPTS)
            and now - _aware(existing["created_at"]) < REUSE_WINDOW):
        return existing["token"]
    challenges.delete_many(pair)  # un seul lien actif par membre : le précédent meurt
    settings = get_verification_settings(guild_id)
    token = secrets.token_urlsafe(32)
    challenges.insert_one({
        **pair, "token": token, "attempts": 0, "created_at": now,
        "max_attempts": settings["verification_max_attempts"],
        "expires_at": now + datetime.timedelta(minutes=settings["verification_link_minutes"]),
    })
    return token


def get_valid_challenge(token, now=None):
    now = now or _now()
    doc = col.verification_challenges.find_one({"token": token})
    if (not doc or doc.get("used") or _aware(doc["expires_at"]) <= now
            or doc.get("attempts", 0) >= doc.get("max_attempts", MAX_ATTEMPTS)):
        return None
    return doc


def record_failed_attempt(token):
    doc = col.verification_challenges.find_one_and_update(
        {"token": token}, {"$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
    return doc["attempts"] if doc else MAX_ATTEMPTS


def mark_used(token, now=None):
    """Usage unique et atomique : True pour UN SEUL appel, même si deux requêtes arrivent ensemble."""
    res = col.verification_challenges.update_one({"token": token, "used": False},
                                                 {"$set": {"used": True, "used_at": now or _now()}})
    return res.modified_count == 1


def check_turnstile(response_token, remote_ip=None):
    """Validation côté serveur auprès de Cloudflare. Retourne (ok, raison). Échoue FERMÉ : si
    Cloudflare est injoignable, personne n'est vérifié."""
    secret = _secret_key()
    if not secret:
        return False, "not_configured"
    payload = {"secret": secret, "response": response_token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        data = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=10).json()
    except (requests.RequestException, ValueError) as e:
        log_json("error", "turnstile_unreachable", error=repr(e))
        return False, "service_unavailable"
    if data.get("success") is True:
        return True, None
    return False, ",".join(data.get("error-codes", [])) or "failed"


async def grant_verified(guild, user_id):
    """Donne Verified et retire Unverified. Retourne (ok, message d'erreur lisible par le membre)."""
    cfg = get_config(guild.id)
    verified_id, unverified_id = cfg.get("verified_role_id"), cfg.get("unverified_role_id")
    autorole_id = cfg.get("verification_autorole_id")
    if not verified_id:
        return False, "Verification isn't configured on this server. Please contact a moderator."
    member = guild.get_member(int(user_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.HTTPException:
            return False, "You are no longer a member of this server."
    verified_role = guild.get_role(int(verified_id))
    unverified_role = guild.get_role(int(unverified_id)) if unverified_id else None
    if verified_role is None:
        return False, "The Verified role no longer exists. Please contact a moderator."
    try:
        if unverified_role is not None and unverified_role in member.roles:
            await member.remove_roles(unverified_role, reason="Verified (Cloudflare Turnstile)")
        if verified_role not in member.roles:
            await member.add_roles(verified_role, reason="Verified (Cloudflare Turnstile)")
        extra_role = guild.get_role(int(autorole_id)) if autorole_id else None
        if extra_role is not None and extra_role != verified_role and extra_role not in member.roles:
            try:
                await member.add_roles(extra_role, reason="Extra role after verification")
            except discord.HTTPException as e:
                log_json("error", "verification_autorole_failed", guild_id=guild.id, user_id=user_id, role_id=extra_role.id, error=repr(e))

    except discord.HTTPException as e:
        log_json("error", "verification_role_update_failed", guild_id=guild.id, user_id=user_id, error=repr(e))
        return False, "I couldn't update your roles. Please contact a moderator."
    await _log_suspicious_signals(guild, member)
    return True, None


def account_days_left(user, min_days, now=None):
    """Jours restants avant que le compte atteigne l'âge minimal (0 = OK ou pas de limite)."""
    created = getattr(user, "created_at", None)
    if not min_days or created is None:
        return 0
    remaining = datetime.timedelta(days=min_days) - ((now or _now()) - _aware(created))
    return max(0, math.ceil(remaining.total_seconds() / 86400))


def suspicious_signals(member, now=None):
    """Signaux d'alerte (informatifs, ne bloquent rien) : compte très récent, avatar par défaut."""
    signals = []
    created = getattr(member, "created_at", None)
    if created is not None:
        age_days = ((now or _now()) - _aware(created)).days
        if age_days < SUSPICIOUS_ACCOUNT_DAYS:
            signals.append(f"account created {age_days} day(s) ago")
    if hasattr(member, "avatar") and member.avatar is None:
        signals.append("default avatar")
    return signals


async def _log_suspicious_signals(guild, member):
    signals = suspicious_signals(member)
    if not signals:
        return
    try:
        embed = discord.Embed(title="⚠️ Verified account with warning signals", color=0xffcc00)
        embed.add_field(name="User", value=f"<@{member.id}>", inline=True)
        embed.add_field(name="Signals", value="\n".join(f"• {sig}" for sig in signals), inline=False)
        await _send_log_embed(guild, embed)
    except Exception as e:  # un log raté ne doit jamais empêcher la vérification
        log_json("error", "verification_signal_log_failed", guild_id=guild.id, error=repr(e))


async def kick_unverified_members(guild, hours, now=None):
    """Kick les membres restés Unverified depuis plus de `hours` heures. Retourne la liste des kickés.

    Garde-fous : inactif si hours = 0 ou si le rôle Unverified est absent / est @everyone ; ignore les
    bots ; jamais plus de KICK_BATCH_LIMIT kicks par passage (une mauvaise config ne vide pas un serveur)."""
    cfg = get_config(guild.id)
    unverified_id = cfg.get("unverified_role_id")
    if not hours or not unverified_id:
        return []
    role = guild.get_role(int(unverified_id))
    if role is None or role.is_default():
        return []
    cutoff = (now or _now()) - datetime.timedelta(hours=hours)
    kicked = []
    for member in list(role.members):
        if len(kicked) >= KICK_BATCH_LIMIT:
            break
        joined = getattr(member, "joined_at", None)
        if member.bot or joined is None or _aware(joined) > cutoff:
            continue
        reason = f"Did not verify within {hours}h"
        try:
            await member.kick(reason=reason)
        except discord.HTTPException as e:
            log_json("error", "unverified_kick_failed", guild_id=guild.id, user_id=member.id, error=repr(e))
            continue
        log_sanction(guild.id, member.id, "kick", reason, "automod")
        kicked.append(member)
    if kicked:
        try:
            embed = discord.Embed(title="👢 Unverified members kicked", color=0xff6600)
            embed.add_field(name="Reason", value=f"Still Unverified after {hours}h", inline=False)
            embed.add_field(name="Members", value=", ".join(f"<@{m.id}>" for m in kicked)[:1000], inline=False)
            await _send_log_embed(guild, embed)
        except Exception as e:
            log_json("error", "unverified_kick_log_failed", guild_id=guild.id, error=repr(e))
    return kicked


async def kick_pass(guild):
    """Un passage de la boucle de fond pour un serveur (règles d'activation comprises)."""
    cfg = get_config(guild.id)
    if not (cfg.get("verification_enabled") and cfg.get("unverified_role_id")):
        return []
    return await kick_unverified_members(guild, get_verification_settings(guild.id)["verification_kick_hours"])
