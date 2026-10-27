"""Persistencia repetible del catálogo comercial (tabla products), indexada por referencia comercial."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError


@dataclass(frozen=True)
class ProductRecord:
    reference: str
    brand: str
    category: str
    description: str | None
    price_cop: Decimal | None
    availability_label: str | None
    availability_as_of: date | None
    product_url: str | None
    source_hash: str


@dataclass(frozen=True)
class UpsertResult:
    inserted: int
    updated: int
    unchanged: int


class CatalogRepository(Protocol):
    def upsert_products(self, records: list[ProductRecord]) -> UpsertResult: ...


# Una sola sentencia por lote. Solo actualiza filas cuyo hash de origen cambió,
# por lo que una reimportación del mismo archivo no reescribe nada.
_UPSERT_SQL = """
    insert into public.products
        (reference, brand, category, description, price_cop,
         availability_label, availability_as_of, product_url, source_hash)
    select *
    from unnest(
        %s::text[], %s::text[], %s::text[], %s::text[], %s::numeric[],
        %s::text[], %s::date[], %s::text[], %s::text[]
    )
    on conflict (reference) do update set
        brand = excluded.brand,
        category = excluded.category,
        description = excluded.description,
        price_cop = excluded.price_cop,
        availability_label = excluded.availability_label,
        availability_as_of = excluded.availability_as_of,
        product_url = excluded.product_url,
        source_hash = excluded.source_hash
    where public.products.source_hash is distinct from excluded.source_hash
    returning (xmax = 0) as inserted
"""


class PostgresCatalogRepository:
    def __init__(self, pool: ConnectionPool, batch_size: int = 500) -> None:
        self._pool = pool
        self._batch_size = batch_size

    def upsert_products(self, records: list[ProductRecord]) -> UpsertResult:
        inserted = 0
        updated = 0
        for start in range(0, len(records), self._batch_size):
            batch = records[start : start + self._batch_size]
            try:
                with self._pool.connection() as conn:
                    rows = conn.execute(_UPSERT_SQL, _as_arrays(batch)).fetchall()
            except (psycopg.Error, PoolClosed):
                raise StorageError("catalog_write_failed") from None
            batch_inserted = sum(1 for (is_inserted,) in rows if is_inserted)
            inserted += batch_inserted
            updated += len(rows) - batch_inserted
        return UpsertResult(inserted=inserted, updated=updated, unchanged=len(records) - inserted - updated)


def _as_arrays(batch: list[ProductRecord]) -> tuple[list, ...]:
    return (
        [r.reference for r in batch],
        [r.brand for r in batch],
        [r.category for r in batch],
        [r.description for r in batch],
        [r.price_cop for r in batch],
        [r.availability_label for r in batch],
        [r.availability_as_of for r in batch],
        [r.product_url for r in batch],
        [r.source_hash for r in batch],
    )
