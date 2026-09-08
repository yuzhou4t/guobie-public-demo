"""Immutable observations and idempotent local tracking occurrences."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.entities import ID_TYPE, JSON_TYPE


class TrackingObservation(Base):
    __tablename__ = "tracking_observations"
    __table_args__ = (UniqueConstraint("run_id", "item_id"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    run_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("capability_runs.id"), index=True)
    item_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("tracking_items.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    observation: Mapped[str] = mapped_column(String(24))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrackingSchedule(Base):
    __tablename__ = "tracking_schedules"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    config_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("capability_configs.id"), unique=True)
    created_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    interval_days: Mapped[int]
    local_time: Mapped[str] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(80))
    output_prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrackingOccurrence(Base):
    __tablename__ = "tracking_occurrences"
    __table_args__ = (UniqueConstraint("schedule_id", "occurrence_key"),)
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("tracking_schedules.id"), index=True)
    occurrence_key: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(24))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    covered_slots: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    run_id: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("capability_runs.id"), unique=True)
    draft_revision_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("capability_run_revisions.id"), unique=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, default=list)


class TrackingDelivery(Base):
    __tablename__ = "tracking_deliveries"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    occurrence_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("tracking_occurrences.id"), unique=True)
    research_case_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("research_cases.id"), index=True)
    observation_ids: Mapped[list[int]] = mapped_column(JSON_TYPE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
