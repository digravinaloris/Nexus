"""Flask application construction, independent from the Discord bot lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from flask import Flask, request

from i18n import current_lang, current_theme, t
from web import auth as web_auth
from web import dashboard as web_dashboard
from web import github_webhook
from web import public as web_public
from web import verify as web_verify
from web.csrf import register_csrf
from web.rest_api import bp as rest_api_bp


def create_web_app(
    *,
    config: Optional[Mapping[str, object]] = None,
    environ: Optional[Mapping[str, str]] = None,
    template_folder: Optional[str] = None,
    bot_service=None,
    internal_bot_service=None,
    internal_bot_service_keyring=None,
    internal_nonce_store=None,
    session_interface=None,
) -> Flask:
    """Build the existing Nexus web surface without importing or starting main.py.

    Bot-dependent work remains behind the BotService boundary and is performed
    only when a request actually needs it. Runtime settings may be supplied
    through an explicit environment mapping or Flask config.
    """
    runtime_env = os.environ if environ is None else environ
    root_dir = Path(__file__).resolve().parent.parent
    app = Flask(
        "",
        template_folder=template_folder or str(root_dir / "templates",
    ))
    app.config.update(
        SECRET_KEY=runtime_env.get("FLASK_SECRET_KEY"),
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        DISCORD_CLIENT_ID=runtime_env.get("DISCORD_CLIENT_ID", ""),
        DISCORD_CLIENT_SECRET=runtime_env.get("DISCORD_CLIENT_SECRET", ""),
        DISCORD_REDIRECT_URI=runtime_env.get("DISCORD_REDIRECT_URI", ""),
        API_KEY=runtime_env.get("API_KEY"),
    )
    if config:
        app.config.update(config)
    if session_interface is not None:
        app.session_interface = session_interface
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("A Flask SECRET_KEY must be configured before creating the web app")
    if bot_service is not None:
        app.extensions["nexus_bot_service"] = bot_service

    internal_options = (
        internal_bot_service,
        internal_bot_service_keyring,
        internal_nonce_store,
    )
    if any(option is not None for option in internal_options):
        if not all(option is not None for option in internal_options):
            raise ValueError(
                "Internal BotService registration requires a service, keyring, and nonce store"
            )
        from web.internal_bot_service import create_internal_bot_service_blueprint

        app.register_blueprint(
            create_internal_bot_service_blueprint(
                service=internal_bot_service,
                keyring=internal_bot_service_keyring,
                nonce_store=internal_nonce_store,
            )
        )

    app.register_blueprint(rest_api_bp)
    web_auth.routes.register(app)
    web_public.routes.register(app)
    web_dashboard.routes.register(app)
    web_verify.routes.register(app)
    github_webhook.routes.register(app)
    register_csrf(app)

    @app.after_request
    def add_security_headers(response):
        """Apply baseline browser security headers to every web response."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "base-uri 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        if (
            request.path in {"/login", "/callback", "/logout"}
            or request.path == "/dashboard"
            or request.path.startswith("/dashboard/")
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.jinja_env.globals["t"] = t
    app.jinja_env.globals["lang"] = current_lang
    app.jinja_env.globals["theme"] = current_theme
    return app
