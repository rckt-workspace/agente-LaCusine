"""Identidad verificada para chats @lid en el puente de WhatsApp Web.

Contrato: la autorización compara el número VERIFICADO por el conector con WHATSAPP_ALLOWED_RECIPIENTS; la respuesta
va a la ruta original del chat (que puede ser un identificador opaco @lid). Sin verificación = rechazo.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.web_bridge import WebBridgeSender, sign, verify
from app.integrations.whatsapp_client import RecipientNotAllowedError, SendRequest
from app.main import create_app
from tests.fakes import FakeWebhookEventRepository

SECRET = "s3cr3t-de-prueba-de-32-caracteres!!"
LINE = "573153863179"
CLIENT = "573001112233"  # número autorizado (verificado)
OTHER = "573009998877"  # número no autorizado
ROUTE = "912345678901"  # ruta opaca de un chat @lid
LID = "123456789012345@lid"


def _sender(handler) -> WebBridgeSender:
    return WebBridgeSender(url="http://127.0.0.1:8787", secret=SECRET, line_id=LINE, auto_reply=True,
                           allowed_recipients=(CLIENT,), audience_mode="test_allowlist",
                           client=httpx.Client(transport=httpx.MockTransport(handler)))


def _req(to: str, key: str = "k1") -> SendRequest:
    return SendRequest(to=to, text="hola", business_phone_id=LINE, idempotency_key=key)


class _Recorder:
    """Simula el conector local: /identity devuelve la identidad verificada de cada ruta; /send acepta."""

    def __init__(self, identities: dict[str, str]) -> None:
        self.identities = identities
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1", "nunca hay tráfico hacia Meta ni otro host"
        body = request.read()
        assert verify(SECRET, "status" if request.url.path == "/identity" else "send",
                      request.headers["X-Bridge-Timestamp"], body, request.headers["X-Bridge-Signature"])
        payload = json.loads(body)
        self.calls.append((request.url.path, payload))
        if request.url.path == "/identity":
            phone = self.identities.get(payload["route"])
            if phone is None:
                return httpx.Response(404, json={"error": "unknown_route"})
            return httpx.Response(200, json={"phone": phone})
        return httpx.Response(200, json={"message_id": "BAE5OUT"})

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


# ---------- Envío: la identidad verificada decide, la ruta es el destino ----------

def test_a_lid_route_is_sent_to_its_original_chat_when_its_verified_identity_is_listed():
    rec = _Recorder({ROUTE: CLIENT})
    result = _sender(rec).send_text(_req(ROUTE))
    assert result.message_id == "BAE5OUT"
    assert rec.paths() == ["/identity", "/send"]
    assert rec.calls[1][1]["to"] == ROUTE, "la respuesta va a la ruta del chat original, no a un @c.us inventado"


def test_a_lid_route_whose_verified_identity_is_not_listed_is_never_sent():
    rec = _Recorder({ROUTE: OTHER})
    with pytest.raises(RecipientNotAllowedError):
        _sender(rec).send_text(_req(ROUTE))
    assert rec.paths() == ["/identity"]


def test_an_unknown_lid_route_is_refused_because_nothing_is_verified():
    rec = _Recorder({})
    with pytest.raises(RecipientNotAllowedError):
        _sender(rec).send_text(_req(ROUTE))
    assert rec.paths() == ["/identity"]


def test_an_unverifiable_identity_response_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity":
            return httpx.Response(200, json={"phone": "no-es-un-numero"})
        raise AssertionError("no debe enviarse nada")

    with pytest.raises(RecipientNotAllowedError):
        _sender(handler).send_text(_req(ROUTE))


def test_a_connector_that_is_down_means_the_recipient_is_refused_without_sending():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin conector", request=request)

    with pytest.raises(RecipientNotAllowedError):
        _sender(handler).send_text(_req(ROUTE))


def test_a_listed_plain_number_is_sent_without_any_identity_lookup():
    rec = _Recorder({})
    _sender(rec).send_text(_req(CLIENT))
    assert rec.paths() == ["/send"]


def test_an_unlisted_plain_number_is_refused_without_any_identity_lookup():
    rec = _Recorder({OTHER: OTHER})
    with pytest.raises(RecipientNotAllowedError):
        _sender(rec).send_text(_req(OTHER))
    assert rec.paths() == []


# ---------- Entrada: la lista se aplica a la identidad, no a la ruta ----------

def _settings() -> Settings:
    return Settings(
        meta_app_secret=None, meta_verify_token=None, database_url=None,
        whatsapp_transport="web_bridge", web_bridge_url="http://127.0.0.1:8787", web_bridge_secret=SECRET,
        web_bridge_line_id=LINE, web_bridge_auto_reply=True, whatsapp_audience_mode="test_allowlist",
        whatsapp_allowed_recipients=(CLIENT,),
    )


def _item(**overrides):
    base = {"message_id": "BAE5LID", "from": ROUTE, "identity": CLIENT, "timestamp": int(time.time()) - 5,
            "kind": "text", "text": "Hola", "from_me": False, "is_group": False, "is_history": False}
    base.update(overrides)
    return base


def _post(client: TestClient, items: list[dict]):
    body = json.dumps({"messages": items}).encode()
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
               "X-Bridge-Signature": sign(SECRET, "inbound", stamp, body)}
    return client.post("/internal/web-bridge/inbound", content=body, headers=headers)


def _app():
    repo = FakeWebhookEventRepository()
    return TestClient(create_app(settings=_settings(), webhook_repository=repo)), repo


def test_a_lid_message_with_a_verified_listed_identity_is_stored_with_its_original_route():
    client, repo = _app()
    response = _post(client, [_item()])
    assert response.json()["accepted"] == 1
    stored = repo.rows["BAE5LID"]
    assert stored.contact_wa_id == ROUTE, "la ruta de respuesta se conserva tal cual en el registro"


def test_a_lid_message_without_identity_is_rejected_and_stores_nothing():
    client, repo = _app()
    response = _post(client, [_item(identity=None)])
    assert response.json()["accepted"] == 0 and response.json()["rejected"] == 1
    assert repo.rows == {}


def test_the_identity_decides_even_when_the_route_looks_listed():
    client, repo = _app()
    response = _post(client, [_item(**{"from": CLIENT, "identity": OTHER})])
    assert response.json()["accepted"] == 0
    assert repo.rows == {}


# ---------- Flujo completo (simulado): entrada -> registro -> respuesta al mismo chat ----------

def test_the_reply_goes_back_to_the_same_lid_chat_end_to_end_with_one_send():
    client, repo = _app()
    assert _post(client, [_item()]).json()["accepted"] == 1
    contact = repo.rows["BAE5LID"].contact_wa_id
    rec = _Recorder({contact: CLIENT})
    _sender(rec).send_text(_req(contact, key="BAE5LID-reply"))
    sends = [payload for path, payload in rec.calls if path == "/send"]
    assert len(sends) == 1
    assert sends[0] == {"to": ROUTE, "text": "hola", "idempotency_key": "BAE5LID-reply"}
