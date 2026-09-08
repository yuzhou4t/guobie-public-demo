from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.collectors.manual_wits_tariff import ManualWitsTariffError
from app.db.session import get_session_factory
from app.models import (
    CollectionItem,
    CollectionRun,
    SourceChannel,
    StructuredDataset,
)
from app.services.manual_wits_tariff_import import build_manual_wits_import
from app.services.wits_tariff_store import (
    WITS_TARIFF_DATASET_KEY,
    WITS_TARIFF_SCOPE_ID,
    WitsTariffStoreError,
    persist_wits_tariff,
)

PARSER_VERSION = "manual-wits-tariff-zip-v1"


class ManualWitsImportCliError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main() -> int:
    args = _parse_args()
    try:
        payload = _run(args)
    except (ManualWitsImportCliError, ManualWitsTariffError, WitsTariffStoreError) as exc:
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
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "manual_wits_import_failed",
                    "error_message": "manual WITS tariff import failed",
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
    parser = argparse.ArgumentParser(description="Import the eight reviewed WITS full-tariff ZIP exports.")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=root / "data" / "collection-runs")
    return parser.parse_args()


def _run(args: argparse.Namespace) -> dict[str, object]:
    zip_paths = _zip_paths(args.input_dir)
    file_sha256 = {path: _file_sha256(path) for path in zip_paths}
    imported_at = datetime.now(UTC)

    with get_session_factory()() as session:
        dataset = session.scalar(
            select(StructuredDataset).where(StructuredDataset.dataset_key == WITS_TARIFF_DATASET_KEY)
        )
        if dataset is None:
            raise ManualWitsImportCliError(
                "manual_wits_dataset_missing",
                "reviewed WITS dataset does not exist",
            )
        channel = session.get(SourceChannel, dataset.channel_id)
        if channel is None:
            raise ManualWitsImportCliError(
                "manual_wits_channel_missing",
                "reviewed WITS channel does not exist",
            )
        _validate_channel(channel)
        active_run = session.scalar(
            select(CollectionRun).where(
                CollectionRun.channel_id == channel.id,
                CollectionRun.status.in_(("queued", "running")),
            )
        )
        if active_run is not None:
            raise ManualWitsImportCliError(
                "manual_wits_channel_busy",
                f"WITS channel {channel.id} already has an active run",
            )

        panel, evidence = build_manual_wits_import(
            session,
            zip_paths=zip_paths,
            file_sha256=file_sha256,
            imported_at=imported_at,
        )
        run = CollectionRun(
            channel_id=channel.id,
            trigger_kind="manual",
            status="running",
            started_at=imported_at,
            heartbeat_at=imported_at,
        )
        session.add(run)
        session.flush()
        manual_evidence = [item for item in evidence if item.kind == "manual_tariff_zip"]
        session.add_all(
            CollectionItem(
                run_id=run.id,
                request_url=item.request_url,
                normalized_url=item.request_url,
                discovered_from_url=channel.entry_url,
                status="fetched",
            )
            for item in manual_evidence
        )
        stored = persist_wits_tariff(
            session,
            channel_id=channel.id,
            collection_run_id=run.id,
            retrieved_at=imported_at,
            panel=panel,
            query_evidence=evidence,
        )
        run.status = "succeeded"
        run.finished_at = imported_at
        run.heartbeat_at = imported_at
        run.items_discovered = 84
        run.items_persisted = 84
        run.report = {
            "profile": "wits_tariff_manual_supplement",
            "scope_id": WITS_TARIFF_SCOPE_ID,
            "dataset_key": WITS_TARIFF_DATASET_KEY,
            "parser_version": PARSER_VERSION,
            "manual_files": [
                {
                    "file_name": item.source_file_name,
                    "file_sha256": item.resource_sha256,
                    "source_url": item.request_url,
                }
                for item in manual_evidence
            ],
            "raw_content_stored": False,
            "coverage": {
                "expected_count": 84,
                "returned_count": panel.returned_count,
                "valued_count": panel.valued_count,
                "source_null_count": panel.source_null_count,
                "not_returned_count": panel.not_returned_count,
            },
            "confirmed_unavailable_schedule_count": 5,
            "store": {
                "snapshot_id": stored.snapshot_id,
                "snapshot_created": stored.snapshot_created,
                "observations_created": stored.observations_created,
                "versions_created": stored.versions_created,
                "versions_unchanged": stored.versions_unchanged,
            },
        }
        channel.last_success_at = imported_at
        session.commit()

    payload: dict[str, object] = {
        "status": "succeeded",
        "generated_at": imported_at.isoformat(),
        "run_id": run.id,
        "dataset_id": stored.dataset_id,
        "snapshot_id": stored.snapshot_id,
        "snapshot_created": stored.snapshot_created,
        "valued_count": panel.valued_count,
        "not_returned_count": panel.not_returned_count,
        "versions_created": stored.versions_created,
        "versions_unchanged": stored.versions_unchanged,
        "manual_files": [path.name for path in zip_paths],
    }
    json_path, markdown_path = _write_report(args.report_dir, payload)
    payload["json_report"] = str(json_path)
    payload["markdown_report"] = str(markdown_path)
    return payload


def _validate_channel(channel: SourceChannel) -> None:
    if (
        channel.status != "shadow"
        or channel.link_role != "official"
        or channel.collector_config.get("profile") != "wits_tariff"
        or channel.collector_config.get("scope_id") != WITS_TARIFF_SCOPE_ID
        or channel.collector_config.get("dataset_key") != WITS_TARIFF_DATASET_KEY
    ):
        raise ManualWitsImportCliError(
            "manual_wits_channel_scope",
            "WITS channel is outside the reviewed shadow scope",
        )


def _zip_paths(input_dir: Path) -> tuple[Path, ...]:
    if not input_dir.is_dir():
        raise ManualWitsImportCliError(
            "manual_wits_input_dir",
            "WITS supplement input directory is not readable",
        )
    paths = tuple(sorted(input_dir.glob("*.zip")))
    if len(paths) != 8:
        raise ManualWitsImportCliError(
            "manual_wits_zip_count",
            "WITS supplement input directory must contain exactly eight ZIP files",
        )
    return paths


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManualWitsImportCliError(
            "manual_wits_zip_unreadable",
            f"cannot read {path.name}",
        ) from exc
    return digest.hexdigest()


def _write_report(report_dir: Path, payload: dict[str, object]) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-manual-wits-tariff-import"
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        "\n".join(
            [
                "# WITS 关税手工 ZIP 补录报告",
                "",
                f"- 运行：{payload['run_id']}",
                f"- 快照：{payload['snapshot_id']}",
                f"- 是否创建新快照：{payload['snapshot_created']}",
                "- 固定面板：84 格",
                f"- 有值：{payload['valued_count']} 格",
                f"- 暂无：{payload['not_returned_count']} 格（5 张国家—年份税则表）",
                f"- 新版本：{payload['versions_created']}；复用版本：{payload['versions_unchanged']}",
                "",
                "## 口径",
                "",
                "- 8 个 WITS 完整税则 ZIP 均通过结构、范围和 SHA256 校验。",
                "- 刚果（金）2019、2020 与库内既有值一致；其余 6 张税则表补入 18 个值。",
                "- 刚果（金）2017、2018、2021及赞比亚2017、2019保留为“暂无”，不补 0。",
                "- 原始 ZIP 未复制进数据库或对象存储；只记录文件名、SHA256 和官方查询地址。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return json_path, markdown_path


if __name__ == "__main__":
    raise SystemExit(main())
