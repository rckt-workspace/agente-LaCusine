"""Integración de la capa de IA con PostgreSQL real (base de pruebas protegida por db_guard).

Reutiliza el reinicio y las migraciones de test_postgres_persistence. Todos los datos son SINTÉTICOS.
"""

from __future__ import annotations

import io
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import openpyxl
import psycopg
import pytest
from psycopg.errors import CheckViolation

from app.agent.conversation_context import ConversationContextService
from app.agent.factory import build_commercial_agent
from app.agent.knowledge import KnowledgeService
from app.agent.orchestrator import OutcomeStatus
from app.core.config import Settings
from app.db.pool import create_pool
from app.integrations.llm import FakeLLMProvider
from app.repositories.catalog import PostgresCatalogRepository
from app.repositories.crm import (
    PostgresConversationRepository,
    PostgresConversationStateReader,
    PostgresHandoffRepository,
    PostgresLeadRepository,
    PostgresMessageRepository,
    PostgresRecentTurnsReader,
)
from app.repositories.llm_usage import PostgresUsageStore
from app.repositories.products import PostgresProductRepository
from app.services.catalog_xlsx import import_catalog_xlsx
from app.services.cost_tracker import UsageEvent
from app.integrations.llm import LLMUsage
from app.services.product_search import ProductSearchService
from tests.integration.test_postgres_persistence import (  # noqa: F401  (fixture autouse del módulo base)
    TEST_DATABASE_URL,
    _reset_and_migrate,
)

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture
def pool():
    p = create_pool(TEST_DATABASE_URL)
    p.open(wait=True, timeout=10)
    yield p
    p.close()


@pytest.fixture(autouse=True)
def clean(schema_ready):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "truncate public.leads, public.products, public.webhook_events, public.llm_usage_events "
            "restart identity cascade"
        )


def _catalog_book(extra_rows=()) -> io.BytesIO:
    headers = ["Marca", "Categoría", "Producto", "Referencia", "Precio (COP)", "Precio antes (COP)", "Ahorro (COP)",
               "Disponibilidad", "Nota de entrega", "Descripción", "Link del producto"]
    rows = [
        ["TestJura", "Cafeteras automáticas", "Cafetera TEST Espresso C3", "TEST-JURA-C3", 2990000, 4810000, 1820000,
         "Disponible", None, "Espresso automática de prueba", "https://lacuisineappliances.co/products/test-jura-c3"],
        ["TestWhp", "Neveras y refrigeración", "Nevera TEST Side by Side", "TEST-WHP-SBS", 3049900, None, None,
         "Disponible", "Entrega a finales de octubre", "Nevera de prueba 518 litros",
         "https://lacuisineappliances.co/products/test-whp-sbs"],
        ["TestSmeg", "Neveras y refrigeración", "Nevera TEST Retro", "TEST-SMEG-50", 12500000, None, None,
         "Agotado", None, "Nevera retro de prueba", "https://lacuisineappliances.co/products/test-smeg-50"],
        *extra_rows,
    ]
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = "Catálogo"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def _write_book(tmp_path, name="catalog.xlsx", extra_rows=()):
    path = tmp_path / name
    path.write_bytes(_catalog_book(extra_rows).getvalue())
    return path


def _count(sql: str, params: tuple = ()) -> int:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(sql, params).fetchone()[0]


# --- Catálogo: importación repetible y restricciones ---

def test_xlsx_import_is_repeatable_against_database(pool, tmp_path):
    from datetime import date
    repo = PostgresCatalogRepository(pool)
    path = _write_book(tmp_path)
    first = import_catalog_xlsx(path, date(2026, 10, 26), repo)
    second = import_catalog_xlsx(path, date(2026, 10, 26), repo)
    assert (first.inserted, first.updated, first.unchanged) == (3, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 3)
    assert _count("select count(*) from public.products") == 3


def test_new_extraction_date_updates_existing_rows_without_duplicates(pool, tmp_path):
    from datetime import date
    repo = PostgresCatalogRepository(pool)
    path = _write_book(tmp_path)
    import_catalog_xlsx(path, date(2026, 10, 26), repo)
    report = import_catalog_xlsx(path, date(2026, 11, 2), repo)
    assert report.updated == 3
    assert _count("select count(*) from public.products") == 3


def test_database_rejects_previous_price_not_higher_than_current(pool):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "insert into public.products (reference, brand, category, price_cop, previous_price_cop, "
                "source_hash, name) values ('TEST-BAD', 'M', 'C', 100000, 90000, %s, 'x')",
                ("a" * 64,),
            )


# --- Búsqueda comercial sobre la base real ---

