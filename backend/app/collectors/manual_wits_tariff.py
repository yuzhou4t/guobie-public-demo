from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.collectors.wits_tariff import (
    WITS_AVAILABILITY_URL,
    WITS_COUNTRIES,
    WITS_PRODUCTS,
    WITS_YEARS,
    WitsTariffCell,
    WitsTariffObservation,
    WitsTariffPanel,
    WitsTariffShardResult,
    build_wits_tariff_url,
)

WITS_MANUAL_SUPPLEMENT_SCHEDULES = frozenset(
    {
        ("180", 2015),
        ("180", 2016),
        ("180", 2019),
        ("180", 2020),
        ("716", 2017),
        ("716", 2018),
        ("716", 2019),
        ("716", 2020),
    }
)
WITS_CONFIRMED_UNAVAILABLE_SCHEDULES = frozenset(
    {
        ("180", 2017),
        ("180", 2018),
        ("180", 2021),
        ("894", 2017),
        ("894", 2019),
    }
)

_EXPECTED_COLUMNS = (
    "NomenCode",
    "Reporter_ISO_N",
    "ReporterName",
    "ProductCode",
    "Partner",
    "PartnerName",
    "Year",
    "AdValorem",
    "MeasureCode",
    "MeasureName",
    "NonAdValorem",
    "Affected",
)
_COUNTRY_BY_REPORTER = {
    reporter: (country_iso3, source_iso3) for country_iso3, reporter, source_iso3 in WITS_COUNTRIES
}


