"""Carga opcional de un archivo .env para el desarrollo local en Windows/PowerShell.

Reglas:
  * Solo se activa con LACUSINE_LOAD_DOTENV=true. En Render no se usa: las variables llegan del panel.
  * Nunca sobrescribe una variable ya definida (shell, Render, CI).
  * Nunca imprime valores: solo devuelve los nombres cargados.
  * No habilita ninguna capacidad por sí mismo: los flags de envío siguen en su valor por defecto.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TRUE = {"1", "true", "yes", "si", "sí", "on"}


def dotenv_enabled(environ: MutableMapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get("LACUSINE_LOAD_DOTENV", "").strip().lower() in _TRUE


def load_local_env(path: Path, environ: MutableMapping[str, str] | None = None) -> list[str]:
    target = os.environ if environ is None else environ
    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if not _KEY.match(key) or key in target:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if value == "":
            continue
        target[key] = value
        loaded.append(key)
    return loaded


def load_if_enabled(path: Path = Path(".env")) -> list[str]:
    return load_local_env(path) if dotenv_enabled() else []
