"""Demostración /demo de extremo a extremo en PostgreSQL local desechable.

Recorre los servicios reales: la ruta de la demo persiste el evento, el worker lo procesa, el despacho usa el
emisor SIMULADO (ninguna solicitud a Meta), la transferencia pausa la automatización y el aviso se entrega
por el canal simulado. Ningún proveedor real.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

LINE = "100200300400"
TOKEN = "token-demo-integracion-7d2e9c"


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


def _settings():
    return Settings(meta_app_secret=None, meta_verify_token=None, database_url=TEST_DATABASE_URL,
                    demo_enabled=True, demo_token=TOKEN, meta_phone_number_id=LINE,
                    meta_send_enabled=False, whatsapp_dry_run=True, anthropic_enabled=False, agent_dry_run=True)


def _worker_cycle(settings, owner):
    worker, pool = build_worker(settings, WorkerSettings(enabled=True, owner=owner, lease_seconds=60,
                                                         processing_timeout_seconds=30))
    pool.open(wait=True, timeout=10)
    try:
        return worker.run_once()
    finally:
        worker.shutdown()
        pool.close()


def test_demo_runs_the_real_pipeline_with_simulated_providers_only():
    settings = _settings()
    with TestClient(create_app(settings=settings), base_url="https://testserver") as client:
        assert client.post("/demo/login", json={"token": TOKEN}).status_code == 200

        # 1. El visitante envía un mensaje sintético desde un anuncio.
        sent = client.post("/demo/api/message", json={"text": "Busco una cafetera Jura para mi casa", "referral": True})
        assert sent.status_code == 200 and sent.json()["accepted"] == "new"
        contact = sent.json()["contact"]

        # 2. El worker procesa el evento y despacha la respuesta con el emisor simulado.
        assert _worker_cycle(settings, "demo-1") == 1
        state = client.get("/demo/api/state").json()
        assert state["events"] == {"processed": 1}
        assert state["leads"][0]["attribution"] == "120200000DEMO"
        assert state["replies"][0]["delivery"] == "sent"
        assert state["replies"][0]["kind"] == "reply"
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            external = conn.execute("select external_message_id from public.outbound_messages").fetchone()[0]
        assert external.startswith("wamid.SIM")  # identificador del simulador, no de Meta

        # 3. El asesor toma el control: se abre la transferencia y se encola el aviso.
        conversation = state["open_conversations"][0]["id"]
        assert client.post("/demo/api/handoff", json={"conversation_id": conversation}).status_code == 200
        assert _worker_cycle(settings, "demo-2") == 0
        state = client.get("/demo/api/state").json()
        assert state["notifications"] == {"sent": 1}
        assert state["conversations"] == {"paused_for_human": 1}

        # 4. Un mensaje nuevo del cliente no recibe respuesta automática.
        # Mismo contacto sintético: es el mismo cliente, con la conversación ya transferida.
        client.post("/demo/api/message", json={"text": "¿Me confirman el precio?", "contact": contact})
        _worker_cycle(settings, "demo-3")
        state = client.get("/demo/api/state").json()
        assert state["events"] == {"processed": 1, "ignored": 1}
        assert len(state["replies"]) == 1
        assert state["conversations"] == {"paused_for_human": 1}

        # 5. Ninguna respuesta del demo contiene el token.
        assert TOKEN not in str(state)


def test_demo_refuses_to_send_while_meta_send_is_enabled():
    settings = _settings()
    settings = Settings(**{**settings.__dict__, "meta_send_enabled": True})
    with pytest.raises(RuntimeError, match="demo_with_meta_send_refused"):
        create_app(settings=settings)
