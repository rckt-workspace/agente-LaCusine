"""Transporte experimental WhatsApp Web: firma, filtros, remitente y endpoint interno. Sin red ni WhatsApp real.

Garantías:
  * El remitente web nunca envía con WEB_BRIDGE_AUTO_REPLY distinto de true.
  * Con lista de prueba, nunca responde fuera de la lista (ni en el remitente ni en la entrada).
  * El modo público no puede usar el transporte web.
  * El endpoint interno solo existe con transporte web y respuestas autorizadas; exige firma.
"""

import json
import time
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.web_bridge import (
    BridgeRejectedError,
    WebBridgeSender,
    inbound_to_meta_payload,
    sign,
    verify,
)
from app.integrations.whatsapp_client import (
    ConnectionFailedError,
    MetaRejectedError,
    RecipientNotAllowedError,
    SendDisabledError,
    SendRequest,
    UncertainSendError,
)
from app.main import create_app
from app.worker.audience import PUBLIC_CONFIRMATION_TOKEN, audience_problems
from tests.fakes import FakeWebhookEventRepository

SECRET = "s3cr3t-de-prueba-de-32-caracteres!!"
LINE = "573153863179"
CLIENT = "573001112233"
NOW = 1_800_000_000
# Vector de referencia compartido con connector/whatsapp-web/src/test/bridge.test.ts.
PYTHON_VECTOR = "3df9a7b82f064dc8ead2502faf2f6e09d6834a97e48a0cb9df3bbafcecd29f82"


def _item(**overrides):
    # identity: número verificado por WhatsApp (para @c.us coincide con from). Es lo que autoriza el filtro.
    base = {"message_id": "BAE5ABC", "from": CLIENT, "identity": CLIENT, "timestamp": NOW - 5, "kind": "text",
            "text": "Hola", "from_me": False, "is_group": False, "is_history": False}
    base.update(overrides)
    return base


def _settings(**overrides) -> Settings:
    base = dict(
        meta_app_secret=None, meta_verify_token=None, database_url=None,
        whatsapp_transport="web_bridge", web_bridge_url="http://127.0.0.1:8787", web_bridge_secret=SECRET,
        web_bridge_line_id=LINE, web_bridge_auto_reply=True, whatsapp_audience_mode="test_allowlist",
        whatsapp_allowed_recipients=(CLIENT,),
    )
    base.update(overrides)
    return Settings(**base)


def _sender(handler, **overrides) -> WebBridgeSender:
    options = dict(url="http://127.0.0.1:8787", secret=SECRET, line_id=LINE, auto_reply=True,
                   allowed_recipients=(CLIENT,), audience_mode="test_allowlist")
    options.update(overrides)
    return WebBridgeSender(client=httpx.Client(transport=httpx.MockTransport(handler)), **options)


def _req(to=CLIENT, business=LINE, key="k1") -> SendRequest:
    return SendRequest(to=to, text="hola", business_phone_id=business, idempotency_key=key)


# --- Firma ---

def test_signature_matches_the_connector_vector():
    assert sign(SECRET, "send", str(NOW), b'{"to":"573001112233","text":"hola","idempotency_key":"k1"}') == PYTHON_VECTOR


def test_signature_is_bound_to_direction_window_and_body():
    body = b"{}"
    sig = sign(SECRET, "send", str(NOW), body)
    assert verify(SECRET, "send", str(NOW), body, sig, now=NOW)
    assert not verify(SECRET, "inbound", str(NOW), body, sig, now=NOW)
    assert not verify(SECRET, "send", str(NOW - 301), body, sig, now=NOW)
    assert not verify(SECRET, "send", str(NOW), b"{ }", sig, now=NOW)
    assert not verify(SECRET, "send", "no-es-numero", body, sig, now=NOW)


# --- Conversión de mensajes entrantes ---

def test_a_live_text_from_a_client_becomes_a_meta_shaped_payload():
    payload = inbound_to_meta_payload(_item(), LINE, now=NOW)
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    assert message == {"from": CLIENT, "id": "BAE5ABC", "timestamp": str(NOW - 5), "type": "text",
                       "text": {"body": "Hola"}}
    assert payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] == LINE


