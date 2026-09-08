from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models import (
    CollectionItem,
    CollectionRun,
    Document,
    DocumentVersion,
    RawAsset,
    Source,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
)
from app.services.collection_plan import CollectionPlan, load_collection_plan
from app.services.collection_runner import ChannelOutcome, collect_channel
from app.services.source_catalog import SourceCatalog, load_source_catalog
from app.services.source_importer import import_catalog
from app.services.source_probe import SafeHttpClient
from app.services.structured_scope import StructuredScope, load_structured_scope

WORLD_BANK_PROFILE = "world_bank_indicators"


class WorldBankCliError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main() -> int:
    args = _parse_args()
    try:
        return _run(args)
    except WorldBankCliError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "error_message": exc.message,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


def _run(args: argparse.Namespace) -> int:
    catalog = load_source_catalog(args.catalog)
    plan = load_collection_plan(args.plan)
    scope = load_structured_scope(args.scope)
    catalog_file_sha256 = _file_sha256(args.catalog)
    plan_file_sha256 = _file_sha256(args.plan)

    with get_session_factory()() as session:
        imported = import_catalog(session, catalog, plan)
        session.commit()
        channel = _select_world_bank_channel(session, imported.channel_ids)
        _validate_channel_scope(channel, scope)

        boundary_before = _storage_boundary_snapshot(session)
        outcome = collect_channel(session, channel.id, client=SafeHttpClient())
        run = session.get(CollectionRun, outcome.run_id)
        if run is None:
            raise WorldBankCliError(
                "collection_run_missing",
                "World Bank collection outcome does not reference a persisted run",
            )
        boundary_after = _storage_boundary_snapshot(session)
        database = _database_snapshot(session)
        run_report = dict(run.report)

    payload = _build_report_payload(
        catalog=catalog,
        catalog_file_sha256=catalog_file_sha256,
        plan=plan,
        plan_file_sha256=plan_file_sha256,
        scope=scope,
        outcome=outcome,
        run_report=run_report,
        database=database,
        boundary_before=boundary_before,
        boundary_after=boundary_after,
    )
    json_path, markdown_path = _write_report(args.report_dir, payload)
    boundary_unchanged = bool(payload["storage_boundary"]["unchanged"])
    terminal_status = outcome.status if boundary_unchanged else "failed"
    print(
        json.dumps(
            {
                "status": terminal_status,
                "run_id": outcome.run_id,
                "items_discovered": outcome.items_discovered,
                "items_persisted": outcome.items_persisted,
                "storage_boundary_unchanged": boundary_unchanged,
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if outcome.status == "succeeded" and boundary_unchanged else 1


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Collect only the reviewed World Bank structured-data channel in shadow mode."
    )
    parser.add_argument("--catalog", type=Path, default=root / "data" / "source_catalog.json")
    parser.add_argument("--plan", type=Path, default=root / "data" / "source_collection_plan.json")
    parser.add_argument(
        "--scope",
        type=Path,
        default=root / "data" / "structured_collection_scope.json",
    )
    parser.add_argument("--report-dir", type=Path, default=root / "data" / "collection-runs")
    return parser.parse_args()


def _select_world_bank_channel(
    session: Session,
    imported_channel_ids: tuple[int, ...],
) -> SourceChannel:
    if len(imported_channel_ids) != len(set(imported_channel_ids)):
        raise WorldBankCliError(
            "imported_channel_ids_invalid",
            "imported channel IDs must be unique",
        )

    matches: list[SourceChannel] = []
    for channel_id in imported_channel_ids:
        channel = session.get(SourceChannel, channel_id)
        if channel is None:
            raise WorldBankCliError(
                "imported_channel_missing",
                f"imported channel {channel_id} does not exist",
            )
        if channel.collector_config.get("profile") == WORLD_BANK_PROFILE:
            matches.append(channel)

    if not matches:
        raise WorldBankCliError(
            "world_bank_channel_not_found",
            "this import contains no reviewed World Bank structured-data channel",
        )
    if len(matches) != 1:
        raise WorldBankCliError(
            "world_bank_channel_ambiguous",
            "this import contains more than one World Bank structured-data channel",
        )
    return matches[0]


def _validate_channel_scope(channel: SourceChannel, scope: StructuredScope) -> None:
    if (
        channel.collector_config.get("scope_id") != scope.scope_id
        or channel.collector_config.get("dataset_key") != scope.world_bank.dataset_key
    ):
        raise WorldBankCliError(
            "world_bank_channel_scope_mismatch",
            "World Bank channel does not reference the loaded structured-data scope",
        )


def _database_snapshot(session: Session) -> dict[str, int]:
    tables = {
        "sources": Source,
        "channels": SourceChannel,
        "collection_runs": CollectionRun,
        "collection_items": CollectionItem,
        "documents": Document,
        "document_versions": DocumentVersion,
        "raw_assets": RawAsset,
        "structured_datasets": StructuredDataset,
        "structured_snapshots": StructuredSnapshot,
        "structured_observations": StructuredObservation,
        "structured_observation_versions": StructuredObservationVersion,
    }
    return {
        name: session.scalar(select(func.count()).select_from(model)) or 0 for name, model in tables.items()
    }


def _storage_boundary_snapshot(session: Session) -> dict[str, int]:
    return {
        "raw_assets": session.scalar(select(func.count()).select_from(RawAsset)) or 0,
        "document_versions_with_abstract": session.scalar(
            select(func.count()).select_from(DocumentVersion).where(DocumentVersion.abstract.is_not(None))
        )
        or 0,
        "document_versions_with_body": session.scalar(
            select(func.count()).select_from(DocumentVersion).where(DocumentVersion.body_text.is_not(None))
        )
        or 0,
    }


def _build_report_payload(
    *,
    catalog: SourceCatalog,
    catalog_file_sha256: str,
    plan: CollectionPlan,
    plan_file_sha256: str,
    scope: StructuredScope,
    outcome: ChannelOutcome,
    run_report: dict[str, Any],
    database: dict[str, int],
    boundary_before: dict[str, int],
    boundary_after: dict[str, int],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(UTC)
    raw_assets_changed = boundary_before["raw_assets"] != boundary_after["raw_assets"]
    document_text_changed = any(
        boundary_before[field_name] != boundary_after[field_name]
        for field_name in (
            "document_versions_with_abstract",
            "document_versions_with_body",
        )
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "catalog": {
            "source_file": catalog.source_file,
            "workbook_sha256": catalog.workbook_sha256,
            "file_sha256": catalog_file_sha256,
        },
        "plan": {
            "source_file": plan.source_file,
            "file_sha256": plan_file_sha256,
        },
        "scope": {
            "scope_id": scope.scope_id,
            "file_sha256": scope.scope_sha256,
            "dataset_key": scope.world_bank.dataset_key,
        },
        "collection": {
            "outcome": outcome.as_dict(),
            "run_report": run_report,
        },
        "database": database,
        "storage_boundary": {
            "before": boundary_before,
            "after": boundary_after,
            "unchanged": boundary_before == boundary_after,
            "raw_assets_changed": raw_assets_changed,
            "summaries_or_bodies_changed": document_text_changed,
        },
    }


def _write_report(
    report_dir: Path,
    payload: dict[str, Any],
    *,
    timestamp: datetime | None = None,
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = (timestamp or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-world-bank-structured-shadow"
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown_report(payload), encoding="utf-8")
    return json_path, markdown_path


def _markdown_report(payload: dict[str, Any]) -> str:
    outcome = payload["collection"]["outcome"]
    boundary = payload["storage_boundary"]
    lines = [
        "# 国别智枢 World Bank 结构化数据影子采集报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 清单工作簿 SHA256：`{payload['catalog']['workbook_sha256']}`",
        f"- 清单目录文件 SHA256：`{payload['catalog']['file_sha256']}`",
        f"- 采集计划文件 SHA256：`{payload['plan']['file_sha256']}`",
        f"- 范围 ID：`{payload['scope']['scope_id']}`",
        f"- 范围文件 SHA256：`{payload['scope']['file_sha256']}`",
        f"- 数据集：`{payload['scope']['dataset_key']}`",
        f"- 运行 ID：{outcome['run_id']}",
        f"- 运行结果：{outcome['status']}",
        f"- 逻辑单元：发现 {outcome['items_discovered']}，接受 {outcome['items_persisted']}",
        "",
        "## 数据库计数",
        "",
        "| 表 | 行数 |",
        "|---|---:|",
    ]
    lines.extend(f"| `{name}` | {count} |" for name, count in payload["database"].items())
    lines.extend(
        [
            "",
            "## 保存边界",
            "",
            f"- 原始资产：{boundary['before']['raw_assets']} → {boundary['after']['raw_assets']}",
            "- 非空摘要版本："
            f"{boundary['before']['document_versions_with_abstract']} → "
            f"{boundary['after']['document_versions_with_abstract']}",
            "- 非空正文版本："
            f"{boundary['before']['document_versions_with_body']} → "
            f"{boundary['after']['document_versions_with_body']}",
            f"- 边界是否保持不变：{'yes' if boundary['unchanged'] else 'no'}",
            "- 本轮不保存 API 原始响应、摘要或正文，不进入 RAG。",
            "",
            "## 完整运行报告",
            "",
            "```json",
            json.dumps(payload["collection"]["run_report"], ensure_ascii=False, indent=2),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
