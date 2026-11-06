"""Ensamblado del panel. Devuelve None si falta la base o la configuración de Supabase: el panel no existe entonces.

El panel (React compilado) se sirve desde /admin. Los archivos estáticos no contienen secretos: solo la clave
publicable de Supabase, que está pensada para el navegador. Las rutas de datos viven bajo /api/admin.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from app.admin.service import AdminService
from app.admin_api.auth import SupabaseTokenVerifier
from app.admin_api.queries import AdminReadModel
from app.admin_api.commercial import CommercialFacade, build_commercial_router
from app.services.analysis_budget import AnalysisLimits, PostgresAnalysisBudgetLedger
from app.services.analysis_narrative import NarrativeConfig
from app.services.cost_tracker import Pricing
from app.admin_api.router import build_admin_router
from app.core.config import Settings

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def build_admin_panel(settings: Settings, pool, dist_dir: Path = FRONTEND_DIST):
    if pool is None or not settings.supabase_url or not settings.supabase_jwks_url:
        return None
    verifier = SupabaseTokenVerifier(settings.supabase_url, settings.supabase_jwks_url)
    api = build_admin_router(
        verifier=verifier,
        reads=AdminReadModel(pool),
        service_factory=lambda ctx: AdminService(pool, ctx),
        environment=os.environ.get("APP_ENV", "unknown"),
        daily_budget_usd=settings.agent_daily_budget_usd,
        monthly_budget_usd=settings.agent_monthly_budget_usd,
    )
    combined = APIRouter()
    combined.include_router(api)
    combined.include_router(build_commercial_router(verifier=verifier,
                                                    facade=CommercialFacade(pool, narrative=_narrative(settings, pool))))
    combined.include_router(_static_router(dist_dir))
    return combined


def _narrative(settings: Settings, pool):
    """Análisis narrativo solo si está habilitado y tiene proveedor, tarifas del mismo modelo y libro de presupuesto."""
    if not settings.analysis_ai_enabled or not settings.anthropic_api_key or pool is None:
        return None
    pricing = Pricing.for_anthropic(settings, settings.anthropic_model)
    if pricing is None:
        return None
    # Importación diferida: el SDK de IA solo se carga si el análisis narrativo está habilitado.
    from app.integrations.anthropic_client import AnthropicAgentClient

    provider = AnthropicAgentClient(api_key=settings.anthropic_api_key, model=settings.anthropic_model,
                                    timeout_seconds=settings.analysis_ai_timeout_seconds, max_retries=0,
                                    max_concurrency=1)
    config = NarrativeConfig(enabled=True, max_output_tokens=settings.analysis_ai_max_output_tokens,
                             budget_usd_per_report=settings.analysis_ai_budget_usd_per_report,
                             timeout_seconds=settings.analysis_ai_timeout_seconds)
    limits = AnalysisLimits(per_report_usd=settings.analysis_ai_budget_usd_per_report,
                            daily_usd=settings.analysis_ai_daily_budget_usd,
                            monthly_usd=settings.analysis_ai_monthly_budget_usd)
    return provider, pricing, config, PostgresAnalysisBudgetLedger(pool), limits


def _static_router(dist_dir: Path) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    root = dist_dir.resolve()
    index = root / "index.html"

    @router.get("/admin")
    @router.get("/admin/{path:path}")
    def serve(path: str = "") -> object:
        if path:
            target = (root / path).resolve()
            # Nunca salir del directorio compilado: la comprobación se hace sobre la ruta resuelta.
            if target.is_file() and root in target.parents:
                return FileResponse(target)
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "panel_not_built"}, status_code=404)

    return router
