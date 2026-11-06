"""API administrativa de inteligencia comercial. Mismo modelo de autenticación que /api/admin.

Permisos:
  * operator y admin: consultar informes, tendencias, ventas y exportar CSV.
  * admin: confirmar y cancelar ventas. El responsable sale del token (`sub`), nunca del cuerpo de la petición.
PDF: no disponible todavía (requiere aprobar una dependencia). Responde 501 en lugar de inventar un documento.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.admin_api.auth import ROLE_ADMIN, ROLE_OPERATOR, AuthError, Principal, bearer_token
from app.repositories.analytics import PostgresAnalyticsRepository
from app.repositories.commercial import (
    PostgresCommercialRepository,
    SaleConflictError,
    SaleStateError,
)
from app.repositories.errors import StorageError
from app.services.analytics import BOGOTA, local_day, previous_week, week_containing
from app.services.analytics_report import build_weekly_report, report_to_csv_rows
from app.services.commercial import ForbiddenError, SalesService, ValidationFailed
from app.services.analysis_narrative import generate_narrative
from app.services.pdf_report import KINDS, PdfTooLargeError, build_pdf

MIN_DAY = date(2020, 1, 1)
MAX_TREND_WEEKS = 26


class SaleBody(BaseModel):
    sale_ref: str = Field(min_length=3, max_length=100)
    lead_id: UUID
    amount_cop: int
    sold_at: datetime
    source: str = Field(pattern="^(admin_manual|external_order_system)$")
    evidence_ref: str = Field(min_length=3, max_length=300)
    products: dict[UUID, int] = Field(default_factory=dict)


class CancelBody(BaseModel):
    reason: str = Field(min_length=5, max_length=300)


class CommercialFacade:
    """Único punto que toca PostgreSQL para la analítica comercial. Solo lectura, salvo las ventas."""

    def __init__(self, pool, narrative=None) -> None:
        self._pool = pool
        self._narrative = narrative  # (proveedor, tarifas, configuración) o None: sin análisis narrativo
        self._analytics = PostgresAnalyticsRepository(pool)
        self._commercial = PostgresCommercialRepository(pool)

    def weekly_report(self, day: date) -> dict:
        window = week_containing(day)
        counts = self._analytics.weekly_counts(window)
        previous = self._analytics.weekly_counts(previous_week(window))
        return build_weekly_report(counts, window, previous=previous)

    def weekly_pdf(self, kind: str, day: date, narrative: bool = False) -> bytes:
        report = self.weekly_report(day)
        trend = self.trend(6, day)
        text = None
        if narrative and kind == "ejecutivo" and self._narrative is not None:
            provider, pricing, config, ledger, limits = self._narrative
            text = generate_narrative(provider=provider, pricing=pricing, report=report, config=config,
                                      ledger=ledger, limits=limits, report_kind="ejecutivo",
                                      week_start=week_containing(day).start_day,
                                      now=datetime.now(timezone.utc))
        return build_pdf(kind=kind, report=report, generated_at=datetime.now(timezone.utc), trend=trend,
                         narrative=text)

    def weekly_csv(self, day: date) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["seccion", "indicador", "valor"])
        writer.writerows(report_to_csv_rows(self.weekly_report(day)))
        return buffer.getvalue()

    def trend(self, weeks: int, today: date) -> list[dict]:
        out = []
        for back in range(weeks - 1, -1, -1):
            window = week_containing(today - timedelta(days=7 * back))
            counts = self._analytics.weekly_counts(window)
            out.append({
                "semana_inicio": window.start_day.isoformat(),
                "mensajes_entrantes": counts["inbound_messages"],
                "conversaciones_nuevas": counts["conversations_new"],
                "ventas_confirmadas": counts["commercial"]["sales_in_week"][0],
                "ingresos_cop": int(counts["commercial"]["sales_in_week"][1]),
            })
        return out

    def list_sales(self, limit: int) -> list[dict]:
        try:
            with self._pool.connection() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                rows = conn.execute("""
                    select s.sale_ref, s.sold_at, s.amount_cop, s.confirmation_source, s.confirmed_by,
                      (select e.status from public.sale_status_events e where e.sale_id = s.id
                       order by e.occurred_at desc, e.id desc limit 1)
                    from public.sales s order by s.sold_at desc, s.id limit %s
                """, (limit,)).fetchall()
        except Exception:
            raise StorageError("sales_read_failed") from None
        return [{"sale_ref": ref, "sold_at": at.isoformat(), "amount_cop": int(amount), "source": source,
                 "confirmed_by": by, "status": status} for ref, at, amount, source, by, status in rows]

    def confirm_sale(self, principal: Principal, body: SaleBody) -> str:
        service = SalesService(self._commercial)
        return service.confirm(
            role=principal.role, sale_ref=body.sale_ref, lead_id=body.lead_id, amount_cop=body.amount_cop,
            sold_at=body.sold_at, source=body.source, confirmed_by=principal.subject,
            evidence_ref=body.evidence_ref, products=body.products,
        )

    def cancel_sale(self, principal: Principal, sale_ref: str, reason: str) -> None:
        SalesService(self._commercial).cancel(role=principal.role, sale_ref=sale_ref, reason=reason,
                                              actor=principal.subject)


def _check_day(value: date | None, today: date) -> date:
    day = value or local_day(datetime.now(BOGOTA))
    if day < MIN_DAY or day > today + timedelta(days=1):
        raise HTTPException(status_code=422, detail="date_out_of_range")
    return day


def build_commercial_router(*, verifier, facade: CommercialFacade) -> APIRouter:
    router = APIRouter(prefix="/api/admin/commercial", tags=["admin", "commercial"])

    def principal_dep(authorization: str | None = Header(default=None)) -> Principal:
        token = bearer_token(authorization)
        if token is None:
            raise HTTPException(status_code=401, detail="invalid_token")
        try:
            return verifier.principal(token)
        except AuthError as exc:
            raise HTTPException(status_code=401 if exc.code == "invalid_token" else 403, detail=exc.code) from None

    def require(role: str):
        def dependency(principal: Principal = Depends(principal_dep)) -> Principal:
            if not principal.allows(role):
                raise HTTPException(status_code=403, detail="forbidden")
            return principal
        return dependency

    operator_dep = require(ROLE_OPERATOR)
    admin_dep = require(ROLE_ADMIN)

    def guarded(call):
        try:
            return call()
        except ForbiddenError:
            raise HTTPException(status_code=403, detail="forbidden") from None
        except ValidationFailed as exc:
            raise HTTPException(status_code=422, detail=exc.code) from None
        except (SaleConflictError, SaleStateError) as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None
        except StorageError:
            raise HTTPException(status_code=503, detail="storage_unavailable") from None

    @router.get("/report", response_model=None)
    def report(_: Principal = Depends(operator_dep), day: date | None = Query(default=None)) -> dict:
        today = local_day(datetime.now(BOGOTA))
        return guarded(lambda: facade.weekly_report(_check_day(day, today)))

    @router.get("/report.csv")
    def report_csv(_: Principal = Depends(operator_dep), day: date | None = Query(default=None)) -> Response:
        today = local_day(datetime.now(BOGOTA))
        body = guarded(lambda: facade.weekly_csv(_check_day(day, today)))
        return Response(content=body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=informe-semanal.csv"})

    @router.get("/report.pdf")
    def report_pdf(_: Principal = Depends(operator_dep),
                   kind: str = Query(default="operativo", pattern="^(operativo|marketing|oportunidades|ventas|ejecutivo)$"),
                   day: date | None = Query(default=None),
                   narrative: bool = Query(default=False)) -> Response:
        today = local_day(datetime.now(BOGOTA))
        chosen = _check_day(day, today)
        try:
            body = facade.weekly_pdf(kind, chosen, narrative=narrative)
        except PdfTooLargeError:
            raise HTTPException(status_code=413, detail="pdf_too_large") from None
        except StorageError:
            raise HTTPException(status_code=503, detail="storage_unavailable") from None
        except Exception:  # noqa: BLE001 - nunca se devuelve el detalle interno del fallo
            raise HTTPException(status_code=500, detail="pdf_generation_failed") from None
        filename = f"la-cuisine-{kind}-{chosen.isoformat()}.pdf"  # nombre generado: sin entrada libre del cliente
        return Response(content=body, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})

    @router.get("/trend", response_model=None)
    def trend(_: Principal = Depends(operator_dep), weeks: int = Query(default=6, ge=1, le=MAX_TREND_WEEKS)) -> dict:
        today = local_day(datetime.now(BOGOTA))
        return {"semanas": guarded(lambda: facade.trend(weeks, today))}

    @router.get("/sales", response_model=None)
    def sales(_: Principal = Depends(operator_dep), limit: int = Query(default=50, ge=1, le=200)) -> dict:
        return {"items": guarded(lambda: facade.list_sales(limit))}

    @router.post("/sales", status_code=201, response_model=None)
    def confirm(body: SaleBody, principal: Principal = Depends(admin_dep)) -> dict:
        outcome = guarded(lambda: facade.confirm_sale(principal, body))
        return {"status": outcome, "sale_ref": body.sale_ref}

    @router.post("/sales/{sale_ref}/cancel", response_model=None)
    def cancel(sale_ref: str, body: CancelBody, principal: Principal = Depends(admin_dep)) -> dict:
        guarded(lambda: facade.cancel_sale(principal, sale_ref, body.reason))
        return {"status": "cancelled", "sale_ref": sale_ref}

    return router
