"""Importador del catálogo XLSX de La Cuisine (hoja "Catálogo").

Columnas reales de la fuente (verificadas el 28/10/2026 sobre el archivo entregado):
  Marca, Categoría, Producto, Referencia, Precio (COP), Precio antes (COP), Ahorro (COP),
  Disponibilidad ("Disponible" | "Agotado"), Nota de entrega, Descripción, Link del producto

Reglas:
  * Los encabezados se comparan sin tildes ni mayúsculas.
  * Todo o nada por defecto: si hay filas inválidas o duplicadas no se escribe nada.
    Con allow_partial=True se escriben solo las filas válidas y no duplicadas.
  * Precios: enteros COP positivos. El precio anterior, si existe, debe ser mayor al actual
    y el ahorro debe coincidir con la diferencia.
  * Enlaces: solo dominio oficial https.
  * Ningún registro se inventa: si el archivo no existe, la importación falla con un error claro.

Uso (dry-run por defecto; --apply escribe usando DATABASE_URL):
  python -m app.services.catalog_xlsx --file RUTA.xlsx --extracted-on 2026-10-26
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from app.repositories.catalog import CatalogRepository, ProductRecord
from app.services.text import normalize

SHEET_NAME = "Catálogo"
REQUIRED_HEADERS = (
    "Marca", "Categoría", "Producto", "Referencia", "Precio (COP)", "Disponibilidad", "Link del producto",
)
OPTIONAL_HEADERS = ("Precio antes (COP)", "Ahorro (COP)", "Nota de entrega", "Descripción")
ALLOWED_LINK_HOSTS = frozenset({"lacuisineappliances.co", "www.lacuisineappliances.co"})
AVAILABILITY = {"disponible": True, "agotado": False}
MAX_LEN = {"Referencia": 64, "Marca": 120, "Categoría": 120, "Producto": 200, "Descripción": 2000, "Nota de entrega": 200}


@dataclass(frozen=True)
class RowIssue:
    line: int
    code: str
    field: str = ""


@dataclass
class ParseReport:
    rows_total: int = 0
    valid: list[ProductRecord] = field(default_factory=list)
    invalid: list[RowIssue] = field(default_factory=list)
    duplicates: list[RowIssue] = field(default_factory=list)

    @property
    def invalid_rows(self) -> int:
        """Filas con al menos un error (una fila puede tener varias incidencias)."""
        return len({issue.line for issue in self.invalid})


@dataclass(frozen=True)
class ImportSummary:
    rows_total: int
    valid: int
    invalid: int
    duplicates: int
    written: bool
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    error_counts: dict[str, int] = field(default_factory=dict)


class CatalogFileError(Exception):
    """El archivo no tiene la hoja o las columnas esperadas."""


def read_rows(path: Path) -> tuple[list[str], Iterable[tuple[Any, ...]]]:
    if not path.is_file():
        raise CatalogFileError(f"archivo no encontrado: {path.name}")
    import openpyxl  # importación tardía: solo el importador lo necesita

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = next((ws for ws in workbook.worksheets if normalize(ws.title) == normalize(SHEET_NAME)), None)
    if sheet is None:
        raise CatalogFileError("no existe la hoja de catálogo esperada")
    iterator = sheet.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(iterator, ())]
    return header, iterator


def parse_rows(header: list[str], rows: Iterable[tuple[Any, ...]], extracted_on: date) -> ParseReport:
    index = _header_index(header)
    report = ParseReport()
    seen_counts: Counter[str] = Counter()
    candidates: list[tuple[int, dict[str, Any]]] = []

    for offset, raw in enumerate(rows):
        line = offset + 2  # encabezado en la fila 1
        if raw is None or all(cell is None or str(cell).strip() == "" for cell in raw):
            continue
        report.rows_total += 1
        candidates.append((line, _cells(raw, index)))

    for _, cells in candidates:
        ref = str(cells.get("Referencia") or "").strip()
        seen_counts[ref] += 1

    for line, cells in candidates:
        ref = str(cells.get("Referencia") or "").strip()
        if ref and seen_counts[ref] > 1:
            report.duplicates.append(RowIssue(line, "duplicate_reference", "Referencia"))
            continue
        issues = _validate(cells)
        if issues:
            report.invalid.extend(RowIssue(line, code, fld) for code, fld in issues)
            continue
        report.valid.append(_to_record(cells, extracted_on))
    return report


def import_catalog_xlsx(
    path: Path,
    extracted_on: date,
    repository: CatalogRepository | None,
    allow_partial: bool = False,
) -> ImportSummary:
    header, rows = read_rows(path)
    report = parse_rows(header, rows, extracted_on)
    error_counts = dict(Counter(issue.code for issue in report.invalid + report.duplicates))

    clean = not report.invalid and not report.duplicates
    if repository is None or (not clean and not allow_partial):
        return ImportSummary(
            rows_total=report.rows_total,
            valid=len(report.valid),
            invalid=report.invalid_rows,
            duplicates=len(report.duplicates),
            written=False,
            error_counts=error_counts,
        )

    outcome = repository.upsert_products(report.valid) if report.valid else None
    return ImportSummary(
        rows_total=report.rows_total,
        valid=len(report.valid),
        invalid=report.invalid_rows,
        duplicates=len(report.duplicates),
        written=bool(report.valid),
        inserted=outcome.inserted if outcome else 0,
        updated=outcome.updated if outcome else 0,
        unchanged=outcome.unchanged if outcome else 0,
        error_counts=error_counts,
    )


def _header_index(header: list[str]) -> dict[str, int]:
    by_norm = {normalize(h): i for i, h in enumerate(header) if h}
    missing = [h for h in REQUIRED_HEADERS if normalize(h) not in by_norm]
    if missing:
        raise CatalogFileError("faltan columnas obligatorias: " + ", ".join(missing))
    index: dict[str, int] = {}
    for name in (*REQUIRED_HEADERS, *OPTIONAL_HEADERS):
        if normalize(name) in by_norm:
            index[name] = by_norm[normalize(name)]
    return index


def _cells(raw: tuple[Any, ...], index: dict[str, int]) -> dict[str, Any]:
    return {name: (raw[pos] if pos < len(raw) else None) for name, pos in index.items()}


def _validate(cells: dict[str, Any]) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    for name in REQUIRED_HEADERS:
        if not str(cells.get(name) or "").strip():
            issues.append(("required", name))
    for name, limit in MAX_LEN.items():
        if len(str(cells.get(name) or "")) > limit:
            issues.append(("too_long", name))

    price = _as_cop(cells.get("Precio (COP)"))
    if price is None or price <= 0:
        issues.append(("invalid_price", "Precio (COP)"))

    previous = _as_cop(cells.get("Precio antes (COP)"))
    savings = _as_cop(cells.get("Ahorro (COP)"))
    if cells.get("Precio antes (COP)") not in (None, "") and previous is None:
        issues.append(("invalid_price", "Precio antes (COP)"))
    if previous is not None and price is not None and previous <= price:
        issues.append(("previous_price_not_higher", "Precio antes (COP)"))
    if savings is not None:
        if previous is None:
            issues.append(("savings_without_previous_price", "Ahorro (COP)"))
        elif price is not None and savings != previous - price:
            issues.append(("savings_mismatch", "Ahorro (COP)"))

    availability = str(cells.get("Disponibilidad") or "").strip().lower()
    if availability not in AVAILABILITY:
        issues.append(("availability_unknown", "Disponibilidad"))

    link = str(cells.get("Link del producto") or "").strip()
    parsed = urlparse(link)
    if parsed.scheme != "https" or parsed.netloc.lower() not in ALLOWED_LINK_HOSTS:
        issues.append(("invalid_link", "Link del producto"))
    return issues


def _as_cop(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _to_record(cells: dict[str, Any], extracted_on: date) -> ProductRecord:
    reference = str(cells["Referencia"]).strip()
    name = str(cells["Producto"]).strip()
    brand = str(cells["Marca"]).strip()
    category = str(cells["Categoría"]).strip()
    description = str(cells.get("Descripción") or "").strip() or None
    delivery_note = str(cells.get("Nota de entrega") or "").strip() or None
    availability = str(cells["Disponibilidad"]).strip()
    price = _as_cop(cells["Precio (COP)"])
    previous = _as_cop(cells.get("Precio antes (COP)"))
    savings = _as_cop(cells.get("Ahorro (COP)"))
    url = str(cells["Link del producto"]).strip()

    payload = {
        "reference": reference, "name": name, "brand": brand, "category": category,
        "description": description, "price": price, "previous": previous, "savings": savings,
        "availability": availability, "delivery_note": delivery_note, "url": url,
        "extracted_on": extracted_on.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    search_text = normalize(" ".join([name, brand, category, reference, description or ""]))

    return ProductRecord(
        reference=reference,
        brand=brand,
        category=category,
        description=description,
        price_cop=Decimal(price),
        availability_label=availability,
        availability_as_of=extracted_on,
        product_url=url,
        source_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        name=name,
        previous_price_cop=Decimal(previous) if previous is not None else None,
        savings_cop=Decimal(savings) if savings is not None else None,
        is_available=AVAILABILITY[availability.lower()],
        delivery_note=delivery_note,
        extracted_on=extracted_on,
        search_text=search_text,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Importa el catálogo XLSX de La Cuisine.")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--extracted-on", required=True, type=date.fromisoformat)
    parser.add_argument("--apply", action="store_true", help="escribe en la base indicada por DATABASE_URL")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()

    repository = None
    if args.apply:
        from app.db.pool import create_pool
        from app.repositories.catalog import PostgresCatalogRepository

        url = os.environ.get("DATABASE_URL")
        if not url:
            print("error: DATABASE_URL no configurada")
            return 2
        pool = create_pool(url)
        pool.open(wait=True, timeout=10)
        repository = PostgresCatalogRepository(pool)

    try:
        summary = import_catalog_xlsx(args.file, args.extracted_on, repository, allow_partial=args.allow_partial)
    except CatalogFileError as exc:
        print(f"error: {exc}")
        return 2
    finally:
        if repository is not None:
            pool.close()

    print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
    return 0 if summary.invalid == 0 and summary.duplicates == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
