"""Cargador de .env para desarrollo local: opt-in, no sobrescribe, no imprime valores."""

from pathlib import Path

from app.core.env import dotenv_enabled, load_local_env


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_key_value_pairs_skipping_comments_and_blanks(tmp_path):
    path = _write(tmp_path, "# comentario\n\nMETA_VERIFY_TOKEN=valor-uno\n  WORKER_BATCH_SIZE = 7 \n")
    env: dict[str, str] = {}
    assert sorted(load_local_env(path, env)) == ["META_VERIFY_TOKEN", "WORKER_BATCH_SIZE"]
    assert env == {"META_VERIFY_TOKEN": "valor-uno", "WORKER_BATCH_SIZE": "7"}


def test_never_overrides_existing_environment(tmp_path):
    path = _write(tmp_path, "DATABASE_URL=postgresql://local\n")
    env = {"DATABASE_URL": "postgresql://render"}
    assert load_local_env(path, env) == []
    assert env["DATABASE_URL"] == "postgresql://render"


def test_strips_matching_quotes_and_skips_empty_values(tmp_path):
    path = _write(tmp_path, 'A_KEY="con espacios"\nB_KEY=\'comillas\'\nC_KEY=\n')
    env: dict[str, str] = {}
    load_local_env(path, env)
    assert env == {"A_KEY": "con espacios", "B_KEY": "comillas"}


def test_ignores_invalid_key_names(tmp_path):
    path = _write(tmp_path, "lower_case=1\nVALID_ONE=2\nBAD-NAME=3\n")
    env: dict[str, str] = {}
    load_local_env(path, env)
    assert env == {"VALID_ONE": "2"}


def test_missing_file_is_not_an_error(tmp_path):
    assert load_local_env(tmp_path / "no-existe.env", {}) == []


def test_loader_returns_names_not_values(tmp_path):
    path = _write(tmp_path, "META_ACCESS_TOKEN=SECRETO-NO-DEBE-SALIR\n")
    names = load_local_env(path, {})
    assert names == ["META_ACCESS_TOKEN"]
    assert all("SECRETO" not in name for name in names)


def test_disabled_unless_explicitly_requested():
    assert dotenv_enabled({}) is False
    assert dotenv_enabled({"LACUSINE_LOAD_DOTENV": "false"}) is False
    assert dotenv_enabled({"LACUSINE_LOAD_DOTENV": "true"}) is True
