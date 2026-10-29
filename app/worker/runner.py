"""Bucle del worker: recuperación de leases vencidos, adquisición por lotes, ejecución concurrente y finalización.

Reglas de finalización (todas con el token de propiedad; un propietario antiguo no puede escribir):
  ok                  -> processed | ignored (y se elimina el payload)
  transitorio         -> retry con backoff; si se agotan los intentos -> manual_review
  permanente          -> dead_letter
  resultado incierto  -> manual_review (nunca reenvío automático de una llamada facturable)
  lease perdido       -> no se escribe nada
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from app.repositories.errors import StorageError
from app.repositories.jobs import Claim
from app.worker.config import WorkerSettings
from app.worker.errors import ErrorClass, classify
from app.worker.processor import ProcessResult
from app.worker.retry import backoff_seconds

logger = logging.getLogger("lacusine.worker")


@dataclass
class Metrics:
    claimed: int = 0
    processed: int = 0
    ignored: int = 0
    retried: int = 0
    dead_letter: int = 0
    manual_review: int = 0
    lease_lost: int = 0
    recovered_expired: int = 0
    slow_jobs: int = 0
    counters: dict[str, int] = field(default_factory=dict)

    def bump(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        jobs,
        processor,
        *,
        stop_event: threading.Event | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
        token_factory=uuid.uuid4,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._processor = processor
        self._stop = stop_event or threading.Event()
        self._sleep = sleep
        self._clock = clock
        self._token_factory = token_factory
        self._rng = rng or random.Random()
        self._executor = ThreadPoolExecutor(max_workers=settings.max_concurrency, thread_name_prefix="lacusine-job")
        self._lock = threading.Lock()
        self.metrics = Metrics()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def run_once(self) -> int:
        """Un ciclo. Devuelve cuántos trabajos se reclamaron."""
        try:
            recovered = self._jobs.recover_expired(self._settings.max_attempts)
            with self._lock:
                self.metrics.recovered_expired += recovered
            claims = self._jobs.claim(self._settings.batch_size, self._token_factory)
        except StorageError as exc:
            logger.error("worker_cycle_failed code=%s", exc.args[0] if exc.args else "storage_error")
            return 0

        with self._lock:
            self.metrics.claimed += len(claims)
        futures = [self._executor.submit(self._run_claim, claim) for claim in claims]
        wait(futures)
        return len(claims)

    def run_forever(self) -> None:
        logger.info("worker_started owner=%s concurrency=%d", self._settings.owner, self._settings.max_concurrency)
        try:
            while not self._stop.is_set():
                claimed = self.run_once()
                if claimed == 0:
                    self._stop.wait(self._settings.poll_interval_seconds)
        finally:
            self.shutdown()

    def request_stop(self) -> None:
        """Seguro desde manejadores de señal: no bloquea ni cancela trabajos en curso."""
        self._stop.set()

    def shutdown(self) -> None:
        """Espera a los trabajos en curso. Los leases pendientes vencen solos si el proceso muere."""
        self._stop.set()
        self._executor.shutdown(wait=True)
        logger.info("worker_stopped owner=%s", self._settings.owner)

    def _run_claim(self, claim: Claim) -> None:
        started = self._clock()
        try:
            result: ProcessResult = self._processor.process(claim, renew=lambda: self._jobs.renew(claim))
            self._apply(claim, result)
        except Exception as exc:  # noqa: BLE001 - se clasifica y se registra solo el tipo, nunca el mensaje
            self._apply_error(claim, exc)
        finally:
            elapsed = self._clock() - started
            if elapsed > self._settings.processing_timeout_seconds:
                with self._lock:
                    self.metrics.slow_jobs += 1
                logger.warning("job_slow event=%s seconds=%.1f", claim.event_id, elapsed)

    def _apply(self, claim: Claim, result: ProcessResult) -> None:
        if result.status == "lease_lost":
            self._record("lease_lost", claim, "lease_lost")
            return
        if result.retry:
            self._retry_or_review(claim, result.error_code or "transient")
            return
        if result.status == "manual_review":
            self._review(claim, result.error_code or "manual_review")
            return
        ok = self._jobs.finish(claim, result.status, error_code=result.error_code, drop_payload=True)
        if not ok:
            self._record("lease_lost", claim, "stale_owner_rejected")
            return
        self._record(result.status, claim, result.error_code)

    def _apply_error(self, claim: Claim, exc: BaseException) -> None:
        error_class, code = classify(exc)
        if error_class is ErrorClass.PERMANENT:
            self._finish(claim, "dead_letter", code)
        elif error_class is ErrorClass.UNCERTAIN:
            self._review(claim, code)
        else:
            self._retry_or_review(claim, code)

    def _retry_or_review(self, claim: Claim, code: str) -> None:
        if claim.attempts >= self._settings.max_attempts:
            self._review(claim, f"{code}_attempts_exhausted")
            return
        delay = backoff_seconds(claim.attempts, self._settings.backoff_base_seconds,
                                self._settings.backoff_max_seconds, self._rng)
        ok = self._jobs.finish(claim, "retry", error_code=code, retry_after_seconds=max(1, int(delay)))
        if ok:
            self._record("retried", claim, code)
        else:
            self._record("lease_lost", claim, "stale_owner_rejected")

    def _review(self, claim: Claim, code: str) -> None:
        self._finish(claim, "manual_review", code)

    def _finish(self, claim: Claim, status: str, code: str) -> None:
        ok = self._jobs.finish(claim, status, error_code=code, drop_payload=False)
        if ok:
            self._record(status, claim, code)
        else:
            self._record("lease_lost", claim, "stale_owner_rejected")

    def _record(self, outcome: str, claim: Claim, code: str | None) -> None:
        attribute = {
            "processed": "processed", "ignored": "ignored", "retried": "retried", "retry": "retried",
            "dead_letter": "dead_letter", "manual_review": "manual_review", "lease_lost": "lease_lost",
        }.get(outcome)
        with self._lock:
            if attribute:
                setattr(self.metrics, attribute, getattr(self.metrics, attribute) + 1)
            self.metrics.bump(outcome)
        logger.info("job_finished event=%s outcome=%s code=%s attempt=%d", claim.event_id, outcome, code or "-",
                    claim.attempts)
