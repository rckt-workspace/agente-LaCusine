"""Validación y extracción de mensajes entrantes del payload de WhatsApp Cloud API."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

WA_ID_PATTERN = re.compile(r"^[0-9]{6,20}$")
MESSAGE_ID_MAX_LENGTH = 128


class PayloadValidationError(ValueError):
    """El payload no cumple la estructura mínima esperada. El mensaje de error no incluye contenido."""


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: str | None
    raw: dict[str, Any]

    @property
    def payload_sha256(self) -> str:
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_inbound_messages(payload: Any, max_messages: int) -> list[InboundMessage]:
    """Recorre entry[].changes[].value.messages[] y valida solo lo necesario para persistir.

    Los estados de entrega (value.statuses) no son mensajes entrantes y se ignoran.
    """
    if not isinstance(payload, dict):
        raise PayloadValidationError("payload_not_object")

    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        raise PayloadValidationError("entry_not_list")

    messages: list[InboundMessage] = []
    for entry in entries:
        changes = _as_list(entry, "changes")
        for change in changes:
            value = _as_dict(change, "value")
            for raw in _as_list(value, "messages"):
                if not isinstance(raw, dict):
                    raise PayloadValidationError("message_not_object")
                message_id = raw.get("id")
                if not isinstance(message_id, str) or not 0 < len(message_id) <= MESSAGE_ID_MAX_LENGTH:
                    raise PayloadValidationError("message_id_invalid")
                sender = raw.get("from")
                if sender is not None and (not isinstance(sender, str) or not WA_ID_PATTERN.match(sender)):
                    raise PayloadValidationError("sender_invalid")
                messages.append(InboundMessage(message_id=message_id, sender=sender, raw=raw))
                if len(messages) > max_messages:
                    raise PayloadValidationError("too_many_messages")
    return messages


def _as_list(container: Any, key: str) -> list[Any]:
    if not isinstance(container, dict):
        return []
    value = container.get(key, [])
    if not isinstance(value, list):
        raise PayloadValidationError(f"{key}_not_list")
    return value


def _as_dict(container: Any, key: str) -> dict[str, Any]:
    if not isinstance(container, dict):
        return {}
    value = container.get(key, {})
    if not isinstance(value, dict):
        raise PayloadValidationError(f"{key}_not_object")
    return value
