"""Importador XLSX sobre libros SINTÉTICOS generados en memoria. Sin base de datos."""

import io
import os
import uuid
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from app.repositories.catalog import UpsertResult
from app.services.catalog_xlsx import CatalogFileError, import_catalog_xlsx, parse_rows, read_rows

HEADERS = [
    "Marca", "Categoría", "Producto", "Referencia", "Precio (COP)", "Precio antes (COP)", "Ahorro (COP)",
    "Disponibilidad", "Nota de entrega", "Descripción", "Link del producto",
]
EXTRACTED = date(2026, 10, 26)


def _row(**overrides):
    base = {
        "Marca": "MarcaPrueba", "Categoría": "Nevera", "Producto": "Nevera TEST 1", "Referencia": "TEST-REF-1",
        "Precio (COP)": 2990000, "Precio antes (COP)": 4810000, "Ahorro (COP)": 1820000,
        "Disponibilidad": "Disponible", "Nota de entrega": "Entrega a finales de octubre",
        "Descripción": "Fixture sintético", "Link del producto": "https://lacuisineappliances.co/products/test-ref-1",
    }
    base.update(overrides)
    return base


def _book(path: Path, rows: list[dict], headers: list[str] | None = None, sheet: str = "Catálogo") -> Path:
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = sheet
    cols = headers or HEADERS
    ws.append(cols)
    for row in rows:
        ws.append([row.get(c) for c in cols])
    workbook.save(path)
    return path


class FakeCatalog:
    def __init__(self):
        self.written: list = []

    def upsert_products(self, records):
        self.written.extend(records)
        return UpsertResult(inserted=len(records), updated=0, unchanged=0)


def _parse(tmp_path, rows, **kwargs):
    path = _book(tmp_path / f"{uuid.uuid4().hex}.xlsx", rows, **kwargs)
    header, data = read_rows(path)
    return parse_rows(header, data, EXTRACTED)


def test_valid_rows_are_normalized_with_previous_price_savings_and_extraction_date(tmp_path):
    report = _parse(tmp_path, [_row()])
    assert report.invalid == [] and len(report.valid) == 1
    record = report.valid[0]
    assert record.price_cop == 2990000 and record.previous_price_cop == 4810000 and record.savings_cop == 1820000
    assert record.is_available is True
    assert record.extracted_on == EXTRACTED
    assert record.availability_as_of == EXTRACTED
    assert record.delivery_note == "Entrega a finales de octubre"
    assert "nevera test 1" in record.search_text and "test ref 1" in record.search_text


def test_search_text_is_accent_free_and_lowercase(tmp_path):
    report = _parse(tmp_path, [_row(**{"Categoría": "Neveras y refrigeración", "Descripción": "Acción técnica"})])
    text = report.valid[0].search_text
    assert text == text.lower()
    assert "refrigeracion" in text and "accion" in text


def test_headers_are_matched_without_accents_or_case(tmp_path):
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = "Catálogo"
    ws.append([h.replace("í", "i").replace("ó", "o").lower() for h in HEADERS])
    ws.append([_row()[h] for h in HEADERS])
    path = tmp_path / "h.xlsx"
    workbook.save(path)
    header, data = read_rows(path)
    report = parse_rows(header, data, EXTRACTED)
    assert report.invalid == [] and len(report.valid) == 1


@pytest.mark.parametrize("override, code, field", [
    ({"Precio (COP)": 0}, "invalid_price", "Precio (COP)"),
    ({"Precio (COP)": "2990000"}, "invalid_price", "Precio (COP)"),
    ({"Precio (COP)": 2990000.5}, "invalid_price", "Precio (COP)"),
    ({"Precio antes (COP)": 2990000}, "previous_price_not_higher", "Precio antes (COP)"),
    ({"Ahorro (COP)": 1}, "savings_mismatch", "Ahorro (COP)"),
    ({"Precio antes (COP)": None, "Ahorro (COP)": 5}, "savings_without_previous_price", "Ahorro (COP)"),
    ({"Disponibilidad": "Pronto"}, "availability_unknown", "Disponibilidad"),
    ({"Link del producto": "http://lacuisineappliances.co/x"}, "invalid_link", "Link del producto"),
    ({"Link del producto": "https://tienda-falsa.example/x"}, "invalid_link", "Link del producto"),
    ({"Marca": ""}, "required", "Marca"),
    ({"Producto": "x" * 201}, "too_long", "Producto"),
])
def test_invalid_rows_are_reported_with_code_and_field(tmp_path, override, code, field):
    report = _parse(tmp_path, [_row(**override)])
    assert report.valid == []
    assert any(i.code == code and i.field == field for i in report.invalid), report.invalid


