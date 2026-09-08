from __future__ import annotations

import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from time import struct_time
from typing import Any, Literal, Protocol
from urllib.parse import urljoin


@dataclass(frozen=True, slots=True)
class FetchedResource:
    request_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    fetched_at: datetime
    sha256: str
    content_type: str
    etag: str | None = None
    last_modified: str | None = None

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")

    def text(self) -> str:
        charset_match = re.search(r"charset=([^;\s]+)", self.content_type, re.IGNORECASE)
        charset = charset_match.group(1).strip("\"'") if charset_match else "utf-8"
        try:
            return self.body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class CandidateDocument:
    discovery_url: str
    canonical_url: str | None = None
    external_id: str | None = None
    title: str | None = None
    doi: str | None = None
    authors: tuple[str, ...] = ()
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    issue_text: str | None = None
    summary: str | None = None
    summary_kind: Literal["feed_summary", "api_abstract", "html_summary"] | None = None
    summary_source_url: str | None = None
    body_text: str | None = None
    language: str | None = None
    media_type: str | None = None
    resource_sha256: str | None = None
    locator: str | None = None
    source_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FollowupRequest:
    url: str
    discovery_url: str
    parent_resource_sha256: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    level: Literal["info", "warning", "error"] = "warning"
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    candidates: tuple[CandidateDocument, ...] = ()
    followups: tuple[FollowupRequest, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


class Collector(Protocol):
    collector_type: str

    def parse(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str = "unknown",
    ) -> ParseResult: ...


def absolute_url(value: Any, base_url: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    resolved = urljoin(base_url, text)
    return resolved if resolved.startswith(("http://", "https://")) else None


def official_url(url: str | None, link_role: str) -> str | None:
    return url if link_role == "official" else None


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, struct_time):
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    if isinstance(value, (tuple, list)) and len(value) >= 6:
        try:
            return datetime(*[int(part) for part in value[:6]], tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    partial_iso = re.fullmatch(r"(\d{4})(?:-(\d{2}))?", text)
    if partial_iso is not None:
        try:
            return datetime(
                int(partial_iso.group(1)),
                int(partial_iso.group(2) or 1),
                1,
                tzinfo=UTC,
            )
        except ValueError:
            return None
    iso_value = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value).strip()
    return re.sub(r"\s+", " ", text) or None


def tuple_of_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("name") or item.get("title") or item.get("value")
        cleaned = clean_text(item)
        if cleaned:
            result.append(cleaned)
    return tuple(result)


def slug_to_title(slug: str) -> str:
    """Convert a URL slug (kebab-case final path segment) to a readable title."""
    return slug.replace("-", " ").title()
