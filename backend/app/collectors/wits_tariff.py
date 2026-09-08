from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.collectors.base import FetchedResource

WITS_COUNTRIES = (
    ("COD", "180", "ZAR"),
    ("ZWE", "716", "ZWE"),
    ("ZMB", "894", "ZMB"),
    ("ZAF", "710", "ZAF"),
)
WITS_YEARS = tuple(range(2015, 2022))
WITS_PRODUCTS = ("260300", "260500", "283691")
WITS_AVAILABILITY_URL = (
    "https://wits.worldbank.org/API/V1/wits/datasource/trn/dataavailability/"
    "country/180;716;894;710/year/2015;2016;2017;2018;2019;2020;2021"
)
_COUNTRY_BY_REPORTER = {reporter: (country, source_iso3) for country, reporter, source_iso3 in WITS_COUNTRIES}


class WitsTariffParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WitsTariffSchedule:
    country_iso3: str
    source_reporter_code: str
    source_iso3: str
    period: int
    nomenclature: str


@dataclass(frozen=True, slots=True)
class WitsTariffAvailabilityResult:
    schedules: tuple[WitsTariffSchedule, ...]


@dataclass(frozen=True, slots=True)
class WitsTariffObservation:
    country_iso3: str
    source_reporter_code: str
    source_iso3: str
    period: int
    commodity_code: str
    commodity_classification: str
    value: Decimal | None
    total_lines: int
    preferential_lines: int
    mfn_lines: int
    non_ad_valorem_lines: int
    sum_of_rates: Decimal
    min_rate: Decimal
    max_rate: Decimal


@dataclass(frozen=True, slots=True)
class WitsTariffShardResult:
    country_iso3: str
    source_reporter_code: str
    source_iso3: str
    period: int
    commodity_classification: str
    observations: tuple[WitsTariffObservation, ...]


@dataclass(frozen=True, slots=True)
class WitsTariffCell:
    country_iso3: str
    source_reporter_code: str
    source_iso3: str
    period: int
    commodity_code: str
    commodity_classification: str | None
    value: Decimal | None
    missing_reason: str | None
    source_url: str
    total_lines: int | None
    preferential_lines: int | None
    mfn_lines: int | None
    non_ad_valorem_lines: int | None
    sum_of_rates: Decimal | None
    min_rate: Decimal | None
    max_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class WitsTariffPanel:
    cells: tuple[WitsTariffCell, ...]
    available_schedule_count: int
    candidate_cell_count: int
    returned_count: int
    valued_count: int
    source_null_count: int
    not_returned_count: int


def build_wits_tariff_url(source_reporter_code: str, period: int) -> str:
    if source_reporter_code not in _COUNTRY_BY_REPORTER or period not in WITS_YEARS:
        raise ValueError("WITS reporter-year is outside the reviewed scope")
    products = ";".join(WITS_PRODUCTS)
    return (
        "https://wits.worldbank.org/API/V1/SDMX/V21/datasource/TRN/"
        f"reporter/{source_reporter_code}/partner/000/product/{products}/"
        f"year/{period}/datatype/reported"
    )


