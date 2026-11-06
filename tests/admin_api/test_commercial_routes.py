"""API comercial: permisos por rol, validaciones y traducción de errores. Fachada simulada: sin base de datos."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_api.auth import SupabaseTokenVerifier
from app.admin_api.commercial import build_commercial_router
from app.repositories.commercial import SaleConflictError, SaleStateError
from app.repositories.errors import StorageError
from app.services.commercial import ForbiddenError, ValidationFailed
from app.services.pdf_report import PdfTooLargeError
from tests.admin_api.test_admin_auth_and_router import SUPABASE_URL, _FakeJwkClient, _auth, _token, rsa_key  # noqa: F401

LEAD = str(uuid.uuid4())
PRODUCT = str(uuid.uuid4())


class FakeFacade:
    def __init__(self, fail=None):
        self.fail = fail
        self.confirmed = []
        self.cancelled = []

    def _maybe_fail(self):
        if self.fail is not None:
            raise self.fail

    def weekly_report(self, day):
        self._maybe_fail()
        return {"semana": {"inicio_bogota": day.isoformat()}, "ventas": {"estado": "sin_datos"}}

    def weekly_pdf(self, kind, day, narrative=False):
        self._maybe_fail()
        return b"%PDF-1.4 sintetico"

    def weekly_csv(self, day):
        self._maybe_fail()
        return "seccion,indicador,valor\nventas,estado,sin_datos\n"

    def trend(self, weeks, today):
        self._maybe_fail()
        return [{"semana_inicio": today.isoformat(), "mensajes_entrantes": 0}] * weeks

    def list_sales(self, limit):
        self._maybe_fail()
        return [{"sale_ref": "VTA-1", "status": "confirmed"}][:limit]

    def confirm_sale(self, principal, body):
        self._maybe_fail()
        self.confirmed.append((principal.subject, body))
        return "created"

    def cancel_sale(self, principal, sale_ref, reason):
        self._maybe_fail()
        self.cancelled.append((principal.subject, sale_ref, reason))


def _app(rsa_key, facade):
    verifier = SupabaseTokenVerifier(SUPABASE_URL, "https://unused.example/jwks",
                                     jwk_client=_FakeJwkClient(rsa_key.public_key()))
    app = FastAPI()
    app.include_router(build_commercial_router(verifier=verifier, facade=facade))
    return TestClient(app)


@pytest.fixture()
def facade():
    return FakeFacade()


@pytest.fixture()
def client(rsa_key, facade):
    return _app(rsa_key, facade)


def _sale_body(**overrides):
    body = {"sale_ref": "VTA-0001", "lead_id": LEAD, "amount_cop": 2990000,
            "sold_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "source": "admin_manual", "evidence_ref": "factura FV-100", "products": {PRODUCT: 1}}
    body.update(overrides)
    return body


# --- Autenticación ---

@pytest.mark.parametrize("path", ["/api/admin/commercial/report", "/api/admin/commercial/report.csv",
                                  "/api/admin/commercial/trend", "/api/admin/commercial/sales"])
def test_anonymous_requests_are_denied(client, path):
    assert client.get(path).status_code == 401


def test_invalid_token_is_denied(client):
    assert client.get("/api/admin/commercial/report", headers=_auth("no-es-un-jwt")).status_code == 401


def test_role_in_user_metadata_grants_nothing(client, rsa_key):
    token = _token(rsa_key, role="admin", location="user")
    assert client.get("/api/admin/commercial/report", headers=_auth(token)).status_code == 403


# --- Lectura: operador y administrador ---

def test_operator_reads_report_trend_and_sales(client, rsa_key):
    headers = _auth(_token(rsa_key, role="operator"))
    assert client.get("/api/admin/commercial/report?day=2026-11-05", headers=headers).status_code == 200
    assert len(client.get("/api/admin/commercial/trend?weeks=3", headers=headers).json()["semanas"]) == 3
    assert client.get("/api/admin/commercial/sales?limit=5", headers=headers).status_code == 200


def test_csv_export_is_an_attachment(client, rsa_key):
    response = client.get("/api/admin/commercial/report.csv?day=2026-11-05",
                          headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]


def test_pdf_is_served_as_an_attachment_with_no_store(client, rsa_key):
    response = client.get("/api/admin/commercial/report.pdf?kind=ejecutivo&day=2026-11-05",
                          headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == 'attachment; filename="la-cuisine-ejecutivo-2026-11-05.pdf"'
    assert response.content.startswith(b"%PDF")


def test_unknown_report_kind_is_rejected(client, rsa_key):
    response = client.get("/api/admin/commercial/report.pdf?kind=../../etc", headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 422


def test_pdf_requires_authentication(client):
    assert client.get("/api/admin/commercial/report.pdf?kind=operativo").status_code == 401


@pytest.mark.parametrize("error, status, detail", [
    (PdfTooLargeError(), 413, "pdf_too_large"),
    (StorageError("x"), 503, "storage_unavailable"),
    (RuntimeError("detalle interno que no debe salir"), 500, "pdf_generation_failed"),
])
def test_pdf_failures_return_stable_codes_without_internal_details(rsa_key, error, status, detail):
    client = _app(rsa_key, FakeFacade(fail=error))
    response = client.get("/api/admin/commercial/report.pdf?kind=operativo", headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == status and response.json()["detail"] == detail
    assert "detalle interno" not in response.text


@pytest.mark.parametrize("query", ["day=1999-01-01", "day=2999-01-01", "day=no-es-fecha"])
def test_invalid_dates_are_rejected(client, rsa_key, query):
    response = client.get(f"/api/admin/commercial/report?{query}", headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 422


def test_trend_length_is_bounded(client, rsa_key):
    response = client.get("/api/admin/commercial/trend?weeks=999", headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 422


# --- Escritura: solo administrador ---

def test_operator_cannot_confirm_a_sale(client, rsa_key, facade):
    response = client.post("/api/admin/commercial/sales", json=_sale_body(),
                           headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 403 and facade.confirmed == []


def test_admin_confirms_a_sale_and_the_responsible_comes_from_the_token(client, rsa_key, facade):
    body = _sale_body(confirmed_by="otra-persona-inventada")  # el cuerpo no puede elegir responsable
    response = client.post("/api/admin/commercial/sales", json=body, headers=_auth(_token(rsa_key, role="admin")))
    assert response.status_code == 201
    subject, sent = facade.confirmed[0]
    assert subject != "otra-persona-inventada"
    assert sent.amount_cop == 2990000


@pytest.mark.parametrize("override", [{"amount_cop": "mucho"}, {"source": "ia"}, {"sold_at": "no-es-fecha"},
                                       {"products": {"no-uuid": 1}}, {"evidence_ref": "x"}])
def test_malformed_sale_is_rejected_before_the_service(client, rsa_key, facade, override):
    response = client.post("/api/admin/commercial/sales", json=_sale_body(**override),
                           headers=_auth(_token(rsa_key, role="admin")))
    assert response.status_code == 422 and facade.confirmed == []


def test_service_validation_codes_are_returned_without_internal_details(rsa_key):
    client = _app(rsa_key, FakeFacade(fail=ValidationFailed("amount_out_of_range")))
    response = client.post("/api/admin/commercial/sales", json=_sale_body(), headers=_auth(_token(rsa_key, role="admin")))
    assert response.status_code == 422 and response.json()["detail"] == "amount_out_of_range"


@pytest.mark.parametrize("error, status", [(SaleConflictError(), 409), (SaleStateError(), 409),
                                           (ForbiddenError(), 403), (StorageError("x"), 503)])
def test_errors_map_to_stable_codes_without_tracebacks(rsa_key, error, status):
    client = _app(rsa_key, FakeFacade(fail=error))
    response = client.post("/api/admin/commercial/sales", json=_sale_body(), headers=_auth(_token(rsa_key, role="admin")))
    assert response.status_code == status
    assert "Traceback" not in response.text


def test_admin_cancels_with_a_reason(client, rsa_key, facade):
    response = client.post("/api/admin/commercial/sales/VTA-0001/cancel", json={"reason": "devuelto antes de entrega"},
                           headers=_auth(_token(rsa_key, role="admin")))
    assert response.status_code == 200
    assert facade.cancelled[0][1] == "VTA-0001"


def test_cancel_without_a_reason_is_rejected(client, rsa_key, facade):
    response = client.post("/api/admin/commercial/sales/VTA-0001/cancel", json={"reason": "no"},
                           headers=_auth(_token(rsa_key, role="admin")))
    assert response.status_code == 422 and facade.cancelled == []


def test_operator_cannot_cancel(client, rsa_key, facade):
    response = client.post("/api/admin/commercial/sales/VTA-0001/cancel", json={"reason": "motivo válido"},
                           headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 403 and facade.cancelled == []


# --- Importes: el frontend los trata como números; Decimal no debe salir como texto ---

def test_money_values_are_json_numbers_not_strings(rsa_key):
    class MoneyFacade(FakeFacade):
        def weekly_report(self, day):
            return {"operacion_ia": {"costo_estimado_usd": Decimal("0.0031"), "costo_por_conversacion_usd": None}}

    client = _app(rsa_key, MoneyFacade())
    response = client.get("/api/admin/commercial/report?day=2026-11-05", headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 200
    costo = response.json()["operacion_ia"]["costo_estimado_usd"]
    assert isinstance(costo, float) and costo == 0.0031
    assert response.json()["operacion_ia"]["costo_por_conversacion_usd"] is None
