"""Atención pública por WhatsApp Web (WEB_BRIDGE_INBOUND_MODE=public_inbound + WHATSAPP_AUDIENCE_MODE=public_inbound).

Contrato: sin lista ni agenda para ENTRAR; para RESPONDER, solo a rutas que el conector confirma como nacidas de un
evento entrante suyo. Activación explícita. Sin ventana temporal. Nunca Meta Cloud API.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.web_bridge import (
    PUBLIC_INBOUND_CONFIRMATION,
    WebBridgeSender,
    inbound_window_state,
    sign,
    verify,
)
from app.integrations.whatsapp_client import RecipientNotAllowedError, SendRequest
from app.main import create_app
from app.worker.audience import audience_problems
from tests.fakes import FakeWebhookEventRepository

SECRET = "s3cr3t-de-prueba-de-32-caracteres!!"
LINE = "573153863179"
NEW_CONTACT = "573009998877"  # nunca estuvo en ninguna lista
KNOWN_ROUTE = "912345678901"  # ruta opaca de un chat que ya escribió (reenviado por el conector)
UNKNOWN_ROUTE = "911111111111"


def _public_settings(**overrides) -> Settings:
    base = dict(
        meta_app_secret=None, meta_verify_token=None, database_url=None,
        whatsapp_transport="web_bridge", web_bridge_url="http://127.0.0.1:8787", web_bridge_secret=SECRET,
        web_bridge_line_id=LINE, web_bridge_auto_reply=True,
        whatsapp_audience_mode="public_inbound", web_bridge_inbound_mode="public_inbound",
        web_bridge_public_confirmation=PUBLIC_INBOUND_CONFIRMATION,
        whatsapp_allowed_recipients=(),
    )
    base.update(overrides)
    return Settings(**base)


# ---------- Política de arranque ----------

def test_public_inbound_starts_only_with_every_explicit_condition():
    assert audience_problems(_public_settings()) == []


@pytest.mark.parametrize("override, problem", [
    ({"web_bridge_public_confirmation": None}, "public_inbound_not_confirmed"),
    ({"web_bridge_public_confirmation": "si"}, "public_inbound_not_confirmed"),
    ({"web_bridge_auto_reply": False}, "public_inbound_requires_auto_reply"),
    ({"web_bridge_inbound_mode": "single_chat"}, "public_inbound_mode_not_declared"),
    ({"web_bridge_inbound_until": int(time.time()) + 60}, "public_inbound_must_not_have_window"),
    ({"web_bridge_secret": None}, "public_inbound_requires_bridge_config"),
])
def test_each_missing_condition_blocks_the_start(override, problem):
    assert problem in audience_problems(_public_settings(**override))


def test_public_inbound_is_refused_over_cloud_api():
    settings = _public_settings(whatsapp_transport="cloud_api")
    assert "public_inbound_requires_web_bridge" in audience_problems(settings)


def test_public_inbound_entry_with_the_test_audience_is_refused():
    settings = _public_settings(whatsapp_audience_mode="test_allowlist", whatsapp_allowed_recipients=(NEW_CONTACT,))
    assert "public_inbound_requires_public_inbound_audience" in audience_problems(settings)


def test_the_public_mode_stays_refused_on_the_web_transport():
    settings = _public_settings(whatsapp_audience_mode="public")
    assert "public_mode_refused_on_web_bridge" in audience_problems(settings)


def test_public_window_state_has_no_expiry():
    assert inbound_window_state("public_inbound", None) == "public"
    assert inbound_window_state("public_inbound", 1) == "public"


# ---------- Entrada: sin lista ----------

def _item(**overrides):
    base = {"message_id": "BAE5NEW", "from": NEW_CONTACT, "timestamp": int(time.time()) - 5, "kind": "text",
            "text": "Hola, ¿qué cafeteras tienen?", "from_me": False, "is_group": False, "is_history": False}
    base.update(overrides)
    return base


def _post(client: TestClient, items: list[dict]):
    body = json.dumps({"messages": items}).encode()
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
               "X-Bridge-Signature": sign(SECRET, "inbound", stamp, body)}
    return client.post("/internal/web-bridge/inbound", content=body, headers=headers)


def test_a_new_number_that_writes_is_accepted_without_any_list():
    repo = FakeWebhookEventRepository()
    client = TestClient(create_app(settings=_public_settings(), webhook_repository=repo))
    response = _post(client, [_item()])
    assert response.json()["accepted"] == 1
    assert repo.rows["BAE5NEW"].contact_wa_id == NEW_CONTACT


def test_a_new_lid_route_is_accepted_and_keeps_its_route_as_reply_address():
    repo = FakeWebhookEventRepository()
    client = TestClient(create_app(settings=_public_settings(), webhook_repository=repo))
    response = _post(client, [_item(**{"from": KNOWN_ROUTE, "message_id": "BAE5LID"})])
    assert response.json()["accepted"] == 1
    assert repo.rows["BAE5LID"].contact_wa_id == KNOWN_ROUTE


def test_group_own_and_history_items_stay_rejected_in_public_mode():
    repo = FakeWebhookEventRepository()
    client = TestClient(create_app(settings=_public_settings(), webhook_repository=repo))
    response = _post(client, [_item(is_group=True, message_id="G1"), _item(from_me=True, message_id="O1"),
                              _item(is_history=True, message_id="H1")])
    assert response.json()["accepted"] == 0 and response.json()["rejected"] == 3
    assert repo.rows == {}


def test_public_entry_refuses_when_the_declared_audience_is_not_public_inbound():
    repo = FakeWebhookEventRepository()
    client = TestClient(create_app(
        settings=_public_settings(whatsapp_audience_mode="test_allowlist", whatsapp_allowed_recipients=(NEW_CONTACT,)),
        webhook_repository=repo))
    response = _post(client, [_item()])
    assert response.status_code == 403
    assert repo.rows == {}


# ---------- Salida: solo a rutas conocidas ----------

def _recorder(known: set[str]):
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1", "nunca tráfico hacia Meta"
        body = request.read()
        direction = "status" if request.url.path == "/route" else "send"
        assert verify(SECRET, direction, request.headers["X-Bridge-Timestamp"], body, request.headers["X-Bridge-Signature"])
        payload = json.loads(body)
        calls.append((request.url.path, payload))
        if request.url.path == "/route":
            return httpx.Response(200, json={"known": payload["route"] in known})
        return httpx.Response(200, json={"message_id": "BAE5OUT"})

    return handler, calls


def _sender(handler) -> WebBridgeSender:
    return WebBridgeSender(url="http://127.0.0.1:8787", secret=SECRET, line_id=LINE, auto_reply=True,
                           inbound_mode="public_inbound", audience_mode="public_inbound",
                           client=httpx.Client(transport=httpx.MockTransport(handler)))


def _req(to: str, key: str = "k1") -> SendRequest:
    return SendRequest(to=to, text="Respuesta", business_phone_id=LINE, idempotency_key=key)


def test_a_known_route_gets_its_reply_in_the_same_chat():
    handler, calls = _recorder({KNOWN_ROUTE})
    assert _sender(handler).send_text(_req(KNOWN_ROUTE)).message_id == "BAE5OUT"
    assert [p for p, _ in calls] == ["/route", "/send"]
    assert calls[1][1]["to"] == KNOWN_ROUTE


def test_an_unknown_route_is_never_sent_to_in_public_mode():
    handler, calls = _recorder({KNOWN_ROUTE})
    with pytest.raises(RecipientNotAllowedError):
        _sender(handler).send_text(_req(UNKNOWN_ROUTE))
    assert [p for p, _ in calls] == ["/route"]


def test_an_arbitrary_number_is_refused_without_contacting_the_connector_for_send():
    handler, calls = _recorder(set())
    with pytest.raises(RecipientNotAllowedError):
        _sender(handler).send_text(_req(NEW_CONTACT))
    assert all(path != "/send" for path, _ in calls)


def test_a_connector_error_means_the_route_is_refused_and_nothing_is_sent():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/route":
            raise httpx.ConnectError("sin conector", request=request)
        raise AssertionError("no debe enviarse nada")

    with pytest.raises(RecipientNotAllowedError):
        _sender(handler).send_text(_req(KNOWN_ROUTE))
