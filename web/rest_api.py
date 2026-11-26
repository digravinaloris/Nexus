"""web.rest_api -- REST API (/api/*) utilisée par l'app mobile Nexus.

Extrait de main.py (étape 2b-1) : mêmes routes, mêmes chemins, même
comportement. Les routes sont portées par un Blueprint que main.py enregistre
sur l'app Flask. Ce module n'importe JAMAIS main.py (sous `python main.py` le
module s'appelle `__main__`, l'importer le rechargerait avec un 2e bot) : le
bot vient de `core.bot`, la base de `core.col`.
"""
import datetime
import os
import secrets
from functools import wraps

import discord
from flask import Blueprint, g, jsonify, request

import core
from audit import get_audit_log, record_audit
from bridge import run_coroutine
from config_store import get_config, update_config
from core import col
from web.bot_service import get_bot_service
from discord_logging import log_ban, log_kick, log_mute, log_unmute, log_unwarn, log_warn
from features import FEATURE_TOGGLES
from moderation_actions import _dm_user_appeal_result, reverse_sanction
from sanctions_store import (
    claim_sanction_appeal, get_ban_appeal, get_sanction_stats, get_warns,
    log_sanction, release_sanction_appeal_claim, resolve_sanction_appeal, set_warns,
)

bp = Blueprint("rest_api", __name__)

API_KEY = os.getenv("API_KEY")  # clé secrète pour protéger l'API, à définir sur Render


def _is_valid_snowflake(value):
    """Return whether a route identifier is a valid positive Discord snowflake."""
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and 0 < int(value) <= (2**64 - 1)
    )


def _get_json_object():
    """Return a JSON object, allowing an omitted body for legacy default actions."""
    data = request.get_json(silent=True)
    if data is None and not request.data:
        return {}
    return data if isinstance(data, dict) else None


def _validated_reason(data, default):
    """Return a bounded moderation reason, or None for an invalid payload value."""
    reason = data.get("reason", default)
    if not isinstance(reason, str) or len(reason) > 512:
        return None
    return reason.strip() or default


def _api_key_matches(provided, expected):
    """Compare API keys in constant time, ignoring malformed stored values."""
    return isinstance(expected, str) and secrets.compare_digest(provided, expected)


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
        for parameter in ("guild_id", "user_id"):
            value = kwargs.get(parameter)
            if value is not None and not _is_valid_snowflake(value):
                return jsonify({"error": f"Invalid {parameter}"}), 400
        if API_KEY and _api_key_matches(key, API_KEY):
            g.auth_guild_id = None
            return f(*args, **kwargs)
        guild_id = kwargs.get("guild_id")
        if guild_id:
            cfg = get_config(guild_id)
            if cfg.get("api_key") and _api_key_matches(key, cfg["api_key"]):
                g.auth_guild_id = str(guild_id)
                return f(*args, **kwargs)
        elif core.bot.is_ready():
            # Pas de guild_id dans l'URL (ex: /api/health, /api/guilds) : on cherche
            # à quel serveur cette clé appartient parmi tous les serveurs du bot.
            for candidate_guild in core.bot.guilds:
                cfg = get_config(candidate_guild.id)
                if cfg.get("api_key") and _api_key_matches(key, cfg["api_key"]):
                    g.auth_guild_id = str(candidate_guild.id)
                    return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401
    return wrapper


@bp.route('/api/health', methods=['GET'])
@require_api_key
def health():
    return jsonify(get_bot_service().api_health_snapshot(g.auth_guild_id))


@bp.route('/api/guilds', methods=['GET'])
@require_api_key
def list_guilds():
    guilds = get_bot_service().api_guild_list_snapshot(g.auth_guild_id)
    if guilds is None:
        return jsonify({"error": "Bot not ready"}), 503
    return jsonify(guilds)


