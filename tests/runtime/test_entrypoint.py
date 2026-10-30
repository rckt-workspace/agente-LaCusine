"""Selección del modo de ejecución. Sin arrancar procesos reales."""

import pytest

from app.runtime import entrypoint


def test_default_mode_is_web_and_port_defaults_to_8000():
    assert entrypoint.resolve_mode({}) == "web"
    assert entrypoint.resolve_port({}) == "8000"


@pytest.mark.parametrize("environ, error", [
    ({"APP_RUN_MODE": "otro"}, "run_mode_invalid"),
    ({"APP_RUN_MODE": "WEB"}, "run_mode_invalid"),
    ({"PORT": "abc"}, "port_invalid"),
    ({"PORT": "0"}, "port_invalid"),
    ({"PORT": "70000"}, "port_invalid"),
])
def test_invalid_mode_or_port_refuses_to_start(environ, error):
    with pytest.raises(entrypoint.EntrypointError) as excinfo:
        entrypoint.resolve_mode(environ)
        entrypoint.resolve_port(environ)
    assert excinfo.value.code == error


def test_web_mode_replaces_the_process_with_uvicorn_on_the_platform_port():
    calls = []
    entrypoint.main({"APP_RUN_MODE": "web", "PORT": "10000"}, exec_fn=lambda p, a: calls.append((p, a)))
    program, argv = calls[0]
    assert program == argv[0]
    assert argv[1:] == ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]


def test_worker_mode_replaces_the_process_with_the_worker_module():
    calls = []
    entrypoint.main({"APP_RUN_MODE": "worker"}, exec_fn=lambda p, a: calls.append((p, a)))
    assert calls[0][1][1:] == ["-m", "app.worker.main"]


def test_demo_combined_refuses_without_demo_enabled():
    assert entrypoint.main({"APP_RUN_MODE": "demo-combined"}) == 2


def test_demo_combined_runs_api_and_worker_as_two_supervised_children(monkeypatch):
    captured = {}

    class FakeSupervisor:
        def __init__(self, children):
            captured["children"] = [c.name for c in children]
            captured["argv"] = {c.name: c.argv for c in children}

        def run(self):
            return 0

    monkeypatch.setattr(entrypoint, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(entrypoint, "install_signal_handlers", lambda s: None)
    code = entrypoint.main({"APP_RUN_MODE": "demo-combined", "DEMO_ENABLED": "true", "PORT": "9000"})
    assert code == 0
    assert captured["children"] == ["api", "worker"]
    assert captured["argv"]["api"][1:] == ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
    assert captured["argv"]["worker"][1:] == ["-m", "app.worker.main"]
