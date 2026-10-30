"""Validación estructural del blueprint de Render con un parser YAML real.

Además de las pruebas de texto de test_deployment_contract.py, este módulo:
- rechaza claves duplicadas en cualquier mapa (un duplicado sobrescribe valores en silencio);
- comprueba la forma exacta del servicio, no solo la presencia de subcadenas.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader que falla si una clave aparece dos veces en el mismo mapa."""


def _no_duplicate_keys(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"clave duplicada: {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


@pytest.fixture(scope="module")
def blueprint():
    return yaml.load((ROOT / "render.yaml").read_text(encoding="utf-8"), Loader=_StrictLoader)


@pytest.fixture(scope="module")
def service(blueprint):
    assert list(blueprint) == ["services"]
    services = blueprint["services"]
    assert len(services) == 1, "debe existir exactamente un servicio"
    return services[0]


def _env(service):
    return {item["key"]: item for item in service["envVars"]}


def test_blueprint_has_no_duplicate_keys_anywhere(blueprint):
    # La carga estricta ya habría fallado; esta aserción documenta la garantía.
    assert isinstance(blueprint, dict)


def test_single_free_docker_web_service(service):
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["plan"] == "free"
    assert service["dockerfilePath"] == "./Dockerfile"
    assert service["autoDeployTrigger"] == "off"
    assert service["healthCheckPath"] == "/health"


def test_no_build_or_start_commands_and_no_starter_plan(service):
    # Con runtime docker el comando lo fija la imagen (APP_RUN_MODE). Un comando heredado sería un error.
    assert "buildCommand" not in service and "startCommand" not in service
    assert "starter" not in str(service)


def test_only_one_dockerfile_exists_in_the_repository():
    dockerfiles = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("Dockerfile*")
                         if ".venv" not in p.parts and ".git" not in p.parts)
    assert dockerfiles == ["Dockerfile"]


def test_demo_mode_and_flags_are_exact(service):
    env = _env(service)
    assert env["APP_RUN_MODE"]["value"] == "demo-combined"
    assert env["DEMO_ENABLED"]["value"] == "true"
    assert env["DEMO_TOKEN"] == {"key": "DEMO_TOKEN", "sync": False}


def test_external_integrations_are_disabled_with_exact_values(service):
    env = _env(service)
    assert env["META_SEND_ENABLED"]["value"] == "false"
    assert env["WHATSAPP_DRY_RUN"]["value"] == "true"
    assert env["ANTHROPIC_ENABLED"]["value"] == "false"
    assert env["AGENT_DRY_RUN"]["value"] == "true"


@pytest.mark.parametrize("secret", ["DEMO_TOKEN", "DATABASE_URL", "META_APP_SECRET", "META_VERIFY_TOKEN",
                                    "META_PHONE_NUMBER_ID"])
def test_secrets_come_from_render_environment_not_literals(service, secret):
    assert _env(service)[secret] == {"key": secret, "sync": False}


def test_every_env_key_is_unique_and_has_a_value_or_sync(service):
    keys = [item["key"] for item in service["envVars"]]
    assert len(keys) == len(set(keys))
    for item in service["envVars"]:
        assert ("value" in item) != ("sync" in item), item["key"]
