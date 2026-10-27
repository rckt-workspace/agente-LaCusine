"""Pruebas unitarias de persistencia del webhook. Sin base de datos real: se usan dobles en memoria."""

import hashlib
import hmac
import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.webhook import InboundMessage, PayloadValidationError, extract_inbound_messages
from app.repositories.errors import StorageError
from app.repositories.webhook_events import RegisterOutcome, WebhookEventRecord
from tests.fakes import FailingWebhookEventRepository, FakeWebhookEventRepository

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
SENDER = "573001112233"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def build_payload(*messages: dict) -> bytes:
    return json.dumps(
        {"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}
    ).encode()


def text_message(message_id: str, sender: str = SENDER, body: str = "hola, busco una nevera") -> dict:
    return {"from": sender, "id": message_id, "type": "text", "text": {"body": body}}


def post(client: TestClient, body: bytes):
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})


def make_client(repository, **overrides) -> TestClient:
    settings = Settings(
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        database_url="postgresql://unused",
        **overrides,
    )
    return TestClient(create_app(settings=settings, webhook_repository=repository))


class FailOnceRepository(FakeWebhookEventRepository):
    """Falla la primera vez que ve un wamid concreto, simulando una caída a mitad del payload."""

    def __init__(self, failing_id: str) -> None:
        super().__init__()
        self.failing_id = failing_id
        self._failed = False

    def register(self, record: WebhookEventRecord) -> RegisterOutcome:
        if record.external_event_id == self.failing_id and not self._failed:
            self._failed = True
            raise StorageError("webhook_event_write_failed")
        return super().register(record)


# --- Fallos persistentes de la base ---

def test_storage_failure_returns_503_so_meta_retries():
    repository = FailingWebhookEventRepository()
    client = make_client(repository)
    response = post(client, build_payload(text_message("wamid.X1")))
    assert response.status_code == 503
    assert response.json() == {"detail": "storage_unavailable"}


def test_persistent_storage_failure_never_acknowledges_the_event():
    repository = FailingWebhookEventRepository()
    client = make_client(repository)
    body = build_payload(text_message("wamid.X2"))
    for _ in range(3):
        assert post(client, body).status_code == 503
    assert repository.calls == 3


def test_storage_error_response_does_not_echo_payload_or_secrets():
    client = make_client(FailingWebhookEventRepository())
    response = post(client, build_payload(text_message("wamid.X3", body="contenido sensible")))
    assert "contenido sensible" not in response.text
    assert APP_SECRET not in response.text
    assert "postgresql" not in response.text


def test_retry_after_partial_failure_is_idempotent():
    # El primer mensaje se guarda, el segundo falla. El reintento de Meta reenvía ambos:
    # el primero es duplicado y el segundo se registra por primera vez.
    repository = FailOnceRepository(failing_id="wamid.B")
    client = make_client(repository)
    body = build_payload(text_message("wamid.A"), text_message("wamid.B"))

    first = post(client, body)
    assert first.status_code == 503

    retry = post(client, body)
    assert retry.status_code == 200
    assert retry.json() == {"accepted": 1, "duplicates": 1, "conflicts": 0}
    assert set(repository.rows) == {"wamid.A", "wamid.B"}


def test_conflicting_content_for_same_wamid_is_not_overwritten():
    repository = FakeWebhookEventRepository()
    client = make_client(repository)
    post(client, build_payload(text_message("wamid.C", body="original")))
    response = post(client, build_payload(text_message("wamid.C", body="alterado")))

    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 0, "conflicts": 1}
    stored = repository.rows["wamid.C"]
    assert stored.payload["text"]["body"] == "original"


# --- Validación de payload ---

@pytest.mark.parametrize(
    "body",
    [
        b'{"entry": "no es lista"}',
        b'{"entry": [{"changes": [{"value": {"messages": "no es lista"}}]}]}',
        b'{"entry": [{"changes": [{"value": {"messages": [42]}}]}]}',
        b'{"entry": [{"changes": [{"value": {"messages": [{"id": "", "from": "573001112233"}]}}]}]}',
        b'{"entry": [{"changes": [{"value": {"messages": [{"from": "573001112233"}]}}]}]}',
        b'{"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.V", "from": "+57 300"}]}}]}]}',
        b'[]',
    ],
)
def test_structurally_invalid_payloads_are_rejected_before_storage(body):
    repository = FakeWebhookEventRepository()
    client = make_client(repository)
    response = post(client, body)
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_payload"}
    assert repository.calls == 0


