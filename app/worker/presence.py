"""Indicador 'escribiendo...' antes de una respuesta real. Nunca bloquea ni cambia la respuesta comercial.

Reglas:
  * Solo se activa cuando el worker va a llamar a un proveedor real (el bootstrap no lo conecta en dry-run).
  * No se activa si el kill switch está activo o la conversación está bajo control humano.
  * Cualquier fallo se registra sin datos del cliente y se ignora.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

logger = logging.getLogger("lacusine.typing")


class TypingPresence:
    def __init__(self, sender, automation_state: Callable[[UUID], str | None], kill_switch: bool) -> None:
        self._sender = sender
        self._automation_state = automation_state
        self._kill_switch = kill_switch

    def start(self, conversation_id: UUID, contact_wa_id: str, inbound_message_id: str) -> bool:
        try:
            if self._kill_switch:
                return False
            if self._automation_state(conversation_id) != "automated":
                return False
            self._sender.show_typing(contact_wa_id, inbound_message_id)
            return True
        except Exception as exc:  # noqa: BLE001 - el indicador nunca debe impedir la respuesta
            logger.info("typing_indicator_skipped code=%s", getattr(exc, "code", type(exc).__name__))
            return False
