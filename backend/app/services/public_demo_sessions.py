"""Guest sessions backed by the Reader's existing member permissions, never a shared demo owner."""

import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.models import ResearchCase, User
from app.models.public_demo import PublicDemoInstallation, PublicDemoSession
from app.services.reader_access import ensure_owner_memberships, issue_reader_key

COOKIE = "guobie_public_demo"
SESSION_SECONDS = 86400
KEY_SECONDS = 3600
LEASE_SECONDS = 105


def now():
    return int(time.time())


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def cipher():
    key = get_settings().public_demo_secret_key
    if key is None:
        raise RuntimeError("GUOBIE_PUBLIC_DEMO_SECRET_KEY is required")
    return Fernet(key.get_secret_value().encode())


def csrf(token):
    return hmac.new(
        get_settings().public_demo_secret_key.get_secret_value().encode(),
        ("csrf:" + token).encode(),
        hashlib.sha256,
    ).hexdigest()


def seal(sid, purpose, value):
    return cipher().encrypt(json.dumps({"sid": sid, "purpose": purpose, "value": value}).encode()).decode()


def unseal(sid, purpose, ciphertext):
    try:
        value = json.loads(cipher().decrypt(ciphertext.encode()))
        if value["sid"] != sid or value["purpose"] != purpose:
            raise ValueError("binding mismatch")
        return value["value"]
    except (InvalidToken, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(401, "体验凭据已失效，请刷新并重新连接") from None


def installation(db):
    marker = db.get(PublicDemoInstallation, 1)
    if marker is None:
        raise HTTPException(503, "公开 Demo 样本尚未初始化")
    return marker


def find_session(db, token):
    if not token or len(token) > 100:
        return None
    row = db.get(PublicDemoSession, digest(token))
    return row if row and row.expires_at > now() else None


def establish(db: Session, token):
    installation(db)
    existing = find_session(db, token)
    if existing:
        return existing, token, False
    token = secrets.token_urlsafe(32)
    sid = digest(token)
    user = User(
        email=f"experience-{sid}@public-demo.invalid", display_name="演示体验者", role="user", status="active"
    )
    db.add(user)
    db.flush()
    access = issue_reader_key(db, user, label="公开 Demo 临时会话")
    # Reuse an existing project description approved in the exported sample manifest.
    from app.services.public_demo_samples import sample_manifest

    template = sample_manifest().get("project", {})
    db.add(
        ResearchCase(
            owner_id=user.id,
            title=template.get("title", "刚果（金）公开资料体验"),
            research_question=template.get("research_question", "核对所选公开资料与证据缺口"),
            scope={"country_iso3": "COD", "public_demo": True},
            status="active",
        )
    )
    db.flush()
    ensure_owner_memberships(db, user)
    row = PublicDemoSession(
        id=sid,
        user_id=user.id,
        reader_key_ciphertext=seal(sid, "reader", access.raw_key),
        connection={},
        created_at=now(),
        expires_at=now() + SESSION_SECONDS,
        busy_until=0,
        quota_window=now(),
        calls=0,
    )
    db.add(row)
    db.commit()
    return row, token, True


def view(row):
    connected = bool(row.provider_key_ciphertext and (row.key_expires_at or 0) > now())
    return {**row.connection, "connected": connected, "expires_at": row.key_expires_at if connected else None}


def provider_key(row):
    if not row or row.expires_at <= now() or not view(row)["connected"]:
        raise HTTPException(409, "请先在设置中连接自己的 API；连接最长保留一小时")
    value = unseal(row.id, "provider", row.provider_key_ciphertext)
    if value["connection"] != row.connection:
        raise HTTPException(409, "连接配置已变化，请重新测试连接")
    return value["key"]


def reserve(db, sid):
    at = now()
    db.execute(
        update(PublicDemoSession)
        .where(PublicDemoSession.id == sid, PublicDemoSession.quota_window <= at - 3600)
        .values(quota_window=at, calls=0)
    )
    result = db.execute(
        update(PublicDemoSession)
        .where(
            PublicDemoSession.id == sid,
            PublicDemoSession.expires_at > at,
            PublicDemoSession.busy_until <= at,
            PublicDemoSession.calls < 30,
        )
        .values(busy_until=at + LEASE_SECONDS, calls=PublicDemoSession.calls + 1)
    )
    db.commit()
    if result.rowcount != 1:
        raise HTTPException(429, "已有模型请求正在运行，或本会话已达到每小时调用上限")
    return at + LEASE_SECONDS


def release(db, sid, lease):
    db.execute(
        update(PublicDemoSession)
        .where(PublicDemoSession.id == sid, PublicDemoSession.busy_until == lease)
        .values(busy_until=0)
    )
    db.commit()


def purge_guest(db, row):
    """Delete only the FK closure of an identified guest, keeping the public sample roots intact.

    The public entry blocks sharing, publishing and global-data writes, so no other
    guest or sample may depend on this user's private project graph.
    """
    user = db.get(User, row.user_id)
    if not user or user.email != f"experience-{row.id}@public-demo.invalid":
        raise HTTPException(409, "会话归属异常，未清空任何数据")
    tables = list(Base.metadata.sorted_tables)
    records = {table.name: {} for table in tables}
    users = Base.metadata.tables["users"]
    user_record = db.execute(select(users).where(users.c.id == user.id)).mappings().one()
    records["users"][(user.id,)] = dict(user_record)
    changed = True
    while changed:
        changed = False
        for table in tables:
            pk = list(table.primary_key.columns)
            for fk in table.foreign_keys:
                parents = records[fk.column.table.name].values()
                values = {parent[fk.column.name] for parent in parents if parent[fk.column.name] is not None}
                if not values:
                    continue
                for found in db.execute(select(table).where(fk.parent.in_(values))).mappings():
                    key = tuple(found[col.name] for col in pk)
                    if key not in records[table.name]:
                        if table.name == "users":
                            raise HTTPException(409, "发现跨会话关联，未清空数据")
                        records[table.name][key] = dict(found)
                        changed = True
    for table in reversed(tables):
        keys = list(records[table.name])
        if keys:
            db.execute(delete(table).where(tuple_(*table.primary_key.columns).in_(keys)))
    db.commit()


def expire(db):
    db.execute(
        update(PublicDemoSession)
        .where(PublicDemoSession.key_expires_at <= now())
        .values(provider_key_ciphertext=None, key_expires_at=None)
    )
    db.commit()
    rows = list(db.scalars(select(PublicDemoSession).where(PublicDemoSession.expires_at <= now()).limit(50)))
    for row in rows:
        purge_guest(db, row)
    return {"expired_sessions": len(rows)}