def parse_wits_tariff_availability(resource: FetchedResource) -> WitsTariffAvailabilityResult:
    root = _xml_root(resource, "invalid_availability_xml")
    if _local_name(root.tag) != "datasource" or root.attrib.get("datasourcecode") != "TRN":
        raise WitsTariffParseError(
            "invalid_availability_envelope",
            "WITS availability response has an unexpected data source",
        )

    reporters = [element for element in root.iter() if _local_name(element.tag) == "reporter"]
    schedules: dict[tuple[str, int], WitsTariffSchedule] = {}
    for reporter in reporters:
        source_reporter_code = reporter.attrib.get("countrycode")
        source_iso3 = reporter.attrib.get("iso3Code")
        expected_country = _COUNTRY_BY_REPORTER.get(source_reporter_code or "")
        if expected_country is None or source_iso3 != expected_country[1]:
            raise WitsTariffParseError(
                "availability_out_of_scope",
                "WITS availability response contains an unexpected reporter",
            )
        year_text = _single_descendant_text(reporter, "year", "invalid_availability_year")
        try:
            period = int(year_text)
        except ValueError as exc:
            raise WitsTariffParseError(
                "invalid_availability_year",
                "WITS availability year is invalid",
            ) from exc
        if period not in WITS_YEARS:
            raise WitsTariffParseError(
                "availability_out_of_scope",
                "WITS availability response contains an out-of-scope year",
            )
        nomenclature_element = _single_descendant(
            reporter,
            "reporternernomenclature",
            "invalid_availability_nomenclature",
        )
        nomenclature = nomenclature_element.attrib.get("reporternernomenclaturecode")
        if nomenclature not in {"H4", "H5"}:
            raise WitsTariffParseError(
                "invalid_availability_nomenclature",
                "WITS availability nomenclature is outside the reviewed classifications",
            )
        identity = (source_reporter_code, period)
        if identity in schedules:
            raise WitsTariffParseError(
                "duplicate_availability",
                "WITS availability response repeats a reporter-year",
            )
        schedules[identity] = WitsTariffSchedule(
            country_iso3=expected_country[0],
            source_reporter_code=source_reporter_code,
            source_iso3=source_iso3,
            period=period,
            nomenclature=nomenclature,
        )

    total_text = root.attrib.get("total")
    try:
        total = int(total_text or "")
    except ValueError as exc:
        raise WitsTariffParseError(
            "invalid_availability_total",
            "WITS availability total is invalid",
        ) from exc
    if total != len(schedules) or not schedules:
        raise WitsTariffParseError(
            "invalid_availability_total",
            "WITS availability total does not match the parsed schedules",
        )

    ordered = tuple(
        schedules[(reporter, period)]
        for _country, reporter, _source_iso3 in WITS_COUNTRIES
        for period in WITS_YEARS
        if (reporter, period) in schedules
    )
    return WitsTariffAvailabilityResult(schedules=ordered)


