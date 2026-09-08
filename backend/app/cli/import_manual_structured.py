from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.manual_structured import (
    ManualStructuredParseResult,
    parse_imf_weo_csv,
    parse_mofcom_nonfinancial_odi_csv,
    parse_mofcom_south_africa_trade_csv,
    parse_nbs_annual_xlsx,
)
from app.db.session import get_session_factory
from app.models import CollectionItem, CollectionRun, SourceChannel
from app.services.collection_plan import load_collection_plan
from app.services.manual_structured_store import (
    IMF_WEO_DATASET_KEY,
    IMF_WEO_SCOPE_ID,
    MOFCOM_ODI_DATASET_KEY,
    MOFCOM_ODI_SCOPE_ID,
    MOFCOM_TRADE_DATASET_KEY,
    MOFCOM_TRADE_SCOPE_ID,
    NBS_ANNUAL_DATASET_KEY,
    NBS_ANNUAL_SCOPE_ID,
    ManualStructuredDatasetSpec,
    ManualStructuredStoreResult,
    persist_manual_structured,
)
from app.services.source_catalog import load_source_catalog
from app.services.source_importer import import_catalog

IMF_PROFILE = "imf_weo_manual"
NBS_PROFILE = "nbs_annual_manual"
MOFCOM_TRADE_PROFILE = "mofcom_trade_manual"
MOFCOM_ODI_PROFILE = "mofcom_odi_manual"
IMF_OFFICIAL_PAGE = "https://data.imf.org/en/datasets/IMF.RES%3AWEO?indicator_id=GGXWDG_NGDP"
NBS_OFFICIAL_PAGE = "https://data.stats.gov.cn/dg/website/page.html#/pc/national/yearData"
MOFCOM_TRADE_OFFICIAL_PAGE = "https://data.mofcom.gov.cn/hwmy/imexCountry.shtml"
MOFCOM_ODI_OFFICIAL_PAGE = "https://data.mofcom.gov.cn/tzhz/fordirinvest.shtml"
PARSER_VERSION = "manual-structured-v1"


class ManualImportCliError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _ImportJob:
    excel_row: int
    profile: str
    file_path: Path
    official_page_url: str
    parser: Callable[[Path], ManualStructuredParseResult]
    spec: ManualStructuredDatasetSpec


