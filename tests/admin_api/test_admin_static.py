"""Servicio estático del panel en /admin: sin salir del directorio compilado y sin exponer archivos fuera de él."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_api.wiring import _static_router


def _client(dist: Path) -> TestClient:
    app = FastAPI()
    app.include_router(_static_router(dist))
    return TestClient(app)


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    # Archivo fuera del directorio compilado: nunca debe servirse.
    (tmp_path / "secreto.txt").write_text("no publicar", encoding="utf-8")
    return dist


def test_raiz_del_panel_sirve_index(tmp_path):
    response = _client(_dist(tmp_path)).get("/admin")
    assert response.status_code == 200
    assert "panel" in response.text


def test_ruta_de_la_spa_devuelve_index(tmp_path):
    response = _client(_dist(tmp_path)).get("/admin/conversaciones/123")
    assert response.status_code == 200
    assert "panel" in response.text


def test_asset_existente_se_sirve(tmp_path):
    response = _client(_dist(tmp_path)).get("/admin/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_no_se_puede_salir_del_directorio(tmp_path):
    client = _client(_dist(tmp_path))
    for attempt in ("/admin/../secreto.txt", "/admin/%2e%2e/secreto.txt", "/admin/assets/../../secreto.txt"):
        response = client.get(attempt)
        assert "no publicar" not in response.text, attempt


def test_sin_compilar_responde_404_sin_detalles(tmp_path):
    response = _client(tmp_path / "no-existe").get("/admin")
    assert response.status_code == 404
    assert response.json() == {"detail": "panel_not_built"}
