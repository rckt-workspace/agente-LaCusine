"""Piloto: perfil de arranque del worker, guardias de configuración, indicador "escribiendo..." y trazabilidad.

Sin red ni base de datos: el transporte HTTP es simulado y los errores de arranque se comprueban antes de abrir el pool.
"""

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.agent.factory import requires_real_provider, worker_profile_problems
from app.agent.orchestrator import AgentOutcome, OutcomeStatus
from app.agent.schemas import LeadStage
from app.core.config import Settings
from app.integrations.llm import FakeLLMProvider
from app.integrations.whatsapp_client import (
    CloudApiSender,
    MetaRejectedError,
    PayloadRejectedError,
    RecipientNotAllowedError,
    SendDisabledError,
    WhatsAppError,
)
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from app.worker.presence import TypingPresence
from app.worker.processor import EventProcessor
from app.worker.profile import describe_worker_profile, format_profile
from tests.worker.test_processor import (
    ALWAYS_RENEW,
    FakeAgent,
    FakeConversations,
    FakeHandoffs,
    FakeLeads,
    FakeMessages,
    FakeOutbox,
    _claim,
)

SECRET_KEY = "marcador-sintetico-anthropic-no-real"
DB_PASSWORD = "marcador-sintetico-db-no-real"
TOKEN = "marcador-sintetico-meta-no-real"
DB_URL = f"postgresql://lacusine_app:{DB_PASSWORD}@db.example.test:5432/postgres?sslmode=require"
PHONE_ID = "123456789012"
ALLOWED = "573001112233"


def _settings(**overrides) -> Settings:
    base = dict(
        meta_app_secret=None, meta_verify_token=None, database_url=DB_URL, meta_phone_number_id=PHONE_ID,
        meta_access_token=TOKEN, meta_graph_api_version="v26.0", llm_provider="anthropic", agent_dry_run=False,
        anthropic_enabled=True, anthropic_api_key=SECRET_KEY, anthropic_model="claude-sonnet-5-5",
        price_input_usd_per_mtok=Decimal("1"), price_output_usd_per_mtok=Decimal("1"),
        agent_daily_budget_usd=Decimal("1"), whatsapp_allowed_recipients=(ALLOWED,),
    )
    base.update(overrides)
    return Settings(**base)


# --- Guardias de configuración ---

def test_dry_run_cannot_silently_replace_an_explicit_real_provider():
    settings = _settings(agent_dry_run=True)
    assert worker_profile_problems(settings) == ["dry_run_conflicts_with_llm_provider"]
    with pytest.raises(ValueError, match="dry_run_conflicts_with_llm_provider"):
        build_worker(settings, WorkerSettings(enabled=True, owner="w"), sender=None)


def test_simulated_provider_is_refused_when_meta_sending_is_enabled():
    settings = _settings(llm_provider=None, agent_dry_run=True, meta_send_enabled=True, whatsapp_dry_run=False)
    assert worker_profile_problems(settings) == ["simulated_provider_send_refused"]


def test_injected_simulator_does_not_reach_meta_so_the_send_guard_does_not_apply():
    settings = _settings(llm_provider=None, agent_dry_run=True, meta_send_enabled=True, whatsapp_dry_run=False)
    assert worker_profile_problems(settings, real_sender=False) == []


def test_simulated_provider_without_sending_is_allowed():
    assert worker_profile_problems(_settings(llm_provider=None, agent_dry_run=True, meta_send_enabled=False)) == []


def test_unknown_provider_is_refused_explicitly():
    assert worker_profile_problems(_settings(llm_provider="gpt-x")) == ["llm_provider_unknown"]


def test_explicit_real_provider_without_key_stops_the_worker_before_opening_the_pool():
    settings = _settings(anthropic_api_key=None)
    with pytest.raises(ValueError, match="llm_provider_unavailable"):
        build_worker(settings, WorkerSettings(enabled=True, owner="w"), sender=None)


def test_requires_real_provider_only_when_a_real_call_is_expected():
    assert requires_real_provider(_settings()) is True
    assert requires_real_provider(_settings(agent_dry_run=True)) is False
    assert requires_real_provider(_settings(llm_provider=None, anthropic_enabled=False)) is False
    assert requires_real_provider(_settings(llm_provider=None, anthropic_enabled=True)) is True


# --- Perfil de arranque sin secretos ---

def test_startup_profile_contains_effective_fields_and_no_secret_values():
    fields = describe_worker_profile(_settings(), WorkerSettings(owner="worker-pilot-1"),
                                     SimpleNamespace(name="anthropic", model="claude-sonnet-5-5"))
    line = format_profile(fields)
    for needle in ("worker_id=worker-pilot-1", "provider=anthropic", "model=claude-sonnet-5-5", "dry_run=False",
                   "send_enabled=False", "database_environment=db.example.test/postgres",
                   "configuration_fingerprint="):
        assert needle in line, needle
    for secret in (SECRET_KEY, DB_PASSWORD, TOKEN, ALLOWED):
        assert secret not in line


def test_fingerprint_detects_a_different_provider_but_ignores_the_worker_id():
    base = _settings()
    real = describe_worker_profile(base, WorkerSettings(owner="a"), SimpleNamespace(name="anthropic", model="m"))
    same_other_owner = describe_worker_profile(base, WorkerSettings(owner="b"),
                                               SimpleNamespace(name="anthropic", model="m"))
    simulated = describe_worker_profile(base, WorkerSettings(owner="a"), FakeLLMProvider())
    assert real["configuration_fingerprint"] == same_other_owner["configuration_fingerprint"]
    assert real["configuration_fingerprint"] != simulated["configuration_fingerprint"]


