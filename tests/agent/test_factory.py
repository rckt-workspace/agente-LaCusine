"""Reglas de selección de proveedor. Garantizan que el arranque nunca habilita llamadas facturables."""

import logging
from decimal import Decimal

import pytest

import app.integrations.anthropic_client as anthropic_module
from app.agent import factory
from app.core.config import Settings
from app.integrations.llm import FakeLLMProvider


def _settings(**overrides) -> Settings:
    base = dict(meta_app_secret=None, meta_verify_token=None)
    base.update(overrides)
    return Settings(**base)


class SdkSpy:
    """Sustituye al SDK de Anthropic: registra si se construye. Nunca llama a la red."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        SdkSpy.instances.append(kwargs)


@pytest.fixture
def sdk_spy(monkeypatch):
    SdkSpy.instances = []
    monkeypatch.setattr(anthropic_module.anthropic, "Anthropic", SdkSpy)
    return SdkSpy


def test_safe_defaults_select_no_billable_provider(sdk_spy):
    # Sin ninguna variable: dry-run y Anthropic deshabilitado.
    settings = _settings()
    assert settings.agent_dry_run is True
    assert settings.anthropic_enabled is False
    provider = factory.select_provider(settings)
    assert isinstance(provider, FakeLLMProvider)
    assert sdk_spy.instances == []


def test_dry_run_forces_simulated_provider_even_when_anthropic_is_enabled(sdk_spy):
    settings = _settings(anthropic_enabled=True, agent_dry_run=True, anthropic_api_key="k", anthropic_model="m")
    provider = factory.select_provider(settings)
    assert provider.name == "fake"
    assert sdk_spy.instances == [], "en dry-run no debe construirse el cliente real"


def test_disabled_anthropic_without_dry_run_has_no_provider_at_all(sdk_spy):
    settings = _settings(agent_dry_run=False, anthropic_enabled=False, anthropic_api_key="k", anthropic_model="m")
    assert factory.select_provider(settings) is None
    assert sdk_spy.instances == []


def test_enabled_without_key_fails_closed_and_builds_nothing(sdk_spy, caplog):
    settings = _settings(agent_dry_run=False, anthropic_enabled=True, anthropic_api_key=None, anthropic_model="m")
    with caplog.at_level(logging.ERROR, logger="lacusine.agent"):
        assert factory.select_provider(settings) is None
    assert sdk_spy.instances == []
    assert "missing_api_key" in caplog.text


def test_enabled_without_model_fails_closed(sdk_spy):
    settings = _settings(agent_dry_run=False, anthropic_enabled=True, anthropic_api_key="k", anthropic_model=None)
    assert factory.select_provider(settings) is None
    assert sdk_spy.instances == []


def test_fully_configured_real_provider_is_built_with_bounded_options(sdk_spy):
    settings = _settings(
        agent_dry_run=False, anthropic_enabled=True, anthropic_api_key="clave-ficticia",
        anthropic_model="claude-haiku-5-5", anthropic_timeout_seconds=7, anthropic_max_retries=1,
    )
    provider = factory.select_provider(settings)
    assert provider is not None and provider.name == "anthropic"
    assert sdk_spy.instances == [{"api_key": "clave-ficticia", "timeout": 7, "max_retries": 0}]


def test_api_key_never_appears_in_settings_representation():
    settings = _settings(anthropic_api_key="CLAVE-FICTICIA-NO-DEBE-VERSE")
    assert "CLAVE-FICTICIA-NO-DEBE-VERSE" not in repr(settings)
    assert "CLAVE-FICTICIA-NO-DEBE-VERSE" not in str(settings)


def test_pricing_is_never_hardcoded_in_settings_defaults():
    settings = _settings()
    assert settings.price_input_usd_per_mtok is None
    assert settings.price_output_usd_per_mtok is None
    assert settings.agent_daily_budget_usd is None
    assert settings.anthropic_model is None


@pytest.mark.parametrize("raw", ["abc", "1,5"])
def test_invalid_decimal_configuration_fails_at_startup(monkeypatch, raw):
    monkeypatch.setenv("ANTHROPIC_PRICE_INPUT_USD_PER_MTOK", raw)
    with pytest.raises(ValueError, match="decimal"):
        Settings.from_env()


def test_negative_price_is_rejected_at_startup(monkeypatch):
    monkeypatch.setenv("AGENT_DAILY_BUDGET_USD", "-1")
    with pytest.raises(ValueError, match="negativo"):
        Settings.from_env()


def test_env_defaults_are_safe(monkeypatch):
    for name in ("AGENT_DRY_RUN", "ANTHROPIC_ENABLED", "AGENT_KILL_SWITCH", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert (settings.agent_dry_run, settings.anthropic_enabled, settings.agent_kill_switch) == (True, False, False)
    assert settings.anthropic_model is None and settings.anthropic_api_key is None
