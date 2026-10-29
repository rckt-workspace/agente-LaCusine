"""Controles de seguridad comercial y legal: entrada, salida y redacción para logs.

Entrada: datos sensibles y posibles instrucciones maliciosas no llegan a Claude.
Salida: la respuesta del modelo se rechaza si promete algo que la empresa no puede garantizar.
Logs: ningún texto de cliente se registra sin redacción.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

ALLOWED_LINK_HOSTS = ("lacuisineappliances.co",)

# --- Mensajes fijos. Nunca salen de un modelo sin validar. ---

SAFE_REPLY_SENSITIVE = (
    "Por tu seguridad, no compartas datos de tarjeta, contraseñas ni cédula por WhatsApp. "
    "Los pagos se realizan en la web o con un asesor de La Cuisine."
)
SAFE_REPLY_INJECTION = (
    "Solo puedo ayudarte con información de productos y compras de La Cuisine. "
    "¿Qué electrodoméstico estás buscando?"
)
SAFE_REPLY_HUMAN = (
    "Con gusto te comunico con un asesor de La Cuisine. "
    "Para que pueda ayudarte mejor, ¿me compartes tu nombre y tu ciudad?"
)
SAFE_REPLY_UNSAFE_OUTPUT = (
    "Para responderte con exactitud, te comunico con un asesor de La Cuisine."
)

# --- Patrones de entrada ---

_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SECRET_REQUEST = re.compile(
    r"\b(contrase[ñn]a|password|clave de (la )?(tarjeta|cuenta|banco)|cvv|c[oó]digo de seguridad|pin)\b",
    re.IGNORECASE,
)
_NATIONAL_ID = re.compile(r"\b(c[eé]dula|c\.?c\.?|documento)\b.{0,15}\d{6,10}\b", re.IGNORECASE)
_INJECTION = re.compile(
    r"(ignora(r)?|olvida(r)?|descarta(r)?)\s+(todas?\s+)?(las|tus|sus|tu|mis)\s+(instrucciones|reglas|normas)"
    r"|ignore\s+(all|previous|the)\s+(instructions|rules)"
    r"|(system|sistema)\s+prompt"
    r"|revela\s+(tu|el|tus)\s+(prompt|instrucciones|configuraci[oó]n)"
    r"|eres\s+ahora\s+(un|una)\b"
    r"|ahora\s+eres\s+(un|una)\b"
    r"|modo\s+(desarrollador|developer|dios|sin\s+restricciones)"
    r"|developer\s+mode",
    re.IGNORECASE,
)
_HUMAN_REQUEST = re.compile(
    r"\b(asesor|asesora|humano|persona real|hablar con (alguien|una persona)|agente humano|"
    r"comunicar(me)? con (un|una) (asesor|persona)|quiero hablar con)\b",
    re.IGNORECASE,
)

# --- Patrones de salida (promesas no permitidas) ---

_OUTPUT_VIOLATIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("discount_promise", re.compile(
        r"(te\s+(hago|doy|ofrezco|regalo)\s+(un\s+|el\s+)?descuento|descuento\s+del\s+\d+\s*%)", re.IGNORECASE)),
    ("stock_guarantee", re.compile(
        r"(stock|inventario|disponibilidad)\b.{0,15}\b(garantizad|confirmad|asegurad)|tenemos\s+\d+\s+unidades",
        re.IGNORECASE)),
    ("exact_date", re.compile(
        r"\b(llegar[aá]|entregad[oa]|instalad[oa]|entrega)\s+el\s+\d{1,2}\s+de\s+[a-záéíóú]+", re.IGNORECASE)),
    ("payment_confirmed", re.compile(
        r"(pago|pedido)\b.{0,12}\b(confirmad[oa]|aprobad[oa]|recibid[oa])|tu\s+pedido\s+(est[aá]|ya\s+est[aá])\s+en\s+camino",
        re.IGNORECASE)),
    ("definitive_legal", re.compile(
        r"(asesor[ií]a\s+jur[ií]dica|te\s+recomiendo\s+demandar|la\s+ley\s+te\s+(da|permite)\s+siempre)", re.IGNORECASE)),
    ("absolute_warranty_exclusion", re.compile(
        r"(accesorios|productos\s+en\s+promoci[oó]n|promocionados?)\s+no\s+tienen\s+(garant[ií]a|devoluci[oó]n)",
        re.IGNORECASE)),
    ("sensitive_request", re.compile(
        r"(env[ií]ame|comp[aá]rteme|dime|escr[ií]beme|mándame)\b.{0,40}\b(contrase[ñn]a|cvv|tarjeta|c[eé]dula)",
        re.IGNORECASE)),
    ("installment_price", re.compile(r"\bcuotas?\s+sin\s+intereses\b", re.IGNORECASE)),
)
_URL = re.compile(r"https?://([^\s/]+)", re.IGNORECASE)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮]")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_NUMBER = re.compile(r"\d{7,}")


@dataclass(frozen=True)
class InputFindings:
    card_number: bool = False
    secret_request: bool = False
    national_id: bool = False
    injection: bool = False
    human_request: bool = False

    @property
    def sensitive(self) -> bool:
        return self.card_number or self.secret_request or self.national_id

    @property
    def sensitive_kinds(self) -> list[str]:
        kinds = []
        if self.card_number:
            kinds.append("card_number")
        if self.secret_request:
            kinds.append("credentials")
        if self.national_id:
            kinds.append("national_id")
        return kinds


def sanitize_input(text: str, max_chars: int) -> str:
    cleaned = _CONTROL_CHARS.sub(" ", unicodedata.normalize("NFC", text or ""))
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned[:max_chars]


def inspect_input(text: str) -> InputFindings:
    return InputFindings(
        card_number=any(_is_luhn_valid(m.group(0)) for m in _CARD_CANDIDATE.finditer(text)),
        secret_request=bool(_SECRET_REQUEST.search(text)),
        national_id=bool(_NATIONAL_ID.search(text)),
        injection=bool(_INJECTION.search(text)),
        human_request=bool(_HUMAN_REQUEST.search(text)),
    )


def output_violations(reply_text: str, allowed_hosts: Iterable[str] = ALLOWED_LINK_HOSTS) -> list[str]:
    """Devuelve los códigos de las promesas o enlaces no permitidos. Lista vacía = respuesta aceptable."""
    violations = [code for code, pattern in _OUTPUT_VIOLATIONS if pattern.search(reply_text)]
    allowed = tuple(allowed_hosts)
    for match in _URL.finditer(reply_text):
        host = match.group(1).lower().split(":")[0]
        if not any(host == h or host.endswith("." + h) for h in allowed):
            violations.append("link_not_allowed")
            break
    return violations


def redact(text: str) -> str:
    """Versión segura de un texto para logs: sin correos ni números largos (teléfonos, cédulas, tarjetas)."""
    redacted = _EMAIL.sub("[correo]", text)
    return _LONG_NUMBER.sub("[numero]", redacted)


def _is_luhn_valid(candidate: str) -> bool:
    digits = [int(ch) for ch in candidate if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
