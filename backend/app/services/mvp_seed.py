from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    CollectionRun,
    Source,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)

SEED_VERSION = "mvp-reader-rescue-v2"


def export_cod_structured_seed(session: Session, output_path: Path) -> dict[str, Any]:
    rows = list(
        session.execute(
            select(
                StructuredObservation,
                StructuredDataset,
                SourceChannel,
                Source,
                StructuredObservationVersion,
            )
            .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
            .join(SourceChannel, SourceChannel.id == StructuredDataset.channel_id)
            .join(Source, Source.id == SourceChannel.source_id)
            .join(
                StructuredObservationVersion,
                and_(
                    StructuredObservationVersion.observation_id == StructuredObservation.id,
                    StructuredObservationVersion.version_no == StructuredObservation.latest_version_no,
                ),
            )
            .where(StructuredObservation.country_iso3 == "COD")
            .order_by(StructuredDataset.dataset_key, StructuredObservation.observation_key)
        )
    )
    datasets: dict[str, dict[str, Any]] = {}
    for observation, dataset, channel, source, version in rows:
        item = datasets.setdefault(
            dataset.dataset_key,
            {
                "dataset": {
                    "dataset_key": dataset.dataset_key,
                    "name": dataset.name,
                    "scope_id": dataset.scope_id,
                    "frequency": dataset.frequency,
                },
                "source": {
                    "name": source.name,
                    "organization_name": source.organization_name,
                    "source_type": source.source_type,
                    "country_or_region": source.country_or_region,
                    "primary_language": source.primary_language,
                    "homepage_url": source.homepage_url,
                    "authority_level": source.authority_level,
                },
                "channel": {
                    "name": channel.name,
                    "entry_url": channel.entry_url,
                    "collector_type": channel.collector_type,
                    "adapter_version": channel.adapter_version,
                    "link_role": channel.link_role,
                },
                "observations": [],
            },
        )
        item["observations"].append(
            {
                "observation_key": observation.observation_key,
                "country_iso3": observation.country_iso3,
                "partner_iso3": observation.partner_iso3,
                "indicator_code": observation.indicator_code,
                "commodity_classification": observation.commodity_classification,
                "source_dataset_code": observation.source_dataset_code,
                "commodity_code": observation.commodity_code,
                "trade_flow": observation.trade_flow,
                "partner2_code": observation.partner2_code,
                "customs_code": observation.customs_code,
                "mot_code": observation.mot_code,
                "frequency": observation.frequency,
                "period": observation.period,
                "metric_code": observation.metric_code,
                "first_seen_at": _iso(observation.first_seen_at),
                "last_seen_at": _iso(observation.last_seen_at),
                "version": {
                    "observation_hash": version.observation_hash,
                    "value": str(version.value) if version.value is not None else None,
                    "unit": version.unit,
                    "currency": version.currency,
                    "price_basis": version.price_basis,
                    "source_url": version.source_url,
                    "source_release_date": (
                        version.source_release_date.isoformat() if version.source_release_date else None
                    ),
                    "source_status": version.source_status,
                    "missing_reason": version.missing_reason,
                    "is_reported": version.is_reported,
                    "is_aggregate": version.is_aggregate,
                    "quality_flags": version.quality_flags,
                },
            }
        )
    payload = {
        "schema_version": "1.0",
        "seed_version": SEED_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "country_iso3": "COD",
        "policy": {
            "contains_raw_assets": False,
            "contains_document_body": False,
            "contains_credentials": False,
            "latest_structured_versions_only": True,
        },
        "datasets": list(datasets.values()),
    }
    payload["summary"] = {
        "dataset_count": len(datasets),
        "observation_count": sum(len(item["observations"]) for item in datasets.values()),
        "valued_observation_count": sum(
            row["version"]["value"] is not None for item in datasets.values() for row in item["observations"]
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload["summary"]


def import_cod_structured_seed(session: Session, seed_path: Path) -> dict[str, int]:
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    if payload.get("seed_version") != SEED_VERSION or payload.get("country_iso3") != "COD":
        raise ValueError("unsupported MVP seed package")
    counters = {"datasets_created": 0, "observations_created": 0, "observations_existing": 0}
    for item in payload.get("datasets") or []:
        source_data = item["source"]
        source = session.scalar(
            select(Source).where(
                Source.name == source_data["name"],
                Source.organization_name == source_data["organization_name"],
            )
        )
        if source is None:
            source = Source(**source_data, status="active")
            session.add(source)
            session.flush()
        channel_data = item["channel"]
        channel = session.scalar(
            select(SourceChannel).where(
                SourceChannel.source_id == source.id,
                SourceChannel.entry_url == channel_data["entry_url"],
            )
        )
        if channel is None:
            channel = SourceChannel(
                source_id=source.id,
                **channel_data,
                collector_config={"seed_version": SEED_VERSION},
                status="shadow",
            )
            session.add(channel)
            session.flush()
        dataset_data = item["dataset"]
        dataset = session.scalar(
            select(StructuredDataset).where(StructuredDataset.dataset_key == dataset_data["dataset_key"])
        )
        if dataset is None:
            dataset = StructuredDataset(channel_id=channel.id, **dataset_data)
            session.add(dataset)
            session.flush()
            counters["datasets_created"] += 1
        marker = f"{SEED_VERSION}:{dataset.dataset_key}"
        run = next(
            (
                row
                for row in session.scalars(
                    select(CollectionRun).where(CollectionRun.channel_id == channel.id)
                )
                if (row.report or {}).get("mvp_seed_marker") == marker
            ),
            None,
        )
        rows = item.get("observations") or []
        if run is None:
            observed_times = [_datetime(row["last_seen_at"]) for row in rows]
            finished_at = max(observed_times, default=datetime.now(UTC))
            run = CollectionRun(
                channel_id=channel.id,
                trigger_kind="manual",
                status="succeeded",
                queued_at=finished_at,
                started_at=finished_at,
                finished_at=finished_at,
                items_discovered=len(rows),
                items_persisted=len(rows),
                report={"mvp_seed_marker": marker, "offline_seed": True},
            )
            session.add(run)
            session.flush()
        snapshot = session.scalar(
            select(StructuredSnapshot).where(StructuredSnapshot.collection_run_id == run.id)
        )
        if snapshot is None:
            valued_count = sum(row["version"]["value"] is not None for row in rows)
            digest = hashlib.sha256(marker.encode()).hexdigest()
            snapshot = StructuredSnapshot(
                dataset_id=dataset.id,
                collection_run_id=run.id,
                query_manifest={"seed_version": SEED_VERSION, "country_iso3": "COD"},
                query_signature=digest,
                snapshot_hash=digest,
                provider_version="offline-seed-v1",
                source_metadata={"no_raw_response": True},
                expected_count=len(rows),
                returned_count=len(rows),
                valued_count=valued_count,
                source_null_count=len(rows) - valued_count,
                not_returned_count=0,
                retrieved_at=max(
                    (_datetime(row["last_seen_at"]) for row in rows),
                    default=datetime.now(UTC),
                ),
            )
            session.add(snapshot)
            session.flush()
        for row in rows:
            version_data = row["version"]
            observation = session.scalar(
                select(StructuredObservation).where(
                    StructuredObservation.dataset_id == dataset.id,
                    StructuredObservation.observation_key == row["observation_key"],
                )
            )
            if observation is not None:
                version = session.scalar(
                    select(StructuredObservationVersion)
                    .where(
                        StructuredObservationVersion.observation_id == observation.id,
                        StructuredObservationVersion.observation_hash == version_data["observation_hash"],
                    )
                    .order_by(StructuredObservationVersion.version_no.desc())
                    .limit(1)
                )
                if version is None:
                    version = session.scalar(
                        select(StructuredObservationVersion).where(
                            StructuredObservationVersion.observation_id == observation.id,
                            StructuredObservationVersion.version_no == observation.latest_version_no,
                        )
                    )
                if version is None:
                    raise ValueError(f"existing observation has no version: {observation.observation_key}")
                membership = session.get(
                    StructuredSnapshotObservation,
                    (snapshot.id, observation.id),
                )
                if membership is None:
                    session.add(
                        StructuredSnapshotObservation(
                            snapshot_id=snapshot.id,
                            observation_id=observation.id,
                            observation_version_id=version.id,
                        )
                    )
                else:
                    membership.observation_version_id = version.id
                counters["observations_existing"] += 1
                continue
            observation_data = {key: value for key, value in row.items() if key != "version"}
            observation_data["first_seen_at"] = _datetime(observation_data["first_seen_at"])
            observation_data["last_seen_at"] = _datetime(observation_data["last_seen_at"])
            observation = StructuredObservation(
                dataset_id=dataset.id,
                **observation_data,
                latest_version_no=1,
            )
            session.add(observation)
            session.flush()
            version = StructuredObservationVersion(
                observation_id=observation.id,
                snapshot_id=snapshot.id,
                version_no=1,
                observation_hash=version_data["observation_hash"],
                value=Decimal(version_data["value"]) if version_data["value"] is not None else None,
                unit=version_data["unit"],
                currency=version_data["currency"],
                price_basis=version_data["price_basis"],
                source_url=version_data["source_url"],
                source_release_date=(
                    date.fromisoformat(version_data["source_release_date"])
                    if version_data["source_release_date"]
                    else None
                ),
                source_status=version_data["source_status"],
                missing_reason=version_data["missing_reason"],
                is_reported=version_data["is_reported"],
                is_aggregate=version_data["is_aggregate"],
                quality_flags=version_data["quality_flags"],
            )
            session.add(version)
            session.flush()
            session.add(
                StructuredSnapshotObservation(
                    snapshot_id=snapshot.id,
                    observation_id=observation.id,
                    observation_version_id=version.id,
                )
            )
            counters["observations_created"] += 1
    session.commit()
    return counters


def _iso(value: datetime) -> str:
    return value.isoformat()


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return result if result.tzinfo else result.replace(tzinfo=UTC)
