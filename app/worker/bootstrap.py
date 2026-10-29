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
from app.repositories.jobs import JobRepository
from app.repositories.llm_usage import PostgresUsageStore
from app.repositories.outbox import OutboxRepository
from app.repositories.products import PostgresProductRepository
from app.worker.config import WorkerSettings
from app.worker.processor import EventProcessor
from app.worker.runner import Worker


def build_worker(settings: Settings, worker_settings: WorkerSettings, stop_event: threading.Event | None = None):
    """Devuelve (worker, pool). El llamador abre y cierra el pool."""
    if not settings.database_url:
        raise ValueError("database_url_missing")

    pool = create_pool(settings.database_url, min_size=1, max_size=worker_settings.max_concurrency + 2)
    handoffs = PostgresHandoffRepository(pool)
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    messages = PostgresMessageRepository(pool)
    outbox = OutboxRepository(pool, retention_days=settings.webhook_event_retention_days)

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
    )
    jobs = JobRepository(pool, owner=worker_settings.owner, lease_seconds=worker_settings.lease_seconds)
    return Worker(worker_settings, jobs, processor, stop_event=stop_event), pool
