"""Seguridad de /demo sin base de datos: sin sesión no hay datos, el token no se filtra, la cookie es estricta."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.demo import build_demo_router
from app.core.config import Settings
from app.main import create_app

TOKEN = "token-demo-unico-para-pruebas-9f3c"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(build_demo_router(token=TOKEN, pool=None, line="100200300400", retention_days=7))
    # HTTPS para que el navegador simulado envíe la cookie marcada como Secure.
    return TestClient(app, base_url="https://testserver")


def test_demo_is_not_mounted_unless_enabled():
    app = create_app(settings=Settings(meta_app_secret=None, meta_verify_token=None), webhook_repository=None)
    assert TestClient(app).get("/demo").status_code == 404


@pytest.mark.parametrize("method, path", [
    ("get", "/demo/app.js"),
    ("get", "/demo/api/state"),
    ("post", "/demo/api/message"),
    ("post", "/demo/api/handoff"),
])
def test_api_and_script_require_a_session(client, method, path):
    kwargs = {"json": {"text": "hola", "conversation_id": "00000000-0000-0000-0000-000000000000"}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert TOKEN not in response.text


def test_login_page_is_served_without_data_and_with_strict_headers(client):
    response = client.get("/demo")
    assert response.status_code == 200
    assert "Token de demostración" in response.text
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_wrong_token_is_rejected_and_never_echoed(client):
    response = client.post("/demo/login", json={"token": "incorrecto"})
    assert response.status_code == 401
    assert "incorrecto" not in response.text and TOKEN not in response.text


def test_correct_token_issues_a_hardened_session_cookie(client):
    response = client.post("/demo/login", json={"token": TOKEN})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "demo_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert TOKEN not in response.text


def test_session_unlocks_the_dashboard_and_script_only(client):
    client.post("/demo/login", json={"token": TOKEN})
    page = client.get("/demo")
    assert "DEMO · DATOS SINTÉTICOS" in page.text
    assert client.get("/demo/app.js").status_code == 200
    assert TOKEN not in page.text


def test_forged_session_cookie_is_rejected(client):
    client.cookies.set("demo_session", "9999999999.00ff")
    assert client.get("/demo/app.js").status_code == 401


def test_repeated_failed_logins_are_rate_limited_even_with_the_right_token(client):
    for _ in range(5):
        assert client.post("/demo/login", json={"token": "mal"}).status_code == 401
    assert client.post("/demo/login", json={"token": TOKEN}).status_code == 429


@pytest.mark.parametrize("settings_kwargs, error", [
    ({"demo_enabled": True}, "demo_token_missing"),
    ({"demo_enabled": True, "demo_token": TOKEN, "meta_send_enabled": True,
      "meta_phone_number_id": "100200300400"}, "demo_with_meta_send_refused"),
    ({"demo_enabled": True, "demo_token": TOKEN}, "demo_line_missing"),
    ({"demo_enabled": True, "demo_token": TOKEN, "meta_phone_number_id": "100200300400"}, "demo_database_missing"),
])
def test_demo_refuses_to_start_when_anything_is_missing_or_unsafe(settings_kwargs, error):
    settings = Settings(meta_app_secret=None, meta_verify_token=None, **settings_kwargs)
    with pytest.raises(RuntimeError, match=error):
        create_app(settings=settings, webhook_repository=None)


def test_demo_token_is_never_in_settings_representation():
    settings = Settings(meta_app_secret=None, meta_verify_token=None, demo_token="secreto-muy-largo-demo-1234")
    assert "secreto-muy-largo-demo-1234" not in repr(settings)


def test_form_post_without_json_is_refused_even_with_the_right_token(client):
    response = client.post("/demo/login", data={"token": TOKEN})
    assert response.status_code in (415, 422)  # rechazado antes de comprobar el token
    assert "set-cookie" not in response.headers


def test_cross_origin_post_is_refused_even_with_a_valid_session(client):
    client.post("/demo/login", json={"token": TOKEN})
    foreign = {"Origin": "https://sitio-ajeno.example"}
    assert client.post("/demo/api/message", json={"text": "hola"}, headers=foreign).status_code == 403
    cross_site = {"Sec-Fetch-Site": "cross-site"}
    assert client.post("/demo/api/message", json={"text": "hola"}, headers=cross_site).status_code == 403


def test_same_origin_login_is_accepted(client):
    response = client.post("/demo/login", json={"token": TOKEN},
                           headers={"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200


def test_session_requests_are_rate_limited():
    app = FastAPI()
    app.include_router(build_demo_router(token=TOKEN, pool=None, line="100200300400", retention_days=7))
    # Sin base de datos de prueba la ruta de estado falla con 500 tras el límite: no se propaga el error.
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    client.post("/demo/login", json={"token": TOKEN})
    codes = [client.get("/demo/api/state").status_code for _ in range(70)]
    assert 429 in codes
    assert codes.index(429) >= 60  # las primeras 60 peticiones del minuto pasan