class ManualWitsTariffError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_manual_wits_tariff_zip(path: Path) -> WitsTariffShardResult:
    try:
        with zipfile.ZipFile(path) as archive:
            csv_members = [
                item
                for item in archive.infolist()
                if not item.is_dir() and item.filename.lower().endswith(".csv")
            ]
            if len(csv_members) != 1:
                raise ManualWitsTariffError(
                    "manual_wits_csv_member_count",
                    f"{path.name} must contain exactly one CSV file",
                )
            member = csv_members[0]
            if member.file_size > 50 * 1024 * 1024:
                raise ManualWitsTariffError(
                    "manual_wits_csv_too_large",
                    f"{path.name} contains an oversized CSV file",
                )
            body = archive.read(member)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ManualWitsTariffError(
            "manual_wits_zip_invalid",
            f"{path.name} is not a readable ZIP file",
        ) from exc

    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ManualWitsTariffError(
            "manual_wits_csv_encoding",
            f"{path.name} CSV is not UTF-8",
        ) from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != _EXPECTED_COLUMNS:
        raise ManualWitsTariffError(
            "manual_wits_csv_schema",
            f"{path.name} CSV columns differ from the reviewed WITS export",
        )

    schedule: tuple[str, int, str] | None = None
    rates_by_product: dict[str, list[Decimal]] = {product: [] for product in WITS_PRODUCTS}
    non_ad_valorem_by_product = {product: 0 for product in WITS_PRODUCTS}
    row_count = 0
    for row_number, row in enumerate(reader, start=2):
        row_count += 1
        if None in row:
            raise _row_error(path, row_number, "CSV row has unexpected extra columns")
        reporter = (row["Reporter_ISO_N"] or "").strip()
        country = _COUNTRY_BY_REPORTER.get(reporter)
        if country is None:
            raise _row_error(path, row_number, "reporter is outside the reviewed WITS scope")
        try:
            period = int((row["Year"] or "").strip())
        except ValueError as exc:
            raise _row_error(path, row_number, "year is invalid") from exc
        nomenclature = (row["NomenCode"] or "").strip()
        if period not in WITS_YEARS or nomenclature not in {"H4", "H5"}:
            raise _row_error(path, row_number, "year or nomenclature is outside scope")
        current_schedule = (reporter, period, nomenclature)
        if schedule is None:
            schedule = current_schedule
        elif current_schedule != schedule:
            raise _row_error(path, row_number, "CSV mixes multiple reporter-year schedules")

        if (row["Partner"] or "").strip() != "000":
            raise _row_error(path, row_number, "partner must be 000 (World)")
        if (row["MeasureCode"] or "").strip() != "2":
            raise _row_error(path, row_number, "measure must be MFN duty rate")

        product_code = (row["ProductCode"] or "").strip()
        product = next(
            (candidate for candidate in WITS_PRODUCTS if product_code.startswith(candidate)),
            None,
        )
        if product is None:
            continue
        rate = _decimal(row["AdValorem"] or "", path=path, row_number=row_number)
        rates_by_product[product].append(rate)
        if (row["NonAdValorem"] or "").strip():
            non_ad_valorem_by_product[product] += 1

    if row_count == 0 or schedule is None:
        raise ManualWitsTariffError(
            "manual_wits_csv_empty",
            f"{path.name} CSV contains no tariff rows",
        )
    if row_count < 1000:
        raise ManualWitsTariffError(
            "manual_wits_csv_incomplete",
            f"{path.name} does not look like a complete tariff schedule",
        )
    reporter, period, nomenclature = schedule
    if (reporter, period) not in WITS_MANUAL_SUPPLEMENT_SCHEDULES:
        raise ManualWitsTariffError(
            "manual_wits_schedule_unexpected",
            f"{path.name} is not one of the eight reviewed supplement schedules",
        )

    country_iso3, source_iso3 = _COUNTRY_BY_REPORTER[reporter]
    observations: list[WitsTariffObservation] = []
    for product in WITS_PRODUCTS:
        rates = rates_by_product[product]
        if not rates:
            raise ManualWitsTariffError(
                "manual_wits_product_missing",
                f"{path.name} has no tariff lines for HS6 {product}",
            )
        sum_of_rates = sum(rates, Decimal(0))
        observations.append(
            WitsTariffObservation(
                country_iso3=country_iso3,
                source_reporter_code=reporter,
                source_iso3=source_iso3,
                period=period,
                commodity_code=product,
                commodity_classification=nomenclature,
                value=sum_of_rates / len(rates),
                total_lines=len(rates),
                preferential_lines=0,
                mfn_lines=len(rates),
                non_ad_valorem_lines=non_ad_valorem_by_product[product],
                sum_of_rates=sum_of_rates,
                min_rate=min(rates),
                max_rate=max(rates),
            )
        )
    return WitsTariffShardResult(
        country_iso3=country_iso3,
        source_reporter_code=reporter,
        source_iso3=source_iso3,
        period=period,
        commodity_classification=nomenclature,
        observations=tuple(observations),
    )


