from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentEntity,
    DocumentTopic,
    DocumentVersion,
    EventEntity,
    EventMention,
    EventRelation,
    EvidenceClaim,
    ResearchCase,
    ResearchCaseDocument,
    ResearchCaseEvent,
    ResearchEntity,
    ResearchEvent,
    Topic,
    User,
)

REQUIRED_COLUMNS = {
    "research_case_title",
    "research_question",
    "country_iso3",
    "event_series_key",
    "event_key",
    "event_title",
    "event_type",
    "event_start_at",
    "date_precision",
    "document_version_id",
    "canonical_url",
    "mention_summary",
    "source_reported_place",
    "actors",
    "commodities",
    "topics",
    "action",
    "claim_kind",
    "claimant",
    "claim_subject",
    "claim_predicate",
    "claim_value_text",
    "claim_numeric_value",
    "claim_unit",
    "claim_time_scope",
    "comparison_key",
    "position_summary",
    "evidence_locator",
    "usage_type",
    "relation_type",
    "relation_target_event_key",
}
EVENT_TYPES = {"policy", "conflict", "market", "accident", "other"}
DATE_PRECISIONS = {"day", "month", "year", "unknown"}
CLAIM_KINDS = {"factual", "numeric", "position"}
USAGE_TYPES = {"support", "background", "refute", "to_verify"}
RELATION_TYPES = {"precedes", "extends", "replaces", "responds_to", "same_series"}


class ResearchEvidenceImportError(ValueError):
    pass


@dataclass(slots=True)
class ResearchEvidenceImportResult:
    rows: int = 0
    cases_created: int = 0
    events_created: int = 0
    mentions_created: int = 0
    claims_created: int = 0
    relations_created: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def import_research_evidence_csv(
    session: Session,
    path: Path,
    *,
    owner_email: str,
    reviewer: str,
    dry_run: bool = False,
) -> ResearchEvidenceImportResult:
    rows = _read_rows(path)
    owner = session.scalar(select(User).where(User.email == owner_email, User.status == "active"))
    if owner is None:
        raise ResearchEvidenceImportError("owner_email must identify an active existing user")

    result = ResearchEvidenceImportResult(rows=len(rows), dry_run=dry_run)
    pending_relations: set[tuple[str, str, str]] = set()
    for line_number, row in enumerate(rows, start=2):
        _validate_row(row, line_number)
        document_version = _resolve_document_version(session, row, line_number)
        country = _get_or_create_entity(
            session,
            entity_type="country",
            key=row["country_iso3"].upper(),
            name=_country_name(row["country_iso3"]),
            details={"iso3": row["country_iso3"].upper()},
        )
        research_case, created = _get_or_create_case(session, owner, row)
        result.cases_created += int(created)
        event, created = _get_or_create_event(session, country, row, reviewer)
        result.events_created += int(created)
        _link_case_event(session, research_case, event)
        _link_case_document(session, research_case, document_version, owner.id, row)
        _link_document_entity(session, document_version.id, country.id, "about", reviewer)

        place = row["source_reported_place"].strip()
        if place:
            place_entity = _get_or_create_entity(
                session,
                entity_type="place",
                key=_slug(place),
                name=place,
            )
            if event.primary_place_entity_id is None:
                event.primary_place_entity_id = place_entity.id
            _link_event_entity(session, event.id, place_entity.id, "location")
            _link_document_entity(session, document_version.id, place_entity.id, "location", reviewer)

        for actor in _split_values(row["actors"]):
            entity_type = "armed_group" if actor.casefold() in {"m23", "afd/m23"} else "organization"
            entity = _get_or_create_entity(
                session,
                entity_type=entity_type,
                key=_slug(actor),
                name=actor,
            )
            _link_event_entity(session, event.id, entity.id, "actor")
            _link_document_entity(session, document_version.id, entity.id, "actor", reviewer)

        for commodity in _split_values(row["commodities"]):
            entity = _get_or_create_entity(
                session,
                entity_type="commodity",
                key=_slug(commodity),
                name=commodity,
            )
            _link_event_entity(session, event.id, entity.id, "commodity")
            _link_document_entity(session, document_version.id, entity.id, "commodity", reviewer)

        for topic_title in _split_values(row["topics"]):
            topic = _get_or_create_topic(session, topic_title)
            _link_document_topic(session, document_version.id, topic.id, reviewer)

        mention, created = _get_or_create_mention(session, event, document_version, row, reviewer)
        result.mentions_created += int(created)
        if row["claim_predicate"].strip():
            created = _get_or_create_claim(session, mention, row, reviewer)
            result.claims_created += int(created)

        relation_type = row["relation_type"].strip()
        relation_target = row["relation_target_event_key"].strip()
        if relation_type and relation_target:
            pending_relations.add((event.event_key, relation_target, relation_type))

    session.flush()
    for source_key, target_key, relation_type in sorted(pending_relations):
        source_event = session.scalar(select(ResearchEvent).where(ResearchEvent.event_key == source_key))
        target_event = session.scalar(select(ResearchEvent).where(ResearchEvent.event_key == target_key))
        if source_event is None or target_event is None:
            raise ResearchEvidenceImportError(
                f"relation references an unknown event: {source_key} -> {target_key}"
            )
        existing = session.scalar(
            select(EventRelation).where(
                EventRelation.source_event_id == source_event.id,
                EventRelation.target_event_id == target_event.id,
                EventRelation.relation_type == relation_type,
            )
        )
        if existing is None:
            session.add(
                EventRelation(
                    source_event_id=source_event.id,
                    target_event_id=target_event.id,
                    relation_type=relation_type,
                    note=f"CSV reviewed by {reviewer}",
                )
            )
            result.relations_created += 1

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return result


