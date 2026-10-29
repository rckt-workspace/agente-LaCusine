"""Conciliación de reservas en el orquestador: nunca se llama dos veces a una llamada facturable sin resolverla."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.agent.orchestrator import OutcomeStatus
from app.integrations.llm import FakeLLMProvider, LLMAuthError, LLMRateLimitError, LLMTimeoutError
from app.repositories.errors import StorageError
from tests.agent.fakes import FailingUsageStore
from tests.agent.test_orchestrator import Harness, reply


def _billable(script):
    return FakeLLMProvider(script=script, name="anthropic", model="claude-haiku-5-5")


def _harness(script, usage=None):
    provider = _billable(script)
    h = Harness(provider, usage=usage)
    return h, provider


def test_successful_billable_call_settles_reservation_with_actual_cost():
    h, provider = _harness([reply("Te muestro opciones.")])
    outcome = h.ask("Busco una cafetera", call_id="wamid.OK")
    assert outcome.status is OutcomeStatus.REPLIED
    assert h.cost.reservation_state("wamid.OK") == "settled"
    now = h.cost._clock()
    assert h.cost.ledger.counted_between(now - timedelta(days=1), now + timedelta(seconds=1)) == outcome.cost_usd


def test_timeout_marks_reservation_uncertain_and_next_attempt_does_not_call_model():
    h, provider = _harness([LLMTimeoutError()])
    first = h.ask("Busco una cafetera", call_id="wamid.TO")
    assert first.status is OutcomeStatus.PROVIDER_ERROR
    assert first.billed_state == "uncertain"
    assert h.cost.reservation_state("wamid.TO") == "uncertain"

    second = h.ask("Busco una cafetera", call_id="wamid.TO")
    assert second.status is OutcomeStatus.REQUIRES_RECONCILIATION
    assert second.error_code == "reservation_uncertain"
    assert len(provider.calls) == 1  # ninguna segunda llamada facturable


def test_not_billed_error_releases_reservation_so_retry_can_proceed():
    h, provider = _harness([LLMRateLimitError(), reply("Listo, aquí tienes opciones.")])
    first = h.ask("Busco una cafetera", call_id="wamid.RL")
    assert first.billed_state == "not_billed"
    assert h.cost.reservation_state("wamid.RL") == "released"

    second = h.ask("Busco una cafetera", call_id="wamid.RL")
    assert second.status is OutcomeStatus.REPLIED
    assert len(provider.calls) == 2
    assert h.cost.reservation_state("wamid.RL") == "settled"


def test_auth_failure_releases_reservation():
    h, provider = _harness([LLMAuthError()])
    h.ask("Busco una cafetera", call_id="wamid.AU")
    assert h.cost.reservation_state("wamid.AU") == "released"


def test_reserved_state_left_by_crashed_worker_forces_reconciliation():
    h, provider = _harness([reply("no debería enviarse")])
    h.cost.reserve("wamid.CRASH", None, Decimal("0.001"))  # simula un worker que murió tras reservar
    outcome = h.ask("Busco una cafetera", call_id="wamid.CRASH")
    assert outcome.status is OutcomeStatus.REQUIRES_RECONCILIATION
    assert provider.calls == []


def test_settled_call_is_not_repeated():
    h, provider = _harness([reply("ok")])
    h.ask("Busco una cafetera", call_id="wamid.DONE")
    again = h.ask("Busco una cafetera", call_id="wamid.DONE")
    assert again.status is OutcomeStatus.DUPLICATE_SKIPPED
    assert len(provider.calls) == 1


def test_audit_failure_after_success_keeps_reservation_and_prevents_second_billing():
    h, provider = _harness([reply("Te muestro opciones.")], usage=FailingUsageStore())
    with pytest.raises(StorageError):
        h.ask("Busco una cafetera", call_id="wamid.AUD")
    assert h.cost.reservation_state("wamid.AUD") == "reserved"

    # El reintento no vuelve a llamar al modelo: la reserva pendiente exige conciliación.
    retry = h.ask("Busco una cafetera", call_id="wamid.AUD")
    assert retry.status is OutcomeStatus.REQUIRES_RECONCILIATION
    assert len(provider.calls) == 1


def test_simulated_provider_never_creates_reservations():
    h = Harness()  # FakeLLMProvider con name='fake'
    h.provider.script = [reply("ok")]
    h.ask("cafetera", call_id="wamid.SIM")
    assert h.cost.reservation_state("wamid.SIM") is None
