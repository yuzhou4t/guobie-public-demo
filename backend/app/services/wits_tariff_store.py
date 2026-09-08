from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.wits_tariff import WITS_AVAILABILITY_URL, WitsTariffPanel
from app.models import (
    CollectionRun,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)

WITS_TARIFF_DATASET_KEY = "wits-trains-mfn-hs6"
WITS_TARIFF_SCOPE_ID = "wits-mfn-hs6-shadow-v1"
WITS_TARIFF_INDICATOR = "WITS.TRAINS.MFN.SimpleAverage"
WITS_TARIFF_METRIC = "mfn_simple_average_percent"
WITS_TARIFF_SOURCE_DATASET = "DF_WITS_Tariff_TRAINS"


class WitsTariffStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WitsTariffQueryEvidence:
    kind: str
    request_url: str
    resource_sha256: str
    fetched_at: datetime
    source_file_name: str | None = None


@dataclass(frozen=True, slots=True)
class WitsTariffStoreResult:
    dataset_id: int
    snapshot_id: int
    query_signature: str
    snapshot_hash: str
    snapshot_created: bool
    observations_created: int
    versions_created: int
    versions_unchanged: int


def persist_wits_tariff(
    session: Session,
    *,
    channel_id: int,
    collection_run_id: int,
    retrieved_at: datetime,
    panel: WitsTariffPanel,
    query_evidence: tuple[WitsTariffQueryEvidence, ...],
) -> WitsTariffStoreResult:
    channel = session.get(SourceChannel, channel_id)
    run = session.get(CollectionRun, collection_run_id)
    if channel is None or run is None or run.channel_id != channel_id or run.status != "running":
        raise WitsTariffStoreError("invalid_owner", "WITS collection owner is invalid")
    if len(panel.cells) != 84:
        raise WitsTariffStoreError("incomplete_panel", "WITS panel must contain exactly 84 cells")
    if panel.returned_count != panel.valued_count + panel.source_null_count:
        raise WitsTariffStoreError("invalid_counts", "WITS returned count is inconsistent")
    if panel.returned_count + panel.not_returned_count != 84:
        raise WitsTariffStoreError("invalid_counts", "WITS expected count is inconsistent")

    _validate_query_evidence(panel, query_evidence)
    query_signature = _sha256(
        {
            "scope_id": WITS_TARIFF_SCOPE_ID,
            "queries": [_query_signature_entry(item) for item in query_evidence],
        }
    )
    normalized = [_normalized_cell(cell) for cell in panel.cells]
    snapshot_hash = _sha256(normalized)

    dataset = session.scalar(
        select(StructuredDataset).where(StructuredDataset.dataset_key == WITS_TARIFF_DATASET_KEY)
    )
    if dataset is None:
        dataset = StructuredDataset(
            channel_id=channel_id,
            dataset_key=WITS_TARIFF_DATASET_KEY,
            name="WITS UNCTAD TRAINS MFN simple-average tariffs for three HS6 products",
            scope_id=WITS_TARIFF_SCOPE_ID,
            frequency="annual",
        )
        session.add(dataset)
        session.flush()
    elif dataset.channel_id != channel_id or dataset.scope_id != WITS_TARIFF_SCOPE_ID:
        raise WitsTariffStoreError(
            "dataset_mismatch",
            "existing WITS dataset has a different owner or scope",
        )

    latest_snapshot = session.scalar(
        select(StructuredSnapshot)
        .where(StructuredSnapshot.dataset_id == dataset.id)
        .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
        .limit(1)
    )
    _validate_no_manual_regression(latest_snapshot, panel, query_evidence)
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
        if len(members) != 84:
            raise WitsTariffStoreError(
                "stored_panel_incomplete",
                "stored WITS snapshot does not have 84 exact members",
            )
        for member in members:
            observation = by_id.get(member.observation_id)
            if observation is None:
                raise WitsTariffStoreError(
                    "stored_panel_incomplete",
                    "stored WITS snapshot references an unknown observation",
                )
            observation.last_seen_at = retrieved_at
        return WitsTariffStoreResult(
            dataset_id=dataset.id,
            snapshot_id=latest_snapshot.id,
            query_signature=query_signature,
            snapshot_hash=snapshot_hash,
            snapshot_created=False,
            observations_created=0,
            versions_created=0,
            versions_unchanged=84,
        )

    snapshot = StructuredSnapshot(
        dataset_id=dataset.id,
        collection_run_id=collection_run_id,
        query_manifest={
            "scope_id": WITS_TARIFF_SCOPE_ID,
            "queries": [
                {
                    "kind": item.kind,
                    "request_url": item.request_url,
                    "resource_sha256": item.resource_sha256,
                    "fetched_at": item.fetched_at.isoformat(),
                    "source_file_name": item.source_file_name,
                }
                for item in query_evidence
            ],
            "raw_content_stored": False,
        },
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        provider_version=(
            "WITS API 1.4.1 / UNCTAD TRAINS + reviewed manual ZIP exports"
            if any(item.kind == "manual_tariff_zip" for item in query_evidence)
            else "WITS API 1.4.1 / UNCTAD TRAINS"
        ),
        source_metadata={
            "measure": "SimpleAverage",
            "tariff_type": "MFN",
            "data_type": "Reported",
            "partner_code": "000",
            "panel_status": "incomplete",
            "sparse_panel_accepted": True,
            "available_schedule_count": panel.available_schedule_count,
            "candidate_cell_count": panel.candidate_cell_count,
            "manual_schedule_count": sum(item.kind == "manual_tariff_zip" for item in query_evidence),
        },
        expected_count=84,
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
        identity = {
            "dataset_key": WITS_TARIFF_DATASET_KEY,
            "country_iso3": cell.country_iso3,
            "partner_iso3": "WLD",
            "indicator_code": WITS_TARIFF_INDICATOR,
            "commodity_classification": cell.commodity_classification,
            "commodity_code": cell.commodity_code,
            "period": cell.period,
            "metric_code": WITS_TARIFF_METRIC,
        }
        observation_key = _sha256(identity)
        observation = by_key.get(observation_key)
        if observation is None:
            observation = StructuredObservation(
                dataset_id=dataset.id,
                observation_key=observation_key,
                country_iso3=cell.country_iso3,
                partner_iso3="WLD",
                indicator_code=WITS_TARIFF_INDICATOR,
                commodity_classification=cell.commodity_classification,
                source_dataset_code=WITS_TARIFF_SOURCE_DATASET,
                commodity_code=cell.commodity_code,
                trade_flow=None,
                partner2_code=None,
                customs_code=None,
                mot_code=None,
                frequency="annual",
                period=cell.period,
                metric_code=WITS_TARIFF_METRIC,
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
            if not _observation_matches(observation, cell):
                raise WitsTariffStoreError(
                    "identity_mismatch",
                    "WITS observation identity changed",
                )
            observation.last_seen_at = retrieved_at

        quality_flags = _quality_flags(cell)
        version_payload = {
            **identity,
            **normalized_cell,
            "unit": "percent",
            "source_status": _source_status(cell.missing_reason),
            "quality_flags": quality_flags,
        }
        observation_hash = _sha256(version_payload)
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
                unit="percent",
                currency=None,
                price_basis=None,
                source_url=cell.source_url,
                source_release_date=None,
                source_status=_source_status(cell.missing_reason),
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
    return WitsTariffStoreResult(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        snapshot_created=True,
        observations_created=observations_created,
        versions_created=versions_created,
        versions_unchanged=versions_unchanged,
    )


def _validate_query_evidence(
    panel: WitsTariffPanel,
    query_evidence: tuple[WitsTariffQueryEvidence, ...],
) -> None:
    expected_urls = {WITS_AVAILABILITY_URL}
    expected_urls.update(cell.source_url for cell in panel.cells if cell.source_url != WITS_AVAILABILITY_URL)
    actual_urls = {item.request_url for item in query_evidence}
    if actual_urls != expected_urls or len(actual_urls) != len(query_evidence):
        raise WitsTariffStoreError(
            "query_evidence_mismatch",
            "WITS query evidence does not cover the exact sparse-panel requests",
        )
    for item in query_evidence:
        allowed_kinds = (
            {"availability"} if item.request_url == WITS_AVAILABILITY_URL else {"tariff", "manual_tariff_zip"}
        )
        manual_file_invalid = item.kind == "manual_tariff_zip" and not item.source_file_name
        automated_file_invalid = item.kind != "manual_tariff_zip" and item.source_file_name is not None
        if (
            item.kind not in allowed_kinds
            or len(item.resource_sha256) != 64
            or manual_file_invalid
            or automated_file_invalid
        ):
            raise WitsTariffStoreError(
                "query_evidence_mismatch",
                "WITS query evidence has an invalid kind or digest",
            )


def _validate_no_manual_regression(
    latest_snapshot: StructuredSnapshot | None,
    panel: WitsTariffPanel,
    query_evidence: tuple[WitsTariffQueryEvidence, ...],
) -> None:
    if latest_snapshot is None or any(item.kind == "manual_tariff_zip" for item in query_evidence):
        return
    previous_queries = latest_snapshot.query_manifest.get("queries")
    if not isinstance(previous_queries, list):
        return
    manual_urls = {
        str(item["request_url"])
        for item in previous_queries
        if isinstance(item, dict) and item.get("kind") == "manual_tariff_zip" and item.get("request_url")
    }
    for url in manual_urls:
        reported = [cell for cell in panel.cells if cell.source_url == url and cell.value is not None]
        if len(reported) != 3:
            raise WitsTariffStoreError(
                "manual_supplement_regression",
                "automated WITS collection cannot replace reviewed manual values with missing cells",
            )


def _normalized_cell(cell: Any) -> dict[str, Any]:
    return {
        "country_iso3": cell.country_iso3,
        "source_reporter_code": cell.source_reporter_code,
        "source_iso3": cell.source_iso3,
        "period": cell.period,
        "commodity_code": cell.commodity_code,
        "commodity_classification": cell.commodity_classification,
        "value": str(cell.value) if cell.value is not None else None,
        "missing_reason": cell.missing_reason,
        "source_url": cell.source_url,
        "total_lines": cell.total_lines,
        "preferential_lines": cell.preferential_lines,
        "mfn_lines": cell.mfn_lines,
        "non_ad_valorem_lines": cell.non_ad_valorem_lines,
        "sum_of_rates": str(cell.sum_of_rates) if cell.sum_of_rates is not None else None,
        "min_rate": str(cell.min_rate) if cell.min_rate is not None else None,
        "max_rate": str(cell.max_rate) if cell.max_rate is not None else None,
    }


def _quality_flags(cell: Any) -> dict[str, Any]:
    flags = {
        "panel_status": "incomplete",
        "panel_completeness": "sparse",
        "source_reporter_code": cell.source_reporter_code,
        "source_iso3": cell.source_iso3,
        "source_partner_code": "000",
        "data_type": "Reported",
        "tariff_type": "MFN",
        "availability_status": (
            "schedule_unavailable"
            if cell.missing_reason == "tariff_schedule_unavailable"
            else "schedule_available"
        ),
    }
    if cell.total_lines is not None:
        flags.update(
            {
                "total_lines": cell.total_lines,
                "preferential_lines": cell.preferential_lines,
                "mfn_lines": cell.mfn_lines,
                "non_ad_valorem_lines": cell.non_ad_valorem_lines,
                "sum_of_rates": str(cell.sum_of_rates),
                "min_rate": str(cell.min_rate),
                "max_rate": str(cell.max_rate),
            }
        )
    return flags


def _observation_matches(observation: StructuredObservation, cell: Any) -> bool:
    return (
        observation.country_iso3 == cell.country_iso3
        and observation.partner_iso3 == "WLD"
        and observation.indicator_code == WITS_TARIFF_INDICATOR
        and observation.commodity_classification == cell.commodity_classification
        and observation.source_dataset_code == WITS_TARIFF_SOURCE_DATASET
        and observation.commodity_code == cell.commodity_code
        and observation.trade_flow is None
        and observation.period == cell.period
        and observation.metric_code == WITS_TARIFF_METRIC
    )


def _source_status(missing_reason: str | None) -> str:
    return "reported" if missing_reason in {None, "source_null"} else "not_returned"


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _query_signature_entry(item: WitsTariffQueryEvidence) -> dict[str, str]:
    entry = {"kind": item.kind, "request_url": item.request_url}
    if item.source_file_name is not None:
        entry["source_file_name"] = item.source_file_name
    return entry
