"""Scripts del piloto y cadena de respuesta. Sin procesos reales, sin red y sin Anthropic (mocks)."""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pilot_preflight  # noqa: E402
import pilot_workers  # noqa: E402
from app.integrations.llm import FakeLLMProvider  # noqa: E402
from app.integrations.whatsapp_client import AuthRejectedError, CloudApiSender, SendRequest  # noqa: E402
from app.worker.processor import EventProcessor  # noqa: E402
from tests.agent.test_orchestrator import Harness, reply  # noqa: E402
from tests.worker.test_processor import (  # noqa: E402
    ALWAYS_RENEW,
    FakeConversations,
    FakeHandoffs,
    FakeLeads,
    FakeMessages,
    FakeOutbox,
    _claim,
)

REPO = r"C:\Users\maura\OneDrive\Documents\GitHub\agente-LaCusine"
OTHER = r"C:\Users\maura\OneDrive\Documents\GitHub\otro-proyecto"


def _proc(pid, parent, cmd):
    return {"ProcessId": pid, "ParentProcessId": parent, "CommandLine": cmd}


def _worker_launcher(pid, parent=1, repo=REPO):
    return _proc(pid, parent, f'"{repo}\\.venv\\Scripts\\python.exe" -m app.worker.main')


def _worker_base(pid, parent):
    # Intérprete base hijo: su línea de comando NO contiene la ruta del repo.
    return _proc(pid, parent, r'"C:\Python311\python.exe" -m app.worker.main ')


# --- Identificación de árboles de workers ---

def test_worker_tree_includes_the_base_child_and_ignores_other_projects():
    processes = [
        _worker_launcher(100),
        _worker_base(101, 100),
        _worker_launcher(200, repo=OTHER),
        _worker_base(201, 200),
        _proc(300, 1, f'"{REPO}\\.venv\\Scripts\\python.exe" -m uvicorn app.main:app'),
    ]
    trees = pilot_workers.find_worker_trees(processes, REPO)
    assert trees == [{"root": 100, "pids": [100, 101]}]


def test_two_worker_launchers_of_this_repo_are_two_trees():
    processes = [_worker_launcher(100), _worker_base(101, 100), _worker_launcher(500), _worker_base(501, 500)]
    assert len(pilot_workers.find_worker_trees(processes, REPO)) == 2


# --- Parada verificada: un worker antiguo que no termina de inmediato ---

def test_stop_waits_for_a_slow_worker_and_kills_only_this_repos_roots():
    killed = []
    snapshots = [
        [_worker_launcher(100), _worker_base(101, 100), _worker_launcher(200, repo=OTHER)],  # inicial
        [_worker_base(101, 100), _worker_launcher(200, repo=OTHER)],  # el lanzador murió, el hijo sigue
        [_worker_base(101, 100), _worker_launcher(200, repo=OTHER)],  # aún vivo
        [_worker_launcher(200, repo=OTHER)],  # por fin termina
    ]
    calls = iter(snapshots + [snapshots[-1]] * 10)
    gone, remaining = pilot_workers.stop_and_wait(
        REPO, timeout_seconds=30, sample=lambda: next(calls), kill=lambda pid: killed.append(pid),
        sleep=lambda s: None, clock=iter(range(0, 1000)).__next__,
    )
    assert gone is True and remaining == []
    assert killed == [100]  # solo el árbol de este repo; el otro proyecto queda intacto


def test_stop_reports_failure_when_a_worker_never_ends():
    stuck = [_worker_launcher(100), _worker_base(101, 100)]
    gone, remaining = pilot_workers.stop_and_wait(
        REPO, timeout_seconds=3, sample=lambda: stuck, kill=lambda pid: None,
        sleep=lambda s: None, clock=iter(range(0, 1000)).__next__,
    )
    assert gone is False and remaining == [100, 101]


def test_wait_until_gone_returns_false_on_timeout():
    ticks = iter(range(0, 100))
    assert pilot_workers.wait_until_gone({101}, lambda: [_worker_base(101, 100)], 5,
                                         sleep=lambda s: None, clock=ticks.__next__) is False


def test_check_rejects_start_when_another_worker_of_this_repo_is_alive(monkeypatch):
    monkeypatch.setattr(pilot_workers, "sample_processes", lambda: [_worker_launcher(100), _worker_base(101, 100)])
    assert pilot_workers.main(["check", "--repo", REPO, "--expect", "0"]) == 1
    assert pilot_workers.main(["check", "--repo", REPO, "--expect", "1"]) == 0


