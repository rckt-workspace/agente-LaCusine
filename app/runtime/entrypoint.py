"""Selección del modo de ejecución. La misma imagen sirve para los tres modos.

APP_RUN_MODE:
  web            solo FastAPI (uvicorn). Recomendado para AWS: un contenedor por proceso.
  worker         solo el worker. Recomendado para AWS.
  demo-combined  FastAPI y worker supervisados en el mismo contenedor. Solo para la demostración en Render Free.

Los modos web y worker reemplazan el proceso con exec: el servicio recibe directamente las señales.
El modo demo-combined exige DEMO_ENABLED=true: así no puede arrancar por accidente con la demo activa.
Ningún modo ejecuta migraciones.
"""

from __future__ import annotations

import logging
import os
import sys

from app.runtime.supervisor import ChildSpec, Supervisor, install_signal_handlers, python_module

MODES = ("web", "worker", "demo-combined")
logger = logging.getLogger("lacusine.entrypoint")


class EntrypointError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def resolve_port(environ) -> str:
    raw = environ.get("PORT") or "8000"
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise EntrypointError("port_invalid")
    return raw


def resolve_mode(environ) -> str:
    mode = (environ.get("APP_RUN_MODE") or "web").strip()
    if mode not in MODES:
        raise EntrypointError("run_mode_invalid")
    return mode


def api_argv(port: str) -> list[str]:
    return python_module("uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port)


def worker_argv() -> list[str]:
    return python_module("app.worker.main")


def main(environ=None, exec_fn=os.execv) -> int:
    environ = os.environ if environ is None else environ
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        mode = resolve_mode(environ)
        port = resolve_port(environ)
        if mode == "demo-combined" and (environ.get("DEMO_ENABLED") or "").strip().lower() != "true":
            raise EntrypointError("demo_combined_requires_demo_enabled")
    except EntrypointError as exc:
        print(f'{{"error": "{exc.code}"}}', file=sys.stderr)
        return 2

    if mode == "web":
        argv = api_argv(port)
        exec_fn(argv[0], argv)
        return 0  # no se alcanza
    if mode == "worker":
        argv = worker_argv()
        exec_fn(argv[0], argv)
        return 0

    supervisor = Supervisor([
        ChildSpec("api", api_argv(port)),
        ChildSpec("worker", worker_argv()),
    ])
    install_signal_handlers(supervisor)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