def _read_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() != ".csv":
        raise ResearchEvidenceImportError("MVP importer accepts reviewed UTF-8 CSV files only")
    if not path.is_file():
        raise ResearchEvidenceImportError(f"input file does not exist: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise ResearchEvidenceImportError(f"missing required columns: {', '.join(missing)}")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not rows:
        raise ResearchEvidenceImportError("input file has no data rows")
    return rows


def _validate_row(row: dict[str, str], line_number: int) -> None:
    required_values = ("research_case_title", "country_iso3", "event_key", "event_title")
    for key in required_values:
        if not row[key]:
            raise ResearchEvidenceImportError(f"line {line_number}: {key} is required")
    if not re.fullmatch(r"[A-Za-z]{3}", row["country_iso3"]):
        raise ResearchEvidenceImportError(f"line {line_number}: country_iso3 must have 3 letters")
    if row["event_type"] not in EVENT_TYPES:
        raise ResearchEvidenceImportError(f"line {line_number}: invalid event_type")
    if row["date_precision"] not in DATE_PRECISIONS:
        raise ResearchEvidenceImportError(f"line {line_number}: invalid date_precision")
    if row["claim_kind"] and row["claim_kind"] not in CLAIM_KINDS:
        raise ResearchEvidenceImportError(f"line {line_number}: invalid claim_kind")
    if row["usage_type"] not in USAGE_TYPES:
        raise ResearchEvidenceImportError(f"line {line_number}: invalid usage_type")
    if row["relation_type"] and row["relation_type"] not in RELATION_TYPES:
        raise ResearchEvidenceImportError(f"line {line_number}: invalid relation_type")
    if not row["document_version_id"] and not row["canonical_url"]:
        raise ResearchEvidenceImportError(
            f"line {line_number}: document_version_id or canonical_url is required"
        )
    if row["claim_kind"] == "numeric" and not row["claim_numeric_value"]:
        raise ResearchEvidenceImportError(f"line {line_number}: numeric claim requires claim_numeric_value")
    _parse_datetime(row["event_start_at"], "event_start_at", line_number)


def _resolve_document_version(session: Session, row: dict[str, str], line_number: int) -> DocumentVersion:
    if row["document_version_id"]:
        try:
            version_id = int(row["document_version_id"])
        except ValueError as exc:
            raise ResearchEvidenceImportError(
                f"line {line_number}: document_version_id must be an integer"
            ) from exc
        version = session.get(DocumentVersion, version_id)
    else:
        version = session.scalar(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                or_(
                    Document.canonical_url == row["canonical_url"],
                    Document.discovery_url == row["canonical_url"],
                ),
                DocumentVersion.version_no == Document.latest_version_no,
            )
        )
    if version is None:
        raise ResearchEvidenceImportError(
            f"line {line_number}: exact document version is not in the existing document store"
        )
    return version


