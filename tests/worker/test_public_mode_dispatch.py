"""Modo público y modo de prueba, de extremo a extremo en el despachador con el cliente REAL de Cloud API.

El transporte HTTP es simulado: cada caso cuenta las llamadas que saldrían hacia Meta. Sin red ni base de datos.
Garantías comprobadas:
  * Público: responde a un cliente que escribió dentro de las últimas 24 horas, sin lista de destinatarios.
  * Público: nunca envía a un contacto fuera de ventana, a una conversación bajo control humano, a un contacto sin
    mensaje entrante (conversación iniciada por la empresa) ni desde una línea distinta de la registrada.
  * test_allowlist: sigue limitado a su lista, aunque el cliente haya escrito.
"""

from datetime import timedelta

import httpx
import pytest

from app.integrations.whatsapp_client import CloudApiSender
from app.worker.dispatcher import OutboxDispatcher
from tests.worker.test_dispatcher import NOW, PHONE, FakeOutbound, _dispatcher

CLIENT = "573001112233"
OTHER_PHONE = "999888777666"


class CountingTransport:
    """Cuenta cada POST que llegaría a Graph API. Responde como Meta cuando acepta el mensaje."""

    def __init__(self):
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json={"messages": [{"id": f"wamid.REAL{self.calls}"}]})


def _cloud_sender(transport, *, phone=PHONE, audience="public", allowed=()):
    return CloudApiSender(
        access_token="token-sintetico", phone_number_id=phone, graph_api_version="v26.0",
        allowed_recipients=tuple(allowed), send_enabled=True, dry_run=False,
        client=httpx.Client(transport=httpx.MockTransport(transport)), audience_mode=audience,
    )


def _run(repo, sender) -> tuple:
    dispatcher: OutboxDispatcher = _dispatcher(repo, sender)
    stats = dispatcher.dispatch_once()
    return stats, repo.finished


def test_public_replies_to_a_client_who_wrote_within_the_window_without_any_list():
    repo = FakeOutbound(last_inbound=NOW - timedelta(hours=1), wa=CLIENT)
    repo.add()
    transport = CountingTransport()
    stats, finished = _run(repo, _cloud_sender(transport, audience="public"))
    assert transport.calls == 1
    assert stats.sent == 1 and finished[0]["status"] == "sent"
    assert finished[0]["external_message_id"] == "wamid.REAL1"
    assert finished[0]["from_phone_id"] == PHONE


@pytest.mark.parametrize("hours_ago", [25, 48])
def test_public_never_sends_outside_the_24_hour_window(hours_ago):
    repo = FakeOutbound(last_inbound=NOW - timedelta(hours=hours_ago), wa=CLIENT)
    repo.add()
    transport = CountingTransport()
    stats, finished = _run(repo, _cloud_sender(transport, audience="public"))
    assert transport.calls == 0
    assert finished[0]["status"] == "blocked" and finished[0]["error_code"] == "outside_service_window"


def test_public_never_starts_a_conversation_the_company_initiated():
    repo = FakeOutbound(last_inbound=None, wa=CLIENT)
    repo.add()
    transport = CountingTransport()
    _run(repo, _cloud_sender(transport, audience="public"))
    assert transport.calls == 0


def test_public_never_replies_under_human_control():
    repo = FakeOutbound(state="human", last_inbound=NOW - timedelta(minutes=5), wa=CLIENT)
    repo.add()
    transport = CountingTransport()
    stats, finished = _run(repo, _cloud_sender(transport, audience="public"))
    assert transport.calls == 0
    assert finished[0]["status"] == "cancelled" and finished[0]["error_code"] == "human_control"


def test_public_refuses_a_send_from_a_line_other_than_the_registered_one():
    repo = FakeOutbound(last_inbound=NOW - timedelta(hours=1), wa=CLIENT)
    repo.add()
    transport = CountingTransport()
    # El despachador opera con PHONE, pero el cliente de envío apunta a otra línea: no debe salir ninguna llamada.
    _run(repo, _cloud_sender(transport, phone=OTHER_PHONE, audience="public"))
    assert transport.calls == 0


def test_test_allowlist_refuses_a_client_who_wrote_even_within_the_window():
    repo = FakeOutbound(last_inbound=NOW - timedelta(hours=1), wa=CLIENT)
    repo.add()
    transport = CountingTransport()
    stats, finished = _run(repo, _cloud_sender(transport, audience="test_allowlist", allowed=("573009990000",)))
    assert transport.calls == 0
    assert finished[0]["status"] == "failed" and finished[0]["error_code"] == "recipient_not_allowed"


def test_test_allowlist_still_sends_to_its_listed_recipient_within_the_window():
    repo = FakeOutbound(last_inbound=NOW - timedelta(hours=1), wa=CLIENT)
    repo.add()
    transport = CountingTransport()
    stats, finished = _run(repo, _cloud_sender(transport, audience="test_allowlist", allowed=(CLIENT,)))
    assert transport.calls == 1 and finished[0]["status"] == "sent"
