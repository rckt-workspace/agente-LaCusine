"""Costos y modo de pensamiento de Anthropic. Proveedor simulado y SDK falso: sin red, sin cobro, sin claves reales."""

import logging
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.integrations.anthropic_client as anthropic_module
from app.agent import factory
from app.agent.orchestrator import OutcomeStatus
from app.core.config import Settings
from app.integrations.anthropic_client import AnthropicAgentClient
from app.integrations.llm import ChatMessage, FakeLLMProvider, LLMRequest, LLMUsage
from app.services.cost_tracker import Pricing, estimate_cost
from tests.agent.test_orchestrator import Harness, reply

SONNET = "claude-sonnet-5-5"
OFFICIAL = dict(
    price_input_usd_per_mtok=Decimal("2"), price_output_usd_per_mtok=Decimal("10"),
    price_cache_write_usd_per_mtok=Decimal("2.50"), price_cache_read_usd_per_mtok=Decimal("0.10"),
)


def _settings(**overrides) -> Settings:
    base = dict(meta_app_secret=None, meta_verify_token=None, anthropic_enabled=True,
                anthropic_api_key="clave-ficticia", anthropic_model=SONNET, anthropic_pricing_model=SONNET,
                agent_dry_run=False, llm_provider="anthropic", **OFFICIAL)
    base.update(overrides)
    return Settings(**base)


class FakeSdk:
    def __init__(self, response=None):
        self.calls = []
        self._response = response
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _message(blocks, stop="end_turn", usage=(4200, 350)):
    return SimpleNamespace(
        content=blocks, stop_reason=stop,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1],
                              cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _thinking_block(text):
    return SimpleNamespace(type="thinking", thinking=text)


# --- Tarifas ligadas a un modelo ---

def test_prices_apply_only_to_the_model_they_were_written_for():
    assert Pricing.for_anthropic(_settings(), SONNET) is not None
    assert Pricing.for_anthropic(_settings(), "claude-haiku-5-5") is None


def test_missing_pricing_model_means_no_pricing_at_all():
    assert Pricing.for_anthropic(_settings(anthropic_pricing_model=None), SONNET) is None


def test_missing_cache_prices_default_to_the_input_price_which_overestimates():
    settings = _settings(price_cache_write_usd_per_mtok=None, price_cache_read_usd_per_mtok=None)
    pricing = Pricing.for_anthropic(settings, SONNET)
    assert pricing.cache_write_per_mtok == Decimal("2") and pricing.cache_read_per_mtok == Decimal("2")


def test_provider_with_mismatched_pricing_is_not_built(monkeypatch):
    monkeypatch.setattr(anthropic_module.anthropic, "Anthropic", lambda **kw: FakeSdk())
    settings = _settings(anthropic_pricing_model="claude-haiku-5-5")
    assert factory.select_provider(settings) is None


def test_worker_refuses_to_start_when_anthropic_pricing_does_not_match(monkeypatch):
    from app.worker.bootstrap import build_worker
    from app.worker.config import WorkerSettings

    monkeypatch.setattr(anthropic_module.anthropic, "Anthropic", lambda **kw: FakeSdk())
    settings = _settings(anthropic_pricing_model="claude-haiku-5-5", database_url="postgresql://u:p@h/db",
                         meta_phone_number_id="123456789012")
    with pytest.raises(ValueError, match="llm_provider_unavailable"):
        build_worker(settings, WorkerSettings(enabled=True, owner="w"), sender=None)


def test_pricing_reference_names_the_model():
    pricing = Pricing.for_anthropic(_settings(pricing_reference=None), SONNET)
    assert SONNET in pricing.reference


# --- Costos con tarifas oficiales de Sonnet 5.5 (USD por millón de tokens) ---

@pytest.mark.parametrize("input_tokens, output_tokens, expected", [
    (4200, 350, Decimal("0.011900")),
    (4200, 600, Decimal("0.014400")),
    (4200, 1100, Decimal("0.019400")),
    (4358, 1100, Decimal("0.019716")),  # la última llamada real registrada
])
def test_sonnet_cost_scenarios_match_the_official_prices(input_tokens, output_tokens, expected):
    pricing = Pricing.for_anthropic(_settings(), SONNET)
    assert estimate_cost(pricing, LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens)) == expected


def test_cached_system_prompt_is_billed_at_the_cache_read_price():
    pricing = Pricing.for_anthropic(_settings(), SONNET)
    uncached = estimate_cost(pricing, LLMUsage(input_tokens=2300, output_tokens=0))
    cached = estimate_cost(pricing, LLMUsage(input_tokens=0, output_tokens=0, cache_read_tokens=2300))
    assert uncached == Decimal("0.004600") and cached == Decimal("0.000230")


# --- Parámetros de esfuerzo y pensamiento: solo si se configuran ---

def _client(sdk, **kwargs):
    return AnthropicAgentClient(api_key="clave-ficticia", model=SONNET, sdk_client=sdk, **kwargs)


def _request():
    return LLMRequest(call_id="wamid.T1", system="sistema", messages=(), max_output_tokens=550)


