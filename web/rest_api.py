"""web.rest_api -- REST API (/api/*) utilisée par l'app mobile Nexus.

Extrait de main.py (étape 2b-1) : mêmes routes, mêmes chemins, même
comportement. Les routes sont portées par un Blueprint que main.py enregistre
sur l'app Flask. Ce module n'importe JAMAIS main.py (sous `python main.py` le
module s'appelle `__main__`, l'importer le rechargerait avec un 2e bot) : le
bot vient de `core.bot`, la base de `core.col`.
"""
import logging
import secrets
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException

from web.bot_service import get_bot_service
from catalogs.features import FEATURE_TOGGLES

bp = Blueprint("rest_api", __name__)
logger = logging.getLogger(__name__)


@bp.errorhandler(Exception)
def handle_unexpected_api_error(error):
    """Return a stable JSON 500 for unexpected API failures without masking HTTP errors."""
    if isinstance(error, HTTPException):
        return error
    logger.exception("REST API operation failed")
    return jsonify({"error": "Internal server error"}), 500


MAX_API_KEY_LENGTH = 512
MAX_API_REQUEST_BYTES = 1024 * 1024


def _is_valid_snowflake(value):
    """Return whether a route identifier is a valid positive Discord snowflake."""
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and len(value) <= 20
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
    return (
        isinstance(provided, str)
        and isinstance(expected, str)
        and provided.isascii()
        and expected.isascii()
        and 0 < len(provided) <= MAX_API_KEY_LENGTH
        and 0 < len(expected) <= MAX_API_KEY_LENGTH
        and secrets.compare_digest(provided, expected)
    )


def _invoke_authorized(f, args, kwargs):
    """Reject oversized API bodies before JSON parsing or service work."""
    if (
        request.content_length is not None
        and request.content_length > MAX_API_REQUEST_BYTES
    ):
        return jsonify({"error": "Request body too large"}), 413
    return f(*args, **kwargs)


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
        if len(key) > MAX_API_KEY_LENGTH:
            return jsonify({"error": "Unauthorized"}), 401
        for parameter in ("guild_id", "user_id"):
            value = kwargs.get(parameter)
            if value is not None and not _is_valid_snowflake(value):
                return jsonify({"error": f"Invalid {parameter}"}), 400
        master_api_key = current_app.config.get("API_KEY")
        if master_api_key and _api_key_matches(key, master_api_key):
            g.auth_guild_id = None
            return _invoke_authorized(f, args, kwargs)
        guild_id = kwargs.get("guild_id")
        # Per-guild API keys are verified inside the bot service. The secret is
        # never returned as config data to the Worker or route layer.
        matched_guild_id = get_bot_service().api_authenticate_key(key, str(guild_id) if guild_id else None)
        if matched_guild_id:
            g.auth_guild_id = matched_guild_id
            return _invoke_authorized(f, args, kwargs)
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
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404

    cfg = get_bot_service().api_config_snapshot(guild_id)
    try:
        snapshot = get_bot_service().api_guild_stats_snapshot(
            guild_id, cfg.get("ticket_category_id")
        )
    except Exception:
        logger.exception("REST API operation failed")
        return jsonify({"error": "Internal server error"}), 500
    if snapshot is None:
        return jsonify({"error": "Bot not ready"}), 503

    return jsonify(snapshot)


@bp.route('/api/guilds/<guild_id>/config', methods=['GET'])
@require_api_key
def get_guild_config(guild_id):
    cfg = get_bot_service().api_config_snapshot(guild_id)
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

    get_bot_service().api_update_config(guild_id, updates)

    return jsonify({"success": True, "updated": updates})


@bp.route('/api/guilds/<guild_id>/lock', methods=['POST'])
@require_api_key
def lock_bot_route(guild_id):
    if not get_bot_service().set_guild_locked(guild_id, True):
        return jsonify({"error": "Bot not ready"}), 503
    return jsonify({"success": True, "locked": True})


@bp.route('/api/guilds/<guild_id>/unlock', methods=['POST'])
@require_api_key
def unlock_bot_route(guild_id):
    if not get_bot_service().set_guild_locked(guild_id, False):
        return jsonify({"error": "Bot not ready"}), 503
    return jsonify({"success": True, "locked": False})


