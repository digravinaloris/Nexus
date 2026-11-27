import discord
from discord.ext import commands
from discord import app_commands
import os
from flask import Flask, request, jsonify, g, session, redirect, url_for, render_template, flash, get_flashed_messages, send_from_directory
import requests
import yaml
import pyotp
from threading import Thread
import asyncio
import datetime
import time
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId
from functools import wraps
import re
import secrets
import subprocess
import signal
import io
from PIL import Image, ImageDraw, ImageFont
import matplotlib
matplotlib.use("Agg")  # pas d'affichage sur Render -- rendu fichier uniquement
import matplotlib.pyplot as plt
from deep_translator import GoogleTranslator
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import json as _json
import zipfile
import utils
import core
from config_store import (
    get_config, update_config, add_command_role, remove_command_role,
    set_command_template, get_global_lock, set_global_lock,
)
from sanctions_store import (
    get_warns, set_warns, mark_channel_locked, mark_channel_unlocked,
    get_locked_channels, get_next_case_id, log_sanction, get_case,
    create_sanction_appeal, get_ban_appeal, get_ban_appeal_by_case,
    get_sanction_stats,
)
from audit import record_audit, build_sparkline_svg, get_audit_log
from i18n import (
    TRANSLATIONS_DIR, TRANSLATIONS, t, current_lang, current_theme,
)
from features import FEATURE_TOGGLES
from bridge import run_coroutine
from discord_logging import get_or_create_log_webhook, _send_log_embed, log_dashboard_action
from moderation_actions import reverse_sanction, _dm_user_appeal_result, SANCTION_ICONS, post_appeal_for_review
from owner_notify import get_owner_user, notify_owner_killswitch, notify_owner_bruteforce
from web.app_factory import create_web_app
from verification import (turnstile_configured, create_challenge, verification_url, get_verification_settings,
                          account_days_left, kick_pass, SETTINGS_LIMITS)
from command_catalog import MODERATION_COMMANDS, COMMAND_TEMPLATES
from audit_export import _get_audit_records, _build_audit_csv_bytes
from template_ops import _apply_template_base_config
from jsonlog import log_json
from welcome import send_welcome, DEFAULT_WELCOME_MESSAGE, MAX_WELCOME_MESSAGE_LENGTH
from utils import (
    parse_duration, check_caps, check_banned_words, check_banned_domains, check_any_link,
    is_mention_spam, detect_phone_number, detect_address_hint, detect_raid_username_pattern,
    parse_bounded_number, normalize_banned_word, normalize_domain, parse_lines_list,
)
import csv
import io
import sys

# MongoDB setup
mongo = None
db = None
warns_col = None
config_col = None
locked_channels_col = None
sanctions_col = None
reaction_roles_col = None
notes_col = None
audit_col = None
bot_state_col = None
killswitch_tokens_col = None
case_counters_col = None
sticky_messages_col = None
ban_appeals_col = None
server_backups_col = None
invite_uses_col = None
role_menus_col = None
scheduled_col = None
quarantine_col = None
reminders_col = None
templates_col = None  # globale -- PAS scopée par guild_id, contrairement à tout le reste

def init_mongo():
    global mongo, db, warns_col, config_col, locked_channels_col, sanctions_col, reaction_roles_col, notes_col, audit_col, bot_state_col, killswitch_tokens_col, case_counters_col, sticky_messages_col, ban_appeals_col, server_backups_col, invite_uses_col, role_menus_col, scheduled_col, quarantine_col, reminders_col, templates_col
    mongo = MongoClient(os.getenv("MONGO_URI"), serverSelectionTimeoutMS=5000)
    db = mongo["discordbot"]
    warns_col = db["warns"]
    config_col = db["config"]
    locked_channels_col = db["locked_channels"]
    sanctions_col = db["sanctions"]
    reaction_roles_col = db["reaction_roles"]
    notes_col = db["notes"]
    audit_col = db["audit_log"]
    bot_state_col = db["bot_state"]
    killswitch_tokens_col = db["killswitch_tokens"]
    case_counters_col = db["case_counters"]
    sticky_messages_col = db["sticky_messages"]
    ban_appeals_col = db["ban_appeals"]
    server_backups_col = db["server_backups"]
    invite_uses_col = db["invite_uses"]
    role_menus_col = db["role_menus"]
    scheduled_col = db["scheduled_announcements"]
    quarantine_col = db["quarantine"]
    reminders_col = db["reminders"]
    templates_col = db["templates"]
    # Même collections, exposées via l'objet stable `core.col` (cf. core.py) :
    # les futurs modules extraits de main.py s'appuient dessus, pas sur ces globals.
    core.init_db(mongo, db)
    # Exposé sur l'objet bot (pas avant : init_mongo() tourne dans on_ready(),
    # après setup_hook()/le chargement des cogs) -- un cog qui a besoin de Mongo
    # (ex: cogs/faq.py) lit bot.db au moment de la commande/l'event, jamais à
    # l'import du module, donc cette valeur est déjà prête à ce moment-là.
    bot.db = db


async def send_sanction_dm(member_or_user, guild, sanction_type, reason):
    """Envoie le DM de sanction avec, si un salon de revue d'appels est
    configuré pour ce serveur, un bouton pour faire appel. Retourne
    (dm_sent: bool, appeal_token: str|None) — le token doit être associé
    au case_id via create_sanction_appeal() une fois celui-ci connu
    (généralement juste après log_sanction())."""
    cfg = get_config(guild.id)
    appeals_configured = bool(cfg.get("appeal_channel_id"))
    appeal_token = secrets.token_urlsafe(16) if appeals_configured else None

    label = sanction_type.replace("_", " ")
    message = f"You have received a **{label}** in **{guild.name}**.\nReason: {reason}"
    view = None
    if appeals_configured:
        message += "\n\nIf you believe this is a mistake, you can appeal below."
        view = discord.ui.View(timeout=None)
        view.add_item(AppealButton(appeal_token))

    dm_sent = True
    try:
        if view is not None:
            await member_or_user.send(message, view=view)
        else:
            await member_or_user.send(message)
    except discord.HTTPException:
        dm_sent = False

    return dm_sent, appeal_token


def _serialize_overwrites(channel_or_category):
    """Ne garde que les permissions par rôle (pas par membre) — c'est la
    structure qu'on veut restaurer, les cas spécifiques par membre sont
    trop fragiles à recréer fidèlement après coup."""
    data = []
    for target, overwrite in channel_or_category.overwrites.items():
        if isinstance(target, discord.Role):
            allow, deny = overwrite.pair()
            data.append({"role_name": target.name, "allow": allow.value, "deny": deny.value})
    return data


def _rebuild_overwrites(serialized_overwrites, role_name_to_role):
    result = {}
    for item in serialized_overwrites:
        role = role_name_to_role.get(item["role_name"])
        if role is None:
            continue
        result[role] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(item["allow"]), discord.Permissions(item["deny"])
        )
    return result


def create_server_backup(guild):
    roles_data = []
    for role in guild.roles:
        if role.is_default():
            continue
        roles_data.append({
            "name": role.name,
            "color": role.color.value,
            "permissions": role.permissions.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "position": role.position,
        })

    channels_data = []
    for category in guild.categories:
        channels_data.append({
            "type": "category",
            "name": category.name,
            "overwrites": _serialize_overwrites(category),
        })
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        if isinstance(channel, discord.TextChannel):
            ch_type = "text"
        elif isinstance(channel, discord.VoiceChannel):
            ch_type = "voice"
        else:
            continue  # threads/forums/stage etc. pas gérés pour l'instant
        channels_data.append({
            "type": ch_type,
            "name": channel.name,
            "category": channel.category.name if channel.category else None,
            "topic": getattr(channel, "topic", None),
            "overwrites": _serialize_overwrites(channel),
        })

    backup_doc = {
        "guild_id": str(guild.id),
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "roles": roles_data,
        "channels": channels_data,
    }
    result = server_backups_col.insert_one(backup_doc)

    # Ne garde que les 5 derniers backups par serveur (évite une croissance
    # illimitée de la base pour un usage qui reste occasionnel).
    all_backups = list(server_backups_col.find({"guild_id": str(guild.id)}).sort("created_at", -1))
    for old in all_backups[5:]:
        server_backups_col.delete_one({"_id": old["_id"]})

    return result.inserted_id


async def restore_server_backup(guild, backup):
    """Ne supprime jamais rien : ne recrée que les rôles/salons manquants
    (matchés par nom). Peut créer des doublons si relancé après un premier
    restore partiel qui aurait changé un nom entre-temps — c'est un
    compromis assumé pour rester simple et sans danger de suppression."""
    role_name_to_role = {r.name: r for r in guild.roles}
    created_roles = 0
    for role_data in sorted(backup["roles"], key=lambda r: r["position"]):
        if role_data["name"] in role_name_to_role:
            continue
        try:
            new_role = await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                permissions=discord.Permissions(role_data["permissions"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                reason="Server backup restore",
            )
            role_name_to_role[new_role.name] = new_role
            created_roles += 1
        except discord.HTTPException:
            pass

    category_name_to_category = {c.name: c for c in guild.categories}
    created_channels = 0
    for ch_data in backup["channels"]:
        if ch_data["type"] != "category" or ch_data["name"] in category_name_to_category:
            continue
        overwrites = _rebuild_overwrites(ch_data["overwrites"], role_name_to_role)
        try:
            new_cat = await guild.create_category(ch_data["name"], overwrites=overwrites, reason="Server backup restore")
            category_name_to_category[new_cat.name] = new_cat
            created_channels += 1
        except discord.HTTPException:
            pass

    existing_channel_names = {c.name for c in guild.channels if not isinstance(c, discord.CategoryChannel)}
    for ch_data in backup["channels"]:
        if ch_data["type"] == "category" or ch_data["name"] in existing_channel_names:
            continue
        category = category_name_to_category.get(ch_data["category"]) if ch_data["category"] else None
        overwrites = _rebuild_overwrites(ch_data["overwrites"], role_name_to_role)
        try:
            if ch_data["type"] == "text":
                await guild.create_text_channel(
                    ch_data["name"], category=category, topic=ch_data.get("topic"),
                    overwrites=overwrites, reason="Server backup restore",
                )
            else:
                await guild.create_voice_channel(
                    ch_data["name"], category=category, overwrites=overwrites, reason="Server backup restore",
                )
            created_channels += 1
        except discord.HTTPException:
            pass

    return created_roles, created_channels


def set_sticky_message(guild_id, channel_id, content):
    sticky_messages_col.update_one(
        {"channel_id": str(channel_id)},
        {"$set": {
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "content": content,
            "last_message_id": None,
            "last_reposted_at": None,
        }},
        upsert=True,
    )

def remove_sticky_message(channel_id):
    sticky_messages_col.delete_one({"channel_id": str(channel_id)})

def get_sticky_message(channel_id):
    return sticky_messages_col.find_one({"channel_id": str(channel_id)})

def get_sanction_history(guild_id, user_id, limit=15):
    return list(
        sanctions_col.find({"guild_id": str(guild_id), "user_id": str(user_id)})
        .sort("timestamp", -1)
        .limit(limit)
    )

def save_reaction_role(guild_id, message_id, channel_id, emoji, role_id):
    reaction_roles_col.update_one(
        {"guild_id": str(guild_id), "message_id": str(message_id), "emoji": str(emoji)},
        {"$set": {"channel_id": str(channel_id), "role_id": str(role_id)}},
        upsert=True,
    )

def get_reaction_role(guild_id, message_id, emoji):
    return reaction_roles_col.find_one({
        "guild_id": str(guild_id),
        "message_id": str(message_id),
        "emoji": str(emoji),
    })

def delete_reaction_roles(guild_id, message_id):
    reaction_roles_col.delete_many({"guild_id": str(guild_id), "message_id": str(message_id)})

# Tempbans actifs : {(guild_id, user_id): timestamp_unban}
# Stocké en mémoire + DB pour survivre aux redémarrages
def save_tempban(guild_id, user_id, unban_at):
    sanctions_col.update_one(
        {"guild_id": str(guild_id), "user_id": str(user_id), "type": "tempban_active"},
        {"$set": {"unban_at": unban_at}},
        upsert=True,
    )

def remove_tempban(guild_id, user_id):
    sanctions_col.delete_one({"guild_id": str(guild_id), "user_id": str(user_id), "type": "tempban_active"})

def get_active_tempbans():
    return list(sanctions_col.find({"type": "tempban_active"}))

# Bot setup
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
core.bot = bot  # accès au bot pour les modules extraits (jamais `import main`)
bot.locked_guilds = set()  # {guild_id, ...} — serveurs actuellement verrouillés (par serveur, pas global)
bot.start_time = time.time()
bot.ready_event = None  # set in on_ready, used so Flask waits until bot is ready
_cached_global_commands = []  # snapshot des commandes, pris avant de vider le bucket global (voir on_ready) ; réutilisé par on_guild_join pour les serveurs qui rejoignent en cours de route

# ============================================================
# ==================== GLOBAL KILL-SWITCH ====================
# ============================================================
# Verrou global (tous les serveurs), indépendant du lock par-serveur
# existant (bot.locked_guilds). Activable UNIQUEMENT via un lien email
# à usage unique + un code TOTP (2FA), donc indépendant d'un compte
# Discord ou d'un token bot compromis.


_command_cooldowns = {}  # {user_id: last_invocation_timestamp}
COMMAND_COOLDOWN_SECONDS = 2

_invite_cache = {}  # {guild_id: {invite_code: uses}} — snapshot pour détecter quelle invite a servi à un join
_temp_voice_channels = {}  # {channel_id: guild_id} — salons "Join to Create" à supprimer une fois vides

async def refresh_invite_cache(guild):
    try:
        invites = await guild.invites()
        _invite_cache[guild.id] = {inv.code: (inv.uses or 0) for inv in invites}
    except discord.HTTPException:
        pass  # probablement pas la permission Manage Server

async def global_interaction_check(interaction: discord.Interaction) -> bool:
    """Appliqué à TOUTES les commandes, sur TOUS les serveurs. Un seul
    point de contrôle, pour ne jamais risquer d'oublier une vérif
    éparpillée dans chaque commande : lockdown global + cooldown anti-spam."""
    if get_global_lock() and not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "🔒 Nexus is currently locked down by its owner. Please try again later.",
            ephemeral=True,
        )
        return False

    now = time.time()
    last = _command_cooldowns.get(interaction.user.id, 0)
    if now - last < COMMAND_COOLDOWN_SECONDS:
        await interaction.response.send_message(
            "⏳ Slow down a little — wait a couple seconds between commands.", ephemeral=True
        )
        return False
    _command_cooldowns[interaction.user.id] = now
    return True

bot.tree.interaction_check = global_interaction_check


