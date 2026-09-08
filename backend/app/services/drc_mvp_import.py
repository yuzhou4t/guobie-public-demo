from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import CandidateDocument, parse_datetime
from app.models import (
    DocumentEntity,
    ResearchCase,
    ResearchCaseDocument,
    ResearchEntity,
    Source,
    SourceChannel,
    SourceChannelPolicy,
)
from app.services.document_store import persist_candidate_metadata


class DrcMvpImportError(ValueError):
    pass


@dataclass(slots=True)
class DrcMvpImportResult:
    verified_sources: int = 0
    sources_created: int = 0
    channels_created: int = 0
    documents_created: int = 0
    versions_created: int = 0
    items_seen: int = 0
    country_links_created: int = 0
    case_links_created: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def import_drc_mvp_harvest(
    session: Session,
    *,
    manifest_path: Path,
    harvest_path: Path,
) -> DrcMvpImportResult:
    manifest = _load_json(manifest_path)
    harvest = _load_json(harvest_path)
    manifest_sources = {item["source_id"]: item for item in manifest.get("sources", [])}
    result = DrcMvpImportResult()

    for harvested in harvest.get("sources", []):
        if harvested.get("status") != "metadata_verified":
            continue
        source_key = harvested.get("source_id")
        configured = manifest_sources.get(source_key)
        if configured is None:
            raise DrcMvpImportError(f"verified source is missing from manifest: {source_key}")

        result.verified_sources += 1
        source, created = _get_or_create_source(session, configured)
        result.sources_created += int(created)
        channel, created = _get_or_create_channel(session, source, configured, harvested)
        result.channels_created += int(created)

        fetched_at = _latest_fetch_time(harvested) or datetime.now(UTC)
        for item in harvested.get("items", []):
            result.items_seen += 1
            candidate = _candidate(
                item, source_key=source_key, harvested_at=fetched_at, configured=configured
            )
            persisted = persist_candidate_metadata(
                session,
                source.id,
                candidate,
                document_type=_document_type(
                    item.get("media_type"),
                    configured.get("document_type"),
                ),
                extractor_name="drc_mvp_harvest",
                extractor_version="v1",
                seen_at=fetched_at,
                storage_scope="metadata",
                channel_id=channel.id,
            )
            result.documents_created += int(persisted.document_created)
            result.versions_created += int(persisted.version_created)
            if persisted.version is not None:
                country_created, case_created = _link_reviewed_relevance(
                    session,
                    version_id=persisted.version.id,
                    configured=configured,
                )
                result.country_links_created += int(country_created)
                result.case_links_created += int(case_created)

    session.commit()
    return result


