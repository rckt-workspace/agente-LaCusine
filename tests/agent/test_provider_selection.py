"""Selección de proveedor de IA y protección de facturación. Sin red: el guardián de red del paquete está activo."""

from decimal import Decimal

import pytest

import app.integrations.anthropic_client as anthropic_module
from app.agent import factory
from app.agent.orchestrator import OutcomeStatus
from app.core.config import Settings
from app.integrations.llm import FakeLLMProvider, LLMAuthError, LLMResponse, LLMTimeoutError, LLMUsage
from app.integrations.openrouter_client import OpenRouterAgentClient
from app.services.cost_tracker import Pricing
from tests.agent.test_orchestrator import Harness

OR_PRICING = dict(
    openrouter_price_input_usd_per_mtok=Decimal("1.00"),
    openrouter_price_output_usd_per_mtok=Decimal("2.00"),
    openrouter_pricing_model="openrouter/test-model",
    openrouter_pricing_reference="tarifas de prueba",
)
OR_READY = dict(
    agent_dry_run=False,
    llm_provider="openrouter",
    openrouter_api_key="sk-or-FICTICIA-PARA-SELECCION",
    openrouter_model="openrouter/test-model",
    agent_daily_budget_usd=Decimal("5"),
    **OR_PRICING,
)


def _settings(**overrides) -> Settings:
    base = dict(meta_app_secret=None, meta_verify_token=None)
    base.update(overrides)
    return Settings(**base)


class SdkSpy:
    instances: list = []

    def __init__(self, *args, **kwargs):
        SdkSpy.instances.append(kwargs)


@pytest.fixture
def sdk_spy(monkeypatch):
    SdkSpy.instances = []
    monkeypatch.setattr(anthropic_module.anthropic, "Anthropic", SdkSpy)
    return SdkSpy


# --- Selección ---

def test_dry_run_forces_the_simulated_provider_even_with_openrouter_fully_configured():
    provider = factory.select_provider(_settings(**{**OR_READY, "agent_dry_run": True}))
    assert isinstance(provider, FakeLLMProvider)


def test_openrouter_is_selected_only_when_every_requirement_is_met():
    provider = factory.select_provider(_settings(**OR_READY))
    assert isinstance(provider, OpenRouterAgentClient)
    assert provider.name == "openrouter"


@pytest.mark.parametrize("missing", [
    "openrouter_api_key", "openrouter_model", "openrouter_pricing_model",
    "openrouter_price_input_usd_per_mtok", "openrouter_price_output_usd_per_mtok",
])
def test_missing_requirement_fails_closed(missing):
    overrides = {**OR_READY, missing: None}
    assert factory.select_provider(_settings(**overrides)) is None


def test_pricing_bound_to_another_model_is_refused():
    assert factory.select_provider(_settings(**{**OR_READY, "openrouter_pricing_model": "otro/modelo"})) is None


def test_negative_price_is_refused():
    assert factory.select_provider(_settings(**{**OR_READY, "openrouter_price_input_usd_per_mtok": Decimal("-1")})) is None


def test_no_budget_limit_means_no_openrouter_call_ever():
    settings = _settings(**{**OR_READY, "agent_daily_budget_usd": None, "agent_monthly_budget_usd": None})
    assert factory.select_provider(settings) is None


def test_unknown_provider_selects_nothing(caplog):
    assert factory.select_provider(_settings(**{**OR_READY, "llm_provider": "otro"})) is None
    assert "llm_provider_unknown" in caplog.text


def test_explicit_openrouter_never_builds_the_anthropic_client(sdk_spy):
    factory.select_provider(_settings(**OR_READY, anthropic_enabled=True, anthropic_api_key="k",
                                      anthropic_model="m"))
    assert sdk_spy.instances == []


def test_explicit_anthropic_builds_only_anthropic_even_with_openrouter_ready(sdk_spy):
    settings = _settings(**{**OR_READY, "llm_provider": "anthropic", "anthropic_enabled": True,
                            "anthropic_api_key": "k", "anthropic_model": "m"})
    provider = factory.select_provider(settings)
    assert provider.name == "anthropic"
    assert len(sdk_spy.instances) == 1  # solo el cliente de Anthropic; ningún cliente de OpenRouter


def test_explicit_anthropic_with_anthropic_disabled_selects_nothing_even_if_openrouter_is_ready():
    settings = _settings(**{**OR_READY, "llm_provider": "anthropic", "anthropic_enabled": False})
    assert factory.select_provider(settings) is None


def test_historical_default_is_unchanged_without_llm_provider(sdk_spy):
    assert factory.select_provider(_settings()) is not None  # dry-run por defecto
    settings = _settings(agent_dry_run=False, anthropic_enabled=False, openrouter_api_key="k",
                         openrouter_model="m", **OR_PRICING)
    assert factory.select_provider(settings) is None  # sin LLM_PROVIDER no hay OpenRouter


