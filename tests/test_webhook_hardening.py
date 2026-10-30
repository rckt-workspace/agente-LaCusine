"""Endurecimiento del webhook: límite efectivo del cuerpo, firma sobre bytes originales y líneas de negocio."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.fakes import FakeWebhookEventRepository

SECRET = "secreto-de-prueba-5-1"
LINE = "100200300400"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _client(repository, **overrides):
    base = dict(meta_app_secret=SECRET, meta_verify_token="v", database_url="postgresql://unused",
                max_body_bytes=2048)
    base.update(overrides)
    return TestClient(create_app(settings=Settings(**base), webhook_repository=repository))


def _notification(messages=(), statuses=(), phone=LINE):
    return json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": phone},
        "messages": list(messages), "statuses": list(statuses)}}]}]}).encode()


def _message(wamid="wamid.M1", body="hola", stamp="1761830400"):
    return {"from": "573001112233", "id": wamid, "timestamp": stamp, "type": "text", "text": {"body": body}}


def _status(message_id="wamid.S1", status="delivered"):
    return {"id": message_id, "status": status, "timestamp": "1761830400", "recipient_id": "573001112233"}


def test_body_within_limit_is_accepted():
    body = _notification([_message()])
    assert len(body) < 2048
    response = _client(FakeWebhookEventRepository()).post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert response.status_code == 200


def test_declared_oversized_body_is_rejected_before_reading_it():
    body = _notification([_message(body="x" * 4000)])
    response = _client(FakeWebhookEventRepository()).post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body), "Content-Length": str(len(body))})
    assert response.status_code == 413


class _CountingRequest:
    """Solicitud mínima: cuenta cuántos fragmentos consume el lector del servidor."""

    def __init__(self, chunks):
        self.headers = {}
        self.consumed = 0
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


def test_chunked_oversized_body_is_cut_off_while_streaming():
    import asyncio

    from fastapi import HTTPException

    from app.api.webhook import read_limited_body

    request = _CountingRequest([b"a" * 512] * 100)  # 51.200 bytes en total
    try:
        asyncio.run(read_limited_body(request, 2048))
    except HTTPException as exc:
        assert exc.status_code == 413
    else:
        raise AssertionError("debió rechazarse")
    # Se detiene en cuanto supera el límite (2048 bytes = 5 fragmentos): no consume los 100.
    assert request.consumed <= 5


def test_invalid_content_length_is_a_client_error():
    body = _notification([_message()])
    response = _client(FakeWebhookEventRepository()).post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body), "Content-Length": "no-es-numero"})
    assert response.status_code == 400


def test_signature_is_computed_over_the_original_bytes_not_a_reserialization():
    body = _notification([_message()])
    reformatted = json.dumps(json.loads(body), indent=2).encode()  # mismo JSON, bytes distintos
    response = _client(FakeWebhookEventRepository()).post(
        "/webhook", content=reformatted, headers={"X-Hub-Signature-256": _sign(body)})
    assert response.status_code == 403


def test_messages_and_statuses_in_one_notification_are_both_persisted():
    repository = FakeWebhookEventRepository()
    body = _notification(messages=[_message("wamid.MX")], statuses=[_status("wamid.OUT9", "read")])
    response = _client(repository).post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "duplicates": 0, "conflicts": 0}
    assert "wamid.MX" in repository.rows
    assert "status:wamid.OUT9:read" in repository.rows


def test_events_from_another_business_line_are_ignored_when_the_line_is_configured():
    repository = FakeWebhookEventRepository()
    body = _notification(messages=[_message("wamid.OTHER")], statuses=[_status("wamid.OUT2")], phone="999000111222")
    response = _client(repository, meta_phone_number_id=LINE).post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 0, "conflicts": 0}
    assert repository.rows == {}


def test_own_business_line_is_accepted_when_configured():
    repository = FakeWebhookEventRepository()
    body = _notification(messages=[_message("wamid.MINE")], phone=LINE)
    _client(repository, meta_phone_number_id=LINE).post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert "wamid.MINE" in repository.rows


def test_duplicate_notification_is_idempotent():
    repository = FakeWebhookEventRepository()
    client = _client(repository)
    body = _notification([_message("wamid.DUP")])
    headers = {"X-Hub-Signature-256": _sign(body)}
    client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)
    assert second.json() == {"accepted": 0, "duplicates": 1, "conflicts": 0}
