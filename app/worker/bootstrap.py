"""Ensamblado del worker con los componentes reales. Único lugar donde se conocen todas las dependencias."""

from __future__ import annotations

import logging
import threading
import uuid

from app.agent.factory import build_commercial_agent, requires_real_provider, select_provider, worker_profile_problems
from app.core.config import Settings
from app.worker.audience import audience_problems
from app.worker.presence import TypingPresence
from app.worker.profile import describe_worker_profile, format_profile
from app.db.pool import create_pool
from app.services.budget_ledger import PostgresBudgetLedger
from app.repositories.commercial import PostgresCommercialRepository
from app.repositories.crm import (
    PostgresConversationRepository,
    PostgresConversationStateReader,
    PostgresHandoffRepository,
    PostgresLeadRepository,
    PostgresMessageRepository,
    PostgresRecentTurnsReader,
)
from app.integrations.whatsapp_client import CloudApiSender, SimulatedWhatsAppSender
from app.repositories.jobs import JobRepository
from app.repositories.outbound import OutboundRepository
from app.services.advisor_notifications import (
    AdvisorNotificationDispatcher,
    PostgresAdvisorNotificationRepository,
    SimulatedAdvisorNotifier,
)
from app.worker.dispatcher import OutboxDispatcher
from app.repositories.llm_usage import PostgresUsageStore
from app.repositories.outbox import OutboxRepository
from app.repositories.products import PostgresProductRepository
from app.worker.config import WorkerSettings
from app.worker.processor import EventProcessor
from app.worker.runner import Worker

logger = logging.getLogger("lacusine.worker")


def build_worker(settings: Settings, worker_settings: WorkerSettings, stop_event: threading.Event | None = None,
                 sender=None, advisor_notifier=None):
    """Devuelve (worker, pool). El llamador abre y cierra el pool.

    `sender` solo lo usan las pruebas (simulador). En producción se usa el adaptador configurado.
    """
    if not settings.database_url:
        raise ValueError("database_url_missing")
    if not settings.meta_phone_number_id:
        # Sin línea de negocio el worker no puede aislar conversaciones: falla cerrado al arrancar.
        raise ValueError("meta_phone_number_id_missing")
    problems = worker_profile_problems(settings, real_sender=sender is None)
    if sender is None:
        problems = problems + audience_problems(settings)
    if problems:
        # Configuración incoherente: el worker no arranca y lo dice, en lugar de operar en silencio.
        raise ValueError(problems[0])
    provider = select_provider(settings)
    if provider is None and requires_real_provider(settings):
        # Se pidió un proveedor real y no se pudo construir (clave, modelo, tarifas, presupuesto): error visible.
        raise ValueError("llm_provider_unavailable")

    pool = create_pool(settings.database_url, min_size=1, max_size=worker_settings.max_concurrency + 2)
    handoffs = PostgresHandoffRepository(pool)
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    messages = PostgresMessageRepository(pool)
    outbox = OutboxRepository(pool, retention_days=settings.webhook_event_retention_days)
    outbound = OutboundRepository(pool)
    # Con el envío deshabilitado el cliente no hace solicitudes y el dispatcher no reclama nada.
    if settings.demo_enabled and settings.meta_send_enabled:
        raise ValueError("demo_with_meta_send_refused")
    if sender is None and settings.demo_enabled:
        # Demostración: el despacho es simulado. Nunca llega a Meta aunque el worker procese mensajes.
        sender = SimulatedWhatsAppSender()
    sender = sender or CloudApiSender(
        access_token=settings.meta_access_token,
        phone_number_id=settings.meta_phone_number_id,
        graph_api_version=settings.meta_graph_api_version,
        allowed_recipients=settings.whatsapp_allowed_recipients,
        send_enabled=settings.meta_send_enabled,
        dry_run=settings.whatsapp_dry_run,
        timeout_seconds=settings.whatsapp_timeout_seconds,
        max_concurrency=settings.whatsapp_max_concurrency,
        audience_mode=settings.whatsapp_audience_mode,
    )

    conversation_reader = PostgresConversationStateReader(pool)
    agent = build_commercial_agent(
        settings,
        usage_store=PostgresUsageStore(pool),
        product_repository=PostgresProductRepository(pool),
        conversation_reader=conversation_reader,
        turns_reader=PostgresRecentTurnsReader(pool),
        handoff_repository=handoffs,
        budget_ledger=PostgresBudgetLedger(pool),
        provider=provider,
    )
    # "Escribiendo..." solo con proveedor real y envío de WhatsApp realmente habilitado.
    real_call = provider is not None and provider.name != "fake"
    typing = None
    if real_call and isinstance(sender, CloudApiSender) and sender.enabled:
        typing = TypingPresence(sender, conversation_reader.automation_state, kill_switch=settings.agent_kill_switch)
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
        typing=typing,
        contact_limits=(settings.contact_max_inbound_per_hour, settings.contact_max_inbound_per_day),
        commercial=PostgresCommercialRepository(pool),
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
    if settings.advisor_notifier != "simulated" and advisor_notifier is None:
        # Ningún canal real está implementado. Falla cerrado en lugar de caer en otro canal.
        raise ValueError("advisor_notifier_unsupported")
    notifications = AdvisorNotificationDispatcher(
        PostgresAdvisorNotificationRepository(pool, max_attempts=worker_settings.max_attempts),
        advisor_notifier or SimulatedAdvisorNotifier(),
        owner_token_factory=uuid.uuid4,
        lease_seconds=worker_settings.lease_seconds,
        batch_size=worker_settings.batch_size,
    )
    logger.info("worker_profile %s typing_indicator=%s",
                format_profile(describe_worker_profile(settings, worker_settings, provider)),
                "on" if typing is not None else "off")
    return Worker(worker_settings, jobs, processor, stop_event=stop_event, dispatcher=dispatcher,
                  notifications=notifications), pool
