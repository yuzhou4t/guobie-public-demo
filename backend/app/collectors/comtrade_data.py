from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.collectors.base import FetchedResource
from app.collectors.comtrade_availability import (
    ComtradeAvailabilityParseError,
    ComtradeAvailabilityParseResult,
    ComtradeAvailabilityRecord,
    resolve_comtrade_availability_identity,
)
from app.collectors.comtrade_metadata import (
    COMTRADE_CLASSIFICATIONS,
    COMTRADE_REPORTERS,
    COMTRADE_YEARS,
    ComtradeMetadataParseError,
    ComtradeMetadataParseResult,
    resolve_comtrade_dataset_identity,
)
from app.services.structured_scope import (
    COMTRADE_COMMODITIES,
    COMTRADE_FLOWS,
    COMTRADE_MAX_RECORDS,
    COMTRADE_PARTNER_AREAS,
)

COMTRADE_COMMODITY_CODES = frozenset(item[1] for item in COMTRADE_COMMODITIES)
COMTRADE_PARTNER_CODES = tuple(item[1] for item in COMTRADE_PARTNER_AREAS)
# Current official reference: https://comtradeapi.un.org/files/v1/app/reference/QuantityUnits.json
COMTRADE_QUANTITY_UNITS = {
    -1: "N/A",
    2: "m²",
    3: "1000 kWh",
    4: "m",
    5: "u",
    6: "2u",
    7: "l",
    8: "kg",
    9: "1000u",
    10: "U (jeu/pack)",
    11: "12u",
    12: "m³",
    13: "carat",
    14: "km",
    15: "g",
    16: "hive",
    17: "1000 m³",
    18: "TJ",
    19: "BBL",
    20: "1000 L",
    21: "1000 KG",
    22: "kWH",
    23: "l alc 100%",
    24: "head",
    25: "kg/net eda",
    26: "kg C5H14ClNO",
    27: "kg P2O5",
    28: "kg H2O2",
    29: "kg met.am.",
    30: "kg N",
    31: "kg KOH",
    32: "kg K2O",
    33: "kg NaOH",
    34: "kg 90% sdt",
    35: "kg U",
    36: "ct/l",
    37: "Bq",
    38: "gi F/S",
    39: "GRT",
    40: "GT",
    41: "ce/el",
}

_INTEGER_PATTERN = re.compile(r"0|[1-9]\d*")
_MISSING = object()