# --- Token de Meta: detección previa sin enviar nada ---

def test_expired_meta_token_is_reported_without_leaking_the_token():
    token = "EAAG-SECRETO-NO-IMPRIMIR"

    def handler(request):
        assert request.url.path.endswith("/123456789012")
        return httpx.Response(401, json={"error": {"type": "OAuthException", "code": 190, "error_subcode": 463,
                                                   "message": "Session has expired"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    valid, detail = pilot_preflight.check_meta_token(token, "123456789012", "v26.0", client=client)
    assert valid is False
    assert detail == "http=401 codigo=190 subcodigo=463"
    assert token not in detail


def test_valid_meta_token_passes_the_read_only_check():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "1"})))
    assert pilot_preflight.check_meta_token("t", "123456789012", "v26.0", client=client) == (True, "vigente")


def test_meta_auth_rejection_is_logged_as_an_operational_hint_without_the_token(caplog):
    token = "EAAG-SECRETO-NO-IMPRIMIR"
    sender = CloudApiSender(
        access_token=token, phone_number_id="123456789012", graph_api_version="v26.0",
        allowed_recipients=("573001112233",), send_enabled=True, dry_run=False,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))),
    )
    with caplog.at_level(logging.ERROR, logger="lacusine.whatsapp"), pytest.raises(AuthRejectedError):
        sender.send_text(SendRequest(to="573001112233", text="hola", business_phone_id="123456789012",
                                     idempotency_key="k1"))
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "meta_auth_rejected http=401 hint=renovar_META_ACCESS_TOKEN" in messages
    assert token not in messages


# --- Cuatro turnos: proveedor tipo Anthropic simulado, outbox y sin transferencias ---

@dataclass
class AnthropicDouble(FakeLLMProvider):
    name: str = "anthropic"
    model: str = "claude-sonnet-5-5"


def test_four_turns_through_processor_persist_one_reply_each_and_never_transfer():
    provider = AnthropicDouble(script=[
        reply("¡Hola! Soy el asistente de La Cuisine. ¿En qué te ayudo?", intent="greeting"),
        reply("¡Muy bien, gracias! ¿Qué electrodoméstico buscas?", intent="other"),
        reply("Tenemos cafeteras. ¿Para cuántas personas la necesitas?", intent="product_inquiry",
              stage="DISCOVERY"),
        reply("Perfecto. Para dos personas puedes ver opciones compactas; el pago se completa en la web o con un "
              "asesor cuando decidas.", intent="purchase_intent", stage="HIGH_INTENT"),
    ])
    harness = Harness(provider=provider)
    conversations = FakeConversations()
    harness.conversations.states[conversations.id] = "automated"
    outbox, handoffs = FakeOutbox(), FakeHandoffs()
    processor = EventProcessor(
        agent=harness.agent, leads=FakeLeads(), conversations=conversations, messages=FakeMessages(),
        handoffs=handoffs, outbox=outbox, retention_days=7, expected_business_phone_id="1",
    )
    texts = ["Hola, buenos días", "¿Cómo estás?", "¿Qué cafeteras tienes?",
             "Quiero comprar una cafetera para dos personas."]
    for index, text in enumerate(texts):
        claim = _claim(payload={"type": "text", "text": {"body": text}}, external=f"wamid.TURN{index}")
        assert processor.process(claim, ALWAYS_RENEW).status == "processed"

    assert len(outbox.rows) == 4
    assert len({row["idempotency_key"] for row in outbox.rows}) == 4  # una respuesta por mensaje
    assert all(row["reply_text"] for row in outbox.rows)
    assert handoffs.opened == [] and harness.handoffs.opened == []
    assert harness.conversations.states[conversations.id] == "automated"
    assert provider.calls and all(call.call_id.startswith("wamid.TURN") for call in provider.calls)


def test_model_requiring_human_on_greeting_still_yields_the_model_reply_in_outbox():
    provider = AnthropicDouble(script=[
        reply("¡Hola! ¿Qué buscas hoy?", intent="other", requires_human=True, reason="unsupported_request"),
    ])
    harness = Harness(provider=provider)
    conversations = FakeConversations()
    harness.conversations.states[conversations.id] = "automated"
    outbox, handoffs = FakeOutbox(), FakeHandoffs()
    processor = EventProcessor(
        agent=harness.agent, leads=FakeLeads(), conversations=conversations, messages=FakeMessages(),
        handoffs=handoffs, outbox=outbox, retention_days=7, expected_business_phone_id="1",
    )
    processor.process(_claim(payload={"type": "text", "text": {"body": "Hola, buenos días"}}, external="wamid.H1"),
                      ALWAYS_RENEW)
    assert outbox.rows[0]["reply_text"] == "¡Hola! ¿Qué buscas hoy?"
    assert handoffs.opened == [] and harness.handoffs.opened == []


