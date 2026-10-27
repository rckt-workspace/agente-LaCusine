import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.idempotency import InMemoryEventStore

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def message_payload(message_id: str = "wamid.TEST1", sender: str = "573001112233") -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {"from": sender, "id": message_id, "type": "text", "text": {"body": "hola"}}
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode()


@pytest.fixture
def client():
    settings = Settings(meta_app_secret=APP_SECRET, meta_verify_token=VERIFY_TOKEN)
    return TestClient(create_app(settings=settings, event_store=InMemoryEventStore()))


@pytest.fixture
def unconfigured_client():
    return TestClient(create_app(settings=Settings(meta_app_secret=None, meta_verify_token=None)))


# --- Handshake GET ---

def test_handshake_returns_challenge_when_token_matches(client):
    response = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "123456"},
    )
    assert response.status_code == 200
    assert response.text == "123456"


def test_handshake_rejects_wrong_token(client):
    response = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "otro", "hub.challenge": "123456"},
    )
    assert response.status_code == 403


def test_handshake_rejects_wrong_mode(client):
    response = client.get(
        "/webhook",
        params={"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
    )
    assert response.status_code == 403


def test_handshake_fails_closed_without_configured_token(unconfigured_client):
    response = unconfigured_client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"},
    )
    assert response.status_code == 403


# --- POST con firma ---

def test_post_with_valid_signature_is_accepted(client):
    body = message_payload()
    response = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "duplicates": 0}


def test_post_without_signature_is_rejected(client):
    response = client.post("/webhook", content=message_payload())
    assert response.status_code == 403


def test_post_with_wrong_signature_is_rejected(client):
    body = message_payload()
    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, "otro-secreto")})
    assert response.status_code == 403


def test_post_with_signature_for_different_body_is_rejected(client):
    signed = message_payload("wamid.A")
    sent = message_payload("wamid.B")
    response = client.post("/webhook", content=sent, headers={"X-Hub-Signature-256": sign(signed)})
    assert response.status_code == 403


def test_post_with_malformed_signature_header_is_rejected(client):
    body = message_payload()
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": digest})
    assert response.status_code == 403


def test_post_fails_closed_without_app_secret(unconfigured_client):
    body = message_payload()
    response = unconfigured_client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert response.status_code == 503


# --- Idempotencia ---

def test_duplicate_message_is_not_processed_twice(client):
    body = message_payload("wamid.DUP")
    headers = {"X-Hub-Signature-256": sign(body)}
    first = client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)
    assert first.json() == {"accepted": 1, "duplicates": 0}
    assert second.json() == {"accepted": 0, "duplicates": 1}


# --- Robustez del payload ---

def test_status_only_payload_is_acknowledged_without_messages(client):
    body = json.dumps(
        {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.S", "status": "read"}]}}]}]}
    ).encode()
    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 0}


def test_invalid_json_with_valid_signature_returns_400(client):
    body = b"{no es json"
    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert response.status_code == 400


def test_oversized_body_is_rejected(client):
    client.app.state.settings = Settings(
        meta_app_secret=APP_SECRET, meta_verify_token=VERIFY_TOKEN, max_body_bytes=10
    )
    body = message_payload()
    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert response.status_code == 413


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}