def main() -> int:
    args = _parse_args()
    try:
        payload = _run(args)
    except ManualImportCliError as exc:
        print(
            json.dumps(
                {"status": "failed", "error_code": exc.code, "error_message": exc.message},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "manual_structured_import_failed",
                    "error_message": "manual structured import failed",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(
        description="Import reviewed structured data files from official source pages."
    )
    parser.add_argument("--catalog", type=Path, default=root / "data" / "source_catalog.json")
    parser.add_argument("--plan", type=Path, default=root / "data" / "source_collection_plan.json")
    parser.add_argument("--imf-csv", type=Path)
    parser.add_argument("--nbs-xlsx", type=Path)
    parser.add_argument("--mofcom-trade-csv", type=Path)
    parser.add_argument("--mofcom-odi-csv", type=Path)
    parser.add_argument("--registration-docx", type=Path)
    parser.add_argument("--report-dir", type=Path, default=root / "data" / "collection-runs")
    return parser.parse_args()


def _run(args: argparse.Namespace) -> dict[str, object]:
    supplied_paths = (
        args.imf_csv,
        args.nbs_xlsx,
        args.mofcom_trade_csv,
        args.mofcom_odi_csv,
    )
    if not any(supplied_paths):
        raise ManualImportCliError("manual_file_required", "at least one reviewed data file is required")
    if (args.imf_csv or args.nbs_xlsx) and args.registration_docx is None:
        raise ManualImportCliError(
            "registration_required",
            "IMF and NBS imports require the delivery registration DOCX",
        )

    registration_sha256 = _file_sha256(args.registration_docx) if args.registration_docx is not None else None
    jobs: list[_ImportJob] = []
    if args.imf_csv is not None:
        jobs.append(
            _ImportJob(
                excel_row=45,
                profile=IMF_PROFILE,
                file_path=args.imf_csv,
                official_page_url=IMF_OFFICIAL_PAGE,
                parser=parse_imf_weo_csv,
                spec=ManualStructuredDatasetSpec(
                    dataset_key=IMF_WEO_DATASET_KEY,
                    scope_id=IMF_WEO_SCOPE_ID,
                    name="IMF WEO April 2026 general government gross debt for four focus countries",
                    expected_count=40,
                ),
            )
        )
    if args.nbs_xlsx is not None:
        jobs.append(
            _ImportJob(
                excel_row=47,
                profile=NBS_PROFILE,
                file_path=args.nbs_xlsx,
                official_page_url=NBS_OFFICIAL_PAGE,
                parser=parse_nbs_annual_xlsx,
                spec=ManualStructuredDatasetSpec(
                    dataset_key=NBS_ANNUAL_DATASET_KEY,
                    scope_id=NBS_ANNUAL_SCOPE_ID,
                    name="国家统计局全国年度工业与有色金属指标",
                    expected_count=60,
                ),
            )
        )
    if args.mofcom_trade_csv is not None:
        jobs.append(
            _ImportJob(
                excel_row=51,
                profile=MOFCOM_TRADE_PROFILE,
                file_path=args.mofcom_trade_csv,
                official_page_url=MOFCOM_TRADE_OFFICIAL_PAGE,
                parser=parse_mofcom_south_africa_trade_csv,
                spec=ManualStructuredDatasetSpec(
                    dataset_key=MOFCOM_TRADE_DATASET_KEY,
                    scope_id=MOFCOM_TRADE_SCOPE_ID,
                    name="商务部中国与南非货物进出口月度累计统计",
                    expected_count=360,
                    frequency="monthly",
                    import_mode="user_provided_reviewed_copy",
                ),
            )
        )
    if args.mofcom_odi_csv is not None:
        jobs.append(
            _ImportJob(
                excel_row=51,
                profile=MOFCOM_ODI_PROFILE,
                file_path=args.mofcom_odi_csv,
                official_page_url=MOFCOM_ODI_OFFICIAL_PAGE,
                parser=parse_mofcom_nonfinancial_odi_csv,
                spec=ManualStructuredDatasetSpec(
                    dataset_key=MOFCOM_ODI_DATASET_KEY,
                    scope_id=MOFCOM_ODI_SCOPE_ID,
                    name="商务部中国非金融类对外直接投资月度累计统计",
                    expected_count=480,
                    frequency="monthly",
                    import_mode="user_provided_reviewed_copy",
                ),
            )
        )
    parsed_jobs = [(job, _file_sha256(job.file_path), job.parser(job.file_path)) for job in jobs]
    catalog = load_source_catalog(args.catalog)
    plan = load_collection_plan(args.plan)

    outcomes: list[dict[str, object]] = []
    with get_session_factory()() as session:
        imported = import_catalog(session, catalog, plan)
        session.commit()
        for job, file_sha256, parsed in parsed_jobs:
            channel = _select_channel(session, imported.channel_ids, job)
            outcomes.append(
                _import_one(
                    session,
                    job=job,
                    channel=channel,
                    file_sha256=file_sha256,
                    registration_file_name=(
                        args.registration_docx.name if args.registration_docx is not None else None
                    ),
                    registration_sha256=registration_sha256,
                    parsed=parsed,
                )
            )

    payload: dict[str, object] = {
        "status": "succeeded",
        "generated_at": datetime.now(UTC).isoformat(),
        "registration_file": (args.registration_docx.name if args.registration_docx is not None else None),
        "registration_sha256": registration_sha256,
        "imports": outcomes,
    }
    json_path, markdown_path = _write_report(args.report_dir, payload)
    payload["json_report"] = str(json_path)
    payload["markdown_report"] = str(markdown_path)
    return payload


def _select_channel(
    session: Session,
    imported_channel_ids: tuple[int, ...],
    job: _ImportJob,
) -> SourceChannel:
    matches = []
    for channel_id in imported_channel_ids:
        channel = session.get(SourceChannel, channel_id)
        if (
            channel is not None
            and channel.collector_config.get("catalog_excel_row") == job.excel_row
            and channel.collector_config.get("profile") == job.profile
        ):
            matches.append(channel)
    if len(matches) != 1:
        raise ManualImportCliError(
            "manual_channel_mismatch",
            f"expected exactly one reviewed manual channel for Excel row {job.excel_row}",
        )
    channel = matches[0]
    if (
        channel.status != "shadow"
        or channel.link_role != "official"
        or channel.collector_config.get("scope_id") != job.spec.scope_id
        or channel.collector_config.get("dataset_key") != job.spec.dataset_key
    ):
        raise ManualImportCliError(
            "manual_channel_scope_mismatch",
            f"manual channel for Excel row {job.excel_row} is outside the reviewed scope",
        )
    return channel


def _import_one(
    session: Session,
    *,
    job: _ImportJob,
    channel: SourceChannel,
    file_sha256: str,
    registration_file_name: str | None,
    registration_sha256: str | None,
    parsed: ManualStructuredParseResult,
) -> dict[str, object]:
    active = session.scalar(
        select(CollectionRun).where(
            CollectionRun.channel_id == channel.id,
            CollectionRun.status.in_(("queued", "running")),
        )
    )
    if active is not None:
        raise ManualImportCliError(
            "manual_channel_busy",
            f"manual channel {channel.id} already has an active run",
        )
    now = datetime.now(UTC)
    run = CollectionRun(
        channel_id=channel.id,
        trigger_kind="manual",
        status="running",
        started_at=now,
        heartbeat_at=now,
    )
    session.add(run)
    session.flush()
    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=job.official_page_url,
            normalized_url=job.official_page_url,
            status="fetched",
        )
    )
    stored = persist_manual_structured(
        session,
        channel_id=channel.id,
        collection_run_id=run.id,
        retrieved_at=now,
        official_page_url=job.official_page_url,
        file_name=job.file_path.name,
        file_sha256=file_sha256,
        registration_file_name=registration_file_name,
        registration_sha256=registration_sha256,
        parser_version=PARSER_VERSION,
        spec=job.spec,
        parsed=parsed,
    )
    run.status = "succeeded"
    run.finished_at = now
    run.heartbeat_at = now
    run.items_discovered = parsed.expected_count
    run.items_persisted = parsed.expected_count
    run.report = _run_report(job, file_sha256, parsed, stored)
    channel.last_success_at = now
    channel.source.status = "active"
    session.commit()
    return {
        "excel_row": job.excel_row,
        "file_name": job.file_path.name,
        "file_sha256": file_sha256,
        "run_id": run.id,
        "dataset_id": stored.dataset_id,
        "snapshot_id": stored.snapshot_id,
        "snapshot_created": stored.snapshot_created,
        "expected_count": parsed.expected_count,
        "valued_count": parsed.valued_count,
        "source_null_count": parsed.source_null_count,
        "not_returned_count": parsed.not_returned_count,
        "observations_created": stored.observations_created,
        "versions_created": stored.versions_created,
        "versions_unchanged": stored.versions_unchanged,
    }


