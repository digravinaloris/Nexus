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
import os
import secrets

import discord
import requests
from pymongo import ReturnDocument

from config_store import get_config
from core import col
from jsonlog import log_json

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
LINK_TTL = datetime.timedelta(minutes=10)
MAX_ATTEMPTS = 3                              # échecs Turnstile avant que le lien meure
REUSE_WINDOW = datetime.timedelta(seconds=30)  # un double-clic réutilise le même lien
METHODS = ("button", "turnstile")


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
    if (existing and _aware(existing["expires_at"]) > now and existing.get("attempts", 0) < MAX_ATTEMPTS
            and now - _aware(existing["created_at"]) < REUSE_WINDOW):
        return existing["token"]
    challenges.delete_many(pair)  # un seul lien actif par membre : le précédent meurt
    token = secrets.token_urlsafe(32)
    challenges.insert_one({**pair, "token": token, "attempts": 0, "created_at": now, "expires_at": now + LINK_TTL})
    return token


def get_valid_challenge(token, now=None):
    now = now or _now()
    doc = col.verification_challenges.find_one({"token": token})
    if not doc or doc.get("used") or _aware(doc["expires_at"]) <= now or doc.get("attempts", 0) >= MAX_ATTEMPTS:
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
    except discord.HTTPException as e:
        log_json("error", "verification_role_update_failed", guild_id=guild.id, user_id=user_id, error=repr(e))
        return False, "I couldn't update your roles. Please contact a moderator."
    return True, None