def _get_or_create_entity(
    session: Session,
    *,
    entity_type: str,
    key: str,
    name: str,
    details: dict[str, Any] | None = None,
) -> ResearchEntity:
    entity = session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == entity_type,
            ResearchEntity.canonical_key == key,
        )
    )
    if entity is None:
        entity = ResearchEntity(
            entity_type=entity_type,
            canonical_key=key,
            canonical_name=name,
            aliases=[],
            details=details or {},
        )
        session.add(entity)
        session.flush()
    return entity


def _get_or_create_case(session: Session, owner: User, row: dict[str, str]) -> tuple[ResearchCase, bool]:
    research_case = session.scalar(
        select(ResearchCase).where(
            ResearchCase.owner_id == owner.id,
            ResearchCase.title == row["research_case_title"],
        )
    )
    if research_case is not None:
        return research_case, False
    research_case = ResearchCase(
        owner_id=owner.id,
        title=row["research_case_title"],
        research_question=row["research_question"],
        scope={"countries": [row["country_iso3"].upper()], "topics": _split_values(row["topics"])},
        status="active",
    )
    session.add(research_case)
    session.flush()
    return research_case, True


def _get_or_create_event(
    session: Session,
    country: ResearchEntity,
    row: dict[str, str],
    reviewer: str,
) -> tuple[ResearchEvent, bool]:
    event = session.scalar(select(ResearchEvent).where(ResearchEvent.event_key == row["event_key"]))
    if event is not None:
        if event.country_entity_id != country.id or event.event_type != row["event_type"]:
            raise ResearchEvidenceImportError(
                f"event_key {row['event_key']} conflicts with the existing country or event type"
            )
        return event, False
    event = ResearchEvent(
        event_key=row["event_key"],
        series_key=row["event_series_key"] or None,
        title=row["event_title"],
        event_type=row["event_type"],
        country_entity_id=country.id,
        start_at=_parse_datetime(row["event_start_at"], "event_start_at", 0),
        date_precision=row["date_precision"],
        summary=row["mention_summary"],
        review_status="reviewed",
        details={"reviewed_by": reviewer, "action": row["action"]},
    )
    session.add(event)
    session.flush()
    return event, True


def _get_or_create_mention(
    session: Session,
    event: ResearchEvent,
    document_version: DocumentVersion,
    row: dict[str, str],
    reviewer: str,
) -> tuple[EventMention, bool]:
    mention = session.scalar(
        select(EventMention).where(
            EventMention.event_id == event.id,
            EventMention.document_version_id == document_version.id,
        )
    )
    if mention is not None:
        return mention, False
    mention = EventMention(
        event_id=event.id,
        document_version_id=document_version.id,
        source_reported_start_at=_parse_datetime(row["event_start_at"], "event_start_at", 0),
        source_reported_place=row["source_reported_place"] or None,
        mention_summary=row["mention_summary"],
        evidence_locator=_locator(row["evidence_locator"]),
        source_fields={"action": row["action"], "reviewed_by": reviewer},
        aggregation_version="manual-review-v1",
        review_status="confirmed",
    )
    session.add(mention)
    session.flush()
    return mention, True


