from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.collectors.cod_basics_scope import COD_BASICS_INDICATORS, COD_BASICS_KEY, COD_RECENT_KEY

SCOPE_ID = "mvp-structured-data-v1"
WORLD_BANK_CATALOG_ROW = 44
WORLD_BANK_DATASET_KEY = "world-bank-indicators-v2"
WORLD_BANK_SOURCE_NAME = "World Bank Open Data"
WORLD_BANK_SOURCE_ID = "2"
WORLD_BANK_API_BASE_URL = "https://api.worldbank.org/v2"
WORLD_BANK_FREQUENCY = "annual"
WORLD_BANK_START_YEAR = 2015
WORLD_BANK_END_YEAR = 2024
WORLD_BANK_COUNTRIES = ("COD", "ZWE", "ZMB", "ZAF")
WORLD_BANK_INDICATOR_CODES = (
    "SP.POP.TOTL",
    "NY.GDP.MKTP.CD",
    "NY.GDP.MKTP.KD.ZG",
    "BX.KLT.DINV.WD.GD.ZS",
    "NY.GDP.MINR.RT.ZS",
)
WORLD_BANK_LOGICAL_DIMENSION_CELLS = 200
COMTRADE_CATALOG_ROW = 48
COMTRADE_DATASET_KEY = "un-comtrade-goods-annual-hs"
COMTRADE_SOURCE_NAME = "UN Comtrade Plus"
COMTRADE_API_BASE_URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
COMTRADE_METADATA_API_URL = "https://comtradeapi.un.org/public/v1/getMetadata/C/A/HS"
COMTRADE_DATA_AVAILABILITY_API_URL = "https://comtradeapi.un.org/public/v1/getDA/C/A/HS"
COMTRADE_FREQUENCY = "annual"
COMTRADE_START_YEAR = 2017
COMTRADE_END_YEAR = 2024
COMTRADE_REPORTER_AREAS = (
    ("COD", 180, "COD"),
    ("ZWE", 716, "ZWE"),
    ("ZMB", 894, "ZMB"),
    ("ZAF", 710, "ZAF"),
)
COMTRADE_PARTNER_AREAS = (("WLD", 0, "W00"), ("CHN", 156, "CHN"))
COMTRADE_FLOWS = ("M", "X")
COMTRADE_CLASSIFICATION_QUERY = "HS"
COMTRADE_ACTUAL_CLASSIFICATIONS = ("H0", "H1", "H2", "H3", "H4", "H5", "H6")
COMTRADE_CLASSIFICATION_REFERENCE_URL_TEMPLATE = (
    "https://comtradeapi.un.org/files/v1/app/reference/{classification}.json"
)
COMTRADE_COMMODITIES = (
    ("copper", "260300", "铜矿砂及其精矿", "copper_ores_and_concentrates", "kg"),
    ("cobalt", "260500", "钴矿砂及其精矿", "cobalt_ores_and_concentrates", "kg"),
    ("lithium", "283691", "碳酸锂", "lithium_carbonates", "kg"),
)
COMTRADE_METRICS = (
    ("trade_value", "primaryValue", "fixed", "USD", "USD"),
    ("net_weight", "netWgt", "fixed", "kg", None),
    ("quantity", "qty", "use_qtyUnitCode_and_official_reference", None, None),
)
COMTRADE_LOGICAL_DIMENSION_CELLS = 384
COMTRADE_REQUEST_SHARDS = 96
COMTRADE_METADATA_REQUESTS = 4
COMTRADE_DATA_AVAILABILITY_REQUESTS = 4
COMTRADE_MINIMUM_METRIC_OBSERVATION_CELLS = 1152
COMTRADE_MAX_RECORDS = 500


