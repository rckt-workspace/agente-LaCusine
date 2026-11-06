"""Análisis narrativo opcional con Claude para informes de gerencia.

Reglas:
  * Desactivado por defecto (ANALYSIS_AI_ENABLED=false). Si falta algo, el informe se genera sin narrativa.
  * Solo recibe cifras agregadas del informe: sin nombres, teléfonos, conversaciones ni credenciales.
  * Cada conclusión cita un indicador del informe con su valor exacto. Si algo no se puede validar, se descarta
    la narrativa completa y el PDF sale determinista.
  * Presupuesto acumulado (app/services/analysis_budget.py): reserva antes de llamar, liquidación con el uso real,
    idempotencia por informe y datos, y 'uncertain' si la llamada falla después de salir.
  * Nunca lanza excepciones hacia el PDF: cualquier fallo devuelve None con un motivo genérico.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from app.integrations.llm import ChatMessage, LLMRequest
from app.services.analysis_budget import AnalysisLimits
from app.services.budget_ledger import BudgetExceededError, ReservationConflictError
from app.services.cost_tracker import Pricing, estimate_cost, worst_case_cost

logger = logging.getLogger("lacusine.analysis")

MAX_LINES = 5
MAX_LINE_CHARS = 280
SMALL_NUMBER_LIMIT = 10  # cifras pequeñas de redacción ("tres mensajes") no se exigen en el informe
_NUMBER = re.compile(r"\d[\d.,]*")
SYSTEM_PROMPT = (
    "Eres analista comercial de La Cuisine (electrodomésticos, Colombia). Recibes únicamente cifras agregadas "
    "de un informe semanal. Escribe en español, en máximo 5 frases breves, interpretaciones y recomendaciones. "
    "Usa solo las cifras que aparecen en los datos; no inventes ventas, ingresos, tasas ni productos. "
    "Si un dato dice 'Sin datos', no lo comentes. Cada hallazgo cita la clave del indicador y su valor exacto. "
    "Responde solo con este JSON: {\"hallazgos\": [{\"indicador\": \"clave\", \"cifra\": \"valor\", \"texto\": \"frase\"}]}."
)


@dataclass(frozen=True)
class NarrativeConfig:
    enabled: bool = False
    max_output_tokens: int = 400
    budget_usd_per_report: Decimal = Decimal("0.02")  # límite por informe; se pasa al libro como AnalysisLimits
    timeout_seconds: float = 20.0


class _Finding(BaseModel):
    indicador: str = Field(min_length=1, max_length=60)
    cifra: str = Field(min_length=1, max_length=40)
    texto: str = Field(min_length=1, max_length=MAX_LINE_CHARS)


class _NarrativeOutput(BaseModel):
    hallazgos: list[_Finding] = Field(min_length=1, max_length=MAX_LINES)


def indicators(report: dict) -> dict:
    """Indicadores estructurados del informe. Cada conclusión del modelo debe citar una de estas claves."""
    act, pro, ven, ia = report["actividad"], report["prospectos"], report["ventas"], report["operacion_ia"]
    return {
        "mensajes_recibidos": act["mensajes_entrantes"],
        "respuestas_aceptadas": act["respuestas_aceptadas_por_meta"],
        "respuestas_entregadas_o_leidas": act["respuestas_entregadas_o_leidas"],
        "tasa_entrega_pct": act["tasa_entrega_pct"],
        "conversaciones_nuevas": act["conversaciones_nuevas"],
        "derivaciones_a_asesor": act["conversaciones_derivadas_a_asesor"],
        "consultas_fallidas": act["consultas_fallidas"],
        "leads_nuevos": pro["leads_nuevos"],
        "leads_calificados": pro["leads_calificados"],
        "intencion_de_compra": pro["leads_con_intencion_de_compra"],
        "cotizaciones_solicitadas": pro["leads_con_cotizacion_solicitada"],
        "ventas_confirmadas": ven["ventas_confirmadas"],
        "ingresos_confirmados_cop": ven["valor_confirmado_cop"],
        "conversion_lead_a_venta_pct": ven["conversion_lead_a_venta_pct"],
        "llamadas_ia_exitosas": ia["llamadas_exitosas"],
        "costo_ia_usd": ia["costo_estimado_usd"],
    }


def aggregate_payload(report: dict) -> str:
    """Cifras agregadas en texto plano. Nunca incluye identificadores ni contenido de mensajes."""
    lines = [f"{key}: {'Sin datos' if value is None else value}" for key, value in indicators(report).items()]
    return "\n".join(lines)


def allowed_numbers(report: dict) -> set[str]:
    """Todas las cifras del informe, normalizadas a dígitos sin separadores."""
    values: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, (int, float, Decimal)) and not isinstance(node, bool):
            text = f"{Decimal(str(node)):f}"
            values.add(_digits(text))
            values.add(_digits(str(int(Decimal(str(node))))))
        elif isinstance(node, str):
            for token in _NUMBER.findall(node):
                values.add(_digits(token))

    walk(report)
    return {v for v in values if v}


def _digits(text: str) -> str:
    """Solo dígitos: '9.870.000', '9870000' y '91,7' se comparan de forma consistente con el informe."""
    return re.sub(r"\D", "", text)


def validate_findings(findings: list[_Finding], report: dict) -> list[str] | None:
    """Cada hallazgo debe citar un indicador existente con su valor exacto, y cualquier cifra grande
    del texto debe ser ESE valor. Si algo no se puede validar, devuelve None: no se publica nada."""
    values = indicators(report)
    lines = []
    for finding in findings:
        if finding.indicador not in values:
            return None
        expected = values[finding.indicador]
        if expected is None:
            return None  # no se concluye sobre un indicador sin datos
        expected_digits = _digits(f"{Decimal(str(expected)):f}")
        if _digits(finding.cifra) != expected_digits:
            return None
        for token in _NUMBER.findall(finding.texto):
            normalized = _digits(token)
            if normalized and int(normalized) >= SMALL_NUMBER_LIMIT and normalized != expected_digits:
                return None
        lines.append(finding.texto.strip())
    return lines


def validate_lines(lines: list[str], report: dict) -> list[str] | None:
    """Compatibilidad: valida textos sueltos contra todas las cifras del informe (menos estricto)."""
    allowed = allowed_numbers(report)
    for line in lines:
        if len(line) > MAX_LINE_CHARS:
            return None
        for token in _NUMBER.findall(line):
            normalized = _digits(token)
            if normalized and int(normalized) >= SMALL_NUMBER_LIMIT and normalized not in allowed:
                return None
    return lines


def _settle_safely(ledger, call_id: str, actual_usd: Decimal) -> None:
    try:
        ledger.settle(call_id, actual_usd)
    except Exception as exc:  # noqa: BLE001 - la reserva queda 'reserved' y cuenta hasta conciliar
        logger.error("analysis_settle_failed call_id=%s code=%s", call_id, getattr(exc, "code", type(exc).__name__))


def _mark_uncertain_safely(ledger, call_id: str) -> None:
    try:
        ledger.mark_uncertain(call_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("analysis_uncertain_failed call_id=%s code=%s", call_id, getattr(exc, "code", type(exc).__name__))


def generate_narrative(*, provider, pricing: Pricing | None, report: dict, config: NarrativeConfig, ledger,
                       limits: AnalysisLimits, report_kind: str, week_start: date, now: datetime) -> list[str] | None:
    """Devuelve las frases validadas o None. Orden: reservar -> llamar -> liquidar con el uso real -> validar."""
    if not config.enabled or provider is None or pricing is None or ledger is None:
        return None
    payload = aggregate_payload(report)
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # Idempotencia: mismo tipo, misma semana y mismos datos agregados = una sola llamada facturable.
    call_id = f"analysis:{report_kind}:{week_start.isoformat()}:{fingerprint[:32]}"
    request = LLMRequest(
        call_id=call_id, system=SYSTEM_PROMPT,
        messages=(ChatMessage(role="user", content=f"<cifras>\n{payload}\n</cifras>"),),
        max_output_tokens=config.max_output_tokens,
    )
    estimate = (len(SYSTEM_PROMPT) + len(payload)) // 2 + 50
    worst_case = worst_case_cost(pricing, estimate, config.max_output_tokens)
    try:
        ledger.reserve(call_id, report_kind, week_start, fingerprint, worst_case, limits, now)
    except BudgetExceededError as exc:
        logger.warning("analysis_budget_blocked call_id=%s code=%s", call_id, exc.code)
        return None
    except ReservationConflictError:
        logger.info("analysis_already_processed call_id=%s", call_id)
        return None
    except Exception as exc:  # noqa: BLE001 - sin libro no hay llamada
        logger.warning("analysis_ledger_unavailable call_id=%s code=%s", call_id,
                       getattr(exc, "code", type(exc).__name__))
        return None
    try:
        response = provider.complete(request)
    except Exception as exc:  # noqa: BLE001 - puede haber facturado: queda incierta hasta conciliar
        _mark_uncertain_safely(ledger, call_id)
        logger.warning("analysis_unavailable call_id=%s code=%s", call_id,
                       getattr(exc, "code", type(exc).__name__))
        return None
    # El uso verificado se liquida aunque la respuesta luego sea descartada: la API ya facturó.
    _settle_safely(ledger, call_id, estimate_cost(pricing, response.usage))
    try:
        parsed = _NarrativeOutput.model_validate_json(_json_block(response.text))
    except (ValidationError, ValueError):
        logger.warning("analysis_output_rejected call_id=%s", call_id)
        return None
    lines = validate_findings(parsed.hallazgos, report)
    if lines is None:
        logger.warning("analysis_findings_rejected call_id=%s", call_id)
        return None
    logger.info("analysis_generated call_id=%s lines=%d out_tokens=%d", call_id, len(lines),
                response.usage.output_tokens)
    return lines


def _json_block(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no_json")
    return text[start:end + 1]
