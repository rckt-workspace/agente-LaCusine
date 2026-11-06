"""Informe semanal de analítica comercial. Solo agrega cifras verificadas: no inventa ventas ni ingresos.

Reglas de honestidad:
  * Ventas e ingresos: solo registros confirmados administrativamente. Sin historia comercial: "sin_datos".
    El estado administrativo `won` de un lead NO es una venta: no tiene importe ni pedido.
  * Entregas: solo cuentan los estados que Meta confirma (delivered, read). "sent" es aceptación, no entrega.
  * Consultas fallidas: errores registrados de IA, eventos fallidos o en dead_letter y respuestas fallidas.
    Las respuestas de contingencia no tienen un estado propio, así que no se separan.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.analytics import WeekWindow, change_percent, money, rate

ACCEPTED_BY_META = ("sent", "delivered", "read")
DELIVERED_BY_META = ("delivered", "read")
LEAD_STATUSES = ("new", "qualifying", "qualified", "quoted", "handed_off", "won", "lost", "opted_out")


def _sum(mapping: dict, keys) -> int:
    return sum(int(mapping.get(key, 0)) for key in keys)


def build_weekly_report(counts: dict, window: WeekWindow, previous: dict | None = None) -> dict:
    out = counts["outbound"]
    llm = counts["llm"]
    accepted = _sum(out, ACCEPTED_BY_META)
    delivered = _sum(out, DELIVERED_BY_META)
    failed_outbound = int(out.get("failed", 0))
    failed_events = int(counts["webhook_events"].get("failed", 0)) + int(counts["webhook_events"].get("dead_letter", 0))
    ai_failures = int(llm["calls_failed"])
    leads_total = sum(int(v) for v in counts["leads_created_by_status"].values())
    cost = Decimal(llm["cost_usd"])
    conversations_ai = int(llm["conversations_with_ai"])
    leads_ai = int(llm["leads_with_ai"])
    handoffs_total = sum(int(v) for v in counts["handoffs_by_reason"].values())

    report = {
        "semana": {
            "inicio_bogota": window.start_day.isoformat(),
            "fin_exclusivo_bogota": window.end_day.isoformat(),
            "zona_horaria": "America/Bogota",
        },
        "actividad": {
            "mensajes_entrantes": int(counts["inbound_messages"]),
            "respuestas_aceptadas_por_meta": accepted,
            "respuestas_entregadas_o_leidas": delivered,
            "respuestas_leidas": int(out.get("read", 0)),
            "respuestas_fallidas": failed_outbound,
            "respuestas_pendientes": int(out.get("pending", 0)),
            "respuestas_inciertas": int(out.get("uncertain", 0)),
            "tasa_entrega_pct": rate(delivered, accepted),
            "conversaciones_nuevas": int(counts["conversations_new"]),
            "conversaciones_atendidas_por_ia": conversations_ai,
            "conversaciones_derivadas_a_asesor": handoffs_total,
            "consultas_fallidas": ai_failures + failed_events + failed_outbound,
            "detalle_fallos_ia": dict(counts["llm_failures_by_code"]),
        },
        "prospectos": {
            "leads_nuevos": leads_total,
            "leads_por_estado": {key: int(counts["leads_created_by_status"].get(key, 0)) for key in LEAD_STATUSES},
            "leads_en_calificacion": int(counts["leads_created_by_status"].get("qualifying", 0)),
            "leads_calificados": int(counts["leads_created_by_status"].get("qualified", 0)),
            "leads_transferidos_a_asesor": int(counts["leads_created_by_status"].get("handed_off", 0)),
            "derivaciones_por_motivo": dict(counts["handoffs_by_reason"]),
            "leads_con_intencion_de_compra": _events_leads(counts, "purchase_intent"),
            "leads_con_cotizacion_solicitada": _events_leads(counts, "quote_request"),
            "productos_mas_consultados": _top_products(counts),
            "origen_prospectos": _sources(counts),
        },
        "ventas": _sales_section(counts),
        "operacion_ia": {
            "llamadas_exitosas": int(llm["calls_succeeded"]),
            "llamadas_fallidas": ai_failures,
            "tokens_entrada": int(llm["input_tokens"]),
            "tokens_salida": int(llm["output_tokens"]),
            "tokens_lectura_cache": int(llm["cache_read_tokens"]),
            "tokens_escritura_cache": int(llm["cache_write_tokens"]),
            "costo_estimado_usd": money(cost),
            "costo_por_conversacion_usd": money(cost / conversations_ai) if conversations_ai else None,
            "costo_por_lead_atendido_usd": money(cost / leads_ai) if leads_ai else None,
            "reservas_presupuesto_por_estado": dict(counts["budget_reservations_by_status"]),
            "bloqueos_por_presupuesto": None,  # el bloqueo no se persiste todavía; solo queda en el log
        },
    }

    if previous is not None:
        prev_accepted = _sum(previous["outbound"], ACCEPTED_BY_META)
        report["comparacion_semana_anterior"] = {
            "mensajes_entrantes_pct": change_percent(counts["inbound_messages"], previous["inbound_messages"]),
            "respuestas_aceptadas_pct": change_percent(accepted, prev_accepted),
            "conversaciones_nuevas_pct": change_percent(counts["conversations_new"], previous["conversations_new"]),
            "costo_estimado_pct": change_percent(cost, Decimal(previous["llm"]["cost_usd"])),
        }

    report["resumen_ejecutivo"] = _executive_summary(report)
    return report



def _has_commercial_history(counts: dict) -> bool:
    com = counts.get("commercial")
    return bool(com) and (int(com["events_total_ever"]) > 0 or int(com["sales_total_ever"]) > 0)


def _events_leads(counts: dict, event_type: str):
    """Leads distintos con ese evento en la semana. None si nunca se registró historia comercial."""
    if not _has_commercial_history(counts):
        return None
    return int(counts["commercial"]["events_by_type"].get(event_type, (0, 0))[1])


def _sources(counts: dict) -> dict:
    """Origen de los leads creados. Sin atribución capturada, lo declara en lugar de inventar campañas."""
    raw = counts.get("lead_sources", {})
    total = sum(int(v) for v in raw.values())
    unknown = int(raw.get("sin_informacion", 0))
    if total == 0:
        return {"estado": "sin_leads", "detalle": {}}
    if unknown == total:
        return {"estado": "sin_informacion", "mensaje": "Sin información de origen disponible.", "detalle": {}}
    return {"estado": "disponible", "detalle": {k: int(v) for k, v in raw.items()}}


def _top_products(counts: dict):
    if not _has_commercial_history(counts):
        return None
    return [{"marca": brand, "categoria": category, "consultas": n}
            for brand, category, n in counts["commercial"]["top_product_interest"]]


def _sales_section(counts: dict) -> dict:
    """Ventas solo desde registros confirmados. Sin historia comercial el estado es 'sin_datos', no cero."""
    empty = {
        "ventas_confirmadas": None, "valor_confirmado_cop": None, "ticket_promedio_cop": None,
        "ventas_canceladas": None, "conversion_lead_a_venta_pct": None,
        "leads_de_la_cohorte": None, "leads_con_venta_confirmada": None,
        "leads_con_estado_administrativo_won": int(counts["leads_created_by_status"].get("won", 0)),
    }
    if not _has_commercial_history(counts):
        return {"estado": "sin_datos",
                "motivo": "Todavía no hay ventas ni eventos comerciales registrados.", **empty}
    com = counts["commercial"]
    sales_count, revenue, cancelled = com["sales_in_week"]
    cohort_total, cohort_with_sale = com["lead_cohort"]
    sales_count, revenue, cancelled = int(sales_count), Decimal(revenue), int(cancelled)
    return {
        "estado": "disponible",
        "motivo": "Ventas con confirmación administrativa registrada.",
        "ventas_confirmadas": sales_count,
        "valor_confirmado_cop": int(revenue),
        "ticket_promedio_cop": int(revenue / sales_count) if sales_count else None,
        "ventas_canceladas": cancelled,
        "conversion_lead_a_venta_pct": rate(int(cohort_with_sale), int(cohort_total)),
        "leads_de_la_cohorte": int(cohort_total),
        "leads_con_venta_confirmada": int(cohort_with_sale),
        "leads_con_estado_administrativo_won": empty["leads_con_estado_administrativo_won"],
    }


def _cop_text(value) -> str:
    return "$" + f"{int(value):,}".replace(",", ".") + " COP"


def _usd_text(value) -> str:
    amount = Decimal(str(value))
    text = f"{amount:.2f}" if abs(amount) >= 1 else f"{amount:.4f}"
    return text.replace(".", ",") + " USD"


def _executive_summary(report: dict) -> list[str]:
    act, pro, ops = report["actividad"], report["prospectos"], report["operacion_ia"]
    lines = [
        f"Se recibieron {act['mensajes_entrantes']} mensajes de clientes y Meta aceptó "
        f"{act['respuestas_aceptadas_por_meta']} respuestas.",
    ]
    if act["tasa_entrega_pct"] is not None:
        lines.append(f"De las respuestas aceptadas, {str(act['tasa_entrega_pct']).replace('.', ',')} % "
                     "figuran como entregadas o leídas.")
    lines.append(f"Se abrieron {act['conversaciones_nuevas']} conversaciones nuevas y "
                 f"{act['conversaciones_derivadas_a_asesor']} derivaciones a asesor.")
    lines.append(f"Hubo {pro['leads_calificados']} leads calificados y {pro['leads_transferidos_a_asesor']} "
                 f"transferidos a un asesor.")
    if report["ventas"]["estado"] == "disponible":
        lines.append(f"Ventas confirmadas: {report['ventas']['ventas_confirmadas']} por "
                     f"{_cop_text(report['ventas']['valor_confirmado_cop'])}.")
    else:
        lines.append("Ventas confirmadas: sin datos todavía; no se estiman ingresos.")
    if ops["costo_estimado_usd"] is not None:
        lines.append(f"Costo estimado de IA: {_usd_text(ops['costo_estimado_usd'])}.")
    return lines


def report_to_csv_rows(report: dict) -> list[tuple[str, str, str]]:
    """Filas (sección, indicador, valor) para CSV. Sin datos personales: el informe no los contiene."""
    rows: list[tuple[str, str, str]] = []

    def walk(section: str, value, path: str = "") -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                walk(section, inner, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, inner in enumerate(value, 1):
                walk(section, inner, f"{path}[{index}]")
        else:
            rows.append((section, path, "" if value is None else str(value)))

    for section, content in report.items():
        if section == "resumen_ejecutivo":
            for index, line in enumerate(content, 1):
                rows.append((section, f"linea_{index}", line))
        else:
            walk(section, content)
    return rows
