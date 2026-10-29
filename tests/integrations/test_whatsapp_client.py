"""Adaptador de WhatsApp Cloud API con transporte HTTP simulado. Ninguna solicitud sale de la máquina."""

import json
import threading

import httpx
import pytest

from app.integrations.whatsapp_client import (
    WhatsAppError,
    AuthRejectedError,
    BusinessNumberMismatchError,
    BusyError,
    CloudApiSender,
    ConnectionFailedError,
    MetaRejectedError,
    PayloadRejectedError,
    RateLimitedError,
    RecipientNotAllowedError,
    SendConfigError,
    SendDisabledError,
    SendRequest,
    SimulatedWhatsAppSender,
    UncertainSendError,
)

TOKEN = "TOKEN-FICTICIO-NO-DEBE-APARECER"
PHONE_ID = "123456789012"
VERSION = "v26.0"
RECIPIENT = "573001112233"


def _request(**overrides) -> SendRequest:
    base = dict(to=RECIPIENT, text="Hola, tenemos varias opciones.", business_phone_id=PHONE_ID,
                idempotency_key="wamid.K1:reply")
    base.update(overrides)
    return SendRequest(**base)


def _sender(handler=None, *, enabled=True, dry_run=False, allowed=(RECIPIENT,), phone=PHONE_ID, token=TOKEN,
            version=VERSION, max_concurrency=2, timeout=5.0):
    transport = httpx.MockTransport(handler or _must_not_call)
    client = httpx.Client(transport=transport, timeout=timeout)
    return CloudApiSender(
        access_token=token, phone_number_id=phone, graph_api_version=version,
        allowed_recipients=allowed, send_enabled=enabled, dry_run=dry_run,
        timeout_seconds=timeout, max_concurrency=max_concurrency, client=client,
    )


def _must_not_call(request: httpx.Request) -> httpx.Response:
    raise AssertionError("se intentó una solicitud HTTP real o no permitida")


def _ok(message_id="wamid.HBgNNTczMDAx"):
    def handler(request):
        return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": message_id}]})
    return handler


def _status(code):
    def handler(request):
        return httpx.Response(code, json={"error": {"message": "detalle", "code": code}})
    return handler


# --- Seguridad por defecto ---

def test_sending_is_disabled_by_default_and_never_calls_the_network():
    sender = CloudApiSender(access_token=TOKEN, phone_number_id=PHONE_ID, graph_api_version=VERSION,
                            allowed_recipients=(RECIPIENT,), send_enabled=False, dry_run=True)
    assert sender.enabled is False
    with pytest.raises(SendDisabledError):
        sender.send_text(_request())


def test_api_key_present_but_send_disabled_does_not_send():
    sender = _sender(enabled=False, dry_run=False)
    with pytest.raises(SendDisabledError):
        sender.send_text(_request())


def test_dry_run_blocks_send_even_when_send_flag_is_true():
    sender = _sender(enabled=True, dry_run=True)
    assert sender.enabled is False
    with pytest.raises(SendDisabledError):
        sender.send_text(_request())


def test_enabling_send_without_required_variables_fails_closed_and_names_only():
    with pytest.raises(SendConfigError) as excinfo:
        CloudApiSender(access_token=None, phone_number_id=None, graph_api_version=None,
                       allowed_recipients=(RECIPIENT,), send_enabled=True, dry_run=False)
    assert "META_ACCESS_TOKEN" in str(excinfo.value)
    assert "META_PHONE_NUMBER_ID" in str(excinfo.value)


def test_enabling_send_with_empty_recipient_allowlist_fails_closed():
    with pytest.raises(SendConfigError, match="recipients_allowlist_empty"):
        CloudApiSender(access_token=TOKEN, phone_number_id=PHONE_ID, graph_api_version=VERSION,
                       allowed_recipients=(), send_enabled=True, dry_run=False)


def test_invalid_phone_number_id_format_fails_closed():
    with pytest.raises(SendConfigError, match="phone_number_id_format"):
        CloudApiSender(access_token=TOKEN, phone_number_id="no-numerico", graph_api_version=VERSION,
                       allowed_recipients=(RECIPIENT,), send_enabled=True, dry_run=False)


def test_recipient_outside_allowlist_is_rejected_before_any_request():
    sender = _sender()
    with pytest.raises(RecipientNotAllowedError):
        sender.send_text(_request(to="573009999999"))


def test_business_number_mismatch_is_rejected_before_any_request():
    sender = _sender()
    with pytest.raises(BusinessNumberMismatchError):
        sender.send_text(_request(business_phone_id="999999999999"))


@pytest.mark.parametrize("request_kwargs, code", [
    ({"to": "abc"}, "recipient_format"),
    ({"to": "+57300"}, "recipient_format"),
    ({"text": ""}, "text_length"),
    ({"text": "x" * 4097}, "text_length"),
])
def test_invalid_payload_is_rejected_before_any_request(request_kwargs, code):
    sender = _sender(allowed=(RECIPIENT, "abc", "+57300"))
    with pytest.raises(PayloadRejectedError) as excinfo:
        sender.send_text(_request(**request_kwargs))
    assert excinfo.value.code == code


# --- Solicitud correcta ---

def test_successful_send_builds_official_endpoint_and_returns_message_id():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    result = _sender(handler).send_text(_request())
    assert result.message_id == "wamid.ABC" and result.http_status == 200
    assert seen["url"] == f"https://graph.facebook.com/{VERSION}/{PHONE_ID}/messages"
    assert seen["body"]["messaging_product"] == "whatsapp"
    assert seen["body"]["to"] == RECIPIENT
    assert seen["body"]["text"]["body"] == "Hola, tenemos varias opciones."
    assert seen["auth"] == f"Bearer {TOKEN}"


