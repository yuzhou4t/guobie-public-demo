from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import OperationalError

from app.cli.collect_catalog import _catalog_collection_lock, _parse_excel_rows
from app.db.session import (
    get_advisory_lock_engine,
    get_session_factory,
    reset_database_caches,
)
from app.services.abstract_backfill import AbstractBackfillReport, backfill_reviewed_abstracts
from app.services.collection_plan import load_collection_plan
from app.services.source_catalog import load_source_catalog
from app.services.source_importer import import_catalog
from app.services.source_probe import SafeHttpClient

_MAX_RECONNECT_ATTEMPTS = 10
_COMMIT_EVERY = 5


def main() -> int:
    args = _parse_args()
    catalog = load_source_catalog(args.catalog)
    plan = load_collection_plan(args.plan)
    report: AbstractBackfillReport | None = None
    last_error: OperationalError | None = None
    run_started_at = datetime.now(UTC)
    # 云端 Neon 会在空闲后休眠 compute，长时间回填可能中途断开数据库连接
    # （SSL EOF）。小批提交保留已完成进度；重试时跳过本次运行已提交的文档，
    # 只重做最后一个未提交小批次。
    for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
        try:
            with _catalog_collection_lock(get_advisory_lock_engine()) as lock_acquired:
                if not lock_acquired:
                    print(
                        json.dumps(
                            {
                                "error": "collection_already_running",
                                "message": (
                                    "another catalog or abstract backfill process holds the database lock"
                                ),
                                "exit_code": 75,
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                    )
                    return 75
                with get_session_factory()() as session:
                    import_catalog(session, catalog, plan)
                    report = backfill_reviewed_abstracts(
                        session,
                        client=SafeHttpClient(),
                        limit=args.limit,
                        rows=args.rows,
                        retry_due=args.retry_due,
                        recheck_retryable=args.recheck_retryable,
                        recheck_unavailable=args.recheck_unavailable,
                        recheck_blocked=args.recheck_blocked,
                        progress_callback=_print_progress,
                        commit_every=_COMMIT_EVERY,
                        skip_checked_since=run_started_at if attempt > 1 else None,
                    )
                    session.commit()
            break
        except OperationalError as exc:
            last_error = exc
            if attempt >= _MAX_RECONNECT_ATTEMPTS:
                break
            print(
                f"database connection lost during abstract backfill (attempt {attempt}); "
                f"reconnecting after 5s: {exc.__class__.__name__}",
                file=sys.stderr,
            )
            reset_database_caches()
            time.sleep(5)
    if report is None:
        if last_error is not None:
            raise last_error
        raise RuntimeError("abstract backfill produced no report")

    json_path, markdown_path = _write_report(args.report_dir, report)
    print(
        json.dumps(
            {
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
                **report.as_dict(),
                "exit_code": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _print_progress(attempted: int, limit: int) -> None:
    if attempted != 1 and attempted % 10 != 0 and attempted != limit:
        return
    print(
        json.dumps(
            {
                "event": "abstract_backfill_progress",
                "attempted": attempted,
                "limit": limit,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Backfill reviewed source-provided abstracts without storing bodies or raw responses."
    )
    parser.add_argument("--catalog", type=Path, default=root / "data" / "source_catalog.json")
    parser.add_argument("--plan", type=Path, default=root / "data" / "source_collection_plan.json")
    parser.add_argument("--report-dir", type=Path, default=root / "var" / "collection-runs")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--rows", type=_parse_excel_rows)
    parser.add_argument("--retry-due", action="store_true")
    parser.add_argument(
        "--recheck-retryable",
        action="store_true",
        help="immediately retry every selected retryable state, even before next_retry_at",
    )
    parser.add_argument("--recheck-unavailable", action="store_true")
    parser.add_argument("--recheck-blocked", action="store_true")
    return parser.parse_args()


def _write_report(
    report_dir: Path,
    report: AbstractBackfillReport,
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = report_dir / f"{stamp}-abstract-backfill.json"
    markdown_path = report_dir / f"{stamp}-abstract-backfill.md"
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "storage_policy": "reviewed_source_provided_abstracts_no_body_no_raw_no_rag",
        **report.as_dict(),
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown_report(payload), encoding="utf-8")
    return json_path, markdown_path


def _markdown_report(payload: dict[str, Any]) -> str:
    statuses = "，".join(f"{key}={value}" for key, value in payload["status_counts"].items())
    reasons = "，".join(f"{key}={value}" for key, value in payload["reason_counts"].items())
    lines = [
        "# 国别智枢历史摘要回填报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 文档总数：{payload['total_documents']}",
        f"- 当前有摘要：{payload['current_documents_with_abstract']}",
        f"- 本轮初始化状态：{payload['states_initialized']}",
        f"- 本轮实际请求：{payload['attempted']}",
        f"- 新增摘要版本：{payload['versions_created']}",
        f"- 状态：{statuses or '无'}",
        f"- 缺失原因：{reasons or '无'}",
        "- 保存边界：只保存已审核来源自带摘要；不保存正文、原始响应或 RAG 内容。",
        "",
        "| 信源 | 文档 | 已存摘要 | 待处理 | 可重试 | 无摘要 | 政策/范围阻断 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for source in payload["source_coverage"]:
        lines.append(
            f"| {_cell(source['source_name'])} | {source.get('total', 0)} | "
            f"{source.get('stored', 0)} | {source.get('pending', 0)} | "
            f"{source.get('retryable', 0)} | {source.get('unavailable', 0)} | "
            f"{source.get('blocked', 0)} |"
        )
    return "\n".join(lines) + "\n"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


if __name__ == "__main__":
    raise SystemExit(main())