# --- Preflight: histórico fallido = advertencia; estados peligrosos = bloqueo ---

ALL_ZERO = {name: 0 for name in pilot_preflight.PENDING_QUERIES}


def test_historical_failed_replies_warn_but_never_block():
    counts = {**ALL_ZERO, "respuestas_fallidas_historicas": 1}
    blocking, warnings = pilot_preflight.evaluate_pending(counts)
    assert blocking == []
    assert warnings == ["respuestas_fallidas_historicas=1"]


def test_clean_queue_has_no_blocks_and_no_warnings():
    assert pilot_preflight.evaluate_pending(ALL_ZERO) == ([], [])


@pytest.mark.parametrize("critical", sorted(pilot_preflight.CRITICAL_QUEUE))
def test_each_critical_state_blocks_startup(critical):
    blocking, _ = pilot_preflight.evaluate_pending({**ALL_ZERO, critical: 1})
    assert blocking == [f"{critical}=1"]


@pytest.mark.parametrize("non_critical", sorted(
    set(pilot_preflight.RECOVERABLE_QUEUE) | set(pilot_preflight.PENDING_QUEUE) | set(pilot_preflight.HISTORICAL_QUEUE)))
def test_recoverable_pending_and_historical_states_never_block(non_critical):
    blocking, notes = pilot_preflight.evaluate_pending({**ALL_ZERO, non_critical: 3})
    assert blocking == []
    assert notes == [f"{non_critical}=3"]


def test_mission_incident_does_not_block_startup():
    # Un lease vencido, un dead_letter histórico, tres manual_review sin contacto y trabajo en cola.
    counts = {**ALL_ZERO, "eventos_con_lease_vencido": 1, "eventos_muertos": 1,
              "eventos_revision_manual_sin_contacto": 3, "eventos_pendientes_de_procesar": 6,
              "respuestas_fallidas_historicas": 2}
    blocking, notes = pilot_preflight.evaluate_pending(counts)
    assert blocking == []
    assert len(notes) == 5


def test_live_lease_blocks_because_another_process_is_processing_right_now():
    blocking, _ = pilot_preflight.evaluate_pending({**ALL_ZERO, "eventos_con_lease_vigente": 1})
    assert blocking == ["eventos_con_lease_vigente=1"]


def test_every_checked_state_has_a_query():
    names = (set(pilot_preflight.CRITICAL_QUEUE) | set(pilot_preflight.RECOVERABLE_QUEUE)
             | set(pilot_preflight.PENDING_QUEUE) | set(pilot_preflight.HISTORICAL_QUEUE))
    assert names <= set(pilot_preflight.PENDING_QUERIES)


PILOT_ENV = {
    "LLM_PROVIDER": "anthropic", "AGENT_DRY_RUN": "false", "ANTHROPIC_ENABLED": "true",
    "ANTHROPIC_API_KEY": "test-key-not-real", "ANTHROPIC_MODEL": "claude-sonnet-5-5",
    "ANTHROPIC_PRICING_MODEL": "claude-sonnet-5-5",
    "ANTHROPIC_PRICE_INPUT_USD_PER_MTOK": "1", "ANTHROPIC_PRICE_OUTPUT_USD_PER_MTOK": "5",
    "AGENT_DAILY_BUDGET_USD": "1", "AGENT_KILL_SWITCH": "false", "META_SEND_ENABLED": "true",
    "WHATSAPP_DRY_RUN": "false", "WHATSAPP_ALLOWED_RECIPIENTS": "573001112233",
    "META_PHONE_NUMBER_ID": "123456789012", "META_ACCESS_TOKEN": "test-token-not-real",
    "DATABASE_URL": "postgresql://user:pass@db.test:5432/postgres",
}