def parse_wits_tariff(
    resource: FetchedResource,
    *,
    schedule: WitsTariffSchedule,
) -> WitsTariffShardResult:
    root = _xml_root(resource, "invalid_tariff_xml")
    if _local_name(root.tag) != "StructureSpecificData":
        raise WitsTariffParseError(
            "invalid_tariff_envelope",
            "WITS tariff response is not structure-specific SDMX data",
        )
    datasets = [element for element in root.iter() if _local_name(element.tag) == "DataSet"]
    if len(datasets) != 1 or datasets[0].attrib.get("DATASOURCE") != "TRN":
        raise WitsTariffParseError(
            "invalid_tariff_dataset",
            "WITS tariff response has an unexpected dataset",
        )

    observations: dict[str, WitsTariffObservation] = {}
    series_elements = [element for element in datasets[0] if _local_name(element.tag) == "Series"]
    for series in series_elements:
        expected_series = {
            "FREQ": "A",
            "DATATYPE": "Reported",
            "PARTNER": "000",
            "REPORTER": schedule.source_reporter_code,
        }
        if any(series.attrib.get(key) != value for key, value in expected_series.items()):
            raise WitsTariffParseError(
                "tariff_series_out_of_scope",
                "WITS tariff series is outside the reviewed dimensions",
            )
        product = series.attrib.get("PRODUCTCODE")
        if product not in WITS_PRODUCTS or set(series.attrib) != {*expected_series, "PRODUCTCODE"}:
            raise WitsTariffParseError(
                "invalid_tariff_series",
                "WITS tariff series has an unexpected product or schema",
            )
        if product in observations:
            raise WitsTariffParseError(
                "duplicate_tariff_observation",
                "WITS tariff response repeats a product",
            )
        obs_elements = [element for element in series if _local_name(element.tag) == "Obs"]
        if len(obs_elements) != 1:
            raise WitsTariffParseError(
                "invalid_tariff_observation",
                "WITS tariff series must contain exactly one observation",
            )
        obs = obs_elements[0]
        required = {
            "TIME_PERIOD",
            "OBS_VALUE",
            "TARIFFTYPE",
            "OBS_VALUE_MEASURE",
            "TOTALNOOFLINES",
            "NBR_PREF_LINES",
            "NBR_MFN_LINES",
            "NBR_NA_LINES",
            "SUM_OF_RATES",
            "MIN_RATE",
            "MAX_RATE",
            "NOMENCODE",
        }
        if set(obs.attrib) != required:
            raise WitsTariffParseError(
                "invalid_tariff_observation",
                "WITS tariff observation schema differs from the reviewed response",
            )
        if (
            obs.attrib["TIME_PERIOD"] != str(schedule.period)
            or obs.attrib["TARIFFTYPE"] != "MFN"
            or obs.attrib["OBS_VALUE_MEASURE"] != "SimpleAverage"
            or obs.attrib["NOMENCODE"] != schedule.nomenclature
        ):
            raise WitsTariffParseError(
                "tariff_observation_out_of_scope",
                "WITS tariff observation differs from the reviewed scope",
            )
        observations[product] = WitsTariffObservation(
            country_iso3=schedule.country_iso3,
            source_reporter_code=schedule.source_reporter_code,
            source_iso3=schedule.source_iso3,
            period=schedule.period,
            commodity_code=product,
            commodity_classification=schedule.nomenclature,
            value=_optional_decimal(obs.attrib["OBS_VALUE"], "OBS_VALUE"),
            total_lines=_nonnegative_int(obs.attrib["TOTALNOOFLINES"], "TOTALNOOFLINES"),
            preferential_lines=_nonnegative_int(obs.attrib["NBR_PREF_LINES"], "NBR_PREF_LINES"),
            mfn_lines=_nonnegative_int(obs.attrib["NBR_MFN_LINES"], "NBR_MFN_LINES"),
            non_ad_valorem_lines=_nonnegative_int(obs.attrib["NBR_NA_LINES"], "NBR_NA_LINES"),
            sum_of_rates=_decimal(obs.attrib["SUM_OF_RATES"], "SUM_OF_RATES"),
            min_rate=_decimal(obs.attrib["MIN_RATE"], "MIN_RATE"),
            max_rate=_decimal(obs.attrib["MAX_RATE"], "MAX_RATE"),
        )

    return WitsTariffShardResult(
        country_iso3=schedule.country_iso3,
        source_reporter_code=schedule.source_reporter_code,
        source_iso3=schedule.source_iso3,
        period=schedule.period,
        commodity_classification=schedule.nomenclature,
        observations=tuple(observations[product] for product in WITS_PRODUCTS if product in observations),
    )


