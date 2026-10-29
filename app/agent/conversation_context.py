"""Contexto conversacional acotado. Sin memoria en proceso: se reconstruye desde la base tras cualquier reinicio."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from typing import Protocol


class RecentTurnsReader(Protocol):
    def recent_turns(self, conversation_id: UUID, limit: int) -> list[tuple[str, str, object]]: ...


@dataclass(frozen=True)
class Turn:
    role: str  # "user" (cliente) | "assistant" (agente)
    text: str


@dataclass(frozen=True)
class ContextWindow:
    turns: tuple[Turn, ...]
    truncated: bool


class ConversationContextService:
    def __init__(self, reader: RecentTurnsReader, max_messages: int = 8, max_chars: int = 3000) -> None:
        if max_messages < 1 or max_chars < 200:
            raise ValueError("límites de contexto inválidos")
        self._reader = reader
        self._max_messages = max_messages
        self._max_chars = max_chars

    def build(self, conversation_id: UUID) -> ContextWindow:
        rows = self._reader.recent_turns(conversation_id, self._max_messages)
        turns = [Turn(role="user" if direction == "inbound" else "assistant", text=text) for direction, text, _ in rows]
        truncated = len(rows) == self._max_messages

        kept: list[Turn] = []
        budget = self._max_chars
        for turn in reversed(turns):
            if len(turn.text) > budget:
                truncated = True
                break
            kept.append(turn)
            budget -= len(turn.text)
        if len(kept) < len(turns):
            truncated = True
        return ContextWindow(turns=tuple(reversed(kept)), truncated=truncated)
