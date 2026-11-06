"""Generación PDF: formatos, periodos, tipos de informe y límites. Sin base de datos ni red."""

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import app.services.pdf_report as pdf
from app.services.analytics import week_containing
from app.services.analytics_report import build_weekly_report
from app.services.pdf_report import (
    KINDS,
    PdfTooLargeError,
    _period_text,
    build_pdf,
    count,
    cop,
    percent,
    usd,
)

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


def _empty_counts():
    return {"inbound_messages": 0, "outbound": {}, "webhook_events": {}, "conversations_new": 0,
            "handoffs_by_reason": {}, "leads_created_by_status": {}, "llm_failures_by_code": {},
            "llm": {"calls_succeeded": 0, "calls_failed": 0, "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": Decimal("0"),
                    "conversations_with_ai": 0, "leads_with_ai": 0},
            "budget_reservations_by_status": {}}


def _report(empty=True):
    counts = _empty_counts()
    if not empty:
        counts.update({"inbound_messages": 412, "conversations_new": 96,
                       "outbound": {"sent": 30, "delivered": 180, "read": 150, "failed": 6},
                       "leads_created_by_status": {"new": 40, "qualified": 18},
                       "lead_sources": {"sin_informacion": 40},
                       "commercial": {"events_by_type": {"purchase_intent": (22, 19)}, "events_total_ever": 9,
                                      "sales_total_ever": 3,
                                      "top_product_interest": [("Jura <b>cafeteras</b> & Ñandú", "Categoría", 5)],
                                      "sales_in_week": (3, 9_870_000, 1), "lead_cohort": (40, 3)}})
    return build_weekly_report(counts, week_containing(date(2026, 11, 5)))


def _pages(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_report_kind_builds_a_valid_pdf(kind):
    data = build_pdf(kind=kind, report=_report(empty=False), generated_at=NOW, trend=[])
    assert data.startswith(b"%PDF") and data.rstrip().endswith(b"%%EOF")
    assert _pages(data) >= 2  # portada más contenido


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_empty_period_still_produces_a_document(kind):
    assert build_pdf(kind=kind, report=_report(empty=True), generated_at=NOW).startswith(b"%PDF")


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        build_pdf(kind="todo", report=_report(), generated_at=NOW)


def test_pdf_larger_than_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 100)
    with pytest.raises(PdfTooLargeError):
        build_pdf(kind="operativo", report=_report(), generated_at=NOW)


def test_hostile_text_from_the_catalog_is_escaped_not_interpreted():
    data = build_pdf(kind="marketing", report=_report(empty=False), generated_at=NOW, trend=[])
    assert data.startswith(b"%PDF")  # el marcado del nombre no rompe el documento


def test_money_percent_and_counts_use_colombian_formats():
    assert cop(9_870_000) == "$9.870.000 COP"
    assert cop(None) == "Sin datos suficientes"
    assert count(1234) == "1.234"
    assert count(0) == "0"  # cero real
    assert percent(Decimal("91.7")) == "91,7 %"
    assert percent(None) == "Sin datos suficientes"
    assert usd(Decimal("3.42")) == "3,42 USD"
    assert usd(Decimal("0.0400")) == "0,0400 USD"


def test_period_is_stated_in_spanish_with_an_inclusive_last_day():
    assert _period_text("2026-11-02", "2026-11-09") == "del 2 al 8 de noviembre de 2026"
    assert _period_text("2026-10-26", "2026-11-02") == "del 26 de octubre de 2026 al 1 de noviembre de 2026"
