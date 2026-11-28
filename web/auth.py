"""web.auth -- connexion au dashboard via OAuth2 Discord, et garde-fous des pages admin.

Extrait de main.py (étape 2b-2a). `dash_login_required` et `dash_guild_admin_required` sont
réutilisés par toutes les pages /dashboard/*. Ce module n'importe JAMAIS main.py (le bot
vient de `core.bot`). Les routes sont enregistrées sur l'app par main.py via
`routes.register(api)` en gardant les noms d'endpoints d'origine (cf. web/routing.py).
"""
from functools import wraps
import math
import re
import secrets
import time
from urllib.parse import urlencode
import importlib

from flask import current_app, jsonify, redirect, request, session, url_for

from web.oauth_utils import OAUTH_STATE_MAX_AGE, create_code_challenge, is_fresh_state_timestamp, is_valid_code_verifier, is_valid_oauth_code
from web.bot_service import get_bot_service
from web.routing import Routes

routes = Routes()


class OAuthTransportError(Exception):
    """A network failure while contacting Discord's OAuth or identity API."""


class _LazyRequestsTransport:
    """Load requests only when the default non-Worker transport is actually used."""

    @staticmethod
    def _request(method, *args, **kwargs):
        try:
            requests_module = importlib.import_module("requests")
        except ImportError as exc:
            raise OAuthTransportError("No Discord HTTP transport is configured") from exc
        try:
            return getattr(requests_module, method)(*args, **kwargs)
        except requests_module.RequestException as exc:
            raise OAuthTransportError("Discord HTTP request failed") from exc

    def get(self, *args, **kwargs):
        return self._request("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._request("post", *args, **kwargs)


# Kept as a patchable compatibility seam for the existing Requests-based tests.
requests = _LazyRequestsTransport()


def _oauth_http_client():
    """Return an app-injected transport or the lazy Requests compatibility client."""
    client = current_app.config.get("DISCORD_HTTP_CLIENT")
    return requests if client is None else client


def _oauth_config():
    """Resolve OAuth credentials exclusively from the current Flask app."""
    return (
        current_app.config.get("DISCORD_CLIENT_ID", ""),
        current_app.config.get("DISCORD_CLIENT_SECRET", ""),
        current_app.config.get("DISCORD_REDIRECT_URI", ""),
    )
DISCORD_API_BASE = "https://discord.com/api"
ADMINISTRATOR_PERM = 0x8
GUILD_ID_RE = re.compile(r"^\d{17,20}$")  # valide un vrai snowflake Discord


def _session_backend_action(action):
    """Invoke an optional lifecycle hook on an injected server-side session backend.

    Flask's default signed-cookie interface intentionally has no such hooks.
    A durable backend can implement rotate_session(session) and
    revoke_session(session) to invalidate opaque IDs at the storage layer.
    """
    callback = getattr(current_app.session_interface, action, None)
    if callable(callback):
        callback(session)


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
            try:
                _session_backend_action("revoke_session")
            except Exception:
                # Credentials must not survive a failed backend revocation.
                pass
            finally:
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
        response = _oauth_http_client().get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except OAuthTransportError:
        return None

    if response.status_code == 401:
        try:
            _session_backend_action("revoke_session")
        except Exception:
            # A backend outage must not keep an invalid token in the session.
            pass
        finally:
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
    client_id, _client_secret, redirect_uri = _oauth_config()
    if not client_id or not redirect_uri:
        return jsonify({"error": "Discord OAuth is not configured"}), 503
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = create_code_challenge(code_verifier)
    session["oauth_state"] = state
    session["oauth_state_created_at"] = time.time()
    session["oauth_code_verifier"] = code_verifier
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
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
    state_is_fresh = is_fresh_state_timestamp(state_created_at, now)
    verifier_is_valid = is_valid_code_verifier(code_verifier)
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
    if not is_valid_oauth_code(code):
        return jsonify({"error": "Invalid code"}), 400

    client_id, client_secret, redirect_uri = _oauth_config()
    if not all((client_id, client_secret, redirect_uri)):
        session.clear()
        return jsonify({"error": "Discord OAuth is not configured"}), 503

    # Consume the one-time state and discard any pre-authentication session data.
    # A fresh session is created only after Discord has authenticated the user.
    session.clear()

    try:
        token_r = _oauth_http_client().post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
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

        # Refresh tokens are not used by the dashboard; never persist them in the browser session.

        expires_in = token_data.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(expires_in)
            or expires_in <= 0
        ):
            return jsonify({"error": "Invalid token expiration"}), 502

        user_r = _oauth_http_client().get(
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
    except (OAuthTransportError, ValueError):
        return jsonify({"error": "Discord authentication is temporarily unavailable"}), 502

    # Rotate any pre-authentication identifier only after both OAuth exchange
    # and identity lookup succeed. Server-side backends must invalidate the old
    # opaque ID before persisting the authenticated session.
    _session_backend_action("rotate_session")

    # Store credentials only after both OAuth exchange and identity lookup succeed.
    session["access_token"] = access_token
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
    try:
        _session_backend_action("revoke_session")
    except Exception:
        # Never leave credentials in a session after logout invalidation fails.
        session.clear()
        return jsonify({"error": "Unable to revoke session. Please retry."}), 503
    session.clear()
    return redirect(url_for("dash_login"))
