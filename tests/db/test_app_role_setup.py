"""Helpers puros de la configuración del rol de aplicación. Sin red ni base de datos."""

from urllib.parse import unquote, urlsplit

import pytest

from app.db.app_role_setup import SetupError, build_app_database_url, set_env_value


def test_url_codifica_contrasena_con_caracteres_especiales():
    password = "a/b@c:d#e%f?g&h"
    url = build_app_database_url("aws-0-sa.pooler.supabase.com", 5432, "abcdefgh", password)
    parts = urlsplit(url)
    assert parts.scheme == "postgresql"
    assert parts.hostname == "aws-0-sa.pooler.supabase.com"
    assert parts.port == 5432
    assert unquote(parts.username) == "lacusine_app.abcdefgh"
    assert unquote(parts.password) == password
    assert parts.path == "/postgres"
    assert "sslmode=require" in parts.query


def test_set_env_value_reemplaza_una_sola_linea_y_conserva_el_resto():
    original = "APP_ENV=local\nDATABASE_URL=\nAGENT_DRY_RUN=true\n"
    updated = set_env_value(original, "DATABASE_URL", "postgresql://x")
    assert updated == "APP_ENV=local\nDATABASE_URL=postgresql://x\nAGENT_DRY_RUN=true\n"


def test_set_env_value_añade_la_clave_si_no_existe():
    assert set_env_value("APP_ENV=local\n", "DATABASE_URL", "u") == "APP_ENV=local\nDATABASE_URL=u\n"
    assert set_env_value("", "DATABASE_URL", "u") == "DATABASE_URL=u\n"


def test_set_env_value_rechaza_claves_duplicadas():
    with pytest.raises(SetupError) as exc:
        set_env_value("DATABASE_URL=a\nDATABASE_URL=b\n", "DATABASE_URL", "c")
    assert exc.value.code == "env_key_duplicated"


def test_set_env_value_conserva_fin_de_linea_windows():
    updated = set_env_value("APP_ENV=local\r\nDATABASE_URL=\r\n", "DATABASE_URL", "u")
    assert updated == "APP_ENV=local\r\nDATABASE_URL=u\r\n"


def test_el_modulo_no_contiene_credenciales_de_ejemplo():
    from pathlib import Path
    source = Path("app/db/app_role_setup.py").read_text(encoding="utf-8")
    assert "supabase.co" not in source
    assert "print(url" not in source
