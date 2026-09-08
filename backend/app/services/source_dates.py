from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import CandidateDocument
from app.models import Document, DocumentVersion, Source
from app.services.document_store import persist_candidate_metadata

_REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MONTHLY_ISSUE_SOURCES = {
    "AMERICAN ECONOMIC REVIEW",
    "世界经济",
    "中国工业经济",
    "管理世界",
}
_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}


@dataclass(frozen=True, slots=True)
class SourceDateBackfillReport:
    scanned: int
    updated: int
    versions_created: int
    projections_repaired: int
    precision_counts: dict[str, int]
    last_document_id: int | None


def source_date_from_url(url: str) -> tuple[datetime | None, dict[str, Any] | None]:
    path = urlsplit(url).path
    patterns = (
        (r"(?<!\d)Y(20\d{2})/[^?#]*/I(\d{1,2})(?!\d)", "month"),
        (r"(?<!\d)(20\d{2})[-/](\d{2})[-/](\d{2})(?!\d)", "day"),
        (r"(?<!\d)(20\d{2})(\d{2})/(\d{2})(?!\d)", "day"),
        (r"(?<!\d)(20\d{2})/(\d{2})(\d{2})(?!\d)", "day"),
        (r"(?<!\d)t(20\d{2})(\d{2})(\d{2})(?!\d)", "day"),
        (r"(?<!\d)(20\d{2})[-/](\d{2})(?!\d)", "month"),
        (r"(?<!\d)(20\d{2})(\d{2})(?!\d)", "month"),
        (r"(?:^|/)(20\d{2})(?:/|$)", "year"),
    )
    for pattern, precision in patterns:
        match = re.search(pattern, path, re.IGNORECASE)
        if match is None:
            continue
        try:
            year = int(match.group(1))
            month = int(match.group(2)) if precision != "year" else 1
            day = int(match.group(3)) if precision == "day" else 1
            published_at = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            continue
        return published_at, {
            "source": "official_url_path",
            "precision": precision,
            "raw_value": match.group(0),
            "source_url": url,
        }
    return None, None


def source_date_from_issue(
    source_name: str,
    issue_text: str | None,
    title: str,
    source_url: str,
) -> tuple[datetime | None, dict[str, Any] | None]:
    if source_name == "Associated Press" and "Spotlights" in title and "Explore content" in title:
        listed = re.search(
            r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),\s+(20\d{2})\b",
            title,
            re.IGNORECASE,
        )
        if listed is not None:
            month = _MONTHS[listed.group(1).casefold()]
            day, year = int(listed.group(2)), int(listed.group(3))
            try:
                published_at = datetime(year, month, day, tzinfo=UTC)
            except ValueError:
                return None, None
            return published_at, {
                "source": "official_listing_text",
                "precision": "day",
                "raw_value": listed.group(0),
                "source_url": source_url,
            }
    if source_name not in _MONTHLY_ISSUE_SOURCES:
        return None, None
    raw_value = issue_text or title
    chinese = re.search(r"(20\d{2})\s*年\s*第?\s*(\d{1,2})\s*期", raw_value)
    if chinese is not None:
        year, month = int(chinese.group(1)), int(chinese.group(2))
        matched = chinese.group(0)
    else:
        english = re.search(
            r"\b(" + "|".join(_MONTHS) + r")\s+(20\d{2})\b",
            raw_value,
            re.IGNORECASE,
        )
        if english is None:
            return None, None
        year, month = int(english.group(2)), _MONTHS[english.group(1).casefold()]
        matched = english.group(0)
    if not 1 <= month <= 12:
        return None, None
    return datetime(year, month, 1, tzinfo=UTC), {
        "source": "official_issue_label",
        "precision": "month",
        "raw_value": matched,
        "source_url": source_url,
    }


