"""Validación y extracción de mensajes entrantes del payload de WhatsApp Cloud API."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

WA_ID_PATTERN = re.compile(r"^[0-9]{6,20}$")
PHONE_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")
MESSAGE_ID_MAX_LENGTH = 128


class PayloadValidationError(ValueError):
    """El payload no cumple la estructura mínima esperada. El mensaje de error no incluye contenido."""


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: str | None
    raw: dict[str, Any]
    business_phone_id: str = ""

    @property
    def payload_sha256(self) -> str:
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_inbound_messages(payload: Any, max_messages: int) -> list[InboundMessage]:
    """Recorre entry[].changes[].value.messages[] y valida solo lo necesario para persistir.

    Los estados de entrega (value.statuses) no son mensajes entrantes y se ignoran.
    La línea de negocio (value.metadata.phone_number_id) identifica la conversación junto con el contacto.
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
            business_phone_id = _business_phone_id(value)
            for raw in _as_list(value, "messages"):
                if not isinstance(raw, dict):
                    raise PayloadValidationError("message_not_object")
                message_id = raw.get("id")
                if not isinstance(message_id, str) or not 0 < len(message_id) <= MESSAGE_ID_MAX_LENGTH:
                    raise PayloadValidationError("message_id_invalid")
                sender = raw.get("from")
                if sender is not None and (not isinstance(sender, str) or not WA_ID_PATTERN.match(sender)):
                    raise PayloadValidationError("sender_invalid")
                messages.append(
                    InboundMessage(message_id=message_id, sender=sender, raw=raw, business_phone_id=business_phone_id)
                )
                if len(messages) > max_messages:
                    raise PayloadValidationError("too_many_messages")
    return messages


@dataclass(frozen=True)
class StatusEvent:
    """Estado de un mensaje saliente (value.statuses[]). Los campos desconocidos de Meta se ignoran."""

    message_id: str
    status: str
    recipient_id: str | None
    error_code: str | None
    business_phone_id: str
    raw: dict[str, Any]

    @property
    def external_event_id(self) -> str:
        return f"status:{self.message_id}:{self.status}"

    @property
    def payload_sha256(self) -> str:
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_statuses(payload: Any, max_items: int) -> list[StatusEvent]:
    """Recorre entry[].changes[].value.statuses[]. No convierte estados en mensajes entrantes."""
    if not isinstance(payload, dict):
        return []
    events: list[StatusEvent] = []
    for entry in _as_list(payload, "entry"):
        for change in _as_list(entry, "changes"):
            value = _as_dict(change, "value")
            business_phone_id = _business_phone_id(value)
            for raw in _as_list(value, "statuses"):
                if not isinstance(raw, dict):
                    raise PayloadValidationError("status_not_object")
                message_id = raw.get("id")
                status = raw.get("status")
                if not isinstance(message_id, str) or not 0 < len(message_id) <= 100:
                    raise PayloadValidationError("status_id_invalid")
                if not isinstance(status, str) or not 0 < len(status) <= 20:
                    raise PayloadValidationError("status_value_invalid")
                recipient = raw.get("recipient_id")
                if recipient is not None and (not isinstance(recipient, str) or not WA_ID_PATTERN.match(recipient)):
                    recipient = None
                error_code = _first_error_code(raw.get("errors"))
                events.append(StatusEvent(message_id, status, recipient, error_code, business_phone_id, raw))
                if len(events) > max_items:
                    raise PayloadValidationError("too_many_statuses")
    return events


def _first_error_code(errors: Any) -> str | None:
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return None
    code = errors[0].get("code")
    return str(code)[:32] if code is not None else None


def _business_phone_id(value: dict[str, Any]) -> str:
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    phone_id = metadata.get("phone_number_id", "")
    if phone_id in (None, ""):
        return ""
    if not isinstance(phone_id, str) or not PHONE_ID_PATTERN.match(phone_id):
        raise PayloadValidationError("phone_number_id_invalid")
    return phone_id


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
