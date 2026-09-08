from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.collectors.base import FetchedResource

OECD_ODA_COUNTRIES = ("COD", "ZWE", "ZMB", "ZAF")
OECD_ODA_YEARS = tuple(range(2015, 2025))
OECD_ODA_DIMENSIONS = (
    "DONOR",
    "RECIPIENT",
    "MEASURE",
    "UNIT_MEASURE",
    "PRICE_BASE",
    "TIME_PERIOD",
)


class OecdOdaParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class OecdOdaObservation:
    country_iso3: str
    period: int
    value: Decimal
    unit: str
    currency: str
    price_basis: str
    source_status: str


@dataclass(frozen=True, slots=True)
class OecdOdaParseResult:
    observations: tuple[OecdOdaObservation, ...]


def parse_oecd_oda(resource: FetchedResource) -> OecdOdaParseResult:
    try:
        payload = json.loads(
            resource.body,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError) as exc:
        raise OecdOdaParseError("invalid_json", "OECD response is not valid finite JSON") from exc

    root = _mapping(payload, "invalid_envelope", "OECD response must be an object")
    errors = root.get("errors")
    if errors not in (None, []):
        raise OecdOdaParseError("source_errors", "OECD response contains source errors")
    data = _mapping(root.get("data"), "invalid_data", "OECD response data must be an object")
    structures = _single_list_item(data, "structures", "invalid_structure")
    dataset = _single_list_item(data, "dataSets", "invalid_dataset")

    structure = _mapping(structures, "invalid_structure", "OECD structure must be an object")
    dimensions = _mapping(
        structure.get("dimensions"),
        "invalid_dimensions",
        "OECD dimensions must be an object",
    )
    observation_dimensions = dimensions.get("observation")
    if not isinstance(observation_dimensions, list) or len(observation_dimensions) != 6:
        raise OecdOdaParseError("invalid_dimensions", "OECD response must expose six observation dimensions")

    dimension_values: list[tuple[str, ...]] = []
    dimension_ids: list[str] = []
    for dimension in observation_dimensions:
        item = _mapping(dimension, "invalid_dimensions", "OECD dimension must be an object")
        dimension_id = item.get("id")
        values = item.get("values")
        if not isinstance(dimension_id, str) or not isinstance(values, list):
            raise OecdOdaParseError("invalid_dimensions", "OECD dimension id or values are invalid")
        ids: list[str] = []
        for value in values:
            member = _mapping(value, "invalid_dimensions", "OECD dimension value must be an object")
            member_id = member.get("id")
            if not isinstance(member_id, str) or not member_id or member_id in ids:
                raise OecdOdaParseError("invalid_dimensions", "OECD dimension values are invalid")
            ids.append(member_id)
        dimension_ids.append(dimension_id)
        dimension_values.append(tuple(ids))
    if tuple(dimension_ids) != OECD_ODA_DIMENSIONS:
        raise OecdOdaParseError("invalid_dimensions", "OECD dimension order is outside the reviewed schema")

    _require_dimension_members(dimension_values[0], {"ALLD"}, "DONOR")
    _require_dimension_members(dimension_values[1], set(OECD_ODA_COUNTRIES), "RECIPIENT", exact=False)
    _require_dimension_members(dimension_values[2], {"206"}, "MEASURE")
    _require_dimension_members(dimension_values[3], {"USD"}, "UNIT_MEASURE")
    _require_dimension_members(dimension_values[4], {"V"}, "PRICE_BASE")
    _require_dimension_members(dimension_values[5], {str(year) for year in OECD_ODA_YEARS}, "TIME_PERIOD")
    _validate_attributes(structure)

    observations_payload = _mapping(
        _mapping(dataset, "invalid_dataset", "OECD dataset must be an object").get("observations"),
        "invalid_observations",
        "OECD observations must be an object",
    )
    parsed: dict[tuple[str, int], OecdOdaObservation] = {}
    for key, raw in observations_payload.items():
        if not isinstance(key, str) or not isinstance(raw, list) or len(raw) != 5:
            raise OecdOdaParseError("invalid_observation", "OECD observation shape is invalid")
        try:
            indexes = tuple(int(value) for value in key.split(":"))
        except ValueError as exc:
            raise OecdOdaParseError("invalid_observation", "OECD observation key is invalid") from exc
        if len(indexes) != 6 or any(
            index < 0 or index >= len(dimension_values[position]) for position, index in enumerate(indexes)
        ):
            raise OecdOdaParseError("invalid_observation", "OECD observation key is out of range")
        members = tuple(dimension_values[position][index] for position, index in enumerate(indexes))
        donor, country, measure, unit, price_base, year_text = members
        if (
            donor != "ALLD"
            or country not in OECD_ODA_COUNTRIES
            or measure != "206"
            or unit != "USD"
            or price_base != "V"
            or year_text not in {str(year) for year in OECD_ODA_YEARS}
        ):
            raise OecdOdaParseError("observation_out_of_scope", "OECD observation is outside scope")
        value = raw[0]
        if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
            raise OecdOdaParseError("invalid_value", "OECD ODA value must be a finite number")
        if raw[1:] != [None, Decimal(0), Decimal(0), Decimal(0)]:
            raise OecdOdaParseError(
                "invalid_attributes", "OECD observation attributes differ from the reviewed schema"
            )
        identity = (country, int(year_text))
        if identity in parsed:
            raise OecdOdaParseError("duplicate_observation", "OECD response repeats a country-year")
        parsed[identity] = OecdOdaObservation(
            country_iso3=country,
            period=int(year_text),
            value=value,
            unit="million_USD",
            currency="USD",
            price_basis="current_prices",
            source_status="normal",
        )

    expected = {(country, year) for country in OECD_ODA_COUNTRIES for year in OECD_ODA_YEARS}
    if set(parsed) != expected:
        raise OecdOdaParseError("incomplete_grid", "OECD response does not contain the complete 40-cell grid")
    return OecdOdaParseResult(
        observations=tuple(
            parsed[(country, year)] for country in OECD_ODA_COUNTRIES for year in OECD_ODA_YEARS
        )
    )


