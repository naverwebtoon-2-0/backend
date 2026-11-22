from pathlib import Path

from scripts.export_openapi import OUTPUT, render


def test_openapi_file_is_up_to_date():
    assert Path(OUTPUT).read_text(encoding="utf-8") == render(), "scripts/export_openapi.py를 실행하세요"


def test_docs_disabled_in_production(monkeypatch):
    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "app_env", "production")
    app = create_app()
    assert app.docs_url is None and app.openapi_url is None
