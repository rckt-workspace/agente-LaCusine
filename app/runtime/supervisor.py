"""Supervisor mínimo para el modo `demo-combined`: FastAPI y worker en un mismo contenedor.

Elección: biblioteca estándar, sin supervisord ni tini. Así no se añaden paquetes a la imagen y la lógica
de señales y de fallos queda en el código revisado. El supervisor:

- Arranca cada proceso una sola vez. No reinicia nada automáticamente: un fallo se hace visible.
- Reenvía SIGTERM y SIGINT a los hijos, espera su cierre y mata solo si exceden el plazo.
- Recoge (wait) cada hijo que termina: no quedan zombis.
- Si cualquier hijo termina sin que se haya pedido parar, detiene a los demás y sale con código distinto de 0.
- Registra nombre y código de salida. Nunca registra entorno, argumentos sensibles ni contenido.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

logger = logging.getLogger("lacusine.supervisor")

GRACE_SECONDS = 20.0
POLL_SECONDS = 0.5


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: Sequence[str]


class Supervisor:
    def __init__(self, children: Sequence[ChildSpec], *, spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
                 grace_seconds: float = GRACE_SECONDS, poll_seconds: float = POLL_SECONDS) -> None:
        if not children:
            raise ValueError("supervisor_requires_children")
        names = [c.name for c in children]
        if len(names) != len(set(names)):
            raise ValueError("supervisor_child_names_duplicated")
        self._children = list(children)
        self._spawn = spawn
        self._grace = grace_seconds
        self._poll = poll_seconds
        self._procs: dict[str, subprocess.Popen] = {}
        self._stop_requested = threading.Event()

    def request_stop(self, signum: int | None = None) -> None:
        if signum is not None:
            logger.info("supervisor_signal received=%s", signum)
        self._stop_requested.set()

    def run(self) -> int:
        """Devuelve el código de salida del supervisor: 0 si todo terminó tras una parada pedida."""
        for spec in self._children:
            self._procs[spec.name] = self._spawn(list(spec.argv))
            logger.info("child_started name=%s pid=%s", spec.name, self._procs[spec.name].pid)

        exit_code = 0
        while self._procs:
            if self._stop_requested.is_set():
                self._terminate_all()
                break
            for name, proc in list(self._procs.items()):
                code = proc.poll()
                if code is None:
                    continue
                del self._procs[name]
                if self._stop_requested.is_set():
                    logger.info("child_stopped name=%s code=%s", name, code)
                    continue
                logger.error("child_exited_unexpectedly name=%s code=%s", name, code)
                exit_code = code if code != 0 else 1
                self._stop_requested.set()
            if self._procs:
                time.sleep(self._poll)

        self._terminate_all()
        return exit_code

    def _terminate_all(self) -> None:
        for name, proc in self._procs.items():
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + self._grace
        for name, proc in list(self._procs.items()):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.error("child_killed_after_grace name=%s", name)
                proc.kill()
                code = proc.wait()
            logger.info("child_stopped name=%s code=%s", name, code)
            del self._procs[name]


def install_signal_handlers(supervisor: Supervisor) -> None:
    def handler(signum, _frame):
        supervisor.request_stop(signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def python_module(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]
