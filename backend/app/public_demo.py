"""Public deployment entry: the existing Reader UI, API and workflow implementation.

Use only with a separately initialized sample database. This entry does not expose
collection administration, accounts, publishing, uploads or internal reports.
"""

import hmac
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.public_demo import PublicDemoSession
from app.services import public_demo_sessions as sessions
from app.services.community_auth import CURRENT_REQUEST
from app.services.model_http import ProviderError, normalize_base_url
from app.services.public_demo_runtime import UserAPIRuntime

STATIC = Path(__file__).resolve().parent / "static" / "reader"


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    protocol: str = Field(pattern="^(responses|chat_completions)$")
    base_url: str = Field(min_length=8, max_length=350)
    model: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9._:/-]+$")
    api_key: SecretStr
    consent: bool


def create_app():
    settings = get_settings()
    if not settings.public_demo_enabled or settings.agent_runtime != "user_api":
        raise RuntimeError("Public Reader entry requires explicit demo mode and user_api runtime")
    if settings.public_api_read_only or settings.live_search_provider != "disabled":
        raise RuntimeError("Public Reader uses session-owned writes and no live search")
    sessions.cipher()
    origins = {
        origin.strip().rstrip("/")
        for origin in settings.public_demo_allowed_origins.split(",")
        if origin.strip()
    }
    if not origins or (
        settings.environment == "production" and any(not x.startswith("https://") for x in origins)
    ):
        raise RuntimeError("Public Reader requires exact allowed origins")
    app = FastAPI(title="国别智枢 · 公开体验", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        path = request.url.path
        write = request.method not in {"GET", "HEAD", "OPTIONS"}
        is_api = path.startswith("/api/v1/")
        created, token, row = False, None, None
        context = CURRENT_REQUEST.set(request)
        try:
            if is_api:
                if write:
                    if (
                        request.headers.get("origin") not in origins
                        or request.headers.get("sec-fetch-site") == "cross-site"
                    ):
                        raise HTTPException(403, "请从公开 Demo 原页面提交操作")
                    # Only Reader-owned project and research operations may write.
                    if not path.startswith("/api/v1/reader/"):
                        raise HTTPException(403, "公开体验不提供数据采集或管理操作")
                    forbidden = (
                        "/auth/",
                        "/members",
                        "/publication",
                        "/field-library/",
                        "/publish",
                        "/grants",
                        "/schedules",
                        "/tracking-schedules",
                    )
                    if any(part in path for part in forbidden):
                        raise HTTPException(403, "公开体验不提供账号管理、对外发布或后台调度")
                    if path.endswith(("/field-materials", "/uploads", "/materials", "/versions")):
                        # Existing sample review remains available; public uploads are disabled.
                        raise HTTPException(403, "公开体验仅使用已选示例材料，不接收私人文件")
                    chunks, size = [], 0
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > 256 * 1024:
                            raise HTTPException(413, "本次请求过大，请缩短研究内容")
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                with get_session_factory()() as db:
                    sessions.installation(db)
                    token = request.cookies.get(sessions.COOKIE, "")
                    row = sessions.find_session(db, token)
                    if write and row is None:
                        raise HTTPException(401, "体验会话已过期，请刷新页面")
                    if not row:
                        row, token, created = sessions.establish(db, token)
                    if write and not hmac.compare_digest(
                        request.headers.get("x-csrf-token", ""), sessions.csrf(token)
                    ):
                        raise HTTPException(403, "会话校验失败，请刷新页面")
                    reader_key = sessions.unseal(row.id, "reader", row.reader_key_ciphertext)
                    request.state.public_demo_session_id = row.id
                    # The browser cannot select another Reader identity.
                    request.scope["headers"] = [
                        (k, v) for k, v in request.scope["headers"] if k.lower() != b"x-reader-key"
                    ]
                    request.scope["headers"].append((b"x-reader-key", reader_key.encode()))
                    request._headers = None
                    # Starlette rehydrates headers lazily only when the attribute is absent.
                    del request._headers
            response = await call_next(request)
            if created:
                secure = settings.environment == "production"
                options = {
                    "secure": secure,
                    "samesite": "strict",
                    "max_age": sessions.SESSION_SECONDS,
                    "path": "/",
                }
                response.set_cookie(sessions.COOKIE, token, httponly=True, **options)
                response.set_cookie("guobie_csrf", sessions.csrf(token), httponly=False, **options)
            if is_api:
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["Vary"] = "Cookie"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Frame-Options"] = "DENY"
            return response
        except HTTPException as exc:
            return JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers={"Cache-Control": "no-store"}
            )
        except SQLAlchemyError:
            return JSONResponse({"detail": "演示数据库暂不可用，请稍后刷新"}, status_code=503)
        finally:
            CURRENT_REQUEST.reset(context)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, exc):
        return JSONResponse({"detail": "请求格式无效，请核对输入内容"}, status_code=422)

    @app.exception_handler(ProviderError)
    async def provider_error(_, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=400)

    @app.get("/api/health")
    def health():
        with get_session_factory()() as db:
            marker = sessions.installation(db)
            return {
                "status": "ok",
                "edition": "existing-reader-public-demo",
                "sample_version": marker.sample_version,
            }

    @app.get("/api/v1/reader/auth/status")
    def identity_status():
        return {
            "initialized": True,
            "user": None,
            "setup_allowed": False,
            "demo_available": True,
            "public_demo": True,
        }

    @app.get("/api/v1/reader/field-demo")
    def field_demo():
        from app.services.public_demo_samples import sample_manifest

        return sample_manifest().get("field_demo", {"materials": [], "results": {}})

    @app.get("/api/v1/reader/model-connection")
    def connection(request: Request):
        with get_session_factory()() as db:
            return sessions.view(db.get(PublicDemoSession, request.state.public_demo_session_id))

    @app.post("/api/v1/reader/model-connection/test")
    def test_connection(payload: ConnectionInput, request: Request):
        secret = payload.api_key.get_secret_value()
        if (
            not payload.consent
            or not 8 <= len(secret) <= 1024
            or any(ord(c) < 33 or ord(c) > 126 for c in secret)
        ):
            raise HTTPException(422, "请确认服务地址、使用授权和 API Key 格式")
        config = {
            "protocol": payload.protocol,
            "base_url": normalize_base_url(payload.base_url),
            "model": payload.model,
        }
        sid = request.state.public_demo_session_id
        with get_session_factory()() as db:
            lease = sessions.reserve(db, sid)
        try:
            runtime = UserAPIRuntime(sid=sid, connection=config, api_key=secret, timeout_seconds=30)
            runtime.invoke(
                instructions='连接测试，只返回 JSON {"ok":true}。',
                prompt="不包含研究资料。",
                schema={
                    "type": "object",
                    "properties": {"ok": {"const": True}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                max_tokens=128,
            )
            with get_session_factory()() as db:
                row = db.get(PublicDemoSession, sid)
                if not row or row.expires_at <= sessions.now():
                    raise HTTPException(401, "会话已失效，连接未保存")
                row.connection = config
                row.provider_key_ciphertext = sessions.seal(
                    sid, "provider", {"connection": config, "key": secret}
                )
                row.key_expires_at = min(sessions.now() + sessions.KEY_SECONDS, row.expires_at)
                db.commit()
                return sessions.view(row)
        finally:
            with get_session_factory()() as db:
                sessions.release(db, sid, lease)

    @app.delete("/api/v1/reader/model-connection")
    def disconnect(request: Request):
        with get_session_factory()() as db:
            row = db.get(PublicDemoSession, request.state.public_demo_session_id)
            row.provider_key_ciphertext, row.key_expires_at = None, None
            db.commit()
        return {"connected": False}

    @app.delete("/api/v1/reader/demo-session")
    def reset(request: Request):
        with get_session_factory()() as db:
            row = db.get(PublicDemoSession, request.state.public_demo_session_id)
            if row.busy_until > sessions.now():
                raise HTTPException(409, "模型仍在运行，请等待完成后清空")
            sessions.purge_guest(db, row)
        response = JSONResponse({"cleared": True})
        response.delete_cookie(sessions.COOKIE, path="/")
        response.delete_cookie("guobie_csrf", path="/")
        return response

    @app.get("/api/maintenance/expire")
    def expire(request: Request):
        expected = (
            settings.public_demo_cron_secret.get_secret_value() if settings.public_demo_cron_secret else ""
        )
        if not expected or not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + expected
        ):
            raise HTTPException(404, "Not found")
        with get_session_factory()() as db:
            return sessions.expire(db)

    # Reuse production Reader routers directly. No replacement research UI or fake API replies.
    from app.api import (
        field_community,
        project_materials,
        project_skill_turns,
        project_workspace,
        research,
        skill_workflows,
        sources,
        structured_data,
        workflow_documents,
    )

    for module in (
        research,
        project_workspace,
        project_materials,
        project_skill_turns,
        skill_workflows,
        workflow_documents,
        field_community,
        sources,
        structured_data,
    ):
        app.include_router(module.router)

    @app.get("/")
    def home():
        return RedirectResponse("/reader/")

    @app.get("/reader/")
    @app.get("/reader/index.html")
    def reader_index():
        original = (STATIC / "index.html").read_text()
        # Add only the model-settings/session integration to the existing entry.
        original = original.replace(
            '<script src="./app.js', '<script src="./public-reader.js"></script>\n  <script src="./app.js'
        )
        return HTMLResponse(original, headers={"Cache-Control": "no-cache"})

    @app.get("/reader/public-sample.json")
    def sample_info():
        from app.services.public_demo_samples import sample_manifest

        manifest = sample_manifest()
        return {"version": manifest["version"], "summary": manifest["summary"], "notice": manifest["notice"]}

    @app.get("/robots.txt")
    def robots():
        return HTMLResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")

    app.mount("/reader", StaticFiles(directory=STATIC, html=True), name="reader")
    return app


app = create_app()
