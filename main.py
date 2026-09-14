import discord
from discord.ext import commands
from discord import app_commands
import os
from flask import Flask, request, jsonify, g, session, redirect, url_for, render_template_string, flash, get_flashed_messages
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
import yt_dlp
import re
import secrets
import subprocess
import signal
import utils
from utils import parse_duration, check_caps, check_banned_words, check_banned_domains
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

def init_mongo():
    global mongo, db, warns_col, config_col, locked_channels_col, sanctions_col, reaction_roles_col, notes_col, audit_col, bot_state_col, killswitch_tokens_col, case_counters_col, sticky_messages_col, ban_appeals_col, server_backups_col, invite_uses_col, role_menus_col, scheduled_col
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

def record_audit(guild_id, actor_id, actor_name, action, details=""):
    """Trace de chaque changement de config (dashboard ou commande) pour
    l'audit log affiché dans le dashboard — indépendant du log Discord
    optionnel (log_dashboard_actions)."""
    try:
        audit_col.insert_one({
            "guild_id": str(guild_id),
            "actor_id": str(actor_id) if actor_id else None,
            "actor_name": actor_name,
            "action": action,
            "details": details[:500] if details else "",
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
        })
    except Exception as e:
        print(f"[AUDIT] Failed to record audit entry: {e}", flush=True)

def get_audit_log(guild_id, limit=20):
    """Fil d'activité multi-source : fusionne les changements faits depuis
    le dashboard (audit_col) et les sanctions prises via les commandes
    (sanctions_col), triés chronologiquement — une vraie vue multi-admin,
    pas juste les actions web."""
    entries = []
    try:
        entries.extend(audit_col.find({"guild_id": str(guild_id)}).sort("timestamp", -1).limit(limit))
    except Exception as e:
        print(f"[AUDIT] Failed to fetch audit log: {e}", flush=True)

    try:
        for doc in sanctions_col.find({"guild_id": str(guild_id)}).sort("timestamp", -1).limit(limit):
            mod_id = doc.get("moderator_id", "")
            if mod_id == "mobile_app":
                actor_name = "📱 Mobile App"
            elif mod_id == "automod":
                actor_name = "🤖 AutoMod"
            else:
                try:
                    cached_user = bot.get_user(int(mod_id))
                    actor_name = str(cached_user) if cached_user else f"Moderator {mod_id}"
                except (ValueError, TypeError):
                    actor_name = f"Moderator {mod_id}"
            entries.append({
                "actor_name": actor_name,
                "action": f"{doc.get('type', 'sanction').replace('_', ' ').title()} (case #{doc.get('case_id', '?')})",
                "details": doc.get("reason", ""),
                "timestamp": doc["timestamp"],
            })
    except Exception as e:
        print(f"[AUDIT] Failed to fetch sanctions for activity feed: {e}", flush=True)

    def _sort_key(entry):
        ts = entry["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return ts

    entries.sort(key=_sort_key, reverse=True)
    return entries[:limit]

def get_config(guild_id):
    doc = config_col.find_one({"guild_id": str(guild_id)})
    if not doc:
        doc = {
            "guild_id": str(guild_id),
            "logs_channel": "logs",
            "autorole": None,
            "allowed_roles": [],
            "command_roles": {},
            "command_templates": {},
            "language": None,
            "log_dashboard_actions": False,
            "log_webhook_url": None,
            "log_webhook_channel_id": None,
            "tickets_enabled": False,
            "ticket_category_id": None,
            "ticket_support_role_id": None,
            "ticket_archive_category_id": None,
            "appeal_channel_id": None,
            "boost_channel_id": None,
            "detailed_logs_enabled": False,
            "banned_words": [],
            "antinuke_enabled": False,
            "antinuke_threshold": 5,
            "weekly_digest_enabled": False,
            "last_digest_sent_at": None,
            "member_count_channel_id": None,
            "min_account_age_days": 0,
            "banned_domains": [],
            "automod_spam_count": 10,
            "automod_spam_window": 5,
            "automod_caps_ratio": 0.7,
            "automod_raid_count": 5,
            "automod_raid_window": 10,
            "warn_escalation_enabled": False,
            "warn_mute_threshold": 3,
            "warn_mute_minutes": 10,
            "warn_kick_threshold": 5,
            "jtc_channel_id": None,
            "jtc_category_id": None,
            "pending_role_menu_items": [],
        }
        config_col.insert_one(doc)
    return doc

def update_config(guild_id, key, value):
    config_col.update_one({"guild_id": str(guild_id)}, {"$set": {key: value}}, upsert=True)

def add_command_role(guild_id, command_name, role_id):
    """Autorise un rôle à utiliser une commande spécifique pour ce serveur."""
    config_col.update_one(
        {"guild_id": str(guild_id)},
        {"$addToSet": {f"command_roles.{command_name}": role_id}},
        upsert=True,
    )

def remove_command_role(guild_id, command_name, role_id):
    """Retire l'autorisation d'un rôle pour une commande spécifique sur ce serveur."""
    config_col.update_one(
        {"guild_id": str(guild_id)},
        {"$pull": {f"command_roles.{command_name}": role_id}},
        upsert=True,
    )

def set_command_template(guild_id, template_name, commands_list):
    """Crée ou remplace un template de permissions propre à ce serveur."""
    config_col.update_one(
        {"guild_id": str(guild_id)},
        {"$set": {f"command_templates.{template_name}": commands_list}},
        upsert=True,
    )

def get_warns(guild_id, user_id):
    doc = warns_col.find_one({"guild_id": str(guild_id), "user_id": str(user_id)})
    return doc["count"] if doc else 0

def set_warns(guild_id, user_id, count):
    warns_col.update_one({"guild_id": str(guild_id), "user_id": str(user_id)}, {"$set": {"count": count}}, upsert=True)

def mark_channel_locked(guild_id, channel_id, channel_name, locked_by, channel_type="text"):
    locked_channels_col.update_one(
        {"guild_id": str(guild_id), "channel_id": str(channel_id)},
        {"$set": {
            "channel_name": channel_name,
            "channel_type": channel_type,
            "locked_by": str(locked_by),
            "locked_at": datetime.datetime.utcnow(),
        }},
        upsert=True,
    )

def mark_channel_unlocked(guild_id, channel_id):
    locked_channels_col.delete_one({"guild_id": str(guild_id), "channel_id": str(channel_id)})

def get_locked_channels(guild_id):
    return list(locked_channels_col.find({"guild_id": str(guild_id)}))

def get_next_case_id(guild_id):
    """Compteur atomique par serveur — jamais de doublon même avec des
    sanctions concurrentes."""
    doc = case_counters_col.find_one_and_update(
        {"_id": str(guild_id)},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["seq"]

def log_sanction(guild_id, user_id, sanction_type, reason, moderator_id):
    """Enregistre une sanction dans l'historique. moderator_id peut être un ID Discord,
    'mobile_app' (action via l'app Android), ou 'automod' (déclenchée automatiquement).
    Retourne le case_id généré, consultable ensuite via /case view."""
    case_id = get_next_case_id(guild_id)
    sanctions_col.insert_one({
        "guild_id": str(guild_id),
        "case_id": case_id,
        "user_id": str(user_id),
        "type": sanction_type,
        "reason": reason or "No reason provided",
        "moderator_id": str(moderator_id),
        "timestamp": datetime.datetime.now(datetime.timezone.utc),
    })
    return case_id

def get_case(guild_id, case_id):
    return sanctions_col.find_one({"guild_id": str(guild_id), "case_id": case_id})


def create_sanction_appeal(guild_id, case_id, sanction_type, user_id, user_name, reason, token):
    ban_appeals_col.insert_one({
        "token": token,
        "guild_id": str(guild_id),
        "case_id": case_id,
        "sanction_type": sanction_type,
        "user_id": str(user_id),
        "user_name": user_name,
        "ban_reason": reason,  # nom de champ conservé pour compat avec les anciens enregistrements
        "status": "pending",  # pending -> submitted -> accepted / denied
        "appeal_text": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "submitted_at": None,
        "resolved_at": None,
    })

def get_ban_appeal(token):
    return ban_appeals_col.find_one({"token": token})

def get_ban_appeal_by_case(guild_id, case_id):
    return ban_appeals_col.find_one({"guild_id": str(guild_id), "case_id": case_id})


async def post_appeal_for_review(guild, appeal, appeal_text):
    cfg = get_config(guild.id)
    channel_id = cfg.get("appeal_channel_id")
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if channel is None:
        return
    sanction_type = appeal.get("sanction_type", "ban")
    icon = SANCTION_ICONS.get(sanction_type, "📨")
    embed = discord.Embed(title=f"{icon} Appeal — Case #{appeal['case_id']} ({sanction_type})", color=0xffb84d)
    embed.add_field(name="User", value=f"{appeal['user_name']} ({appeal['user_id']})", inline=False)
    embed.add_field(name="Original reason", value=appeal["ban_reason"][:1000] or "No reason provided", inline=False)
    embed.add_field(name="Appeal", value=appeal_text[:1000], inline=False)
    embed.set_footer(text=f"Review with: /appeal accept case_id:{appeal['case_id']}  or  /appeal deny case_id:{appeal['case_id']}")
    await channel.send(embed=embed)


async def reverse_sanction(guild, sanction_type, user_id):
    """Tente d'annuler les effets d'une sanction acceptée en appel. Retourne
    une courte description de ce qui a été fait (ou pas) pour informer le
    staff, même quand aucune action automatique n'est possible."""
    if sanction_type in ("ban", "tempban", "softban"):
        try:
            await guild.unban(discord.Object(id=int(user_id)), reason="Appeal accepted")
            return "user unbanned"
        except discord.HTTPException:
            return "user was already not banned"
    if sanction_type == "mute":
        member = guild.get_member(int(user_id))
        if member is not None and member.timed_out_until:
            try:
                await member.timeout(None, reason="Appeal accepted")
                return "timeout removed"
            except discord.HTTPException:
                return "couldn't remove the timeout"
        return "user isn't currently timed out"
    if sanction_type == "warn":
        count = get_warns(guild.id, user_id)
        if count > 0:
            set_warns(guild.id, user_id, count - 1)
            return "one warning removed"
        return "no warnings left to remove"
    return "no automatic action for this sanction type — reviewed manually"


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

def get_global_lock():
    doc = bot_state_col.find_one({"_id": "global"})
    return bool(doc and doc.get("locked"))

def set_global_lock(locked: bool):
    bot_state_col.update_one({"_id": "global"}, {"$set": {"locked": locked}}, upsert=True)


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


async def get_owner_user():
    app_info = await bot.application_info()
    if app_info.owner:
        return app_info.owner
    if app_info.team:
        try:
            return await bot.fetch_user(app_info.team.owner_id)
        except discord.HTTPException:
            return None
    return None


async def notify_owner_killswitch(locked: bool):
    owner = await get_owner_user()
    if owner is None:
        return
    msg = (
        "🔒 Nexus has just been **locked down** across all servers via the email kill-switch."
        if locked else
        "🔓 Nexus has just been **unlocked** and is back to normal across all servers."
    )
    try:
        await owner.send(msg)
    except discord.HTTPException:
        pass


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



API_KEY = os.getenv("API_KEY")  # clé secrète pour protéger l'API, à définir sur Render

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
        await interaction.response.send_message(embed=embed)
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
        await interaction.response.send_message(embed=embed)
        return False
    if native_permission is None:
        return True
    if getattr(interaction.user.guild_permissions, native_permission, False):
        return True
    embed = discord.Embed(description="❌ You don't have permission to use this command.", color=0xff0000)
    await interaction.response.send_message(embed=embed)
    return False

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
    if not getattr(bot, "_nexus_persistent_views_added", False):
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

# /ban
@bot.tree.command(name="ban", description="Ban a member")
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
SANCTION_ICONS = {
    "ban": "🔨",
    "kick": "👢",
    "mute": "🔇",
    "warn": "⚠️",
    "softban": "🧹",
    "note": "📝",
    "automod_spam": "🤖",
    "automod_link": "🔗",
    "automod_caps": "🔠",
}

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
    await interaction.followup.send(embed=view.build_embed(), view=view if view.max_page > 0 else None)

# /broadcast
@bot.tree.command(name="broadcast", description="Send a broadcast message")
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


@bot.tree.command(name="schedule", description="Schedule a message to be posted later")
@app_commands.describe(channel="Channel to post in", message="What to send", when="When to send it: 30m, 2h, 1d, 1w")
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

config_group = app_commands.Group(name="config", description="Configure the bot (Administrator only)")

@config_group.command(name="logs", description="Set the logs channel")
@has_admin()
async def config_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    update_config(interaction.guild_id, "logs_channel", channel.name)
    embed = discord.Embed(title="✅ Logs Channel Updated", description=f"Logs will now be sent to {channel.mention}", color=0x00cc00)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@config_group.command(name="autorole", description="Set the auto-join role")
@has_admin()
async def config_autorole(interaction: discord.Interaction, role: discord.Role):
    update_config(interaction.guild_id, "autorole", role.id)
    embed = discord.Embed(title="✅ Auto-Role Updated", description=f"New members will get {role.mention}", color=0x00cc00)
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


@config_group.command(name="antinuke", description="Set the anti-nuke sensitivity threshold — admin only. Use /feature enable|disable to turn it on/off.")
@app_commands.describe(threshold="Destructive actions (bans/kicks/channel or role deletions) by the same person within 60s that trigger it (default 5)")
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


# Système de tickets par bouton : /config ticket setup|disable
ticket_group = app_commands.Group(
    name="ticket",
    description="Configure the button-based ticket system for this server — server owner only",
    parent=config_group,
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
appeal_group = app_commands.Group(name="appeal", description="Review sanction appeals — admin only")


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
backup_group = app_commands.Group(name="backup", description="Server structure backups (roles, channels, permissions) — server owner only")


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


export_group = app_commands.Group(name="export", description="Export server data as CSV — admin only")


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
    records = list(audit_col.find({"guild_id": str(interaction.guild_id)}).sort("timestamp", -1))
    if not records:
        await interaction.followup.send("No audit entries to export.", ephemeral=True)
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["actor_name", "action", "details", "timestamp"])
    for r in records:
        writer.writerow([r.get("actor_name", ""), r.get("action", ""), r.get("details", ""), r.get("timestamp", "")])
    file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename=f"audit_{interaction.guild_id}.csv")
    await interaction.followup.send("📄 Here's the export:", file=file, ephemeral=True)


bot.tree.add_command(export_group)


# Interrupteur unique pour toutes les fonctionnalités à bascule simple
# (on/off) — évite d'avoir une commande différente par feature.
FEATURE_TOGGLES = {
    "detailed_logs": {"config_key": "detailed_logs_enabled", "label": "Detailed activity logs (voice, nicknames, roles)"},
    "antinuke": {"config_key": "antinuke_enabled", "label": "Anti-nuke protection"},
    "weekly_digest": {"config_key": "weekly_digest_enabled", "label": "Weekly DM digest"},
    "dashboard_logging": {"config_key": "log_dashboard_actions", "label": "Log dashboard actions to the logs channel"},
}
FEATURE_CHOICES = [app_commands.Choice(name=v["label"], value=k) for k, v in FEATURE_TOGGLES.items()]

feature_group = app_commands.Group(name="feature", description="Enable or disable optional bot features — admin only")


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


class TicketCloseView(discord.ui.View):
    """Bouton persistant dans chaque ticket ouvert pour le fermer."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="nexus_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        cfg = get_config(interaction.guild_id)
        support_role = interaction.guild.get_role(cfg.get("ticket_support_role_id") or 0)
        is_opener = channel.topic and str(interaction.user.id) == channel.topic
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
        opener_id = channel.topic
        if opener_id and opener_id.isdigit():
            try:
                opener_user = await bot.fetch_user(int(opener_id))
                await opener_user.send(
                    content="How was your experience with this ticket?",
                    view=SatisfactionSurveyView(),
                )
            except discord.HTTPException:
                pass

        archive_category = None
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
    """Formulaire affiché au clic sur le bouton — la réponse est postée par
    le bot dans le salon du ticket dès sa création."""
    reason = discord.ui.TextInput(
        label="What do you need help with?",
        style=discord.TextStyle.paragraph,
        placeholder="Briefly describe your issue so the team can help faster...",
        max_length=500,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild_id)
        if not cfg.get("tickets_enabled"):
            await interaction.response.send_message("Tickets are currently disabled on this server.", ephemeral=True)
            return

        category = interaction.guild.get_channel(cfg.get("ticket_category_id") or 0)
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket system isn't configured properly. Ask an admin to run `/config ticket setup` again.",
                ephemeral=True,
            )
            return

        # Re-vérifié ici (pas juste au clic du bouton) : le formulaire prend
        # du temps à remplir, un doublon a pu être ouvert entre-temps.
        existing = discord.utils.get(category.text_channels, topic=str(interaction.user.id))
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
        support_role = interaction.guild.get_role(cfg.get("ticket_support_role_id") or 0)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        safe_name = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower().replace(" ", "-"))[:20] or "user"
        try:
            channel = await category.create_text_channel(
                name=f"ticket-{safe_name}",
                overwrites=overwrites,
                topic=str(interaction.user.id),
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't create the ticket channel: {e}", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎫 New Ticket",
            description=f"Hey {interaction.user.mention}! The support team will be with you shortly.",
            color=0x3399ff,
        )
        embed.add_field(name="What they need help with", value=self.reason.value[:1000], inline=False)
        await channel.send(
            content=support_role.mention if support_role else None,
            embed=embed,
            view=TicketCloseView(),
        )
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)


class TicketPanelView(discord.ui.View):
    """Bouton persistant posté par /config ticket setup pour ouvrir un ticket."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="nexus_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_config(interaction.guild_id)
        if not cfg.get("tickets_enabled"):
            await interaction.response.send_message(
                "Tickets are currently disabled on this server.", ephemeral=True
            )
            return

        category = interaction.guild.get_channel(cfg.get("ticket_category_id") or 0)
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket system isn't configured properly. Ask an admin to run `/config ticket setup` again.",
                ephemeral=True,
            )
            return

        # Empêche un utilisateur d'ouvrir plusieurs tickets en même temps :
        # on stocke son id dans le topic du salon pour le retrouver.
        existing = discord.utils.get(category.text_channels, topic=str(interaction.user.id))
        if existing:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}", ephemeral=True
            )
            return

        await interaction.response.send_modal(TicketOpenModal())


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
# =======================  MUSIC  ===========================
# ============================================================

