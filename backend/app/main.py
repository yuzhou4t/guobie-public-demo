import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.community_auth import router as community_auth_router
from app.api.field_community import router as field_community_router
from app.api.field_demo import router as field_demo_router
from app.api.health import router as health_router
from app.api.partner import router as partner_router
from app.api.project_materials import router as project_materials_router
from app.api.project_skill_turns import router as project_skill_turns_router
from app.api.project_workspace import router as project_workspace_router
from app.api.public_data import router as public_data_router
from app.api.research import router as research_router
from app.api.skill_workflows import router as skill_workflows_router
from app.api.sources import router as sources_router
from app.api.structured_data import router as structured_data_router
from app.core.config import get_settings

READER_STATIC_DIR = Path(__file__).resolve().parent / "static" / "reader"
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def create_app() -> FastAPI:
    settings = get_settings()
    production = settings.environment == "production"
    if production and settings.public_api_key is None:
        raise RuntimeError("GUOBIE_PUBLIC_API_KEY is required in production")
    if production and not settings.public_api_read_only:
        raise RuntimeError("GUOBIE_PUBLIC_API_READ_ONLY must be true in production")
    if production and settings.agent_runtime in {"codex_local", "coze_test"}:
        raise RuntimeError(f"GUOBIE_AGENT_RUNTIME={settings.agent_runtime} is not allowed in production")
    if production and settings.live_search_provider in {"codex_local", "coze_test"}:
        raise RuntimeError(
            f"GUOBIE_LIVE_SEARCH_PROVIDER={settings.live_search_provider} is not allowed in production"
        )
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    from app.services.community_auth import CURRENT_REQUEST

    @app.middleware("http")
    async def bind_reader_request(request, call_next):
        token = CURRENT_REQUEST.set(request)
        try:
            return await call_next(request)
        finally:
            CURRENT_REQUEST.reset(token)

    if settings.public_api_key is not None:
        expected_api_key = settings.public_api_key.get_secret_value()

        @app.middleware("http")
        async def require_api_key(request, call_next):
            path = request.url.path
            is_partner_api = path == "/api/v1/partner" or path.startswith("/api/v1/partner/")
            if path.startswith("/api/v1") and not is_partner_api:
                provided_api_key = request.headers.get("X-API-Key", "")
                if not secrets.compare_digest(provided_api_key, expected_api_key):
                    return JSONResponse(status_code=401, content={"detail": "invalid API key"})
            return await call_next(request)

    if settings.parsed_cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.parsed_cors_allowed_origins,
            allow_methods=["GET", "HEAD", "OPTIONS"],
            allow_headers=["*"],
        )
    app.include_router(health_router)
    app.include_router(community_auth_router)
    app.include_router(field_community_router)
    app.include_router(field_demo_router)
    app.include_router(sources_router)
    app.include_router(structured_data_router)
    app.include_router(public_data_router)
    app.include_router(research_router)
    app.include_router(project_workspace_router)
    app.include_router(project_materials_router)
    from app.api.tracking_schedules import router as tracking_schedules_router
    from app.api.workflow_documents import router as workflow_documents_router

    app.include_router(tracking_schedules_router)
    app.include_router(workflow_documents_router)
    app.include_router(skill_workflows_router)
    app.include_router(project_skill_turns_router)
    app.include_router(partner_router)

    from app.services.field_access import FieldAccessDenied

    @app.exception_handler(FieldAccessDenied)
    async def field_access_denied(request, exc):
        return JSONResponse(
            status_code=403, content={"detail": str(exc)}, headers={"Cache-Control": "private, no-store"}
        )

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(request, exc):
        sensitive = {"password", "token", "content_base64", "x-reader-key"}
        errors = []
        for error in exc.errors():
            entry = {k: v for k, v in error.items() if k != "ctx"}
            if any(str(part).casefold() in sensitive for part in error.get("loc", [])):
                entry.pop("input", None)
            errors.append(entry)
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/partner-openapi.json", include_in_schema=False)
    def partner_openapi():
        return get_openapi(
            title="国别智枢合作方数据 API",
            version="1.0.0",
            routes=partner_router.routes,
        )

    @app.get("/partner-docs", include_in_schema=False)
    def partner_docs():
        return get_swagger_ui_html(
            openapi_url="/partner-openapi.json",
            title="国别智枢合作方数据 API 文档",
        )

    @app.get("/", include_in_schema=False)
    def reader_home() -> RedirectResponse:
        return RedirectResponse(url="/partner-docs" if production else "/reader/")

    @app.get("/resource-portal/", include_in_schema=False)
    def resource_portal() -> FileResponse:
        return FileResponse(REPORTS_DIR / "resource_portal_showcase.html")

    @app.get("/resource-portal/resource_portal_data.js", include_in_schema=False)
    def resource_portal_data() -> FileResponse:
        return FileResponse(REPORTS_DIR / "resource_portal_data.js", media_type="text/javascript")

    app.mount(
        "/reader",
        StaticFiles(directory=READER_STATIC_DIR, html=True),
        name="reader-mvp",
    )
    return app


app = create_app()
