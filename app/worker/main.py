"""Punto de entrada del worker: python -m app.worker.main

Proceso independiente del servidor HTTP. No se inicia desde FastAPI ni con auto-reload.
Desactivado por defecto (WORKER_ENABLED=false): sin decisión explícita no consume la cola.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from app.core.config import Settings
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings

logger = logging.getLogger("lacusine.worker")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        worker_settings = WorkerSettings.from_env()
        settings = Settings.from_env()
    except ValueError as exc:
        logger.error("worker_misconfigured code=%s", exc)
        return 2

    if not worker_settings.enabled:
        logger.info("worker_disabled set WORKER_ENABLED=true to consume the queue")
        return 0
    if not settings.database_url:
        logger.error("worker_misconfigured code=database_url_missing")
        return 2

    stop = threading.Event()

    def _request_stop(signum, _frame) -> None:
        logger.info("worker_signal received=%s", signum)
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    worker, pool = build_worker(settings, worker_settings, stop_event=stop)
    pool.open(wait=True, timeout=10)
    try:
        worker.run_forever()
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
