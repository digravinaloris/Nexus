"""web.verify -- public Cloudflare Turnstile verification page.

The URL token is the only credential: bound to a guild and member, valid for
10 minutes, single-use, with at most three failed attempts. No Discord login
or browser session is required.
"""
from flask import current_app, make_response, render_template, request

from turnstile import MAX_ATTEMPTS, check_turnstile, turnstile_configured
from web.public import get_client_ip
from web.bot_service import get_bot_service
from web.routing import Routes

routes = Routes()


def _turnstile_settings():
    config = current_app.config
    return (
        config.get("TURNSTILE_SITE_KEY", ""),
        config.get("TURNSTILE_SECRET_KEY", ""),
        config.get("PUBLIC_BASE_URL", ""),
    )


def _turnstile_is_configured():
    site, secret, base_url = _turnstile_settings()
    return turnstile_configured(site, secret, base_url)


def _page(template, status=200, **ctx):
    resp = make_response(render_template(template, **ctx), status)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _message(title, message, status=200):
    return _page("verify_message.html", status, title=title, message=message)


def _form(doc, status=200, error=None):
    site, _, _ = _turnstile_settings()
    return _page("verify.html", status, site_key=site, error=error,
                 guild_name=get_bot_service().guild_name(doc["guild_id"]),
                 attempts_left=max(0, doc.get("max_attempts", MAX_ATTEMPTS) - doc.get("attempts", 0)))


@routes.route("/verify/<token>", methods=["GET"])
def verify_page(token):
    if not _turnstile_is_configured():
        return _message("Verification unavailable", "Web verification isn't enabled on this bot.", 503)
    doc = get_bot_service().verify_get_valid_challenge(token)
    if doc is None:
        return _message("Link expired", "This verification link is invalid, expired or already used. "
                                        "Go back to Discord and click the Verify button again.", 410)
    return _form(doc)


@routes.route("/verify/<token>", methods=["POST"])
def verify_submit(token):
    if not _turnstile_is_configured():
        return _message("Verification unavailable", "Web verification isn't enabled on this bot.", 503)
    doc = get_bot_service().verify_get_valid_challenge(token)
    if doc is None:
        return _message("Link expired", "This verification link is invalid, expired or already used. "
                                        "Go back to Discord and click the Verify button again.", 410)

    response_token = request.form.get("cf-turnstile-response", "").strip()
    if not response_token:
        return _form(doc, 400, "Please complete the check before continuing.")

    _, secret, _ = _turnstile_settings()
    ok, reason = check_turnstile(
        response_token,
        get_client_ip(),
        http_client=current_app.config.get("TURNSTILE_HTTP_CLIENT"),
        secret_key=secret,
    )
    if not ok:
        if reason == "service_unavailable":
            return _form(doc, 503, "The verification service is unreachable. Please try again in a moment.")
        attempts = get_bot_service().verify_record_failed_attempt(token)
        if attempts >= doc.get("max_attempts", MAX_ATTEMPTS):
            return _message("Too many attempts", "This link has been disabled. Go back to Discord and click "
                                                 "the Verify button again.", 410)
        doc["attempts"] = attempts
        return _form(doc, 400, "The check failed. Please try again.")

    if not get_bot_service().verify_mark_used(token):
        return _message("Link expired", "This verification link was already used.", 410)

    try:
        granted, error = get_bot_service().api_grant_verified(doc["guild_id"], doc["user_id"])
    except Exception as e:
        print(f"[VERIFY] grant_verified failed: {e}", flush=True)
        granted, error = False, "Something went wrong while giving you access. Please contact a moderator."
    if not granted:
        return _message("Verification failed", error, 500)

    get_bot_service().api_record_audit(doc["guild_id"], doc["user_id"], "Member (web verification)", "Verified via Cloudflare Turnstile")
    return _message("You're verified ✅", "You can close this tab and go back to Discord: the server is now unlocked.")