@pytest.mark.parametrize("override, code", [
    ({"from_me": True}, "own_message"),
    ({"is_group": True}, "group_message"),
    ({"is_history": True}, "history_message"),
    ({"kind": "image"}, "not_text"),
    ({"timestamp": NOW - 3600}, "message_too_old"),
    ({"timestamp": NOW + 3600}, "message_too_old"),
    ({"from": "12"}, "sender_invalid"),
    ({"message_id": ""}, "message_id_invalid"),
    ({"text": ""}, "text_length"),
    ({"timestamp": "5"}, "timestamp_invalid"),
])
def test_unsafe_or_out_of_scope_items_are_rejected_with_a_reason(override, code):
    with pytest.raises(BridgeRejectedError) as excinfo:
        inbound_to_meta_payload(_item(**override), LINE, now=NOW)
    assert excinfo.value.code == code


# --- Remitente ---

def test_the_sender_never_calls_the_connector_when_auto_reply_is_off():
    calls = []
    sender = _sender(lambda r: (calls.append(r), httpx.Response(200, json={"message_id": "x"}))[1], auto_reply=False)
    assert sender.enabled is False
    with pytest.raises(SendDisabledError):
        sender.send_text(_req())
    assert calls == []


def test_the_test_allowlist_blocks_a_non_listed_client_even_if_auto_reply_is_on():
    calls = []
    sender = _sender(lambda r: (calls.append(r), httpx.Response(200, json={"message_id": "x"}))[1])
    with pytest.raises(RecipientNotAllowedError):
        sender.send_text(_req(to="573009998877"))
    assert calls == []


def test_an_authorized_send_is_signed_and_returns_the_connector_message_id():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = request.read()
        seen["sig"] = request.headers["X-Bridge-Signature"]
        seen["ts"] = request.headers["X-Bridge-Timestamp"]
        return httpx.Response(200, json={"message_id": "BAE5OUT"})

    result = _sender(handler).send_text(_req())
    assert result.message_id == "BAE5OUT"
    assert verify(SECRET, "send", seen["ts"], seen["body"], seen["sig"])
    assert json.loads(seen["body"]) == {"to": CLIENT, "text": "hola", "idempotency_key": "k1"}


def test_connector_refusal_409_means_not_sent():
    with pytest.raises(SendDisabledError):
        _sender(lambda r: httpx.Response(409, json={"error": "killed"})).send_text(_req())


def test_connection_refused_is_a_safe_retry_and_a_timeout_is_uncertain():
    def refused(request):
        raise httpx.ConnectError("refused")

    def timeout(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ConnectionFailedError):
        _sender(refused).send_text(_req())
    with pytest.raises(UncertainSendError):
        _sender(timeout).send_text(_req())


def test_server_errors_are_uncertain_and_client_errors_are_rejected():
    with pytest.raises(UncertainSendError):
        _sender(lambda r: httpx.Response(500, json={"error": "send_failed"})).send_text(_req())
    with pytest.raises(MetaRejectedError):
        _sender(lambda r: httpx.Response(401, json={"error": "invalid_signature"})).send_text(_req())


def test_a_send_from_another_line_is_refused_before_any_request():
    calls = []
    sender = _sender(lambda r: (calls.append(r), httpx.Response(200, json={"message_id": "x"}))[1])
    with pytest.raises(Exception):
        sender.send_text(_req(business="111111111111"))
    assert calls == []


def test_typing_indicator_is_not_supported_and_is_ignored_by_presence():
    with pytest.raises(Exception):
        _sender(lambda r: httpx.Response(200)).show_typing(CLIENT, "wamid.X")


# --- Arranque y auditoría de configuración ---

def test_public_mode_cannot_use_the_web_transport():
    problems = audience_problems(_settings(whatsapp_audience_mode="public", whatsapp_public_confirmation=PUBLIC_CONFIRMATION_TOKEN,
                                           whatsapp_expected_display_number="+573153863179", database_url="postgresql://x",
                                           meta_app_secret="s", meta_send_enabled=True, whatsapp_dry_run=False,
                                           llm_provider="anthropic", agent_dry_run=False, anthropic_enabled=True))
    assert "public_mode_refused_on_web_bridge" in problems


def test_unknown_transport_is_refused_at_startup():
    assert "transport_unknown" in audience_problems(_settings(whatsapp_transport="sms"))