def backfill_source_dates(
    session: Session,
    *,
    seen_at: datetime | None = None,
    after_document_id: int | None = None,
    limit: int | None = None,
) -> SourceDateBackfillReport:
    now = seen_at or datetime.now(UTC)
    statement = (
        select(Source, Document, DocumentVersion)
        .join(Document, Document.source_id == Source.id)
        .join(
            DocumentVersion,
            (DocumentVersion.document_id == Document.id)
            & (DocumentVersion.version_no == Document.latest_version_no),
        )
        .order_by(Document.id)
    )
    if after_document_id is not None:
        statement = statement.where(Document.id > after_document_id)
    if limit is not None:
        statement = statement.limit(limit)
    rows = session.execute(statement).all()
    updated = 0
    versions_created = 0
    projections_repaired = 0
    precision_counts: Counter[str] = Counter()
    for source, document, current in rows:
        if not _same_datetime(document.published_at, current.published_at) or (
            document.issue_date_text != current.issue_date_text
        ):
            document.published_at = current.published_at
            document.issue_date_text = current.issue_date_text
            projections_repaired += 1
        source_url = document.canonical_url or document.discovery_url
        if not source_url:
            continue
        url_candidate = source_date_from_url(source_url)
        candidates = (
            url_candidate,
            source_date_from_issue(
                source.name,
                current.issue_date_text,
                current.title,
                source_url,
            ),
        )
        published_at, provenance = max(
            candidates,
            key=lambda item: {None: 0, "year": 1, "month": 2, "day": 3}.get(
                item[1].get("precision") if item[1] else None,
                0,
            ),
        )
        if published_at is None or provenance is None:
            if _has_invalid_url_year(current, url_candidate):
                outcome = _persist_source_date(
                    session,
                    document,
                    current,
                    source_url,
                    published_at=None,
                    provenance=None,
                    seen_at=now,
                )
                if outcome.document is not None:
                    updated += 1
                    versions_created += int(outcome.version_created)
                    precision_counts["cleared_invalid_year"] += 1
            continue
        if not _is_better_source_date(document, current, published_at, provenance["precision"]):
            continue

        outcome = _persist_source_date(
            session,
            document,
            current,
            source_url,
            published_at=published_at,
            provenance=provenance,
            seen_at=now,
        )
        if outcome.document is None:
            continue
        updated += 1
        versions_created += int(outcome.version_created)
        precision_counts[provenance["precision"]] += 1

    return SourceDateBackfillReport(
        scanned=len(rows),
        updated=updated,
        versions_created=versions_created,
        projections_repaired=projections_repaired,
        precision_counts=dict(sorted(precision_counts.items())),
        last_document_id=rows[-1][1].id if rows else None,
    )


def _persist_source_date(
    session: Session,
    document: Document,
    current: DocumentVersion,
    source_url: str,
    *,
    published_at: datetime | None,
    provenance: dict[str, Any] | None,
    seen_at: datetime,
):
    raw_authors = current.source_metadata.get("authors", [])
    authors = tuple(str(value) for value in raw_authors) if isinstance(raw_authors, list) else ()
    source_metadata = {
        key: value
        for key, value in current.source_metadata.items()
        if key not in {"authors", "published_at_provenance"}
    }
    if provenance is not None:
        source_metadata["published_at_provenance"] = provenance
    candidate = CandidateDocument(
        discovery_url=document.discovery_url or source_url,
        canonical_url=document.canonical_url,
        external_id=document.external_id,
        title=current.title,
        doi=document.doi,
        authors=authors,
        published_at=published_at,
        source_updated_at=current.source_updated_at,
        issue_text=current.issue_date_text,
        language=current.language,
        source_metadata=source_metadata,
    )
    return persist_candidate_metadata(
        session,
        document.source_id,
        candidate,
        document_type=document.document_type,
        extractor_name="source_date_backfill",
        extractor_version="url-path-v2",
        seen_at=seen_at,
        storage_scope="metadata",
    )


def _has_invalid_url_year(
    current: DocumentVersion,
    url_candidate: tuple[datetime | None, dict[str, Any] | None],
) -> bool:
    if url_candidate[0] is not None:
        return False
    provenance = current.source_metadata.get("published_at_provenance")
    return (
        isinstance(provenance, dict)
        and provenance.get("source") == "official_url_path"
        and provenance.get("precision") == "year"
    )


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    left_utc = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
    right_utc = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    return abs((left_utc - right_utc).total_seconds()) < 1


def _is_better_source_date(
    document: Document,
    current: DocumentVersion,
    inferred: datetime,
    inferred_precision: str,
) -> bool:
    current_provenance = current.source_metadata.get("published_at_provenance")
    current_precision = current_provenance.get("precision") if isinstance(current_provenance, dict) else None
    rank = {None: 0, "year": 1, "month": 2, "day": 3}
    if current.published_at is None:
        return True
    current_at = current.published_at
    if current_at.tzinfo is None:
        current_at = current_at.replace(tzinfo=UTC)
    else:
        current_at = current_at.astimezone(UTC)
    first_seen_at = document.first_seen_at
    first_seen_at = (
        first_seen_at.replace(tzinfo=UTC) if first_seen_at.tzinfo is None else first_seen_at.astimezone(UTC)
    )
    extracted_at = current.extracted_at
    extracted_at = (
        extracted_at.replace(tzinfo=UTC) if extracted_at.tzinfo is None else extracted_at.astimezone(UTC)
    )
    if current_at == first_seen_at or current_at == extracted_at:
        return True
    if current_precision is not None and current_precision in rank:
        return rank[inferred_precision] > rank[current_precision]
    if inferred == current_at:
        return True
    current_local = current_at.astimezone(_REPORT_TIMEZONE)
    return (
        current_local.month == 1
        and current_local.day == 1
        and (inferred != current_at or inferred_precision == "year")
    )
