"""Apply explicitly reviewed method descriptions supported by visible abstract quotes."""

import argparse
import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.research import get_material
from app.collectors.base import CandidateDocument
from app.db.session import get_engine, get_session_factory
from app.models import Document, DocumentEntity, DocumentVersion
from app.services.document_store import persist_candidate_metadata


def enrich_method(session: Session, evidence: dict) -> dict:
    version = session.get(DocumentVersion, evidence["document_version_id"])
    if version is None:
        raise ValueError("Evidence version does not exist")
    document = session.scalar(select(Document).where(Document.id == version.document_id).with_for_update())
    current = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.version_no == document.latest_version_no,
        )
    )
    checked = date.fromisoformat(evidence["checked_at"])
    if checked > date.today():
        raise ValueError("Evidence verification date is in the future")
    material = get_material(version.id, session)
    quote, method = evidence["quote"].strip(), evidence["method"].strip()
    if (
        not quote
        or not method
        or quote not in (material.get("abstract") or "")
        or not any(item["iso3"] == "COD" for item in material.get("countries", []))
    ):
        raise ValueError("Method requires a verbatim quote in a visible COD abstract")
    if current.source_metadata.get("research_method") == method:
        return {"old_version_id": version.id, "version_id": current.id, "created": False}
    if current.id != version.id or current.source_metadata.get("research_method"):
        raise ValueError("Evidence version is stale or already has a different method")
    source_url = (current.source_metadata.get("abstract_provenance") or {}).get("source_url")
    if not source_url:
        raise ValueError("Abstract source provenance is required")
    proof = {
        "source_url": source_url,
        "checked_at": checked.isoformat(),
        "document_version_id": version.id,
        "field": "abstract",
        "quote": quote,
    }
    metadata = {
        **current.source_metadata,
        "research_method": method,
        "field_provenance": {**current.source_metadata.get("field_provenance", {}), "research_method": proof},
    }
    result = persist_candidate_metadata(
        session,
        document.source_id,
        CandidateDocument(
            discovery_url=document.discovery_url or document.canonical_url or "",
            canonical_url=document.canonical_url,
            external_id=document.external_id,
            title=current.title,
            doi=document.doi,
            authors=tuple(current.source_metadata.get("authors", [])),
            published_at=current.published_at,
            source_updated_at=current.source_updated_at,
            issue_text=current.issue_date_text,
            language=current.language,
            source_metadata=metadata,
        ),
        document_type=document.document_type,
        extractor_name="verified_method_enrichment",
    )
    if result.version_created:
        for link in session.scalars(
            select(DocumentEntity).where(DocumentEntity.document_version_id == current.id)
        ):
            session.add(
                DocumentEntity(
                    document_version_id=result.version.id,
                    entity_id=link.entity_id,
                    role=link.role,
                    extraction_method=link.extraction_method,
                    review_status=link.review_status,
                    note=link.note,
                )
            )
    return {
        "old_version_id": version.id,
        "version_id": result.version.id,
        "created": result.version_created,
        "method": method,
        "evidence": proof,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if get_engine().url.host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise ValueError("This enrichment command only supports the local database")
    with get_session_factory()() as session:
        result = [enrich_method(session, row) for row in json.loads(args.evidence.read_text())]
        if args.apply:
            session.commit()
        else:
            session.rollback()
    args.report.write_text(
        json.dumps({"applied": args.apply, "items": result}, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "applied": args.apply,
                "items": len(result),
                "versions_created": sum(r["created"] for r in result),
            }
        )
    )


if __name__ == "__main__":
    main()
