from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.collectors.base import FetchedResource
from app.collectors.comtrade_availability import (
    ComtradeAvailabilityParseError,
    ComtradeAvailabilityParseResult,
    ComtradeAvailabilityQuerySpec,
    parse_comtrade_availability,
)
from app.collectors.comtrade_data import (
    ComtradeDataParseError,
    ComtradeDataQuerySpec,
    parse_comtrade_data,
)
from app.collectors.comtrade_metadata import (
    ComtradeMetadataParseError,
    ComtradeMetadataParseResult,
    ComtradeMetadataQuerySpec,
    parse_comtrade_metadata,
    resolve_comtrade_dataset_identity,
)
from app.core.config import Settings, get_settings
from app.services.robots_policy import check_robots
from app.services.source_probe import ProbeError, SafeHttpClient
from app.services.structured_scope import (
    StructuredScope,
    StructuredScopeValidationError,
    build_comtrade_data_availability_request_url,
    build_comtrade_metadata_request_url,
    build_comtrade_request_url,
    load_structured_scope,
)

_SAMPLE_REPORTER = "ZAF"
_SAMPLE_YEAR = 2024
_SAMPLE_COMMODITY = "260300"


class ComtradeReadinessError(RuntimeError):
    """A readiness stage failed without exposing source or credential details."""

    def __init__(self, code: str, stage: str) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage


def main() -> int:
    try:
        settings = get_settings()
        report, exit_code = run_comtrade_readiness(settings=settings)
    except Exception:
        report = _empty_report()
        report.update(
            {
                "status": "blocked",
                "blocker": "comtrade_readiness_failed",
                "blocked_stage": "startup",
            }
        )
        exit_code = 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


