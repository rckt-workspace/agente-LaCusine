"""Invariantes del modo de observación de recepción (scripts/diagnostico_recepcion_web.ps1).

Estáticas: el script nunca arranca backend ni worker, nunca borra .auth ni STOP, nunca habilita respuestas.
Ejecutables: el resumen de un log se calcula con PowerShell real (solo si hay PowerShell en Windows).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnostico_recepcion_web.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def _code() -> str:
    """Solo líneas de código (sin comentarios de cabecera)."""
    return "\n".join(line for line in _text().splitlines() if not line.lstrip().startswith("#"))


def test_the_script_starts_only_the_connector_and_never_the_backend_or_worker():
    code = _code()
    for forbidden in ("uvicorn", "app.worker", "app.main", "worker.main", "-Go", "-AutoReply"):
        assert forbidden not in code, forbidden
    assert "node dist/index.js" in code


def test_observation_forces_no_replies_and_no_forwarding():
    code = _code()
    assert 'WEB_BRIDGE_AUTO_REPLY    = "false"' in code
    assert 'WEB_BRIDGE_OBSERVE_ONLY  = "true"' in code


def test_the_script_never_deletes_auth_stop_env_or_credentials():
    code = _code()
    for forbidden in ("Remove-Item", "rmSync", "unlink", ".auth", "Set-Content", ".env"):
        assert forbidden not in code, forbidden
    assert "NUNCA se elimina" in _text()  # la garantía queda escrita en la cabecera


def test_stop_is_created_when_missing_and_kept_otherwise():
    code = _code()
    assert "New-Item -ItemType File -Path $stopFile" in code
    assert "(se conserva)" in code


def test_a_single_connector_is_enforced_before_starting():
    code = _code()
    assert "Get-ConnectorProcesses" in code
    assert "observe.lock" in code
    assert "Test-PortFree $bridgePort" in code


def test_the_window_is_bounded_to_fifteen_minutes():
    code = _code()
    assert "$Segundos -gt 900" in code and "$Segundos -lt 60" in code


@pytest.mark.skipif(PS is None, reason="sin PowerShell en este equipo")
def test_the_script_parses_in_powershell():
    cmd = ("$e=$null;$t=$null;[System.Management.Automation.Language.Parser]::ParseFile("
           f"'{SCRIPT}',[ref]$t,[ref]$e)|Out-Null; if($e){{$e|%{{$_.Message}}}}else{{'PARSE_OK'}}")
    out = subprocess.run([PS, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120).stdout
    assert "PARSE_OK" in out


@pytest.mark.skipif(PS is None, reason="sin PowerShell en este equipo")
def test_the_summary_counts_events_without_printing_content(tmp_path):
    log = tmp_path / "observe-test.log"
    lines = [
        '{"level":30,"msg":"stage=observe state=started hasta_unix=1 reenvio=bloqueado envio=bloqueado"}',
        '{"level":30,"msg":"stage=00_raw_upsert type=notify count=1"}',
        '{"level":30,"msg":"stage=01_classified cid=ab12cd34ef decision=ignore reason=stop_active"}',
        '{"level":30,"msg":"stage=00_raw_upsert type=append count=3"}',
        '{"level":30,"msg":"stage=01_classified cid=11aa22bb33 decision=ignore reason=history_sync"}',
        '{"level":30,"msg":"stage=04_forward_skipped reason=observe_only items=1"}',
        '{"level":50,"msg":"stage=01_handler_error error=TypeError"}',
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = subprocess.run([PS, "-NoProfile", "-File", str(SCRIPT), "-Resumen", "-Log", str(log)],
                         capture_output=True, text=True, timeout=120).stdout
    # La consola de PowerShell usa otra codificación: las comprobaciones se hacen con anclas ASCII.
    assert "eventos brutos (stage 00): 2" in out
    assert "type=notify: eventos=1 mensajes=1" in out
    assert "type=append: eventos=1 mensajes=3" in out
    assert "stop_active: 1" in out
    assert "observe_only: 1" in out
    assert re.search(r"backend \(debe ser 0 en observ\S+\): 0", out)
    assert "errores del manejador: 1" in out
    assert "cid=" not in out and '"msg"' not in out  # no se imprime contenido de línea
