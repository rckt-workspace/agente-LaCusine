"""Ensamblado del worker con los componentes reales. Único lugar donde se conocen todas las dependencias."""

from __future__ import annotations

import threading

from app.agent.factory import build_commercial_agent
from app.core.config import Settings
from app.db.pool import create_pool
from app.services.budget_ledger import PostgresBudgetLedger
from app.repositories.crm import (
    PostgresConversationRepository,
    PostgresConversationStateReader,
    PostgresHandoffRepository,
    PostgresLeadRepository,
    PostgresMessageRepository,
    PostgresRecentTurnsReader,
)
from app.integrations.whatsapp_client import CloudApiSender
from app.repositories.jobs import JobRepository
from app.repositories.outbound import OutboundRepository
from app.worker.dispatcher import OutboxDispatcher
from app.repositories.llm_usage import PostgresUsageStore
from app.repositories.outbox import OutboxRepository
from app.repositories.products import PostgresProductRepository
from app.worker.config import WorkerSettings
from app.worker.processor import EventProcessor
from app.worker.runner import Worker


def build_worker(settings: Settings, worker_settings: WorkerSettings, stop_event: threading.Event | None = None,
                 sender=None):
    """Devuelve (worker, pool). El llamador abre y cierra el pool.

    `sender` solo lo usan las pruebas (simulador). En producción se usa el adaptador configurado.
    """
    if not settings.database_url:
        raise ValueError("database_url_missing")
    if not settings.meta_phone_number_id:
        # Sin línea de negocio el worker no puede aislar conversaciones: falla cerrado al arrancar.
        raise ValueError("meta_phone_number_id_missing")

    pool = create_pool(settings.database_url, min_size=1, max_size=worker_settings.max_concurrency + 2)
    handoffs = PostgresHandoffRepository(pool)
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    messages = PostgresMessageRepository(pool)
    outbox = OutboxRepository(pool, retention_days=settings.webhook_event_retention_days)
    outbound = OutboundRepository(pool)
    # Con el envío deshabilitado el cliente no hace solicitudes y el dispatcher no reclama nada.
    sender = sender or CloudApiSender(
        access_token=settings.meta_access_token,
        phone_number_id=settings.meta_phone_number_id,
        graph_api_version=settings.meta_graph_api_version,
        allowed_recipients=settings.whatsapp_allowed_recipients,
        send_enabled=settings.meta_send_enabled,
        dry_run=settings.whatsapp_dry_run,
        timeout_seconds=settings.whatsapp_timeout_seconds,
        max_concurrency=settings.whatsapp_max_concurrency,
    )

    agent = build_commercial_agent(
        settings,
        usage_store=PostgresUsageStore(pool),
        product_repository=PostgresProductRepository(pool),
        conversation_reader=PostgresConversationStateReader(pool),
        turns_reader=PostgresRecentTurnsReader(pool),
        handoff_repository=handoffs,
        budget_ledger=PostgresBudgetLedger(pool),
    )
    processor = EventProcessor(
        agent=agent,
        leads=leads,
        conversations=conversations,
        messages=messages,
        handoffs=handoffs,
        outbox=outbox,
        retention_days=settings.webhook_event_retention_days,
        expected_business_phone_id=settings.meta_phone_number_id,
        outbound=outbound,
    )
    jobs = JobRepository(pool, owner=worker_settings.owner, lease_seconds=worker_settings.lease_seconds)
    dispatcher = OutboxDispatcher(
        repository=outbound,
        sender=sender,
        business_phone_id=settings.meta_phone_number_id,
        owner=worker_settings.owner,
        lease_seconds=worker_settings.lease_seconds,
        max_attempts=worker_settings.max_attempts,
        backoff_base_seconds=worker_settings.backoff_base_seconds,
        backoff_max_seconds=worker_settings.backoff_max_seconds,
        batch_size=worker_settings.batch_size,
    )
    return Worker(worker_settings, jobs, processor, stop_event=stop_event, dispatcher=dispatcher), pool
