"""Adaptador de OpenRouter con transporte simulado (httpx.MockTransport). Ninguna solicitud sale de la máquina."""

import inspect
import json
import logging

import httpx
import pytest

from app.integrations.llm import (
    LLMAuthError,
    LLMBusyError,
    LLMDisabledError,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    ChatMessage,
)
from app.integrations.openrouter_client import (
    OFFICIAL_BASE_URL,
    OpenRouterAgentClient,
)

KEY = "sk-or-CLAVE-FICTICIA-NO-DEBE-APARECER-0123456789"
MODEL = "openrouter/test-model"
OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": '{"reply_text": "Hola"}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 40},
}


def _request(max_output_tokens=350):
    return LLMRequest(
        call_id="wamid.OR1",
        system="Eres el asistente comercial.",
        messages=(ChatMessage(role="user", content="¿Qué cafeteras tienen?"),),
        max_output_tokens=max_output_tokens,
    )


def _client(handler, **overrides):
    options = dict(api_key=KEY, model=MODEL, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    options.update(overrides)
    return OpenRouterAgentClient(**options)


def _json(body, status=200):
    return lambda request: httpx.Response(status, json=body)


def _counting(response_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return response_factory(request)

    return handler, calls


# --- Configuración ---

def test_base_url_is_fixed_and_not_configurable():
    assert OFFICIAL_BASE_URL == "https://openrouter.ai/api/v1"
    assert "base_url" not in inspect.signature(OpenRouterAgentClient).parameters
    client = OpenRouterAgentClient(api_key=KEY, model=MODEL)
    assert str(client._client.base_url).rstrip("/") == OFFICIAL_BASE_URL


@pytest.mark.parametrize("kwargs", [{"api_key": None, "model": MODEL}, {"api_key": "", "model": MODEL}])
def test_missing_key_fails_closed(kwargs):
    with pytest.raises(LLMDisabledError) as excinfo:
        OpenRouterAgentClient(**kwargs)
    assert excinfo.value.code == "missing_api_key"


def test_missing_model_fails_closed():
    with pytest.raises(LLMDisabledError) as excinfo:
        OpenRouterAgentClient(api_key=KEY, model=None)
    assert excinfo.value.code == "missing_model"


@pytest.mark.parametrize("retries", [-1, 3])
def test_retry_limits_are_enforced(retries):
    with pytest.raises(ValueError):
        OpenRouterAgentClient(api_key=KEY, model=MODEL, max_retries=retries)


def test_representation_never_shows_the_key():
    assert KEY not in repr(_client(_json(OK_BODY)))


# --- Solicitud ---

def test_request_targets_the_official_chat_completions_endpoint_with_expected_body():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    _client(handler).complete(_request())
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert body["model"] == MODEL
    assert body["max_tokens"] == 350
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "Eres el asistente comercial."}
    assert body["messages"][1] == {"role": "user", "content": "¿Qué cafeteras tienen?"}
    assert set(body) == {"model", "messages", "max_tokens", "stream"}


# --- Respuesta correcta ---

def test_success_maps_text_usage_and_provider():
    response = _client(_json(OK_BODY)).complete(_request())
    assert response.text == '{"reply_text": "Hola"}'
    assert response.provider == "openrouter"
    assert response.model == MODEL
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 40)
    assert response.truncated is False


def test_finish_reason_length_is_reported_as_truncated():
    body = {**OK_BODY, "choices": [{**OK_BODY["choices"][0], "finish_reason": "length"}]}
    assert _client(_json(body)).complete(_request()).truncated is True


def test_missing_usage_is_charged_at_worst_case_never_zero():
    body = {"choices": OK_BODY["choices"]}
    response = _client(_json(body)).complete(_request(max_output_tokens=350))
    assert response.usage.output_tokens == 350
    assert response.usage.input_tokens > 0


@pytest.mark.parametrize("usage", [{"prompt_tokens": True, "completion_tokens": 4},
                                   {"prompt_tokens": 12.0, "completion_tokens": 4},
                                   {"prompt_tokens": -1, "completion_tokens": 4}])
def test_invalid_usage_counts_fall_back_to_worst_case(usage):
    body = {**OK_BODY, "usage": usage}
    response = _client(_json(body)).complete(_request(max_output_tokens=350))
    assert response.usage.output_tokens == 350


# --- Errores HTTP: facturación ---

