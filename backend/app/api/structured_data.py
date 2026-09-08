import csv
import io
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)
from app.schemas.structured_data import (
    StructuredDatasetRead,
    StructuredObservationPage,
    StructuredObservationRead,
    StructuredSnapshotRead,
)
from app.services.country_data_catalog import SERIES_DIMENSIONS, data_permissions

router = APIRouter(prefix="/api/v1/structured-datasets", tags=["structured-data"])
DbSession = Annotated[Session, Depends(get_db)]
PageLimit = Annotated[int, Query(ge=1, le=1000)]
PageOffset = Annotated[int, Query(ge=0)]
Iso3 = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]
CodeFilter = Annotated[str | None, Query(min_length=1, max_length=80)]


def _get_dataset_or_404(db: Session, dataset_id: int) -> StructuredDataset:
    dataset = db.get(StructuredDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="structured dataset not found")
    return dataset


def _latest_snapshot(db: Session, dataset_id: int) -> StructuredSnapshot | None:
    return db.scalar(
        select(StructuredSnapshot)
        .where(StructuredSnapshot.dataset_id == dataset_id)
        .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
        .limit(1)
    )


def _dataset_read(
    db: Session,
    dataset: StructuredDataset,
) -> StructuredDatasetRead:
    channel_status = db.scalar(select(SourceChannel.status).where(SourceChannel.id == dataset.channel_id))
    if channel_status is None:
        raise HTTPException(status_code=500, detail="structured dataset channel is missing")
    latest_snapshot = _latest_snapshot(db, dataset.id)
    if latest_snapshot is not None:
        require_snapshot_permission(db, latest_snapshot, "view")
    return StructuredDatasetRead(
        id=dataset.id,
        channel_id=dataset.channel_id,
        dataset_key=dataset.dataset_key,
        name=dataset.name,
        scope_id=dataset.scope_id,
        frequency=dataset.frequency,
        channel_status=channel_status,
        latest_snapshot_id=latest_snapshot.id if latest_snapshot else None,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


@router.get("", response_model=list[StructuredDatasetRead])
def list_structured_datasets(
    db: DbSession,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
) -> list[StructuredDatasetRead]:
    datasets = db.scalars(
        select(StructuredDataset).order_by(StructuredDataset.id).limit(limit).offset(offset)
    )
    return [
        _dataset_read(db, dataset)
        for dataset in datasets
        if (snapshot := _latest_snapshot(db, dataset.id)) is None
        or data_permissions(snapshot.source_metadata)["view"]["allowed"]
    ]


@router.get("/{dataset_id}", response_model=StructuredDatasetRead)
def get_structured_dataset(dataset_id: int, db: DbSession) -> StructuredDatasetRead:
    return _dataset_read(db, _get_dataset_or_404(db, dataset_id))


@router.get("/{dataset_id}/snapshots", response_model=list[StructuredSnapshotRead])
def list_structured_snapshots(
    dataset_id: int,
    db: DbSession,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
) -> list[StructuredSnapshot]:
    dataset = _get_dataset_or_404(db, dataset_id)
    _dataset_read(db, dataset)
    return list(
        db.scalars(
            select(StructuredSnapshot)
            .where(StructuredSnapshot.dataset_id == dataset_id)
            .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )


def _observation_query(
    snapshot_id: int,
) -> Select[tuple[StructuredObservation, StructuredObservationVersion]]:
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


@router.get("/{dataset_id}/observations", response_model=StructuredObservationPage)
def list_structured_observations(
    dataset_id: int,
    db: DbSession,
    snapshot_id: Annotated[int | None, Query(ge=1)] = None,
    country_iso3: Iso3 = None,
    partner_iso3: Iso3 = None,
    period_from: Annotated[int | None, Query(ge=1900, le=210012)] = None,
    period_to: Annotated[int | None, Query(ge=1900, le=210012)] = None,
    indicator_code: CodeFilter = None,
    commodity_code: CodeFilter = None,
    trade_flow: Literal["M", "X"] | None = None,
    metric_code: CodeFilter = None,
    commodity_classification: CodeFilter = None,
    partner2_code: CodeFilter = None,
    customs_code: CodeFilter = None,
    mot_code: CodeFilter = None,
    source_dataset_code: CodeFilter = None,
    series_key: Annotated[str | None, Query(max_length=2000)] = None,
    limit: PageLimit = 100,
    offset: PageOffset = 0,
) -> StructuredObservationPage:
    dataset = _get_dataset_or_404(db, dataset_id)
    if period_from is not None and period_to is not None and period_from > period_to:
        raise HTTPException(status_code=422, detail="period_from must not exceed period_to")

    if snapshot_id is None:
        snapshot = _latest_snapshot(db, dataset.id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="structured dataset has no snapshots")
    else:
        snapshot = db.scalar(
            select(StructuredSnapshot).where(
                StructuredSnapshot.id == snapshot_id,
                StructuredSnapshot.dataset_id == dataset.id,
            )
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="structured snapshot not found for dataset")

    require_snapshot_permission(db, snapshot, "view")
    statement = _observation_query(snapshot.id).where(StructuredObservation.dataset_id == dataset.id)
    filters = {
        StructuredObservation.country_iso3: country_iso3,
        StructuredObservation.partner_iso3: partner_iso3,
        StructuredObservation.indicator_code: indicator_code,
        StructuredObservation.commodity_code: commodity_code,
        StructuredObservation.trade_flow: trade_flow,
        StructuredObservation.metric_code: metric_code,
        StructuredObservation.commodity_classification: commodity_classification,
        StructuredObservation.partner2_code: partner2_code,
        StructuredObservation.customs_code: customs_code,
        StructuredObservation.mot_code: mot_code,
        StructuredObservation.source_dataset_code: source_dataset_code,
    }
    for column, value in filters.items():
        if value is not None:
            statement = statement.where(column == value)
    if series_key is not None:
        try:
            dimensions = json.loads(series_key)
            if not isinstance(dimensions, list) or len(dimensions) != len(SERIES_DIMENSIONS):
                raise ValueError("invalid dimensions")
            for field, value in zip(SERIES_DIMENSIONS, dimensions, strict=True):
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid dimension value")
                column = getattr(StructuredObservation, field)
                statement = statement.where(column.is_(None) if value is None else column == value)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="invalid series key") from exc
    if period_from is not None:
        statement = statement.where(StructuredObservation.period >= period_from)
    if period_to is not None:
        statement = statement.where(StructuredObservation.period <= period_to)

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
        StructuredObservationRead(
            id=observation.id,
            observation_key=observation.observation_key,
            snapshot_id=snapshot.id,
            observation_version_id=version.id,
            version_no=version.version_no,
            country_iso3=observation.country_iso3,
            partner_iso3=observation.partner_iso3,
            indicator_code=observation.indicator_code,
            commodity_classification=observation.commodity_classification,
            source_dataset_code=observation.source_dataset_code,
            commodity_code=observation.commodity_code,
            trade_flow=observation.trade_flow,
            partner2_code=observation.partner2_code,
            customs_code=observation.customs_code,
            mot_code=observation.mot_code,
            frequency=observation.frequency,
            period=observation.period,
            metric_code=observation.metric_code,
            value=version.value,
            unit=version.unit,
            currency=version.currency,
            price_basis=version.price_basis,
            source_url=version.source_url,
            source_release_date=version.source_release_date,
            source_status=version.source_status,
            missing_reason=version.missing_reason,
            is_reported=version.is_reported,
            is_aggregate=version.is_aggregate,
            quality_flags=version.quality_flags,
        )
        for observation, version in rows
    ]
    return StructuredObservationPage(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        limit=limit,
        offset=offset,
        items=items,
    )


