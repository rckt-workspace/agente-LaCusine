"""Análisis narrativo: validación semántica por indicador, presupuesto acumulado, idempotencia y aislamiento de datos.
Proveedor simulado y libro en memoria: sin red, sin Anthropic real, sin base de datos."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.integrations.llm import LLMResponse, LLMUsage
from app.repositories.errors import StorageError
from app.services.analysis_budget import AnalysisLimits, InMemoryAnalysisBudgetLedger
from app.services.analysis_narrative import (
    NarrativeConfig,
    aggregate_payload,
    generate_narrative,
    validate_lines,
)
from app.services.analytics import week_containing
from app.services.analytics_report import build_weekly_report
from app.services.cost_tracker import Pricing, estimate_cost

PRICING = Pricing(input_per_mtok=Decimal("2"), output_per_mtok=Decimal("10"), cache_write_per_mtok=Decimal("2.5"),
                  cache_read_per_mtok=Decimal("0.1"), reference="test")
ON = NarrativeConfig(enabled=True, max_output_tokens=400, budget_usd_per_report=Decimal("0.02"))
LIMITS = AnalysisLimits(per_report_usd=Decimal("0.02"), daily_usd=Decimal("0.10"), monthly_usd=Decimal("1.00"))
WEEK = week_containing(date(2026, 11, 5))
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


def _report():
    counts = {
        "inbound_messages": 412, "outbound": {"sent": 30, "delivered": 180, "read": 150},
        "webhook_events": {}, "conversations_new": 96, "handoffs_by_reason": {"quote_request": 14},
        "leads_created_by_status": {"new": 40, "qualified": 18}, "llm_failures_by_code": {},
        "llm": {"calls_succeeded": 310, "calls_failed": 4, "input_tokens": 1, "output_tokens": 1,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": Decimal("3.42"),
                "conversations_with_ai": 80, "leads_with_ai": 95},
        "budget_reservations_by_status": {}, "lead_sources": {"sin_informacion": 40},
        "commercial": {"events_by_type": {"purchase_intent": (22, 19)}, "events_total_ever": 5, "sales_total_ever": 1,
                       "top_product_interest": [("Jura", "Cafeteras automáticas", 48)],
                       "sales_in_week": (3, 9_870_000, 0), "lead_cohort": (40, 3)},
    }
    return build_weekly_report(counts, WEEK)


@dataclass
class FakeProvider:
    reply: str = ""
    raises: Exception | None = None
    calls: int = 0
    last_request: object = None
    ledger: object = None
    state_during_call: str | None = None
    input_tokens: int = 900
    output_tokens: int = 120

    def complete(self, request):
        self.calls += 1
        self.last_request = request
        if self.ledger is not None:
            self.state_during_call = self.ledger.state(request.call_id)
        if self.raises is not None:
            raise self.raises
        return LLMResponse(text=self.reply, model="m", provider="anthropic",
                           usage=LLMUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens))


def _finding(indicador, cifra, texto):
    return {"indicador": indicador, "cifra": cifra, "texto": texto}


def _ok_reply(findings):
    return json.dumps({"hallazgos": findings})


def _run(reply=None, *, report=None, config=ON, limits=LIMITS, ledger=None, provider=None, now=NOW):
    ledger = ledger if ledger is not None else InMemoryAnalysisBudgetLedger()
    provider = provider if provider is not None else FakeProvider(reply=reply or "", ledger=ledger)
    if provider.ledger is None:
        provider.ledger = ledger
    lines = generate_narrative(provider=provider, pricing=PRICING, report=report or _report(), config=config,
                               ledger=ledger, limits=limits, report_kind="ejecutivo", week_start=WEEK.start_day,
                               now=now)
    return lines, provider, ledger


def _valid_reply():
    return _ok_reply([_finding("mensajes_recibidos", "412", "Se recibieron 412 mensajes.")])


# --- Activación y fallos: nunca llegan al PDF ---

def test_disabled_by_default_and_never_calls_the_provider():
    lines, provider, _ = _run(_valid_reply(), config=NarrativeConfig(enabled=False))
    assert lines is None and provider.calls == 0


def test_without_pricing_provider_or_ledger_nothing_is_generated():
    ledger = InMemoryAnalysisBudgetLedger()
    kwargs = dict(report=_report(), config=ON, ledger=ledger, limits=LIMITS, report_kind="ejecutivo",
                  week_start=WEEK.start_day, now=NOW)
    assert generate_narrative(provider=None, pricing=PRICING, **kwargs) is None
    assert generate_narrative(provider=FakeProvider(), pricing=None, **kwargs) is None
    assert generate_narrative(provider=FakeProvider(), pricing=PRICING, **{**kwargs, "ledger": None}) is None


def test_ledger_unavailable_means_no_call():
    class BrokenLedger(InMemoryAnalysisBudgetLedger):
        def reserve(self, *args, **kwargs):
            raise StorageError("analysis_budget_write_failed")

    lines, provider, _ = _run(_valid_reply(), ledger=BrokenLedger())
    assert lines is None and provider.calls == 0


def test_provider_failure_never_propagates_and_leaves_the_call_uncertain():
    class Boom(Exception):
        code = "timeout"

    ledger = InMemoryAnalysisBudgetLedger()
    lines, provider, _ = _run(ledger=ledger, provider=FakeProvider(raises=Boom("secreto interno")))
    assert lines is None and provider.calls == 1
    (call_id,) = list(ledger._rows)
    assert ledger.state(call_id) == "uncertain"
    counted = ledger._rows[call_id]["counted"]
    assert counted > 0  # la reserva incierta sigue contando contra el presupuesto
    assert ledger.counted_between(datetime(2026, 1, 1, tzinfo=timezone.utc), NOW) == counted


def test_malformed_oversized_or_empty_output_is_rejected():
    assert _run("no es json")[0] is None
    assert _run(_ok_reply([_finding("mensajes_recibidos", "412", "x" * 400)]))[0] is None
    assert _run(_ok_reply([]))[0] is None
    assert _run(_ok_reply([_finding("mensajes_recibidos", "412", "a")] * 6))[0] is None  # más de MAX_LINES


def test_old_free_text_schema_is_no_longer_accepted():
    assert _run(json.dumps({"lineas": ["Hay 412 mensajes."]}))[0] is None


# --- Validación semántica: cada conclusión cita un indicador real con su valor exacto ---

def test_a_finding_citing_an_existing_indicator_with_its_exact_value_is_accepted():
    lines, _, _ = _run(_ok_reply([
        _finding("mensajes_recibidos", "412", "Se recibieron 412 mensajes."),
        _finding("ingresos_confirmados_cop", "9.870.000", "Los ingresos confirmados fueron 9.870.000 COP."),
    ]))
    assert lines == ["Se recibieron 412 mensajes.", "Los ingresos confirmados fueron 9.870.000 COP."]


def test_an_invented_figure_in_the_text_discards_the_whole_narrative():
    assert _run(_ok_reply([_finding("mensajes_recibidos", "412", "Se vendieron 777 cafeteras.")]))[0] is None


def test_a_cited_value_that_differs_from_the_report_is_rejected():
    assert _run(_ok_reply([_finding("mensajes_recibidos", "500", "Se recibieron 500 mensajes.")]))[0] is None


def test_an_unknown_indicator_is_rejected():
    assert _run(_ok_reply([_finding("ventas_inventadas", "7", "Hubo 7 ventas.")]))[0] is None


def test_an_indicator_without_data_is_never_concluded_on():
    report = _report()
    report["ventas"]["ventas_confirmadas"] = None
    assert _run(_ok_reply([_finding("ventas_confirmadas", "3", "Hubo 3 ventas.")]), report=report)[0] is None


def test_small_wording_numbers_are_allowed_and_large_ones_must_match_the_cited_value():
    assert _run(_ok_reply([_finding("mensajes_recibidos", "412", "Hubo tres observaciones y 412 mensajes.")]))[0] \
        == ["Hubo tres observaciones y 412 mensajes."]


def test_validate_lines_keeps_small_wording_numbers_and_checks_large_ones():
    report = _report()
    assert validate_lines(["Hubo tres observaciones y 412 mensajes."], report) is not None
    assert validate_lines(["Hubo 9999 ventas."], report) is None


# --- Contabilidad: reserva antes de llamar, liquidación con uso real, idempotencia, límites ---

def test_the_reservation_exists_before_the_provider_is_called():
    lines, provider, _ = _run(_valid_reply())
    assert lines is not None
    assert provider.state_during_call == "reserved"


def test_success_is_settled_with_the_verified_usage_cost():
    provider = FakeProvider(reply=_valid_reply(), input_tokens=900, output_tokens=120)
    _, provider, ledger = _run(provider=provider)
    (call_id,) = list(ledger._rows)
    assert ledger.state(call_id) == "settled"
    assert ledger._rows[call_id]["counted"] == estimate_cost(PRICING, LLMUsage(input_tokens=900, output_tokens=120))


def test_a_response_that_is_discarded_is_still_settled_because_the_api_billed_it():
    ledger = InMemoryAnalysisBudgetLedger()
    lines, _, _ = _run("no es json", ledger=ledger)
    assert lines is None
    (call_id,) = list(ledger._rows)
    assert ledger.state(call_id) == "settled"


def test_the_same_data_is_never_paid_twice():
    ledger = InMemoryAnalysisBudgetLedger()
    first, provider, _ = _run(_valid_reply(), ledger=ledger)
    second_provider = FakeProvider(reply=_valid_reply())
    second, second_provider, _ = _run(provider=second_provider, ledger=ledger)
    assert first is not None and second is None
    assert second_provider.calls == 0


def test_changed_data_is_a_new_call_with_its_own_reservation():
    ledger = InMemoryAnalysisBudgetLedger()
    _run(_valid_reply(), ledger=ledger)
    changed = _report()
    changed["actividad"]["mensajes_entrantes"] = 999
    _, provider, _ = _run(_valid_reply(), report=changed, ledger=ledger)
    assert provider.calls == 1 and len(ledger._rows) == 2


def test_a_report_above_its_own_limit_is_blocked_before_the_call():
    tight = AnalysisLimits(per_report_usd=Decimal("0.0001"), daily_usd=Decimal("0.10"), monthly_usd=Decimal("1.00"))
    lines, provider, _ = _run(_valid_reply(), limits=tight)
    assert lines is None and provider.calls == 0


def _worst_case_for(report) -> Decimal:
    """Peor caso real de una llamada con este informe (mismo cálculo que el módulo)."""
    from app.services.analysis_narrative import SYSTEM_PROMPT
    from app.services.cost_tracker import worst_case_cost

    payload = aggregate_payload(report)
    return worst_case_cost(PRICING, (len(SYSTEM_PROMPT) + len(payload)) // 2 + 50, ON.max_output_tokens)


def test_the_daily_limit_blocks_the_next_report_of_the_same_day():
    worst = _worst_case_for(_report())
    # Cabe una llamada reservada. Después queda liquidada con su coste real (menor que el peor caso),
    # y la segunda no cabe: el libro usa el importe liquidado, no solo la reserva.
    daily = AnalysisLimits(per_report_usd=Decimal("0.02"), daily_usd=worst + Decimal("0.0005"),
                           monthly_usd=Decimal("1.00"))
    ledger = InMemoryAnalysisBudgetLedger()
    first, _, _ = _run(_valid_reply(), limits=daily, ledger=ledger)
    assert first is not None
    changed = _report()
    changed["actividad"]["mensajes_entrantes"] = 1000
    lines, provider, _ = _run(_valid_reply(), report=changed, limits=daily, ledger=ledger)
    assert lines is None and provider.calls == 0


def test_the_monthly_limit_blocks_even_when_the_day_is_free():
    worst = _worst_case_for(_report())
    monthly = AnalysisLimits(per_report_usd=Decimal("0.02"), daily_usd=Decimal("0.10"),
                             monthly_usd=worst + Decimal("0.0005"))
    ledger = InMemoryAnalysisBudgetLedger()
    _run(_valid_reply(), limits=monthly, ledger=ledger, now=datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc))
    changed = _report()
    changed["actividad"]["mensajes_entrantes"] = 1000
    # 20 de noviembre: el día está libre, pero el mes ya tiene el gasto del 2.
    lines, provider, _ = _run(_valid_reply(), report=changed, limits=monthly, ledger=ledger,
                              now=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))
    assert lines is None and provider.calls == 0


def test_a_released_reservation_no_longer_counts_and_can_be_reserved_again():
    ledger = InMemoryAnalysisBudgetLedger()
    ledger.reserve("analysis:x", "ejecutivo", WEEK.start_day, "f" * 64, Decimal("0.02"), LIMITS, NOW)
    ledger.release("analysis:x")
    assert ledger.counted_between(datetime(2026, 1, 1, tzinfo=timezone.utc), NOW) == Decimal("0")
    ledger.reserve("analysis:x", "ejecutivo", WEEK.start_day, "f" * 64, Decimal("0.02"), LIMITS, NOW)
    assert ledger.state("analysis:x") == "reserved"


def test_a_settle_failure_never_reaches_the_pdf():
    class FailingSettle(InMemoryAnalysisBudgetLedger):
        def settle(self, call_id, actual_usd):
            raise StorageError("analysis_budget_write_failed")

    lines, _, _ = _run(_valid_reply(), ledger=FailingSettle())
    assert lines == ["Se recibieron 412 mensajes."]


# --- Payload: solo agregados, sin datos personales ni de mensajes ---

def test_the_payload_has_only_aggregates_and_no_personal_or_message_data():
    provider = FakeProvider(reply=_valid_reply())
    _run(provider=provider)
    content = provider.last_request.messages[0].content
    assert "412" in content and "Jura" not in content
    assert "573" not in content and "wamid" not in content and "@" not in content


def test_aggregate_payload_marks_missing_data_instead_of_zero():
    report = _report()
    report["ventas"]["ventas_confirmadas"] = None
    assert "ventas_confirmadas: Sin datos" in aggregate_payload(report)