@bp.route('/api/guilds/<guild_id>/stats', methods=['GET'])
@require_api_key
def guild_stats(guild_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
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

    warned_count = col.warns.count_documents({"guild_id": str(guild_id), "count": {"$gt": 0}})

    cfg = get_config(guild_id)
    open_tickets_count = 0
    ticket_category_id = cfg.get("ticket_category_id")
    if ticket_category_id:
        category = guild.get_channel(int(ticket_category_id))
        if isinstance(category, discord.CategoryChannel):
            open_tickets_count = len(category.text_channels)

    today_start = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    warns_today = col.sanctions.count_documents({
        "guild_id": str(guild_id), "type": "warn", "timestamp": {"$gte": today_start},
    })
    activity_7d = [count for _, count in get_sanction_stats(guild_id, days=7)]

    return jsonify({
        "guild_id": str(guild.id),
        "name": guild.name,
        "member_count": guild.member_count,
        "ban_count": ban_count,
        "muted_count": muted_count,
        "warned_count": warned_count,
        "warns_today": warns_today,
        "open_tickets_count": open_tickets_count,
        "activity_7d": activity_7d,
        "locked": int(guild_id) in core.bot.locked_guilds
    })


@bp.route('/api/guilds/<guild_id>/config', methods=['GET'])
@require_api_key
def get_guild_config(guild_id):
    cfg = get_config(guild_id)
    cfg.pop("_id", None)
    cfg.pop("api_key", None)  # jamais renvoyée en clair
    return jsonify(cfg)


@bp.route('/api/guilds/<guild_id>/config', methods=['POST'])
@require_api_key
def update_guild_config(guild_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON body must be an object"}), 400

    allowed_keys = {"logs_channel", "autorole", "allowed_roles", "command_roles", "language"}
    updates = {key: value for key, value in data.items() if key in allowed_keys}
    if not updates:
        return jsonify({"error": "No valid fields provided"}), 400

    # Validate the complete payload before writing anything to MongoDB.
    for key, value in updates.items():
        if key == "logs_channel":
            valid = (
                isinstance(value, str)
                and len(value) <= 100
                and (value == "logs" or _is_valid_snowflake(value))
            )
        elif key == "autorole":
            valid = value is None or _is_valid_snowflake(value)
        elif key == "allowed_roles":
            valid = (
                isinstance(value, list)
                and len(value) <= 100
                and all(_is_valid_snowflake(role_id) for role_id in value)
            )
        elif key == "command_roles":
            valid = (
                isinstance(value, dict)
                and len(value) <= 100
                and all(
                    isinstance(command, str)
                    and 1 <= len(command) <= 100
                    and isinstance(role_ids, list)
                    and len(role_ids) <= 100
                    and all(_is_valid_snowflake(role_id) for role_id in role_ids)
                    for command, role_ids in value.items()
                )
            )
        else:  # language
            valid = value is None or value in ("en", "fr")

        if not valid:
            return jsonify({"error": f"Invalid {key}"}), 400

    for key, value in updates.items():
        update_config(guild_id, key, value)

    return jsonify({"success": True, "updated": updates})


@bp.route('/api/guilds/<guild_id>/lock', methods=['POST'])
@require_api_key
def lock_bot_route(guild_id):
    core.bot.locked_guilds.add(int(guild_id))
    return jsonify({"success": True, "locked": True})


@bp.route('/api/guilds/<guild_id>/unlock', methods=['POST'])
@require_api_key
def unlock_bot_route(guild_id):
    core.bot.locked_guilds.discard(int(guild_id))
    return jsonify({"success": True, "locked": False})


@bp.route('/api/guilds/<guild_id>/broadcast', methods=['POST'])
@require_api_key
def broadcast_route(guild_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    message = data.get("message")
    channel_id = data.get("channel_id")
    mention = data.get("mention", "none")

    if not isinstance(message, str) or not message.strip() or len(message) > 4096:
        return jsonify({"error": "message must be a non-empty string of at most 4096 characters"}), 400
    if not _is_valid_snowflake(channel_id):
        return jsonify({"error": "channel_id must be a valid Discord ID"}), 400
    if not isinstance(mention, str) or len(mention) > 100:
        return jsonify({"error": "mention must be a string of at most 100 characters"}), 400

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


@bp.route('/api/guilds/<guild_id>/members/<user_id>/ban', methods=['POST'])
@require_api_key
def ban_member_route(guild_id, user_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    reason = _validated_reason(data, "Banned via mobile app")
    if reason is None:
        return jsonify({"error": "reason must be a string of at most 512 characters"}), 400

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


@bp.route('/api/guilds/<guild_id>/members/<user_id>/kick', methods=['POST'])
@require_api_key
def kick_member_route(guild_id, user_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    reason = _validated_reason(data, "Kicked via mobile app")
    if reason is None:
        return jsonify({"error": "reason must be a string of at most 512 characters"}), 400

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


@bp.route('/api/guilds/<guild_id>/members/<user_id>/warnings', methods=['GET'])
@require_api_key
def get_warnings_route(guild_id, user_id):
    count = get_warns(guild_id, user_id)
    return jsonify({"user_id": user_id, "warnings": count})


@bp.route('/api/guilds/<guild_id>/warnlist', methods=['GET'])
@require_api_key
def warnlist_route(guild_id):
    docs = list(col.warns.find({"guild_id": str(guild_id), "count": {"$gt": 0}}))
    result = [{"user_id": d["user_id"], "count": d["count"]} for d in docs]
    return jsonify(result)


@bp.route('/api/guilds/<guild_id>/channels', methods=['GET'])
@require_api_key
def list_channels_route(guild_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404
    return jsonify(get_bot_service().api_text_channels_snapshot(guild_id) or [])


@bp.route('/api/guilds/<guild_id>/roles', methods=['GET'])
@require_api_key
def list_roles_route(guild_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404
    return jsonify(get_bot_service().api_roles_snapshot(guild_id) or [])


@bp.route('/api/guilds/<guild_id>/members', methods=['GET'])
@require_api_key
def list_members_route(guild_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
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


@bp.route('/api/guilds/<guild_id>/members/<user_id>/mute', methods=['POST'])
@require_api_key
def mute_member_route(guild_id, user_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    reason = _validated_reason(data, "Muted by mobile app")
    if reason is None:
        return jsonify({"error": "reason must be a string of at most 512 characters"}), 400
    raw_minutes = data.get("minutes", 10)
    if isinstance(raw_minutes, bool) or not (
        isinstance(raw_minutes, int)
        or (isinstance(raw_minutes, str) and raw_minutes.isdecimal())
    ):
        return jsonify({"error": "minutes must be an integer between 1 and 40320"}), 400
    minutes = int(raw_minutes)
    if not 1 <= minutes <= 40320:
        return jsonify({"error": "minutes must be an integer between 1 and 40320"}), 400

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


@bp.route('/api/guilds/<guild_id>/members/<user_id>/unmute', methods=['POST'])
@require_api_key
def unmute_member_route(guild_id, user_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
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


@bp.route('/api/guilds/<guild_id>/members/<user_id>/warn', methods=['POST'])
@require_api_key
def warn_member_route(guild_id, user_id):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    reason = _validated_reason(data, "Warned by mobile app")
    if reason is None:
        return jsonify({"error": "reason must be a string of at most 512 characters"}), 400

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


@bp.route('/api/guilds/<guild_id>/members/<user_id>/unwarn', methods=['POST'])
@require_api_key
def unwarn_member_route(guild_id, user_id):
    count = get_warns(guild_id, user_id)
    if count == 0:
        return jsonify({"error": "No warnings to remove"}), 400
    count -= 1
    set_warns(guild_id, user_id, count)

    if core.bot.is_ready():
        guild = core.bot.get_guild(int(guild_id))
        if guild:
            member = guild.get_member(int(user_id))
            if member:
                try:
                    run_coroutine(log_unwarn(guild, member, count))
                except Exception:
                    pass

    return jsonify({"success": True, "warnings": count})


@bp.route('/api/guilds/<guild_id>/audit', methods=['GET'])
@require_api_key
def guild_audit_route(guild_id):
    """Fil d'activité multi-admin pour l'app mobile -- même source que le
    dashboard web (get_audit_log), fusionne actions dashboard + sanctions."""
    requested_limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(requested_limit if requested_limit is not None else 20, 100))
    entries = get_audit_log(guild_id, limit=limit)
    return jsonify([{
        "actor_name": e.get("actor_name", "Unknown"),
        "action": e.get("action", ""),
        "details": e.get("details", ""),
        "timestamp": e["timestamp"].isoformat(),
    } for e in entries])


@bp.route('/api/guilds/<guild_id>/cases', methods=['GET'])
@require_api_key
def guild_cases_route(guild_id):
    """Historique des sanctions (case browser) pour l'app mobile -- même
    requête Mongo que la page /dashboard/<guild_id>/cases côté web."""
    filter_type = request.args.get("type", "")
    requested_limit = request.args.get("limit", default=50, type=int)
    limit = max(1, min(requested_limit if requested_limit is not None else 50, 200))
    query = {"guild_id": str(guild_id)}
    if filter_type:
        query["type"] = filter_type
    cases = list(col.sanctions.find(query).sort("timestamp", -1).limit(limit))
    return jsonify([{
        "case_id": c.get("case_id"),
        "user_id": c.get("user_id"),
        "type": c.get("type"),
        "reason": c.get("reason", ""),
        "moderator_id": c.get("moderator_id"),
        "timestamp": c["timestamp"].isoformat(),
    } for c in cases])


@bp.route('/api/guilds/<guild_id>/appeals', methods=['GET'])
@require_api_key
def guild_appeals_route(guild_id):
    """Appels de sanction en attente de revue, pour l'app mobile -- mêmes
    entrées que la page /dashboard/<guild_id>/appeals côté web."""
    appeals = list(col.ban_appeals.find({"guild_id": str(guild_id), "status": "submitted"}).sort("submitted_at", -1))
    return jsonify([{
        "token": a["token"],
        "case_id": a.get("case_id"),
        "sanction_type": a.get("sanction_type", "ban"),
        "user_id": a.get("user_id"),
        "user_name": a.get("user_name"),
        "original_reason": a.get("ban_reason", ""),
        "appeal_text": a.get("appeal_text", ""),
    } for a in appeals])


@bp.route('/api/guilds/<guild_id>/appeals/<token>/accept', methods=['POST'])
@require_api_key
def guild_appeal_accept_route(guild_id, token):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    appeal = get_ban_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        return jsonify({"error": "Appeal not found or already resolved"}), 404

    if not claim_sanction_appeal(guild_id, token):
        return jsonify({"error": "Appeal not found or already resolved"}), 404

    sanction_type = appeal.get("sanction_type", "ban")
    try:
        result = run_coroutine(reverse_sanction(guild, sanction_type, appeal["user_id"]))
    except Exception:
        release_sanction_appeal_claim(guild_id, token)
        return jsonify({"error": "Unable to resolve appeal"}), 500
    if not resolve_sanction_appeal(guild_id, token, "accepted"):
        return jsonify({"error": "Appeal resolution conflict"}), 409
    record_audit(guild_id, "mobile_app", "📱 Mobile App", "Accepted appeal (via mobile app)", f"Case #{appeal['case_id']} ({sanction_type}) — {result}")
    try:
        run_coroutine(_dm_user_appeal_result(int(appeal["user_id"]), guild.name, appeal["case_id"], True, result))
    except Exception as e:
        print(f"[APPEAL] Failed to DM user: {e}", flush=True)
    return jsonify({"success": True, "result": result})


@bp.route('/api/guilds/<guild_id>/appeals/<token>/deny', methods=['POST'])
@require_api_key
def guild_appeal_deny_route(guild_id, token):
    if not core.bot.is_ready():
        return jsonify({"error": "Bot not ready"}), 503
    guild = core.bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    appeal = get_ban_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        return jsonify({"error": "Appeal not found or already resolved"}), 404

    if not claim_sanction_appeal(guild_id, token):
        return jsonify({"error": "Appeal not found or already resolved"}), 404
    if not resolve_sanction_appeal(guild_id, token, "denied"):
        return jsonify({"error": "Appeal resolution conflict"}), 409
    record_audit(guild_id, "mobile_app", "📱 Mobile App", "Denied appeal (via mobile app)", f"Case #{appeal['case_id']}")
    try:
        run_coroutine(_dm_user_appeal_result(int(appeal["user_id"]), guild.name, appeal["case_id"], False, None))
    except Exception as e:
        print(f"[APPEAL] Failed to DM user: {e}", flush=True)
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/features', methods=['GET'])
@require_api_key
def guild_features_route(guild_id):
    cfg = get_config(guild_id)
    return jsonify([{
        "key": key,
        "label": meta["label"],
        "enabled": bool(cfg.get(meta["config_key"])),
    } for key, meta in FEATURE_TOGGLES.items()])


@bp.route('/api/guilds/<guild_id>/features/<feature_key>', methods=['POST'])
@require_api_key
def guild_feature_toggle_route(guild_id, feature_key):
    if feature_key not in FEATURE_TOGGLES:
        return jsonify({"error": "Unknown feature"}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "enabled must be a boolean"}), 400
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    update_config(guild_id, FEATURE_TOGGLES[feature_key]["config_key"], enabled)
    return jsonify({"success": True, "key": feature_key, "enabled": enabled})


@bp.route('/api/guilds/<guild_id>/faq', methods=['GET'])
@require_api_key
def guild_faq_list_route(guild_id):
    """Même collection Mongo que cogs/faq.py -- l'app lit/écrit directement
    dedans, pas besoin de passer par le bot (pas d'accès discord.py requis ici)."""
    doc = col.faq.find_one({"guild_id": str(guild_id)})
    if not doc:
        return jsonify({"enabled": False, "entries": []})
    return jsonify({"enabled": bool(doc.get("enabled")), "entries": doc.get("entries", [])})


@bp.route('/api/guilds/<guild_id>/faq', methods=['POST'])
@require_api_key
def guild_faq_add_route(guild_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "keywords must be a list of 1 to 25 strings (max 100 characters each) and response must be 1 to 2000 characters"}), 400

    raw_keywords = data.get("keywords")
    raw_response = data.get("response")
    if not isinstance(raw_keywords, list) or not 1 <= len(raw_keywords) <= 25:
        return jsonify({"error": "keywords must be a list of 1 to 25 strings (max 100 characters each) and response must be 1 to 2000 characters"}), 400
    if any(not isinstance(keyword, str) for keyword in raw_keywords):
        return jsonify({"error": "keywords must be a list of 1 to 25 strings (max 100 characters each) and response must be 1 to 2000 characters"}), 400

    keywords = [keyword.strip() for keyword in raw_keywords]
    response_text = raw_response.strip() if isinstance(raw_response, str) else ""
    if (
        any(not keyword or len(keyword) > 100 for keyword in keywords)
        or not response_text
        or len(response_text) > 2000
    ):
        return jsonify({"error": "keywords must be a list of 1 to 25 strings (max 100 characters each) and response must be 1 to 2000 characters"}), 400

    doc = col.faq.find_one({"guild_id": str(guild_id)}) or {"guild_id": str(guild_id), "enabled": False, "entries": []}
    existing_ids = {e["id"] for e in doc["entries"]}
    entry_id = secrets.token_hex(16)
    while entry_id in existing_ids:
        entry_id = secrets.token_hex(16)
    doc["entries"].append({"id": entry_id, "keywords": keywords, "response": response_text})
    col.faq.update_one({"guild_id": str(guild_id)}, {"$set": {"entries": doc["entries"]}}, upsert=True)
    return jsonify({"success": True, "id": entry_id})


@bp.route('/api/guilds/<guild_id>/faq/<entry_id>', methods=['DELETE'])
@require_api_key
def guild_faq_remove_route(guild_id, entry_id):
    doc = col.faq.find_one({"guild_id": str(guild_id)})
    if not doc:
        return jsonify({"error": "No FAQ entries for this server"}), 404
    remaining = [e for e in doc["entries"] if e["id"] != entry_id]
    if len(remaining) == len(doc["entries"]):
        return jsonify({"error": "Entry not found"}), 404
    col.faq.update_one({"guild_id": str(guild_id)}, {"$set": {"entries": remaining}})
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/faq/toggle', methods=['POST'])
@require_api_key
def guild_faq_toggle_route(guild_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "enabled must be a boolean"}), 400
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    col.faq.update_one({"guild_id": str(guild_id)}, {"$set": {"enabled": enabled}}, upsert=True)
    return jsonify({"success": True, "enabled": enabled})