def require_snapshot_permission(db: Session, snapshot: StructuredSnapshot, action: str) -> None:
    latest = _latest_snapshot(db, snapshot.dataset_id)
    for candidate in (snapshot, latest):
        if candidate is not None:
            permissions = data_permissions(candidate.source_metadata)
            if not permissions["view"]["allowed"] or not permissions[action]["allowed"]:
                raise HTTPException(status_code=403, detail=f"{action}: {permissions[action]['reason']}")


@router.get("/{dataset_id}/export")
def export_structured_observations(
    dataset_id: int,
    db: DbSession,
    snapshot_id: Annotated[int, Query(ge=1)],
    country_iso3: Annotated[str, Query(pattern=r"^[A-Z]{3}$")],
    format: Literal["csv", "citation"] = "csv",
    indicator_code: CodeFilter = None,
    metric_code: CodeFilter = None,
    partner_iso3: Iso3 = None,
    commodity_code: CodeFilter = None,
    trade_flow: Literal["M", "X"] | None = None,
    commodity_classification: CodeFilter = None,
    partner2_code: CodeFilter = None,
    customs_code: CodeFilter = None,
    mot_code: CodeFilter = None,
    source_dataset_code: CodeFilter = None,
    series_key: Annotated[str | None, Query(max_length=2000)] = None,
):
    dataset = _get_dataset_or_404(db, dataset_id)
    snapshot = db.get(StructuredSnapshot, snapshot_id)
    if snapshot is None or snapshot.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail="structured snapshot not found for dataset")
    require_snapshot_permission(db, snapshot, "download" if format == "csv" else "citation")
    page = list_structured_observations(
        dataset_id,
        db,
        snapshot_id=snapshot_id,
        country_iso3=country_iso3,
        indicator_code=indicator_code,
        metric_code=metric_code,
        partner_iso3=partner_iso3,
        commodity_code=commodity_code,
        trade_flow=trade_flow,
        commodity_classification=commodity_classification,
        partner2_code=partner2_code,
        customs_code=customs_code,
        mot_code=mot_code,
        source_dataset_code=source_dataset_code,
        series_key=series_key,
        limit=1000,
        offset=0,
    )
    if format == "citation":
        return {
            "citation": (
                f"{dataset.name}. {country_iso3}; "
                f"{indicator_code or commodity_code or metric_code or dataset.dataset_key}; "
                f"snapshot {snapshot.id}; version {snapshot.provider_version}; "
                f"retrieved {snapshot.retrieved_at.isoformat()}."
            ),
            "license": snapshot.source_metadata.get("official_dataset", {}).get("license_name")
            or snapshot.source_metadata.get("license"),
            "source_urls": sorted({item.source_url for item in page.items}),
        }
    if len(page.items) == 1000:
        raise HTTPException(status_code=422, detail="请缩小指标范围后下载，避免截断记录")
    official = snapshot.source_metadata.get("official_dataset", {})
    rows = [
        {
            **item.model_dump(mode="json"),
            "dataset_name": dataset.name,
            "license": snapshot.source_metadata.get("license") or official.get("license_name"),
            "license_url": snapshot.source_metadata.get("license_url") or official.get("license_url"),
        }
        for item in page.items
    ]
    output = io.StringIO()
    fields = list(rows[0]) if rows else ["country_iso3", "period", "value"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "@")) else value)
                for key, value in row.items()
            }
        )
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{dataset.dataset_key}-{country_iso3}-{snapshot.id}.csv"'
            )
        },
    )
