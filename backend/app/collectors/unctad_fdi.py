from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO

import py7zr

from app.collectors.base import FetchedResource

UNCTAD_FDI_BULK_URL = "https://unctadstat-api.unctad.org/bulkdownload/US.FdiFlowsStock/US_FdiFlowsStock"
UNCTAD_FDI_COUNTRIES = (
    ("COD", "180", "Dem. Rep. of the Congo"),
    ("ZWE", "716", "Zimbabwe"),
    ("ZMB", "894", "Zambia"),
    ("ZAF", "710", "South Africa"),
)
UNCTAD_FDI_YEARS = tuple(range(2015, 2025))
UNCTAD_FDI_DIMENSIONS = (
    ("08", "Flow", "1", "Inward", "fdi_inward_flow_current_usd_millions"),
    ("08", "Flow", "2", "Outward", "fdi_outward_flow_current_usd_millions"),
    ("09", "Stock", "1", "Inward", "fdi_inward_stock_current_usd_millions"),
    ("09", "Stock", "2", "Outward", "fdi_outward_stock_current_usd_millions"),
)
_ARCHIVE_MEMBER = "US_FdiFlowsStock.csv"
_MAX_CSV_BYTES = 4 * 1024 * 1024
_VALUE_COLUMN = "Millions of US$ at current prices"
_FOOTNOTE_COLUMN = f"{_VALUE_COLUMN} Footnote"
_MISSING_COLUMN = f"{_VALUE_COLUMN} Missing value"
_CSV_HEADER = (
    "Year",
    "Economy",
    "Economy Label",
    "Flow",
    "Flow Label",
    "Direction",
    "Direction Label",
    _VALUE_COLUMN,
    _FOOTNOTE_COLUMN,
    _MISSING_COLUMN,
    "Percentage of total world",
    "Percentage of total world Footnote",
    "Percentage of total world Missing value",
    "Percentage of gross Domestic Product",
    "Percentage of gross Domestic Product Footnote",
    "Percentage of gross Domestic Product Missing value",
    "Percentage of gross Fixed Capital Formation",
    "Percentage of gross Fixed Capital Formation Footnote",
    "Percentage of gross Fixed Capital Formation Missing value",
)
_COUNTRY_BY_CODE = {
    source_code: (country_iso3, economy_label)
    for country_iso3, source_code, economy_label in UNCTAD_FDI_COUNTRIES
}
_DIMENSION_BY_CODES = {
    (flow_code, direction_code): (flow_label, direction_label, metric_code)
    for flow_code, flow_label, direction_code, direction_label, metric_code in UNCTAD_FDI_DIMENSIONS
}


class UnctadFdiParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class UnctadFdiCell:
    country_iso3: str
    source_economy_code: str
    economy_label: str
    period: int
    flow_code: str
    flow_label: str
    direction_code: str
    direction_label: str
    metric_code: str
    value: Decimal | None
    missing_reason: str | None
    source_missing_value: str | None
    footnote: str | None
    source_url: str


@dataclass(frozen=True, slots=True)
class UnctadFdiPanel:
    cells: tuple[UnctadFdiCell, ...]
    returned_count: int
    valued_count: int
    source_null_count: int
    not_returned_count: int


