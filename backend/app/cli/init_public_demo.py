"""Initialize the selected sample in an explicitly empty independent database."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models import Document, DocumentEntity, DocumentVersion, ResearchEntity, Source, User
from app.models.public_demo import PublicDemoInstallation
from app.services.mvp_seed import import_cod_structured_seed
from app.services.public_demo_samples import sample_manifest
from app.services.research_capabilities import seed_capability_templates, seed_public_capability_catalog


def initialize(db):
    if not get_settings().public_demo_enabled:
        raise RuntimeError("Explicit public demo mode is required")
    payload = sample_manifest()
    marker = db.get(PublicDemoInstallation, 1)
    if marker:
        if marker.sample_version != payload["version"]:
            raise RuntimeError("Sample version mismatch")
        return payload["summary"]
    if db.scalar(select(User.id).limit(1)) or db.scalar(select(Source.id).limit(1)):
        raise RuntimeError("Refusing to initialize a nonempty unmarked database")
    with NamedTemporaryFile(mode="w+", suffix=".json") as seed:
        json.dump(payload["structured"], seed)
        seed.flush()
        import_cod_structured_seed(db, Path(seed.name))
    country = ResearchEntity(entity_type="country", canonical_key="COD", canonical_name="刚果（金）")
    db.add(country)
    db.flush()
    timestamp = datetime.now(UTC)
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
        published = datetime.fromisoformat(item["published_at"]) if item["published_at"] else None
        document = Document(
            source_id=source.id,
            title=item["title"],
            canonical_url=item["canonical_url"],
            doi=item["doi"],
            document_type=item["document_type"],
            language=item["language"],
            published_at=published,
            issue_date_text=item["issue_date_text"],
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            latest_version_no=1,
        )
        db.add(document)
        db.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_no=1,
            content_sha256=hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest(),
            title=item["title"],
            language=item["language"],
            published_at=published,
            extractor_name="selected-public-metadata",
            extractor_version="1",
            source_metadata={"sample_version": payload["version"]},
            extracted_at=timestamp,
        )
        db.add(version)
        db.flush()
        db.add(
            DocumentEntity(
                document_version_id=version.id,
                entity_id=country.id,
                role="about",
                extraction_method="imported",
                review_status="confirmed",
                note="沿用现有资料库已确认国别关联",
            )
        )
    root = Path(__file__).resolve().parents[3]
    seed_capability_templates(db, root / "data/research_capability_templates.json")
    seed_public_capability_catalog(db)
    db.add(PublicDemoInstallation(id=1, sample_version=payload["version"]))
    db.commit()
    return payload["summary"]


if __name__ == "__main__":
    with get_session_factory()() as session:
        print(json.dumps(initialize(session), ensure_ascii=False))
