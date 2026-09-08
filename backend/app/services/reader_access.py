from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ReaderAccessKey,
    ResearchCase,
    ResearchCaseMember,
    ResearchContribution,
    User,
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(
        {
            "view",
            "edit",
            "download",
            "cite",
            "run_skill",
            "revise",
            "confirm",
            "manage_members",
            "archive",
            "view_restricted",
        }
    ),
    "reviewer": frozenset(
        {
            "view",
            "download",
            "cite",
            "revise",
            "confirm",
            "view_restricted",
        }
    ),
    "editor": frozenset(
        {
            "view",
            "edit",
            "download",
            "cite",
            "run_skill",
            "revise",
            "view_restricted",
        }
    ),
    "viewer": frozenset({"view", "cite"}),
}


@dataclass(frozen=True)
class IssuedReaderKey:
    record: ReaderAccessKey
    raw_key: str


class ReaderAccessDenied(ValueError):
    pass


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def issue_reader_key(session: Session, user: User, *, label: str) -> IssuedReaderKey:
    raw_key = f"gbr_{secrets.token_urlsafe(32)}"
    record = ReaderAccessKey(
        user_id=user.id,
        key_prefix=raw_key[:12],
        key_sha256=_hash_key(raw_key),
        label=label.strip() or "本机访问密钥",
        status="active",
    )
    session.add(record)
    session.flush()
    return IssuedReaderKey(record=record, raw_key=raw_key)


def reader_keys_initialized(session: Session) -> bool:
    return bool(
        session.scalar(
            select(func.count()).select_from(ReaderAccessKey).where(ReaderAccessKey.status == "active")
        )
    )


def authenticate_reader_key(session: Session, raw_key: str | None) -> User | None:
    supplied = (raw_key or "").strip()
    if not supplied.startswith("gbr_") or len(supplied) < 24:
        return None
    record = session.scalar(
        select(ReaderAccessKey).where(
            ReaderAccessKey.key_prefix == supplied[:12],
            ReaderAccessKey.status == "active",
        )
    )
    if record is None or not secrets.compare_digest(record.key_sha256, _hash_key(supplied)):
        return None
    user = session.get(User, record.user_id)
    if user is None or user.status != "active":
        return None
    record.last_used_at = datetime.now(UTC)
    session.flush()
    return user


def ensure_owner_memberships(session: Session, user: User) -> int:
    created = 0
    for research_case in session.scalars(select(ResearchCase).where(ResearchCase.owner_id == user.id)):
        existing = session.scalar(
            select(ResearchCaseMember.id).where(
                ResearchCaseMember.research_case_id == research_case.id,
                ResearchCaseMember.user_id == user.id,
            )
        )
        if existing is None:
            session.add(
                ResearchCaseMember(
                    research_case_id=research_case.id,
                    user_id=user.id,
                    role="owner",
                    added_by=user.id,
                )
            )
            created += 1
    session.flush()
    return created


def research_case_member(
    session: Session,
    research_case_id: int,
    user_id: int,
) -> ResearchCaseMember | None:
    research_case = session.get(ResearchCase, research_case_id)
    if research_case is not None and research_case.owner_id == user_id:
        member = session.scalar(
            select(ResearchCaseMember).where(
                ResearchCaseMember.research_case_id == research_case_id,
                ResearchCaseMember.user_id == user_id,
            )
        )
        if member is None:
            member = ResearchCaseMember(
                research_case_id=research_case_id,
                user_id=user_id,
                role="owner",
                added_by=user_id,
            )
            session.add(member)
            session.flush()
        return member
    return session.scalar(
        select(ResearchCaseMember).where(
            ResearchCaseMember.research_case_id == research_case_id,
            ResearchCaseMember.user_id == user_id,
        )
    )


def require_case_permission(
    session: Session,
    research_case_id: int,
    user: User,
    permission: str,
) -> ResearchCaseMember:
    case = session.get(ResearchCase, research_case_id)
    if case is not None and (case.scope or {}).get("deleted_at"):
        raise ReaderAccessDenied("项目已删除")
    member = research_case_member(session, research_case_id, user.id)
    if member is None or permission not in ROLE_PERMISSIONS[member.role]:
        raise ReaderAccessDenied(f"当前成员没有专题的{permission}权限")
    return member


def record_contribution(
    session: Session,
    *,
    research_case_id: int,
    user_id: int | None,
    action_type: str,
    object_type: str,
    object_key: str | int,
    details: dict[str, Any] | None = None,
) -> ResearchContribution:
    contribution = ResearchContribution(
        research_case_id=research_case_id,
        user_id=user_id,
        action_type=action_type,
        object_type=object_type,
        object_key=str(object_key),
        details=details or {},
    )
    session.add(contribution)
    session.flush()
    return contribution


def public_member_payload(member: ResearchCaseMember, user: User) -> dict[str, Any]:
    return {
        "id": member.id,
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": member.role,
        "permissions": sorted(ROLE_PERMISSIONS[member.role]),
        "created_at": member.created_at,
        "updated_at": member.updated_at,
    }
