"""Versioned, metadata-only or CC VoR abstract import for the bounded COD list."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import CandidateDocument
from app.models import (
    DocumentAbstractState,
    DocumentEntity,
    ResearchEntity,
    Source,
    SourceChannel,
    SourceChannelPolicy,
)
from app.services.document_store import persist_candidate_metadata


def store_cod_papers(session: Session, candidates: list[CandidateDocument]) -> dict:
    country = session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country", ResearchEntity.canonical_key == "COD"
        )
    )
    if country is None:
        raise ValueError("COD country must already be registered")
    result = {"documents_created": 0, "versions_created": 0, "papers": []}
    for candidate in candidates:
        meta = candidate.source_metadata
        journal = meta["journal_title"]
        source = session.scalar(
            select(Source).where(
                Source.name == journal, Source.organization_name == (meta.get("publisher") or "")
            )
        )
        if source is None:
            source = Source(
                name=journal,
                organization_name=meta.get("publisher") or "",
                source_type="academic_journal",
                country_or_region="COD",
                primary_language="en",
                homepage_url=candidate.canonical_url,
                status="active",
            )
            session.add(source)
            session.flush()
        channel = session.scalar(
            select(SourceChannel).where(
                SourceChannel.source_id == source.id, SourceChannel.entry_url == candidate.discovery_url
            )
        )
        if channel is None:
            channel = SourceChannel(
                source_id=source.id,
                name=f"COD 2022—2026 · {candidate.doi}",
                entry_url=candidate.discovery_url,
                collector_type="api",
                collector_config={"profile": "cod_research_on_demand", "doi": candidate.doi},
                link_role="official",
                status="shadow",
            )
            session.add(channel)
            session.flush()
        if channel.status != "shadow":
            raise ValueError("Research supplementation requires a shadow channel")
        terms = meta.get("abstract_license_url")
        if candidate.summary and (not terms or meta.get("abstract_license_content_version") != "vor"):
            raise ValueError("Abstract requires verified CC VoR permission")
        scope = "official_abstract" if candidate.summary else "metadata"
        checked_at = datetime.fromisoformat(meta["verified_at"]).replace(tzinfo=UTC)
        if channel.policy is None:
            channel.policy = SourceChannelPolicy(channel_id=channel.id)
        channel.policy.robots_state = "allowed"
        channel.policy.storage_scope = scope
        channel.policy.rag_scope = "none"
        channel.policy.terms_url = terms
        channel.policy.terms_state = "crossref_cc_vor" if terms else "metadata_only"
        channel.policy.reviewed_at = checked_at
        channel.policy.notes = "出版元数据及国别相关性已核对；摘要逐条检查 CC VoR。无全文或原始响应归档。"
        stored = persist_candidate_metadata(
            session,
            source.id,
            candidate,
            document_type="journal_article",
            extractor_name="cod_research_crossref",
            storage_scope=scope,
            terms_url=terms,
            channel_id=channel.id,
            reviewed_at=checked_at,
        )
        abstract_state = session.get(DocumentAbstractState, stored.document.id)
        if abstract_state is None:
            abstract_state = DocumentAbstractState(document_id=stored.document.id)
            session.add(abstract_state)
        abstract_state.channel_id = channel.id
        abstract_state.status = "stored" if candidate.summary else "unavailable"
        abstract_state.reason_code = "crossref_cc_vor" if candidate.summary else "no_licensed_abstract"
        abstract_state.last_checked_at = checked_at
        link = session.get(DocumentEntity, (stored.version.id, country.id, "about"))
        if link is None:
            session.add(
                DocumentEntity(
                    document_version_id=stored.version.id,
                    entity_id=country.id,
                    role="about",
                    extraction_method="imported",
                    review_status="confirmed",
                    note=f"出版来源资料已核对 {meta['verified_at']}：{meta['relevance_basis']}",
                )
            )
        channel.last_success_at = datetime.now(UTC)
        result["documents_created"] += int(stored.document_created)
        result["versions_created"] += int(stored.version_created)
        result["papers"].append(
            {
                "doi": candidate.doi,
                "title": candidate.title,
                "version_id": stored.version.id,
                "published_at": candidate.published_at.isoformat(),
                "abstract_stored": bool(candidate.summary),
            }
        )
    session.flush()
    return result
