"""Estabilización 5.1 contra PostgreSQL real: transferencia humana, ventana, línea de negocio, envío incierto,
conciliación administrativa y arranque del pipeline. Ninguna solicitud sale a Meta ni a Anthropic.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest

from app.admin.cli import run as admin_cli
from app.admin.policy import AdminPolicyError
from app.admin.service import AdminContext, AdminService
from app.core.config import Settings
from app.db.pool import create_pool
from app.integrations.whatsapp_client import SimulatedWhatsAppSender
from app.repositories.crm import (
    PostgresConversationRepository,
    PostgresConversationStateReader,
    PostgresHandoffRepository,
    PostgresLeadRepository,
)
from app.repositories.errors import StorageError
from app.repositories.outbound import OutboundRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository, WebhookEventRecord
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from app.worker.dispatcher import OutboxDispatcher
from tests.integration.leases import age_all_leases
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

LINE = "100200300400"
RECIPIENT = "573001112233"


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture(autouse=True)
def clean(schema_ready):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "truncate public.leads, public.products, public.webhook_events, public.llm_usage_events, "
            "public.budget_reservations, public.outbound_messages, public.contact_leases, "
            "public.admin_audit_log restart identity cascade"
        )


def _pool(size=6):
    p = create_pool(TEST_DATABASE_URL, max_size=size)
    p.open(wait=True, timeout=10)
    return p


def _q(sql, params=()):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def _one(sql, params=()):
    return _q(sql, params)[0][0]


def _conversation(contact, last_inbound: datetime | None = None, state="automated"):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead = conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id", (contact,)).fetchone()[0]
        conv = conn.execute("insert into public.conversations (lead_id, automation_state) values (%s, %s) returning id",
                            (lead, state)).fetchone()[0]
        if last_inbound is not None:
            conn.execute(
                "insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at, "
                "content_text, content_expires_at, window_trusted) values (%s, 'inbound', %s, 'text', %s, 'hola', %s, true)",
                (conv, f"wamid.IN-{uuid.uuid4().hex[:8]}", last_inbound, last_inbound + timedelta(days=7)),
            )
    return conv


def _reply(conv, key, created=None, status="pending"):
    created = created or datetime.now(timezone.utc)
    return _one(
        "insert into public.outbound_messages (idempotency_key, conversation_id, content_text, created_at, "
        "delivery_status, business_phone_id, kind) values (%s, %s, 'respuesta de IA', %s, %s, %s, 'reply') returning id",
        (key, conv, created, status, LINE),
    )


def _status(table, column, row_id):
    return _one(f"select {column} from public.{table} where id = %s", (row_id,))


def _settings(**extra):
    return Settings(meta_app_secret="secreto-5-1", meta_verify_token="v", database_url=TEST_DATABASE_URL,
                    meta_phone_number_id=LINE, meta_graph_api_version="v26.0", **extra)


def _dispatcher(pool, sender, owner="d1"):
    return OutboxDispatcher(repository=OutboundRepository(pool), sender=sender, business_phone_id=LINE,
                            owner=owner, lease_seconds=60, max_attempts=3, backoff_base_seconds=5,
                            backoff_max_seconds=900, batch_size=10)


# --- A. Transferencia humana efectiva ---

@pytest.mark.parametrize("reason", ["customer_request", "warranty", "complaint", "quote_request", "return_request"])
def test_opening_a_handoff_pauses_automation_for_every_escalation_reason(reason):
    pool = _pool()
    try:
        conv = _conversation(f"5730060{abs(hash(reason)) % 10000:04d}", last_inbound=datetime.now(timezone.utc))
        PostgresHandoffRepository(pool).open(conv, reason, "high")
        assert PostgresConversationStateReader(pool).automation_state(conv) == "paused_for_human"
        assert _one("select reason from public.human_handoffs where conversation_id = %s", (conv,)) == reason
    finally:
        pool.close()


def test_opening_a_handoff_cancels_pending_ai_replies_in_the_same_transaction():
    pool = _pool()
    try:
        conv = _conversation("573006000001", last_inbound=datetime.now(timezone.utc))
        stale = _reply(conv, "old-reply")
        PostgresHandoffRepository(pool).open(conv, "customer_request")
        assert _status("outbound_messages", "delivery_status", stale) == "cancelled"
        assert _status("outbound_messages", "last_error_code", stale) == "handoff_opened"
    finally:
        pool.close()


def test_duplicate_handoff_is_idempotent_and_keeps_one_active_request():
    pool = _pool()
    try:
        conv = _conversation("573006000002", last_inbound=datetime.now(timezone.utc))
        repo = PostgresHandoffRepository(pool)
        first = repo.open(conv, "customer_request")
        second = repo.open(conv, "warranty", "urgent")
        assert first == second
        assert _one("select count(*) from public.human_handoffs where conversation_id = %s", (conv,)) == 1
    finally:
        pool.close()


def test_two_processes_opening_the_same_handoff_produce_one_request_and_one_pause():
    pool_a, pool_b = _pool(), _pool()
    try:
        conv = _conversation("573006000003", last_inbound=datetime.now(timezone.utc))
        barrier = threading.Barrier(2)
        results = []

        def open_from(pool):
            barrier.wait()
            results.append(PostgresHandoffRepository(pool).open(conv, "complaint", "high"))

        threads = [threading.Thread(target=open_from, args=(p,)) for p in (pool_a, pool_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 1
        assert _one("select count(*) from public.human_handoffs where conversation_id = %s", (conv,)) == 1
    finally:
        pool_a.close()
        pool_b.close()


def test_message_arriving_during_human_attention_gets_no_automatic_reply():
    pool = _pool()
    try:
        conv_lead = PostgresLeadRepository(pool).get_or_create(RECIPIENT)
        conv = PostgresConversationRepository(pool).get_or_open(conv_lead)
        PostgresHandoffRepository(pool).open(conv, "customer_request")
        now = int(time.time())
        event = PostgresWebhookEventRepository(pool, retention_days=7)
        body = {"id": "wamid.DURING", "from": RECIPIENT, "timestamp": str(now), "type": "text",
                "text": {"body": "¿ya tienen la cafetera?"}}
        event.register(WebhookEventRecord(external_event_id="wamid.DURING", contact_wa_id=RECIPIENT, payload=body,
                                          payload_sha256="c" * 64, business_phone_id=LINE))
        sender = SimulatedWhatsAppSender()
        worker, worker_pool = build_worker(_settings(), WorkerSettings(enabled=True, owner="h1", lease_seconds=60,
                                                                       processing_timeout_seconds=30), sender=sender)
        worker_pool.open(wait=True, timeout=10)
        try:
            worker.run_once()
        finally:
            worker.shutdown()
            worker_pool.close()
        assert _one("select count(*) from public.outbound_messages where kind = 'reply'") == 0
        assert sender.sent == [] or all(r.idempotency_key.endswith(":handoff_notice") for r in sender.sent)
    finally:
        pool.close()


def test_handoff_notice_is_sent_once_and_the_ai_reply_is_not():
    pool = _pool()
    try:
        conv = _conversation("573006000004", last_inbound=datetime.now(timezone.utc))
        ai_reply = _reply(conv, "ai-before-transfer")
        PostgresHandoffRepository(pool).open(conv, "warranty")
        notice_id = _one(
            "insert into public.outbound_messages (idempotency_key, conversation_id, content_text, delivery_status, "
            "kind, business_phone_id) values ('x:handoff_notice', %s, 'Te comunico con un asesor.', 'pending', "
            "'handoff_notice', %s) returning id", (conv, LINE))
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender).dispatch_once()
        assert [r.idempotency_key for r in sender.sent] == ["x:handoff_notice"]
        assert _status("outbound_messages", "delivery_status", notice_id) == "sent"
        assert _status("outbound_messages", "delivery_status", ai_reply) == "cancelled"
    finally:
        pool.close()


def test_resume_requires_no_active_handoff_and_is_audited(tmp_path):
    pool = _pool()
    try:
        conv = _conversation("573006000005", last_inbound=datetime.now(timezone.utc))
        handoff = PostgresHandoffRepository(pool).open(conv, "customer_request")
        admin = AdminService(pool, AdminContext(operator="ana", environment="test"))
        with pytest.raises(AdminPolicyError) as excinfo:
            admin.resume_conversation(conv, "el asesor terminó")
        assert excinfo.value.code == "active_handoff_exists"

        admin.resolve_handoff(handoff, "resolved", "atendido por teléfono", None)
        admin.resume_conversation(conv, "atendido y cerrado por asesor")
        assert PostgresConversationStateReader(pool).automation_state(conv) == "automated"
        assert _one("select count(*) from public.admin_audit_log where action = 'resume_automation'") == 1
    finally:
        pool.close()


# --- B. Ventana de atención con cola atrasada ---

def test_delayed_customer_message_does_not_reopen_the_window_at_processing_time():
    pool = _pool()
    try:
        conv = _conversation("573006000006")
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        _one(
            "insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at, "
            "content_text, content_expires_at, window_trusted) values (%s, 'inbound', 'wamid.OLD30', 'text', %s, "
            "'hola', %s, true) returning id", (conv, old, old + timedelta(days=7)))
        reply = _reply(conv, "late-reply")
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender).dispatch_once()
        assert _status("outbound_messages", "delivery_status", reply) == "blocked"
        assert sender.sent == []
    finally:
        pool.close()


def test_newer_trusted_message_keeps_window_open_even_if_an_older_one_is_processed_later():
    pool = _pool()
    try:
        conv = _conversation("573006000007")
        now = datetime.now(timezone.utc)
        for wamid, when in (("wamid.NEW", now - timedelta(minutes=5)), ("wamid.LATE-OLD", now - timedelta(hours=30))):
            _one(
                "insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at, "
                "content_text, content_expires_at, window_trusted) values (%s, 'inbound', %s, 'text', %s, 'x', %s, true) "
                "returning id", (conv, wamid, when, when + timedelta(days=7)))
        reply = _reply(conv, "fresh-reply")
        _dispatcher(pool, SimulatedWhatsAppSender()).dispatch_once()
        assert _status("outbound_messages", "delivery_status", reply) == "sent"
    finally:
        pool.close()


def test_message_without_trusted_timestamp_never_opens_the_window():
    pool = _pool()
    try:
        conv = _conversation("573006000008")
        _one(
            "insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at, "
            "content_text, content_expires_at, window_trusted) values (%s, 'inbound', 'wamid.UNTRUSTED', 'text', now(), "
            "'hola', now() + interval '7 days', false) returning id", (conv,))
        reply = _reply(conv, "untrusted-reply")
        _dispatcher(pool, SimulatedWhatsAppSender()).dispatch_once()
        assert _status("outbound_messages", "delivery_status", reply) == "blocked"
    finally:
        pool.close()


# --- C. Línea de negocio ---

def test_outbound_from_another_business_line_is_never_sent():
    pool = _pool()
    try:
        conv = _conversation("573006000009", last_inbound=datetime.now(timezone.utc))
        foreign = _one(
            "insert into public.outbound_messages (idempotency_key, conversation_id, content_text, delivery_status, "
            "kind, business_phone_id) values ('foreign', %s, 'hola', 'pending', 'reply', '999999999999') returning id",
            (conv,))
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender).dispatch_once()
        assert sender.sent == []
        assert _status("outbound_messages", "delivery_status", foreign) == "failed"
        assert _status("outbound_messages", "last_error_code", foreign) == "business_line_mismatch"
    finally:
        pool.close()


# --- D. Envío incierto y fallo de persistencia posterior al envío ---

def test_persistence_failure_after_a_successful_send_becomes_uncertain_and_is_never_resent(monkeypatch):
    pool = _pool()
    try:
        conv = _conversation("573006000010", last_inbound=datetime.now(timezone.utc))
        outbound_id = _reply(conv, "persist-fail")
        repo = OutboundRepository(pool)
        original_finish = repo.finish
        calls = {"n": 0}

        def flaky_finish(claim, status, **kwargs):
            if status == "sent" and calls["n"] == 0:
                calls["n"] += 1
                raise StorageError("outbound_finish_failed")  # la base cae justo después de que Meta aceptara
            return original_finish(claim, status, **kwargs)

        monkeypatch.setattr(repo, "finish", flaky_finish)
        sender = SimulatedWhatsAppSender()
        dispatcher = OutboxDispatcher(repository=repo, sender=sender, business_phone_id=LINE, owner="d1",
                                      lease_seconds=1, max_attempts=3, backoff_base_seconds=5,
                                      backoff_max_seconds=900)
        with pytest.raises(StorageError):
            dispatcher.dispatch_once()
        assert len(sender.sent) == 1
        assert _status("outbound_messages", "delivery_status", outbound_id) == "sending"

        age_all_leases(TEST_DATABASE_URL)  # el lease vence: el resultado es desconocido
        dispatcher.dispatch_once()
        assert _status("outbound_messages", "delivery_status", outbound_id) == "uncertain"
        assert len(sender.sent) == 1  # nunca hubo un segundo envío
    finally:
        pool.close()


# --- E. Conciliación administrativa ---

def test_operator_confirms_uncertain_send_with_external_evidence_and_audit_has_no_content():
    pool = _pool()
    try:
        conv = _conversation("573006000011", last_inbound=datetime.now(timezone.utc))
        outbound_id = _reply(conv, "recon-ok", status="uncertain")
        admin = AdminService(pool, AdminContext(operator="ana", environment="test"))
        admin.resolve_outbound(outbound_id, "sent", "Meta confirma el mensaje en el panel", "wamid.HBgNREC1")
        assert _status("outbound_messages", "delivery_status", outbound_id) == "sent"
        assert _status("outbound_messages", "external_message_id", outbound_id) == "wamid.HBgNREC1"
        trail = admin.audit_trail("outbound_message", outbound_id)
        assert trail[0]["operator"] == "ana" and trail[0]["evidence"] == "wamid.HBgNREC1"
        assert "respuesta de IA" not in json.dumps(trail, ensure_ascii=False)
    finally:
        pool.close()


def test_uncertain_send_cannot_be_resolved_without_evidence():
    pool = _pool()
    try:
        conv = _conversation("573006000012", last_inbound=datetime.now(timezone.utc))
        outbound_id = _reply(conv, "recon-noev", status="uncertain")
        admin = AdminService(pool, AdminContext(operator="ana", environment="test"))
        with pytest.raises(AdminPolicyError) as excinfo:
            admin.resolve_outbound(outbound_id, "sent", "sin prueba", None)
        assert excinfo.value.code == "evidence_required"
        assert _status("outbound_messages", "delivery_status", outbound_id) == "uncertain"
    finally:
        pool.close()


def test_concurrent_double_resolution_applies_exactly_one_decision():
    pool_a, pool_b = _pool(), _pool()
    try:
        conv = _conversation("573006000013", last_inbound=datetime.now(timezone.utc))
        outbound_id = _reply(conv, "recon-race", status="uncertain")
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def decide(pool, operator, evidence):
            barrier.wait()
            try:
                AdminService(pool, AdminContext(operator=operator, environment="test")).resolve_outbound(
                    outbound_id, "sent", "decisión concurrente", evidence)
                result = "ok"
            except AdminPolicyError as exc:
                result = exc.code
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=decide, args=(pool_a, "ana", "wamid.RACE-A")),
                   threading.Thread(target=decide, args=(pool_b, "luis", "wamid.RACE-B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(outcomes) in (sorted(["ok", "state_not_eligible"]),
                                    sorted(["ok", "state_changed_concurrently"]))
        assert _one("select count(*) from public.admin_audit_log where target_id = %s", (outbound_id,)) == 1
    finally:
        pool_a.close()
        pool_b.close()


def test_budget_settlement_cannot_exceed_the_reservation_and_release_needs_evidence():
    pool = _pool()
    try:
        reservation = _one(
            "insert into public.budget_reservations (call_id, status, reserved_usd, counted_usd) "
            "values ('wamid.BUD-1', 'uncertain', 0.300000, 0.300000) returning id")
        admin = AdminService(pool, AdminContext(operator="ana", environment="test"))
        with pytest.raises(AdminPolicyError) as excinfo:
            admin.resolve_reservation(reservation, "settled", "costo real", None, Decimal("0.5"))
        assert excinfo.value.code == "evidence_required"
        with pytest.raises(AdminPolicyError) as excinfo:
            admin.resolve_reservation(reservation, "settled", "costo real", "factura-1", Decimal("0.5"))
        assert excinfo.value.code == "amount_exceeds_reservation"
        admin.resolve_reservation(reservation, "settled", "costo real verificado", "factura-1", Decimal("0.120000"))
        assert _status("budget_reservations", "counted_usd", reservation) == Decimal("0.120000")
    finally:
        pool.close()


def test_event_requeue_is_blocked_while_its_budget_is_unreconciled():
    pool = _pool()
    try:
        event = _one(
            "insert into public.webhook_events (external_event_id, payload_sha256, expires_at, processing_status) "
            "values ('wamid.REQ', %s, now() + interval '7 days', 'manual_review') returning id", ("d" * 64,))
        _one("insert into public.budget_reservations (call_id, status, reserved_usd, counted_usd) "
             "values ('wamid.REQ', 'reserved', 0.1, 0.1) returning id")
        admin = AdminService(pool, AdminContext(operator="ana", environment="test"))
        with pytest.raises(AdminPolicyError) as excinfo:
            admin.resolve_event(event, "retry", "reintento tras revisión")
        assert excinfo.value.code == "budget_unreconciled"
        assert _status("webhook_events", "processing_status", event) == "manual_review"
    finally:
        pool.close()


def test_inspection_shows_metadata_only_and_rejects_non_uuid_identifiers():
    pool = _pool()
    try:
        conv = _conversation("573006000014", last_inbound=datetime.now(timezone.utc))
        outbound_id = _reply(conv, "inspect-me", status="uncertain")
        admin = AdminService(pool, AdminContext(operator="ana", environment="test"))
        case = admin.inspect("outbound_message", outbound_id)
        assert case["status"] == "uncertain"
        assert "content_text" not in case and "respuesta de IA" not in json.dumps(case)
        assert case["content_length"] == len("respuesta de IA")
        with pytest.raises(AdminPolicyError) as excinfo:
            admin.inspect("outbound_message", "no-es-uuid")
        assert excinfo.value.code == "id_invalid"
    finally:
        pool.close()


def test_admin_cli_status_runs_against_the_test_database_and_mutation_needs_all_guards(capsys):
    url = TEST_DATABASE_URL
    env = {
        "LACUSINE_ADMIN_DATABASE_URL": url, "LACUSINE_ENVIRONMENT": "test", "DATABASE_URL": "",
        "LACUSINE_ADMIN_ROLE": "postgres", "LACUSINE_ADMIN_ENABLED": "true",
    }
    import io

    out = io.StringIO()
    assert admin_cli(["--environment", "test", "--operator", "ana", "status"], environ=env, out=out) == 0
    assert '"ok": true' in out.getvalue()

    out = io.StringIO()
    missing = str(uuid.uuid4())
    code = admin_cli(["--environment", "test", "--operator", "ana", "resolve-event", "--id", missing,
                      "--confirm", "otro", "--reason", "motivo", "--to", "retry"], environ=env, out=out)
    assert code == 2 and "confirmation_mismatch" in out.getvalue()


def test_handoff_transaction_rolls_back_pause_and_request_if_a_later_step_fails():
    pool = _pool()
    try:
        conv = _conversation("573006000015", last_inbound=datetime.now(timezone.utc))
        pending = _reply(conv, "must-survive-rollback")
        # Fallo forzado DESPUÉS de la pausa y de la solicitud: al cancelar la respuesta pendiente.
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute("alter table public.outbound_messages add constraint no_cancel_for_test "
                         "check (delivery_status <> 'cancelled') not valid")
        try:
            with pytest.raises(StorageError):
                PostgresHandoffRepository(pool).open(conv, "warranty")
        finally:
            with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
                conn.execute("alter table public.outbound_messages drop constraint no_cancel_for_test")
        # Ningún paso parcial sobrevive: ni la solicitud, ni la pausa, ni la cancelación.
        assert _one("select count(*) from public.human_handoffs where conversation_id = %s", (conv,)) == 0
        assert PostgresConversationStateReader(pool).automation_state(conv) == "automated"
        assert _status("outbound_messages", "delivery_status", pending) == "pending"
    finally:
        pool.close()
