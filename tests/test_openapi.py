from pathlib import Path

from fastapi.testclient import TestClient

from scripts.export_openapi import OUTPUT, render


def test_openapi_file_is_up_to_date():
    assert Path(OUTPUT).read_text(encoding="utf-8") == render(), "scripts/export_openapi.py를 실행하세요"


def test_docs_disabled_in_production(monkeypatch):
    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "app_env", "production")
    app = create_app()
    assert app.docs_url is None and app.openapi_url is None


def test_production_swagger_exposes_bearer_authorization(monkeypatch):
    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "app_env", "production")
    monkeypatch.setattr(get_settings(), "production_docs_enabled", True)
    with TestClient(create_app()) as client:
        docs = client.get("/v1/docs")
        assert docs.status_code == 200 and "/v1/openapi.json" in docs.text
        schema = client.get("/v1/openapi.json").json()
        assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
        assert schema["paths"]["/v1/me"]["get"]["security"] == [{"BearerAuth": []}]
        assert client.get("/v1/me").status_code == 401