@pytest.fixture
def loaded_catalog(pool, tmp_path):
    from datetime import date
    import_catalog_xlsx(_write_book(tmp_path), date(2026, 10, 26), PostgresCatalogRepository(pool))
    return PostgresProductRepository(pool)


def test_search_ranks_matches_and_respects_budget_and_limit(loaded_catalog):
    hits = loaded_catalog.search(["cafetera", "espresso"], [], None, 5)
    assert hits[0].reference == "TEST-JURA-C3"
    cheap = loaded_catalog.search(["nevera"], [], 4_000_000, 5)
    assert [h.reference for h in cheap] == ["TEST-WHP-SBS"]
    capped = loaded_catalog.search(["nevera"], [], None, 50)
    assert len(capped) <= 5


def test_reference_match_is_exact_and_case_insensitive(loaded_catalog):
    hits = loaded_catalog.search([], ["TEST-SMEG-50"], None, 5)
    assert hits and hits[0].reference == "TEST-SMEG-50" and hits[0].reference_match is True
    assert hits[0].is_available is False


def test_search_is_parametrized_and_safe_against_sql_text(loaded_catalog):
    hits = loaded_catalog.search(["x'; drop table public.products; --"], [], None, 5)
    assert hits == []
    assert _count("select count(*) from public.products") == 3


def test_service_corrects_typos_using_database_vocabulary(loaded_catalog):
    service = ProductSearchService(loaded_catalog)
    result = service.search("cafetra espresso")
    assert result.corrections.get("cafetra") == "cafetera"
    assert result.hits[0].reference == "TEST-JURA-C3"


def test_get_by_ids_returns_only_existing_products(loaded_catalog):
    known = loaded_catalog.search([], ["TEST-JURA-C3"], None, 1)[0].id
    found = loaded_catalog.get_by_ids([known, uuid.uuid4()])
    assert list(found) == [known]


# --- Costos persistidos ---

def _usage(call_id: str, cost: str = "0.001100") -> UsageEvent:
    return UsageEvent(
        call_id=call_id, conversation_id=None, provider="anthropic", model="claude-haiku-5-5", status="succeeded",
        usage=LLMUsage(input_tokens=500, output_tokens=120), cost_usd=Decimal(cost), error_code=None,
        pricing_reference="test",
    )


def test_success_is_recorded_once_under_concurrent_retries(pool):
    store = PostgresUsageStore(pool)
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        outcome = store.record_success_once(_usage("wamid.CONC"))
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert _count("select count(*) from public.llm_usage_events where call_id = 'wamid.CONC'") == 1


def test_failures_can_be_recorded_repeatedly_for_the_same_event(pool):
    store = PostgresUsageStore(pool)
    failure = UsageEvent(call_id="wamid.FAIL", conversation_id=None, provider="anthropic", model="m",
                         status="failed", usage=LLMUsage(), cost_usd=Decimal("0"), error_code="timeout",
                         pricing_reference=None)
    store.record_failure(failure)
    store.record_failure(failure)
    assert _count("select count(*) from public.llm_usage_events where call_id = 'wamid.FAIL'") == 2
    assert store.has_success("wamid.FAIL") is False


def test_failed_rows_must_carry_an_error_code(pool):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "insert into public.llm_usage_events (call_id, model, purpose, status) "
                "values ('wamid.BADFAIL', 'm', 'commercial_reply', 'failed')"
            )


def test_spend_window_sums_only_successful_costs(pool):
    store = PostgresUsageStore(pool)
    store.record_success_once(_usage("wamid.S1", "0.500000"))
    store.record_success_once(_usage("wamid.S2", "0.250000"))
    now = datetime.now(timezone.utc)
    assert store.spent_between(now - timedelta(hours=1), now + timedelta(hours=1)) == Decimal("0.750000")
    assert store.spent_between(now + timedelta(days=1), now + timedelta(days=2)) == Decimal("0")


# --- Handoffs y contexto ---

def test_return_request_handoff_reason_is_accepted_and_bogus_reason_rejected(pool):
    leads = PostgresLeadRepository(pool)
    conv = PostgresConversationRepository(pool).get_or_open(leads.get_or_create("573009990001"))
    handoffs = PostgresHandoffRepository(pool)
    assert handoffs.open(conv, "return_request", "high")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "insert into public.human_handoffs (conversation_id, reason) values (%s, 'bogus')", (conv,)
            )


