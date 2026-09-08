from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models import (
    Document,
    DocumentVersion,
    PartnerApiClient,
    PartnerDocumentRelease,
    PartnerDocumentReleaseItem,
    StructuredDataset,
    StructuredDatasetPublication,
    StructuredSnapshot,
)
from app.services.partner_access import create_partner_client

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CREDENTIAL_DIR = ROOT / "var" / "partner-credentials"


def _latest_snapshot(db: Session, dataset_id: int) -> StructuredSnapshot | None:
    return db.scalar(
        select(StructuredSnapshot)
        .where(StructuredSnapshot.dataset_id == dataset_id)
        .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
        .limit(1)
    )


def publish_latest_snapshot(
    db: Session,
    *,
    dataset: StructuredDataset,
    approved_by: str,
    notes: str,
) -> StructuredDatasetPublication:
    snapshot = _latest_snapshot(db, dataset.id)
    if snapshot is None:
        raise ValueError(f"dataset has no snapshots: {dataset.dataset_key}")
    now = datetime.now(UTC)
    current = db.scalar(
        select(StructuredDatasetPublication).where(
            StructuredDatasetPublication.dataset_id == dataset.id,
            StructuredDatasetPublication.status == "published",
        )
    )
    if current is not None and current.snapshot_id == snapshot.id:
        current.approved_by = approved_by
        current.notes = notes
        return current
    if current is not None:
        current.status = "withdrawn"
        current.withdrawn_at = now
        db.flush()

    publication = db.scalar(
        select(StructuredDatasetPublication).where(
            StructuredDatasetPublication.dataset_id == dataset.id,
            StructuredDatasetPublication.snapshot_id == snapshot.id,
        )
    )
    if publication is None:
        publication = StructuredDatasetPublication(
            dataset_id=dataset.id,
            snapshot_id=snapshot.id,
            approved_by=approved_by,
            notes=notes,
            status="published",
            published_at=now,
        )
        db.add(publication)
    else:
        publication.status = "published"
        publication.approved_by = approved_by
        publication.notes = notes
        publication.published_at = now
        publication.withdrawn_at = None
    db.flush()
    return publication


def publish_current_documents(
    db: Session,
    *,
    approved_by: str,
    notes: str,
) -> tuple[PartnerDocumentRelease, bool]:
    rows = list(
        db.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                and_(
                    DocumentVersion.document_id == Document.id,
                    DocumentVersion.version_no == Document.latest_version_no,
                ),
            )
            .order_by(Document.id)
        ).tuples()
    )
    document_count = db.scalar(select(func.count()).select_from(Document)) or 0
    if not rows:
        raise ValueError("no documents available for partner release")
    if len(rows) != document_count:
        raise ValueError("not every document has a latest version")

    manifest = hashlib.sha256()
    for document, version in rows:
        manifest.update(f"{document.id}:{version.id}:{version.content_sha256}\n".encode())
    manifest_sha256 = manifest.hexdigest()
    abstract_count = sum(bool(version.abstract and version.abstract.strip()) for _, version in rows)
    now = datetime.now(UTC)
    current = db.scalar(
        select(PartnerDocumentRelease).where(PartnerDocumentRelease.status == "published").with_for_update()
    )
    if current is not None and current.manifest_sha256 == manifest_sha256:
        current.approved_by = approved_by
        current.notes = notes
        return current, True
    if current is not None:
        current.status = "withdrawn"
        current.withdrawn_at = now
        db.flush()

    release = db.scalar(
        select(PartnerDocumentRelease).where(PartnerDocumentRelease.manifest_sha256 == manifest_sha256)
    )
    if release is None:
        release = PartnerDocumentRelease(
            status="published",
            approved_by=approved_by,
            notes=notes,
            item_count=len(rows),
            abstract_count=abstract_count,
            manifest_sha256=manifest_sha256,
            published_at=now,
        )
        db.add(release)
        db.flush()
        db.add_all(
            PartnerDocumentReleaseItem(
                release_id=release.id,
                document_id=document.id,
                document_version_id=version.id,
            )
            for document, version in rows
        )
    else:
        release.status = "published"
        release.approved_by = approved_by
        release.notes = notes
        release.published_at = now
        release.withdrawn_at = None
    db.flush()
    return release, False