def _link_reviewed_relevance(
    session: Session,
    *,
    version_id: int,
    configured: dict[str, Any],
) -> tuple[bool, bool]:
    review = configured.get("reviewed_relevance")
    if not isinstance(review, dict):
        return False, False
    iso3 = str(review.get("country_iso3") or "").strip().upper()
    if len(iso3) != 3 or not iso3.isalpha():
        raise DrcMvpImportError(f"invalid reviewed country relevance: {iso3!r}")
    reviewer = str(review.get("reviewed_by") or "").strip()
    if not reviewer:
        raise DrcMvpImportError("reviewed relevance requires reviewed_by")
    note = str(review.get("note") or "").strip()
    country = session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == iso3,
        )
    )
    if country is None:
        country = ResearchEntity(
            entity_type="country",
            canonical_key=iso3,
            canonical_name="刚果民主共和国（刚果（金））" if iso3 == "COD" else iso3,
            aliases=["刚果（金）", "Democratic Republic of the Congo"] if iso3 == "COD" else [],
            details={"iso3": iso3, "seed_role": "reviewed_material_relevance"},
        )
        session.add(country)
        session.flush()
    country_link = session.get(DocumentEntity, (version_id, country.id, "about"))
    country_created = country_link is None
    if country_link is None:
        session.add(
            DocumentEntity(
                document_version_id=version_id,
                entity_id=country.id,
                role="about",
                extraction_method="manual",
                review_status="confirmed",
                note=f"reviewed by {reviewer}: {note}",
            )
        )
    elif country_link.review_status != "confirmed":
        country_link.review_status = "confirmed"
        country_link.extraction_method = "manual"
        country_link.note = f"reviewed by {reviewer}: {note}"

    case_title = str(review.get("research_case_title") or "").strip()
    if not case_title:
        return country_created, False
    research_case = session.scalar(
        select(ResearchCase)
        .where(ResearchCase.title == case_title, ResearchCase.status == "active")
        .order_by(ResearchCase.id)
        .limit(1)
    )
    if research_case is None:
        return country_created, False
    usage_type = str(review.get("usage_type") or "background")
    if usage_type not in {"support", "background", "refute", "to_verify"}:
        raise DrcMvpImportError(f"invalid reviewed material usage_type: {usage_type!r}")
    case_link = session.scalar(
        select(ResearchCaseDocument).where(
            ResearchCaseDocument.research_case_id == research_case.id,
            ResearchCaseDocument.document_version_id == version_id,
        )
    )
    case_created = case_link is None
    if case_link is None:
        session.add(
            ResearchCaseDocument(
                research_case_id=research_case.id,
                document_version_id=version_id,
                added_by=research_case.owner_id,
                usage_type=usage_type,
                note=f"reviewed by {reviewer}: {note}",
            )
        )
    else:
        case_link.usage_type = usage_type
        case_link.note = f"reviewed by {reviewer}: {note}"
    return country_created, case_created


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DrcMvpImportError(f"input file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DrcMvpImportError(f"input must contain a JSON object: {path}")
    return payload


def _get_or_create_source(session: Session, configured: dict[str, Any]) -> tuple[Source, bool]:
    name = str(configured["name"])
    organization = str(configured.get("organization") or "")
    source = session.scalar(
        select(Source).where(Source.name == name, Source.organization_name == organization)
    )
    if source is not None:
        return source, False
    source = Source(
        name=name,
        organization_name=organization,
        source_type=_source_type(configured.get("category")),
        country_or_region="COD",
        primary_language=str(configured.get("language") or "und"),
        homepage_url=str(configured["entry_url"]),
        authority_level="official" if "官方" in str(configured.get("category")) else "unrated",
        status="active",
    )
    session.add(source)
    session.flush()
    return source, True


def _get_or_create_channel(
    session: Session,
    source: Source,
    configured: dict[str, Any],
    harvested: dict[str, Any],
) -> tuple[SourceChannel, bool]:
    entry_url = str(configured["entry_url"])
    channel = session.scalar(
        select(SourceChannel).where(
            SourceChannel.source_id == source.id,
            SourceChannel.entry_url == entry_url,
        )
    )
    created = channel is None
    if channel is None:
        channel = SourceChannel(
            source_id=source.id,
            name=str(configured["name"]),
            entry_url=entry_url,
            collector_type=_collector_type(configured.get("profile")),
            collector_config={
                "profile": configured.get("profile"),
                "manifest_source_id": configured.get("source_id"),
                "priority": configured.get("priority"),
                "dimensions": configured.get("dimensions", []),
                "topics": configured.get("topics", []),
            },
            adapter_version="drc-mvp-v1",
            link_role="official",
            status="shadow",
            last_success_at=_latest_fetch_time(harvested),
        )
        session.add(channel)
        session.flush()
    elif channel.status not in {"active", "shadow"}:
        channel.status = "shadow"
    if channel.policy is None:
        session.add(
            SourceChannelPolicy(
                channel_id=channel.id,
                robots_state="allowed",
                terms_state="metadata_only_internal_review",
                storage_scope="metadata",
                rag_scope="none",
                reviewed_at=_latest_fetch_time(harvested),
                notes="两次安全复采一致；仅入库书目元数据，不保存正文或原始响应。",
            )
        )
    return channel, created


def _candidate(
    item: dict[str, Any],
    *,
    source_key: str,
    harvested_at: datetime,
    configured: dict[str, Any] | None = None,
) -> CandidateDocument:
    published_at = parse_datetime(item.get("published_at"))
    precision = str(item.get("published_at_precision") or "unknown")
    url = str(item.get("url") or item.get("discovery_url") or "")
    source_metadata = {
        "manifest_source_id": source_key,
        "harvested_at": harvested_at.isoformat(),
        "detail_retrieved_at": item.get("retrieved_at"),
        "published_at_provenance": {
            "precision": precision,
            "method": "source_metadata",
        },
        "verification_status": "metadata_verified",
        "storage_boundary": "metadata_only_no_raw_response",
    }
    configured = configured or {}
    optional_metadata = {
        "topics": item.get("topics") or configured.get("topics"),
        "topic_provenance": {"kind": "platform_classification", "source": source_key}
        if configured.get("topics")
        else None,
        "keywords": item.get("keywords"),
        "research_object": item.get("research_object"),
        "research_method": item.get("research_method"),
        "sample_scope": item.get("sample_scope"),
        "data_sources": item.get("data_sources"),
        "field_provenance": item.get("field_provenance"),
        "replication_urls": item.get("replication_urls"),
        "official_url_aliases": item.get("url_aliases"),
        "resource_sha256": item.get("sha256"),
        "resource_size_bytes": item.get("size_bytes"),
        "page_count": item.get("page_count"),
        "delivery_provenance": item.get("delivery_provenance"),
    }
    source_metadata.update(
        {key: value for key, value in optional_metadata.items() if value not in (None, [], {})}
    )
    return CandidateDocument(
        discovery_url=str(item.get("discovery_url") or url),
        canonical_url=url or None,
        external_id=item.get("external_id"),
        doi=item.get("doi"),
        authors=tuple(item.get("authors") or []),
        title=item.get("title"),
        published_at=published_at,
        issue_text=_issue_text(item.get("published_at"), precision),
        language=item.get("language") or "und",
        media_type=item.get("media_type"),
        source_metadata=source_metadata,
    )


def _latest_fetch_time(harvested: dict[str, Any]) -> datetime | None:
    values = [
        parse_datetime(item.get("fetched_at"))
        for item in harvested.get("fetches", [])
        if item.get("fetched_at")
    ]
    return max((item for item in values if item is not None), default=None)


def _issue_text(value: Any, precision: str) -> str | None:
    if not value or precision == "day":
        return None
    text = str(value)
    if precision == "month":
        return text[:7]
    if precision == "year":
        return text[:4]
    return None


def _collector_type(profile: Any) -> str:
    return "pdf" if profile == "single_document" else "html"


def _document_type(media_type: Any, configured_type: Any = None) -> str:
    if configured_type:
        return str(configured_type)
    return "report" if str(media_type or "").lower() == "application/pdf" else "article"


def _source_type(category: Any) -> str:
    label = str(category or "")
    if "学术" in label or "期刊" in label:
        return "academic_journal"
    if "媒体" in label:
        return "news_media"
    if "数据" in label:
        return "official_data"
    if "研究" in label:
        return "research_organization"
    return "government_or_international"