def merge_manual_wits_tariff_panel(
    base_panel: WitsTariffPanel,
    manual_shards: Mapping[tuple[str, int], WitsTariffShardResult],
) -> WitsTariffPanel:
    if set(manual_shards) != WITS_MANUAL_SUPPLEMENT_SCHEDULES:
        raise ManualWitsTariffError(
            "manual_wits_schedule_set",
            "manual WITS import must contain the exact eight reviewed ZIP schedules",
        )
    if len(base_panel.cells) != 84:
        raise ManualWitsTariffError(
            "manual_wits_base_panel",
            "latest WITS snapshot does not contain the fixed 84-cell panel",
        )

    manual_by_cell: dict[tuple[str, int, str], WitsTariffObservation] = {}
    for schedule_key, shard in manual_shards.items():
        if schedule_key != (shard.source_reporter_code, shard.period):
            raise ManualWitsTariffError(
                "manual_wits_shard_identity",
                "manual WITS shard identity differs from its schedule key",
            )
        for observation in shard.observations:
            manual_by_cell[(shard.source_reporter_code, shard.period, observation.commodity_code)] = (
                observation
            )

    cells: list[WitsTariffCell] = []
    for cell in base_panel.cells:
        schedule_key = (cell.source_reporter_code, cell.period)
        cell_key = (*schedule_key, cell.commodity_code)
        if schedule_key in WITS_MANUAL_SUPPLEMENT_SCHEDULES:
            observation = manual_by_cell.get(cell_key)
            if observation is None:
                raise ManualWitsTariffError(
                    "manual_wits_product_set",
                    "manual WITS schedules do not contain all three reviewed products",
                )
            if cell.value is not None and not _same_observation(cell, observation):
                raise ManualWitsTariffError(
                    "manual_wits_overlap_mismatch",
                    "manual WITS value differs from an existing reported observation",
                )
            cells.append(
                WitsTariffCell(
                    country_iso3=observation.country_iso3,
                    source_reporter_code=observation.source_reporter_code,
                    source_iso3=observation.source_iso3,
                    period=observation.period,
                    commodity_code=observation.commodity_code,
                    commodity_classification=observation.commodity_classification,
                    value=observation.value,
                    missing_reason=None,
                    source_url=build_wits_tariff_url(
                        observation.source_reporter_code,
                        observation.period,
                    ),
                    total_lines=observation.total_lines,
                    preferential_lines=observation.preferential_lines,
                    mfn_lines=observation.mfn_lines,
                    non_ad_valorem_lines=observation.non_ad_valorem_lines,
                    sum_of_rates=observation.sum_of_rates,
                    min_rate=observation.min_rate,
                    max_rate=observation.max_rate,
                )
            )
        elif schedule_key in WITS_CONFIRMED_UNAVAILABLE_SCHEDULES:
            cells.append(
                replace(
                    cell,
                    commodity_classification=None,
                    value=None,
                    missing_reason="tariff_schedule_unavailable",
                    source_url=WITS_AVAILABILITY_URL,
                    total_lines=None,
                    preferential_lines=None,
                    mfn_lines=None,
                    non_ad_valorem_lines=None,
                    sum_of_rates=None,
                    min_rate=None,
                    max_rate=None,
                )
            )
        else:
            cells.append(cell)

    valued_count = sum(cell.value is not None for cell in cells)
    source_null_count = sum(cell.missing_reason == "source_null" for cell in cells)
    returned_count = valued_count + source_null_count
    not_returned_count = len(cells) - returned_count
    if (valued_count, source_null_count, not_returned_count) != (69, 0, 15):
        raise ManualWitsTariffError(
            "manual_wits_final_coverage",
            "manual WITS merge must produce 69 valued and 15 unavailable cells",
        )
    return WitsTariffPanel(
        cells=tuple(cells),
        available_schedule_count=23,
        candidate_cell_count=69,
        returned_count=returned_count,
        valued_count=valued_count,
        source_null_count=source_null_count,
        not_returned_count=not_returned_count,
    )


def _same_observation(cell: WitsTariffCell, observation: WitsTariffObservation) -> bool:
    return (
        cell.country_iso3 == observation.country_iso3
        and cell.source_iso3 == observation.source_iso3
        and cell.commodity_classification == observation.commodity_classification
        and cell.value == observation.value
        and cell.total_lines == observation.total_lines
        and cell.preferential_lines == observation.preferential_lines
        and cell.mfn_lines == observation.mfn_lines
        and cell.non_ad_valorem_lines == observation.non_ad_valorem_lines
        and cell.sum_of_rates == observation.sum_of_rates
        and cell.min_rate == observation.min_rate
        and cell.max_rate == observation.max_rate
    )


def _decimal(value: str, *, path: Path, row_number: int) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation as exc:
        raise _row_error(path, row_number, "AdValorem is invalid") from exc
    if not parsed.is_finite():
        raise _row_error(path, row_number, "AdValorem must be finite")
    return parsed


def _row_error(path: Path, row_number: int, message: str) -> ManualWitsTariffError:
    return ManualWitsTariffError(
        "manual_wits_row_invalid",
        f"{path.name} row {row_number}: {message}",
    )