def _validate_attributes(structure: Mapping[str, object]) -> None:
    attributes = _mapping(
        structure.get("attributes"),
        "invalid_attributes",
        "OECD attributes must be an object",
    )
    observation = attributes.get("observation")
    if not isinstance(observation, list):
        raise OecdOdaParseError("invalid_attributes", "OECD observation attributes must be a list")
    expected = {
        "BASE_PER": (),
        "UNIT_MULT": ("6",),
        "FLOW_TYPE": ("D",),
        "OBS_STATUS": ("A",),
    }
    actual: dict[str, tuple[str, ...]] = {}
    for attribute in observation:
        item = _mapping(attribute, "invalid_attributes", "OECD attribute must be an object")
        attribute_id = item.get("id")
        values = item.get("values")
        if not isinstance(attribute_id, str) or not isinstance(values, list):
            raise OecdOdaParseError("invalid_attributes", "OECD attribute schema is invalid")
        ids = tuple(
            member.get("id")
            for member in values
            if isinstance(member, Mapping) and isinstance(member.get("id"), str)
        )
        if len(ids) != len(values):
            raise OecdOdaParseError("invalid_attributes", "OECD attribute values are invalid")
        actual[attribute_id] = ids
    if actual != expected:
        raise OecdOdaParseError("invalid_attributes", "OECD attributes differ from reviewed schema")


def _require_dimension_members(
    actual: tuple[str, ...], expected: set[str], name: str, *, exact: bool = True
) -> None:
    valid = set(actual) == expected if exact else expected.issubset(actual)
    if not valid:
        raise OecdOdaParseError("invalid_dimensions", f"OECD {name} values differ from scope")


def _mapping(value: object, code: str, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OecdOdaParseError(code, message)
    return value


def _single_list_item(container: Mapping[str, object], key: str, code: str) -> object:
    value = container.get(key)
    if not isinstance(value, list) or len(value) != 1:
        raise OecdOdaParseError(code, f"OECD {key} must contain exactly one item")
    return value[0]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