def test_too_many_messages_in_one_request_is_rejected():
    repository = FakeWebhookEventRepository()
    client = make_client(repository, max_messages_per_request=2)
    body = build_payload(text_message("wamid.M1"), text_message("wamid.M2"), text_message("wamid.M3"))
    assert post(client, body).status_code == 400
    assert repository.calls == 0


def test_extraction_ignores_status_updates_and_returns_only_messages():
    payload = {
        "entry": [
            {
                "changes": [
                    {"value": {"statuses": [{"id": "wamid.S", "status": "delivered"}]}},
                    {"value": {"messages": [text_message("wamid.R")]}},
                ]
            }
        ]
    }
    messages = extract_inbound_messages(payload, max_messages=10)
    assert [m.message_id for m in messages] == ["wamid.R"]


def test_payload_hash_is_independent_of_key_order():
    first = InboundMessage("wamid.H", SENDER, {"id": "wamid.H", "from": SENDER, "type": "text"})
    second = InboundMessage("wamid.H", SENDER, {"type": "text", "from": SENDER, "id": "wamid.H"})
    assert first.payload_sha256 == second.payload_sha256


def test_validation_error_messages_are_codes_only():
    with pytest.raises(PayloadValidationError) as excinfo:
        extract_inbound_messages({"entry": [{"changes": [{"value": {"messages": [{"id": 1}]}}]}]}, 10)
    assert str(excinfo.value) == "message_id_invalid"


# --- Credenciales y configuración ausentes ---

def test_missing_database_fails_closed_without_parsing_body():
    client = TestClient(create_app(settings=Settings(meta_app_secret=APP_SECRET, meta_verify_token=VERIFY_TOKEN)))
    response = post(client, b"no-es-json-y-no-importa")
    assert response.status_code == 503
    assert response.json() == {"detail": "not_configured"}


def test_missing_secret_fails_closed_even_with_database():
    client = make_client(FakeWebhookEventRepository())
    client.app.state.settings = Settings(meta_app_secret=None, meta_verify_token=None, database_url="x")
    response = post(client, build_payload(text_message("wamid.N")))
    assert response.status_code == 503


def test_settings_from_env_returns_none_for_missing_credentials(monkeypatch):
    for name in ("META_APP_SECRET", "META_VERIFY_TOKEN", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.meta_app_secret is None
    assert settings.meta_verify_token is None
    assert settings.database_url is None


def test_empty_env_values_are_treated_as_missing(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "")
    monkeypatch.setenv("DATABASE_URL", "")
    settings = Settings.from_env()
    assert settings.meta_app_secret is None
    assert settings.database_url is None


def test_app_construction_does_not_connect_to_database():
    app = create_app(
        settings=Settings(meta_app_secret=APP_SECRET, meta_verify_token=VERIFY_TOKEN, database_url="postgresql://unused")
    )
    assert app.state.pool is not None
    assert app.state.pool.closed


def test_webhook_path_does_not_load_ai_client():
    import app.main  # noqa: F401

    assert "anthropic" not in sys.modules


# --- Logging seguro ---

def test_logs_do_not_contain_message_text_or_full_phone(caplog):
    repository = FakeWebhookEventRepository()
    client = make_client(repository)
    caplog.set_level(logging.DEBUG, logger="lacusine.webhook")
    body = build_payload(text_message("wamid.LOG", sender="573009998877", body="texto privado del cliente"))
    assert post(client, body).status_code == 200

    combined = " ".join(record.getMessage() for record in caplog.records)
    assert "texto privado del cliente" not in combined
    assert "573009998877" not in combined
    assert "***8877" in combined


def test_storage_failure_log_does_not_contain_payload(caplog):
    client = make_client(FailingWebhookEventRepository())
    caplog.set_level(logging.DEBUG, logger="lacusine.webhook")
    post(client, build_payload(text_message("wamid.LOG2", body="dato privado")))
    combined = " ".join(record.getMessage() for record in caplog.records)
    assert "dato privado" not in combined


# --- Modelo de registro ---

def test_record_rejects_empty_event_id():
    with pytest.raises(ValueError):
        WebhookEventRecord(external_event_id="", contact_wa_id=SENDER, payload={}, payload_sha256="0" * 64)


def test_record_rejects_malformed_hash():
    with pytest.raises(ValueError):
        WebhookEventRecord(external_event_id="wamid.Z", contact_wa_id=SENDER, payload={}, payload_sha256="abc")
