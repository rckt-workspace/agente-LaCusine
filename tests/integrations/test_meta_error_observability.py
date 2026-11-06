"""Observabilidad de errores de Meta (envío e indicador) con respuestas HTTP simuladas. Sin red real."""

import json
import logging

import httpx
import pytest

from app.integrations.whatsapp_client import (
    AuthRejectedError,
    CloudApiSender,
    MetaRejectedError,
    RateLimitedError,
    SendRequest,
    parse_meta_error,
    redact_details,
)
from tests.worker.test_dispatcher import PHONE, FakeOutbound, _dispatcher

RECIPIENT = "573001112233"
SECRET_TOKEN = "EAAG-TOKEN-QUE-NUNCA-DEBE-SALIR"
CUSTOMER_TEXT = "mensaje privado del cliente 987654"


def _error(code=None, error_type="OAuthException", subcode=None, details=None, fbtrace="AbC123xyz"):
    error = {"message": f"Mensaje de Meta para {RECIPIENT}", "type": error_type, "fbtrace_id": fbtrace}
    if code is not None:
        error["code"] = code
    if subcode is not None:
        error["error_subcode"] = subcode
    if details is not None:
        error["error_data"] = {"messaging_product": "whatsapp", "details": details}
    return {"error": error}


def _sender(status, body, counter):
    def handler(request):
        counter.append(request)
        return httpx.Response(status, json=body)

    return CloudApiSender(
        access_token=SECRET_TOKEN, phone_number_id=PHONE, graph_api_version="v26.0",
        allowed_recipients=(RECIPIENT,), send_enabled=True, dry_run=False,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _send(sender):
    return sender.send_text(SendRequest(to=RECIPIENT, text=CUSTOMER_TEXT, business_phone_id=PHONE,
                                        idempotency_key="wamid.X:reply"))


def test_accepted_send_returns_the_message_id_and_logs_no_error(caplog):
    calls = []
    body = {"messages": [{"id": "wamid.ACCEPTED1"}]}
    with caplog.at_level(logging.WARNING, logger="lacusine.whatsapp"):
        result = _send(_sender(200, body, calls))
    assert result.message_id == "wamid.ACCEPTED1"
    assert len(calls) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("code, expected_class", [
    (100, MetaRejectedError),       # parámetro no soportado
    (131009, MetaRejectedError),    # valor de parámetro inválido
    (131030, MetaRejectedError),    # destinatario no permitido (lista de prueba)
    (131047, MetaRejectedError),    # ventana de 24 h cerrada: requiere plantilla
    (131026, MetaRejectedError),    # no entregable
])
def test_definitive_meta_400_keeps_the_specific_code_and_is_not_retried(code, expected_class):
    calls = []
    with pytest.raises(expected_class) as info:
        _send(_sender(400, _error(code=code, details="Detalle para 573001112233"), calls))
    assert info.value.code == f"meta_{code}"  # no se sustituye por http_400
    assert len(calls) == 1  # un solo intento HTTP por envío


@pytest.mark.parametrize("code", [130429, 131056, 80007])
def test_documented_transient_meta_codes_are_retryable(code):
    calls = []
    with pytest.raises(RateLimitedError) as info:
        _send(_sender(400, _error(code=code), calls))
    assert info.value.code == f"meta_{code}"
    assert len(calls) == 1


def test_meta_401_with_expired_session_code_is_auth_rejected_and_keeps_190():
    calls = []
    with pytest.raises(AuthRejectedError) as info:
        _send(_sender(401, _error(code=190, subcode=463), calls))
    assert info.value.code == "meta_190"
    assert len(calls) == 1


def test_400_without_a_meta_code_falls_back_to_the_http_status():
    calls = []
    with pytest.raises(MetaRejectedError) as info:
        _send(_sender(400, {"unexpected": True}, calls))
    assert info.value.code == "http_400"


def test_logs_carry_the_meta_code_type_and_trace_but_never_secrets_or_customer_text(caplog):
    calls = []
    with caplog.at_level(logging.WARNING, logger="lacusine.whatsapp"), pytest.raises(MetaRejectedError):
        _send(_sender(400, _error(code=131030, details="No permitido para 573001112233 con " + SECRET_TOKEN),
                      calls))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "operation=send" in text and "code=131030" in text
    assert "type=OAuthException" in text and "fbtrace_id=AbC123xyz" in text
    assert SECRET_TOKEN not in text
    assert "573001112233" not in text  # número completo redactado
    assert CUSTOMER_TEXT not in text
    assert "EAAG" not in text


def test_typing_failures_are_reported_as_typing_and_never_as_send_failures(caplog):
    calls = []
    sender = _sender(400, _error(code=131009, details="valor no válido"), calls)
    with caplog.at_level(logging.WARNING, logger="lacusine.whatsapp"), pytest.raises(MetaRejectedError) as info:
        sender.show_typing(RECIPIENT, "wamid.HBgMNTczTEST")
    assert info.value.code == "typing_meta_131009"
    assert "operation=typing" in " ".join(r.getMessage() for r in caplog.records)
    assert len(calls) == 1


def test_redaction_removes_tokens_and_long_numbers_and_limits_length():
    raw = "Bearer abc.def " + SECRET_TOKEN + " para 573001112233 " + "x" * 400
    cleaned = redact_details(raw)
    assert "Bearer abc" not in cleaned and SECRET_TOKEN not in cleaned
    assert "573001112233" not in cleaned and "[numero]" in cleaned
    assert len(cleaned) <= 160


def test_parse_meta_error_handles_non_json_bodies():
    response = httpx.Response(502, content=b"<html>gateway</html>")
    info = parse_meta_error(response)
    assert info.code is None and info.specific_code() == "http_502"


# --- Un solo envío por reclamo; los códigos definitivos no reintentan ---

def test_dispatcher_fails_a_definitive_meta_rejection_once_and_never_resends():
    repo = FakeOutbound(wa=RECIPIENT)
    repo.add(attempts=1)
    calls = []
    sender = _sender(400, _error(code=131047), calls)
    dispatcher = _dispatcher(repo, sender)
    dispatcher.dispatch_once()
    dispatcher.dispatch_once()
    assert repo.finished[0]["status"] == "failed"
    assert repo.finished[0]["error_code"] == "meta_131047"
    assert len(calls) == 1


def test_dispatcher_schedules_a_transient_meta_rejection_for_retry_with_its_code():
    repo = FakeOutbound(wa=RECIPIENT)
    repo.add(attempts=1)
    calls = []
    _dispatcher(repo, _sender(400, _error(code=130429), calls)).dispatch_once()
    assert repo.finished[0]["status"] == "pending"
    assert repo.finished[0]["error_code"] == "meta_130429"
    assert len(calls) == 1


def test_json_body_is_the_only_source_of_the_meta_code():
    response = httpx.Response(400, content=json.dumps(_error(code=100)).encode())
    assert parse_meta_error(response).specific_code() == "meta_100"
