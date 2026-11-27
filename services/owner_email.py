"""Owner email delivery and daily kill-switch control messages."""
import os

import requests

from stores.config_store import get_global_lock
from stores.killswitch_store import create_killswitch_token


def send_email(subject, body):
    """Send an owner email through Resend without blocking the async event loop.

    Callers should run this synchronous helper through asyncio.to_thread.
    Returns (success: bool, error_message: str | None).
    """
    resend_api_key = os.getenv("RESEND_API_KEY")
    owner_email = os.getenv("OWNER_EMAIL")
    if not (resend_api_key and owner_email):
        error = "Missing RESEND_API_KEY / OWNER_EMAIL env var(s)."
        print(f"[EMAIL] {error}", flush=True)
        return False, error
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_api_key}"},
            json={
                "from": "Nexus <onboarding@resend.dev>",
                "to": [owner_email],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        if response.status_code >= 400:
            error = f"Resend API returned {response.status_code}: {response.text[:300]}"
            print(f"[EMAIL] {error}", flush=True)
            return False, error
        return True, None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[EMAIL] Failed to send: {error}", flush=True)
        return False, error


def send_daily_killswitch_email():
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        error = "PUBLIC_BASE_URL not set, cannot build the confirmation link."
        print(f"[KILLSWITCH] {error}", flush=True)
        return False, error
    token = create_killswitch_token()
    link = f"{base_url}/admin/kill-switch/{token}"
    locked = get_global_lock()
    state_txt = "currently LOCKED down" if locked else "currently active (unlocked)"
    action_txt = "unlock it" if locked else "lock it down"
    body = (
        "Nexus — daily control link\n\n"
        f"The bot is {state_txt} across all servers.\n\n"
        f"If you need to {action_txt}, open this link and enter your authenticator code:\n{link}\n\n"
        "This link expires in 30 minutes and can only be used once.\n"
        "Nothing happens if you ignore this email."
    )
    return send_email("Nexus — daily control link", body)