def parse_unctad_fdi(resource: FetchedResource) -> UnctadFdiPanel:
    csv_bytes = _extract_csv(resource.body)
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnctadFdiParseError(
            "invalid_csv_encoding",
            "UNCTAD FDI bulk CSV must be UTF-8",
        ) from exc

    reader = csv.DictReader(StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != _CSV_HEADER:
        raise UnctadFdiParseError(
            "invalid_csv_header",
            "UNCTAD FDI bulk CSV header differs from the reviewed schema",
        )

    parsed: dict[tuple[str, int, str, str], UnctadFdiCell] = {}
    for line_number, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise UnctadFdiParseError(
                "invalid_csv_row",
                f"UNCTAD FDI CSV row {line_number} has an invalid column count",
            )
        try:
            period = int(row["Year"])
        except ValueError as exc:
            raise UnctadFdiParseError(
                "invalid_year",
                f"UNCTAD FDI CSV row {line_number} has an invalid year",
            ) from exc
        source_code = row["Economy"]
        country = _COUNTRY_BY_CODE.get(source_code)
        if country is None or period not in UNCTAD_FDI_YEARS:
            continue

        country_iso3, expected_economy_label = country
        if row["Economy Label"] != expected_economy_label:
            raise UnctadFdiParseError(
                "country_identity_mismatch",
                f"UNCTAD FDI CSV row {line_number} has an unexpected economy label",
            )
        dimension = _DIMENSION_BY_CODES.get((row["Flow"], row["Direction"]))
        if dimension is None:
            raise UnctadFdiParseError(
                "dimension_out_of_scope",
                f"UNCTAD FDI CSV row {line_number} has an unexpected FDI dimension",
            )
        expected_flow_label, expected_direction_label, metric_code = dimension
        if row["Flow Label"] != expected_flow_label or row["Direction Label"] != expected_direction_label:
            raise UnctadFdiParseError(
                "dimension_identity_mismatch",
                f"UNCTAD FDI CSV row {line_number} has an unexpected dimension label",
            )

        value, missing_reason, source_missing_value = _parse_value(row, line_number)
        footnote = row[_FOOTNOTE_COLUMN].strip() or None
        if footnote is not None and len(footnote) > 2000:
            raise UnctadFdiParseError(
                "footnote_too_long",
                f"UNCTAD FDI CSV row {line_number} has an oversized footnote",
            )
        identity = (source_code, period, row["Flow"], row["Direction"])
        if identity in parsed:
            raise UnctadFdiParseError(
                "duplicate_observation",
                f"UNCTAD FDI CSV repeats a scoped observation at row {line_number}",
            )
        parsed[identity] = UnctadFdiCell(
            country_iso3=country_iso3,
            source_economy_code=source_code,
            economy_label=expected_economy_label,
            period=period,
            flow_code=row["Flow"],
            flow_label=expected_flow_label,
            direction_code=row["Direction"],
            direction_label=expected_direction_label,
            metric_code=metric_code,
            value=value,
            missing_reason=missing_reason,
            source_missing_value=source_missing_value,
            footnote=footnote,
            source_url=resource.final_url,
        )

    cells: list[UnctadFdiCell] = []
    for country_iso3, source_code, economy_label in UNCTAD_FDI_COUNTRIES:
        for period in UNCTAD_FDI_YEARS:
            for flow_code, flow_label, direction_code, direction_label, metric_code in UNCTAD_FDI_DIMENSIONS:
                identity = (source_code, period, flow_code, direction_code)
                cell = parsed.get(identity)
                if cell is None:
                    cell = UnctadFdiCell(
                        country_iso3=country_iso3,
                        source_economy_code=source_code,
                        economy_label=economy_label,
                        period=period,
                        flow_code=flow_code,
                        flow_label=flow_label,
                        direction_code=direction_code,
                        direction_label=direction_label,
                        metric_code=metric_code,
                        value=None,
                        missing_reason="not_returned",
                        source_missing_value=None,
                        footnote=None,
                        source_url=resource.final_url,
                    )
                cells.append(cell)

    valued_count = sum(cell.value is not None for cell in cells)
    source_null_count = sum(cell.missing_reason == "source_null" for cell in cells)
    not_returned_count = sum(cell.missing_reason == "not_returned" for cell in cells)
    return UnctadFdiPanel(
        cells=tuple(cells),
        returned_count=len(parsed),
        valued_count=valued_count,
        source_null_count=source_null_count,
        not_returned_count=not_returned_count,
    )


def _extract_csv(body: bytes) -> bytes:
    if not body.startswith(b"7z\xbc\xaf'\x1c"):
        raise UnctadFdiParseError(
            "invalid_archive_header",
            "UNCTAD FDI bulk response is not a 7z archive",
        )
    try:
        with py7zr.SevenZipFile(BytesIO(body), mode="r") as archive:
            members = archive.list()
            if (
                len(members) != 1
                or members[0].filename != _ARCHIVE_MEMBER
                or members[0].is_directory
                or members[0].uncompressed is None
                or members[0].uncompressed > _MAX_CSV_BYTES
            ):
                raise UnctadFdiParseError(
                    "invalid_archive_members",
                    "UNCTAD FDI archive must contain one bounded reviewed CSV member",
                )
            extracted = archive.readall()
    except UnctadFdiParseError:
        raise
    except (
        py7zr.Bad7zFile,
        py7zr.DecompressionError,
        py7zr.PasswordRequired,
        py7zr.UnsupportedCompressionMethodError,
        EOFError,
        OSError,
        ValueError,
    ) as exc:
        raise UnctadFdiParseError(
            "invalid_archive",
            "UNCTAD FDI bulk archive could not be safely decompressed",
        ) from exc
    if not isinstance(extracted, dict) or set(extracted) != {_ARCHIVE_MEMBER}:
        raise UnctadFdiParseError(
            "invalid_archive_members",
            "UNCTAD FDI archive extraction differs from the reviewed member list",
        )
    csv_bytes = extracted[_ARCHIVE_MEMBER].read(_MAX_CSV_BYTES + 1)
    if len(csv_bytes) > _MAX_CSV_BYTES:
        raise UnctadFdiParseError(
            "csv_too_large",
            "UNCTAD FDI decompressed CSV exceeds the parser limit",
        )
    return csv_bytes


def _parse_value(
    row: dict[str | None, str | list[str]],
    line_number: int,
) -> tuple[Decimal | None, str | None, str | None]:
    raw_value = str(row[_VALUE_COLUMN]).strip()
    source_missing_value = str(row[_MISSING_COLUMN]).strip() or None
    if not raw_value:
        if source_missing_value is None:
            raise UnctadFdiParseError(
                "unexplained_missing_value",
                f"UNCTAD FDI CSV row {line_number} has an unexplained missing value",
            )
        return None, "source_null", source_missing_value
    if source_missing_value is not None:
        raise UnctadFdiParseError(
            "conflicting_missing_value",
            f"UNCTAD FDI CSV row {line_number} has both a value and missing marker",
        )
    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise UnctadFdiParseError(
            "invalid_value",
            f"UNCTAD FDI CSV row {line_number} has an invalid numeric value",
        ) from exc
    if not value.is_finite():
        raise UnctadFdiParseError(
            "invalid_value",
            f"UNCTAD FDI CSV row {line_number} has a non-finite numeric value",
        )
    return value, None, None
