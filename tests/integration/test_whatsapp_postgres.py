"""Salida a WhatsApp contra PostgreSQL real: dispatch, estados de Meta, ventana, control humano y E2E simulado.

Ninguna prueba realiza solicitudes a Meta: el envío usa SimulatedWhatsAppSender.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.pool import create_pool
from app.integrations.whatsapp_client import SimulatedWhatsAppSender
from app.main import create_app
from app.repositories.errors import StorageError
from app.repositories.jobs import JobRepository
from app.repositories.outbound import OutboundRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository, WebhookEventRecord
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from app.worker.dispatcher import OutboxDispatcher
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

SECRET = "secreto-de-prueba-fase-5"
PHONE_ID = "100200300400"
RECIPIENT = "573001112233"


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture(autouse=True)
def clean(schema_ready):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "truncate public.leads, public.products, public.webhook_events, public.llm_usage_events, "
            "public.budget_reservations, public.outbound_messages, public.contact_leases restart identity cascade"
        )


def _pool(max_size=8):
    p = create_pool(TEST_DATABASE_URL, max_size=max_size)
    p.open(wait=True, timeout=10)
    return p


def _count(sql, params=()):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(sql, params).fetchone()[0]


def _conversation(contact, last_inbound: datetime | None, state="automated"):
    """Crea lead y conversación, y opcionalmente un mensaje entrante que abre la ventana de 24 horas."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead = conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id", (contact,)).fetchone()[0]
        conv = conn.execute(
            "insert into public.conversations (lead_id, automation_state) values (%s, %s) returning id", (lead, state)
        ).fetchone()[0]
        if last_inbound is not None:
            conn.execute(
                "insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at, "
                "content_text, content_expires_at, window_trusted) values (%s, 'inbound', %s, 'text', %s, 'hola', %s, true)",
                (conv, f"wamid.IN-{uuid.uuid4().hex[:8]}", last_inbound, last_inbound + timedelta(days=7)),
            )
    return conv


def _outbound(conv, key, content="Tenemos varias opciones.", created=None, next_attempt=None, status="pending"):
    created = created or datetime.now(timezone.utc)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(
            "insert into public.outbound_messages (idempotency_key, conversation_id, content_text, created_at, "
            "next_attempt_at, delivery_status, business_phone_id) values (%s, %s, %s, %s, coalesce(%s, now()), %s, %s) "
            "returning id",
            (key, conv, content, created, next_attempt, status, PHONE_ID),
        ).fetchone()[0]


def _status_of(outbound_id):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute("select delivery_status from public.outbound_messages where id = %s",
                            (outbound_id,)).fetchone()[0]


def _dispatcher(pool, sender, owner="w1"):
    return OutboxDispatcher(
        repository=OutboundRepository(pool), sender=sender, business_phone_id=PHONE_ID, owner=owner,
        lease_seconds=60, max_attempts=3, backoff_base_seconds=5, backoff_max_seconds=900, batch_size=5,
    )


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _status_webhook(client, message_id, status):
    body = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE_ID},
        "statuses": [{"id": message_id, "status": status, "timestamp": "1761830400", "recipient_id": RECIPIENT}],
    }}]}]}).encode()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})


def _message_webhook(client, wamid, body_text, timestamp=None):
    stamp = str(int(time.time())) if timestamp is None else timestamp
    body = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE_ID},
        "messages": [{"from": RECIPIENT, "id": wamid, "timestamp": stamp, "type": "text",
                      "text": {"body": body_text}}],
    }}]}]}).encode()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})


# --- Dispatch: competencia, orden y ventana ---