class StructuredScopeValidationError(ValueError):
    """Raised when the checked-in structured-data scope is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class OfficialDataset:
    id: str
    name: str
    license_name: str
    license_url: str
    catalog_url: str


@dataclass(frozen=True, slots=True)
class OfficialDataTerms:
    id: str
    name: str
    terms_name: str
    terms_url: str
    catalog_url: str


@dataclass(frozen=True, slots=True)
class WorldBankIndicator:
    code: str
    name_zh: str
    metric_code: str
    unit: str
    currency: str | None
    price_basis: str | None


@dataclass(frozen=True, slots=True)
class WorldBankScope:
    dataset_key: str
    source_catalog_row: int
    name: str
    source_id: str
    api_base_url: str
    official_dataset: OfficialDataset
    frequency: str
    start_year: int
    end_year: int
    countries: tuple[str, ...]
    indicators: tuple[WorldBankIndicator, ...]
    logical_dimension_cells: int


@dataclass(frozen=True, slots=True)
class ComtradeArea:
    iso3: str
    source_code: int
    source_iso3: str


@dataclass(frozen=True, slots=True)
class ComtradeCommodity:
    mineral: str
    hs6: str
    name_zh: str
    verified_meaning: str
    standard_unit: str


@dataclass(frozen=True, slots=True)
class ComtradeMetric:
    metric_code: str
    source_field: str
    unit_policy: str
    unit: str | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class ComtradeScope:
    dataset_key: str
    source_catalog_row: int
    name: str
    api_base_url: str
    metadata_api_url: str
    data_availability_api_url: str
    official_dataset: OfficialDataTerms
    frequency: str
    start_year: int
    end_year: int
    reporters: tuple[ComtradeArea, ...]
    partners: tuple[ComtradeArea, ...]
    flows: tuple[str, ...]
    classification_query: str
    actual_classifications: tuple[str, ...]
    classification_reference_url_template: str
    commodities: tuple[ComtradeCommodity, ...]
    metrics: tuple[ComtradeMetric, ...]
    logical_dimension_cells: int
    request_shards: int
    metadata_requests: int
    data_availability_requests: int
    minimum_metric_observation_cells: int
    max_records_per_request: int


@dataclass(frozen=True, slots=True)
class StructuredScope:
    scope_id: str
    scope_sha256: str
    world_bank: WorldBankScope
    comtrade: ComtradeScope


def load_structured_scope(path: str | Path | None = None) -> StructuredScope:
    scope_path = Path(path) if path is not None else _default_scope_path()
    try:
        raw = scope_path.read_bytes()
        payload: Any = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredScopeValidationError(f"unable to read structured scope: {scope_path}") from exc

    root = _require_object(payload, "scope")
    scope_id = _require_nonempty_string(root, "scope_id", "scope")
    if scope_id != SCOPE_ID:
        raise StructuredScopeValidationError(f"scope_id must be {SCOPE_ID!r}")

    datasets = _require_list(root, "datasets", "scope")
    world_bank_entries = [
        item
        for item in datasets
        if isinstance(item, dict) and item.get("source_catalog_row") == WORLD_BANK_CATALOG_ROW
    ]
    if len(world_bank_entries) != 1:
        raise StructuredScopeValidationError(
            f"scope must contain exactly one dataset for source_catalog_row {WORLD_BANK_CATALOG_ROW}"
        )
    comtrade_entries = [
        item
        for item in datasets
        if isinstance(item, dict) and item.get("source_catalog_row") == COMTRADE_CATALOG_ROW
    ]
    if len(comtrade_entries) != 1:
        raise StructuredScopeValidationError(
            f"scope must contain exactly one dataset for source_catalog_row {COMTRADE_CATALOG_ROW}"
        )

    return StructuredScope(
        scope_id=scope_id,
        scope_sha256=hashlib.sha256(raw).hexdigest(),
        world_bank=_parse_world_bank_scope(world_bank_entries[0]),
        comtrade=_parse_comtrade_scope(comtrade_entries[0]),
    )


def build_world_bank_request_url(scope: WorldBankScope, indicator_code: str) -> str:
    supplement = scope.dataset_key == COD_BASICS_KEY
    recent = scope.dataset_key == COD_RECENT_KEY
    if recent and {item.code for item in scope.indicators} != set(WORLD_BANK_INDICATOR_CODES):
        raise StructuredScopeValidationError("COD recent-year scope requires the reviewed five indicators")
    if supplement and {item.code for item in scope.indicators} != {item[0] for item in COD_BASICS_INDICATORS}:
        raise StructuredScopeValidationError("COD supplement indicators must match the approved eight")
    if (
        scope.dataset_key not in (WORLD_BANK_DATASET_KEY, COD_BASICS_KEY, COD_RECENT_KEY)
        or scope.source_catalog_row != WORLD_BANK_CATALOG_ROW
        or scope.source_id != WORLD_BANK_SOURCE_ID
        or scope.api_base_url != WORLD_BANK_API_BASE_URL
        or scope.frequency != WORLD_BANK_FREQUENCY
        or (scope.start_year, scope.end_year)
        != (
            (2025, 2025)
            if recent
            else (2015, 2025)
            if supplement
            else (WORLD_BANK_START_YEAR, WORLD_BANK_END_YEAR)
        )
        or scope.countries != (("COD",) if supplement or recent else WORLD_BANK_COUNTRIES)
    ):
        raise StructuredScopeValidationError("World Bank request scope is outside the reviewed boundary")
    if indicator_code not in {indicator.code for indicator in scope.indicators}:
        raise StructuredScopeValidationError(
            f"indicator {indicator_code!r} is outside the reviewed World Bank scope"
        )
    countries = ";".join(scope.countries)
    return (
        f"{scope.api_base_url}/country/{countries}/indicator/{indicator_code}"
        f"?date={scope.start_year}:{scope.end_year}"
        f"&format=json&per_page=100&source={scope.source_id}"
    )


def build_comtrade_request_url(
    scope: ComtradeScope,
    reporter_iso3: str,
    year: int,
    commodity_code: str,
) -> str:
    if not _is_reviewed_comtrade_scope(scope):
        raise StructuredScopeValidationError("UN Comtrade request scope is outside the reviewed boundary")
    reporters = {area.iso3: area for area in scope.reporters}
    reporter = reporters.get(reporter_iso3)
    if reporter is None:
        raise StructuredScopeValidationError(
            f"reporter {reporter_iso3!r} is outside the reviewed UN Comtrade scope"
        )
    if type(year) is not int or year not in range(scope.start_year, scope.end_year + 1):
        raise StructuredScopeValidationError(f"year {year!r} is outside the reviewed UN Comtrade scope")
    if commodity_code not in {commodity.hs6 for commodity in scope.commodities}:
        raise StructuredScopeValidationError(
            f"commodity {commodity_code!r} is outside the reviewed UN Comtrade scope"
        )
    query = urlencode(
        (
            ("reporterCode", str(reporter.source_code)),
            ("period", str(year)),
            ("partnerCode", ",".join(str(area.source_code) for area in scope.partners)),
            ("partner2Code", "0"),
            ("cmdCode", commodity_code),
            ("flowCode", ",".join(scope.flows)),
            ("customsCode", "C00"),
            ("motCode", "0"),
            ("maxRecords", str(scope.max_records_per_request)),
            ("format", "JSON"),
            ("breakdownMode", "classic"),
            ("includeDesc", "false"),
        ),
        safe=",",
    )
    return f"{scope.api_base_url}?{query}"


def build_comtrade_metadata_request_url(scope: ComtradeScope, reporter_iso3: str) -> str:
    if not _is_reviewed_comtrade_scope(scope):
        raise StructuredScopeValidationError("UN Comtrade request scope is outside the reviewed boundary")
    reporter = {area.iso3: area for area in scope.reporters}.get(reporter_iso3)
    if reporter is None:
        raise StructuredScopeValidationError(
            f"reporter {reporter_iso3!r} is outside the reviewed UN Comtrade scope"
        )
    periods = ",".join(str(year) for year in range(scope.start_year, scope.end_year + 1))
    query = urlencode(
        (("period", periods), ("reporterCode", str(reporter.source_code))),
        safe=",",
    )
    return f"{scope.metadata_api_url}?{query}"


def build_comtrade_data_availability_request_url(
    scope: ComtradeScope,
    reporter_iso3: str,
) -> str:
    if not _is_reviewed_comtrade_scope(scope):
        raise StructuredScopeValidationError("UN Comtrade request scope is outside the reviewed boundary")
    reporter = {area.iso3: area for area in scope.reporters}.get(reporter_iso3)
    if reporter is None:
        raise StructuredScopeValidationError(
            f"reporter {reporter_iso3!r} is outside the reviewed UN Comtrade scope"
        )
    periods = ",".join(str(year) for year in range(scope.start_year, scope.end_year + 1))
    query = urlencode(
        (("period", periods), ("reporterCode", str(reporter.source_code))),
        safe=",",
    )
    return f"{scope.data_availability_api_url}?{query}"


def _parse_world_bank_scope(value: dict[str, Any]) -> WorldBankScope:
    dataset_key = _require_nonempty_string(value, "dataset_id", "World Bank dataset")
    if dataset_key != WORLD_BANK_DATASET_KEY:
        raise StructuredScopeValidationError(f"World Bank dataset_id must be {WORLD_BANK_DATASET_KEY!r}")
    source_name = _require_nonempty_string(value, "source_name", "World Bank dataset")
    if source_name != WORLD_BANK_SOURCE_NAME:
        raise StructuredScopeValidationError(f"World Bank source_name must be {WORLD_BANK_SOURCE_NAME!r}")

    api_base_url = _require_nonempty_string(value, "api_base_url", "World Bank dataset")
    if api_base_url != WORLD_BANK_API_BASE_URL:
        raise StructuredScopeValidationError(f"World Bank api_base_url must be {WORLD_BANK_API_BASE_URL!r}")

    official_value = _require_object_field(value, "official_dataset", "World Bank dataset")
    official_dataset = OfficialDataset(
        id=_require_nonempty_string(official_value, "id", "World Bank official_dataset"),
        name=_require_nonempty_string(official_value, "name", "World Bank official_dataset"),
        license_name=_require_nonempty_string(official_value, "license_name", "World Bank official_dataset"),
        license_url=_require_https_url(official_value, "license_url", "World Bank official_dataset"),
        catalog_url=_require_https_url(official_value, "catalog_url", "World Bank official_dataset"),
    )
    if official_dataset.id != WORLD_BANK_SOURCE_ID:
        raise StructuredScopeValidationError(
            f"World Bank official_dataset.id must be {WORLD_BANK_SOURCE_ID!r}"
        )

    frequency = _require_nonempty_string(value, "frequency", "World Bank dataset")
    if frequency != WORLD_BANK_FREQUENCY:
        raise StructuredScopeValidationError(f"World Bank frequency must be {WORLD_BANK_FREQUENCY!r}")

    period = _require_object_field(value, "baseline_period", "World Bank dataset")
    start_year = _require_integer(period, "start_year", "World Bank baseline_period")
    end_year = _require_integer(period, "end_year", "World Bank baseline_period")
    if (start_year, end_year) != (WORLD_BANK_START_YEAR, WORLD_BANK_END_YEAR):
        raise StructuredScopeValidationError(
            f"World Bank baseline period must be {WORLD_BANK_START_YEAR}-{WORLD_BANK_END_YEAR}"
        )

    raw_countries = _require_list(value, "countries", "World Bank dataset")
    if any(not isinstance(country, str) or not country.strip() for country in raw_countries):
        raise StructuredScopeValidationError("World Bank countries must be non-empty strings")
    countries = tuple(country.strip() for country in raw_countries)
    if len(set(countries)) != len(countries):
        raise StructuredScopeValidationError("World Bank countries must not contain duplicates")
    if countries != WORLD_BANK_COUNTRIES:
        raise StructuredScopeValidationError(
            f"World Bank countries must be ordered as {WORLD_BANK_COUNTRIES!r}"
        )

    raw_indicators = _require_list(value, "indicators", "World Bank dataset")
    indicators = tuple(_parse_world_bank_indicator(item, index) for index, item in enumerate(raw_indicators))
    codes = tuple(indicator.code for indicator in indicators)
    if len(set(codes)) != len(codes):
        raise StructuredScopeValidationError("World Bank indicator codes must not contain duplicates")
    metric_codes = tuple(indicator.metric_code for indicator in indicators)
    if len(set(metric_codes)) != len(metric_codes):
        raise StructuredScopeValidationError("World Bank metric codes must not contain duplicates")
    if codes != WORLD_BANK_INDICATOR_CODES:
        raise StructuredScopeValidationError(
            f"World Bank indicators must be ordered as {WORLD_BANK_INDICATOR_CODES!r}"
        )

    request_policy = _require_object_field(value, "request_policy", "World Bank dataset")
    if request_policy.get("one_indicator_per_request") is not True:
        raise StructuredScopeValidationError(
            "World Bank request_policy.one_indicator_per_request must be true"
        )
    if request_policy.get("format") != "json":
        raise StructuredScopeValidationError("World Bank request_policy.format must be 'json'")
    per_page = _require_integer(request_policy, "per_page", "World Bank request_policy")
    if per_page != 100:
        raise StructuredScopeValidationError("World Bank request_policy.per_page must be 100")
    if request_policy.get("source_id") != WORLD_BANK_SOURCE_ID:
        raise StructuredScopeValidationError(
            f"World Bank request_policy.source_id must be {WORLD_BANK_SOURCE_ID!r}"
        )
    if request_policy.get("raw_response_storage") is not False:
        raise StructuredScopeValidationError("World Bank request_policy.raw_response_storage must be false")

    logical_dimension_cells = _require_integer(value, "logical_dimension_cells", "World Bank dataset")
    expected_cells = len(countries) * len(indicators) * (end_year - start_year + 1)
    if (
        logical_dimension_cells != expected_cells
        or logical_dimension_cells != WORLD_BANK_LOGICAL_DIMENSION_CELLS
    ):
        raise StructuredScopeValidationError(
            "World Bank logical_dimension_cells must equal the fixed 200-cell boundary"
        )

    return WorldBankScope(
        dataset_key=dataset_key,
        source_catalog_row=WORLD_BANK_CATALOG_ROW,
        name=source_name,
        source_id=WORLD_BANK_SOURCE_ID,
        api_base_url=api_base_url,
        official_dataset=official_dataset,
        frequency=frequency,
        start_year=start_year,
        end_year=end_year,
        countries=countries,
        indicators=indicators,
        logical_dimension_cells=logical_dimension_cells,
    )


def _parse_comtrade_scope(value: dict[str, Any]) -> ComtradeScope:
    context = "UN Comtrade dataset"
    dataset_key = _require_nonempty_string(value, "dataset_id", context)
    if dataset_key != COMTRADE_DATASET_KEY:
        raise StructuredScopeValidationError(f"UN Comtrade dataset_id must be {COMTRADE_DATASET_KEY!r}")
    source_name = _require_nonempty_string(value, "source_name", context)
    if source_name != COMTRADE_SOURCE_NAME:
        raise StructuredScopeValidationError(f"UN Comtrade source_name must be {COMTRADE_SOURCE_NAME!r}")
    api_base_url = _require_https_url(value, "api_base_url", context)
    if api_base_url != COMTRADE_API_BASE_URL:
        raise StructuredScopeValidationError(f"UN Comtrade api_base_url must be {COMTRADE_API_BASE_URL!r}")

    official_value = _require_object_field(value, "official_dataset", context)
    official_dataset = OfficialDataTerms(
        id=_require_nonempty_string(official_value, "id", "UN Comtrade official_dataset"),
        name=_require_nonempty_string(official_value, "name", "UN Comtrade official_dataset"),
        terms_name=_require_nonempty_string(
            official_value,
            "terms_name",
            "UN Comtrade official_dataset",
        ),
        terms_url=_require_https_url(
            official_value,
            "terms_url",
            "UN Comtrade official_dataset",
        ),
        catalog_url=_require_https_url(
            official_value,
            "catalog_url",
            "UN Comtrade official_dataset",
        ),
    )
    expected_official = (
        "un-comtrade-final-goods-annual-hs",
        "UN Comtrade final annual goods data in reported HS classifications",
        "UN Comtrade Policy on use and re-dissemination",
        "https://uncomtrade.org/docs/policy-on-use-and-re-dissemination/",
        "https://comtradeplus.un.org/",
    )
    if (
        official_dataset.id,
        official_dataset.name,
        official_dataset.terms_name,
        official_dataset.terms_url,
        official_dataset.catalog_url,
    ) != expected_official:
        raise StructuredScopeValidationError(
            "UN Comtrade official_dataset does not match the reviewed terms evidence"
        )
    metadata_api_url = _require_https_url(value, "metadata_api_url", context)
    if metadata_api_url != COMTRADE_METADATA_API_URL:
        raise StructuredScopeValidationError(
            f"UN Comtrade metadata_api_url must be {COMTRADE_METADATA_API_URL!r}"
        )
    data_availability_api_url = _require_https_url(value, "data_availability_api_url", context)
    if data_availability_api_url != COMTRADE_DATA_AVAILABILITY_API_URL:
        raise StructuredScopeValidationError(
            f"UN Comtrade data_availability_api_url must be {COMTRADE_DATA_AVAILABILITY_API_URL!r}"
        )

    frequency = _require_nonempty_string(value, "frequency", context)
    period = _require_object_field(value, "baseline_period", context)
    start_year = _require_integer(period, "start_year", "UN Comtrade baseline_period")
    end_year = _require_integer(period, "end_year", "UN Comtrade baseline_period")
    if frequency != COMTRADE_FREQUENCY or (start_year, end_year) != (
        COMTRADE_START_YEAR,
        COMTRADE_END_YEAR,
    ):
        raise StructuredScopeValidationError(
            "UN Comtrade frequency and baseline period must remain annual 2017-2024"
        )

    reporters = tuple(
        _parse_comtrade_area(item, f"UN Comtrade reporters[{index}]")
        for index, item in enumerate(_require_list(value, "reporters", context))
    )
    partners = tuple(
        _parse_comtrade_area(item, f"UN Comtrade partners[{index}]")
        for index, item in enumerate(_require_list(value, "partners", context))
    )
    if tuple((area.iso3, area.source_code, area.source_iso3) for area in reporters) != (
        COMTRADE_REPORTER_AREAS
    ):
        raise StructuredScopeValidationError("UN Comtrade reporters do not match official references")
    if tuple((area.iso3, area.source_code, area.source_iso3) for area in partners) != (
        COMTRADE_PARTNER_AREAS
    ):
        raise StructuredScopeValidationError("UN Comtrade partners do not match official references")

    raw_flows = _require_list(value, "flows", context)
    if any(not isinstance(flow, str) or not flow.strip() for flow in raw_flows):
        raise StructuredScopeValidationError("UN Comtrade flows must be non-empty strings")
    flows = tuple(flow.strip() for flow in raw_flows)
    if flows != COMTRADE_FLOWS:
        raise StructuredScopeValidationError(f"UN Comtrade flows must be ordered as {COMTRADE_FLOWS!r}")
    classification_query = _require_nonempty_string(value, "classification_query", context)
    if classification_query != COMTRADE_CLASSIFICATION_QUERY:
        raise StructuredScopeValidationError("UN Comtrade classification_query must be 'HS'")
    _require_nonempty_string(value, "classification_policy", context)
    raw_classifications = _require_list(value, "verified_actual_classifications", context)
    if tuple(raw_classifications) != COMTRADE_ACTUAL_CLASSIFICATIONS:
        raise StructuredScopeValidationError("UN Comtrade verified classifications must cover H0 through H6")
    reference_template = _require_nonempty_string(
        value,
        "classification_reference_url_template",
        context,
    )
    if reference_template != COMTRADE_CLASSIFICATION_REFERENCE_URL_TEMPLATE:
        raise StructuredScopeValidationError(
            "UN Comtrade classification reference template is outside the reviewed boundary"
        )

    commodities = tuple(
        _parse_comtrade_commodity(item, index)
        for index, item in enumerate(_require_list(value, "commodities", context))
    )
    if (
        tuple(
            (
                commodity.mineral,
                commodity.hs6,
                commodity.name_zh,
                commodity.verified_meaning,
                commodity.standard_unit,
            )
            for commodity in commodities
        )
        != COMTRADE_COMMODITIES
    ):
        raise StructuredScopeValidationError(
            "UN Comtrade commodities do not match the reviewed H0-H6 references"
        )
    metrics = tuple(
        _parse_comtrade_metric(item, index)
        for index, item in enumerate(_require_list(value, "metrics", context))
    )
    if (
        tuple(
            (
                metric.metric_code,
                metric.source_field,
                metric.unit_policy,
                metric.unit,
                metric.currency,
            )
            for metric in metrics
        )
        != COMTRADE_METRICS
    ):
        raise StructuredScopeValidationError("UN Comtrade metrics do not match the reviewed fields")

    logical_dimension_cells = _require_integer(value, "logical_dimension_cells", context)
    request_shards = _require_integer(value, "request_shards", context)
    metadata_requests = _require_integer(value, "metadata_requests", context)
    data_availability_requests = _require_integer(value, "data_availability_requests", context)
    minimum_metric_observation_cells = _require_integer(
        value,
        "minimum_metric_observation_cells",
        context,
    )
    _require_nonempty_string(value, "observation_count_policy", context)
    years = end_year - start_year + 1
    if (
        logical_dimension_cells != len(reporters) * len(partners) * len(flows) * len(commodities) * years
        or logical_dimension_cells != COMTRADE_LOGICAL_DIMENSION_CELLS
        or request_shards != len(reporters) * len(commodities) * years
        or request_shards != COMTRADE_REQUEST_SHARDS
        or metadata_requests != len(reporters)
        or metadata_requests != COMTRADE_METADATA_REQUESTS
        or data_availability_requests != len(reporters)
        or data_availability_requests != COMTRADE_DATA_AVAILABILITY_REQUESTS
        or minimum_metric_observation_cells != logical_dimension_cells * len(metrics)
        or minimum_metric_observation_cells != COMTRADE_MINIMUM_METRIC_OBSERVATION_CELLS
    ):
        raise StructuredScopeValidationError(
            "UN Comtrade counts must remain 384 logical cells, 96 shards, "
            "4 metadata requests, 4 data-availability requests, and at least 1152 metrics"
        )

    request_policy = _require_object_field(value, "request_policy", context)
    _validate_comtrade_request_policy(request_policy)
    return ComtradeScope(
        dataset_key=dataset_key,
        source_catalog_row=COMTRADE_CATALOG_ROW,
        name=source_name,
        api_base_url=api_base_url,
        metadata_api_url=metadata_api_url,
        data_availability_api_url=data_availability_api_url,
        official_dataset=official_dataset,
        frequency=frequency,
        start_year=start_year,
        end_year=end_year,
        reporters=reporters,
        partners=partners,
        flows=flows,
        classification_query=classification_query,
        actual_classifications=tuple(raw_classifications),
        classification_reference_url_template=reference_template,
        commodities=commodities,
        metrics=metrics,
        logical_dimension_cells=logical_dimension_cells,
        request_shards=request_shards,
        metadata_requests=metadata_requests,
        data_availability_requests=data_availability_requests,
        minimum_metric_observation_cells=minimum_metric_observation_cells,
        max_records_per_request=COMTRADE_MAX_RECORDS,
    )


def _parse_comtrade_area(value: Any, context: str) -> ComtradeArea:
    item = _require_object(value, context)
    if set(item) != {"iso3", "source_code", "source_iso3"}:
        raise StructuredScopeValidationError(f"{context} fields do not match the schema")
    return ComtradeArea(
        iso3=_require_nonempty_string(item, "iso3", context),
        source_code=_require_integer(item, "source_code", context),
        source_iso3=_require_nonempty_string(item, "source_iso3", context),
    )


def _parse_comtrade_commodity(value: Any, index: int) -> ComtradeCommodity:
    context = f"UN Comtrade commodities[{index}]"
    item = _require_object(value, context)
    if set(item) != {"mineral", "hs6", "name_zh", "verified_meaning", "standard_unit"}:
        raise StructuredScopeValidationError(f"{context} fields do not match the schema")
    return ComtradeCommodity(
        mineral=_require_nonempty_string(item, "mineral", context),
        hs6=_require_nonempty_string(item, "hs6", context),
        name_zh=_require_nonempty_string(item, "name_zh", context),
        verified_meaning=_require_nonempty_string(item, "verified_meaning", context),
        standard_unit=_require_nonempty_string(item, "standard_unit", context),
    )


def _parse_comtrade_metric(value: Any, index: int) -> ComtradeMetric:
    context = f"UN Comtrade metrics[{index}]"
    item = _require_object(value, context)
    if set(item) != {"metric_code", "source_field", "unit_policy", "unit", "currency"}:
        raise StructuredScopeValidationError(f"{context} fields do not match the schema")
    return ComtradeMetric(
        metric_code=_require_nonempty_string(item, "metric_code", context),
        source_field=_require_nonempty_string(item, "source_field", context),
        unit_policy=_require_nonempty_string(item, "unit_policy", context),
        unit=_require_optional_string(item, "unit", context),
        currency=_require_optional_string(item, "currency", context),
    )


def _validate_comtrade_request_policy(value: dict[str, Any]) -> None:
    context = "UN Comtrade request_policy"
    if _require_boolean(value, "preview_for_schema_probe_only", context) is not True:
        raise StructuredScopeValidationError(f"{context}.preview_for_schema_probe_only must be true")
    if _require_boolean(value, "free_subscription_required_for_complete_collection", context) is not True:
        raise StructuredScopeValidationError(
            f"{context}.free_subscription_required_for_complete_collection must be true"
        )
    if _require_boolean(value, "premium_bulk_allowed", context) is not False:
        raise StructuredScopeValidationError(f"{context}.premium_bulk_allowed must be false")
    _require_nonempty_string(value, "credential_policy", context)
    if _require_integer(value, "max_records_per_request", context) != COMTRADE_MAX_RECORDS:
        raise StructuredScopeValidationError(f"{context}.max_records_per_request must be 500")
    if _require_boolean(value, "reject_when_record_count_reaches_limit", context) is not True:
        raise StructuredScopeValidationError(f"{context}.reject_when_record_count_reaches_limit must be true")
    if _require_list(value, "shard_by", context) != ["reporter", "period", "commodity"]:
        raise StructuredScopeValidationError(f"{context}.shard_by must be reporter, period, commodity")
    if (
        value.get("format") != "JSON"
        or value.get("breakdownMode") != "classic"
        or _require_boolean(value, "includeDesc", context) is not False
        or _require_boolean(value, "require_original_classification", context) is not True
    ):
        raise StructuredScopeValidationError(
            "UN Comtrade request format, breakdown, and classification policy changed"
        )
    fixed = _require_object_field(value, "fixed_dimensions", context)
    if fixed != {"partner2Code": 0, "customsCode": "C00", "motCode": 0}:
        raise StructuredScopeValidationError(
            "UN Comtrade fixed partner2, customs, and transport dimensions changed"
        )
    if _require_boolean(value, "reject_rows_outside_fixed_dimensions", context) is not True:
        raise StructuredScopeValidationError(f"{context}.reject_rows_outside_fixed_dimensions must be true")
    if _require_boolean(value, "raw_response_storage", context) is not False:
        raise StructuredScopeValidationError(f"{context}.raw_response_storage must be false")


def _is_reviewed_comtrade_scope(scope: ComtradeScope) -> bool:
    return (
        scope.dataset_key == COMTRADE_DATASET_KEY
        and scope.source_catalog_row == COMTRADE_CATALOG_ROW
        and scope.api_base_url == COMTRADE_API_BASE_URL
        and scope.metadata_api_url == COMTRADE_METADATA_API_URL
        and scope.data_availability_api_url == COMTRADE_DATA_AVAILABILITY_API_URL
        and scope.frequency == COMTRADE_FREQUENCY
        and (scope.start_year, scope.end_year) == (COMTRADE_START_YEAR, COMTRADE_END_YEAR)
        and tuple((area.iso3, area.source_code, area.source_iso3) for area in scope.reporters)
        == COMTRADE_REPORTER_AREAS
        and tuple((area.iso3, area.source_code, area.source_iso3) for area in scope.partners)
        == COMTRADE_PARTNER_AREAS
        and scope.flows == COMTRADE_FLOWS
        and scope.classification_query == COMTRADE_CLASSIFICATION_QUERY
        and scope.actual_classifications == COMTRADE_ACTUAL_CLASSIFICATIONS
        and tuple(commodity.hs6 for commodity in scope.commodities)
        == tuple(item[1] for item in COMTRADE_COMMODITIES)
        and tuple(metric.metric_code for metric in scope.metrics)
        == tuple(item[0] for item in COMTRADE_METRICS)
        and scope.logical_dimension_cells == COMTRADE_LOGICAL_DIMENSION_CELLS
        and scope.request_shards == COMTRADE_REQUEST_SHARDS
        and scope.metadata_requests == COMTRADE_METADATA_REQUESTS
        and scope.data_availability_requests == COMTRADE_DATA_AVAILABILITY_REQUESTS
        and scope.minimum_metric_observation_cells == COMTRADE_MINIMUM_METRIC_OBSERVATION_CELLS
        and scope.max_records_per_request == COMTRADE_MAX_RECORDS
    )


def _parse_world_bank_indicator(value: Any, index: int) -> WorldBankIndicator:
    context = f"World Bank indicators[{index}]"
    item = _require_object(value, context)
    return WorldBankIndicator(
        code=_require_nonempty_string(item, "code", context),
        name_zh=_require_nonempty_string(item, "name_zh", context),
        metric_code=_require_nonempty_string(item, "metric_code", context),
        unit=_require_nonempty_string(item, "unit", context),
        currency=_require_optional_string(item, "currency", context),
        price_basis=_require_optional_string(item, "price_basis", context),
    )


def _default_scope_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "structured_collection_scope.json"


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredScopeValidationError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise StructuredScopeValidationError(f"non-JSON numeric constant is not allowed: {value}")


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StructuredScopeValidationError(f"{context} must be an object")
    return value


def _require_object_field(value: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    if key not in value:
        raise StructuredScopeValidationError(f"{context}.{key} is required")
    return _require_object(value[key], f"{context}.{key}")


def _require_list(value: dict[str, Any], key: str, context: str) -> list[Any]:
    if key not in value:
        raise StructuredScopeValidationError(f"{context}.{key} is required")
    item = value[key]
    if not isinstance(item, list):
        raise StructuredScopeValidationError(f"{context}.{key} must be an array")
    return item


def _require_nonempty_string(value: dict[str, Any], key: str, context: str) -> str:
    if key not in value:
        raise StructuredScopeValidationError(f"{context}.{key} is required")
    item = value[key]
    if not isinstance(item, str) or not item.strip():
        raise StructuredScopeValidationError(f"{context}.{key} must be a non-empty string")
    return item.strip()


def _require_optional_string(value: dict[str, Any], key: str, context: str) -> str | None:
    if key not in value:
        raise StructuredScopeValidationError(f"{context}.{key} is required")
    item = value[key]
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise StructuredScopeValidationError(f"{context}.{key} must be null or a non-empty string")
    return item.strip()


def _require_integer(value: dict[str, Any], key: str, context: str) -> int:
    if key not in value:
        raise StructuredScopeValidationError(f"{context}.{key} is required")
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, int):
        raise StructuredScopeValidationError(f"{context}.{key} must be an integer")
    return item


def _require_boolean(value: dict[str, Any], key: str, context: str) -> bool:
    if key not in value:
        raise StructuredScopeValidationError(f"{context}.{key} is required")
    item = value[key]
    if type(item) is not bool:
        raise StructuredScopeValidationError(f"{context}.{key} must be a boolean")
    return item


def _require_https_url(value: dict[str, Any], key: str, context: str) -> str:
    url = _require_nonempty_string(value, key, context)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise StructuredScopeValidationError(f"{context}.{key} must be a safe HTTPS URL") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise StructuredScopeValidationError(f"{context}.{key} must be a safe HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise StructuredScopeValidationError(f"{context}.{key} must be a safe HTTPS URL")
    return url
