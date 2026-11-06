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


_REAL_PROVIDERS = ("anthropic", "openrouter")
_KNOWN_PROVIDERS = ("fake", "anthropic", "openrouter")
_AUTO = object()


def worker_profile_problems(settings: Settings, *, real_sender: bool = True) -> list[str]:
    """Incoherencias de configuración que impiden arrancar un worker. Función pura: no abre conexiones.

    Un worker con proveedor simulado no debe enviar nada a WhatsApp real: sus respuestas son texto fijo de prueba
    y podrían entregarse a un cliente. `real_sender=False` (pruebas con simulador inyectado) no toca Meta.
    """
    problems: list[str] = []
    explicit = settings.llm_provider
    if explicit is not None and explicit not in _KNOWN_PROVIDERS:
        problems.append("llm_provider_unknown")
    if explicit in _REAL_PROVIDERS and settings.agent_dry_run:
        # LLM_PROVIDER pide Claude/OpenRouter real pero AGENT_DRY_RUN lo sustituiría por el simulado en silencio.
        problems.append("dry_run_conflicts_with_llm_provider")
    simulated = settings.agent_dry_run or explicit == "fake"
    if real_sender and simulated and settings.meta_send_enabled and not settings.demo_enabled:
        problems.append("simulated_provider_send_refused")
    return problems


def requires_real_provider(settings: Settings) -> bool:
    """True si la configuración pide un proveedor real. En ese caso no se acepta un agente deshabilitado."""
    if settings.agent_dry_run:
        return False
    if settings.llm_provider in _REAL_PROVIDERS:
        return True
    return settings.llm_provider is None and settings.anthropic_enabled


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
        client = AnthropicAgentClient(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout_seconds=settings.anthropic_timeout_seconds,
            max_retries=settings.anthropic_max_retries,
            max_concurrency=settings.anthropic_max_concurrency,
            prompt_caching=settings.anthropic_prompt_caching,
            effort=settings.anthropic_effort,
            thinking=settings.anthropic_thinking,
        )
        # Tarifas ligadas al modelo: sin coincidencia el proveedor no se usa (ninguna llamada facturable).
        if Pricing.for_anthropic(settings, client.model) is None:
            logger.error("agent_misconfigured code=anthropic_pricing_model_mismatch")
            return None
        return client
    except LLMDisabledError as exc:
        # Configuración incompleta: el agente se deshabilita en lugar de llamar sin clave o sin modelo.
        logger.error("agent_misconfigured code=%s", exc.code)
        return None
    except ValueError as exc:
        # Esfuerzo o modo de pensamiento no admitidos: se rechaza en lugar de enviar un parámetro desconocido.
        logger.error("agent_misconfigured code=%s", exc)
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
    if provider is not None and provider.name == "anthropic":
        return Pricing.for_anthropic(settings, provider.model)
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
    provider=_AUTO,
) -> CommercialAgent:
    if provider is _AUTO:
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