def test_default_transport_is_cloud_api_and_auto_reply_is_off():
    settings = Settings(meta_app_secret=None, meta_verify_token=None)
    assert settings.whatsapp_transport == "cloud_api"
    assert settings.web_bridge_auto_reply is False
    assert settings.active_line_id() is None


def test_active_line_follows_the_transport():
    assert _settings().active_line_id() == LINE
    assert _settings(whatsapp_transport="cloud_api", meta_phone_number_id="123456789012").active_line_id() == "123456789012"


# --- Endpoint interno ---

def _client(settings: Settings, repository=None):
    repo = repository or FakeWebhookEventRepository()
    app = create_app(settings=settings, webhook_repository=repo)
    return TestClient(app), repo


def _live(**overrides):
    # El endpoint compara con el reloj real: los mensajes de estos casos llegan "ahora mismo".
    return _item(timestamp=int(time.time()) - 5, **overrides)


def _post(client, items, *, secret=SECRET, direction="inbound", stamp=None, signature=None):
    body = json.dumps({"messages": items}).encode()
    stamp = stamp or str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
               "X-Bridge-Signature": signature or sign(secret, direction, stamp, body)}
    return client.post("/internal/web-bridge/inbound", content=body, headers=headers)


def test_the_endpoint_is_invisible_unless_web_transport_and_auto_reply_are_on():
    client, _ = _client(_settings(web_bridge_auto_reply=False))
    assert _post(client, [_item()]).status_code == 404
    client, _ = _client(_settings(whatsapp_transport="cloud_api"))
    assert _post(client, [_item()]).status_code == 404


def test_an_unsigned_request_is_rejected_and_stores_nothing():
    client, repo = _client(_settings())
    response = _post(client, [_live()], signature="00")
    assert response.status_code == 401
    assert repo.rows == {}


def test_an_authorized_listed_message_is_stored_once_and_repeats_are_idempotent():
    client, repo = _client(_settings())
    same_message = _live()  # un reenvío real del conector es el MISMO elemento, no uno con otra marca de tiempo
    first = _post(client, [same_message])
    assert first.status_code == 200
    assert {k: first.json()[k] for k in ("accepted", "duplicates", "conflicts", "rejected")} == {
        "accepted": 1, "duplicates": 0, "conflicts": 0, "rejected": 0}
    second = _post(client, [same_message])
    assert second.json()["duplicates"] == 1
    assert list(repo.rows) == ["BAE5ABC"]


def test_messages_from_unlisted_contacts_never_reach_the_queue_in_test_mode():
    client, repo = _client(_settings())
    response = _post(client, [_live(**{"from": "573009998877", "identity": "573009998877"})])
    assert response.json()["accepted"] == 0 and response.json()["rejected"] == 1
    assert repo.rows == {}


def test_rejected_items_are_counted_without_storing_content():
    client, repo = _client(_settings())
    response = _post(client, [_live(from_me=True), _live(is_group=True, message_id="G1")])
    body = response.json()
    assert body["rejected"] == 2 and body["accepted"] == 0
    assert repo.rows == {}
    assert "Hola" not in response.text


def test_too_many_items_are_refused():
    client, _ = _client(_settings())
    assert _post(client, [_live(message_id=f"M{i}") for i in range(21)]).status_code == 400


# --- Modo universal web_inbound_all: ventana de 15 minutos, sin lista de destinatarios ---

from app.integrations.web_bridge import INBOUND_MODE_ALL, inbound_window_state  # noqa: E402

UNIVERSAL_NOW = int(time.time())


def _universal(**overrides):
    base = dict(web_bridge_inbound_mode=INBOUND_MODE_ALL, web_bridge_inbound_until=UNIVERSAL_NOW + 900,
                whatsapp_allowed_recipients=())
    base.update(overrides)
    return _settings(**base)


