"""Autenticación del panel: JWT de Supabase Auth verificado con claves públicas (JWKS).

Reglas:
  * Solo algoritmos asimétricos (RS256/ES256). Nunca HS256: con una clave pública, aceptar HS256 permitiría
    forjar tokens usando esa misma clave como secreto.
  * Emisor y audiencia verificados: el token debe venir del proyecto configurado y dirigido a usuarios autenticados.
  * El rol sale de app_metadata.role, que solo un administrador de Supabase puede escribir. Nunca de user_metadata,
    que el propio usuario puede modificar.
  * La identidad de auditoría es el `sub` (UUID). No se guarda correo ni nombre.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
ROLES = frozenset({ROLE_OPERATOR, ROLE_ADMIN})
AUDIENCE = "authenticated"
ALGORITHMS = ["RS256", "ES256"]


class AuthError(Exception):
    """code = 'invalid_token' (401) o 'forbidden' (403). Sin detalles del token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str

    def allows(self, required: str) -> bool:
        if required == ROLE_OPERATOR:
            return self.role in ROLES
        return self.role == ROLE_ADMIN


class SupabaseTokenVerifier:
    def __init__(self, supabase_url: str, jwks_url: str, jwk_client: PyJWKClient | None = None) -> None:
        self._issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self._jwks = jwk_client or PyJWKClient(jwks_url, cache_keys=True, lifespan=600)

    def principal(self, token: str) -> Principal:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=ALGORITHMS,
                audience=AUDIENCE,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError:
            raise AuthError("invalid_token") from None

        app_metadata = claims.get("app_metadata")
        role = app_metadata.get("role") if isinstance(app_metadata, dict) else None
        if role not in ROLES:
            raise AuthError("forbidden")
        return Principal(subject=str(claims["sub"]), role=role)


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