def test_unset_options_send_exactly_the_same_request_as_before():
    sdk = FakeSdk(_message([_text_block("{}")]))
    _client(sdk).complete(_request())
    assert "thinking" not in sdk.calls[0] and "output_config" not in sdk.calls[0]


def test_adaptive_thinking_and_low_effort_are_sent_when_configured():
    sdk = FakeSdk(_message([_text_block("{}")]))
    _client(sdk, thinking="adaptive", effort="low").complete(_request())
    assert sdk.calls[0]["thinking"] == {"type": "adaptive"}
    assert sdk.calls[0]["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_sonnet_refuses_enabled_and_disabled_thinking(thinking):
    with pytest.raises(ValueError, match="thinking_type_unsupported_for_model"):
        _client(FakeSdk(), thinking=thinking)


def test_between_tools_is_sent_for_sonnet_with_low_effort_and_no_extra_fields():
    sdk = FakeSdk(_message([_text_block("{}")]))
    _client(sdk, thinking="between_tools", effort="low").complete(_request())
    call = sdk.calls[0]
    assert call["thinking"] == {"type": "between_tools"}
    assert call["output_config"] == {"effort": "low"}
    assert set(call["thinking"]) == {"type"}  # sin display, budget_tokens ni block_binding


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_between_tools_accepts_medium_and_high_effort(effort):
    _client(FakeSdk(), thinking="between_tools", effort=effort)


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_between_tools_refuses_xhigh_and_max_effort_locally(effort):
    with pytest.raises(ValueError, match="effort_incompatible_with_between_tools"):
        _client(FakeSdk(), thinking="between_tools", effort=effort)


def test_between_tools_is_never_extended_to_other_models():
    with pytest.raises(ValueError, match="thinking_type_unsupported_for_model"):
        AnthropicAgentClient(api_key="k", model="claude-haiku-5-5", sdk_client=FakeSdk(),
                             thinking="between_tools")


def test_unknown_effort_level_is_refused():
    with pytest.raises(ValueError, match="effort_unsupported"):
        _client(FakeSdk(), effort="ultra")


# --- Bloques de respuesta: el texto nunca incluye el pensamiento ---

def test_thinking_blocks_are_counted_but_never_mixed_into_the_reply_text():
    sdk = FakeSdk(_message([_thinking_block("razonamiento interno"), _text_block('{"reply_text": "hola"}')]))
    result = _client(sdk).complete(_request())
    assert result.block_types == ("thinking", "text")
    assert result.text == '{"reply_text": "hola"}'
    assert "razonamiento interno" not in result.text


def test_max_tokens_stop_is_reported_as_truncated():
    sdk = FakeSdk(_message([_text_block('{"reply_text": "parcial')], stop="max_tokens", usage=(4200, 550)))
    assert _client(sdk).complete(_request()).truncated is True


# --- Presupuesto con tarifas de escala real ---

@dataclass
class AnthropicDouble(FakeLLMProvider):
    name: str = "anthropic"
    model: str = SONNET


def test_interaction_limit_blocks_a_sonnet_scale_call_before_the_provider_is_used():
    provider = AnthropicDouble(script=[reply("No debería enviarse.")])
    harness = Harness(provider=provider, interaction=Decimal("0.002"))
    outcome = harness.ask("cafetera Jura")
    assert outcome.status is OutcomeStatus.BUDGET_BLOCKED
    assert provider.calls == []


def test_output_stats_log_the_block_types_without_content(caplog):
    @dataclass
    class WithBlocks(AnthropicDouble):
        def complete(self, request):
            base = super().complete(request)
            return type(base)(text=base.text, model=base.model, provider=base.provider, usage=base.usage,
                              block_types=("thinking", "text"))

    harness = Harness(provider=WithBlocks(script=[reply("Texto privado del cliente Ana Pérez")]))
    with caplog.at_level(logging.INFO, logger="lacusine.agent"):
        harness.ask("cafetera Jura")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "blocks=text:1,thinking:1" in text
    assert "Ana Pérez" not in text


# --- Estimación conservadora de la reserva y ausencia de contenido de pensamiento en logs ---

def test_reserve_estimate_covers_the_observed_input_of_4358_tokens():
    from app.agent.orchestrator import input_tokens_reserve_estimate
    # ~9.500 caracteres de entrada (sistema más mensaje, candidatos e historial): la llamada real tuvo 4.358 tokens.
    request = LLMRequest(call_id="c", system="s" * 7952,
                         messages=(ChatMessage(role="user", content="u" * 1550),), max_output_tokens=700)
    assert input_tokens_reserve_estimate(request) >= 4358


def test_thinking_text_never_reaches_the_logs(caplog):
    sdk = FakeSdk(_message([_thinking_block("razonamiento privado del modelo"), _text_block('{"reply_text":"ok"}')]))
    with caplog.at_level(logging.DEBUG):
        _client(sdk, thinking="between_tools", effort="low").complete(_request())
    assert "razonamiento privado" not in " ".join(r.getMessage() for r in caplog.records)
