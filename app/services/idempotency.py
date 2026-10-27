"""Control de duplicados. La implementación en memoria es provisional: la Fase 2 la reemplaza por Supabase."""

from __future__ import annotations

import threading
from typing import Protocol


class EventStore(Protocol):
    def register(self, event_id: str) -> bool:
        """Devuelve True si el evento es nuevo y False si ya fue recibido."""
        ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def register(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen.add(event_id)
            return True
