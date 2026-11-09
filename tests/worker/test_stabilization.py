"""Estabilización: errores de Anthropic, transiciones de reserva, resiliencia del bucle y tiempos. Sin red."""

from __future__ import annotations

import threading

import anthropic
import httpx
import pytest

from app.agent.orchestrator import CommercialAgent as Orchestrator
from app.integrations.anthropic_client import AnthropicAgentClient, worst_case_call_seconds
from app.integrations.llm import ChatMessage, LLMRequest, LLMTimeoutError
from app.repositories.errors import StorageError
from app.worker.config import WorkerSettings
from app.worker.runner import Worker as WorkerRunner

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class FakeMessages:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        raise self.errors.pop(0)


class FakeSdk:
    def __init__(self, errors):
        self.messages = FakeMessages(errors)


def _client(errors, max_retries=1):
    sleeps: list[float] = []
    sdk = FakeSdk(errors)
    client = AnthropicAgentClient(api_key="clave-de-prueba", model="claude-sonnet-5-5", timeout_seconds=25,
                                  max_retries=max_retries, sdk_client=sdk, sleep=sleeps.append)
    return client, sdk, sleeps


def _request() -> LLMRequest:
    return LLMRequest(call_id="call-1", system="sistema", messages=(ChatMessage("user", "hola"),), max_output_tokens=700)


# --- A. Errores del cliente Anthropic: estado explícito y reintentos acotados ---

def test_timeout_is_never_retried_because_the_generation_may_have_been_billed():
    client, sdk, sleeps = _client([anthropic.APITimeoutError(request=REQ)], max_retries=1)
    with pytest.raises(LLMTimeoutError) as excinfo:
        client.complete(_request())
    assert excinfo.value.code == "timeout"
    assert sdk.messages.calls == 1 and sleeps == []


def test_connection_error_is_not_retried_because_the_request_may_have_arrived():
    client, sdk, sleeps = _client([anthropic.APIConnectionError(request=REQ)], max_retries=1)
    with pytest.raises(Exception) as excinfo:
        client.complete(_request())
    assert getattr(excinfo.value, "code", None) == "connection_failed"
    assert sdk.messages.calls == 1 and sleeps == []


def test_rate_limit_429_is_retried_within_the_configured_bound_then_reported():
    resp = httpx.Response(429, request=REQ)
    client, sdk, sleeps = _client([anthropic.RateLimitError("limite", response=resp, body=None),
                                   anthropic.RateLimitError("limite", response=resp, body=None)], max_retries=1)
    with pytest.raises(Exception) as excinfo:
        client.complete(_request())
    assert getattr(excinfo.value, "code", None) == "rate_limited"
    assert sdk.messages.calls == 2 and len(sleeps) == 1


def test_server_overload_5xx_is_retried_within_the_bound():
    resp = httpx.Response(503, request=REQ)
    client, sdk, sleeps = _client([anthropic.APIStatusError("sobrecarga", response=resp, body=None)] * 2, max_retries=1)
    with pytest.raises(Exception) as excinfo:
        client.complete(_request())
    assert getattr(excinfo.value, "code", None) == "unavailable"
    assert sdk.messages.calls == 2


def test_authentication_error_is_never_retried():
    resp = httpx.Response(401, request=REQ)
    client, sdk, sleeps = _client([anthropic.AuthenticationError("auth", response=resp, body=None)], max_retries=1)
    with pytest.raises(Exception):
        client.complete(_request())
    assert sdk.messages.calls == 1 and sleeps == []


def test_the_retry_budget_never_exceeds_the_lease_budget_with_default_settings():
    # Timeout 25 s y 1 reintento: peor caso 75.5 s. Con lease de 120 s queda margen de sobra.
    worst = worst_case_call_seconds(25, 1)
    assert worst == pytest.approx(75.5)
    assert 120 >= worst + 30


def test_raising_the_timeout_to_45s_would_exceed_the_default_lease():
    # Motivo por el que NO se sube el timeout de forma arbitraria: el lease quedaría insuficiente.
    assert worst_case_call_seconds(45, 1) > 120 - 30


# --- B. Transiciones de reserva: reintentos acotados, sin llamar al proveedor ---

class FlakyLedger:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def mark_uncertain(self, call_id):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise StorageError("db_unavailable")


def test_a_transient_failure_when_marking_uncertain_is_retried_and_eventually_applied(monkeypatch):
    monkeypatch.setattr("app.agent.orchestrator.time.sleep", lambda s: None)
    ledger = FlakyLedger(failures=2)
    assert Orchestrator._transition_with_retry(None, "call-1", ledger.mark_uncertain) is True
    assert ledger.calls == 3


def test_a_persistent_failure_keeps_the_reservation_reserved_for_reconciliation(monkeypatch):
    monkeypatch.setattr("app.agent.orchestrator.time.sleep", lambda s: None)
    ledger = FlakyLedger(failures=10)
    assert Orchestrator._transition_with_retry(None, "call-1", ledger.mark_uncertain) is False
    assert ledger.calls == 3  # acotado: nunca bucle infinito


# --- C. Resiliencia del bucle del worker: un error inesperado no detiene el proceso ---

class _Jobs:
    def __init__(self):
        self.recover_calls = 0

    def recover_expired(self, max_attempts):
        self.recover_calls += 1
        if self.recover_calls == 1:
            raise RuntimeError("fallo inesperado de un ciclo")
        return 0

    def claim(self, limit, token_factory):
        # Paro determinista: tras dos ciclos completos se solicita la parada.
        self.claim_calls = getattr(self, "claim_calls", 0) + 1
        if self.claim_calls >= 2:
            self.stop.set()
        return []


def test_an_unexpected_error_in_one_cycle_does_not_stop_the_worker_loop():
    stop = threading.Event()
    jobs = _Jobs()
    jobs.stop = stop
    runner = WorkerRunner(WorkerSettings(enabled=True, owner="t", poll_interval_seconds=1), jobs, processor=None,
                          stop_event=stop, sleep=lambda s: None)
    runner.run_forever()
    assert jobs.recover_calls >= 2  # sobrevivió al primer error y siguió ciclando


# --- D. Configuración del pool de conexiones ---

def test_the_pool_validates_connections_and_rotates_idle_and_old_ones():
    from app.db.pool import create_pool

    pool = create_pool("postgresql://u:p@127.0.0.1:1/x")
    assert pool.max_idle == 60
    assert pool.max_lifetime == 1800
    assert pool.reconnect_timeout == 60
    assert pool._check is not None  # validación antes de reutilizar