def _get_or_create_claim(
    session: Session,
    mention: EventMention,
    row: dict[str, str],
    reviewer: str,
) -> bool:
    numeric_value = _decimal(row["claim_numeric_value"])
    key_parts = [
        row["claim_kind"],
        row["claimant"],
        row["claim_subject"],
        row["claim_predicate"],
        row["claim_value_text"],
        row["claim_numeric_value"],
        row["claim_unit"],
        row["claim_time_scope"],
        row["comparison_key"],
        json.dumps(_locator(row["evidence_locator"]), ensure_ascii=False, sort_keys=True),
    ]
    claim_key = hashlib.sha256("\u241f".join(key_parts).encode()).hexdigest()
    existing = session.scalar(
        select(EvidenceClaim).where(
            EvidenceClaim.event_mention_id == mention.id,
            EvidenceClaim.claim_key == claim_key,
        )
    )
    if existing is not None:
        return False
    claim = EvidenceClaim(
        event_mention_id=mention.id,
        claim_key=claim_key,
        claim_kind=row["claim_kind"],
        claimant_name=row["claimant"],
        subject_text=row["claim_subject"],
        predicate=row["claim_predicate"],
        value_text=row["claim_value_text"],
        numeric_value=numeric_value,
        unit=row["claim_unit"] or None,
        time_scope=row["claim_time_scope"] or None,
        comparison_key=row["comparison_key"]
        or _slug(
            "-".join(
                [
                    row["claim_subject"],
                    row["claim_predicate"],
                    row["claim_unit"],
                    row["claim_time_scope"],
                ]
            )
        ),
        position_summary=row["position_summary"] or None,
        evidence_locator={**_locator(row["evidence_locator"]), "reviewed_by": reviewer},
        review_status="confirmed",
    )
    session.add(claim)
    return True


def _link_case_event(session: Session, research_case: ResearchCase, event: ResearchEvent) -> None:
    exists = session.scalar(
        select(ResearchCaseEvent.id).where(
            ResearchCaseEvent.research_case_id == research_case.id,
            ResearchCaseEvent.event_id == event.id,
        )
    )
    if exists is None:
        session.add(
            ResearchCaseEvent(
                research_case_id=research_case.id,
                event_id=event.id,
                usage_type="primary",
                note="reviewed CSV import",
            )
        )


def _link_case_document(
    session: Session,
    research_case: ResearchCase,
    document_version: DocumentVersion,
    owner_id: int,
    row: dict[str, str],
) -> None:
    exists = session.scalar(
        select(ResearchCaseDocument.id).where(
            ResearchCaseDocument.research_case_id == research_case.id,
            ResearchCaseDocument.document_version_id == document_version.id,
        )
    )
    if exists is None:
        session.add(
            ResearchCaseDocument(
                research_case_id=research_case.id,
                document_version_id=document_version.id,
                added_by=owner_id,
                usage_type=row["usage_type"],
                note="reviewed CSV import",
            )
        )


def _link_event_entity(session: Session, event_id: int, entity_id: int, role: str) -> None:
    if session.get(EventEntity, (event_id, entity_id, role)) is None:
        session.add(EventEntity(event_id=event_id, entity_id=entity_id, role=role, note=""))


def _link_document_entity(
    session: Session, version_id: int, entity_id: int, role: str, reviewer: str
) -> None:
    if session.get(DocumentEntity, (version_id, entity_id, role)) is None:
        session.add(
            DocumentEntity(
                document_version_id=version_id,
                entity_id=entity_id,
                role=role,
                extraction_method="imported",
                review_status="confirmed",
                note=f"reviewed by {reviewer}",
            )
        )


def _get_or_create_topic(session: Session, title: str) -> Topic:
    slug = _slug(title)
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        topic = Topic(slug=slug, title=title, description="", status="active")
        session.add(topic)
        session.flush()
    return topic


def _link_document_topic(session: Session, version_id: int, topic_id: int, reviewer: str) -> None:
    if session.get(DocumentTopic, (version_id, topic_id)) is None:
        session.add(
            DocumentTopic(
                document_version_id=version_id,
                topic_id=topic_id,
                review_status="confirmed",
                note=f"reviewed by {reviewer}",
            )
        )


def _parse_datetime(value: str, field: str, line_number: int) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        prefix = f"line {line_number}: " if line_number else ""
        raise ResearchEvidenceImportError(f"{prefix}{field} must use ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _decimal(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ResearchEvidenceImportError("claim_numeric_value must be a decimal number") from exc


def _locator(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"description": value}
    if not isinstance(parsed, dict):
        raise ResearchEvidenceImportError("evidence_locator JSON must be an object")
    return parsed


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", normalized)
    return normalized.strip("-")[:160] or "unknown"


def _country_name(iso3: str) -> str:
    names = {"COD": "刚果民主共和国（刚果（金））"}
    return names.get(iso3.upper(), iso3.upper())