# --- Indicador "escribiendo..." ---

class RecordingSender:
    def __init__(self, error=None):
        self.calls: list[tuple[str, str]] = []
        self.error = error

    def show_typing(self, to, inbound_message_id):
        self.calls.append((to, inbound_message_id))
        if self.error:
            raise self.error


def test_typing_indicator_uses_the_inbound_message_id_when_conversation_is_automated():
    sender = RecordingSender()
    presence = TypingPresence(sender, lambda _cid: "automated", kill_switch=False)
    assert presence.start(uuid4(), ALLOWED, "wamid.INBOUND1") is True
    assert sender.calls == [(ALLOWED, "wamid.INBOUND1")]


def test_typing_indicator_never_shows_under_human_control_or_kill_switch():
    sender = RecordingSender()
    assert TypingPresence(sender, lambda _c: "human", kill_switch=False).start(uuid4(), ALLOWED, "wamid.X1") is False
    assert TypingPresence(sender, lambda _c: "automated", kill_switch=True).start(uuid4(), ALLOWED, "wamid.X2") is False
    assert sender.calls == []


def test_typing_failure_is_ignored_and_never_raises():
    presence = TypingPresence(RecordingSender(error=WhatsAppError("typing_transport")),
                              lambda _c: "automated", kill_switch=False)
    assert presence.start(uuid4(), ALLOWED, "wamid.X3") is False


def test_processor_requests_typing_with_the_real_message_id_before_generating_a_reply():
    typing_calls: list[tuple] = []

    class SpyTyping:
        def start(self, conversation_id, contact, inbound_id):
            typing_calls.append((contact, inbound_id))
            return True

    agent = FakeAgent(AgentOutcome(OutcomeStatus.REPLIED, reply_text="Claro, te ayudo.",
                                   lead_stage=LeadStage.DISCOVERY))
    from app.worker.processor import EventProcessor as _EP
    processor = _EP(agent=agent, leads=FakeLeads(), conversations=FakeConversations(), messages=FakeMessages(),
                    handoffs=FakeHandoffs(), outbox=FakeOutbox(), retention_days=7,
                    expected_business_phone_id="1", typing=SpyTyping())
    processor.process(_claim(external="wamid.REAL42", contact=ALLOWED), ALWAYS_RENEW)
    assert typing_calls == [(ALLOWED, "wamid.REAL42")]
    assert len(agent.calls) == 1


def test_processor_does_not_show_typing_for_unsupported_message_types():
    typing_calls: list = []

    class SpyTyping:
        def start(self, *args):
            typing_calls.append(args)
            return True

    processor = EventProcessor(agent=FakeAgent(), leads=FakeLeads(), conversations=FakeConversations(),
                               messages=FakeMessages(), handoffs=FakeHandoffs(), outbox=FakeOutbox(),
                               retention_days=7, expected_business_phone_id="1", typing=SpyTyping())
    processor.process(_claim(payload={"type": "image", "id": "wamid.IMG1"}), ALWAYS_RENEW)
    assert typing_calls == []


# --- Llamada HTTP del indicador (transporte simulado) ---

def _sender(handler, *, enabled=True, allowed=(ALLOWED,)):
    return CloudApiSender(
        access_token=TOKEN, phone_number_id=PHONE_ID, graph_api_version="v26.0",
        allowed_recipients=tuple(allowed), send_enabled=enabled, dry_run=False,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_show_typing_posts_read_and_typing_indicator_for_the_inbound_message():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    _sender(handler).show_typing(ALLOWED, "wamid.INBOUND9")
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == f"/v26.0/{PHONE_ID}/messages"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp", "status": "read", "message_id": "wamid.INBOUND9",
        "typing_indicator": {"type": "text"},
    }


@pytest.mark.parametrize("kwargs, error", [
    ({"enabled": False}, SendDisabledError),
    ({"allowed": ("573000000000",)}, RecipientNotAllowedError),
])
def test_show_typing_makes_no_request_when_sending_is_off_or_recipient_not_allowed(kwargs, error):
    seen: list = []
    sender = _sender(lambda r: (seen.append(r), httpx.Response(200))[1], **kwargs)
    with pytest.raises(error):
        sender.show_typing(ALLOWED, "wamid.INBOUND9")
    assert seen == []


def test_show_typing_rejects_malformed_message_ids_before_any_request():
    seen: list = []
    sender = _sender(lambda r: (seen.append(r), httpx.Response(200))[1])
    with pytest.raises(PayloadRejectedError):
        sender.show_typing(ALLOWED, "../../messages")
    assert seen == []


def test_show_typing_http_error_is_reported_without_secrets():
    sender = _sender(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(MetaRejectedError) as info:
        sender.show_typing(ALLOWED, "wamid.INBOUND9")
    assert TOKEN not in str(info.value) and info.value.code == "typing_http_500"


def test_show_typing_transport_error_is_a_controlled_failure():
    def handler(request):
        raise httpx.ReadError("connection reset", request=request)

    with pytest.raises(WhatsAppError) as info:
        _sender(handler).show_typing(ALLOWED, "wamid.INBOUND9")
    assert info.value.code == "typing_transport"
