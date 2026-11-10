"""Búsqueda comercial sobre el catálogo: nombre, marca, referencia, categoría, presupuesto y características.

- Recupera hasta CANDIDATE_LIMIT candidatos y devuelve al modelo solo MAX_RESULTS, ya ordenados por relevancia.
- Distingue la intención: máquina (cafetera, espresso…) o accesorio (filtro, pastilla, descalcificador…).
  Un accesorio nunca gana a una máquina por ser más barato si la consulta pide una máquina.
- El precio solo desempata cuando la consulta pide algo económico. Las palabras conversacionales no cuentan.
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

from app.repositories.products import CANDIDATE_LIMIT, MAX_RESULTS, ProductHit
from app.services.text import normalize, tokens, words

TYPO_CUTOFF = 0.82
MIN_TOKEN_LENGTH_FOR_TYPO = 5
REFERENCE_PATTERN = re.compile(r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{3,}\b")

# Vocabulario de intención. Se evalúa sobre el texto normalizado, antes de quitar palabras vacías.
ACCESSORY_TERMS = frozenset({
    "filtro", "filtros", "pastilla", "pastillas", "descalcificador", "descalcificacion", "limpiador",
    "limpieza", "accesorio", "accesorios", "cartucho", "cartuchos", "repuesto", "repuestos", "kit", "juego",
    "mantenimiento", "consumible", "consumibles",
})
MACHINE_TERMS = frozenset({
    "cafetera", "cafeteras", "maquina", "maquinas", "espresso", "superautomatica", "superautomaticas",
    "automatica", "automaticas", "cafe",
})
ECONOMY_TERMS = frozenset({"economica", "economico", "economicas", "economicos", "barata", "barato", "baratas", "baratos", "asequible"})


class ProductReadRepository(Protocol):
    def search(self, tokens: list[str], references: list[str], max_price_cop: int | None, limit: int) -> list[ProductHit]: ...
    def get_by_ids(self, ids: list[UUID]) -> dict[UUID, ProductHit]: ...
    def search_texts(self) -> list[str]: ...


@dataclass(frozen=True)
class QueryIntent:
    wants_accessory: bool
    economy: bool
    query_tokens: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    hits: list[ProductHit]
    confident: bool
    tokens: list[str]
    corrections: dict[str, str] = field(default_factory=dict)


def _singular(token: str) -> str:
    """«filtros» → «filtro», «accesorios» → «accesorio». La coincidencia es por subcadena: el singular
    aparece dentro del plural del catálogo y dentro del singular, así que ambos casos coinciden."""
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _in_category(token: str, category_words: set[str]) -> bool:
    """El término aparece como palabra de la categoría, en singular o plural («cafetera» ↔ «cafeteras»)."""
    return token in category_words or f"{token}s" in category_words or f"{token}es" in category_words


def query_intent(raw_words: list[str], query_tokens: list[str]) -> QueryIntent:
    """Intención de la consulta. `raw_words` incluye palabras vacías; así se detectan «económica» y «filtros»."""
    words = set(raw_words) | set(query_tokens)
    wants_accessory = bool(words & ACCESSORY_TERMS)
    return QueryIntent(
        wants_accessory=wants_accessory,
        economy=bool(words & ECONOMY_TERMS),
        query_tokens=tuple(query_tokens),
    )


def is_accessory(hit: ProductHit) -> bool:
    """Accesorio por categoría del catálogo (p. ej. «Accesorios cafeteras», «Accesorios parrilla»)."""
    category = normalize(hit.category or "")
    return category.startswith("accesorio") or "repuesto" in category


def is_aligned(hit: ProductHit, intent: QueryIntent) -> bool:
    """El tipo del producto coincide con lo que pide el cliente. Sin intención explícita, las máquinas
    (no accesorios) son la opción por defecto: es lo que se busca al nombrar una marca."""
    return is_accessory(hit) == intent.wants_accessory


def rank_candidates(hits: list[ProductHit], intent: QueryIntent) -> list[ProductHit]:
    """Ordena candidatos sin duplicados. Función pura y determinista.

    Criterios, en orden: referencia exacta; tipo alineado con la intención; marca mencionada; términos
    coincidentes; disponibilidad; precio solo si la consulta pide económico; nombre como desempate estable.
    """
    unique: dict[UUID, ProductHit] = {}
    for hit in hits:
        unique.setdefault(hit.id, hit)
    ordered = sorted(unique.values(), key=lambda h: normalize(h.name or h.reference))

    def score(hit: ProductHit) -> tuple:
        brand_words = set(normalize(hit.brand or "").split())
        brand_mentions = sum(1 for t in intent.query_tokens if t in brand_words)
        if intent.economy and hit.price_cop is not None:
            price_rank = -float(hit.price_cop)  # más barato = mayor puntuación, solo en consultas económicas
        else:
            price_rank = 0.0
        # La categoría del producto nombra lo que busca el cliente («cafeteras» → «Cafeteras automáticas»).
        # Así una máquina gana a un electrodoméstico que solo comparte una palabra de relleno.
        category_words = set(normalize(hit.category or "").split())
        category_mentions = sum(1 for t in intent.query_tokens if _in_category(t, category_words))
        return (
            bool(hit.reference_match),
            is_aligned(hit, intent),
            brand_mentions,
            category_mentions,
            hit.matched_tokens,
            bool(hit.is_available),
            price_rank,
        )

    return sorted(ordered, key=score, reverse=True)


class ProductSearchService:
    def __init__(self, repository: ProductReadRepository) -> None:
        self._repository = repository
        self._vocabulary: set[str] | None = None
        self._lock = threading.Lock()

    def search(self, query: str, max_price_cop: int | None = None, limit: int = MAX_RESULTS) -> SearchResult:
        raw_words = words(query)
        query_tokens, corrections = self._correct([_singular(t) for t in tokens(query)])
        references = [m.group(0).upper() for m in REFERENCE_PATTERN.finditer(query)]
        if not query_tokens and not references:
            return SearchResult(hits=[], confident=False, tokens=[], corrections=corrections)

        intent = query_intent(raw_words, query_tokens)
        candidates = self._repository.search(query_tokens, references, max_price_cop, CANDIDATE_LIMIT)
        ranked = rank_candidates(candidates, intent)[: min(limit, MAX_RESULTS)]
        top = ranked[0] if ranked else None
        confident = any(h.reference_match for h in ranked) or (
            top is not None
            and is_aligned(top, intent)
            and top.matched_tokens > 0
            and top.matched_tokens >= math.ceil(len(query_tokens) / 2)
        )
        return SearchResult(hits=ranked, confident=confident, tokens=query_tokens, corrections=corrections)

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
