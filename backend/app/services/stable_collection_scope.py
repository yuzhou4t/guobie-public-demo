from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.services.collection_plan import EXPECTED_EXCEL_ROWS, CollectionPlan

SCHEMA_VERSION = 2
GROUPS = ("metadata", "structured")
METADATA_CADENCES = ("daily", "weekly", "monthly")
_ROOT_KEYS = {
    "schema_version",
    "scope_id",
    "source_file",
    "evidence_reports",
    "stable_groups",
    "expected_channel_counts",
    "expected_plan_slice_sha256",
    "metadata_cadences",
    "expected_cadence_channel_counts",
    "expected_cadence_plan_slice_sha256",
    "deferred_sources",
    "limited_sources",
}
_DEFERRED_KEYS = {"excel_row", "category", "evidence", "next_action"}
_LIMITED_KEYS = {"excel_row", "limitation"}
_CATEGORIES = {
    "coverage_review",
    "manual_refresh",
    "official_file_required",
    "policy_or_license",
    "temporary_access_failure",
}


class StableCollectionScopeValidationError(ValueError):
    """Raised when the stable update scope is incomplete or stale."""


@dataclass(frozen=True, slots=True)
class StableCollectionScope:
    scope_id: str
    source_file: str
    evidence_reports: tuple[str, ...]
    groups: dict[str, tuple[int, ...]]
    expected_channel_counts: dict[str, int]
    expected_plan_hashes: dict[str, str]
    metadata_cadences: dict[str, tuple[int, ...]]
    expected_cadence_channel_counts: dict[str, int]
    expected_cadence_plan_hashes: dict[str, str]
    deferred_sources: tuple[dict[str, Any], ...]
    limited_sources: tuple[dict[str, Any], ...]
    scope_sha256: str

    @property
    def deferred_excel_rows(self) -> tuple[int, ...]:
        return tuple(item["excel_row"] for item in self.deferred_sources)

    @property
    def metadata_excel_rows(self) -> tuple[int, ...]:
        return self.groups["metadata"]

    @property
    def structured_excel_rows(self) -> tuple[int, ...]:
        return self.groups["structured"]

    @property
    def stable_excel_rows(self) -> tuple[int, ...]:
        selected = set(self.metadata_excel_rows) | set(self.structured_excel_rows)
        return tuple(row for row in EXPECTED_EXCEL_ROWS if row in selected)

    def metadata_excel_rows_for_cadence(self, cadence: str) -> tuple[int, ...]:
        if cadence not in METADATA_CADENCES:
            raise ValueError(f"unsupported metadata cadence: {cadence}")
        return self.metadata_cadences[cadence]

    def excel_rows_for_group(self, group: str) -> tuple[int, ...]:
        if group in self.groups:
            return self.groups[group]
        if group.startswith("metadata-"):
            return self.metadata_excel_rows_for_cadence(group.removeprefix("metadata-"))
        if group == "stable":
            return self.stable_excel_rows
        if group == "all":
            return EXPECTED_EXCEL_ROWS
        raise ValueError(f"unsupported collection group: {group}")

    def validate_against_plan(
        self,
        plan: CollectionPlan,
        *,
        groups: tuple[str, ...] = (),
    ) -> None:
        if self.source_file != plan.source_file:
            raise StableCollectionScopeValidationError(
                "stable scope source_file must match the collection plan"
            )
        by_row = {source.excel_row: source for source in plan.sources}
        if tuple(by_row) != EXPECTED_EXCEL_ROWS:
            raise StableCollectionScopeValidationError("collection plan rows changed")
        supported_groups = set(GROUPS) | {f"metadata-{cadence}" for cadence in METADATA_CADENCES}
        if any(group not in supported_groups for group in groups):
            raise ValueError(f"unsupported stable validation groups: {groups}")
        for group in groups:
            if group in GROUPS:
                selected_rows = self.groups[group]
                expected_count = self.expected_channel_counts[group]
                expected_hash = self.expected_plan_hashes[group]
            else:
                cadence = group.removeprefix("metadata-")
                selected_rows = self.metadata_cadences[cadence]
                expected_count = self.expected_cadence_channel_counts[cadence]
                expected_hash = self.expected_cadence_plan_hashes[cadence]
            blocked = [row for row in selected_rows if by_row[row].automation_status == "blocked"]
            if blocked:
                raise StableCollectionScopeValidationError(
                    f"stable scope cannot select blocked plan rows: {blocked}"
                )
            actual_count = sum(len(by_row[row].channels) for row in selected_rows)
            if actual_count != expected_count:
                raise StableCollectionScopeValidationError(
                    "stable scope channel counts no longer match the collection plan"
                )
            if plan_slice_sha256(plan, selected_rows) != expected_hash:
                raise StableCollectionScopeValidationError(
                    f"stable collection plan slice changed and requires review: {group}"
                )


