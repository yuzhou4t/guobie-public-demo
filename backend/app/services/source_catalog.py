from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
EXPECTED_HEADER_COUNT = 57
EXPECTED_SOURCE_COUNT = 50
EXPECTED_EXCEL_ROWS = tuple(range(4, 54))

URL_FIELDS = (
    "官网URL",
    "栏目URL",
    "RSS URL",
    "API文档URL",
    "搜索页URL",
    "PDF列表URL",
    "样例材料URL 1",
    "样例材料URL 2",
    "样例材料URL 3",
    "使用条款URL",
)
ENTRY_URL_FIELD_ORDER = ("栏目URL", "RSS URL", "PDF列表URL", "官网URL")

_REQUIRED_HEADERS = {
    "一级类型",
    "二级类型",
    "信源名称",
    "所属机构",
    "国家/地区",
    "主要语言",
    "官网URL",
    "栏目名称",
    "栏目URL",
    "推荐优先级",
    "建议保存策略",
    "是否可进入RAG",
}
_URL_SEPARATOR = re.compile(r"[;；]")
_SHA256 = re.compile(r"[0-9a-f]{64}")

JsonScalar = str | int | float | bool | None


class CatalogValidationError(ValueError):
    """Raised when the checked-in source catalog does not match its schema."""


def split_urls(value: JsonScalar) -> tuple[str, ...]:
    """Split a worksheet URL cell on Chinese or ASCII semicolons."""

    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in _URL_SEPARATOR.split(value) if part.strip())


@dataclass(frozen=True)
class CatalogSource:
    excel_row: int
    fields: dict[str, JsonScalar]

    @property
    def name(self) -> str:
        return str(self.fields["信源名称"])

    @property
    def priority(self) -> str:
        return str(self.fields["推荐优先级"])

    @property
    def urls_by_field(self) -> dict[str, tuple[str, ...]]:
        return {field: split_urls(self.fields[field]) for field in URL_FIELDS}

    @property
    def entry_urls_by_field(self) -> dict[str, tuple[str, ...]]:
        urls = self.urls_by_field
        return {field: urls[field] for field in ENTRY_URL_FIELD_ORDER}

    @property
    def entry_urls(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for urls in self.entry_urls_by_field.values():
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    ordered.append(url)
        return tuple(ordered)

    @property
    def effective_storage_policy(self) -> str:
        raw_value = self.fields["建议保存策略"]
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        return {
            "全文保存": "full_text",
            "仅元数据": "metadata",
            "摘要+短摘录": "excerpt",
            "仅链接": "link",
        }.get(value, "metadata")

    @property
    def effective_rag_scope(self) -> str:
        raw_value = self.fields["是否可进入RAG"]
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        return "allowed" if value == "是" else "none"


@dataclass(frozen=True)
class SourceCatalog:
    schema_version: int
    workbook_sha256: str
    source_file: str
    headers: tuple[str, ...]
    sources: tuple[CatalogSource, ...]


def load_source_catalog(path: str | Path | None = None) -> SourceCatalog:
    catalog_path = Path(path) if path is not None else _default_catalog_path()
    try:
        payload: Any = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogValidationError(f"unable to read source catalog: {catalog_path}") from exc

    if not isinstance(payload, dict):
        raise CatalogValidationError("catalog root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CatalogValidationError(f"catalog schema_version must be {SCHEMA_VERSION}")

    workbook_sha256 = payload.get("workbook_sha256")
    if not isinstance(workbook_sha256, str) or _SHA256.fullmatch(workbook_sha256) is None:
        raise CatalogValidationError("catalog workbook_sha256 must be a lowercase SHA-256")

    source_file = payload.get("source_file")
    if not isinstance(source_file, str) or not source_file.strip():
        raise CatalogValidationError("catalog source_file must be a non-empty string")

    headers = _validate_headers(payload.get("headers"))
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != EXPECTED_SOURCE_COUNT:
        raise CatalogValidationError(f"catalog must contain exactly {EXPECTED_SOURCE_COUNT} sources")

    sources = tuple(_validate_source(raw_source, headers) for raw_source in raw_sources)
    excel_rows = tuple(source.excel_row for source in sources)
    if excel_rows != EXPECTED_EXCEL_ROWS:
        raise CatalogValidationError("catalog excel_row values must be consecutive from 4 through 53")

    return SourceCatalog(
        schema_version=SCHEMA_VERSION,
        workbook_sha256=workbook_sha256,
        source_file=source_file,
        headers=headers,
        sources=sources,
    )


def _default_catalog_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "source_catalog.json"


def _validate_headers(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != EXPECTED_HEADER_COUNT:
        raise CatalogValidationError(f"catalog must declare exactly {EXPECTED_HEADER_COUNT} headers")
    if any(not isinstance(header, str) or not header for header in value):
        raise CatalogValidationError("catalog headers must be non-empty strings")
    headers = tuple(value)
    if len(set(headers)) != len(headers):
        raise CatalogValidationError("catalog headers must be unique")
    missing = _REQUIRED_HEADERS.difference(headers)
    if missing:
        raise CatalogValidationError(f"catalog is missing required headers: {sorted(missing)}")
    if any(field not in headers for field in URL_FIELDS):
        raise CatalogValidationError("catalog is missing one or more URL fields")
    return headers


def _validate_source(value: Any, headers: tuple[str, ...]) -> CatalogSource:
    if not isinstance(value, dict):
        raise CatalogValidationError("each catalog source must be an object")
    expected_keys = set(headers) | {"excel_row"}
    if set(value) != expected_keys:
        raise CatalogValidationError("each catalog source must contain excel_row and all declared headers")

    excel_row = value["excel_row"]
    if not isinstance(excel_row, int) or isinstance(excel_row, bool):
        raise CatalogValidationError("source excel_row must be an integer")

    fields: dict[str, JsonScalar] = {}
    for header in headers:
        field_value = value[header]
        if field_value is not None and not isinstance(field_value, (str, int, float, bool)):
            raise CatalogValidationError(f"source field {header!r} must contain a scalar value")
        fields[header] = field_value

    name = fields["信源名称"]
    if not isinstance(name, str) or not name.strip():
        raise CatalogValidationError("source 信源名称 must be a non-empty string")
    return CatalogSource(excel_row=excel_row, fields=fields)
