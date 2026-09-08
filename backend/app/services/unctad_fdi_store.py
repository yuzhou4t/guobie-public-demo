from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.unctad_fdi import UnctadFdiCell, UnctadFdiPanel
from app.models import (
    CollectionRun,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)

UNCTAD_FDI_DATASET_KEY = "unctad-fdi-flows-stock"
UNCTAD_FDI_SCOPE_ID = "unctad-fdi-shadow-v1"
UNCTAD_FDI_SOURCE_DATASET = "US.FdiFlowsStock"


class UnctadFdiStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class UnctadFdiStoreResult:
    dataset_id: int
    snapshot_id: int
    query_signature: str
    snapshot_hash: str
    snapshot_created: bool
    observations_created: int
    versions_created: int
    versions_unchanged: int


def persist_unctad_fdi(
    session: Session,
    *,
    channel_id: int,
    collection_run_id: int,
    retrieved_at: datetime,
    query_url: str,
    resource_sha256: str,
    panel: UnctadFdiPanel,
) -> UnctadFdiStoreResult:
    channel = session.get(SourceChannel, channel_id)
    run = session.get(CollectionRun, collection_run_id)
    if channel is None or run is None or run.channel_id != channel_id or run.status != "running":
        raise UnctadFdiStoreError("invalid_owner", "UNCTAD FDI collection owner is invalid")
    if len(panel.cells) != 160:
        raise UnctadFdiStoreError("incomplete_panel", "UNCTAD FDI panel must contain 160 cells")
    if panel.returned_count != panel.valued_count + panel.source_null_count:
        raise UnctadFdiStoreError("invalid_counts", "UNCTAD FDI returned count is inconsistent")
    if panel.returned_count + panel.not_returned_count != 160:
        raise UnctadFdiStoreError("invalid_counts", "UNCTAD FDI expected count is inconsistent")

    query_signature = _sha256({"scope_id": UNCTAD_FDI_SCOPE_ID, "query_url": query_url})
    normalized = [_normalized_cell(cell) for cell in panel.cells]
    snapshot_hash = _sha256(normalized)
    dataset = session.scalar(
        select(StructuredDataset).where(StructuredDataset.dataset_key == UNCTAD_FDI_DATASET_KEY)
    )
    if dataset is None:
        dataset = StructuredDataset(
            channel_id=channel_id,
            dataset_key=UNCTAD_FDI_DATASET_KEY,
            name="UNCTAD FDI inward/outward flows and stocks for four focus countries",
            scope_id=UNCTAD_FDI_SCOPE_ID,
            frequency="annual",
        )
        session.add(dataset)
        session.flush()
    elif dataset.channel_id != channel_id or dataset.scope_id != UNCTAD_FDI_SCOPE_ID:
        raise UnctadFdiStoreError(
            "dataset_mismatch",
            "existing UNCTAD FDI dataset has a different owner or scope",
        )

    latest_snapshot = session.scalar(
        select(StructuredSnapshot)
        .where(StructuredSnapshot.dataset_id == dataset.id)
        .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
        .limit(1)
    )
    observations = session.scalars(
        select(StructuredObservation).where(StructuredObservation.dataset_id == dataset.id)
    ).all()
    by_key = {item.observation_key: item for item in observations}
    by_id = {item.id: item for item in observations}
    latest_versions: dict[int, StructuredObservationVersion] = {}
    if observations:
        versions = session.scalars(
            select(StructuredObservationVersion)
            .where(StructuredObservationVersion.observation_id.in_(item.id for item in observations))
            .order_by(
                StructuredObservationVersion.observation_id,
                StructuredObservationVersion.version_no.desc(),
            )
        ).all()
        for version in versions:
            latest_versions.setdefault(version.observation_id, version)

    if (
        latest_snapshot is not None
        and latest_snapshot.query_signature == query_signature
        and latest_snapshot.snapshot_hash == snapshot_hash
    ):
        members = session.scalars(
            select(StructuredSnapshotObservation).where(
                StructuredSnapshotObservation.snapshot_id == latest_snapshot.id
            )
        ).all()
        if len(members) != 160:
            raise UnctadFdiStoreError(
                "stored_panel_incomplete",
                "stored UNCTAD FDI snapshot does not have 160 exact members",
            )
        for member in members:
            observation = by_id.get(member.observation_id)
            if observation is None:
                raise UnctadFdiStoreError(
                    "stored_panel_incomplete",
                    "stored UNCTAD FDI snapshot references an unknown observation",
                )
            observation.last_seen_at = retrieved_at
        return UnctadFdiStoreResult(
            dataset_id=dataset.id,
            snapshot_id=latest_snapshot.id,
            query_signature=query_signature,
            snapshot_hash=snapshot_hash,
            snapshot_created=False,
            observations_created=0,
            versions_created=0,
            versions_unchanged=160,
        )

    panel_status = "complete" if panel.not_returned_count == 0 else "incomplete"
    snapshot = StructuredSnapshot(
        dataset_id=dataset.id,
        collection_run_id=collection_run_id,
        query_manifest={
            "scope_id": UNCTAD_FDI_SCOPE_ID,
            "query_url": query_url,
            "resource_sha256": resource_sha256,
            "archive_member": "US_FdiFlowsStock.csv",
            "raw_content_stored": False,
        },
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        provider_version="US.FdiFlowsStock bulk file",
        source_metadata={
            "dataset_code": UNCTAD_FDI_SOURCE_DATASET,
            "measure": "US$ at current prices in millions",
            "flow_codes": {"08": "Flow", "09": "Stock"},
            "direction_codes": {"1": "Inward", "2": "Outward"},
            "panel_status": panel_status,
            "scope_origin": "agent_proposed_user_authorized_2026-07-15",
        },
        expected_count=160,
        returned_count=panel.returned_count,
        valued_count=panel.valued_count,
        source_null_count=panel.source_null_count,
        not_returned_count=panel.not_returned_count,
        retrieved_at=retrieved_at,
    )
    session.add(snapshot)
    session.flush()

    observations_created = 0
    versions_created = 0
    versions_unchanged = 0
    members: list[tuple[StructuredObservation, StructuredObservationVersion]] = []
    for cell, normalized_cell in zip(panel.cells, normalized, strict=True):
        indicator_code = _indicator_code(cell)
        identity = {
            "dataset_key": UNCTAD_FDI_DATASET_KEY,
            "country_iso3": cell.country_iso3,
            "period": cell.period,
            "indicator_code": indicator_code,
            "metric_code": cell.metric_code,
        }
        observation_key = _sha256(identity)
        observation = by_key.get(observation_key)
        if observation is None:
            observation = StructuredObservation(
                dataset_id=dataset.id,
                observation_key=observation_key,
                country_iso3=cell.country_iso3,
                partner_iso3=None,
                indicator_code=indicator_code,
                commodity_classification=None,
                source_dataset_code=UNCTAD_FDI_SOURCE_DATASET,
                commodity_code=None,
                trade_flow=None,
                partner2_code=None,
                customs_code=None,
                mot_code=None,
                frequency="annual",
                period=cell.period,
                metric_code=cell.metric_code,
                first_seen_at=retrieved_at,
                last_seen_at=retrieved_at,
                latest_version_no=0,
            )
            session.add(observation)
            session.flush()
            by_key[observation_key] = observation
            by_id[observation.id] = observation
            observations_created += 1
        else:
            if not _observation_matches(observation, cell, indicator_code):
                raise UnctadFdiStoreError(
                    "identity_mismatch",
                    "UNCTAD FDI observation identity changed",
                )
            observation.last_seen_at = retrieved_at

        quality_flags = _quality_flags(cell)
        observation_hash = _sha256(
            {
                **identity,
                **normalized_cell,
                "unit": "million_USD",
                "currency": "USD",
                "price_basis": "current_prices",
                "source_status": _source_status(cell),
                "quality_flags": quality_flags,
            }
        )
        latest = latest_versions.get(observation.id)
        if latest is not None and latest.observation_hash == observation_hash:
            version = latest
            versions_unchanged += 1
        else:
            version = StructuredObservationVersion(
                observation_id=observation.id,
                snapshot_id=snapshot.id,
                version_no=observation.latest_version_no + 1,
                observation_hash=observation_hash,
                value=cell.value,
                unit="million_USD" if cell.value is not None else None,
                currency="USD" if cell.value is not None else None,
                price_basis="current_prices" if cell.value is not None else None,
                source_url=cell.source_url,
                source_release_date=None,
                source_status=_source_status(cell),
                missing_reason=cell.missing_reason,
                is_reported=cell.missing_reason in {None, "source_null"},
                is_aggregate=False,
                quality_flags=quality_flags,
            )
            session.add(version)
            observation.latest_version_no += 1
            latest_versions[observation.id] = version
            versions_created += 1
        members.append((observation, version))

    session.flush()
    session.add_all(
        StructuredSnapshotObservation(
            snapshot_id=snapshot.id,
            observation_id=observation.id,
            observation_version_id=version.id,
        )
        for observation, version in members
    )
    session.flush()
    return UnctadFdiStoreResult(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        snapshot_created=True,
        observations_created=observations_created,
        versions_created=versions_created,
        versions_unchanged=versions_unchanged,
    )