def test_two_dispatchers_never_send_the_same_outbound_twice():
    pool_a, pool_b = _pool(), _pool()
    try:
        now = datetime.now(timezone.utc)
        for i in range(12):
            conv = _conversation(f"57300{i:07d}", last_inbound=now)
            _outbound(conv, f"wamid.D{i}:reply", created=now - timedelta(seconds=i))
        sender = SimulatedWhatsAppSender()
        barrier = threading.Barrier(2)

        def run(pool, owner):
            barrier.wait()
            dispatcher = _dispatcher(pool, sender, owner=owner)
            for _ in range(10):
                if dispatcher.dispatch_once().claimed == 0:
                    return

        threads = [threading.Thread(target=run, args=(p, f"d{i}")) for i, p in enumerate((pool_a, pool_b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        keys = [r.idempotency_key for r in sender.sent]
        assert len(keys) == 12 and len(set(keys)) == 12
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'sent'") == 12
    finally:
        pool_a.close()
        pool_b.close()


def test_pending_reply_delays_later_reply_of_the_same_conversation():
    pool = _pool()
    try:
        now = datetime.now(timezone.utc)
        conv = _conversation("573004000001", last_inbound=now)
        first = _outbound(conv, "k-first", created=now - timedelta(minutes=2),
                          next_attempt=now + timedelta(minutes=10))
        _outbound(conv, "k-second", created=now - timedelta(minutes=1))
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender).dispatch_once()
        assert sender.sent == []  # el primero aún espera reintento y el segundo no puede adelantarlo
        assert _status_of(first) == "pending"
    finally:
        pool.close()


def test_lease_expiry_makes_send_uncertain_and_it_is_never_resent():
    pool = _pool()
    try:
        conv = _conversation("573004000002", last_inbound=datetime.now(timezone.utc))
        outbound_id = _outbound(conv, "k-lease")
        repo = OutboundRepository(pool)
        claims = repo.claim(1, "crashed-owner", lease_seconds=1, token=uuid.uuid4())
        assert len(claims) == 1 and _status_of(outbound_id) == "sending"
        time.sleep(1.2)  # el dispatcher muere con la solicitud posiblemente aceptada por Meta
        assert repo.expire_leases() == 1
        assert _status_of(outbound_id) == "uncertain"
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender).dispatch_once()
        assert sender.sent == []  # no hay reenvío automático
    finally:
        pool.close()


def test_stale_owner_cannot_close_a_recovered_send():
    pool = _pool()
    try:
        conv = _conversation("573004000003", last_inbound=datetime.now(timezone.utc))
        _outbound(conv, "k-stale")
        repo = OutboundRepository(pool)
        stale = repo.claim(1, "old", lease_seconds=1, token=uuid.uuid4())[0]
        time.sleep(1.2)
        repo.expire_leases()
        assert repo.finish(stale, "sent", external_message_id="wamid.STALE") is False
        assert _count("select count(*) from public.outbound_messages where external_message_id = 'wamid.STALE'") == 0
    finally:
        pool.close()


def test_closed_service_window_blocks_and_open_window_sends():
    pool = _pool()
    try:
        now = datetime.now(timezone.utc)
        closed = _conversation("573004000004", last_inbound=now - timedelta(hours=30))
        open_conv = _conversation("573004000005", last_inbound=now - timedelta(hours=2))
        blocked_id = _outbound(closed, "k-closed", created=now - timedelta(minutes=2))
        sent_id = _outbound(open_conv, "k-open", created=now - timedelta(minutes=1))
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender).dispatch_once()
        assert _status_of(blocked_id) == "blocked"
        assert _status_of(sent_id) == "sent"
        assert len(sender.sent) == 1
    finally:
        pool.close()


def test_human_takeover_cancels_pending_reply_without_sending():
    pool = _pool()
    try:
        conv = _conversation("573004000006", last_inbound=datetime.now(timezone.utc), state="paused_for_human")
        outbound_id = _outbound(conv, "k-human")
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender).dispatch_once()
        assert _status_of(outbound_id) == "cancelled"
        assert sender.sent == []
    finally:
        pool.close()


