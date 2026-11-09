"""Diagnóstico de configuración EFECTIVA de este proceso. Solo lectura. Nunca imprime secretos ni contenido.

Se ejecuta con el mismo entorno que el proceso que se quiere comprobar (p. ej. el de la ventana del lanzador):
    $env:LACUSINE_LOAD_DOTENV="true"; .\\.venv\\Scripts\\python.exe scripts\\diagnostico_runtime.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.core.env import load_if_enabled  # noqa: E402


def _presence(value: str | None) -> str:
    return f"presente(len={len(value)})" if value else "AUSENTE"


def main() -> int:
    load_if_enabled(ROOT / ".env")
    s = Settings.from_env()
    now = int(time.time())
    problems: list[str] = []

    print("== Transporte y ventana")
    print(f"transporte={s.whatsapp_transport}")
    print(f"modo_entrada={s.web_bridge_inbound_mode}")
    print(f"respuestas_automaticas={'si' if s.web_bridge_auto_reply else 'no'}")
    if s.web_bridge_inbound_mode == "web_inbound_all":
        remaining = (s.web_bridge_inbound_until or 0) - now
        print(f"ventana_restante_s={max(remaining, 0)}")
        if not (0 < remaining <= 900):
            problems.append("ventana_universal_invalida")
    print(f"linea_web={'configurada' if s.web_bridge_line_id else 'AUSENTE'}")
    print(f"url_puente={'configurada' if s.web_bridge_url else 'AUSENTE'}")
    print(f"secreto_hmac={_presence(s.web_bridge_secret)}")
    if s.whatsapp_transport == "web_bridge" and len(s.web_bridge_secret or "") < 32:
        problems.append("secreto_hmac_corto")

    print("== Proveedor y worker")
    print(f"llm_provider={s.llm_provider}")
    print(f"anthropic_enabled={s.anthropic_enabled}")
    print(f"anthropic_model={s.anthropic_model}")
    print(f"agent_dry_run={s.agent_dry_run}")
    print(f"demo_enabled={s.demo_enabled}")
    print(f"kill_switch={s.agent_kill_switch}")
    if s.agent_dry_run or (s.llm_provider or "") not in ("anthropic", "openrouter"):
        problems.append("proveedor_no_real")

    print("== Presupuestos")
    print(f"limite_interaccion_usd={s.agent_interaction_limit_usd}")
    print(f"limite_diario_usd={s.agent_daily_budget_usd}")
    print(f"limite_mensual_usd={s.agent_monthly_budget_usd}")
    if s.agent_interaction_limit_usd is not None and s.agent_interaction_limit_usd < 0.02:
        problems.append("limite_interaccion_insuficiente")

    print("== Base de datos")
    print(f"database_url={_presence(s.database_url)}")
    if s.database_url:
        try:
            import psycopg

            with psycopg.connect(s.database_url, autocommit=True, connect_timeout=10,
                                 options="-c default_transaction_read_only=on") as conn:
                conn.execute("select 1").fetchone()
            print("DATABASE: CONNECTED")
        except Exception as exc:  # noqa: BLE001 - solo se muestra el tipo de error
            print(f"DATABASE: UNAVAILABLE (error={type(exc).__name__})")
            problems.append("base_no_disponible")

    print("== Resultado")
    if problems:
        print("INCONSISTENCIAS: " + ", ".join(problems))
        return 1
    print("CONFIGURACION_COHERENTE (este proceso). Esto no verifica el recorrido completo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