def _indicator_code(cell: UnctadFdiCell) -> str:
    return f"UNCTAD.US.FdiFlowsStock.{cell.flow_code}.{cell.direction_code}"


def _normalized_cell(cell: UnctadFdiCell) -> dict[str, Any]:
    return {
        "country_iso3": cell.country_iso3,
        "source_economy_code": cell.source_economy_code,
        "economy_label": cell.economy_label,
        "period": cell.period,
        "flow_code": cell.flow_code,
        "flow_label": cell.flow_label,
        "direction_code": cell.direction_code,
        "direction_label": cell.direction_label,
        "metric_code": cell.metric_code,
        "value": str(cell.value) if cell.value is not None else None,
        "missing_reason": cell.missing_reason,
        "source_missing_value": cell.source_missing_value,
        "footnote": cell.footnote,
        "source_url": cell.source_url,
    }


def _quality_flags(cell: UnctadFdiCell) -> dict[str, Any]:
    flags: dict[str, Any] = {
        "source_economy_code": cell.source_economy_code,
        "source_economy_label": cell.economy_label,
        "flow_code": cell.flow_code,
        "flow_label": cell.flow_label,
        "direction_code": cell.direction_code,
        "direction_label": cell.direction_label,
    }
    if cell.footnote is not None:
        flags["footnote"] = cell.footnote
    if cell.source_missing_value is not None:
        flags["source_missing_value"] = cell.source_missing_value
    return flags


def _source_status(cell: UnctadFdiCell) -> str:
    if cell.value is not None:
        return "normal"
    return cell.missing_reason or "unknown"


def _observation_matches(
    observation: StructuredObservation,
    cell: UnctadFdiCell,
    indicator_code: str,
) -> bool:
    return (
        observation.country_iso3 == cell.country_iso3
        and observation.partner_iso3 is None
        and observation.indicator_code == indicator_code
        and observation.source_dataset_code == UNCTAD_FDI_SOURCE_DATASET
        and observation.period == cell.period
        and observation.metric_code == cell.metric_code
        and observation.commodity_classification is None
        and observation.commodity_code is None
        and observation.trade_flow is None
        and observation.partner2_code is None
        and observation.customs_code is None
        and observation.mot_code is None
    )


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
