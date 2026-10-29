"""Costos: cálculo, presupuestos, límites y registro idempotente. Tarifas de prueba, no de Anthropic."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.agent.conversation_context import ConversationContextService
from app.core.config import Settings
from app.integrations.llm import LLMResponse, LLMUsage
from app.services.cost_tracker import (
    BOGOTA,
    BudgetExceededError,
    CostTracker,
    Pricing,
    estimate_cost,
    worst_case_cost,
)
from tests.agent.fakes import InMemoryTurns, InMemoryUsageStore

PRICING = Pricing(
    input_per_mtok=Decimal("2"),
    output_per_mtok=Decimal("10"),
    cache_write_per_mtok=Decimal("2.5"),
    cache_read_per_mtok=Decimal("0.2"),
    reference="test",
)


def test_estimate_uses_all_four_token_categories():
    usage = LLMUsage(input_tokens=1_000_000, output_tokens=500_000, cache_read_tokens=1_000_000, cache_write_tokens=200_000)
    # 1M*2 + 0.5M*10 + 1M*0.2 + 0.2M*2.5 = 2 + 5 + 0.2 + 0.5 = 7.7 USD
    assert estimate_cost(PRICING, usage) == Decimal("7.700000")


def test_worst_case_is_input_plus_max_output():
    assert worst_case_cost(PRICING, 500_000, 100_000) == Decimal("2.000000")  # 1 + 1


def test_pricing_from_settings_requires_input_and_output_prices():
    assert Pricing.from_settings(Settings(meta_app_secret=None, meta_verify_token=None)) is None
    partial = Settings(meta_app_secret=None, meta_verify_token=None, price_input_usd_per_mtok=Decimal("1"))
    assert Pricing.from_settings(partial) is None


def test_missing_cache_prices_fall_back_to_input_price_which_overestimates():
    settings = Settings(
        meta_app_secret=None, meta_verify_token=None,
        price_input_usd_per_mtok=Decimal("2"), price_output_usd_per_mtok=Decimal("10"),
    )
    pricing = Pricing.from_settings(settings)
    assert pricing.cache_write_per_mtok == Decimal("2")
    assert pricing.cache_read_per_mtok == Decimal("2")


def _tracker(store=None, daily=None, monthly=None, interaction=None, now=None):
    clock_value = now or datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
    return CostTracker(
        store=store or InMemoryUsageStore(),
        pricing=PRICING,
        daily_budget_usd=daily,
        monthly_budget_usd=monthly,
        interaction_limit_usd=interaction,
        clock=lambda: clock_value,
    )


def test_precheck_blocks_without_pricing_even_for_zero_budget_configuration():
    tracker = CostTracker(store=InMemoryUsageStore(), pricing=None)
    with pytest.raises(BudgetExceededError) as excinfo:
        tracker.precheck(Decimal("0"))
    assert excinfo.value.code == "pricing_not_configured"


def test_daily_budget_counts_spend_since_bogota_midnight_only():
    store = InMemoryUsageStore()
    now = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)  # 10:00 en Bogotá
    start_today = datetime(2026, 10, 28, 5, 0, tzinfo=timezone.utc)  # 00:00 en Bogotá
    store.created.append((start_today - timedelta(minutes=1), Decimal("5")))  # ayer: no cuenta
    store.created.append((start_today + timedelta(hours=1), Decimal("0.9")))  # hoy: sí cuenta
    tracker = _tracker(store=store, daily=Decimal("1"), now=now)
    with pytest.raises(BudgetExceededError) as excinfo:
        tracker.precheck(Decimal("0.2"))
    assert excinfo.value.code == "daily_budget"
    tracker_ok = _tracker(store=store, daily=Decimal("1"), now=now)
    tracker_ok.precheck(Decimal("0.05"))  # 0.9 + 0.05 <= 1


def test_monthly_budget_uses_bogota_month_start():
    assert BOGOTA.utcoffset(None) == timedelta(hours=-5)
    store = InMemoryUsageStore()
    store.created.append((datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc), Decimal("9.9")))  # 30/sep local: no cuenta
    tracker = _tracker(store=store, monthly=Decimal("10"))
    tracker.precheck(Decimal("0.5"))


def test_interaction_limit_is_checked_before_budgets():
    tracker = _tracker(interaction=Decimal("0.01"))
    with pytest.raises(BudgetExceededError) as excinfo:
        tracker.precheck(Decimal("0.02"))
    assert excinfo.value.code == "interaction_limit"


def test_success_is_recorded_once_even_when_the_same_event_is_retried():
    store = InMemoryUsageStore()
    tracker = _tracker(store=store)
    response = LLMResponse(text="ok", model="m", provider="anthropic", usage=LLMUsage(input_tokens=1000, output_tokens=100))
    first = tracker.record_success("wamid.R1", None, response)
    second = tracker.record_success("wamid.R1", None, response)
    assert first == second == Decimal("0.003000")
    assert len(store.created) == 1  # el reintento no suma costo otra vez


def test_simulated_provider_never_records_cost():
    store = InMemoryUsageStore()
    tracker = _tracker(store=store)
    response = LLMResponse(text="ok", model="m", provider="fake", usage=LLMUsage(input_tokens=9999, output_tokens=9999))
    assert tracker.record_success("wamid.F1", None, response) == Decimal("0")


def test_failures_are_audited_with_zero_cost():
    store = InMemoryUsageStore()
    tracker = _tracker(store=store)
    tracker.record_failure("wamid.E1", None, "anthropic", "m", "timeout")
    assert store.failures[0].error_code == "timeout"
    assert store.failures[0].cost_usd == Decimal("0")


# --- Contexto conversacional ---

def _context(max_messages=8, max_chars=3000):
    turns = InMemoryTurns()
    return turns, ConversationContextService(turns, max_messages=max_messages, max_chars=max_chars)


def test_context_limits_number_of_messages_and_keeps_most_recent():
    import uuid
    turns, service = _context(max_messages=3)
    cid = uuid.uuid4()
    for i in range(6):
        turns.add(cid, "inbound", f"mensaje {i}")
    window = service.build(cid)
    assert [t.text for t in window.turns] == ["mensaje 3", "mensaje 4", "mensaje 5"]
    assert window.truncated is True


def test_context_trims_oldest_turns_to_respect_character_budget():
    import uuid
    turns, service = _context(max_messages=8, max_chars=250)
    cid = uuid.uuid4()
    turns.add(cid, "inbound", "a" * 230)  # 250 - 40 = 210 disponibles: no cabe
    turns.add(cid, "outbound", "b" * 40)
    window = service.build(cid)
    assert [t.text for t in window.turns] == ["b" * 40]
    assert window.truncated is True


def test_context_maps_directions_to_roles_and_orders_chronologically():
    import uuid
    turns, service = _context()
    cid = uuid.uuid4()
    turns.add(cid, "inbound", "hola")
    turns.add(cid, "outbound", "buenas")
    window = service.build(cid)
    assert [(t.role, t.text) for t in window.turns] == [("user", "hola"), ("assistant", "buenas")]
    assert window.truncated is False


def test_context_service_rejects_invalid_limits():
    with pytest.raises(ValueError):
        ConversationContextService(InMemoryTurns(), max_messages=0)