def test_fake_provider_is_explicit_and_never_bills():
    provider = factory.select_provider(_settings(agent_dry_run=False, llm_provider="fake"))
    assert isinstance(provider, FakeLLMProvider)


# --- Tarifas por proveedor ---

def test_each_provider_uses_its_own_pricing():
    settings = _settings(**{**OR_READY, "price_input_usd_per_mtok": Decimal("9"),
                            "price_output_usd_per_mtok": Decimal("9")})
    openrouter = factory.select_provider(settings)
    pricing = factory.pricing_for(settings, openrouter)
    assert pricing.input_per_mtok == Decimal("1.00")
    assert pricing.reference == "tarifas de prueba"
    anthropic_pricing = factory.pricing_for(settings, None)
    assert anthropic_pricing.input_per_mtok == Decimal("9")


def test_openrouter_pricing_is_absent_without_model_binding():
    settings = _settings(**{**OR_READY, "openrouter_pricing_model": None})
    assert Pricing.from_openrouter(settings) is None


# --- Facturación y agente (con un proveedor simulado llamado openrouter) ---

class OpenRouterDouble:
    """Proveedor simulado que se comporta como el adaptador real frente a la facturación."""

    name = "openrouter"
    model = "openrouter/test-model"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text='{"reply_text": "Gracias por escribirnos.", "intent": "other", "lead_stage": "NEW", '
             '"recommended_product_ids": [], "requires_human": false, "handoff_reason": null}',
        input_tokens=500, output_tokens=120, truncated=False):
    return LLMResponse(text=text, model="openrouter/test-model", provider="openrouter",
                       usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens), truncated=truncated)


def test_successful_openrouter_call_records_provider_model_and_cost():
    provider = OpenRouterDouble([_ok()])
    harness = Harness(provider=provider, pricing=True, daily=Decimal("5"))
    outcome = harness.ask("Busco una cafetera para mi casa", call_id="wamid.OK1")
    assert outcome.status is OutcomeStatus.REPLIED
    event = harness.usage.successes["wamid.OK1"]
    assert event.provider == "openrouter"
    assert event.model == "openrouter/test-model"
    assert event.status == "succeeded"


def test_timeout_is_uncertain_and_the_reservation_stays_counted_without_a_second_call():
    provider = OpenRouterDouble([LLMTimeoutError()])
    harness = Harness(provider=provider, pricing=True, daily=Decimal("5"))
    outcome = harness.ask("Busco una cafetera", call_id="wamid.T1")
    assert outcome.status is OutcomeStatus.PROVIDER_ERROR
    assert outcome.billed_state == "uncertain"
    assert harness.cost.reservation_state("wamid.T1") == "uncertain"

    again = harness.ask("Busco una cafetera", call_id="wamid.T1")
    assert again.status is OutcomeStatus.REQUIRES_RECONCILIATION
    assert provider.calls == 1  # doble facturación evitada: nunca se llama otra vez sola


def test_authentication_failure_is_not_billed_and_releases_the_reservation():
    provider = OpenRouterDouble([LLMAuthError()])
    harness = Harness(provider=provider, pricing=True, daily=Decimal("5"))
    outcome = harness.ask("Busco una cafetera", call_id="wamid.A1")
    assert outcome.status is OutcomeStatus.PROVIDER_ERROR
    assert outcome.billed_state == "not_billed"
    assert harness.cost.reservation_state("wamid.A1") == "released"


def test_budget_limit_blocks_the_call_before_the_provider_is_used():
    provider = OpenRouterDouble([_ok()])
    harness = Harness(provider=provider, pricing=True, daily=Decimal("0.000001"))
    outcome = harness.ask("Busco una cafetera", call_id="wamid.B1")
    assert outcome.status is OutcomeStatus.BUDGET_BLOCKED
    assert provider.calls == 0


def test_invalid_json_is_rejected_by_the_commercial_validation_and_not_retried():
    provider = OpenRouterDouble([_ok(text="esto no es json")])
    harness = Harness(provider=provider, pricing=True, daily=Decimal("5"))
    outcome = harness.ask("Busco una cafetera", call_id="wamid.J1")
    assert outcome.status is OutcomeStatus.INVALID_OUTPUT
    assert provider.calls == 1
    assert harness.handoffs.opened, "una respuesta inválida se transfiere a una persona"


def test_commercial_agent_keeps_working_with_openrouter_and_human_transfer_still_blocks():
    provider = OpenRouterDouble([_ok()])
    harness = Harness(provider=provider, pricing=True, daily=Decimal("5"))
    harness.conversations.states[harness.conversation_id] = "paused_for_human"
    outcome = harness.ask("Busco una cafetera", call_id="wamid.H1")
    assert outcome.status is OutcomeStatus.SUPPRESSED_HUMAN_CONTROL
    assert provider.calls == 0