def test_recent_turns_exclude_expired_and_empty_content_and_stay_in_one_conversation(pool):
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    messages = PostgresMessageRepository(pool)
    conv = conversations.get_or_open(leads.get_or_create("573009990002"))
    other = conversations.get_or_open(leads.get_or_create("573009990003"))
    now = datetime.now(timezone.utc)
    messages.record_inbound(conv, "wamid.T1", "text", now - timedelta(minutes=3), "primero", now + timedelta(days=1))
    messages.record_inbound(conv, "wamid.T2", "text", now - timedelta(minutes=2), "vencido", now - timedelta(seconds=1))
    messages.record_inbound(conv, "wamid.T3", "text", now - timedelta(minutes=1), "segundo", now + timedelta(days=1))
    messages.record_inbound(other, "wamid.T4", "text", now, "de otro cliente", now + timedelta(days=1))
    messages.record_inbound(conv, "wamid.T5", "text", now)  # sin contenido: no se recupera
    turns = PostgresRecentTurnsReader(pool).recent_turns(conv, limit=10)
    assert [text for _, text, _ in turns] == ["primero", "segundo"]
    assert all(direction == "inbound" for direction, _, _ in turns)


def test_conversation_state_reader_reports_automation_state(pool):
    leads = PostgresLeadRepository(pool)
    conv = PostgresConversationRepository(pool).get_or_open(leads.get_or_create("573009990004"))
    reader = PostgresConversationStateReader(pool)
    assert reader.automation_state(conv) == "automated"
    assert reader.automation_state(uuid.uuid4()) is None


# --- Flujo extremo a extremo con repositorios reales y proveedor simulado ---

def _settings(**overrides) -> Settings:
    base = dict(meta_app_secret=None, meta_verify_token=None, database_url=TEST_DATABASE_URL,
                agent_dry_run=True, anthropic_max_output_tokens=350)
    base.update(overrides)
    return Settings(**base)


def test_end_to_end_warranty_question_creates_handoff_row_in_database(pool, tmp_path):
    from datetime import date
    from app.repositories.crm import PostgresHandoffRepository as Handoffs
    import json

    import_catalog_xlsx(_write_book(tmp_path), date(2026, 10, 26), PostgresCatalogRepository(pool))
    leads = PostgresLeadRepository(pool)
    conv = PostgresConversationRepository(pool).get_or_open(leads.get_or_create("573009990005"))
    PostgresMessageRepository(pool).record_inbound(
        conv, "wamid.E2E0", "text", datetime.now(timezone.utc), "hola", datetime.now(timezone.utc) + timedelta(days=1)
    )

    agent = build_commercial_agent(
        _settings(),
        usage_store=PostgresUsageStore(pool),
        product_repository=PostgresProductRepository(pool),
        conversation_reader=PostgresConversationStateReader(pool),
        turns_reader=PostgresRecentTurnsReader(pool),
        handoff_repository=Handoffs(pool),
        knowledge=KnowledgeService(),
    )
    agent._provider = FakeLLMProvider(script=[json.dumps({
        "reply_text": "Te ayudo con la garantía; un asesor te contactará.",
        "intent": "warranty", "lead_stage": "HUMAN_REQUIRED", "recommended_product_ids": [],
        "requires_human": False, "handoff_reason": None,
    })])

    outcome = agent.handle(conv, "¿Qué garantía tiene la cafetera TEST Espresso?", "wamid.E2E1")

    assert outcome.status is OutcomeStatus.REPLIED
    assert outcome.requires_human is True and outcome.handoff_reason == "warranty"
    assert _count(
        "select count(*) from public.human_handoffs where conversation_id = %s and reason = 'warranty' "
        "and priority = 'high'",
        (conv,),
    ) == 1


def test_end_to_end_duplicate_event_is_answered_once_in_database(pool, tmp_path):
    from datetime import date
    import json

    import_catalog_xlsx(_write_book(tmp_path), date(2026, 10, 26), PostgresCatalogRepository(pool))
    conv = PostgresConversationRepository(pool).get_or_open(
        PostgresLeadRepository(pool).get_or_create("573009990006")
    )
    agent = build_commercial_agent(
        _settings(),
        usage_store=PostgresUsageStore(pool),
        product_repository=PostgresProductRepository(pool),
        conversation_reader=PostgresConversationStateReader(pool),
        turns_reader=PostgresRecentTurnsReader(pool),
        handoff_repository=PostgresHandoffRepository(pool),
    )
    provider = FakeLLMProvider(script=[json.dumps({
        "reply_text": "Tenemos la cafetera espresso.", "intent": "product_inquiry", "lead_stage": "DISCOVERY",
        "recommended_product_ids": [], "requires_human": False, "handoff_reason": None,
    })])
    agent._provider = provider
    first = agent.handle(conv, "busco cafetera espresso", "wamid.DUPE")
    second = agent.handle(conv, "busco cafetera espresso", "wamid.DUPE")
    assert first.status is OutcomeStatus.REPLIED
    assert second.status is OutcomeStatus.DUPLICATE_SKIPPED
    assert len(provider.calls) == 1
