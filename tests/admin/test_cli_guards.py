"""Guardas de la CLI administrativa. Sin base de datos: solo se evalúan las condiciones antes de conectar."""

from uuid import UUID

import pytest

from app.admin.cli import GuardError, build_parser, check_guards

CASE = "6f1c2a3e-1111-4a2b-9c3d-000000000001"
LOCAL_ADMIN_URL = "postgresql://lacusine_admin:clave-ficticia@127.0.0.1:55439/lacusine_test"
APP_URL = "postgresql://lacusine_app:otra-ficticia@127.0.0.1:55439/lacusine_test"


def _args(*argv):
    return build_parser().parse_args(list(argv))


def _env(**overrides):
    env = {
        "LACUSINE_ADMIN_DATABASE_URL": LOCAL_ADMIN_URL,
        "DATABASE_URL": APP_URL,
        "LACUSINE_ENVIRONMENT": "local",
        "LACUSINE_ADMIN_ENABLED": "true",
        "LACUSINE_ADMIN_ROLE": "lacusine_admin",
    }
    env.update(overrides)
    return env


def _code(fn, *args):
    with pytest.raises(GuardError) as excinfo:
        fn(*args)
    return excinfo.value.code


def _mutation(confirm=CASE, environment="local"):
    return _args("--environment", environment, "--operator", "ana",
                 "resolve-outbound", "--id", CASE, "--confirm", confirm, "--reason", "motivo",
                 "--to", "failed", "--evidence", "ref-1")


def _read():
    return _args("--environment", "local", "--operator", "ana", "status")


def test_status_needs_only_environment_and_admin_url():
    assert check_guards(_read(), _env()) == LOCAL_ADMIN_URL


def test_admin_url_must_be_configured():
    env = _env()
    del env["LACUSINE_ADMIN_DATABASE_URL"]
    assert _code(check_guards, _read(), env) == "admin_database_url_missing"


def test_admin_url_must_not_be_the_application_url():
    assert _code(check_guards, _read(), _env(LACUSINE_ADMIN_DATABASE_URL=APP_URL)) == "admin_url_must_differ_from_app_url"


def test_environment_must_be_declared_and_match_the_command():
    assert _code(check_guards, _read(), _env(LACUSINE_ENVIRONMENT="")) == "environment_not_set"
    assert _code(check_guards, _read(), _env(LACUSINE_ENVIRONMENT="pilot")) == "environment_mismatch"


def test_cloud_host_is_refused_unless_explicitly_allowed():
    cloud = "postgresql://lacusine_admin:x@db.abc.supabase.co:5432/lacusine_test"
    assert _code(check_guards, _read(), _env(LACUSINE_ADMIN_DATABASE_URL=cloud)) == "remote_host_not_allowed"
    assert check_guards(_read(), _env(LACUSINE_ADMIN_DATABASE_URL=cloud, LACUSINE_ADMIN_ALLOW_REMOTE="true"))


def test_mutations_are_disabled_by_default():
    assert _code(check_guards, _mutation(), _env(LACUSINE_ADMIN_ENABLED="false")) == "mutations_disabled"


def test_mutation_requires_confirmation_equal_to_the_case_identifier():
    assert _code(check_guards, _mutation(confirm="si"), _env()) == "confirmation_mismatch"
    assert _code(check_guards, _mutation(confirm=""), _env()) == "confirmation_mismatch"


def test_mutation_requires_the_least_privilege_role_to_be_declared():
    assert _code(check_guards, _mutation(), _env(LACUSINE_ADMIN_ROLE="")) == "admin_role_missing"


def test_production_mutations_need_an_extra_explicit_flag():
    env = _env(LACUSINE_ENVIRONMENT="production")
    assert _code(check_guards, _mutation(environment="production"), env) == "production_not_allowed"
    env["LACUSINE_ADMIN_ALLOW_PRODUCTION"] = "true"
    assert check_guards(_mutation(environment="production"), env)


def test_mutation_identifier_must_be_a_uuid_even_when_confirmation_matches():
    args = _args("--environment", "local", "--operator", "ana", "resolve-event", "--id", "wamid.X",
                 "--confirm", "wamid.X", "--reason", "motivo", "--to", "retry")
    assert _code(check_guards, args, _env()) == "id_must_be_uuid"


def test_inspect_accepts_text_identifiers_for_reservations_only_by_type():
    args = build_parser().parse_args(["--environment", "local", "--operator", "a", "inspect",
                                      "--type", "budget_reservation", "--id", "wamid.X:reply"])
    assert args.id == "wamid.X:reply"
    assert isinstance(build_parser().parse_args(["--environment", "local", "--operator", "a", "inspect",
                                                 "--type", "human_handoff", "--id", CASE]).id, UUID)
