"""Persistent source positions and human decisions for S03 / S05."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.entities import ID_TYPE, JSON_TYPE


class FieldMaterialProfile(Base):
    __tablename__ = "field_material_profiles"
    material_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("field_materials.id", ondelete="CASCADE"), primary_key=True
    )
    context: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    sensitivity: Mapped[str] = mapped_column(String(24), default="unknown")


class FieldTextVersion(Base):
    __tablename__ = "field_text_versions"
    __table_args__ = (UniqueConstraint("material_id", "version_no"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    material_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_materials.id"), index=True)
    version_no: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    parent_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("field_text_versions.id"))
    run_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("capability_runs.id"))
    units: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FieldSegment(Base):
    __tablename__ = "field_segments"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    material_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_materials.id"), index=True)
    run_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("capability_runs.id"), index=True)
    raw_version_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_text_versions.id"))
    clean_version_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_text_versions.id"))
    positions: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    original_text: Mapped[str] = mapped_column(Text)


class FieldAnnotation(Base):
    __tablename__ = "field_annotations"
    __table_args__ = (UniqueConstraint("segment_id", "revision_no"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    segment_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_segments.id"), index=True)
    revision_no: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    status: Mapped[str] = mapped_column(String(24), default="candidate")
    created_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FieldExcerpt(Base):
    __tablename__ = "field_excerpts"
    __table_args__ = (UniqueConstraint("annotation_id", "kind"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    annotation_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("field_annotations.id"))
    kind: Mapped[str] = mapped_column(String(24))
    created_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrackingItem(Base):
    __tablename__ = "tracking_items"
    __table_args__ = (UniqueConstraint("config_id", "identity"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    config_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("capability_configs.id"), index=True)
    identity: Mapped[str] = mapped_column(String(64))
    material_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("field_materials.id"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrackingReview(Base):
    __tablename__ = "tracking_reviews"
    __table_args__ = (UniqueConstraint("item_id", "revision_no"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    item_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("tracking_items.id"), index=True)
    observation_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("tracking_observations.id"), index=True
    )
    revision_no: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    created_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