def build_wits_tariff_panel(
    availability: WitsTariffAvailabilityResult,
    shards: Mapping[tuple[str, int], WitsTariffShardResult],
) -> WitsTariffPanel:
    schedules = {
        (schedule.source_reporter_code, schedule.period): schedule for schedule in availability.schedules
    }
    if set(shards) != set(schedules):
        raise WitsTariffParseError(
            "incomplete_tariff_shards",
            "WITS tariff shards do not match the available schedules",
        )

    cells: list[WitsTariffCell] = []
    returned_count = 0
    valued_count = 0
    source_null_count = 0
    for country, reporter, source_iso3 in WITS_COUNTRIES:
        for period in WITS_YEARS:
            schedule = schedules.get((reporter, period))
            shard = shards.get((reporter, period))
            if schedule is not None and (
                shard is None
                or shard.country_iso3 != country
                or shard.source_iso3 != source_iso3
                or shard.commodity_classification != schedule.nomenclature
            ):
                raise WitsTariffParseError(
                    "tariff_shard_mismatch",
                    "WITS tariff shard identity differs from availability",
                )
            by_product = (
                {observation.commodity_code: observation for observation in shard.observations}
                if shard is not None
                else {}
            )
            for product in WITS_PRODUCTS:
                observation = by_product.get(product)
                if schedule is None:
                    cells.append(
                        _missing_cell(
                            country=country,
                            reporter=reporter,
                            source_iso3=source_iso3,
                            period=period,
                            product=product,
                            classification=None,
                            reason="tariff_schedule_unavailable",
                            source_url=WITS_AVAILABILITY_URL,
                        )
                    )
                    continue
                source_url = build_wits_tariff_url(reporter, period)
                if observation is None:
                    cells.append(
                        _missing_cell(
                            country=country,
                            reporter=reporter,
                            source_iso3=source_iso3,
                            period=period,
                            product=product,
                            classification=schedule.nomenclature,
                            reason="product_not_returned",
                            source_url=source_url,
                        )
                    )
                    continue
                returned_count += 1
                missing_reason = None if observation.value is not None else "source_null"
                if missing_reason is None:
                    valued_count += 1
                else:
                    source_null_count += 1
                cells.append(
                    WitsTariffCell(
                        country_iso3=country,
                        source_reporter_code=reporter,
                        source_iso3=source_iso3,
                        period=period,
                        commodity_code=product,
                        commodity_classification=schedule.nomenclature,
                        value=observation.value,
                        missing_reason=missing_reason,
                        source_url=source_url,
                        total_lines=observation.total_lines,
                        preferential_lines=observation.preferential_lines,
                        mfn_lines=observation.mfn_lines,
                        non_ad_valorem_lines=observation.non_ad_valorem_lines,
                        sum_of_rates=observation.sum_of_rates,
                        min_rate=observation.min_rate,
                        max_rate=observation.max_rate,
                    )
                )

    expected_count = len(WITS_COUNTRIES) * len(WITS_YEARS) * len(WITS_PRODUCTS)
    if len(cells) != expected_count:
        raise WitsTariffParseError(
            "incomplete_tariff_panel",
            "WITS tariff panel does not contain the fixed 84 cells",
        )
    return WitsTariffPanel(
        cells=tuple(cells),
        available_schedule_count=len(schedules),
        candidate_cell_count=len(schedules) * len(WITS_PRODUCTS),
        returned_count=returned_count,
        valued_count=valued_count,
        source_null_count=source_null_count,
        not_returned_count=expected_count - returned_count,
    )


def _missing_cell(
    *,
    country: str,
    reporter: str,
    source_iso3: str,
    period: int,
    product: str,
    classification: str | None,
    reason: str,
    source_url: str,
) -> WitsTariffCell:
    return WitsTariffCell(
        country_iso3=country,
        source_reporter_code=reporter,
        source_iso3=source_iso3,
        period=period,
        commodity_code=product,
        commodity_classification=classification,
        value=None,
        missing_reason=reason,
        source_url=source_url,
        total_lines=None,
        preferential_lines=None,
        mfn_lines=None,
        non_ad_valorem_lines=None,
        sum_of_rates=None,
        min_rate=None,
        max_rate=None,
    )


def _xml_root(resource: FetchedResource, code: str) -> ET.Element:
    try:
        return ET.fromstring(resource.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise WitsTariffParseError(code, "WITS response is not valid XML") from exc


def _single_descendant(element: ET.Element, name: str, code: str) -> ET.Element:
    matches = [item for item in element.iter() if _local_name(item.tag) == name]
    if len(matches) != 1:
        raise WitsTariffParseError(code, f"WITS {name} must occur exactly once")
    return matches[0]


def _single_descendant_text(element: ET.Element, name: str, code: str) -> str:
    item = _single_descendant(element, name, code)
    text = (item.text or "").strip()
    if not text:
        raise WitsTariffParseError(code, f"WITS {name} must have text")
    return text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _optional_decimal(value: str, name: str) -> Decimal | None:
    if not value.strip():
        return None
    return _decimal(value, name)


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise WitsTariffParseError("invalid_tariff_value", f"WITS {name} is invalid") from exc
    if not parsed.is_finite():
        raise WitsTariffParseError("invalid_tariff_value", f"WITS {name} must be finite")
    return parsed


def _nonnegative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise WitsTariffParseError("invalid_tariff_value", f"WITS {name} is invalid") from exc
    if parsed < 0:
        raise WitsTariffParseError("invalid_tariff_value", f"WITS {name} must be nonnegative")
    return parsed
