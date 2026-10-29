"""Pruebas del guard de seguridad. No se conectan a ninguna base: solo validan la configuración."""

import pytest

from tests.integration.db_guard import UnsafeTestDatabaseError, validate_test_database_url

SAFE_URL = "postgresql://lacusine_test_user:fake-local-pass@127.0.0.1:55439/lacusine_test"
SAFE_ENV = {"APP_ENV": "test", "LACUSINE_TEST_DB_RESET": "I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA"}


def test_accepts_local_loopback_test_database_with_explicit_opt_in():
    target = validate_test_database_url(SAFE_URL, dict(SAFE_ENV))
    assert (target.host, target.port, target.dbname) == ("127.0.0.1", "55439", "lacusine_test")


@pytest.mark.parametrize("env_override", [{"APP_ENV": ""}, {"APP_ENV": "production"}, {"APP_ENV": "development"}])
def test_rejects_when_not_in_test_environment(env_override):
    env = {**SAFE_ENV, **env_override}
    with pytest.raises(UnsafeTestDatabaseError, match="APP_ENV"):
        validate_test_database_url(SAFE_URL, env)


@pytest.mark.parametrize("confirmation", [None, "", "yes", "I_UNDERSTAND"])
def test_rejects_without_exact_reset_confirmation(confirmation):
    env = dict(SAFE_ENV)
    if confirmation is None:
        env.pop("LACUSINE_TEST_DB_RESET")
    else:
        env["LACUSINE_TEST_DB_RESET"] = confirmation
    with pytest.raises(UnsafeTestDatabaseError, match="confirmación"):
        validate_test_database_url(SAFE_URL, env)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pass@db.abcdefghij.supabase.co:5432/lacusine_test",
        "postgresql://postgres.abcdefghij:pass@aws-0-sa-east-1.pooler.supabase.com:6543/lacusine_test",
        "postgresql://user:pass@10.0.0.5:5432/lacusine_test",
        "postgresql://user:pass@db.internal.example.com:5432/lacusine_test",
        "postgresql://user:pass@0.0.0.0:5432/lacusine_test",
    ],
)
def test_rejects_cloud_and_remote_hosts(url):
    with pytest.raises(UnsafeTestDatabaseError):
        validate_test_database_url(url, dict(SAFE_ENV))


def test_rejects_supabase_user_even_on_loopback():
    url = "postgresql://postgres.supabase_project:pass@127.0.0.1:55439/lacusine_test"
    with pytest.raises(UnsafeTestDatabaseError, match="Supabase"):
        validate_test_database_url(url, dict(SAFE_ENV))


@pytest.mark.parametrize(
    "dbname",
    ["postgres", "lacusine", "lacusine_test_prod", "my_test_db", "lacusine_production", "lacusine_test2", "LacuSine_Test_X"],
)
def test_database_name_must_match_allowlist_exactly(dbname):
    url = f"postgresql://user:pass@127.0.0.1:55439/{dbname}"
    with pytest.raises(UnsafeTestDatabaseError, match="nombre de base"):
        validate_test_database_url(url, dict(SAFE_ENV))


def test_rejects_database_url_pointing_to_another_target():
    environ = {**SAFE_ENV, "DATABASE_URL": "postgresql://user:pass@db.example.supabase.co:5432/postgres"}
    with pytest.raises(UnsafeTestDatabaseError, match="otro destino"):
        validate_test_database_url(SAFE_URL, environ)


def test_rejects_database_url_with_same_host_but_different_database():
    environ = {**SAFE_ENV, "DATABASE_URL": "postgresql://user:pass@127.0.0.1:55439/lacusine"}
    with pytest.raises(UnsafeTestDatabaseError, match="otro destino"):
        validate_test_database_url(SAFE_URL, environ)


def test_accepts_database_url_only_when_it_is_the_same_test_target():
    environ = {**SAFE_ENV, "DATABASE_URL": "postgresql://other:otra@127.0.0.1:55439/lacusine_test"}
    target = validate_test_database_url(SAFE_URL, environ)
    assert target.dbname == "lacusine_test"


@pytest.mark.parametrize("url", ["mysql://user:pass@127.0.0.1:55439/lacusine_test", "sqlite:///lacusine_test.db"])
def test_rejects_non_postgres_schemes(url):
    with pytest.raises(UnsafeTestDatabaseError, match="esquema"):
        validate_test_database_url(url, dict(SAFE_ENV))


def test_missing_host_is_rejected_without_defaulting_to_socket():
    with pytest.raises(UnsafeTestDatabaseError, match="host"):
        validate_test_database_url("postgresql:///lacusine_test", dict(SAFE_ENV))


def test_error_messages_never_contain_the_password():
    url = "postgresql://user:clave-muy-secreta@10.0.0.5:5432/lacusine_test"
    with pytest.raises(UnsafeTestDatabaseError) as excinfo:
        validate_test_database_url(url, dict(SAFE_ENV))
    assert "clave-muy-secreta" not in str(excinfo.value)


def test_unparseable_url_error_does_not_echo_input():
    with pytest.raises(UnsafeTestDatabaseError) as excinfo:
        validate_test_database_url("postgresql://user:clave-rota@127.0.0.1:notaport/lacusine_test", dict(SAFE_ENV))
    assert "clave-rota" not in str(excinfo.value)
