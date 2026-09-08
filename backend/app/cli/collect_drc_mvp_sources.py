from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.collectors.drc_mvp import parse_drc_mvp_detail, parse_drc_mvp_listing
from app.services.robots_policy import check_robots
from app.services.source_probe import ProbeError, SafeHttpClient

_CLOSED_DATA_TEAM_STATUSES = {"closed", "closed_for_mvp"}


def main() -> int:
    args = _parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    existing_sources: list[dict[str, Any]] = []
    if args.output.is_file():
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        existing_sources = existing.get("sources", [])
    selected_ids = set(args.source_id or [])
    configured_sources = manifest.get("sources", [])
    if selected_ids:
        unknown = selected_ids - {item.get("source_id") for item in configured_sources}
        if unknown:
            print(f"unknown source_id: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        configured_sources = [item for item in configured_sources if item.get("source_id") in selected_ids]
    if args.due_only:
        existing_by_id = {item.get("source_id"): item for item in existing_sources}
        now = datetime.now(UTC)
        configured_sources = [
            item
            for item in configured_sources
            if _source_due(item, existing_by_id.get(item.get("source_id")), now=now)
        ]
    client = SafeHttpClient()
    source_results = [
        _collect_source(client, source, repeat_count=args.repeat) for source in configured_sources
    ]
    if args.merge_existing and existing_sources:
        source_results = _merge_source_results(manifest, existing_sources, source_results)
    payload = _build_payload(manifest, source_results)
    _write_atomic(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "source_count": len(source_results),
                "verified_source_count": payload["summary"]["verified_source_count"],
                "item_count": payload["summary"]["item_count"],
                "review_required_count": payload["summary"]["review_required_count"],
                "database_state": "not_imported",
            },
            ensure_ascii=False,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Safely collect bounded public metadata for the DRC MVP browser."
    )
    parser.add_argument("--manifest", type=Path, default=root / "data" / "drc_mvp_sources.json")
    parser.add_argument("--output", type=Path, default=root / "data" / "drc_mvp_harvest.json")
    parser.add_argument(
        "--repeat",
        type=int,
        choices=(1, 2),
        default=2,
        help="fetch each allowed listing twice by default to check parsed-metadata idempotence",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        help="collect only the named manifest source; repeat to select multiple sources",
    )
    parser.add_argument(
        "--merge-existing",
        action="store_true",
        help="merge selected results into the output and preserve a prior verified result on failure",
    )
    parser.add_argument(
        "--due-only",
        action="store_true",
        help="collect only daily/weekly sources whose last verified fetch is due",
    )
    return parser.parse_args()


def _collect_source(
    client: SafeHttpClient,
    source: dict[str, Any],
    *,
    repeat_count: int,
) -> dict[str, Any]:
    result = _base_source_payload(source)
    entry_url = str(source["entry_url"])
    robots = check_robots(client, entry_url)
    result["robots"] = {
        "state": robots.state,
        "allowed": robots.allowed,
        "code": robots.code,
        "url": robots.robots_url,
    }
    if not robots.allowed:
        result.update(
            status="review_required",
            status_label="抓取待复核",
            diagnostic_code=robots.code,
            diagnostic_message="robots 检查未允许自动抓取",
        )
        return result

    parsed_runs: list[list[dict[str, Any]]] = []
    fetches: list[dict[str, Any]] = []
    try:
        for _ in range(repeat_count):
            resource = client.fetch(entry_url)
            parsed = parse_drc_mvp_listing(resource, source, link_role="official")
            candidates = list(parsed.candidates)
            if source.get("fetch_detail_metadata"):
                detail_limit = int(source.get("max_detail_items") or 8)
                candidates = [
                    _fetch_detail_metadata(client, item) if index < detail_limit else item
                    for index, item in enumerate(candidates)
                ]
            parsed_items = [_candidate_payload(item) for item in candidates]
            parsed_runs.append(parsed_items)
            fetches.append(
                {
                    "fetched_at": resource.fetched_at.isoformat(),
                    "final_url": resource.final_url,
                    "status_code": resource.status_code,
                    "content_type": resource.content_type,
                    "byte_count": len(resource.body),
                    "sha256": resource.sha256,
                    "item_count": len(parsed_items),
                    "diagnostics": [item.code for item in parsed.diagnostics],
                }
            )
    except ProbeError as exc:
        result.update(
            status="review_required",
            status_label="抓取待复核",
            diagnostic_code=exc.code,
            diagnostic_message=exc.message,
            fetches=fetches,
        )
        return result

    identities = [[_item_identity(item) for item in run] for run in parsed_runs]
    repeat_consistent = len(identities) == 1 or all(run == identities[0] for run in identities[1:])
    items = parsed_runs[-1] if parsed_runs else []
    if not items:
        status = "review_required"
        status_label = "抓取待复核"
        diagnostic_code = "drc_mvp_no_items"
        diagnostic_message = "页面可访问，但没有解析到符合已审核规则的条目"
    elif not repeat_consistent:
        status = "review_required"
        status_label = "复采不一致"
        diagnostic_code = "repeat_inconsistent"
        diagnostic_message = "两次抓取的条目标识不一致，需人工复核后再登记"
    else:
        status = "metadata_verified"
        status_label = "已抓元数据（待入库）"
        diagnostic_code = None
        diagnostic_message = None
    result.update(
        status=status,
        status_label=status_label,
        diagnostic_code=diagnostic_code,
        diagnostic_message=diagnostic_message,
        repeat_consistent=repeat_consistent,
        fetches=fetches,
        items=items,
        item_count=len(items),
    )
    return result