def test_window_states_for_the_universal_mode():
    assert inbound_window_state(INBOUND_MODE_ALL, UNIVERSAL_NOW + 900, now=UNIVERSAL_NOW) == "active"
    assert inbound_window_state(INBOUND_MODE_ALL, UNIVERSAL_NOW, now=UNIVERSAL_NOW) == "expired"
    assert inbound_window_state(INBOUND_MODE_ALL, None, now=UNIVERSAL_NOW) == "missing"
    assert inbound_window_state(INBOUND_MODE_ALL, UNIVERSAL_NOW + 901, now=UNIVERSAL_NOW) == "invalid"
    assert inbound_window_state("single_chat", None) == "single_chat"
    assert inbound_window_state("otro", None) == "invalid"


def test_universal_sender_sends_to_any_recipient_while_the_window_is_open():
    sender = WebBridgeSender(url="http://127.0.0.1:8787", secret=SECRET, line_id=LINE, auto_reply=True,
                             allowed_recipients=(), audience_mode="test_allowlist",
                             inbound_mode=INBOUND_MODE_ALL, inbound_until=UNIVERSAL_NOW + 600,
                             client=httpx.Client(transport=httpx.MockTransport(
                                 lambda r: httpx.Response(200, json={"message_id": "BAE5ANY"}))))
    assert sender.enabled is True
    assert sender.send_text(_req(to="573009990011")).message_id == "BAE5ANY"


def test_sender_refuses_everything_after_the_window_expires_even_with_auto_reply_on():
    calls = []
    sender = WebBridgeSender(url="http://127.0.0.1:8787", secret=SECRET, line_id=LINE, auto_reply=True,
                             inbound_mode=INBOUND_MODE_ALL, inbound_until=UNIVERSAL_NOW - 1,
                             client=httpx.Client(transport=httpx.MockTransport(
                                 lambda r: (calls.append(r), httpx.Response(200, json={"message_id": "x"}))[1])))
    assert sender.enabled is False
    with pytest.raises(SendDisabledError):
        sender.send_text(_req(to="573009990011"))
    assert calls == []


def test_universal_endpoint_accepts_unlisted_numbers_and_refuses_when_closed():
    client, repo = _client(_universal())
    response = _post(client, [_live(**{"from": "573009990011", "message_id": "U1"})])
    assert response.status_code == 200 and response.json()["accepted"] == 1
    assert "U1" in repo.rows

    closed, repo2 = _client(_universal(web_bridge_inbound_until=UNIVERSAL_NOW - 5))
    refused = _post(closed, [_live(**{"from": "573009990011", "message_id": "U2"})])
    assert refused.status_code == 403 and refused.json()["detail"] == "inbound_window_closed"
    assert repo2.rows == {}


def test_universal_endpoint_still_refuses_own_group_and_history_messages():
    client, repo = _client(_universal())
    response = _post(client, [_live(from_me=True, message_id="O1"), _live(is_group=True, message_id="G1"),
                              _live(is_history=True, message_id="H1")])
    assert response.json()["accepted"] == 0 and response.json()["rejected"] == 3
    assert repo.rows == {}


def test_startup_refuses_the_universal_mode_without_a_valid_window_or_auto_reply():
    assert "inbound_all_window_missing" in audience_problems(_universal(web_bridge_inbound_until=None))
    assert "inbound_all_window_expired" in audience_problems(_universal(web_bridge_inbound_until=UNIVERSAL_NOW - 1))
    assert "inbound_all_window_invalid" in audience_problems(_universal(web_bridge_inbound_until=UNIVERSAL_NOW + 3600))
    assert "inbound_all_requires_auto_reply" in audience_problems(_universal(web_bridge_auto_reply=False))
    assert audience_problems(_universal()) == []


def test_single_chat_mode_is_unchanged_at_startup():
    assert audience_problems(_settings()) == []


# --- Proveedor del worker con transporte web: nunca simulado si puede enviar ---

def test_a_simulated_provider_cannot_send_through_the_web_bridge():
    from app.agent.factory import worker_profile_problems
    simulated = _universal(llm_provider=None, agent_dry_run=True, anthropic_enabled=False)
    assert "simulated_provider_send_refused_web_bridge" in worker_profile_problems(simulated, real_sender=True)


def test_the_real_anthropic_provider_is_allowed_through_the_web_bridge():
    from app.agent.factory import worker_profile_problems
    real = _universal(llm_provider="anthropic", agent_dry_run=False, anthropic_enabled=True)
    assert "simulated_provider_send_refused_web_bridge" not in worker_profile_problems(real, real_sender=True)
