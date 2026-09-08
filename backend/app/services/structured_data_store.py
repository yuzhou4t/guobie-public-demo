from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.cod_basics_scope import COD_BASICS_KEY, COD_RECENT_KEY
from app.collectors.world_bank import WorldBankObservation, WorldBankParseResult
from app.models import (
    CollectionRun,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)
from app.services.structured_scope import (
    StructuredScope,
    WorldBankIndicator,
    build_world_bank_request_url,
)

_FORBIDDEN_MANIFEST_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "body",
    "content",
    "key",
    "ocp_apim_subscription_key",
    "raw_body",
    "response_body",
    "response_content",
    "subscription_key",
    "token",
}
_FORBIDDEN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "key",
    "ocp-apim-subscription-key",
    "subscription-key",
    "token",
}


class StructuredDataStoreError(ValueError):
    """A structured collection cannot be persisted without violating its contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StructuredCollectionPayload:
    scope: StructuredScope
    channel_id: int
    collection_run_id: int
    retrieved_at: datetime
    query_manifest: Mapping[str, Any]
    query_urls: tuple[str, ...]
    parse_results: tuple[WorldBankParseResult, ...]


@dataclass(frozen=True, slots=True)
class StructuredCollectionResult:
    dataset_id: int
    snapshot_id: int
    query_signature: str
    snapshot_hash: str
    snapshot_created: bool
    observations_created: int
    versions_created: int
    versions_unchanged: int


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    observation_key: str
    observation_hash: str
    country_iso3: str
    indicator_code: str
    frequency: str
    period: int
    metric_code: str
    value: Decimal | None
    unit: str
    currency: str | None
    price_basis: str | None
    source_url: str
    source_release_date: date
    source_status: str
    missing_reason: str | None
    quality_flags: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _NormalizedCollection:
    retrieved_at: datetime
    query_manifest: dict[str, Any]
    query_signature: str
    snapshot_hash: str
    provider_version: str
    source_metadata: dict[str, Any]
    expected_count: int
    returned_count: int
    valued_count: int
    source_null_count: int
    not_returned_count: int
    observations: tuple[_NormalizedObservation, ...]


def persist_structured_collection(
    session: Session,
    payload: StructuredCollectionPayload,
) -> StructuredCollectionResult:
    """Flush one complete World Bank collection into immutable structured-data versions."""

    normalized = _normalize_payload(payload)
    channel, run = _load_owner(session, payload)
    dataset = _load_or_create_dataset(session, channel, payload.scope)
    observations, latest_versions, latest_snapshot = _load_dataset_state(session, dataset)
    existing_run_snapshot = session.scalar(
        select(StructuredSnapshot).where(
            StructuredSnapshot.collection_run_id == run.id,
        )
    )

    if existing_run_snapshot is not None:
        if latest_snapshot is None or existing_run_snapshot.id != latest_snapshot.id:
            raise StructuredDataStoreError(
                "collection_run_already_snapshotted",
                "collection run is linked to a non-latest structured snapshot",
            )
        _assert_snapshot_matches(existing_run_snapshot, normalized)
        return _reuse_snapshot(
            session,
            dataset,
            latest_snapshot,
            observations,
            latest_versions,
            normalized,
        )

    if latest_snapshot is not None and _same_snapshot(latest_snapshot, normalized):
        return _reuse_snapshot(
            session,
            dataset,
            latest_snapshot,
            observations,
            latest_versions,
            normalized,
        )

    snapshot = StructuredSnapshot(
        dataset_id=dataset.id,
        collection_run_id=run.id,
        query_manifest=normalized.query_manifest,
        query_signature=normalized.query_signature,
        snapshot_hash=normalized.snapshot_hash,
        provider_version=normalized.provider_version,
        source_metadata=normalized.source_metadata,
        expected_count=normalized.expected_count,
        returned_count=normalized.returned_count,
        valued_count=normalized.valued_count,
        source_null_count=normalized.source_null_count,
        not_returned_count=normalized.not_returned_count,
        retrieved_at=normalized.retrieved_at,
    )
    session.add(snapshot)
    session.flush()

    by_key = {observation.observation_key: observation for observation in observations}
    observations_created = 0
    versions_created = 0
    versions_unchanged = 0
    snapshot_members: list[tuple[StructuredObservation, StructuredObservationVersion]] = []
    for item in normalized.observations:
        observation = by_key.get(item.observation_key)
        if observation is None:
            observation = StructuredObservation(
                dataset_id=dataset.id,
                observation_key=item.observation_key,
                country_iso3=item.country_iso3,
                partner_iso3=None,
                indicator_code=item.indicator_code,
                commodity_classification=None,
                commodity_code=None,
                trade_flow=None,
                partner2_code=None,
                customs_code=None,
                mot_code=None,
                frequency=item.frequency,
                period=item.period,
                metric_code=item.metric_code,
                first_seen_at=normalized.retrieved_at,
                last_seen_at=normalized.retrieved_at,
                latest_version_no=0,
            )
            session.add(observation)
            session.flush()
            by_key[item.observation_key] = observation
            observations_created += 1
        else:
            _assert_observation_identity(observation, item)
            observation.last_seen_at = normalized.retrieved_at

        latest_version = latest_versions.get(observation.id)
        if latest_version is not None and latest_version.observation_hash == item.observation_hash:
            versions_unchanged += 1
            version = latest_version
        else:
            next_version_no = observation.latest_version_no + 1
            version = StructuredObservationVersion(
                observation_id=observation.id,
                snapshot_id=snapshot.id,
                version_no=next_version_no,
                observation_hash=item.observation_hash,
                value=item.value,
                unit=item.unit,
                currency=item.currency,
                price_basis=item.price_basis,
                source_url=item.source_url,
                source_release_date=item.source_release_date,
                source_status=item.source_status,
                missing_reason=item.missing_reason,
                is_reported=None,
                is_aggregate=None,
                quality_flags=item.quality_flags,
            )
            session.add(version)
            observation.latest_version_no = next_version_no
            versions_created += 1
        snapshot_members.append((observation, version))

    session.flush()
    session.add_all(
        [
            StructuredSnapshotObservation(
                snapshot_id=snapshot.id,
                observation_id=observation.id,
                observation_version_id=version.id,
            )
            for observation, version in snapshot_members
        ]
    )
    session.flush()
    return StructuredCollectionResult(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        query_signature=normalized.query_signature,
        snapshot_hash=normalized.snapshot_hash,
        snapshot_created=True,
        observations_created=observations_created,
        versions_created=versions_created,
        versions_unchanged=versions_unchanged,
    )


def _normalize_payload(payload: StructuredCollectionPayload) -> _NormalizedCollection:
    retrieved_at = _utc_datetime(payload.retrieved_at)
    world_bank = payload.scope.world_bank
    indicators = {indicator.code: indicator for indicator in world_bank.indicators}
    expected_size = {COD_BASICS_KEY: 88, COD_RECENT_KEY: 5}.get(world_bank.dataset_key, 200)
    if (
        len(indicators) != (8 if expected_size == 88 else 5)
        or world_bank.logical_dimension_cells != expected_size
    ):
        raise StructuredDataStoreError(
            "scope_mismatch",
            "structured scope must contain the reviewed five-indicator, 200-cell World Bank grid",
        )

    expected_urls = {
        indicator.code: _normalize_query_url(build_world_bank_request_url(world_bank, indicator.code))
        for indicator in world_bank.indicators
    }
    normalized_urls = tuple(sorted(_normalize_query_url(url) for url in payload.query_urls))
    if len(normalized_urls) != len(expected_urls) or len(set(normalized_urls)) != len(normalized_urls):
        raise StructuredDataStoreError(
            "query_scope_mismatch",
            "query URLs must contain each reviewed World Bank request exactly once",
        )
    if set(normalized_urls) != set(expected_urls.values()):
        raise StructuredDataStoreError(
            "query_scope_mismatch",
            "query URLs do not match the reviewed World Bank requests",
        )
    query_signature = _sha256(list(normalized_urls))
    query_manifest = _normalize_manifest(payload.query_manifest)

    if len(payload.parse_results) != len(indicators):
        raise StructuredDataStoreError(
            "parse_result_scope_mismatch",
            "exactly five World Bank parse results are required",
        )
    results_by_indicator: dict[str, WorldBankParseResult] = {}
    normalized_observations: list[_NormalizedObservation] = []
    last_updated_by_indicator: dict[str, str] = {}
    returned_count = 0
    valued_count = 0
    source_null_count = 0
    not_returned_count = 0

    for result in payload.parse_results:
        indicator_code = result.query_spec.indicator_code
        if indicator_code in results_by_indicator or indicator_code not in indicators:
            raise StructuredDataStoreError(
                "parse_result_scope_mismatch",
                "parse results must match each reviewed indicator exactly once",
            )
        indicator = indicators[indicator_code]
        _validate_query_spec(result, indicator, payload.scope)
        results_by_indicator[indicator_code] = result
        last_updated_by_indicator[indicator_code] = result.last_updated.isoformat()

        expected_cells = {
            (country, year)
            for country in world_bank.countries
            for year in range(world_bank.start_year, world_bank.end_year + 1)
        }
        seen_cells: set[tuple[str, int]] = set()
        result_returned_count = 0
        for observation in result.observations:
            normalized = _normalize_observation(
                observation,
                result,
                indicator,
                payload.scope,
                expected_urls[indicator_code],
            )
            cell = (normalized.country_iso3, normalized.period)
            if cell not in expected_cells or cell in seen_cells:
                raise StructuredDataStoreError(
                    "observation_grid_mismatch",
                    "parse result must contain each reviewed country-year cell exactly once",
                )
            seen_cells.add(cell)
            normalized_observations.append(normalized)
            if normalized.value is not None:
                valued_count += 1
                result_returned_count += 1
            elif normalized.missing_reason == "source_null":
                source_null_count += 1
                result_returned_count += 1
            elif normalized.missing_reason == "not_returned":
                not_returned_count += 1
            else:
                raise StructuredDataStoreError(
                    "invalid_missing_reason",
                    "null observations must be source_null or not_returned",
                )
        if seen_cells != expected_cells or len(result.observations) != len(expected_cells):
            raise StructuredDataStoreError(
                "observation_grid_mismatch",
                "parse result does not cover its complete 40-cell grid",
            )
        if result.returned_row_count != result_returned_count:
            raise StructuredDataStoreError(
                "returned_count_mismatch",
                "parse result returned_row_count does not match valued and source-null cells",
            )
        returned_count += result.returned_row_count

    if set(results_by_indicator) != set(indicators):
        raise StructuredDataStoreError(
            "parse_result_scope_mismatch",
            "parse results do not cover the reviewed indicators",
        )
    _validate_query_manifest(
        query_manifest,
        scope=payload.scope,
        expected_urls=expected_urls,
        results_by_indicator=results_by_indicator,
        retrieved_at=retrieved_at,
    )
    if len(normalized_observations) != world_bank.logical_dimension_cells:
        raise StructuredDataStoreError(
            "observation_grid_mismatch",
            "World Bank collection must normalize to exactly 200 observations",
        )

    normalized_observations.sort(key=lambda item: item.observation_key)
    snapshot_hash = _sha256(
        [[item.observation_key, item.observation_hash] for item in normalized_observations]
    )
    last_updated_values = sorted(set(last_updated_by_indicator.values()))
    provider_version = (
        last_updated_values[0] if len(last_updated_values) == 1 else ",".join(last_updated_values)
    )
    source_metadata = {
        "last_updated_by_indicator": dict(sorted(last_updated_by_indicator.items())),
        "official_dataset": {
            "id": world_bank.official_dataset.id,
            "name": world_bank.official_dataset.name,
            "license_name": world_bank.official_dataset.license_name,
            "license_url": world_bank.official_dataset.license_url,
            "catalog_url": world_bank.official_dataset.catalog_url,
        },
        "scope_id": payload.scope.scope_id,
        "scope_sha256": payload.scope.scope_sha256,
        "source_catalog_row": world_bank.source_catalog_row,
    }
    expected_count = world_bank.logical_dimension_cells
    if expected_count != returned_count + not_returned_count:
        raise StructuredDataStoreError(
            "expected_count_mismatch",
            "expected count does not equal returned plus not-returned observations",
        )
    if returned_count != valued_count + source_null_count:
        raise StructuredDataStoreError(
            "returned_count_mismatch",
            "returned count does not equal valued plus source-null observations",
        )

    return _NormalizedCollection(
        retrieved_at=retrieved_at,
        query_manifest=query_manifest,
        query_signature=query_signature,
        snapshot_hash=snapshot_hash,
        provider_version=provider_version,
        source_metadata=source_metadata,
        expected_count=expected_count,
        returned_count=returned_count,
        valued_count=valued_count,
        source_null_count=source_null_count,
        not_returned_count=not_returned_count,
        observations=tuple(normalized_observations),
    )


def _validate_query_spec(
    result: WorldBankParseResult,
    indicator: WorldBankIndicator,
    scope: StructuredScope,
) -> None:
    query = result.query_spec
    world_bank = scope.world_bank
    expected_years = tuple(range(world_bank.start_year, world_bank.end_year + 1))
    if (
        query.indicator_code != indicator.code
        or query.metric_code != indicator.metric_code
        or query.unit != indicator.unit
        or query.currency != indicator.currency
        or query.price_basis != indicator.price_basis
        or query.countries != world_bank.countries
        or query.years != expected_years
        or query.source_id != int(world_bank.source_id)
    ):
        raise StructuredDataStoreError(
            "query_result_mismatch",
            "parse result query specification does not match the structured scope",
        )
    if not isinstance(result.last_updated, date) or isinstance(result.last_updated, datetime):
        raise StructuredDataStoreError(
            "invalid_provider_version",
            "World Bank last_updated must be a date",
        )
    if type(result.returned_row_count) is not int or not 0 <= result.returned_row_count <= len(
        world_bank.countries
    ) * len(expected_years):
        raise StructuredDataStoreError(
            "invalid_returned_count",
            "World Bank returned_row_count must be between zero and forty",
        )


def _normalize_observation(
    observation: WorldBankObservation,
    result: WorldBankParseResult,
    indicator: WorldBankIndicator,
    scope: StructuredScope,
    source_url: str,
) -> _NormalizedObservation:
    world_bank = scope.world_bank
    try:
        period = int(observation.period)
    except (TypeError, ValueError) as exc:
        raise StructuredDataStoreError(
            "observation_scope_mismatch",
            "observation period must be a canonical year",
        ) from exc
    if str(period) != observation.period:
        raise StructuredDataStoreError(
            "observation_scope_mismatch",
            "observation period must be a canonical year",
        )
    if (
        observation.source_id != int(world_bank.source_id)
        or observation.country_iso3 not in world_bank.countries
        or observation.indicator_code != indicator.code
        or observation.metric_code != indicator.metric_code
        or observation.frequency != world_bank.frequency
        or period not in range(world_bank.start_year, world_bank.end_year + 1)
        or observation.unit != indicator.unit
        or observation.currency != indicator.currency
        or observation.price_basis != indicator.price_basis
    ):
        raise StructuredDataStoreError(
            "observation_scope_mismatch",
            "observation dimensions or measurement metadata do not match the scope",
        )

    value_text = None
    if observation.value is not None:
        if observation.missing_reason is not None:
            raise StructuredDataStoreError(
                "invalid_missing_state",
                "valued observations cannot have a missing reason",
            )
        value_text = _canonical_decimal(observation.value)
    elif observation.missing_reason not in {"source_null", "not_returned"}:
        raise StructuredDataStoreError(
            "invalid_missing_state",
            "null observations must have an explicit supported missing reason",
        )

    quality = dict(observation.quality_metadata)
    if set(quality) != {"obs_status", "api_unit"}:
        raise StructuredDataStoreError(
            "invalid_quality_metadata",
            "World Bank quality metadata must contain obs_status and api_unit only",
        )
    source_status = quality.pop("obs_status")
    api_unit = quality.get("api_unit")
    if (
        not isinstance(source_status, str)
        or not source_status
        or source_status.strip() != source_status
        or len(source_status) > 24
    ):
        raise StructuredDataStoreError(
            "invalid_quality_metadata",
            "World Bank obs_status must be reviewed text of at most 24 characters",
        )
    if api_unit is not None and not isinstance(api_unit, str):
        raise StructuredDataStoreError(
            "invalid_quality_metadata",
            "World Bank api_unit must be text or null",
        )
    quality_flags = {"api_unit": api_unit}
    identity = {
        "commodity_classification": None,
        "commodity_code": None,
        "country_iso3": observation.country_iso3,
        "dataset_key": world_bank.dataset_key,
        "frequency": observation.frequency,
        "indicator_code": observation.indicator_code,
        "metric_code": observation.metric_code,
        "mot_code": None,
        "partner2_code": None,
        "partner_iso3": None,
        "period": period,
        "trade_flow": None,
        "customs_code": None,
    }
    observation_key = _sha256(identity)
    observation_hash = _sha256(
        {
            "currency": observation.currency,
            "identity": identity,
            "last_updated": result.last_updated.isoformat(),
            "missing_reason": observation.missing_reason,
            "obs_status": source_status,
            "price_basis": observation.price_basis,
            "quality_flags": quality_flags,
            "unit": observation.unit,
            "value": value_text,
        }
    )
    return _NormalizedObservation(
        observation_key=observation_key,
        observation_hash=observation_hash,
        country_iso3=observation.country_iso3,
        indicator_code=observation.indicator_code,
        frequency=observation.frequency,
        period=period,
        metric_code=observation.metric_code,
        value=observation.value,
        unit=observation.unit,
        currency=observation.currency,
        price_basis=observation.price_basis,
        source_url=source_url,
        source_release_date=result.last_updated,
        source_status=source_status,
        missing_reason=observation.missing_reason,
        quality_flags=quality_flags,
    )


def _load_owner(
    session: Session,
    payload: StructuredCollectionPayload,
) -> tuple[SourceChannel, CollectionRun]:
    if type(payload.channel_id) is not int or payload.channel_id <= 0:
        raise StructuredDataStoreError("invalid_channel", "channel_id must be a positive integer")
    if type(payload.collection_run_id) is not int or payload.collection_run_id <= 0:
        raise StructuredDataStoreError(
            "invalid_collection_run",
            "collection_run_id must be a positive integer",
        )
    channel = session.get(SourceChannel, payload.channel_id)
    run = session.get(CollectionRun, payload.collection_run_id)
    if channel is None or run is None:
        raise StructuredDataStoreError(
            "owner_not_found",
            "structured collection channel or run does not exist",
        )
    if run.channel_id != channel.id:
        raise StructuredDataStoreError(
            "owner_mismatch",
            "collection run does not belong to the structured dataset channel",
        )
    if channel.collector_type != "api" or run.status != "running":
        raise StructuredDataStoreError(
            "owner_state_mismatch",
            "structured collection requires an API channel and a currently running run",
        )
    return channel, run


def _load_or_create_dataset(
    session: Session,
    channel: SourceChannel,
    scope: StructuredScope,
) -> StructuredDataset:
    world_bank = scope.world_bank
    by_channel = session.scalar(
        select(StructuredDataset).where(StructuredDataset.channel_id == channel.id).with_for_update()
    )
    by_key = session.scalar(
        select(StructuredDataset)
        .where(StructuredDataset.dataset_key == world_bank.dataset_key)
        .with_for_update()
    )
    if by_channel is not None and by_key is not None and by_channel.id != by_key.id:
        raise StructuredDataStoreError(
            "dataset_channel_conflict",
            "dataset key and channel resolve to different structured datasets",
        )
    dataset = by_channel or by_key
    if dataset is None:
        dataset = StructuredDataset(
            channel_id=channel.id,
            dataset_key=world_bank.dataset_key,
            name=world_bank.name,
            scope_id=scope.scope_id,
            frequency=world_bank.frequency,
        )
        session.add(dataset)
        session.flush()
        return dataset
    if (
        dataset.channel_id != channel.id
        or dataset.dataset_key != world_bank.dataset_key
        or dataset.name != world_bank.name
        or dataset.scope_id != scope.scope_id
        or dataset.frequency != world_bank.frequency
    ):
        raise StructuredDataStoreError(
            "dataset_channel_conflict",
            "existing structured dataset does not match the reviewed channel and scope",
        )
    return dataset


def _load_dataset_state(
    session: Session,
    dataset: StructuredDataset,
) -> tuple[
    tuple[StructuredObservation, ...],
    dict[int, StructuredObservationVersion],
    StructuredSnapshot | None,
]:
    observations = tuple(
        session.scalars(
            select(StructuredObservation)
            .where(StructuredObservation.dataset_id == dataset.id)
            .order_by(StructuredObservation.id)
            .with_for_update()
        )
    )
    latest_snapshot = session.scalar(
        select(StructuredSnapshot)
        .where(StructuredSnapshot.dataset_id == dataset.id)
        .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
        .limit(1)
    )
    if observations and len(observations) != (
        {COD_BASICS_KEY: 88, COD_RECENT_KEY: 5}.get(dataset.dataset_key, 200)
    ):
        raise StructuredDataStoreError(
            "incomplete_dataset_state",
            "existing World Bank dataset does not contain exactly 200 observations",
        )
    if bool(observations) != (latest_snapshot is not None):
        raise StructuredDataStoreError(
            "incomplete_dataset_state",
            "structured observations and snapshots must exist together",
        )
    latest_versions: dict[int, StructuredObservationVersion] = {}
    if observations:
        members = tuple(
            session.scalars(
                select(StructuredSnapshotObservation).where(
                    StructuredSnapshotObservation.snapshot_id == latest_snapshot.id
                )
            )
        )
        member_by_observation = {member.observation_id: member for member in members}
        if len(member_by_observation) != len(observations):
            raise StructuredDataStoreError(
                "incomplete_dataset_state",
                "latest snapshot must contain one member for every structured observation",
            )
        version_by_id = {
            version.id: version
            for version in session.scalars(
                select(StructuredObservationVersion).where(
                    StructuredObservationVersion.id.in_(member.observation_version_id for member in members)
                )
            )
        }
        for observation in observations:
            member = member_by_observation.get(observation.id)
            version = version_by_id.get(member.observation_version_id) if member is not None else None
            if (
                version is None
                or version.observation_id != observation.id
                or version.version_no != observation.latest_version_no
            ):
                raise StructuredDataStoreError(
                    "incomplete_dataset_state",
                    "latest snapshot member must reference the declared latest observation version",
                )
            latest_versions[observation.id] = version
    return observations, latest_versions, latest_snapshot


def _same_snapshot(snapshot: StructuredSnapshot, normalized: _NormalizedCollection) -> bool:
    return (
        snapshot.query_signature == normalized.query_signature
        and snapshot.snapshot_hash == normalized.snapshot_hash
    )


def _assert_snapshot_matches(
    snapshot: StructuredSnapshot,
    normalized: _NormalizedCollection,
) -> None:
    if not _same_snapshot(snapshot, normalized):
        raise StructuredDataStoreError(
            "collection_run_snapshot_conflict",
            "collection run already has a different structured snapshot",
        )
    counts = (
        snapshot.expected_count,
        snapshot.returned_count,
        snapshot.valued_count,
        snapshot.source_null_count,
        snapshot.not_returned_count,
    )
    expected = (
        normalized.expected_count,
        normalized.returned_count,
        normalized.valued_count,
        normalized.source_null_count,
        normalized.not_returned_count,
    )
    if counts != expected or snapshot.provider_version != normalized.provider_version:
        raise StructuredDataStoreError(
            "collection_run_snapshot_conflict",
            "collection run snapshot metadata does not match the current payload",
        )


def _reuse_snapshot(
    session: Session,
    dataset: StructuredDataset,
    snapshot: StructuredSnapshot,
    observations: tuple[StructuredObservation, ...],
    latest_versions: dict[int, StructuredObservationVersion],
    normalized: _NormalizedCollection,
) -> StructuredCollectionResult:
    by_key = {observation.observation_key: observation for observation in observations}
    if set(by_key) != {item.observation_key for item in normalized.observations}:
        raise StructuredDataStoreError(
            "incomplete_dataset_state",
            "latest snapshot identity set does not match the current complete payload",
        )
    for item in normalized.observations:
        observation = by_key[item.observation_key]
        _assert_observation_identity(observation, item)
        latest_version = latest_versions.get(observation.id)
        if latest_version is None or latest_version.observation_hash != item.observation_hash:
            raise StructuredDataStoreError(
                "snapshot_state_mismatch",
                "latest observation versions do not reproduce the snapshot hash",
            )
        observation.last_seen_at = normalized.retrieved_at
    session.flush()
    return StructuredCollectionResult(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        query_signature=normalized.query_signature,
        snapshot_hash=normalized.snapshot_hash,
        snapshot_created=False,
        observations_created=0,
        versions_created=0,
        versions_unchanged=len(normalized.observations),
    )


def _assert_observation_identity(
    observation: StructuredObservation,
    item: _NormalizedObservation,
) -> None:
    if (
        observation.country_iso3 != item.country_iso3
        or observation.partner_iso3 is not None
        or observation.indicator_code != item.indicator_code
        or observation.commodity_classification is not None
        or observation.commodity_code is not None
        or observation.trade_flow is not None
        or observation.partner2_code is not None
        or observation.customs_code is not None
        or observation.mot_code is not None
        or observation.frequency != item.frequency
        or observation.period != item.period
        or observation.metric_code != item.metric_code
    ):
        raise StructuredDataStoreError(
            "observation_identity_conflict",
            "existing observation key resolves to different identity dimensions",
        )


def _normalize_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            "query_manifest must be a JSON object",
        )
    normalized = _normalize_manifest_value(value, path="query_manifest")
    assert isinstance(normalized, dict)
    return normalized


def _validate_query_manifest(
    manifest: dict[str, Any],
    *,
    scope: StructuredScope,
    expected_urls: dict[str, str],
    results_by_indicator: dict[str, WorldBankParseResult],
    retrieved_at: datetime,
) -> None:
    if set(manifest) != {"scope_id", "scope_sha256", "queries"}:
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            "query_manifest must contain only scope_id, scope_sha256, and queries",
        )
    if manifest["scope_id"] != scope.scope_id or manifest["scope_sha256"] != scope.scope_sha256:
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            "query_manifest scope evidence does not match the loaded structured scope",
        )

    queries = manifest["queries"]
    if not isinstance(queries, list) or len(queries) != len(expected_urls):
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            "query_manifest must contain exactly five World Bank query records",
        )

    query_keys = {
        "indicator_code",
        "request_url",
        "final_url",
        "http_status",
        "resource_sha256",
        "fetched_at",
        "last_updated",
        "returned_row_count",
    }
    seen_indicators: set[str] = set()
    for index, query in enumerate(queries):
        if not isinstance(query, dict) or set(query) != query_keys:
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                f"query_manifest.queries[{index}] does not match the reviewed evidence fields",
            )

        indicator_code = query["indicator_code"]
        if (
            not isinstance(indicator_code, str)
            or indicator_code not in expected_urls
            or indicator_code in seen_indicators
        ):
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                "query_manifest must identify each reviewed indicator exactly once",
            )
        seen_indicators.add(indicator_code)

        expected_url = expected_urls[indicator_code]
        request_url = _manifest_query_url(query["request_url"], index, "request_url")
        final_url = _manifest_query_url(query["final_url"], index, "final_url")
        if request_url != expected_url or final_url != expected_url:
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                "query_manifest request and final URLs must match the reviewed fixed query",
            )
        if type(query["http_status"]) is not int or query["http_status"] != 200:
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                "query_manifest http_status must be 200",
            )

        resource_sha256 = query["resource_sha256"]
        if (
            not isinstance(resource_sha256, str)
            or len(resource_sha256) != 64
            or any(character not in "0123456789abcdef" for character in resource_sha256)
        ):
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                "query_manifest resource_sha256 must be 64 lowercase hexadecimal characters",
            )

        fetched_at = _manifest_fetched_at(query["fetched_at"], index)
        if fetched_at > retrieved_at:
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                "query_manifest fetched_at cannot be later than collection retrieved_at",
            )

        result = results_by_indicator[indicator_code]
        if query["last_updated"] != result.last_updated.isoformat():
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                "query_manifest last_updated does not match its parsed World Bank result",
            )
        if (
            type(query["returned_row_count"]) is not int
            or query["returned_row_count"] != result.returned_row_count
        ):
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                "query_manifest returned_row_count does not match its parsed World Bank result",
            )

    if seen_indicators != set(expected_urls):
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            "query_manifest does not cover every reviewed World Bank indicator",
        )


def _manifest_query_url(value: Any, index: int, field_name: str) -> str:
    try:
        return _normalize_query_url(value)
    except StructuredDataStoreError as exc:
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            f"query_manifest.queries[{index}].{field_name} is not a reviewed query URL",
        ) from exc


def _manifest_fetched_at(value: Any, index: int) -> datetime:
    if not isinstance(value, str):
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            f"query_manifest.queries[{index}].fetched_at must be a timezone-aware ISO datetime",
        )
    try:
        parsed = datetime.fromisoformat(value)
        return _utc_datetime(parsed)
    except (ValueError, StructuredDataStoreError) as exc:
        raise StructuredDataStoreError(
            "invalid_query_manifest",
            f"query_manifest.queries[{index}].fetched_at must be a timezone-aware ISO datetime",
        ) from exc


def _normalize_manifest_value(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                f"{path} contains a non-finite number",
            )
        return value
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, datetime):
        return _utc_datetime(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        keys = tuple(value)
        if any(not isinstance(key, str) or not key for key in keys):
            raise StructuredDataStoreError(
                "invalid_query_manifest",
                f"{path} keys must be non-empty strings",
            )
        normalized: dict[str, Any] = {}
        for key in sorted(keys):
            normalized_key = key.lower().replace("-", "_")
            if normalized_key in _FORBIDDEN_MANIFEST_KEYS:
                raise StructuredDataStoreError(
                    "invalid_query_manifest",
                    f"{path}.{key} is forbidden in normalized query evidence",
                )
            normalized[key] = _normalize_manifest_value(value[key], path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_manifest_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise StructuredDataStoreError(
        "invalid_query_manifest",
        f"{path} contains a non-JSON evidence value",
    )


def _normalize_query_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise StructuredDataStoreError("invalid_query_url", "query URL must be non-empty text")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise StructuredDataStoreError(
            "invalid_query_url",
            "query URL is not a valid reviewed HTTPS URL",
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise StructuredDataStoreError(
            "invalid_query_url",
            "query URL is not a valid reviewed HTTPS URL",
        )
    query_keys = [key.lower() for key, _ in query]
    if len(set(query_keys)) != len(query_keys) or any(key in _FORBIDDEN_QUERY_KEYS for key in query_keys):
        raise StructuredDataStoreError(
            "invalid_query_url",
            "query URL contains duplicate or credential parameters",
        )
    host = parsed.hostname.lower()
    netloc = host if port in {None, 443} else f"{host}:{port}"
    normalized_query = urlencode(sorted(query), doseq=True, safe=":;")
    return urlunsplit(("https", netloc, parsed.path, normalized_query, ""))


def _canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StructuredDataStoreError(
            "invalid_decimal",
            "structured observation values must be finite Decimal instances",
        )
    if value == 0:
        return "0"
    normalized = value.normalize()
    fractional_digits = max(-normalized.as_tuple().exponent, 0)
    integer_digits = max(normalized.adjusted() + 1, 0)
    if fractional_digits > 18 or integer_digits > 20:
        raise StructuredDataStoreError(
            "decimal_out_of_range",
            "structured observation value exceeds Numeric(38,18)",
        )
    return format(normalized, "f")


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StructuredDataStoreError(
            "invalid_retrieved_at",
            "retrieved_at must be a timezone-aware datetime",
        )
    return value.astimezone(UTC)


def _sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(serialized).hexdigest()
