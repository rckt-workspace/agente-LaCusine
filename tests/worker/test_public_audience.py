"""Audiencia de envío: test_allowlist (por defecto) y public (solo con confirmación explícita). Sin red, con transporte simulado."""

from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.integrations.whatsapp_client import (
    CloudApiSender,
    RecipientNotAllowedError,
    SendDisabledError,
    SendRequest,
)
from app.worker.audience import PUBLIC_CONFIRMATION_TOKEN, audience_problems
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings

PHONE = "123456789012"
CLIENT_A = "573001112233"
CLIENT_B = "573009998877"  # cliente no listado en ninguna lista de prueba


def _public_settings(**overrides) -> Settings:
    base = dict(
        meta_app_secret="secreto-sintetico", meta_verify_token="verify-sintetico",
        database_url="postgresql://u:p@db.example.test:5432/postgres", meta_phone_number_id=PHONE,
        meta_access_token="token-sintetico", meta_graph_api_version="v26.0",
        meta_send_enabled=True, whatsapp_dry_run=False, whatsapp_audience_mode="public",
        whatsapp_public_confirmation=PUBLIC_CONFIRMATION_TOKEN, whatsapp_allowed_recipients=(),
        llm_provider="anthropic", agent_dry_run=False, anthropic_enabled=True,
        anthropic_api_key="clave-sintetica", anthropic_model="claude-sonnet-5-5",
        anthropic_pricing_model="claude-sonnet-5-5", price_input_usd_per_mtok=Decimal("2"),
        price_output_usd_per_mtok=Decimal("10"), agent_daily_budget_usd=Decimal("0.20"),
    )
    base.update(overrides)
    return Settings(**base)


def _sender(handler, *, audience="test_allowlist", allowed=(CLIENT_A,), enabled=True):
    return CloudApiSender(
        access_token="token-sintetico", phone_number_id=PHONE, graph_api_version="v26.0",
        allowed_recipients=tuple(allowed), send_enabled=enabled, dry_run=False,
        client=httpx.Client(transport=httpx.MockTransport(handler)), audience_mode=audience,
    )


def _send(sender, to):
    return sender.send_text(SendRequest(to=to, text="hola", business_phone_id=PHONE, idempotency_key=f"k-{to}"))


# --- Activación explícita ---

def test_default_audience_is_the_test_allowlist_and_needs_no_confirmation():
    assert Settings(meta_app_secret=None, meta_verify_token=None).whatsapp_audience_mode == "test_allowlist"
    assert audience_problems(Settings(meta_app_secret=None, meta_verify_token=None)) == []


@pytest.mark.parametrize("confirmation", [None, "", "public", "true", "ABRIR_ATENCION_PUBLICA", "abrir_atencion_publica_confirmada"])
def test_public_mode_is_not_activated_by_anything_but_the_exact_confirmation(confirmation):
    settings = _public_settings(whatsapp_public_confirmation=confirmation)
    assert "public_mode_not_confirmed" in audience_problems(settings)


def test_public_mode_with_full_validated_configuration_has_no_problems():
    assert audience_problems(_public_settings()) == []


def test_unknown_audience_mode_is_refused():
    assert audience_problems(_public_settings(whatsapp_audience_mode="todos")) == ["audience_mode_unknown"]


@pytest.mark.parametrize("override, problem", [
    ({"demo_enabled": True}, "public_mode_with_demo_refused"),
    ({"meta_app_secret": None}, "public_mode_requires_app_secret"),
    ({"database_url": None}, "public_mode_requires_database"),
    ({"meta_send_enabled": False}, "public_mode_requires_send_enabled"),
    ({"whatsapp_dry_run": True}, "public_mode_requires_send_enabled"),
    ({"llm_provider": None, "anthropic_enabled": False}, "public_mode_requires_real_agent"),
    ({"agent_dry_run": True}, "public_mode_requires_real_agent"),
])
def test_public_mode_refuses_incomplete_or_non_production_configuration(override, problem):
    assert problem in audience_problems(_public_settings(**override))


# --- Comportamiento del cliente de envío ---

def test_public_sender_accepts_different_legitimate_clients_without_a_list():
    seen = []

    def handler(request):
        seen.append(request.read())
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(seen)}"}]})

    sender = _sender(handler, audience="public", allowed=())
    assert _send(sender, CLIENT_A).message_id == "wamid.1"
    assert _send(sender, CLIENT_B).message_id == "wamid.2"
    assert len(seen) == 2


def test_test_allowlist_still_refuses_a_client_that_is_not_listed():
    seen = []
    sender = _sender(lambda r: (seen.append(r), httpx.Response(200, json={}))[1], audience="test_allowlist",
                     allowed=(CLIENT_A,))
    with pytest.raises(RecipientNotAllowedError):
        _send(sender, CLIENT_B)
    assert seen == []


def test_public_sender_keeps_format_and_sending_switches():
    seen = []
    off = _sender(lambda r: (seen.append(r), httpx.Response(200))[1], audience="public", allowed=(), enabled=False)
    with pytest.raises(SendDisabledError):
        _send(off, CLIENT_A)
    assert seen == []
    with pytest.raises(Exception):
        _send(_sender(lambda r: httpx.Response(200), audience="public", allowed=()), "no-es-numero")


def test_public_typing_indicator_is_allowed_for_any_valid_client():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    _sender(handler, audience="public", allowed=()).show_typing(CLIENT_B, "wamid.INBOUND-B")
    assert len(seen) == 1


def test_unknown_audience_mode_is_refused_by_the_client():
    with pytest.raises(Exception):
        _sender(lambda r: httpx.Response(200), audience="cualquiera")


# --- El worker no arranca en modo público sin confirmación ---

def test_worker_refuses_public_mode_without_confirmation_before_opening_the_pool():
    settings = _public_settings(whatsapp_public_confirmation=None)
    with pytest.raises(ValueError, match="public_mode_not_confirmed"):
        build_worker(settings, WorkerSettings(enabled=True, owner="w"), sender=None)
