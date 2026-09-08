from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.collectors.base import FetchedResource

COMTRADE_YEARS = tuple(range(2017, 2025))
COMTRADE_REPORTERS = {
    "COD": 180,
    "ZWE": 716,
    "ZMB": 894,
    "ZAF": 710,
}
COMTRADE_CLASSIFICATIONS = frozenset({"H0", "H1", "H2", "H3", "H4", "H5", "H6"})

_INTEGER_PATTERN = re.compile(r"0|[1-9]\d*")
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")
_MISSING = object()


class ComtradeMetadataParseError(ValueError):
    """A Comtrade metadata response violates the fixed MVP contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ComtradeMetadataQuerySpec:
    reporter_iso3: str
    reporter_code: int
    years: tuple[int, ...] = COMTRADE_YEARS

    def __post_init__(self) -> None:
        expected_code = COMTRADE_REPORTERS.get(self.reporter_iso3)
        if expected_code is None or type(self.reporter_code) is not int:
            raise ComtradeMetadataParseError(
                "invalid_metadata_reporter",
                "UN Comtrade metadata reporter is outside the reviewed four-country scope",
            )
        if self.reporter_code != expected_code:
            raise ComtradeMetadataParseError(
                "invalid_metadata_reporter",
                "UN Comtrade metadata reporter code does not match the official reference",
            )
        if self.years != COMTRADE_YEARS:
            raise ComtradeMetadataParseError(
                "invalid_metadata_years",
                "UN Comtrade metadata years must cover 2017 through 2024",
            )


@dataclass(frozen=True, slots=True)
class ComtradeDatasetMetadata:
    reporter_iso3: str
    reporter_code: int
    period: int
    dataset_code: str
    classification_code: str
    currency: str
    publication_date: date


@dataclass(frozen=True, slots=True)
class ComtradeMetadataParseResult:
    query_spec: ComtradeMetadataQuerySpec
    returned_dataset_count: int
    notes: tuple[ComtradeDatasetMetadata, ...]
    unavailable_years: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ResolvedComtradeDatasetIdentity:
    dataset_code: str
    binding_source: str
    publication_notes: tuple[ComtradeDatasetMetadata, ...]


class ComtradeMetadataCollector:
    """Parse one already-fetched official Comtrade metadata response."""

    def parse(
        self,
        resource: FetchedResource,
        query_spec: ComtradeMetadataQuerySpec,
    ) -> ComtradeMetadataParseResult:
        return parse_comtrade_metadata(resource, query_spec)


def parse_comtrade_metadata(
    resource: FetchedResource,
    query_spec: ComtradeMetadataQuerySpec,
) -> ComtradeMetadataParseResult:
    try:
        payload = json.loads(
            resource.body,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError) as exc:
        raise ComtradeMetadataParseError(
            "invalid_metadata_json",
            "UN Comtrade metadata response is not valid finite JSON",
        ) from exc
    if not isinstance(payload, Mapping):
        raise ComtradeMetadataParseError(
            "invalid_metadata_envelope",
            "UN Comtrade metadata response must be an object",
        )
    error = payload.get("error", _MISSING)
    if not isinstance(error, str) or error:
        raise ComtradeMetadataParseError(
            "metadata_api_error",
            "UN Comtrade metadata response contains an error",
        )
    rows = payload.get("data", _MISSING)
    if not isinstance(rows, list):
        raise ComtradeMetadataParseError(
            "invalid_metadata_rows",
            "UN Comtrade metadata data field must be an array",
        )
    count = _integer(payload.get("count", _MISSING))
    if count is None or count != len(rows):
        raise ComtradeMetadataParseError(
            "invalid_metadata_count",
            "UN Comtrade metadata count must equal the number of data records",
        )

    notes: list[ComtradeDatasetMetadata] = []
    seen_datasets: dict[str, int] = {}
    seen_notes: set[tuple[str, date, str, str]] = set()
    available_years: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ComtradeMetadataParseError(
                "invalid_metadata_row",
                f"UN Comtrade metadata row {index} must be an object",
            )
        reporter_code = _required_integer(row, "reporterCode", index)
        if reporter_code != query_spec.reporter_code:
            raise ComtradeMetadataParseError(
                "metadata_reporter_out_of_scope",
                f"UN Comtrade metadata row {index} belongs to an unexpected reporter",
            )
        if row.get("typeCode") != "C" or row.get("freqCode") != "A":
            raise ComtradeMetadataParseError(
                "metadata_dataset_out_of_scope",
                f"UN Comtrade metadata row {index} is not annual goods data",
            )
        period = _required_integer(row, "period", index)
        if period not in query_spec.years:
            raise ComtradeMetadataParseError(
                "metadata_period_out_of_scope",
                f"UN Comtrade metadata row {index} belongs to a year outside the baseline",
            )
        dataset_code = _dataset_code(row.get("datasetCode", _MISSING), index)
        previous_period = seen_datasets.setdefault(dataset_code, period)
        if previous_period != period:
            raise ComtradeMetadataParseError(
                "metadata_dataset_conflict",
                "UN Comtrade metadata datasetCode appears under multiple periods",
            )
        raw_notes = row.get("notes", _MISSING)
        if not isinstance(raw_notes, list) or not raw_notes:
            raise ComtradeMetadataParseError(
                "invalid_metadata_notes",
                f"UN Comtrade metadata row {index} must contain at least one note",
            )
        for note_index, note in enumerate(raw_notes):
            if not isinstance(note, Mapping):
                raise ComtradeMetadataParseError(
                    "invalid_metadata_note",
                    f"UN Comtrade metadata row {index} note {note_index} must be an object",
                )
            note_dataset = _dataset_code(note.get("datasetCode", _MISSING), index)
            if note_dataset != dataset_code:
                raise ComtradeMetadataParseError(
                    "metadata_dataset_conflict",
                    "UN Comtrade metadata note datasetCode does not match its data record",
                )
            classification = note.get("classificationCode", _MISSING)
            if classification not in COMTRADE_CLASSIFICATIONS:
                raise ComtradeMetadataParseError(
                    "metadata_classification_out_of_scope",
                    "UN Comtrade metadata classification is outside verified H0-H6 references",
                )
            currency = note.get("currency", _MISSING)
            if not isinstance(currency, str) or _CURRENCY_PATTERN.fullmatch(currency) is None:
                raise ComtradeMetadataParseError(
                    "invalid_metadata_currency",
                    "UN Comtrade metadata currency must be a three-letter uppercase code",
                )
            publication_date = _publication_date(note.get("publicationDate", _MISSING))
            note_key = (dataset_code, publication_date, classification, currency)
            if note_key in seen_notes:
                raise ComtradeMetadataParseError(
                    "duplicate_metadata_note",
                    "UN Comtrade metadata repeats the same dataset publication note",
                )
            seen_notes.add(note_key)
            notes.append(
                ComtradeDatasetMetadata(
                    reporter_iso3=query_spec.reporter_iso3,
                    reporter_code=query_spec.reporter_code,
                    period=period,
                    dataset_code=dataset_code,
                    classification_code=classification,
                    currency=currency,
                    publication_date=publication_date,
                )
            )
        available_years.add(period)

    notes.sort(
        key=lambda item: (
            item.period,
            item.dataset_code,
            item.publication_date,
            item.classification_code,
            item.currency,
        )
    )
    return ComtradeMetadataParseResult(
        query_spec=query_spec,
        returned_dataset_count=count,
        notes=tuple(notes),
        unavailable_years=tuple(year for year in query_spec.years if year not in available_years),
    )


def resolve_comtrade_dataset_identity(
    metadata: ComtradeMetadataParseResult,
    *,
    reporter_code: int,
    period: int,
    classification_code: str,
    explicit_dataset_code: str | None,
) -> ResolvedComtradeDatasetIdentity:
    if reporter_code != metadata.query_spec.reporter_code:
        raise ComtradeMetadataParseError(
            "metadata_dataset_identity_reporter_mismatch",
            "UN Comtrade data row reporter does not match its metadata response",
        )
    if type(period) is not int or period not in metadata.query_spec.years:
        raise ComtradeMetadataParseError(
            "metadata_dataset_identity_period_invalid",
            "UN Comtrade data row period is outside its metadata response",
        )
    if classification_code not in COMTRADE_CLASSIFICATIONS:
        raise ComtradeMetadataParseError(
            "metadata_dataset_identity_classification_invalid",
            "UN Comtrade data row classification is outside the reviewed references",
        )
    if explicit_dataset_code is not None and (
        _INTEGER_PATTERN.fullmatch(explicit_dataset_code) is None or int(explicit_dataset_code) <= 0
    ):
        raise ComtradeMetadataParseError(
            "metadata_dataset_identity_code_invalid",
            "UN Comtrade data row datasetCode must be a positive canonical integer",
        )

    matching_notes = tuple(
        note
        for note in metadata.notes
        if note.reporter_code == reporter_code
        and note.period == period
        and note.classification_code == classification_code
    )
    candidates = {note.dataset_code for note in matching_notes}
    if explicit_dataset_code is not None:
        if explicit_dataset_code not in candidates:
            raise ComtradeMetadataParseError(
                "metadata_dataset_identity_mismatch",
                "UN Comtrade data row datasetCode is not present in matching metadata",
            )
        dataset_code = explicit_dataset_code
        binding_source = "data_row_verified"
    else:
        if not candidates:
            raise ComtradeMetadataParseError(
                "metadata_dataset_identity_missing",
                "UN Comtrade metadata has no dataset identity for the data row",
            )
        if len(candidates) > 1:
            raise ComtradeMetadataParseError(
                "metadata_dataset_identity_ambiguous",
                "UN Comtrade metadata has multiple dataset identities for the data row",
            )
        dataset_code = next(iter(candidates))
        binding_source = "metadata_unique_match"

    return ResolvedComtradeDatasetIdentity(
        dataset_code=dataset_code,
        binding_source=binding_source,
        publication_notes=tuple(note for note in matching_notes if note.dataset_code == dataset_code),
    )


def _required_integer(row: Mapping[str, Any], field_name: str, index: int) -> int:
    value = _integer(row.get(field_name, _MISSING))
    if value is None:
        raise ComtradeMetadataParseError(
            "invalid_metadata_integer",
            f"UN Comtrade metadata row {index} field {field_name} must be an integer",
        )
    return value


def _dataset_code(value: Any, index: int) -> str:
    parsed = _integer(value)
    if parsed is None or parsed <= 0:
        raise ComtradeMetadataParseError(
            "invalid_metadata_dataset_code",
            f"UN Comtrade metadata row {index} datasetCode must be a positive integer",
        )
    return str(parsed)


def _publication_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ComtradeMetadataParseError(
            "invalid_metadata_publication_date",
            "UN Comtrade metadata publicationDate must be an ISO datetime",
        )
    try:
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ComtradeMetadataParseError(
            "invalid_metadata_publication_date",
            "UN Comtrade metadata publicationDate must be an ISO datetime",
        ) from exc
    if "T" not in value:
        raise ComtradeMetadataParseError(
            "invalid_metadata_publication_date",
            "UN Comtrade metadata publicationDate must include a time",
        )
    return parsed.date()


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
