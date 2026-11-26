"""Session-backed CSRF protection for authenticated dashboard actions."""
import hmac
import re
import secrets

from flask import abort, request, session


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


def generate_csrf_token():
    """Return the current session token, creating one when needed."""
    token = session.get("csrf_token")
    if not isinstance(token, str) or not _CSRF_TOKEN_RE.fullmatch(token):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf_request():
    """Reject cross-site dashboard mutations without a matching session token."""
    if not request.path.startswith("/dashboard/") or request.method not in _UNSAFE_METHODS:
        return None

    expected = session.get("csrf_token")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if (
        not isinstance(expected, str)
        or not isinstance(supplied, str)
        or not _CSRF_TOKEN_RE.fullmatch(expected)
        or not _CSRF_TOKEN_RE.fullmatch(supplied)
        or not hmac.compare_digest(expected, supplied)
    ):
        abort(400, description="Invalid or missing CSRF token")
    return None


def register_csrf(app):
    """Install CSRF validation and expose a token to dashboard templates."""
    app.before_request(validate_csrf_request)

    @app.context_processor
    def inject_csrf_token():
        return {"csrf_token": generate_csrf_token()}
