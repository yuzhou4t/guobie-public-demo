from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.collectors.base import CandidateDocument, clean_text
from app.models import Document, DocumentVersion

_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PersistCandidateResult:
    document: Document | None
    version: DocumentVersion | None
    document_created: bool
    version_created: bool
    skipped_reason: str | None = None


def persist_candidate_metadata(
    session: Session,
    source_id: int,
    candidate: CandidateDocument,
    *,
    document_type: str,
    extractor_name: str,
    extractor_version: str = "v1",
    seen_at: datetime | None = None,
    storage_scope: str = "metadata",
    terms_url: str | None = None,
    channel_id: int | None = None,
    reviewed_at: datetime | None = None,
    preserve_existing_abstract: bool = True,
) -> PersistCandidateResult:
    """Persist metadata and, only for a reviewed scope, the source-provided abstract."""

    if storage_scope not in {"metadata", "official_abstract"}:
        raise ValueError("document persistence supports metadata or official_abstract storage")
    normalized_terms_url = clean_text(terms_url)
    if storage_scope == "official_abstract" and (
        normalized_terms_url is None or not normalized_terms_url.startswith("https://")
    ):
        raise ValueError("official_abstract storage requires a reviewed HTTPS terms URL")

    title = clean_text(candidate.title)
    if title is None:
        return PersistCandidateResult(None, None, False, False, "missing_title")

    seen_at = _utc(seen_at or datetime.now(UTC))
    doi = normalize_doi(candidate.doi)
    canonical_url = clean_text(candidate.canonical_url)
    external_id = clean_text(candidate.external_id)
    discovery_url = clean_text(candidate.discovery_url)
    if not any((doi, canonical_url, external_id, discovery_url)):
        return PersistCandidateResult(None, None, False, False, "missing_identity")

    document = _find_document(
        session,
        source_id=source_id,
        doi=doi,
        canonical_url=canonical_url,
        external_id=external_id,
        discovery_url=discovery_url,
    )
    document_created = document is None
    document_versions = (
        list(session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id)))
        if document is not None
        else []
    )
    previous_version = next(
        (version for version in document_versions if version.version_no == document.latest_version_no),
        None,
    )

    effective_doi = doi or (document.doi if document else None)
    effective_canonical_url = canonical_url or (document.canonical_url if document else None)
    effective_external_id = external_id or (document.external_id if document else None)
    effective_discovery_url = discovery_url or (document.discovery_url if document else None)
    language = clean_text(candidate.language) or (document.language if document else "und")
    published_at = _utc(candidate.published_at) if candidate.published_at else None
    source_updated_at = _utc(candidate.source_updated_at) if candidate.source_updated_at else None
    issue_date_text = clean_text(candidate.issue_text)
    authors = tuple(author for value in candidate.authors if (author := clean_text(value)))
    source_metadata = _normalized_source_metadata(candidate.source_metadata, authors)
    abstract = clean_text(candidate.summary) if storage_scope == "official_abstract" else None
    if abstract is not None:
        summary_kind = candidate.summary_kind
        summary_source_url = clean_text(candidate.summary_source_url)
        if summary_kind not in {"feed_summary", "api_abstract", "html_summary"}:
            raise ValueError("official_abstract storage requires a declared summary kind")
        if summary_source_url is not None and summary_source_url.startswith("http://"):
            summary_source_url = "https://" + summary_source_url[7:]
        if summary_source_url is None or not summary_source_url.startswith("https://"):
            raise ValueError("official_abstract storage requires an HTTPS summary source URL")
        abstract_license_url = clean_text(source_metadata.get("abstract_license_url"))
        abstract_license_content_version = clean_text(source_metadata.get("abstract_license_content_version"))
        abstract_license_start_at = clean_text(source_metadata.get("abstract_license_start_at"))
        abstract_fetch_policy_version = clean_text(source_metadata.get("abstract_fetch_policy_version"))
        source_metadata["abstract_provenance"] = {
            "content_kind": summary_kind,
            "source_url": summary_source_url,
            "terms_url": normalized_terms_url,
            **({"license_url": abstract_license_url} if abstract_license_url is not None else {}),
            **(
                {"license_content_version": abstract_license_content_version}
                if abstract_license_content_version is not None
                else {}
            ),
            **(
                {"license_start_at": abstract_license_start_at}
                if abstract_license_start_at is not None
                else {}
            ),
            **(
                {"fetch_policy_version": abstract_fetch_policy_version}
                if abstract_fetch_policy_version is not None
                else {}
            ),
            **({"channel_id": channel_id} if channel_id is not None else {}),
            **({"reviewed_at": _isoformat(_utc(reviewed_at))} if reviewed_at is not None else {}),
        }
    elif preserve_existing_abstract and previous_version is not None:
        abstract = clean_text(previous_version.abstract)
        previous_provenance = previous_version.source_metadata.get("abstract_provenance")
        if abstract is not None and isinstance(previous_provenance, Mapping):
            source_metadata["abstract_provenance"] = _json_safe(dict(previous_provenance))

    # Keep a reviewed method across metadata-only refreshes while its exact
    # supporting excerpt still exists in the effective abstract.
    if previous_version is not None:
        previous_metadata = previous_version.source_metadata
        method = previous_metadata.get("research_method")
        proof = previous_metadata.get("field_provenance", {}).get("research_method", {})
        if (
            method
            and source_metadata.get("research_method") in {None, method}
            and proof.get("field") == "abstract"
            and proof.get("quote")
            and proof["quote"] in (abstract or "")
        ):
            source_metadata["research_method"] = method
            source_metadata["field_provenance"] = {
                **source_metadata.get("field_provenance", {}),
                "research_method": proof,
            }

    version_payload = {
        "document_type": document_type,
        "title": title,
        "doi": effective_doi,
        "canonical_url": effective_canonical_url,
        "discovery_url": effective_discovery_url,
        "external_id": effective_external_id,
        "authors": list(authors),
        "language": language,
        "published_at": _isoformat(published_at),
        "source_updated_at": _isoformat(source_updated_at),
        "issue_date_text": issue_date_text,
        "source_metadata": source_metadata,
    }
    if abstract is not None:
        version_payload["abstract"] = abstract
    content_sha256 = _metadata_hash(version_payload)

    if document is not None:
        existing_version = next(
            (version for version in document_versions if version.content_sha256 == content_sha256),
            None,
        )
        if existing_version is not None and clean_text(existing_version.abstract) != abstract:
            source_metadata["content_integrity_repair"] = {
                "reason": "legacy_abstract_hash_mismatch",
                "expected_abstract_present": abstract is not None,
            }
            version_payload["source_metadata"] = source_metadata
            content_sha256 = _metadata_hash(version_payload)
            existing_version = next(
                (version for version in document_versions if version.content_sha256 == content_sha256),
                None,
            )
        if existing_version is not None:
            document.canonical_url = effective_canonical_url
            document.discovery_url = effective_discovery_url
            document.external_id = effective_external_id
            document.doi = effective_doi
            document.document_type = document_type
            document.title = existing_version.title
            document.language = existing_version.language
            document.published_at = existing_version.published_at
            document.issue_date_text = existing_version.issue_date_text
            document.last_seen_at = seen_at
            document.latest_version_no = existing_version.version_no
            session.flush()
            return PersistCandidateResult(document, existing_version, False, False)
    else:
        document = Document(
            source_id=source_id,
            canonical_url=effective_canonical_url,
            discovery_url=effective_discovery_url,
            external_id=effective_external_id,
            doi=effective_doi,
            document_type=document_type,
            title=title,
            language=language,
            published_at=published_at,
            issue_date_text=issue_date_text,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            latest_version_no=1,
        )
        session.add(document)
        session.flush()

    if not document_created:
        document.canonical_url = effective_canonical_url
        document.discovery_url = effective_discovery_url
        document.external_id = effective_external_id
        document.doi = effective_doi
        document.document_type = document_type
        document.title = title
        document.language = language
        document.published_at = published_at
        document.issue_date_text = issue_date_text
        document.last_seen_at = seen_at
        max_version_no = max((version.version_no for version in document_versions), default=0)
        document.latest_version_no = (max_version_no or 0) + 1

    version = DocumentVersion(
        document_id=document.id,
        raw_asset_id=None,
        version_no=document.latest_version_no,
        content_sha256=content_sha256,
        title=title,
        abstract=abstract,
        body_text=None,
        language=language,
        published_at=published_at,
        source_updated_at=source_updated_at,
        issue_date_text=issue_date_text,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        source_metadata=source_metadata,
        extracted_at=seen_at,
    )
    session.add(version)
    session.flush()
    return PersistCandidateResult(document, version, document_created, True)


