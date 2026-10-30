"""Recorrido real de navegador sobre /demo: JavaScript, CSP, sesión, worker y transferencia.

Arranca el proceso real de la demo (APP_RUN_MODE=demo-combined) contra PostgreSQL local desechable y controla
Chrome instalado en la máquina con Playwright. Sin red externa: los proveedores son simulados.
Se omite si falta la base de pruebas, Playwright o Chrome.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.browser]

ROOT = Path(__file__).resolve().parents[2]
CHROME_CANDIDATES = [
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
]
LINE = "100200300400"
QUESTION = "Hola, estoy buscando una cafetera para mi casa. ¿Qué opciones tienen?"


def _chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _stop_tree(proc: subprocess.Popen) -> None:
    """Detiene el supervisor y sus hijos. En Windows terminate() mata solo el padre y deja huérfanos
    a la API y al worker, que siguen consumiendo la base de pruebas. Se usa taskkill /T para el árbol."""
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            proc.terminate()  # en POSIX el supervisor reenvía SIGTERM a sus hijos
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL no configurada")
    if _chrome() is None:
        pytest.skip("Chrome o Edge no instalados")
    pytest.importorskip("playwright.sync_api")

    from tests.integration.test_postgres_persistence import _reset_and_migrate

    _reset_and_migrate()
    port = _free_port()
    token = secrets.token_urlsafe(24)
    env = {**os.environ,
           "APP_RUN_MODE": "demo-combined", "DEMO_ENABLED": "true", "DEMO_TOKEN": token,
           "DEMO_PUBLIC_ORIGIN": f"http://127.0.0.1:{port}", "DATABASE_URL": test_url,
           "META_PHONE_NUMBER_ID": LINE, "META_SEND_ENABLED": "false", "WHATSAPP_DRY_RUN": "true",
           "ANTHROPIC_ENABLED": "false", "AGENT_DRY_RUN": "true", "WORKER_ENABLED": "true",
           "WORKER_MAX_CONCURRENCY": "1", "ADVISOR_NOTIFIER": "simulated", "PORT": str(port)}
    log = tmp_path_factory.mktemp("demo") / "server.log"
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen([sys.executable, "-m", "app.runtime.entrypoint"], cwd=ROOT, env=env,
                                stdout=out, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(base + "/health", timeout=2).status == 200:
                break
        except OSError:
            time.sleep(1)
    else:
        _stop_tree(proc)
        pytest.fail(f"la demo no respondió; log: {log.read_text(encoding='utf-8')[-800:]}")
    yield {"base": base, "token": token, "log": log, "dir": tmp_path_factory.mktemp("shots")}
    _stop_tree(proc)


def _state(page):
    return page.evaluate("async () => (await fetch('/demo/api/state')).json()")


def _wait_for(predicate, page, seconds=120):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = _state(page)
        if predicate(last):
            return last
        time.sleep(2)
    pytest.fail(f"condición no alcanzada; último estado: {json.dumps(last)[:600]}")


def test_full_browser_journey_with_csp_login_worker_and_handoff(demo_server):
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(_chrome()), headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("console", lambda m: errors.append(f"console:{m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))

        # 1-2. Abrir /demo e introducir el token.
        page.goto(demo_server["base"] + "/demo")
        page.fill("#token", demo_server["token"])
        # 3. Pulsar Entrar.
        page.click("button[type=submit]")
        # 4-5. Sesión y dashboard visibles.
        page.wait_for_selector("#send", timeout=30000)
        cookies = {c["name"]: c for c in context.cookies()}
        assert "demo_session" in cookies and cookies["demo_session"]["httpOnly"]

        before = _state(page)
        # 6. Mensaje sintético.
        page.fill("#text", QUESTION)
        page.click("#send")
        page.wait_for_function("() => document.getElementById('sent').textContent.includes('Evento')", timeout=30000)
        # 7-8. El worker procesa y el simulador entrega la respuesta.
        state = _wait_for(lambda s: any(r["delivery"] == "sent" for r in s["replies"]), page)
        assert state["events"].get("processed", 0) > before["events"].get("processed", 0)
        assert any(r["kind"] == "reply" for r in state["replies"])
        page.wait_for_function("() => document.getElementById('state').textContent.includes('Respuestas del agente')",
                               timeout=30000)
        replies_before_handoff = len(state["replies"])

        # 9. Transferir la conversación.
        page.click("#handoff")
        page.wait_for_function("() => document.getElementById('handoffmsg').textContent.includes('Solicitud')",
                               timeout=30000)
        state = _wait_for(lambda s: s["conversations"].get("paused_for_human", 0) >= 1, page)

        # 10. Otro mensaje del mismo contacto.
        page.fill("#text", "¿Me confirman el precio?")
        page.click("#send")
        ignored_before = state["events"].get("ignored", 0)
        state = _wait_for(lambda s: s["events"].get("ignored", 0) > ignored_before, page)

        # 11. La IA no responde tras la transferencia.
        assert len(state["replies"]) == replies_before_handoff

        # Dashboard en escritorio.
        page.screenshot(path=str(demo_server["dir"] / "dashboard_desktop.png"), full_page=True)
        browser.close()

    assert errors == [], f"errores de navegador: {errors}"


def test_dashboard_on_a_mobile_viewport(demo_server):
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(_chrome()), headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                      device_scale_factor=2)
        page = context.new_page()
        page.on("console", lambda m: errors.append(f"console:{m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))
        page.goto(demo_server["base"] + "/demo")
        page.fill("#token", demo_server["token"])
        page.click("button[type=submit]")
        page.wait_for_selector("#send", timeout=30000)
        overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        page.screenshot(path=str(demo_server["dir"] / "dashboard_mobile.png"), full_page=True)
        browser.close()

    assert errors == [], f"errores de navegador en móvil: {errors}"
    assert overflow <= 0, f"desbordamiento horizontal de {overflow}px en móvil"
