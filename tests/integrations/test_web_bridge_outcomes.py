"""Resultado por elemento del puente y clasificación de envíos: NO ENVIADO / ENVIADO / INCIERTO."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.web_bridge import WebBridgeSender, sign
from app.integrations.whatsapp_client import MetaRejectedError, SendDisabledError, SendRequest, UncertainSendError
from app.main import create_app
from tests.fakes import FakeWebhookEventRepository

SECRET = "s3cr3t-de-prueba-de-32-caracteres!!"
LINE = "573153863179"
CLIENT = "573001112233"
ROUTE = "912345678901"


def _settings() -> Settings:
    return Settings(
        meta_app_secret=None, meta_verify_token=None, database_url=None,
        whatsapp_transport="web_bridge", web_bridge_url="http://127.0.0.1:8787", web_bridge_secret=SECRET,
        web_bridge_line_id=LINE, web_bridge_auto_reply=True, whatsapp_audience_mode="test_allowlist",
        whatsapp_allowed_recipients=(CLIENT,),
    )


def _item(**overrides):
    base = {"message_id": "BAE5OUT1", "correlation_id": "abc123", "from": ROUTE, "identity": CLIENT,
            "timestamp": int(time.time()) - 5, "kind": "text", "text": "Hola", "from_me": False, "is_group": False,
            "is_history": False}
    base.update(overrides)
    return base


def _post(client: TestClient, items: list[dict]):
    body = json.dumps({"messages": items}).encode()
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
               "X-Bridge-Signature": sign(SECRET, "inbound", stamp, body)}
    return client.post("/internal/web-bridge/inbound", content=body, headers=headers)


def test_each_item_reports_its_own_outcome_and_a_rejected_one_is_never_accepted():
    repo = FakeWebhookEventRepository()
    client = TestClient(create_app(settings=_settings(), webhook_repository=repo))
    old = _item(message_id="BAE5OLD", correlation_id="old1", timestamp=int(time.time()) - 3600)
    fresh = _item(message_id="BAE5NEW", correlation_id="new1")
    body = _post(client, [old, fresh]).json()
    by_cid = {i["correlation_id"]: i for i in body["items"]}
    assert by_cid["old1"] == {"correlation_id": "old1", "outcome": "rejected", "reason": "message_too_old"}
    assert by_cid["new1"]["outcome"] == "accepted"
    assert body["accepted"] == 1 and body["rejected"] == 1, "el contrato de contadores no cambia"


def test_a_repeat_is_reported_as_duplicate_and_the_old_fields_are_kept():
    repo = FakeWebhookEventRepository()
    client = TestClient(create_app(settings=_settings(), webhook_repository=repo))
    same_message = _item()  # mismo elemento: un reenvío real del conector
    _post(client, [same_message])
    body = _post(client, [same_message]).json()
    assert body["items"][0]["outcome"] == "duplicate"
    assert set(body) >= {"accepted", "duplicates", "conflicts", "rejected", "items"}


def test_identity_rejections_are_listed_with_their_reason():
    repo = FakeWebhookEventRepository()
    client = TestClient(create_app(settings=_settings(), webhook_repository=repo))
    body = _post(client, [_item(identity="573009998877", correlation_id="x1")]).json()
    assert body["items"] == [{"correlation_id": "x1", "outcome": "rejected", "reason": "not_in_test_allowlist"}]
    assert repo.rows == {}


def _sender(handler) -> WebBridgeSender:
    return WebBridgeSender(url="http://127.0.0.1:8787", secret=SECRET, line_id=LINE, auto_reply=True,
                           allowed_recipients=(CLIENT,), audience_mode="test_allowlist",
                           client=httpx.Client(transport=httpx.MockTransport(handler)))


def _req() -> SendRequest:
    return SendRequest(to=CLIENT, text="hola", business_phone_id=LINE, idempotency_key="k-out")


@pytest.mark.parametrize("status, expected", [
    (200, None),
    (409, SendDisabledError),
    (422, MetaRejectedError),  # chat no resuelto: nada salió, rechazo definitivo sin reintento automático
    (500, UncertainSendError),  # se intentó y no hay confirmación: incierto, nunca se reenvía
])
def test_the_status_of_the_bridge_maps_to_not_sent_sent_or_uncertain(status, expected):
    body = {"message_id": "true_x_OUT"} if status == 200 else {"error": "x"}
    sender = _sender(lambda request: httpx.Response(status, json=body))
    if expected is None:
        assert sender.send_text(_req()).message_id == "true_x_OUT"
    else:
        with pytest.raises(expected):
            sender.send_text(_req())


def test_a_200_without_message_id_is_uncertain_not_sent():
    sender = _sender(lambda request: httpx.Response(200, json={}))
    with pytest.raises(UncertainSendError):
        sender.send_text(_req())