def persist_official_abstract(
    session: Session,
    document: Document,
    summary: str,
    *,
    summary_kind: str,
    summary_source_url: str,
    channel_id: int,
    terms_url: str,
    reviewed_at: datetime,
    seen_at: datetime | None = None,
    license_metadata: Mapping[str, str] | None = None,
) -> PersistCandidateResult:
    """Add a reviewed abstract while preserving the document's current metadata."""

    current = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.version_no == document.latest_version_no,
        )
    )
    if current is None:
        raise ValueError("document has no current version")
    raw_authors = current.source_metadata.get("authors", [])
    authors = tuple(str(value) for value in raw_authors) if isinstance(raw_authors, list) else ()
    candidate = CandidateDocument(
        discovery_url=document.discovery_url or document.canonical_url or "",
        canonical_url=document.canonical_url,
        external_id=document.external_id,
        title=current.title,
        doi=document.doi,
        authors=authors,
        published_at=current.published_at,
        source_updated_at=current.source_updated_at,
        issue_text=current.issue_date_text,
        summary=summary,
        summary_kind=summary_kind,  # type: ignore[arg-type]
        summary_source_url=summary_source_url,
        language=current.language,
        media_type=None,
        source_metadata={
            key: value
            for key, value in current.source_metadata.items()
            if key
            not in {
                "abstract_provenance",
                "abstract_license_url",
                "abstract_license_content_version",
                "abstract_license_start_at",
                "abstract_fetch_policy_version",
                "abstract_withdrawal",
                "authors",
            }
        }
        | dict(license_metadata or {}),
    )
    return persist_candidate_metadata(
        session,
        document.source_id,
        candidate,
        document_type=document.document_type,
        extractor_name="abstract_backfill",
        extractor_version="v1",
        seen_at=seen_at,
        storage_scope="official_abstract",
        terms_url=terms_url,
        channel_id=channel_id,
        reviewed_at=reviewed_at,
    )


