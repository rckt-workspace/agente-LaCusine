"""Guard de seguridad para pruebas destructivas de PostgreSQL.

Las pruebas de integración reinician el esquema `public`. Este módulo decide, sin conectarse a nada,
si una configuración es segura. Cualquier condición no cumplida produce UnsafeTestDatabaseError.

Requisitos (todos obligatorios):
  * APP_ENV=test.
  * LACUSINE_TEST_DB_RESET=I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA (confirmación explícita).
  * Host de loopback exacto: 127.0.0.1, localhost o ::1 (contenedor Docker publicado solo en loopback).
  * Nombre de base en lista blanca exacta: lacusine_test.
  * Usuario y host sin rastros de Supabase; esquema postgresql:// o postgres://.
  * Si DATABASE_URL está definida, debe apuntar exactamente al mismo destino de pruebas.
    Una DATABASE_URL de otro destino indica una configuración de producción presente en el entorno.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import conninfo

ALLOWED_DB_NAMES = frozenset({"lacusine_test"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
RESET_CONFIRMATION = "I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA"
FORBIDDEN_FRAGMENTS = ("supabase", "pooler", "amazonaws", "render.com")
ALLOWED_SCHEMES = ("postgresql://", "postgres://")


class UnsafeTestDatabaseError(RuntimeError):
    """La configuración de pruebas destructivas no es segura. El mensaje nunca incluye credenciales."""


@dataclass(frozen=True)
class TestTarget:
    host: str
    port: str
    dbname: str
    user: str


def validate_test_database_url(url: str, environ: dict[str, str]) -> TestTarget:
    if environ.get("APP_ENV") != "test":
        raise UnsafeTestDatabaseError("APP_ENV debe ser 'test' para ejecutar pruebas destructivas")
    if environ.get("LACUSINE_TEST_DB_RESET") != RESET_CONFIRMATION:
        raise UnsafeTestDatabaseError("falta la confirmación LACUSINE_TEST_DB_RESET")
    if not url.startswith(ALLOWED_SCHEMES):
        raise UnsafeTestDatabaseError("esquema de conexión no permitido")

    target = _parse(url)
    lowered_host = target.host.lower()
    lowered_user = target.user.lower()
    if any(fragment in lowered_host or fragment in lowered_user for fragment in FORBIDDEN_FRAGMENTS):
        raise UnsafeTestDatabaseError("destino de nube o Supabase rechazado")
    if lowered_host not in LOOPBACK_HOSTS:
        raise UnsafeTestDatabaseError("host no permitido: solo loopback")
    if target.dbname not in ALLOWED_DB_NAMES:
        raise UnsafeTestDatabaseError("nombre de base no permitido para pruebas destructivas")

    configured = environ.get("DATABASE_URL")
    if configured:
        other = _parse(configured)
        if (other.host.lower(), other.port, other.dbname) != (target.host.lower(), target.port, target.dbname):
            raise UnsafeTestDatabaseError("DATABASE_URL apunta a otro destino: posible configuración de producción")
    return target


def _parse(url: str) -> TestTarget:
    try:
        parts = conninfo.conninfo_to_dict(url)
    except Exception:  # noqa: BLE001 - nunca se muestra la URL original
        raise UnsafeTestDatabaseError("URL de base de datos inválida") from None
    port = str(parts.get("port") or "5432")
    if not (port.isdigit() and 0 < int(port) <= 65535):
        raise UnsafeTestDatabaseError("puerto inválido en la URL de base de datos")
    return TestTarget(
        host=str(parts.get("host") or ""),
        port=port,
        dbname=str(parts.get("dbname") or ""),
        user=str(parts.get("user") or ""),
    )
