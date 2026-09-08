from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.manual_structured import ManualStructuredParseResult
from app.models import (
    CollectionRun,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)

IMF_WEO_DATASET_KEY = "imf-weo-government-debt"
IMF_WEO_SCOPE_ID = "imf-weo-debt-shadow-v1"
NBS_ANNUAL_DATASET_KEY = "nbs-china-annual-industry-metals"
NBS_ANNUAL_SCOPE_ID = "nbs-annual-industry-metals-manual-v1"
MOFCOM_TRADE_DATASET_KEY = "mofcom-china-south-africa-goods-trade"
MOFCOM_TRADE_SCOPE_ID = "mofcom-south-africa-trade-monthly-manual-v1"
MOFCOM_ODI_DATASET_KEY = "mofcom-china-nonfinancial-odi"
MOFCOM_ODI_SCOPE_ID = "mofcom-nonfinancial-odi-monthly-manual-v1"


class ManualStructuredStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ManualStructuredDatasetSpec:
    dataset_key: str
    scope_id: str
    name: str
    expected_count: int
    frequency: str = "annual"
    import_mode: str = "manual_official_export"


@dataclass(frozen=True, slots=True)
class ManualStructuredStoreResult:
    dataset_id: int
    snapshot_id: int
    query_signature: str
    snapshot_hash: str
    snapshot_created: bool
    observations_created: int
    versions_created: int
    versions_unchanged: int


