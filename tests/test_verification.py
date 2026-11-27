


def test_web_verification_uses_app_injected_turnstile_settings(vguild, monkeypatch):
    for var in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY", "PUBLIC_BASE_URL", "DISCORD_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    app = vguild.nexus.api
    app.config.update({
        "TURNSTILE_SITE_KEY": SITE,
        "TURNSTILE_SECRET_KEY": SECRET,
        "PUBLIC_BASE_URL": BASE,
    })
    token = _link(vguild)
    page = vguild.client.get(f"/verify/{token}")
    assert page.status_code == 200
    assert SITE in page.get_data(as_text=True)

    calls = []

    class InjectedClient:
        def post(self, url, data=None, timeout=None):
            calls.append({"url": url, "data": data, "timeout": timeout})
            return types.SimpleNamespace(json=lambda: {"success": True})

    app.config["TURNSTILE_HTTP_CLIENT"] = InjectedClient()
    response = vguild.client.post(
        f"/verify/{token}",
        data={"cf-turnstile-response": "worker-token"},
    )
    assert response.status_code == 200
    assert calls[0]["data"] == {
        "secret": SECRET,
        "response": "worker-token",
        "remoteip": "127.0.0.1",
    }
