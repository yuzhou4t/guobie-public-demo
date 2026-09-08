"""Identity, explicit project grants and public descriptions for field collections."""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.entities import ID_TYPE, JSON_TYPE, TimestampMixin


class ReaderAccount(TimestampMixin, Base):
    __tablename__ = "reader_accounts"
    user_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str | None] = mapped_column(Text)


class ReaderSession(Base):
    __tablename__ = "reader_sessions"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    user_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"), index=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReaderInvitation(Base):
    __tablename__ = "reader_invitations"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    user_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"), index=True)
    invited_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReaderLoginAttempt(Base):
    __tablename__ = "reader_login_attempts"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class FieldAssetVersion(Base):
    __tablename__ = "field_asset_versions"
    __table_args__ = (UniqueConstraint("material_id", "version_no"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    material_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_materials.id"), index=True)
    version_no: Mapped[int] = mapped_column(default=1)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FieldPublication(TimestampMixin, Base):
    __tablename__ = "field_publications"
    __table_args__ = (CheckConstraint("status in ('draft','published','withdrawn')", name="status"),)
    material_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_materials.id"), primary_key=True)
    asset_version_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_asset_versions.id"))
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    revision_no: Mapped[int] = mapped_column(default=1)
    public_title: Mapped[str] = mapped_column(String(240))
    introduction: Mapped[str] = mapped_column(Text, default="")
    excerpts: Mapped[str] = mapped_column(Text, default="")
    image_key: Mapped[str | None] = mapped_column(String(200))
    image_sha256: Mapped[str | None] = mapped_column(String(64))


class FieldProjectGrant(TimestampMixin, Base):
    __tablename__ = "field_project_grants"
    __table_args__ = (
        UniqueConstraint("asset_version_id", "research_case_id"),
        CheckConstraint("status in ('pending','approved','rejected','revoked')", name="status"),
    )
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    asset_version_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_asset_versions.id"), index=True)
    research_case_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("research_cases.id"), index=True)
    requested_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(Text)
    permissions: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    ai_provider: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FieldProjectLink(Base):
    __tablename__ = "field_project_links"
    __table_args__ = (UniqueConstraint("asset_version_id", "research_case_id"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    asset_version_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_asset_versions.id"), index=True)
    research_case_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("research_cases.id"), index=True)
    grant_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_project_grants.id"))
    added_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FieldComment(TimestampMixin, Base):
    __tablename__ = "field_comments"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    material_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_materials.id"), index=True)
    author_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    parent_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("field_comments.id"))
    body: Mapped[str] = mapped_column(Text)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    revision_no: Mapped[int] = mapped_column(default=1)


class FieldCommunityEvent(Base):
    __tablename__ = "field_community_events"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    material_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("field_materials.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(48))
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FieldRunBinding(Base):
    __tablename__ = "field_run_bindings"
    __table_args__ = (UniqueConstraint("run_id", "asset_version_id"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    run_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("capability_runs.id"), index=True)
    asset_version_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_asset_versions.id"), index=True)
    grant_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_project_grants.id"), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FieldRunRightsReview(Base):
    __tablename__ = "field_run_rights_reviews"
    __table_args__ = (UniqueConstraint("run_id", "grant_id"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    run_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("capability_runs.id"), index=True)
    grant_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_project_grants.id"), index=True)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("users.id"))
