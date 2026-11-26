"""web.auth -- connexion au dashboard via OAuth2 Discord, et garde-fous des pages admin.

Extrait de main.py (étape 2b-2a). `dash_login_required` et `dash_guild_admin_required` sont
réutilisés par toutes les pages /dashboard/*. Ce module n'importe JAMAIS main.py (le bot
vient de `core.bot`). Les routes sont enregistrées sur l'app par main.py via
`routes.register(api)` en gardant les noms d'endpoints d'origine (cf. web/routing.py).
"""
from functools import wraps
import base64
import hashlib
import math
import os
import re
import secrets
import time
from urllib.parse import urlencode

from flask import jsonify, redirect, request, session, url_for
import requests

from web.bot_service import get_bot_service
from web.routing import Routes

routes = Routes()


DASH_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DASH_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
DASH_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]
DISCORD_API_BASE = "https://discord.com/api"
ADMINISTRATOR_PERM = 0x8
OAUTH_STATE_MAX_AGE = 600
GUILD_ID_RE = re.compile(r"^\d{17,20}$")  # valide un vrai snowflake Discord


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
    """Fetch current Discord permissions for every dashboard authorization check.

    A cached guild list can keep a user authorized after they lose Administrator
    access. If Discord cannot confirm the current list, fail closed instead of
    falling back to stale permissions.
    """
    access_token = session.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None

    try:
        response = requests.get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException:
        return None

    if response.status_code == 401:
        session.clear()
        return None
    if response.status_code != 200:
        return None

    try:
        guilds = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(guilds, list):
        return None

    admin_guilds = []
    for guild in guilds:
        if not isinstance(guild, dict):
            continue
        guild_id = guild.get("id")
        permissions = guild.get("permissions")
        if not isinstance(guild_id, str) or not GUILD_ID_RE.fullmatch(guild_id):
            continue
        if isinstance(permissions, bool):
            continue
        if isinstance(permissions, str):
            if not permissions.isdecimal():
                continue
            permissions = int(permissions)
        elif not isinstance(permissions, int):
            continue
        if permissions & ADMINISTRATOR_PERM == ADMINISTRATOR_PERM:
            admin_guilds.append(guild)

    return admin_guilds


def dash_guild_admin_required(f):
    """Vérifie que guild_id dans l'URL est un serveur où l'user est admin ET où le bot est présent."""
    @wraps(f)
    def wrapper(guild_id, *args, **kwargs):
        if not GUILD_ID_RE.match(guild_id):
            return jsonify({"error": "Invalid guild id"}), 400
        admin_guilds = get_user_admin_guilds()
        if admin_guilds is None:
            if session.get("access_token"):
                return jsonify({"error": "Unable to verify Discord server permissions. Please try again shortly."}), 503
            return redirect(url_for("dash_login"))
        admin_guild_ids = {g_["id"] for g_ in admin_guilds}
        if guild_id not in admin_guild_ids:
            return jsonify({"error": "Forbidden"}), 403
        presence = get_bot_service().guild_presence(guild_id)
        if not presence["bot_ready"] or not presence["guild_present"]:
            return jsonify({"error": "Bot not present on this server"}), 403
        return f(guild_id, *args, **kwargs)
    return wrapper


@routes.route("/login")
def dash_login():
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    session["oauth_state"] = state
    session["oauth_state_created_at"] = time.time()
    session["oauth_code_verifier"] = code_verifier
    params = {
        "client_id": DASH_CLIENT_ID,
        "redirect_uri": DASH_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    query = urlencode(params)
    return redirect(f"{DISCORD_API_BASE}/oauth2/authorize?{query}")


@routes.route("/callback")
def dash_callback():
    # OAuth parameters must be unambiguous. Reject parameter-pollution attempts
    # instead of silently trusting the first value supplied by the client.
    states = request.args.getlist("state")
    if len(states) != 1:
        session.pop("oauth_state", None)
        session.pop("oauth_state_created_at", None)
        session.pop("oauth_code_verifier", None)
        return jsonify({"error": "Invalid state"}), 403
    state = states[0]
    expected_state = session.pop("oauth_state", None)
    state_created_at = session.pop("oauth_state_created_at", None)
    code_verifier = session.pop("oauth_code_verifier", None)
    now = time.time()
    state_is_fresh = (
        not isinstance(state_created_at, bool)
        and isinstance(state_created_at, (int, float))
        and math.isfinite(state_created_at)
        and -30 <= now - state_created_at <= OAUTH_STATE_MAX_AGE
    )
    verifier_is_valid = (
        isinstance(code_verifier, str)
        and 43 <= len(code_verifier) <= 128
        and re.fullmatch(r"[A-Za-z0-9._~-]+", code_verifier) is not None
    )
    if (
        not isinstance(state, str)
        or not state
        or not isinstance(expected_state, str)
        or not expected_state
        or not state_is_fresh
        or not secrets.compare_digest(state, expected_state)
        or not verifier_is_valid
    ):
        return jsonify({"error": "Invalid state"}), 403

    codes = request.args.getlist("code")
    if not codes:
        return jsonify({"error": "Missing code"}), 400
    if len(codes) != 1:
        return jsonify({"error": "Invalid code"}), 400
    code = codes[0]
    if (
        not isinstance(code, str)
        or len(code) > 2048
        or re.search(r"[\x00-\x20\x7f]", code) is not None
    ):
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
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if token_r.status_code != 200:
            return jsonify({"error": "Token exchange failed"}), 400

        token_data = token_r.json()
        access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
        # Discord's bearer token is opaque, but it must be bounded and safe for
        # an HTTP Authorization header. Reject whitespace and control characters.
        if (
            not isinstance(access_token, str)
            or len(access_token) > 4096
            or re.fullmatch(r"[A-Za-z0-9._~+/=-]+", access_token) is None
        ):
            return jsonify({"error": "Invalid token response"}), 502

        refresh_token = token_data.get("refresh_token")
        if refresh_token is not None and (
            not isinstance(refresh_token, str)
            or not refresh_token
            or len(refresh_token) > 4096
            or re.fullmatch(r"[A-Za-z0-9._~+/=-]+", refresh_token) is None
        ):
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
        user_id = user_data.get("id") if isinstance(user_data, dict) else None
        if not isinstance(user_id, str) or not re.fullmatch(r"\d{17,20}", user_id):
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
    if (
        not isinstance(token, str)
        or not isinstance(expected, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is None
        or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", expected) is None
        or not secrets.compare_digest(token, expected)
    ):
        return jsonify({"error": "Invalid CSRF token"}), 403
    session.clear()
    return redirect(url_for("dash_login"))