@bp.route('/api/guilds/<guild_id>/broadcast', methods=['POST'])
@require_api_key
def broadcast_route(guild_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
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

    try:
        result = get_bot_service().api_broadcast(guild_id, channel_id, message, mention)
    except Exception:
        logger.exception("REST API operation failed")
        return jsonify({"error": "Internal server error"}), 500
    if result is None:
        return jsonify({"error": "Bot not ready"}), 503
    success, error = result
    if not success:
        return jsonify({"error": error}), 404
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/members/<user_id>/ban', methods=['POST'])
@require_api_key
def ban_member_route(guild_id, user_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    reason = _validated_reason(data, "Banned via mobile app")
    if reason is None:
        return jsonify({"error": "reason must be a string of at most 512 characters"}), 400

    try:
        result = get_bot_service().api_ban_member(guild_id, user_id, reason)
    except Exception:
        logger.exception("REST API operation failed")
        return jsonify({"error": "Internal server error"}), 500
    if result is None:
        return jsonify({"error": "Bot not ready"}), 503
    success, error = result
    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/members/<user_id>/kick', methods=['POST'])
@require_api_key
def kick_member_route(guild_id, user_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    reason = _validated_reason(data, "Kicked via mobile app")
    if reason is None:
        return jsonify({"error": "reason must be a string of at most 512 characters"}), 400

    try:
        result = get_bot_service().api_kick_member(guild_id, user_id, reason)
    except Exception:
        logger.exception("REST API operation failed")
        return jsonify({"error": "Internal server error"}), 500
    if result is None:
        return jsonify({"error": "Bot not ready"}), 503
    success, error = result
    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/members/<user_id>/warnings', methods=['GET'])
@require_api_key
def get_warnings_route(guild_id, user_id):
    count = get_bot_service().api_member_warnings(guild_id, user_id)
    return jsonify({"user_id": user_id, "warnings": count})


@bp.route('/api/guilds/<guild_id>/warnlist', methods=['GET'])
@require_api_key
def warnlist_route(guild_id):
    docs = get_bot_service().api_warnlist_snapshot(guild_id)
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
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404
    return jsonify(get_bot_service().api_members_snapshot(guild_id) or [])


@bp.route('/api/guilds/<guild_id>/members/<user_id>/mute', methods=['POST'])
@require_api_key
def mute_member_route(guild_id, user_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
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
        or (
            isinstance(raw_minutes, str)
            and raw_minutes.isdecimal()
            and len(raw_minutes) <= 5
        )
    ):
        return jsonify({"error": "minutes must be an integer between 1 and 40320"}), 400
    minutes = int(raw_minutes)
    if not 1 <= minutes <= 40320:
        return jsonify({"error": "minutes must be an integer between 1 and 40320"}), 400

    try:
        result = get_bot_service().api_mute_member(guild_id, user_id, minutes, reason)
    except Exception:
        logger.exception("REST API operation failed")
        return jsonify({"error": "Internal server error"}), 500
    if result is None:
        return jsonify({"error": "Bot not ready"}), 503
    success, error = result
    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/members/<user_id>/unmute', methods=['POST'])
@require_api_key
def unmute_member_route(guild_id, user_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404

    try:
        success, error = get_bot_service().api_unmute_member(guild_id, user_id)
    except Exception:
        logger.exception("REST API operation failed")
        return jsonify({"error": "Internal server error"}), 500

    if not success:
        return jsonify({"error": error}), 400
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/members/<user_id>/warn', methods=['POST'])
@require_api_key
def warn_member_route(guild_id, user_id):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404

    data = _get_json_object()
    if data is None:
        return jsonify({"error": "JSON body must be an object"}), 400
    reason = _validated_reason(data, "Warned by mobile app")
    if reason is None:
        return jsonify({"error": "reason must be a string of at most 512 characters"}), 400

    result = get_bot_service().api_warn_member(guild_id, user_id, reason)
    if result is None:
        return jsonify({"error": "Bot not ready"}), 503
    success, value = result
    if not success:
        return jsonify({"error": value}), 404
    return jsonify({"success": True, "warnings": value, "reason": reason})


@bp.route('/api/guilds/<guild_id>/members/<user_id>/unwarn', methods=['POST'])
@require_api_key
def unwarn_member_route(guild_id, user_id):
    success, value = get_bot_service().api_unwarn_member(guild_id, user_id)
    if not success:
        return jsonify({"error": value}), 400
    return jsonify({"success": True, "warnings": value})


@bp.route('/api/guilds/<guild_id>/audit', methods=['GET'])
@require_api_key
def guild_audit_route(guild_id):
    """Fil d'activité multi-admin pour l'app mobile -- même source que le
    dashboard web (get_audit_log), fusionne actions dashboard + sanctions."""
    requested_limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(requested_limit if requested_limit is not None else 20, 100))
    entries = get_bot_service().api_audit_snapshot(guild_id, limit=limit)
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
    cases = get_bot_service().api_sanction_cases_snapshot(guild_id, filter_type, limit)
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
    appeals = get_bot_service().api_submitted_appeals(guild_id)
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
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404
    appeal = get_bot_service().api_get_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        return jsonify({"error": "Appeal not found or already resolved"}), 404

    if not get_bot_service().api_claim_appeal(guild_id, token):
        return jsonify({"error": "Appeal not found or already resolved"}), 404

    sanction_type = appeal.get("sanction_type", "ban")
    success, result = get_bot_service().api_accept_appeal(
        guild_id, appeal["user_id"], sanction_type
    )
    if not success:
        get_bot_service().api_release_appeal_claim(guild_id, token)
        status_code = 503 if result == "Bot not ready" else 404 if result == "Guild not found" else 500
        return jsonify({"error": result}), status_code
    if not get_bot_service().api_resolve_appeal(guild_id, token, "accepted"):
        return jsonify({"error": "Appeal resolution conflict"}), 409
    get_bot_service().api_record_audit(guild_id, "mobile_app", "📱 Mobile App", "Accepted appeal (via mobile app)", f"Case #{appeal['case_id']} ({sanction_type}) — {result}")
    get_bot_service().api_notify_appeal_result(guild_id, appeal["user_id"], appeal["case_id"], True, result)
    return jsonify({"success": True, "result": result})


@bp.route('/api/guilds/<guild_id>/appeals/<token>/deny', methods=['POST'])
@require_api_key
def guild_appeal_deny_route(guild_id, token):
    presence = get_bot_service().guild_presence(guild_id)
    if not presence["bot_ready"]:
        return jsonify({"error": "Bot not ready"}), 503
    if not presence["guild_present"]:
        return jsonify({"error": "Guild not found"}), 404
    appeal = get_bot_service().api_get_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        return jsonify({"error": "Appeal not found or already resolved"}), 404

    if not get_bot_service().api_claim_appeal(guild_id, token):
        return jsonify({"error": "Appeal not found or already resolved"}), 404
    if not get_bot_service().api_resolve_appeal(guild_id, token, "denied"):
        return jsonify({"error": "Appeal resolution conflict"}), 409
    get_bot_service().api_record_audit(guild_id, "mobile_app", "📱 Mobile App", "Denied appeal (via mobile app)", f"Case #{appeal['case_id']}")
    get_bot_service().api_notify_appeal_result(guild_id, appeal["user_id"], appeal["case_id"], False)
    return jsonify({"success": True})


@bp.route('/api/guilds/<guild_id>/features', methods=['GET'])
@require_api_key
def guild_features_route(guild_id):
    cfg = get_bot_service().api_config_snapshot(guild_id)
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
    get_bot_service().api_update_config(guild_id, {FEATURE_TOGGLES[feature_key]["config_key"]: enabled})
    return jsonify({"success": True, "key": feature_key, "enabled": enabled})


@bp.route('/api/guilds/<guild_id>/faq', methods=['GET'])
@require_api_key
def guild_faq_list_route(guild_id):
    """Return FAQ data through the bot service without exposing persistence details."""
    doc = get_bot_service().api_faq_snapshot(guild_id)
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

    existing_doc = get_bot_service().api_faq_snapshot(guild_id) or {"guild_id": str(guild_id), "enabled": False, "entries": []}
    existing_ids = {e["id"] for e in existing_doc["entries"]}
    entry_id = secrets.token_hex(16)
    while entry_id in existing_ids:
        entry_id = secrets.token_hex(16)
    get_bot_service().api_faq_add_entry(guild_id, {"id": entry_id, "keywords": keywords, "response": response_text})
    return jsonify({"success": True, "id": entry_id})


@bp.route('/api/guilds/<guild_id>/faq/<entry_id>', methods=['DELETE'])
@require_api_key
def guild_faq_remove_route(guild_id, entry_id):
    if not get_bot_service().api_faq_snapshot(guild_id):
        return jsonify({"error": "No FAQ entries for this server"}), 404
    if not get_bot_service().api_faq_remove_entry(guild_id, entry_id):
        return jsonify({"error": "Entry not found"}), 404
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
    get_bot_service().api_faq_set_enabled(guild_id, enabled)
    return jsonify({"success": True, "enabled": enabled})
