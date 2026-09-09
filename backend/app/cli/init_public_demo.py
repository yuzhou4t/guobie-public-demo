"""Atomically initialize or extend the selected sample in an independent marked database."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models import (
    Document,
    DocumentEntity,
    DocumentVersion,
    EventMention,
    ResearchEntity,
    ResearchEvent,
    Source,
    User,
)
from app.models.public_demo import PublicDemoInstallation
from app.services.mvp_seed import import_cod_structured_seed
from app.services.public_demo_samples import sample_manifest
from app.services.research_capabilities import seed_capability_templates, seed_public_capability_catalog


def initialize(db):
    try:
        with Session(
            bind=db.connection(), join_transaction_mode="rollback_only", expire_on_commit=False
        ) as staged:
            result = _initialize_selected(staged)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _date(value, fallback=None):
    return datetime.fromisoformat(value) if value else fallback


def _initialize_selected(db):
    if not get_settings().public_demo_enabled:
        raise RuntimeError("Explicit public demo mode is required")
    payload = sample_manifest()
    marker = db.get(PublicDemoInstallation, 1)
    if marker:
        if marker.sample_version == payload["version"]:
            return payload["summary"]
        if marker.sample_version not in payload.get("previous_versions", []):
            raise RuntimeError("Sample version mismatch")
    elif db.scalar(select(User.id).limit(1)) or db.scalar(select(Source.id).limit(1)):
        raise RuntimeError("Refusing to initialize a nonempty unmarked database")
    with NamedTemporaryFile(mode="w+", suffix=".json") as seed:
        json.dump(payload["structured"], seed)
        seed.flush()
        import_cod_structured_seed(db, Path(seed.name), snapshot_tag=payload["version"])
    country = db.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country", ResearchEntity.canonical_key == "COD"
        )
    )
    if country is None:
        country = ResearchEntity(entity_type="country", canonical_key="COD", canonical_name="刚果（金）")
        db.add(country)
        db.flush()
    timestamp = datetime.now(UTC)
    versions = {}
    for item in payload["documents"]:
        source = db.scalar(select(Source).where(Source.name == item["source_name"]))
        if source is None:
            source = Source(
                name=item["source_name"],
                source_type=item["source_type"],
                homepage_url=item["source_url"],
                status="paused",
            )
            db.add(source)
            db.flush()
        published = _date(item["published_at"])
        document = db.scalar(select(Document).where(Document.canonical_url == item["canonical_url"]))
        if document is None:
            document = Document(
                source_id=source.id,
                title=item["title"],
                canonical_url=item["canonical_url"],
                doi=item["doi"],
                document_type=item["document_type"],
                language=item["language"],
                published_at=published,
                issue_date_text=item["issue_date_text"],
                first_seen_at=_date(item.get("recorded_at"), timestamp),
                last_seen_at=_date(item.get("observed_at"), timestamp),
                latest_version_no=0,
            )
            db.add(document)
            # A new document must satisfy the positive version constraint at flush.
            document.latest_version_no = 1
            db.flush()
        digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        version = db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id, DocumentVersion.content_sha256 == digest
            )
        )
        if version is None:
            existing = db.scalar(
                select(DocumentVersion.id).where(DocumentVersion.document_id == document.id).limit(1)
            )
            number = document.latest_version_no + 1 if existing else 1
            version = DocumentVersion(
                document_id=document.id,
                version_no=number,
                content_sha256=digest,
                title=item["title"],
                language=item["language"],
                published_at=published,
                issue_date_text=item["issue_date_text"],
                extractor_name="selected-public-metadata",
                extractor_version="2",
                source_metadata={**item.get("source_metadata", {}), "sample_version": payload["version"]},
                extracted_at=_date(item.get("recorded_at"), timestamp),
            )
            db.add(version)
            document.latest_version_no = number
            document.title = item["title"]
            document.published_at = published
            document.last_seen_at = _date(item.get("observed_at"), document.last_seen_at)
            db.flush()
        versions[item["canonical_url"]] = version.id
        link = db.scalar(
            select(DocumentEntity.document_version_id).where(
                DocumentEntity.document_version_id == version.id, DocumentEntity.entity_id == country.id
            )
        )
        if link is None:
            db.add(
                DocumentEntity(
                    document_version_id=version.id,
                    entity_id=country.id,
                    role="about",
                    extraction_method="imported",
                    review_status="confirmed",
                    note="沿用所选公开资料的已有国别关联",
                )
            )
    for item in payload.get("events", []):
        event = db.scalar(select(ResearchEvent).where(ResearchEvent.event_key == item["event_key"]))
        if event is None:
            event = ResearchEvent(
                event_key=item["event_key"],
                country_entity_id=country.id,
                title=item["title"],
                event_type=item["event_type"],
                review_status="reviewed",
            )
            db.add(event)
        for field in ("series_key", "title", "event_type", "date_precision", "summary"):
            setattr(event, field, item.get(field))
        event.start_at, event.end_at = _date(item.get("start_at")), _date(item.get("end_at"))
        event.details = {
            "sample_version": payload["version"],
            "provenance": "existing_reviewed_public_sources",
        }
        db.flush()
        for mention in item["mentions"]:
            vid = versions[mention["document_url"]]
            existing = db.scalar(
                select(EventMention.id).where(
                    EventMention.event_id == event.id, EventMention.document_version_id == vid
                )
            )
            if existing is None:
                db.add(
                    EventMention(
                        event_id=event.id,
                        document_version_id=vid,
                        review_status="confirmed",
                        mention_summary=mention.get("mention_summary", ""),
                        source_reported_start_at=_date(mention.get("source_reported_start_at")),
                        source_reported_end_at=_date(mention.get("source_reported_end_at")),
                        source_reported_place=mention.get("source_reported_place") or "",
                        source_fields={"perspective_group": mention.get("perspective_group")},
                        evidence_locator={"canonical_url": mention["document_url"]},
                    )
                )
    root = Path(__file__).resolve().parents[3]
    seed_capability_templates(db, root / "data/research_capability_templates.json")
    seed_public_capability_catalog(db)
    if marker:
        marker.sample_version = payload["version"]
    else:
        db.add(PublicDemoInstallation(id=1, sample_version=payload["version"]))
    db.commit()
    return payload["summary"]


if __name__ == "__main__":
    with get_session_factory()() as session:
        print(json.dumps(initialize(session), ensure_ascii=False))
