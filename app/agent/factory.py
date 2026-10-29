"""Ensamblado del agente comercial. Único punto donde se elige el proveedor de lenguaje.

Reglas de selección:
  * AGENT_DRY_RUN=true (por defecto): proveedor simulado, aunque Anthropic esté habilitado. Nunca cobra.
  * AGENT_DRY_RUN=false y ANTHROPIC_ENABLED=true: cliente real de Anthropic (requiere clave y modelo).
  * Cualquier otro caso: sin proveedor; el agente responde DISABLED y no llama a nada.
"""

from __future__ import annotations

import logging

from app.agent.conversation_context import ConversationContextService
from app.agent.knowledge import KnowledgeService
from app.agent.orchestrator import AgentSettings, CommercialAgent
from app.core.config import Settings
from app.integrations.anthropic_client import AnthropicAgentClient
from app.integrations.llm import FakeLLMProvider, LLMDisabledError, LLMProvider
from app.services.cost_tracker import CostTracker, Pricing
from app.services.product_search import ProductSearchService

logger = logging.getLogger("lacusine.agent")


def select_provider(settings: Settings) -> LLMProvider | None:
    if settings.agent_dry_run:
        return FakeLLMProvider()
    if not settings.anthropic_enabled:
        return None
    try:
        return AnthropicAgentClient(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout_seconds=settings.anthropic_timeout_seconds,
            max_retries=settings.anthropic_max_retries,
            max_concurrency=settings.anthropic_max_concurrency,
            prompt_caching=settings.anthropic_prompt_caching,
        )
    except LLMDisabledError as exc:
        # Configuración incompleta: el agente se deshabilita en lugar de llamar sin clave o sin modelo.
        logger.error("agent_misconfigured code=%s", exc.code)
        return None


def build_commercial_agent(
    settings: Settings,
    *,
    usage_store,
    product_repository,
    conversation_reader,
    turns_reader,
    handoff_repository,
    knowledge: KnowledgeService | None = None,
    budget_ledger=None,
) -> CommercialAgent:
    provider = select_provider(settings)
    cost = CostTracker(
        store=usage_store,
        pricing=Pricing.from_settings(settings),
        daily_budget_usd=settings.agent_daily_budget_usd,
        monthly_budget_usd=settings.agent_monthly_budget_usd,
        interaction_limit_usd=settings.agent_interaction_limit_usd,
        ledger=budget_ledger,
    )
    return CommercialAgent(
        provider=provider,
        cost=cost,
        search=ProductSearchService(product_repository),
        context=ConversationContextService(turns_reader, max_chars=settings.agent_max_context_chars),
        conversations=conversation_reader,
        handoffs=handoff_repository,
        products=product_repository,
        knowledge=knowledge or KnowledgeService(),
        settings=AgentSettings(
            kill_switch=settings.agent_kill_switch,
            max_output_tokens=settings.anthropic_max_output_tokens,
            max_input_chars=settings.agent_max_input_chars,
            dry_run=settings.agent_dry_run,
        ),
    )
