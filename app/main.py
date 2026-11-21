import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.errors import install_error_handlers
from app.logging import RequestContextMiddleware, configure_logging

logger = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    docs = not settings.is_production
    app = FastAPI(
        title="naverwebtoon 2.0 API",
        version="0.3",
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    install_error_handlers(app)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=False,  # 쿠키 미사용, Bearer 헤더만
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "revision": settings.revision}, headers=_NO_STORE)

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            logger.warning("readiness check failed", exc_info=True)
            return JSONResponse({"status": "unavailable"}, status_code=503, headers=_NO_STORE)
        return JSONResponse({"status": "ok"}, headers=_NO_STORE)

    from app.api.v1 import router as v1_router

    app.include_router(v1_router)
    if settings.storage_backend == "fake" and not settings.is_production:
        from app.api.fake_storage import router as fake_storage_router

        app.include_router(fake_storage_router)
    return app


app = create_app()
