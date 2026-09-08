from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import and_, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import (
    get_advisory_lock_engine,
    get_session_factory,
    reset_database_caches,
)
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
    StructuredSnapshotObservation,
)
from app.services.collection_plan import CollectionPlan, load_collection_plan
from app.services.collection_runner import BatchCollectionReport, collect_catalog_batch
from app.services.source_catalog import load_source_catalog
from app.services.source_importer import ImportResult, import_catalog
from app.services.stable_collection_scope import (
    StableCollectionScope,
    load_stable_collection_scope,
    plan_slice_sha256,
)

_CATALOG_COLLECTION_LOCK_ID = 0x47554F424945
_MAX_RECONNECT_ATTEMPTS = 10
_RECONNECT_DELAY_SECONDS = 5
T = TypeVar("T")


def main() -> int:
    args = _parse_args()
    catalog = load_source_catalog(args.catalog)
    plan = load_collection_plan(args.plan)
    stable_scope = load_stable_collection_scope(args.stable_scope)
    execution_group = "targeted" if args.rows is not None else args.group
    validation_groups = {
        "metadata": ("metadata",),
        "metadata-daily": ("metadata-daily",),
        "metadata-weekly": ("metadata-weekly",),
        "metadata-monthly": ("metadata-monthly",),
        "structured": ("structured",),
        "stable": ("metadata", "structured"),
    }.get(execution_group, ())
    stable_scope.validate_against_plan(plan, groups=validation_groups)
    selected_excel_rows = (
        args.rows if args.rows is not None else stable_scope.excel_rows_for_group(args.group)
    )
    preflight_error = _preflight_error(
        execution_group,
        selected_excel_rows,
        has_comtrade_key=get_settings().comtrade_subscription_key is not None,
    )
    if preflight_error is not None:
        print(json.dumps(preflight_error, ensure_ascii=False), file=sys.stderr)
        return 2

    with _catalog_collection_lock(get_advisory_lock_engine()) as lock_acquired:
        if not lock_acquired:
            print(
                json.dumps(
                    {
                        "error": "collection_already_running",
                        "message": "another catalog collection process holds the database lock",
                        "exit_code": 75,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 75

        def prepare(
            session: Session,
        ) -> tuple[ImportResult, tuple[int, ...], tuple[int, ...], dict[str, int]]:
            imported = import_catalog(session, catalog, plan)
            session.commit()
            selected_channel_ids = _select_channel_ids(
                session,
                imported.channel_ids,
                selected_excel_rows,
            )
            active_channel_ids = _active_collection_channel_ids(session, selected_channel_ids)
            database_before = _database_snapshot(session)
            return imported, selected_channel_ids, active_channel_ids, database_before

        imported, selected_channel_ids, active_channel_ids, database_before = (
            _run_database_operation_with_reconnect(prepare, phase="catalog preparation")
        )
        if active_channel_ids:
            print(
                json.dumps(
                    {
                        "error": "selected_channels_already_running",
                        "message": "selected channels have queued or running collection records",
                        "channel_ids": list(active_channel_ids),
                        "exit_code": 75,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 75

        with get_session_factory()() as session:
            report = collect_catalog_batch(
                session,
                channel_ids=selected_channel_ids,
                trigger_kind=args.trigger_kind,
            )
        database = _run_database_operation_with_reconnect(
            _database_snapshot,
            phase="report finalization",
        )
        database_delta = {key: database[key] - database_before[key] for key in database}

    payload = {
        "schema_version": 1,
        "catalog": {
            "source_file": catalog.source_file,
            "workbook_sha256": catalog.workbook_sha256,
            "policy": "metadata_reviewed_official_abstracts_and_structured_observations_no_rag",
        },
        "execution_scope": _execution_scope_payload(
            execution_group,
            stable_scope,
            plan,
            selected_excel_rows,
            len(selected_channel_ids),
        ),
        "import": {key: value for key, value in asdict(imported).items() if key != "channel_ids"},
        "database_before": database_before,
        "database": database,
        "database_delta": database_delta,
        "storage_boundary": _storage_boundary_payload(database_delta),
        "collection": report.as_dict(),
    }
    json_path, markdown_path = _write_report(
        args.report_dir,
        payload,
        report,
        group=execution_group,
    )
    exit_code = _exit_code(
        execution_group,
        report,
        storage_boundary_unchanged=payload["storage_boundary"]["unchanged"],
    )
    print(
        json.dumps(
            {
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
                "collection_group": execution_group,
                "selected_source_count": len(selected_excel_rows),
                "selected_channel_count": len(selected_channel_ids),
                "source_status_counts": report.source_status_counts,
                "items_discovered": report.items_discovered,
                "items_persisted": report.items_persisted,
                "database": database,
                "database_delta": database_delta,
                "storage_boundary": payload["storage_boundary"],
                "exit_code": exit_code,
            },
            ensure_ascii=False,
        )
    )
    return exit_code


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description=(
            "Import the complete checked catalog, then execute either the full audit batch or a "
            "versioned stable metadata/structured subset."
        )
    )
    parser.add_argument("--catalog", type=Path, default=root / "data" / "source_catalog.json")
    parser.add_argument("--plan", type=Path, default=root / "data" / "source_collection_plan.json")
    parser.add_argument(
        "--stable-scope",
        type=Path,
        default=root / "data" / "stable_collection_scope.json",
    )
    parser.add_argument(
        "--group",
        choices=(
            "all",
            "stable",
            "metadata",
            "metadata-daily",
            "metadata-weekly",
            "metadata-monthly",
            "structured",
        ),
        default="all",
        help=(
            "all preserves the full catalog audit; stable runs all verified rows; metadata and "
            "structured run the stable classes separately; metadata-daily, metadata-weekly, "
            "and metadata-monthly run the reviewed cadence partitions"
        ),
    )
    parser.add_argument(
        "--rows",
        type=_parse_excel_rows,
        help=(
            "comma-separated Excel rows for one targeted verification run; cannot be combined "
            "with a non-default --group"
        ),
    )
    parser.add_argument("--report-dir", type=Path, default=root / "data" / "collection-runs")
    parser.add_argument(
        "--trigger-kind",
        choices=("manual", "scheduled"),
        default="manual",
        help="record whether the batch was started manually or by the daily scheduler",
    )
    args = parser.parse_args()
    if args.rows is not None and args.group != "all":
        parser.error("--rows cannot be combined with a non-default --group")
    return args


def _write_report(
    report_dir: Path,
    payload: dict[str, Any],
    report: BatchCollectionReport,
    *,
    group: str,
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = {
        "all": "full-catalog-audit",
        "stable": "stable-shadow",
        "metadata": "stable-metadata-shadow",
        "metadata-daily": "stable-metadata-daily-shadow",
        "metadata-weekly": "stable-metadata-weekly-shadow",
        "metadata-monthly": "stable-metadata-monthly-shadow",
        "structured": "stable-structured-shadow",
        "targeted": "targeted-shadow",
    }[group]
    run_ids = [outcome.run_id for outcome in report.outcomes]
    run_label = f"runs-{min(run_ids)}-{max(run_ids)}"
    json_path = report_dir / f"{stamp}-{suffix}-{run_label}.json"
    markdown_path = report_dir / f"{stamp}-{suffix}-{run_label}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown_report(payload, report), encoding="utf-8")
    return json_path, markdown_path


def _run_database_operation_with_reconnect(
    operation: Callable[[Session], T],
    *,
    phase: str,
) -> T:
    last_error: OperationalError | None = None
    for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
        try:
            with get_session_factory()() as session:
                return operation(session)
        except OperationalError as exc:
            last_error = exc
            if attempt >= _MAX_RECONNECT_ATTEMPTS:
                break
            print(
                f"database connection lost during {phase} (attempt {attempt}); "
                f"reconnecting after {_RECONNECT_DELAY_SECONDS}s: {exc.__class__.__name__}",
                file=sys.stderr,
            )
            reset_database_caches()
            time.sleep(_RECONNECT_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"database operation did not run during {phase}")


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
        "structured_snapshot_observations": StructuredSnapshotObservation,
    }
    snapshot = {
        name: session.scalar(select(func.count()).select_from(model)) or 0 for name, model in tables.items()
    }
    snapshot["document_versions_with_abstract"] = (
        session.scalar(
            select(func.count()).select_from(DocumentVersion).where(DocumentVersion.abstract.is_not(None))
        )
        or 0
    )
    snapshot["documents_with_current_abstract"] = (
        session.scalar(
            select(func.count())
            .select_from(Document)
            .join(
                DocumentVersion,
                and_(
                    DocumentVersion.document_id == Document.id,
                    DocumentVersion.version_no == Document.latest_version_no,
                ),
            )
            .where(DocumentVersion.abstract.is_not(None))
        )
        or 0
    )
    snapshot["document_versions_with_body"] = (
        session.scalar(
            select(func.count()).select_from(DocumentVersion).where(DocumentVersion.body_text.is_not(None))
        )
        or 0
    )
    return snapshot


def _storage_boundary_payload(database_delta: dict[str, int]) -> dict[str, Any]:
    boundary_delta = {
        key: database_delta.get(key, 0)
        for key in (
            "raw_assets",
            "document_versions_with_abstract",
            "documents_with_current_abstract",
            "document_versions_with_body",
        )
    }
    return {
        "delta": boundary_delta,
        "unchanged": (
            boundary_delta["raw_assets"] == 0 and boundary_delta["document_versions_with_body"] == 0
        ),
        "abstract_scope": "reviewed_source_provided_abstracts_only",
    }


def _markdown_report(payload: dict[str, Any], report: BatchCollectionReport) -> str:
    scope = payload["execution_scope"]
    database_before = payload["database_before"]
    database = payload["database"]
    database_delta = payload["database_delta"]
    boundary = payload["storage_boundary"]
    source_counts = "，".join(f"{status}={count}" for status, count in report.source_status_counts.items())
    channel_counts = "，".join(f"{status}={count}" for status, count in report.channel_status_counts.items())
    title = {
        "all": "# 国别智枢全清单审计采集报告",
        "targeted": "# 国别智枢定向核验采集报告",
    }.get(scope["group"], "# 国别智枢稳定更新采集报告")
    lines = [
        title,
        "",
        f"- 开始时间：{report.started_at.isoformat()}",
        f"- 结束时间：{report.finished_at.isoformat()}",
        f"- 执行分组：`{scope['group']}`",
        f"- 选择范围：{scope['selected_source_count']} 个信源、{scope['selected_channel_count']} 个栏目",
        f"- 稳定范围参考文件 SHA256：`{scope['stable_scope_reference_sha256']}`",
        f"- 计划切片 SHA256：`{scope['selected_plan_sha256']}`",
        f"- 清单文件：{payload['catalog']['source_file']}",
        f"- 工作簿 SHA256：`{payload['catalog']['workbook_sha256']}`",
        f"- 来源结果：{source_counts}",
        f"- 栏目结果：{channel_counts}",
        f"- 发现条目：{report.items_discovered}",
        f"- 处理通过：{report.items_persisted}",
        f"- 去重文档：{database_before['documents']} → {database['documents']}"
        f"（Δ {database_delta['documents']:+d}）",
        f"- 文档版本：{database_before['document_versions']} → "
        f"{database['document_versions']}（Δ {database_delta['document_versions']:+d}）",
        f"- 当前含摘要文档：{database_before.get('documents_with_current_abstract', 0)} → "
        f"{database.get('documents_with_current_abstract', 0)}"
        f"（Δ {database_delta.get('documents_with_current_abstract', 0):+d}）",
        f"- 结构化快照：{database_before['structured_snapshots']} → "
        f"{database['structured_snapshots']}（Δ {database_delta['structured_snapshots']:+d}）",
        f"- 结构化观测版本：{database_before['structured_observation_versions']} → "
        f"{database['structured_observation_versions']}"
        f"（Δ {database_delta['structured_observation_versions']:+d}）",
        f"- 保存边界核验：{'通过' if boundary['unchanged'] else '告警'}；"
        f"原始资产 Δ {boundary['delta']['raw_assets']:+d}，"
        f"白名单官方摘要版本 Δ {boundary['delta']['document_versions_with_abstract']:+d}，"
        f"含正文版本 Δ {boundary['delta']['document_versions_with_body']:+d}。",
        "- 保存政策：全源保存书目元数据与链接；仅审核白名单保存来源显式提供的摘要；"
        "不保存正文或原始响应，不进入 RAG。",
    ]
    if scope["group"] != "all":
        not_selected_rows = "、".join(str(row) for row in scope["not_selected_excel_rows"])
        if not_selected_rows:
            lines.append(f"- 本轮未选择行：{not_selected_rows}；不创建采集运行记录。")
        deferred_rows = "、".join(str(row) for row in scope["not_selected_deferred_excel_rows"])
        if deferred_rows:
            lines.append(f"- 暂缓且未进入本轮：{deferred_rows}；不创建采集运行记录。")
    for limited in scope["limited_sources"]:
        if limited["excel_row"] in scope["selected_excel_rows"]:
            lines.append(f"- 覆盖限定（第 {limited['excel_row']} 行）：{limited['limitation']}")
    lines.extend(
        [
            "",
            "| Excel行 | 信源 | 栏目 | 结果 | 发现 | 入库 | 错误码 |",
            "|---:|---|---|---|---:|---:|---|",
        ]
    )
    for outcome in report.outcomes:
        status = (
            "blocked"
            if outcome.error_code in {"catalog_blocked", "channel_not_runnable", "source_not_runnable"}
            else outcome.status
        )
        lines.append(
            "| "
            f"{outcome.excel_row} | {_cell(outcome.source_name)} | {_cell(outcome.channel_name)} | "
            f"{status} | {outcome.items_discovered} | {outcome.items_persisted} | "
            f"{_cell(outcome.error_code or '')} |"
        )
    return "\n".join(lines) + "\n"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _select_channel_ids(
    session: Session,
    imported_channel_ids: tuple[int, ...],
    selected_excel_rows: tuple[int, ...],
) -> tuple[int, ...]:
    selected_rows = set(selected_excel_rows)
    observed_rows: set[int] = set()
    selected_channel_ids: list[int] = []
    for channel_id in imported_channel_ids:
        channel = session.get(SourceChannel, channel_id)
        if channel is None:
            raise ValueError(f"imported source channel {channel_id} does not exist")
        excel_row = channel.collector_config.get("catalog_excel_row")
        if excel_row in selected_rows:
            observed_rows.add(excel_row)
            selected_channel_ids.append(channel.id)
    if observed_rows != selected_rows:
        missing = sorted(selected_rows - observed_rows)
        raise ValueError(f"selected source rows have no imported channels: {missing}")
    return tuple(selected_channel_ids)


def _active_collection_channel_ids(
    session: Session,
    selected_channel_ids: tuple[int, ...],
) -> tuple[int, ...]:
    return tuple(
        session.scalars(
            select(CollectionRun.channel_id)
            .where(
                CollectionRun.channel_id.in_(selected_channel_ids),
                CollectionRun.status.in_(("queued", "running")),
            )
            .order_by(CollectionRun.channel_id)
        )
    )


@contextmanager
def _catalog_collection_lock(engine: Engine) -> Generator[bool, None, None]:
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as connection:
        acquired = bool(
            connection.scalar(
                text("select pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _CATALOG_COLLECTION_LOCK_ID},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    connection.execute(
                        text("select pg_advisory_unlock(:lock_id)"),
                        {"lock_id": _CATALOG_COLLECTION_LOCK_ID},
                    )
                except DBAPIError as exc:
                    # Losing the session also releases its advisory lock server-side.
                    if not exc.connection_invalidated:
                        raise


def _execution_scope_payload(
    group: str,
    stable_scope: StableCollectionScope,
    plan: CollectionPlan,
    selected_excel_rows: tuple[int, ...],
    selected_channel_count: int,
) -> dict[str, Any]:
    selected_rows = set(selected_excel_rows)
    return {
        "scope_id": {
            "all": "full-catalog-audit-v1",
            "targeted": "targeted-catalog-rows-v1",
        }.get(group, stable_scope.scope_id),
        "group": group,
        "stable_scope_reference_sha256": stable_scope.scope_sha256,
        "selected_plan_sha256": plan_slice_sha256(plan, selected_excel_rows),
        "selected_excel_rows": list(selected_excel_rows),
        "selected_source_count": len(selected_excel_rows),
        "selected_channel_count": selected_channel_count,
        "not_selected_excel_rows": [
            source.excel_row for source in plan.sources if source.excel_row not in selected_rows
        ],
        "deferred_excel_rows": list(stable_scope.deferred_excel_rows),
        "not_selected_deferred_excel_rows": [
            row for row in stable_scope.deferred_excel_rows if row not in selected_excel_rows
        ],
        "deferred_sources": list(stable_scope.deferred_sources),
        "limited_sources": list(stable_scope.limited_sources),
        "evidence_reports": list(stable_scope.evidence_reports),
    }


def _exit_code(
    group: str,
    report: BatchCollectionReport,
    *,
    storage_boundary_unchanged: bool = True,
) -> int:
    if not storage_boundary_unchanged:
        return 1
    if len(report.outcomes) != report.channel_count:
        return 1
    if any(
        outcome.error_category == "internal" or outcome.error_code == "unhandled_collection_error"
        for outcome in report.outcomes
    ):
        return 1
    if group == "all":
        return 0
    succeeded = report.channel_status_counts.get("succeeded", 0)
    return 0 if succeeded == report.channel_count else 1


def _preflight_error(
    group: str,
    selected_excel_rows: tuple[int, ...],
    *,
    has_comtrade_key: bool,
) -> dict[str, Any] | None:
    if group == "all" or 48 not in selected_excel_rows or has_comtrade_key:
        return None
    return {
        "error": "missing_comtrade_subscription_key",
        "message": (
            "stable structured collection requires GUOBIE_COMTRADE_SUBSCRIPTION_KEY; no source was fetched"
        ),
        "collection_group": group,
        "exit_code": 2,
    }


def _parse_excel_rows(value: str) -> tuple[int, ...]:
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise argparse.ArgumentTypeError("--rows must be a comma-separated list of Excel rows")
    try:
        rows = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--rows values must be integers") from exc
    if len(rows) != len(set(rows)):
        raise argparse.ArgumentTypeError("--rows must not contain duplicates")
    if any(row < 4 or row > 53 for row in rows):
        raise argparse.ArgumentTypeError("--rows values must be between 4 and 53")
    return tuple(sorted(rows))


if __name__ == "__main__":
    raise SystemExit(main())
