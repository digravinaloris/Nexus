import hashlib
import hmac
import io
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


def app(secret=SECRET):
    flask_app = Flask(__name__)
    flask_app.config["GITHUB_RELEASE_WEBHOOK_SECRET"] = secret
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
    assert github_webhook.validate_release(payload(html_url="https://github.com.evil.example/digravinaloris/Nexus/releases/tag/v2.1.0")) is None
    assert github_webhook.validate_release(payload(html_url="https://user@github.com/digravinaloris/Nexus/releases/tag/v2.1.0")) is None
    assert github_webhook.validate_release(payload(html_url="https://github.com/digravinaloris/Nexus/releases/tag/v2.1.0?redirect=evil")) is None
    assert github_webhook.validate_release(payload(html_url="https://github.com/digravinaloris/Nexus/releases/../../other")) is None
    assert github_webhook.validate_release(payload(tag_name="")) is None
    assert github_webhook.validate_release(payload(tag_name="x" * 256)) is None
    assert github_webhook.validate_release(payload(id=True)) is None
    assert github_webhook.validate_release(payload(id=0)) is None


def test_missing_secret_returns_503():
    assert app(secret="").post("/webhooks/github/releases", data=b"{}").status_code == 503


def test_bad_signature_returns_401():
    client = app()
    response = client.post("/webhooks/github/releases", data=b"{}", headers={"X-Hub-Signature-256": "sha256=wrong"})
    assert response.status_code == 401


def test_payload_limit_returns_413():
    response = app().post("/webhooks/github/releases", data=b"x" * (github_webhook.MAX_PAYLOAD_BYTES + 1))
    assert response.status_code == 413


def test_unknown_length_payload_read_is_bounded(monkeypatch):

    class TrackingStream(io.BytesIO):
        requested_size = None

        def read(self, size=-1):
            self.requested_size = size
            return super().read(size)

    stream = TrackingStream(b"x" * (github_webhook.MAX_PAYLOAD_BYTES + 100))
    fake_request = type("FakeRequest", (), {
        "content_length": None,
        "headers": {},
        "stream": stream,
    })()
    monkeypatch.setattr(github_webhook, "request", fake_request)

    response = app().post("/webhooks/github/releases")
    assert response.status_code == 413
    assert stream.requested_size == github_webhook.MAX_PAYLOAD_BYTES + 1
    assert stream.tell() == github_webhook.MAX_PAYLOAD_BYTES + 1


def test_non_release_event_ignored_after_signature():
    body, headers = signed(payload(), event="issues")
    response = app().post("/webhooks/github/releases", data=body, headers=headers)
    assert response.status_code == 202
    assert response.json == {"ignored": "event"}


def test_duplicate_delivery_suppressed_and_valid_event_dispatched(monkeypatch):
    body, headers = signed(payload())
    deliveries = []

    class FakeBotService:
        def api_handle_github_release(self, delivery_id, release):
            assert release["tag_name"] == "v2.1.0"
            if delivery_id in deliveries:
                return "duplicate"
            deliveries.append(delivery_id)
            return "sent"

    monkeypatch.setattr(github_webhook, "get_bot_service", lambda: FakeBotService())
    client = app()
    first = client.post("/webhooks/github/releases", data=body, headers=headers)
    assert first.status_code == 200 and first.json == {"ok": True}
    second = client.post("/webhooks/github/releases", data=body, headers=headers)
    assert second.status_code == 200 and second.json == {"duplicate": True}
    assert deliveries == [DELIVERY]
