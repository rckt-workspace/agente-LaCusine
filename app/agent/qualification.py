"""Calificación comercial a partir de señales observables. No usa puntuaciones generadas por Claude."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agent.schemas import LeadStage
from app.services.text import normalize

KNOWN_CITIES = (
    "bogota", "medellin", "envigado", "barranquilla", "cali", "bucaramanga", "pereira", "santa marta",
    "cartagena", "chia", "sopo", "cajica", "guasca", "la calera", "tenjo", "tabio", "subachoque",
)

_PURCHASE = re.compile(
    r"\b(lo compro|la compro|quiero comprar|quiero (pedirlo|pedirla|llevarlo|llevarla)|me lo llevo|"
    r"c[oó]mo pago|formas? de pago|hacer (el|un) pedido|finalizar (la )?compra|lo quiero)\b",
    re.IGNORECASE,
)
_QUOTE = re.compile(r"\b(cotiz\w*|por mayor|proyecto)\b", re.IGNORECASE)
_INSTALLATION = re.compile(r"\binstal\w*", re.IGNORECASE)
_WARRANTY = re.compile(r"\bgarant\w*", re.IGNORECASE)
_RETURN = re.compile(r"\b(devol\w*|retract\w*|cambiar (el|la|un|una) (producto|nevera|estufa|horno))\b", re.IGNORECASE)
_COMPLAINT = re.compile(
    r"\b(reclam\w*|queja\w*|da[ñn]ad\w*|defectuos\w*|no funciona|lleg[oó] (roto|mal)|mal estado|p[eé]simo)\b",
    re.IGNORECASE,
)
_BUDGET_MILLIONS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(millones?|mill\b)", re.IGNORECASE)
_BUDGET_COP = re.compile(r"\$?\s*(\d{1,3}(?:\.\d{3})+|\d{5,9})(?!\d)")


@dataclass(frozen=True)
class Signals:
    human_request: bool = False
    purchase_intent: bool = False
    quote_request: bool = False
    installation: bool = False
    warranty: bool = False
    return_request: bool = False
    complaint: bool = False
    budget_cop: int | None = None
    city: str | None = None
    product_interest: bool = False
    confident_product: bool = False

    @property
    def needs_human(self) -> bool:
        return any((self.human_request, self.quote_request, self.installation, self.warranty,
                    self.return_request, self.complaint))


def detect_signals(text: str, product_interest: bool, confident_product: bool, human_request: bool) -> Signals:
    return Signals(
        human_request=human_request,
        purchase_intent=bool(_PURCHASE.search(text)),
        quote_request=bool(_QUOTE.search(text)),
        installation=bool(_INSTALLATION.search(text)),
        warranty=bool(_WARRANTY.search(text)),
        return_request=bool(_RETURN.search(text)),
        complaint=bool(_COMPLAINT.search(text)),
        budget_cop=parse_budget_cop(text),
        city=detect_city(text),
        product_interest=product_interest,
        confident_product=confident_product,
    )


def parse_budget_cop(text: str) -> int | None:
    """Presupuesto en COP: acepta '2 millones', '$3.000.000' o '3000000'. Devuelve el mayor encontrado."""
    values: list[int] = []
    for match in _BUDGET_MILLIONS.finditer(text):
        values.append(int(float(match.group(1).replace(",", ".")) * 1_000_000))
    for match in _BUDGET_COP.finditer(text):
        digits = match.group(1).replace(".", "")
        values.append(int(digits))
    # El producto más barato del catálogo cuesta 36.990 COP: montos menores no son presupuestos.
    plausible = [v for v in values if 100_000 <= v <= 100_000_000]
    return max(plausible) if plausible else None


def detect_city(text: str) -> str | None:
    normalized = f" {normalize(text)} "
    for city in KNOWN_CITIES:
        if f" {city} " in normalized:
            return city
    return None


def derive_stage(signals: Signals, previous: LeadStage | None = None) -> LeadStage:
    """Etapa comercial desde señales. Escalar tiene prioridad; las etapas no bajan sin señal nueva."""
    if signals.needs_human:
        return LeadStage.HUMAN_REQUIRED
    if signals.purchase_intent and signals.confident_product:
        derived = LeadStage.HIGH_INTENT
    elif signals.confident_product and (signals.budget_cop or signals.city):
        derived = LeadStage.QUALIFIED
    elif signals.product_interest or signals.confident_product:
        derived = LeadStage.DISCOVERY
    else:
        derived = LeadStage.NEW

    if previous in (LeadStage.QUALIFIED, LeadStage.HIGH_INTENT) and derived in (LeadStage.NEW, LeadStage.DISCOVERY):
        return previous
    return derived
