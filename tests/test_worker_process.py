"""Arranque del worker como proceso independiente real (python -m app.worker.main). Sin red ni base."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.worker.main import run_standby, worker_requested

ROOT = Path(__file__).resolve().parents[1]
WORKER_VARS = (
    "WORKER_ENABLED", "DATABASE_URL", "META_PHONE_NUMBER_ID", "META_SEND_ENABLED", "WHATSAPP_DRY_RUN",
    "AGENT_DRY_RUN", "ANTHROPIC_ENABLED", "LACUSINE_LOAD_DOTENV",
)


def _env(**overrides):
    base = {key: value for key, value in os.environ.items() if key not in WORKER_VARS}
    base.update({"PYTHONIOENCODING": "utf-8"})
    base.update(overrides)
    return base


def _spawn(**overrides) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "app.worker.main"], cwd=ROOT, env=_env(**overrides),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def test_disabled_worker_stays_alive_without_consuming_anything():
    process = _spawn(WORKER_ENABLED="false")
    try:
        time.sleep(3)
        assert process.poll() is None, "el worker deshabilitado no debe terminar"
    finally:
        process.terminate()
        process.wait(timeout=20)


def test_disabled_worker_with_invalid_numeric_setting_still_stands_by():
    # Una variable numérica mal escrita no debe provocar un ciclo de reinicios mientras el worker está apagado.
    process = _spawn(WORKER_ENABLED="false", WORKER_BATCH_SIZE="no-es-numero")
    try:
        time.sleep(3)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=20)


def test_enabled_worker_without_database_exits_with_configuration_error():
    process = _spawn(WORKER_ENABLED="true")
    output, _ = process.communicate(timeout=30)
    assert process.returncode == 2
    assert "database_url_missing" in output


def test_enabled_worker_without_business_line_refuses_to_start():
    process = _spawn(WORKER_ENABLED="true", DATABASE_URL="postgresql://u:p@127.0.0.1:1/lacusine_test")
    output, _ = process.communicate(timeout=30)
    assert process.returncode == 2
    assert "meta_phone_number_id_missing" in output


def test_standby_returns_when_stop_is_requested():
    stop = threading.Event()
    thread = threading.Thread(target=run_standby, args=(stop, 0.05))
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("1", True), ("SÍ", True), ("false", False), ("", False), (None, False),
])
def test_worker_is_requested_only_by_explicit_enablement(value, expected):
    environ = {} if value is None else {"WORKER_ENABLED": value}
    assert worker_requested(environ) is expected
