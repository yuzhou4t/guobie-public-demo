from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.comtrade_availability import (
    ComtradeAvailabilityParseResult,
    ComtradeAvailabilityRecord,
)
from app.collectors.comtrade_data import ComtradeDataObservation, ComtradeDataParseResult
from app.collectors.comtrade_metadata import ComtradeMetadataParseResult
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
    ComtradeScope,
    StructuredScope,
    build_comtrade_data_availability_request_url,
    build_comtrade_metadata_request_url,
    build_comtrade_request_url,
)

_PROFILE = "un_comtrade_goods_annual_hs"
_FORBIDDEN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "key",
    "ocp-apim-subscription-key",
    "subscription-key",
    "subscription_key",
    "token",
}
_MANIFEST_QUERY_KEYS = {
    "kind",
    "reporter_iso3",
    "period",
    "commodity_code",
    "request_url",
    "final_url",
    "http_status",
    "resource_sha256",
    "fetched_at",
    "returned_row_count",
}


class ComtradeDataStoreError(ValueError):
    """A complete Comtrade collection cannot be stored safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ComtradeCollectionPayload:
    scope: StructuredScope
    channel_id: int
    collection_run_id: int
    retrieved_at: datetime
    query_manifest: Mapping[str, Any]
    query_urls: tuple[str, ...]
    availability_results: tuple[ComtradeAvailabilityParseResult, ...]
    metadata_results: tuple[ComtradeMetadataParseResult, ...]
    data_results: tuple[ComtradeDataParseResult, ...]


@dataclass(frozen=True, slots=True)
class ComtradeCollectionResult:
    dataset_id: int
    snapshot_id: int
    query_signature: str
    snapshot_hash: str
    snapshot_created: bool
    observations_created: int
    versions_created: int
    versions_unchanged: int
    expected_count: int
    returned_count: int
    valued_count: int
    source_null_count: int
    not_returned_count: int
    logical_dimension_count: int
    logical_returned_count: int
    logical_not_returned_count: int
    data_row_count: int


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    observation_key: str
    observation_hash: str
    country_iso3: str
    partner_iso3: str
    source_dataset_code: str | None
    commodity_classification: str | None
    commodity_code: str
    trade_flow: str
    partner2_code: str
    customs_code: str
    mot_code: str
    frequency: str
    period: int
    metric_code: str
    value: Decimal | None
    unit: str | None
    currency: str | None
    price_basis: str | None
    source_url: str
    source_release_date: date | None
    source_status: str
    missing_reason: str | None
    is_reported: bool | None
    is_aggregate: bool | None
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
    logical_dimension_count: int
    logical_returned_count: int
    logical_not_returned_count: int
    data_row_count: int
    observations: tuple[_NormalizedObservation, ...]


@dataclass(frozen=True, slots=True)
class _DatasetState:
    observations: tuple[StructuredObservation, ...]
    latest_versions: dict[int, StructuredObservationVersion]
    latest_snapshot: StructuredSnapshot | None
    latest_member_versions: dict[int, StructuredObservationVersion]


def persist_comtrade_collection(
    session: Session,
    payload: ComtradeCollectionPayload,
) -> ComtradeCollectionResult:
    """Persist one fully parsed 104-request Comtrade baseline atomically."""

    normalized = _normalize_payload(payload)
    channel, run = _load_owner(session, payload)
    dataset = _load_or_create_dataset(session, channel, payload.scope)
    state = _load_dataset_state(session, dataset)
    existing_run_snapshot = session.scalar(
        select(StructuredSnapshot).where(
            StructuredSnapshot.collection_run_id == run.id,
        )
    )

    if existing_run_snapshot is not None:
        if state.latest_snapshot is None or existing_run_snapshot.id != state.latest_snapshot.id:
            raise ComtradeDataStoreError(
                "collection_run_already_snapshotted",
                "collection run is linked to a non-latest Comtrade snapshot",
            )
        _assert_snapshot_matches(existing_run_snapshot, normalized)
        return _reuse_snapshot(session, dataset, state, normalized)

    if state.latest_snapshot is not None and _same_snapshot(state.latest_snapshot, normalized):
        return _reuse_snapshot(session, dataset, state, normalized)

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

    by_key = {observation.observation_key: observation for observation in state.observations}
    observations_created = 0
    versions_created = 0
    versions_unchanged = 0
    members: list[tuple[StructuredObservation, StructuredObservationVersion]] = []
    for item in normalized.observations:
        observation = by_key.get(item.observation_key)
        if observation is None:
            observation = StructuredObservation(
                dataset_id=dataset.id,
                observation_key=item.observation_key,
                country_iso3=item.country_iso3,
                partner_iso3=item.partner_iso3,
                indicator_code=None,
                commodity_classification=item.commodity_classification,
                source_dataset_code=item.source_dataset_code,
                commodity_code=item.commodity_code,
                trade_flow=item.trade_flow,
                partner2_code=item.partner2_code,
                customs_code=item.customs_code,
                mot_code=item.mot_code,
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

        latest_version = state.latest_versions.get(observation.id)
        if latest_version is not None and latest_version.observation_hash == item.observation_hash:
            version = latest_version
            versions_unchanged += 1
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
                is_reported=item.is_reported,
                is_aggregate=item.is_aggregate,
                quality_flags=item.quality_flags,
            )
            session.add(version)
            observation.latest_version_no = next_version_no
            versions_created += 1
        members.append((observation, version))

    session.flush()
    session.add_all(
        [
            StructuredSnapshotObservation(
                snapshot_id=snapshot.id,
                observation_id=observation.id,
                observation_version_id=version.id,
            )
            for observation, version in members
        ]
    )
    session.flush()
    return _result(
        dataset=dataset,
        snapshot=snapshot,
        normalized=normalized,
        snapshot_created=True,
        observations_created=observations_created,
        versions_created=versions_created,
        versions_unchanged=versions_unchanged,
    )


def _normalize_payload(payload: ComtradeCollectionPayload) -> _NormalizedCollection:
    retrieved_at = _utc_datetime(payload.retrieved_at)
    comtrade = payload.scope.comtrade
    if (
        comtrade.logical_dimension_cells != 384
        or comtrade.request_shards != 96
        or comtrade.minimum_metric_observation_cells != 1152
        or len(comtrade.reporters) != 4
        or len(comtrade.partners) != 2
        or len(comtrade.commodities) != 3
        or len(comtrade.metrics) != 3
    ):
        raise ComtradeDataStoreError(
            "scope_mismatch",
            "Comtrade scope must match the reviewed 384-cell and 96-shard baseline",
        )

    expected_urls, expected_keys = _expected_queries(comtrade)
    normalized_urls = tuple(sorted(_normalize_query_url(url) for url in payload.query_urls))
    if (
        len(normalized_urls) != 104
        or len(set(normalized_urls)) != 104
        or set(normalized_urls) != set(expected_urls.values())
    ):
        raise ComtradeDataStoreError(
            "query_scope_mismatch",
            "query URLs must contain the four getDA, four metadata, and 96 Data requests exactly once",
        )
    query_signature = _sha256(list(normalized_urls))

    availability_by_reporter, availability_evidence = _validate_availability_results(
        payload.availability_results,
        comtrade,
    )
    metadata_by_reporter, metadata_evidence = _validate_metadata_results(
        payload.metadata_results,
        comtrade,
    )
    identity_records = _availability_identity_records(availability_by_reporter)
    _validate_public_identity_contract(
        identity_records,
        metadata_by_reporter,
    )

    data_by_key = _validate_data_result_set(payload.data_results, comtrade)
    result_counts: dict[tuple[str, str, int | None, str | None], int] = {}
    for reporter in comtrade.reporters:
        result_counts[("getDA", reporter.iso3, None, None)] = availability_by_reporter[
            reporter.iso3
        ].returned_dataset_count
        result_counts[("metadata", reporter.iso3, None, None)] = metadata_by_reporter[
            reporter.iso3
        ].returned_dataset_count
    for key, result in data_by_key.items():
        result_counts[("data", *key)] = result.returned_row_count
    query_manifest = _validate_manifest(
        payload.query_manifest,
        scope=payload.scope,
        expected_urls=expected_urls,
        expected_keys=expected_keys,
        result_counts=result_counts,
        retrieved_at=retrieved_at,
    )

    normalized_observations: list[_NormalizedObservation] = []
    logical_returned_count = 0
    data_row_count = 0
    for reporter in comtrade.reporters:
        availability = availability_by_reporter[reporter.iso3]
        metadata = metadata_by_reporter[reporter.iso3]
        for period in range(comtrade.start_year, comtrade.end_year + 1):
            for commodity in comtrade.commodities:
                key = (reporter.iso3, period, commodity.hs6)
                result = data_by_key[key]
                source_url = expected_urls[("data", *key)]
                returned_cells: set[tuple[int, str]] = set()
                for observation in result.observations:
                    _validate_data_observation(
                        observation,
                        result=result,
                        availability=availability,
                        metadata=metadata,
                        comtrade=comtrade,
                    )
                    returned_cells.add((observation.partner_code, observation.flow_code))
                    normalized_observations.extend(
                        _normalize_returned_row(
                            observation,
                            source_url=source_url,
                            comtrade=comtrade,
                        )
                    )
                if len(result.observations) != result.returned_row_count:
                    raise ComtradeDataStoreError(
                        "returned_count_mismatch",
                        "Comtrade Data result row count does not match parsed observations",
                    )
                data_row_count += result.returned_row_count

                expected_cells = {
                    (partner.source_code, flow) for partner in comtrade.partners for flow in comtrade.flows
                }
                if not returned_cells.issubset(expected_cells):
                    raise ComtradeDataStoreError(
                        "observation_grid_mismatch",
                        "Comtrade Data result contains a partner-flow cell outside the fixed grid",
                    )
                logical_returned_count += len(returned_cells)
                for partner_code, flow_code in sorted(expected_cells - returned_cells):
                    normalized_observations.extend(
                        _normalize_not_returned_cell(
                            reporter_iso3=reporter.iso3,
                            partner_iso3=_partner_iso3(comtrade, partner_code),
                            period=period,
                            commodity_code=commodity.hs6,
                            flow_code=flow_code,
                            source_url=source_url,
                            comtrade=comtrade,
                        )
                    )

    logical_not_returned_count = comtrade.logical_dimension_cells - logical_returned_count
    if logical_not_returned_count < 0:
        raise ComtradeDataStoreError(
            "observation_grid_mismatch",
            "Comtrade logical returned count exceeds the reviewed grid",
        )

    by_key: dict[str, _NormalizedObservation] = {}
    for observation in normalized_observations:
        if observation.observation_key in by_key:
            raise ComtradeDataStoreError(
                "duplicate_observation_identity",
                "Comtrade normalization produced a duplicate metric identity",
            )
        by_key[observation.observation_key] = observation
    normalized_observations = sorted(by_key.values(), key=lambda item: item.observation_key)

    returned_count = data_row_count * len(comtrade.metrics)
    not_returned_count = logical_not_returned_count * len(comtrade.metrics)
    valued_count = sum(item.value is not None for item in normalized_observations)
    source_null_count = sum(item.missing_reason == "source_null" for item in normalized_observations)
    expected_count = len(normalized_observations)
    if (
        expected_count < comtrade.minimum_metric_observation_cells
        or expected_count != returned_count + not_returned_count
        or returned_count != valued_count + source_null_count
        or logical_returned_count + logical_not_returned_count != comtrade.logical_dimension_cells
    ):
        raise ComtradeDataStoreError(
            "observation_count_mismatch",
            "Comtrade metric and logical-cell counts do not balance",
        )

    public_identity_hash = _sha256(
        {
            "availability": availability_evidence,
            "metadata": metadata_evidence,
        }
    )
    snapshot_hash = _sha256(
        {
            "observations": [
                [item.observation_key, item.observation_hash] for item in normalized_observations
            ],
            "public_identity_hash": public_identity_hash,
        }
    )
    release_times = [
        record.last_released for result in availability_by_reporter.values() for record in result.records
    ]
    provider_version = (
        max(_canonical_datetime(value) for value in release_times) if release_times else "unknown"
    )
    source_metadata = {
        "scope_id": payload.scope.scope_id,
        "scope_sha256": payload.scope.scope_sha256,
        "source_catalog_row": comtrade.source_catalog_row,
        "public_identity_hash": public_identity_hash,
        "availability_dataset_count": sum(
            result.returned_dataset_count for result in availability_by_reporter.values()
        ),
        "metadata_dataset_count": sum(
            result.returned_dataset_count for result in metadata_by_reporter.values()
        ),
        "metadata_note_count": sum(len(result.notes) for result in metadata_by_reporter.values()),
        "data_row_count": data_row_count,
        "logical_dimension_count": comtrade.logical_dimension_cells,
        "logical_returned_count": logical_returned_count,
        "logical_not_returned_count": logical_not_returned_count,
        "official_dataset": {
            "id": comtrade.official_dataset.id,
            "name": comtrade.official_dataset.name,
            "terms_name": comtrade.official_dataset.terms_name,
            "terms_url": comtrade.official_dataset.terms_url,
            "catalog_url": comtrade.official_dataset.catalog_url,
        },
    }
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
        logical_dimension_count=comtrade.logical_dimension_cells,
        logical_returned_count=logical_returned_count,
        logical_not_returned_count=logical_not_returned_count,
        data_row_count=data_row_count,
        observations=tuple(normalized_observations),
    )


def _expected_queries(
    scope: ComtradeScope,
) -> tuple[
    dict[tuple[str, str, int | None, str | None], str],
    set[tuple[str, str, int | None, str | None]],
]:
    urls: dict[tuple[str, str, int | None, str | None], str] = {}
    for reporter in scope.reporters:
        urls[("getDA", reporter.iso3, None, None)] = _normalize_query_url(
            build_comtrade_data_availability_request_url(scope, reporter.iso3)
        )
        urls[("metadata", reporter.iso3, None, None)] = _normalize_query_url(
            build_comtrade_metadata_request_url(scope, reporter.iso3)
        )
        for period in range(scope.start_year, scope.end_year + 1):
            for commodity in scope.commodities:
                urls[("data", reporter.iso3, period, commodity.hs6)] = _normalize_query_url(
                    build_comtrade_request_url(
                        scope,
                        reporter.iso3,
                        period,
                        commodity.hs6,
                    )
                )
    return urls, set(urls)


def _validate_availability_results(
    results: tuple[ComtradeAvailabilityParseResult, ...],
    scope: ComtradeScope,
) -> tuple[dict[str, ComtradeAvailabilityParseResult], list[dict[str, Any]]]:
    if len(results) != scope.data_availability_requests:
        raise ComtradeDataStoreError(
            "availability_scope_mismatch",
            "exactly four Comtrade getDA parse results are required",
        )
    expected_reporters = {reporter.iso3: reporter for reporter in scope.reporters}
    by_reporter: dict[str, ComtradeAvailabilityParseResult] = {}
    evidence: list[dict[str, Any]] = []
    for result in results:
        reporter = expected_reporters.get(result.query_spec.reporter_iso3)
        if (
            reporter is None
            or result.query_spec.reporter_iso3 in by_reporter
            or result.query_spec.reporter_code != reporter.source_code
            or result.query_spec.years != tuple(range(scope.start_year, scope.end_year + 1))
            or result.returned_dataset_count != len(result.records)
        ):
            raise ComtradeDataStoreError(
                "availability_scope_mismatch",
                "Comtrade getDA results must cover each fixed reporter exactly once",
            )
        by_reporter[reporter.iso3] = result
        for record in result.records:
            if (
                record.reporter_iso3 != reporter.iso3
                or record.reporter_code != reporter.source_code
                or record.period not in result.query_spec.years
                or record.type_code != "C"
                or record.freq_code != "A"
                or record.classification_search_code != "HS"
                or record.is_original_classification is not True
                or record.classification_code not in scope.actual_classifications
                or not _is_canonical_dataset_code(record.dataset_code)
                or type(record.total_records) is not int
                or record.total_records < 0
                or not isinstance(record.dataset_checksum, str)
                or not record.dataset_checksum
                or record.first_released > record.last_released
            ):
                raise ComtradeDataStoreError(
                    "availability_scope_mismatch",
                    "Comtrade getDA record is outside the fixed identity scope",
                )
            evidence.append(
                {
                    "reporter_code": record.reporter_code,
                    "period": record.period,
                    "dataset_code": record.dataset_code,
                    "classification_code": record.classification_code,
                    "total_records": record.total_records,
                    "dataset_checksum": record.dataset_checksum,
                    "first_released": _canonical_datetime(record.first_released),
                    "last_released": _canonical_datetime(record.last_released),
                }
            )
    if set(by_reporter) != set(expected_reporters):
        raise ComtradeDataStoreError(
            "availability_scope_mismatch",
            "Comtrade getDA results do not cover all fixed reporters",
        )
    evidence.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return by_reporter, evidence


def _validate_metadata_results(
    results: tuple[ComtradeMetadataParseResult, ...],
    scope: ComtradeScope,
) -> tuple[dict[str, ComtradeMetadataParseResult], list[dict[str, Any]]]:
    if len(results) != scope.metadata_requests:
        raise ComtradeDataStoreError(
            "metadata_scope_mismatch",
            "exactly four Comtrade metadata parse results are required",
        )
    expected_reporters = {reporter.iso3: reporter for reporter in scope.reporters}
    by_reporter: dict[str, ComtradeMetadataParseResult] = {}
    evidence: list[dict[str, Any]] = []
    for result in results:
        reporter = expected_reporters.get(result.query_spec.reporter_iso3)
        if (
            reporter is None
            or result.query_spec.reporter_iso3 in by_reporter
            or result.query_spec.reporter_code != reporter.source_code
            or result.query_spec.years != tuple(range(scope.start_year, scope.end_year + 1))
        ):
            raise ComtradeDataStoreError(
                "metadata_scope_mismatch",
                "Comtrade metadata results must cover each fixed reporter exactly once",
            )
        by_reporter[reporter.iso3] = result
        dataset_codes: set[str] = set()
        for note in result.notes:
            if (
                note.reporter_iso3 != reporter.iso3
                or note.reporter_code != reporter.source_code
                or note.period not in result.query_spec.years
                or note.classification_code not in scope.actual_classifications
                or not _is_canonical_dataset_code(note.dataset_code)
            ):
                raise ComtradeDataStoreError(
                    "metadata_scope_mismatch",
                    "Comtrade metadata note is outside the fixed identity scope",
                )
            dataset_codes.add(note.dataset_code)
            evidence.append(
                {
                    "reporter_code": note.reporter_code,
                    "period": note.period,
                    "dataset_code": note.dataset_code,
                    "classification_code": note.classification_code,
                    "currency": note.currency,
                    "publication_date": note.publication_date.isoformat(),
                }
            )
        if len(dataset_codes) != result.returned_dataset_count:
            raise ComtradeDataStoreError(
                "metadata_count_mismatch",
                "Comtrade metadata dataset count does not match unique parsed datasets",
            )
    if set(by_reporter) != set(expected_reporters):
        raise ComtradeDataStoreError(
            "metadata_scope_mismatch",
            "Comtrade metadata results do not cover all fixed reporters",
        )
    evidence.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return by_reporter, evidence


def _availability_identity_records(
    results: Mapping[str, ComtradeAvailabilityParseResult],
) -> dict[tuple[int, int, str], ComtradeAvailabilityRecord]:
    candidates: dict[tuple[int, int, str], list[ComtradeAvailabilityRecord]] = {}
    for result in results.values():
        for record in result.records:
            key = (record.reporter_code, record.period, record.classification_code)
            candidates.setdefault(key, []).append(record)
    if any(len(records) != 1 for records in candidates.values()):
        raise ComtradeDataStoreError(
            "availability_identity_ambiguous",
            "each reporter-period-classification must resolve to one getDA dataset",
        )
    return {key: records[0] for key, records in candidates.items()}


def _validate_public_identity_contract(
    identity_records: Mapping[tuple[int, int, str], ComtradeAvailabilityRecord],
    metadata_by_reporter: Mapping[str, ComtradeMetadataParseResult],
) -> None:
    metadata_by_code = {result.query_spec.reporter_code: result for result in metadata_by_reporter.values()}
    for record in identity_records.values():
        metadata = metadata_by_code.get(record.reporter_code)
        candidates = {
            note.dataset_code
            for note in metadata.notes
            if metadata is not None
            if note.reporter_code == record.reporter_code
            and note.period == record.period
            and note.classification_code == record.classification_code
        }
        if candidates != {record.dataset_code}:
            raise ComtradeDataStoreError(
                "public_identity_mismatch",
                "getDA identity is not uniquely verified by metadata",
            )


def _validate_data_result_set(
    results: tuple[ComtradeDataParseResult, ...],
    scope: ComtradeScope,
) -> dict[tuple[str, int, str], ComtradeDataParseResult]:
    if len(results) != scope.request_shards:
        raise ComtradeDataStoreError(
            "data_result_scope_mismatch",
            "exactly 96 Comtrade Data parse results are required",
        )
    reporter_codes = {reporter.iso3: reporter.source_code for reporter in scope.reporters}
    expected_keys = {
        (reporter.iso3, period, commodity.hs6)
        for reporter in scope.reporters
        for period in range(scope.start_year, scope.end_year + 1)
        for commodity in scope.commodities
    }
    by_key: dict[tuple[str, int, str], ComtradeDataParseResult] = {}
    for result in results:
        query = result.query_spec
        key = (query.reporter_iso3, query.period, query.commodity_code)
        if (
            key not in expected_keys
            or key in by_key
            or query.reporter_code != reporter_codes.get(query.reporter_iso3)
            or query.partner_codes != tuple(partner.source_code for partner in scope.partners)
            or query.flow_codes != scope.flows
            or query.partner2_code != 0
            or query.customs_code != "C00"
            or query.mot_code != 0
            or result.returned_row_count != len(result.observations)
        ):
            raise ComtradeDataStoreError(
                "data_result_scope_mismatch",
                "Comtrade Data results must cover each fixed shard exactly once",
            )
        by_key[key] = result
    if set(by_key) != expected_keys:
        raise ComtradeDataStoreError(
            "data_result_scope_mismatch",
            "Comtrade Data results do not cover all 96 fixed shards",
        )
    return by_key


def _validate_data_observation(
    observation: ComtradeDataObservation,
    *,
    result: ComtradeDataParseResult,
    availability: ComtradeAvailabilityParseResult,
    metadata: ComtradeMetadataParseResult,
    comtrade: ComtradeScope,
) -> None:
    query = result.query_spec
    if (
        observation.reporter_iso3 != query.reporter_iso3
        or observation.reporter_code != query.reporter_code
        or observation.period != query.period
        or observation.commodity_code != query.commodity_code
        or observation.partner_code not in query.partner_codes
        or observation.partner2_code != query.partner2_code
        or observation.flow_code not in query.flow_codes
        or observation.customs_code != query.customs_code
        or observation.mot_code != query.mot_code
        or observation.type_code != "C"
        or observation.freq_code != "A"
        or observation.classification_search_code != "HS"
        or observation.classification_code not in comtrade.actual_classifications
        or observation.dataset_binding_source
        not in {
            "availability_unique_metadata_verified",
            "data_row_availability_metadata_verified",
        }
    ):
        raise ComtradeDataStoreError(
            "observation_scope_mismatch",
            "Comtrade Data observation dimensions are outside its parsed shard",
        )
    availability_matches = [
        record
        for record in availability.records
        if record.reporter_code == observation.reporter_code
        and record.period == observation.period
        and record.classification_code == observation.classification_code
    ]
    if len(availability_matches) != 1:
        raise ComtradeDataStoreError(
            "observation_identity_mismatch",
            "Comtrade observation has no unique getDA identity",
        )
    record = availability_matches[0]
    if (
        observation.dataset_code != record.dataset_code
        or observation.dataset_checksum != record.dataset_checksum
        or observation.dataset_first_released != record.first_released
        or observation.dataset_last_released != record.last_released
    ):
        raise ComtradeDataStoreError(
            "observation_identity_mismatch",
            "Comtrade observation dataset evidence differs from getDA",
        )
    publication_dates = tuple(
        note.publication_date
        for note in metadata.notes
        if note.reporter_code == observation.reporter_code
        and note.period == observation.period
        and note.classification_code == observation.classification_code
        and note.dataset_code == observation.dataset_code
    )
    if not publication_dates or tuple(sorted(publication_dates)) != tuple(
        sorted(observation.metadata_publication_dates)
    ):
        raise ComtradeDataStoreError(
            "observation_identity_mismatch",
            "Comtrade observation metadata evidence differs from the public identity response",
        )


def _normalize_returned_row(
    observation: ComtradeDataObservation,
    *,
    source_url: str,
    comtrade: ComtradeScope,
) -> tuple[_NormalizedObservation, ...]:
    partner_iso3 = _partner_iso3(comtrade, observation.partner_code)
    evidence = {
        "dataset_binding_source": observation.dataset_binding_source,
        "dataset_checksum": observation.dataset_checksum,
        "dataset_first_released": _canonical_datetime(observation.dataset_first_released),
        "dataset_last_released": _canonical_datetime(observation.dataset_last_released),
        "metadata_publication_dates": sorted(
            publication_date.isoformat() for publication_date in observation.metadata_publication_dates
        ),
        "quantity_unit_code": observation.quantity_unit_code,
        "quantity_unit_abbreviation": observation.quantity_unit_abbreviation,
    }
    metrics = (
        ("trade_value", observation.primary_value, "USD", "USD", None),
        ("net_weight", observation.net_weight, "kg", None, None),
        (
            "quantity",
            observation.quantity,
            observation.quantity_unit_abbreviation,
            None,
            None,
        ),
    )
    return tuple(
        _normalized_observation(
            country_iso3=observation.reporter_iso3,
            partner_iso3=partner_iso3,
            source_dataset_code=observation.dataset_code,
            commodity_classification=observation.classification_code,
            commodity_code=observation.commodity_code,
            trade_flow=observation.flow_code,
            partner2_code=str(observation.partner2_code),
            customs_code=observation.customs_code,
            mot_code=str(observation.mot_code),
            frequency=comtrade.frequency,
            period=observation.period,
            metric_code=metric_code,
            value=value,
            unit=unit,
            currency=currency,
            price_basis=price_basis,
            source_url=source_url,
            source_release_date=observation.dataset_last_released.date(),
            source_status="reported" if observation.is_reported else "not_reported",
            missing_reason=None if value is not None else "source_null",
            is_reported=observation.is_reported,
            is_aggregate=observation.is_aggregate,
            quality_flags=evidence,
            dataset_key=comtrade.dataset_key,
        )
        for metric_code, value, unit, currency, price_basis in metrics
    )


def _normalize_not_returned_cell(
    *,
    reporter_iso3: str,
    partner_iso3: str,
    period: int,
    commodity_code: str,
    flow_code: str,
    source_url: str,
    comtrade: ComtradeScope,
) -> tuple[_NormalizedObservation, ...]:
    metrics = (
        ("trade_value", "USD", "USD"),
        ("net_weight", "kg", None),
        ("quantity", None, None),
    )
    return tuple(
        _normalized_observation(
            country_iso3=reporter_iso3,
            partner_iso3=partner_iso3,
            source_dataset_code=None,
            commodity_classification=None,
            commodity_code=commodity_code,
            trade_flow=flow_code,
            partner2_code="0",
            customs_code="C00",
            mot_code="0",
            frequency=comtrade.frequency,
            period=period,
            metric_code=metric_code,
            value=None,
            unit=unit,
            currency=currency,
            price_basis=None,
            source_url=source_url,
            source_release_date=None,
            source_status="not_returned",
            missing_reason="not_returned",
            is_reported=None,
            is_aggregate=None,
            quality_flags={"logical_cell_status": "not_returned"},
            dataset_key=comtrade.dataset_key,
        )
        for metric_code, unit, currency in metrics
    )


def _normalized_observation(
    *,
    country_iso3: str,
    partner_iso3: str,
    source_dataset_code: str | None,
    commodity_classification: str | None,
    commodity_code: str,
    trade_flow: str,
    partner2_code: str,
    customs_code: str,
    mot_code: str,
    frequency: str,
    period: int,
    metric_code: str,
    value: Decimal | None,
    unit: str | None,
    currency: str | None,
    price_basis: str | None,
    source_url: str,
    source_release_date: date | None,
    source_status: str,
    missing_reason: str | None,
    is_reported: bool | None,
    is_aggregate: bool | None,
    quality_flags: dict[str, Any],
    dataset_key: str,
) -> _NormalizedObservation:
    if value is not None and (missing_reason is not None or unit is None):
        raise ComtradeDataStoreError(
            "invalid_metric_state",
            "valued Comtrade metrics require a unit and cannot have a missing reason",
        )
    if value is None and missing_reason not in {"source_null", "not_returned"}:
        raise ComtradeDataStoreError(
            "invalid_metric_state",
            "null Comtrade metrics require source_null or not_returned",
        )
    identity = {
        "dataset_key": dataset_key,
        "country_iso3": country_iso3,
        "partner_iso3": partner_iso3,
        "source_dataset_code": source_dataset_code,
        "commodity_classification": commodity_classification,
        "commodity_code": commodity_code,
        "trade_flow": trade_flow,
        "partner2_code": partner2_code,
        "customs_code": customs_code,
        "mot_code": mot_code,
        "frequency": frequency,
        "period": period,
        "metric_code": metric_code,
    }
    observation_key = _sha256(identity)
    observation_hash = _sha256(
        {
            "identity": identity,
            "value": None if value is None else _canonical_decimal(value),
            "unit": unit,
            "currency": currency,
            "price_basis": price_basis,
            "source_url": source_url,
            "source_release_date": (None if source_release_date is None else source_release_date.isoformat()),
            "source_status": source_status,
            "missing_reason": missing_reason,
            "is_reported": is_reported,
            "is_aggregate": is_aggregate,
            "quality_flags": quality_flags,
        }
    )
    return _NormalizedObservation(
        observation_key=observation_key,
        observation_hash=observation_hash,
        country_iso3=country_iso3,
        partner_iso3=partner_iso3,
        source_dataset_code=source_dataset_code,
        commodity_classification=commodity_classification,
        commodity_code=commodity_code,
        trade_flow=trade_flow,
        partner2_code=partner2_code,
        customs_code=customs_code,
        mot_code=mot_code,
        frequency=frequency,
        period=period,
        metric_code=metric_code,
        value=value,
        unit=unit,
        currency=currency,
        price_basis=price_basis,
        source_url=source_url,
        source_release_date=source_release_date,
        source_status=source_status,
        missing_reason=missing_reason,
        is_reported=is_reported,
        is_aggregate=is_aggregate,
        quality_flags=quality_flags,
    )


def _partner_iso3(scope: ComtradeScope, partner_code: int) -> str:
    matches = [partner.iso3 for partner in scope.partners if partner.source_code == partner_code]
    if len(matches) != 1:
        raise ComtradeDataStoreError(
            "partner_reference_mismatch",
            "Comtrade partner code does not resolve to one fixed ISO3 area",
        )
    return matches[0]


def _validate_manifest(
    value: Mapping[str, Any],
    *,
    scope: StructuredScope,
    expected_urls: Mapping[tuple[str, str, int | None, str | None], str],
    expected_keys: set[tuple[str, str, int | None, str | None]],
    result_counts: Mapping[tuple[str, str, int | None, str | None], int],
    retrieved_at: datetime,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"scope_id", "scope_sha256", "queries"}:
        raise ComtradeDataStoreError(
            "invalid_query_manifest",
            "Comtrade query manifest must contain scope_id, scope_sha256, and queries only",
        )
    if value["scope_id"] != scope.scope_id or value["scope_sha256"] != scope.scope_sha256:
        raise ComtradeDataStoreError(
            "invalid_query_manifest",
            "Comtrade query manifest scope evidence does not match the loaded scope",
        )
    queries = value["queries"]
    if not isinstance(queries, list) or len(queries) != 104:
        raise ComtradeDataStoreError(
            "invalid_query_manifest",
            "Comtrade query manifest must contain exactly 104 query records",
        )

    normalized_queries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None, str | None]] = set()
    for index, query in enumerate(queries):
        if not isinstance(query, Mapping) or set(query) != _MANIFEST_QUERY_KEYS:
            raise ComtradeDataStoreError(
                "invalid_query_manifest",
                f"Comtrade query manifest record {index} has unexpected fields",
            )
        key = (
            query["kind"],
            query["reporter_iso3"],
            query["period"],
            query["commodity_code"],
        )
        if key not in expected_keys or key in seen:
            raise ComtradeDataStoreError(
                "invalid_query_manifest",
                "Comtrade query manifest does not identify each fixed request exactly once",
            )
        seen.add(key)
        expected_url = expected_urls[key]
        if (
            _normalize_query_url(query["request_url"]) != expected_url
            or _normalize_query_url(query["final_url"]) != expected_url
        ):
            raise ComtradeDataStoreError(
                "invalid_query_manifest",
                "Comtrade manifest request and final URLs must match the fixed request",
            )
        if type(query["http_status"]) is not int or query["http_status"] != 200:
            raise ComtradeDataStoreError(
                "invalid_query_manifest",
                "Comtrade query manifest HTTP status must be 200",
            )
        resource_sha256 = query["resource_sha256"]
        if (
            not isinstance(resource_sha256, str)
            or len(resource_sha256) != 64
            or any(character not in "0123456789abcdef" for character in resource_sha256)
        ):
            raise ComtradeDataStoreError(
                "invalid_query_manifest",
                "Comtrade query manifest resource hash must be lowercase SHA256",
            )
        fetched_at = _manifest_datetime(query["fetched_at"])
        if fetched_at > retrieved_at:
            raise ComtradeDataStoreError(
                "invalid_query_manifest",
                "Comtrade query manifest fetch time cannot exceed retrieved_at",
            )
        if type(query["returned_row_count"]) is not int or query["returned_row_count"] != result_counts[key]:
            raise ComtradeDataStoreError(
                "invalid_query_manifest",
                "Comtrade query manifest count does not match its parsed result",
            )
        normalized_queries.append(
            {
                "kind": key[0],
                "reporter_iso3": key[1],
                "period": key[2],
                "commodity_code": key[3],
                "request_url": expected_url,
                "final_url": expected_url,
                "http_status": 200,
                "resource_sha256": resource_sha256,
                "fetched_at": fetched_at.isoformat(),
                "returned_row_count": query["returned_row_count"],
            }
        )
    if seen != expected_keys:
        raise ComtradeDataStoreError(
            "invalid_query_manifest",
            "Comtrade query manifest is incomplete",
        )
    normalized_queries.sort(
        key=lambda item: (
            item["kind"],
            item["reporter_iso3"],
            -1 if item["period"] is None else item["period"],
            "" if item["commodity_code"] is None else item["commodity_code"],
        )
    )
    return {
        "scope_id": scope.scope_id,
        "scope_sha256": scope.scope_sha256,
        "queries": normalized_queries,
    }


def _load_owner(
    session: Session,
    payload: ComtradeCollectionPayload,
) -> tuple[SourceChannel, CollectionRun]:
    if type(payload.channel_id) is not int or payload.channel_id <= 0:
        raise ComtradeDataStoreError("invalid_channel", "channel_id must be a positive integer")
    if type(payload.collection_run_id) is not int or payload.collection_run_id <= 0:
        raise ComtradeDataStoreError(
            "invalid_collection_run",
            "collection_run_id must be a positive integer",
        )
    channel = session.get(SourceChannel, payload.channel_id)
    run = session.get(CollectionRun, payload.collection_run_id)
    if channel is None or run is None:
        raise ComtradeDataStoreError(
            "owner_not_found",
            "Comtrade collection channel or run does not exist",
        )
    config = channel.collector_config
    if (
        run.channel_id != channel.id
        or channel.collector_type != "api"
        or run.status != "running"
        or config.get("profile") != _PROFILE
        or config.get("scope_id") != payload.scope.scope_id
        or config.get("dataset_key") != payload.scope.comtrade.dataset_key
    ):
        raise ComtradeDataStoreError(
            "owner_state_mismatch",
            "Comtrade collection requires its reviewed API profile and a running owned run",
        )
    return channel, run


def _load_or_create_dataset(
    session: Session,
    channel: SourceChannel,
    scope: StructuredScope,
) -> StructuredDataset:
    comtrade = scope.comtrade
    by_channel = session.scalar(
        select(StructuredDataset).where(StructuredDataset.channel_id == channel.id).with_for_update()
    )
    by_key = session.scalar(
        select(StructuredDataset)
        .where(StructuredDataset.dataset_key == comtrade.dataset_key)
        .with_for_update()
    )
    if by_channel is not None and by_key is not None and by_channel.id != by_key.id:
        raise ComtradeDataStoreError(
            "dataset_channel_conflict",
            "Comtrade dataset key and channel resolve to different datasets",
        )
    dataset = by_channel or by_key
    if dataset is None:
        dataset = StructuredDataset(
            channel_id=channel.id,
            dataset_key=comtrade.dataset_key,
            name=comtrade.name,
            scope_id=scope.scope_id,
            frequency=comtrade.frequency,
        )
        session.add(dataset)
        session.flush()
        return dataset
    if (
        dataset.channel_id != channel.id
        or dataset.dataset_key != comtrade.dataset_key
        or dataset.name != comtrade.name
        or dataset.scope_id != scope.scope_id
        or dataset.frequency != comtrade.frequency
    ):
        raise ComtradeDataStoreError(
            "dataset_channel_conflict",
            "existing Comtrade dataset does not match the reviewed channel and scope",
        )
    return dataset


def _load_dataset_state(session: Session, dataset: StructuredDataset) -> _DatasetState:
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
    if bool(observations) != (latest_snapshot is not None):
        raise ComtradeDataStoreError(
            "incomplete_dataset_state",
            "Comtrade observations and snapshots must exist together",
        )
    if not observations:
        return _DatasetState((), {}, None, {})

    observation_by_id = {observation.id: observation for observation in observations}
    versions = tuple(
        session.scalars(
            select(StructuredObservationVersion).where(
                StructuredObservationVersion.observation_id.in_(observation_by_id)
            )
        )
    )
    latest_versions: dict[int, StructuredObservationVersion] = {}
    for version in versions:
        observation = observation_by_id[version.observation_id]
        if version.version_no == observation.latest_version_no:
            if version.observation_id in latest_versions:
                raise ComtradeDataStoreError(
                    "incomplete_dataset_state",
                    "Comtrade observation has duplicate declared latest versions",
                )
            latest_versions[version.observation_id] = version
    if set(latest_versions) != set(observation_by_id):
        raise ComtradeDataStoreError(
            "incomplete_dataset_state",
            "every Comtrade observation must have its declared latest version",
        )

    assert latest_snapshot is not None
    members = tuple(
        session.scalars(
            select(StructuredSnapshotObservation).where(
                StructuredSnapshotObservation.snapshot_id == latest_snapshot.id
            )
        )
    )
    if len(members) != latest_snapshot.expected_count:
        raise ComtradeDataStoreError(
            "incomplete_dataset_state",
            "latest Comtrade snapshot membership count is incomplete",
        )
    version_by_id = {version.id: version for version in versions}
    latest_member_versions: dict[int, StructuredObservationVersion] = {}
    for member in members:
        observation = observation_by_id.get(member.observation_id)
        version = version_by_id.get(member.observation_version_id)
        if observation is None or version is None or version.observation_id != observation.id:
            raise ComtradeDataStoreError(
                "incomplete_dataset_state",
                "latest Comtrade snapshot member does not bind an owned observation version",
            )
        latest_member_versions[observation.id] = version
    if len(latest_member_versions) != len(members):
        raise ComtradeDataStoreError(
            "incomplete_dataset_state",
            "latest Comtrade snapshot contains duplicate observation members",
        )
    return _DatasetState(
        observations=observations,
        latest_versions=latest_versions,
        latest_snapshot=latest_snapshot,
        latest_member_versions=latest_member_versions,
    )


def _reuse_snapshot(
    session: Session,
    dataset: StructuredDataset,
    state: _DatasetState,
    normalized: _NormalizedCollection,
) -> ComtradeCollectionResult:
    snapshot = state.latest_snapshot
    if snapshot is None:
        raise ComtradeDataStoreError(
            "snapshot_state_mismatch",
            "cannot reuse a missing Comtrade snapshot",
        )
    by_key = {observation.observation_key: observation for observation in state.observations}
    current_keys = {item.observation_key for item in normalized.observations}
    member_ids = set(state.latest_member_versions)
    current_ids = {by_key[key].id for key in current_keys if key in by_key}
    if set(by_key).intersection(current_keys) != current_keys or current_ids != member_ids:
        raise ComtradeDataStoreError(
            "snapshot_state_mismatch",
            "latest Comtrade snapshot identity set does not match the payload",
        )
    for item in normalized.observations:
        observation = by_key[item.observation_key]
        _assert_observation_identity(observation, item)
        version = state.latest_member_versions.get(observation.id)
        if version is None or version.observation_hash != item.observation_hash:
            raise ComtradeDataStoreError(
                "snapshot_state_mismatch",
                "latest Comtrade snapshot member version does not reproduce the payload",
            )
        observation.last_seen_at = normalized.retrieved_at
    session.flush()
    return _result(
        dataset=dataset,
        snapshot=snapshot,
        normalized=normalized,
        snapshot_created=False,
        observations_created=0,
        versions_created=0,
        versions_unchanged=len(normalized.observations),
    )


def _same_snapshot(snapshot: StructuredSnapshot, normalized: _NormalizedCollection) -> bool:
    return (
        snapshot.query_signature == normalized.query_signature
        and snapshot.snapshot_hash == normalized.snapshot_hash
    )


def _assert_snapshot_matches(
    snapshot: StructuredSnapshot,
    normalized: _NormalizedCollection,
) -> None:
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
    if (
        not _same_snapshot(snapshot, normalized)
        or counts != expected
        or snapshot.provider_version != normalized.provider_version
    ):
        raise ComtradeDataStoreError(
            "collection_run_snapshot_conflict",
            "collection run already has a different Comtrade snapshot",
        )


def _assert_observation_identity(
    observation: StructuredObservation,
    item: _NormalizedObservation,
) -> None:
    if (
        observation.country_iso3 != item.country_iso3
        or observation.partner_iso3 != item.partner_iso3
        or observation.indicator_code is not None
        or observation.commodity_classification != item.commodity_classification
        or observation.source_dataset_code != item.source_dataset_code
        or observation.commodity_code != item.commodity_code
        or observation.trade_flow != item.trade_flow
        or observation.partner2_code != item.partner2_code
        or observation.customs_code != item.customs_code
        or observation.mot_code != item.mot_code
        or observation.frequency != item.frequency
        or observation.period != item.period
        or observation.metric_code != item.metric_code
    ):
        raise ComtradeDataStoreError(
            "observation_identity_conflict",
            "existing Comtrade observation key resolves to different dimensions",
        )


def _result(
    *,
    dataset: StructuredDataset,
    snapshot: StructuredSnapshot,
    normalized: _NormalizedCollection,
    snapshot_created: bool,
    observations_created: int,
    versions_created: int,
    versions_unchanged: int,
) -> ComtradeCollectionResult:
    return ComtradeCollectionResult(
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        query_signature=normalized.query_signature,
        snapshot_hash=normalized.snapshot_hash,
        snapshot_created=snapshot_created,
        observations_created=observations_created,
        versions_created=versions_created,
        versions_unchanged=versions_unchanged,
        expected_count=normalized.expected_count,
        returned_count=normalized.returned_count,
        valued_count=normalized.valued_count,
        source_null_count=normalized.source_null_count,
        not_returned_count=normalized.not_returned_count,
        logical_dimension_count=normalized.logical_dimension_count,
        logical_returned_count=normalized.logical_returned_count,
        logical_not_returned_count=normalized.logical_not_returned_count,
        data_row_count=normalized.data_row_count,
    )


def _normalize_query_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ComtradeDataStoreError("invalid_query_url", "query URL must be non-empty text")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ComtradeDataStoreError("invalid_query_url", "query URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "comtradeapi.un.org"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise ComtradeDataStoreError(
            "invalid_query_url",
            "query URL must remain on the reviewed Comtrade HTTPS origin",
        )
    seen_keys: set[str] = set()
    normalized_query: list[tuple[str, str]] = []
    for key, item in query:
        normalized_key = key.lower().replace("_", "-")
        if normalized_key in _FORBIDDEN_QUERY_KEYS or normalized_key in seen_keys:
            raise ComtradeDataStoreError(
                "invalid_query_url",
                "query URL contains a credential or duplicate parameter",
            )
        seen_keys.add(normalized_key)
        normalized_query.append((key, item))
    netloc = parsed.hostname.lower()
    return urlunsplit(
        (
            "https",
            netloc,
            parsed.path,
            urlencode(sorted(normalized_query), doseq=True, safe=","),
            "",
        )
    )


def _manifest_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ComtradeDataStoreError(
            "invalid_query_manifest",
            "Comtrade manifest fetched_at must be an aware ISO datetime",
        )
    try:
        return _utc_datetime(datetime.fromisoformat(value))
    except (ValueError, ComtradeDataStoreError) as exc:
        raise ComtradeDataStoreError(
            "invalid_query_manifest",
            "Comtrade manifest fetched_at must be an aware ISO datetime",
        ) from exc


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ComtradeDataStoreError(
            "invalid_retrieved_at",
            "Comtrade timestamps must be timezone-aware",
        )
    return value.astimezone(UTC)


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.isoformat()
    return value.astimezone(UTC).isoformat()


def _canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ComtradeDataStoreError(
            "invalid_metric_value",
            "Comtrade metric values must be finite Decimals",
        )
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _is_canonical_dataset_code(value: Any) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit() and value[0] != "0"


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