def _run_report(
    job: _ImportJob,
    file_sha256: str,
    parsed: ManualStructuredParseResult,
    stored: ManualStructuredStoreResult,
) -> dict[str, object]:
    return {
        "profile": job.profile,
        "scope_id": job.spec.scope_id,
        "dataset_key": job.spec.dataset_key,
        "manual_file": {
            "file_name": job.file_path.name,
            "file_sha256": file_sha256,
            "official_page_url": job.official_page_url,
            "parser_version": PARSER_VERSION,
            "import_mode": job.spec.import_mode,
            "raw_content_stored": False,
        },
        "coverage": {
            "expected_count": parsed.expected_count,
            "returned_count": parsed.returned_count,
            "valued_count": parsed.valued_count,
            "source_null_count": parsed.source_null_count,
            "not_returned_count": parsed.not_returned_count,
        },
        "store": asdict(stored),
    }


def _write_report(report_dir: Path, payload: dict[str, object]) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-manual-structured-import"
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 国别智枢人工结构化文件导入报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 登记表：`{payload['registration_file'] or '未提供'}`",
        f"- 登记表 SHA256：`{payload['registration_sha256'] or '未提供'}`",
        "",
        "| 行号 | 文件 | 运行 | 快照 | 预期 | 有值 | 来源空值 | 新观测 | 新版本 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["imports"]:
        lines.append(
            f"| {item['excel_row']} | `{item['file_name']}` | {item['run_id']} | "
            f"{item['snapshot_id']} | {item['expected_count']} | {item['valued_count']} | "
            f"{item['source_null_count']} | {item['observations_created']} | "
            f"{item['versions_created']} |"
        )
    lines.extend(
        [
            "",
            "## 保存边界",
            "",
            "- 原始 CSV、Excel 和登记表未复制进数据库或对象存储；数据库只记录文件名与 SHA256。",
            "- 所有观测进入结构化快照与不可变版本表，来源空值不写成 0。",
            "- 未附登记表的文件按“用户提供的人工复核副本”记录，不表述为已证明未修改的官方原始导出。",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def _file_sha256(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as exc:
        raise ManualImportCliError("manual_file_unreadable", f"cannot read {path.name}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
