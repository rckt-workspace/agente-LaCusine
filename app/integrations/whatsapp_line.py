"""Clasificación de la línea de WhatsApp Cloud API: número virtual de prueba de Meta frente a línea empresarial real.

Función pura, sin red. Se usa en el preflight y en la validación de arranque del modo público.

Un número de prueba de Meta puede mostrar account_mode, verificación y estado con aspecto de producción.
Por eso la decisión se basa también en el número visible (display_phone_number): los números de prueba de
Meta de Estados Unidos son +1 555. Ningún dato de la API basta por sí solo para afirmar que una línea
atiende a clientes públicos: la línea debe ser LIVE, VERIFIED, CONNECTED, con un número visible que no sea de
prueba y que coincida con el número esperado de La Cuisine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Prefijo de los números virtuales de prueba de Meta (+1 555 ...). Los números reales de La Cuisine no empiezan por él.
META_TEST_NUMBER_PREFIX = "15551"


@dataclass(frozen=True)
class LineVerdict:
    ready_for_public: bool
    reasons: tuple[str, ...]


def digits_only(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def is_meta_test_number(display_phone: str | None) -> bool:
    digits = digits_only(display_phone)
    return digits.startswith(META_TEST_NUMBER_PREFIX)


def assess_line(identity: Mapping[str, object], expected_display: str | None) -> LineVerdict:
    """Decide si la línea puede atender a clientes públicos. Devuelve los motivos de rechazo, sin datos personales."""
    reasons: list[str] = []
    if identity.get("account_mode") != "LIVE":
        reasons.append("cuenta_no_live")
    if identity.get("verified") != "VERIFIED":
        reasons.append("numero_no_verificado")
    if identity.get("status") != "CONNECTED":
        reasons.append("linea_no_conectada")
    display = identity.get("display_phone")
    if not digits_only(str(display) if display else None):
        reasons.append("numero_visible_desconocido")
    elif is_meta_test_number(str(display)):
        reasons.append("numero_de_prueba_de_meta")
    expected = digits_only(expected_display)
    if not expected:
        reasons.append("numero_esperado_no_configurado")
    elif digits_only(str(display) if display else None) != expected:
        reasons.append("numero_distinto_del_esperado")
    return LineVerdict(ready_for_public=not reasons, reasons=tuple(reasons))
