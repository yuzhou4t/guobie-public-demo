"""Invitation accounts and opaque, revocable sessions; no credentials in URLs or logs."""

import hashlib
import secrets
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, Response
from pwdlib import PasswordHash
from sqlalchemy import delete, func, select

from app.models import User
from app.models.field_community import ReaderAccount, ReaderLoginAttempt, ReaderSession

PASSWORDS = PasswordHash.recommended()
COOKIE = "guobie_session"
CSRF_COOKIE = "guobie_csrf"
SESSION_SECONDS = 12 * 60 * 60
CURRENT_REQUEST = ContextVar("reader_http_request", default=None)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def accounts_initialized(db) -> bool:
    return db.scalar(select(ReaderAccount.user_id).limit(1)) is not None


def check_origin(request: Request):
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != expected):
        raise HTTPException(403, "请从平台原页面提交操作")


def authenticate_session(db, request: Request, *, required=False):
    raw = request.cookies.get(COOKIE, "")
    row = db.scalar(select(ReaderSession).where(ReaderSession.token_sha256 == digest(raw))) if raw else None
    now = datetime.now(UTC)
    user = db.get(User, row.user_id) if row and not row.revoked and utc(row.expires_at) > now else None
    account = db.get(ReaderAccount, user.id) if user else None
    if not user or user.status != "active" or not account or not account.password_hash:
        if required:
            raise HTTPException(401, "请先登录平台账号")
        return None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or not secrets.compare_digest(digest(csrf), row.csrf_sha256):
            raise HTTPException(403, "登录校验已失效，请刷新页面后重试")
    return user


def issue_session(db, user, response: Response, request: Request):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        ReaderSession(
            user_id=user.id,
            token_sha256=digest(token),
            csrf_sha256=digest(csrf),
            expires_at=datetime.now(UTC) + timedelta(seconds=SESSION_SECONDS),
        )
    )
    secure = request.url.scheme == "https"
    options = {"max_age": SESSION_SECONDS, "secure": secure, "samesite": "strict", "path": "/"}
    response.set_cookie(COOKIE, token, httponly=True, **options)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **options)
    response.headers["Cache-Control"] = "no-store"
    # Retain only live sessions; never store bearer tokens in database.
    db.execute(delete(ReaderSession).where(ReaderSession.expires_at < datetime.now(UTC)))


def password_hash(password: str) -> str:
    if len(password) < 12 or len(password) > 256:
        raise HTTPException(422, "密码长度应为 12 至 256 个字符")
    return PASSWORDS.hash(password)


def normalize_email(email: str) -> str:
    email = email.strip().casefold()
    if len(email) > 200 or "@" not in email or any(c.isspace() for c in email):
        raise HTTPException(422, "请输入有效邮箱")
    return email


def login(db, email, password, request):
    identity = digest(email + ":" + (request.client.host if request.client else "unknown"))
    now = datetime.now(UTC)
    failures = db.scalar(
        select(func.count())
        .select_from(ReaderLoginAttempt)
        .where(
            ReaderLoginAttempt.identity_hash == identity,
            ReaderLoginAttempt.created_at > now - timedelta(minutes=15),
        )
    )
    if failures >= 5:
        raise HTTPException(429, "尝试次数过多，请 15 分钟后重试")
    user = db.scalar(select(User).where(func.lower(User.email) == email, User.status == "active"))
    account = db.get(ReaderAccount, user.id) if user else None
    valid = False
    if account and account.password_hash:
        try:
            valid = PASSWORDS.verify(password, account.password_hash)
        except Exception as exc:
            raise HTTPException(503, "账号凭据需要管理员检查") from exc
    else:
        # A constant-cost check avoids a fast unknown-account branch.
        PASSWORDS.hash(password)
    if not valid:
        db.add(ReaderLoginAttempt(identity_hash=identity))
        db.commit()
        raise HTTPException(401, "邮箱或密码不正确")
    db.execute(
        delete(ReaderLoginAttempt).where(
            (ReaderLoginAttempt.identity_hash == identity)
            | (ReaderLoginAttempt.created_at < now - timedelta(days=1))
        )
    )
    return user