def is_bot_owner():
    """Decorator: réservé au vrai propriétaire de l'application Discord
    (vérifié via le Developer Portal par discord.py, jamais un id codé
    en dur ici)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not await bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ This command is restricted to the bot owner.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def send_email(subject, body):
    """Envoi via l'API HTTPS de Resend (port 443, jamais bloqué) — Render
    bloque le SMTP brut (25/465/587) sur son offre gratuite, donc pas de
    smtplib possible ici. Toujours appelé via asyncio.to_thread depuis le
    code async pour ne pas geler l'event loop du bot.
    Retourne (succès: bool, message_erreur: str|None)."""
    resend_api_key = os.getenv("RESEND_API_KEY")
    owner_email = os.getenv("OWNER_EMAIL")
    if not (resend_api_key and owner_email):
        error = "Missing RESEND_API_KEY / OWNER_EMAIL env var(s)."
        print(f"[EMAIL] {error}", flush=True)
        return False, error
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_api_key}"},
            json={
                "from": "Nexus <onboarding@resend.dev>",
                "to": [owner_email],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        if r.status_code >= 400:
            error = f"Resend API returned {r.status_code}: {r.text[:300]}"
            print(f"[EMAIL] {error}", flush=True)
            return False, error
        return True, None
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"[EMAIL] Failed to send: {error}", flush=True)
        return False, error


def create_killswitch_token():
    token = secrets.token_urlsafe(32)
    now = datetime.datetime.now(datetime.timezone.utc)
    killswitch_tokens_col.insert_one({
        "token": token,
        "created_at": now,
        "expires_at": now + datetime.timedelta(minutes=30),
        "used": False,
        "attempts": 0,
    })
    return token


def send_daily_killswitch_email():
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        error = "PUBLIC_BASE_URL not set, cannot build the confirmation link."
        print(f"[KILLSWITCH] {error}", flush=True)
        return False, error
    token = create_killswitch_token()
    link = f"{base_url}/admin/kill-switch/{token}"
    locked = get_global_lock()
    state_txt = "currently LOCKED down" if locked else "currently active (unlocked)"
    action_txt = "unlock it" if locked else "lock it down"
    body = (
        "Nexus — daily control link\n\n"
        f"The bot is {state_txt} across all servers.\n\n"
        f"If you need to {action_txt}, open this link and enter your authenticator code:\n{link}\n\n"
        "This link expires in 30 minutes and can only be used once.\n"
        "Nothing happens if you ignore this email."
    )
    return send_email("Nexus — daily control link", body)


async def daily_killswitch_email_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            success, error = await asyncio.to_thread(send_daily_killswitch_email)
            if not success:
                print(f"[KILLSWITCH] Daily email failed: {error}", flush=True)
        except Exception as e:
            print(f"[KILLSWITCH] Daily email loop error: {e}", flush=True)
        await asyncio.sleep(24 * 60 * 60)


def has_admin():
    """Decorator: réservé aux membres avec la permission Discord Administrator."""
    async def predicate(interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            embed = discord.Embed(description="❌ This command can only be used in a server.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(description="❌ You need Administrator permission to use this command.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def has_owner():
    """Decorator: réservé au owner du serveur (interaction.guild.owner_id), même les admins ne passent pas."""
    async def predicate(interaction: discord.Interaction):
        if interaction.guild is None:
            embed = discord.Embed(description="❌ This command can only be used in a server.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        if interaction.user.id != interaction.guild.owner_id:
            embed = discord.Embed(description="❌ Only the server owner can use this command.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

async def check_access(interaction: discord.Interaction, command_name: str, native_permission: str = None) -> bool:
    """Vérifie si l'utilisateur peut utiliser une commande donnée.
    - Si CE serveur est verrouillé, seuls les admins passent.
    - Les administrateurs du serveur passent toujours.
    - Si des rôles ont été configurés pour cette commande via /config allow, seuls ces rôles (ou un admin) peuvent l'utiliser.
    - Sinon, retombe sur la permission Discord native fournie (ou ouvert à tous si aucune n'est fournie).
    """
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        embed = discord.Embed(description="❌ This command can only be used in a server.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if interaction.guild_id in bot.locked_guilds and not interaction.user.guild_permissions.administrator:
        embed = discord.Embed(description="🔒 The bot is currently locked on this server.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = get_config(interaction.guild_id)
    allowed_roles = set(cfg.get("command_roles", {}).get(command_name, []))
    if allowed_roles:
        user_roles = {role.id for role in interaction.user.roles}
        if user_roles & allowed_roles:
            return True
        embed = discord.Embed(description="❌ You don't have permission to use this command.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if native_permission is None:
        return True
    if getattr(interaction.user.guild_permissions, native_permission, False):
        return True
    embed = discord.Embed(description="❌ You don't have permission to use this command.", color=0xff0000)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    return False

@bot.event
async def setup_hook():
    """Appelé une seule fois par discord.py avant la connexion — contrairement
    à on_ready qui peut se redéclencher à chaque reconnexion. C'est ici qu'on
    charge les extensions (cogs), pour ne pas tenter de les recharger en
    boucle et déclencher une erreur "already loaded"."""
    # Les cogs n'importent jamais main.py (ça créerait un import circulaire) :
    # ils accèdent aux quelques helpers partagés via l'objet bot lui-même.
    bot.check_access = check_access
    bot.get_config = get_config
    for extension in ("cogs.faq", "cogs.calendar", "cogs.github_updates"):
        try:
            await bot.load_extension(extension)
            print(f"[COGS] Loaded {extension}", flush=True)
        except Exception as e:
            print(f"[COGS] Failed to load {extension}: {e}", flush=True)

@bot.event
async def on_ready():
    init_mongo()
    global _cached_global_commands
    try:
        # On garde une copie des commandes déclarées (via les décorateurs)
        # AVANT de vider le bucket global — sert à alimenter les nouveaux
        # serveurs qui rejoignent plus tard dans la même session (voir
        # on_guild_join), une fois que le bucket global aura été vidé.
        _cached_global_commands = list(bot.tree.get_commands())

        # Sync instantanée sur CHAQUE serveur où le bot est déjà présent —
        # plus besoin d'attendre la propagation globale de Discord pour voir
        # les nouvelles commandes, sur aucun de tes serveurs.
        instant_count = 0
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                instant_count += 1
            except Exception as e:
                print(f"[SYNC] Failed to instantly sync guild {guild.id}: {e}", flush=True)
        print(f"Instantly synced commands to {instant_count}/{len(bot.guilds)} server(s)", flush=True)

        # On ne garde JAMAIS de commandes globales en parallèle : ça créait
        # un doublon de chaque commande (une copie globale + une copie par
        # serveur). On vide le bucket global et on pousse la liste vide à
        # Discord pour effacer les anciennes commandes globales déjà enregistrées.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Cleared global commands — running per-server only from now on", flush=True)
    except Exception as e:
        print(f"Sync error: {e}", flush=True)
    print(f"{bot.user} is online!", flush=True)
    log_json("info", "bot_ready", user=str(bot.user), guild_count=len(bot.guilds))
    if not getattr(bot, "_nexus_persistent_views_added", False):
        bot.add_view(VerificationView())
        bot.add_view(TicketPanelView())
        bot.add_view(TicketCloseView())
        bot.add_view(SatisfactionSurveyView())
        bot.add_dynamic_items(AppealButton)
        bot.add_view(RoleMenuView())
        bot._nexus_persistent_views_added = True
    bot.loop.create_task(tempban_check_loop())
    bot.loop.create_task(member_count_loop())
    bot.loop.create_task(daily_killswitch_email_loop())
    bot.loop.create_task(weekly_digest_loop())
    bot.loop.create_task(scheduled_announcements_loop())
    bot.loop.create_task(reminders_loop())
    bot.loop.create_task(mongo_backup_loop())
    bot.loop.create_task(auto_slowmode_revert_loop())
    bot.loop.create_task(quarantine_review_loop())
    if not getattr(bot, "_nexus_unverified_kick_started", False):  # on_ready peut se redéclencher
        bot._nexus_unverified_kick_started = True
        bot.loop.create_task(unverified_kick_loop())
    for guild in bot.guilds:
        await refresh_invite_cache(guild)

@bot.event
async def on_invite_create(invite: discord.Invite):
    await refresh_invite_cache(invite.guild)

@bot.event
async def on_invite_delete(invite: discord.Invite):
    await refresh_invite_cache(invite.guild)

@bot.event
async def on_guild_join(guild: discord.Guild):
    """Quand le bot rejoint un nouveau serveur : crée sa config par défaut et prévient le owner."""
    get_config(guild.id)  # crée le document de config par défaut pour ce serveur
    try:
        # Le bucket global est vidé après le démarrage (voir on_ready), donc
        # copy_global_to n'aurait plus rien à copier ici — on repart du
        # snapshot pris juste avant ce nettoyage.
        for cmd in _cached_global_commands:
            bot.tree.add_command(cmd, guild=guild, override=True)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        print(f"[SYNC] Failed to instantly sync new guild {guild.id}: {e}", flush=True)
    try:
        owner = guild.owner or await guild.fetch_owner()
        embed = discord.Embed(
            title="👋 Thanks for adding Nexus!",
            description=(
                f"I'm now active on **{guild.name}**. Here's how to get started:\n\n"
                "• `/config logs <channel>` — set where logs are sent\n"
                "• `/config autorole <role>` — role given automatically to new members\n"
                "• `/config allow <command> <role>` / `/config disallow` — let a specific role use a specific command "
                "(otherwise the default Discord permission is used, e.g. Ban Members for `/ban`) — **server owner only**\n"
                "• `/config apikey` — generate an API key if you want to use the mobile companion app\n"
                "• `/botlock` / `/botunlock` — lock/unlock the bot on this server — **server owner only**\n\n"
                "Most `/config` commands require the **Administrator** permission, except `allow`/`disallow` which are owner-only. Have fun! 🎉"
            ),
            color=0x3399ff,
        )
        await owner.send(embed=embed)
    except Exception as e:
        print(f"on_guild_join welcome DM failed: {e}", flush=True)

@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Quand le bot est retiré d'un serveur : nettoie toutes les données stockées pour ce serveur."""
    guild_id = str(guild.id)
    try:
        config_col.delete_one({"guild_id": guild_id})
        warns_col.delete_many({"guild_id": guild_id})
        locked_channels_col.delete_many({"guild_id": guild_id})
        sanctions_col.delete_many({"guild_id": guild_id})
        reaction_roles_col.delete_many({"guild_id": guild_id})
        notes_col.delete_many({"guild_id": guild_id})
        print(f"Cleaned up data for removed guild {guild_id}", flush=True)
    except Exception as e:
        print(f"on_guild_remove cleanup failed: {e}", flush=True)

# /dashboard
@bot.tree.command(name="dashboard", description="Open the Nexus web dashboard")
async def dashboard(interaction: discord.Interaction):
    """Send a private link to the Nexus web dashboard."""
    dashboard_url = "https://nexus-1qqu.onrender.com/dashboard"
    embed = discord.Embed(
        title="⚙️ Nexus Dashboard",
        description="Manage your server directly from the web dashboard.",
        color=0x3399ff,
    )
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            label="Open Dashboard",
            style=discord.ButtonStyle.link,
            url=dashboard_url,
        )
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# /ban
@bot.tree.command(name="ban", description="Ban a member")
@app_commands.default_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not await check_access(interaction, "ban", "ban_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't ban this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return

    # Le DM doit partir AVANT le ban : une fois banni, le bot ne partage
    # plus de serveur avec la personne et ne peut généralement plus lui
    # écrire pour la première fois.
    dm_sent, appeal_token = await send_sanction_dm(member, interaction.guild, "ban", reason)

    try:
        await member.ban(reason=reason)
    except Exception:
        embed = discord.Embed(description="❌ I can't ban this member.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return

    case_id = log_sanction(interaction.guild_id, member.id, "ban", reason, interaction.user.id)
    if appeal_token:
        create_sanction_appeal(interaction.guild_id, case_id, "ban", member.id, str(member), reason, appeal_token)

    embed = discord.Embed(title="🔨 Member Banned", color=0xff0000)
    embed.add_field(name="Case", value=f"#{case_id}", inline=True)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Banned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    if not dm_sent:
        embed.set_footer(text="Couldn't DM the user (DMs closed or the bot is blocked).")
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /kick
@bot.tree.command(name="kick", description="Kick a member")
@app_commands.default_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not await check_access(interaction, "kick", "kick_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't kick this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return

    # Comme pour le ban : le DM doit partir avant, sinon le bot ne peut
    # plus écrire à quelqu'un avec qui il ne partage plus de serveur.
    dm_sent, appeal_token = await send_sanction_dm(member, interaction.guild, "kick", reason)

    try:
        await member.kick(reason=reason)
    except Exception:
        embed = discord.Embed(description="❌ I can't kick this member.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return

    case_id = log_sanction(interaction.guild_id, member.id, "kick", reason, interaction.user.id)
    if appeal_token:
        create_sanction_appeal(interaction.guild_id, case_id, "kick", member.id, str(member), reason, appeal_token)

    embed = discord.Embed(title="👢 Member Kicked", color=0xff0000)
    embed.add_field(name="Case", value=f"#{case_id}", inline=True)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Kicked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    if not dm_sent:
        embed.set_footer(text="Couldn't DM the user (DMs closed or the bot is blocked).")
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /mute
@bot.tree.command(name="mute", description="Timeout a member")
@app_commands.default_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "No reason provided"):
    if not await check_access(interaction, "mute", "moderate_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't mute this member, their role is too high.", color=0xff6600)
        await interaction.response.send_message(embed=embed)
        return

    dm_sent, appeal_token = await send_sanction_dm(member, interaction.guild, "mute", f"{reason} ({minutes} min)")

    try:
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
    except Exception:
        embed = discord.Embed(description="❌ I can't mute this member.", color=0xff6600)
        await interaction.response.send_message(embed=embed)
        return

    case_id = log_sanction(interaction.guild_id, member.id, "mute", f"{reason} ({minutes} min)", interaction.user.id)
    if appeal_token:
        create_sanction_appeal(interaction.guild_id, case_id, "mute", member.id, str(member), f"{reason} ({minutes} min)", appeal_token)

    embed = discord.Embed(title="🔇 Member Muted", color=0xff6600)
    embed.add_field(name="Case", value=f"#{case_id}", inline=True)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Muted by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.add_field(name="Duration", value=f"{minutes} minutes", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    if not dm_sent:
        embed.set_footer(text="Couldn't DM the user (DMs closed or the bot is blocked).")
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /unmute
@bot.tree.command(name="unmute", description="Remove timeout from a member")
@app_commands.default_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "unmute", "moderate_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't unmute this member, their role is too high.", color=0xff6600)
        await interaction.response.send_message(embed=embed)
        return
    await member.timeout(None)
    embed = discord.Embed(title="🔊 Member Unmuted", color=0xff6600)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unmuted by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /unban
@bot.tree.command(name="unban", description="Unban a user by ID")
@app_commands.default_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    if not await check_access(interaction, "unban", "ban_members"): return
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        embed = discord.Embed(title="✅ Member Unbanned", color=0x00cc00)
        embed.add_field(name="User", value=f"**{user}**", inline=True)
        embed.add_field(name="Unbanned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except:
        embed = discord.Embed(description="❌ User not found or not banned.", color=0xff0000)
        await interaction.response.send_message(embed=embed)

# /warn
@bot.tree.command(name="warn", description="Warn a member")
@app_commands.default_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not await check_access(interaction, "warn", "manage_messages"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't warn this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    await interaction.response.defer()
    dm_sent, appeal_token = await send_sanction_dm(member, interaction.guild, "warn", reason)
    count = get_warns(interaction.guild_id, member.id) + 1
    set_warns(interaction.guild_id, member.id, count)
    case_id = log_sanction(interaction.guild_id, member.id, "warn", reason, interaction.user.id)
    if appeal_token:
        create_sanction_appeal(interaction.guild_id, case_id, "warn", member.id, str(member), reason, appeal_token)

    # Escalade automatique : mute ou kick une fois certains seuils de warns atteints.
    escalation_note = None
    cfg = get_config(interaction.guild_id)
    if cfg.get("warn_escalation_enabled"):
        kick_at = cfg.get("warn_kick_threshold", 5)
        mute_at = cfg.get("warn_mute_threshold", 3)
        if count == kick_at:
            try:
                await member.kick(reason=f"Auto-escalation: reached {count} warnings")
                log_sanction(interaction.guild_id, member.id, "kick", f"Auto-escalation at {count} warnings", "automod")
                escalation_note = f"🚨 Auto-kicked — reached {count} warnings."
            except discord.HTTPException:
                pass
        elif count == mute_at:
            try:
                minutes = cfg.get("warn_mute_minutes", 10)
                await member.timeout(datetime.timedelta(minutes=minutes), reason=f"Auto-escalation: reached {count} warnings")
                log_sanction(interaction.guild_id, member.id, "mute", f"Auto-escalation at {count} warnings ({minutes} min)", "automod")
                escalation_note = f"🚨 Auto-muted for {minutes} min — reached {count} warnings."
            except discord.HTTPException:
                pass

    embed = discord.Embed(title="⚠️ Member Warned", color=0xffcc00)
    embed.add_field(name="Case", value=f"#{case_id}", inline=True)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Warned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
    if escalation_note:
        embed.add_field(name="Escalation", value=escalation_note, inline=False)
    if not dm_sent:
        embed.set_footer(text="Couldn't DM the user (DMs closed or the bot is blocked).")
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.followup.send(embed=embed)

# /unwarn
@bot.tree.command(name="unwarn", description="Remove a warning from a member")
@app_commands.default_permissions(manage_messages=True)
async def unwarn(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "unwarn", "manage_messages"): return
    await interaction.response.defer()
    count = get_warns(interaction.guild_id, member.id)
    if count == 0:
        embed = discord.Embed(description=f"❌ **{member}** has no warnings.", color=0xff0000)
        await interaction.followup.send(embed=embed)
        return
    count -= 1
    set_warns(interaction.guild_id, member.id, count)
    embed = discord.Embed(title="✅ Warning Removed", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unwarn by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.add_field(name="Remaining Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.followup.send(embed=embed)

# /warnings
@bot.tree.command(name="warnings", description="Check warnings of a member")
async def warnings(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "warnings", None): return
    await interaction.response.defer()
    count = get_warns(interaction.guild_id, member.id)
    embed = discord.Embed(title="📋 Warnings", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.followup.send(embed=embed)

# /clear
@bot.tree.command(name="clear", description="Cleans messages from a channel")
@app_commands.describe(
    amount="Number of messages to scan (required)",
    filter_by_user="Only delete messages from this user",
    filter_by_role="Only delete messages from members with this role",
    filter_by_bots="Only delete messages sent by bots",
)
@app_commands.default_permissions(manage_messages=True)
async def clear(
    interaction: discord.Interaction,
    amount: int,
    filter_by_user: discord.Member = None,
    filter_by_role: discord.Role = None,
    filter_by_bots: bool = False,
):
    if not await check_access(interaction, "clear", "manage_messages"): return

    def message_check(message):
        if filter_by_user and message.author.id != filter_by_user.id:
            return False
        if filter_by_role and (not isinstance(message.author, discord.Member) or filter_by_role not in message.author.roles):
            return False
        if filter_by_bots and not message.author.bot:
            return False
        return True

    filters_active = filter_by_user or filter_by_role or filter_by_bots
    desc = f"🗑️ Clearing **{amount}** messages"
    if filter_by_user:
        desc += f" from {filter_by_user.mention}"
    if filter_by_role:
        desc += f" with role {filter_by_role.mention}"
    if filter_by_bots:
        desc += " sent by bots"
    desc += "..."

    embed = discord.Embed(description=desc, color=0x3399ff)
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(2)

    if filters_active:
        deleted = await interaction.channel.purge(limit=amount, check=message_check)
        result_embed = discord.Embed(description=f"✅ Deleted **{len(deleted)}** matching message(s).", color=0x00cc00)
        await interaction.channel.send(embed=result_embed)
    else:
        await interaction.channel.purge(limit=amount + 1)

# /mutelist
@bot.tree.command(name="mutelist", description="List all muted members")
async def mutelist(interaction: discord.Interaction):
    if not await check_access(interaction, "mutelist", None): return
    muted = [m for m in interaction.guild.members if m.is_timed_out()]
    if not muted:
        embed = discord.Embed(description="✅ No members are currently muted.", color=0x00cc00)
        await interaction.response.send_message(embed=embed)
        return
    lines = [f"**{m}** — until {m.timed_out_until.strftime('%Y-%m-%d %H:%M UTC')}" for m in muted]
    view = PaginatedEmbedView("🔇 Muted Members", lines, color=0xff6600, author_id=interaction.user.id)
    await interaction.response.send_message(embed=view.build_embed(), view=view if view.max_page > 0 else None)

# /roleadd
@bot.tree.command(name="roleadd", description="Give a role to a member")
@app_commands.default_permissions(manage_roles=True)
async def roleadd(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not await check_access(interaction, "roleadd", "manage_roles"): return
    if role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't give this role, it's too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    if role in member.roles:
        embed = discord.Embed(description=f"❌ **{member}** already has the role {role.mention}.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    await member.add_roles(role)
    embed = discord.Embed(title="✅ Role Added", color=0x00cc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Role", value=role.mention, inline=True)
    embed.add_field(name="Added by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /roleremove
@bot.tree.command(name="roleremove", description="Remove a role from a member")
@app_commands.default_permissions(manage_roles=True)
async def roleremove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if not await check_access(interaction, "roleremove", "manage_roles"): return
    if role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't remove this role, it's too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    if role not in member.roles:
        embed = discord.Embed(description=f"❌ **{member}** doesn't have the role {role.mention}.", color=0xff0000)
        await interaction.response.send_message(embed=embed)
        return
    await member.remove_roles(role)
    embed = discord.Embed(title="✅ Role Removed", color=0x00cc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Role", value=role.mention, inline=True)
    embed.add_field(name="Removed by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# /lock
@bot.tree.command(name="lock", description="Lock a channel")
@app_commands.default_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await check_access(interaction, "lock", "manage_channels"): return
    channel = channel or interaction.channel
    await channel.set_permissions(interaction.guild.default_role, send_messages=False)
    mark_channel_locked(interaction.guild_id, channel.id, channel.name, interaction.user.id)
    embed = discord.Embed(title="🔒 Channel Locked", color=0xff0000)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Locked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    await interaction.response.send_message(embed=embed)

# /unlock
@bot.tree.command(name="unlock", description="Unlock a channel")
@app_commands.default_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await check_access(interaction, "unlock", "manage_channels"): return
    channel = channel or interaction.channel
    await channel.set_permissions(interaction.guild.default_role, send_messages=True)
    mark_channel_unlocked(interaction.guild_id, channel.id)
    embed = discord.Embed(title="🔓 Channel Unlocked", color=0x00cc00)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Unlocked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    await interaction.response.send_message(embed=embed)

# /vlock
@bot.tree.command(name="vlock", description="Lock a voice channel (prevent members from connecting)")
@app_commands.default_permissions(manage_channels=True)
async def vlock(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    if not await check_access(interaction, "vlock", "manage_channels"): return
    channel = channel or (interaction.user.voice.channel if interaction.user.voice else None)
    if channel is None:
        embed = discord.Embed(description="❌ Specify a voice channel or join one first.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await channel.set_permissions(interaction.guild.default_role, connect=False)
    mark_channel_locked(interaction.guild_id, channel.id, channel.name, interaction.user.id, channel_type="voice")

    # Déconnecte tous les membres déjà présents dans le salon au moment du lock
    disconnected = []
    for member in channel.members:
        try:
            await member.move_to(None)
            disconnected.append(member)
        except Exception as e:
            print(f"Failed to disconnect {member} from voice channel: {e}", flush=True)

    embed = discord.Embed(title="🔒 Voice Channel Locked", color=0xff0000)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Locked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    if disconnected:
        embed.add_field(name="Disconnected", value=", ".join(m.mention for m in disconnected), inline=False)
    await interaction.response.send_message(embed=embed)

# /vunlock
@bot.tree.command(name="vunlock", description="Unlock a voice channel")
@app_commands.default_permissions(manage_channels=True)
async def vunlock(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    if not await check_access(interaction, "vunlock", "manage_channels"): return
    channel = channel or (interaction.user.voice.channel if interaction.user.voice else None)
    if channel is None:
        embed = discord.Embed(description="❌ Specify a voice channel or join one first.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await channel.set_permissions(interaction.guild.default_role, connect=True)
    mark_channel_unlocked(interaction.guild_id, channel.id)
    embed = discord.Embed(title="🔓 Voice Channel Unlocked", color=0x00cc00)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="Unlocked by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
    await interaction.response.send_message(embed=embed)

# /lockedchannels
@bot.tree.command(name="lockedchannels", description="List all currently locked channels")
async def lockedchannels(interaction: discord.Interaction):
    if not await check_access(interaction, "lockedchannels", None): return
    locked = get_locked_channels(interaction.guild_id)
    if not locked:
        embed = discord.Embed(description="✅ No channels are currently locked.", color=0x00cc00)
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title="🔒 Locked Channels", color=0xff0000)
    for doc in locked:
        channel_id = doc["channel_id"]
        channel_name = doc.get("channel_name", "unknown")
        channel_type = doc.get("channel_type", "text")
        icon = "🔊" if channel_type == "voice" else "#"
        locked_at = doc.get("locked_at")
        locked_at_str = locked_at.strftime("%Y-%m-%d %H:%M UTC") if locked_at else "Unknown"
        try:
            locked_by_user = await bot.fetch_user(int(doc.get("locked_by")))
            locked_by_str = str(locked_by_user)
        except Exception:
            locked_by_str = f"ID {doc.get('locked_by', 'unknown')}"
        embed.add_field(
            name=f"{icon} {channel_name}",
            value=f"<#{channel_id}>\nLocked by **{locked_by_str}** on {locked_at_str}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="slowmode", description="Set slowmode delay on a channel")
@app_commands.describe(channel="The channel to apply slowmode to", seconds="Delay in seconds (0 to disable, max 21600)")
@app_commands.default_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, channel: discord.TextChannel, seconds: int):
    if not await check_access(interaction, "slowmode", "manage_channels"): return
    seconds = max(0, min(seconds, 21600))
    await channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        embed = discord.Embed(description=f"✅ Slowmode disabled in {channel.mention}.", color=0x00cc00)
    else:
        embed = discord.Embed(description=f"✅ Slowmode set to **{seconds}s** in {channel.mention}.", color=0x00cc00)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nickname", description="Change a member's nickname")
@app_commands.describe(member="The member to rename", nickname="New nickname (leave empty to reset)")
@app_commands.default_permissions(manage_nicknames=True)
async def nickname(interaction: discord.Interaction, member: discord.Member, nickname: str = None):
    if not await check_access(interaction, "nickname", "manage_nicknames"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't rename this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    try:
        await member.edit(nick=nickname)
    except Exception as e:
        embed = discord.Embed(description=f"❌ Error: {e}", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if nickname:
        embed = discord.Embed(description=f"✅ {member.mention}'s nickname changed to **{nickname}**.", color=0x00cc00)
    else:
        embed = discord.Embed(description=f"✅ {member.mention}'s nickname reset.", color=0x00cc00)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="groupnickname", description="Add or remove a prefix on the nickname of every member with a role")
@app_commands.describe(role="The role to target", prefix="The prefix to add (e.g. '[EVENT]', a space is added automatically)", remove="Remove this prefix instead of adding it")
@app_commands.default_permissions(manage_nicknames=True)
async def groupnickname(interaction: discord.Interaction, role: discord.Role, prefix: str, remove: bool = False):
    if not await check_access(interaction, "groupnickname", "manage_nicknames"): return
    if role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't manage nicknames for this role, it's too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    updated, skipped = 0, 0
    for member in role.members:
        if member.top_role >= interaction.guild.me.top_role:
            skipped += 1
            continue
        current = member.nick or member.name
        full_prefix = f"{prefix} "
        try:
            if remove:
                if current.startswith(full_prefix):
                    new_nick = current[len(full_prefix):] or None
                    await member.edit(nick=new_nick)
                    updated += 1
            else:
                if not current.startswith(full_prefix):
                    new_nick = (full_prefix + current)[:32]
                    await member.edit(nick=new_nick)
                    updated += 1
        except Exception:
            skipped += 1
    action = "removed from" if remove else "added to"
    embed = discord.Embed(
        description=f"✅ Prefix **{action}** {updated} member(s) with {role.mention}." + (f" ({skipped} skipped)" if skipped else ""),
        color=0x00cc00,
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="warnlist", description="List all warned members")
@app_commands.default_permissions(manage_messages=True)
async def warnlist(interaction: discord.Interaction):
    if not await check_access(interaction, "warnlist", "manage_messages"): return
    await interaction.response.defer()
    docs = warns_col.find({"guild_id": str(interaction.guild_id), "count": {"$gt": 0}})
    warned = list(docs)
    if not warned:
        embed = discord.Embed(description="✅ No members have warnings.", color=0x00cc00)
        await interaction.followup.send(embed=embed)
        return
    lines = []
    for doc in warned:
        try:
            user = await bot.fetch_user(int(doc["user_id"]))
            lines.append(f"**{user}** — {doc['count']} warning(s)")
        except Exception:
            lines.append(f"Unknown ({doc['user_id']}) — {doc['count']} warning(s)")
    view = PaginatedEmbedView("⚠️ Warned Members", lines, color=0xffcc00, author_id=interaction.user.id)
    await interaction.followup.send(embed=view.build_embed(), view=view if view.max_page > 0 else None)

# /history
@bot.tree.command(name="history", description="Show moderation history for a member")
async def history(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "history", None): return
    await interaction.response.defer()
    records = get_sanction_history(interaction.guild_id, member.id, limit=100)
    if not records:
        embed = discord.Embed(description=f"✅ No sanctions found for **{member}**.", color=0x00cc00)
        await interaction.followup.send(embed=embed)
        return

    lines = []
    for record in records:
        icon = SANCTION_ICONS.get(record["type"], "•")
        date_str = record["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
        mod_id = record.get("moderator_id", "")
        if mod_id == "mobile_app":
            mod_str = "📱 Mobile App"
        elif mod_id == "automod":
            mod_str = "🤖 AutoMod"
        else:
            try:
                mod_user = await bot.fetch_user(int(mod_id))
                mod_str = str(mod_user)
            except Exception:
                mod_str = f"ID {mod_id}"
        lines.append(
            f"{icon} **Case #{record.get('case_id', '?')}** — {record['type'].replace('_', ' ').title()} — {date_str}\n"
            f"Reason: {record['reason']} · By: {mod_str}"
        )

    view = PaginatedEmbedView(f"📋 Sanction History — {member}", lines, color=0x3399ff, per_page=5, author_id=interaction.user.id)
    embed = view.build_embed()
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.followup.send(embed=embed, view=view if view.max_page > 0 else None)


case_group = app_commands.Group(name="case", description="View or edit moderation cases")


@case_group.command(name="view", description="Look up a moderation case by its ID")
@app_commands.describe(case_id="The case number shown when a sanction was issued")
async def case_view(interaction: discord.Interaction, case_id: int):
    if not await check_access(interaction, "case", None): return
    case = get_case(interaction.guild_id, case_id)
    if not case:
        embed = discord.Embed(description=f"❌ No case **#{case_id}** found on this server.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    mod_id = case.get("moderator_id", "")
    if mod_id == "mobile_app":
        mod_str = "📱 Mobile App"
    elif mod_id == "automod":
        mod_str = "🤖 AutoMod"
    else:
        try:
            mod_user = await bot.fetch_user(int(mod_id))
            mod_str = str(mod_user)
        except Exception:
            mod_str = f"ID {mod_id}"

    try:
        target_user = await bot.fetch_user(int(case["user_id"]))
        target_str = str(target_user)
    except Exception:
        target_str = f"ID {case['user_id']}"

    icon = SANCTION_ICONS.get(case["type"], "•")
    reason_value = case["reason"]
    if case.get("edited"):
        reason_value += f"\n*(edited by {case.get('edited_by_name', 'unknown')})*"
    embed = discord.Embed(title=f"{icon} Case #{case_id}", color=0x3399ff)
    embed.add_field(name="Type", value=case["type"].replace("_", " ").title(), inline=True)
    embed.add_field(name="User", value=target_str, inline=True)
    embed.add_field(name="Moderator", value=mod_str, inline=True)
    embed.add_field(name="Reason", value=reason_value, inline=False)
    notes = case.get("followup_notes", [])
    if notes:
        notes_text = "\n".join(f"• {n['text']} — *{n['author_name']}*" for n in notes[-10:])
        embed.add_field(name=f"Follow-up notes ({len(notes)})", value=notes_text[:1000], inline=False)
    embed.set_footer(text=case["timestamp"].strftime("%Y-%m-%d %H:%M UTC"))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@case_group.command(name="edit", description="Edit the reason on an existing case — admin only")
@app_commands.describe(case_id="The case number to edit", reason="The new reason")
@has_admin()
async def case_edit(interaction: discord.Interaction, case_id: int, reason: str):
    case = get_case(interaction.guild_id, case_id)
    if not case:
        await interaction.response.send_message(f"❌ No case **#{case_id}** found on this server.", ephemeral=True)
        return
    old_reason = case["reason"]
    sanctions_col.update_one(
        {"guild_id": str(interaction.guild_id), "case_id": case_id},
        {"$set": {
            "reason": reason,
            "edited": True,
            "edited_by": str(interaction.user.id),
            "edited_by_name": str(interaction.user),
            "edited_at": datetime.datetime.now(datetime.timezone.utc),
        }},
    )
    record_audit(
        interaction.guild_id, interaction.user.id, str(interaction.user),
        f"Edited case #{case_id}", f"Old reason: {old_reason} → New reason: {reason}",
    )
    await interaction.response.send_message(f"✅ Case **#{case_id}** updated with the new reason.", ephemeral=True)


@case_group.command(name="note", description="Add a follow-up note to a case, without touching the original reason — admin only")
@app_commands.describe(case_id="The case number", text="The note to add")
@has_admin()
async def case_note(interaction: discord.Interaction, case_id: int, text: str):
    case = get_case(interaction.guild_id, case_id)
    if not case:
        await interaction.response.send_message(f"❌ No case **#{case_id}** found on this server.", ephemeral=True)
        return
    sanctions_col.update_one(
        {"guild_id": str(interaction.guild_id), "case_id": case_id},
        {"$push": {"followup_notes": {
            "author_name": str(interaction.user),
            "text": text,
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
        }}},
    )
    record_audit(interaction.guild_id, interaction.user.id, str(interaction.user), f"Added a note to case #{case_id}", text)
    await interaction.response.send_message(f"📝 Note added to case **#{case_id}**.", ephemeral=True)


bot.tree.add_command(case_group)


@bot.tree.command(name="note", description="Add an internal staff note on a member (not visible to them)")
@app_commands.describe(member="The member to note", text="The note content")
async def note(interaction: discord.Interaction, member: discord.Member, text: str):
    if not await check_access(interaction, "note", "manage_messages"): return
    notes_col.insert_one({
        "guild_id": str(interaction.guild_id),
        "user_id": str(member.id),
        "text": text,
        "moderator_id": str(interaction.user.id),
        "timestamp": datetime.datetime.utcnow(),
    })
    log_sanction(interaction.guild_id, member.id, "note", text, interaction.user.id)
    embed = discord.Embed(description=f"📝 Note added for **{member}**.", color=0x3399ff)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="notes", description="View internal staff notes for a member")
@app_commands.describe(member="The member whose notes to view")
async def notes(interaction: discord.Interaction, member: discord.Member):
    if not await check_access(interaction, "notes", "manage_messages"): return
    docs = list(notes_col.find({"guild_id": str(interaction.guild_id), "user_id": str(member.id)}).sort("timestamp", -1).limit(15))
    if not docs:
        embed = discord.Embed(description=f"✅ No notes for **{member}**.", color=0x00cc00)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    embed = discord.Embed(title=f"📝 Notes — {member}", color=0x3399ff)
    embed.set_thumbnail(url=member.display_avatar.url)
    for doc in docs:
        date_str = doc["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
        try:
            mod = await bot.fetch_user(int(doc["moderator_id"]))
            mod_str = str(mod)
        except Exception:
            mod_str = f"ID {doc['moderator_id']}"
        embed.add_field(name=f"{date_str} — by {mod_str}", value=doc["text"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="softban", description="Kick a member and delete their recent messages")
@app_commands.describe(member="The member to softban", reason="Reason", delete_days="Days of messages to delete (0-7)")
@app_commands.default_permissions(ban_members=True)
async def softban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: int = 1):
    if not await check_access(interaction, "softban", "ban_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't softban this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    delete_days = max(0, min(delete_days, 7))

    dm_sent, appeal_token = await send_sanction_dm(member, interaction.guild, "softban", reason)

    try:
        await member.ban(reason=f"[Softban] {reason}", delete_message_days=delete_days)
        await interaction.guild.unban(member, reason="Softban — automatic unban")
        case_id = log_sanction(interaction.guild_id, member.id, "softban", reason, interaction.user.id)
        if appeal_token:
            create_sanction_appeal(interaction.guild_id, case_id, "softban", member.id, str(member), reason, appeal_token)
        embed = discord.Embed(title="🧹 Member Softbanned", color=0xff6600)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.add_field(name="By", value=f"**{interaction.user}**", inline=True)
        embed.add_field(name="Messages deleted", value=f"Last {delete_days} day(s)", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        if not dm_sent:
            embed.set_footer(text="Couldn't DM the user (DMs closed or the bot is blocked).")
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        embed = discord.Embed(description=f"❌ I can't softban this member: {e}", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="purgeuser", description="Delete all stored data (warnings, sanctions, notes) for a member on this server")
@app_commands.describe(user_id="The Discord user ID whose data to erase")
@app_commands.default_permissions(administrator=True)
@has_admin()
async def purgeuser(interaction: discord.Interaction, user_id: str):
    guild_id = str(interaction.guild_id)
    warns_col.delete_many({"guild_id": guild_id, "user_id": user_id})
    sanctions_col.delete_many({"guild_id": guild_id, "user_id": user_id})
    notes_col.delete_many({"guild_id": guild_id, "user_id": user_id})
    embed = discord.Embed(
        description=f"🗑️ All stored data (warnings, sanctions, notes) for user ID `{user_id}` has been erased for this server.",
        color=0x00cc00,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# /banlist
@bot.tree.command(name="banlist", description="List all banned members")
@app_commands.default_permissions(ban_members=True)
async def banlist(interaction: discord.Interaction):
    if not await check_access(interaction, "banlist", "ban_members"): return
    await interaction.response.defer()
    bans = [entry async for entry in interaction.guild.bans()]
    if not bans:
        embed = discord.Embed(description="✅ No members are banned.", color=0x00cc00)
        await interaction.followup.send(embed=embed)
        return
    lines = [f"**{entry.user}** — {entry.reason or 'No reason'}" for entry in bans]
    view = PaginatedEmbedView("🔨 Banned Members", lines, color=0xff0000, author_id=interaction.user.id)
    embed = view.build_embed()

    if view.max_page > 0:
        await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed)

# /broadcast
@bot.tree.command(name="broadcast", description="Send a broadcast message")
@app_commands.default_permissions(manage_messages=True)
async def broadcast(interaction: discord.Interaction, message: str, mention: str = "none"):
    if not await check_access(interaction, "broadcast", "manage_messages"): return
    if mention == "everyone":
        ping = "@everyone"
    elif mention == "here":
        ping = "@here"
    else:
        role = discord.utils.get(interaction.guild.roles, name=mention)
        ping = role.mention if role else ""
    embed = discord.Embed(description=message, color=0x3399ff)
    embed.set_footer(text=f"📢 Sent by {interaction.user.top_role.name} · {interaction.user.name}")
    confirm = discord.Embed(description="✅ Broadcast sent!", color=0x00cc00)
    await interaction.response.send_message(embed=confirm, ephemeral=True)
    await interaction.channel.send(content=ping if ping else None, embed=embed)


@bot.tree.command(name="setup", description="Guided setup wizard for this server — admin only")
@app_commands.default_permissions(administrator=True)
@has_admin()
async def setup_wizard(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    current_logs = cfg.get("logs_channel", "logs")
    autorole = interaction.guild.get_role(cfg.get("autorole") or 0)
    appeal_channel = interaction.guild.get_channel(cfg.get("appeal_channel_id") or 0)

    embed = discord.Embed(
        title="⚙️ Nexus Setup",
        description=(
            "Pick your settings from the menus below — each one applies immediately, "
            "so you can stop at any point.\n\n"
            "Everything here is also available individually via `/config`."
        ),
        color=0x3399ff,
    )
    embed.add_field(name="Logs channel", value=f"#{current_logs}", inline=True)
    embed.add_field(name="Auto-role", value=autorole.name if autorole else "*(none)*", inline=True)
    embed.add_field(name="Appeals channel", value=appeal_channel.mention if appeal_channel else "*(disabled)*", inline=True)
    embed.add_field(name="Anti-nuke", value="🟢 On" if cfg.get("antinuke_enabled") else "⚪ Off", inline=True)
    embed.set_footer(text="This wizard stays open for 5 minutes.")

    view = SetupWizardView(interaction.guild_id, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="schedule", description="Schedule a message to be posted later")
@app_commands.describe(channel="Channel to post in", message="What to send", when="When to send it: 30m, 2h, 1d, 1w")
@app_commands.default_permissions(manage_messages=True)
async def schedule_command(interaction: discord.Interaction, channel: discord.TextChannel, message: str, when: str):
    if not await check_access(interaction, "schedule", "manage_messages"): return
    seconds = parse_duration(when)
    if seconds <= 0:
        embed = discord.Embed(description="❌ Invalid duration. Use `30m`, `2h`, `1d`, `1w`.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    send_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    schedule_announcement(interaction.guild_id, channel.id, message, send_at, interaction.user.id)
    embed = discord.Embed(
        description=f"📅 Scheduled for **{send_at.strftime('%Y-%m-%d %H:%M UTC')}** in {channel.mention}.",
        color=0x00cc00,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# =======================  CONFIG  ===========================
# ============================================================

config_group = app_commands.Group(name="config", description="Configure the bot (Administrator only)", default_permissions=discord.Permissions(administrator=True))

@config_group.command(name="logs", description="Set the logs channel")
@has_admin()
async def config_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    update_config(interaction.guild_id, "logs_channel", channel.name)
    embed = discord.Embed(title="✅ Logs Channel Updated", description=f"Logs will now be sent to {channel.mention}", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="autorole", description="Set or disable the auto-join role")
@app_commands.describe(role="Role given to new members; leave empty to disable")
@has_admin()
async def config_autorole(interaction: discord.Interaction, role: discord.Role = None):
    update_config(interaction.guild_id, "autorole", role.id if role else None)
    description = f"New members will get {role.mention}" if role else "Auto-role disabled."
    embed = discord.Embed(title="✅ Auto-Role Updated", description=description, color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="allow", description="Allow a role to use a specific command (e.g. ban, kick, roleadd) — server owner only")
@app_commands.describe(command="Command name, without the slash (e.g. ban)", role="Role allowed to use it")
@has_owner()
async def config_allow(interaction: discord.Interaction, command: str, role: discord.Role):
    command = command.strip().lower().lstrip("/")
    add_command_role(interaction.guild_id, command, role.id)
    embed = discord.Embed(title="✅ Permission Added", description=f"{role.mention} can now use `/{command}`.", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="disallow", description="Remove a role's permission for a specific command — server owner only")
@app_commands.describe(command="Command name, without the slash (e.g. ban)", role="Role to remove")
@has_owner()
async def config_disallow(interaction: discord.Interaction, command: str, role: discord.Role):
    command = command.strip().lower().lstrip("/")
    remove_command_role(interaction.guild_id, command, role.id)
    embed = discord.Embed(title="✅ Permission Removed", description=f"{role.mention} can no longer use `/{command}` (falls back to default Discord permissions).", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="apikey", description="Generate a new API key for this server's mobile app access")
@has_admin()
async def config_apikey(interaction: discord.Interaction):
    new_key = secrets.token_hex(16)
    update_config(interaction.guild_id, "api_key", new_key)
    embed = discord.Embed(
        title="🔑 New API Key Generated",
        description=f"||{new_key}||\n\nUse this in the mobile app to manage **this server only**. Keep it secret — generating a new one invalidates the old one.",
        color=0x00cc00,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="view", description="View current config")
@has_admin()
async def config_view(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    autorole = f"<@&{cfg['autorole']}>" if cfg.get("autorole") else "None"
    embed = discord.Embed(title="⚙️ Server Config", color=0x3399ff)
    embed.add_field(name="Logs Channel", value=f"#{cfg.get('logs_channel', 'logs')}", inline=True)
    embed.add_field(name="Auto-Role", value=autorole, inline=True)
    embed.add_field(name="API Key", value="✅ Set (use `/config apikey` to regenerate)" if cfg.get("api_key") else "❌ Not set — use `/config apikey` to generate one", inline=True)
    embed.add_field(name="Locked", value="🔒 Yes" if interaction.guild_id in bot.locked_guilds else "🔓 No", inline=True)
    embed.add_field(name="Dashboard Language", value=(cfg.get("language") or "auto (browser default)").upper() if cfg.get("language") else "Auto (defaults to English)", inline=True)
    command_roles = cfg.get("command_roles", {})
    if command_roles:
        lines = []
        for cmd, roles in command_roles.items():
            if not roles:
                continue
            mentions = " ".join(f"<@&{r}>" for r in roles)
            lines.append(f"**/{cmd}** → {mentions}")
        if not lines:
            embed.add_field(name="Command Permissions", value="None configured", inline=False)
        else:
            # Discord limite chaque champ d'embed à 1024 caractères : on
            # répartit sur plusieurs champs si la liste est trop longue
            # (le mass-add du dashboard peut vite dépasser cette limite).
            chunk = ""
            chunk_index = 1
            for line in lines:
                if len(chunk) + len(line) + 1 > 1000:
                    embed.add_field(
                        name="Command Permissions" if chunk_index == 1 else "Command Permissions (cont.)",
                        value=chunk,
                        inline=False,
                    )
                    chunk = ""
                    chunk_index += 1
                chunk += (line + "\n")
            if chunk:
                embed.add_field(
                    name="Command Permissions" if chunk_index == 1 else "Command Permissions (cont.)",
                    value=chunk,
                    inline=False,
                )
    else:
        embed.add_field(name="Command Permissions", value="None configured (using default Discord permissions)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@config_group.command(name="language", description="Set the default dashboard language for this server — server owner only")
@app_commands.describe(language="Default language shown on the web dashboard for this server")
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Français", value="fr"),
])
@has_owner()
async def config_language(interaction: discord.Interaction, language: app_commands.Choice[str]):
    update_config(interaction.guild_id, "language", language.value)
    await interaction.response.send_message(
        f"✅ Dashboard default language set to **{language.name}** for this server.",
        ephemeral=True,
    )


# Templates de permissions propres à un serveur (distincts des modèles
# prédéfinis intégrés au dashboard) : /config template set|list
template_group = app_commands.Group(
    name="template",
    description="Manage custom command-permission templates for this server — server owner only",
    parent=config_group,
)


@template_group.command(name="set", description="Create or update a custom permission template — server owner only")
@app_commands.describe(
    name="Template name (e.g. moderator, helper)",
    commands="Comma-separated command names (e.g. ban,kick,warn)",
)
@has_owner()
async def config_template_set(interaction: discord.Interaction, name: str, commands: str):
    name = name.strip().lower()[:32]
    if not name:
        await interaction.response.send_message("❌ Template name can't be empty.", ephemeral=True)
        return

    requested = [c.strip().lower() for c in commands.split(",") if c.strip()]
    valid = [c for c in requested if c in MODERATION_COMMANDS]
    invalid = [c for c in requested if c not in MODERATION_COMMANDS]

    if not valid:
        await interaction.response.send_message(
            f"❌ None of those commands are recognized. Available: {', '.join(sorted(MODERATION_COMMANDS))}",
            ephemeral=True,
        )
        return

    set_command_template(interaction.guild_id, name, valid)

    msg = f"✅ Template **{name}** saved with {len(valid)} command(s): {', '.join(f'/{c}' for c in valid)}"
    if invalid:
        msg += f"\n⚠️ Ignored unknown command(s): {', '.join(invalid)}"
    await interaction.response.send_message(msg, ephemeral=True)


@template_group.command(name="list", description="List custom permission templates for this server")
@has_admin()
async def config_template_list(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    custom_templates = cfg.get("command_templates", {})

    embed = discord.Embed(title="📋 Command Permission Templates", color=0x3399ff)

    builtin_lines = [f"**{TRANSLATIONS['en'].get(tpl['label_key'], tpl['label_key'])}** → " + ", ".join(f"/{c}" for c in tpl["commands"]) for tpl in COMMAND_TEMPLATES.values()]
    builtin_value = "\n".join(builtin_lines) or "None"
    embed.add_field(name="Built-in templates", value=builtin_value[:1000], inline=False)

    if custom_templates:
        custom_lines = [f"**{name}** → " + ", ".join(f"/{c}" for c in cmds) for name, cmds in custom_templates.items() if cmds]
        custom_value = "\n".join(custom_lines) or "None"
        if len(custom_value) > 1000:
            custom_value = custom_value[:990] + "\n…"
        embed.add_field(
            name="Server templates",
            value=custom_value,
            inline=False,
        )
        embed.set_footer(text="Manage these from /config template set, or apply them from the web dashboard.")
    else:
        embed.add_field(name="Server templates", value="None yet — create one with `/config template set`", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@config_group.command(name="membercount", description="Show the live member count in a voice channel's name — admin only")
@app_commands.describe(channel="Voice channel to rename with the live member count. Leave empty to disable.")
@has_admin()
async def config_membercount(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    if channel is None:
        update_config(interaction.guild_id, "member_count_channel_id", None)
        await interaction.response.send_message("📊 Member count channel disabled.", ephemeral=True)
        return
    update_config(interaction.guild_id, "member_count_channel_id", channel.id)
    await interaction.response.send_message(
        f"📊 {channel.mention} will now show the live member count (updates roughly every 10 minutes due to Discord rate limits).",
        ephemeral=True,
    )


@config_group.command(name="accountage", description="Auto-kick new joins whose Discord account is younger than X days — admin only. 0 to disable.")
@app_commands.describe(days="Minimum account age in days required to join (0 = disabled)")
@has_admin()
async def config_accountage(interaction: discord.Interaction, days: int):
    days = max(0, min(days, 365))
    update_config(interaction.guild_id, "min_account_age_days", days)
    if days == 0:
        await interaction.response.send_message("🛡️ Account age check disabled.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"🛡️ Accounts younger than **{days} day(s)** will now be kicked automatically on join.", ephemeral=True
        )


@config_group.command(name="automod", description="Adjust auto-moderation sensitivity — admin only. Leave a value empty to keep it unchanged.")
@app_commands.describe(
    spam_count="Messages within the spam window before it's flagged as spam (default 10)",
    spam_window="Spam detection window in seconds (default 5)",
    caps_ratio="Fraction of capital letters that counts as caps spam, 0-1 (default 0.7)",
    raid_count="Joins within the raid window before it's flagged as a raid (default 5)",
    raid_window="Raid detection window in seconds (default 10)",
)
@has_admin()
async def config_automod(
    interaction: discord.Interaction,
    spam_count: int = None,
    spam_window: int = None,
    caps_ratio: float = None,
    raid_count: int = None,
    raid_window: int = None,
):
    changes = []
    if spam_count is not None:
        update_config(interaction.guild_id, "automod_spam_count", max(1, spam_count))
        changes.append(f"spam count → {spam_count}")
    if spam_window is not None:
        update_config(interaction.guild_id, "automod_spam_window", max(1, spam_window))
        changes.append(f"spam window → {spam_window}s")
    if caps_ratio is not None:
        caps_ratio = max(0.1, min(caps_ratio, 1.0))
        update_config(interaction.guild_id, "automod_caps_ratio", caps_ratio)
        changes.append(f"caps ratio → {caps_ratio}")
    if raid_count is not None:
        update_config(interaction.guild_id, "automod_raid_count", max(2, raid_count))
        changes.append(f"raid count → {raid_count}")
    if raid_window is not None:
        update_config(interaction.guild_id, "automod_raid_window", max(2, raid_window))
        changes.append(f"raid window → {raid_window}s")

    if not changes:
        cfg = get_config(interaction.guild_id)
        await interaction.response.send_message(
            f"Current thresholds: spam {cfg.get('automod_spam_count', 10)} msgs/{cfg.get('automod_spam_window', 5)}s, "
            f"caps ratio {cfg.get('automod_caps_ratio', 0.7)}, "
            f"raid {cfg.get('automod_raid_count', 5)} joins/{cfg.get('automod_raid_window', 10)}s.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(f"🛡️ Updated: {', '.join(changes)}.", ephemeral=True)


@config_group.command(name="automodexempt", description="Add or remove a role exempt from AutoMod checks — admin only")
@app_commands.choices(action=[
    app_commands.Choice(name="Add exemption", value="add"),
    app_commands.Choice(name="Remove exemption", value="remove"),
])
@app_commands.describe(role="Role that should bypass AutoMod", action="Add or remove this exemption")
@has_admin()
async def config_automodexempt(interaction: discord.Interaction, role: discord.Role, action: str):
    if role.is_default():
        await interaction.response.send_message("❌ @everyone cannot be exempted from AutoMod.", ephemeral=True)
        return
    from config_store import get_config, update_config
    exempt_roles = set(map(str, get_config(interaction.guild_id).get("automod_exempt_role_ids", [])))
    role_id = str(role.id)
    if action == "add":
        if role_id in exempt_roles:
            await interaction.response.send_message(f"{role.mention} is already exempt from AutoMod.", ephemeral=True)
            return
        exempt_roles.add(role_id)
    else:
        if role_id not in exempt_roles:
            await interaction.response.send_message(f"{role.mention} is not currently exempt from AutoMod.", ephemeral=True)
            return
        exempt_roles.remove(role_id)
    update_config(interaction.guild_id, "automod_exempt_role_ids", sorted(exempt_roles))
    state = "now exempt from" if action == "add" else "no longer exempt from"
    await interaction.response.send_message(f"🛡️ {role.mention} is {state} AutoMod checks.", ephemeral=True)


@config_group.command(name="warnescalation", description="Auto-mute/kick members once they reach a certain number of warnings — admin only")
@app_commands.describe(
    enabled="Turn auto-escalation on or off",
    mute_at="Warning count that triggers an auto-mute (default 3)",
    mute_minutes="How long the auto-mute lasts, in minutes (default 10)",
    kick_at="Warning count that triggers an auto-kick (default 5)",
)
@has_admin()
async def config_warnescalation(
    interaction: discord.Interaction,
    enabled: bool,
    mute_at: int = 3,
    mute_minutes: int = 10,
    kick_at: int = 5,
):
    update_config(interaction.guild_id, "warn_escalation_enabled", enabled)
    update_config(interaction.guild_id, "warn_mute_threshold", max(1, mute_at))
    update_config(interaction.guild_id, "warn_mute_minutes", max(1, mute_minutes))
    update_config(interaction.guild_id, "warn_kick_threshold", max(mute_at + 1, kick_at))
    if enabled:
        await interaction.response.send_message(
            f"⚠️ Escalation enabled: **{mute_at}** warnings → {mute_minutes} min mute, **{kick_at}** warnings → kick.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message("⚠️ Warn escalation disabled.", ephemeral=True)


@config_group.command(name="jointocreate", description="Set up a 'Join to Create' voice channel — admin only. Leave empty to disable.")
@app_commands.describe(channel="Voice channel that spawns a temporary channel for whoever joins it")
@has_admin()
async def config_jointocreate(interaction: discord.Interaction, channel: discord.VoiceChannel = None):
    if channel is None:
        update_config(interaction.guild_id, "jtc_channel_id", None)
        await interaction.response.send_message("🔊 Join-to-Create disabled.", ephemeral=True)
        return
    update_config(interaction.guild_id, "jtc_channel_id", channel.id)
    await interaction.response.send_message(
        f"🔊 Joining {channel.mention} will now create a temporary voice channel (deleted automatically once empty).",
        ephemeral=True,
    )


# Menu de rôles en dropdown : /config rolemenu additem|clearitems|post — admin only
rolemenu_group = app_commands.Group(
    name="rolemenu",
    description="Build and post a dropdown role-selection menu — admin only",
    parent=config_group,
)


@rolemenu_group.command(name="additem", description="Add a role to the pending role menu (build it, then /config rolemenu post) — admin only")
@app_commands.describe(role="Role to add to the menu", label="Text shown in the dropdown (defaults to the role name)")
@has_admin()
async def config_rolemenu_additem(interaction: discord.Interaction, role: discord.Role, label: str = None):
    cfg = get_config(interaction.guild_id)
    items = cfg.get("pending_role_menu_items", [])
    if len(items) >= 25:
        await interaction.response.send_message("❌ A dropdown menu can only have 25 options max.", ephemeral=True)
        return
    if any(item["role_id"] == role.id for item in items):
        await interaction.response.send_message(f"**{role.name}** is already in the pending menu.", ephemeral=True)
        return
    items.append({"role_id": role.id, "label": (label or role.name)[:100]})
    update_config(interaction.guild_id, "pending_role_menu_items", items)
    await interaction.response.send_message(f"➕ Added **{role.name}** ({len(items)}/25). Run `/config rolemenu post` when ready.", ephemeral=True)


@rolemenu_group.command(name="clearitems", description="Clear the pending role menu items — admin only")
@has_admin()
async def config_rolemenu_clearitems(interaction: discord.Interaction):
    update_config(interaction.guild_id, "pending_role_menu_items", [])
    await interaction.response.send_message("🗑️ Pending role menu cleared.", ephemeral=True)


@rolemenu_group.command(name="post", description="Post the pending role menu as a dropdown — admin only")
@app_commands.describe(channel="Channel to post the menu in", title="Title shown above the menu")
@has_admin()
async def config_rolemenu_post(interaction: discord.Interaction, channel: discord.TextChannel, title: str = "Choose your roles"):
    cfg = get_config(interaction.guild_id)
    items = cfg.get("pending_role_menu_items", [])
    if not items:
        await interaction.response.send_message("❌ No pending items — add some first with `/config rolemenu additem`.", ephemeral=True)
        return

    options = [discord.SelectOption(label=item["label"], value=str(item["role_id"])) for item in items]
    embed = discord.Embed(title=f"🎭 {title}", description="Pick the roles you want from the dropdown below.", color=0x3399ff)
    view = discord.ui.View(timeout=None)
    view.add_item(RoleMenuSelect(options))
    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Couldn't post the menu: {e}", ephemeral=True)
        return

    role_menus_col.insert_one({
        "guild_id": str(interaction.guild_id),
        "message_id": str(message.id),
        "channel_id": str(channel.id),
        "items": items,
    })
    update_config(interaction.guild_id, "pending_role_menu_items", [])
    await interaction.response.send_message(f"✅ Role menu posted in {channel.mention}.", ephemeral=True)


@config_group.command(name="appeals", description="Set the channel where sanction appeals are reviewed — admin only. Leave empty to disable.")
@app_commands.describe(channel="Channel where submitted ban appeals will be posted for staff review")
@has_admin()
async def config_appeals(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if channel is None:
        update_config(interaction.guild_id, "appeal_channel_id", None)
        await interaction.response.send_message("📨 Sanction appeals disabled — future sanction DMs won't include an appeal button.", ephemeral=True)
        return
    update_config(interaction.guild_id, "appeal_channel_id", channel.id)
    await interaction.response.send_message(
        f"📨 Sanction appeals will now be reviewed in {channel.mention}. Future sanction DMs will include an appeal button.",
        ephemeral=True,
    )


@config_group.command(name="boostmessage", description="Set where boost thank-you messages are posted — admin only. Leave empty to use the logs channel.")
@app_commands.describe(channel="Channel for boost thank-you messages")
@has_admin()
async def config_boostmessage(interaction: discord.Interaction, channel: discord.TextChannel = None):
    update_config(interaction.guild_id, "boost_channel_id", channel.id if channel else None)
    where = channel.mention if channel else "the logs channel (default)"
    await interaction.response.send_message(f"🎉 Boost thank-you messages will be posted in {where}.", ephemeral=True)


@config_group.command(name="welcome", description="Set the welcome channel and message — admin only. Enable via /feature welcome_banner.")
@app_commands.describe(
    channel="Channel where the welcome message + banner (member's profile picture) get posted",
    message="Optional text. Placeholders: {user} {username} {server} {count}. Type 'default' to reset.",
)
@has_admin()
async def config_welcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str = None):
    update_config(interaction.guild_id, "welcome_channel_id", channel.id)
    note = ""
    if message is not None:
        if message.strip().lower() == "default" or not message.strip():
            update_config(interaction.guild_id, "welcome_message", None)
            note = f"\n✏️ Message reset to the default: `{DEFAULT_WELCOME_MESSAGE}`"
        else:
            message = message.strip()[:MAX_WELCOME_MESSAGE_LENGTH]
            update_config(interaction.guild_id, "welcome_message", message)
            note = f"\n✏️ Message set to: {message}"
    await interaction.response.send_message(
        f"🖼️ Welcome messages will be posted in {channel.mention} once enabled (`/feature enable welcome_banner`).{note}",
        ephemeral=True)


verification_group = app_commands.Group(name="verification", description="Set up member verification — admin only", parent=config_group)


@verification_group.command(name="setup", description="Create/assign the Verified and Unverified roles — admin only")
@app_commands.describe(
    verified_role="Existing role to use as 'Verified' (leave empty to create a new one)",
    unverified_role="Existing role to use as 'Unverified' (leave empty to create a new one)",
)
@has_admin()
async def verification_setup(interaction: discord.Interaction, verified_role: discord.Role = None, unverified_role: discord.Role = None):
    guild = interaction.guild
    if verified_role is None:
        verified_role = discord.utils.get(guild.roles, name="Verified") or await guild.create_role(name="Verified", reason="Nexus verification setup")
    if unverified_role is None:
        unverified_role = discord.utils.get(guild.roles, name="Unverified") or await guild.create_role(name="Unverified", reason="Nexus verification setup")
    update_config(interaction.guild_id, "verified_role_id", verified_role.id)
    update_config(interaction.guild_id, "unverified_role_id", unverified_role.id)
    await interaction.response.send_message(
        f"✅ Verification roles set: {verified_role.mention} / {unverified_role.mention}.\n"
        f"**You still need to configure channel permissions yourself** (deny {unverified_role.mention} access to normal channels, "
        f"allow it only in your verification channel) — Nexus only manages the roles, not channel visibility.\n"
        f"Next: run `/config verification post` in your verification channel, then `/feature enable verification`.",
        ephemeral=True,
    )


@verification_group.command(name="autorole", description="Set an extra role granted after verification — admin only")
@app_commands.describe(role="Extra role to give after successful verification; leave empty to disable")
@has_admin()
async def verification_autorole(interaction: discord.Interaction, role: discord.Role = None):
    update_config(interaction.guild_id, "verification_autorole_id", role.id if role else None)
    state = f"will be granted after verification: {role.mention}" if role else "disabled"
    await interaction.response.send_message(f"✅ Verification auto-role {state}.", ephemeral=True)


@verification_group.command(name="method", description="Choose how members verify: one-click button or Cloudflare Turnstile — admin only")
@app_commands.describe(method="button = one click; turnstile = Cloudflare human check on a web page")
@app_commands.choices(method=[
    app_commands.Choice(name="Button (one click)", value="button"),
    app_commands.Choice(name="Cloudflare Turnstile (web check)", value="turnstile"),
])
@has_admin()
async def verification_method(interaction: discord.Interaction, method: app_commands.Choice[str]):
    if method.value == "turnstile" and not turnstile_configured():
        await interaction.response.send_message(
            "❌ Turnstile isn't configured on this bot yet (it needs `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` and a public URL). Ask the bot owner.",
            ephemeral=True)
        return
    update_config(interaction.guild_id, "verification_method", method.value)
    label = "Cloudflare Turnstile" if method.value == "turnstile" else "one-click button"
    await interaction.response.send_message(f"✅ Members will now verify with: **{label}**.", ephemeral=True)


@verification_group.command(name="settings", description="Tune web verification: link lifetime, attempts, auto-kick, minimum account age — admin only")
@app_commands.describe(
    link_minutes="How long the personal link stays valid, in minutes (2-60, default 10)",
    max_attempts="Failed checks before the link dies (1-10, default 3)",
    kick_hours="Kick members still Unverified after N hours (0 = never, max 168)",
    min_account_days="Minimum Discord account age to verify, in days (0 = no limit, max 365)",
)
@has_admin()
async def verification_settings(interaction: discord.Interaction, link_minutes: int = None, max_attempts: int = None,
                                kick_hours: int = None, min_account_days: int = None):
    requested = {
        "verification_link_minutes": ("Link lifetime (minutes)", link_minutes),
        "verification_max_attempts": ("Max attempts", max_attempts),
        "verification_kick_hours": ("Auto-kick after (hours, 0 = never)", kick_hours),
        "verification_min_account_days": ("Minimum account age (days, 0 = none)", min_account_days),
    }
    errors = []
    for key, (label, value) in requested.items():
        lo, hi = SETTINGS_LIMITS[key]
        if value is not None and not lo <= value <= hi:
            errors.append(f"{label} must be between {lo} and {hi}.")
    if errors:  # tout ou rien : aucune valeur n'est enregistrée si l'une est invalide
        await interaction.response.send_message("❌ " + " ".join(errors), ephemeral=True)
        return
    for key, (_, value) in requested.items():
        if value is not None:
            update_config(interaction.guild_id, key, value)
    current = get_verification_settings(interaction.guild_id)
    lines = [f"• {label}: **{current[key]}**" for key, (label, _) in requested.items()]
    await interaction.response.send_message("✅ Web verification settings:\n" + "\n".join(lines), ephemeral=True)


@verification_group.command(name="post", description="Post the Verify button in this channel — admin only")
@has_admin()
async def verification_post(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    if not cfg.get("verified_role_id") or not cfg.get("unverified_role_id"):
        await interaction.response.send_message("Run `/config verification setup` first.", ephemeral=True)
        return
    embed = discord.Embed(title="👋 Verify to get access", description="Click the button below to verify and unlock the rest of the server.", color=0x3399ff)
    await interaction.channel.send(embed=embed, view=VerificationView())
    await interaction.response.send_message("✅ Verification button posted.", ephemeral=True)


@config_group.command(name="trapchannel", description="Set the trap channel — any message there = instant softban. Admin only.")
@app_commands.describe(channel="Channel where ANY message posted gets the author instantly softbanned")
@has_admin()
async def config_trapchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    update_config(interaction.guild_id, "trap_channel_id", channel.id)
    await interaction.response.send_message(
        f"🪤 {channel.mention} is now set as the trap channel — anyone posting there (except staff) gets instantly softbanned.\n"
        f"**Make sure regular members can't normally see/post in it** (it only works if nobody legit ever has a reason to post there). "
        f"Enable with `/feature enable trap_channel`.",
        ephemeral=True,
    )


@config_group.command(name="quarantine", description="Configure the quarantine hold for new accounts — admin only. Enable via /feature.")
@app_commands.describe(
    role="Role to apply while a member is held for review",
    channel="Channel where review alerts are posted",
    suspiciondays="Accounts younger than this (days) get quarantined (default 7)",
    holdminutes="Minutes before a reminder ping if nobody reviewed it yet (default 30)",
)
@has_admin()
async def config_quarantine(interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel, suspiciondays: int = 7, holdminutes: int = 30):
    update_config(interaction.guild_id, "quarantine_role_id", role.id)
    update_config(interaction.guild_id, "quarantine_channel_id", channel.id)
    update_config(interaction.guild_id, "quarantine_suspicion_days", suspiciondays)
    update_config(interaction.guild_id, "quarantine_hold_minutes", holdminutes)
    await interaction.response.send_message(
        f"🔎 Quarantine configured: accounts under {suspiciondays}d old get {role.mention}, reviewed in {channel.mention} "
        f"(reminder after {holdminutes} min). Enable with `/feature enable quarantine`.",
        ephemeral=True,
    )


@config_group.command(name="antinuke", description="Set the anti-nuke sensitivity threshold — admin only. Use /feature enable|disable to turn it on/off.")
@app_commands.describe(threshold="Destructive actions by the same person within 60s that trigger it (default 5)")
@has_admin()
async def config_antinuke(interaction: discord.Interaction, threshold: int = 5):
    threshold = max(2, min(threshold, 20))
    update_config(interaction.guild_id, "antinuke_threshold", threshold)
    await interaction.response.send_message(
        f"🛡️ Anti-nuke threshold set to **{threshold}+** destructive actions within 60 seconds. "
        f"Use `/feature enable name:antinuke` to turn anti-nuke on if it isn't already.",
        ephemeral=True,
    )


# Messages épinglés (sticky) : /config sticky set|remove — admin only
sticky_group = app_commands.Group(
    name="sticky",
    description="Manage a sticky message that stays at the bottom of a channel — admin only",
    parent=config_group,
)


@sticky_group.command(name="set", description="Post a sticky message that keeps reappearing at the bottom of a channel — admin only")
@app_commands.describe(channel="Channel to stick the message in", content="The message content to keep at the bottom")
@has_admin()
async def config_sticky_set(interaction: discord.Interaction, channel: discord.TextChannel, content: str):
    set_sticky_message(interaction.guild_id, channel.id, content[:1900])
    await interaction.response.send_message(f"📌 Sticky message set for {channel.mention}.", ephemeral=True)


@sticky_group.command(name="remove", description="Remove the sticky message from a channel — admin only")
@app_commands.describe(channel="Channel to remove the sticky message from")
@has_admin()
async def config_sticky_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    existing = get_sticky_message(channel.id)
    if not existing:
        await interaction.response.send_message(f"No sticky message is set for {channel.mention}.", ephemeral=True)
        return
    last_id = existing.get("last_message_id")
    if last_id:
        try:
            old_msg = await channel.fetch_message(int(last_id))
            await old_msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass
    remove_sticky_message(channel.id)
    await interaction.response.send_message(f"📌 Sticky message removed from {channel.mention}.", ephemeral=True)


# Liste de mots interdits personnalisée, avec import d'un template pré-fait
# depuis un dépôt GitHub public et largement utilisé pour ce genre de liste.
BADWORDS_TEMPLATE_URLS = {
    "en": "https://raw.githubusercontent.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words/master/en",
    "fr": "https://raw.githubusercontent.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words/master/fr",
}

badwords_group = app_commands.Group(
    name="badwords",
    description="Manage a custom list of banned words for this server — admin only",
    parent=config_group,
)


@badwords_group.command(name="add", description="Add a word or phrase to the banned words list — admin only")
@app_commands.describe(word="Word or phrase to ban (matched as a whole word, case-insensitive)")
@has_admin()
async def config_badwords_add(interaction: discord.Interaction, word: str):
    word = word.strip().lower()
    if not word:
        await interaction.response.send_message("❌ Can't add an empty word.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    banned = set(cfg.get("banned_words", []))
    banned.add(word)
    update_config(interaction.guild_id, "banned_words", sorted(banned))
    await interaction.response.send_message(f"🚫 Added **{word}** to the banned words list ({len(banned)} total).", ephemeral=True)


@badwords_group.command(name="remove", description="Remove a word from the banned words list — admin only")
@app_commands.describe(word="Word or phrase to remove")
@has_admin()
async def config_badwords_remove(interaction: discord.Interaction, word: str):
    word = word.strip().lower()
    cfg = get_config(interaction.guild_id)
    banned = set(cfg.get("banned_words", []))
    if word not in banned:
        await interaction.response.send_message(f"**{word}** isn't in the list.", ephemeral=True)
        return
    banned.discard(word)
    update_config(interaction.guild_id, "banned_words", sorted(banned))
    await interaction.response.send_message(f"✅ Removed **{word}** from the banned words list ({len(banned)} remaining).", ephemeral=True)


@badwords_group.command(name="list", description="Show the current banned words list")
@has_admin()
async def config_badwords_list(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    banned = sorted(cfg.get("banned_words", []))
    if not banned:
        await interaction.response.send_message("No banned words configured yet.", ephemeral=True)
        return
    embed = discord.Embed(title=f"🚫 Banned Words ({len(banned)})", color=0xff6600)
    text = ", ".join(banned)
    if len(text) <= 1000:
        embed.description = text
    else:
        # Discord limite un champ/description d'embed à 1024 caractères
        chunk, chunks = "", []
        for w in banned:
            if len(chunk) + len(w) + 2 > 1000:
                chunks.append(chunk)
                chunk = ""
            chunk += w + ", "
        if chunk:
            chunks.append(chunk)
        for i, c in enumerate(chunks):
            embed.add_field(name="Words" if i == 0 else "Words (cont.)", value=c.rstrip(", "), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@badwords_group.command(name="import", description="Import a pre-made banned words list from a public GitHub template — admin only")
@app_commands.describe(language="Which pre-made list to import (adds to your existing list, doesn't replace it)")
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Français", value="fr"),
])
@has_admin()
async def config_badwords_import(interaction: discord.Interaction, language: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    url = BADWORDS_TEMPLATE_URLS.get(language.value)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        imported = {w.strip().lower() for w in r.text.splitlines() if w.strip()}
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't fetch the template list: {e}", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    banned = set(cfg.get("banned_words", []))
    before_count = len(banned)
    banned |= imported
    update_config(interaction.guild_id, "banned_words", sorted(banned))
    await interaction.followup.send(
        f"🚫 Imported **{len(imported)}** words from the {language.name} template "
        f"({len(banned) - before_count} new, {len(banned)} total).\n"
        f"Source: github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words (CC-BY-4.0)",
        ephemeral=True,
    )


# Ticket system commands are available directly under /ticket.
ticket_group = app_commands.Group(
    name="ticket",
    description="Manage the ticket system and current tickets",
)


@ticket_group.command(name="setup", description="Post the ticket panel and configure where tickets go — server owner only")
@app_commands.describe(
    panel_channel="Channel where the 'Open Ticket' button will be posted",
    category="Category where new ticket channels will be created",
    support_role="Role that can see and manage tickets",
    archive_category="Category to move closed tickets into instead of deleting them (optional)",
)
@has_owner()
async def config_ticket_setup(
    interaction: discord.Interaction,
    panel_channel: discord.TextChannel,
    category: discord.CategoryChannel,
    support_role: discord.Role,
    archive_category: discord.CategoryChannel = None,
):
    update_config(interaction.guild_id, "ticket_category_id", category.id)
    update_config(interaction.guild_id, "ticket_support_role_id", support_role.id)
    update_config(interaction.guild_id, "ticket_archive_category_id", archive_category.id if archive_category else None)
    update_config(interaction.guild_id, "tickets_enabled", True)

    embed = discord.Embed(
        title="🎫 Need help?",
        description="Click the button below to open a private ticket with our team.",
        color=0x3399ff,
    )
    await panel_channel.send(embed=embed, view=TicketPanelView())
    archive_txt = (
        f" Closed tickets will be archived under **{archive_category.name}** instead of deleted."
        if archive_category else
        " Closed tickets will be deleted (no archive category set — pass one to keep a history instead)."
    )
    await interaction.response.send_message(
        f"✅ Ticket panel posted in {panel_channel.mention}. New tickets are created under **{category.name}**, visible to {support_role.mention}.{archive_txt}",
        ephemeral=True,
    )


@ticket_group.command(name="disable", description="Disable the ticket system — server owner only")
@has_owner()
async def config_ticket_disable(interaction: discord.Interaction):
    update_config(interaction.guild_id, "tickets_enabled", False)
    await interaction.response.send_message("🎫 Ticket system disabled. Existing open tickets are unaffected.", ephemeral=True)



# Multiple independently configured ticket panels.
# Keep these as direct commands: discord.py does not allow nested groups.


@ticket_group.command(name="panel-create", description="Create an additional ticket panel — server owner only")
@app_commands.describe(
    name="Panel name, e.g. Support, Reports or Partnerships",
    panel_channel="Channel where this panel will be posted",
    category="Category where tickets from this panel will be created",
    support_role="Role that can view and manage these tickets",
    archive_category="Optional category for closed tickets",
    opening_message="Optional message shown inside newly created tickets",
)
@has_owner()
async def config_ticket_panel_create(
    interaction: discord.Interaction,
    name: str,
    panel_channel: discord.TextChannel,
    category: discord.CategoryChannel,
    support_role: discord.Role,
    archive_category: discord.CategoryChannel = None,
    opening_message: str = None,
):
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 40:
        await interaction.response.send_message("Panel name must contain 1–40 characters.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    panels = list(cfg.get("ticket_panels") or [])
    if any(str(p.get("name", "")).casefold() == clean_name.casefold() for p in panels):
        await interaction.response.send_message("A ticket panel with that name already exists.", ephemeral=True)
        return
    panel_id = secrets.token_hex(4)
    panel = {
        "id": panel_id, "name": clean_name, "category_id": category.id,
        "support_role_id": support_role.id,
        "archive_category_id": archive_category.id if archive_category else None,
        "opening_message": (opening_message or "").strip()[:1000],
        "panel_channel_id": panel_channel.id,
    }
    panels.append(panel)
    update_config(interaction.guild_id, "ticket_panels", panels)
    embed = discord.Embed(title=f"🎫 {clean_name}", description="Click the button below to open a private ticket.", color=0x3399ff)
    embed.set_footer(text=f"nexus-ticket-panel:{panel_id}")
    try:
        await panel_channel.send(embed=embed, view=TicketPanelView())
    except discord.HTTPException as e:
        update_config(interaction.guild_id, "ticket_panels", [p for p in panels if p.get("id") != panel_id])
        await interaction.response.send_message(f"Couldn't post the panel: {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Panel {clean_name} created in {panel_channel.mention}. Tickets route to {category.name} and {support_role.mention}.",
        ephemeral=True,
    )


@ticket_group.command(name="panel-list", description="List configured ticket panels — server owner only")
@has_owner()
async def config_ticket_panel_list(interaction: discord.Interaction):
    panels = get_config(interaction.guild_id).get("ticket_panels") or []
    if not panels:
        await interaction.response.send_message("No additional ticket panels configured. Use /ticket panel-create.", ephemeral=True)
        return
    lines = []
    for panel in panels:
        channel = interaction.guild.get_channel(int(panel.get("panel_channel_id") or 0))
        category = interaction.guild.get_channel(int(panel.get("category_id") or 0))
        lines.append(
            f"• **{discord.utils.escape_markdown(str(panel.get('name', 'Unnamed')))}** "
            f"— panel: {channel.mention if channel else 'missing channel'}, "
            f"category: {category.name if category else 'missing category'} ({panel.get('id', '?')})"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@ticket_group.command(name="panel-remove", description="Remove a ticket panel by its ID — server owner only")
@app_commands.describe(panel_id="ID shown by /ticket panel-list")
@has_owner()
async def config_ticket_panel_remove(interaction: discord.Interaction, panel_id: str):
    cfg = get_config(interaction.guild_id)
    panels = list(cfg.get("ticket_panels") or [])
    remaining = [p for p in panels if str(p.get("id")) != panel_id.strip()]
    if len(remaining) == len(panels):
        await interaction.response.send_message("Panel ID not found. Run /ticket panel list.", ephemeral=True)
        return
    update_config(interaction.guild_id, "ticket_panels", remaining)

    await interaction.response.send_message("Panel removed. Existing tickets are unaffected; delete its posted message manually.", ephemeral=True)


@ticket_group.command(name="panel-questions", description="Set a ticket panel's custom intake questions — server owner only")
@app_commands.describe(
    panel_id="Panel ID shown by /ticket panel-list",
    question1="First required question",
    question2="Optional second question",
    question3="Optional third question",
    question4="Optional fourth question",
    question5="Optional fifth question",
)
@has_owner()
async def config_ticket_panel_questions(
    interaction: discord.Interaction,
    panel_id: str,
    question1: str,
    question2: str = None,
    question3: str = None,
    question4: str = None,
    question5: str = None,
):
    cfg = get_config(interaction.guild_id)
    panels = list(cfg.get("ticket_panels") or [])
    panel = next((p for p in panels if str(p.get("id")) == panel_id.strip()), None)
    if panel is None:
        await interaction.response.send_message("Panel ID not found. Run /ticket panel-list.", ephemeral=True)
        return
    questions = [q.strip() for q in (question1, question2, question3, question4, question5) if q and q.strip()]
    if any(len(q) > 45 for q in questions):
        await interaction.response.send_message("Each question must be 45 characters or fewer.", ephemeral=True)
        return
    panel["form_questions"] = questions
    update_config(interaction.guild_id, "ticket_panels", panels)
    await interaction.response.send_message(
        f"✅ Updated the intake form for **{discord.utils.escape_markdown(str(panel.get('name', 'Panel')))}** with {len(questions)} question(s).",
        ephemeral=True,
    )


@ticket_group.command(name="reopen", description="Reopen an archived ticket")
async def config_ticket_reopen(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Use this command inside an archived ticket channel.", ephemeral=True)
        return
    opener_id, panel_id, topic_support_role_id, _ = _ticket_topic_parts(channel.topic)
    if not opener_id or not channel.name.startswith("closed-"):
        await interaction.response.send_message("This isn't an archived Nexus ticket.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    panel = _find_ticket_panel(cfg, panel_id)
    support_role_id = (panel or {}).get("support_role_id") or topic_support_role_id or cfg.get("ticket_support_role_id")
    support_role = interaction.guild.get_role(int(support_role_id or 0))
    if not (interaction.user.guild_permissions.manage_channels or (support_role and support_role in interaction.user.roles)):
        await interaction.response.send_message("Only ticket support staff or server staff can reopen tickets.", ephemeral=True)
        return
    category_id = (panel or {}).get("category_id") or cfg.get("ticket_category_id")
    category = interaction.guild.get_channel(int(category_id or 0))
    if not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message("The original ticket category is missing. Reconfigure the ticket panel first.", ephemeral=True)
        return
    opener = interaction.guild.get_member(int(opener_id))
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    if opener:
        overwrites[opener] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    assignee_id = _ticket_topic_value(channel.topic, "assignee")
    assignee = interaction.guild.get_member(int(assignee_id)) if assignee_id and assignee_id.isdigit() else None
    if assignee:
        overwrites[assignee] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    new_name = channel.name[len("closed-"):]
    try:
        await channel.edit(
            category=category,
            name=new_name,
            overwrites=overwrites,
            topic=_ticket_topic_update(channel.topic, "status", "open"),
            reason=f"Ticket reopened by {interaction.user}",
        )
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Couldn't reopen this ticket: {exc}", ephemeral=True)
        return
    await interaction.response.send_message(f"🔓 Ticket reopened by {interaction.user.mention}.", allowed_mentions=discord.AllowedMentions(users=True))


@ticket_group.command(name="assign", description="Assign the current ticket to a staff member")
@app_commands.describe(member="Staff member responsible for this ticket")
async def config_ticket_assign(interaction: discord.Interaction, member: discord.Member):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Use this command inside a ticket channel.", ephemeral=True)
        return
    opener_id, panel_id, topic_support_role_id, _ = _ticket_topic_parts(channel.topic)
    if not opener_id:
        await interaction.response.send_message("This channel isn't a recognized Nexus ticket.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    panel = _find_ticket_panel(cfg, panel_id)
    support_role_id = (panel or {}).get("support_role_id") or topic_support_role_id or cfg.get("ticket_support_role_id")
    support_role = interaction.guild.get_role(int(support_role_id or 0))
    is_support = support_role is not None and support_role in interaction.user.roles
    if not (is_support or interaction.user.guild_permissions.manage_channels):
        await interaction.response.send_message("Only the ticket support team or server staff can assign tickets.", ephemeral=True)
        return
    if member.bot or str(member.id) == opener_id:
        await interaction.response.send_message("Choose a staff member, not a bot or the ticket opener.", ephemeral=True)
        return
    member_is_staff = (support_role is not None and support_role in member.roles) or member.guild_permissions.manage_channels
    if not member_is_staff:
        await interaction.response.send_message("The assigned member must have the ticket support role or Manage Channels permission.", ephemeral=True)
        return
    try:
        await channel.set_permissions(
            member,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            reason=f"Ticket assigned to {member} by {interaction.user}",
        )
        await channel.edit(topic=_ticket_topic_update(channel.topic, "assignee", str(member.id)))
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Couldn't assign this ticket: {exc}", ephemeral=True)
        return
    embed = discord.Embed(
        title="Ticket assigned",
        description=f"This ticket is now assigned to {member.mention}.",
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed)


@ticket_group.command(name="unassign", description="Remove the current ticket assignment")
async def config_ticket_unassign(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Use this command inside a ticket channel.", ephemeral=True)
        return
    opener_id, panel_id, topic_support_role_id, _ = _ticket_topic_parts(channel.topic)
    if not opener_id:
        await interaction.response.send_message("This channel isn't a recognized Nexus ticket.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    panel = _find_ticket_panel(cfg, panel_id)
    support_role_id = (panel or {}).get("support_role_id") or topic_support_role_id or cfg.get("ticket_support_role_id")
    support_role = interaction.guild.get_role(int(support_role_id or 0))
    is_support = support_role is not None and support_role in interaction.user.roles
    if not (is_support or interaction.user.guild_permissions.manage_channels):
        await interaction.response.send_message("Only the ticket support team or server staff can unassign tickets.", ephemeral=True)
        return
    assignee_id = _ticket_topic_value(channel.topic, "assignee")
    if not assignee_id or not assignee_id.isdigit():
        await interaction.response.send_message("This ticket isn't assigned to anyone.", ephemeral=True)
        return
    assignee = interaction.guild.get_member(int(assignee_id))
    try:
        if assignee is not None:
            await channel.set_permissions(
                assignee,
                overwrite=None,
                reason=f"Ticket unassigned by {interaction.user}",
            )
        await channel.edit(topic=_ticket_topic_update(channel.topic, "assignee", ""))
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Couldn't unassign this ticket: {exc}", ephemeral=True)
        return
    await interaction.response.send_message("Ticket assignment removed.")


@ticket_group.command(name="priority", description="Set the priority of the current ticket")
@app_commands.choices(priority=[
    app_commands.Choice(name="Low", value="low"),
    app_commands.Choice(name="Normal", value="normal"),
    app_commands.Choice(name="High", value="high"),
    app_commands.Choice(name="Urgent", value="urgent"),
])
async def config_ticket_priority(interaction: discord.Interaction, priority: app_commands.Choice[str]):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Use this command inside a ticket channel.", ephemeral=True)
        return
    opener_id, panel_id, topic_support_role_id, _ = _ticket_topic_parts(channel.topic)
    if not opener_id:
        await interaction.response.send_message("This channel isn't a recognized Nexus ticket.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    panel = _find_ticket_panel(cfg, panel_id)
    support_role_id = (panel or {}).get("support_role_id") or topic_support_role_id or cfg.get("ticket_support_role_id")
    support_role = interaction.guild.get_role(int(support_role_id or 0))
    is_support = support_role is not None and support_role in interaction.user.roles
    is_admin = interaction.user.guild_permissions.manage_channels
    if not (is_support or is_admin):
        await interaction.response.send_message("Only the ticket support team or server staff can change its priority.", ephemeral=True)
        return
    new_topic = _ticket_topic_update(channel.topic, "priority", priority.value)
    try:
        await channel.edit(topic=new_topic, reason=f"Ticket priority changed to {priority.value} by {interaction.user}")
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Couldn't update the ticket priority: {exc}", ephemeral=True)
        return
    labels = {"low": "🟢 Low", "normal": "🔵 Normal", "high": "🟠 High", "urgent": "🔴 Urgent"}
    embed = discord.Embed(
        title="Ticket priority updated",
        description=f"Priority set to **{labels[priority.value]}** by {interaction.user.mention}.",
        color={"low": 0x57F287, "normal": 0x5865F2, "high": 0xFEE75C, "urgent": 0xED4245}[priority.value],
    )
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(ticket_group)


# Liste noire de domaines/liens — /config domains add|remove|list
domains_group = app_commands.Group(
    name="domains",
    description="Manage a blocked domains/links list for this server — admin only",
    parent=config_group,
)


@domains_group.command(name="add", description="Block a domain (and its subdomains) — admin only")
@app_commands.describe(domain="Domain to block, e.g. scam-site.com")
@has_admin()
async def config_domains_add(interaction: discord.Interaction, domain: str):
    domain = domain.strip().lower().removeprefix("http://").removeprefix("https://").removeprefix("www.").split("/")[0]
    if not domain:
        await interaction.response.send_message("❌ Can't add an empty domain.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    banned = set(cfg.get("banned_domains", []))
    banned.add(domain)
    update_config(interaction.guild_id, "banned_domains", sorted(banned))
    await interaction.response.send_message(f"🚫 Blocked **{domain}** (and its subdomains). {len(banned)} domain(s) total.", ephemeral=True)


@domains_group.command(name="remove", description="Unblock a domain — admin only")
@app_commands.describe(domain="Domain to unblock")
@has_admin()
async def config_domains_remove(interaction: discord.Interaction, domain: str):
    domain = domain.strip().lower()
    cfg = get_config(interaction.guild_id)
    banned = set(cfg.get("banned_domains", []))
    if domain not in banned:
        await interaction.response.send_message(f"**{domain}** isn't in the list.", ephemeral=True)
        return
    banned.discard(domain)
    update_config(interaction.guild_id, "banned_domains", sorted(banned))
    await interaction.response.send_message(f"✅ Unblocked **{domain}**. {len(banned)} domain(s) remaining.", ephemeral=True)


@domains_group.command(name="list", description="Show the current blocked domains list")
@has_admin()
async def config_domains_list(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    banned = sorted(cfg.get("banned_domains", []))
    if not banned:
        await interaction.response.send_message("No domains blocked yet.", ephemeral=True)
        return
    embed = discord.Embed(title=f"🚫 Blocked Domains ({len(banned)})", description="\n".join(f"`{d}`" for d in banned)[:4000], color=0xff6600)
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(config_group)


# Kill-switch d'urgence : commandes réservées au vrai owner de l'app Discord
# (jamais un rôle serveur, jamais un id codé en dur — voir is_bot_owner()).
admin_group = app_commands.Group(name="admin", description="Bot owner only")

killswitch_admin_group = app_commands.Group(
    name="killswitch",
    description="Manage the emergency kill-switch — bot owner only",
    parent=admin_group,
)


@killswitch_admin_group.command(name="status", description="Check whether the bot is globally locked — bot owner only")
@is_bot_owner()
async def admin_killswitch_status(interaction: discord.Interaction):
    locked = get_global_lock()
    await interaction.response.send_message(
        "🔒 The bot is currently locked down across all servers." if locked
        else "🔓 The bot is currently active normally across all servers.",
        ephemeral=True,
    )


@killswitch_admin_group.command(name="resend", description="Send a fresh kill-switch email now instead of waiting for the daily one — bot owner only")
@is_bot_owner()
async def admin_killswitch_resend(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    success, error = await asyncio.to_thread(send_daily_killswitch_email)
    if success:
        await interaction.followup.send("📧 A fresh kill-switch email has been sent — check your inbox (and spam folder).", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Email failed to send:\n```{error}```", ephemeral=True)


bot.tree.add_command(admin_group)


# Revue des appels de ban : /appeal accept|deny — admin only
appeal_group = app_commands.Group(name="appeal", description="Review sanction appeals — admin only", default_permissions=discord.Permissions(administrator=True))


@appeal_group.command(name="accept", description="Accept a ban appeal and unban the user — admin only")
@app_commands.describe(case_id="The case number shown in the appeal")
@has_admin()
async def appeal_accept(interaction: discord.Interaction, case_id: int):
    appeal = get_ban_appeal_by_case(interaction.guild_id, case_id)
    if not appeal:
        await interaction.response.send_message(f"❌ No appeal found for case #{case_id}.", ephemeral=True)
        return
    if appeal["status"] != "submitted":
        await interaction.response.send_message(f"⚠️ This appeal is already **{appeal['status']}**.", ephemeral=True)
        return

    sanction_type = appeal.get("sanction_type", "ban")
    result = await reverse_sanction(interaction.guild, sanction_type, appeal["user_id"])

    ban_appeals_col.update_one(
        {"token": appeal["token"]},
        {"$set": {"status": "accepted", "resolved_at": datetime.datetime.now(datetime.timezone.utc)}},
    )
    record_audit(interaction.guild_id, interaction.user.id, str(interaction.user), "Accepted appeal", f"Case #{case_id} ({sanction_type}) — {result}")
    try:
        user = await bot.fetch_user(int(appeal["user_id"]))
        await user.send(f"✅ Your appeal for **{interaction.guild.name}** (case #{case_id}) was accepted — {result}.")
    except discord.HTTPException:
        pass
    await interaction.response.send_message(f"✅ Case #{case_id} accepted — {result}.", ephemeral=True)


@appeal_group.command(name="deny", description="Deny a ban appeal — admin only")
@app_commands.describe(case_id="The case number shown in the appeal", note="Optional internal note (not sent to the user)")
@has_admin()
async def appeal_deny(interaction: discord.Interaction, case_id: int, note: str = ""):
    appeal = get_ban_appeal_by_case(interaction.guild_id, case_id)
    if not appeal:
        await interaction.response.send_message(f"❌ No appeal found for case #{case_id}.", ephemeral=True)
        return
    if appeal["status"] != "submitted":
        await interaction.response.send_message(f"⚠️ This appeal is already **{appeal['status']}**.", ephemeral=True)
        return
    ban_appeals_col.update_one(
        {"token": appeal["token"]},
        {"$set": {"status": "denied", "resolved_at": datetime.datetime.now(datetime.timezone.utc)}},
    )
    record_audit(interaction.guild_id, interaction.user.id, str(interaction.user), "Denied appeal", f"Case #{case_id}" + (f" — {note}" if note else ""))
    try:
        user = await bot.fetch_user(int(appeal["user_id"]))
        await user.send(f"❌ Your appeal for **{interaction.guild.name}** (case #{case_id}) was reviewed and denied.")
    except discord.HTTPException:
        pass
    await interaction.response.send_message(f"Case #{case_id} denied.", ephemeral=True)


bot.tree.add_command(appeal_group)


# Sauvegarde de la structure du serveur : /backup create|list|restore — server owner only
template_transfer_group = app_commands.Group(
    name="template",
    description="Share bot config (automod, features, command perms) between servers via a code — admin only",
    default_permissions=discord.Permissions(administrator=True),
)
# Distinct de /config template (templates de permissions internes à CE serveur) --
# celui-ci exporte/importe de la config ENTRE serveurs via un code partageable.


def _build_template_snapshot(guild, cfg):
    """Partagé par /template create (Discord) et la marketplace web."""
    feature_toggles = {key: bool(cfg.get(meta["config_key"])) for key, meta in FEATURE_TOGGLES.items()}
    automod = {k: cfg.get(k) for k in (
        "banned_words", "banned_domains", "automod_spam_count", "automod_spam_window",
        "automod_caps_ratio", "automod_raid_count", "automod_raid_window",
    )}
    command_roles_by_name = {}
    for command, role_ids in cfg.get("command_roles", {}).items():
        names = [role.name for rid in role_ids if (role := guild.get_role(int(rid)))]
        if names:
            command_roles_by_name[command] = names
    return {
        "feature_toggles": feature_toggles,
        "automod": automod,
        "warn_escalation_enabled": cfg.get("warn_escalation_enabled", False),
        "command_roles_by_name": command_roles_by_name,
    }


@template_transfer_group.command(name="create", description="Snapshot this server's config into a shareable code — admin only")
@app_commands.describe(name="Short label shown if you list it on the dashboard marketplace", public="List this template on the web dashboard marketplace for any admin to browse")
@has_admin()
async def template_create(interaction: discord.Interaction, name: str = None, public: bool = False):
    guild = interaction.guild
    cfg = get_config(guild.id)
    snapshot = _build_template_snapshot(guild, cfg)

    code = secrets.token_hex(4)
    while templates_col.find_one({"code": code}):
        code = secrets.token_hex(4)

    templates_col.insert_one({
        "code": code,
        "name": name or f"{guild.name} template",
        "public": bool(public),
        "created_guild_id": str(guild.id),
        "created_guild_name": guild.name,
        "created_by": str(interaction.user.id),
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "snapshot": snapshot,
    })

    embed = discord.Embed(title="📦 Template Created", description=f"Share this code with the other server's admin:\n```\n{code}\n```", color=0x00cc00)
    embed.add_field(name="Includes", value=f"{len(snapshot['feature_toggles'])} feature toggles, automod settings, warn escalation, {len(snapshot['command_roles_by_name'])} command role permission(s)", inline=False)
    if public:
        embed.add_field(name="Marketplace", value="✅ Listed publicly on the dashboard marketplace (any admin can browse/import it).", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@template_transfer_group.command(name="load", description="Apply a config template from another server using its code — admin only")
@app_commands.describe(code="The code shared by the server that ran /template create")
@has_admin()
async def template_load(interaction: discord.Interaction, code: str):
    doc = templates_col.find_one({"code": code.strip()})
    if not doc:
        await interaction.response.send_message("❌ No template found with that code.", ephemeral=True)
        return
    snapshot = doc["snapshot"]
    guild_id = interaction.guild_id
    _apply_template_base_config(guild_id, snapshot)

    command_roles_by_name = snapshot.get("command_roles_by_name", {})
    distinct_names = sorted({name for names in command_roles_by_name.values() for name in names})

    if not distinct_names:
        await interaction.response.send_message(f"✅ Template from **{doc['created_guild_name']}** applied (no role permissions to map).", ephemeral=True)
        return

    batch, remaining = distinct_names[:4], distinct_names[4:]
    view = RoleMappingView(guild_id, batch, command_roles_by_name, remaining)
    await interaction.response.send_message(
        f"✅ Config from **{doc['created_guild_name']}** applied. Now map the old command-permission role(s) to roles on this server:",
        view=view,
        ephemeral=True,
    )


backup_group = app_commands.Group(name="backup", description="Server structure backups (roles, channels, permissions) — server owner only", default_permissions=discord.Permissions(administrator=True))


@backup_group.command(name="create", description="Snapshot this server's roles, channels, and permissions — server owner only")
@has_owner()
async def backup_create(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    backup_id = create_server_backup(interaction.guild)
    await interaction.followup.send(f"💾 Backup created (`{backup_id}`). Use `/backup list` to see all backups.", ephemeral=True)


@backup_group.command(name="list", description="List available backups for this server — server owner only")
@has_owner()
async def backup_list(interaction: discord.Interaction):
    backups = list(server_backups_col.find({"guild_id": str(interaction.guild_id)}).sort("created_at", -1))
    if not backups:
        await interaction.response.send_message("No backups yet — create one with `/backup create`.", ephemeral=True)
        return
    lines = []
    for b in backups:
        ts = b["created_at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        lines.append(f"`{b['_id']}` — {ts.strftime('%Y-%m-%d %H:%M UTC')} ({len(b['roles'])} roles, {len(b['channels'])} channels)")
    embed = discord.Embed(title="💾 Server Backups", description="\n".join(lines), color=0x3399ff)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@backup_group.command(name="preview", description="See what's inside a backup before restoring it — server owner only")
@app_commands.describe(backup_id="The backup ID from /backup list")
@has_owner()
async def backup_preview(interaction: discord.Interaction, backup_id: str):
    try:
        backup = server_backups_col.find_one({"_id": ObjectId(backup_id), "guild_id": str(interaction.guild_id)})
    except Exception:
        backup = None
    if not backup:
        await interaction.response.send_message("❌ Backup not found. Check the ID with `/backup list`.", ephemeral=True)
        return

    role_names = [r["name"] for r in backup["roles"]]
    categories = [c["name"] for c in backup["channels"] if c["type"] == "category"]
    text_channels = [c["name"] for c in backup["channels"] if c["type"] == "text"]
    voice_channels = [c["name"] for c in backup["channels"] if c["type"] == "voice"]

    ts = backup["created_at"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)

    embed = discord.Embed(title=f"💾 Backup Preview — {ts.strftime('%Y-%m-%d %H:%M UTC')}", color=0x3399ff)
    embed.add_field(name=f"Roles ({len(role_names)})", value=", ".join(role_names)[:1000] or "None", inline=False)
    embed.add_field(name=f"Categories ({len(categories)})", value=", ".join(categories)[:1000] or "None", inline=False)
    embed.add_field(name=f"Text channels ({len(text_channels)})", value=", ".join(text_channels)[:1000] or "None", inline=False)
    embed.add_field(name=f"Voice channels ({len(voice_channels)})", value=", ".join(voice_channels)[:1000] or "None", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@backup_group.command(name="restore", description="Restore missing roles/channels from a backup — owner only. Never deletes anything.")
@app_commands.describe(backup_id="The backup ID from /backup list", confirm="Set to true to actually run the restore")
@has_owner()
async def backup_restore(interaction: discord.Interaction, backup_id: str, confirm: bool = False):
    if not confirm:
        await interaction.response.send_message(
            "⚠️ This re-creates any missing roles/channels from that backup, matched by name. "
            "It never deletes or overwrites anything that already exists. Re-run with `confirm:true` to actually do it.",
            ephemeral=True,
        )
        return
    try:
        backup = server_backups_col.find_one({"_id": ObjectId(backup_id), "guild_id": str(interaction.guild_id)})
    except Exception:
        backup = None
    if not backup:
        await interaction.response.send_message("❌ Backup not found. Check the ID with `/backup list`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    created_roles, created_channels = await restore_server_backup(interaction.guild, backup)
    record_audit(
        interaction.guild_id, interaction.user.id, str(interaction.user),
        "Restored server backup", f"backup {backup_id}: +{created_roles} roles, +{created_channels} channels",
    )
    await interaction.followup.send(
        f"✅ Restore complete — created {created_roles} missing role(s) and {created_channels} missing channel(s).",
        ephemeral=True,
    )


bot.tree.add_command(backup_group)
bot.tree.add_command(template_transfer_group)


invites_group = app_commands.Group(name="invites", description="Track who invited whom on this server")


@invites_group.command(name="leaderboard", description="Show the top inviters on this server")
async def invites_leaderboard(interaction: discord.Interaction):
    pipeline = [
        {"$match": {"guild_id": str(interaction.guild_id)}},
        {"$group": {"_id": "$inviter_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    results = list(invite_uses_col.aggregate(pipeline))
    if not results:
        await interaction.response.send_message("No tracked invites yet on this server.", ephemeral=True)
        return
    lines = []
    for i, r in enumerate(results, start=1):
        lines.append(f"**{i}.** <@{r['_id']}> — {r['count']} invite(s)")
    embed = discord.Embed(title="🏆 Top Inviters", description="\n".join(lines), color=0x3399ff)
    await interaction.response.send_message(embed=embed)


@invites_group.command(name="who", description="Show who invited a specific member")
@app_commands.describe(member="The member to check")
async def invites_who(interaction: discord.Interaction, member: discord.Member):
    record = invite_uses_col.find_one({"guild_id": str(interaction.guild_id), "invited_user_id": str(member.id)})
    if not record:
        await interaction.response.send_message(f"No invite record found for **{member}** (may have joined before tracking was enabled, or via a discoverable/vanity link).", ephemeral=True)
        return
    embed = discord.Embed(description=f"**{member}** was invited by <@{record['inviter_id']}> using code `{record['invite_code']}`.", color=0x3399ff)
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(invites_group)


export_group = app_commands.Group(name="export", description="Export server data as CSV — admin only", default_permissions=discord.Permissions(administrator=True))


@export_group.command(name="sanctions", description="Export this server's moderation history as a CSV file — admin only")
@has_admin()
async def export_sanctions(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    records = list(sanctions_col.find({"guild_id": str(interaction.guild_id)}).sort("timestamp", -1))
    if not records:
        await interaction.followup.send("No sanctions to export.", ephemeral=True)
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["case_id", "type", "user_id", "moderator_id", "reason", "timestamp"])
    for r in records:
        writer.writerow([
            r.get("case_id", ""), r.get("type", ""), r.get("user_id", ""),
            r.get("moderator_id", ""), r.get("reason", ""), r.get("timestamp", ""),
        ])
    file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename=f"sanctions_{interaction.guild_id}.csv")
    await interaction.followup.send("📄 Here's the export:", file=file, ephemeral=True)


@export_group.command(name="audit", description="Export this server's dashboard audit log as a CSV file — admin only")
@has_admin()
async def export_audit(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    records = _get_audit_records(interaction.guild_id)
    if not records:
        await interaction.followup.send("No audit entries to export.", ephemeral=True)
        return
    file = discord.File(io.BytesIO(_build_audit_csv_bytes(records)), filename=f"audit_{interaction.guild_id}.csv")
    await interaction.followup.send("📄 Here's the export:", file=file, ephemeral=True)


bot.tree.add_command(export_group)


# Interrupteur unique pour toutes les fonctionnalités à bascule simple
# (on/off) — évite d'avoir une commande différente par feature.
FEATURE_CHOICES = [app_commands.Choice(name=v["label"], value=k) for k, v in FEATURE_TOGGLES.items()]

feature_group = app_commands.Group(name="feature", description="Enable or disable optional bot features — admin only", default_permissions=discord.Permissions(administrator=True))


@feature_group.command(name="enable", description="Enable an optional feature — admin only")
@app_commands.describe(name="Which feature to enable")
@app_commands.choices(name=FEATURE_CHOICES)
@has_admin()
async def feature_enable(interaction: discord.Interaction, name: app_commands.Choice[str]):
    feature = FEATURE_TOGGLES[name.value]
    update_config(interaction.guild_id, feature["config_key"], True)
    extra = ""
    if name.value == "antinuke":
        extra = " Set the sensitivity with `/config antinuke threshold:5` (default 5). Needs the **View Audit Log** permission."
    await interaction.response.send_message(f"✅ **{feature['label']}** enabled.{extra}", ephemeral=True)


@feature_group.command(name="disable", description="Disable an optional feature — admin only")
@app_commands.describe(name="Which feature to disable")
@app_commands.choices(name=FEATURE_CHOICES)
@has_admin()
async def feature_disable(interaction: discord.Interaction, name: app_commands.Choice[str]):
    feature = FEATURE_TOGGLES[name.value]
    update_config(interaction.guild_id, feature["config_key"], False)
    await interaction.response.send_message(f"🚫 **{feature['label']}** disabled.", ephemeral=True)


@feature_group.command(name="list", description="Show which optional features are currently on or off")
@has_admin()
async def feature_list(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    lines = []
    for key, meta in FEATURE_TOGGLES.items():
        state = "🟢 On" if cfg.get(meta["config_key"]) else "⚪ Off"
        lines.append(f"{state} — **{meta['label']}** (`{key}`)")
    embed = discord.Embed(title="⚙️ Feature Toggles", description="\n".join(lines), color=0x3399ff)
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(feature_group)

@bot.tree.command(name="botlock", description="Lock the bot on this server (server owner only)")
@has_owner()
async def botlock(interaction: discord.Interaction):
    bot.locked_guilds.add(interaction.guild_id)
    embed = discord.Embed(description="🔒 Bot locked on this server. Only the server owner can use commands until `/botunlock`.", color=0xff0000)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="botunlock", description="Unlock the bot on this server (server owner only)")
@has_owner()
async def botunlock(interaction: discord.Interaction):
    bot.locked_guilds.discard(interaction.guild_id)
    embed = discord.Embed(description="🔓 Bot unlocked on this server.", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# ===========  USERINFO / SERVERINFO / TEMPBAN / REACTION ROLES
# ============================================================

async def tempban_check_loop():
    """Tâche de fond qui lève les tempbans arrivés à expiration, toutes les 60 secondes."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.datetime.utcnow()
            for doc in get_active_tempbans():
                if doc["unban_at"] <= now:
                    guild = bot.get_guild(int(doc["guild_id"]))
                    if guild:
                        try:
                            user = await bot.fetch_user(int(doc["user_id"]))
                            await guild.unban(user, reason="Tempban expired")
                            remove_tempban(doc["guild_id"], doc["user_id"])
                            embed = discord.Embed(title="✅ Tempban Expired", color=0x00cc00)
                            embed.add_field(name="User", value=f"**{user}**", inline=True)
                            await _send_log_embed(guild, embed)
                        except Exception as e:
                            print(f"Tempban unban error: {e}", flush=True)
                            remove_tempban(doc["guild_id"], doc["user_id"])
        except Exception as e:
            print(f"Tempban loop error: {e}", flush=True)
        await asyncio.sleep(60)


async def member_count_loop():
    """Renomme les salons vocaux configurés avec le nombre de membres, toutes
    les 10 minutes (Discord limite le renommage d'un salon à environ 2 fois
    toutes les 10 minutes, donc on reste large)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                cfg = get_config(guild.id)
                channel_id = cfg.get("member_count_channel_id")
                if not channel_id:
                    continue
                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    continue
                new_name = f"👥 Members: {guild.member_count}"
                if channel.name != new_name:
                    try:
                        await channel.edit(name=new_name)
                    except discord.HTTPException as e:
                        print(f"[MEMBERCOUNT] Failed to rename channel in guild {guild.id}: {e}", flush=True)
        except Exception as e:
            print(f"[MEMBERCOUNT] loop error: {e}", flush=True)
        await asyncio.sleep(600)


async def generate_weekly_digest(guild):
    week_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
    sanctions_this_week = list(sanctions_col.find({
        "guild_id": str(guild.id),
        "timestamp": {"$gte": week_ago},
    }))
    counts = {}
    for s in sanctions_this_week:
        counts[s["type"]] = counts.get(s["type"], 0) + 1

    lines = [f"📊 **Weekly digest — {guild.name}**", ""]
    lines.append(f"👥 Members: {guild.member_count}")
    if counts:
        breakdown = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        lines.append(f"🛡️ Moderation actions this week: {breakdown}")
    else:
        lines.append("🛡️ No moderation actions this week.")
    return "\n".join(lines)


async def weekly_digest_loop():
    """Vérifie toutes les 6h, par serveur, si 7 jours se sont écoulés
    depuis le dernier digest envoyé — plus fiable qu'un simple sleep(7
    jours) global qui ne s'adapterait pas à une activation en cours de
    semaine."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            try:
                cfg = get_config(guild.id)
                if not cfg.get("weekly_digest_enabled"):
                    continue
                now = datetime.datetime.now(datetime.timezone.utc)
                last_sent = cfg.get("last_digest_sent_at")
                if last_sent:
                    if last_sent.tzinfo is None:
                        last_sent = last_sent.replace(tzinfo=datetime.timezone.utc)
                    if (now - last_sent).total_seconds() < 7 * 24 * 60 * 60:
                        continue
                digest_text = await generate_weekly_digest(guild)
                owner = guild.owner or await bot.fetch_user(guild.owner_id)
                await owner.send(digest_text)
                update_config(guild.id, "last_digest_sent_at", now)
            except Exception as e:
                print(f"[DIGEST] Failed for guild {guild.id}: {e}", flush=True)
        await asyncio.sleep(6 * 60 * 60)


def schedule_announcement(guild_id, channel_id, content, send_at, author_id):
    scheduled_col.insert_one({
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "content": content,
        "send_at": send_at,
        "author_id": str(author_id),
        "sent": False,
    })


async def scheduled_announcements_loop():
    """Check toutes les 60s si une annonce programmée est due. La comparaison
    de date se fait DANS la requête Mongo (pas en Python après lecture),
    donc pas de piège naive/aware ici."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            due = list(scheduled_col.find({"sent": False, "send_at": {"$lte": now}}))
            for doc in due:
                channel = bot.get_channel(int(doc["channel_id"]))
                if channel:
                    try:
                        await channel.send(doc["content"])
                    except discord.HTTPException as e:
                        print(f"[SCHEDULE] Failed to send scheduled announcement: {e}", flush=True)
                scheduled_col.update_one({"_id": doc["_id"]}, {"$set": {"sent": True}})
        except Exception as e:
            print(f"[SCHEDULE] loop error: {e}", flush=True)
        await asyncio.sleep(60)


async def mongo_backup_loop():
    """Dump quotidien de TOUTES les collections Mongo en JSON, zippé, envoyé
    en DM au bot owner (is_bot_owner -- même notion que /admin killswitch,
    pas le owner d'un serveur Discord précis). Mongo Atlas M0 (free tier)
    n'inclut aucun backup automatique -- celui-ci comble ce trou.
    ⚠️ Le dump contient tout tel quel, y compris les clés API stockées en
    config -- volontairement envoyé seulement en DM au bot owner, jamais
    ailleurs."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if db is not None:
                app_info = await bot.application_info()
                owner = app_info.owner
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for name in db.list_collection_names():
                        docs = list(db[name].find({}))
                        zf.writestr(f"{name}.json", _json.dumps(docs, default=str, indent=2))
                buf.seek(0)
                size_mb = buf.getbuffer().nbytes / (1024 * 1024)
                if size_mb > 9:
                    log_json("warning", "mongo_backup_too_large", size_mb=round(size_mb, 1))
                else:
                    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                    await owner.send(
                        content=f"🗄️ Daily Mongo backup — {stamp}",
                        file=discord.File(buf, filename=f"nexus_backup_{stamp}.zip"),
                    )
                    log_json("info", "mongo_backup_sent", size_mb=round(size_mb, 2))
        except discord.Forbidden:
            log_json(
                "error", "mongo_backup_dm_blocked",
                hint="Owner has DMs closed from the bot. Enable 'Allow direct messages from server members' on a shared server to receive backups.",
            )
        except Exception as e:
            log_json("error", "mongo_backup_loop_error", error=str(e))
        await asyncio.sleep(86400)  # 24h


async def reminders_loop():
    """Check toutes les 30s les /remindme dus. Comparaison de date faite dans
    la requête Mongo, pas en Python après lecture (même piège naive/aware que
    scheduled_announcements_loop à éviter)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            due = list(reminders_col.find({"sent": False, "due_at": {"$lte": now}}))
            for doc in due:
                try:
                    user = bot.get_user(int(doc["user_id"])) or await bot.fetch_user(int(doc["user_id"]))
                    embed = discord.Embed(title="⏰ Reminder", description=doc["text"], color=0x3399ff)
                    await user.send(embed=embed)
                except discord.HTTPException as e:
                    log_json("error", "remindme_dm_failed", user_id=doc["user_id"], error=str(e))
                reminders_col.update_one({"_id": doc["_id"]}, {"$set": {"sent": True}})
        except Exception as e:
            log_json("error", "remindme_loop_error", error=str(e))
        await asyncio.sleep(30)


async def auto_slowmode_revert_loop():
    """Check toutes les 30s les salons dont la fenêtre d'auto-slowmode est
    passée, et remet slowmode_delay à 0. État en mémoire (_auto_slowmode_active)
    -- un redémarrage du bot pendant une fenêtre active laisse juste le
    slowmode manuel en place, rien de cassé, juste pas auto-reverté cette fois."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            expired = [cid for cid, revert_at in _auto_slowmode_active.items() if now >= revert_at]
            for channel_id in expired:
                channel = bot.get_channel(channel_id)
                if channel:
                    try:
                        await channel.edit(slowmode_delay=0, reason="Auto-slowmode window expired")
                    except discord.HTTPException as e:
                        log_json("error", "auto_slowmode_revert_failed", channel_id=channel_id, error=str(e))
                del _auto_slowmode_active[channel_id]
        except Exception as e:
            log_json("error", "auto_slowmode_revert_loop_error", error=str(e))
        await asyncio.sleep(30)


async def unverified_kick_loop():
    """Toutes les 15 min : kick des membres restés Unverified plus de `verification_kick_hours`
    heures (0 = désactivé, valeur par défaut). Garde-fous et règles dans verification.kick_pass."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild in list(bot.guilds):
                await kick_pass(guild)
        except Exception as e:
            log_json("error", "unverified_kick_loop_error", error=str(e))
        await asyncio.sleep(900)


async def quarantine_review_loop():
    """Check toutes les 5 min les entrées de quarantaine pas encore reviewées
    depuis plus de quarantine_hold_minutes -- envoie un rappel unique dans le
    salon de review (flag 'reminded' pour ne jamais relancer deux fois)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            pending = list(quarantine_col.find({"reviewed": False, "reminded": False}))
            for doc in pending:
                guild = bot.get_guild(int(doc["guild_id"]))
                if not guild:
                    continue
                cfg = get_config(doc["guild_id"])
                hold_minutes = cfg.get("quarantine_hold_minutes", 30)
                joined_at = doc["joined_at"]
                if joined_at.tzinfo is None:
                    joined_at = joined_at.replace(tzinfo=datetime.timezone.utc)
                if (now - joined_at).total_seconds() < hold_minutes * 60:
                    continue
                channel_id = cfg.get("quarantine_channel_id")
                channel = guild.get_channel(int(channel_id)) if channel_id else None
                if channel:
                    try:
                        await channel.send(f"⏳ Reminder: <@{doc['user_id']}> is still waiting for quarantine review (joined {hold_minutes}+ min ago).")
                    except discord.HTTPException as e:
                        log_json("error", "quarantine_reminder_failed", error=str(e))
                quarantine_col.update_one({"_id": doc["_id"]}, {"$set": {"reminded": True}})
        except Exception as e:
            log_json("error", "quarantine_review_loop_error", error=str(e))
        await asyncio.sleep(300)


@bot.tree.command(name="userinfo", description="Show information about a member")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    roles = [r.mention for r in member.roles if not r.is_default()]
    roles_str = " ".join(roles) if roles else "None"
    joined_at = member.joined_at.strftime("%Y-%m-%d %H:%M UTC") if member.joined_at else "Unknown"
    created_at = member.created_at.strftime("%Y-%m-%d %H:%M UTC")
    status_icons = {
        discord.Status.online: "🟢 Online",
        discord.Status.idle: "🟡 Idle",
        discord.Status.dnd: "🔴 Do Not Disturb",
        discord.Status.offline: "⚫ Offline",
    }
    status = status_icons.get(member.status, "⚫ Offline")
    warn_count = get_warns(interaction.guild_id, member.id)
    embed = discord.Embed(title=f"👤 {member}", color=member.color if member.color.value else 0x3399ff)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
    embed.add_field(name="Joined Server", value=joined_at, inline=True)
    embed.add_field(name="Account Created", value=created_at, inline=True)
    embed.add_field(name="Warnings", value=str(warn_count), inline=True)
    embed.add_field(name=f"Roles ({len(roles)})", value=roles_str[:1024] if roles_str else "None", inline=False)
    embed.add_field(name="Top Role", value=member.top_role.mention if not member.top_role.is_default() else "None", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="remindme", description="Set a personal reminder — Nexus will DM you when it's time")
@app_commands.describe(duration="e.g. 10m, 2h, 1d, 1w", text="What to remind you about")
async def remindme(interaction: discord.Interaction, duration: str, text: str):
    cfg = get_config(interaction.guild_id) if interaction.guild_id else {}
    if interaction.guild_id and not cfg.get("remindme_enabled", True):
        await interaction.response.send_message("❌ `/remindme` is disabled on this server.", ephemeral=True)
        return
    seconds = parse_duration(duration)
    if seconds <= 0:
        await interaction.response.send_message("❌ Invalid duration. Use something like `10m`, `2h`, `1d`, `1w`.", ephemeral=True)
        return
    due_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    reminders_col.insert_one({
        "user_id": str(interaction.user.id),
        "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
        "text": text,
        "due_at": due_at,
        "sent": False,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    })
    await interaction.response.send_message(f"⏰ Got it — I'll DM you in **{duration}**: \"{text}\"", ephemeral=True)


def _generate_activity_chart(guild_id):
    """CPU-bound (matplotlib) -- toujours appelée via asyncio.to_thread.
    Barres sobres, cohérentes avec le reste de l'identité visuelle du bot (bleu #3399ff)."""
    stats = get_sanction_stats(guild_id, days=7)
    labels = [label for label, _ in stats]
    values = [count for _, count in stats]
    fig, ax = plt.subplots(figsize=(6, 2.2), dpi=120)
    ax.bar(labels, values, color="#3399ff")
    ax.set_title("Moderation activity · last 7 days", fontsize=10, color="#333333")
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf


@bot.tree.command(name="serverinfo", description="Show information about the server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    created_at = guild.created_at.strftime("%Y-%m-%d %H:%M UTC")
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    total_channels = text_channels + voice_channels
    bots = sum(1 for m in guild.members if m.bot)
    humans = guild.member_count - bots
    embed = discord.Embed(title=f"🌐 {guild.name}", color=0x3399ff)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="ID", value=str(guild.id), inline=True)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Created", value=created_at, inline=True)
    embed.add_field(name="Members", value=f"👥 {humans} humans · 🤖 {bots} bots", inline=True)
    embed.add_field(name="Channels", value=f"💬 {text_channels} text · 🔊 {voice_channels} voice", inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles) - 1), inline=True)
    embed.add_field(name="Boosts", value=f"⚡ {guild.premium_subscription_count} (Level {guild.premium_tier})", inline=True)
    embed.add_field(name="Verification Level", value=str(guild.verification_level).title(), inline=True)

    cfg = get_config(guild.id)
    if cfg.get("serverinfo_visual_enabled"):
        await interaction.response.defer()
        try:
            buf = await asyncio.to_thread(_generate_activity_chart, guild.id)
            file = discord.File(buf, filename="activity.png")
            embed.set_image(url="attachment://activity.png")
            await interaction.followup.send(embed=embed, file=file)
        except Exception as e:
            log_json("error", "serverinfo_chart_failed", error=str(e))
            await interaction.followup.send(embed=embed)
        return

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="List all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Nexus — Commands", color=0x3399ff)
    embed.add_field(
        name="🛡️ Moderation",
        value="`/ban` `/unban` `/kick` `/mute` `/unmute` `/tempban` `/softban`\n"
              "`/warn` `/unwarn` `/warnings` `/warnlist` `/note` `/notes` `/history` `/banlist` `/mutelist` `/clear`",
        inline=False,
    )
    embed.add_field(
        name="🔒 Channels & Roles",
        value="`/lock` `/unlock` `/vlock` `/vunlock` `/lockedchannels`\n"
              "`/roleadd` `/roleremove` `/reactionrole` `/slowmode`\n"
              "`/nickname` `/groupnickname`",
        inline=False,
    )
    embed.add_field(
        name="📋 Applications & Utilities",
        value="`/apply` `/userinfo` `/serverinfo` `/broadcast` `/poll` `/botinfo` `/ping` `/help`",
        inline=False,
    )
    embed.add_field(
        name="🎵 Music",
        value="`/play` `/search` `/pause` `/resume` `/skip` `/stop` `/queue` `/volume`",
        inline=False,
    )
    embed.add_field(
        name="⚙️ Server Config",
        value="`/config logs` `/config autorole` `/config apikey` `/config view`\n"
              "`/config allow` `/config disallow` `/purgeuser` — owner/admin only\n"
              "`/botlock` `/botunlock` — server owner only",
        inline=False,
    )
    embed.set_footer(text="Full documentation on GitHub")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(description=f"🏓 Pong! `{latency_ms}ms`", color=0x00cc00)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="botinfo", description="Show information about the bot")
async def botinfo(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - bot.start_time)
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"
    embed = discord.Embed(title="🤖 Nexus", color=0x3399ff)
    if bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Library", value=f"discord.py {discord.__version__}", inline=True)
    embed.set_footer(text="github.com/digravinaloris/dc-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="poll", description="Create a quick poll")
@app_commands.describe(
    question="The poll question",
    option1="First option (leave empty for a simple 👍/👎 poll)",
    option2="Second option",
    option3="Third option",
    option4="Fourth option",
)
async def poll(interaction: discord.Interaction, question: str, option1: str = None, option2: str = None, option3: str = None, option4: str = None):
    options = [o for o in [option1, option2, option3, option4] if o]
    embed = discord.Embed(title=f"📊 {question}", color=0x3399ff)
    embed.set_footer(text=f"Poll started by {interaction.user}")
    if options:
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
        embed.description = "\n".join(f"{number_emojis[i]} {opt}" for i, opt in enumerate(options))
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        for i in range(len(options)):
            await message.add_reaction(number_emojis[i])
    else:
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        await message.add_reaction("👍")
        await message.add_reaction("👎")


@bot.tree.command(name="tempban", description="Temporarily ban a member")
@app_commands.describe(duration="Duration: 30m, 2h, 1d, 1w")
@app_commands.default_permissions(ban_members=True)
async def tempban(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
    if not await check_access(interaction, "tempban", "ban_members"): return
    if member.top_role >= interaction.guild.me.top_role:
        embed = discord.Embed(description="❌ I can't ban this member, their role is too high.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    seconds = parse_duration(duration)
    if seconds <= 0:
        embed = discord.Embed(description="❌ Invalid duration. Use `30m`, `2h`, `1d`, `1w`.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    unban_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    full_reason = f"[Tempban {duration}] {reason}"

    dm_sent, appeal_token = await send_sanction_dm(member, interaction.guild, "tempban", full_reason)

    try:
        await member.ban(reason=full_reason)
        save_tempban(interaction.guild_id, member.id, unban_at)
        case_id = log_sanction(interaction.guild_id, member.id, "ban", full_reason, interaction.user.id)
        if appeal_token:
            create_sanction_appeal(interaction.guild_id, case_id, "tempban", member.id, str(member), full_reason, appeal_token)
        embed = discord.Embed(title="⏳ Member Tempbanned", color=0xff6600)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        embed.add_field(name="User", value=f"**{member}**", inline=True)
        embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Unbanned at", value=unban_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Banned by", value=f"**{interaction.user.top_role.name}** · {interaction.user.name}", inline=True)
        if not dm_sent:
            embed.set_footer(text="Couldn't DM the user (DMs closed or the bot is blocked).")
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        embed = discord.Embed(description=f"❌ Couldn't ban this member: {e}", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="reactionrole", description="Create a reaction role message")
@app_commands.describe(
    title="Title of the embed",
    channel="Channel where to post the embed",
    pairs="Emoji/role pairs separated by commas, e.g: 🎮 Gamer, 🎵 Music, 🎨 Art"
)
@app_commands.default_permissions(manage_roles=True)
async def reactionrole(interaction: discord.Interaction, title: str, channel: discord.TextChannel, pairs: str):
    if not await check_access(interaction, "reactionrole", "manage_roles"): return
    await interaction.response.defer(ephemeral=True)

    # Parse les paires emoji/rôle depuis la string
    # Format attendu: "emoji RoleName, emoji RoleName, ..."
    parsed = []
    for pair in pairs.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(None, 1)  # split sur le premier espace
        if len(parts) != 2:
            await interaction.followup.send(f"❌ Invalid pair: `{pair}` — expected `emoji RoleName`", ephemeral=True)
            return
        emoji_str, role_name = parts[0].strip(), parts[1].strip()
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if not role:
            await interaction.followup.send(f"❌ Role `{role_name}` not found.", ephemeral=True)
            return
        if role >= interaction.guild.me.top_role:
            await interaction.followup.send(f"❌ Role `{role_name}` is too high for me to manage.", ephemeral=True)
            return
        parsed.append((emoji_str, role))

    if not parsed:
        await interaction.followup.send("❌ No valid emoji/role pairs found.", ephemeral=True)
        return

    description = "\n".join(f"{emoji} — {role.mention}" for emoji, role in parsed)
    embed = discord.Embed(title=title, description=description, color=0x3399ff)
    embed.set_footer(text="React to get the corresponding role")

    try:
        msg = await channel.send(embed=embed)
        for emoji_str, role in parsed:
            try:
                await msg.add_reaction(emoji_str)
                save_reaction_role(interaction.guild_id, msg.id, channel.id, emoji_str, role.id)
            except discord.HTTPException:
                await interaction.followup.send(f"⚠️ Couldn't add reaction for `{emoji_str}` — make sure it's a valid emoji.", ephemeral=True)
        await interaction.followup.send(f"✅ Reaction role message created in {channel.mention}!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create message: {e}", ephemeral=True)

# ============================================================
# ====================  APPLY SYSTEM  =======================
# ============================================================

class ApplicationModal(discord.ui.Modal):
    def __init__(self, role: discord.Role):
        super().__init__(title=f"Application for {role.name}")
        self.role = role
        self.question1 = discord.ui.TextInput(
            label="Why do you want this role?",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.question2 = discord.ui.TextInput(
            label="How old are you?",
            style=discord.TextStyle.short,
            max_length=50,
            required=True,
        )
        self.question3 = discord.ui.TextInput(
            label="What can you bring to the team?",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.add_item(self.question1)
        self.add_item(self.question2)
        self.add_item(self.question3)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "✅ Your application has been submitted! You'll be notified of the decision.", ephemeral=True
        )
        owner = interaction.guild.owner or await interaction.guild.fetch_owner()
        embed = discord.Embed(title=f"📋 New Application — {self.role.name}", color=0x3399ff)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Applicant", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(name="Server", value=interaction.guild.name, inline=True)
        embed.add_field(name="Role", value=self.role.mention, inline=True)
        embed.add_field(name="Why this role?", value=self.question1.value, inline=False)
        embed.add_field(name="Age", value=self.question2.value, inline=True)
        embed.add_field(name="What they bring", value=self.question3.value, inline=False)
        view = ApplicationDecisionView(
            applicant_id=interaction.user.id,
            guild_id=interaction.guild_id,
            role_id=self.role.id,
        )
        try:
            await owner.send(embed=embed, view=view)
        except Exception as e:
            print(f"Apply DM error: {e}", flush=True)


class AppealModal(discord.ui.Modal, title="Submit your appeal"):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    reason = discord.ui.TextInput(
        label="Why should this be reconsidered?",
        style=discord.TextStyle.paragraph,
        placeholder="Explain your side...",
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        appeal = get_ban_appeal(self.token)
        if not appeal or appeal["status"] != "pending":
            await interaction.response.send_message("This appeal is no longer available.", ephemeral=True)
            return
        ban_appeals_col.update_one(
            {"token": self.token},
            {"$set": {
                "status": "submitted",
                "appeal_text": self.reason.value,
                "submitted_at": datetime.datetime.now(datetime.timezone.utc),
            }},
        )
        guild = bot.get_guild(int(appeal["guild_id"]))
        if guild:
            await post_appeal_for_review(guild, appeal, self.reason.value)
        await interaction.response.send_message(
            "✅ Your appeal has been submitted. You'll get a DM once it's reviewed.", ephemeral=True
        )


_appeal_button_cooldowns = {}  # {user_id: last_click_timestamp} — anti-spam sur le bouton d'appel
APPEAL_BUTTON_COOLDOWN_SECONDS = 5

class AppealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nexus_appeal:(?P<token>[a-zA-Z0-9_\-]+)"):
    """Bouton persistant attaché au DM de sanction. Le token est encodé
    directement dans le custom_id (via DynamicItem) pour survivre aux
    redémarrages du bot sans avoir à tout garder en mémoire."""
    def __init__(self, token: str):
        super().__init__(
            discord.ui.Button(
                label="Appeal this",
                style=discord.ButtonStyle.secondary,
                emoji="📨",
                custom_id=f"nexus_appeal:{token}",
            )
        )
        self.token = token

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["token"])

    async def callback(self, interaction: discord.Interaction):
        now = time.time()
        last = _appeal_button_cooldowns.get(interaction.user.id, 0)
        if now - last < APPEAL_BUTTON_COOLDOWN_SECONDS:
            await interaction.response.send_message("⏳ Slow down a little before trying again.", ephemeral=True)
            return
        _appeal_button_cooldowns[interaction.user.id] = now

        appeal = get_ban_appeal(self.token)
        if not appeal:
            await interaction.response.send_message("This appeal link is no longer valid.", ephemeral=True)
            return
        if appeal["status"] != "pending":
            status_msgs = {
                "submitted": "You've already submitted an appeal for this — it's awaiting review.",
                "accepted": "This appeal was already accepted.",
                "denied": "This appeal was already reviewed and denied.",
            }
            await interaction.response.send_message(
                status_msgs.get(appeal["status"], "This appeal has already been processed."), ephemeral=True
            )
            return
        await interaction.response.send_modal(AppealModal(self.token))


class PaginatedEmbedView(discord.ui.View):
    """Vue générique de pagination par boutons — réutilisée par toutes les
    commandes qui listent potentiellement beaucoup d'entrées (mutelist,
    warnlist, banlist, history...), pour ne jamais dépasser les limites
    d'un embed Discord."""
    def __init__(self, title, lines, color=0x3399ff, per_page=10, author_id=None):
        super().__init__(timeout=120)
        self.title = title
        self.lines = lines
        self.color = color
        self.per_page = per_page
        self.page = 0
        self.author_id = author_id
        self.max_page = max(0, (len(lines) - 1) // per_page)
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can navigate pages.", ephemeral=True
            )
            return False
        return True

    def _update_buttons(self):
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.max_page

    def build_embed(self):
        start = self.page * self.per_page
        chunk = self.lines[start:start + self.per_page]
        embed = discord.Embed(title=self.title, description="\n".join(chunk) or "—", color=self.color)
        if self.max_page > 0:
            embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


def get_role_menu(message_id):
    return role_menus_col.find_one({"message_id": str(message_id)})


class RoleMenuSelect(discord.ui.Select):
    """Menu déroulant persistant : les options réellement affichées sont
    déjà figées dans le message Discord une fois posté, donc un placeholder
    suffit ici pour l'enregistrement persistant au démarrage — la vraie
    liste de rôles est relue depuis Mongo (par message_id) à chaque clic,
    ce qui marche même après un redémarrage du bot."""
    def __init__(self, options=None):
        super().__init__(
            placeholder="Select your roles...",
            min_values=0,
            max_values=len(options) if options else 1,
            options=options or [discord.SelectOption(label="placeholder", value="0")],
            custom_id="nexus_role_menu_select",
        )

    async def callback(self, interaction: discord.Interaction):
        menu = get_role_menu(interaction.message.id)
        if not menu:
            await interaction.response.send_message("This role menu is no longer configured.", ephemeral=True)
            return
        all_ids = {item["role_id"] for item in menu["items"]}
        selected_ids = {int(v) for v in self.values}
        member = interaction.user
        to_add = [interaction.guild.get_role(rid) for rid in selected_ids if interaction.guild.get_role(rid)]
        to_remove = [interaction.guild.get_role(rid) for rid in (all_ids - selected_ids) if interaction.guild.get_role(rid)]
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Role menu")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Role menu")
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't update your roles: {e}", ephemeral=True)
            return
        await interaction.response.send_message("✅ Your roles have been updated.", ephemeral=True)


class RoleMenuView(discord.ui.View):
    def __init__(self, options=None):
        super().__init__(timeout=None)
        self.add_item(RoleMenuSelect(options))


class SetupWizardView(discord.ui.View):
    """Assistant de configuration guidé : évite d'avoir à connaître chaque
    commande /config une par une. Chaque sélecteur applique son réglage
    immédiatement, pour que l'assistant reste utile même si on le quitte
    en cours de route."""
    def __init__(self, guild_id, author_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who started the setup can use it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="1. Pick your logs channel",
        min_values=1,
        max_values=1,
    )
    async def pick_logs(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        update_config(self.guild_id, "logs_channel", channel.name)
        await interaction.response.send_message(f"✅ Logs channel set to **#{channel.name}**.", ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="2. Pick an auto-role for new members (optional)",
        min_values=0,
        max_values=1,
    )
    async def pick_autorole(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not select.values:
            update_config(self.guild_id, "autorole", None)
            await interaction.response.send_message("✅ Auto-role cleared.", ephemeral=True)
            return
        role = select.values[0]
        risky = ("administrator", "manage_guild", "ban_members", "kick_members", "manage_roles")
        update_config(self.guild_id, "autorole", role.id)
        warning = ""
        if any(getattr(role.permissions, p) for p in risky):
            warning = "\n⚠️ Careful: this role has sensitive permissions and will be given to **every** new member."
        await interaction.response.send_message(f"✅ Auto-role set to **{role.name}**.{warning}", ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="3. Pick a channel for sanction appeals (optional)",
        min_values=0,
        max_values=1,
    )
    async def pick_appeals(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if not select.values:
            update_config(self.guild_id, "appeal_channel_id", None)
            await interaction.response.send_message("✅ Appeals disabled.", ephemeral=True)
            return
        channel = select.values[0]
        update_config(self.guild_id, "appeal_channel_id", channel.id)
        await interaction.response.send_message(
            f"✅ Sanction appeals will be reviewed in **#{channel.name}**. Sanction DMs now include an appeal button.",
            ephemeral=True,
        )

    @discord.ui.button(label="Enable anti-nuke", style=discord.ButtonStyle.success, emoji="🛡️", row=3)
    async def enable_antinuke(self, interaction: discord.Interaction, button: discord.ui.Button):
        update_config(self.guild_id, "antinuke_enabled", True)
        button.disabled = True
        button.label = "Anti-nuke enabled"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "✅ Anti-nuke enabled. Make sure the bot has the **View Audit Log** permission for it to work.",
            ephemeral=True,
        )

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="✅ **Setup complete.** Run `/config view` any time to check your settings, or `/setup` again to change them.",
            embed=None,
            view=self,
        )
        self.stop()


class SatisfactionSurveyView(discord.ui.View):
    """DM envoyé au propriétaire du ticket une fois celui-ci fermé."""
    def __init__(self):
        super().__init__(timeout=None)

    async def _record(self, interaction: discord.Interaction, satisfied: bool):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        record_audit("global", str(interaction.user.id), str(interaction.user),
                      "Ticket satisfaction feedback", "👍 satisfied" if satisfied else "👎 not satisfied")
        await interaction.followup.send("Thanks for the feedback! 🙏", ephemeral=True)

    @discord.ui.button(label="Good experience", style=discord.ButtonStyle.success, emoji="👍", custom_id="nexus_satisfaction_up")
    async def satisfied(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, True)

    @discord.ui.button(label="Could be better", style=discord.ButtonStyle.secondary, emoji="👎", custom_id="nexus_satisfaction_down")
    async def unsatisfied(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, False)


def _render_ticket_transcript(channel_name, messages):
    """Render a self-contained, Discord-inspired HTML transcript."""
    import html as _html

    safe_channel = _html.escape(str(channel_name), quote=True)
    rows = []
    for message in messages:
        author = _html.escape(str(getattr(message.author, "display_name", message.author)), quote=True)
        avatar_url = _html.escape(str(getattr(getattr(message.author, "display_avatar", None), "url", "")), quote=True)
        timestamp = message.created_at.strftime("%b %d, %Y at %H:%M UTC")
        safe_timestamp = _html.escape(timestamp, quote=True)
        content = _html.escape(message.content or "")
        content_html = content.replace("\n", "<br>") if content else ""
        if not content_html and not getattr(message, "attachments", None) and not getattr(message, "embeds", None):
            content_html = '<span class="muted">This message had no text content.</span>'

        avatar = (
            f'<img class="avatar" src="{avatar_url}" alt="" loading="lazy">'
            if avatar_url else '<div class="avatar avatar-fallback">?</div>'
        )
        bot_badge = '<span class="bot-badge">BOT</span>' if getattr(message.author, "bot", False) else ""
        edited = ' <span class="edited">(edited)</span>' if getattr(message, "edited_at", None) else ""

        attachment_html = []
        for attachment in getattr(message, "attachments", []):
            filename = _html.escape(str(attachment.filename), quote=True)
            url = _html.escape(str(attachment.url), quote=True)
            content_type = str(getattr(attachment, "content_type", "") or "")
            if content_type.startswith("image/"):
                attachment_html.append(
                    f'<a class="attachment image-attachment" href="{url}" target="_blank" rel="noopener noreferrer">'
                    f'<img src="{url}" alt="{filename}" loading="lazy"><span>{filename}</span></a>'
                )
            else:
                attachment_html.append(
                    f'<a class="attachment file-attachment" href="{url}" target="_blank" rel="noopener noreferrer">'
                    f'<span class="file-icon">📎</span><span>{filename}</span></a>'
                )

        embed_html = []
        for embed in getattr(message, "embeds", []):
            title = _html.escape(str(embed.title), quote=True) if embed.title else ""
            description = _html.escape(str(embed.description), quote=True).replace("\n", "<br>") if embed.description else ""
            if title or description:
                embed_html.append(
                    f'<div class="discord-embed">{f"<strong>{title}</strong>" if title else ""}'
                    f'{f"<div>{description}</div>" if description else ""}</div>'
                )

        rows.append(
            f'<article class="message">{avatar}<div class="message-body">'
            f'<div class="message-meta"><span class="author">{author}</span>{bot_badge}'
            f'<time datetime="{message.created_at.isoformat()}">{safe_timestamp}</time>{edited}</div>'
            f'<div class="message-content">{content_html}</div>'
            f'{"".join(attachment_html)}{"".join(embed_html)}</div></article>'
        )

    count = len(messages)
    noun = "message" if count == 1 else "messages"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light"><title>Transcript — {safe_channel}</title>
<style>
:root {{ color-scheme: dark; --bg:#313338; --panel:#2b2d31; --text:#dbdee1; --muted:#949ba4; --line:#3f4147; --link:#00a8fc; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:28px 16px 48px; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:920px; margin:0 auto; }}
header {{ padding:20px 22px; margin-bottom:18px; background:var(--panel); border-radius:8px; }}
h1 {{ margin:0 0 4px; font-size:21px; }}
.summary {{ color:var(--muted); font-size:13px; }}
.message {{ display:flex; gap:16px; padding:12px 18px; border-radius:6px; }}
.message:hover {{ background:rgba(0,0,0,.08); }}
.avatar {{ width:40px; height:40px; flex:0 0 40px; border-radius:50%; object-fit:cover; background:#404249; }}
.avatar-fallback {{ display:grid; place-items:center; color:var(--muted); font-weight:700; }}
.message-body {{ min-width:0; flex:1; }}
.message-meta {{ display:flex; align-items:baseline; flex-wrap:wrap; gap:8px; }}
.author {{ color:#f2f3f5; font-weight:600; }}
time,.edited,.muted {{ color:var(--muted); font-size:12px; }}
.bot-badge {{ padding:1px 4px; border-radius:3px; background:#5865f2; color:white; font-size:9px; font-weight:700; }}
.message-content {{ margin-top:3px; overflow-wrap:anywhere; white-space:normal; }}
.attachment {{ display:inline-flex; align-items:center; gap:9px; max-width:100%; margin:8px 8px 0 0; padding:8px; border:1px solid var(--line); border-radius:6px; color:var(--link); text-decoration:none; }}
.file-attachment {{ min-width:190px; }}
.file-icon {{ font-size:20px; }}
.image-attachment {{ flex-direction:column; align-items:flex-start; }}
.image-attachment img {{ max-width:min(100%,420px); max-height:320px; border-radius:4px; object-fit:contain; }}
.discord-embed {{ margin-top:8px; padding:10px 12px; border-left:4px solid #5865f2; border-radius:4px; background:var(--panel); overflow-wrap:anywhere; }}
.discord-embed strong {{ display:block; margin-bottom:4px; }}
@media(max-width:520px) {{ body {{ padding:12px 6px 28px; }} .message {{ gap:10px; padding:10px 8px; }} .avatar {{ width:32px; height:32px; flex-basis:32px; }} header {{ padding:16px; }} }}
</style></head><body><main>
<header><h1>#{safe_channel}</h1><div class="summary">{count} {noun} · Ticket transcript</div></header>
<section aria-label="Ticket messages">{"".join(rows)}</section>
</main></body></html>"""


async def _generate_ticket_transcript(channel):
    """Fetch the ticket history and render it in chronological order."""
    messages = [message async for message in channel.history(limit=500, oldest_first=True)]
    return _render_ticket_transcript(channel.name, messages)


def _ticket_topic_parts(topic):
    """Read legacy opener-only topics and the panel-aware topic format."""
    value = str(topic or "")
    if value.startswith("nexus-ticket|"):
        parts = {}
        for item in value.split("|")[1:]:
            if ":" in item:
                key, val = item.split(":", 1)
                parts[key] = val
        return parts.get("opener"), parts.get("panel"), parts.get("support"), parts.get("archive")
    return (value if value.isdigit() else None), None, None, None


def _ticket_topic_value(topic, key):
    """Return one metadata value from a Nexus ticket topic."""
    value = str(topic or "")
    if not value.startswith("nexus-ticket|"):
        return None
    for item in value.split("|")[1:]:
        if ":" in item:
            field, field_value = item.split(":", 1)
            if field == key:
                return field_value
    return None


def _find_ticket_panel(cfg, panel_id):
    if not panel_id:
        return None
    return next((p for p in (cfg.get("ticket_panels") or []) if str(p.get("id")) == str(panel_id)), None)


TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}


def _ticket_topic_update(topic, key, value):
    """Update one metadata field without breaking legacy ticket topics."""
    current = str(topic or "")
    if current.startswith("nexus-ticket|"):
        items = current.split("|")
        found = False
        for index in range(1, len(items)):
            if items[index].split(":", 1)[0] == key:
                items[index] = f"{key}:{value}"
                found = True
                break
        if not found:
            items.append(f"{key}:{value}")
        return "|".join(items)
    opener = current if current.isdigit() else ""
    fields = [f"nexus-ticket|opener:{opener}"]
    if key != "opener":
        fields.append(f"{key}:{value}")
    return "|".join(fields)


class TicketCloseView(discord.ui.View):
    """Bouton persistant dans chaque ticket ouvert pour le fermer."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="nexus_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        cfg = get_config(interaction.guild_id)
        opener_id, panel_id, topic_support_role_id, topic_archive_category_id = _ticket_topic_parts(channel.topic)
        panel = _find_ticket_panel(cfg, panel_id)
        support_role_id = (panel or {}).get("support_role_id") or topic_support_role_id or cfg.get("ticket_support_role_id")
        support_role = interaction.guild.get_role(int(support_role_id or 0))
        is_opener = opener_id is not None and str(interaction.user.id) == opener_id
        is_support = support_role is not None and support_role in interaction.user.roles
        is_admin = interaction.user.guild_permissions.manage_channels
        if not (is_opener or is_support or is_admin):
            await interaction.response.send_message(
                "You don't have permission to close this ticket.", ephemeral=True
            )
            return

        # 1 tap, pas de re-clic possible : le bouton est désactivé immédiatement,
        # avant même de faire quoi que ce soit d'autre.
        button.disabled = True
        button.label = "Closed"
        await interaction.response.edit_message(view=self)

        # Note de satisfaction : DM à la personne qui a ouvert le ticket,
        # indépendamment de l'archivage ou de la suppression qui suit.
        if opener_id and opener_id.isdigit():
            try:
                opener_user = await bot.fetch_user(int(opener_id))
                await opener_user.send(
                    content="How was your experience with this ticket?",
                    view=SatisfactionSurveyView(),
                )
            except discord.HTTPException:
                pass

        # Transcript : généré avant tout archivage/suppression, envoyé dans le
        # salon de logs configuré + en DM à la personne qui a ouvert le ticket.
        try:
            transcript_html = await _generate_ticket_transcript(channel)
            filename = f"transcript-{channel.name}.html"
            logs_channel = discord.utils.get(interaction.guild.text_channels, name=cfg.get("logs_channel", "logs"))
            if logs_channel:
                await logs_channel.send(
                    content=f"📜 Transcript for closed ticket **#{channel.name}** (by {interaction.user.mention}):",
                    file=discord.File(io.BytesIO(transcript_html.encode("utf-8")), filename=filename),
                )
            if opener_id and opener_id.isdigit():
                try:
                    opener_user = await bot.fetch_user(int(opener_id))
                    await opener_user.send(
                        content="Here's a transcript of your ticket:",
                        file=discord.File(io.BytesIO(transcript_html.encode("utf-8")), filename=filename),
                    )
                except discord.HTTPException:
                    pass
        except Exception as e:
            log_json("error", "ticket_transcript_failed", channel_id=channel.id, guild_id=interaction.guild_id, error=str(e))

        archive_category = None
        if panel is not None:
            archive_category_id = panel.get("archive_category_id")
        elif panel_id:
            archive_category_id = None if topic_archive_category_id in (None, "none") else topic_archive_category_id
        else:
            archive_category_id = cfg.get("ticket_archive_category_id")
        if archive_category_id:
            candidate = interaction.guild.get_channel(int(archive_category_id))
            if isinstance(candidate, discord.CategoryChannel):
                archive_category = candidate

        if archive_category is not None:
            # Archive au lieu de supprimer : le salon hérite entièrement des
            # permissions de la catégorie d'archive (sync_permissions), donc
            # c'est cette catégorie qui décide qui peut encore voir les
            # tickets fermés — plus aucune permission propre au ticket.
            try:
                new_name = channel.name if channel.name.startswith("closed-") else f"closed-{channel.name}"[:100]
                await channel.edit(
                    category=archive_category,
                    name=new_name,
                    sync_permissions=True,
                    topic=_ticket_topic_update(channel.topic, "status", "closed"),
                    reason=f"Ticket archived by {interaction.user}",
                )
                await interaction.followup.send(
                    f"🔒 Ticket archived by {interaction.user.mention} — moved to **{archive_category.name}**, now following that category's permissions."
                )
            except discord.HTTPException as e:
                await interaction.followup.send(f"Couldn't archive the ticket: {e}", ephemeral=True)
            return

        # Pas de catégorie d'archive configurée -> suppression immédiate, en un seul tap.
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException as e:
            print(f"[TICKET] Failed to delete channel: {e}", flush=True)


class TicketOpenModal(discord.ui.Modal, title="Open a Ticket"):
    """Dynamic intake form: each panel can define up to five questions."""
    def __init__(self, panel_config=None):
        super().__init__()
        self.panel_config = panel_config
        questions = list((panel_config or {}).get("form_questions") or [])
        if not questions:
            questions = ["What do you need help with?"]
        for index, question in enumerate(questions[:5]):
            self.add_item(discord.ui.TextInput(
                label=str(question)[:45],
                custom_id=f"ticket_question_{index}",
                style=discord.TextStyle.paragraph,
                placeholder="Write your answer here...",
                max_length=500,
                required=True,
            ))

    async def on_submit(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild_id)
        panel = self.panel_config
        if not panel and not cfg.get("tickets_enabled"):
            await interaction.response.send_message("Tickets are currently disabled on this server.", ephemeral=True)
            return

        category_id = (panel or {}).get("category_id") or cfg.get("ticket_category_id")
        category = interaction.guild.get_channel(int(category_id or 0))
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket system isn't configured properly. Ask an admin to run `/ticket setup` again.",
                ephemeral=True,
            )
            return

        # Re-vérifié ici (pas juste au clic du bouton) : le formulaire prend
        # du temps à remplir, un doublon a pu être ouvert entre-temps.
        existing = next((ch for ch in interaction.guild.text_channels
                         if not ch.name.startswith("closed-")
                         and _ticket_topic_parts(ch.topic)[0] == str(interaction.user.id)), None)
        if existing:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}", ephemeral=True
            )
            return

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        support_role_id = (panel or {}).get("support_role_id") or cfg.get("ticket_support_role_id")
        support_role = interaction.guild.get_role(int(support_role_id or 0))
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        safe_name = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower().replace(" ", "-"))[:20] or "user"
        panel_id = str((panel or {}).get("id") or "")
        panel_slug = re.sub(r"[^a-z0-9-]", "", str((panel or {}).get("name", "")).lower().replace(" ", "-"))[:16]
        channel_name = f"{panel_slug}-{safe_name}" if panel_slug else f"ticket-{safe_name}"
        archive_id = (panel or {}).get("archive_category_id")
        ticket_topic = (
            f"nexus-ticket|opener:{interaction.user.id}|panel:{panel_id}"
            f"|support:{support_role_id or ''}|archive:{archive_id if archive_id else 'none'}"
            "|priority:normal"
        )
        try:
            channel = await category.create_text_channel(
                name=channel_name[:100],
                overwrites=overwrites,
                topic=ticket_topic,
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't create the ticket channel: {e}", ephemeral=True)
            return

        opening_message = (panel or {}).get("opening_message") or "The support team will be with you shortly."
        panel_name = (panel or {}).get("name") or "New Ticket"
        embed = discord.Embed(
            title=f"🎫 {panel_name}",
            description=f"Hey {interaction.user.mention}! {discord.utils.escape_mentions(opening_message)}",
            color=0x3399ff,
        )
        embed.add_field(name="Priority", value="🔵 Normal", inline=True)
        for index, answer in enumerate(
            child for child in self.children if isinstance(child, discord.ui.TextInput)
        ):
            question = str(answer.label)
            response = (answer.value or "").strip() or "No answer provided."
            embed.add_field(name=question, value=response[:1000], inline=False)
        await channel.send(
            content=support_role.mention if support_role else None,
            embed=embed,
            view=TicketCloseView(),
        )
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)


class RoleMappingView(discord.ui.View):
    """Posée par /template load quand le template importe des permissions de
    commande liées à des rôles -- un RoleSelect par nom de rôle distinct de
    l'ancien serveur. Max 4 par passage : Discord limite à 5 lignes par message
    et la ligne 4 est réservée au bouton Confirm (au-delà, l'admin relance
    /template load pour le reste). Pas persistante (timeout normal) : c'est un
    flux ponctuel, pas un bouton qui doit survivre à un redémarrage."""
    def __init__(self, guild_id, role_names, command_roles_by_name, remaining_role_names):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.role_names = role_names  # ordre stable, correspond à l'ordre des selects ajoutés
        self.command_roles_by_name = command_roles_by_name
        self.remaining_role_names = remaining_role_names  # noms pas traités dans ce batch (si >4)
        for i, name in enumerate(role_names):
            select = discord.ui.RoleSelect(placeholder=f"Map old role: {name}", min_values=1, max_values=1, row=i)
            self.add_item(select)

    @discord.ui.button(label="Confirm mapping", style=discord.ButtonStyle.success, row=4)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        selects = [item for item in self.children if isinstance(item, discord.ui.RoleSelect)]
        if any(not s.values for s in selects):
            await interaction.response.send_message("❌ Pick a role for every mapping before confirming.", ephemeral=True)
            return
        name_to_new_id = {name: selects[i].values[0].id for i, name in enumerate(self.role_names)}
        new_command_roles = get_config(self.guild_id).get("command_roles", {})
        for command, old_names in self.command_roles_by_name.items():
            mapped_ids = [name_to_new_id[n] for n in old_names if n in name_to_new_id]
            if mapped_ids:
                new_command_roles[command] = mapped_ids
        update_config(self.guild_id, "command_roles", new_command_roles)
        note = ""
        if self.remaining_role_names:
            note = f"\n⚠️ {len(self.remaining_role_names)} more role(s) in this template weren't mapped yet (5 per batch) — run `/template load` again with the same code to continue."
        await interaction.response.edit_message(content=f"✅ Role mapping applied for this batch.{note}", view=None)


class VerificationView(discord.ui.View):
    """Bouton persistant posté par /config verification post -- retire le rôle
    Unverified et donne le rôle Verified. Les salons restent à configurer
    manuellement par l'admin (permissions Discord), le bot ne gère que les rôles."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id="nexus_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_config(interaction.guild_id)
        unverified_id = cfg.get("unverified_role_id")
        verified_id = cfg.get("verified_role_id")
        if not unverified_id or not verified_id:
            await interaction.response.send_message("Verification isn't configured properly. Ask an admin to run `/config verification setup`.", ephemeral=True)
            return
        unverified_role = interaction.guild.get_role(int(unverified_id))
        verified_role = interaction.guild.get_role(int(verified_id))
        # Anti-spam : déjà vérifié -> réponse immédiate, aucun appel à l'API Discord.
        # Protège à la fois contre le spam-click d'un même membre et le risque de
        # rate-limit si plusieurs clics arrivent d'un coup sur un bouton partagé.
        if verified_role and verified_role in interaction.user.roles:
            await interaction.response.send_message("✅ You're already verified.", ephemeral=True)
            return
        # Turnstile doit rester configuré : jamais de fallback silencieux vers un clic simple.
        turnstile = cfg.get("verification_method", "button") == "turnstile"
        if turnstile and not turnstile_configured():
            await interaction.response.send_message("❌ Web verification is temporarily unavailable. Please try again later or contact a moderator.", ephemeral=True)
            return
        # L'âge minimum s'applique à TOUS les modes, y compris le bouton one-click :
        # sinon un membre trop jeune pourrait contourner la règle en changeant de méthode.
        days_left = account_days_left(interaction.user, get_verification_settings(interaction.guild_id)["verification_min_account_days"])
        if days_left > 0:
            await interaction.response.send_message(f"❌ Your Discord account is too new to verify on this server yet. Please try again in {days_left} day(s), or contact a moderator.", ephemeral=True)
            return
        if turnstile:
            token = create_challenge(interaction.guild_id, interaction.user.id)
            link_view = discord.ui.View()
            link_view.add_item(discord.ui.Button(label="Open verification page", style=discord.ButtonStyle.link, url=verification_url(token)))
            await interaction.response.send_message("🛡️ One quick check to confirm you're human. This link is yours only, valid 10 minutes, and works once.", view=link_view, ephemeral=True)
            return
        try:
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role, reason="Verified")
            if verified_role:
                await interaction.user.add_roles(verified_role, reason="Verified")
            await interaction.response.send_message("✅ You're verified! Welcome in.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Couldn't update your roles: {e}", ephemeral=True)


class TicketPanelView(discord.ui.View):
    """Bouton persistant posté par /ticket setup pour ouvrir un ticket."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="nexus_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_config(interaction.guild_id)
        footer = interaction.message.embeds[0].footer.text if interaction.message.embeds and interaction.message.embeds[0].footer else ""
        panel_id = footer.split("nexus-ticket-panel:", 1)[1].strip() if "nexus-ticket-panel:" in footer else None
        panel = _find_ticket_panel(cfg, panel_id)
        if panel_id and panel is None:
            await interaction.response.send_message("This ticket panel is no longer configured. Please contact a server administrator.", ephemeral=True)
            return
        if not panel and not cfg.get("tickets_enabled"):
            await interaction.response.send_message("Tickets are currently disabled on this server.", ephemeral=True)
            return
        category_id = (panel or {}).get("category_id") or cfg.get("ticket_category_id")
        category = interaction.guild.get_channel(int(category_id or 0))
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket system isn't configured properly. Ask an admin to run `/ticket setup` again.",
                ephemeral=True,
            )
            return

        # Empêche un utilisateur d'ouvrir plusieurs tickets en même temps :
        # on stocke son id dans le topic du salon pour le retrouver.
        existing = next((ch for ch in interaction.guild.text_channels
                         if not ch.name.startswith("closed-")
                         and _ticket_topic_parts(ch.topic)[0] == str(interaction.user.id)), None)
        if existing:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}", ephemeral=True
            )
            return

        await interaction.response.send_modal(TicketOpenModal(panel_config=panel))


class ApplicationDecisionView(discord.ui.View):
    def __init__(self, applicant_id, guild_id, role_id):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.guild_id = guild_id
        self.role_id = role_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.green, custom_id="apply_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.client.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
            return
        member = guild.get_member(self.applicant_id)
        role = guild.get_role(self.role_id)
        if member and role:
            try:
                await member.add_roles(role, reason="Application accepted")
                try:
                    dm_embed = discord.Embed(
                        title="✅ Application Accepted!",
                        description=f"Your application for **{role.name}** on **{guild.name}** has been **accepted**. The role has been given to you!",
                        color=0x00cc00,
                    )
                    await member.send(embed=dm_embed)
                except Exception:
                    pass
                await interaction.response.send_message(f"✅ Accepted — {role.name} given to {member}.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Member or role not found.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="❌ Refuse", style=discord.ButtonStyle.red, custom_id="apply_refuse")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.client.get_guild(self.guild_id)
        if guild:
            member = guild.get_member(self.applicant_id)
            role = guild.get_role(self.role_id)
            if member:
                try:
                    dm_embed = discord.Embed(
                        title="❌ Application Refused",
                        description=f"Your application for **{role.name if role else 'the role'}** on **{guild.name}** has been **refused**.",
                        color=0xff0000,
                    )
                    await member.send(embed=dm_embed)
                except Exception:
                    pass
        await interaction.response.send_message("❌ Application refused.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)


@bot.tree.command(name="apply", description="Apply for a role")
@app_commands.describe(role="The role you want to apply for")
async def apply(interaction: discord.Interaction, role: discord.Role):
    if not await check_access(interaction, "apply", None): return
    await interaction.response.send_modal(ApplicationModal(role=role))

# ============================================================

# ============================================================
# ===================  AUTO-MODERATION  =====================
# ============================================================

# Suivi en mémoire des messages récents par utilisateur, pour la détection de spam.
# Pas besoin de persister ça en DB : ça repart à zéro si le bot redémarre, ce qui est très bien.
_recent_messages = {}  # {(guild_id, user_id): [timestamps]}

# Suivi en mémoire des arrivées récentes par serveur, pour la détection de raid.
_recent_joins = {}  # {guild_id: [timestamps]}
_recent_join_names = {}  # {guild_id: [(timestamp, display_name)]} -- pour la détection de pattern de raid
_media_flood_tracker = {}  # {(guild_id, user_id): [timestamps]}
_channel_activity_tracker = {}  # {channel_id: [timestamps]} -- pour déclencher l'auto-slowmode
_auto_slowmode_active = {}  # {channel_id: revert_at (datetime aware UTC)}

SPAM_MESSAGE_COUNT = 10
SPAM_WINDOW_SECONDS = 5
INVITE_LINK_RE = re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)", re.IGNORECASE)
RAID_JOIN_COUNT = 5
RAID_WINDOW_SECONDS = 10

# Suivi en mémoire des actions destructrices récentes par (serveur, auteur),
# pour l'anti-nuke. Repart à zéro si le bot redémarre — acceptable ici, un
# nuke se joue en secondes, pas sur plusieurs redémarrages.
_antinuke_tracker = {}  # {(guild_id, actor_id): [timestamps]}
ANTINUKE_WINDOW_SECONDS = 60
ANTINUKE_ACTIONS = {
    discord.AuditLogAction.channel_delete,
    discord.AuditLogAction.role_delete,
    discord.AuditLogAction.ban,
    discord.AuditLogAction.kick,
}


@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    """Anti-nuke : repose sur l'Audit Log natif de Discord, donc détecte une
    action destructrice même faite en dehors du bot (ex: compte admin
    compromis agissant directement via le client Discord)."""
    if entry.action not in ANTINUKE_ACTIONS:
        return
    guild = entry.guild
    cfg = get_config(guild.id)
    if not cfg.get("antinuke_enabled"):
        return

    actor = entry.user
    if actor is None or actor.id == bot.user.id or actor.id == guild.owner_id:
        return  # jamais quarantainer le bot lui-même ou le vrai propriétaire

    key = (guild.id, actor.id)
    now = time.time()
    timestamps = [t for t in _antinuke_tracker.get(key, []) if now - t < ANTINUKE_WINDOW_SECONDS]
    timestamps.append(now)
    _antinuke_tracker[key] = timestamps

    threshold = cfg.get("antinuke_threshold", 5)
    if len(timestamps) < threshold:
        return

    # Seuil dépassé : quarantaine immédiate, une seule fois (on vide le
    # compteur pour ne pas re-déclencher en boucle sur les mêmes actions).
    _antinuke_tracker[key] = []

    member = guild.get_member(actor.id)
    quarantined = False
    if member is not None:
        try:
            removable_roles = [r for r in member.roles if r != guild.default_role and r < guild.me.top_role]
            if removable_roles:
                await member.remove_roles(*removable_roles, reason="Anti-nuke: abnormal spike of destructive actions")
                quarantined = True
        except discord.HTTPException as e:
            print(f"[ANTINUKE] Failed to strip roles: {e}", flush=True)

    bot.locked_guilds.add(guild.id)
    record_audit(
        guild.id, actor.id, str(actor), "Anti-nuke triggered",
        f"{len(timestamps)} destructive actions in {ANTINUKE_WINDOW_SECONDS}s — "
        f"{'roles stripped' if quarantined else 'could not strip roles'}, bot locked",
    )

    try:
        owner = guild.owner or await bot.fetch_user(guild.owner_id)
        await owner.send(
            f"🚨 **Anti-nuke triggered on {guild.name}**\n"
            f"**{actor}** performed {len(timestamps)} destructive actions (bans/kicks/channel or role "
            f"deletions) in under {ANTINUKE_WINDOW_SECONDS} seconds.\n"
            + ("Their roles have been stripped and " if quarantined else "I couldn't strip their roles (role hierarchy?), but ")
            + "the bot is now locked on this server. Review what happened, then use `/botunlock` when it's safe."
        )
    except discord.HTTPException:
        pass

    embed = discord.Embed(title="🚨 Anti-Nuke Triggered", color=0xff0000)
    embed.add_field(name="User", value=f"**{actor}**", inline=True)
    embed.add_field(name="Actions detected", value=str(len(timestamps)), inline=True)
    embed.add_field(
        name="Result",
        value="Roles stripped + bot locked" if quarantined else "Bot locked (couldn't strip roles)",
        inline=False,
    )
    await _send_log_embed(guild, embed)


def is_automod_exempt(member, cfg):
    """Les modérateurs (rôles autorisés ou permission gérer les messages) ne sont jamais auto-modérés."""
    if member.guild_permissions.manage_messages:
        return True
    allowed = set(cfg.get("allowed_roles", []))
    member_roles = {role.id for role in member.roles}
    if member_roles & allowed:
        return True
    exempt_roles = {int(r) for r in cfg.get("automod_exempt_role_ids", []) if str(r).isdigit()}
    return bool(member_roles & exempt_roles)


def check_spam(guild_id, user_id, cfg=None):
    cfg = cfg or get_config(guild_id)
    spam_count = cfg.get("automod_spam_count", SPAM_MESSAGE_COUNT)
    spam_window = cfg.get("automod_spam_window", SPAM_WINDOW_SECONDS)
    key = (guild_id, user_id)
    now = time.time()
    timestamps = _recent_messages.get(key, [])
    timestamps = [t for t in timestamps if now - t < spam_window]
    timestamps.append(now)
    _recent_messages[key] = timestamps
    return len(timestamps) > spam_count


def check_raid(guild_id, cfg=None):
    """Retourne True si assez de membres ont rejoint en peu de temps (seuils
    configurables par serveur, /config automod raidcount/raidwindow)."""
    cfg = cfg or get_config(guild_id)
    raid_count = cfg.get("automod_raid_count", RAID_JOIN_COUNT)
    raid_window = cfg.get("automod_raid_window", RAID_WINDOW_SECONDS)
    now = time.time()
    timestamps = _recent_joins.get(guild_id, [])
    timestamps = [t for t in timestamps if now - t < raid_window]
    timestamps.append(now)
    _recent_joins[guild_id] = timestamps
    return len(timestamps) >= raid_count


def record_join_name_and_check_pattern(guild_id, display_name, window_seconds=30):
    """Enregistre le pseudo d'un membre qui vient de join, et retourne True si
    les pseudos récents (fenêtre glissante) partagent un pattern suspect
    (voir utils.detect_raid_username_pattern). Indépendant du seuil brut de
    check_raid -- peut se déclencher même sous le seuil de comptage."""
    now = time.time()
    entries = _recent_join_names.get(guild_id, [])
    entries = [(t, n) for t, n in entries if now - t < window_seconds]
    entries.append((now, display_name))
    _recent_join_names[guild_id] = entries
    return detect_raid_username_pattern([n for _, n in entries])


def check_media_flood(guild_id, user_id, cfg):
    """True si ce membre poste des images/pièces jointes trop vite (seuils
    /config dans media_flood_count / media_flood_window)."""
    count_threshold = cfg.get("media_flood_count", 5)
    window = cfg.get("media_flood_window", 10)
    now = time.time()
    key = (guild_id, user_id)
    timestamps = _media_flood_tracker.get(key, [])
    timestamps = [t for t in timestamps if now - t < window]
    timestamps.append(now)
    _media_flood_tracker[key] = timestamps
    return len(timestamps) > count_threshold


def check_channel_burst(channel_id, cfg):
    """True si ce salon reçoit trop de messages trop vite (tous auteurs
    confondus) -- déclenche l'auto-slowmode temporaire."""
    threshold = cfg.get("auto_slowmode_threshold", 10)
    window = cfg.get("auto_slowmode_window", 10)
    now = time.time()
    timestamps = _channel_activity_tracker.get(channel_id, [])
    timestamps = [t for t in timestamps if now - t < window]
    timestamps.append(now)
    _channel_activity_tracker[channel_id] = timestamps
    return len(timestamps) > threshold


def check_invite_link(content):
    return bool(INVITE_LINK_RE.search(content))


async def apply_automod_action(message, violation_type, reason):
    """Supprime le message, ajoute un avertissement, logue la sanction et notifie dans le channel + logs."""
    try:
        await message.delete()
    except Exception as e:
        print(f"AutoMod: failed to delete message: {e}", flush=True)

    count = get_warns(message.guild.id, message.author.id) + 1
    set_warns(message.guild.id, message.author.id, count)
    log_sanction(message.guild.id, message.author.id, violation_type, reason, "automod")

    warning_embed = discord.Embed(
        description=f"⚠️ {message.author.mention}, {reason}. This has been logged as a warning ({count} total).",
        color=0xffcc00,
    )
    try:
        warning_msg = await message.channel.send(embed=warning_embed)
        await asyncio.sleep(5)
        await warning_msg.delete()
    except Exception as e:
        print(f"AutoMod: failed to send/delete warning message: {e}", flush=True)

    cfg = get_config(message.guild.id)
    icon = SANCTION_ICONS.get(violation_type, "🤖")
    log_embed = discord.Embed(title=f"{icon} AutoMod Action", color=0xffcc00)
    log_embed.add_field(name="User", value=f"**{message.author}**", inline=True)
    log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    log_embed.add_field(name="Reason", value=reason, inline=False)
    log_embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
    log_embed.set_thumbnail(url=message.author.display_avatar.url)
    await _send_log_embed(message.guild, log_embed)


@bot.event
async def on_message(message):
    if message.guild is None or message.author.bot:
        return

    cfg = get_config(message.guild.id)

    if not is_automod_exempt(message.author, cfg):
        # Salon piège : AUCUNE analyse de contenu -- si ce salon est configuré et
        # qu'un non-staff y poste quoi que ce soit, c'est un softban instantané.
        # Ne marche que si les membres normaux n'ont aucune raison légitime d'y
        # poster (salon cosmétique/caché pour la plupart, vu seulement par les
        # bots qui scrapent tous les salons -- voir /config trapchannel).
        if cfg.get("trap_channel_enabled") and cfg.get("trap_channel_id") and message.channel.id == int(cfg["trap_channel_id"]):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                await message.author.ban(reason="Posted in trap channel (auto-softban)", delete_message_seconds=60)
                await message.guild.unban(message.author, reason="Trap channel — automatic unban (softban)")
                log_sanction(message.guild.id, str(message.author.id), "softban", "Posted in trap channel", "automod")
                await message.channel.send(f"🪤 {message.author} softbanned for posting in the trap channel.")
                log_json("info", "trap_channel_catch", guild_id=message.guild.id, user_id=message.author.id)
            except discord.Forbidden:
                log_json("warning", "trap_channel_softban_failed_permissions", guild_id=message.guild.id, user_id=message.author.id)
            return

        if check_invite_link(message.content):
            await apply_automod_action(message, "automod_link", "posting an invite link")
            return
        if cfg.get("anti_link_global_enabled") and check_any_link(message.content):
            await apply_automod_action(message, "automod_link_global", "posting a link")
            return
        if check_spam(message.guild.id, message.author.id, cfg):
            await apply_automod_action(message, "automod_spam", "sending messages too quickly")
            return
        if check_caps(message.content, cfg):
            await apply_automod_action(message, "automod_caps", "excessive use of capital letters")
            return
        if check_banned_words(message.content, cfg.get("banned_words", [])):
            await apply_automod_action(message, "automod_badword", "using a banned word")
            return
        if check_banned_domains(message.content, cfg.get("banned_domains", [])):
            await apply_automod_action(message, "automod_domain", "sharing a blocked link/domain")
            return
        if cfg.get("anti_mention_spam_enabled"):
            has_mass_mention = message.mention_everyone
            mention_count = len(message.mentions) + len(message.role_mentions)
            if is_mention_spam(mention_count, has_mass_mention, cfg.get("mention_spam_threshold", 5)):
                await apply_automod_action(message, "automod_mention_spam", "mass-mentioning members/@everyone")
                return
        if cfg.get("anti_media_flood_enabled") and message.attachments:
            if check_media_flood(message.guild.id, message.author.id, cfg):
                await apply_automod_action(message, "automod_media_flood", "posting images/attachments too quickly")
                return
        if cfg.get("anti_doxx_enabled"):
            phone_confidence = detect_phone_number(message.content)
            address_hint = detect_address_hint(message.content)
            if phone_confidence == "high":
                await apply_automod_action(message, "automod_doxx", "posting what looks like a phone number")
                return
            if phone_confidence == "low" or address_hint:
                # Confiance basse -- jamais de suppression auto, juste une alerte pour review humaine.
                alert = discord.Embed(
                    title="🔎 Possible Personal Info Shared",
                    description=f"{message.author.mention} in {message.channel.mention} — low-confidence match, left in place for a human to review.",
                    color=0xffaa00,
                )
                alert.add_field(name="Message", value=message.content[:1000] or "*(empty)*", inline=False)
                await _send_log_embed(message.guild, alert)

    # Auto-slowmode : suivi de l'activité du salon tous auteurs confondus
    # (indépendant de l'exemption automod -- c'est une question de charge, pas de faute).
    if cfg.get("auto_slowmode_enabled") and message.channel.id not in _auto_slowmode_active:
        if check_channel_burst(message.channel.id, cfg):
            seconds = cfg.get("auto_slowmode_seconds", 10)
            duration_minutes = cfg.get("auto_slowmode_duration_minutes", 5)
            try:
                await message.channel.edit(slowmode_delay=seconds, reason="Auto-slowmode: channel activity burst detected")
                revert_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=duration_minutes)
                _auto_slowmode_active[message.channel.id] = revert_at
                alert = discord.Embed(
                    title="🐌 Auto-Slowmode Enabled",
                    description=f"{message.channel.mention} is getting hyperactive — slowmode set to {seconds}s for {duration_minutes} min.",
                    color=0x3399ff,
                )
                await _send_log_embed(message.guild, alert)
            except discord.HTTPException as e:
                log_json("error", "auto_slowmode_apply_failed", channel_id=message.channel.id, guild_id=message.guild.id, error=str(e))

    # Message épinglé (sticky) : republié en bas du salon après le passage
    # d'un cooldown, pour rester visible sans spammer à chaque message.
    sticky = get_sticky_message(message.channel.id)
    if sticky:
        now = datetime.datetime.now(datetime.timezone.utc)
        last_reposted_at = sticky.get("last_reposted_at")
        if last_reposted_at and last_reposted_at.tzinfo is None:
            last_reposted_at = last_reposted_at.replace(tzinfo=datetime.timezone.utc)
        cooldown_ok = (not last_reposted_at) or (now - last_reposted_at).total_seconds() > 10
        if cooldown_ok:
            try:
                old_id = sticky.get("last_message_id")
                if old_id:
                    try:
                        old_msg = await message.channel.fetch_message(int(old_id))
                        await old_msg.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass
                new_msg = await message.channel.send(sticky["content"])
                sticky_messages_col.update_one(
                    {"channel_id": str(message.channel.id)},
                    {"$set": {"last_message_id": str(new_msg.id), "last_reposted_at": now}},
                )
            except discord.HTTPException as e:
                print(f"[STICKY] Failed to repost in channel {message.channel.id}: {e}", flush=True)

    # Nécessaire pour que les éventuelles commandes à préfixe continuent de fonctionner
    # (aucune n'est définie actuellement, mais ça évite un piège classique si on en ajoute plus tard)
    await bot.process_commands(message)


# Logs
@bot.event
async def on_member_join(member):
    cfg = get_config(member.guild.id)

    # Vérification par âge de compte : kick automatique si le compte est
    # trop récent (protection contre les raids par comptes fraîchement créés).
    min_age_days = cfg.get("min_account_age_days", 0)
    if min_age_days > 0:
        account_age = datetime.datetime.now(datetime.timezone.utc) - member.created_at
        if account_age.days < min_age_days:
            try:
                await member.send(
                    f"Your account is too new to join **{member.guild.name}** right now "
                    f"(minimum account age: {min_age_days} day(s)). Feel free to try again later."
                )
            except discord.HTTPException:
                pass
            try:
                await member.kick(reason=f"Account age check: account is younger than {min_age_days} day(s)")
                log_sanction(member.guild.id, member.id, "kick", f"Account too new (< {min_age_days}d)", "automod")
                embed = discord.Embed(
                    title="🛡️ Account Age Check",
                    description=f"**{member}** was kicked automatically — account created {account_age.days} day(s) ago (minimum: {min_age_days}).",
                    color=0xff6600,
                )
                await _send_log_embed(member.guild, embed)
            except discord.HTTPException as e:
                print(f"[ACCOUNTAGE] Failed to kick {member.id}: {e}", flush=True)
            return  # pas la peine de continuer le reste de on_member_join pour un membre qu'on vient de kick

    # Suivi des invitations : compare le cache d'avant à l'état actuel pour
    # repérer quelle invite a vu son compteur augmenter.
    inviter_id = None
    invite_code_used = None
    try:
        before = _invite_cache.get(member.guild.id, {})
        current_invites = await member.guild.invites()
        for inv in current_invites:
            if inv.uses and inv.uses > before.get(inv.code, 0):
                inviter_id = inv.inviter.id if inv.inviter else None
                invite_code_used = inv.code
                break
        _invite_cache[member.guild.id] = {inv.code: (inv.uses or 0) for inv in current_invites}
        if inviter_id:
            invite_uses_col.insert_one({
                "guild_id": str(member.guild.id),
                "inviter_id": str(inviter_id),
                "invited_user_id": str(member.id),
                "invite_code": invite_code_used,
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
            })
    except discord.HTTPException:
        pass  # probablement pas la permission Manage Server

    # Quarantine : compte jugé suspect (trop récent) -> rôle de confinement +
    # file d'attente de review, au lieu de l'autorole/vérification normale.
    quarantined = False
    if cfg.get("quarantine_enabled") and member.guild.id not in bot.locked_guilds:
        suspicion_days = cfg.get("quarantine_suspicion_days", 7)
        account_age_days = (datetime.datetime.now(datetime.timezone.utc) - member.created_at).days
        quarantine_role_id = cfg.get("quarantine_role_id")
        quarantine_channel_id = cfg.get("quarantine_channel_id")
        if account_age_days < suspicion_days and quarantine_role_id and quarantine_channel_id:
            role = member.guild.get_role(int(quarantine_role_id))
            channel = member.guild.get_channel(int(quarantine_channel_id))
            if role and channel:
                quarantined = True
                try:
                    await member.add_roles(role, reason="Quarantine: new account held for review")
                except discord.HTTPException as e:
                    log_json("error", "quarantine_role_add_failed", user_id=member.id, guild_id=member.guild.id, error=str(e))
                quarantine_col.insert_one({
                    "guild_id": str(member.guild.id),
                    "user_id": str(member.id),
                    "account_age_days": account_age_days,
                    "joined_at": datetime.datetime.now(datetime.timezone.utc),
                    "reviewed": False,
                    "reminded": False,
                })
                review_embed = discord.Embed(
                    title="🔎 New Account Held for Review",
                    description=f"**{member}** (<@{member.id}>) joined with an account only **{account_age_days} day(s)** old.",
                    color=0xffaa00,
                )
                review_embed.add_field(name="Hold duration", value=f"{cfg.get('quarantine_hold_minutes', 30)} min before a reminder ping", inline=False)
                await channel.send(embed=review_embed)

    autorole_id = cfg.get("autorole")
    verified_flow = cfg.get("verification_enabled") and cfg.get("unverified_role_id")
    # On ne file pas de rôle si le serveur est déjà verrouillé (raid en cours),
    # ou si le membre vient d'être placé en quarantine juste au-dessus : pas de
    # vérification anti-alt/anti-raid en amont, donc mieux vaut ne rien
    # distribuer automatiquement tant que la situation n'est pas confirmée saine.
    if not quarantined and member.guild.id not in bot.locked_guilds:
        if verified_flow:
            role = member.guild.get_role(int(cfg["unverified_role_id"]))
            if role:
                try:
                    await member.add_roles(role, reason="Awaiting verification")
                except discord.HTTPException as e:
                    print(f"[VERIFICATION] Failed to add Unverified role to {member.id}: {e}", flush=True)
        elif autorole_id:
            role = member.guild.get_role(autorole_id)
            if role:
                await member.add_roles(role)

    # Message de bienvenue (texte configurable) + bannière avec la seule photo de profil.
    if cfg.get("welcome_banner_enabled") and cfg.get("welcome_channel_id"):
        welcome_channel = member.guild.get_channel(int(cfg["welcome_channel_id"]))
        if welcome_channel:
            asyncio.create_task(send_welcome(member, welcome_channel, cfg.get("welcome_message")))

    # Détection de pattern de raid par pseudo (indépendante du seuil brut de
    # check_raid -- peut capter un raid coordonné même sous le seuil de comptage).
    if cfg.get("raid_pattern_alert_enabled"):
        if record_join_name_and_check_pattern(member.guild.id, member.name):
            pattern_alert = discord.Embed(
                title="⚠️ Suspicious Join Pattern",
                description=f"Recent joins share a suspicious username pattern (possible coordinated raid). Latest: **{member}**.",
                color=0xff6600,
            )
            await _send_log_embed(member.guild, pattern_alert)
    embed = discord.Embed(title="✅ Member Joined", color=0x00cc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    if inviter_id:
        embed.add_field(name="Invited by", value=f"<@{inviter_id}> (`{invite_code_used}`)", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await _send_log_embed(member.guild, embed)

    # Anti-raid : seuils configurables par serveur (/config automod raidcount/raidwindow).
    if check_raid(member.guild.id, cfg) and member.guild.id not in bot.locked_guilds:
        bot.locked_guilds.add(member.guild.id)
        alert = discord.Embed(
            title="🚨 Raid Detected",
            description=(
                f"{RAID_JOIN_COUNT}+ members joined **{member.guild.name}** in under {RAID_WINDOW_SECONDS} seconds.\n"
                "The server has been **automatically locked** — most commands now require Administrator until it's unlocked.\n\n"
                f"Use `/botunlock` (server owner only) or the mobile app to unlock once it's safe."
            ),
            color=0xff0000,
        )
        await _send_log_embed(member.guild, alert)
        try:
            owner = member.guild.owner or await member.guild.fetch_owner()
            await owner.send(embed=alert)
        except Exception as e:
            print(f"Anti-raid owner DM failed: {e}", flush=True)

@bot.event
async def on_member_remove(member):
    embed = discord.Embed(title="❌ Member Left", color=0xff0000)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await _send_log_embed(member.guild, embed)

@bot.event
async def on_member_update(before, after):
    cfg = get_config(after.guild.id)

    # Détecte un boost qui vient de démarrer (premium_since passe de None à une date)
    # -- toujours actif, indépendamment du toggle "detailed logs".
    if before.premium_since is None and after.premium_since is not None:
        channel_id = cfg.get("boost_channel_id")
        channel = after.guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            channel = discord.utils.get(after.guild.text_channels, name=cfg.get("logs_channel", "logs"))
        if channel:
            embed = discord.Embed(
                title="🎉 New Server Boost!",
                description=f"Thanks {after.mention} for boosting **{after.guild.name}**!",
                color=0xf47fff,
            )
            embed.set_thumbnail(url=after.display_avatar.url)
            boost_count = after.guild.premium_subscription_count
            if boost_count:
                embed.set_footer(text=f"{after.guild.name} now has {boost_count} boosts")
            await channel.send(embed=embed)

    if not cfg.get("detailed_logs_enabled"):
        return

    if before.nick != after.nick:
        embed = discord.Embed(title="✏️ Nickname Changed", color=0x3399ff)
        embed.add_field(name="Member", value=f"**{after}**", inline=True)
        embed.add_field(name="Before", value=before.nick or "*(none)*", inline=True)
        embed.add_field(name="After", value=after.nick or "*(none)*", inline=True)
        await _send_log_embed(after.guild, embed)

    before_roles = set(before.roles)
    after_roles = set(after.roles)
    if before_roles != after_roles:
        added = after_roles - before_roles
        removed = before_roles - after_roles
        if added or removed:
            embed = discord.Embed(title="🎭 Roles Updated", color=0x3399ff)
            embed.add_field(name="Member", value=f"**{after}**", inline=False)
            if added:
                embed.add_field(name="Added", value=", ".join(r.mention for r in added), inline=True)
            if removed:
                embed.add_field(name="Removed", value=", ".join(r.mention for r in removed), inline=True)
            await _send_log_embed(after.guild, embed)


@bot.event
async def on_voice_state_update(member, before, after):
    cfg = get_config(member.guild.id)

    # Salons vocaux temporaires ("Join to Create") : indépendant du toggle
    # detailed_logs, doit toujours fonctionner si configuré.
    jtc_channel_id = cfg.get("jtc_channel_id")
    if jtc_channel_id and after.channel and after.channel.id == int(jtc_channel_id):
        category = after.channel.category
        try:
            new_channel = await member.guild.create_voice_channel(
                name=f"{member.display_name}'s channel"[:100],
                category=category,
                reason=f"Join-to-Create channel for {member}",
            )
            await new_channel.set_permissions(member, manage_channels=True, move_members=True)
            await member.move_to(new_channel, reason="Join-to-Create")
            _temp_voice_channels[new_channel.id] = member.guild.id
        except discord.HTTPException as e:
            print(f"[JTC] Failed to create temp channel for {member.id}: {e}", flush=True)

    # Suppression auto d'un salon temporaire une fois vide.
    if before.channel and before.channel.id in _temp_voice_channels:
        if len(before.channel.members) == 0:
            try:
                await before.channel.delete(reason="Join-to-Create: channel empty")
            except discord.HTTPException:
                pass
            _temp_voice_channels.pop(before.channel.id, None)

    if not cfg.get("detailed_logs_enabled"):
        return

    if before.channel is None and after.channel is not None:
        embed = discord.Embed(description=f"🔊 **{member}** joined voice channel {after.channel.mention}", color=0x00cc00)
        await _send_log_embed(member.guild, embed)
    elif before.channel is not None and after.channel is None:
        embed = discord.Embed(description=f"🔇 **{member}** left voice channel {before.channel.mention}", color=0xff6600)
        await _send_log_embed(member.guild, embed)
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        embed = discord.Embed(
            description=f"🔀 **{member}** moved from {before.channel.mention} to {after.channel.mention}",
            color=0x3399ff,
        )
        await _send_log_embed(member.guild, embed)

@bot.event
async def on_message_delete(message):
    if message.guild is None or message.author.bot:
        return
    embed = discord.Embed(title="🗑️ Message Deleted", color=0xff6600)
    embed.add_field(name="Author", value=f"**{message.author}**", inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.add_field(name="Content", value=message.content or "*(empty)*", inline=False)
    await _send_log_embed(message.guild, embed)

@bot.event
async def on_message_edit(before, after):
    if before.guild is None or before.author.bot:
        return
    embed = discord.Embed(title="✏️ Message Edited", color=0x3399ff)
    embed.add_field(name="Author", value=f"**{before.author}**", inline=True)
    embed.add_field(name="Channel", value=before.channel.mention, inline=True)
    embed.add_field(name="Before", value=before.content or "*(empty)*", inline=False)
    embed.add_field(name="After", value=after.content or "*(empty)*", inline=False)
    await _send_log_embed(before.guild, embed)


async def _handle_translate_reaction(payload):
    """Traduit le contenu du message réagi (🌐) vers l'anglais et l'envoie en
    DM au réacteur. deep-translator passe par Google Translate (pas de clé
    API) -- best-effort, comme yt-dlp ailleurs dans ce projet : jamais fatal
    si ça échoue, juste un DM d'erreur discret."""
    try:
        channel = bot.get_channel(payload.channel_id)
        if channel is None:
            return
        message = await channel.fetch_message(payload.message_id)
        if not message.content.strip():
            return
        translated = await asyncio.to_thread(lambda: GoogleTranslator(source="auto", target="en").translate(message.content))
        reactor = bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
        embed = discord.Embed(title="🌐 Translation", description=translated[:4000], color=0x3399ff)
        embed.set_footer(text=f"Original by {message.author}")
        await reactor.send(embed=embed)
    except Exception as e:
        log_json("error", "translate_reaction_failed", error=str(e))


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.member is None or payload.member.bot:
        return
    emoji_str = str(payload.emoji)

    # Traduction auto : réaction 🌐 sur un message -> traduction envoyée en DM
    # au réacteur. Complètement indépendant du système de reaction roles ci-dessous.
    if emoji_str == "🌐":
        cfg = get_config(payload.guild_id)
        if cfg.get("auto_translate_enabled"):
            asyncio.create_task(_handle_translate_reaction(payload))

    doc = get_reaction_role(payload.guild_id, payload.message_id, emoji_str)
    if not doc:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    role = guild.get_role(int(doc["role_id"]))
    if role and role < guild.me.top_role:
        try:
            await payload.member.add_roles(role, reason="Reaction role")
        except Exception as e:
            print(f"Reaction role add error: {e}", flush=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    emoji_str = str(payload.emoji)
    doc = get_reaction_role(payload.guild_id, payload.message_id, emoji_str)
    if not doc:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    role = guild.get_role(int(doc["role_id"]))
    if role and role < guild.me.top_role:
        try:
            await member.remove_roles(role, reason="Reaction role removed")
        except Exception as e:
            print(f"Reaction role remove error: {e}", flush=True)


# ============================================================
# ===================  API REST (App Android) =================
# ============================================================

_SENTRY_DSN = os.environ.get("SENTRY_DSN")
if _SENTRY_DSN:
    sentry_sdk.init(dsn=_SENTRY_DSN, integrations=[FlaskIntegration()], traces_sample_rate=0.1)
    log_json("info", "sentry_initialized")
# Si SENTRY_DSN n'est pas défini sur Render, sentry_sdk.init() n'est jamais
# appelé -- aucun comportement différent, complètement silencieux/opt-in.

# template_folder explicite : avec Flask('') (nom vide), Flask retombe sur le
# dossier courant (cwd) pour chercher templates/ -- ça marchait sur Render (lancé
# depuis la racine du repo) mais cassait dès qu'on lance main.py depuis ailleurs.
def _internal_bot_service_options():
    if os.environ.get("NEXUS_BOT_SERVICE_INTERNAL_ENABLED", "").strip().lower() != "true":
        return {}

    from web.bot_service import get_bot_service
    from web.internal_bot_service import internal_bot_service_app_options

    return internal_bot_service_app_options(
        environ=os.environ,
        service_factory=get_bot_service,
        nonce_collection_provider=lambda: db["bot_service_nonces"] if db is not None else None,
    )


api = create_web_app(
    config={"SECRET_KEY": os.environ["FLASK_SECRET_KEY"]},
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
    **_internal_bot_service_options(),
)


# ---------- Templates HTML ----------

def run_api():
    port = int(os.getenv("PORT", 8080))
    try:
        # Serveur WSGI de production : le serveur intégré de Flask n'est qu'un serveur de développement.
        from waitress import serve
        serve(api, host='0.0.0.0', port=port, threads=16)
    except Exception as e:  # waitress absent ou incompatible : repli sur le serveur de développement
        print(f"[WEB] waitress indisponible ({e!r}), repli sur le serveur de développement Flask", flush=True)
        api.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_api)
    t.daemon = True
    t.start()


# Arrêt propre : Render envoie SIGTERM avant de tuer le process lors d'un
# redéploiement. Par défaut, SIGTERM termine Python immédiatement sans
# nettoyage -- on le transforme en KeyboardInterrupt (comme Ctrl+C), que
# discord.py sait déjà gérer proprement (fermeture de la connexion websocket
# avant de quitter, plutôt qu'une coupure brutale en plein milieu).
def _handle_sigterm(signum, frame):
    print("[SHUTDOWN] Received SIGTERM, shutting down gracefully...", flush=True)
    log_json("info", "shutdown_sigterm")
    raise KeyboardInterrupt()


# Si Discord/Cloudflare renvoie un 429 au login (rate limit) : bot.run() ferme
# sa session HTTP en interne dès qu'il ressort (même sur erreur, via son
# `async with self:`), donc rappeler bot.run() sur CE MÊME objet `bot` ensuite
# plante avec "Session is closed" -- discord.py ne supporte pas de relancer un
# client déjà fermé. Au lieu de boucler dans le même process, on attend le
# backoff ICI (le process reste vivant pendant l'attente, donc Render ne nous
# redémarre pas entre-temps), puis on laisse le process ressortir pour de bon :
# Render le relance avec un `bot` tout neuf, sans l'état cassé de l'ancien.
# Contrepartie connue : le compteur de tentatives repart de zéro à chaque
# redémarrage process (pas d'escalade 60s/120s/240s qui survit à un restart) --
# avant si pépin après la 1ère attente de 60s.
def main():
    """Point d'entrée : le serveur Flask, le handler SIGTERM et bot.run() ne
    démarrent QUE si on lance `python main.py`. Importer main.py (tests,
    futurs modules) n'a donc plus d'effet de bord."""
    keep_alive()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        bot.run(os.getenv("TOKEN"))
    except discord.errors.HTTPException as e:
        if e.status == 429:
            print("[LOGIN] 429 rate limited par Discord/Cloudflare, attente 60s avant de laisser Render redémarrer proprement", flush=True)
            time.sleep(60)
        raise
    except KeyboardInterrupt:
        pass

    if mongo:
        try:
            mongo.close()
            print("[SHUTDOWN] MongoDB connection closed.", flush=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
