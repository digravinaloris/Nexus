"""web.auth -- connexion au dashboard via OAuth2 Discord, et garde-fous des pages admin.

Extrait de main.py (étape 2b-2a). `dash_login_required` et `dash_guild_admin_required` sont
réutilisés par toutes les pages /dashboard/*. Ce module n'importe JAMAIS main.py (le bot
vient de `core.bot`). Les routes sont enregistrées sur l'app par main.py via
`routes.register(api)` en gardant les noms d'endpoints d'origine (cf. web/routing.py).
"""
from functools import wraps
import math
import os
import re
import secrets
import time
from urllib.parse import urlencode

from flask import jsonify, redirect, request, session, url_for
import requests

import core
from web.routing import Routes

routes = Routes()


DASH_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DASH_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
DASH_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]
DISCORD_API_BASE = "https://discord.com/api"
ADMINISTRATOR_PERM = 0x8
GUILD_ID_RE = re.compile(r"^\d{17,19}$")  # valide un vrai snowflake Discord


def dash_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        expires_at = session.get("token_expires_at")
        if (
            not isinstance(session.get("access_token"), str)
            or not session.get("access_token")
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at)
            or time.time() >= expires_at
        ):
            session.clear()
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
        if not core.bot.is_ready() or not core.bot.get_guild(int(guild_id)):
            return jsonify({"error": "Bot not present on this server"}), 403
        return f(guild_id, *args, **kwargs)
    return wrapper


@routes.route("/login")
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
    query = urlencode(params)
    return redirect(f"{DISCORD_API_BASE}/oauth2/authorize?{query}")


@routes.route("/callback")
def dash_callback():
    state = request.args.get("state")
    expected_state = session.pop("oauth_state", None)
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return jsonify({"error": "Invalid state"}), 403

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing code"}), 400
    if len(code) > 2048:
        return jsonify({"error": "Invalid code"}), 400

    # Consume the one-time state and discard any pre-authentication session data.
    # A fresh session is created only after Discord has authenticated the user.
    session.clear()

    try:
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
        access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
        if not isinstance(access_token, str) or not access_token:
            return jsonify({"error": "Invalid token response"}), 502

        expires_in = token_data.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(expires_in)
            or expires_in <= 0
        ):
            return jsonify({"error": "Invalid token expiration"}), 502

        user_r = requests.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if user_r.status_code != 200:
            return jsonify({"error": "Discord user lookup failed"}), 502

        user_data = user_r.json()
        if not isinstance(user_data, dict) or not user_data.get("id"):
            return jsonify({"error": "Invalid Discord user response"}), 502
    except (requests.RequestException, ValueError):
        return jsonify({"error": "Discord authentication is temporarily unavailable"}), 502

    # Store credentials only after both OAuth exchange and identity lookup succeed.
    session["access_token"] = access_token
    refresh_token = token_data.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        session["refresh_token"] = refresh_token
    session["token_expires_at"] = time.time() + min(expires_in, 60 * 60 * 24)
    session["dash_user"] = user_data
    session["csrf_token"] = secrets.token_urlsafe(32)

    return redirect(url_for("dash_home"))


@routes.route("/logout", methods=["POST"])
def dash_logout():
    token = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not token or not expected or not secrets.compare_digest(token, expected):
        return jsonify({"error": "Invalid CSRF token"}), 403
    session.clear()
    return redirect(url_for("dash_login"))
