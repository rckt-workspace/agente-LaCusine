"""Analítica semanal: semanas en America/Bogota, tasas, comparativa y honestidad del informe. Sin base de datos."""

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.analytics import (
    BOGOTA,
    change_percent,
    local_day,
    previous_week,
    rate,
    week_containing,
)
from app.services.analytics_report import build_weekly_report


def _counts(**overrides):
    base = {
        "inbound_messages": 10,
        "outbound": {"sent": 2, "delivered": 3, "read": 4, "failed": 1},
        "webhook_events": {"processed": 10, "failed": 1, "dead_letter": 0, "ignored": 2},
        "conversations_new": 5,
        "handoffs_by_reason": {"customer_request": 1},
        "leads_created_by_status": {"new": 3, "qualifying": 2, "qualified": 1, "handed_off": 1, "won": 1},
        "llm": {"calls_succeeded": 8, "calls_failed": 1, "input_tokens": 40000, "output_tokens": 3000,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": Decimal("0.120000"),
                "conversations_with_ai": 4, "leads_with_ai": 4},
        "llm_failures_by_code": {"timeout": 1},
        "budget_reservations_by_status": {"settled": 8},
    }
    base.update(overrides)
    return base


# --- Semanas en Bogotá ---

def test_week_starts_on_monday_and_ends_exclusively_the_next_monday():
    window = week_containing(date(2026, 11, 5))  # jueves
    assert window.start_day == date(2026, 11, 2) and window.start_day.weekday() == 0
    assert window.end_day == date(2026, 11, 9)


def test_sunday_belongs_to_the_week_that_started_the_previous_monday():
    window = week_containing(date(2026, 11, 8))  # domingo
    assert window.start_day == date(2026, 11, 2)


def test_monday_midnight_bogota_opens_the_new_week_and_closes_the_previous_one():
    window = week_containing(date(2026, 11, 9))
    assert window.start_day == date(2026, 11, 9)
    assert previous_week(window).end_day == date(2026, 11, 9)


def test_week_boundaries_are_bogota_midnight_expressed_in_utc():
    window = week_containing(date(2026, 11, 5))
    assert window.start_utc == datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    assert window.end_utc == datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)


def test_an_event_at_bogota_23_59_on_sunday_is_inside_and_one_second_later_is_outside():
    window = week_containing(date(2026, 11, 5))
    just_inside = datetime(2026, 11, 9, 4, 59, 59, tzinfo=timezone.utc)  # domingo 23:59:59 Bogotá
    just_outside = datetime(2026, 11, 9, 5, 0, 0, tzinfo=timezone.utc)   # lunes 00:00 Bogotá
    assert window.start_utc <= just_inside < window.end_utc
    assert not (window.start_utc <= just_outside < window.end_utc)


def test_local_day_uses_bogota_and_rejects_naive_datetimes():
    assert local_day(datetime(2026, 11, 5, 4, 30, tzinfo=timezone.utc)) == date(2026, 11, 4)
    with pytest.raises(ValueError):
        local_day(datetime(2026, 11, 5, 4, 30))


# --- Tasas y comparativa ---

def test_rate_is_none_when_there_is_no_base_instead_of_dividing_by_zero():
    assert rate(0, 0) is None
    assert rate(3, 4) == Decimal("75.0")


def test_change_is_none_against_a_zero_base_and_signed_otherwise():
    assert change_percent(10, 0) is None
    assert change_percent(15, 10) == Decimal("50.0")
    assert change_percent(5, 10) == Decimal("-50.0")


# --- Informe: honestidad y forma ---

def test_sales_and_revenue_are_never_estimated_and_won_is_not_a_sale():
    report = build_weekly_report(_counts(), week_containing(date(2026, 11, 5)))
    assert report["ventas"]["estado"] == "sin_datos"
    assert report["ventas"]["ventas_confirmadas"] is None
    assert report["ventas"]["valor_confirmado_cop"] is None
    assert report["ventas"]["leads_con_estado_administrativo_won"] == 1


def test_delivery_rate_uses_only_meta_confirmed_states_as_numerator():
    report = build_weekly_report(_counts(), week_containing(date(2026, 11, 5)))
    activity = report["actividad"]
    assert activity["respuestas_aceptadas_por_meta"] == 2 + 3 + 4  # sent + delivered + read
    assert activity["respuestas_entregadas_o_leidas"] == 3 + 4     # solo delivered + read
    assert activity["tasa_entrega_pct"] == Decimal("77.8")


def test_cost_per_conversation_and_per_lead_are_none_without_activity():
    empty = _counts(llm={**_counts()["llm"], "cost_usd": Decimal("0"), "conversations_with_ai": 0, "leads_with_ai": 0})
    report = build_weekly_report(empty, week_containing(date(2026, 11, 5)))
    assert report["operacion_ia"]["costo_por_conversacion_usd"] is None
    assert report["operacion_ia"]["costo_por_lead_atendido_usd"] is None


