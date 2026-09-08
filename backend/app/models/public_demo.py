"""Anonymous, isolated sessions for a separately provisioned public Reader deployment."""

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.entities import ID_TYPE, JSON_TYPE


class PublicDemoInstallation(Base):
    __tablename__ = "public_demo_installation"
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_version: Mapped[str] = mapped_column(String(80))


class PublicDemoSession(Base):
    __tablename__ = "public_demo_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ID_TYPE, ForeignKey("users.id"), unique=True)
    reader_key_ciphertext: Mapped[str] = mapped_column(Text)
    provider_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    connection: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    key_expires_at: Mapped[int | None] = mapped_column(BigInteger)
    busy_until: Mapped[int] = mapped_column(BigInteger, default=0)
    quota_window: Mapped[int] = mapped_column(BigInteger)
    calls: Mapped[int] = mapped_column(default=0)
