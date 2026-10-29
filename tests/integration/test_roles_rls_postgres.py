"""Roles de mínimo privilegio con RLS real en PostgreSQL local desechable.

Cada prueba se ejecuta con una conexión de un rol de login NO propietario, sin SUPERUSER ni BYPASSRLS.
Las contraseñas son aleatorias, existen solo durante la prueba y nunca se escriben en el repositorio.
Ninguna prueba usa el propietario para comprobar permisos de la aplicación.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors

from app.admin.service import AdminContext, AdminService
from app.core.config import Settings
from app.db.pool import create_pool
from app.integrations.whatsapp_client import SimulatedWhatsAppSender
from app.main import create_app
from app.repositories.jobs import JobRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

from urllib.parse import urlsplit  # noqa: E402

_PARTS = urlsplit(TEST_DATABASE_URL)
APP_LOGIN = "lacusine_app"
ADMIN_LOGIN = "lacusine_admin"
BUSINESS_TABLES = (
    "leads", "conversations", "messages", "webhook_events", "products", "human_handoffs",
    "llm_usage_events", "contact_leases", "outbound_messages", "budget_reservations", "admin_audit_log",
)
LINE = "100200300400"

_credentials: dict[str, str] = {}


def _owner() -> psycopg.Connection:
    return psycopg.connect(TEST_DATABASE_URL, autocommit=True)


def _url_for(login: str) -> str:
    return f"postgresql://{login}:{_credentials[login]}@{_PARTS.hostname}:{_PARTS.port}{_PARTS.path}"


def _as(login: str) -> psycopg.Connection:
    return psycopg.connect(_url_for(login), autocommit=True)


@pytest.fixture(scope="module", autouse=True)
def roles_ready():
    _reset_and_migrate()
    with _owner() as conn:
        for login, group in ((APP_LOGIN, "lacusine_backend"), (ADMIN_LOGIN, "lacusine_admin_ops")):
            conn.execute(f"drop role if exists {login}")
            _credentials[login] = secrets.token_urlsafe(18)
            conn.execute(f"create role {login} login password '{_credentials[login]}'")
            conn.execute(f"grant {group} to {login}")
    yield
    with _owner() as conn:
        for login in (APP_LOGIN, ADMIN_LOGIN):
            conn.execute(f"drop role if exists {login}")


@pytest.fixture(autouse=True)
def clean():
    with _owner() as conn:
        conn.execute(
            "truncate public.leads, public.products, public.webhook_events, public.llm_usage_events, "
            "public.budget_reservations, public.outbound_messages, public.contact_leases, "
            "public.admin_audit_log restart identity cascade"
        )


def _seed_event(event_id: str) -> None:
    payload = {"id": event_id, "from": "573001112233", "type": "text", "text": {"body": "hola"}}
    sha = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with _owner() as conn:
        conn.execute(
            "insert into public.webhook_events (external_event_id, contact_wa_id, payload, payload_sha256, "
            "expires_at, business_phone_id) values (%s, %s, %s::jsonb, %s, now() + interval '7 days', %s)",
            (event_id, "573001112233", json.dumps(payload), sha, LINE),
        )


def _denied(conn: psycopg.Connection, sql: str, params=()) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(sql, params)


# --- Atributos de los roles ---

@pytest.mark.parametrize("login", [APP_LOGIN, ADMIN_LOGIN])
def test_login_roles_are_neither_superuser_nor_bypassrls_nor_owner(login):
    with _owner() as conn:
        row = conn.execute(
            "select rolsuper, rolbypassrls, rolcreaterole, rolcreatedb from pg_roles where rolname = %s", (login,)
        ).fetchone()
        owns = conn.execute(
            "select count(*) from pg_class c join pg_roles r on r.oid = c.relowner "
            "where r.rolname = %s and c.relnamespace = 'public'::regnamespace", (login,)
        ).fetchone()[0]
    assert row == (False, False, False, False)
    assert owns == 0


def test_group_roles_cannot_login_and_have_no_bypass():
    with _owner() as conn:
        rows = conn.execute(
            "select rolname, rolcanlogin, rolsuper, rolbypassrls from pg_roles "
            "where rolname in ('lacusine_backend', 'lacusine_admin_ops')"
        ).fetchall()
    assert sorted(rows) == [("lacusine_admin_ops", False, False, False), ("lacusine_backend", False, False, False)]


# --- RLS y políticas ---

def test_rls_is_enabled_on_every_business_table():
    with _owner() as conn:
        rows = conn.execute(
            "select relname, relrowsecurity from pg_class where relnamespace = 'public'::regnamespace "
            "and relname = any(%s)", (list(BUSINESS_TABLES),)
        ).fetchall()
    assert len(rows) == len(BUSINESS_TABLES)
    assert all(enabled for _, enabled in rows)


def test_no_policy_targets_anon_authenticated_or_public():
    with _owner() as conn:
        leaked = conn.execute(
            "select tablename, policyname from pg_policies where schemaname = 'public' and "
            "(roles::text[] && array['anon', 'authenticated', 'public'])"
        ).fetchall()
    assert leaked == []


def test_app_role_without_group_membership_sees_nothing_under_rls():
    # Demuestra la causa del defecto: un privilegio sin política no basta; RLS oculta todas las filas.
    probe = "lacusine_probe"
    password = secrets.token_urlsafe(12)
    with _owner() as conn:
        conn.execute(f"drop role if exists {probe}")
        conn.execute(f"create role {probe} login password '{password}'")
        conn.execute(f"grant usage on schema public to {probe}")
        conn.execute(f"grant select on public.leads to {probe}")
        conn.execute("insert into public.leads (wa_contact_id) values ('573000000009')")
    try:
        with psycopg.connect(
            host=_PARTS.hostname, port=_PARTS.port, dbname=_PARTS.path.lstrip("/"), user=probe, password=password,
        ) as conn:
            assert conn.execute("select count(*) from public.leads").fetchone()[0] == 0
    finally:
        with _owner() as conn:
            conn.execute(f"drop owned by {probe}")
            conn.execute(f"drop role {probe}")


# --- Rol de la aplicación ---

def test_app_role_runs_the_backend_flow_through_rls():
    _seed_event("wamid.ROLE-1")
    pool = create_pool(_url_for(APP_LOGIN), max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        with pool.connection() as conn:
            assert conn.execute("select current_user").fetchone()[0] == APP_LOGIN
            lead = conn.execute(
                "insert into public.leads (wa_contact_id) values ('573000000002') returning id"
            ).fetchone()[0]
            conv = conn.execute(
                "insert into public.conversations (lead_id, automation_state) values (%s, 'automated') returning id",
                (lead,),
            ).fetchone()[0]
            conn.execute(
                "insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at, "
                "content_text, content_expires_at, window_trusted) values (%s, 'inbound', 'wamid.ROLE-M', 'text', "
                "now(), 'hola', now() + interval '7 days', true)",
                (conv,),
            )
            assert conn.execute("select count(*) from public.leads").fetchone()[0] == 1
            conn.execute("update public.conversations set automation_state = 'paused_for_human' where id = %s", (conv,))
            conn.execute(
                "insert into public.outbound_messages (idempotency_key, conversation_id, content_text, "
                "delivery_status, business_phone_id, kind) values ('k-role', %s, 'hola', 'pending', %s, 'reply')",
                (conv, LINE),
            )
            conn.execute("select count(*) from public.products").fetchone()
            conn.execute("select count(*) from public.llm_usage_events").fetchone()

        jobs = JobRepository(pool, owner="role-worker", lease_seconds=60)
        claimed = jobs.claim(5, uuid.uuid4)
        assert [c.external_event_id for c in claimed] == ["wamid.ROLE-1"]

        with pool.connection() as conn:
            purged = conn.execute("select * from public.purge_expired_personal_data(10)").fetchall()
            assert purged is not None
    finally:
        pool.close()


def test_app_role_is_denied_outside_its_grants():
    with _as(APP_LOGIN) as conn:
        _denied(conn, "select count(*) from public.admin_audit_log")
        _denied(conn, "insert into public.admin_audit_log (operator, action, target_type, target_id, from_state, "
                      "to_state, reason) values ('x', 'resolve_event', 'webhook_event', gen_random_uuid(), 'a', 'b', "
                      "'motivo de prueba')")
        _denied(conn, "delete from public.leads")
        _denied(conn, "truncate public.webhook_events")
        _denied(conn, "create table public.no_permitida (id int)")
        _denied(conn, "drop table public.leads")


def test_app_role_cannot_rewrite_the_audit_log_even_if_it_could_insert():
    with _owner() as conn:
        conn.execute(
            "insert into public.admin_audit_log (operator, action, target_type, target_id, from_state, to_state, "
            "reason) values ('seed', 'resolve_event', 'webhook_event', gen_random_uuid(), 'a', 'b', 'motivo de prueba')"
        )
    with _as(APP_LOGIN) as conn:
        _denied(conn, "update public.admin_audit_log set reason = 'alterado'")
        _denied(conn, "delete from public.admin_audit_log")


# --- Rol administrativo ---

def test_admin_role_reads_resolves_and_audits_through_the_service():
    with _owner() as conn:
        conv = _seed_paused_conversation(conn)
    pool = create_pool(_url_for(ADMIN_LOGIN), max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        service = AdminService(pool, AdminContext(operator="operador-de-prueba", environment="test"))
        assert isinstance(service.counts(), dict)
        service.resume_conversation(conv, reason="reactivación de prueba con rol administrativo")
        trail = service.audit_trail("conversation", conv)
        assert [row["action"] for row in trail] == ["resume_automation"]
    finally:
        pool.close()
    with _owner() as conn:
        assert conn.execute("select automation_state from public.conversations where id = %s", (conv,)).fetchone()[0] \
            == "automated"


def test_admin_role_cannot_rewrite_audit_or_touch_business_inserts():
    with _as(ADMIN_LOGIN) as conn:
        _denied(conn, "update public.admin_audit_log set reason = 'alterado'")
        _denied(conn, "delete from public.admin_audit_log")
        _denied(conn, "truncate public.admin_audit_log")
        _denied(conn, "insert into public.leads (wa_contact_id) values ('573000000003')")
        _denied(conn, "insert into public.webhook_events (external_event_id, contact_wa_id, payload, payload_sha256, "
                      "expires_at, business_phone_id) values ('x', 'x', '{}'::jsonb, 'x', now(), 'x')")


# --- Anónimos y autenticados ---

@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_supabase_public_roles_cannot_read_business_data(role):
    with _owner() as conn:
        conn.execute("insert into public.leads (wa_contact_id) values ('573000000004')")
        conn.execute(f"set role {role}")
        try:
            for table in ("leads", "messages", "webhook_events", "admin_audit_log"):
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute(f"select count(*) from public.{table}")
        finally:
            conn.execute("reset role")


def _seed_paused_conversation(conn: psycopg.Connection) -> uuid.UUID:
    lead = conn.execute("insert into public.leads (wa_contact_id) values ('573000000005') returning id").fetchone()[0]
    return conn.execute(
        "insert into public.conversations (lead_id, automation_state) values (%s, 'paused_for_human') returning id",
        (lead,),
    ).fetchone()[0]


# --- Flujo extremo a extremo con identidades no propietarias ---

SECRET = "secreto-de-prueba-roles"
RECIPIENT = "573001112233"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post_message(client, wamid: str, text: str):
    body = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": LINE},
        "messages": [{"from": RECIPIENT, "id": wamid, "timestamp": str(int(time.time())), "type": "text",
                      "text": {"body": text}}],
    }}]}]}).encode()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})


def _post_status(client, message_id: str, status: str):
    body = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": LINE},
        "statuses": [{"id": message_id, "status": status, "timestamp": "1761830400", "recipient_id": RECIPIENT}],
    }}]}]}).encode()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)})


def test_end_to_end_webhook_worker_outbox_with_app_role_only():
    app_url = _url_for(APP_LOGIN)
    settings = Settings(
        meta_app_secret=SECRET, meta_verify_token="v", database_url=app_url,
        meta_access_token="token-simulado", meta_phone_number_id=LINE, meta_graph_api_version="v26.0",
        meta_send_enabled=True, whatsapp_dry_run=False, whatsapp_allowed_recipients=(RECIPIENT,),
    )
    web_pool = create_pool(app_url, max_size=3)
    web_pool.open(wait=True, timeout=10)
    simulator = SimulatedWhatsAppSender()
    worker, worker_pool = build_worker(
        settings, WorkerSettings(enabled=True, owner="roles-e2e", lease_seconds=60, processing_timeout_seconds=30),
        sender=simulator,
    )
    worker_pool.open(wait=True, timeout=10)
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(web_pool, 7)))
    try:
        # 1. El cliente escribe; el webhook firmado se persiste con el rol de la aplicación.
        response = _post_message(client, "wamid.ROLES1", "Busco una cafetera Jura para mi casa")
        assert response.status_code == 200 and response.json()["accepted"] == 1

        # 2. Un ciclo del worker procesa el mensaje y despacha la respuesta simulada.
        assert worker.run_once() == 1
        assert [s.idempotency_key for s in simulator.sent] == ["wamid.ROLES1:reply"]

        # 3. Meta confirma la entrega mediante un webhook de estado firmado.
        with _as(APP_LOGIN) as conn:
            external_id = conn.execute("select external_message_id from public.outbound_messages").fetchone()[0]
        assert external_id.startswith("wamid.SIM")
        assert _post_status(client, external_id, "delivered").status_code == 200
        worker.run_once()
        with _as(APP_LOGIN) as conn:
            final = conn.execute(
                "select delivery_status, delivered_at is not null from public.outbound_messages"
            ).fetchone()
        assert final == ("delivered", True)

        # 4. Un webhook idéntico no genera otra respuesta.
        _post_message(client, "wamid.ROLES1", "texto distinto")
        worker.run_once()
        assert len(simulator.sent) == 1
        with _as(APP_LOGIN) as conn:
            assert conn.execute("select count(*) from public.outbound_messages").fetchone()[0] == 1
            assert conn.execute("select current_user").fetchone()[0] == APP_LOGIN
    finally:
        client.close()
        worker.shutdown()
        worker_pool.close()
        web_pool.close()


# --- Control de presupuesto con el rol de la aplicación ---

def test_budget_ledger_enforces_the_daily_limit_as_app_role():
    from app.services.budget_ledger import BudgetExceededError, BudgetLimits, PostgresBudgetLedger

    pool = create_pool(_url_for(APP_LOGIN), max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        ledger = PostgresBudgetLedger(pool)
        limits = BudgetLimits(daily_usd=Decimal("1.00"))
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for index in range(3):
            ledger.reserve(f"role-call-{index}", None, Decimal("0.30"), limits, day_start, month_start, now)
        with pytest.raises(BudgetExceededError):
            ledger.reserve("role-call-3", None, Decimal("0.30"), limits, day_start, month_start, now)
        ledger.settle("role-call-0", Decimal("0.10"))
        ledger.release("role-call-1")
        assert ledger.state("role-call-0") == "settled"
        assert ledger.state("role-call-1") == "released"
        assert ledger.state("role-call-3") is None
    finally:
        pool.close()
