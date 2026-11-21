"""web.verify -- page publique de vérification Cloudflare Turnstile (/verify/<jeton>).

Le jeton de l'URL est l'unique credential : lié à un serveur + un membre, 10 minutes, usage unique,
3 échecs maximum. Aucune session ni connexion Discord n'est nécessaire. Ce module n'importe JAMAIS
main.py (bot -> `core.bot`).
"""
from flask import make_response, render_template, request

from audit import record_audit
from bridge import run_coroutine
import core
from verification import (MAX_ATTEMPTS, check_turnstile, get_valid_challenge, grant_verified, mark_used,
                          record_failed_attempt, site_key, turnstile_configured)
from web.public import get_client_ip
from web.routing import Routes

routes = Routes()


def _page(template, status=200, **ctx):
    resp = make_response(render_template(template, **ctx), status)
    resp.headers["Cache-Control"] = "no-store"        # la page contient un lien personnel
    resp.headers["Referrer-Policy"] = "no-referrer"   # le jeton de l'URL ne part pas dans le Referer
    return resp


def _message(title, message, status=200):
    return _page("verify_message.html", status, title=title, message=message)


def _form(doc, status=200, error=None):
    guild = core.bot.get_guild(int(doc["guild_id"]))
    return _page("verify.html", status, site_key=site_key(), error=error,
                 guild_name=guild.name if guild else "the server",
                 attempts_left=max(0, MAX_ATTEMPTS - doc.get("attempts", 0)))


@routes.route("/verify/<token>", methods=["GET"])
def verify_page(token):
    if not turnstile_configured():
        return _message("Verification unavailable", "Web verification isn't enabled on this bot.", 503)
    doc = get_valid_challenge(token)
    if doc is None:
        return _message("Link expired", "This verification link is invalid, expired or already used. "
                                        "Go back to Discord and click the Verify button again.", 410)
    return _form(doc)


@routes.route("/verify/<token>", methods=["POST"])
def verify_submit(token):
    if not turnstile_configured():
        return _message("Verification unavailable", "Web verification isn't enabled on this bot.", 503)
    doc = get_valid_challenge(token)
    if doc is None:
        return _message("Link expired", "This verification link is invalid, expired or already used. "
                                        "Go back to Discord and click the Verify button again.", 410)

    response_token = request.form.get("cf-turnstile-response", "").strip()
    if not response_token:  # widget pas encore résolu : on ne consomme pas d'essai
        return _form(doc, 400, "Please complete the check before continuing.")

    ok, reason = check_turnstile(response_token, get_client_ip())
    if not ok:
        if reason == "service_unavailable":  # panne côté Cloudflare : pas la faute du membre
            return _form(doc, 503, "The verification service is unreachable. Please try again in a moment.")
        attempts = record_failed_attempt(token)
        if attempts >= MAX_ATTEMPTS:
            return _message("Too many attempts", "This link has been disabled. Go back to Discord and click "
                                                 "the Verify button again.", 410)
        doc["attempts"] = attempts
        return _form(doc, 400, "The check failed. Please try again.")

    if not mark_used(token):  # deux requêtes simultanées : une seule passe
        return _message("Link expired", "This verification link was already used.", 410)

    guild = core.bot.get_guild(int(doc["guild_id"]))
    if guild is None:
        return _message("Verification failed", "The server isn't available right now. Please try again later.", 500)
    try:
        granted, error = run_coroutine(grant_verified(guild, doc["user_id"]))
    except Exception as e:
        print(f"[VERIFY] grant_verified failed: {e}", flush=True)
        granted, error = False, "Something went wrong while giving you access. Please contact a moderator."
    if not granted:
        return _message("Verification failed", error, 500)

    record_audit(doc["guild_id"], doc["user_id"], "Member (web verification)", "Verified via Cloudflare Turnstile")
    return _message("You're verified ✅", "You can close this tab and go back to Discord: the server is now unlocked.")
