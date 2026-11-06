"""Preflight del piloto de La Cuisine. Solo lectura: no llama a Anthropic, no envía nada a WhatsApp.

Uso (desde la raíz del repo, con las variables del piloto ya definidas en el proceso):
    .venv\\Scripts\\python.exe scripts\\pilot_preflight.py --expect-model claude-sonnet-5-5

Sale con código 0 solo si la configuración es la de un piloto con Anthropic real y no hay trabajo pendiente o incierto.
Nunca imprime claves, contraseñas, tokens, números de clientes ni contenido de mensajes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.factory import requires_real_provider, select_provider, worker_profile_problems  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.env import load_if_enabled  # noqa: E402
from app.worker.config import WorkerSettings  # noqa: E402
from app.worker.profile import describe_worker_profile, format_profile  # noqa: E402


def _check(label: str, ok: bool, failures: list[str]) -> None:
    print(f"[{'OK' if ok else 'FALLO'}] {label}")
    if not ok:
        failures.append(label)


def read_line_identity(token: str, phone_id: str, graph_version: str, client=None) -> tuple[bool, dict]:
    """Identidad de la línea (solo lectura, sin enviar). Devuelve el modo de cuenta y la verificación.

    `account_mode` LIVE + `code_verification_status` VERIFIED identifican una línea empresarial real.
    Un número de pruebas de Meta no cumple esa combinación. Nunca devuelve el token.
    """
    import httpx

    http = client or httpx.Client(timeout=15)
    fields = "id,account_mode,code_verification_status,status"
    try:
        response = http.get(f"https://graph.facebook.com/{graph_version}/{phone_id}", params={"fields": fields},
                            headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return False, {"error": f"red:{type(exc).__name__}"}
    if response.status_code != 200:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        return False, {"error": f"http={response.status_code} codigo={error.get('code', '-')} "
                                f"subcodigo={error.get('error_subcode', '-')}"}
    data = response.json()
    return True, {"account_mode": data.get("account_mode"),
                  "verified": data.get("code_verification_status"),
                  "status": data.get("status")}


def is_production_line(identity: dict) -> bool:
    return identity.get("account_mode") == "LIVE" and identity.get("verified") == "VERIFIED"         and identity.get("status") == "CONNECTED"


def check_meta_token(token: str, phone_id: str, graph_version: str, client=None) -> tuple[bool, str]:
    """Lectura del número de negocio con el token. No envía mensajes. Nunca devuelve el token."""
    valid, identity = read_line_identity(token, phone_id, graph_version, client=client)
    if valid:
        return True, "vigente"
    return False, identity.get("error", "error")
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}

# Clasificación de la cola. Solo CRÍTICO impide el arranque.
# CRÍTICO: puede enviar, cobrar o duplicar algo ahora mismo, o implica un segundo worker.
CRITICAL_QUEUE = (
    "eventos_con_lease_vigente",           # otro proceso procesa un evento ahora mismo: no arrancar otro worker
    "reservas_presupuesto_sin_liquidar",   # gasto reservado o incierto: requiere conciliación humana
    "respuestas_en_envio",                 # envío en curso o lease sin resolver
    "respuestas_inciertas",                # Meta pudo haber aceptado: nunca reenviar sin revisión
    "respuestas_pendientes",               # respuestas listas para enviar: revisar antes de abrir el canal
    "llamadas_ia_con_fallo_incierto",      # llamadas facturables de resultado desconocido
)
# RECUPERABLE: el worker lo resuelve al arrancar con recover_expired(). No bloquea.
RECOVERABLE_QUEUE = ("eventos_con_lease_vencido",)
# PENDIENTE NORMAL: el worker lo procesará. No bloquea.
PENDING_QUEUE = ("eventos_pendientes_de_procesar",)
# HISTÓRICO: incidencias terminales o aisladas por contacto. Se muestran, no bloquean el servicio global.
HISTORICAL_QUEUE = (
    "eventos_muertos",                      # dead_letter terminal (p. ej. missing_sender)
    "eventos_revision_manual_con_contacto", # bloquea SOLO la conversación de ese contacto
    "eventos_revision_manual_sin_contacto", # eventos de estado sin contacto: no afectan a clientes
    "respuestas_fallidas_historicas",       # respuestas terminales fallidas: no se reenvían
)

PENDING_QUERIES = {
    "eventos_con_lease_vigente": (
        "select count(*) from public.webhook_events "
        "where processing_status = 'processing' and lease_expires_at > now()"
    ),
    "eventos_con_lease_vencido": (
        "select count(*) from public.webhook_events "
        "where processing_status = 'processing' and lease_expires_at <= now()"
    ),
    "eventos_pendientes_de_procesar": (
        "select count(*) from public.webhook_events where processing_status in ('received', 'queued', 'retry')"
    ),
    "eventos_muertos": "select count(*) from public.webhook_events where processing_status = 'dead_letter'",
    "eventos_revision_manual_con_contacto": (
        "select count(*) from public.webhook_events "
        "where processing_status = 'manual_review' and contact_wa_id is not null"
    ),
    "eventos_revision_manual_sin_contacto": (
        "select count(*) from public.webhook_events "
        "where processing_status = 'manual_review' and contact_wa_id is null"
    ),
    "respuestas_pendientes": "select count(*) from public.outbound_messages where delivery_status = 'pending'",
    "respuestas_en_envio": "select count(*) from public.outbound_messages where delivery_status = 'sending'",
    "respuestas_inciertas": "select count(*) from public.outbound_messages where delivery_status = 'uncertain'",
    "reservas_presupuesto_sin_liquidar": (
        "select count(*) from public.budget_reservations where status in ('reserved', 'uncertain')"
    ),
    "llamadas_ia_con_fallo_incierto": (
        "select count(*) from public.llm_usage_events where status = 'failed' and error_code in "
        "('timeout', 'unavailable', 'provider_error')"
    ),
    "respuestas_fallidas_historicas": "select count(*) from public.outbound_messages where delivery_status = 'failed'",
}


def evaluate_pending(counts: dict[str, int]) -> tuple[list[str], list[str]]:
    """Función pura. Devuelve (bloqueos críticos, notas no bloqueantes). Nunca modifica nada."""
    blocking = [f"{name}={counts.get(name, 0)}" for name in CRITICAL_QUEUE if counts.get(name, 0) != 0]
    notes = [f"{name}={counts.get(name, 0)}"
             for name in RECOVERABLE_QUEUE + PENDING_QUEUE + HISTORICAL_QUEUE if counts.get(name, 0) != 0]
    return blocking, notes


def _pending_work(database_url: str) -> dict[str, int]:
    """Conteos de trabajo pendiente o incierto. Solo SELECT, sesión de solo lectura."""
    import psycopg

    counts: dict[str, int] = {}
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        for name, sql in PENDING_QUERIES.items():
            counts[name] = int(conn.execute(sql).fetchone()[0])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-model", required=True)
    parser.add_argument("--skip-db", action="store_true", help="Omite la comprobación de pendientes (solo configuración).")
    args = parser.parse_args()

    load_if_enabled(ROOT / ".env")  # nunca sobrescribe variables ya definidas en el proceso
    settings = Settings.from_env()
    failures: list[str] = []

    print("== Configuración efectiva (sin secretos)")
    _check("LLM_PROVIDER=anthropic", settings.llm_provider == "anthropic", failures)
    _check("AGENT_DRY_RUN=false", settings.agent_dry_run is False, failures)
    _check("ANTHROPIC_ENABLED=true", settings.anthropic_enabled is True, failures)
    _check(f"ANTHROPIC_MODEL={args.expect_model}", settings.anthropic_model == args.expect_model, failures)
    _check("ANTHROPIC_API_KEY presente", bool(settings.anthropic_api_key), failures)
    _check("Tarifas de entrada y salida presentes",
           settings.price_input_usd_per_mtok is not None and settings.price_output_usd_per_mtok is not None, failures)
    _check("Presupuesto diario o mensual configurado",
           settings.agent_daily_budget_usd is not None or settings.agent_monthly_budget_usd is not None, failures)
    _check("Kill switch desactivado", settings.agent_kill_switch is False, failures)
    _check("Envío a Meta habilitado (META_SEND_ENABLED=true)", settings.meta_send_enabled is True, failures)
    _check("WHATSAPP_DRY_RUN=false", settings.whatsapp_dry_run is False, failures)
    _check("Lista blanca de destinatarios no vacía", len(settings.whatsapp_allowed_recipients) > 0, failures)
    print(f"destinatarios_autorizados={len(settings.whatsapp_allowed_recipients)} audiencia={settings.whatsapp_audience_mode}")
    _check("Sin simulador con envío activo", not worker_profile_problems(settings, real_sender=True), failures)
    _check("Se exige proveedor real", requires_real_provider(settings), failures)

    if settings.meta_access_token and settings.meta_phone_number_id:
        valid, detail = check_meta_token(settings.meta_access_token, settings.meta_phone_number_id,
                                         settings.meta_graph_api_version or "v26.0")
        _check(f"Token de Meta vigente (lectura, sin envio): {detail}", valid, failures)
        if valid:
            _, identity = read_line_identity(settings.meta_access_token, settings.meta_phone_number_id,
                                             settings.meta_graph_api_version or "v26.0")
            print(f"identidad_linea: modo_cuenta={identity.get('account_mode')} "
                  f"verificacion={identity.get('verified')} estado={identity.get('status')}")
            if settings.whatsapp_audience_mode == "public":
                # El modo público nunca se activa sobre un número virtual de pruebas.
                _check("Modo público: línea LIVE, VERIFIED y CONNECTED (no es número de pruebas)",
                       is_production_line(identity), failures)
    else:
        _check("Token de Meta y número de negocio presentes", False, failures)

    provider = select_provider(settings)
    _check("Proveedor construido sin error (clave, modelo, tarifas, presupuesto)", provider is not None, failures)
    if provider is not None:
        _check("Proveedor efectivo = anthropic", provider.name == "anthropic", failures)
        _check(f"Modelo efectivo = {args.expect_model}", provider.model == args.expect_model, failures)

    worker_settings = WorkerSettings.from_env()
    fields = describe_worker_profile(settings, worker_settings, provider)
    print("== Perfil que registrará el worker")
    print(format_profile(fields))

    if not args.skip_db and settings.database_url:
        print("== Cola de trabajo (solo lectura). Solo CRÍTICO bloquea el arranque.")
        counts = _pending_work(settings.database_url)
        for name in CRITICAL_QUEUE:
            _check(f"[CRÍTICO] {name} = {counts.get(name, 0)}", counts.get(name, 0) == 0, failures)
        for name in RECOVERABLE_QUEUE:
            print(f"[RECUPERABLE] {name}={counts.get(name, 0)}: el worker lo recupera al arrancar (recover_expired).")
        for name in PENDING_QUEUE:
            print(f"[PENDIENTE] {name}={counts.get(name, 0)}: se procesará al arrancar.")
        for name in HISTORICAL_QUEUE:
            value = counts.get(name, 0)
            if value:
                print(f"[HISTÓRICO] {name}={value}: no bloquea el servicio global.")
            else:
                print(f"[OK] {name}=0")

    print("== Resultado")
    if failures:
        print(f"RECHAZADO: {len(failures)} comprobación(es) fallida(s). No arrancar el piloto.")
        return 1
    print("LISTO para un único worker con Anthropic real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
