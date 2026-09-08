from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    CollectionRun,
    Document,
    DocumentAbstractState,
    DocumentVersion,
    Source,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
)
from app.schemas.public_data import (
    PublicCollectionRunRead,
    PublicDocumentPage,
    PublicDocumentRead,
    PublicSummaryRead,
)

router = APIRouter(prefix="/api/v1/public", tags=["public-read-only"])
DbSession = Annotated[Session, Depends(get_db)]
PageLimit = Annotated[int, Query(ge=1, le=200)]
PageOffset = Annotated[int, Query(ge=0)]


def _current_document_statement():
    return (
        select(Document, Source, DocumentVersion, DocumentAbstractState)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(
            DocumentVersion,
            and_(
                DocumentVersion.document_id == Document.id,
                DocumentVersion.version_no == Document.latest_version_no,
            ),
        )
        .outerjoin(DocumentAbstractState, DocumentAbstractState.document_id == Document.id)
    )


def _document_read(
    document: Document,
    source: Source,
    version: DocumentVersion | None,
    abstract_state: DocumentAbstractState | None,
) -> PublicDocumentRead:
    return PublicDocumentRead(
        id=document.id,
        source_id=document.source_id,
        source_name=source.name,
        document_type=document.document_type,
        title=version.title if version is not None else document.title,
        language=version.language if version is not None else document.language,
        published_at=version.published_at if version is not None else document.published_at,
        issue_date_text=version.issue_date_text if version is not None else document.issue_date_text,
        first_seen_at=document.first_seen_at,
        last_seen_at=document.last_seen_at,
        latest_version_no=document.latest_version_no,
        canonical_url=document.canonical_url,
        discovery_url=document.discovery_url,
        doi=document.doi,
        abstract=version.abstract if version is not None else None,
        abstract_status=abstract_state.status if abstract_state is not None else None,
    )


@router.get("/summary", response_model=PublicSummaryRead)
def get_public_summary(db: DbSession) -> PublicSummaryRead:
    current_abstracts = db.scalar(
        select(func.count())
        .select_from(Document)
        .join(
            DocumentVersion,
            and_(
                DocumentVersion.document_id == Document.id,
                DocumentVersion.version_no == Document.latest_version_no,
            ),
        )
        .where(DocumentVersion.abstract.is_not(None))
    )
    return PublicSummaryRead(
        sources=db.scalar(select(func.count()).select_from(Source)) or 0,
        current_channels=db.scalar(
            select(func.count()).select_from(SourceChannel).where(SourceChannel.status != "retired")
        )
        or 0,
        documents=db.scalar(select(func.count()).select_from(Document)) or 0,
        current_abstracts=current_abstracts or 0,
        structured_datasets=db.scalar(select(func.count()).select_from(StructuredDataset)) or 0,
        structured_observations=db.scalar(select(func.count()).select_from(StructuredObservation)) or 0,
        latest_collection_finished_at=db.scalar(select(func.max(CollectionRun.finished_at))),
    )


@router.get("/documents", response_model=PublicDocumentPage)
def list_public_documents(
    db: DbSession,
    source_id: Annotated[int | None, Query(ge=1)] = None,
    has_abstract: bool | None = None,
    query: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> PublicDocumentPage:
    filters = []
    if source_id is not None:
        filters.append(Document.source_id == source_id)
    if has_abstract is True:
        filters.append(DocumentVersion.abstract.is_not(None))
    elif has_abstract is False:
        filters.append(DocumentVersion.abstract.is_(None))
    if query is not None:
        filters.append(Document.title.contains(query.strip(), autoescape=True))

    count_statement = (
        select(func.count())
        .select_from(Document)
        .outerjoin(
            DocumentVersion,
            and_(
                DocumentVersion.document_id == Document.id,
                DocumentVersion.version_no == Document.latest_version_no,
            ),
        )
        .where(*filters)
    )
    rows = db.execute(
        _current_document_statement()
        .where(*filters)
        .order_by(func.coalesce(Document.published_at, Document.first_seen_at).desc(), Document.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return PublicDocumentPage(
        total=db.scalar(count_statement) or 0,
        limit=limit,
        offset=offset,
        items=[_document_read(*row) for row in rows],
    )


@router.get("/documents/{document_id}", response_model=PublicDocumentRead)
def get_public_document(document_id: int, db: DbSession) -> PublicDocumentRead:
    row = db.execute(_current_document_statement().where(Document.id == document_id)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    return _document_read(*row)


@router.get("/collection-runs", response_model=list[PublicCollectionRunRead])
def list_public_collection_runs(
    db: DbSession,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> list[PublicCollectionRunRead]:
    rows = db.execute(
        select(CollectionRun, SourceChannel, Source)
        .join(SourceChannel, SourceChannel.id == CollectionRun.channel_id)
        .join(Source, Source.id == SourceChannel.source_id)
        .order_by(CollectionRun.created_at.desc(), CollectionRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        PublicCollectionRunRead(
            id=run.id,
            source_id=source.id,
            source_name=source.name,
            channel_id=channel.id,
            channel_name=channel.name,
            trigger_kind=run.trigger_kind,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            items_discovered=run.items_discovered,
            items_persisted=run.items_persisted,
            error_category=run.error_category,
            error_code=run.error_code,
            retry_count=run.retry_count,
        )
        for run, channel, source in rows
    ]