# FFmpeg : Render (plan gratuit/standard) n'autorise pas apt-get (filesystem en lecture seule
# pour les paquets système), et le binaire statique téléchargé manuellement (johnvansickle.com)
# segfaultait (return code -11) -- probablement une incompatibilité d'architecture/glibc avec
# l'environnement Render. imageio-ffmpeg télécharge un binaire FFmpeg empaqueté spécifiquement
# pour fonctionner avec l'environnement Python détecté, ce qui est beaucoup plus fiable.
import imageio_ffmpeg
try:
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"[FFMPEG] Using imageio-ffmpeg binary at: {FFMPEG_PATH}", flush=True)
    print(f"[FFMPEG] File exists: {os.path.isfile(FFMPEG_PATH)}, executable: {os.access(FFMPEG_PATH, os.X_OK)}", flush=True)
except Exception as e:
    print(f"[FFMPEG] imageio_ffmpeg failed to provide a binary, falling back to system ffmpeg: {e}", flush=True)
    FFMPEG_PATH = "ffmpeg"

# Cookies YouTube : Render bloque souvent les requêtes anonymes ("Sign in to confirm you're not a bot").
# On les fournit via une variable d'env (contenu du fichier cookies.txt exporté du navigateur) et on
# les réécrit sur disque au démarrage, car yt-dlp veut un vrai fichier.
YOUTUBE_COOKIES_CONTENT = os.getenv("YOUTUBE_COOKIES")
YOUTUBE_COOKIES_PATH = "/tmp/youtube_cookies.txt"

if YOUTUBE_COOKIES_CONTENT:
    with open(YOUTUBE_COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES_CONTENT)

DOWNLOAD_DIR = "/tmp/music_cache"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

YTDL_BASE_OPTIONS = {
    # bestaudio en priorité, puis n'importe quel format jouable en dernier recours
    # (FFmpeg extraira la piste audio même d'un format vidéo+audio combiné).
    "format": "bestaudio/best/bv*+ba/b",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "ffmpeg_location": FFMPEG_PATH,
    # On télécharge et convertit en mp3 localement plutôt que de streamer l'URL YouTube en
    # direct : plus stable, évite les soucis de protocole réseau qui faisaient planter FFmpeg
    # pendant le streaming live (segfault -11 observé avec le streaming direct).
    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }],
}

# YouTube change régulièrement quel "client" d'extraction fonctionne, et ça casse
# souvent en quelques jours (chat du mainteneur yt-dlp). Situation constatée en
# sept. 2026 : le client "tv_downgraded" -- choisi automatiquement dès qu'un
# cookiefile est fourni -- est actuellement cassé côté YouTube et renvoie
# "The page needs to be reloaded." (github.com/yt-dlp/yt-dlp/issues/17389).
# Donc : on essaie D'ABORD sans cookies (web_embedded/tv, jamais concernés
# par ce bug précis), et on ne se rabat sur les cookies que si ça échoue --
# utile seulement pour du contenu réservé aux comptes connectés.
YTDL_OPTIONS_NO_COOKIES = {
    **YTDL_BASE_OPTIONS,
    "extractor_args": {"youtube": {"player_client": ["web_embedded", "tv"]}},
}

YTDL_OPTIONS_WITH_COOKIES = {
    **YTDL_BASE_OPTIONS,
    "extractor_args": {"youtube": {"player_client": ["default", "web_embedded"]}},
}
if YOUTUBE_COOKIES_CONTENT:
    YTDL_OPTIONS_WITH_COOKIES["cookiefile"] = YOUTUBE_COOKIES_PATH

# Conservé pour la recherche (extract_flat, ne télécharge rien -- beaucoup
# moins exposé à ce bug précis puisqu'il ne résout pas les formats jouables).
YTDL_OPTIONS = YTDL_OPTIONS_NO_COOKIES

# Fichier local mp3 déjà téléchargé : pas besoin de -reconnect (plus de réseau pendant la lecture).
FFMPEG_OPTIONS_TEMPLATE = {
    "before_options": "",
    "options": "-vn -af volume={volume}",
}

DEFAULT_VOLUME = 0.5  # 50%, ajustable par /volume (0.0 à 2.0)

ytdl_no_cookies = yt_dlp.YoutubeDL(YTDL_OPTIONS_NO_COOKIES)
ytdl_with_cookies = yt_dlp.YoutubeDL(YTDL_OPTIONS_WITH_COOKIES) if YOUTUBE_COOKIES_CONTENT else None

SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/(playlist|album)/([A-Za-z0-9]+)")


class Track:
    def __init__(self, title, filepath, webpage_url, duration, requester):
        self.title = title
        self.filepath = filepath  # chemin local du mp3 téléchargé
        self.webpage_url = webpage_url  # lien à afficher
        self.duration = duration
        self.requester = requester


class GuildMusicState:
    """Garde la queue et le lecteur vocal pour un serveur donné."""
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue = []
        self.voice_client = None
        self.current = None
        self.loop_lock = asyncio.Lock()
        self.volume = DEFAULT_VOLUME  # 0.0 à 2.0, ajustable via /volume

    def is_playing(self):
        return self.voice_client is not None and self.voice_client.is_playing()


music_states = {}  # {guild_id: GuildMusicState}


def get_music_state(guild_id):
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id)
    return music_states[guild_id]


async def resolve_query(query, requester):
    """
    Transforme une recherche texte ou un lien YouTube/SoundCloud/Spotify en Track jouable.
    Pour Spotify, on récupère le titre/artiste via oEmbed (pas besoin de clé API) et on recherche sur YouTube.
    """
    loop = asyncio.get_event_loop()

    spotify_match = SPOTIFY_TRACK_RE.search(query)
    if spotify_match:
        # Spotify ne permet pas le streaming direct (DRM) : on récupère le titre via oEmbed et on recherche sur YouTube
        try:
            import urllib.request
            import json as jsonlib
            oembed_url = f"https://open.spotify.com/oembed?url={query}"
            with urllib.request.urlopen(oembed_url, timeout=5) as resp:
                data = jsonlib.loads(resp.read().decode())
            title = data.get("title", "")
            query = f"ytsearch:{title}"
        except Exception:
            query = f"ytsearch:{query}"
    elif query.startswith("http"):
        pass  # lien direct YouTube/SoundCloud, yt-dlp gère nativement
    else:
        query = f"ytsearch:{query}"

    def extract():
        # download=True : on télécharge réellement le fichier, le post-processeur le convertit en mp3.
        # On essaie d'abord SANS cookies (évite le bug tv_downgraded actuel), et on ne se
        # rabat sur les cookies que si ça échoue vraiment (contenu réservé aux comptes connectés).
        try:
            info = ytdl_no_cookies.extract_info(query, download=True)
        except yt_dlp.utils.DownloadError as e:
            if ytdl_with_cookies is None:
                raise
            print(f"[MUSIC] No-cookies extraction failed ({e}), retrying with cookies...", flush=True)
            info = ytdl_with_cookies.extract_info(query, download=True)
        if "entries" in info:
            info = info["entries"][0]
        return info

    info = await loop.run_in_executor(None, extract)

    video_id = info.get("id")
    expected_mp3_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

    if not os.path.isfile(expected_mp3_path):
        raise FileNotFoundError(f"Downloaded file not found at expected path: {expected_mp3_path}")

    return Track(
        title=info.get("title", "Unknown title"),
        filepath=expected_mp3_path,
        webpage_url=info.get("webpage_url"),
        duration=info.get("duration"),
        requester=requester,
    )


def format_duration(seconds):
    if not seconds:
        return "Live/Unknown"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


async def play_next(guild_id):
    state = get_music_state(guild_id)
    async with state.loop_lock:
        if not state.queue:
            state.current = None
            return
        track = state.queue.pop(0)
        state.current = track

        if state.voice_client is None or not state.voice_client.is_connected():
            return

        ffmpeg_options = {
            "before_options": FFMPEG_OPTIONS_TEMPLATE["before_options"],
            "options": FFMPEG_OPTIONS_TEMPLATE["options"].format(volume=state.volume),
        }
        print(f"[FFMPEG] Launching with executable={FFMPEG_PATH}, file={track.filepath}", flush=True)
        print(f"[FFMPEG] File exists: {os.path.isfile(track.filepath)}", flush=True)
        try:
            source = discord.FFmpegPCMAudio(
                track.filepath, executable=FFMPEG_PATH, stderr=sys.stdout, **ffmpeg_options
            )
        except Exception as e:
            print(f"[FFMPEG] Failed to start FFmpeg process: {e}", flush=True)
            state.current = None
            return

        def after_play(error):
            if error:
                print(f"Player error: {error}", flush=True)
            # Nettoie le fichier mp3 seulement s'il n'est pas remis en queue (cas /volume qui relance le morceau courant)
            still_queued = state.queue and state.queue[0] is track
            if not still_queued:
                try:
                    if os.path.isfile(track.filepath):
                        os.remove(track.filepath)
                except Exception as cleanup_error:
                    print(f"Cleanup error: {cleanup_error}", flush=True)
            fut = asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
            try:
                fut.result()
            except Exception as e:
                print(f"after_play error: {e}", flush=True)

        state.voice_client.play(source, after=after_play)


async def ensure_voice_connected(interaction: discord.Interaction):
    """Connecte (ou déplace) le bot dans le salon vocal de l'utilisateur. Retourne le GuildMusicState, ou None si erreur déjà gérée."""
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        embed = discord.Embed(description="❌ You need to be in a voice channel.", color=0xff0000)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return None

    state = get_music_state(interaction.guild_id)
    voice_channel = interaction.user.voice.channel

    if state.voice_client is None or not state.voice_client.is_connected():
        state.voice_client = await voice_channel.connect()
    elif state.voice_client.channel != voice_channel:
        await state.voice_client.move_to(voice_channel)

    return state


