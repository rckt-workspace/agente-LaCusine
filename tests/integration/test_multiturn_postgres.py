"""Conversación multiturno real contra PostgreSQL aislada: entrada por el puente, worker, outbox y envío simulado.

Cinco turnos del mismo contacto, otro contacto intercalado, duplicado, envío incierto (no se reenvía y no bloquea),
reinicio del worker (no reprocesa) y cero envíos a Meta. El conector es un simulador firmado; no hay WhatsApp real.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db.pool import create_pool
from app.main import create_app
from app.repositories.webhook_events import PostgresWebhookEventRepository
from tests.integration import test_webjs_recorrido_postgres as base
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

OTHER = "573009998877"


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


def _settings():
    return base._settings(whatsapp_allowed_recipients=(base.CLIENT, OTHER))


def _worker_for(settings, simulator, owner):
    from app.integrations.web_bridge import WebBridgeSender
    from app.worker.bootstrap import build_worker
    from app.worker.config import WorkerSettings
    import httpx

    sender = WebBridgeSender(
        url=base.BRIDGE, secret=base.SECRET, line_id=base.LINE, auto_reply=True,
        allowed_recipients=(base.CLIENT, OTHER), audience_mode="test_allowlist", inbound_mode="single_chat",
        client=httpx.Client(transport=httpx.MockTransport(simulator.handler)),
    )
    worker, worker_pool = build_worker(
        settings, WorkerSettings(enabled=True, owner=owner, lease_seconds=120, processing_timeout_seconds=30),
        sender=sender,
    )
    worker_pool.open(wait=True, timeout=10)
    return worker, worker_pool


def _states():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return dict(conn.execute("select delivery_status, count(*) from public.outbound_messages group by 1").fetchall())


def test_cinco_turnos_dos_contactos_un_incierto_y_un_reinicio_sin_duplicados():
    settings = _settings()
    pool = base._pool()
    client = TestClient(create_app(settings=settings, webhook_repository=PostgresWebhookEventRepository(pool, 7)))
    simulator = base.ConnectorSimulator()
    worker, worker_pool = _worker_for(settings, simulator, "multiturn-a")
    try:
        # Turno 1 del cliente principal y, en medio, un mensaje de otro contacto.
        assert base._post(client, [base._item("T1-A", "Hola, ¿qué cafeteras Jura tienen?")]).json()["accepted"] == 1
        assert worker.run_once() == 1
        assert base._post(client, [base._item("T2-A", "Quiero comprar la Jura C3")]).json()["accepted"] == 1
        assert base._post(client, [base._item("T1-B", "Hola, ¿tienen café en grano?", sender=OTHER)]).json()["accepted"] == 1
        for _ in range(3):
            worker.run_once()
        assert len(simulator.received) == 3, simulator.received
        assert sorted(m["to"] for m in simulator.received) == sorted([base.CLIENT, base.CLIENT, OTHER])

        # Duplicado exacto del turno 2: no genera nueva respuesta.
        assert base._post(client, [base._item("T2-A", "Quiero comprar la Jura C3")]).json()["duplicates"] == 1
        worker.run_once()
        assert len(simulator.received) == 3

        # Turno 3: el conector falla al responder (500). El envío queda INCIERTO y nunca se reenvía.
        simulator.mode = "server_error"
        assert base._post(client, [base._item("T3-A", "¿Cuánto cuesta?")]).json()["accepted"] == 1
        worker.run_once()
        assert _states().get("uncertain") == 1
        assert len(simulator.received) == 4, "un intento: el incierto no se reintenta"
        simulator.mode = "ok"
        worker.run_once()
        worker.run_once()
        assert len(simulator.received) == 4, "el incierto sigue sin reenviarse aunque el conector vuelva"

        # Turnos 4 y 5 avanzan a pesar del incierto anterior.
        assert base._post(client, [base._item("T4-A", "¿Cómo puedo pagar?")]).json()["accepted"] == 1
        worker.run_once()
        assert len(simulator.received) == 5

        # Reinicio del worker: no reprocesa lo ya respondido.
        worker.shutdown()
        worker_pool.close()
        worker, worker_pool = _worker_for(settings, simulator, "multiturn-b")
        worker.run_once()
        assert len(simulator.received) == 5, "tras reiniciar no se reenvía nada ya respondido"

        assert base._post(client, [base._item("T5-A", "Gracias, eso es todo")]).json()["accepted"] == 1
        worker.run_once()
        assert len(simulator.received) == 6

        # Resultados: 5 respuestas enviadas, 1 incierta, nada pendiente; claves únicas; todo firmado.
        states = _states()
        assert states.get("sent") == 5, states
        assert states.get("uncertain") == 1, states
        assert states.get("pending", 0) == 0, states
        keys = [m["idempotency_key"] for m in simulator.received]
        assert len(keys) == len(set(keys)), "ninguna clave de envío se repite"
        contacts = [m["to"] for m in simulator.received]
        assert contacts.count(base.CLIENT) == 5 and contacts.count(OTHER) == 1
        assert simulator.signatures_ok == len(simulator.received), "todas las solicitudes van firmadas"
    finally:
        worker.shutdown()
        worker_pool.close()
        pool.close()