def test_cost_per_conversation_is_computed_from_real_counts():
    report = build_weekly_report(_counts(), week_containing(date(2026, 11, 5)))
    assert report["operacion_ia"]["costo_por_conversacion_usd"] == Decimal("0.03")


def test_previous_week_comparison_is_present_and_safe_with_zero_base():
    previous = _counts(inbound_messages=0, conversations_new=0)
    report = build_weekly_report(_counts(), week_containing(date(2026, 11, 5)), previous=previous)
    comparison = report["comparacion_semana_anterior"]
    assert comparison["mensajes_entrantes_pct"] is None
    assert comparison["conversaciones_nuevas_pct"] is None


def test_report_contains_no_phone_numbers_or_message_text():
    report = build_weekly_report(_counts(), week_containing(date(2026, 11, 5)))
    flat = repr(report)
    assert "573" not in flat and "wamid" not in flat
    assert not any(re.search(r"\d{7,}", line) for line in report["resumen_ejecutivo"])


# --- Ventas e indicadores comerciales: cero real frente a sin datos ---

def _commercial(**overrides):
    base = {
        "events_by_type": {"purchase_intent": (4, 3), "quote_request": (2, 2)},
        "events_total_ever": 6, "sales_total_ever": 2,
        "top_product_interest": [("Jura", "Cafeteras automáticas", 7)],
        "sales_in_week": (2, 5_980_000, 1),
        "lead_cohort": (10, 2),
    }
    base.update(overrides)
    return base


def test_sales_are_sin_datos_without_any_commercial_history_not_zero():
    report = build_weekly_report(_counts(), week_containing(date(2026, 11, 5)))
    assert report["ventas"]["estado"] == "sin_datos"
    assert report["ventas"]["ventas_confirmadas"] is None
    assert report["prospectos"]["leads_con_intencion_de_compra"] is None


def test_real_zero_sales_are_reported_as_zero_when_history_exists():
    counts = _counts(commercial=_commercial(sales_in_week=(0, 0, 0), lead_cohort=(4, 0)))
    report = build_weekly_report(counts, week_containing(date(2026, 11, 5)))
    assert report["ventas"]["estado"] == "disponible"
    assert report["ventas"]["ventas_confirmadas"] == 0
    assert report["ventas"]["valor_confirmado_cop"] == 0
    assert report["ventas"]["conversion_lead_a_venta_pct"] == Decimal("0.0")


def test_confirmed_sales_drive_revenue_ticket_and_cohort_conversion():
    report = build_weekly_report(_counts(commercial=_commercial()), week_containing(date(2026, 11, 5)))
    sales = report["ventas"]
    assert sales["ventas_confirmadas"] == 2 and sales["valor_confirmado_cop"] == 5_980_000
    assert sales["ticket_promedio_cop"] == 2_990_000
    assert sales["ventas_canceladas"] == 1
    assert sales["conversion_lead_a_venta_pct"] == Decimal("20.0")


def test_purchase_intent_is_counted_as_intent_not_as_sale():
    report = build_weekly_report(_counts(commercial=_commercial(sales_in_week=(0, 0, 0))),
                                 week_containing(date(2026, 11, 5)))
    assert report["prospectos"]["leads_con_intencion_de_compra"] == 3
    assert report["ventas"]["ventas_confirmadas"] == 0


def test_conversion_is_none_when_the_cohort_is_empty():
    report = build_weekly_report(_counts(commercial=_commercial(lead_cohort=(0, 0))),
                                 week_containing(date(2026, 11, 5)))
    assert report["ventas"]["conversion_lead_a_venta_pct"] is None


def test_csv_export_flattens_the_report_without_personal_data():
    from app.services.analytics_report import report_to_csv_rows
    report = build_weekly_report(_counts(commercial=_commercial()), week_containing(date(2026, 11, 5)))
    rows = report_to_csv_rows(report)
    assert ("ventas", "ventas_confirmadas", "2") in rows
    assert any(section == "resumen_ejecutivo" for section, _, _ in rows)
    assert not any("573" in value or "wamid" in value for _, _, value in rows)


def test_origin_without_captured_attribution_is_declared_not_invented():
    counts = _counts(lead_sources={"sin_informacion": 3})
    report = build_weekly_report(counts, week_containing(date(2026, 11, 5)))
    assert report["prospectos"]["origen_prospectos"]["estado"] == "sin_informacion"
    assert report["prospectos"]["origen_prospectos"]["detalle"] == {}


def test_captured_origin_is_reported_by_source_type():
    counts = _counts(lead_sources={"sin_informacion": 1, "ctwa": 2})
    report = build_weekly_report(counts, week_containing(date(2026, 11, 5)))
    assert report["prospectos"]["origen_prospectos"] == {"estado": "disponible",
                                                         "detalle": {"sin_informacion": 1, "ctwa": 2}}