def run_comtrade_readiness(
    *,
    settings: Settings,
    client: SafeHttpClient | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        scope = load_structured_scope()
    except Exception:
        return _blocked(_empty_report(), "comtrade_scope_invalid", "scope")

    report = _base_report(scope)
    try:
        safe_client = client or SafeHttpClient(settings)
        availability_summary, availability_by_reporter = _collect_data_availability(
            safe_client,
            scope,
        )
        report["data_availability"] = availability_summary
        metadata_summary, metadata_by_reporter = _collect_metadata(safe_client, scope)
        report["metadata"] = metadata_summary
    except ComtradeReadinessError as exc:
        return _blocked(report, exc.code, exc.stage)
    except Exception:
        return _blocked(report, "comtrade_public_preflight_failed", "public_preflight")

    try:
        report["dataset_identity_contract"] = _validate_public_dataset_identity_contract(
            availability_by_reporter,
            metadata_by_reporter,
        )
    except ComtradeReadinessError as exc:
        return _blocked(report, exc.code, exc.stage)
    except Exception:
        return _blocked(report, "comtrade_dataset_identity_contract_failed", "dataset_identity")

    if settings.comtrade_subscription_key is None:
        return _blocked(report, "comtrade_subscription_key_missing", "subscription")

    try:
        sample_url = build_comtrade_request_url(
            scope.comtrade,
            _SAMPLE_REPORTER,
            _SAMPLE_YEAR,
            _SAMPLE_COMMODITY,
        )
        _require_robots(safe_client, sample_url, stage="keyed_sample")
        try:
            resource = safe_client.fetch_comtrade(sample_url)
        except ProbeError:
            raise ComtradeReadinessError("comtrade_keyed_sample_fetch_failed", "keyed_sample") from None
        report["keyed_sample"].update(
            {
                "http_status": resource.status_code,
                "content_type": resource.content_type,
                "byte_size": len(resource.body),
                "response_sha256": resource.sha256,
            }
        )
        report["keyed_sample"].update(
            _summarize_keyed_sample(
                resource,
                availability_by_reporter,
                metadata_by_reporter,
            )
        )
    except ComtradeReadinessError as exc:
        return _blocked(report, exc.code, exc.stage)
    except StructuredScopeValidationError:
        return _blocked(report, "comtrade_scope_invalid", "keyed_sample")
    except Exception:
        return _blocked(report, "comtrade_keyed_sample_failed", "keyed_sample")

    report.update(
        {
            "status": "sample_validated",
            "blocker": None,
            "blocked_stage": None,
            "next_step": "comtrade_store_runner_and_shadow_double_run_required",
        }
    )
    return report, 0


def _collect_data_availability(
    client: SafeHttpClient,
    scope: StructuredScope,
) -> tuple[dict[str, Any], dict[int, ComtradeAvailabilityParseResult]]:
    reporters: list[dict[str, Any]] = []
    availability_by_reporter: dict[int, ComtradeAvailabilityParseResult] = {}
    for reporter in scope.comtrade.reporters:
        try:
            url = build_comtrade_data_availability_request_url(scope.comtrade, reporter.iso3)
            _require_robots(client, url, stage="data_availability")
            resource = client.fetch_without_redirects(url)
        except ComtradeReadinessError:
            raise
        except (ProbeError, StructuredScopeValidationError):
            raise ComtradeReadinessError(
                "comtrade_data_availability_fetch_failed",
                "data_availability",
            ) from None

        query = ComtradeAvailabilityQuerySpec(
            reporter_iso3=reporter.iso3,
            reporter_code=reporter.source_code,
        )
        try:
            result = parse_comtrade_availability(resource, query)
        except ComtradeAvailabilityParseError:
            raise ComtradeReadinessError(
                "comtrade_data_availability_invalid",
                "data_availability",
            ) from None
        availability_by_reporter[reporter.source_code] = result

        identity_candidates: dict[tuple[int, int, str], set[str]] = {}
        for record in result.records:
            identity_key = (
                record.reporter_code,
                record.period,
                record.classification_code,
            )
            identity_candidates.setdefault(identity_key, set()).add(record.dataset_code)
        if any(len(dataset_codes) > 1 for dataset_codes in identity_candidates.values()):
            raise ComtradeReadinessError(
                "comtrade_data_availability_identity_ambiguous",
                "data_availability",
            )

        reporters.append(
            {
                "reporter_iso3": reporter.iso3,
                "reporter_code": reporter.source_code,
                "returned_dataset_count": result.returned_dataset_count,
                "identity_count": len(identity_candidates),
                "classification_code_count": len({record.classification_code for record in result.records}),
                "available_year_count": len({record.period for record in result.records}),
            }
        )

    classification_codes = {
        record.classification_code
        for result in availability_by_reporter.values()
        for record in result.records
    }
    return (
        {
            "status": "validated",
            "reporter_count": len(reporters),
            "dataset_count": sum(item["returned_dataset_count"] for item in reporters),
            "identity_count": sum(item["identity_count"] for item in reporters),
            "classification_code_count": len(classification_codes),
            "reporters": reporters,
        },
        availability_by_reporter,
    )


def _collect_metadata(
    client: SafeHttpClient,
    scope: StructuredScope,
) -> tuple[dict[str, Any], dict[int, ComtradeMetadataParseResult]]:
    reporters: list[dict[str, Any]] = []
    metadata_by_reporter: dict[int, ComtradeMetadataParseResult] = {}
    for reporter in scope.comtrade.reporters:
        try:
            url = build_comtrade_metadata_request_url(scope.comtrade, reporter.iso3)
            _require_robots(client, url, stage="metadata")
            resource = client.fetch_without_redirects(url)
        except ComtradeReadinessError:
            raise
        except (ProbeError, StructuredScopeValidationError):
            raise ComtradeReadinessError("comtrade_metadata_fetch_failed", "metadata") from None

        query = ComtradeMetadataQuerySpec(
            reporter_iso3=reporter.iso3,
            reporter_code=reporter.source_code,
        )
        try:
            result = parse_comtrade_metadata(resource, query)
        except ComtradeMetadataParseError:
            raise ComtradeReadinessError("comtrade_metadata_invalid", "metadata") from None
        metadata_by_reporter[reporter.source_code] = result

        reporters.append(
            {
                "reporter_iso3": reporter.iso3,
                "reporter_code": reporter.source_code,
                "returned_dataset_count": result.returned_dataset_count,
                "note_count": len(result.notes),
                "classification_code_count": len({note.classification_code for note in result.notes}),
                "currency_count": len({note.currency for note in result.notes}),
                "unavailable_year_count": len(result.unavailable_years),
            }
        )

    classification_codes = {
        note.classification_code for result in metadata_by_reporter.values() for note in result.notes
    }
    currencies = {note.currency for result in metadata_by_reporter.values() for note in result.notes}
    summary = {
        "status": "validated",
        "reporter_count": len(reporters),
        "dataset_count": sum(item["returned_dataset_count"] for item in reporters),
        "note_count": sum(item["note_count"] for item in reporters),
        "classification_code_count": len(classification_codes),
        "currency_count": len(currencies),
        "reporters": reporters,
    }
    return summary, metadata_by_reporter


def _validate_public_dataset_identity_contract(
    availability_by_reporter: Mapping[int, ComtradeAvailabilityParseResult],
    metadata_by_reporter: Mapping[int, ComtradeMetadataParseResult],
) -> dict[str, Any]:
    matched_datasets: set[tuple[int, str]] = set()
    matched_records = 0
    for reporter_code, availability in availability_by_reporter.items():
        metadata = metadata_by_reporter.get(reporter_code)
        if metadata is None:
            raise ComtradeReadinessError(
                "comtrade_dataset_identity_metadata_missing",
                "dataset_identity",
            )
        for record in availability.records:
            try:
                identity = resolve_comtrade_dataset_identity(
                    metadata,
                    reporter_code=record.reporter_code,
                    period=record.period,
                    classification_code=record.classification_code,
                    explicit_dataset_code=record.dataset_code,
                )
            except ComtradeMetadataParseError:
                raise ComtradeReadinessError(
                    "comtrade_dataset_identity_sources_mismatch",
                    "dataset_identity",
                ) from None
            if identity.dataset_code != record.dataset_code:
                raise ComtradeReadinessError(
                    "comtrade_dataset_identity_sources_mismatch",
                    "dataset_identity",
                )
            matched_datasets.add((record.reporter_code, identity.dataset_code))
            matched_records += 1

    return {
        "status": "validated",
        "sources": ["data_availability", "metadata"],
        "matched_record_count": matched_records,
        "matched_dataset_count": len(matched_datasets),
    }


def _require_robots(client: SafeHttpClient, url: str, *, stage: str) -> None:
    try:
        decision = check_robots(client, url)
        if decision.code in {
            "robots_connection_failed",
            "robots_dns_resolution_failed",
            "robots_timeout",
        }:
            decision = check_robots(client, url)
    except Exception:
        raise ComtradeReadinessError("comtrade_robots_check_failed", stage) from None
    if not decision.allowed:
        raise ComtradeReadinessError("comtrade_robots_blocked", stage)


def _summarize_keyed_sample(
    resource: FetchedResource,
    availability_by_reporter: Mapping[int, ComtradeAvailabilityParseResult],
    metadata_by_reporter: Mapping[int, ComtradeMetadataParseResult],
) -> dict[str, Any]:
    availability = availability_by_reporter.get(710)
    metadata = metadata_by_reporter.get(710)
    if availability is None or metadata is None:
        raise ComtradeReadinessError("comtrade_keyed_sample_invalid", "keyed_sample")
    query_spec = ComtradeDataQuerySpec(
        reporter_iso3=_SAMPLE_REPORTER,
        reporter_code=710,
        period=_SAMPLE_YEAR,
        commodity_code=_SAMPLE_COMMODITY,
    )
    try:
        result = parse_comtrade_data(resource, query_spec, availability, metadata)
    except ComtradeDataParseError as exc:
        blocker_by_code = {
            "data_dataset_identity_missing": "comtrade_keyed_sample_dataset_identity_missing",
            "data_dataset_identity_ambiguous": "comtrade_keyed_sample_dataset_identity_ambiguous",
            "data_dataset_identity_mismatch": "comtrade_keyed_sample_dataset_identity_mismatch",
        }
        raise ComtradeReadinessError(
            blocker_by_code.get(exc.code, "comtrade_keyed_sample_invalid"),
            "keyed_sample",
        ) from None
    if result.returned_row_count <= 0:
        raise ComtradeReadinessError("comtrade_keyed_sample_invalid", "keyed_sample")

    payload = json.loads(resource.body)
    rows = payload["data"]
    missing_dataset_code_count = sum("datasetCode" not in row for row in rows)
    dataset_codes = {observation.dataset_code for observation in result.observations}
    binding_sources = {observation.dataset_binding_source for observation in result.observations}
    classification_codes = {observation.classification_code for observation in result.observations}
    return {
        "status": "validated",
        "credential_accepted": True,
        "formal_data_parser_validated": True,
        "record_count": result.returned_row_count,
        "dataset_code_contract_validated": missing_dataset_code_count == 0,
        "dataset_identity_resolved": True,
        "dataset_identity_sources_validated": ["data_availability", "metadata"],
        "dataset_identity_binding_sources": sorted(binding_sources),
        "response_schema_mismatch_observed": missing_dataset_code_count > 0,
        "fixed_dimensions_validated": True,
        "original_classification_validated": True,
        "dataset_code_present_on_all_rows": missing_dataset_code_count == 0,
        "dataset_code_missing_row_count": missing_dataset_code_count,
        "dataset_code_count": len(dataset_codes),
        "classification_code_count": len(classification_codes),
        "top_level_fields": sorted(payload),
        "first_row_fields": sorted(rows[0]),
    }


def _empty_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "checking",
        "full_collection_ready": False,
        "blocker": None,
        "blocked_stage": None,
        "storage_boundary": {
            "raw_body_saved": False,
            "database_written": False,
        },
    }


def _base_report(scope: StructuredScope) -> dict[str, Any]:
    report = _empty_report()
    report.update(
        {
            "scope": {
                "scope_id": scope.scope_id,
                "file_sha256": scope.scope_sha256,
                "dataset_key": scope.comtrade.dataset_key,
            },
            "metadata": {"status": "pending", "reporters": []},
            "data_availability": {"status": "pending", "reporters": []},
            "dataset_identity_contract": {"status": "pending"},
            "keyed_sample": {
                "status": "not_run",
                "reporter_iso3": _SAMPLE_REPORTER,
                "year": _SAMPLE_YEAR,
                "commodity_code": _SAMPLE_COMMODITY,
            },
        }
    )
    return report


def _blocked(
    report: dict[str, Any],
    blocker: str,
    stage: str,
) -> tuple[dict[str, Any], int]:
    report.update(
        {
            "status": "blocked",
            "blocker": blocker,
            "blocked_stage": stage,
        }
    )
    return report, 1


if __name__ == "__main__":
    raise SystemExit(main())