def _run_preflight(monkeypatch, counts):
    for key, value in PILOT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LACUSINE_LOAD_DOTENV", raising=False)
    monkeypatch.setattr(pilot_preflight, "check_meta_token", lambda *a, **k: (True, "vigente"))
    monkeypatch.setattr(pilot_preflight, "_pending_work", lambda url: counts)
    monkeypatch.setattr(sys, "argv", ["pilot_preflight.py", "--expect-model", "claude-sonnet-5-5"])
    return pilot_preflight.main()


def test_preflight_accepts_start_with_only_historical_failures(monkeypatch, capsys):
    assert _run_preflight(monkeypatch, {**ALL_ZERO, "respuestas_fallidas_historicas": 1}) == 0
    out = capsys.readouterr().out
    assert "[HISTÓRICO] respuestas_fallidas_historicas=1" in out
    assert "LISTO" in out


def test_preflight_refuses_start_with_a_pending_reply(monkeypatch, capsys):
    assert _run_preflight(monkeypatch, {**ALL_ZERO, "respuestas_pendientes": 1,
                                        "respuestas_fallidas_historicas": 1}) == 1
    assert "RECHAZADO" in capsys.readouterr().out


def test_preflight_refuses_start_with_an_uncertain_send(monkeypatch):
    assert _run_preflight(monkeypatch, {**ALL_ZERO, "respuestas_inciertas": 1}) == 1


def test_preflight_refuses_start_with_an_unreconciled_budget_reservation(monkeypatch):
    assert _run_preflight(monkeypatch, {**ALL_ZERO, "reservas_presupuesto_sin_liquidar": 1}) == 1


# --- Identidad de la línea: el modo público nunca se activa sobre un número de pruebas ---

def _identity_client(payload, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload)))


def test_test_number_is_not_a_production_line():
    identity = {"account_mode": "SANDBOX", "verified": "VERIFIED", "status": "CONNECTED"}
    assert pilot_preflight.is_production_line(identity) is False


def test_live_verified_connected_line_is_production():
    identity = {"account_mode": "LIVE", "verified": "VERIFIED", "status": "CONNECTED"}
    assert pilot_preflight.is_production_line(identity) is True


def test_unverified_or_disconnected_live_line_is_not_production():
    assert pilot_preflight.is_production_line({"account_mode": "LIVE", "verified": "NOT_VERIFIED",
                                               "status": "CONNECTED"}) is False
    assert pilot_preflight.is_production_line({"account_mode": "LIVE", "verified": "VERIFIED",
                                               "status": "FLAGGED"}) is False


def test_read_line_identity_returns_mode_without_leaking_the_token():
    client = _identity_client({"id": "1", "account_mode": "LIVE", "code_verification_status": "VERIFIED",
                               "status": "CONNECTED"})
    ok, identity = pilot_preflight.read_line_identity("token-secreto", "123456789012", "v26.0", client=client)
    assert ok is True and identity == {"account_mode": "LIVE", "verified": "VERIFIED", "status": "CONNECTED"}
    assert "token-secreto" not in repr(identity)


def test_read_line_identity_reports_meta_error_codes():
    client = _identity_client({"error": {"code": 190, "error_subcode": 463}}, status=401)
    ok, identity = pilot_preflight.read_line_identity("t", "123456789012", "v26.0", client=client)
    assert ok is False and identity["error"] == "http=401 codigo=190 subcodigo=463"


def test_preflight_refuses_public_mode_on_a_test_number(monkeypatch, capsys):
    monkeypatch.setenv("WHATSAPP_AUDIENCE_MODE", "public")
    monkeypatch.setenv("WHATSAPP_PUBLIC_CONFIRMATION", "ABRIR_ATENCION_PUBLICA_CONFIRMADA")
    monkeypatch.setattr(pilot_preflight, "read_line_identity",
                        lambda *a, **k: (True, {"account_mode": "SANDBOX", "verified": "VERIFIED", "status": "CONNECTED"}))
    assert _run_preflight(monkeypatch, ALL_ZERO) == 1
    assert "Modo público: línea LIVE" in capsys.readouterr().out


def test_preflight_accepts_public_mode_only_on_a_live_verified_line(monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUDIENCE_MODE", "public")
    monkeypatch.setenv("WHATSAPP_PUBLIC_CONFIRMATION", "ABRIR_ATENCION_PUBLICA_CONFIRMADA")
    monkeypatch.setattr(pilot_preflight, "read_line_identity",
                        lambda *a, **k: (True, {"account_mode": "LIVE", "verified": "VERIFIED", "status": "CONNECTED"}))
    assert _run_preflight(monkeypatch, ALL_ZERO) == 0