def persist_manual_structured(
    session: Session,
    *,
    channel_id: int,
    collection_run_id: int,
    retrieved_at: datetime,
    official_page_url: str,
    file_name: str,
    file_sha256: str,
    registration_file_name: str | None,
    registration_sha256: str | None,
    parser_version: str,
    spec: ManualStructuredDatasetSpec,
    parsed: ManualStructuredParseResult,
) -> ManualStructuredStoreResult:
    channel = session.get(SourceChannel, channel_id)
    run = session.get(CollectionRun, collection_run_id)
    if channel is None or run is None or run.channel_id != channel_id or run.status != "running":
        raise ManualStructuredStoreError("invalid_owner", "manual structured import owner is invalid")
    if (
        parsed.expected_count != spec.expected_count
        or len(parsed.observations) != spec.expected_count
        or parsed.returned_count != parsed.valued_count + parsed.source_null_count
        or parsed.expected_count != parsed.returned_count + parsed.not_returned_count
    ):
        raise ManualStructuredStoreError(
            "incomplete_scope",
            "manual structured import counts do not match the reviewed scope",
        )
    if len(file_sha256) != 64 or registration_sha256 is not None and len(registration_sha256) != 64:
        raise ManualStructuredStoreError("invalid_hash", "manual file evidence hash is invalid")

    query_manifest = {
        "scope_id": spec.scope_id,
        "official_page_url": official_page_url,
        "file_name": file_name,
        "file_sha256": file_sha256,
        "registration_file_name": registration_file_name,
        "registration_sha256": registration_sha256,
        "parser_version": parser_version,
        "raw_content_stored": False,
        "import_mode": spec.import_mode,
    }
    query_signature = _sha256(query_manifest)
    normalized = [
        {
            "country_iso3": item.country_iso3,
            "partner_iso3": item.partner_iso3,
            "period": item.period,
            "indicator_code": item.indicator_code,
            "metric_code": item.metric_code,
            "value": str(item.value) if item.value is not None else None,
            "unit": item.unit,
            "currency": item.currency,
            "price_basis": item.price_basis,
            "source_status": item.source_status,
            "missing_reason": item.missing_reason,
            "is_aggregate": item.is_aggregate,
            "quality_flags": item.quality_flags,
        }
        for item in parsed.observations
    ]
    snapshot_hash = _sha256(normalized)

    dataset = session.scalar(
        select(StructuredDataset).where(StructuredDataset.dataset_key == spec.dataset_key)
    )
    if dataset is None:
        dataset = StructuredDataset(
            channel_id=channel_id,
            dataset_key=spec.dataset_key,
            name=spec.name,
            scope_id=spec.scope_id,
            frequency=spec.frequency,
        )
        session.add(dataset)
        session.flush()
    elif (
        dataset.channel_id != channel_id
        or dataset.scope_id != spec.scope_id
        or dataset.frequency != spec.frequency
    ):
        raise ManualStructuredStoreError(
            "dataset_mismatch",
            "existing manual structured dataset has a different owner or scope",
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
    latest_versions = _latest_versions(session, observations)

    if (
        latest_snapshot is not None
        and latest_snapshot.query_signature == query_signature
        and latest_snapshot.snapshot_hash == snapshot_hash
    ):
        if len(observations) != spec.expected_count or len(latest_versions) != spec.expected_count:
            raise ManualStructuredStoreError(
                "stored_scope_incomplete",
                "stored manual snapshot does not contain the reviewed observation count",
            )
        for observation in observations:
            observation.last_seen_at = retrieved_at
        return ManualStructuredStoreResult(
            dataset_id=dataset.id,
            snapshot_id=latest_snapshot.id,
            query_signature=query_signature,
            snapshot_hash=snapshot_hash,
            snapshot_created=False,
            observations_created=0,
            versions_created=0,
            versions_unchanged=spec.expected_count,
        )

    snapshot = StructuredSnapshot(
        dataset_id=dataset.id,
        collection_run_id=collection_run_id,
        query_manifest=query_manifest,
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        provider_version=parsed.provider_version,
        source_metadata=parsed.source_metadata,
        expected_count=parsed.expected_count,
        returned_count=parsed.returned_count,
        valued_count=parsed.valued_count,
        source_null_count=parsed.source_null_count,
        not_returned_count=parsed.not_returned_count,
        retrieved_at=retrieved_at,
    )
    session.add(snapshot)
    session.flush()

    observations_created = 0
    versions_created = 0
    versions_unchanged = 0
    members: list[tuple[StructuredObservation, StructuredObservationVersion]] = []
    for item, normalized_item in zip(parsed.observations, normalized, strict=True):
        identity = {
            "dataset_key": spec.dataset_key,
            "country_iso3": item.country_iso3,
            "partner_iso3": item.partner_iso3,
            "period": item.period,
            "indicator_code": item.indicator_code,
            "metric_code": item.metric_code,
            "frequency": spec.frequency,
        }
        observation_key = _sha256(identity)
        observation = by_key.get(observation_key)
        if observation is None:
            observation = StructuredObservation(
                dataset_id=dataset.id,
                observation_key=observation_key,
                country_iso3=item.country_iso3,
                partner_iso3=item.partner_iso3,
                indicator_code=item.indicator_code,
                commodity_classification=None,
                source_dataset_code=parsed.provider_version,
                commodity_code=None,
                trade_flow=None,
                partner2_code=None,
                customs_code=None,
                mot_code=None,
                frequency=spec.frequency,
                period=item.period,
                metric_code=item.metric_code,
                first_seen_at=retrieved_at,
                last_seen_at=retrieved_at,
                latest_version_no=0,
            )
            session.add(observation)
            session.flush()
            by_key[observation_key] = observation
            observations_created += 1
        else:
            _assert_identity(observation, item, parsed.provider_version, spec.frequency)
            observation.last_seen_at = retrieved_at

        observation_hash = _sha256({**identity, **normalized_item})
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
                value=item.value,
                unit=item.unit,
                currency=item.currency,
                price_basis=item.price_basis,
                source_url=official_page_url,
                source_release_date=parsed.source_release_date,
                source_status=item.source_status,
                missing_reason=item.missing_reason,
                is_reported=None,
                is_aggregate=item.is_aggregate,
                quality_flags=item.quality_flags,
            )
            session.add(version)
            observation.latest_version_no += 1
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
    return ManualStructuredStoreResult(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        snapshot_created=True,
        observations_created=observations_created,
        versions_created=versions_created,
        versions_unchanged=versions_unchanged,
    )


def _latest_versions(
    session: Session,
    observations: list[StructuredObservation],
) -> dict[int, StructuredObservationVersion]:
    if not observations:
        return {}
    versions = session.scalars(
        select(StructuredObservationVersion)
        .where(StructuredObservationVersion.observation_id.in_(item.id for item in observations))
        .order_by(
            StructuredObservationVersion.observation_id,
            StructuredObservationVersion.version_no.desc(),
        )
    ).all()
    latest: dict[int, StructuredObservationVersion] = {}
    for version in versions:
        latest.setdefault(version.observation_id, version)
    return latest


def _assert_identity(
    observation: StructuredObservation,
    item: Any,
    provider_version: str,
    frequency: str = "annual",
) -> None:
    if (
        observation.country_iso3 != item.country_iso3
        or observation.partner_iso3 != item.partner_iso3
        or observation.period != item.period
        or observation.indicator_code != item.indicator_code
        or observation.metric_code != item.metric_code
        or observation.source_dataset_code != provider_version
        or observation.frequency != frequency
    ):
        raise ManualStructuredStoreError(
            "identity_mismatch",
            "manual structured observation identity changed",
        )


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
