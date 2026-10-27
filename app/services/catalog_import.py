"""Importación repetible del catálogo de La Cuisine desde un CSV.

Contrato del archivo (encabezados obligatorios en minúsculas, separador coma, UTF-8):
  referencia, marca, categoria, descripcion, precio_cop, disponibilidad, fecha_disponibilidad, enlace

- referencia, marca y categoria son obligatorios; referencia es la clave de actualización.
- precio_cop: solo dígitos con punto decimal opcional (p. ej. 1299000 o 1299000.50). Vacío = sin precio.
- fecha_disponibilidad: AAAA-MM-DD, opcional.
- enlace: vacío o una URL https.

La importación es todo o nada: si una fila tiene errores, no se escribe ninguna.
Los precios y la disponibilidad son referencias de la fuente, no confirmaciones en tiempo real.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import IO

from app.repositories.catalog import CatalogRepository, ProductRecord, UpsertResult

REQUIRED_COLUMNS = ("referencia", "marca", "categoria")
OPTIONAL_COLUMNS = ("descripcion", "precio_cop", "disponibilidad", "fecha_disponibilidad", "enlace")
PRICE_PATTERN = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,2})?$")
MAX_LENGTH = {"referencia": 64, "marca": 120, "categoria": 120, "descripcion": 2000, "disponibilidad": 120}


@dataclass(frozen=True)
class RowError:
    line: int
    field: str
    code: str


@dataclass
class ParseResult:
    records: list[ProductRecord] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


@dataclass(frozen=True)
class ImportReport:
    rows_read: int
    inserted: int
    updated: int
    unchanged: int


class CatalogImportError(Exception):
    def __init__(self, errors: list[RowError]) -> None:
        super().__init__(f"catalog_invalid rows_with_errors={len(errors)}")
        self.errors = errors


def parse_catalog_csv(stream: IO[str]) -> ParseResult:
    result = ParseResult()
    reader = csv.DictReader(stream)
    headers = tuple(h.strip().lower() for h in (reader.fieldnames or ()))
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        result.errors.append(RowError(line=1, field=",".join(missing), code="missing_column"))
        return result

    seen_references: set[str] = set()
    for row in reader:
        line = reader.line_num
        normalized = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        errors_before = len(result.errors)

        for name in REQUIRED_COLUMNS:
            if not normalized.get(name):
                result.errors.append(RowError(line, name, "required"))

        for name, limit in MAX_LENGTH.items():
            if len(normalized.get(name, "")) > limit:
                result.errors.append(RowError(line, name, "too_long"))

        reference = normalized.get("referencia", "")
        if reference and reference in seen_references:
            result.errors.append(RowError(line, "referencia", "duplicate_in_file"))
        seen_references.add(reference)

        price = _parse_price(normalized.get("precio_cop", ""))
        if price is False:
            result.errors.append(RowError(line, "precio_cop", "invalid_price"))

        as_of = _parse_date(normalized.get("fecha_disponibilidad", ""))
        if as_of is False:
            result.errors.append(RowError(line, "fecha_disponibilidad", "invalid_date"))

        url = normalized.get("enlace", "") or None
        if url is not None and not url.startswith("https://"):
            result.errors.append(RowError(line, "enlace", "invalid_url"))

        if len(result.errors) == errors_before:
            result.records.append(
                ProductRecord(
                    reference=reference,
                    brand=normalized["marca"],
                    category=normalized["categoria"],
                    description=normalized.get("descripcion") or None,
                    price_cop=price,
                    availability_label=normalized.get("disponibilidad") or None,
                    availability_as_of=as_of,
                    product_url=url,
                    source_hash=_row_hash(normalized),
                )
            )
    return result


def import_catalog(stream: IO[str], repository: CatalogRepository) -> ImportReport:
    parsed = parse_catalog_csv(stream)
    if parsed.errors:
        raise CatalogImportError(parsed.errors)
    if not parsed.records:
        return ImportReport(rows_read=0, inserted=0, updated=0, unchanged=0)

    outcome: UpsertResult = repository.upsert_products(parsed.records)
    return ImportReport(
        rows_read=len(parsed.records),
        inserted=outcome.inserted,
        updated=outcome.updated,
        unchanged=outcome.unchanged,
    )


def _parse_price(raw: str) -> Decimal | None | bool:
    if not raw:
        return None
    if not PRICE_PATTERN.match(raw):
        return False
    try:
        return Decimal(raw)
    except InvalidOperation:
        return False


def _parse_date(raw: str) -> date | None | bool:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return False


def _row_hash(normalized: dict[str, str]) -> str:
    keys = sorted(set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS))
    payload = {k: normalized.get(k, "") for k in keys}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
