"""web.dashboard -- pages HTML du dashboard (/dashboard/*) : serveurs, réglages, fonctionnalités,
permissions, templates, cases, appels, exports d'audit.

Extrait de main.py (étape 2b-2b). Ce module n'importe JAMAIS main.py (bot -> `core.bot`,
collections -> `core.col`). Les routes sont enregistrées par main.py via `routes.register(api)`
avec leurs noms d'endpoints d'origine (cf. web/routing.py) : les `url_for('dash_...')` des
templates continuent de fonctionner sans modification.
"""
import datetime
from functools import wraps
import secrets
import time

from flask import flash, jsonify, redirect, render_template, request, session, url_for

from audit_visuals import build_sparkline_svg
from audit_export_utils import _build_audit_csv_bytes, _build_audit_pdf_bytes
from command_catalog import COMMAND_TEMPLATES, MODERATION_COMMANDS
from calendar_utils import parse_event_datetime, local_when, validate_timezone
from features import FEATURE_TOGGLES
from i18n import TRANSLATIONS, current_lang, t
from utils import normalize_banned_word, normalize_domain, parse_lines_list
from dashboard_utils import (\n    read_bounded_form_numbers, validate_channel_selection, validate_role_selection,\n    is_safe_relative_redirect, build_feature_toggle_updates,\n)
from web.auth import dash_guild_admin_required, dash_login_required, get_user_admin_guilds
from web.bot_service import get_bot_service
from web.routing import Routes

routes = Routes()


def _safe_next_url(default):
    """Cible de redirection tirée de ?next=, uniquement si c'est un chemin RELATIF de ce site.

    Sans ce filtre, `/dashboard/lang/fr?next=https://site-pirate` redirigerait n'importe qui vers
    un site externe (« open redirect », utile pour du phishing depuis un vrai lien Nexus).
    Refusés : URL absolues, `//hote` (protocole relatif), antislash, caractères de contrôle
    (les navigateurs ignorent tab/retour ligne : `/\t/hote` redevient `//hote`)."""
    target = request.args.get("next") or ""
    if is_safe_relative_redirect(target):
        return target
    return default


@routes.route("/dashboard/lang/<lang_code>", methods=["POST"])
def dash_set_lang(lang_code):
    if lang_code in TRANSLATIONS:
        session["lang"] = lang_code
    return redirect(_safe_next_url(url_for("dash_home")))


_dashboard_presence = {}  # {guild_id: {user_id: (name, last_seen_epoch)}}
_PRESENCE_TTL_SECONDS = 25  # un peu plus que l'intervalle de ping JS (10s) côté client