def test_duplicate_meta_message_id_is_rejected_by_the_database():
    pool = _pool()
    try:
        now = datetime.now(timezone.utc)
        conv = _conversation("573004000007", last_inbound=now)
        _outbound(conv, "k-dup-1", created=now - timedelta(minutes=2))
        _outbound(conv, "k-dup-2", created=now - timedelta(minutes=1))
        repo = OutboundRepository(pool)
        a = repo.claim(1, "w", 60, uuid.uuid4())[0]
        repo.finish(a, "sent", external_message_id="wamid.SAME")
        b = repo.claim(1, "w", 60, uuid.uuid4())[0]
        with pytest.raises(StorageError):
            repo.finish(b, "sent", external_message_id="wamid.SAME")
    finally:
        pool.close()


# --- Estados de Meta ---

def _sent_outbound(pool, key="k-state"):
    conv = _conversation(f"573005{uuid.uuid4().int % 10**7:07d}", last_inbound=datetime.now(timezone.utc))
    outbound_id = _outbound(conv, key)
    repo = OutboundRepository(pool)
    claim = repo.claim(1, "w", 60, uuid.uuid4())[0]
    assert repo.finish(claim, "sent", external_message_id=f"wamid.{key}")
    return outbound_id


def test_status_before_sent_confirmation_is_unmatched_then_applies():
    pool = _pool()
    try:
        conv = _conversation("573004000008", last_inbound=datetime.now(timezone.utc))
        outbound_id = _outbound(conv, "k-early")
        repo = OutboundRepository(pool)
        assert repo.apply_meta_status("wamid.k-early", "delivered") == "unmatched"
        claim = repo.claim(1, "w", 60, uuid.uuid4())[0]
        repo.finish(claim, "sent", external_message_id="wamid.k-early")
        assert repo.apply_meta_status("wamid.k-early", "delivered") == "applied"
        assert _status_of(outbound_id) == "delivered"
    finally:
        pool.close()


def test_read_before_delivered_does_not_regress_the_state():
    pool = _pool()
    try:
        outbound_id = _sent_outbound(pool, "k-order")
        repo = OutboundRepository(pool)
        assert repo.apply_meta_status("wamid.k-order", "read") == "applied"
        assert repo.apply_meta_status("wamid.k-order", "delivered") == "unchanged"
        assert _status_of(outbound_id) == "read"
    finally:
        pool.close()


def test_duplicate_status_changes_nothing():
    pool = _pool()
    try:
        _sent_outbound(pool, "k-dupstatus")
        repo = OutboundRepository(pool)
        assert repo.apply_meta_status("wamid.k-dupstatus", "delivered") == "applied"
        assert repo.apply_meta_status("wamid.k-dupstatus", "delivered") == "unchanged"
    finally:
        pool.close()


def test_failed_after_delivered_is_ignored_and_failed_after_sent_applies():
    pool = _pool()
    try:
        delivered = _sent_outbound(pool, "k-fail-late")
        repo = OutboundRepository(pool)
        repo.apply_meta_status("wamid.k-fail-late", "delivered")
        assert repo.apply_meta_status("wamid.k-fail-late", "failed", "131047") == "unchanged"
        assert _status_of(delivered) == "delivered"

        failing = _sent_outbound(pool, "k-fail-now")
        assert repo.apply_meta_status("wamid.k-fail-now", "failed", "131026") == "applied"
        assert _status_of(failing) == "failed"
    finally:
        pool.close()


def test_meta_confirmation_recovers_an_uncertain_send():
    pool = _pool()
    try:
        conv = _conversation("573004000009", last_inbound=datetime.now(timezone.utc))
        outbound_id = _outbound(conv, "k-recover")
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c:
            c.execute("update public.outbound_messages set delivery_status = 'uncertain', external_message_id = "
                      "'wamid.k-recover' where id = %s", (outbound_id,))
        assert OutboundRepository(pool).apply_meta_status("wamid.k-recover", "delivered") == "applied"
        assert _status_of(outbound_id) == "delivered"
    finally:
        pool.close()


