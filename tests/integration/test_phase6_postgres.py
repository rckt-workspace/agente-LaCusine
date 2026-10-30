"""Fase 6 en PostgreSQL local desechable: atribución, notificaciones a asesores, migraciones y flujo E2E.

Proveedores externos simulados. Ninguna solicitud sale a Meta, Anthropic ni a ningún canal real.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import MigrationError, load_migrations, run_migrations
from app.db.pool import create_pool
from app.integrations.whatsapp_client import SimulatedWhatsAppSender
from app.main import create_app
from app.repositories.crm import PostgresHandoffRepository, PostgresLeadRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.services.advisor_notifications import (
    AdvisorNotificationDispatcher,
    NotificationError,
    PostgresAdvisorNotificationRepository,
    SimulatedAdvisorNotifier,
)
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from tests.integration.leases import age_all_leases
from tests.integration.test_postgres_persistence import (
    MIGRATIONS_DIR,
    TEST_DATABASE_URL,
    _reset_and_migrate,
)

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

LINE = "100200300400"
RECIPIENT = "573001112233"
SECRET = "secreto-fase-6"


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture(autouse=True)
def clean(schema_ready):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "truncate public.leads, public.products, public.webhook_events, public.llm_usage_events, "
            "public.budget_reservations, public.outbound_messages, public.contact_leases, "
            "public.advisor_notifications, public.admin_audit_log restart identity cascade"
        )


def _q(sql, params=()):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def _one(sql, params=()):
    return _q(sql, params)[0][0]


def _pool(size=4):
    p = create_pool(TEST_DATABASE_URL, max_size=size)
    p.open(wait=True, timeout=10)
    return p


def _conversation(contact="573009990001"):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead = conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id", (contact,)).fetchone()[0]
        return conn.execute("insert into public.conversations (lead_id) values (%s) returning id", (lead,)).fetchone()[0]


# --- Atribución de anuncios ---

def test_first_referral_is_kept_and_later_ones_never_overwrite_it():
    pool = _pool()
    try:
        leads = PostgresLeadRepository(pool)
        lead = leads.get_or_create("573008880001")
        assert leads.capture_referral(lead, {"source_type": "ad", "source_id": "111", "ctwa_clid": "primero"}) is True
        assert leads.capture_referral(lead, {"source_type": "ad", "source_id": "222", "ctwa_clid": "segundo"}) is False
        row = _q("select referral_source_id, referral_ctwa_clid, referral_captured_at is not null "
                 "from public.leads where id = %s", (lead,))[0]
        assert row == ("111", "primero", True)
    finally:
        pool.close()


def test_lead_without_referral_has_no_attribution():
    pool = _pool()
    try:
        lead = PostgresLeadRepository(pool).get_or_create("573008880002")
        assert _one("select referral_captured_at from public.leads where id = %s", (lead,)) is None
    finally:
        pool.close()


# --- Notificaciones a asesores ---

def test_handoff_queues_exactly_one_notice_with_identifiers_only():
    conv = _conversation("573009990010")
    pool = _pool()
    try:
        handoff = PostgresHandoffRepository(pool).open(conv, "warranty", "high")
        rows = _q("select handoff_id, conversation_id, reason, priority, status from public.advisor_notifications")
        assert rows == [(handoff, conv, "warranty", "high", "pending")]
        columns = {r[0] for r in _q(
            "select column_name from information_schema.columns where table_name = 'advisor_notifications'")}
        assert not columns & {"content", "content_text", "wa_contact_id", "phone", "name", "payload"}
    finally:
        pool.close()


def test_duplicate_handoff_request_creates_no_second_notice():
    conv = _conversation("573009990011")
    pool = _pool()
    try:
        repo = PostgresHandoffRepository(pool)
        first = repo.open(conv, "purchase_intent", "normal")
        again = repo.open(conv, "purchase_intent", "normal")
        assert first == again
        assert _one("select count(*) from public.advisor_notifications") == 1
    finally:
        pool.close()


def _dispatcher(pool, notifier, max_attempts=3, lease_seconds=60):
    return AdvisorNotificationDispatcher(
        PostgresAdvisorNotificationRepository(pool, max_attempts=max_attempts), notifier,
        owner_token_factory=uuid.uuid4, lease_seconds=lease_seconds, batch_size=10,
    )


def test_pending_notice_is_sent_once_and_marked_sent():
    conv = _conversation("573009990020")
    pool = _pool()
    try:
        PostgresHandoffRepository(pool).open(conv, "customer_request", "normal")
        notifier = SimulatedAdvisorNotifier()
        assert _dispatcher(pool, notifier).dispatch_once().sent == 1
        assert _dispatcher(pool, notifier).dispatch_once().claimed == 0
        assert len(notifier.sent) == 1
        assert _one("select status from public.advisor_notifications") == "sent"
        assert _one("select sent_at is not null from public.advisor_notifications") is True
    finally:
        pool.close()


def test_channel_failures_retry_then_stop_at_max_attempts():
    conv = _conversation("573009990021")
    pool = _pool()
    try:
        PostgresHandoffRepository(pool).open(conv, "complaint", "urgent")
        notifier = SimulatedAdvisorNotifier(script=[NotificationError("channel_busy", True)] * 2)
        dispatcher = _dispatcher(pool, notifier, max_attempts=2)
        dispatcher.dispatch_once()
        assert _one("select status from public.advisor_notifications") == "pending"
        dispatcher.dispatch_once()
        assert _q("select status, last_error_code, attempts from public.advisor_notifications") == \
            [("failed", "channel_busy", 2)]
    finally:
        pool.close()


def test_expired_lease_never_marks_a_notice_as_sent():
    conv = _conversation("573009990022")
    pool = _pool()
    try:
        PostgresHandoffRepository(pool).open(conv, "quote_request", "normal")
        repo = PostgresAdvisorNotificationRepository(pool, max_attempts=3)
        claims = repo.claim(1, lease_seconds=60, token=uuid.uuid4())
        assert len(claims) == 1
        age_all_leases(TEST_DATABASE_URL)
        assert repo.expire_leases() == 1
        assert _one("select status from public.advisor_notifications") == "pending"
        assert repo.finish_sent(claims[0]) is False  # el propietario antiguo ya no puede cerrarlo
        assert _one("select status from public.advisor_notifications") == "pending"
    finally:
        pool.close()


def test_concurrent_dispatchers_claim_disjoint_notices():
    pool = _pool(size=6)
    try:
        for index in range(12):
            conv = _conversation(f"5730099{index:05d}")
            PostgresHandoffRepository(pool).open(conv, "warranty", "normal")
        notifier_a, notifier_b = SimulatedAdvisorNotifier(), SimulatedAdvisorNotifier()
        barrier = threading.Barrier(2)

        def run(notifier):
            barrier.wait()
            _dispatcher(pool, notifier).dispatch_once()

        threads = [threading.Thread(target=run, args=(n,)) for n in (notifier_a, notifier_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids_a = {n.notification_id for n in notifier_a.sent}
        ids_b = {n.notification_id for n in notifier_b.sent}
        assert ids_a.isdisjoint(ids_b)
        assert len(ids_a | ids_b) == 12
    finally:
        pool.close()


# --- Migraciones: aplicación explícita e integridad ---

def _empty_schema():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("drop schema if exists public cascade")
        conn.execute("create schema public")
        conn.execute("grant usage on schema public to public")


def test_migration_runner_applies_once_and_refuses_modified_history(tmp_path):
    files = load_migrations(MIGRATIONS_DIR)
    _empty_schema()
    try:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute("create role anon nologin") if not _role_exists("anon") else None
            conn.execute("create role authenticated nologin") if not _role_exists("authenticated") else None
        first = run_migrations(TEST_DATABASE_URL, files, apply=True)
        assert len(first.applied) == len(files) == 13
        second = run_migrations(TEST_DATABASE_URL, files, apply=True)
        assert second.applied == [] and len(second.up_to_date) == 13

        # Una copia con un archivo alterado: la herramienta debe negarse, sin tocar el original del repositorio.
        copied = tmp_path / "migrations"
        shutil.copytree(MIGRATIONS_DIR, copied)
        target = copied / files[0].name
        target.write_text(target.read_text(encoding="utf-8") + "\n-- cambio posterior\n", encoding="utf-8")
        with pytest.raises(MigrationError) as excinfo:
            run_migrations(TEST_DATABASE_URL, load_migrations(copied), apply=True)
        assert excinfo.value.code == "migration_modified_after_apply"
    finally:
        _reset_and_migrate()


def _role_exists(name: str) -> bool:
    return bool(_one("select count(*) from pg_roles where rolname = %s", (name,)))


def test_migration_dry_run_writes_nothing():
    _empty_schema()
    try:
        report = run_migrations(TEST_DATABASE_URL, load_migrations(MIGRATIONS_DIR)[:1], apply=False)
        assert report.applied == [] and len(report.pending) == 1
        assert _one("select to_regclass('public.schema_migrations') is null") is True  # ensayo: no crea nada
    finally:
        _reset_and_migrate()


def test_backend_role_can_write_notices_and_anonymous_roles_cannot():
    assert _one("select has_table_privilege('lacusine_backend', 'public.advisor_notifications', 'insert')") is True
    assert _one("select has_table_privilege('anon', 'public.advisor_notifications', 'select')") is False
    assert _one("select has_table_privilege('authenticated', 'public.advisor_notifications', 'select')") is False


# --- Flujo E2E local: webhook con atribución -> worker -> transferencia -> aviso ---

def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _referral_message(client, wamid: str, text: str):
    body = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": LINE},
        "messages": [{"from": RECIPIENT, "id": wamid, "timestamp": str(int(time.time())), "type": "text",
                      "text": {"body": text},
                      "referral": {"source_type": "ad", "source_id": "120200000099",
                                   "source_url": "https://example.invalid/anuncio",
                                   "ctwa_clid": "clid-sintetico", "headline": "no se guarda"}}],
    }}]}]}).encode()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})


def test_end_to_end_referral_worker_handoff_and_advisor_notice(tmp_path):
    settings = Settings(meta_app_secret=SECRET, meta_verify_token="v", database_url=TEST_DATABASE_URL,
                        meta_access_token="token-simulado", meta_phone_number_id=LINE,
                        meta_graph_api_version="v26.0", meta_send_enabled=False, whatsapp_dry_run=True,
                        anthropic_enabled=False, agent_dry_run=True)
    web_pool = _pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(web_pool, 7)))
    notifier = SimulatedAdvisorNotifier()
    worker, worker_pool = build_worker(
        settings, WorkerSettings(enabled=True, owner="fase6-e2e", lease_seconds=60, processing_timeout_seconds=30),
        sender=SimulatedWhatsAppSender(), advisor_notifier=notifier,
    )
    worker_pool.open(wait=True, timeout=10)
    try:
        # 1. El cliente llega desde un anuncio. Meta envía el referral en el primer mensaje.
        response = _referral_message(client, "wamid.P6-1", "Hola, estoy buscando una cafetera para mi casa.")
        assert response.status_code == 200 and response.json()["accepted"] == 1

        # 2. El worker procesa el mensaje y guarda la atribución de primer contacto.
        assert worker.run_once() == 1
        attribution = _q("select referral_source_type, referral_source_id, referral_ctwa_clid, "
                         "referral_source_url from public.leads")
        assert attribution == [("ad", "120200000099", "clid-sintetico", "https://example.invalid/anuncio")]

        # 3. Una transferencia a asesor encola el aviso y el worker lo entrega al canal simulado.
        conv = _one("select id from public.conversations")
        handoff = PostgresHandoffRepository(worker_pool).open(conv, "warranty", "high")
        assert worker.run_once() == 0
        assert [n.handoff_id for n in notifier.sent] == [handoff]
        assert _one("select status from public.advisor_notifications") == "sent"

        # 4. Un reinicio del worker no reenvía el aviso ya entregado.
        restarted, restarted_pool = build_worker(
            settings, WorkerSettings(enabled=True, owner="fase6-restart", lease_seconds=60,
                                     processing_timeout_seconds=30),
            sender=SimulatedWhatsAppSender(), advisor_notifier=notifier,
        )
        restarted_pool.open(wait=True, timeout=10)
        try:
            restarted.run_once()
        finally:
            restarted.shutdown()
            restarted_pool.close()
        assert len(notifier.sent) == 1
    finally:
        client.close()
        worker.shutdown()
        worker_pool.close()
        web_pool.close()
