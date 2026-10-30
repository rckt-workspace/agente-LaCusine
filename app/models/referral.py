"""Atribución desde anuncios Click-to-WhatsApp.

Meta incluye el objeto `referral` solo en el primer mensaje de una conversación iniciada desde un anuncio.
Este módulo acepta únicamente los campos documentados, con tipo y longitud acotados. Nunca inventa datos:
si Meta no envía el objeto, no hay atribución.
"""

from __future__ import annotations

from typing import Any

MAX_LENGTHS = {
    "source_type": 20,
    "source_id": 64,
    "source_url": 500,
    "ctwa_clid": 256,
}


def sanitize_referral(raw: Any) -> dict[str, str] | None:
    """Devuelve solo los campos válidos del referral, o None si no hay atribución utilizable.

    Un campo con tipo o longitud incorrectos se descarta; los demás se conservan.
    No se registra el contenido descartado.
    """
    if not isinstance(raw, dict):
        return None
    clean: dict[str, str] = {}
    for key, limit in MAX_LENGTHS.items():
        value = raw.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= limit:
            clean[key] = value.strip()
    return clean or None
