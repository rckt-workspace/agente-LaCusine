"""Lógica del runner: finalización, reintentos, agotamiento, propiedad y apagado. Sin base de datos."""

import random
import threading
import time
import uuid

import pytest

from app.repositories.errors import StorageError
from app.worker.config import WorkerSettings
from app.worker.errors import ManualReviewRequired, PermanentJobError, classify
from app.worker.processor import ProcessResult
from app.worker.retry import backoff_seconds
from app.worker.runner import Worker
from tests.worker.fakes import InMemoryJobs


def _settings(**overrides) -> WorkerSettings:
    base = dict(enabled=True, owner="test-worker", max_concurrency=1, batch_size=10, max_attempts=3,
                backoff_base_seconds=5, backoff_max_seconds=60, lease_seconds=60, processing_timeout_seconds=30)
    base.update(overrides)
    return WorkerSettings(**base)


class ScriptedProcessor:
    """Devuelve o lanza lo que se le indique, por evento."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.calls: list[str] = []

    def process(self, claim, renew):
        self.calls.append(claim.external_event_id)
        action = self.behaviour.get(claim.external_event_id, ProcessResult(status="processed"))
        if callable(action):
            return action(claim, renew)
        if isinstance(action, BaseException):
            raise action
        return action


def _run(jobs, processor, settings=None):
    worker = Worker(settings or _settings(), jobs, processor, sleep=lambda s: None,
                    rng=random.Random(7))
    worker.run_once()
    return worker


# --- Configuración ---

def test_worker_is_disabled_by_default(monkeypatch):
    for name in ("WORKER_ENABLED",):
        monkeypatch.delenv(name, raising=False)
    assert WorkerSettings.from_env().enabled is False


@pytest.mark.parametrize("raw", ["0", "abc", "-1"])
def test_invalid_numeric_configuration_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("WORKER_BATCH_SIZE", raw)
    with pytest.raises(ValueError):
        WorkerSettings.from_env()


def test_lease_must_outlive_processing_timeout():
    with pytest.raises(ValueError, match="lease"):
        WorkerSettings(lease_seconds=30, processing_timeout_seconds=30)


def test_backoff_base_cannot_exceed_maximum():
    with pytest.raises(ValueError):
        WorkerSettings(backoff_base_seconds=100, backoff_max_seconds=10)


# --- Backoff ---

@pytest.mark.parametrize("attempt", [1, 2, 3, 8])
def test_backoff_stays_within_equal_jitter_bounds(attempt):
    rng = random.Random(1)
    ceiling = min(900.0, 5.0 * (2 ** (attempt - 1)))
    for _ in range(200):
        value = backoff_seconds(attempt, 5, 900, rng)
        assert ceiling / 2 <= value <= ceiling


def test_backoff_never_exceeds_cap_and_rejects_invalid_attempt():
    assert backoff_seconds(50, 5, 900, random.Random(3)) <= 900
    with pytest.raises(ValueError):
        backoff_seconds(0, 5, 900)


# --- Clasificación ---

@pytest.mark.parametrize("exc, expected", [
    (PermanentJobError("invalid_text_payload"), ("permanent", "invalid_text_payload")),
    (ManualReviewRequired("timeout"), ("uncertain", "timeout")),
    (StorageError("job_finish_failed"), ("transient", "job_finish_failed")),
    (ValueError("cualquier dato del cliente"), ("transient", "internal_error")),
])
def test_errors_are_classified_with_stable_codes_and_no_content(exc, expected):
    error_class, code = classify(exc)
    assert (error_class.value, code) == expected
    assert "cualquier dato" not in code


# --- Finalización por resultado ---

def test_successful_event_is_processed_and_payload_is_dropped():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.OK")
    _run(jobs, ScriptedProcessor())
    assert row.status == "processed"
    assert row.payload_dropped is True


def test_transient_failure_schedules_retry_with_backoff_until_attempts_exhausted():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.T")
    processor = ScriptedProcessor({"wamid.T": StorageError("job_write_failed")})
    _run(jobs, processor)
    assert row.status == "retry" and row.error == "job_write_failed"
    assert 1 <= row.delay <= 5  # primer intento: entre la mitad y el total de la base (5 s)


def test_transient_failure_after_max_attempts_goes_to_manual_review():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.X")
    row.attempts = 2  # el próximo intento es el 3 = max_attempts
    _run(jobs, ScriptedProcessor({"wamid.X": StorageError("job_write_failed")}))
    assert row.status == "manual_review"
    assert row.error == "job_write_failed_attempts_exhausted"


def test_permanent_error_goes_straight_to_dead_letter_without_retry():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.P")
    _run(jobs, ScriptedProcessor({"wamid.P": PermanentJobError("invalid_text_payload")}))
    assert row.status == "dead_letter"
    assert row.attempts == 1


def test_uncertain_external_result_goes_to_manual_review_and_is_never_retried():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.U")
    _run(jobs, ScriptedProcessor({"wamid.U": ManualReviewRequired("timeout")}))
    assert row.status == "manual_review"
    assert row.error == "timeout"


def test_retry_result_from_processor_uses_backoff_not_immediate_repeat():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.R")
    _run(jobs, ScriptedProcessor({"wamid.R": ProcessResult(status="retry", error_code="rate_limited", retry=True)}))
    assert row.status == "retry" and row.error == "rate_limited" and row.delay is not None


def test_manual_review_status_from_processor_is_respected():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.M")
    _run(jobs, ScriptedProcessor({"wamid.M": ProcessResult(status="manual_review", error_code="reservation_reserved")}))
    assert row.status == "manual_review"


def test_ignored_event_keeps_justified_reason():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.I")
    _run(jobs, ScriptedProcessor({"wamid.I": ProcessResult(status="ignored", error_code="human_control")}))
    assert row.status == "ignored" and row.error == "human_control"


# --- Propiedad: un worker antiguo no sobrescribe ---

def test_stale_owner_cannot_finish_after_lease_was_recovered():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.S")
    stale_claims = []

    def slow_process(claim, renew):
        stale_claims.append(claim)
        # Mientras "dormía", otro worker recuperó el trabajo y lo procesó.
        row.token = uuid.uuid4()
        row.status = "processing"
        return ProcessResult(status="processed")

    _run(jobs, ScriptedProcessor({"wamid.S": slow_process}))
    assert row.status == "processing"  # el propietario nuevo conserva el estado
    assert row.token is not None and row.token != stale_claims[0].lease_token


def test_lease_lost_before_side_effects_writes_nothing():
    jobs = InMemoryJobs()
    row = jobs.add("c1", "wamid.L")

    def process(claim, renew):
        return ProcessResult(status="lease_lost")

    _run(jobs, ScriptedProcessor({"wamid.L": process}))
    assert row.status == "processing"


# --- Orden por contacto ---

def test_events_of_same_contact_are_processed_in_arrival_order_one_at_a_time():
    jobs = InMemoryJobs()
    first = jobs.add("c1", "wamid.1")
    time.sleep(0.001)
    second = jobs.add("c1", "wamid.2")
    processor = ScriptedProcessor()
    worker = Worker(_settings(), jobs, processor, sleep=lambda s: None)
    worker.run_once()
    assert processor.calls == ["wamid.1"]  # el segundo espera: el primero no ha terminado
    assert second.status == "received"
    worker.run_once()
    assert processor.calls == ["wamid.1", "wamid.2"]
    assert first.status == "processed"


def test_different_contacts_are_claimed_together():
    jobs = InMemoryJobs()
    jobs.add("c1", "wamid.A")
    jobs.add("c2", "wamid.B")
    processor = ScriptedProcessor()
    _run(jobs, processor, _settings(batch_size=10))
    assert sorted(processor.calls) == ["wamid.A", "wamid.B"]


def test_batch_size_limits_claims_per_cycle():
    jobs = InMemoryJobs()
    for i in range(5):
        jobs.add(f"c{i}", f"wamid.{i}")
    processor = ScriptedProcessor()
    worker = Worker(_settings(batch_size=2), jobs, processor, sleep=lambda s: None)
    assert worker.run_once() == 2


# --- Apagado limpio y errores de la base en el ciclo ---

def test_storage_error_while_claiming_does_not_crash_the_loop():
    class BrokenJobs(InMemoryJobs):
        def claim(self, limit, token_factory):
            raise StorageError("job_claim_failed")

    worker = Worker(_settings(), BrokenJobs(), ScriptedProcessor(), sleep=lambda s: None)
    assert worker.run_once() == 0


def test_run_forever_stops_when_stop_event_is_set_and_waits_for_inflight_work():
    jobs = InMemoryJobs()
    jobs.add("c1", "wamid.F")
    stop = threading.Event()

    def finish_then_stop(claim, renew):
        stop.set()  # solicitud de apagado durante el trabajo en curso
        return ProcessResult(status="processed")

    worker = Worker(_settings(), jobs, ScriptedProcessor({"wamid.F": finish_then_stop}), stop_event=stop,
                    sleep=lambda s: None)
    worker.run_forever()  # debe terminar sin bloquearse
    assert jobs.rows[0].status == "processed"
    assert stop.is_set()


def test_request_stop_is_idempotent_and_safe():
    worker = Worker(_settings(), InMemoryJobs(), ScriptedProcessor(), sleep=lambda s: None)
    worker.request_stop()
    worker.request_stop()
    worker.shutdown()
    assert worker.stop_event.is_set()


def test_metrics_count_outcomes():
    jobs = InMemoryJobs()
    jobs.add("c1", "wamid.M1")
    jobs.add("c2", "wamid.M2")
    worker = _run(jobs, ScriptedProcessor({"wamid.M2": PermanentJobError("x")}), _settings())
    assert worker.metrics.processed == 1
    assert worker.metrics.dead_letter == 1
    assert worker.metrics.claimed == 2
