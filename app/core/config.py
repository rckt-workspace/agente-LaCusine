"""Configuración cargada exclusivamente desde variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    meta_app_secret: str | None
    meta_verify_token: str | None
    max_body_bytes: int = 256 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            meta_app_secret=os.environ.get("META_APP_SECRET") or None,
            meta_verify_token=os.environ.get("META_VERIFY_TOKEN") or None,
            max_body_bytes=int(os.environ.get("MAX_WEBHOOK_BODY_BYTES", 256 * 1024)),
        )
