"""Punto de entrada del worker: python -m app.worker.main

Proceso independiente del servidor HTTP. No se inicia desde FastAPI ni con auto-reload.
Desactivado por defecto (WORKER_ENABLED=false): sin decisión explícita no consume la cola.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from app.core.config import Settings
from app.core.env import load_if_enabled
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings

logger = logging.getLogger("lacusine.worker")


STANDBY_HEARTBEAT_SECONDS = 600


def worker_requested(environ=None) -> bool:
    """Lee solo WORKER_ENABLED. Una configuración numérica incorrecta no debe matar un worker en espera."""
    source = os.environ if environ is None else environ
    return source.get("WORKER_ENABLED", "").strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def run_standby(stop: threading.Event, heartbeat_seconds: float = STANDBY_HEARTBEAT_SECONDS) -> None:
    """Proceso vivo sin consumir la cola: no abre conexiones, no lee trabajos, no envía nada."""
    logger.info("worker_standby consuming=false set WORKER_ENABLED=true to consume the queue")
    while not stop.wait(heartbeat_seconds):
        logger.info("worker_standby heartbeat consuming=false")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    loaded = load_if_enabled()  # solo con LACUSINE_LOAD_DOTENV=true; nunca sobrescribe variables existentes
    if loaded:
        logger.info("local_env_loaded count=%d", len(loaded))

    stop = threading.Event()

    def _request_stop(signum, _frame) -> None:
        logger.info("worker_signal received=%s", signum)
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    if not worker_requested():
        run_standby(stop)
        return 0

    try:
        worker_settings = WorkerSettings.from_env()
        settings = Settings.from_env()
    except ValueError as exc:
        logger.error("worker_misconfigured code=%s", exc)
        return 2
    if not settings.database_url:
        logger.error("worker_misconfigured code=database_url_missing")
        return 2

    try:
        worker, pool = build_worker(settings, worker_settings, stop_event=stop)
    except ValueError as exc:
        logger.error("worker_misconfigured code=%s", exc)
        return 2
    pool.open(wait=True, timeout=10)
    try:
        worker.run_forever()
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
