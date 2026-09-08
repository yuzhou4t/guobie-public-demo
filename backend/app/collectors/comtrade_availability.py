from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.collectors.base import FetchedResource
from app.collectors.comtrade_metadata import (
    COMTRADE_CLASSIFICATIONS,
    COMTRADE_REPORTERS,
    COMTRADE_YEARS,
)

_INTEGER_PATTERN = re.compile(r"0|[1-9]\d*")
_MISSING = object()


class ComtradeAvailabilityParseError(ValueError):
    """A Comtrade availability response violates the fixed MVP contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ComtradeAvailabilityQuerySpec:
    reporter_iso3: str
    reporter_code: int
    years: tuple[int, ...] = COMTRADE_YEARS

    def __post_init__(self) -> None:
        expected_code = COMTRADE_REPORTERS.get(self.reporter_iso3)
        if expected_code is None or type(self.reporter_code) is not int:
            raise ComtradeAvailabilityParseError(
                "invalid_availability_reporter",
                "UN Comtrade availability reporter is outside the reviewed four-country scope",
            )
        if self.reporter_code != expected_code:
            raise ComtradeAvailabilityParseError(
                "invalid_availability_reporter",
                "UN Comtrade availability reporter code does not match the official reference",
            )
        if self.years != COMTRADE_YEARS:
            raise ComtradeAvailabilityParseError(
                "invalid_availability_years",
                "UN Comtrade availability years must cover 2017 through 2024",
            )


@dataclass(frozen=True, slots=True)
class ComtradeAvailabilityRecord:
    reporter_iso3: str
    reporter_code: int
    period: int
    dataset_code: str
    type_code: str
    freq_code: str
    classification_code: str
    classification_search_code: str
    is_original_classification: bool
    total_records: int
    dataset_checksum: str
    first_released: datetime
    last_released: datetime


@dataclass(frozen=True, slots=True)
class ComtradeAvailabilityParseResult:
    query_spec: ComtradeAvailabilityQuerySpec
    returned_dataset_count: int
    records: tuple[ComtradeAvailabilityRecord, ...]


def parse_comtrade_availability(
    resource: FetchedResource,
    query_spec: ComtradeAvailabilityQuerySpec,
) -> ComtradeAvailabilityParseResult:
    try:
        payload = json.loads(
            resource.body,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError) as exc:
        raise ComtradeAvailabilityParseError(
            "invalid_availability_json",
            "UN Comtrade availability response is not valid finite JSON",
        ) from exc
    if not isinstance(payload, Mapping):
        raise ComtradeAvailabilityParseError(
            "invalid_availability_envelope",
            "UN Comtrade availability response must be an object",
        )
    error = payload.get("error", _MISSING)
    if not isinstance(error, str) or error:
        raise ComtradeAvailabilityParseError(
            "availability_api_error",
            "UN Comtrade availability response contains an error",
        )
    rows = payload.get("data", _MISSING)
    if not isinstance(rows, list):
        raise ComtradeAvailabilityParseError(
            "invalid_availability_rows",
            "UN Comtrade availability data field must be an array",
        )
    count = _integer(payload.get("count", _MISSING))
    if count is None or count != len(rows):
        raise ComtradeAvailabilityParseError(
            "invalid_availability_count",
            "UN Comtrade availability count must equal the number of data records",
        )

    records: list[ComtradeAvailabilityRecord] = []
    seen_datasets: dict[str, ComtradeAvailabilityRecord] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ComtradeAvailabilityParseError(
                "invalid_availability_row",
                f"UN Comtrade availability row {index} must be an object",
            )
        reporter_code = _required_integer(row, "reporterCode", index)
        if reporter_code != query_spec.reporter_code:
            raise ComtradeAvailabilityParseError(
                "availability_reporter_out_of_scope",
                f"UN Comtrade availability row {index} belongs to an unexpected reporter",
            )
        period = _required_integer(row, "period", index)
        if period not in query_spec.years:
            raise ComtradeAvailabilityParseError(
                "availability_period_out_of_scope",
                f"UN Comtrade availability row {index} belongs to a year outside the baseline",
            )
        if (
            row.get("typeCode") != "C"
            or row.get("freqCode") != "A"
            or row.get("classificationSearchCode") != "HS"
            or row.get("isOriginalClassification") is not True
        ):
            raise ComtradeAvailabilityParseError(
                "availability_dataset_out_of_scope",
                f"UN Comtrade availability row {index} is not original annual goods HS data",
            )
        classification_code = row.get("classificationCode", _MISSING)
        if classification_code not in COMTRADE_CLASSIFICATIONS:
            raise ComtradeAvailabilityParseError(
                "availability_classification_out_of_scope",
                "UN Comtrade availability classification is outside verified H0-H6 references",
            )

        dataset_code = _positive_integer_text(row.get("datasetCode", _MISSING), index)
        total_records = _required_integer(row, "totalRecords", index)
        if total_records < 0:
            raise ComtradeAvailabilityParseError(
                "invalid_availability_total_records",
                f"UN Comtrade availability row {index} totalRecords must be non-negative",
            )
        dataset_checksum = _checksum(row.get("datasetChecksum", _MISSING), index)
        first_released = _release_time(row.get("firstReleased", _MISSING), index)
        last_released = _release_time(row.get("lastReleased", _MISSING), index)
        if (first_released.utcoffset() is None) != (last_released.utcoffset() is None):
            raise ComtradeAvailabilityParseError(
                "invalid_availability_release_time",
                "UN Comtrade availability release times must use compatible timezone forms",
            )
        if first_released > last_released:
            raise ComtradeAvailabilityParseError(
                "invalid_availability_release_order",
                "UN Comtrade availability first release must not be after its last release",
            )

        record = ComtradeAvailabilityRecord(
            reporter_iso3=query_spec.reporter_iso3,
            reporter_code=reporter_code,
            period=period,
            dataset_code=dataset_code,
            type_code="C",
            freq_code="A",
            classification_code=classification_code,
            classification_search_code="HS",
            is_original_classification=True,
            total_records=total_records,
            dataset_checksum=dataset_checksum,
            first_released=first_released,
            last_released=last_released,
        )
        previous = seen_datasets.get(dataset_code)
        if previous is not None:
            if previous == record:
                raise ComtradeAvailabilityParseError(
                    "duplicate_availability_record",
                    "UN Comtrade availability repeats the same dataset record",
                )
            raise ComtradeAvailabilityParseError(
                "availability_dataset_conflict",
                "UN Comtrade availability datasetCode has conflicting fields",
            )
        seen_datasets[dataset_code] = record
        records.append(record)

    records.sort(
        key=lambda item: (
            item.period,
            item.classification_code,
            item.dataset_code,
        )
    )
    return ComtradeAvailabilityParseResult(
        query_spec=query_spec,
        returned_dataset_count=count,
        records=tuple(records),
    )


def resolve_comtrade_availability_identity(
    availability: ComtradeAvailabilityParseResult,
    *,
    reporter_code: int,
    period: int,
    classification_code: str,
) -> ComtradeAvailabilityRecord:
    if reporter_code != availability.query_spec.reporter_code:
        raise ComtradeAvailabilityParseError(
            "availability_identity_reporter_mismatch",
            "UN Comtrade data row reporter does not match its availability response",
        )
    if type(period) is not int or period not in availability.query_spec.years:
        raise ComtradeAvailabilityParseError(
            "availability_identity_period_invalid",
            "UN Comtrade data row period is outside its availability response",
        )
    if classification_code not in COMTRADE_CLASSIFICATIONS:
        raise ComtradeAvailabilityParseError(
            "availability_identity_classification_invalid",
            "UN Comtrade data row classification is outside the reviewed references",
        )

    matches = tuple(
        record
        for record in availability.records
        if record.reporter_code == reporter_code
        and record.period == period
        and record.classification_code == classification_code
    )
    if not matches:
        raise ComtradeAvailabilityParseError(
            "availability_identity_missing",
            "UN Comtrade availability has no dataset identity for the data row",
        )
    if len(matches) > 1:
        raise ComtradeAvailabilityParseError(
            "availability_identity_ambiguous",
            "UN Comtrade availability has multiple dataset identities for the data row",
        )
    return matches[0]


def _required_integer(row: Mapping[str, Any], field_name: str, index: int) -> int:
    value = _integer(row.get(field_name, _MISSING))
    if value is None:
        raise ComtradeAvailabilityParseError(
            "invalid_availability_integer",
            f"UN Comtrade availability row {index} field {field_name} must be an integer",
        )
    return value


def _positive_integer_text(value: Any, index: int) -> str:
    parsed = _integer(value)
    if parsed is None or parsed <= 0:
        raise ComtradeAvailabilityParseError(
            "invalid_availability_dataset_code",
            f"UN Comtrade availability row {index} datasetCode must be a positive integer",
        )
    return str(parsed)


def _checksum(value: Any, index: int) -> str:
    if isinstance(value, str):
        if value and value == value.strip():
            return value
    elif isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise ComtradeAvailabilityParseError(
        "invalid_availability_checksum",
        f"UN Comtrade availability row {index} datasetChecksum must be non-empty and finite",
    )


def _release_time(value: Any, index: int) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip() or "T" not in value:
        raise ComtradeAvailabilityParseError(
            "invalid_availability_release_time",
            f"UN Comtrade availability row {index} release time must be an ISO datetime",
        )
    try:
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ComtradeAvailabilityParseError(
            "invalid_availability_release_time",
            f"UN Comtrade availability row {index} release time must be an ISO datetime",
        ) from exc


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return int(value)
        return None
    if isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value):
        return int(value)
    return None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")
