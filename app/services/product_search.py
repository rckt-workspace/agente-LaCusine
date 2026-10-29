"""Búsqueda comercial sobre el catálogo: nombre, marca, referencia, categoría, presupuesto y características.

- Solo devuelve hasta MAX_RESULTS productos: el catálogo completo nunca se envía a Claude.
- Corrige errores menores de escritura contra el vocabulario real del catálogo (difflib, sin dependencias).
- Marca una coincidencia como confiable solo con referencia exacta o con al menos la mitad de los términos.
  Si no es confiable, el agente debe reconocer que no tiene información suficiente.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Protocol
from uuid import UUID

from app.repositories.products import MAX_RESULTS, ProductHit
from app.services.text import normalize, tokens

TYPO_CUTOFF = 0.82
MIN_TOKEN_LENGTH_FOR_TYPO = 5
REFERENCE_PATTERN = re.compile(r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{3,}\b")


class ProductReadRepository(Protocol):
    def search(self, tokens: list[str], references: list[str], max_price_cop: int | None, limit: int) -> list[ProductHit]: ...
    def get_by_ids(self, ids: list[UUID]) -> dict[UUID, ProductHit]: ...
    def search_texts(self) -> list[str]: ...


@dataclass(frozen=True)
class SearchResult:
    hits: list[ProductHit]
    confident: bool
    tokens: list[str]
    corrections: dict[str, str] = field(default_factory=dict)


class ProductSearchService:
    def __init__(self, repository: ProductReadRepository) -> None:
        self._repository = repository
        self._vocabulary: set[str] | None = None
        self._lock = threading.Lock()

    def search(self, query: str, max_price_cop: int | None = None, limit: int = MAX_RESULTS) -> SearchResult:
        query_tokens, corrections = self._correct(tokens(query))
        references = [m.group(0).upper() for m in REFERENCE_PATTERN.finditer(query)]
        if not query_tokens and not references:
            return SearchResult(hits=[], confident=False, tokens=[], corrections=corrections)

        hits = self._repository.search(query_tokens, references, max_price_cop, min(limit, MAX_RESULTS))
        confident = any(h.reference_match for h in hits) or (
            bool(hits) and hits[0].matched_tokens >= math.ceil(len(query_tokens) / 2) and hits[0].matched_tokens > 0
        )
        return SearchResult(hits=hits, confident=confident, tokens=query_tokens, corrections=corrections)

    def _correct(self, query_tokens: list[str]) -> tuple[list[str], dict[str, str]]:
        vocabulary = self._load_vocabulary()
        corrected: list[str] = []
        corrections: dict[str, str] = {}
        for token in query_tokens:
            if token in vocabulary or len(token) < MIN_TOKEN_LENGTH_FOR_TYPO:
                corrected.append(token)
                continue
            match = get_close_matches(token, sorted(vocabulary), n=1, cutoff=TYPO_CUTOFF)
            if match:
                corrections[token] = match[0]
                corrected.append(match[0])
            else:
                corrected.append(token)
        return corrected, corrections

    def _load_vocabulary(self) -> set[str]:
        with self._lock:
            if self._vocabulary is None:
                words: set[str] = set()
                for text in self._repository.search_texts():
                    words.update(normalize(text).split())
                self._vocabulary = words
            return self._vocabulary
