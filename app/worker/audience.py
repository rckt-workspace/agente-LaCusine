"""Política de audiencia de envío: test_allowlist (por defecto) o public (solo con confirmación explícita).

Función pura: no abre conexiones. El arranque del worker se rechaza ante cualquier incoherencia.

test_allowlist: solo se responde a WHATSAPP_ALLOWED_RECIPIENTS. Es el modo de pruebas y no cambia.
public: se responde a cualquier contacto que haya escrito primero a la línea registrada. No es una lista vacía:
        es un modo explícito que exige configuración validada de producción. Siguen vigentes la ventana de 24 horas,
        la firma del webhook, la validación de línea, la idempotencia, el control humano y el presupuesto.
        No envía mensajes iniciados por la empresa (sin mensaje entrante no hay ventana, y el dispatcher lo bloquea).
"""

from __future__ import annotations

from app.core.config import Settings

MODE_TEST_ALLOWLIST = "test_allowlist"
MODE_PUBLIC = "public"
KNOWN_MODES = frozenset({MODE_TEST_ALLOWLIST, MODE_PUBLIC})
PUBLIC_CONFIRMATION_TOKEN = "ABRIR_ATENCION_PUBLICA_CONFIRMADA"
REAL_PROVIDERS = frozenset({"anthropic", "openrouter"})


def audience_problems(settings: Settings) -> list[str]:
    """Incoherencias que impiden arrancar con la audiencia configurada. Lista vacía = correcto."""
    mode = settings.whatsapp_audience_mode
    if mode not in KNOWN_MODES:
        return ["audience_mode_unknown"]
    if mode == MODE_TEST_ALLOWLIST:
        return []

    problems: list[str] = []
    # Activación explícita: cualquier otro valor (incluido un valor vacío) no abre la atención pública.
    if settings.whatsapp_public_confirmation != PUBLIC_CONFIRMATION_TOKEN:
        problems.append("public_mode_not_confirmed")
    if settings.demo_enabled:
        problems.append("public_mode_with_demo_refused")
    if not settings.meta_app_secret:
        problems.append("public_mode_requires_app_secret")
    if not settings.database_url:
        problems.append("public_mode_requires_database")
    if not settings.meta_send_enabled or settings.whatsapp_dry_run:
        problems.append("public_mode_requires_send_enabled")
    if settings.agent_dry_run or (settings.llm_provider or "") not in REAL_PROVIDERS:
        # Sin proveedor real, las respuestas serían texto de prueba enviado a clientes reales.
        problems.append("public_mode_requires_real_agent")
    return problems
