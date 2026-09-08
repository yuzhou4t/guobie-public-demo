from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.oecd_oda import OecdOdaParseResult
from app.models import (
    CollectionRun,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)

OECD_ODA_DATASET_KEY = "oecd-dac2a-oda-disbursements"
OECD_ODA_SCOPE_ID = "oecd-oda-shadow-v1"
OECD_ODA_INDICATOR = "DAC2A.206.ALLD"
OECD_ODA_METRIC = "oda_disbursements_current_usd_millions"
OECD_ODA_URL = (
    "https://sdmx.oecd.org/public/rest/data/OECD.DCD.FSD,DSD_DAC2@DF_DAC2A,/"
    "ALLD.COD+ZWE+ZMB+ZAF.206.USD.V?startPeriod=2015&endPeriod=2024&"
    "dimensionAtObservation=AllDimensions&format=jsondata"
)


class OecdOdaStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class OecdOdaStoreResult:
    dataset_id: int
    snapshot_id: int
    query_signature: str
    snapshot_hash: str
    snapshot_created: bool
    observations_created: int
    versions_created: int
    versions_unchanged: int


def persist_oecd_oda(
    session: Session,
    *,
    channel_id: int,
    collection_run_id: int,
    retrieved_at: datetime,
    query_url: str,
    resource_sha256: str,
    parsed: OecdOdaParseResult,
) -> OecdOdaStoreResult:
    channel = session.get(SourceChannel, channel_id)
    run = session.get(CollectionRun, collection_run_id)
    if channel is None or run is None or run.channel_id != channel_id or run.status != "running":
        raise OecdOdaStoreError("invalid_owner", "OECD collection owner is invalid")
    if len(parsed.observations) != 40:
        raise OecdOdaStoreError("incomplete_grid", "OECD collection must contain exactly 40 values")

    query_signature = _sha256(query_url)
    normalized = [
        {
            "country_iso3": item.country_iso3,
            "period": item.period,
            "value": str(item.value),
            "unit": item.unit,
            "currency": item.currency,
            "price_basis": item.price_basis,
            "source_status": item.source_status,
        }
        for item in parsed.observations
    ]
    snapshot_hash = _sha256(normalized)
    dataset = session.scalar(
        select(StructuredDataset).where(StructuredDataset.dataset_key == OECD_ODA_DATASET_KEY)
    )
    if dataset is None:
        dataset = StructuredDataset(
            channel_id=channel_id,
            dataset_key=OECD_ODA_DATASET_KEY,
            name="OECD DAC2A ODA disbursements to four focus countries",
            scope_id=OECD_ODA_SCOPE_ID,
            frequency="annual",
        )
        session.add(dataset)
        session.flush()
    elif dataset.channel_id != channel_id or dataset.scope_id != OECD_ODA_SCOPE_ID:
        raise OecdOdaStoreError("dataset_mismatch", "existing OECD dataset has a different owner or scope")

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
        if len(observations) != 40 or len(latest_versions) != 40:
            raise OecdOdaStoreError(
                "stored_grid_incomplete",
                "stored OECD snapshot does not have 40 observation versions",
            )
        for observation in observations:
            observation.last_seen_at = retrieved_at
        return OecdOdaStoreResult(
            dataset_id=dataset.id,
            snapshot_id=latest_snapshot.id,
            query_signature=query_signature,
            snapshot_hash=snapshot_hash,
            snapshot_created=False,
            observations_created=0,
            versions_created=0,
            versions_unchanged=40,
        )

    snapshot = StructuredSnapshot(
        dataset_id=dataset.id,
        collection_run_id=collection_run_id,
        query_manifest={
            "scope_id": OECD_ODA_SCOPE_ID,
            "query_url": query_url,
            "resource_sha256": resource_sha256,
            "raw_content_stored": False,
        },
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        provider_version="OECD.DCD.FSD,DSD_DAC2@DF_DAC2A",
        source_metadata={
            "donor": "ALLD",
            "measure": "206",
            "unit_measure": "USD",
            "unit_multiplier": "6",
            "flow_type": "D",
            "price_base": "V",
        },
        expected_count=40,
        returned_count=40,
        valued_count=40,
        source_null_count=0,
        not_returned_count=0,
        retrieved_at=retrieved_at,
    )
    session.add(snapshot)
    session.flush()

    observations_created = 0
    versions_created = 0
    versions_unchanged = 0
    members: list[tuple[StructuredObservation, StructuredObservationVersion]] = []
    for item in parsed.observations:
        identity = {
            "dataset_key": OECD_ODA_DATASET_KEY,
            "country_iso3": item.country_iso3,
            "period": item.period,
            "indicator_code": OECD_ODA_INDICATOR,
            "metric_code": OECD_ODA_METRIC,
        }
        observation_key = _sha256(identity)
        observation = by_key.get(observation_key)
        if observation is None:
            observation = StructuredObservation(
                dataset_id=dataset.id,
                observation_key=observation_key,
                country_iso3=item.country_iso3,
                partner_iso3=None,
                indicator_code=OECD_ODA_INDICATOR,
                commodity_classification=None,
                source_dataset_code=None,
                commodity_code=None,
                trade_flow=None,
                partner2_code=None,
                customs_code=None,
                mot_code=None,
                frequency="annual",
                period=item.period,
                metric_code=OECD_ODA_METRIC,
                first_seen_at=retrieved_at,
                last_seen_at=retrieved_at,
                latest_version_no=0,
            )
            session.add(observation)
            session.flush()
            by_key[observation_key] = observation
            observations_created += 1
        else:
            if (
                observation.country_iso3 != item.country_iso3
                or observation.period != item.period
                or observation.indicator_code != OECD_ODA_INDICATOR
                or observation.metric_code != OECD_ODA_METRIC
            ):
                raise OecdOdaStoreError("identity_mismatch", "OECD observation identity changed")
            observation.last_seen_at = retrieved_at

        observation_hash = _sha256({**identity, **normalized[len(members)]})
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
                source_url=query_url,
                source_release_date=None,
                source_status=item.source_status,
                missing_reason=None,
                is_reported=None,
                is_aggregate=None,
                quality_flags={"flow_type": "disbursements", "unit_multiplier": 6},
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
    return OecdOdaStoreResult(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        snapshot_created=True,
        observations_created=observations_created,
        versions_created=versions_created,
        versions_unchanged=versions_unchanged,
    )


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
