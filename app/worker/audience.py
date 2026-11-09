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
from app.integrations.whatsapp_line import digits_only, is_meta_test_number

MODE_TEST_ALLOWLIST = "test_allowlist"
MODE_PUBLIC = "public"
# Atención pública por WhatsApp Web (no oficial): solo con web_bridge, modo de entrada declarado y confirmación exacta.
MODE_PUBLIC_INBOUND = "public_inbound"
KNOWN_MODES = frozenset({MODE_TEST_ALLOWLIST, MODE_PUBLIC, MODE_PUBLIC_INBOUND})
PUBLIC_CONFIRMATION_TOKEN = "ABRIR_ATENCION_PUBLICA_CONFIRMADA"
REAL_PROVIDERS = frozenset({"anthropic", "openrouter"})


def web_inbound_problems(settings: Settings) -> list[str]:
    """Incoherencias del alcance de entrada del transporte web. Lista vacía = correcto (incluido el modo de un chat)."""
    from app.integrations.web_bridge import INBOUND_MODE_ALL, INBOUND_MODE_SINGLE, inbound_window_state

    from app.integrations.web_bridge import INBOUND_MODE_PUBLIC

    mode = settings.web_bridge_inbound_mode
    if mode not in (INBOUND_MODE_SINGLE, INBOUND_MODE_ALL, INBOUND_MODE_PUBLIC):
        return ["inbound_mode_unknown"]
    if mode == INBOUND_MODE_SINGLE:
        return []
    if mode == INBOUND_MODE_PUBLIC:
        # Sus condiciones (audiencia, confirmación, respuestas) se comprueban en public_inbound_problems.
        return []
    state = inbound_window_state(mode, settings.web_bridge_inbound_until)
    if state != "active":
        return [f"inbound_all_window_{state}"]
    if not settings.web_bridge_auto_reply:
        # El modo universal solo tiene sentido con respuestas autorizadas: evita una ventana sin envíos.
        return ["inbound_all_requires_auto_reply"]
    return []


def public_inbound_problems(settings: Settings) -> list[str]:
    """Atención pública por WhatsApp Web: todas las condiciones deben cumplirse a la vez. Lista vacía = correcto."""
    from app.integrations.web_bridge import INBOUND_MODE_PUBLIC, PUBLIC_INBOUND_CONFIRMATION

    if settings.whatsapp_transport != "web_bridge":
        return ["public_inbound_requires_web_bridge"]
    problems: list[str] = []
    if settings.web_bridge_inbound_mode != INBOUND_MODE_PUBLIC:
        problems.append("public_inbound_mode_not_declared")
    if settings.web_bridge_public_confirmation != PUBLIC_INBOUND_CONFIRMATION:
        problems.append("public_inbound_not_confirmed")
    if not settings.web_bridge_auto_reply:
        problems.append("public_inbound_requires_auto_reply")
    if not (settings.web_bridge_url and settings.web_bridge_secret and settings.web_bridge_line_id):
        problems.append("public_inbound_requires_bridge_config")
    if settings.web_bridge_inbound_until is not None:
        # La atención pública no tiene ventana: una fecha de caducidad indicaría una configuración de piloto.
        problems.append("public_inbound_must_not_have_window")
    return problems


def audience_problems(settings: Settings) -> list[str]:
    """Incoherencias que impiden arrancar con la audiencia configurada. Lista vacía = correcto."""
    mode = settings.whatsapp_audience_mode
    if mode not in KNOWN_MODES:
        return ["audience_mode_unknown"]
    if settings.whatsapp_transport not in ("cloud_api", "web_bridge"):
        return ["transport_unknown"]
    if mode == MODE_PUBLIC and settings.whatsapp_transport == "web_bridge":
        # El transporte de WhatsApp Web es experimental y no oficial: nunca atiende al público.
        return ["public_mode_refused_on_web_bridge"]
    if mode == MODE_PUBLIC_INBOUND:
        return public_inbound_problems(settings)
    if settings.whatsapp_transport == "web_bridge":
        problems = web_inbound_problems(settings)
        if problems:
            return problems
        if settings.web_bridge_inbound_mode == "public_inbound":
            # El modo público de entrada no puede usarse con la audiencia de prueba: deben coincidir.
            return ["public_inbound_requires_public_inbound_audience"]
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
    # El modo público no usa WHATSAPP_ALLOWED_RECIPIENTS: la identidad de la línea y la ventana de 24 horas sí aplican.
    expected = settings.whatsapp_expected_display_number
    if not digits_only(expected):
        problems.append("public_mode_requires_expected_display_number")
    elif is_meta_test_number(expected):
        problems.append("public_mode_refuses_meta_test_number")
    if not settings.meta_send_enabled or settings.whatsapp_dry_run:
        problems.append("public_mode_requires_send_enabled")
    if settings.agent_dry_run or (settings.llm_provider or "") not in REAL_PROVIDERS:
        # Sin proveedor real, las respuestas serían texto de prueba enviado a clientes reales.
        problems.append("public_mode_requires_real_agent")
    return problems
