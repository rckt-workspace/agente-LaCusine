"""Rutas /api/admin del panel. Todas exigen JWT válido; las escrituras además exigen rol y dejan auditoría.

Permisos:
  * operator y admin: lectura, pausa de la IA, asignación y cierre de solicitudes de asesor.
  * admin: reanudar la automatización (decisión de mayor impacto).

Errores: códigos estables; nunca se devuelve texto de excepciones ni datos de la base.
"""

from __future__ import annotations

from typing import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.admin.policy import AdminPolicyError
from app.admin.service import AdminContext, AdminService
from app.admin_api.auth import ROLE_ADMIN, ROLE_OPERATOR, AuthError, Principal, bearer_token
from app.admin_api.queries import AdminReadModel
from app.repositories.errors import StorageError

CONFLICT_CODES = frozenset({"transition_not_allowed", "state_changed_concurrently"})
LEAD_STATUSES = ("new", "qualifying", "qualified", "quoted", "handed_off", "won", "lost", "opted_out")
CONVERSATION_STATES = ("automated", "paused_for_human", "disabled")


class ReasonBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class AssignBody(ReasonBody):
    advisor: str = Field(min_length=1, max_length=120)


def build_admin_router(
    *,
    verifier,
    reads: AdminReadModel,
    service_factory: Callable[[AdminContext], AdminService],
    environment: str,
    daily_budget_usd=None,
    monthly_budget_usd=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

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

    def operator_dep(principal: Principal = Depends(require(ROLE_OPERATOR))) -> Principal:
        return principal

    def admin_dep(principal: Principal = Depends(require(ROLE_ADMIN))) -> Principal:
        return principal

    def write(principal: Principal, action: Callable[[AdminService], None]) -> dict[str, str]:
        service = service_factory(AdminContext(operator=principal.subject, environment=environment))
        try:
            action(service)
        except AdminPolicyError as exc:
            status = 409 if exc.code in CONFLICT_CODES else 422
            raise HTTPException(status_code=status, detail=exc.code) from None
        except StorageError:
            raise HTTPException(status_code=503, detail="storage_unavailable") from None
        return {"status": "ok"}

    def read(call):
        try:
            return call()
        except StorageError:
            raise HTTPException(status_code=503, detail="storage_unavailable") from None

    # --- Lectura ---------------------------------------------------------------

    @router.get("/me")
    def me(principal: Principal = Depends(principal_dep)) -> dict[str, str]:
        return {"role": principal.role}

    @router.get("/dashboard")
    def dashboard(_: Principal = Depends(operator_dep)) -> dict:
        return read(lambda: reads.dashboard(daily_budget_usd, monthly_budget_usd))

    @router.get("/conversations")
    def conversations(
        _: Principal = Depends(operator_dep),
        state: str | None = Query(default=None, pattern="^(automated|paused_for_human|disabled)$"),
        status: str | None = Query(default=None, pattern="^(active|waiting_customer|closed)$"),
        q: str | None = Query(default=None, max_length=80),
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> dict:
        return read(lambda: reads.list_conversations(state=state, status=status, q=q, limit=limit, offset=offset))

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: UUID, _: Principal = Depends(operator_dep)) -> dict:
        found = read(lambda: reads.get_conversation(conversation_id))
        if found is None:
            raise HTTPException(status_code=404, detail="not_found")
        return found

    @router.get("/leads")
    def leads(
        _: Principal = Depends(operator_dep),
        status: str | None = Query(default=None, pattern="^(" + "|".join(LEAD_STATUSES) + ")$"),
        q: str | None = Query(default=None, max_length=80),
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> dict:
        return read(lambda: reads.list_leads(status=status, q=q, limit=limit, offset=offset))

    @router.get("/handoffs")
    def handoffs(
        _: Principal = Depends(operator_dep),
        status: str | None = Query(default=None, pattern="^(open|assigned|resolved|cancelled)$"),
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> dict:
        return read(lambda: reads.list_handoffs(status=status, limit=limit, offset=offset))

    @router.get("/products")
    def products(
        _: Principal = Depends(operator_dep),
        q: str | None = Query(default=None, max_length=80),
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> dict:
        return read(lambda: reads.list_products(q=q, limit=limit, offset=offset))

    @router.get("/analytics")
    def analytics(_: Principal = Depends(operator_dep), days: int = Query(default=7, ge=1, le=90)) -> dict:
        return read(lambda: reads.analytics(days))

    # --- Escrituras auditadas --------------------------------------------------

    @router.post("/conversations/{conversation_id}/pause")
    def pause(conversation_id: UUID, body: ReasonBody, principal: Principal = Depends(operator_dep)) -> dict:
        return write(principal, lambda s: s.pause_conversation(conversation_id, body.reason))

    @router.post("/conversations/{conversation_id}/resume")
    def resume(conversation_id: UUID, body: ReasonBody, principal: Principal = Depends(admin_dep)) -> dict:
        return write(principal, lambda s: s.resume_conversation(conversation_id, body.reason))

    @router.post("/handoffs/{handoff_id}/assign")
    def assign(handoff_id: UUID, body: AssignBody, principal: Principal = Depends(operator_dep)) -> dict:
        return write(principal, lambda s: s.resolve_handoff(handoff_id, "assigned", body.reason, body.advisor))

    @router.post("/handoffs/{handoff_id}/resolve")
    def resolve(handoff_id: UUID, body: ReasonBody, principal: Principal = Depends(operator_dep)) -> dict:
        return write(principal, lambda s: s.resolve_handoff(handoff_id, "resolved", body.reason))

    return router
