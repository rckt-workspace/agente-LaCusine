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
    name: str | None = None
    previous_price_cop: Decimal | None = None
    savings_cop: Decimal | None = None
    is_available: bool | None = None
    delivery_note: str | None = None
    extracted_on: date | None = None
    search_text: str = ""


@dataclass(frozen=True)
class UpsertResult:
    inserted: int
    updated: int
    unchanged: int


class CatalogRepository(Protocol):
    def upsert_products(self, records: list[ProductRecord]) -> UpsertResult: ...


# Una sola sentencia por lote. Solo actualiza filas cuyo hash de origen cambió,
# por lo que una reimportación del mismo archivo no reescribe nada.
_COLUMNS = (
    "reference", "brand", "category", "description", "price_cop",
    "availability_label", "availability_as_of", "product_url", "source_hash",
    "name", "previous_price_cop", "savings_cop", "is_available", "delivery_note",
    "extracted_on", "search_text",
)
_CASTS = (
    "text[]", "text[]", "text[]", "text[]", "numeric[]",
    "text[]", "date[]", "text[]", "text[]",
    "text[]", "numeric[]", "numeric[]", "boolean[]", "text[]",
    "date[]", "text[]",
)
_UNNEST_ARGS = ", ".join(f"%s::{cast}" for cast in _CASTS)
_UPDATE_SET = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "reference")

# Una sola sentencia por lote. Solo actualiza filas cuyo hash de origen cambió,
# por lo que una reimportación del mismo archivo no reescribe nada.
_UPSERT_SQL = f"""
    insert into public.products ({", ".join(_COLUMNS)})
    select *
    from unnest({_UNNEST_ARGS})
    on conflict (reference) do update set {_UPDATE_SET}
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
    return tuple([getattr(r, column) for r in batch] for column in _COLUMNS)
