from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    Document,
    DocumentAbstractState,
    DocumentVersion,
    PartnerDocumentRelease,
    PartnerDocumentReleaseItem,
    Source,
    StructuredDataset,
    StructuredDatasetPublication,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)
from app.schemas.partner import (
    PartnerApiInfo,
    PartnerDatasetDimensions,
    PartnerDatasetRead,
    PartnerDocumentPage,
    PartnerDocumentRead,
    PartnerObservationPage,
    PartnerObservationRead,
    PartnerSummaryRead,
)
from app.services.partner_access import PARTNER_KEY_HEADER, require_partner_client

router = APIRouter(
    prefix="/api/v1/partner",
    tags=["partner-read-only"],
    dependencies=[Depends(require_partner_client)],
)
DbSession = Annotated[Session, Depends(get_db)]
PageLimit = Annotated[int, Query(ge=1, le=500)]
PageOffset = Annotated[int, Query(ge=0)]
Iso3 = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
CodeFilter = Annotated[str | None, Query(min_length=1, max_length=80)]


def _publication_statement() -> Select:
    return (
        select(StructuredDatasetPublication, StructuredDataset, StructuredSnapshot)
        .join(StructuredDataset, StructuredDataset.id == StructuredDatasetPublication.dataset_id)
        .join(StructuredSnapshot, StructuredSnapshot.id == StructuredDatasetPublication.snapshot_id)
        .where(StructuredDatasetPublication.status == "published")
    )


def _publication_or_404(
    db: Session,
    dataset_key: str,
) -> tuple[StructuredDatasetPublication, StructuredDataset, StructuredSnapshot]:
    row = db.execute(
        _publication_statement().where(StructuredDataset.dataset_key == dataset_key)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="published dataset not found")
    return row


def _dataset_read(
    publication: StructuredDatasetPublication,
    dataset: StructuredDataset,
    snapshot: StructuredSnapshot,
) -> PartnerDatasetRead:
    return PartnerDatasetRead(
        dataset_key=dataset.dataset_key,
        name=dataset.name,
        frequency=dataset.frequency,
        scope_id=dataset.scope_id,
        published_snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        provider_version=snapshot.provider_version,
        retrieved_at=snapshot.retrieved_at,
        published_at=publication.published_at,
        expected_count=snapshot.expected_count,
        returned_count=snapshot.returned_count,
        valued_count=snapshot.valued_count,
        source_null_count=snapshot.source_null_count,
        not_returned_count=snapshot.not_returned_count,
        notes=publication.notes,
    )


@router.get("", response_model=PartnerApiInfo)
def get_partner_api_info() -> PartnerApiInfo:
    return PartnerApiInfo(
        name="国别智枢合作方数据 API",
        version="v1",
        documentation_path="/partner-docs",
        authentication_header=PARTNER_KEY_HEADER,
        publication_policy="only explicitly published document versions and dataset snapshots are returned",
    )


def _document_release_or_404(db: Session) -> PartnerDocumentRelease:
    release = db.scalar(select(PartnerDocumentRelease).where(PartnerDocumentRelease.status == "published"))
    if release is None:
        raise HTTPException(status_code=404, detail="published document release not found")
    return release


def _document_statement(release_id: int) -> Select:
    return (
        select(
            PartnerDocumentReleaseItem,
            Document,
            DocumentVersion,
            Source,
            DocumentAbstractState,
        )
        .join(Document, Document.id == PartnerDocumentReleaseItem.document_id)
        .join(
            DocumentVersion,
            and_(
                DocumentVersion.id == PartnerDocumentReleaseItem.document_version_id,
                DocumentVersion.document_id == PartnerDocumentReleaseItem.document_id,
            ),
        )
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentAbstractState, DocumentAbstractState.document_id == Document.id)
        .where(PartnerDocumentReleaseItem.release_id == release_id)
    )


