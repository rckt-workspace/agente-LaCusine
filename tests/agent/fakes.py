"""Dobles en memoria para pruebas del agente. Los productos son FIXTURES SINTÉTICOS, no catálogo real."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.repositories.errors import StorageError
from app.repositories.products import MAX_RESULTS, ProductHit
from app.services.cost_tracker import UsageEvent
from app.services.text import normalize, tokens


def make_product(
    reference: str,
    name: str,
    brand: str,
    category: str,
    price: int,
    previous: int | None = None,
    available: bool = True,
    delivery_note: str | None = None,
    description: str = "Fixture sintético de prueba",
) -> tuple[ProductHit, str]:
    hit = ProductHit(
        id=uuid.uuid4(),
        reference=reference,
        name=name,
        brand=brand,
        category=category,
        price_cop=Decimal(price),
        previous_price_cop=Decimal(previous) if previous else None,
        is_available=available,
        delivery_note=delivery_note,
        product_url=f"https://lacuisineappliances.co/products/{reference.lower()}",
        description=description,
        matched_tokens=0,
        reference_match=False,
    )
    search_text = normalize(" ".join([name, brand, category, reference, description]))
    return hit, search_text


class InMemoryProductRepository:
    def __init__(self, products: list[tuple[ProductHit, str]]) -> None:
        self._rows = products
        self.search_calls: list[dict] = []

    def search(self, tokens_: list[str], references: list[str], max_price_cop, limit: int) -> list[ProductHit]:
        self.search_calls.append({"tokens": tokens_, "references": references, "max_price": max_price_cop, "limit": limit})
        results = []
        for hit, text in self._rows:
            if max_price_cop is not None and hit.price_cop is not None and hit.price_cop > max_price_cop:
                continue
            matched = sum(1 for t in tokens_ if t in text)
            ref_match = hit.reference.upper() in {r.upper() for r in references}
            if matched == 0 and not ref_match:
                continue
            results.append(replace(hit, matched_tokens=matched, reference_match=ref_match))
        results.sort(key=lambda h: (not h.reference_match, -h.matched_tokens, not bool(h.is_available), h.price_cop or 0))
        return results[: min(limit, MAX_RESULTS)]

    def get_by_ids(self, ids: list[UUID]) -> dict[UUID, ProductHit]:
        return {hit.id: hit for hit, _ in self._rows if hit.id in set(ids)}

    def search_texts(self) -> list[str]:
        return [text for _, text in self._rows]


class InMemoryConversations:
    def __init__(self) -> None:
        self.states: dict[UUID, str] = {}

    def automation_state(self, conversation_id: UUID) -> str | None:
        return self.states.get(conversation_id)


class InMemoryHandoffs:
    def __init__(self) -> None:
        self.opened: list[tuple[UUID, str, str]] = []

    def open(self, conversation_id: UUID, reason: str, priority: str = "normal") -> UUID:
        self.opened.append((conversation_id, reason, priority))
        return uuid.uuid4()


class InMemoryTurns:
    def __init__(self) -> None:
        self.by_conversation: dict[UUID, list[tuple[str, str, datetime]]] = {}

    def add(self, conversation_id: UUID, direction: str, text: str) -> None:
        self.by_conversation.setdefault(conversation_id, []).append(
            (direction, text, datetime.now(timezone.utc))
        )

    def recent_turns(self, conversation_id: UUID, limit: int) -> list[tuple[str, str, datetime]]:
        return list(self.by_conversation.get(conversation_id, []))[-limit:]


class InMemoryUsageStore:
    """Imita las restricciones de llm_usage_events: éxito único por call_id; fallos sin restricción."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.successes: dict[str, UsageEvent] = {}
        self.failures: list[UsageEvent] = []
        self.fail_with = fail_with
        self.created: list[tuple[datetime, Decimal]] = []
        self._lock = threading.Lock()

    def record_success_once(self, event: UsageEvent) -> bool:
        with self._lock:
            if event.call_id in self.successes:
                return False
            self.successes[event.call_id] = event
            self.created.append((datetime.now(timezone.utc), event.cost_usd))
            return True

    def record_failure(self, event: UsageEvent) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.failures.append(event)

    def has_success(self, call_id: str) -> bool:
        return call_id in self.successes

    def spent_between(self, start: datetime, end: datetime) -> Decimal:
        return sum((cost for at, cost in self.created if start <= at < end), Decimal("0"))


class FailingUsageStore(InMemoryUsageStore):
    def record_success_once(self, event: UsageEvent) -> bool:
        raise StorageError("usage_write_failed")


def product_fixtures() -> list[tuple[ProductHit, str]]:
    """Catálogo sintético pequeño. Referencias y nombres son de prueba."""
    return [
        make_product("TEST-JURA-C3", "Cafetera Automática TEST C3", "Jura", "Cafeteras automáticas", 2990000, previous=4810000),
        make_product("TEST-JURA-E6", "Cafetera Automática TEST E6", "Jura", "Cafeteras automáticas", 5990000, previous=8699000),
        make_product("TEST-WHP-SBS", "Nevera Side by Side TEST 518", "Whirlpool", "Neveras y refrigeración", 3049900,
                     previous=4549900, delivery_note="Entrega a finales de octubre"),
        make_product("TEST-SMEG-50", "Nevera Estilo Retro TEST", "Smeg", "Neveras y refrigeración", 12500000, available=False),
        make_product("TEST-CHEAP-1", "Sartén Esmaltada TEST", "Victoria", "Esenciales de cocina", 36990),
    ]


def expected_tokens(text: str) -> list[str]:
    return tokens(text)
