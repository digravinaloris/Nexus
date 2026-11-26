import hashlib
import hmac
import json

from flask import Flask
from web import github_webhook

SECRET = "test-webhook-secret"
DELIVERY = "delivery-test-123"


def payload(**overrides):
    release = {
        "id": 9876,
        "tag_name": "v2.1.0",
        "html_url": "https://github.com/digravinaloris/Nexus/releases/tag/v2.1.0",
        "draft": False,
        "prerelease": False,
    }
    release.update(overrides)
    return {"action": "published", "repository": {"full_name": "digravinaloris/Nexus"}, "release": release}


def signed(data, event="release"):
    raw = json.dumps(data).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "X-GitHub-Event": event, "X-GitHub-Delivery": DELIVERY}


def app():
    flask_app = Flask(__name__)
    github_webhook.routes.register(flask_app)
    return flask_app.test_client()


def test_signature_valid_and_tampering_rejected():
    body = b'{"valid":true}'
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert github_webhook.verify_signature(body, sig, SECRET)
    assert not github_webhook.verify_signature(body + b"x", sig, SECRET)
    assert not github_webhook.verify_signature(body, "bad", SECRET)


def test_payload_validation_only_accepts_own_published_release():
    assert github_webhook.validate_release(payload())
    assert github_webhook.validate_release(payload(draft=True)) is None
    assert github_webhook.validate_release(payload(prerelease=True)) is None
    assert github_webhook.validate_release({**payload(), "action": "created"}) is None
    wrong_repo = payload()
    wrong_repo["repository"]["full_name"] = "fork/Nexus"
    assert github_webhook.validate_release(wrong_repo) is None
    assert github_webhook.validate_release(payload(html_url="https://example.org")) is None


def test_missing_secret_returns_503(monkeypatch):
    monkeypatch.delenv("GITHUB_RELEASE_WEBHOOK_SECRET", raising=False)
    assert app().post("/webhooks/github/releases", data=b"{}").status_code == 503


def test_bad_signature_returns_401(monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", SECRET)
    client = app()
    response = client.post("/webhooks/github/releases", data=b"{}", headers={"X-Hub-Signature-256": "sha256=wrong"})
    assert response.status_code == 401


def test_payload_limit_returns_413(monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", SECRET)
    response = app().post("/webhooks/github/releases", data=b"x" * (github_webhook.MAX_PAYLOAD_BYTES + 1))
    assert response.status_code == 413


def test_non_release_event_ignored_after_signature(monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", SECRET)
    body, headers = signed(payload(), event="issues")
    response = app().post("/webhooks/github/releases", data=body, headers=headers)
    assert response.status_code == 202
    assert response.json == {"ignored": "event"}


def test_duplicate_delivery_suppressed_and_valid_event_dispatched(monkeypatch):
    monkeypatch.setenv("GITHUB_RELEASE_WEBHOOK_SECRET", SECRET)
    body, headers = signed(payload())
    markers = []

    class DeliveryCollection:
        def find_one(self, query):
            return next((marker for marker in markers if marker["_id"] == query["_id"]), None)

        def insert_one(self, doc):
            markers.append(doc)

    class FakeDB:
        def __getitem__(self, name):
            assert name == "github_webhook_deliveries"
            return DeliveryCollection()

    class FakeCog:
        async def handle_webhook_release(self, release):
            assert release["tag_name"] == "v2.1.0"
            return True

    class FakeBot:
        def is_ready(self):
            return True

        def get_cog(self, name):
            assert name == "GitHubUpdates"
            return FakeCog()

    monkeypatch.setattr(github_webhook.core, "db", FakeDB())
    monkeypatch.setattr(github_webhook.core, "bot", FakeBot())
    monkeypatch.setattr(github_webhook, "run_coroutine", lambda coro: __import__("asyncio").run(coro))
    client = app()
    first = client.post("/webhooks/github/releases", data=body, headers=headers)
    assert first.status_code == 200 and first.json == {"ok": True}
    second = client.post("/webhooks/github/releases", data=body, headers=headers)
    assert second.status_code == 200 and second.json == {"duplicate": True}
    assert markers == [{"_id": DELIVERY}]
