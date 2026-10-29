from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_liveness_does_not_depend_on_database():
    client = TestClient(create_app(settings=Settings(meta_app_secret=None, meta_verify_token=None)))
    assert client.get("/health").json() == {"status": "ok"}


def test_readiness_reports_not_configured_without_database():
    client = TestClient(create_app(settings=Settings(meta_app_secret=None, meta_verify_token=None)))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_configured"}
