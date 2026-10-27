"""Pruebas unitarias del importador de catálogo.

Los datos son FIXTURES SINTÉTICOS de prueba: no son productos ni precios reales de La Cuisine.
"""

import io
from decimal import Decimal

import pytest

from app.repositories.catalog import ProductRecord, UpsertResult
from app.services.catalog_import import CatalogImportError, import_catalog, parse_catalog_csv

HEADER = "referencia,marca,categoria,descripcion,precio_cop,disponibilidad,fecha_disponibilidad,enlace\n"

VALID_ROWS = (
    "TEST-001,MarcaPrueba,Nevera,Fixture sintético 1,1299000,Disponible según fuente,2026-10-27,https://example.com/p/1\n"
    "TEST-002,MarcaPrueba,Horno,Fixture sintético 2,,,,\n"
)


def csv_stream(text: str) -> io.StringIO:
    return io.StringIO(text)


class FakeCatalogRepository:
    """Imita upsert por referencia con hash: solo cuenta cambios reales."""

    def __init__(self) -> None:
        self.rows: dict[str, ProductRecord] = {}
        self.calls = 0

    def upsert_products(self, records: list[ProductRecord]) -> UpsertResult:
        self.calls += 1
        inserted = updated = unchanged = 0
        for record in records:
            current = self.rows.get(record.reference)
            if current is None:
                inserted += 1
            elif current.source_hash != record.source_hash:
                updated += 1
            else:
                unchanged += 1
                continue
            self.rows[record.reference] = record
        return UpsertResult(inserted=inserted, updated=updated, unchanged=unchanged)


def test_valid_file_is_parsed_with_normalized_values():
    result = parse_catalog_csv(csv_stream(HEADER + VALID_ROWS))
    assert result.errors == []
    first, second = result.records
    assert first.reference == "TEST-001"
    assert first.price_cop == Decimal("1299000")
    assert str(first.availability_as_of) == "2026-10-27"
    assert second.price_cop is None
    assert second.availability_label is None
    assert second.product_url is None


def test_headers_are_case_and_space_insensitive():
    text = " Referencia , MARCA ,Categoria\nTEST-010,MarcaPrueba,Nevera\n"
    result = parse_catalog_csv(csv_stream(text))
    assert result.errors == []
    assert result.records[0].reference == "TEST-010"


def test_missing_required_column_is_reported_once():
    result = parse_catalog_csv(csv_stream("referencia,marca\nTEST-1,X\n"))
    assert [(e.field, e.code) for e in result.errors] == [("categoria", "missing_column")]
    assert result.records == []


@pytest.mark.parametrize(
    ("row", "field", "code"),
    [
        ("TEST-E1,M,C,,12a,,,", "precio_cop", "invalid_price"),
        ("TEST-E2,M,C,,1.234.567,,,", "precio_cop", "invalid_price"),
        ("TEST-E3,M,C,,,,2026-13-01,", "fecha_disponibilidad", "invalid_date"),
        ("TEST-E4,M,C,,,,,http://example.com/x", "enlace", "invalid_url"),
        ("TEST-E5,,C,,,,,", "marca", "required"),
    ],
)
def test_invalid_rows_produce_row_level_errors(row, field, code):
    result = parse_catalog_csv(csv_stream(HEADER + row + "\n"))
    assert (field, code) in [(e.field, e.code) for e in result.errors]
    assert result.records == []


def test_price_with_two_decimals_is_accepted():
    result = parse_catalog_csv(csv_stream(HEADER + "TEST-P1,M,C,,1299000.50,,,\n"))
    assert result.errors == []
    assert result.records[0].price_cop == Decimal("1299000.50")


def test_duplicate_reference_in_file_is_an_error():
    result = parse_catalog_csv(csv_stream(HEADER + "TEST-D,M,C,,,,,\nTEST-D,M,C,,,,,\n"))
    assert any(e.code == "duplicate_in_file" for e in result.errors)


def test_overlong_field_is_an_error():
    long_reference = "R" * 65
    result = parse_catalog_csv(csv_stream(HEADER + f"{long_reference},M,C,,,,,\n"))
    assert any(e.field == "referencia" and e.code == "too_long" for e in result.errors)


def test_import_is_all_or_nothing_when_any_row_is_invalid():
    repository = FakeCatalogRepository()
    text = HEADER + VALID_ROWS + "TEST-BAD,M,C,,abc,,,\n"
    with pytest.raises(CatalogImportError) as excinfo:
        import_catalog(csv_stream(text), repository)
    assert len(excinfo.value.errors) == 1
    assert repository.calls == 0
    assert repository.rows == {}


def test_reimporting_the_same_file_changes_nothing():
    repository = FakeCatalogRepository()
    first = import_catalog(csv_stream(HEADER + VALID_ROWS), repository)
    second = import_catalog(csv_stream(HEADER + VALID_ROWS), repository)
    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert len(repository.rows) == 2


def test_changed_row_is_updated_not_duplicated():
    repository = FakeCatalogRepository()
    import_catalog(csv_stream(HEADER + VALID_ROWS), repository)
    changed = HEADER + VALID_ROWS.replace("1299000", "1350000")
    report = import_catalog(csv_stream(changed), repository)
    assert (report.inserted, report.updated, report.unchanged) == (0, 1, 1)
    assert len(repository.rows) == 2
    assert repository.rows["TEST-001"].price_cop == Decimal("1350000")


def test_empty_catalog_file_imports_nothing_without_calling_repository():
    repository = FakeCatalogRepository()
    report = import_catalog(csv_stream(HEADER), repository)
    assert report.rows_read == 0
    assert repository.calls == 0


def test_source_hash_changes_when_any_field_changes():
    base = parse_catalog_csv(csv_stream(HEADER + "TEST-H,M,C,desc a,,,,\n")).records[0]
    changed = parse_catalog_csv(csv_stream(HEADER + "TEST-H,M,C,desc b,,,,\n")).records[0]
    assert base.source_hash != changed.source_hash
