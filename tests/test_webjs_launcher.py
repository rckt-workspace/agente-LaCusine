"""Invariantes del procedimiento único del canal whatsapp-web.js (scripts/whatsapp_webjs.ps1) y del conector.

Estáticas: nunca desvincula, nunca borra la sesión ni .env, el piloto exige autorización explícita y STOP.
Ejecutables: el lanzador parsea en PowerShell real (solo en Windows).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "whatsapp_webjs.ps1"
CONNECTOR_SRC = ROOT / "connector" / "whatsapp-webjs" / "src"
PS = shutil.which("powershell") or shutil.which("pwsh")


def _launcher_code() -> str:
    text = LAUNCHER.read_text(encoding="utf-8-sig")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _connector_code() -> str:
    parts = []
    for path in sorted(CONNECTOR_SRC.glob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        parts.append("\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("//")))
    return "\n".join(parts)


def test_the_launcher_never_unlinks_deletes_sessions_or_touches_env():
    code = _launcher_code()
    for forbidden in ("Remove-Item", "rmSync", "/unlink", "logout", ".env\"", "Set-Content", "cloud_api"):
        assert forbidden not in code, forbidden


def test_the_connector_never_logs_out_or_deletes_the_session():
    # El cierre destruye el navegador, no la vinculación. Solo el cerrojo propio puede borrarse con unlinkSync.
    code = _connector_code()
    for forbidden in ("logout(", "rmSync", "rmdirSync", "unlink("):
        assert forbidden not in code, forbidden
    assert ".destroy()" in code
    for path in sorted(CONNECTOR_SRC.glob("*.ts")):
        if path.name in ("lock.ts",) or path.name.endswith(".test.ts"):
            continue
        assert "unlinkSync" not in path.read_text(encoding="utf-8"), path.name


def test_the_pilot_requires_explicit_authorization_and_respects_stop():
    code = _launcher_code()
    assert "if ($Piloto) {" in code
    assert 'throw "El piloto requiere -AutoReply' in code
    assert "InteractionLimitUsd -lt 0.02" in code
    assert "-Destinatario <dígitos> o -AllIncoming" in code
    assert "AllIncoming -and $Segundos -gt 900" in code
    assert "Test-Path $stopFile) { throw" in code


def test_the_pilot_never_enables_the_universal_mode_without_the_flag():
    code = _launcher_code()
    assert 'if ($AllIncoming) { $common["WEB_BRIDGE_INBOUND_UNTIL"]' in code
    assert '$inbound = if ($AllIncoming) { "web_inbound_all" } else { "single_chat" }' in code


def test_observe_mode_forces_no_replies_and_detener_blocks_with_stop():
    code = _launcher_code()
    observe_block = code[code.index("if ($Observar) {"): code.index("if ($Piloto) {")]
    assert 'WEBJS_MODE = "observe"' in observe_block
    assert 'WEB_BRIDGE_AUTO_REPLY = "false"' in observe_block
    detener_block = code[code.index("if ($Detener) {"): code.index("if (-not (Test-Path $entry))")]
    assert "New-Item -ItemType File -Path $stopFile" in detener_block
    assert '"/kill"' in detener_block


def test_the_connector_refuses_observe_with_auto_reply_and_has_a_bounded_window():
    text = (CONNECTOR_SRC / "config.ts").read_text(encoding="utf-8")
    assert "WEBJS_MODE=observe no admite WEB_BRIDGE_AUTO_REPLY=true" in text
    assert "WINDOW_MAX_SECONDS = 900" in text


@pytest.mark.skipif(PS is None, reason="sin PowerShell en este equipo")
def test_the_launcher_parses_in_powershell():
    cmd = ("$e=$null;$t=$null;[System.Management.Automation.Language.Parser]::ParseFile("
           f"'{LAUNCHER}',[ref]$t,[ref]$e)|Out-Null; if($e){{$e|%{{$_.Message}}}}else{{'PARSE_OK'}}")
    out = subprocess.run([PS, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120).stdout
    assert "PARSE_OK" in out


def test_the_bridge_contract_refuses_unlink_by_design():
    text = (CONNECTOR_SRC / "bridge.ts").read_text(encoding="utf-8")
    assert re.search(r'respond\(res, 501, \{ error: "not_implemented" \}\)', text)


def _function_body(code: str, name: str) -> str:
    start = code.index(f"function {name}(")
    end = code.index("\nfunction ", start + 10) if "\nfunction " in code[start + 10:] else len(code)
    return code[start:end]


def test_no_log_file_is_appended_from_powershell():
    # Un único escritor por archivo: nada de AppendAllText (que colisiona con el manejador de pino).
    assert "AppendAllText" not in _launcher_code()


def test_the_connector_window_has_no_pipe_so_node_is_the_only_writer():
    window = _function_body(_launcher_code(), "Start-Window")
    assert "*>&1" not in window and "AppendAllText" not in window and "StreamWriter" not in window
    code = _launcher_code()
    for call in ("LaCuisine webjs vinculación", "LaCuisine webjs observación", "LaCuisine webjs conector"):
        line = next(l for l in code.splitlines() if call in l)
        assert "Start-Window" in line and "$logFile" not in line and "$connLog" not in line, call


def test_python_logs_are_written_by_one_open_stream_writer():
    logged = _function_body(_launcher_code(), "Start-Logged")
    assert "New-Object IO.StreamWriter" in logged
    assert logged.count("StreamWriter") == 1
    assert "$sw.Close()" in logged


def test_the_console_is_utf8_for_qr_and_accents():
    setup = _launcher_code()
    assert "chcp 65001" in setup
    assert "[Console]::OutputEncoding = [Text.Encoding]::UTF8" in setup
    assert "PYTHONIOENCODING = 'utf-8'" in setup


def test_the_qr_is_rendered_only_by_the_qr_module_and_never_reaches_the_logger():
    index = (CONNECTOR_SRC / "index.ts").read_text(encoding="utf-8")
    assert "qrcode-terminal" not in index
    assert "printQr(qr" in index
    assert "pino.destination" not in index, "el archivo de log lo abre solo logging.ts"
    assert "pino.destination" in (CONNECTOR_SRC / "logging.ts").read_text(encoding="utf-8")
    qr_module = (CONNECTOR_SRC / "qr.ts").read_text(encoding="utf-8")
    assert "qrcode-terminal" in qr_module
    assert "createLogger" not in qr_module and "log." not in qr_module


def test_every_whatsapp_listener_is_registered_before_initialize():
    # Un evento que se registra después de initialize() puede perderse (p. ej. authenticated o ready).
    index = (CONNECTOR_SRC / "index.ts").read_text(encoding="utf-8")
    # rindex: la primera aparición está en un comentario de cabecera; la llamada real es la última.
    init_at = index.rindex("client.initialize(")
    for event in ("qr", "authenticated", "ready", "auth_failure", "disconnected", "message", "loading_screen", "change_state"):
        assert index.index(f'client.on("{event}"') < init_at, event
    assert index.rindex("instrumentBrowserAndPage()") < init_at


def test_the_connector_fails_explicitly_when_ready_never_arrives():
    index = (CONNECTOR_SRC / "index.ts").read_text(encoding="utf-8")
    assert 'stage=ready state=timeout' in index
    assert "shutdown(2)" in index
    # Nunca se declara ready sin confirmación real del estado de la sesión.
    ready_handler = index[index.index('client.on("ready"'): index.index('client.on("auth_failure"')]
    assert 'real === "CONNECTED"' in ready_handler
    assert ready_handler.index('real === "CONNECTED"') < ready_handler.index('setState("ready")')


def test_observe_window_starts_at_ready_not_at_launch():
    code = _launcher_code()
    observe_block = code[code.index("if ($Observar) {"): code.index("if ($Piloto) {")]
    assert 'WEBJS_OBSERVE_SECONDS = [string]$Segundos' in observe_block
    assert "WEBJS_OBSERVE_UNTIL" not in observe_block
    # La confirmación 'ready' se lee del conector antes de cualquier mensaje de observación lista.
    assert observe_block.index('if ($s.ready -eq $true)') < observe_block.index("== Observación lista")
    assert observe_block.index("throw \"No se confirmó 'ready'") < observe_block.index("== Observación lista")
    assert "Observación iniciada" not in observe_block


def test_the_public_service_requires_the_exact_token_and_never_starts_with_stop():
    code = _launcher_code()
    service = code[code.index("if ($Servicio) {"): code.index("if ($Piloto) {")]
    assert '$ConfirmarAtencionPublica -cne "ACTIVAR_ENTRADA_PUBLICA_WEB_BRIDGE"' in service, "comparación exacta"
    assert "Test-Path $stopFile) { throw" in service
    assert "WEB_BRIDGE_INBOUND_UNTIL" not in service, "la atención pública no tiene ventana"
    assert '"public_inbound"' in service and '"true"' in service


def test_the_public_service_never_declares_success_without_ready_and_a_live_worker():
    code = _launcher_code()
    service = code[code.index("if ($Servicio) {"): code.index("if ($Piloto) {")]
    verify_at = service.index("if (-not $connOk) { throw")
    assert verify_at < service.index('== Servicio de atención pública en marcha')
    assert '"worker_started"' in service
    assert "$workers.Count -ne 1" in service
    assert "Stop-Process" in service, "un fallo cierra los procesos del proyecto"
    assert "New-Item" not in service.split("try {")[-1], "un fallo no crea STOP"
