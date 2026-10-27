"""Verificación de la firma X-Hub-Signature-256 que Meta envía en cada POST."""

from __future__ import annotations

import hashlib
import hmac


def verify_meta_signature(app_secret: str, raw_body: bytes, header_value: str | None) -> bool:
    if not app_secret or not header_value:
        return False
    prefix = "sha256="
    if not header_value.startswith(prefix):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value[len(prefix):])
