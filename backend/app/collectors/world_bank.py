from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.collectors.base import FetchedResource
from app.collectors.cod_basics_scope import (
    COD_BASICS_INDICATORS,
    COD_BASICS_KEY,
    COD_BASICS_YEARS,
    COD_RECENT_KEY,
    COD_RECENT_YEARS,
)

WORLD_BANK_COUNTRIES = ("COD", "ZWE", "ZMB", "ZAF")
WORLD_BANK_YEARS = tuple(range(2015, 2025))
WORLD_BANK_SOURCE_ID = 2
WORLD_BANK_INDICATORS = frozenset(
    {
        "SP.POP.TOTL",
        "NY.GDP.MKTP.CD",
        "NY.GDP.MKTP.KD.ZG",
        "BX.KLT.DINV.WD.GD.ZS",
        "NY.GDP.MINR.RT.ZS",
    }
)

_YEAR_PATTERN = re.compile(r"\d{4}")
_INTEGER_PATTERN = re.compile(r"0|[1-9]\d*")
_MISSING = object()


class WorldBankParseError(ValueError):
    """A World Bank response or reviewed query violates the fixed MVP contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorldBankQuerySpec:
    indicator_code: str
    metric_code: str
    unit: str
    currency: str | None = None
    price_basis: str | None = None
    countries: tuple[str, ...] = WORLD_BANK_COUNTRIES
    years: tuple[int, ...] = WORLD_BANK_YEARS
    source_id: int = WORLD_BANK_SOURCE_ID
    scope_id: str = "mvp"

    def __post_init__(self) -> None:
        if self.scope_id not in ("mvp", COD_BASICS_KEY, COD_RECENT_KEY):
            raise WorldBankParseError("invalid_query_scope", "unknown World Bank scope")
        supplement = self.scope_id == COD_BASICS_KEY
        countries = ("COD",) if supplement or self.scope_id == COD_RECENT_KEY else WORLD_BANK_COUNTRIES
        years = (
            COD_RECENT_YEARS
            if self.scope_id == COD_RECENT_KEY
            else COD_BASICS_YEARS
            if supplement
            else WORLD_BANK_YEARS
        )
        indicators = {item[0] for item in COD_BASICS_INDICATORS} if supplement else WORLD_BANK_INDICATORS
        if self.countries != countries:
            raise WorldBankParseError(
                "invalid_query_countries",
                "World Bank MVP countries must match the reviewed four-country scope",
            )
        if self.years != years:
            raise WorldBankParseError(
                "invalid_query_years",
                "World Bank MVP years must cover 2015 through 2024",
            )
        if self.indicator_code not in indicators:
            raise WorldBankParseError(
                "invalid_query_indicator",
                "World Bank indicator is outside the reviewed MVP scope",
            )
        _require_reviewed_text(self.metric_code, "invalid_query_metric", "metric_code")
        _require_reviewed_text(self.unit, "invalid_query_unit", "unit")
        _require_optional_reviewed_text(
            self.currency,
            "invalid_query_currency",
            "currency",
        )
        _require_optional_reviewed_text(
            self.price_basis,
            "invalid_query_price_basis",
            "price_basis",
        )
        if type(self.source_id) is not int or self.source_id != WORLD_BANK_SOURCE_ID:
            raise WorldBankParseError(
                "invalid_query_source",
                "World Bank Indicators API source_id must be 2",
            )


@dataclass(frozen=True, slots=True)
class WorldBankObservation:
    source_id: int
    country_iso3: str
    indicator_code: str
    metric_code: str
    frequency: str
    period: str
    value: Decimal | None
    unit: str
    currency: str | None
    price_basis: str | None
    missing_reason: str | None
    quality_metadata: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorldBankParseResult:
    query_spec: WorldBankQuerySpec
    last_updated: date
    returned_row_count: int
    observations: tuple[WorldBankObservation, ...]


class WorldBankIndicatorsCollector:
    """Parse one already-fetched, single-indicator World Bank API response."""

    def parse(
        self,
        resource: FetchedResource,
        query_spec: WorldBankQuerySpec,
    ) -> WorldBankParseResult:
        return parse_world_bank_indicators(resource, query_spec)


def parse_world_bank_indicators(
    resource: FetchedResource,
    query_spec: WorldBankQuerySpec,
) -> WorldBankParseResult:
    try:
        payload = json.loads(
            resource.body,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation, ValueError) as exc:
        raise WorldBankParseError("invalid_json", "World Bank response is not valid finite JSON") from exc

    if not isinstance(payload, list) or len(payload) != 2:
        raise WorldBankParseError(
            "invalid_response_envelope",
            "World Bank response must be a two-element metadata and rows array",
        )
    metadata, rows = payload
    if not isinstance(metadata, Mapping):
        raise WorldBankParseError(
            "invalid_metadata",
            "World Bank response metadata must be an object",
        )
    if not isinstance(rows, list):
        raise WorldBankParseError(
            "invalid_rows",
            "World Bank response rows must be an array",
        )

    page = _metadata_integer(metadata, "page", "invalid_pagination")
    pages = _metadata_integer(metadata, "pages", "invalid_pagination")
    if page != 1 or pages != 1:
        raise WorldBankParseError(
            "invalid_pagination",
            "World Bank response must contain exactly the first and only page",
        )
    source_id = _metadata_integer(metadata, "sourceid", "invalid_source_id")
    if source_id != query_spec.source_id:
        raise WorldBankParseError(
            "invalid_source_id",
            "World Bank response sourceid does not match the reviewed source",
        )
    total = _metadata_integer(metadata, "total", "invalid_total")
    if total != len(rows):
        raise WorldBankParseError(
            "invalid_total",
            "World Bank response total does not equal the number of returned rows",
        )
    if not rows:
        raise WorldBankParseError("empty_rows", "World Bank response contains no observation rows")

    expected_cell_count = len(query_spec.countries) * len(query_spec.years)
    if len(rows) > expected_cell_count:
        raise WorldBankParseError(
            "row_count_exceeds_scope",
            "World Bank response contains more rows than the fixed query scope",
        )
    last_updated = _last_updated(metadata)

    returned: dict[tuple[str, int], WorldBankObservation] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise WorldBankParseError(
                "invalid_row",
                f"World Bank row {index} must be an object",
            )

        indicator = row.get("indicator")
        if not isinstance(indicator, Mapping) or not isinstance(indicator.get("id"), str):
            raise WorldBankParseError(
                "invalid_indicator",
                f"World Bank row {index} has no valid indicator id",
            )
        if indicator["id"] != query_spec.indicator_code:
            raise WorldBankParseError(
                "indicator_out_of_scope",
                f"World Bank row {index} belongs to an unexpected indicator",
            )

        country = row.get("countryiso3code")
        if not isinstance(country, str):
            raise WorldBankParseError(
                "invalid_country",
                f"World Bank row {index} has no valid ISO3 country code",
            )
        if country not in query_spec.countries:
            raise WorldBankParseError(
                "country_out_of_scope",
                f"World Bank row {index} belongs to a country outside the fixed scope",
            )

        year = _row_year(row.get("date"), index)
        if year not in query_spec.years:
            raise WorldBankParseError(
                "year_out_of_scope",
                f"World Bank row {index} belongs to a year outside the fixed scope",
            )
        key = (country, year)
        if key in returned:
            raise WorldBankParseError(
                "duplicate_observation",
                f"World Bank response repeats the same country and year at row {index}",
            )

        raw_value = row.get("value", _MISSING)
        if raw_value is _MISSING:
            raise WorldBankParseError(
                "invalid_value",
                f"World Bank row {index} has no value field",
            )
        if raw_value is None:
            value = None
            missing_reason = "source_null"
        elif isinstance(raw_value, bool) or not isinstance(raw_value, Decimal) or not raw_value.is_finite():
            raise WorldBankParseError(
                "invalid_value",
                f"World Bank row {index} value must be a finite JSON number or null",
            )
        else:
            value = raw_value
            missing_reason = None

        api_unit = _api_unit(row.get("unit", _MISSING), index)
        obs_status = _obs_status(row.get("obs_status", _MISSING), index)
        returned[key] = _observation(
            query_spec,
            country=country,
            year=year,
            value=value,
            missing_reason=missing_reason,
            api_unit=api_unit,
            obs_status=obs_status,
        )

    observations: list[WorldBankObservation] = []
    for country in query_spec.countries:
        for year in query_spec.years:
            observation = returned.get((country, year))
            if observation is None:
                observation = _observation(
                    query_spec,
                    country=country,
                    year=year,
                    value=None,
                    missing_reason="not_returned",
                    api_unit=None,
                    obs_status="unknown",
                )
            observations.append(observation)

    return WorldBankParseResult(
        query_spec=query_spec,
        last_updated=last_updated,
        returned_row_count=len(rows),
        observations=tuple(observations),
    )


def _observation(
    query_spec: WorldBankQuerySpec,
    *,
    country: str,
    year: int,
    value: Decimal | None,
    missing_reason: str | None,
    api_unit: str | None,
    obs_status: str,
) -> WorldBankObservation:
    return WorldBankObservation(
        source_id=query_spec.source_id,
        country_iso3=country,
        indicator_code=query_spec.indicator_code,
        metric_code=query_spec.metric_code,
        frequency="annual",
        period=str(year),
        value=value,
        unit=query_spec.unit,
        currency=query_spec.currency,
        price_basis=query_spec.price_basis,
        missing_reason=missing_reason,
        quality_metadata={
            "obs_status": obs_status,
            "api_unit": api_unit,
        },
    )


def _metadata_integer(metadata: Mapping[str, Any], field_name: str, error_code: str) -> int:
    value = metadata.get(field_name, _MISSING)
    parsed = _integer(value)
    if parsed is None:
        raise WorldBankParseError(
            error_code,
            f"World Bank metadata field {field_name} must be an integer",
        )
    return parsed


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


def _last_updated(metadata: Mapping[str, Any]) -> date:
    value = metadata.get("lastupdated")
    if not isinstance(value, str):
        raise WorldBankParseError(
            "invalid_last_updated",
            "World Bank lastupdated metadata must be an ISO date",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise WorldBankParseError(
            "invalid_last_updated",
            "World Bank lastupdated metadata must be an ISO date",
        ) from exc
    if parsed.isoformat() != value:
        raise WorldBankParseError(
            "invalid_last_updated",
            "World Bank lastupdated metadata must be a canonical ISO date",
        )
    return parsed


def _row_year(value: Any, index: int) -> int:
    if isinstance(value, str) and _YEAR_PATTERN.fullmatch(value):
        return int(value)
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    raise WorldBankParseError(
        "invalid_year",
        f"World Bank row {index} year must be a four-digit integer",
    )


def _api_unit(value: Any, index: int) -> str | None:
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        raise WorldBankParseError(
            "invalid_api_unit",
            f"World Bank row {index} API unit must be text or null",
        )
    return value


def _obs_status(value: Any, index: int) -> str:
    if value is _MISSING or value is None:
        return "unknown"
    if not isinstance(value, str):
        raise WorldBankParseError(
            "invalid_obs_status",
            f"World Bank row {index} obs_status must be text or null",
        )
    return value.strip() or "unknown"


def _require_reviewed_text(value: Any, error_code: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise WorldBankParseError(
            error_code,
            f"World Bank query {field_name} must be reviewed non-empty text",
        )


def _require_optional_reviewed_text(
    value: Any,
    error_code: str,
    field_name: str,
) -> None:
    if value is not None:
        _require_reviewed_text(value, error_code, field_name)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")
