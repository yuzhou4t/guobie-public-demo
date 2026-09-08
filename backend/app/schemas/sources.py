from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _http_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL must be a valid http(s) URL") from exc
    if not valid or parsed.username or parsed.password or parsed.hostname is None:
        raise ValueError("URL must be a valid http(s) URL without credentials")
    try:
        host = parsed.hostname.rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("URL host is invalid") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    organization_name: str = Field(default="", max_length=300)
    source_type: str = Field(min_length=1, max_length=80)
    country_or_region: str = Field(default="", max_length=200)
    primary_language: str = Field(default="", max_length=80)
    homepage_url: str | None = Field(default=None, max_length=2048)
    authority_level: str = Field(default="unrated", min_length=1, max_length=80)

    @field_validator(
        "name",
        "organization_name",
        "source_type",
        "country_or_region",
        "primary_language",
        "authority_level",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("homepage_url")
    @classmethod
    def validate_homepage(cls, value: str | None) -> str | None:
        return _http_url(value)


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    organization_name: str
    source_type: str
    country_or_region: str
    primary_language: str
    homepage_url: str | None
    authority_level: str
    status: str
    created_at: datetime
    updated_at: datetime


class SourceChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    entry_url: str = Field(max_length=2048)
    collector_type: Literal["rss", "api", "html", "pdf"]
    collector_config: dict[str, Any] = Field(default_factory=dict)
    adapter_version: str = Field(default="v1", min_length=1, max_length=80)
    link_role: Literal["official", "discovery", "unknown"] = "unknown"
    poll_interval_seconds: int | None = Field(default=None, ge=300)

    @field_validator("name", "adapter_version", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("entry_url")
    @classmethod
    def validate_entry_url(cls, value: str) -> str:
        result = _http_url(value)
        if result is None:
            raise ValueError("entry_url is required")
        return result


class SourceChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    name: str
    entry_url: str
    collector_type: str
    collector_config: dict[str, Any]
    adapter_version: str
    link_role: str
    status: str
    poll_interval_seconds: int | None
    next_run_at: datetime | None
    cursor_state: dict[str, Any]
    last_success_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CollectionRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    trigger_kind: str
    status: str
    queued_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    items_discovered: int
    items_persisted: int
    error_category: str | None
    error_code: str | None
    error_message: str | None
    retry_count: int
    celery_task_id: str | None
    report: dict[str, Any]
    created_at: datetime
