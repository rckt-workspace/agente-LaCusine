"""Configuración del worker. Desactivado por defecto: ningún proceso consume la cola sin decisión explícita."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} debe ser un entero") from None
    if value < minimum:
        raise ValueError(f"{name} debe ser al menos {minimum}")
    return value


@dataclass(frozen=True)
class WorkerSettings:
    enabled: bool = False
    owner: str = "worker"
    poll_interval_seconds: int = 5
    batch_size: int = 10
    max_concurrency: int = 4
    lease_seconds: int = 120
    processing_timeout_seconds: int = 60
    max_attempts: int = 5
    backoff_base_seconds: int = 5
    backoff_max_seconds: int = 900

    def __post_init__(self) -> None:
        if self.lease_seconds <= self.processing_timeout_seconds:
            raise ValueError("el lease debe ser mayor que el tiempo máximo de procesamiento")
        if self.backoff_base_seconds > self.backoff_max_seconds:
            raise ValueError("el backoff base no puede superar el máximo")

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        default_owner = f"{socket.gethostname()}-{os.getpid()}"
        return cls(
            enabled=_env_bool("WORKER_ENABLED", False),
            owner=os.environ.get("WORKER_ID") or default_owner,
            poll_interval_seconds=_env_int("WORKER_POLL_INTERVAL_SECONDS", 5),
            batch_size=_env_int("WORKER_BATCH_SIZE", 10),
            max_concurrency=_env_int("WORKER_MAX_CONCURRENCY", 4),
            lease_seconds=_env_int("WORKER_LEASE_SECONDS", 120),
            processing_timeout_seconds=_env_int("WORKER_PROCESSING_TIMEOUT_SECONDS", 60),
            max_attempts=_env_int("WORKER_MAX_ATTEMPTS", 5),
            backoff_base_seconds=_env_int("WORKER_BACKOFF_BASE_SECONDS", 5),
            backoff_max_seconds=_env_int("WORKER_BACKOFF_MAX_SECONDS", 900),
        )
