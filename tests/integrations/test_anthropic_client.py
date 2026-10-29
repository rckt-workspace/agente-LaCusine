"""Adaptador de Anthropic probado con un SDK simulado. Sin red: la barrera de tests/network_guard lo impide."""

import logging
import threading
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.integrations.anthropic_client import AnthropicAgentClient
from app.integrations.llm import (
    ChatMessage,
    LLMAuthError,
    LLMBusyError,
    LLMDisabledError,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMUnavailableError,
)

REQUEST_URL = "https://api.anthropic.com/v1/messages"
SECRET_PROMPT = "CONTENIDO-PRIVADO-DEL-CLIENTE-123"


def _http(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", REQUEST_URL))


def _message(text: str = '{"reply_text": "ok"}', stop: str = "end_turn", cache_read: int = 0, cache_write: int = 0):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(
            input_tokens=321, output_tokens=45,
            cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
        ),
    )


class FakeMessages:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSDK:
    def __init__(self, outcomes):
        self.messages = FakeMessages(outcomes)


def _request() -> LLMRequest:
    return LLMRequest(
        call_id="wamid.TEST",
        system="Eres un asistente de prueba.",
        messages=(ChatMessage(role="user", content=SECRET_PROMPT),),
        max_output_tokens=350,
    )


def _client(outcomes, **overrides):
    sleeps: list[float] = []
    sdk = FakeSDK(outcomes)
    options = dict(api_key="test-key-not-real", model="claude-haiku-5-5", timeout_seconds=1.0,
                   max_retries=1, max_concurrency=2, sdk_client=sdk, sleep=sleeps.append)
    options.update(overrides)
    return AnthropicAgentClient(**options), sdk, sleeps


# --- Éxito y mapeo de respuesta ---

def test_success_maps_text_usage_and_model():
    client, sdk, _ = _client([_message()])
    response = client.complete(_request())
    assert response.text == '{"reply_text": "ok"}'
    assert response.model == "claude-haiku-5-5"
    assert response.provider == "anthropic"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (321, 45)
    assert response.truncated is False
    call = sdk.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == 350
    assert call["system"] == [{"type": "text", "text": "Eres un asistente de prueba."}]


def test_truncated_output_is_flagged():
    client, _, _ = _client([_message(stop="max_tokens")])
    assert client.complete(_request()).truncated is True


def test_prompt_caching_is_off_by_default_and_opt_in_when_enabled():
    client, sdk, _ = _client([_message()])
    client.complete(_request())
    assert "cache_control" not in sdk.messages.calls[0]["system"][0]

    client2, sdk2, _ = _client([_message()], prompt_caching=True)
    client2.complete(_request())
    assert sdk2.messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_token_counts_are_mapped():
    client, _, _ = _client([_message(cache_read=700, cache_write=50)])
    usage = client.complete(_request()).usage
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (700, 50)


# --- Configuración obligatoria ---

def test_missing_api_key_disables_client_without_building_sdk():
    with pytest.raises(LLMDisabledError) as excinfo:
        AnthropicAgentClient(api_key=None, model="claude-haiku-5-5")
    assert excinfo.value.code == "missing_api_key"


def test_missing_model_disables_client():
    with pytest.raises(LLMDisabledError) as excinfo:
        AnthropicAgentClient(api_key="k", model=None)
    assert excinfo.value.code == "missing_model"


@pytest.mark.parametrize("kwargs", [{"max_retries": 4}, {"max_retries": -1}, {"max_concurrency": 0}])
def test_invalid_retry_and_concurrency_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        AnthropicAgentClient(api_key="k", model="m", sdk_client=FakeSDK([]), **kwargs)


# --- Errores y reintentos ---

def test_authentication_error_is_not_retried_and_hides_sdk_message():
    error = anthropic.AuthenticationError("clave CLAVE-FICTICIA-DE-PRUEBA inválida", response=_http(401), body=None)
    client, sdk, sleeps = _client([error, _message()])
    with pytest.raises(LLMAuthError) as excinfo:
        client.complete(_request())
    assert len(sdk.messages.calls) == 1
    assert sleeps == []
    assert "sk-ant" not in str(excinfo.value)