class ComtradeDataParseError(ValueError):
    """A formal Comtrade Data API response violates the fixed MVP contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ComtradeDataQuerySpec:
    reporter_iso3: str
    reporter_code: int
    period: int
    commodity_code: str
    partner_codes: tuple[int, ...] = COMTRADE_PARTNER_CODES
    flow_codes: tuple[str, ...] = COMTRADE_FLOWS
    partner2_code: int = 0
    customs_code: str = "C00"
    mot_code: int = 0

    def __post_init__(self) -> None:
        expected_code = (
            COMTRADE_REPORTERS.get(self.reporter_iso3) if isinstance(self.reporter_iso3, str) else None
        )
        if expected_code is None or type(self.reporter_code) is not int:
            raise ComtradeDataParseError(
                "invalid_data_reporter",
                "UN Comtrade data reporter is outside the reviewed four-country scope",
            )
        if self.reporter_code != expected_code:
            raise ComtradeDataParseError(
                "invalid_data_reporter",
                "UN Comtrade data reporter code does not match the official reference",
            )
        if type(self.period) is not int or self.period not in COMTRADE_YEARS:
            raise ComtradeDataParseError(
                "invalid_data_period",
                "UN Comtrade data period must be one year from 2017 through 2024",
            )
        if not isinstance(self.commodity_code, str) or self.commodity_code not in COMTRADE_COMMODITY_CODES:
            raise ComtradeDataParseError(
                "invalid_data_commodity",
                "UN Comtrade data commodity is outside the reviewed three-code scope",
            )
        if self.partner_codes != COMTRADE_PARTNER_CODES or any(
            type(code) is not int for code in self.partner_codes
        ):
            raise ComtradeDataParseError(
                "invalid_data_partners",
                "UN Comtrade data partners must be World and China",
            )
        if self.flow_codes != COMTRADE_FLOWS or any(not isinstance(code, str) for code in self.flow_codes):
            raise ComtradeDataParseError(
                "invalid_data_flows",
                "UN Comtrade data flows must be import and export",
            )
        if type(self.partner2_code) is not int or self.partner2_code != 0:
            raise ComtradeDataParseError(
                "invalid_data_partner2",
                "UN Comtrade data second partner must be World",
            )
        if self.customs_code != "C00":
            raise ComtradeDataParseError(
                "invalid_data_customs",
                "UN Comtrade data customs procedure must be C00",
            )
        if type(self.mot_code) is not int or self.mot_code != 0:
            raise ComtradeDataParseError(
                "invalid_data_mot",
                "UN Comtrade data mode of transport must be zero",
            )


@dataclass(frozen=True, slots=True)
class ComtradeDataObservation:
    reporter_iso3: str
    reporter_code: int
    period: int
    dataset_code: str
    dataset_binding_source: str
    dataset_checksum: str
    dataset_first_released: datetime
    dataset_last_released: datetime
    metadata_publication_dates: tuple[date, ...]
    type_code: str
    freq_code: str
    classification_code: str
    classification_search_code: str
    commodity_code: str
    partner_code: int
    partner2_code: int
    flow_code: str
    customs_code: str
    mot_code: int
    primary_value: Decimal | None
    net_weight: Decimal | None
    quantity: Decimal | None
    quantity_unit_code: int | None
    quantity_unit_abbreviation: str | None
    is_reported: bool
    is_aggregate: bool


@dataclass(frozen=True, slots=True)
class ComtradeDataParseResult:
    query_spec: ComtradeDataQuerySpec
    returned_row_count: int
    observations: tuple[ComtradeDataObservation, ...]


def parse_comtrade_data(
    resource: FetchedResource,
    query_spec: ComtradeDataQuerySpec,
    availability: ComtradeAvailabilityParseResult,
    metadata: ComtradeMetadataParseResult,
) -> ComtradeDataParseResult:
    _validate_identity_context(query_spec, availability, metadata)
    try:
        payload = json.loads(
            resource.body,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError) as exc:
        raise ComtradeDataParseError(
            "invalid_data_json",
            "UN Comtrade data response is not valid finite JSON",
        ) from exc
    if not isinstance(payload, Mapping):
        raise ComtradeDataParseError(
            "invalid_data_envelope",
            "UN Comtrade data response must be an object",
        )
    error = payload.get("error", _MISSING)
    if not isinstance(error, str) or error:
        raise ComtradeDataParseError(
            "data_api_error",
            "UN Comtrade data response contains an error",
        )
    rows = payload.get("data", _MISSING)
    if not isinstance(rows, list):
        raise ComtradeDataParseError(
            "invalid_data_rows",
            "UN Comtrade data field must be an array",
        )
    count = _integer(payload.get("count", _MISSING))
    if count is None or count != len(rows) or count >= COMTRADE_MAX_RECORDS:
        raise ComtradeDataParseError(
            "invalid_data_count",
            "UN Comtrade data count must match its records and remain below 500",
        )

    observations: list[ComtradeDataObservation] = []
    seen_identities: set[tuple[Any, ...]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ComtradeDataParseError(
                "invalid_data_row",
                f"UN Comtrade data row {index} must be an object",
            )
        reporter_code = _required_integer(row, "reporterCode", index)
        if reporter_code != query_spec.reporter_code:
            raise ComtradeDataParseError(
                "data_reporter_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected reporter",
            )
        period = _required_integer(row, "period", index)
        if period != query_spec.period:
            raise ComtradeDataParseError(
                "data_period_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected period",
            )
        if row.get("typeCode") != "C" or row.get("freqCode") != "A":
            raise ComtradeDataParseError(
                "data_dataset_out_of_scope",
                f"UN Comtrade data row {index} is not annual goods data",
            )
        commodity_code = row.get("cmdCode", _MISSING)
        if commodity_code != query_spec.commodity_code:
            raise ComtradeDataParseError(
                "data_commodity_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected commodity",
            )
        partner_code = _required_integer(row, "partnerCode", index)
        if partner_code not in query_spec.partner_codes:
            raise ComtradeDataParseError(
                "data_partner_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected partner",
            )
        partner2_code = _required_integer(row, "partner2Code", index)
        if partner2_code != query_spec.partner2_code:
            raise ComtradeDataParseError(
                "data_partner2_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected second partner",
            )
        flow_code = row.get("flowCode", _MISSING)
        if flow_code not in query_spec.flow_codes:
            raise ComtradeDataParseError(
                "data_flow_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected flow",
            )
        customs_code = row.get("customsCode", _MISSING)
        if customs_code != query_spec.customs_code:
            raise ComtradeDataParseError(
                "data_customs_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected customs procedure",
            )
        mot_code = _required_integer(row, "motCode", index)
        if mot_code != query_spec.mot_code:
            raise ComtradeDataParseError(
                "data_mot_out_of_scope",
                f"UN Comtrade data row {index} belongs to an unexpected mode of transport",
            )
        if row.get("classificationSearchCode") != "HS" or row.get("isOriginalClassification") is not True:
            raise ComtradeDataParseError(
                "data_dataset_out_of_scope",
                f"UN Comtrade data row {index} is not original HS data",
            )
        classification_code = row.get("classificationCode", _MISSING)
        if not isinstance(classification_code, str) or classification_code not in COMTRADE_CLASSIFICATIONS:
            raise ComtradeDataParseError(
                "data_classification_out_of_scope",
                "UN Comtrade data classification is outside verified H0-H6 references",
            )

        explicit_dataset_code = _optional_dataset_code(row, index)
        availability_record = _resolve_availability_record(
            availability,
            reporter_code=reporter_code,
            period=period,
            classification_code=classification_code,
        )
        if explicit_dataset_code is not None and explicit_dataset_code != availability_record.dataset_code:
            raise ComtradeDataParseError(
                "data_dataset_identity_mismatch",
                "UN Comtrade data row datasetCode does not match data availability",
            )
        metadata_publication_dates = _verify_metadata_identity(
            metadata,
            availability_record=availability_record,
        )
        binding_source = (
            "data_row_availability_metadata_verified"
            if explicit_dataset_code is not None
            else "availability_unique_metadata_verified"
        )

        primary_value = _nonnegative_decimal_or_none(row, "primaryValue", index)
        net_weight = _nonnegative_decimal_or_none(row, "netWgt", index)
        quantity = _nonnegative_decimal_or_none(row, "qty", index)
        quantity_unit_code, quantity_unit_abbreviation = _quantity_unit(
            row,
            quantity=quantity,
            index=index,
        )
        is_reported = _required_boolean(row, "isReported", index)
        is_aggregate = _required_boolean(row, "isAggregate", index)

        business_identity = (
            availability_record.dataset_code,
            reporter_code,
            period,
            classification_code,
            commodity_code,
            partner_code,
            partner2_code,
            flow_code,
            customs_code,
            mot_code,
        )
        if business_identity in seen_identities:
            raise ComtradeDataParseError(
                "duplicate_data_business_identity",
                "UN Comtrade data repeats the same business identity",
            )
        seen_identities.add(business_identity)
        observations.append(
            ComtradeDataObservation(
                reporter_iso3=query_spec.reporter_iso3,
                reporter_code=reporter_code,
                period=period,
                dataset_code=availability_record.dataset_code,
                dataset_binding_source=binding_source,
                dataset_checksum=availability_record.dataset_checksum,
                dataset_first_released=availability_record.first_released,
                dataset_last_released=availability_record.last_released,
                metadata_publication_dates=metadata_publication_dates,
                type_code="C",
                freq_code="A",
                classification_code=classification_code,
                classification_search_code="HS",
                commodity_code=commodity_code,
                partner_code=partner_code,
                partner2_code=partner2_code,
                flow_code=flow_code,
                customs_code=customs_code,
                mot_code=mot_code,
                primary_value=primary_value,
                net_weight=net_weight,
                quantity=quantity,
                quantity_unit_code=quantity_unit_code,
                quantity_unit_abbreviation=quantity_unit_abbreviation,
                is_reported=is_reported,
                is_aggregate=is_aggregate,
            )
        )

    observations.sort(
        key=lambda item: (
            item.period,
            item.classification_code,
            item.dataset_code,
            item.commodity_code,
            item.partner_code,
            item.flow_code,
        )
    )
    return ComtradeDataParseResult(
        query_spec=query_spec,
        returned_row_count=count,
        observations=tuple(observations),
    )


def _validate_identity_context(
    query_spec: ComtradeDataQuerySpec,
    availability: ComtradeAvailabilityParseResult,
    metadata: ComtradeMetadataParseResult,
) -> None:
    if (
        availability.query_spec.reporter_iso3 != query_spec.reporter_iso3
        or availability.query_spec.reporter_code != query_spec.reporter_code
        or query_spec.period not in availability.query_spec.years
    ):
        raise ComtradeDataParseError(
            "data_availability_context_mismatch",
            "UN Comtrade data query does not match its availability response",
        )
    if (
        metadata.query_spec.reporter_iso3 != query_spec.reporter_iso3
        or metadata.query_spec.reporter_code != query_spec.reporter_code
        or query_spec.period not in metadata.query_spec.years
    ):
        raise ComtradeDataParseError(
            "data_metadata_context_mismatch",
            "UN Comtrade data query does not match its metadata response",
        )


def _resolve_availability_record(
    availability: ComtradeAvailabilityParseResult,
    *,
    reporter_code: int,
    period: int,
    classification_code: str,
) -> ComtradeAvailabilityRecord:
    try:
        return resolve_comtrade_availability_identity(
            availability,
            reporter_code=reporter_code,
            period=period,
            classification_code=classification_code,
        )
    except ComtradeAvailabilityParseError as exc:
        code_by_cause = {
            "availability_identity_missing": "data_dataset_identity_missing",
            "availability_identity_ambiguous": "data_dataset_identity_ambiguous",
        }
        raise ComtradeDataParseError(
            code_by_cause.get(exc.code, "data_dataset_identity_invalid"),
            "UN Comtrade data row has no unique matching availability identity",
        ) from exc


def _verify_metadata_identity(
    metadata: ComtradeMetadataParseResult,
    *,
    availability_record: ComtradeAvailabilityRecord,
) -> tuple[date, ...]:
    try:
        identity = resolve_comtrade_dataset_identity(
            metadata,
            reporter_code=availability_record.reporter_code,
            period=availability_record.period,
            classification_code=availability_record.classification_code,
            explicit_dataset_code=availability_record.dataset_code,
        )
    except ComtradeMetadataParseError as exc:
        code_by_cause = {
            "metadata_dataset_identity_missing": "data_dataset_identity_missing",
            "metadata_dataset_identity_ambiguous": "data_dataset_identity_ambiguous",
            "metadata_dataset_identity_mismatch": "data_dataset_identity_mismatch",
        }
        raise ComtradeDataParseError(
            code_by_cause.get(exc.code, "data_dataset_identity_invalid"),
            "UN Comtrade data availability identity is not verified by metadata",
        ) from exc
    if identity.dataset_code != availability_record.dataset_code:
        raise ComtradeDataParseError(
            "data_dataset_identity_mismatch",
            "UN Comtrade data availability and metadata identities do not match",
        )
    return tuple(note.publication_date for note in identity.publication_notes)


def _optional_dataset_code(row: Mapping[str, Any], index: int) -> str | None:
    if "datasetCode" not in row:
        return None
    value = _integer(row["datasetCode"])
    if value is None or value <= 0:
        raise ComtradeDataParseError(
            "invalid_data_dataset_code",
            f"UN Comtrade data row {index} datasetCode must be a positive integer when present",
        )
    return str(value)


def _required_integer(row: Mapping[str, Any], field_name: str, index: int) -> int:
    value = _integer(row.get(field_name, _MISSING))
    if value is None:
        raise ComtradeDataParseError(
            "invalid_data_integer",
            f"UN Comtrade data row {index} field {field_name} must be an integer",
        )
    return value


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


def _nonnegative_decimal_or_none(
    row: Mapping[str, Any],
    field_name: str,
    index: int,
) -> Decimal | None:
    value = row.get(field_name, _MISSING)
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ComtradeDataParseError(
            "invalid_data_metric",
            f"UN Comtrade data row {index} field {field_name} must be null or non-negative finite number",
        )
    return value


def _quantity_unit(
    row: Mapping[str, Any],
    *,
    quantity: Decimal | None,
    index: int,
) -> tuple[int | None, str | None]:
    raw_code = row.get("qtyUnitCode", _MISSING)
    raw_abbreviation = row.get("qtyUnitAbbr", _MISSING)
    if raw_code is _MISSING or raw_abbreviation is _MISSING:
        raise ComtradeDataParseError(
            "invalid_data_quantity_unit",
            f"UN Comtrade data row {index} must provide quantity unit fields",
        )
    if raw_code is None:
        if raw_abbreviation is None and quantity is None:
            return None, None
        raise ComtradeDataParseError(
            "invalid_data_quantity_unit",
            f"UN Comtrade data row {index} quantity unit fields are invalid",
        )
    unit_code = _integer(raw_code)
    if unit_code is None or unit_code not in COMTRADE_QUANTITY_UNITS:
        raise ComtradeDataParseError(
            "invalid_data_quantity_unit",
            f"UN Comtrade data row {index} quantity unit fields are invalid",
        )
    official_abbreviation = COMTRADE_QUANTITY_UNITS[unit_code]
    if raw_abbreviation is not None and raw_abbreviation != official_abbreviation:
        raise ComtradeDataParseError(
            "invalid_data_quantity_unit",
            f"UN Comtrade data row {index} quantity unit fields are invalid",
        )
    if unit_code == -1 and quantity not in {None, Decimal(0)}:
        raise ComtradeDataParseError(
            "invalid_data_quantity_unit",
            f"UN Comtrade data row {index} positive quantity cannot use the no-quantity unit",
        )
    return unit_code, official_abbreviation


def _required_boolean(row: Mapping[str, Any], field_name: str, index: int) -> bool:
    value = row.get(field_name, _MISSING)
    if type(value) is not bool:
        raise ComtradeDataParseError(
            "invalid_data_quality_flag",
            f"UN Comtrade data row {index} field {field_name} must be a boolean",
        )
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")