@routes.route("/dashboard/<guild_id>/presence/ping", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_presence_ping(guild_id):
    user = session.get("dash_user") or {}
    name = user.get("global_name") or user.get("username") or "?"
    _dashboard_presence.setdefault(guild_id, {})[user.get("id")] = (name, time.time())
    return jsonify({"ok": True})


@routes.route("/dashboard/<guild_id>/presence/who", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
def dash_presence_who(guild_id):
    now = time.time()
    entries = _dashboard_presence.get(guild_id, {})
    active = [name for name, last_seen in entries.values() if now - last_seen < _PRESENCE_TTL_SECONDS]
    return jsonify({"viewers": active})


# Valeurs par défaut utilisées à l'AFFICHAGE : le snapshot de configuration ne remplit les
# défauts qu'à la création d'un doc, donc un vieux serveur peut ne pas avoir
# certaines clés (ex: remindme_enabled est actif par défaut dans le code).
SETTINGS_DEFAULTS = {
    "automod_spam_count": 10, "automod_spam_window": 5, "automod_caps_ratio": 0.7,
    "automod_raid_count": 5, "automod_raid_window": 10,
    "mention_spam_threshold": 5, "media_flood_count": 5, "media_flood_window": 10,
    "auto_slowmode_threshold": 10, "auto_slowmode_window": 10,
    "auto_slowmode_seconds": 10, "auto_slowmode_duration_minutes": 5,
    "warn_escalation_enabled": False, "warn_mute_threshold": 3,
    "warn_mute_minutes": 10, "warn_kick_threshold": 5,
    "min_account_age_days": 0, "antinuke_threshold": 5,
    "quarantine_suspicion_days": 7, "quarantine_hold_minutes": 30,
}


FEATURE_DEFAULT_ENABLED = {"remindme_enabled": True}


# (clé de config = nom du champ de formulaire, type, min, max). Les minimums
# sont ceux des commandes /config ; les maximums sont des garde-fous de bon
# sens que les commandes Discord n'ont pas.
AUTOMOD_NUMBER_FIELDS = [
    ("automod_spam_count", int, 1, 1000), ("automod_spam_window", int, 1, 3600),
    ("automod_caps_ratio", float, 0.1, 1.0),
    ("automod_raid_count", int, 2, 1000), ("automod_raid_window", int, 2, 3600),
    ("mention_spam_threshold", int, 2, 100),
    ("media_flood_count", int, 2, 100), ("media_flood_window", int, 2, 3600),
    ("auto_slowmode_threshold", int, 3, 1000), ("auto_slowmode_window", int, 2, 3600),
    ("auto_slowmode_seconds", int, 1, 21600), ("auto_slowmode_duration_minutes", int, 1, 10080),
]


PROTECTION_NUMBER_FIELDS = [
    ("warn_mute_threshold", int, 1, 100), ("warn_mute_minutes", int, 1, 40320),
    ("warn_kick_threshold", int, 2, 200),
    ("min_account_age_days", int, 0, 365), ("antinuke_threshold", int, 2, 20),
]


QUARANTINE_NUMBER_FIELDS = [
    ("quarantine_suspicion_days", int, 1, 365), ("quarantine_hold_minutes", int, 1, 1440),
]


TEXT_CHANNEL_SETTINGS = ["welcome_channel_id", "appeal_channel_id", "boost_channel_id", "trap_channel_id"]
VOICE_CHANNEL_SETTINGS = ["member_count_channel_id", "jtc_channel_id"]


# Features qui ne font rien tant qu'un réglage n'est pas fait : la page
# Features affiche un avertissement (même piège que sur Discord, où activer
# sans configurer ne dit rien).
FEATURE_REQUIREMENTS = {
    "welcome_banner": (("welcome_channel_id",), "req_welcome_channel"),
    "verification": (("verified_role_id", "unverified_role_id"), "req_verification_roles"),
    "quarantine": (("quarantine_role_id", "quarantine_channel_id"), "req_quarantine"),
    "trap_channel": (("trap_channel_id",), "req_trap_channel"),
}


def _t_or(key, default):
    """Traduction si elle existe, sinon le texte par défaut (t() renverrait la clé brute)."""
    translations = TRANSLATIONS.get(session.get("lang", "en"), TRANSLATIONS["en"])
    return translations.get(key) or TRANSLATIONS["en"].get(key) or default


def _read_numbers(fields):
    """Read bounded numeric form fields; blank values remain unchanged."""
    try:
        return read_bounded_form_numbers(request.form, fields)
    except ValueError as exc:
        raise ValueError(str(exc))


def _read_channel(key, channels):
    """Validate a channel selection against this server's actual channels."""
    try:
        return validate_channel_selection(request.form.get(key, ""), channels)
    except ValueError as exc:
        raise ValueError(key) from exc


def _read_role(key, guild):
    """Validate a role selection against actual server roles, excluding @everyone."""
    try:
        return validate_role_selection(request.form.get(key, ""), guild)
    except ValueError as exc:
        raise ValueError(key) from exc


def _describe_change(key, old, new):
    if isinstance(new, list):
        return f"{key}: {len(old or [])} -> {len(new)} entries"
    return f"{key}: {old} -> {new}"


@routes.route("/dashboard/<guild_id>/features", methods=["GET", "POST"])
@dash_login_required
@dash_guild_admin_required
def dash_features_page(guild_id):
    """Équivalent web de /feature enable|disable|list -- construit depuis
    FEATURE_TOGGLES, donc tout nouveau toggle côté Discord apparaît ici tout seul."""
    guild_name = get_bot_service().guild_name(guild_id)
    cfg = get_bot_service().api_config_snapshot(guild_id)

    def is_enabled(meta):
        return bool(cfg.get(meta["config_key"], FEATURE_DEFAULT_ENABLED.get(meta["config_key"], False)))

    if request.method == "POST":
        submitted_keys = {key for key in FEATURE_TOGGLES if f"feat__{key}" in request.form}
        updates, enabled_now, disabled_now = build_feature_toggle_updates(
            FEATURE_TOGGLES, cfg, submitted_keys, FEATURE_DEFAULT_ENABLED
        )
        for config_key, new_value in updates:
            get_bot_service().api_update_config(guild_id, {config_key: new_value})
        if enabled_now or disabled_now:
            parts = []
            if enabled_now:
                parts.append("enabled: " + ", ".join(enabled_now))
            if disabled_now:
                parts.append("disabled: " + ", ".join(disabled_now))
            dash_log_action(guild_id, "Updated feature toggles", " | ".join(parts))
        flash(t("flash_features_saved"))
        return redirect(url_for("dash_features_page", guild_id=guild_id))

    rows = []
    for key, meta in FEATURE_TOGGLES.items():
        requirement = FEATURE_REQUIREMENTS.get(key)
        needs = t(requirement[1]) if requirement and not all(cfg.get(k) for k in requirement[0]) else None
        rows.append({"key": key, "label": _t_or(f"feat_{key}", meta["label"]), "enabled": is_enabled(meta), "needs": needs})
    return render_template("features.html", guild_id=guild_id, guild_name=guild_name, rows=rows, user=session.get("dash_user"))


@routes.route("/dashboard/<guild_id>/calendar", methods=["GET", "POST"])
@dash_login_required
@dash_guild_admin_required
def dash_calendar_page(guild_id):
    guild = get_bot_service().dashboard_settings_snapshot(guild_id)
    if guild is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    cfg = get_bot_service().api_config_snapshot(guild_id)
    timezone_name = cfg.get("calendar_timezone") or "Europe/Paris"
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "settings":
            try:
                channel_id = validate_channel_selection(
                    request.form.get("calendar_channel_id", ""), guild["text_channels"]
                )
            except ValueError:
                flash("Invalid announcement channel." if current_lang() == "en" else "Salon d'annonce invalide.", "error-calendar")
                return redirect(url_for("dash_calendar_page", guild_id=guild_id))
            try:
                timezone_name = validate_timezone(request.form.get("calendar_timezone", ""))
            except ValueError:
                flash("Invalid timezone." if current_lang() == "en" else "Fuseau horaire invalide.", "error-calendar")
                return redirect(url_for("dash_calendar_page", guild_id=guild_id))
            get_bot_service().api_update_config(guild_id, {"calendar_timezone": timezone_name})
            get_bot_service().api_update_config(guild_id, {"calendar_channel_id": channel_id})
            dash_log_action(guild_id, "Updated calendar settings", f"timezone={timezone_name}; channel={channel_id}")
            flash("Calendar settings saved." if current_lang() == "en" else "Réglages du calendrier enregistrés.", "calendar")
        elif action == "create":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            role_raw = request.form.get("role_id", "")
            if not title or len(title) > 100 or len(description) > 1000 or not role_raw.isdigit():
                flash("Invalid event fields." if current_lang() == "en" else "Champs d'événement invalides.", "error-calendar")
                return redirect(url_for("dash_calendar_page", guild_id=guild_id))
            try:
                role_id = validate_role_selection(role_raw, guild)
            except ValueError:
                role_id = None
            role = next((r for r in guild["roles"] if int(r["id"]) == role_id), None) if role_id is not None else None
            if role is None:
                flash("Choose a valid notification role." if current_lang() == "en" else "Choisis un rôle de notification valide.", "error-calendar")
                return redirect(url_for("dash_calendar_page", guild_id=guild_id))
            try:
                starts_at = parse_event_datetime(request.form.get("date", ""), request.form.get("time", "") or None, timezone_name)
            except ValueError as exc:
                flash(str(exc), "error-calendar")
                return redirect(url_for("dash_calendar_page", guild_id=guild_id))
            event = {"guild_id": str(guild_id), "title": title, "role_id": str(role["id"]), "description": description,
                "starts_at": starts_at, "timezone": timezone_name, "time_set": bool(request.form.get("time", "").strip()),
                "channel_id": str(request.form.get("channel_id") or ""), "reminders_sent": [],
                "created_by": str((session.get("dash_user") or {}).get("id", "")), "created_at": datetime.datetime.now(datetime.timezone.utc)}
            if get_bot_service().dashboard_calendar_has_duplicate_event(guild_id, title, starts_at):
                flash("Duplicate event." if current_lang() == "en" else "Événement en double.", "error-calendar")
                return redirect(url_for("dash_calendar_page", guild_id=guild_id))
            event["_id"] = get_bot_service().dashboard_calendar_insert_event(event)
            target_id = cfg.get("calendar_channel_id") or event["channel_id"]
            sent = False
            if str(target_id).isdigit():
                sent, _error = get_bot_service().api_announce_calendar_event(guild_id, str(target_id), event)
            dash_log_action(guild_id, "Created calendar event", title)
            flash(("Event created and announced." if sent else "Event saved; announcement could not be sent.") if current_lang() == "en"
                  else ("Événement créé et annoncé." if sent else "Événement enregistré, annonce non envoyée."), "calendar")
        elif action == "delete":
            from bson import ObjectId
            raw_id = request.form.get("event_id", "")
            try:
                oid = ObjectId(raw_id)
            except Exception:
                oid = None
            deleted = get_bot_service().dashboard_calendar_delete_event(guild_id, oid) if oid else False
            if deleted:
                dash_log_action(guild_id, "Deleted calendar event", raw_id)
                flash("Event deleted." if current_lang() == "en" else "Événement supprimé.", "calendar")
            else:
                flash("Event not found." if current_lang() == "en" else "Événement introuvable.", "error-calendar")
        elif action == "manager":
            role_raw = request.form.get("role_id", "")
            try:
                role_id = validate_role_selection(role_raw, guild)
            except ValueError:
                role_id = None
            role = next((r for r in guild["roles"] if int(r["id"]) == role_id), None) if role_id is not None else None
            is_owner = get_bot_service().is_guild_owner(guild_id, (session.get("dash_user") or {}).get("id"))
            if role is None or not is_owner:
                flash("Only the server owner can manage calendar roles." if current_lang() == "en" else "Seul le propriétaire peut gérer les rôles du calendrier.", "error-calendar")
                return redirect(url_for("dash_calendar_page", guild_id=guild_id))
            manager_ids = set(str(x) for x in cfg.get("calendar_manager_role_ids", []))
            if request.form.get("manager_action") == "remove": manager_ids.discard(str(role["id"]))
            else: manager_ids.add(str(role["id"]))
            get_bot_service().api_update_config(guild_id, {"calendar_manager_role_ids": sorted(manager_ids)})
            dash_log_action(guild_id, "Updated calendar manager roles", role["name"])
            flash("Calendar manager roles saved." if current_lang() == "en" else "Rôles gestionnaires enregistrés.", "calendar")
        else:
            flash("Unknown action." if current_lang() == "en" else "Action inconnue.", "error-calendar")
        return redirect(url_for("dash_calendar_page", guild_id=guild_id))
    events = get_bot_service().dashboard_calendar_upcoming_events(guild_id, datetime.datetime.now(datetime.timezone.utc), limit=50)
    for event in events:
        event["display_when"] = local_when(event)
        event["id_str"] = str(event.get("_id", ""))
    manager_ids = [str(x) for x in cfg.get("calendar_manager_role_ids", [])]
    manager_roles = [r for r in guild["roles"] if str(r["id"]) in manager_ids]
    return render_template("calendar_dashboard.html", guild=guild, events=events, cfg=cfg, timezone_name=timezone_name,
        channels=guild["text_channels"], roles=guild["roles"], manager_roles=manager_roles,
        is_owner=get_bot_service().is_guild_owner(guild_id, (session.get("dash_user") or {}).get("id")), user=session.get("dash_user"))


@routes.route("/dashboard/<guild_id>/settings", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
def dash_settings_page(guild_id):
    snapshot = get_bot_service().dashboard_settings_snapshot(guild_id)
    if snapshot is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    cfg = get_bot_service().api_config_snapshot(guild_id)
    values = {key: cfg.get(key, default) for key, default in SETTINGS_DEFAULTS.items()}
    user_id = (session.get("dash_user") or {}).get("id")
    return render_template(
        "settings.html", guild=snapshot, cfg=cfg, values=values,
        text_channels=snapshot["text_channels"],
        voice_channels=snapshot["voice_channels"],
        roles=snapshot["roles"],
        banned_words_text="\n".join(cfg.get("banned_words", [])),
        banned_domains_text="\n".join(cfg.get("banned_domains", [])),
        languages=sorted(TRANSLATIONS.keys()),
        is_owner=str(snapshot["owner_id"]) == str(user_id),
        user=session.get("dash_user"),
    )


@routes.route("/dashboard/<guild_id>/settings/<section>", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_settings_save(guild_id, section):
    """Une section = un formulaire = une sauvegarde indépendante (sauvegarder
    l'automod n'écrase jamais les champs d'une autre section)."""
    guild = get_bot_service().dashboard_settings_snapshot(guild_id)
    if guild is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    cfg = get_bot_service().api_config_snapshot(guild_id)
    updates = {}
    try:
        if section == "automod":
            updates = _read_numbers(AUTOMOD_NUMBER_FIELDS)
        elif section == "protection":
            updates = _read_numbers(PROTECTION_NUMBER_FIELDS)
            updates["warn_escalation_enabled"] = "warn_escalation_enabled" in request.form
            mute_at = updates.get("warn_mute_threshold", cfg.get("warn_mute_threshold", 3))
            kick_at = updates.get("warn_kick_threshold", cfg.get("warn_kick_threshold", 5))
            # même règle que /config warnescalation : le kick vient toujours après le mute
            updates["warn_kick_threshold"] = max(mute_at + 1, kick_at)
        elif section == "channels":
            for key in TEXT_CHANNEL_SETTINGS:
                updates[key] = _read_channel(key, guild["text_channels"])
            for key in VOICE_CHANNEL_SETTINGS:
                updates[key] = _read_channel(key, guild["voice_channels"])
        elif section == "quarantine":
            updates = _read_numbers(QUARANTINE_NUMBER_FIELDS)
            updates["quarantine_role_id"] = _read_role("quarantine_role_id", guild)
            updates["quarantine_channel_id"] = _read_channel("quarantine_channel_id", guild["text_channels"])
        elif section == "verification":
            verified = _read_role("verified_role_id", guild)
            unverified = _read_role("unverified_role_id", guild)
            if verified is not None and verified == unverified:
                raise ValueError("verified_role_id")
            updates = {"verified_role_id": verified, "unverified_role_id": unverified}
        elif section == "lists":
            try:
                updates["banned_words"] = parse_lines_list(request.form.get("banned_words", ""), normalize_banned_word, 1000, 100)
            except ValueError:
                raise ValueError("banned_words")
            try:
                updates["banned_domains"] = parse_lines_list(request.form.get("banned_domains", ""), normalize_domain, 500, 253)
            except ValueError:
                raise ValueError("banned_domains")
        elif section == "language":
            if not dash_is_owner(guild):
                return jsonify({"error": "Server owner required"}), 403
            language = request.form.get("language", "")
            if language not in TRANSLATIONS:
                raise ValueError("language")
            updates["language"] = language
        else:
            return jsonify({"error": "Unknown settings section"}), 404
    except ValueError as e:
        flash(f"{t('flash_settings_invalid')} ({e})", f"error-{section}")
        return redirect(url_for("dash_settings_page", guild_id=guild_id) + f"#sec-{section}")

    changes = []
    for key, value in updates.items():
        stored = cfg.get(key, _UNSET)
        if stored == value or (stored is _UNSET and value is None):
            continue
        # Même principe que les features : on écrit dès que la valeur stockée
        # diffère ou manque, mais on n'audite que ce qui change réellement.
        effective_old = SETTINGS_DEFAULTS.get(key) if stored is _UNSET else stored
        get_bot_service().api_update_config(guild_id, {key: value})
        if effective_old != value:
            changes.append(_describe_change(key, effective_old, value))
    if changes:
        dash_log_action(guild_id, f"Updated settings ({section})", "; ".join(changes)[:900])
    flash(t("flash_settings_saved"), section)
    return redirect(url_for("dash_settings_page", guild_id=guild_id) + f"#sec-{section}")


@routes.route("/dashboard/<guild_id>/templates", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
def dash_template_marketplace(guild_id):
    guild_name = get_bot_service().guild_name(guild_id)
    templates = get_bot_service().dashboard_public_templates_snapshot()
    return render_template("template_marketplace.html", guild_id=guild_id, guild_name=guild_name, templates=templates, user=session.get("dash_user"))


@routes.route("/dashboard/<guild_id>/templates/import/<code>", methods=["GET", "POST"])
@dash_login_required
@dash_guild_admin_required
def dash_template_import(guild_id, code):
    guild = get_bot_service().dashboard_settings_snapshot(guild_id)
    if guild is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    doc = get_bot_service().dashboard_template_snapshot(code)
    if not doc:
        flash("No template found with that code." if current_lang() == "en" else "Aucun template trouvé avec ce code.")
        return redirect(url_for("dash_template_marketplace", guild_id=guild_id))

    snapshot = doc["snapshot"]
    command_roles_by_name = snapshot.get("command_roles_by_name", {})
    distinct_names = sorted({name for names in command_roles_by_name.values() for name in names})

    if request.method == "POST":
        get_bot_service().dashboard_apply_template_snapshot(guild_id, snapshot)
        name_to_new_id = {}
        for i, old_name in enumerate(distinct_names):
            selected = request.form.get(f"map__{i}", "")
            if selected:
                name_to_new_id[old_name] = int(selected)
        new_command_roles = get_bot_service().api_config_snapshot(guild_id).get("command_roles", {})
        for command, old_names in command_roles_by_name.items():
            mapped_ids = [name_to_new_id[n] for n in old_names if n in name_to_new_id]
            if mapped_ids:
                new_command_roles[command] = mapped_ids
        get_bot_service().api_update_config(guild_id, {"command_roles": new_command_roles})
        dash_log_action(guild_id, f"Imported template '{doc.get('name', doc['code'])}' from {doc['created_guild_name']}")
        flash("Template applied." if current_lang() == "en" else "Template appliqué.")
        return redirect(url_for("dash_guild_page", guild_id=guild_id))

    return render_template("template_import.html", guild_id=guild_id, tpl=doc, distinct_names=distinct_names,
        roles=guild["roles"], user=session.get("dash_user"),
    )


@routes.route("/dashboard/theme/<theme_name>", methods=["POST"])
def dash_set_theme(theme_name):
    if theme_name in ("dark", "light"):
        session["theme"] = theme_name
    return redirect(_safe_next_url(url_for("dash_home")))


def dash_is_owner(guild):
    user = session.get("dash_user") or {}
    owner_id = guild.get("owner_id") if isinstance(guild, dict) else guild.owner_id
    return str(owner_id) == str(user.get("id"))


def dash_actor():
    """Identité de la personne connectée sur le dashboard, pour l'audit log
    et le log Discord optionnel des actions du dashboard."""
    user = session.get("dash_user") or {}
    name = user.get("global_name") or user.get("username") or "Unknown"
    return user.get("id"), name


def dash_log_action(guild_id, action, details=""):
    """Record the dashboard audit event and delegate optional Discord logging."""
    actor_id, actor_name = dash_actor()
    get_bot_service().api_record_audit(guild_id, actor_id, actor_name, action, details)
    try:
        posted, error = get_bot_service().api_log_dashboard_action(
            guild_id, actor_name, action, details
        )
        if not posted:
            print(f"[DASHLOG] Failed to send Discord log: {error}", flush=True)
    except Exception as e:
        print(f"[DASHLOG] Failed to send Discord log: {e}", flush=True)


def dash_owner_required(f):
    @wraps(f)
    def wrapper(guild_id, *args, **kwargs):
        user_id = (session.get("dash_user") or {}).get("id")
        if not get_bot_service().is_guild_owner(guild_id, user_id):
            return jsonify({"error": "Server owner required"}), 403
        return f(guild_id, *args, **kwargs)
    return wrapper


@routes.route("/dashboard")
@dash_login_required
def dash_home():
    admin_guilds = get_user_admin_guilds()
    if admin_guilds is None:
        return redirect(url_for("dash_login"))
    # ne montre que les serveurs où l'utilisateur est admin ET où le bot est présent
    bot_guild_ids = set(get_bot_service().available_guild_ids([g_["id"] for g_ in admin_guilds]))
    manageable = [g_ for g_ in admin_guilds if g_["id"] in bot_guild_ids]
    return render_template("dashboard_list.html", guilds=manageable, user=session.get("dash_user")
    )


@routes.route("/dashboard/<guild_id>", methods=["GET", "POST"])
@dash_login_required
@dash_guild_admin_required
def dash_guild_page(guild_id):
    cfg = get_bot_service().api_config_snapshot(guild_id)
    service = get_bot_service()
    guild = service.dashboard_overview_snapshot(guild_id, cfg.get("ticket_category_id"))
    if guild is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    channels_by_id = {int(channel["id"]): channel for channel in guild["text_channels"]}
    roles_by_id = {int(role["id"]): role for role in guild["roles"]}

    if request.method == "POST":
        changes = []
        logs_channel_id = request.form.get("logs_channel", "")
        if logs_channel_id.isdigit():
            channel = channels_by_id.get(int(logs_channel_id))
            if channel is not None:
                get_bot_service().api_update_config(guild_id, {"logs_channel": channel["name"]})
                changes.append(f"logs channel → #{channel['name']}")

        autorole_id = request.form.get("autorole", "none")
        risky_perms = ("administrator", "manage_guild", "ban_members", "kick_members", "manage_roles")
        if autorole_id == "none":
            get_bot_service().api_update_config(guild_id, {"autorole": None})
            changes.append("autorole → none")
        elif autorole_id.isdigit():
            role = roles_by_id.get(int(autorole_id))
            if role is not None:
                get_bot_service().api_update_config(guild_id, {"autorole": int(role["id"])})
                changes.append(f"autorole → @{role['name']}")
                if any(role["permissions"].get(permission, False) for permission in risky_perms):
                    flash(f"⚠️ {role['name']}: " + ("sensitive permissions, given to every new member." if current_lang() == "en" else "permissions sensibles, donné à tout nouveau membre."))

        log_toggle = request.form.get("log_dashboard_actions") == "on"
        if log_toggle != bool(get_bot_service().api_config_snapshot(guild_id).get("log_dashboard_actions")):
            get_bot_service().api_update_config(guild_id, {"log_dashboard_actions": log_toggle})
            changes.append(f"Discord logging of dashboard actions → {'on' if log_toggle else 'off'}")

        if changes:
            dash_log_action(guild_id, "Updated general config", "; ".join(changes))

        flash("Configuration updated." if current_lang() == "en" else "Configuration mise à jour.")
        return redirect(url_for("dash_guild_page", guild_id=guild_id))

    # Si l'utilisateur n'a pas déjà choisi une langue pour sa session, on
    # applique la langue par défaut configurée pour CE serveur via
    # /config language (sans écraser un choix explicite déjà fait).
    if "lang" not in session and cfg.get("language") in TRANSLATIONS:
        session["lang"] = cfg["language"]

    is_owner = service.is_guild_owner(guild_id, (session.get("dash_user") or {}).get("id"))
    role_names = {int(role_id): name for role_id, name in guild["role_names"].items()}

    onboarding_missing = []
    if not any(channel["name"] == cfg.get("logs_channel", "logs") for channel in guild["text_channels"]):
        onboarding_missing.append(t("onboard_logs"))
    if not cfg.get("autorole"):
        onboarding_missing.append(t("onboard_autorole"))
    if not cfg.get("tickets_enabled"):
        onboarding_missing.append(t("onboard_tickets"))
    if not cfg.get("appeal_channel_id"):
        onboarding_missing.append(t("onboard_appeals"))
    if not cfg.get("antinuke_enabled"):
        onboarding_missing.append(t("onboard_antinuke"))

    open_tickets = guild["open_tickets"]

    stats_series = get_bot_service().dashboard_sanction_stats_snapshot(guild_id, days=30)
    has_any_activity = any(v for _, v in stats_series)
    sanction_chart = build_sparkline_svg(stats_series) if has_any_activity else ""

    return render_template("dashboard_guild.html",
        guild=guild,
        cfg=cfg,
        channels=guild["text_channels"],
        roles=guild["roles"],
        role_names=role_names,
        moderation_commands=MODERATION_COMMANDS,
        templates=COMMAND_TEMPLATES,
        custom_templates=cfg.get("command_templates", {}),
        audit_entries=get_bot_service().api_audit_snapshot(guild_id) if is_owner else [],
        onboarding_missing=onboarding_missing,
        open_tickets=open_tickets,
        sanction_chart=sanction_chart,
        chart_start=stats_series[0][0] if stats_series else "",
        chart_end=stats_series[-1][0] if stats_series else "",
        is_owner=is_owner,
        is_locked=service.api_health_snapshot(guild_id).get("locked", False),
        user=session.get("dash_user"),
    )


@routes.route("/dashboard/<guild_id>/automod", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_automod(guild_id):
    roles = get_bot_service().dashboard_roles_snapshot(guild_id)
    if roles is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    submitted = request.form.getlist("allowed_roles")
    valid_role_ids = {int(role["id"]) for role in roles}
    # ne garde que des ids qui correspondent à de vrais rôles de CE serveur
    clean = [int(rid) for rid in submitted if rid.isdigit() and int(rid) in valid_role_ids]
    get_bot_service().api_update_config(guild_id, {"allowed_roles": clean})
    role_names = ", ".join(f"@{role['name']}" for role in roles if int(role["id"]) in clean) or "none"
    dash_log_action(guild_id, "Updated anti-raid exemptions", f"exempt roles: {role_names}")
    flash("Anti-raid exemptions updated." if current_lang() == "en" else "Exemptions anti-raid mises à jour.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@routes.route("/dashboard/<guild_id>/permissions/add", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_add(guild_id):
    role_list = get_bot_service().dashboard_roles_snapshot(guild_id)
    if role_list is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    roles = {int(role["id"]): role for role in role_list}
    commands = request.form.getlist("commands")
    role_id = request.form.get("role_id", "")
    valid_commands = [c for c in commands if c in MODERATION_COMMANDS]
    if valid_commands and role_id.isdigit():
        role = roles.get(int(role_id))
        if role is not None:
            for cmd in valid_commands:
                get_bot_service().dashboard_add_command_role(guild_id, cmd, int(role["id"]))
            cmd_list = ", ".join(f"/{c}" for c in valid_commands)
            dash_log_action(guild_id, "Added command permissions", f"@{role['name']}: {cmd_list}")
            if current_lang() == "en":
                flash(f"{role['name']} can now use {len(valid_commands)} command(s): {cmd_list}.")
            else:
                flash(f"{role['name']} peut maintenant utiliser {len(valid_commands)} commande(s) : {cmd_list}.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@routes.route("/dashboard/<guild_id>/permissions/template", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_template(guild_id):
    role_list = get_bot_service().dashboard_roles_snapshot(guild_id)
    if role_list is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    roles = {int(role["id"]): role for role in role_list}
    template_id = request.form.get("template_id", "")
    role_id = request.form.get("role_id", "")
    if not role_id.isdigit():
        return redirect(url_for("dash_guild_page", guild_id=guild_id))
    role = roles.get(int(role_id))
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
        cfg = get_bot_service().api_config_snapshot(guild_id)
        custom_templates = cfg.get("command_templates", {})
        if key in custom_templates:
            commands_list = custom_templates[key]
            label = key

    if commands_list:
        for cmd in commands_list:
            get_bot_service().dashboard_add_command_role(guild_id, cmd, int(role["id"]))
        dash_log_action(guild_id, "Applied permission template", f"template « {label} » → @{role['name']}")
        if current_lang() == "en":
            flash(f"Template « {label} » applied to {role['name']} ({len(commands_list)} commands).")
        else:
            flash(f"Modèle « {label} » appliqué à {role['name']} ({len(commands_list)} commandes).")

    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@routes.route("/dashboard/<guild_id>/permissions/remove", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_permission_remove(guild_id):
    role_list = get_bot_service().dashboard_roles_snapshot(guild_id)
    if role_list is None:
        return jsonify({"error": "Bot not present on this server"}), 403
    roles = {int(role["id"]): role for role in role_list}
    command = request.form.get("command", "")
    role_id = request.form.get("role_id", "")
    # Pas de whitelist ici : la commande vient d'un chip déjà affiché depuis
    # la DB (donc déjà existante), et retirer une permission ne présente
    # aucun risque même pour un nom de commande hors de MODERATION_COMMANDS
    # (ex: ajouté via /config allow avec une casse différente, ou une
    # commande hors de notre liste curatée).
    if command and role_id.isdigit():
        get_bot_service().dashboard_remove_command_role(guild_id, command, int(role_id))
        role = roles.get(int(role_id))
        dash_log_action(guild_id, "Removed command permission", f"/{command} for {'@' + role['name'] if role else role_id}")
        flash(f"Permission removed for /{command}." if current_lang() == "en" else f"Permission retirée pour /{command}.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@routes.route("/dashboard/<guild_id>/apikey/regenerate", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_apikey_regen(guild_id):
    new_key = secrets.token_hex(16)
    get_bot_service().api_update_config(guild_id, {"api_key": new_key})
    dash_log_action(guild_id, "Regenerated mobile API key")
    flash("New API key generated — the old one no longer works." if current_lang() == "en" else "Nouvelle clé API générée — l'ancienne ne fonctionne plus.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@routes.route("/dashboard/<guild_id>/audit/export.csv", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_export_audit_csv(guild_id):
    from flask import Response
    records = get_bot_service().dashboard_audit_export_records(guild_id)
    return Response(
        _build_audit_csv_bytes(records),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_{guild_id}.csv"},
    )


@routes.route("/dashboard/<guild_id>/audit/export.pdf", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_export_audit_pdf(guild_id):
    from flask import Response
    guild_name = get_bot_service().guild_name(guild_id)
    records = get_bot_service().dashboard_audit_export_records(guild_id)
    return Response(
        _build_audit_pdf_bytes(records, guild_name if guild_name != "the server" else guild_id),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=audit_{guild_id}.pdf"},
    )


@routes.route("/dashboard/<guild_id>/lock", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
@dash_owner_required
def dash_toggle_lock(guild_id):
    service = get_bot_service()
    currently_locked = service.api_health_snapshot(guild_id).get("locked", False)
    if not service.set_guild_locked(guild_id, not currently_locked):
        flash("Bot is unavailable; lock state was not changed." if current_lang() == "en" else "Le bot est indisponible ; le verrouillage n'a pas été modifié.", "error")
        return redirect(url_for("dash_guild_page", guild_id=guild_id))
    if currently_locked:
        dash_log_action(guild_id, "Unlocked the bot")
        flash("Bot unlocked." if current_lang() == "en" else "Bot déverrouillé.")
    else:
        dash_log_action(guild_id, "Locked the bot")
        flash("Bot locked — only you can use its commands here." if current_lang() == "en" else "Bot verrouillé — seul toi peux utiliser ses commandes ici.")
    return redirect(url_for("dash_guild_page", guild_id=guild_id))


@routes.route("/dashboard/<guild_id>/cases", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
def dash_cases_page(guild_id):
    guild_name = get_bot_service().guild_name(guild_id)
    filter_type = request.args.get("type", "")
    cases = get_bot_service().api_sanction_cases_snapshot(guild_id, filter_type, 200)
    all_types = get_bot_service().dashboard_sanction_types_snapshot(guild_id)
    return render_template("cases.html", guild_id=guild_id, guild_name=guild_name, cases=cases,
        all_types=all_types, filter_type=filter_type, user=session.get("dash_user"),
    )


@routes.route("/dashboard/<guild_id>/appeals", methods=["GET"])
@dash_login_required
@dash_guild_admin_required
def dash_appeals_page(guild_id):
    guild_name = get_bot_service().guild_name(guild_id)
    appeals = get_bot_service().api_submitted_appeals(guild_id)
    return render_template("appeals_review.html", guild_id=guild_id, guild_name=guild_name,
        appeals=appeals, user=session.get("dash_user"))


@routes.route("/dashboard/<guild_id>/appeals/<token>/accept", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_appeal_accept(guild_id, token):
    appeal = get_bot_service().api_get_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))

    if not get_bot_service().api_claim_appeal(guild_id, token):
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))

    sanction_type = appeal.get("sanction_type", "ban")
    service = get_bot_service()
    success, result = service.api_accept_appeal(guild_id, str(appeal["user_id"]), sanction_type)
    if not success:
        get_bot_service().api_release_appeal_claim(guild_id, token)
        raise RuntimeError(result or "Unable to resolve appeal")
    if not get_bot_service().api_resolve_appeal(guild_id, token, "accepted"):
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))
    actor_id, actor_name = dash_actor()
    get_bot_service().api_record_audit(guild_id, actor_id, actor_name, "Accepted appeal (via dashboard)", f"Case #{appeal['case_id']} ({sanction_type}) — {result}")
    service.api_notify_appeal_result(guild_id, str(appeal["user_id"]), str(appeal["case_id"]), True, result)
    flash(f"Case #{appeal['case_id']} accepted — {result}.")
    return redirect(url_for("dash_appeals_page", guild_id=guild_id))


@routes.route("/dashboard/<guild_id>/appeals/<token>/deny", methods=["POST"])
@dash_login_required
@dash_guild_admin_required
def dash_appeal_deny(guild_id, token):
    appeal = get_bot_service().api_get_appeal(token)
    if not appeal or appeal["guild_id"] != str(guild_id) or appeal["status"] != "submitted":
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))

    if not get_bot_service().api_claim_appeal(guild_id, token):
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))
    if not get_bot_service().api_resolve_appeal(guild_id, token, "denied"):
        flash("This appeal is no longer available.")
        return redirect(url_for("dash_appeals_page", guild_id=guild_id))
    actor_id, actor_name = dash_actor()
    get_bot_service().api_record_audit(guild_id, actor_id, actor_name, "Denied appeal (via dashboard)", f"Case #{appeal['case_id']}")
    get_bot_service().api_notify_appeal_result(
        guild_id, str(appeal["user_id"]), str(appeal["case_id"]), False, None
    )
    flash(f"Case #{appeal['case_id']} denied.")
    return redirect(url_for("dash_appeals_page", guild_id=guild_id))
