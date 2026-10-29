"""Estados de entrega en el webhook: se persisten como eventos de estado, sin contacto ni efectos en mensajes."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.fakes import FailingWebhookEventRepository, FakeWebhookEventRepository

SECRET = "app-secret-de-prueba"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _status_body(message_id="wamid.OUT1", status="delivered", phone="100200300400", extra=None) -> bytes:
    status_obj = {"id": message_id, "status": status, "timestamp": "1761830400", "recipient_id": "573001112233"}
    status_obj.update(extra or {})
    return json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": phone},
        "statuses": [status_obj]}}]}]}).encode()


def _client(repository):
    settings = Settings(meta_app_secret=SECRET, meta_verify_token="v", database_url="postgresql://unused")
    return TestClient(create_app(settings=settings, webhook_repository=repository))


def _post(client, body):
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})


def test_status_is_persisted_as_status_event_without_contact():
    repository = FakeWebhookEventRepository()
    response = _post(_client(repository), _status_body())
    assert response.status_code == 200
    record = repository.rows["status:wamid.OUT1:delivered"]
    assert record.event_type == "status"
    assert record.contact_wa_id is None
    assert record.business_phone_id == "100200300400"


def test_status_does_not_change_message_counters():
    response = _post(_client(FakeWebhookEventRepository()), _status_body())
    assert response.json() == {"accepted": 0, "duplicates": 0, "conflicts": 0}


def test_duplicate_status_is_not_stored_twice():
    repository = FakeWebhookEventRepository()
    client = _client(repository)
    _post(client, _status_body())
    _post(client, _status_body())
    assert len(repository.rows) == 1


def test_different_statuses_for_same_message_are_distinct_events():
    repository = FakeWebhookEventRepository()
    client = _client(repository)
    _post(client, _status_body(status="sent"))
    _post(client, _status_body(status="delivered"))
    assert set(repository.rows) == {"status:wamid.OUT1:sent", "status:wamid.OUT1:delivered"}


def test_storage_failure_on_status_returns_503_so_meta_retries():
    response = _post(_client(FailingWebhookEventRepository()), _status_body())
    assert response.status_code == 503


def test_malformed_status_is_rejected_with_400():
    body = _status_body(message_id="")
    assert _post(_client(FakeWebhookEventRepository()), body).status_code == 400


def test_error_code_is_kept_in_the_stored_payload():
    repository = FakeWebhookEventRepository()
    _post(_client(repository), _status_body(status="failed",
                                           extra={"errors": [{"code": 131047, "title": "detalle"}]}))
    stored = repository.rows["status:wamid.OUT1:failed"]
    assert stored.payload["errors"][0]["code"] == 131047
