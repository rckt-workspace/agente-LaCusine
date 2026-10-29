"""Consultas de lectura del catálogo. Siempre parametrizadas y con límite de resultados."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError

MAX_RESULTS = 5


@dataclass(frozen=True)
class ProductHit:
    id: UUID
    reference: str
    name: str | None
    brand: str
    category: str
    price_cop: Decimal | None
    previous_price_cop: Decimal | None
    is_available: bool | None
    delivery_note: str | None
    product_url: str | None
    description: str | None
    matched_tokens: int
    reference_match: bool


_SEARCH_SQL = """
    select
        p.id, p.reference, p.name, p.brand, p.category, p.price_cop, p.previous_price_cop,
        p.is_available, p.delivery_note, p.product_url, p.description,
        (select count(*) from unnest(%(tokens)s::text[]) as t where strpos(p.search_text, t) > 0)::int as matched_tokens,
        (upper(p.reference) = any(%(refs)s::text[])) as reference_match
    from public.products p
    where
        (
            upper(p.reference) = any(%(refs)s::text[])
            or exists (select 1 from unnest(%(tokens)s::text[]) as t where strpos(p.search_text, t) > 0)
        )
        and (%(max_price)s::numeric is null or p.price_cop <= %(max_price)s::numeric)
    order by reference_match desc, matched_tokens desc, coalesce(p.is_available, false) desc, p.price_cop asc
    limit %(limit)s
"""

_BY_IDS_SQL = """
    select
        p.id, p.reference, p.name, p.brand, p.category, p.price_cop, p.previous_price_cop,
        p.is_available, p.delivery_note, p.product_url, p.description,
        0 as matched_tokens, false as reference_match
    from public.products p
    where p.id = any(%(ids)s::uuid[])
"""

_SEARCH_TEXT_SQL = "select search_text from public.products where search_text <> ''"


class PostgresProductRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def search(
        self,
        tokens: list[str],
        references: list[str],
        max_price_cop: int | None,
        limit: int = MAX_RESULTS,
    ) -> list[ProductHit]:
        if not tokens and not references:
            return []
        params = {
            "tokens": tokens or [""],
            "refs": [r.upper() for r in references] or [""],
            "max_price": max_price_cop,
            "limit": min(max(limit, 1), MAX_RESULTS),
        }
        return self._fetch(_SEARCH_SQL, params)

    def get_by_ids(self, ids: list[UUID]) -> dict[UUID, ProductHit]:
        if not ids:
            return {}
        return {hit.id: hit for hit in self._fetch(_BY_IDS_SQL, {"ids": ids})}

    def search_texts(self) -> list[str]:
        try:
            with self._pool.connection() as conn:
                return [row[0] for row in conn.execute(_SEARCH_TEXT_SQL).fetchall()]
        except (psycopg.Error, PoolClosed):
            raise StorageError("product_read_failed") from None

    def _fetch(self, sql: str, params: dict) -> list[ProductHit]:
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(sql, params).fetchall()
        except (psycopg.Error, PoolClosed):
            raise StorageError("product_read_failed") from None
        return [
            ProductHit(
                id=row[0], reference=row[1], name=row[2], brand=row[3], category=row[4],
                price_cop=row[5], previous_price_cop=row[6], is_available=row[7], delivery_note=row[8],
                product_url=row[9], description=row[10], matched_tokens=row[11], reference_match=row[12],
            )
            for row in rows
        ]
