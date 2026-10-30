"""Contrato de configuración y despliegue: el envío real arranca desactivado y no hay secretos versionados."""

import re

import pytest
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SECRET_SHAPED = re.compile(r"[A-Za-z0-9_\-]{20,}")
DEPLOY_FILES = ("render.yaml", ".env.example", "docs/render-deployment.md", "docs/environment-variables.md")
SECRET_NAME = re.compile(r"(SECRET|TOKEN|PASSWORD|API_KEY|DATABASE_URL|PHONE_NUMBER_ID|RECIPIENTS)")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _render_services() -> list[str]:
    # Cada servicio del Blueprint empieza con "  - type:". Se separan sin parser YAML: el contrato es de texto.
    return _read("render.yaml").split("\n  - type: ")[1:]


def test_real_sending_is_disabled_by_default():
    settings = Settings(meta_app_secret=None, meta_verify_token=None)
    assert settings.meta_send_enabled is False
    assert settings.whatsapp_dry_run is True
    assert settings.anthropic_enabled is False
    assert settings.agent_dry_run is True
    assert settings.whatsapp_allowed_recipients == ()


def test_gitignore_excludes_real_env_files_but_keeps_the_example():
    rules = _read(".gitignore").splitlines()
    assert ".env" in rules
    assert ".env.*" in rules
    assert "!.env.example" in rules


def test_dockerignore_keeps_secrets_out_of_images():
    rules = _read(".dockerignore").splitlines()
    assert ".env" in rules and ".env.*" in rules
    assert ".venv/" in rules


def test_env_example_values_never_look_like_secrets():
    for name, value in re.findall(r"^([A-Z_][A-Z0-9_]*)=(.*)$", _read(".env.example"), flags=re.MULTILINE):
        assert value.strip() == "" or not SECRET_SHAPED.search(value), name


def test_send_flags_in_example_are_safe():
    text = _read(".env.example")
    assert re.search(r"^META_SEND_ENABLED=false$", text, re.MULTILINE)
    assert re.search(r"^WHATSAPP_DRY_RUN=true$", text, re.MULTILINE)
    assert re.search(r"^ANTHROPIC_ENABLED=false$", text, re.MULTILINE)


def test_deployment_files_contain_no_credential_patterns():
    for name in DEPLOY_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"EAA[A-Za-z0-9]{20,}", text), name
        assert not re.search(r"sk-ant-[A-Za-z0-9]", text), name
        assert not re.search(r"postgres(ql)?://[^\s:]+:[^\s@]{4,}@", text), name


def test_render_blueprint_is_a_single_free_docker_web_service_with_auto_deploy_off():
    blocks = _render_services()
    assert [b.split("\n")[0].strip() for b in blocks] == ["web"]
    block = blocks[0]
    assert "runtime: docker" in block
    assert "plan: free" in block
    assert "plan: starter" not in block
    assert "dockerfilePath: ./Dockerfile" in block
    assert 'autoDeployTrigger: "off"' in block


def test_render_blueprint_declares_no_second_service_or_background_worker():
    text = _read("render.yaml")
    assert "type: worker" not in text
    assert text.count("  - type: ") == 1


def test_render_demo_runs_api_and_worker_in_combined_mode_with_health_check():
    block = _render_services()[0]
    assert "healthCheckPath: /health" in block
    assert "key: APP_RUN_MODE\n        value: demo-combined" in block
    assert "key: DEMO_ENABLED\n        value: \"true\"" in block


def test_render_demo_keeps_external_integrations_disabled():
    block = _render_services()[0]
    assert "key: META_SEND_ENABLED\n        value: \"false\"" in block
    assert "key: WHATSAPP_DRY_RUN\n        value: \"true\"" in block
    assert "key: ANTHROPIC_ENABLED\n        value: \"false\"" in block
    assert "key: AGENT_DRY_RUN\n        value: \"true\"" in block


def test_render_secret_names_never_get_literal_values():
    text = _read("render.yaml")
    pattern = re.compile(r"- key: ([A-Z_]+)\n\s+(sync: false|value: .*)")
    for name, body in pattern.findall(text):
        if SECRET_NAME.search(name):
            assert body == "sync: false", name


def test_real_env_variants_are_ignored_by_git():
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git no disponible")
    variants = [".env", ".env.local", ".env.production", ".env.test.local", ".env.staging"]
    for name in variants:
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True)
        assert result.returncode == 0, f"{name} no está ignorado por Git"
    result = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT, capture_output=True)
    assert result.returncode != 0, ".env.example debe seguir versionado"


def test_commercial_spreadsheets_and_pdfs_are_ignored_by_git():
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git no disponible")
    # El XLSX original del catálogo y los PDF comerciales no deben entrar al repositorio público.
    for name in ["catalogo.xlsx", "catalogo.xls", "lista.pdf", "precios.csv"]:
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True)
        assert result.returncode == 0, f"{name} no está ignorado por Git"


def test_render_service_declares_the_business_line_as_secret():
    # Sin META_PHONE_NUMBER_ID el webhook almacena eventos de otras líneas antes de descartarlos.
    (block,) = _render_services()
    assert re.search(r"- key: META_PHONE_NUMBER_ID\n\s+sync: false", block)
