from datetime import UTC, datetime
from typing import Annotated

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from kombu.exceptions import KombuError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.models import CollectionRun, Source, SourceChannel, SourceChannelPolicy
from app.schemas.sources import (
    CollectionRunRead,
    SourceChannelCreate,
    SourceChannelRead,
    SourceCreate,
    SourceRead,
)
from app.workers.tasks import probe_source_channel

router = APIRouter(prefix="/api/v1", tags=["sources"])
DbSession = Annotated[Session, Depends(get_db)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


def require_write_access() -> None:
    if get_settings().public_api_read_only:
        raise HTTPException(status_code=403, detail="public API is read-only")


def _get_source_or_404(db: Session, source_id: int) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    return source


@router.post(
    "/sources",
    response_model=SourceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_write_access)],
)
def create_source(payload: SourceCreate, db: DbSession) -> Source:
    source = Source(**payload.model_dump())
    db.add(source)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="source already exists") from exc
    db.refresh(source)
    return source


@router.get("/sources", response_model=list[SourceRead])
def list_sources(
    db: DbSession,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> list[Source]:
    statement = select(Source).order_by(Source.id).limit(limit).offset(offset)
    return list(db.scalars(statement))


@router.get("/sources/{source_id}", response_model=SourceRead)
def get_source(source_id: int, db: DbSession) -> Source:
    return _get_source_or_404(db, source_id)


@router.post(
    "/sources/{source_id}/channels",
    response_model=SourceChannelRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_write_access)],
)
def create_source_channel(source_id: int, payload: SourceChannelCreate, db: DbSession) -> SourceChannel:
    _get_source_or_404(db, source_id)
    channel = SourceChannel(source_id=source_id, **payload.model_dump())
    db.add(channel)
    try:
        db.flush()
        db.add(SourceChannelPolicy(channel_id=channel.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="source channel already exists") from exc
    db.refresh(channel)
    return channel


@router.get("/sources/{source_id}/channels", response_model=list[SourceChannelRead])
def list_source_channels(source_id: int, db: DbSession) -> list[SourceChannel]:
    _get_source_or_404(db, source_id)
    statement = select(SourceChannel).where(SourceChannel.source_id == source_id).order_by(SourceChannel.id)
    return list(db.scalars(statement))


@router.post(
    "/source-channels/{channel_id}/probe",
    response_model=CollectionRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_write_access)],
)
def create_source_probe(channel_id: int, db: DbSession) -> CollectionRun:
    channel = db.get(SourceChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="source channel not found")
    if channel.status == "retired":
        raise HTTPException(status_code=409, detail="retired source channel cannot be probed")

    active_run_id = db.scalar(
        select(CollectionRun.id).where(
            CollectionRun.channel_id == channel_id,
            CollectionRun.status.in_(("queued", "running")),
        )
    )
    if active_run_id is not None:
        raise HTTPException(status_code=409, detail="source channel already has an active run")

    prior_status = channel.status
    run = CollectionRun(
        channel_id=channel_id,
        trigger_kind="probe",
        status="queued",
        report={"channel_status_before": prior_status},
    )
    channel.status = "probing"
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="source channel already has an active run") from exc
    db.refresh(run)

    try:
        task = probe_source_channel.apply_async(args=[run.id])
    except (CeleryError, KombuError, OSError) as exc:
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.error_category = "internal"
        run.error_code = "broker_unavailable"
        run.error_message = "probe task could not be queued"
        run.report = {"channel_status_before": prior_status, "retryable": True}
        channel.status = prior_status
        db.commit()
        raise HTTPException(status_code=503, detail="probe queue unavailable") from exc

    run.celery_task_id = task.id
    db.commit()
    db.refresh(run)
    return run


@router.get("/collection-runs/{run_id}", response_model=CollectionRunRead)
def get_collection_run(run_id: int, db: DbSession) -> CollectionRun:
    run = db.get(CollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="collection run not found")
    return run