def load_stable_collection_scope(path: str | Path | None = None) -> StableCollectionScope:
    scope_path = Path(path) if path is not None else _default_path()
    try:
        raw = scope_path.read_bytes()
        payload: Any = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise StableCollectionScopeValidationError(
            f"unable to read stable collection scope: {scope_path}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _ROOT_KEYS:
        raise StableCollectionScopeValidationError("stable scope root fields do not match the schema")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise StableCollectionScopeValidationError(f"schema_version must be {SCHEMA_VERSION}")

    groups = _group_mapping(payload["stable_groups"], _row_list)
    counts = _group_mapping(payload["expected_channel_counts"], _positive_int)
    hashes = _group_mapping(payload["expected_plan_slice_sha256"], _sha256)
    cadences = _cadence_mapping(payload["metadata_cadences"], _row_list)
    cadence_counts = _cadence_mapping(payload["expected_cadence_channel_counts"], _positive_int)
    cadence_hashes = _cadence_mapping(payload["expected_cadence_plan_slice_sha256"], _sha256)
    deferred = _records(payload["deferred_sources"], _DEFERRED_KEYS, require_nonempty=False)
    limited = _records(payload["limited_sources"], _LIMITED_KEYS)
    for item in deferred:
        _excel_row(item["excel_row"])
        _text(item["evidence"], "evidence")
        _text(item["next_action"], "next_action")
        if item["category"] not in _CATEGORIES:
            raise StableCollectionScopeValidationError("deferred source category is invalid")
    for item in limited:
        _excel_row(item["excel_row"])
        _text(item["limitation"], "limitation")

    stable_rows = groups["metadata"] + groups["structured"]
    cadence_rows = tuple(row for cadence in METADATA_CADENCES for row in cadences[cadence])
    deferred_rows = tuple(item["excel_row"] for item in deferred)
    limited_rows = {item["excel_row"] for item in limited}
    if len(stable_rows) != len(set(stable_rows)) or len(deferred_rows) != len(set(deferred_rows)):
        raise StableCollectionScopeValidationError("stable and deferred rows must not repeat")
    if set(stable_rows) & set(deferred_rows):
        raise StableCollectionScopeValidationError("stable and deferred rows must not overlap")
    if set(stable_rows) | set(deferred_rows) != set(EXPECTED_EXCEL_ROWS):
        raise StableCollectionScopeValidationError("stable and deferred rows must partition 4 through 53")
    if len(cadence_rows) != len(set(cadence_rows)):
        raise StableCollectionScopeValidationError("metadata cadence rows must not repeat")
    if set(cadence_rows) != set(groups["metadata"]):
        raise StableCollectionScopeValidationError(
            "metadata cadence rows must partition the stable metadata group"
        )
    if not limited_rows.issubset(stable_rows):
        raise StableCollectionScopeValidationError("limited rows must be in a stable group")

    reports = payload["evidence_reports"]
    if not isinstance(reports, list) or not reports:
        raise StableCollectionScopeValidationError("evidence_reports must be a non-empty list")
    return StableCollectionScope(
        scope_id=_text(payload["scope_id"], "scope_id"),
        source_file=_text(payload["source_file"], "source_file"),
        evidence_reports=tuple(_text(item, "evidence_report") for item in reports),
        groups=groups,
        expected_channel_counts=counts,
        expected_plan_hashes=hashes,
        metadata_cadences=cadences,
        expected_cadence_channel_counts=cadence_counts,
        expected_cadence_plan_hashes=cadence_hashes,
        deferred_sources=deferred,
        limited_sources=limited,
        scope_sha256=hashlib.sha256(raw).hexdigest(),
    )


def plan_slice_sha256(plan: CollectionPlan, rows: tuple[int, ...]) -> str:
    selected = set(rows)
    payload = []
    for source in plan.sources:
        if source.excel_row not in selected:
            continue
        source_payload = asdict(source)
        for channel in source_payload["channels"]:
            if channel["policy"] is None:
                del channel["policy"]
        payload.append(source_payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _default_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "stable_collection_scope.json"


def _group_mapping(value: Any, parser: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(GROUPS):
        raise StableCollectionScopeValidationError("group fields must be metadata and structured")
    return {group: parser(value[group], group) for group in GROUPS}


def _cadence_mapping(value: Any, parser: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(METADATA_CADENCES):
        raise StableCollectionScopeValidationError(
            "metadata cadence fields must be daily, weekly, and monthly"
        )
    return {cadence: parser(value[cadence], cadence) for cadence in METADATA_CADENCES}


def _records(value: Any, keys: set[str], *, require_nonempty: bool = False) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or (require_nonempty and not value):
        raise StableCollectionScopeValidationError("scope records must be a valid list")
    if any(not isinstance(item, dict) or set(item) != keys for item in value):
        raise StableCollectionScopeValidationError("scope record fields do not match the schema")
    return tuple(value)


def _row_list(value: Any, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise StableCollectionScopeValidationError(f"{field} rows must be a non-empty list")
    return tuple(_excel_row(row) for row in value)


def _excel_row(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in EXPECTED_EXCEL_ROWS:
        raise StableCollectionScopeValidationError("source rows must be integers from 4 through 53")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StableCollectionScopeValidationError(f"{field} must be a positive integer")
    return value


def _sha256(value: Any, field: str) -> str:
    digest = _text(value, field)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise StableCollectionScopeValidationError(f"{field} must be a lowercase SHA256")
    return digest


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StableCollectionScopeValidationError(f"{field} must be a non-empty string")
    return value.strip()
