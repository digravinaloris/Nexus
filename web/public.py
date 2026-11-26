"""web.public -- pages publiques : statut JSON (/), favicon, /status, kill-switch par e-mail
(+ code TOTP) et formulaire public d'appel de sanction.

Extrait de main.py (étape 2b-2a). Ce module n'importe JAMAIS main.py (bot -> `core.bot`,
Mongo -> `core.mongo` / `core.col`). Routes enregistrées par main.py via `routes.register(api)`.
"""
import datetime
import os
import time

from flask import jsonify, render_template, request, send_from_directory
import pyotp

from audit import record_audit
from config_store import get_global_lock, set_global_lock
from sanctions_store import get_ban_appeal, submit_ban_appeal
import utils
from web.bot_service import get_bot_service
from killswitch_store import get_killswitch_token, increment_killswitch_attempts, mark_killswitch_token_used
from web.routing import Routes

routes = Routes()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # racine du repo (Nexus.png)


@routes.route('/')
def home():
    # Reste toujours 200 (UptimeRobot et l'hébergement s'en servent comme
    # simple ping). L'adaptateur conserve le format JSON historique et isole
    # la route de l'instance Discord/Mongo du processus web.
    return jsonify(get_bot_service().health_snapshot())


@routes.route('/favicon.ico')
def favicon():
    # Nexus.png vit à la racine du repo (à côté de main.py), pas dans un
    # dossier static/ -- send_from_directory pointe dessus directement.
    return send_from_directory(ROOT_DIR, "Nexus.png", mimetype="image/png")


def _killswitch_token_doc(token):
    doc = get_killswitch_token(token)
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


@routes.route("/admin/kill-switch/<token>", methods=["GET"])
def killswitch_confirm_page(token):
    ip = get_client_ip()
    if _killswitch_token_doc(token) is None:
        record_audit("global", None, f"Unknown (IP {ip})", "Viewed an invalid/expired kill-switch link")
        return render_template("killswitch_invalid.html"), 410
    record_audit("global", None, f"Unknown (IP {ip})", "Opened the kill-switch confirmation page")
    return render_template("killswitch_confirm.html", locked=get_global_lock(), error=False)


@routes.route("/admin/kill-switch/<token>", methods=["POST"])
def killswitch_submit(token):
    ip = get_client_ip()
    doc = _killswitch_token_doc(token)
    if doc is None:
        return render_template("killswitch_invalid.html"), 410

    code = request.form.get("code", "").strip()
    totp_secret = os.getenv("TOTP_SECRET")
    valid = bool(totp_secret) and bool(code) and pyotp.TOTP(totp_secret).verify(code, valid_window=1)

    if not valid:
        new_attempts = increment_killswitch_attempts(token)
        if new_attempts is None:
            return render_template("killswitch_invalid.html"), 410
        attempts_left = max(0, 5 - new_attempts)
        record_audit("global", None, f"Unknown (IP {ip})", "Failed kill-switch code attempt", f"{new_attempts}/5 attempts used")
        if new_attempts >= 5:
            try:
                get_bot_service().notify_owner_bruteforce(ip)
            except Exception as e:
                print(f"[KILLSWITCH] Failed to DM owner about brute-force: {e}", flush=True)
        return render_template("killswitch_confirm.html", locked=get_global_lock(), error=True, attempts_left=attempts_left
        )

    # Code correct : le token est consommé immédiatement (usage unique),
    # avant même de faire quoi que ce soit d'autre.
    if not mark_killswitch_token_used(token):
        return render_template("killswitch_invalid.html"), 410
    new_state = not get_global_lock()
    set_global_lock(new_state)
    record_audit("global", None, f"Owner (email + authenticator, IP {ip})", "Locked bot globally" if new_state else "Unlocked bot globally")
    try:
        get_bot_service().notify_owner_killswitch(new_state)
    except Exception as e:
        print(f"[KILLSWITCH] Failed to DM owner: {e}", flush=True)

    return render_template("killswitch_done.html", locked=new_state)


APPEAL_STATUS_COPY = {
    "submitted": ("Already submitted", "Your appeal has already been submitted and is awaiting review."),
    "accepted": ("Appeal accepted", "Your appeal was accepted — you should already be unbanned."),
    "denied": ("Appeal denied", "Your appeal was reviewed and denied."),
}


@routes.route("/appeal/<token>", methods=["GET"])
def appeal_form_page(token):
    appeal = get_ban_appeal(token)
    if not appeal:
        return render_template("appeal_invalid.html"), 404
    if appeal["status"] != "pending":
        title, message = APPEAL_STATUS_COPY.get(appeal["status"], ("Status", "This appeal has already been processed."))
        return render_template("appeal_status.html", title=title, message=message)
    guild_name = get_bot_service().guild_name(appeal["guild_id"])
    return render_template("appeal_form.html",
        guild_name=guild_name,
        case_id=appeal["case_id"],
        ban_reason=appeal["ban_reason"] or "No reason provided",
    )


@routes.route("/appeal/<token>", methods=["POST"])
def appeal_form_submit(token):
    appeal = get_ban_appeal(token)
    if not appeal or appeal["status"] != "pending":
        return render_template("appeal_invalid.html"), 404

    appeal_text = request.form.get("appeal_text", "").strip()[:1000]
    guild_name = get_bot_service().guild_name(appeal["guild_id"])
    if not appeal_text:
        return render_template("appeal_form.html",
            guild_name=guild_name,
            case_id=appeal["case_id"],
            ban_reason=appeal["ban_reason"] or "No reason provided",
        )

    submitted = submit_ban_appeal(token, appeal_text, datetime.datetime.now(datetime.timezone.utc))
    if not submitted:
        title, message = APPEAL_STATUS_COPY["submitted"]
        return render_template("appeal_status.html", title=title, message=message)

    try:
        posted, error = get_bot_service().api_post_appeal_for_review(
            appeal["guild_id"], appeal, appeal_text
        )
        if not posted:
            print(f"[APPEAL] Failed to post appeal for review: {error}", flush=True)
    except Exception as e:
        print(f"[APPEAL] Failed to post appeal for review: {e}", flush=True)

    return render_template("appeal_done.html", case_id=appeal["case_id"])


_format_uptime = utils.format_uptime


@routes.route("/status")
def public_status_page():
    snapshot = get_bot_service().public_status_snapshot()
    if not snapshot["online"]:
        return render_template("status_page.html", online=False), 503
    return render_template("status_page.html",
        online=True,
        guild_count=snapshot["guild_count"],
        latency_ms=snapshot["latency_ms"],
        uptime=snapshot["uptime"],
    )