def remove_official_abstract(
    session: Session,
    document: Document,
    *,
    reason: str,
    seen_at: datetime | None = None,
) -> PersistCandidateResult:
    """Make an unverified prior abstract non-current while preserving metadata history."""

    current = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.version_no == document.latest_version_no,
        )
    )
    if current is None:
        raise ValueError("document has no current version")
    raw_authors = current.source_metadata.get("authors", [])
    authors = tuple(str(value) for value in raw_authors) if isinstance(raw_authors, list) else ()
    candidate = CandidateDocument(
        discovery_url=document.discovery_url or document.canonical_url or "",
        canonical_url=document.canonical_url,
        external_id=document.external_id,
        title=current.title,
        doi=document.doi,
        authors=authors,
        published_at=current.published_at,
        source_updated_at=current.source_updated_at,
        issue_text=current.issue_date_text,
        language=current.language,
        source_metadata={
            key: value
            for key, value in current.source_metadata.items()
            if key
            not in {
                "abstract_provenance",
                "abstract_license_url",
                "abstract_license_content_version",
                "abstract_license_start_at",
                "abstract_fetch_policy_version",
                "abstract_withdrawal",
                "authors",
            }
        }
        | {"abstract_withdrawal": {"reason": reason}},
    )
    return persist_candidate_metadata(
        session,
        document.source_id,
        candidate,
        document_type=document.document_type,
        extractor_name="abstract_policy_revalidation",
        extractor_version="v1",
        seen_at=seen_at,
        storage_scope="metadata",
        preserve_existing_abstract=False,
    )


def normalize_doi(value: str | None) -> str | None:
    doi = clean_text(value)
    if doi is None:
        return None
    normalized = _DOI_PREFIX.sub("", doi).strip().lower()
    return normalized or None


def _find_document(
    session: Session,
    *,
    source_id: int,
    doi: str | None,
    canonical_url: str | None,
    external_id: str | None,
    discovery_url: str | None,
) -> Document | None:
    identities = tuple(
        identity
        for identity in (
            (Document.doi == doi) if doi else None,
            (Document.canonical_url == canonical_url) if canonical_url else None,
            (
                (Document.source_id == source_id) & (Document.external_id == external_id)
                if external_id
                else None
            ),
            (
                (Document.source_id == source_id) & (Document.discovery_url == discovery_url)
                if discovery_url
                else None
            ),
        )
        if identity is not None
    )
    documents = list(session.scalars(select(Document).where(or_(*identities))))
    for identity_name, identity_value in (
        ("doi", doi),
        ("canonical_url", canonical_url),
        ("external_id", external_id),
        ("discovery_url", discovery_url),
    ):
        if identity_value is None:
            continue
        for document in documents:
            if identity_name in {"external_id", "discovery_url"} and document.source_id != source_id:
                continue
            if getattr(document, identity_name) == identity_value:
                return document
    return None


def _normalized_source_metadata(metadata: Mapping[str, Any], authors: tuple[str, ...]) -> dict[str, Any]:
    normalized = _json_safe(dict(metadata))
    assert isinstance(normalized, dict)
    if authors:
        normalized["authors"] = list(authors)
    return normalized


def _metadata_hash(metadata: dict[str, Any]) -> str:
    serialized = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _isoformat(_utc(value))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
