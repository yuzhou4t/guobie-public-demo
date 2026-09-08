from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import PartnerApiClient

PARTNER_KEY_HEADER = "X-API-Key"
PARTNER_KEY_PATTERN = re.compile(r"^gbp_([0-9a-f]{16})_([A-Za-z0-9_-]{32,})$")
partner_api_key = APIKeyHeader(name=PARTNER_KEY_HEADER, auto_error=False)
PartnerKey = Annotated[str | None, Depends(partner_api_key)]
DbSession = Annotated[Session, Depends(get_db)]


def hash_partner_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_partner_client(
    db: Session,
    *,
    name: str,
    expires_in_days: int = 90,
) -> tuple[PartnerApiClient, str]:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("partner client name must not be empty")
    if not 1 <= expires_in_days <= 730:
        raise ValueError("expires_in_days must be between 1 and 730")
    if db.scalar(select(PartnerApiClient.id).where(PartnerApiClient.name == normalized_name)):
        raise ValueError("partner client name already exists")

    for _ in range(5):
        prefix = secrets.token_hex(8)
        if not db.scalar(select(PartnerApiClient.id).where(PartnerApiClient.key_prefix == prefix)):
            break
    else:
        raise RuntimeError("unable to allocate a unique partner API key prefix")

    plaintext_key = f"gbp_{prefix}_{secrets.token_urlsafe(32)}"
    client = PartnerApiClient(
        name=normalized_name,
        key_prefix=prefix,
        key_sha256=hash_partner_key(plaintext_key),
        status="active",
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
    )
    db.add(client)
    db.flush()
    return client, plaintext_key


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def require_partner_client(
    db: DbSession,
    api_key: PartnerKey,
) -> PartnerApiClient:
    match = PARTNER_KEY_PATTERN.fullmatch(api_key or "")
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid partner API key",
        )
    client = db.scalar(select(PartnerApiClient).where(PartnerApiClient.key_prefix == match.group(1)))
    now = datetime.now(UTC)
    valid = (
        client is not None
        and client.status == "active"
        and (client.expires_at is None or _aware(client.expires_at) > now)
        and secrets.compare_digest(client.key_sha256, hash_partner_key(api_key or ""))
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid partner API key",
        )
    return client
