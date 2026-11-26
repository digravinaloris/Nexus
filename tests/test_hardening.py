"""Durcissement : redirection ouverte du dashboard (?next=) et démarrage du serveur web."""
import sys

import pytest

EVIL = [
    "https://evil.example/phish",
    "http://evil.example",
    "//evil.example",
    "///evil.example",
    "/\\evil.example",          # /\hote : certains navigateurs l'interprètent comme //hote
    "/\t/evil.example",         # tab ignoré par les navigateurs -> //evil.example
    "/\n/evil.example",
    "javascript:alert(1)",
    "evil.example",             # relatif sans slash initial
    " https://evil.example",
]


@pytest.mark.parametrize("path", ["/dashboard/lang/fr", "/dashboard/theme/light"])
@pytest.mark.parametrize("nxt", EVIL)
def test_next_param_never_redirects_off_site(nexus, path, nxt):
    client = nexus.api.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "csrf-test-token-012345678901234567890123456789"
    resp = client.post(path, query_string={"next": nxt}, data={"csrf_token": "csrf-test-token-012345678901234567890123456789"})
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.endswith("/dashboard") and "evil" not in location and "javascript" not in location


@pytest.mark.parametrize("path", ["/dashboard/lang/fr", "/dashboard/theme/light"])
def test_next_param_keeps_working_for_site_paths(nexus, path):
    client = nexus.api.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "csrf-test-token-012345678901234567890123456789"
    # Les templates passent next=request.path (ex: /dashboard/123/settings)
    for nxt in ("/status", "/dashboard/123456789012345678/settings", "/dashboard?x=1"):
        resp = client.post(path, query_string={"next": nxt}, data={"csrf_token": "csrf-test-token-012345678901234567890123456789"})
        assert resp.status_code == 302 and resp.headers["Location"].endswith(nxt)
    resp = client.post(path, data={"csrf_token": "csrf-test-token-012345678901234567890123456789"})  # sans next -> dashboard
    assert resp.headers["Location"].endswith("/dashboard")


def test_run_api_serves_with_waitress(nexus, monkeypatch):
    waitress = pytest.importorskip("waitress")
    calls = {}
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: calls.update(app=app, **kw))
    monkeypatch.setattr(nexus.api, "run", lambda **kw: calls.update(dev_server=True))
    monkeypatch.setenv("PORT", "9123")
    nexus.run_api()
    assert calls == {"app": nexus.api, "host": "0.0.0.0", "port": 9123, "threads": 16}


def test_run_api_falls_back_to_flask_dev_server(nexus, monkeypatch, capsys):
    calls = {}
    monkeypatch.setitem(sys.modules, "waitress", None)  # import impossible
    monkeypatch.setattr(nexus.api, "run", lambda **kw: calls.update(kw))
    monkeypatch.setenv("PORT", "9124")
    nexus.run_api()
    assert calls == {"host": "0.0.0.0", "port": 9124}
    assert "repli sur le serveur de développement" in capsys.readouterr().out


def test_run_api_falls_back_when_waitress_crashes_at_startup(nexus, monkeypatch):
    waitress = pytest.importorskip("waitress")
    calls = {}

    def boom(app, **kw):
        raise RuntimeError("incompatible")

    monkeypatch.setattr(waitress, "serve", boom)
    monkeypatch.setattr(nexus.api, "run", lambda **kw: calls.update(kw))
    monkeypatch.setenv("PORT", "9125")
    nexus.run_api()
    assert calls == {"host": "0.0.0.0", "port": 9125}


def test_baseline_security_headers_are_present(nexus):
    response = nexus.api.test_client().get("/status")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"


def test_baseline_security_headers_include_safe_csp_restrictions(nexus):
    response = nexus.api.test_client().get("/status")
    csp = response.headers["Content-Security-Policy"]
    assert "base-uri 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp



@pytest.mark.parametrize(
    "path",
    ["/login", "/callback", "/logout", "/dashboard", "/dashboard/111/calendar"],
)
def test_sensitive_oauth_and_dashboard_responses_are_not_cached(nexus, path):
    response = nexus.api.test_client().get(path)
    assert "no-store" in response.headers["Cache-Control"].lower()
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"


def test_public_status_response_keeps_normal_cache_policy(nexus):
    response = nexus.api.test_client().get("/status")
    assert "no-store" not in response.headers.get("Cache-Control", "").lower()
