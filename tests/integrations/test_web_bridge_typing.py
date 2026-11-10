"""Indicador «escribiendo…» del transporte web: señal firmada al conector, con la misma autorización que el envío."""

from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.web_bridge import WebBridgeSender, verify
from app.integrations.whatsapp_client import RecipientNotAllowedError, SendDisabledError, WhatsAppError
from app.worker.presence import TypingPresence
from uuid import uuid4

SECRET = "s3cr3t-de-prueba-de-32-caracteres!!"
LINE = "573153863179"
CLIENT = "573001112233"
OTHER = "573009998877"


def _sender(handler, *, auto_reply=True, allowed=(CLIENT,)):
    return WebBridgeSender(url="http://127.0.0.1:8787", secret=SECRET, line_id=LINE, auto_reply=auto_reply,
                           allowed_recipients=allowed, audience_mode="test_allowlist",
                           client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_typing_signal_is_signed_and_sent_to_the_typing_route_of_the_client():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert request.url.path == "/typing"
        assert verify(SECRET, "send", request.headers["X-Bridge-Timestamp"], body, request.headers["X-Bridge-Signature"])
        seen["payload"] = json.loads(body)
        return httpx.Response(200, json={"typing": True})

    assert _sender(handler).show_typing(CLIENT, "wamid.x") is None
    assert seen["payload"] == {"route": CLIENT, "action": "start"}


def test_nothing_is_signalled_when_auto_reply_is_off():
    calls = []
    sender = _sender(lambda r: (calls.append(r), httpx.Response(200))[1], auto_reply=False)
    with pytest.raises(SendDisabledError):
        sender.show_typing(CLIENT, "wamid.x")
    assert calls == []


def test_an_unlisted_recipient_gets_no_typing_signal():
    calls = []
    sender = _sender(lambda r: (calls.append(r), httpx.Response(200))[1])
    with pytest.raises(RecipientNotAllowedError):
        sender.show_typing(OTHER, "wamid.x")
    assert calls == []


def test_the_connector_refusing_typing_under_stop_is_an_explicit_error():
    sender = _sender(lambda r: httpx.Response(409, json={"error": "killed"}))
    with pytest.raises(WhatsAppError):
        sender.show_typing(CLIENT, "wamid.x")


def test_presence_never_blocks_the_reply_when_typing_fails():
    sender = _sender(lambda r: httpx.Response(409, json={"error": "killed"}))
    presence = TypingPresence(sender, automation_state=lambda _c: "automated", kill_switch=False)
    assert presence.start(uuid4(), CLIENT, "wamid.x") is False