def _fetch_detail_metadata(client: SafeHttpClient, candidate: Any) -> Any:
    url = candidate.canonical_url or candidate.discovery_url
    robots = check_robots(client, url)
    if not robots.allowed:
        return candidate
    try:
        resource = client.fetch(url)
    except ProbeError:
        return candidate
    return parse_drc_mvp_detail(resource, candidate)


def _base_source_payload(source: dict[str, Any]) -> dict[str, Any]:
    kept_fields = (
        "source_id",
        "name",
        "organization",
        "category",
        "priority",
        "update_cadence",
        "entry_url",
        "dimensions",
        "topics",
        "value",
    )
    result = {key: source.get(key) for key in kept_fields}
    result.update(
        database_state="not_imported",
        database_state_label="尚未正式入库",
        status="pending",
        status_label="待抓取",
        repeat_consistent=None,
        item_count=0,
        items=[],
        fetches=[],
    )
    return result


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    provenance = candidate.source_metadata.get("published_at_provenance") or {}
    return {
        "title": candidate.title,
        "url": candidate.canonical_url or candidate.discovery_url,
        "discovery_url": candidate.discovery_url,
        "external_id": candidate.external_id,
        "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
        "published_at_precision": provenance.get("precision", "unknown"),
        "retrieved_at": candidate.source_metadata.get("detail_retrieved_at"),
        "language": candidate.language,
        "media_type": candidate.media_type,
    }


def _item_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["external_id"],
        item["title"],
        item["url"],
        item["published_at"],
        item["published_at_precision"],
    )


def _build_payload(
    manifest: dict[str, Any],
    source_results: list[dict[str, Any]],
) -> dict[str, Any]:
    verified = [source for source in source_results if source["status"] == "metadata_verified"]
    item_count = sum(int(source["item_count"]) for source in source_results)
    data_team_requests = manifest.get("data_team_requests", [])
    pending_requests = [
        request for request in data_team_requests if request.get("status") not in _CLOSED_DATA_TEAM_STATUSES
    ]
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "country_code": manifest["country_code"],
        "country_name": manifest["country_name"],
        "topic": manifest["topic"],
        "policy": manifest["policy"],
        "summary": {
            "source_count": len(source_results),
            "verified_source_count": len(verified),
            "review_required_count": len(source_results) - len(verified),
            "item_count": item_count,
            "data_team_request_count": len(pending_requests),
            "resolved_data_team_request_count": len(data_team_requests) - len(pending_requests),
        },
        "sources": source_results,
        "data_team_requests": data_team_requests,
    }


def _merge_source_results(
    manifest: dict[str, Any],
    existing: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {item.get("source_id"): item for item in existing}
    for item in updates:
        source_id = item.get("source_id")
        previous = merged.get(source_id)
        if (
            item.get("status") != "metadata_verified"
            and previous is not None
            and previous.get("status") == "metadata_verified"
        ):
            continue
        merged[source_id] = item
    return [
        merged[item["source_id"]] for item in manifest.get("sources", []) if item.get("source_id") in merged
    ]


def _source_due(
    source: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    now: datetime,
) -> bool:
    cadence = str(source.get("update_cadence") or "manual").lower()
    if cadence == "manual":
        return False
    if cadence not in {"daily", "weekly"}:
        raise ValueError(f"unsupported update_cadence: {cadence}")
    if previous is None or previous.get("status") != "metadata_verified":
        return True
    fetched_at_values = [
        item.get("fetched_at")
        for item in previous.get("fetches", [])
        if isinstance(item, dict) and item.get("fetched_at")
    ]
    if not fetched_at_values:
        return True
    try:
        last_fetched = max(
            datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            for value in fetched_at_values
        )
    except (TypeError, ValueError):
        return True
    age_days = (now.astimezone(UTC).date() - last_fetched.date()).days
    return age_days >= (1 if cadence == "daily" else 7)


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    sys.exit(main())
