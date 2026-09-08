"""Persistent project conversations, human-approved plans and evidence adoption."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.entities import ID_TYPE, JSON_TYPE, TimestampMixin


class ProjectTask(TimestampMixin, Base):
    __tablename__ = "project_tasks"
    __table_args__ = (
        CheckConstraint(
            "status in ('planning','awaiting_confirmation','running','review',"
            "'completed','failed','cancelled')",
            name="status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="planning")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    messages: Mapped[list] = mapped_column(JSON_TYPE, default=list)
    error: Mapped[str | None] = mapped_column(Text)


class ProjectPlan(TimestampMixin, Base):
    __tablename__ = "project_plans"
    __table_args__ = (UniqueConstraint("task_id", "revision", name="uq_project_plans_revision"),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project_tasks.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    confirmed_by: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_key: Mapped[str | None] = mapped_column(String(64), unique=True)


class ProjectEvidence(TimestampMixin, Base):
    __tablename__ = "project_evidence"
    __table_args__ = (
        UniqueConstraint("research_case_id", "object_type", "object_id", name="uq_project_evidence_object"),
        CheckConstraint("decision in ('pending','accepted','rejected','needs_revision')", name="decision"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("project_tasks.id", ondelete="SET NULL"), index=True
    )
    object_type: Mapped[str] = mapped_column(String(48))
    object_id: Mapped[int] = mapped_column(ID_TYPE)
    snapshot: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    decision: Mapped[str] = mapped_column(String(24), default="pending")
    note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[int | None] = mapped_column(ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
