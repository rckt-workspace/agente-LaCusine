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
from app.integrations.openrouter_client import OpenRouterAgentClient
from app.services.cost_tracker import CostTracker, Pricing
from app.services.product_search import ProductSearchService

logger = logging.getLogger("lacusine.agent")


def select_provider(settings: Settings) -> LLMProvider | None:
    """Un único proveedor, elegido por LLM_PROVIDER. Nunca dos a la vez.

    AGENT_DRY_RUN=true fuerza el simulado. Sin LLM_PROVIDER se mantiene el comportamiento histórico (Anthropic
    solo si ANTHROPIC_ENABLED). Un valor desconocido no selecciona nada.
    """
    if settings.agent_dry_run:
        return FakeLLMProvider()
    choice = settings.llm_provider or ("anthropic" if settings.anthropic_enabled else None)
    if choice is None:
        return None
    if choice == "fake":
        return FakeLLMProvider()
    if choice == "anthropic":
        return _anthropic_provider(settings)
    if choice == "openrouter":
        return _openrouter_provider(settings)
    logger.error("agent_misconfigured code=llm_provider_unknown")
    return None


def _anthropic_provider(settings: Settings) -> LLMProvider | None:
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


def _openrouter_provider(settings: Settings) -> LLMProvider | None:
    """Solo si hay clave, modelo, tarifas del mismo modelo y al menos un límite de presupuesto.

    Sin límite de presupuesto no hay llamada: no se acepta un gasto ilimitado implícito.
    """
    if Pricing.from_openrouter(settings) is None:
        logger.error("agent_misconfigured code=openrouter_pricing_missing")
        return None
    if settings.agent_daily_budget_usd is None and settings.agent_monthly_budget_usd is None:
        logger.error("agent_misconfigured code=openrouter_budget_missing")
        return None
    try:
        return OpenRouterAgentClient(
            api_key=settings.openrouter_api_key,
            model=settings.openrouter_model,
            timeout_seconds=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
            max_concurrency=settings.openrouter_max_concurrency,
        )
    except (LLMDisabledError, ValueError) as exc:
        logger.error("agent_misconfigured code=%s", getattr(exc, "code", "openrouter_config_invalid"))
        return None


def pricing_for(settings: Settings, provider: LLMProvider | None) -> Pricing | None:
    """Tarifas del proveedor seleccionado. Cada proveedor usa sus propias tarifas."""
    if provider is not None and provider.name == "openrouter":
        return Pricing.from_openrouter(settings)
    return Pricing.from_settings(settings)


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
        pricing=pricing_for(settings, provider),
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
