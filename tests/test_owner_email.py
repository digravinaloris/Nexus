"""Tests for owner email delivery and daily kill-switch messages."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import owner_email


def test_send_email_requires_both_environment_variables(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.test")
    called = []
    monkeypatch.setattr(owner_email.requests, "post", lambda *a, **k: called.append((a, k)))

    success, error = owner_email.send_email("subject", "body")

    assert success is False
    assert error == "Missing RESEND_API_KEY / OWNER_EMAIL env var(s)."
    assert called == []


def test_send_email_posts_to_resend(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.test")
    calls = {}
    monkeypatch.setattr(
        owner_email.requests,
        "post",
        lambda url, **kwargs: calls.update(url=url, **kwargs) or type("Response", (), {"status_code": 200})(),
    )

    assert owner_email.send_email("Subject", "Message") == (True, None)
    assert calls["url"] == "https://api.resend.com/emails"
    assert calls["headers"] == {"Authorization": "Bearer test-key"}
    assert calls["json"]["to"] == ["owner@example.test"]
    assert calls["json"]["subject"] == "Subject"
    assert calls["json"]["text"] == "Message"
    assert calls["timeout"] == 15


def test_send_email_reports_http_errors_and_bounds_response_text(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.test")
    monkeypatch.setattr(
        owner_email.requests,
        "post",
        lambda *a, **k: type("Response", (), {"status_code": 503, "text": "x" * 400})(),
    )

    success, error = owner_email.send_email("Subject", "Message")

    assert success is False
    assert error.startswith("Resend API returned 503: ")
    assert len(error.split(": ", 1)[1]) == 300


def test_send_daily_killswitch_email_requires_public_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    called = []
    monkeypatch.setattr(owner_email, "create_killswitch_token", lambda: called.append("token"))
    assert owner_email.send_daily_killswitch_email() == (
        False,
        "PUBLIC_BASE_URL not set, cannot build the confirmation link.",
    )
    assert called == []


def test_send_daily_killswitch_email_uses_current_lock_state(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://nexus.example/")
    monkeypatch.setattr(owner_email, "create_killswitch_token", lambda: "one-time-token")
    monkeypatch.setattr(owner_email, "get_global_lock", lambda: True)
    sent = {}
    monkeypatch.setattr(owner_email, "send_email", lambda subject, body: sent.update(subject=subject, body=body) or (True, None))

    assert owner_email.send_daily_killswitch_email() == (True, None)
    assert sent["subject"] == "Nexus — daily control link"
    assert "currently LOCKED down" in sent["body"]
    assert "unlock it" in sent["body"]
    assert "https://nexus.example/admin/kill-switch/one-time-token" in sent["body"]