def _dict_value(metadata: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = metadata.get(key)
    return value if isinstance(value, dict) else None


def _document_read(
    item: PartnerDocumentReleaseItem,
    document: Document,
    version: DocumentVersion,
    source: Source,
    abstract_state: DocumentAbstractState | None,
) -> PartnerDocumentRead:
    raw_authors = version.source_metadata.get("authors")
    authors = [str(value) for value in raw_authors] if isinstance(raw_authors, list) else []
    return PartnerDocumentRead(
        document_id=document.id,
        document_version_id=item.document_version_id,
        version_no=version.version_no,
        content_sha256=version.content_sha256,
        source_id=source.id,
        source_name=source.name,
        organization_name=source.organization_name,
        source_type=source.source_type,
        document_type=document.document_type,
        title=version.title,
        authors=authors,
        language=version.language,
        published_at=version.published_at,
        issue_date_text=version.issue_date_text,
        source_updated_at=version.source_updated_at,
        first_seen_at=document.first_seen_at,
        last_seen_at=document.last_seen_at,
        canonical_url=document.canonical_url,
        discovery_url=document.discovery_url,
        external_id=document.external_id,
        doi=document.doi,
        abstract=version.abstract,
        abstract_status=abstract_state.status if abstract_state is not None else None,
        abstract_provenance=_dict_value(version.source_metadata, "abstract_provenance"),
        published_at_provenance=_dict_value(
            version.source_metadata,
            "published_at_provenance",
        ),
        extractor_name=version.extractor_name,
        extractor_version=version.extractor_version,
        extracted_at=version.extracted_at,
    )


@router.get("/summary", response_model=PartnerSummaryRead)
def get_partner_summary(db: DbSession) -> PartnerSummaryRead:
    release = _document_release_or_404(db)
    structured_datasets = (
        db.scalar(
            select(func.count())
            .select_from(StructuredDatasetPublication)
            .where(StructuredDatasetPublication.status == "published")
        )
        or 0
    )
    structured_observations = (
        db.scalar(
            select(func.count())
            .select_from(StructuredSnapshotObservation)
            .join(
                StructuredDatasetPublication,
                StructuredDatasetPublication.snapshot_id == StructuredSnapshotObservation.snapshot_id,
            )
            .where(StructuredDatasetPublication.status == "published")
        )
        or 0
    )
    return PartnerSummaryRead(
        document_release_id=release.id,
        document_manifest_sha256=release.manifest_sha256,
        document_published_at=release.published_at,
        documents=release.item_count,
        current_abstracts=release.abstract_count,
        structured_datasets=structured_datasets,
        structured_observations=structured_observations,
        total_items=release.item_count + structured_observations,
    )


@router.get("/documents", response_model=PartnerDocumentPage)
def list_partner_documents(
    db: DbSession,
    source_id: Annotated[int | None, Query(ge=1)] = None,
    has_abstract: bool | None = None,
    query: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    document_type: CodeFilter = None,
    language: Annotated[str | None, Query(min_length=2, max_length=16)] = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
) -> PartnerDocumentPage:
    release = _document_release_or_404(db)
    if published_from is not None and published_to is not None and published_from > published_to:
        raise HTTPException(status_code=422, detail="published_from must not exceed published_to")
    statement = _document_statement(release.id)
    filters = {
        Document.source_id: source_id,
        Document.document_type: document_type,
        DocumentVersion.language: language,
    }
    for column, value in filters.items():
        if value is not None:
            statement = statement.where(column == value)
    nonempty_abstract = func.nullif(func.trim(DocumentVersion.abstract), "")
    if has_abstract is True:
        statement = statement.where(nonempty_abstract.is_not(None))
    elif has_abstract is False:
        statement = statement.where(nonempty_abstract.is_(None))
    if query is not None:
        statement = statement.where(DocumentVersion.title.contains(query.strip(), autoescape=True))
    if published_from is not None:
        statement = statement.where(DocumentVersion.published_at >= published_from)
    if published_to is not None:
        statement = statement.where(DocumentVersion.published_at <= published_to)

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(
        statement.order_by(
            func.coalesce(DocumentVersion.published_at, Document.first_seen_at).desc(),
            Document.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()
    items = [_document_read(*row) for row in rows]
    next_offset = offset + len(items) if offset + len(items) < total else None
    return PartnerDocumentPage(
        release_id=release.id,
        manifest_sha256=release.manifest_sha256,
        total=total,
        limit=limit,
        offset=offset,
        next_offset=next_offset,
        items=items,
    )


@router.get("/documents/{document_id}", response_model=PartnerDocumentRead)
def get_partner_document(document_id: int, db: DbSession) -> PartnerDocumentRead:
    release = _document_release_or_404(db)
    row = db.execute(_document_statement(release.id).where(Document.id == document_id)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="published document not found")
    return _document_read(*row)


@router.get("/datasets", response_model=list[PartnerDatasetRead])
def list_partner_datasets(db: DbSession) -> list[PartnerDatasetRead]:
    rows = db.execute(_publication_statement().order_by(StructuredDataset.dataset_key)).all()
    return [_dataset_read(*row) for row in rows]


@router.get("/datasets/{dataset_key}", response_model=PartnerDatasetRead)
def get_partner_dataset(dataset_key: str, db: DbSession) -> PartnerDatasetRead:
    return _dataset_read(*_publication_or_404(db, dataset_key))


def _observation_query(snapshot_id: int) -> Select:
    return (
        select(StructuredObservation, StructuredObservationVersion)
        .join(
            StructuredSnapshotObservation,
            StructuredSnapshotObservation.observation_id == StructuredObservation.id,
        )
        .join(
            StructuredObservationVersion,
            and_(
                StructuredObservationVersion.id == StructuredSnapshotObservation.observation_version_id,
                StructuredObservationVersion.observation_id == StructuredSnapshotObservation.observation_id,
            ),
        )
        .where(StructuredSnapshotObservation.snapshot_id == snapshot_id)
    )


@router.get(
    "/datasets/{dataset_key}/dimensions",
    response_model=PartnerDatasetDimensions,
)
def get_partner_dataset_dimensions(
    dataset_key: str,
    db: DbSession,
) -> PartnerDatasetDimensions:
    _, dataset, snapshot = _publication_or_404(db, dataset_key)
    rows = db.execute(
        _observation_query(snapshot.id).where(StructuredObservation.dataset_id == dataset.id)
    ).all()
    observations = [row[0] for row in rows]
    versions = [row[1] for row in rows]

    def values(name: str) -> list[str]:
        return sorted({str(value) for item in observations if (value := getattr(item, name)) is not None})

    return PartnerDatasetDimensions(
        dataset_key=dataset.dataset_key,
        snapshot_id=snapshot.id,
        countries=values("country_iso3"),
        partners=values("partner_iso3"),
        indicators=values("indicator_code"),
        commodity_classifications=values("commodity_classification"),
        commodity_codes=values("commodity_code"),
        trade_flows=values("trade_flow"),
        metrics=values("metric_code"),
        periods=sorted({item.period for item in observations}),
        units=sorted({item.unit for item in versions if item.unit is not None}),
    )


@router.get(
    "/datasets/{dataset_key}/observations",
    response_model=PartnerObservationPage,
)
def list_partner_observations(
    dataset_key: str,
    db: DbSession,
    country_iso3: Iso3 = None,
    partner_iso3: Iso3 = None,
    period_from: Annotated[int | None, Query(ge=1900, le=210012)] = None,
    period_to: Annotated[int | None, Query(ge=1900, le=210012)] = None,
    indicator_code: CodeFilter = None,
    commodity_code: CodeFilter = None,
    trade_flow: Literal["M", "X"] | None = None,
    metric_code: CodeFilter = None,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
) -> PartnerObservationPage:
    _, dataset, snapshot = _publication_or_404(db, dataset_key)
    if period_from is not None and period_to is not None and period_from > period_to:
        raise HTTPException(status_code=422, detail="period_from must not exceed period_to")

    statement = _observation_query(snapshot.id).where(StructuredObservation.dataset_id == dataset.id)
    filters = {
        StructuredObservation.country_iso3: country_iso3,
        StructuredObservation.partner_iso3: partner_iso3,
        StructuredObservation.indicator_code: indicator_code,
        StructuredObservation.commodity_code: commodity_code,
        StructuredObservation.trade_flow: trade_flow,
        StructuredObservation.metric_code: metric_code,
    }
    for column, value in filters.items():
        if value is not None:
            statement = statement.where(column == value)
    if period_from is not None:
        statement = statement.where(StructuredObservation.period >= period_from)
    if period_to is not None:
        statement = statement.where(StructuredObservation.period <= period_to)

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(
        statement.order_by(
            StructuredObservation.country_iso3,
            StructuredObservation.period,
            StructuredObservation.id,
        )
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        PartnerObservationRead(
            observation_key=observation.observation_key,
            country_iso3=observation.country_iso3,
            partner_iso3=observation.partner_iso3,
            indicator_code=observation.indicator_code,
            commodity_classification=observation.commodity_classification,
            source_dataset_code=observation.source_dataset_code,
            commodity_code=observation.commodity_code,
            trade_flow=observation.trade_flow,
            frequency=observation.frequency,
            period=observation.period,
            metric_code=observation.metric_code,
            value=version.value,
            unit=version.unit,
            currency=version.currency,
            price_basis=version.price_basis,
            source_url=version.source_url,
            source_status=version.source_status,
            missing_reason=version.missing_reason,
            is_reported=version.is_reported,
            is_aggregate=version.is_aggregate,
            quality_flags=version.quality_flags,
        )
        for observation, version in rows
    ]
    next_offset = offset + len(items) if offset + len(items) < total else None
    return PartnerObservationPage(
        dataset_key=dataset.dataset_key,
        snapshot_id=snapshot.id,
        total=total,
        limit=limit,
        offset=offset,
        next_offset=next_offset,
        items=items,
    )
