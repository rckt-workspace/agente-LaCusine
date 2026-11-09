"""Recorrido completo del canal whatsapp-web.js contra PostgreSQL real, con el transporte web_bridge.

    mensaje entrante (contrato del conector) -> /internal/web-bridge/inbound -> webhook_events
    -> worker -> agente simulado (FakeLLM, sin llamadas facturables) -> outbox -> WebBridgeSender
    -> conector (simulado aquí por un transporte que implementa el contrato HMAC /send)

Ninguna prueba contacta WhatsApp ni Anthropic. El conector real tiene sus propias pruebas en Node.
"""

from __future__ import annotations

import json
import time

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.pool import create_pool
from app.integrations.web_bridge import WebBridgeSender, sign, verify
from app.main import create_app
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

SECRET = "secreto-webjs-recorrido-de-32-caracteres"
LINE = "573153863179"
CLIENT = "573001112233"
BRIDGE = "http://127.0.0.1:8787"


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


class ConnectorSimulator:
    """Implementa el contrato HTTP firmado de /send del conector. Registra cada solicitud recibida."""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | server_error | timeout
        self.received: list[dict] = []
        self.signatures_ok = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        stamp = request.headers["X-Bridge-Timestamp"]
        if verify(SECRET, "send", stamp, body, request.headers.get("X-Bridge-Signature")):
            self.signatures_ok += 1
        self.received.append(json.loads(body))
        if self.mode == "timeout":
            raise httpx.ReadTimeout("el conector no respondió a tiempo", request=request)
        if self.mode == "server_error":
            return httpx.Response(500, json={"error": "send_failed"})
        return httpx.Response(200, json={"message_id": f"true_{len(self.received)}_WEBJS"})


def _settings(**overrides) -> Settings:
    base = dict(
        meta_app_secret=None, meta_verify_token=None, database_url=TEST_DATABASE_URL,
        whatsapp_transport="web_bridge", web_bridge_url=BRIDGE, web_bridge_secret=SECRET,
        web_bridge_line_id=LINE, web_bridge_auto_reply=True, whatsapp_audience_mode="test_allowlist",
        whatsapp_allowed_recipients=(CLIENT,), web_bridge_inbound_mode="single_chat",
    )
    base.update(overrides)
    return Settings(**base)


def _pool():
    p = create_pool(TEST_DATABASE_URL, max_size=6)
    p.open(wait=True, timeout=10)
    return p


def _count(sql, params=()):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(sql, params).fetchone()[0]


def _item(message_id: str, text: str, *, age: int = 5, sender: str = CLIENT) -> dict:
    return {
        "message_id": message_id,
        "correlation_id": "abc123def0",
        "from": sender,
        # Identidad verificada por el conector (para @c.us coincide con el propio chat).
        "identity": sender,
        "timestamp": int(time.time()) - age,
        "kind": "text",
        "text": text,
    }


def _post(client: TestClient, items: list[dict]):
    body = json.dumps({"messages": items}).encode()
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Bridge-Timestamp": stamp,
               "X-Bridge-Signature": sign(SECRET, "inbound", stamp, body)}
    return client.post("/internal/web-bridge/inbound", content=body, headers=headers)


def _worker(settings: Settings, simulator: ConnectorSimulator, owner: str):
    sender = WebBridgeSender(
        url=BRIDGE, secret=SECRET, line_id=LINE, auto_reply=True,
        allowed_recipients=(CLIENT,), audience_mode="test_allowlist", inbound_mode="single_chat",
        client=httpx.Client(transport=httpx.MockTransport(simulator.handler)),
    )
    worker, worker_pool = build_worker(
        settings, WorkerSettings(enabled=True, owner=owner, lease_seconds=120, processing_timeout_seconds=30),
        sender=sender,
    )
    worker_pool.open(wait=True, timeout=10)
    return worker, worker_pool