def test_duplicate_references_are_excluded_on_every_occurrence(tmp_path):
    report = _parse(tmp_path, [_row(), _row(**{"Producto": "Otra"})])
    assert report.valid == []
    assert len(report.duplicates) == 2


def test_blank_rows_are_ignored_and_not_counted(tmp_path):
    report = _parse(tmp_path, [_row(), {}, _row(**{"Referencia": "TEST-REF-2"})])
    assert report.rows_total == 2
    assert len(report.valid) == 2


def test_all_or_nothing_by_default_writes_nothing_when_any_row_is_invalid(tmp_path):
    path = _book(tmp_path / "mix.xlsx", [_row(), _row(**{"Referencia": "TEST-REF-2", "Precio (COP)": 0})])
    catalog = FakeCatalog()
    summary = import_catalog_xlsx(path, EXTRACTED, catalog)
    assert summary.written is False
    assert summary.invalid == 1 and summary.valid == 1
    assert catalog.written == []
    assert summary.error_counts["invalid_price"] == 1


def test_allow_partial_writes_only_valid_rows(tmp_path):
    path = _book(tmp_path / "mix.xlsx", [_row(), _row(**{"Referencia": "TEST-REF-2", "Precio (COP)": 0})])
    catalog = FakeCatalog()
    summary = import_catalog_xlsx(path, EXTRACTED, catalog, allow_partial=True)
    assert summary.written is True
    assert [r.reference for r in catalog.written] == ["TEST-REF-1"]


def test_dry_run_without_repository_reports_counts_only(tmp_path):
    path = _book(tmp_path / "ok.xlsx", [_row()])
    summary = import_catalog_xlsx(path, EXTRACTED, None)
    assert summary.valid == 1 and summary.written is False


def test_missing_sheet_is_a_clear_error(tmp_path):
    path = _book(tmp_path / "s.xlsx", [_row()], sheet="Hoja1")
    with pytest.raises(CatalogFileError, match="hoja"):
        read_rows(path)


def test_missing_required_columns_are_a_clear_error(tmp_path):
    path = _book(tmp_path / "c.xlsx", [_row()], headers=["Marca", "Referencia"])
    header, data = read_rows(path)
    with pytest.raises(Exception, match="faltan columnas"):
        parse_rows(header, data, EXTRACTED)


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(CatalogFileError, match="no encontrado"):
        read_rows(tmp_path / "no-existe.xlsx")


def test_source_hash_changes_with_extraction_date_so_new_extractions_update_rows(tmp_path):
    first = _parse(tmp_path, [_row()]).valid[0].source_hash
    path = _book(tmp_path / "again.xlsx", [_row()])
    header, data = read_rows(path)
    second = parse_rows(header, data, date(2026, 11, 1)).valid[0].source_hash
    assert first != second


def test_source_hash_is_stable_for_same_file_and_date(tmp_path):
    a = _parse(tmp_path, [_row()]).valid[0].source_hash
    b = _parse(tmp_path, [_row()]).valid[0].source_hash
    assert a == b


_REAL_CATALOG = os.environ.get("LACUSINE_CATALOG_XLSX")


@pytest.mark.skipif(not _REAL_CATALOG, reason="defina LACUSINE_CATALOG_XLSX para validar el catálogo real")
def test_real_catalog_file_passes_validation_without_printing_products():
    path = Path(_REAL_CATALOG)
    summary = import_catalog_xlsx(path, date(2026, 10, 26), None)
    assert summary.rows_total == 349
    assert summary.invalid == 0 and summary.duplicates == 0
    assert summary.valid == 349