def _safe_credential_name(name: str, prefix: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.") or "partner"
    return f"{slug}-{prefix}.txt"


def _write_credentials(path: Path, *, name: str, api_key: str, expires_at: datetime | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        f"partner_name={name}\n"
        f"api_base_url=http://127.0.0.1:8000/api/v1/partner\n"
        f"documentation_url=http://127.0.0.1:8000/partner-docs\n"
        f"header=X-API-Key\n"
        f"api_key={api_key}\n"
        f"expires_at={expires_at.isoformat() if expires_at else ''}\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _create_client(args: argparse.Namespace) -> dict:
    with get_session_factory()() as db:
        client, plaintext_key = create_partner_client(
            db,
            name=args.name,
            expires_in_days=args.expires_in_days,
        )
        credential_path = DEFAULT_CREDENTIAL_DIR / _safe_credential_name(client.name, client.key_prefix)
        _write_credentials(
            credential_path,
            name=client.name,
            api_key=plaintext_key,
            expires_at=client.expires_at,
        )
        db.commit()
        return {
            "client_id": client.id,
            "name": client.name,
            "key_prefix": client.key_prefix,
            "expires_at": client.expires_at.isoformat() if client.expires_at else None,
            "credential_file": str(credential_path),
        }


def _publish(args: argparse.Namespace) -> dict:
    approved_by = args.approved_by.strip()
    if not approved_by:
        raise ValueError("approved_by must not be empty")
    with get_session_factory()() as db:
        statement = select(StructuredDataset).order_by(StructuredDataset.dataset_key)
        if args.dataset_key:
            statement = statement.where(StructuredDataset.dataset_key == args.dataset_key)
        datasets = list(db.scalars(statement))
        if not datasets:
            raise ValueError("no matching structured dataset")
        publications = [
            publish_latest_snapshot(
                db,
                dataset=dataset,
                approved_by=approved_by,
                notes=args.notes.strip(),
            )
            for dataset in datasets
        ]
        db.commit()
        return {
            "published": [
                {
                    "dataset_key": dataset.dataset_key,
                    "snapshot_id": publication.snapshot_id,
                }
                for dataset, publication in zip(datasets, publications, strict=True)
            ]
        }


def _publish_documents(args: argparse.Namespace) -> dict:
    approved_by = args.approved_by.strip()
    if not approved_by:
        raise ValueError("approved_by must not be empty")
    with get_session_factory()() as db:
        release, reused = publish_current_documents(
            db,
            approved_by=approved_by,
            notes=args.notes.strip(),
        )
        db.commit()
        return {
            "release_id": release.id,
            "manifest_sha256": release.manifest_sha256,
            "item_count": release.item_count,
            "abstract_count": release.abstract_count,
            "reused": reused,
        }


def _revoke(args: argparse.Namespace) -> dict:
    with get_session_factory()() as db:
        client = db.scalar(select(PartnerApiClient).where(PartnerApiClient.name == args.name))
        if client is None:
            raise ValueError("partner client not found")
        if client.status != "revoked":
            client.status = "revoked"
            client.revoked_at = datetime.now(UTC)
        db.commit()
        return {"client_id": client.id, "name": client.name, "status": client.status}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage partner API credentials and publications")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-client")
    create.add_argument("--name", required=True)
    create.add_argument("--expires-in-days", type=int, default=90)
    create.set_defaults(handler=_create_client)

    publish = subparsers.add_parser("publish-latest")
    publish.add_argument("--dataset-key")
    publish.add_argument("--approved-by", required=True)
    publish.add_argument("--notes", default="合作方内部研究使用；按精确快照发布。")
    publish.set_defaults(handler=_publish)

    publish_documents = subparsers.add_parser("publish-documents")
    publish_documents.add_argument("--approved-by", required=True)
    publish_documents.add_argument(
        "--notes",
        default="合作方内部研究使用；按文档精确版本发布。",
    )
    publish_documents.set_defaults(handler=_publish_documents)

    revoke = subparsers.add_parser("revoke-client")
    revoke.add_argument("--name", required=True)
    revoke.set_defaults(handler=_revoke)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        payload = args.handler(args)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