async def queue_and_play(interaction: discord.Interaction, state: "GuildMusicState", track: "Track"):
    """Ajoute un track déjà résolu à la queue et lance la lecture si rien ne joue."""
    state.queue.append(track)

    if state.voice_client.is_playing() or state.voice_client.is_paused():
        embed = discord.Embed(title="➕ Added to queue", color=0x3399ff)
        embed.add_field(name="Title", value=track.title, inline=False)
        embed.add_field(name="Duration", value=format_duration(track.duration), inline=True)
        embed.add_field(name="Position", value=f"{len(state.queue)}", inline=True)
        await interaction.followup.send(embed=embed)
    else:
        embed = discord.Embed(title="🎵 Now Playing", color=0x00cc00)
        embed.add_field(name="Title", value=track.title, inline=False)
        embed.add_field(name="Duration", value=format_duration(track.duration), inline=True)
        embed.add_field(name="Requested by", value=track.requester.mention, inline=True)
        await interaction.followup.send(embed=embed)
        await play_next(interaction.guild_id)


_autocomplete_cache = {}  # {query_lowercase: (timestamp, [entries])}
AUTOCOMPLETE_CACHE_TTL = 300  # 5 minutes


async def play_autocomplete(interaction: discord.Interaction, current: str):
    """
    Callback d'autocomplete pour /play : propose des titres en live pendant la frappe.
    Discord impose ~3s de timeout et spamme une requête par frappe, donc on cache les résultats
    et on attend un minimum de caractères avant de chercher, comme FlaviBot.
    """
    current = current.strip()
    if len(current) < 2 or current.startswith("http"):
        return []

    cache_key = current.lower()
    cached = _autocomplete_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < AUTOCOMPLETE_CACHE_TTL:
        entries = cached[1]
    else:
        try:
            # Discord coupe la connexion après ~3s ; on se laisse une marge pour répondre à temps
            entries = await asyncio.wait_for(search_youtube(current, max_results=5), timeout=2.5)
        except asyncio.TimeoutError:
            print(f"autocomplete search timeout for query: {current}", flush=True)
            return []
        except Exception as e:
            print(f"autocomplete search error: {e}", flush=True)
            return []
        _autocomplete_cache[cache_key] = (now, entries)

    choices = []
    for entry in entries[:8]:
        title = entry.get("title", "Unknown")
        duration = format_duration(entry.get("duration"))
        label = f"{title} · {duration}"[:100]
        video_id = entry.get("id")
        raw_url = entry.get("url")
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        elif raw_url and raw_url.startswith("http"):
            video_url = raw_url
        else:
            # dernier recours : certains extracteurs renvoient l'id brut dans "url"
            video_url = f"https://www.youtube.com/watch?v={raw_url}" if raw_url else None
        if not video_url:
            continue
        choices.append(app_commands.Choice(name=label, value=video_url[:100]))
    return choices


@bot.tree.command(name="play", description="Play a song from YouTube, Spotify or SoundCloud")
@app_commands.autocomplete(query=play_autocomplete)
async def play(interaction: discord.Interaction, query: str):
    if not await check_access(interaction, "play", None):
        return

    await interaction.response.defer()
    state = await ensure_voice_connected(interaction)
    if state is None:
        return

    try:
        track = await resolve_query(query, interaction.user)
    except Exception as e:
        error_detail = str(e)[:200]
        embed = discord.Embed(
            description=f"❌ Couldn't find or load that track.\n```{error_detail}```",
            color=0xff0000,
        )
        await interaction.followup.send(embed=embed)
        print(f"resolve_query error: {e}", flush=True)
        return

    await queue_and_play(interaction, state, track)


async def search_youtube(query, max_results=5):
    """Renvoie une liste de résultats (titre, durée, url) depuis une recherche YouTube, sans télécharger l'audio."""
    loop = asyncio.get_event_loop()
    search_opts = dict(YTDL_OPTIONS)
    search_opts["extract_flat"] = True  # juste les métadonnées, pas l'URL de stream complète (plus rapide)

    def extract():
        with yt_dlp.YoutubeDL(search_opts) as ytdl_search:
            info = ytdl_search.extract_info(f"ytsearch{max_results}:{query}", download=False)
            return info.get("entries", [])

    return await loop.run_in_executor(None, extract)