# --- Fallos de base y reinicios ---

def test_database_loss_during_dispatch_does_not_crash_the_worker_loop():
    pool = _pool()
    worker_settings = WorkerSettings(enabled=True, owner="resilience", lease_seconds=60, processing_timeout_seconds=30)
    settings = Settings(meta_app_secret=None, meta_verify_token=None, database_url=TEST_DATABASE_URL, meta_phone_number_id=PHONE_ID)
    worker, worker_pool = build_worker(settings, worker_settings, sender=SimulatedWhatsAppSender())
    worker_pool.open(wait=True, timeout=10)
    try:
        worker_pool.close()  # la base deja de responder
        assert worker.run_once() == 0  # el ciclo registra el error y continúa
    finally:
        worker.shutdown()
        pool.close()


def test_restart_between_send_and_persistence_leaves_an_uncertain_not_a_duplicate():
    pool = _pool()
    try:
        conv = _conversation("573004000010", last_inbound=datetime.now(timezone.utc))
        _outbound(conv, "k-restart")
        repo = OutboundRepository(pool)
        repo.claim(1, "before-restart", lease_seconds=1, token=uuid.uuid4())
        time.sleep(1.2)
        repo.expire_leases()
        sender = SimulatedWhatsAppSender()
        _dispatcher(pool, sender, owner="after-restart").dispatch_once()
        assert sender.sent == []
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'uncertain'") == 1
    finally:
        pool.close()


# --- Flujo extremo a extremo simulado ---

def test_end_to_end_webhook_worker_outbox_meta_simulated_and_status_delivered():
    settings = Settings(
        meta_app_secret=SECRET, meta_verify_token="v", database_url=TEST_DATABASE_URL,
        meta_access_token="token-simulado", meta_phone_number_id=PHONE_ID, meta_graph_api_version="v26.0",
        meta_send_enabled=True, whatsapp_dry_run=False, whatsapp_allowed_recipients=(RECIPIENT,),
    )
    pool = _pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(pool, 7)))
    simulator = SimulatedWhatsAppSender()
    worker, worker_pool = build_worker(
        settings, WorkerSettings(enabled=True, owner="e2e", lease_seconds=60, processing_timeout_seconds=30),
        sender=simulator,
    )
    worker_pool.open(wait=True, timeout=10)
    try:
        # 1. El cliente escribe; el webhook lo firma, lo valida y lo persiste.
        response = _message_webhook(client, "wamid.CLIENTE1",
                                    "Hola, estoy buscando una cafetera Jura para mi casa. ¿Qué opciones tienen?")
        assert response.status_code == 200 and response.json()["accepted"] == 1

        # 2. Un ciclo del worker procesa el mensaje y despacha la respuesta simulada.
        assert worker.run_once() == 1
        assert len(simulator.sent) == 1
        sent_id = simulator.sent[0].idempotency_key
        assert sent_id == "wamid.CLIENTE1:reply"
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'sent'") == 1

        # 3. Meta confirma la entrega mediante un webhook de estado firmado.
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            external_id = conn.execute("select external_message_id from public.outbound_messages").fetchone()[0]
        assert external_id.startswith("wamid.SIM")  # identificador devuelto por el simulador, no por Meta real
        assert _status_webhook(client, external_id, "delivered").status_code == 200
        worker.run_once()
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            final = conn.execute("select delivery_status, delivered_at is not null from public.outbound_messages").fetchone()
        assert final == ("delivered", True)

        # 4. Un webhook idéntico no genera otra respuesta ni cambia el estado.
        _message_webhook(client, "wamid.CLIENTE1", "texto distinto")
        worker.run_once()
        assert len(simulator.sent) == 1
        assert _count("select count(*) from public.outbound_messages") == 1
    finally:
        worker.shutdown()
        worker_pool.close()
        pool.close()