def test_recorrido_webjs_una_sola_respuesta_al_mismo_chat_y_sin_duplicados():
    settings = _settings()
    pool = _pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(pool, 7)))
    simulator = ConnectorSimulator()
    worker, worker_pool = _worker(settings, simulator, "webjs-e2e")
    try:
        # 1. El conector reenvía un mensaje nuevo: el backend lo acepta una vez.
        first = _post(client, [_item("MSG-E2E-1", "Hola, busco una cafetera para mi casa")])
        assert first.status_code == 200 and first.json()["accepted"] == 1

        # 2. El mismo evento repetido no genera otro registro ni otra respuesta.
        again = _post(client, [_item("MSG-E2E-1", "Hola, busco una cafetera para mi casa")])
        assert again.status_code == 200 and again.json()["accepted"] == 0 and again.json()["duplicates"] == 1
        assert _count("select count(*) from public.webhook_events") == 1

        # 3. El worker procesa el evento y entrega UNA respuesta al conector.
        assert worker.run_once() == 1
        assert len(simulator.received) == 1
        sent = simulator.received[0]
        assert sent["to"] == CLIENT, "la respuesta va al mismo chat de origen"
        assert sent["text"].strip(), "la respuesta no puede estar vacía"
        assert sent["idempotency_key"].endswith(":reply")
        assert simulator.signatures_ok == 1, "la solicitud al conector va firmada"
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'sent'") == 1

        # 4. Un nuevo ciclo no reenvía nada: la respuesta ya quedó registrada como enviada.
        worker.run_once()
        assert len(simulator.received) == 1
        assert _count("select count(*) from public.outbound_messages") == 1
    finally:
        worker.shutdown()
        worker_pool.close()
        pool.close()


def test_resultado_incierto_del_conector_no_se_reenvia_automaticamente():
    settings = _settings()
    pool = _pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(pool, 7)))
    simulator = ConnectorSimulator()
    simulator.mode = "timeout"
    worker, worker_pool = _worker(settings, simulator, "webjs-uncertain")
    try:
        assert _post(client, [_item("MSG-E2E-2", "Quiero una cotización")]).status_code == 200
        worker.run_once()
        assert len(simulator.received) == 1
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'uncertain'") == 1
        # Ciclos posteriores no repiten el envío incierto.
        worker.run_once()
        worker.run_once()
        assert len(simulator.received) == 1
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'sent'") == 0
    finally:
        worker.shutdown()
        worker_pool.close()
        pool.close()


def test_error_del_conector_queda_registrado_sin_marcar_como_enviado():
    settings = _settings()
    pool = _pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(pool, 7)))
    simulator = ConnectorSimulator()
    simulator.mode = "server_error"
    worker, worker_pool = _worker(settings, simulator, "webjs-error")
    try:
        assert _post(client, [_item("MSG-E2E-3", "Hola")]).status_code == 200
        worker.run_once()
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'sent'") == 0
        assert _count("select count(*) from public.outbound_messages where delivery_status = 'uncertain'") == 1
        worker.run_once()
        assert len(simulator.received) == 1
    finally:
        worker.shutdown()
        worker_pool.close()
        pool.close()


def test_mensajes_antiguos_y_de_terceros_fuera_de_la_lista_no_entran():
    settings = _settings()
    pool = _pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(pool, 7)))
    try:
        old = _post(client, [_item("MSG-OLD", "de hace mucho", age=900)])
        assert old.status_code == 200 and old.json()["accepted"] == 0
        stranger = _post(client, [_item("MSG-X", "hola", sender="573009998877")])
        assert stranger.status_code == 200 and stranger.json()["accepted"] == 0
        assert _count("select count(*) from public.webhook_events") == 0
    finally:
        pool.close()


def test_reinicio_del_worker_conserva_la_respuesta_sin_duplicarla():
    settings = _settings()
    pool = _pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(pool, 7)))
    simulator = ConnectorSimulator()
    worker, worker_pool = _worker(settings, simulator, "webjs-before-restart")
    try:
        assert _post(client, [_item("MSG-E2E-4", "Hola otra vez")]).status_code == 200
        assert worker.run_once() == 1
    finally:
        worker.shutdown()
        worker_pool.close()
    # Nuevo proceso sobre la misma base: no debe reenviar lo ya enviado.
    restarted, restarted_pool = _worker(settings, simulator, "webjs-after-restart")
    try:
        restarted.run_once()
        assert len(simulator.received) == 1
    finally:
        restarted.shutdown()
        restarted_pool.close()
        pool.close()