class SearchResultSelect(discord.ui.Select):
    def __init__(self, results, requester):
        self.results = results
        self.requester = requester
        options = []
        for i, entry in enumerate(results):
            title = entry.get("title", "Unknown")[:90]
            duration = format_duration(entry.get("duration"))
            options.append(discord.SelectOption(label=f"{i + 1}. {title}", description=duration, value=str(i)))
        super().__init__(placeholder="Choose a track to play...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = self.results[int(self.values[0])]
        video_id = chosen.get("id")
        raw_url = chosen.get("url")
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        elif raw_url and raw_url.startswith("http"):
            video_url = raw_url
        else:
            video_url = f"https://www.youtube.com/watch?v={raw_url}" if raw_url else None

        if not video_url:
            embed = discord.Embed(description="❌ Couldn't load that track.", color=0xff0000)
            await interaction.followup.send(embed=embed)
            return

        state = await ensure_voice_connected(interaction)
        if state is None:
            return

        try:
            track = await resolve_query(video_url, self.requester)
        except Exception as e:
            error_detail = str(e)[:200]
            embed = discord.Embed(
                description=f"❌ Couldn't load that track.\n```{error_detail}```",
                color=0xff0000,
            )
            await interaction.followup.send(embed=embed)
            print(f"resolve_query error (search select): {e}", flush=True)
            return

        await queue_and_play(interaction, state, track)

        # Désactive le menu une fois un choix fait, pour éviter les doubles lectures
        self.disabled = True
        try:
            await interaction.message.edit(view=self.view)
        except Exception:
            pass


class SearchResultView(discord.ui.View):
    def __init__(self, results, requester):
        super().__init__(timeout=60)
        self.add_item(SearchResultSelect(results, requester))


@bot.tree.command(name="search", description="Search for a song and choose from a list of results")
async def search(interaction: discord.Interaction, query: str):
    if not await check_access(interaction, "search", None):
        return

    await interaction.response.defer()

    try:
        results = await search_youtube(query, max_results=5)
    except Exception as e:
        embed = discord.Embed(description="❌ Search failed, try again.", color=0xff0000)
        await interaction.followup.send(embed=embed)
        print(f"search_youtube error: {e}", flush=True)
        return

    if not results:
        embed = discord.Embed(description="❌ No results found.", color=0xff0000)
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(title=f"🔎 Search results for \"{query}\"", color=0x3399ff)
    lines = []
    for i, entry in enumerate(results):
        title = entry.get("title", "Unknown")
        duration = format_duration(entry.get("duration"))
        lines.append(f"**{i + 1}.** {title} · {duration}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Select a track from the menu below")

    view = SearchResultView(results, interaction.user)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    if not await check_access(interaction, "pause", None):
        return
    state = get_music_state(interaction.guild_id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.pause()
        embed = discord.Embed(description="⏸️ Paused.", color=0xff6600)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(description="❌ Nothing is playing.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    if not await check_access(interaction, "resume", None):
        return
    state = get_music_state(interaction.guild_id)
    if state.voice_client and state.voice_client.is_paused():
        state.voice_client.resume()
        embed = discord.Embed(description="▶️ Resumed.", color=0x00cc00)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(description="❌ Nothing is paused.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    if not await check_access(interaction, "skip", None):
        return
    state = get_music_state(interaction.guild_id)
    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
        embed = discord.Embed(description="⏭️ Skipped.", color=0x3399ff)
        await interaction.response.send_message(embed=embed)
        state.voice_client.stop()  # déclenche after_play -> play_next automatiquement
    else:
        embed = discord.Embed(description="❌ Nothing is playing.", color=0xff0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="volume", description="Set the playback volume (0 to 200%)")
async def volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
    if not await check_access(interaction, "volume", None):
        return
    state = get_music_state(interaction.guild_id)
    state.volume = percent / 100.0

    if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
        # Redémarre la source avec le nouveau volume sans perdre la position de la queue.
        # FFmpeg ne permet pas de changer le volume "à chaud" sans relancer le flux ; comme on
        # ne peut pas reprendre exactement où on en était facilement, on relance le morceau courant.
        current_track = state.current
        if current_track:
            state.queue.insert(0, current_track)
        state.voice_client.stop()

    embed = discord.Embed(description=f"🔊 Volume set to **{percent}%**.", color=0x3399ff)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    if not await check_access(interaction, "stop", None):
        return
    state = get_music_state(interaction.guild_id)
    # Nettoie les fichiers mp3 des morceaux encore en attente (espace disque limité sur Render)
    for queued_track in state.queue:
        try:
            if os.path.isfile(queued_track.filepath):
                os.remove(queued_track.filepath)
        except Exception as e:
            print(f"Cleanup error on stop: {e}", flush=True)
    state.queue.clear()
    state.current = None
    if state.voice_client:
        if state.voice_client.is_playing() or state.voice_client.is_paused():
            state.voice_client.stop()
        await state.voice_client.disconnect()
        state.voice_client = None
    embed = discord.Embed(description="⏹️ Stopped and cleared the queue.", color=0xff0000)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="queue", description="Show the current music queue")
async def queue_cmd(interaction: discord.Interaction):
    state = get_music_state(interaction.guild_id)
    embed = discord.Embed(title="🎶 Music Queue", color=0x3399ff)

    if state.current:
        embed.add_field(
            name="Now Playing",
            value=f"{state.current.title} · {format_duration(state.current.duration)}",
            inline=False,
        )
    else:
        embed.add_field(name="Now Playing", value="Nothing", inline=False)

    if state.queue:
        lines = []
        for i, track in enumerate(state.queue[:10], start=1):
            lines.append(f"{i}. {track.title} · {format_duration(track.duration)}")
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
        if len(state.queue) > 10:
            embed.set_footer(text=f"+ {len(state.queue) - 10} more in queue")
    else:
        embed.add_field(name="Up Next", value="Queue is empty", inline=False)

    await interaction.response.send_message(embed=embed)

# ============================================================
# ===================  AUTO-MODERATION  =====================
# ============================================================

# Suivi en mémoire des messages récents par utilisateur, pour la détection de spam.
# Pas besoin de persister ça en DB : ça repart à zéro si le bot redémarre, ce qui est très bien.
_recent_messages = {}  # {(guild_id, user_id): [timestamps]}

# Suivi en mémoire des arrivées récentes par serveur, pour la détection de raid.
_recent_joins = {}  # {guild_id: [timestamps]}

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
    return False


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
        if check_invite_link(message.content):
            await apply_automod_action(message, "automod_link", "posting an invite link")
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

    autorole_id = cfg.get("autorole")
    # On ne file pas l'autorole si le serveur est déjà verrouillé (raid en cours) :
    # pas de vérification anti-alt/anti-raid en amont, donc mieux vaut ne rien
    # distribuer automatiquement tant que la situation n'est pas confirmée saine.
    if autorole_id and member.guild.id not in bot.locked_guilds:
        role = member.guild.get_role(autorole_id)
        if role:
            await member.add_roles(role)
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


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.member is None or payload.member.bot:
        return
    emoji_str = str(payload.emoji)
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

api = Flask('')

def require_api_key(f):
    """Décorateur : vérifie le header X-API-Key sur chaque requête protégée.
    Deux clés sont acceptées :
    - la clé "maîtresse" définie dans la variable d'env API_KEY sur Render (accès à tous les serveurs)
    - la clé propre à UN serveur, générée via /config apikey (accès à ce serveur uniquement)

    Pose g.auth_guild_id = None si la clé maîtresse est utilisée (accès total),
    ou l'ID du serveur si une clé propre à un serveur est utilisée (accès limité à ce serveur).
    Utile pour les routes sans guild_id dans l'URL (/api/health, /api/guilds).
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-API-Key")
        if not key:
            return jsonify({"error": "Missing X-API-Key header"}), 401
        if API_KEY and key == API_KEY:
            g.auth_guild_id = None
            return f(*args, **kwargs)
        guild_id = kwargs.get("guild_id")
        if guild_id:
            cfg = get_config(guild_id)
            if cfg.get("api_key") and key == cfg["api_key"]:
                g.auth_guild_id = str(guild_id)
                return f(*args, **kwargs)
        elif bot.is_ready():
            # Pas de guild_id dans l'URL (ex: /api/health, /api/guilds) : on cherche
            # à quel serveur cette clé appartient parmi tous les serveurs du bot.
            for candidate_guild in bot.guilds:
                cfg = get_config(candidate_guild.id)
                if cfg.get("api_key") and key == cfg["api_key"]:
                    g.auth_guild_id = str(candidate_guild.id)
                    return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401
    return wrapper

def run_coroutine(coro, timeout=10):
    """
    Exécute une coroutine discord.py depuis un thread Flask (sync),
    en la poussant dans l'event loop du bot, et attend le résultat.
    """
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=timeout)

async def get_or_create_log_webhook(guild, channel):
    """Récupère le webhook de logs en cache pour ce salon, ou en crée un.
    Un webhook n'a pas besoin que le bot ait la permission d'envoyer des
    messages classiques dans ce salon — juste de l'avoir créé une fois."""
    cfg = get_config(guild.id)
    webhook_url = cfg.get("log_webhook_url")
    webhook_channel_id = cfg.get("log_webhook_channel_id")
    if webhook_url and webhook_channel_id == str(channel.id):
        try:
            return discord.Webhook.from_url(webhook_url, client=bot)
        except (discord.errors.InvalidData, ValueError):
            pass  # webhook invalide (supprimé manuellement ?), on en recrée un plus bas

    try:
        webhook = await channel.create_webhook(name="Nexus Logs", reason="Nexus log webhook")
        update_config(guild.id, "log_webhook_url", webhook.url)
        update_config(guild.id, "log_webhook_channel_id", str(channel.id))
        return webhook
    except discord.HTTPException as e:
        print(f"[WEBHOOK] Failed to create log webhook in guild {guild.id}: {e}", flush=True)
        return None


async def _send_log_embed(guild, embed):
    """Envoie un embed dans le channel de logs configuré pour ce serveur,
    via un webhook (créé/mis en cache automatiquement) plutôt qu'un message
    classique du bot — avec repli sur l'envoi classique si le webhook
    échoue ou ne peut pas être créé (ex: permission Manage Webhooks manquante)."""
    cfg = get_config(guild.id)
    channel = discord.utils.get(guild.text_channels, name=cfg.get("logs_channel", "logs"))
    if channel is None:
        return
    webhook = await get_or_create_log_webhook(guild, channel)
    if webhook is not None:
        try:
            await webhook.send(embed=embed, username="Nexus Logs")
            return
        except discord.HTTPException as e:
            print(f"[WEBHOOK] Failed to send via webhook, falling back to normal send: {e}", flush=True)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass

def log_dashboard_action(guild, actor_name, action, details=""):
    """Poste un embed dans le salon de logs Discord quand une action est
    faite depuis le dashboard web — seulement si l'option est activée
    (config.log_dashboard_actions)."""
    cfg = get_config(guild.id)
    if not cfg.get("log_dashboard_actions"):
        return None
    embed = discord.Embed(title="🖥️ Dashboard change", color=0x9b59b6)
    embed.add_field(name="By", value=actor_name, inline=True)
    embed.add_field(name="Action", value=action, inline=True)
    if details:
        embed.add_field(name="Details", value=details[:1000], inline=False)
    return _send_log_embed(guild, embed)

def log_ban(guild, member, reason):
    embed = discord.Embed(title="🔨 Member Banned", color=0xff0000)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Banned by", value="📱 mobile app", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)

def log_kick(guild, member, reason):
    embed = discord.Embed(title="👢 Member Kicked", color=0xff0000)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Kicked by", value="📱 mobile app", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)

def log_mute(guild, member, minutes, reason):
    embed = discord.Embed(title="🔇 Member Muted", color=0xff6600)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Muted by", value="📱 mobile app", inline=True)
    embed.add_field(name="Duration", value=f"{minutes} minutes", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)

def log_unmute(guild, member):
    embed = discord.Embed(title="🔊 Member Unmuted", color=0xff6600)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unmuted by", value="📱 mobile app", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)

def log_warn(guild, member, reason, count):
    embed = discord.Embed(title="⚠️ Member Warned", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Warned by", value="📱 mobile app", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)

def log_unwarn(guild, member, count):
    embed = discord.Embed(title="✅ Warning Removed", color=0xffcc00)
    embed.add_field(name="User", value=f"**{member}**", inline=True)
    embed.add_field(name="Unwarn by", value="📱 mobile app", inline=True)
    embed.add_field(name="Remaining Warnings", value=f"{count}", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    return _send_log_embed(guild, embed)

@api.route('/')
def home():
    return "Bot is alive!"

@api.route('/api/health', methods=['GET'])
@require_api_key
def health():
    if g.auth_guild_id:
        # Clé propre à un serveur : "locked" reflète uniquement ce serveur, pas les autres.
        return jsonify({
            "online": bot.is_ready(),
            "locked": int(g.auth_guild_id) in bot.locked_guilds,
            "guild_count": 1
        })
    return jsonify({
        "online": bot.is_ready(),
        "locked": len(bot.locked_guilds) > 0,
        "guild_count": len(bot.guilds) if bot.is_ready() else 0
    })

@api.route('/api/guilds', methods=['GET'])
@require_api_key
def list_guilds():
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    if g.auth_guild_id:
        # Clé propre à un serveur : ne renvoie que CE serveur, jamais les autres.
        guild = bot.get_guild(int(g.auth_guild_id))
        if not guild:
            return jsonify([])
        return jsonify([{"id": str(guild.id), "name": guild.name, "member_count": guild.member_count}])
    guilds = [{"id": str(gd.id), "name": gd.name, "member_count": gd.member_count} for gd in bot.guilds]
    return jsonify(guilds)

@api.route('/api/guilds/<guild_id>/stats', methods=['GET'])
@require_api_key
def guild_stats(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    async def _stats():
        bans = [b async for b in guild.bans()]
        muted = [m for m in guild.members if m.is_timed_out()]
        return len(bans), len(muted)

    try:
        ban_count, muted_count = run_coroutine(_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    warned_count = warns_col.count_documents({"guild_id": str(guild_id), "count": {"$gt": 0}})

    return jsonify({
        "guild_id": str(guild.id),
        "name": guild.name,
        "member_count": guild.member_count,
        "ban_count": ban_count,
        "muted_count": muted_count,
        "warned_count": warned_count,
        "locked": int(guild_id) in bot.locked_guilds
    })

@api.route('/api/guilds/<guild_id>/config', methods=['GET'])
@require_api_key
def get_guild_config(guild_id):
    cfg = get_config(guild_id)
    cfg.pop("_id", None)
    cfg.pop("api_key", None)  # jamais renvoyée en clair
    return jsonify(cfg)

@api.route('/api/guilds/<guild_id>/config', methods=['POST'])
@require_api_key
def update_guild_config(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed_keys = {"logs_channel", "autorole", "allowed_roles", "command_roles"}
    updated = {}
    for key, value in data.items():
        if key in allowed_keys:
            update_config(guild_id, key, value)
            updated[key] = value

    if not updated:
        return jsonify({"error": "No valid fields provided"}), 400
    return jsonify({"success": True, "updated": updated})

@api.route('/api/guilds/<guild_id>/lock', methods=['POST'])
@require_api_key
def lock_bot_route(guild_id):
    bot.locked_guilds.add(int(guild_id))
    return jsonify({"success": True, "locked": True})

@api.route('/api/guilds/<guild_id>/unlock', methods=['POST'])
@require_api_key
def unlock_bot_route(guild_id):
    bot.locked_guilds.discard(int(guild_id))
    return jsonify({"success": True, "locked": False})

@api.route('/api/guilds/<guild_id>/broadcast', methods=['POST'])
@require_api_key
def broadcast_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    message = data.get("message")
    channel_id = data.get("channel_id")
    mention = data.get("mention", "none")

    if not message or not channel_id:
        return jsonify({"error": "message and channel_id are required"}), 400

    channel = guild.get_channel(int(channel_id))
    if not channel:
        return jsonify({"error": "Channel not found"}), 404

    if mention == "everyone":
        ping = "@everyone"
    elif mention == "here":
        ping = "@here"
    else:
        role = discord.utils.get(guild.roles, name=mention)
        ping = role.mention if role else None

    embed = discord.Embed(description=message, color=0x3399ff)
    embed.set_footer(text="📢 Sent from the mobile app")

    async def _send():
        await channel.send(content=ping, embed=embed)

    try:
        run_coroutine(_send())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/ban', methods=['POST'])
@require_api_key
def ban_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "Banned via mobile app")

    async def _ban():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        if member.top_role >= guild.me.top_role:
            return False, "Role too high"
        await member.ban(reason=reason)
        log_sanction(guild_id, user_id, "ban", reason, "mobile_app")
        await log_ban(guild, member, reason)
        return True, None

    try:
        success, error = run_coroutine(_ban())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/kick', methods=['POST'])
@require_api_key
def kick_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "Kicked via mobile app")

    async def _kick():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        if member.top_role >= guild.me.top_role:
            return False, "Role too high"
        await member.kick(reason=reason)
        log_sanction(guild_id, user_id, "kick", reason, "mobile_app")
        await log_kick(guild, member, reason)
        return True, None

    try:
        success, error = run_coroutine(_kick())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/warnings', methods=['GET'])
@require_api_key
def get_warnings_route(guild_id, user_id):
    count = get_warns(guild_id, user_id)
    return jsonify({"user_id": user_id, "warnings": count})

@api.route('/api/guilds/<guild_id>/warnlist', methods=['GET'])
@require_api_key
def warnlist_route(guild_id):
    docs = list(warns_col.find({"guild_id": str(guild_id), "count": {"$gt": 0}}))
    result = [{"user_id": d["user_id"], "count": d["count"]} for d in docs]
    return jsonify(result)

@api.route('/api/guilds/<guild_id>/channels', methods=['GET'])
@require_api_key
def list_channels_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
    return jsonify(channels)

@api.route('/api/guilds/<guild_id>/roles', methods=['GET'])
@require_api_key
def list_roles_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    roles = [{"id": str(r.id), "name": r.name} for r in guild.roles if not r.is_default()]
    return jsonify(roles)

@api.route('/api/guilds/<guild_id>/members', methods=['GET'])
@require_api_key
def list_members_route(guild_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    members = [
        {
            "id": str(m.id),
            "name": m.display_name,
            "username": str(m),
            "avatar_url": m.display_avatar.url,
            "is_muted": m.is_timed_out()
        }
        for m in guild.members if not m.bot
    ]
    return jsonify(members)

@api.route('/api/guilds/<guild_id>/members/<user_id>/mute', methods=['POST'])
@require_api_key
def mute_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    minutes = int(data.get("minutes", 10))
    reason = data.get("reason") or "Muted by mobile app"

    async def _mute():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        if member.top_role >= guild.me.top_role:
            return False, "Role too high"
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        log_sanction(guild_id, user_id, "mute", f"{reason} ({minutes} min)", "mobile_app")
        await log_mute(guild, member, minutes, reason)
        return True, None

    try:
        success, error = run_coroutine(_mute())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/unmute', methods=['POST'])
@require_api_key
def unmute_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    async def _unmute():
        member = guild.get_member(int(user_id))
        if not member:
            return False, "Member not found"
        await member.timeout(None)
        await log_unmute(guild, member)
        return True, None

    try:
        success, error = run_coroutine(_unmute())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})

@api.route('/api/guilds/<guild_id>/members/<user_id>/warn', methods=['POST'])
@require_api_key
def warn_member_route(guild_id, user_id):
    if not bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason") or "Warned by mobile app"

    member = guild.get_member(int(user_id))
    if not member:
        return jsonify({"error": "Member not found"}), 404

    count = get_warns(guild_id, user_id) + 1
    set_warns(guild_id, user_id, count)
    log_sanction(guild_id, user_id, "warn", reason, "mobile_app")

    try:
        run_coroutine(log_warn(guild, member, reason, count))
    except Exception:
        pass  # le warn est déjà enregistré, le log est secondaire

    return jsonify({"success": True, "warnings": count, "reason": reason})

@api.route('/api/guilds/<guild_id>/members/<user_id>/unwarn', methods=['POST'])
@require_api_key
def unwarn_member_route(guild_id, user_id):
    count = get_warns(guild_id, user_id)
    if count == 0:
        return jsonify({"error": "No warnings to remove"}), 400
    count -= 1
    set_warns(guild_id, user_id, count)

    if bot.is_ready():
        guild = bot.get_guild(int(guild_id))
        if guild:
            member = guild.get_member(int(user_id))
            if member:
                try:
                    run_coroutine(log_unwarn(guild, member, count))
                except Exception:
                    pass

    return jsonify({"success": True, "warnings": count})


# ============================================================
# ============ DASHBOARD WEB (OAuth2 Discord) ================
# ============================================================
# Dashboard admin-only : login via Discord, accès limité aux serveurs
# ou l'utilisateur a la permission Administrator ET ou le bot est présent.

api.secret_key = os.environ["FLASK_SECRET_KEY"]
api.config.update(
    SESSION_COOKIE_SECURE=True,     # cookie envoyé que en HTTPS
    SESSION_COOKIE_HTTPONLY=True,   # inaccessible en JS (anti XSS)
    SESSION_COOKIE_SAMESITE="Lax",  # anti CSRF de base
)

DASH_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DASH_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
DASH_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]

DISCORD_API_BASE = "https://discord.com/api"
ADMINISTRATOR_PERM = 0x8
GUILD_ID_RE = re.compile(r"^\d{17,19}$")  # valide un vrai snowflake Discord


def dash_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "access_token" not in session:
            return redirect(url_for("dash_login"))
        return f(*args, **kwargs)
    return wrapper


def get_user_admin_guilds():
    """Revient chercher la vérité chez Discord, avec un court cache pour éviter
    de spammer l'API à chaque clic (et donc de se reprendre un rate limit)."""
    cached = session.get("admin_guilds_cache")
    cached_at = session.get("admin_guilds_cache_at", 0)
    if cached is not None and (time.time() - cached_at) < 120:
        return cached

    r = requests.get(
        f"{DISCORD_API_BASE}/users/@me/guilds",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        timeout=10,
    )
    if r.status_code == 401:
        session.clear()
        return None
    if r.status_code != 200:
        # Discord a répondu autre chose qu'une liste de serveurs (rate limit,
        # erreur temporaire...) : on ne plante pas, on retombe sur le cache
        # précédent s'il existe, sinon on force un nouveau login.
        if cached is not None:
            return cached
        return None

    guilds = r.json()
    if not isinstance(guilds, list):
        return cached if cached is not None else None

    admin_guilds = [
        g_ for g_ in guilds
        if isinstance(g_, dict) and int(g_.get("permissions", 0)) & ADMINISTRATOR_PERM == ADMINISTRATOR_PERM
    ]
    session["admin_guilds_cache"] = admin_guilds
    session["admin_guilds_cache_at"] = time.time()
    return admin_guilds


def dash_guild_admin_required(f):
    """Vérifie que guild_id dans l'URL est un serveur où l'user est admin ET où le bot est présent."""
    @wraps(f)
    def wrapper(guild_id, *args, **kwargs):
        if not GUILD_ID_RE.match(guild_id):
            return jsonify({"error": "Invalid guild id"}), 400
        admin_guilds = get_user_admin_guilds()
        if admin_guilds is None:
            return redirect(url_for("dash_login"))
        admin_guild_ids = {g_["id"] for g_ in admin_guilds}
        if guild_id not in admin_guild_ids:
            return jsonify({"error": "Forbidden"}), 403
        if not bot.is_ready() or not bot.get_guild(int(guild_id)):
            return jsonify({"error": "Bot not present on this server"}), 403
        return f(guild_id, *args, **kwargs)
    return wrapper


@api.route("/login")
def dash_login():
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    params = {
        "client_id": DASH_CLIENT_ID,
        "redirect_uri": DASH_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(f"{DISCORD_API_BASE}/oauth2/authorize?{query}")


@api.route("/callback")
def dash_callback():
    state = request.args.get("state")
    if not state or state != session.pop("oauth_state", None):
        return jsonify({"error": "Invalid state"}), 403

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing code"}), 400

    token_r = requests.post(
        f"{DISCORD_API_BASE}/oauth2/token",
        data={
            "client_id": DASH_CLIENT_ID,
            "client_secret": DASH_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DASH_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if token_r.status_code != 200:
        return jsonify({"error": "Token exchange failed"}), 400

    token_data = token_r.json()
    session["access_token"] = token_data["access_token"]
    session["refresh_token"] = token_data["refresh_token"]

    user_r = requests.get(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {token_data['access_token']}"},
        timeout=10,
    )
    session["dash_user"] = user_r.json()

    return redirect(url_for("dash_home"))


@api.route("/logout")
def dash_logout():
    session.clear()
    return redirect(url_for("dash_login"))


# ---------- Templates HTML ----------

MODERATION_COMMANDS = [
    "ban", "kick", "mute", "unmute", "unban", "warn", "unwarn", "clear",
    "roleadd", "roleremove", "lock", "unlock", "vlock", "vunlock",
    "slowmode", "nickname", "softban", "purgeuser", "tempban", "broadcast",
]

BASE_STYLE = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

  :root {
    --ink: #0b0c12;
    --surface: #171a24;
    --surface-2: #1e2230;
    --surface-3: #262b3d;
    --line: #2a2f42;
    --text: #eceef5;
    --muted: #8b90a8;
    --raspberry: #ff5f8f;
    --raspberry-dim: #3a2230;
    --lime: #c3f24a;
    --lime-dim: #26301a;
    --amber: #ffb84d;
    --amber-dim: #3a2c14;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background:
      radial-gradient(900px 500px at 15% -10%, rgba(255,95,143,.08), transparent 60%),
      radial-gradient(700px 500px at 100% 0%, rgba(195,242,74,.06), transparent 55%),
      var(--ink);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
  }
  a { color: inherit; text-decoration: none; }
  h1, h2, h3 { font-family: 'Fraunces', serif; font-weight: 600; margin: 0; }
  code, .mono { font-family: 'JetBrains Mono', monospace; }

  @keyframes fadeUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
  @keyframes jiggle {
    0%, 100% { transform: translateY(-3px) rotate(-0.3deg); }
    50% { transform: translateY(-5px) rotate(0.4deg); }
  }
  @keyframes popIn { from { opacity: 0; transform: scale(.94); } to { opacity: 1; transform: scale(1); } }
  @keyframes pulseDot { 0%, 100% { box-shadow: 0 0 0 0 rgba(255,95,143,.5); } 50% { box-shadow: 0 0 0 6px rgba(255,95,143,0); } }
  @keyframes shimmer { 0% { background-position: -200px 0; } 100% { background-position: 200px 0; } }

  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 20px 32px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: rgba(11,12,18,.85); backdrop-filter: blur(10px);
    z-index: 10; animation: fadeUp .4s ease both;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-family: 'Fraunces', serif; font-size: 20px; font-weight: 700; }
  .brand .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--raspberry); animation: pulseDot 2.4s infinite; }
  .user-chip { display: flex; align-items: center; gap: 10px; font-size: 14px; color: var(--muted); }
  .user-chip img { width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--line); }
  .logout { color: var(--muted); font-size: 13px; border: 1px solid var(--line); padding: 6px 12px; border-radius: 8px; transition: all .15s; }
  .logout:hover { color: var(--text); border-color: var(--raspberry); }

  .wrap { max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
  .eyebrow { color: var(--raspberry); font-size: 12px; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 8px; font-weight: 600; animation: fadeUp .4s ease .05s both; }
  h1 { animation: fadeUp .45s ease .08s both; }
  .lead { color: var(--muted); font-size: 15px; margin-top: 10px; max-width: 56ch; animation: fadeUp .45s ease .12s both; }

  .flash { background: var(--lime-dim); border: 1px solid var(--lime); color: var(--lime); padding: 10px 16px; border-radius: 12px 4px 12px 4px; font-size: 14px; margin: 20px 0; animation: popIn .25s ease both; }
  .flash.error { background: var(--raspberry-dim); border-color: var(--raspberry); color: var(--raspberry); }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; margin-top: 28px; }
  .card {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 26px 8px 26px 8px;
    padding: 22px; transition: transform .18s ease, border-color .18s, box-shadow .18s;
    animation: fadeUp .4s ease both;
  }
  a.card:hover { animation: jiggle .5s ease; border-color: var(--raspberry); box-shadow: 0 10px 30px -12px rgba(255,95,143,.35); }
  .card-icon {
    width: 44px; height: 44px; border-radius: 14px 4px 14px 4px;
    background: var(--surface-2); display: flex; align-items: center; justify-content: center;
    font-family: 'Fraunces', serif; font-weight: 700; font-size: 18px; color: var(--raspberry);
    margin-bottom: 14px; transition: transform .2s;
  }
  a.card:hover .card-icon { transform: rotate(-6deg) scale(1.05); }
  .card h3 { font-size: 17px; }
  .card .sub { color: var(--muted); font-size: 12px; margin-top: 6px; }
  .owner-tag { display: inline-block; margin-top: 12px; font-size: 11px; color: var(--lime); background: var(--lime-dim); padding: 3px 9px; border-radius: 999px; }

  .empty { color: var(--muted); font-size: 14px; margin-top: 28px; padding: 24px; border: 1px dashed var(--line); border-radius: 16px; animation: fadeUp .4s ease both; }

  .status-strip { display: flex; gap: 10px; margin-top: 22px; flex-wrap: wrap; animation: fadeUp .4s ease .1s both; }
  .pill { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--muted); background: var(--surface); border: 1px solid var(--line); padding: 7px 13px; border-radius: 999px; }
  .pill .pip { width: 7px; height: 7px; border-radius: 50%; background: var(--lime); }
  .pill.locked .pip { background: var(--raspberry); }

  .panel {
    background: var(--surface); border: 1px solid var(--line); border-radius: 20px 6px 20px 6px;
    padding: 26px; margin-top: 20px; animation: fadeUp .4s ease both;
  }
  .panel.danger { border-color: rgba(255,95,143,.35); }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
  .panel h2 { font-size: 16px; }
  .panel .desc { color: var(--muted); font-size: 13px; margin: 6px 0 18px; }
  .badge { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; padding: 3px 8px; border-radius: 999px; font-weight: 600; }
  .badge.owner { color: var(--amber); background: var(--amber-dim); }
  .badge.admin { color: var(--muted); background: var(--surface-3); }

  label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
  select, input[type=text], textarea {
    width: 100%; background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
    padding: 10px 12px; border-radius: 10px; font-family: 'Inter', sans-serif; font-size: 14px;
    margin-bottom: 18px; transition: border-color .15s, box-shadow .15s;
  }
  textarea { resize: vertical; min-height: 110px; }
  select[multiple] { min-height: 110px; }
  select:focus, input:focus, textarea:focus { outline: none; border-color: var(--raspberry); box-shadow: 0 0 0 3px rgba(255,95,143,.12); }
  .hint { color: var(--muted); font-size: 11px; margin-top: -12px; margin-bottom: 18px; }
  .risky-warning { background: var(--amber-dim); border: 1px solid var(--amber); color: var(--amber); padding: 10px 14px; border-radius: 12px 4px 12px 4px; font-size: 12.5px; margin: -6px 0 18px; animation: popIn .2s ease both; }
  .cmd-toggle-row { display: flex; gap: 8px; margin-bottom: 10px; }
  button.ghost.small { padding: 5px 12px; font-size: 12px; border-radius: 8px; }
  .cmd-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; margin-bottom: 18px; background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px; padding: 12px; }
  .cmd-check { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); cursor: pointer; }
  .cmd-check input { width: auto; margin: 0; accent-color: var(--raspberry); }

  button, .btn {
    background: var(--raspberry); color: #1a0a10; border: none; font-weight: 600;
    padding: 11px 20px; border-radius: 12px 4px 12px 4px; font-size: 14px; cursor: pointer;
    transition: filter .15s, transform .1s; display: inline-flex; align-items: center; gap: 6px;
  }
  button:hover, .btn:hover { filter: brightness(1.1); }
  button:active, .btn:active { transform: scale(.97); }
  button.ghost, a.ghost.btn { background: transparent; border: 1px solid var(--line); color: var(--text); }
  button.ghost:hover, a.ghost.btn:hover { border-color: var(--raspberry); }
  button.warn { background: var(--amber); }
  button.stop { background: var(--surface-3); color: var(--raspberry); border: 1px solid rgba(255,95,143,.4); }

  .back { color: var(--muted); font-size: 13px; display: inline-block; margin-bottom: 18px; transition: color .15s; }
  .back:hover { color: var(--text); }

  .row { display: flex; gap: 14px; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 180px; }

  .chip-list { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .chip {
    display: flex; align-items: center; gap: 8px; background: var(--surface-2); border: 1px solid var(--line);
    padding: 6px 8px 6px 12px; border-radius: 999px; font-size: 12px; animation: popIn .2s ease both;
  }
  .chip form { margin: 0; }
  .chip button.x {
    background: var(--surface-3); color: var(--muted); border: none; width: 18px; height: 18px; border-radius: 50%;
    font-size: 11px; line-height: 1; padding: 0; display: flex; align-items: center; justify-content: center;
  }
  .chip button.x:hover { background: var(--raspberry-dim); color: var(--raspberry); }
  .no-perms { color: var(--muted); font-size: 13px; margin-bottom: 16px; }

  .key-box { display: flex; align-items: center; gap: 10px; background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin-bottom: 14px; }
  .key-box code { flex: 1; color: var(--muted); letter-spacing: .04em; }
  .key-box.revealed code { color: var(--lime); filter: none; }
  .key-box code.masked { filter: blur(5px); user-select: none; }
  .reveal-btn { background: var(--surface-3); border: none; color: var(--text); font-size: 11px; padding: 6px 10px; border-radius: 8px; cursor: pointer; }

  .divider { border: none; border-top: 1px solid var(--line); margin: 20px 0; }
  .lang-switch { display: flex; gap: 4px; margin-right: 4px; }
  .lang-switch a { font-size: 11px; color: var(--muted); border: 1px solid var(--line); padding: 4px 8px; border-radius: 7px; transition: all .15s; }
  .lang-switch a.active { color: var(--text); border-color: var(--raspberry); background: var(--surface-2); }
  .lang-switch a:hover { color: var(--text); }
  .audit-list { display: flex; flex-direction: column; gap: 10px; max-height: 360px; overflow-y: auto; }
  .audit-entry { background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; animation: popIn .2s ease both; }
  .audit-meta { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .audit-action { font-size: 13px; color: var(--text); }
  .onboarding-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 8px; }
  .onboarding-list li { font-size: 13px; color: var(--muted); background: var(--surface-2); border-radius: 8px; padding: 8px 12px; }
  .open-tickets-list { display: flex; flex-direction: column; gap: 8px; }
  .open-ticket-row { display: flex; justify-content: space-between; align-items: center; background: var(--surface-2); border-radius: 8px; padding: 8px 12px; font-size: 13px; }
  .open-ticket-row a { color: var(--raspberry); }
</style>
<script>
  function toggleKey(btn) {
    const box = btn.closest('.key-box');
    box.classList.toggle('revealed');
    const code = box.querySelector('code');
    code.classList.toggle('masked');
    btn.textContent = code.classList.contains('masked') ? btn.dataset.show : btn.dataset.hide;
  }
</script>
"""

# ---------- i18n ----------

TRANSLATIONS_DIR = os.path.dirname(os.path.abspath(__file__))

_FALLBACK_TRANSLATIONS = {
    "en": {"logout": "Log out", "dash_title": "Your servers"},
    "fr": {"logout": "Se d\u00e9connecter", "dash_title": "Tes serveurs"},
}


def _load_locale_file(filename):
    """Charge un fichier de langue .yml. En cas de souci (fichier manquant,
    YAML invalide...), on log l'erreur et on retombe sur un mini-dict de
    secours plut\u00f4t que de planter tout le dashboard."""
    path = os.path.join(TRANSLATIONS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        print(f"[I18N] Locale file not found: {path}", flush=True)
        return {}
    except yaml.YAMLError as e:
        print(f"[I18N] Failed to parse {path}: {e}", flush=True)
        return {}


TRANSLATIONS = {
    "en": _load_locale_file("en_us.yml") or _FALLBACK_TRANSLATIONS["en"],
    "fr": _load_locale_file("fr_fr.yml") or _FALLBACK_TRANSLATIONS["fr"],
}


def t(key):
    lang = session.get("lang", "en")
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


def current_lang():
    return session.get("lang", "en")


api.jinja_env.globals["t"] = t
api.jinja_env.globals["lang"] = current_lang


# ---------- Modèles de permissions par commande ----------
# Des packs de commandes courants, pour éviter de tout cocher à la main
# à chaque fois qu'on crée un nouveau rôle de modération.
COMMAND_TEMPLATES = {
    "trial_mod": {"label_key": "tmpl_trial_mod", "commands": ["warn", "unwarn", "mute", "unmute", "clear"]},
    "moderator": {"label_key": "tmpl_moderator", "commands": [
        "warn", "unwarn", "mute", "unmute", "kick", "ban", "unban",
        "clear", "roleadd", "roleremove", "slowmode", "softban", "purgeuser", "tempban",
    ]},
    "helper": {"label_key": "tmpl_helper", "commands": ["warn", "clear", "slowmode"]},
    "dj": {"label_key": "tmpl_dj", "commands": ["play", "skip", "pause", "resume", "stop", "queue", "volume"]},
}


# ---------- Templates HTML ----------

TOPBAR = """
<div class="topbar">
  <div class="brand"><span class="dot"></span> Nexus</div>
  <div class="user-chip">
    <div class="lang-switch">
      <a href="{{ url_for('dash_set_lang', lang_code='en', next=request.path) }}" class="{{ 'active' if lang()=='en' else '' }}">EN</a>
      <a href="{{ url_for('dash_set_lang', lang_code='fr', next=request.path) }}" class="{{ 'active' if lang()=='fr' else '' }}">FR</a>
    </div>
    {% if user and user.avatar %}
      <img src="https://cdn.discordapp.com/avatars/{{ user.id }}/{{ user.avatar }}.png" alt="">
    {% endif %}
    {{ user.global_name or user.username }}
    <a class="logout" href="{{ url_for('dash_logout') }}">{{ t('logout') }}</a>
  </div>
</div>
"""

DASH_LIST_TEMPLATE = BASE_STYLE + TOPBAR + """
<div class="wrap">
  <div class="eyebrow">{{ t('dash_eyebrow') }}</div>
  <h1>{{ t('dash_title') }}</h1>
  <p class="lead">{{ t('dash_lead') }}</p>

  {% for msg in get_flashed_messages() %}<div class="flash">{{ msg }}</div>{% endfor %}

  {% if guilds %}
  <div class="grid">
    {% for g in guilds %}
    <a class="card" style="animation-delay: {{ loop.index0 * 0.05 }}s" href="{{ url_for('dash_guild_page', guild_id=g.id) }}">
      <div class="card-icon">{{ g.name[0]|upper }}</div>
      <h3>{{ g.name }}</h3>
      <div class="sub mono">{{ g.id }}</div>
      {% if g.owner %}<span class="owner-tag">{{ t('owner_tag') }}</span>{% endif %}
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty">{{ t('empty_state') }}</div>
  {% endif %}
</div>
"""

GUILD_PAGE_TEMPLATE = BASE_STYLE + TOPBAR + """
<div class="wrap">
  <a class="back" href="{{ url_for('dash_home') }}">{{ t('back_all_servers')|safe }}</a>
  <div class="eyebrow">{{ t('config_eyebrow') }}</div>
  <h1>{{ guild.name }}</h1>
  <p class="lead mono">{{ guild.id }}</p>

  <div class="status-strip">
    <span class="pill {{ 'locked' if is_locked else '' }}"><span class="pip"></span> Bot {{ t('status_locked') if is_locked else t('status_active') }}</span>
    <span class="pill"><span class="pip"></span> {{ channels|length }} {{ t('status_text_channels') }}</span>
    <span class="pill"><span class="pip"></span> {{ roles|length }} {{ t('status_roles') }}</span>
    {% if is_owner %}<span class="pill"><span class="pip" style="background: var(--amber);"></span> {{ t('status_owner') }}</span>{% endif %}
  </div>

  <div class="row" style="margin-top:16px;">
    <a class="btn ghost" href="{{ url_for('dash_cases_page', guild_id=guild.id) }}">{{ t('nav_cases') }}</a>
    <a class="btn ghost" href="{{ url_for('dash_appeals_page', guild_id=guild.id) }}">{{ t('nav_appeals') }}</a>
  </div>

  {% for msg in get_flashed_messages() %}<div class="flash">{{ msg }}</div>{% endfor %}

  {% if onboarding_missing %}
  <div class="panel" style="animation-delay:.01s">
    <div class="panel-head"><h2>{{ t('panel_onboarding_title') }}</h2><span class="badge admin">{{ t('badge_admin') }}</span></div>
    <div class="desc">{{ t('panel_onboarding_desc') }}</div>
    <ul class="onboarding-list">
      {% for item in onboarding_missing %}
      <li>⚪ {{ item }}</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  <form method="POST" action="{{ url_for('dash_guild_page', guild_id=guild.id) }}">
    <div class="panel" style="animation-delay:.02s">
      <div class="panel-head"><h2>{{ t('panel_logs_title') }}</h2><span class="badge admin">{{ t('badge_admin') }}</span></div>
      <div class="desc">{{ t('panel_logs_desc') }}</div>
      <label for="logs_channel">{{ t('label_channel') }}</label>
      <select name="logs_channel" id="logs_channel">
        {% for c in channels %}
        <option value="{{ c.id }}" {% if c.name == cfg.logs_channel %}selected{% endif %}>#{{ c.name }}</option>
        {% endfor %}
      </select>

      <label for="autorole">{{ t('label_autorole') }}</label>
      <select name="autorole" id="autorole" onchange="checkRiskyRole(this)">
        <option value="none" data-risky="0" {% if not cfg.autorole %}selected{% endif %}>{{ t('option_none') }}</option>
        {% for r in roles %}
        <option value="{{ r.id }}" data-risky="{{ '1' if r.permissions.administrator or r.permissions.manage_guild or r.permissions.ban_members or r.permissions.kick_members or r.permissions.manage_roles else '0' }}" {% if cfg.autorole == r.id %}selected{% endif %}>{{ r.name }}</option>
        {% endfor %}
      </select>
      <div id="autorole-warning" class="risky-warning" style="display:none;">
        {{ t('risky_warning')|safe }}
      </div>

      <label class="cmd-check" style="margin-top:4px;">
        <input type="checkbox" name="log_dashboard_actions" {% if cfg.log_dashboard_actions %}checked{% endif %}>
        {{ t('label_log_dashboard') }}
      </label>
      <div style="height:14px;"></div>

      <button type="submit">{{ t('btn_save') }}</button>
    </div>
  </form>
  <script>
    function checkRiskyRole(select) {
      var opt = select.options[select.selectedIndex];
      var warn = document.getElementById('autorole-warning');
      warn.style.display = (opt.dataset.risky === '1') ? 'block' : 'none';
    }
    document.addEventListener('DOMContentLoaded', function() {
      checkRiskyRole(document.getElementById('autorole'));
    });
  </script>

  <form method="POST" action="{{ url_for('dash_automod', guild_id=guild.id) }}">
    <div class="panel" style="animation-delay:.06s">
      <div class="panel-head"><h2>{{ t('panel_automod_title') }}</h2><span class="badge admin">{{ t('badge_admin') }}</span></div>
      <div class="desc">{{ t('panel_automod_desc') }}</div>
      <label for="allowed_roles">{{ t('label_exempt_roles') }}</label>
      <select name="allowed_roles" id="allowed_roles" multiple>
        {% for r in roles %}
        <option value="{{ r.id }}" {% if r.id in (cfg.allowed_roles or []) %}selected{% endif %}>{{ r.name }}</option>
        {% endfor %}
      </select>
      <div class="hint">{{ t('hint_multiselect') }}</div>
      <button type="submit">{{ t('btn_save') }}</button>
    </div>
  </form>

  {% if is_owner %}
  <div class="panel" style="animation-delay:.1s">
    <div class="panel-head"><h2>{{ t('panel_perms_title') }}</h2><span class="badge owner">{{ t('badge_owner') }}</span></div>
    <div class="desc">{{ t('panel_perms_desc') }}</div>

    {% if cfg.command_roles %}
    {% for cmd, role_ids in cfg.command_roles.items() %}
      {% if role_ids %}
      <label>/{{ cmd }}</label>
      <div class="chip-list">
        {% for rid in role_ids %}
        <div class="chip">
          {{ role_names.get(rid, rid) }}
          <form method="POST" action="{{ url_for('dash_permission_remove', guild_id=guild.id) }}">
            <input type="hidden" name="command" value="{{ cmd }}">
            <input type="hidden" name="role_id" value="{{ rid }}">
            <button type="submit" class="x" title="{{ t('remove_title') }}">&times;</button>
          </form>
        </div>
        {% endfor %}
      </div>
      {% endif %}
    {% endfor %}
    {% else %}
    <div class="no-perms">{{ t('no_perms_yet') }}</div>
    {% endif %}

    <hr class="divider">
    <form method="POST" action="{{ url_for('dash_permission_add', guild_id=guild.id) }}">
      <label>{{ t('label_commands') }}</label>
      <div class="cmd-toggle-row">
        <button type="button" class="ghost small" onclick="toggleAllCmds(true)">{{ t('btn_select_all') }}</button>
        <button type="button" class="ghost small" onclick="toggleAllCmds(false)">{{ t('btn_clear') }}</button>
      </div>
      <div class="cmd-grid">
        {% for cmd in moderation_commands %}
        <label class="cmd-check">
          <input type="checkbox" name="commands" value="{{ cmd }}" class="cmd-checkbox">
          /{{ cmd }}
        </label>
        {% endfor %}
      </div>
      <label for="role_id">{{ t('label_allowed_role') }}</label>
      <select name="role_id" id="role_id">
        {% for r in roles %}
        <option value="{{ r.id }}">{{ r.name }}</option>
        {% endfor %}
      </select>
      <button type="submit">{{ t('btn_add_permissions') }}</button>
    </form>
    <script>
      function toggleAllCmds(state) {
        document.querySelectorAll('.cmd-checkbox').forEach(function(cb) { cb.checked = state; });
      }
    </script>

    <hr class="divider">
    <form method="POST" action="{{ url_for('dash_permission_template', guild_id=guild.id) }}">
      <label for="template_id">{{ t('tmpl_quick_label') }}</label>
      <div class="row">
        <div>
          <label for="template_id">{{ t('tmpl_select_label') }}</label>
          <select name="template_id" id="template_id">
            <optgroup label="{{ t('tmpl_builtin_group') }}">
              {% for tid, tpl in templates.items() %}
              <option value="builtin:{{ tid }}">{{ t(tpl.label_key) }} (/{{ tpl.commands|join(', /') }})</option>
              {% endfor %}
            </optgroup>
            {% if custom_templates %}
            <optgroup label="{{ t('tmpl_custom_group') }}">
              {% for name, cmds in custom_templates.items() %}
              {% if cmds %}
              <option value="custom:{{ name }}">{{ name }} (/{{ cmds|join(', /') }})</option>
              {% endif %}
              {% endfor %}
            </optgroup>
            {% endif %}
          </select>
        </div>
        <div>
          <label for="template_role_id">{{ t('tmpl_role_label') }}</label>
          <select name="role_id" id="template_role_id">
            {% for r in roles %}
            <option value="{{ r.id }}">{{ r.name }}</option>
            {% endfor %}
          </select>
        </div>
      </div>
      <button type="submit" class="ghost">{{ t('btn_apply_template') }}</button>
    </form>
  </div>

  <div class="panel" style="animation-delay:.14s">
    <div class="panel-head"><h2>{{ t('panel_apikey_title') }}</h2><span class="badge owner">{{ t('badge_owner') }}</span></div>
    <div class="desc">{{ t('panel_apikey_desc') }}</div>
    {% if cfg.api_key %}
    <div class="key-box">
      <code class="masked mono">{{ cfg.api_key }}</code>
      <button type="button" class="reveal-btn" data-show="{{ t('btn_show') }}" data-hide="{{ t('btn_hide') }}" onclick="toggleKey(this)">{{ t('btn_show') }}</button>
    </div>
    {% else %}
    <div class="no-perms">{{ t('no_key_yet') }}</div>
    {% endif %}
    <form method="POST" action="{{ url_for('dash_apikey_regen', guild_id=guild.id) }}" onsubmit="return confirm('{{ t('confirm_regen') }}');">
      <button type="submit" class="ghost">{{ t('btn_regenerate') }}</button>
    </form>
  </div>

  <div class="panel danger" style="animation-delay:.18s">
    <div class="panel-head"><h2>{{ t('panel_lockdown_title') }}</h2><span class="badge owner">{{ t('badge_owner') }}</span></div>
    <div class="desc">
      {% if is_locked %}{{ t('lockdown_desc_locked') }}
      {% else %}{{ t('lockdown_desc_unlocked') }}{% endif %}
    </div>
    <form method="POST" action="{{ url_for('dash_toggle_lock', guild_id=guild.id) }}">
      {% if is_locked %}
      <button type="submit" class="warn">{{ t('btn_unlock') }}</button>
      {% else %}
      <button type="submit" class="stop">{{ t('btn_lock') }}</button>
      {% endif %}
    </form>
  </div>

  <div class="panel" style="animation-delay:.21s">
    <div class="panel-head"><h2>{{ t('panel_tickets_title') }}</h2><span class="badge admin">{{ t('badge_admin') }}</span></div>
    <div class="desc">{{ t('panel_tickets_desc') }}</div>
    {% if open_tickets %}
    <div class="open-tickets-list">
      {% for ch in open_tickets %}
      <div class="open-ticket-row">
        <span>#{{ ch.name }}</span>
        <a href="https://discord.com/channels/{{ guild.id }}/{{ ch.id }}" target="_blank">{{ t('btn_open_link') }} →</a>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="no-perms">{{ t('no_open_tickets') }}</div>
    {% endif %}
  </div>

  <div class="panel" style="animation-delay:.22s">
    <div class="panel-head"><h2>{{ t('panel_audit_title') }}</h2><span class="badge owner">{{ t('badge_owner') }}</span></div>
    <div class="desc">{{ t('panel_audit_desc') }}</div>
    {% if audit_entries %}
    <div class="audit-list">
      {% for entry in audit_entries %}
      <div class="audit-entry">
        <div class="audit-meta"><strong>{{ entry.actor_name }}</strong> · {{ entry.timestamp.strftime('%Y-%m-%d %H:%M UTC') }}</div>
        <div class="audit-action">{{ entry.action }}{% if entry.details %} — {{ entry.details }}{% endif %}</div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="no-perms">{{ t('audit_empty') }}</div>
    {% endif %}
  </div>
  {% endif %}
</div>
"""


# ---------- Routes dashboard (HTML) ----------

@api.route("/dashboard/lang/<lang_code>")
def dash_set_lang(lang_code):
    if lang_code in TRANSLATIONS:
        session["lang"] = lang_code
    next_url = request.args.get("next") or url_for("dash_home")
    return redirect(next_url)


def dash_is_owner(guild):
    user = session.get("dash_user") or {}
    return str(guild.owner_id) == str(user.get("id"))


def dash_actor():
    """Identité de la personne connectée sur le dashboard, pour l'audit log
    et le log Discord optionnel des actions du dashboard."""
    user = session.get("dash_user") or {}
    name = user.get("global_name") or user.get("username") or "Unknown"
    return user.get("id"), name


def dash_log_action(guild, guild_id, action, details=""):
    """Enregistre une action dashboard dans l'audit log Mongo, et poste dans
    le salon Discord si l'option log_dashboard_actions est activée."""
    actor_id, actor_name = dash_actor()
    record_audit(guild_id, actor_id, actor_name, action, details)
    coro = log_dashboard_action(guild, actor_name, action, details)
    if coro is not None:
        try:
            run_coroutine(coro)
        except Exception as e:
            print(f"[DASHLOG] Failed to send Discord log: {e}", flush=True)


def dash_owner_required(f):
    @wraps(f)
    def wrapper(guild_id, *args, **kwargs):
        guild = bot.get_guild(int(guild_id))
        if guild is None or not dash_is_owner(guild):
            return jsonify({"error": "Server owner required"}), 403
        return f(guild_id, *args, **kwargs)
    return wrapper


@api.route("/dashboard")
@dash_login_required
def dash_home():
    admin_guilds = get_user_admin_guilds()
    if admin_guilds is None:
        return redirect(url_for("dash_login"))
    # ne montre que les serveurs où l'utilisateur est admin ET où le bot est présent
    bot_guild_ids = {str(g_.id) for g_ in bot.guilds} if bot.is_ready() else set()
    manageable = [g_ for g_ in admin_guilds if g_["id"] in bot_guild_ids]
    return render_template_string(
        DASH_LIST_TEMPLATE, guilds=manageable, user=session.get("dash_user")
    )


@api.route("/dashboard/<guild_id>", methods=["GET", "POST"])
@dash_login_required
@dash_guild_admin_required
def dash_guild_page(guild_id):
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return jsonify({"error": "Bot not present on this server"}), 403

    if request.method == "POST":
        # Toute valeur reçue est revalidée contre les objets réels de CE serveur
        # (jamais de confiance sur un id envoyé par le formulaire).
        changes = []
        logs_channel_id = request.form.get("logs_channel", "")
        if logs_channel_id.isdigit():
            channel = guild.get_channel(int(logs_channel_id))
            if channel is not None and channel in guild.text_channels:
                update_config(guild_id, "logs_channel", channel.name)
                changes.append(f"logs channel → #{channel.name}")

        autorole_id = request.form.get("autorole", "none")
        risky_perms = ("administrator", "manage_guild", "ban_members", "kick_members", "manage_roles")
        if autorole_id == "none":
            update_config(guild_id, "autorole", None)
            changes.append("autorole → none")
        elif autorole_id.isdigit():
            role = guild.get_role(int(autorole_id))
            if role is not None:
                update_config(guild_id, "autorole", role.id)
                changes.append(f"autorole → @{role.name}")
                if any(getattr(role.permissions, p) for p in risky_perms):
                    flash(f"⚠️ {role.name}: " + ("sensitive permissions, given to every new member." if current_lang() == "en" else "permissions sensibles, donné à tout nouveau membre."))

        log_toggle = request.form.get("log_dashboard_actions") == "on"
        if log_toggle != bool(get_config(guild_id).get("log_dashboard_actions")):
            update_config(guild_id, "log_dashboard_actions", log_toggle)
            changes.append(f"Discord logging of dashboard actions → {'on' if log_toggle else 'off'}")

        if changes:
            dash_log_action(guild, guild_id, "Updated general config", "; ".join(changes))

        flash("Configuration updated." if current_lang() == "en" else "Configuration mise à jour.")
        return redirect(url_for("dash_guild_page", guild_id=guild_id))

    cfg = get_config(guild_id)
    # Si l'utilisateur n'a pas déjà choisi une langue pour sa session, on
    # applique la langue par défaut configurée pour CE serveur via
    # /config language (sans écraser un choix explicite déjà fait).
    if "lang" not in session and cfg.get("language") in TRANSLATIONS:
        session["lang"] = cfg["language"]

    is_owner = dash_is_owner(guild)
    role_names = {r.id: r.name for r in guild.roles}

    # Checklist d'onboarding : ce qui n'est pas encore configuré.
    onboarding_missing = []
    if not discord.utils.get(guild.text_channels, name=cfg.get("logs_channel", "logs")):
        onboarding_missing.append(t("onboard_logs"))
    if not cfg.get("autorole"):
        onboarding_missing.append(t("onboard_autorole"))
    if not cfg.get("tickets_enabled"):
        onboarding_missing.append(t("onboard_tickets"))
    if not cfg.get("appeal_channel_id"):
        onboarding_missing.append(t("onboard_appeals"))
    if not cfg.get("antinuke_enabled"):
        onboarding_missing.append(t("onboard_antinuke"))

    # Tickets actuellement ouverts (dans la catégorie ticket, hors archive).
    open_tickets = []
    ticket_category_id = cfg.get("ticket_category_id")
    if ticket_category_id:
        category = guild.get_channel(int(ticket_category_id))
        if isinstance(category, discord.CategoryChannel):
            open_tickets = list(category.text_channels)

    return render_template_string(
        GUILD_PAGE_TEMPLATE,
        guild=guild,
        cfg=cfg,
        channels=guild.text_channels,
        roles=[r for r in guild.roles if not r.is_default()],
        role_names=role_names,
        moderation_commands=MODERATION_COMMANDS,
        templates=COMMAND_TEMPLATES,
        custom_templates=cfg.get("command_templates", {}),
        audit_entries=get_audit_log(guild_id) if is_owner else [],
        onboarding_missing=onboarding_missing,
        open_tickets=open_tickets,
        is_owner=is_owner,
        is_locked=guild.id in bot.locked_guilds,
        user=session.get("dash_user"),
    )


@api.route("/dashboard/<guild_id>/automod", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_automod(guild_id):
    guild = bot.get_guild(int(guild_id))
    submitted = request.form.getlist("allowed_roles")
    valid_role_ids = {r.id for r in guild.roles}
    # ne garde que des ids qui correspondent à de vrais rôles de CE serveur
    clean = [int(rid) for rid in submitted if rid.isdigit() and int(rid) in valid_role_ids]
    update_config(guild_id, "allowed_roles", clean)
    role_names = ", ".join(f"@{r.name}" for r in guild.roles if r.id in clean) or "none"
    dash_log_action(guild, guild_id, "Updated anti-raid exemptions", f"exempt roles: {role_names}")
    flash("Anti-raid exemptions updated." if current_lang() == "en" else "Exemptions anti-raid mises à jour.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/permissions/add", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_add(guild_id):
    guild = bot.get_guild(int(guild_id))
    commands = request.form.getlist("commands")
    role_id = request.form.get("role_id", "")
    valid_commands = [c for c in commands if c in MODERATION_COMMANDS]
    if valid_commands and role_id.isdigit():
        role = guild.get_role(int(role_id))
        if role is not None:
            for cmd in valid_commands:
                add_command_role(guild_id, cmd, role.id)
            cmd_list = ", ".join(f"/{c}" for c in valid_commands)
            dash_log_action(guild, guild_id, "Added command permissions", f"@{role.name}: {cmd_list}")
            if current_lang() == "en":
                flash(f"{role.name} can now use {len(valid_commands)} command(s): {cmd_list}.")
            else:
                flash(f"{role.name} peut maintenant utiliser {len(valid_commands)} commande(s) : {cmd_list}.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/permissions/template", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_template(guild_id):
    guild = bot.get_guild(int(guild_id))
    template_id = request.form.get("template_id", "")
    role_id = request.form.get("role_id", "")
    if not role_id.isdigit():
        return redirect(url_for("dash_guild_page", guild_id=guild_id))
    role = guild.get_role(int(role_id))
    if role is None:
        return redirect(url_for("dash_guild_page", guild_id=guild_id))

    source, _, key = template_id.partition(":")
    commands_list = None
    label = None

    if source == "builtin" and key in COMMAND_TEMPLATES:
        tpl = COMMAND_TEMPLATES[key]
        commands_list = tpl["commands"]
        label = t(tpl["label_key"])
    elif source == "custom":
        cfg = get_config(guild_id)
        custom_templates = cfg.get("command_templates", {})
        if key in custom_templates:
            commands_list = custom_templates[key]
            label = key

    if commands_list:
        for cmd in commands_list:
            add_command_role(guild_id, cmd, role.id)
        dash_log_action(guild, guild_id, "Applied permission template", f"template « {label} » → @{role.name}")
        if current_lang() == "en":
            flash(f"Template « {label} » applied to {role.name} ({len(commands_list)} commands).")
        else:
            flash(f"Modèle « {label} » appliqué à {role.name} ({len(commands_list)} commandes).")

    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/permissions/remove", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_remove(guild_id):
    guild = bot.get_guild(int(guild_id))
    command = request.form.get("command", "")
    role_id = request.form.get("role_id", "")
    # Pas de whitelist ici : la commande vient d'un chip déjà affiché depuis
    # la DB (donc déjà existante), et retirer une permission ne présente
    # aucun risque même pour un nom de commande hors de MODERATION_COMMANDS
    # (ex: ajouté via /config allow avec une casse différente, ou une
    # commande hors de notre liste curatée).
    if command and role_id.isdigit():
        remove_command_role(guild_id, command, int(role_id))
        role = guild.get_role(int(role_id)) if guild else None
        dash_log_action(guild, guild_id, "Removed command permission", f"/{command} for {'@' + role.name if role else role_id}")
        flash(f"Permission removed for /{command}." if current_lang() == "en" else f"Permission retirée pour /{command}.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/apikey/regenerate", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_apikey_regen(guild_id):
    guild = bot.get_guild(int(guild_id))
    new_key = secrets.token_hex(16)
    update_config(guild_id, "api_key", new_key)
    dash_log_action(guild, guild_id, "Regenerated mobile API key")
    flash("New API key generated — the old one no longer works." if current_lang() == "en" else "Nouvelle clé API générée — l'ancienne ne fonctionne plus.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/lock", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_toggle_lock(guild_id):
    guild = bot.get_guild(int(guild_id))
    gid = int(guild_id)
    if gid in bot.locked_guilds:
        bot.locked_guilds.discard(gid)
        dash_log_action(guild, guild_id, "Unlocked the bot")
        flash("Bot unlocked." if current_lang() == "en" else "Bot déverrouillé.")
    else:
        bot.locked_guilds.add(gid)
        dash_log_action(guild, guild_id, "Locked the bot")
        flash("Bot locked — only you can use its commands here." if current_lang() == "en" else "Bot verrouillé — seul toi peux utiliser ses commandes ici.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


# ============================================================
# =============== KILL-SWITCH — confirmation web ==============
# ============================================================
# Ces routes sont volontairement SANS login dashboard/OAuth2 : la
# sécurité vient de deux facteurs indépendants — le token à usage
# unique reçu par email, et le code TOTP de l'app d'authentification.
# Le GET n'a AUCUN effet de bord (pour ne pas se faire déclencher par
# les scanners anti-spam qui pré-visitent les liens des emails) ;
# seul le POST, avec un code correct, change l'état du bot.

KILLSWITCH_CONFIRM_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:420px; margin:80px auto;">
  <div class="eyebrow">Nexus — Owner Control</div>
  <h1>{{ 'Unlock the bot?' if locked else 'Lock the bot down?' }}</h1>
  <p class="lead">The bot is currently {{ 'LOCKED across every server' if locked else 'active normally' }}. Enter your authenticator code to {{ 'unlock it' if locked else 'lock it down everywhere' }}.</p>
  {% if error %}<div class="flash error">❌ Incorrect code, or the link expired. {{ attempts_left }} attempt(s) left.</div>{% endif %}
  <form method="POST">
    <label for="code">6-digit authenticator code</label>
    <input type="text" name="code" id="code" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="one-time-code" autofocus>
    <button type="submit" class="{{ 'warn' if locked else 'stop' }}">{{ 'Confirm unlock' if locked else 'Confirm lockdown' }}</button>
  </form>
</div>
"""

KILLSWITCH_DONE_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:420px; margin:80px auto;">
  <div class="eyebrow">Nexus — Owner Control</div>
  <h1>{{ '🔒 Bot locked down' if locked else '🔓 Bot unlocked' }}</h1>
  <p class="lead">{{ 'All commands are now restricted to you across every server the bot is in.' if locked else 'The bot is back to normal across every server.' }}</p>
</div>
"""

KILLSWITCH_INVALID_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:420px; margin:80px auto;">
  <div class="eyebrow">Nexus — Owner Control</div>
  <h1>Link expired or invalid</h1>
  <p class="lead">This link is invalid, already used, expired, or had too many wrong codes entered. Use <code>/admin killswitch resend</code> in Discord to get a fresh one, or wait for tomorrow's email.</p>
</div>
"""


def _killswitch_token_doc(token):
    doc = killswitch_tokens_col.find_one({"token": token})
    if not doc:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = doc["expires_at"]
    if expires_at.tzinfo is None:
        # pymongo relit les dates sans fuseau (naive) même si on les a
        # stockées "aware" : on remet UTC explicitement avant de comparer,
        # sinon TypeError (naive vs aware) et 500 sur la page de confirmation.
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
    if doc.get("used") or expires_at < now or doc.get("attempts", 0) >= 5:
        return None
    return doc


def get_client_ip():
    """Render est derrière un proxy : la vraie IP du visiteur est dans
    X-Forwarded-For, pas request.remote_addr (qui donnerait l'IP du proxy)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


async def notify_owner_bruteforce(ip):
    owner = await get_owner_user()
    if owner is None:
        return
    msg = (
        f"🚨 **Possible intrusion attempt**: 5 incorrect codes were entered on a kill-switch "
        f"confirmation link (from IP `{ip}`). That link is now dead.\n\n"
        f"If this wasn't you, consider rotating your TOTP secret (`generate_totp_secret.py`) "
        f"and updating `TOTP_SECRET` on Render."
    )
    try:
        await owner.send(msg)
    except discord.HTTPException:
        pass


@api.route("/admin/kill-switch/<token>", methods=["GET"])
def killswitch_confirm_page(token):
    ip = get_client_ip()
    if _killswitch_token_doc(token) is None:
        record_audit("global", None, f"Unknown (IP {ip})", "Viewed an invalid/expired kill-switch link")
        return render_template_string(KILLSWITCH_INVALID_TEMPLATE), 410
    record_audit("global", None, f"Unknown (IP {ip})", "Opened the kill-switch confirmation page")
    return render_template_string(KILLSWITCH_CONFIRM_TEMPLATE, locked=get_global_lock(), error=False)


@api.route("/admin/kill-switch/<token>", methods=["POST"])
def killswitch_submit(token):
    ip = get_client_ip()
    doc = _killswitch_token_doc(token)
    if doc is None:
        return render_template_string(KILLSWITCH_INVALID_TEMPLATE), 410

    code = request.form.get("code", "").strip()
    totp_secret = os.getenv("TOTP_SECRET")
    valid = bool(totp_secret) and bool(code) and pyotp.TOTP(totp_secret).verify(code, valid_window=1)

    if not valid:
        new_attempts = doc.get("attempts", 0) + 1
        killswitch_tokens_col.update_one({"token": token}, {"$inc": {"attempts": 1}})
        attempts_left = max(0, 5 - new_attempts)
        record_audit("global", None, f"Unknown (IP {ip})", "Failed kill-switch code attempt", f"{new_attempts}/5 attempts used")
        if new_attempts >= 5:
            try:
                run_coroutine(notify_owner_bruteforce(ip))
            except Exception as e:
                print(f"[KILLSWITCH] Failed to DM owner about brute-force: {e}", flush=True)
        return render_template_string(
            KILLSWITCH_CONFIRM_TEMPLATE, locked=get_global_lock(), error=True, attempts_left=attempts_left
        )

    # Code correct : le token est consommé immédiatement (usage unique),
    # avant même de faire quoi que ce soit d'autre.
    killswitch_tokens_col.update_one({"token": token}, {"$set": {"used": True}})
    new_state = not get_global_lock()
    set_global_lock(new_state)
    record_audit("global", None, f"Owner (email + authenticator, IP {ip})", "Locked bot globally" if new_state else "Unlocked bot globally")
    try:
        run_coroutine(notify_owner_killswitch(new_state))
    except Exception as e:
        print(f"[KILLSWITCH] Failed to DM owner: {e}", flush=True)

    return render_template_string(KILLSWITCH_DONE_TEMPLATE, locked=new_state)


# ============================================================
# =================== BAN APPEALS — public form ===============
# ============================================================
# Formulaire public (pas de login) : la sécurité vient du token à usage
# unique envoyé uniquement dans le DM de ban, pas d'un compte Discord.

APPEAL_FORM_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:480px; margin:60px auto;">
  <div class="eyebrow">Ban Appeal</div>
  <h1>{{ guild_name }}</h1>
  <p class="lead">You were banned from this server. Case #{{ case_id }}.<br>Reason given: {{ ban_reason }}</p>
  <form method="POST">
    <label for="appeal_text">Why should this ban be reconsidered?</label>
    <textarea name="appeal_text" id="appeal_text" maxlength="1000" required placeholder="Explain your side..."></textarea>
    <button type="submit">Submit appeal</button>
  </form>
</div>
"""

APPEAL_DONE_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:480px; margin:60px auto;">
  <div class="eyebrow">Ban Appeal</div>
  <h1>Appeal submitted</h1>
  <p class="lead">Your appeal for case #{{ case_id }} has been sent to the server's staff team. You'll get a DM once it's reviewed.</p>
</div>
"""

APPEAL_STATUS_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:480px; margin:60px auto;">
  <div class="eyebrow">Ban Appeal</div>
  <h1>{{ title }}</h1>
  <p class="lead">{{ message }}</p>
</div>
"""

APPEAL_INVALID_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:480px; margin:60px auto;">
  <div class="eyebrow">Ban Appeal</div>
  <h1>Link not found</h1>
  <p class="lead">This appeal link is invalid.</p>
</div>
"""

APPEAL_STATUS_COPY = {
    "submitted": ("Already submitted", "Your appeal has already been submitted and is awaiting review."),
    "accepted": ("Appeal accepted", "Your appeal was accepted — you should already be unbanned."),
    "denied": ("Appeal denied", "Your appeal was reviewed and denied."),
}


@api.route("/appeal/<token>", methods=["GET"])
def appeal_form_page(token):
    appeal = get_ban_appeal(token)
    if not appeal:
        return render_template_string(APPEAL_INVALID_TEMPLATE), 404
    if appeal["status"] != "pending":
        title, message = APPEAL_STATUS_COPY.get(appeal["status"], ("Status", "This appeal has already been processed."))
        return render_template_string(APPEAL_STATUS_TEMPLATE, title=title, message=message)
    guild = bot.get_guild(int(appeal["guild_id"]))
    return render_template_string(
        APPEAL_FORM_TEMPLATE,
        guild_name=guild.name if guild else "the server",
        case_id=appeal["case_id"],
        ban_reason=appeal["ban_reason"] or "No reason provided",
    )


@api.route("/appeal/<token>", methods=["POST"])
def appeal_form_submit(token):
    appeal = get_ban_appeal(token)
    if not appeal or appeal["status"] != "pending":
        return render_template_string(APPEAL_INVALID_TEMPLATE), 404

    appeal_text = request.form.get("appeal_text", "").strip()[:1000]
    guild = bot.get_guild(int(appeal["guild_id"]))
    if not appeal_text:
        return render_template_string(
            APPEAL_FORM_TEMPLATE,
            guild_name=guild.name if guild else "the server",
            case_id=appeal["case_id"],
            ban_reason=appeal["ban_reason"] or "No reason provided",
        )

    ban_appeals_col.update_one(
        {"token": token},
        {"$set": {
            "status": "submitted",
            "appeal_text": appeal_text,
            "submitted_at": datetime.datetime.now(datetime.timezone.utc),
        }},
    )

    if guild:
        try:
            run_coroutine(post_appeal_for_review(guild, appeal, appeal_text))
        except Exception as e:
            print(f"[APPEAL] Failed to post appeal for review: {e}", flush=True)

    return render_template_string(APPEAL_DONE_TEMPLATE, case_id=appeal["case_id"])


# ============================================================
# =================== PUBLIC STATUS PAGE =====================
# ============================================================

STATUS_PAGE_TEMPLATE = BASE_STYLE + """
<div class="wrap" style="max-width:480px; margin:60px auto;">
  <div class="eyebrow">Nexus</div>
  <h1>{{ '🟢 All systems operational' if online else '🔴 Bot is offline' }}</h1>
  {% if online %}
  <div class="status-strip" style="margin-top:20px;">
    <span class="pill"><span class="pip"></span> {{ guild_count }} servers</span>
    <span class="pill"><span class="pip"></span> {{ latency_ms }}ms latency</span>
    <span class="pill"><span class="pip"></span> Up {{ uptime }}</span>
  </div>
  {% else %}
  <p class="lead">The bot's Discord connection isn't ready yet. If this persists, check the hosting dashboard.</p>
  {% endif %}
</div>
"""

_format_uptime = utils.format_uptime


@api.route("/status")
def public_status_page():
    online = bot.is_ready()
    if not online:
        return render_template_string(STATUS_PAGE_TEMPLATE, online=False), 503
    uptime = _format_uptime(time.time() - bot.start_time) if hasattr(bot, "start_time") else "unknown"
    return render_template_string(
        STATUS_PAGE_TEMPLATE,
        online=True,
        guild_count=len(bot.guilds),
        latency_ms=round(bot.latency * 1000) if bot.latency == bot.latency else "—",  # NaN check avant le premier heartbeat
        uptime=uptime,
    )


# ============================================================
# =============== CASES BROWSER (dashboard) ===================
# ============================================================

CASES_TEMPLATE = BASE_STYLE + TOPBAR + """
<div class="wrap">
  <a class="back" href="{{ url_for('dash_guild_page', guild_id=guild.id) }}">&larr; {{ guild.name }}</a>
  <div class="eyebrow">{{ t('config_eyebrow') }}</div>
  <h1>{{ t('cases_title') }}</h1>

  <form method="GET" class="row" style="margin-top:20px;">
    <div>
      <label for="type">{{ t('cases_filter_type') }}</label>
      <select name="type" id="type" onchange="this.form.submit()">
        <option value="">{{ t('option_none') }}</option>
        {% for tp in all_types %}
        <option value="{{ tp }}" {% if tp == filter_type %}selected{% endif %}>{{ tp.replace('_',' ')|title }}</option>
        {% endfor %}
      </select>
    </div>
  </form>

  {% if cases %}
  <div class="audit-list" style="max-height:none;">
    {% for c in cases %}
    <div class="audit-entry">
      <div class="audit-meta">#{{ c.case_id }} · {{ c.type.replace('_',' ')|title }} · {{ c.timestamp.strftime('%Y-%m-%d %H:%M UTC') }}</div>
      <div class="audit-action">User: {{ c.user_id }} — {{ c.reason }}</div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="no-perms">{{ t('cases_empty') }}</div>
  {% endif %}
</div>
"""


@api.route("/dashboard/<guild_id>/cases", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
def dash_cases_page(guild_id):
    guild = bot.get_guild(int(guild_id))
    filter_type = request.args.get("type", "")
    query = {"guild_id": str(guild_id)}
    if filter_type:
        query["type"] = filter_type
    cases = list(sanctions_col.find(query).sort("timestamp", -1).limit(200))
    all_types = sanctions_col.distinct("type", {"guild_id": str(guild_id)})
    return render_template_string(
        CASES_TEMPLATE, guild=guild, cases=cases, all_types=all_types, filter_type=filter_type,
        user=session.get("dash_user"),
    )


# ============================================================
# ============== APPEALS REVIEW (dashboard) ====================
# ============================================================

APPEALS_REVIEW_TEMPLATE = BASE_STYLE + TOPBAR + """
<div class="wrap">
  <a class="back" href="{{ url_for('dash_guild_page', guild_id=guild.id) }}">&larr; {{ guild.name }}</a>
  <div class="eyebrow">{{ t('config_eyebrow') }}</div>
  <h1>{{ t('appeals_title') }}</h1>

  {% for msg in get_flashed_messages() %}<div class="flash">{{ msg }}</div>{% endfor %}

  {% if appeals %}
  {% for a in appeals %}
  <div class="panel">
    <div class="panel-head"><h2>Case #{{ a.case_id }} — {{ a.sanction_type|default('ban') }}</h2></div>
    <div class="desc">{{ a.user_name }} ({{ a.user_id }})</div>
    <p class="lead" style="margin-top:10px;"><strong>{{ t('appeals_original_reason') }}:</strong> {{ a.ban_reason }}</p>
    <p class="lead"><strong>{{ t('appeals_appeal_text') }}:</strong> {{ a.appeal_text }}</p>
    <div class="row" style="margin-top:16px;">
      <form method="POST" action="{{ url_for('dash_appeal_accept', guild_id=guild.id, token=a.token) }}">
        <button type="submit">{{ t('btn_accept') }}</button>
      </form>
      <form method="POST" action="{{ url_for('dash_appeal_deny', guild_id=guild.id, token=a.token) }}">
        <button type="submit" class="stop">{{ t('btn_deny') }}</button>
      </form>
    </div>
  </div>
  {% endfor %}
  {% else %}
  <div class="no-perms">{{ t('appeals_empty') }}</div>
  {% endif %}
</div>
"""


@api.route("/dashboard/<guild_id>/appeals", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
def dash_appeals_page(guild_id):
    guild = bot.get_guild(int(guild_id))
    appeals = list(ban_appeals_col.find({"guild_id": str(guild_id), "status": "submitted"}).sort("submitted_at", -1))
    return render_template_string(APPEALS_REVIEW_TEMPLATE, guild=guild, appeals=appeals, user=session.get("dash_user"))


@api.route("/dashboard/<guild_id>/appeals/<token>/accept", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_appeal_accept(guild_id, token):
    guild = bot.get_guild(int(guild_id))
    appeal = get_ban_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))

    sanction_type = appeal.get("sanction_type", "ban")
    result = run_coroutine(reverse_sanction(guild, sanction_type, appeal["user_id"]))
    ban_appeals_col.update_one({"token": token}, {"$set": {"status": "accepted", "resolved_at": datetime.datetime.now(datetime.timezone.utc)}})
    actor_id, actor_name = dash_actor()
    record_audit(guild_id, actor_id, actor_name, "Accepted appeal (via dashboard)", f"Case #{appeal['case_id']} ({sanction_type}) — {result}")
    try:
        run_coroutine(_dm_user_appeal_result(int(appeal["user_id"]), guild.name, appeal["case_id"], True, result))
    except Exception as e:
        print(f"[APPEAL] Failed to DM user: {e}", flush=True)
    flash(f"Case #{appeal['case_id']} accepted — {result}.")
    return redirect(url_for("dash_appeals_page", guild_id=guild_id))


@api.route("/dashboard/<guild_id>/appeals/<token>/deny", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_appeal_deny(guild_id, token):
    guild = bot.get_guild(int(guild_id))
    appeal = get_ban_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))

    ban_appeals_col.update_one({"token": token}, {"$set": {"status": "denied", "resolved_at": datetime.datetime.now(datetime.timezone.utc)}})
    actor_id, actor_name = dash_actor()
    record_audit(guild_id, actor_id, actor_name, "Denied appeal (via dashboard)", f"Case #{appeal['case_id']}")
    try:
        run_coroutine(_dm_user_appeal_result(int(appeal["user_id"]), guild.name, appeal["case_id"], False, None))
    except Exception as e:
        print(f"[APPEAL] Failed to DM user: {e}", flush=True)
    flash(f"Case #{appeal['case_id']} denied.")
    return redirect(url_for("dash_appeals_page", guild_id=guild_id))


async def _dm_user_appeal_result(user_id, guild_name, case_id, accepted, result):
    user = await bot.fetch_user(user_id)
    if accepted:
        await user.send(f"✅ Your appeal for **{guild_name}** (case #{case_id}) was accepted — {result}.")
    else:
        await user.send(f"❌ Your appeal for **{guild_name}** (case #{case_id}) was reviewed and denied.")


def run_api():
    port = int(os.getenv("PORT", 8080))
    api.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_api)
    t.daemon = True
    t.start()

keep_alive()

# Arrêt propre : Render envoie SIGTERM avant de tuer le process lors d'un
# redéploiement. Par défaut, SIGTERM termine Python immédiatement sans
# nettoyage -- on le transforme en KeyboardInterrupt (comme Ctrl+C), que
# discord.py sait déjà gérer proprement (fermeture de la connexion websocket
# avant de quitter, plutôt qu'une coupure brutale en plein milieu).
def _handle_sigterm(signum, frame):
    print("[SHUTDOWN] Received SIGTERM, shutting down gracefully...", flush=True)
    raise KeyboardInterrupt()

signal.signal(signal.SIGTERM, _handle_sigterm)

# Si Discord/Cloudflare renvoie un 429 au login (rate limit), on ne laisse pas
# le process planter : Render le relancerait instantanément, ce qui martèle
# encore plus l'endpoint de login et prolonge le blocage. On attend avec un
# backoff progressif à la place.
_login_attempt = 0
while True:
    try:
        bot.run(os.getenv("TOKEN"))
        break  # bot.run() ne revient normalement qu'à l'arrêt volontaire
    except discord.errors.HTTPException as e:
        if e.status == 429:
            _login_attempt += 1
            wait = min(60 * (2 ** (_login_attempt - 1)), 900)  # 60s, 120s, 240s... max 15 min
            print(f"[LOGIN] 429 rate limited par Discord/Cloudflare, retry dans {wait}s (tentative {_login_attempt})", flush=True)
            time.sleep(wait)
        else:
            raise
    except KeyboardInterrupt:
        break

if mongo:
    try:
        mongo.close()
        print("[SHUTDOWN] MongoDB connection closed.", flush=True)
    except Exception:
        pass