def test_rate_limit_is_retried_with_backoff_then_succeeds():
    error = anthropic.RateLimitError("límite", response=_http(429), body=None)
    client, sdk, sleeps = _client([error, _message()], max_retries=2)
    assert client.complete(_request()).text
    assert len(sdk.messages.calls) == 2
    assert sleeps == [0.5]


def test_rate_limit_stops_after_configured_retries():
    error = anthropic.RateLimitError("límite", response=_http(429), body=None)
    client, sdk, sleeps = _client([error, error, error], max_retries=1)
    with pytest.raises(LLMRateLimitError):
        client.complete(_request())
    assert len(sdk.messages.calls) == 2
    assert sleeps == [0.5]


def test_overload_529_is_retried_as_unavailable():
    overloaded = anthropic.APIStatusError("sobrecarga", response=_http(529), body=None)
    client, sdk, _ = _client([overloaded, _message()], max_retries=1)
    assert client.complete(_request()).text
    assert len(sdk.messages.calls) == 2


def test_server_error_exhausts_retries_as_unavailable():
    error = anthropic.InternalServerError("servidor", response=_http(500), body=None)
    client, sdk, _ = _client([error, error], max_retries=1)
    with pytest.raises(LLMUnavailableError):
        client.complete(_request())
    assert len(sdk.messages.calls) == 2


def test_timeout_is_never_retried_because_generation_may_be_billed():
    client, sdk, sleeps = _client([anthropic.APITimeoutError(request=httpx.Request("POST", REQUEST_URL)), _message()],
                                  max_retries=3)
    with pytest.raises(LLMTimeoutError):
        client.complete(_request())
    assert len(sdk.messages.calls) == 1
    assert sleeps == []


def test_connection_failure_is_not_retried():
    error = anthropic.APIConnectionError(message="sin conexión", request=httpx.Request("POST", REQUEST_URL))
    client, sdk, _ = _client([error, _message()], max_retries=3)
    with pytest.raises(LLMError) as excinfo:
        client.complete(_request())
    assert excinfo.value.code == "connection_failed"
    assert len(sdk.messages.calls) == 1


def test_client_error_400_is_not_retried():
    error = anthropic.APIStatusError("solicitud inválida", response=_http(400), body=None)
    client, sdk, _ = _client([error, _message()], max_retries=3)
    with pytest.raises(LLMError) as excinfo:
        client.complete(_request())
    assert excinfo.value.code == "api_status_error"
    assert len(sdk.messages.calls) == 1


# --- Concurrencia ---

def test_busy_when_no_concurrency_slot_is_available_within_timeout():
    client, sdk, _ = _client([_message()], max_concurrency=1, timeout_seconds=0.05)
    held = client._semaphore.acquire()
    assert held
    try:
        with pytest.raises(LLMBusyError):
            client.complete(_request())
        assert sdk.messages.calls == []
    finally:
        client._semaphore.release()


def test_slot_is_released_after_failure_so_next_call_can_proceed():
    error = anthropic.AuthenticationError("x", response=_http(401), body=None)
    client, _, _ = _client([error, _message()], max_concurrency=1)
    with pytest.raises(LLMAuthError):
        client.complete(_request())
    assert client.complete(_request()).text


# --- Logs seguros ---

def test_logs_contain_identifiers_and_counts_but_never_prompt_text(caplog):
    client, _, _ = _client([_message()])
    with caplog.at_level(logging.DEBUG, logger="lacusine.agent.anthropic"):
        client.complete(_request())
    combined = " ".join(record.getMessage() for record in caplog.records)
    assert "call_id=wamid.TEST" in combined
    assert "in=321" in combined
    assert SECRET_PROMPT not in combined
    assert "Eres un asistente" not in combined


def test_threads_are_bounded_by_semaphore_under_parallel_calls():
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    class SlowMessages(FakeMessages):
        def create(self, **kwargs):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            import time
            time.sleep(0.02)
            with lock:
                active["now"] -= 1
            return _message()

    sdk = SimpleNamespace(messages=SlowMessages([]))
    client = AnthropicAgentClient(api_key="k", model="m", max_concurrency=2, sdk_client=sdk, sleep=lambda s: None,
                                  timeout_seconds=5)
    threads = [threading.Thread(target=client.complete, args=(_request(),)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["peak"] <= 2
