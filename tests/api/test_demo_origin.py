"""Origen público de la demo detrás de un proxy HTTPS (Render termina TLS y entrega HTTP al contenedor).

Simulan el tráfico real con TestClient: el host y el esquema que ve la aplicación son los del proxy, y el
origen que envía el navegador es el público. Ningún cliente real ni red externa.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.demo import build_demo_router, normalise_public_origin

TOKEN = "token-origen-publico-pruebas-4a1d"
PUBLIC = "https://lacusine-demo.onrender.com"
PUBLIC_HOST = "lacusine-demo.onrender.com"


def _app():
    app = FastAPI()
    app.include_router(build_demo_router(token=TOKEN, pool=None, line="100200300400", retention_days=7,
                                         public_origin=PUBLIC))
    return app


def _proxy_client():
    # El proxy entrega HTTP y el host de la petición es el público: así llega al contenedor en Render.
    return TestClient(_app(), base_url=f"http://{PUBLIC_HOST}")


def _login(client, headers=None, token=TOKEN):
    return client.post("/demo/login", json={"token": token}, headers=headers or {})


def test_legitimate_https_browser_origin_behind_proxy_is_accepted():
    client = _proxy_client()
    response = _login(client, {"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200


def test_scheme_seen_by_the_app_does_not_decide_the_origin():
    # La app ve http; el origen válido es https. Debe aceptarse igualmente.
    response = _login(_proxy_client(), {"Origin": PUBLIC})
    assert response.status_code == 200


def test_malicious_origin_is_refused():
    response = _login(_proxy_client(), {"Origin": "https://sitio-ajeno.example"})
    assert response.status_code == 403


def test_origin_with_same_host_but_other_scheme_is_refused():
    response = _login(_proxy_client(), {"Origin": f"http://{PUBLIC_HOST}"})
    assert response.status_code == 403


def test_unexpected_host_is_refused_even_with_a_legitimate_origin():
    client = TestClient(_app(), base_url="http://evil.example")
    response = _login(client, {"Origin": PUBLIC})
    assert response.status_code == 403


def test_spoofed_forwarded_headers_are_ignored():
    # Un cliente no puede cambiar la decisión añadiendo cabeceras X-Forwarded-*.
    client = TestClient(_app(), base_url="http://evil.example")
    response = _login(client, {"Origin": PUBLIC, "X-Forwarded-Host": PUBLIC_HOST,
                               "X-Forwarded-Proto": "https"})
    assert response.status_code == 403
    response = _proxy_client().post("/demo/login", json={"token": TOKEN},
                                    headers={"Origin": "https://sitio-ajeno.example",
                                             "X-Forwarded-Host": PUBLIC_HOST, "X-Forwarded-Proto": "https"})
    assert response.status_code == 403


def test_request_without_origin_is_allowed_only_with_json_and_the_token():
    # Cliente que no es navegador (sin Origin): sigue exigido el token. CSRF requiere un navegador.
    client = _proxy_client()
    assert _login(client).status_code == 200
    assert _login(client, token="incorrecto").status_code == 401


def test_cross_site_fetch_metadata_is_refused():
    response = _login(_proxy_client(), {"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_session_cookie_is_secure_httponly_and_strict():
    response = _login(_proxy_client(), {"Origin": PUBLIC})
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/demo" in cookie


def test_login_script_is_served_without_a_session_and_keeps_csp():
    client = _proxy_client()
    response = client.get("/demo/login.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert TOKEN not in response.text


def test_login_page_has_no_inline_script():
    page = _proxy_client().get("/demo").text
    assert "<script>" not in page
    assert 'src="/demo/login.js"' in page


@pytest.mark.parametrize("value", [
    "http://lacusine-demo.onrender.com",       # HTTP público no permitido
    "https://lacusine-demo.onrender.com/demo",  # ruta
    "https://user:secreto@lacusine-demo.onrender.com",
    "https://lacusine-demo.onrender.com?x=1",
    "https://*.onrender.com",
    "ftp://lacusine-demo.onrender.com",
    "",
])
def test_invalid_public_origins_are_refused(value):
    with pytest.raises(ValueError):
        normalise_public_origin(value)


def test_loopback_http_is_allowed_for_local_browser_tests_only():
    assert normalise_public_origin("http://127.0.0.1:18500") == ("http://127.0.0.1:18500", "127.0.0.1:18500")
    assert normalise_public_origin("https://LACUSINE-DEMO.onrender.com")[0] == "https://lacusine-demo.onrender.com"