@pytest.mark.parametrize("status", [401, 403])
def test_authentication_errors_are_not_billed_and_not_retried(status):
    handler, calls = _counting(lambda r: httpx.Response(status, json={"error": "secreto del proveedor"}))
    with pytest.raises(LLMAuthError) as excinfo:
        _client(handler, max_retries=2).complete(_request())
    assert excinfo.value.billed_state == "not_billed"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 402, 404, 422])
def test_provider_rejections_before_generation_are_not_billed(status):
    handler, calls = _counting(lambda r: httpx.Response(status, json={"error": "modelo no disponible"}))
    with pytest.raises(LLMError) as excinfo:
        _client(handler, max_retries=2).complete(_request())
    assert excinfo.value.code == "provider_rejected"
    assert excinfo.value.billed_state == "not_billed"
    assert len(calls) == 1


def test_rate_limit_is_retried_then_succeeds():
    responses = [httpx.Response(429, json={}), httpx.Response(200, json=OK_BODY)]
    handler, calls = _counting(lambda r: responses.pop(0))
    response = _client(handler, max_retries=1).complete(_request())
    assert response.text.startswith("{")
    assert len(calls) == 2


def test_rate_limit_exhausted_raises_retryable_not_billed_error():
    handler, calls = _counting(lambda r: httpx.Response(429, json={}))
    with pytest.raises(LLMRateLimitError) as excinfo:
        _client(handler, max_retries=1).complete(_request())
    assert excinfo.value.retryable is True
    assert excinfo.value.billed_state == "not_billed"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_uncertain_and_never_retried(status):
    handler, calls = _counting(lambda r: httpx.Response(status, json={}))
    with pytest.raises(LLMError) as excinfo:
        _client(handler, max_retries=2).complete(_request())
    assert excinfo.value.code == "provider_unavailable"
    assert excinfo.value.billed_state == "uncertain"
    assert excinfo.value.retryable is False
    assert len(calls) == 1


# --- Transporte: facturación incierta, sin reintento ---

def test_timeout_is_uncertain_and_never_retried():
    def handler(request):
        raise httpx.ReadTimeout("sin respuesta", request=request)

    handler_calls = []

    def counting(request):
        handler_calls.append(request)
        return handler(request)

    with pytest.raises(LLMTimeoutError) as excinfo:
        _client(counting, max_retries=2).complete(_request())
    assert excinfo.value.billed_state == "uncertain"
    assert len(handler_calls) == 1


def test_connection_error_is_uncertain_and_never_retried():
    def handler(request):
        raise httpx.ConnectError("sin conexión", request=request)

    with pytest.raises(LLMError) as excinfo:
        _client(handler, max_retries=2).complete(_request())
    assert excinfo.value.code == "connection_failed"
    assert excinfo.value.billed_state == "uncertain"


# --- Respuestas inválidas ---

@pytest.mark.parametrize("body", [
    "no es json",
    {"choices": []},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": [{"message": {"content": None}}]},
    {"choices": ["no es objeto"]},
    ["lista, no objeto"],
])
def test_invalid_responses_are_uncertain_and_not_retried(body):
    if isinstance(body, str):
        handler, calls = _counting(lambda r: httpx.Response(200, text=body))
    else:
        handler, calls = _counting(lambda r: httpx.Response(200, json=body))
    with pytest.raises(LLMError) as excinfo:
        _client(handler, max_retries=2).complete(_request())
    assert excinfo.value.code == "invalid_response"
    assert excinfo.value.billed_state == "uncertain"
    assert len(calls) == 1


# --- Concurrencia ---

def test_no_free_slot_returns_busy_without_calling_the_provider():
    handler, calls = _counting(_json(OK_BODY))
    client = _client(handler, timeout_seconds=0.01, max_concurrency=1)
    assert client._semaphore.acquire()
    try:
        with pytest.raises(LLMBusyError):
            client.complete(_request())
    finally:
        client._semaphore.release()
    assert calls == []


# --- Secretos y registros ---

def test_key_never_appears_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(401, json={"error": f"clave inválida {KEY}"})

    with pytest.raises(LLMAuthError) as excinfo:
        _client(handler).complete(_request())
    assert KEY not in str(excinfo.value) and KEY not in repr(excinfo.value)
    assert KEY not in caplog.text


def test_successful_call_logs_only_identifiers_and_counts(caplog):
    caplog.set_level(logging.INFO)
    _client(_json(OK_BODY)).complete(_request())
    assert KEY not in caplog.text
    assert "Hola" not in caplog.text and "cafeteras" not in caplog.text
    assert "llm_call_completed provider=openrouter" in caplog.text