def test_http_201_is_also_accepted():
    result = _sender(_ok("wamid.201")).send_text(_request())
    assert result.message_id == "wamid.201"


# --- Errores tipados con resultado explícito ---

@pytest.mark.parametrize("code, error_class, outcome", [
    (401, AuthRejectedError, "not_sent"),
    (403, AuthRejectedError, "not_sent"),
    (400, MetaRejectedError, "not_sent"),
    (404, MetaRejectedError, "not_sent"),
    (429, RateLimitedError, "not_sent"),
    (500, UncertainSendError, "uncertain"),
    (503, UncertainSendError, "uncertain"),
])
def test_http_errors_are_classified_by_whether_the_message_may_have_been_accepted(code, error_class, outcome):
    with pytest.raises(error_class) as excinfo:
        _sender(_status(code)).send_text(_request())
    assert excinfo.value.outcome == outcome


def test_rate_limit_is_retryable_and_auth_is_not():
    with pytest.raises(RateLimitedError) as excinfo:
        _sender(_status(429)).send_text(_request())
    assert excinfo.value.retryable is True
    with pytest.raises(AuthRejectedError) as excinfo:
        _sender(_status(401)).send_text(_request())
    assert excinfo.value.retryable is False


def test_success_without_message_id_is_uncertain_not_sent():
    def handler(request):
        return httpx.Response(200, json={"messaging_product": "whatsapp"})
    with pytest.raises(UncertainSendError) as excinfo:
        _sender(handler).send_text(_request())
    assert excinfo.value.code == "response_without_message_id"


def test_non_json_success_is_uncertain():
    with pytest.raises(UncertainSendError):
        _sender(lambda r: httpx.Response(200, content=b"<html>")).send_text(_request())


def test_read_timeout_after_sending_is_uncertain_never_retryable():
    def handler(request):
        raise httpx.ReadTimeout("sin respuesta", request=request)
    with pytest.raises(UncertainSendError) as excinfo:
        _sender(handler).send_text(_request())
    assert excinfo.value.code == "timeout"
    assert excinfo.value.retryable is False


def test_connection_failure_means_not_sent_and_is_retryable():
    def handler(request):
        raise httpx.ConnectError("sin conexión", request=request)
    with pytest.raises(ConnectionFailedError) as excinfo:
        _sender(handler).send_text(_request())
    assert excinfo.value.outcome == "not_sent"
    assert excinfo.value.retryable is True


def test_token_never_appears_in_error_messages():
    def handler(request):
        raise httpx.ReadTimeout(f"fallo con {TOKEN}", request=request)
    with pytest.raises(UncertainSendError) as excinfo:
        _sender(handler).send_text(_request())
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(excinfo.value)


def test_concurrency_limit_returns_busy_without_request():
    sender = _sender(max_concurrency=1, timeout=0.05)
    assert sender._semaphore.acquire()
    try:
        with pytest.raises(BusyError):
            sender.send_text(_request())
    finally:
        sender._semaphore.release()


# --- Simulador ---

def test_simulated_sender_returns_deterministic_ids_and_records_requests():
    simulator = SimulatedWhatsAppSender()
    first = simulator.send_text(_request(idempotency_key="k1"))
    second = simulator.send_text(_request(idempotency_key="k2"))
    assert first.message_id != second.message_id
    assert [r.idempotency_key for r in simulator.sent] == ["k1", "k2"]


def test_simulated_sender_can_script_failures():
    simulator = SimulatedWhatsAppSender(script=[UncertainSendError("timeout")])
    with pytest.raises(UncertainSendError):
        simulator.send_text(_request())
    assert simulator.send_text(_request()).message_id.startswith("wamid.SIM")


def test_simulated_sender_validates_payload_like_the_real_one():
    with pytest.raises(PayloadRejectedError):
        SimulatedWhatsAppSender().send_text(_request(to="no-es-numero"))


def test_simulated_sender_is_thread_safe_under_concurrency():
    simulator = SimulatedWhatsAppSender()
    threads = [threading.Thread(target=simulator.send_text, args=(_request(idempotency_key=f"k{i}"),))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {r.idempotency_key for r in simulator.sent}
    assert len(ids) == 20


# --- Clasificación de errores de transporte ---

@pytest.mark.parametrize("error_factory, outcome", [
    (lambda req: httpx.ConnectError("sin conexión", request=req), "not_sent"),
    (lambda req: httpx.ConnectTimeout("handshake incompleto", request=req), "not_sent"),
    (lambda req: httpx.PoolTimeout("sin conexiones libres", request=req), "not_sent"),
    (lambda req: httpx.ReadTimeout("sin respuesta", request=req), "uncertain"),
    (lambda req: httpx.ReadError("conexión cortada tras enviar", request=req), "uncertain"),
    (lambda req: httpx.WriteError("fallo al escribir el cuerpo", request=req), "uncertain"),
    (lambda req: httpx.RemoteProtocolError("servidor cerró sin respuesta", request=req), "uncertain"),
])
def test_transport_errors_are_classified_by_whether_the_request_may_have_left(error_factory, outcome):
    def handler(request):
        raise error_factory(request)

    with pytest.raises(WhatsAppError) as excinfo:
        _sender(handler).send_text(_request())
    assert excinfo.value.outcome == outcome


def test_a_lost_http_response_is_never_reported_as_a_rejection():
    def handler(request):
        raise httpx.ReadError("sin respuesta", request=request)

    with pytest.raises(WhatsAppError) as excinfo:
        _sender(handler).send_text(_request())
    assert not isinstance(excinfo.value, MetaRejectedError)
    assert excinfo.value.outcome == "uncertain"
    assert excinfo.value.retryable is False
