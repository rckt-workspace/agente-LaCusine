"""Extracción mínima de identificadores del payload de WhatsApp Cloud API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: str | None


def extract_inbound_messages(payload: Any) -> list[InboundMessage]:
    """Recorre entry[].changes[].value.messages[] sin asumir la forma completa del payload."""
    messages: list[InboundMessage] = []
    if not isinstance(payload, dict):
        return messages
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            for message in value.get("messages") or []:
                message_id = (message or {}).get("id")
                if isinstance(message_id, str) and message_id:
                    messages.append(InboundMessage(message_id=message_id, sender=message.get("from")))
    return messages
