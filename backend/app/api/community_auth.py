"""Local owner setup, invitation acceptance and session login for the Reader."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from app.api.research import (
    DbSession,
    ReaderActor,
    require_internal_reader_access,
    require_reader_write_access,
)
from app.models import ReaderAccessKey, User
from app.models.field_community import ReaderAccount, ReaderInvitation, ReaderSession
from app.services import community_auth as auth

router = APIRouter(
    prefix="/api/v1/reader/auth",
    tags=["reader-accounts"],
    dependencies=[Depends(require_internal_reader_access)],
)
WRITE = [Depends(require_reader_write_access)]


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=12, max_length=256, repr=False)


class Setup(Credentials):
    display_name: str = Field(min_length=1, max_length=120)


class Invite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)


class Accept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=128, repr=False)
    password: str = Field(min_length=12, max_length=256, repr=False)


def identity(user):
    return {"id": user.id, "display_name": user.display_name, "email": user.email, "role": user.role}


@router.get("/status")
def status(db: DbSession, request: Request, response: Response):
    from app.api.field_demo import available

    user = auth.authenticate_session(db, request)
    response.headers["Cache-Control"] = "no-store"
    return {
        "demo_available": available(request),
        "initialized": auth.accounts_initialized(db),
        "user": identity(user) if user else None,
        "setup_allowed": request.client is not None
        and request.client.host in {"127.0.0.1", "::1", "testclient"},
    }


@router.post("/setup", dependencies=WRITE)
def setup(payload: Setup, db: DbSession, request: Request, response: Response):
    auth.check_origin(request)
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "首次负责人账号只能在本机建立")
    # Serialize first-owner initialization, preserving the demo owner's IDs and data.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text

        db.execute(text("SELECT pg_advisory_xact_lock(71403001)"))
    if auth.accounts_initialized(db):
        raise HTTPException(409, "负责人账号已经建立，请登录")
    from app.api.research import _reader_demo_owner
    from app.services.reader_access import authenticate_reader_key, reader_keys_initialized

    if reader_keys_initialized(db):
        owner = authenticate_reader_key(db, request.headers.get("X-Reader-Key"))
        demo = db.scalar(select(User).where(User.email == "mvp-demo@local.invalid"))
        if not owner or (owner.role != "admin" and (not demo or owner.id != demo.id)):
            raise HTTPException(403, "请先使用现有负责人访问码完成账号升级")
    else:
        owner = _reader_demo_owner(db)
    email = auth.normalize_email(payload.email)
    other = db.scalar(select(User).where(func.lower(User.email) == email, User.id != owner.id))
    if other:
        raise HTTPException(409, "该邮箱已经属于其他成员")
    owner.email, owner.display_name, owner.role = email, payload.display_name.strip(), "admin"
    db.add(ReaderAccount(user_id=owner.id, password_hash=auth.password_hash(payload.password)))
    db.execute(update(ReaderAccessKey).values(status="revoked", revoked_at=datetime.now(UTC)))
    auth.issue_session(db, owner, response, request)
    db.commit()
    return {"user": identity(owner)}


@router.post("/login", dependencies=WRITE)
def login(payload: Credentials, db: DbSession, request: Request, response: Response):
    auth.check_origin(request)
    user = auth.login(db, auth.normalize_email(payload.email), payload.password, request)
    auth.issue_session(db, user, response, request)
    db.commit()
    return {"user": identity(user)}


@router.post("/logout", dependencies=WRITE)
def logout(db: DbSession, request: Request, response: Response, actor: ReaderActor):
    db.execute(
        update(ReaderSession)
        .where(ReaderSession.token_sha256 == auth.digest(request.cookies.get(auth.COOKIE, "")))
        .values(revoked=True)
    )
    db.commit()
    response.delete_cookie(auth.COOKIE, path="/")
    response.delete_cookie(auth.CSRF_COOKIE, path="/")
    return {"signed_out": True}


@router.post("/invitations", dependencies=WRITE)
def invite(payload: Invite, db: DbSession, actor: ReaderActor, request: Request, response: Response):
    if actor.role != "admin" or not db.get(ReaderAccount, actor.id):
        raise HTTPException(403, "仅平台管理员可以邀请账号")
    email = auth.normalize_email(payload.email)
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user and db.get(ReaderAccount, user.id) and db.get(ReaderAccount, user.id).password_hash:
        raise HTTPException(409, "该成员已有账号，可直接添加到项目")
    if user is None:
        user = User(email=email, display_name=payload.display_name.strip(), role="user", status="active")
        db.add(user)
        db.flush()
    if not db.get(ReaderAccount, user.id):
        db.add(ReaderAccount(user_id=user.id))
    # Reissuing invalidates earlier unused invitations for this account.
    db.execute(
        update(ReaderInvitation)
        .where(ReaderInvitation.user_id == user.id, ReaderInvitation.consumed_at.is_(None))
        .values(expires_at=datetime.now(UTC))
    )
    raw = secrets.token_urlsafe(32)
    row = ReaderInvitation(
        user_id=user.id,
        invited_by=actor.id,
        token_sha256=auth.digest(raw),
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    db.add(row)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    # Fragment never reaches server logs or HTTP Referer.
    return {
        "user_id": user.id,
        "expires_at": row.expires_at,
        "invitation_url": str(request.base_url).rstrip("/") + "/reader/#/invite/" + raw,
    }


@router.post("/accept-invitation", dependencies=WRITE)
def accept(payload: Accept, db: DbSession, request: Request, response: Response):
    auth.check_origin(request)
    invitation = db.scalar(
        select(ReaderInvitation)
        .where(ReaderInvitation.token_sha256 == auth.digest(payload.token))
        .with_for_update()
    )
    if not invitation or invitation.consumed_at or auth.utc(invitation.expires_at) <= datetime.now(UTC):
        raise HTTPException(410, "邀请已使用或已过期，请联系管理员重新邀请")
    user = db.get(User, invitation.user_id)
    account = db.get(ReaderAccount, user.id)
    if user.status != "active" or not account or account.password_hash:
        raise HTTPException(410, "该邀请当前不可使用")
    account.password_hash = auth.password_hash(payload.password)
    invitation.consumed_at = datetime.now(UTC)
    auth.issue_session(db, user, response, request)
    db.commit()
    return {"user": identity(user)}


@router.post("/accounts/{user_id}/disable", dependencies=WRITE)
def disable(user_id: int, db: DbSession, actor: ReaderActor):
    if actor.role != "admin" or user_id == actor.id:
        raise HTTPException(403, "仅管理员可以停用其他成员")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "成员不存在")
    user.status = "disabled"
    db.execute(update(ReaderSession).where(ReaderSession.user_id == user_id).values(revoked=True))
    db.commit()
    return {"disabled": True}
