"""Panel administrativo: autenticación JWT, roles, rutas y traducción de errores. Sin red ni base de datos."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.policy import AdminPolicyError
from app.admin_api.auth import SupabaseTokenVerifier
from app.admin_api.queries import escape_like, mask_phone
from app.admin_api.router import build_admin_router
from app.repositories.errors import StorageError

SUPABASE_URL = "https://proyecto-prueba.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
SUBJECT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwkClient:
    def __init__(self, public_key) -> None:
        self._key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self._key})()


def _token(private_key, *, role="operator", location="app", issuer=ISSUER, audience="authenticated",
           algorithm="RS256", sub=SUBJECT, expires_in=300):
    now = int(time.time())
    claims = {"sub": sub, "aud": audience, "iss": issuer, "iat": now, "exp": now + expires_in}
    if location == "app" and role is not None:
        claims["app_metadata"] = {"role": role}
    if location == "user" and role is not None:
        # user_metadata lo controla el propio usuario: nunca debe otorgar permisos.
        claims["user_metadata"] = {"role": role}
    if algorithm == "HS256":
        return jwt.encode(claims, "secreto-compartido-de-prueba-con-mas-de-32-bytes", algorithm="HS256")
    return jwt.encode(claims, private_key, algorithm=algorithm)


@dataclass
class _FakeReads:
    def dashboard(self, daily, monthly):
        return {"open_handoffs": 0, "daily": str(daily) if daily is not None else None}

    def list_conversations(self, **kwargs):
        return {"items": [], "total": 0, **kwargs}

    def get_conversation(self, conversation_id):
        return None if conversation_id == MISSING else {"id": str(conversation_id)}

    def list_leads(self, **kwargs):
        return {"items": [], "total": 0}

    def list_handoffs(self, **kwargs):
        return {"items": [], "total": 0}

    def list_products(self, **kwargs):
        return {"items": [], "total": 0}

    def analytics(self, days):
        return {"days": days, "messages_by_day": []}


MISSING = uuid4()


class _FakeService:
    calls: list = []
    behaviour = None

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def _do(self, name, *args):
        _FakeService.calls.append((name, self.ctx.operator, args))
        if _FakeService.behaviour is not None:
            _FakeService.behaviour(name)

    def pause_conversation(self, conversation_id, reason):
        self._do("pause", conversation_id, reason)

    def resume_conversation(self, conversation_id, reason):
        self._do("resume", conversation_id, reason)

    def resolve_handoff(self, handoff_id, to_state, reason, advisor=None):
        self._do("handoff", handoff_id, to_state, reason, advisor)


@pytest.fixture()
def client(rsa_key):
    _FakeService.calls = []
    _FakeService.behaviour = None
    verifier = SupabaseTokenVerifier(SUPABASE_URL, "https://unused.example/jwks",
                                     jwk_client=_FakeJwkClient(rsa_key.public_key()))
    app = FastAPI()
    app.include_router(build_admin_router(
        verifier=verifier, reads=_FakeReads(), service_factory=lambda ctx: _FakeService(ctx),
        environment="test", daily_budget_usd=None, monthly_budget_usd=None,
    ))
    return TestClient(app)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- Autenticación ----------------------------------------------------------------

def test_sin_token_responde_401(client):
    assert client.get("/api/admin/dashboard").status_code == 401


def test_token_malformado_responde_401(client):
    assert client.get("/api/admin/dashboard", headers=_auth("no.es.un.jwt")).status_code == 401


def test_esquema_distinto_de_bearer_responde_401(client, rsa_key):
    token = _token(rsa_key)
    assert client.get("/api/admin/dashboard", headers={"Authorization": f"Basic {token}"}).status_code == 401


def test_emisor_incorrecto_responde_401(client, rsa_key):
    token = _token(rsa_key, issuer="https://otro-proyecto.supabase.co/auth/v1")
    assert client.get("/api/admin/dashboard", headers=_auth(token)).status_code == 401


def test_audiencia_incorrecta_responde_401(client, rsa_key):
    token = _token(rsa_key, audience="anon")
    assert client.get("/api/admin/dashboard", headers=_auth(token)).status_code == 401


def test_token_expirado_responde_401(client, rsa_key):
    token = _token(rsa_key, expires_in=-60)
    assert client.get("/api/admin/dashboard", headers=_auth(token)).status_code == 401


def test_hs256_con_clave_publica_se_rechaza(client, rsa_key):
    # Ataque de confusión de algoritmo: el token va firmado con un secreto simétrico.
    token = _token(rsa_key, algorithm="HS256")
    assert client.get("/api/admin/dashboard", headers=_auth(token)).status_code == 401


def test_rol_en_user_metadata_no_otorga_acceso(client, rsa_key):
    token = _token(rsa_key, role="admin", location="user")
    assert client.get("/api/admin/dashboard", headers=_auth(token)).status_code == 403


def test_token_sin_rol_reconocido_responde_403(client, rsa_key):
    token = _token(rsa_key, role="superusuario")
    assert client.get("/api/admin/dashboard", headers=_auth(token)).status_code == 403


def test_operador_lee_el_panel(client, rsa_key):
    response = client.get("/api/admin/dashboard", headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 200


def test_me_devuelve_el_rol(client, rsa_key):
    response = client.get("/api/admin/me", headers=_auth(_token(rsa_key, role="admin")))
    assert response.json() == {"role": "admin"}


# --- Roles y auditoría ------------------------------------------------------------

def test_operador_no_puede_reanudar(client, rsa_key):
    conversation = uuid4()
    response = client.post(f"/api/admin/conversations/{conversation}/resume",
                           json={"reason": "motivo válido"}, headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 403
    assert _FakeService.calls == []


def test_admin_reanuda_y_la_auditoria_usa_el_sub(client, rsa_key):
    conversation = uuid4()
    response = client.post(f"/api/admin/conversations/{conversation}/resume",
                           json={"reason": "cliente atendido"}, headers=_auth(_token(rsa_key, role="admin")))
    assert response.status_code == 200
    assert _FakeService.calls == [("resume", SUBJECT, (conversation, "cliente atendido"))]


def test_operador_pausa_la_ia_con_motivo(client, rsa_key):
    conversation = uuid4()
    response = client.post(f"/api/admin/conversations/{conversation}/pause",
                           json={"reason": "asesor toma el caso"}, headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 200
    assert _FakeService.calls[-1] == ("pause", SUBJECT, (conversation, "asesor toma el caso"))


def test_motivo_demasiado_corto_responde_422_sin_llamar_al_servicio(client, rsa_key):
    response = client.post(f"/api/admin/conversations/{uuid4()}/pause",
                           json={"reason": "no"}, headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 422
    assert _FakeService.calls == []


def test_asignar_solicitud_envia_asesor_y_estado(client, rsa_key):
    handoff = uuid4()
    response = client.post(f"/api/admin/handoffs/{handoff}/assign",
                           json={"reason": "caso de cotización", "advisor": "Laura"},
                           headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 200
    assert _FakeService.calls[-1] == ("handoff", SUBJECT, (handoff, "assigned", "caso de cotización", "Laura"))


def test_transicion_no_permitida_responde_409(client, rsa_key):
    def boom(_name):
        raise AdminPolicyError("transition_not_allowed")
    _FakeService.behaviour = boom
    response = client.post(f"/api/admin/conversations/{uuid4()}/pause",
                           json={"reason": "motivo válido"}, headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 409
    assert response.json() == {"detail": "transition_not_allowed"}


def test_error_de_almacenamiento_responde_503_sin_detalle(client, rsa_key):
    def boom(_name):
        raise StorageError("budget_write_failed")
    _FakeService.behaviour = boom
    response = client.post(f"/api/admin/conversations/{uuid4()}/pause",
                           json={"reason": "motivo válido"}, headers=_auth(_token(rsa_key, role="operator")))
    assert response.status_code == 503
    assert "budget" not in response.text


# --- Lectura ----------------------------------------------------------------------

def test_conversacion_inexistente_responde_404(client, rsa_key):
    response = client.get(f"/api/admin/conversations/{MISSING}", headers=_auth(_token(rsa_key)))
    assert response.status_code == 404


def test_filtro_de_estado_invalido_responde_422(client, rsa_key):
    response = client.get("/api/admin/conversations?state=inventado", headers=_auth(_token(rsa_key)))
    assert response.status_code == 422


def test_limite_de_pagina_fuera_de_rango_responde_422(client, rsa_key):
    response = client.get("/api/admin/leads?limit=5000", headers=_auth(_token(rsa_key)))
    assert response.status_code == 422


def test_analitica_pasa_los_dias_validados(client, rsa_key):
    response = client.get("/api/admin/analytics?days=30", headers=_auth(_token(rsa_key)))
    assert response.json()["days"] == 30
    assert client.get("/api/admin/analytics?days=500", headers=_auth(_token(rsa_key))).status_code == 422


# --- Utilidades de datos personales ----------------------------------------------

def test_telefono_se_enmascara_a_los_ultimos_cuatro_digitos():
    assert mask_phone("573001234567") == "•••• 4567"
    assert mask_phone(None) is None


def test_busqueda_escapa_comodines():
    assert escape_like("100%_x") == "%100\\%\\_x%"
    assert escape_like("  ") == "%%"


def test_sin_configuracion_de_supabase_no_hay_panel(monkeypatch):
    from app.admin_api.wiring import build_admin_panel
    from app.core.config import Settings

    settings = Settings(meta_app_secret=None, meta_verify_token=None)
    assert build_admin_panel(settings, pool=object()) is None
    assert build_admin_panel(Settings(meta_app_secret=None, meta_verify_token=None, supabase_url=SUPABASE_URL),
                             pool=object()) is None
    assert build_admin_panel(settings.__class__(meta_app_secret=None, meta_verify_token=None,
                                                 supabase_url=SUPABASE_URL, supabase_jwks_url="https://x"),
                             pool=None) is None
