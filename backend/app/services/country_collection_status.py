"""Country-visible freshness derived from document identity and collection evidence."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import (
    CollectionRun,
    Document,
    DocumentEntity,
    DocumentVersion,
    ResearchEntity,
    Source,
    SourceChannel,
)
from app.services.country_research_collection import collection_coverage, utc

CHANNELS = ("event_dynamics", "policy_center", "frontier_research", "think_tank_report")


def country_collection_status(session, iso3, classify):
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    week_start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    linked = (
        select(DocumentVersion.document_id)
        .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
        .join(ResearchEntity, ResearchEntity.id == DocumentEntity.entity_id)
        .where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == iso3,
            DocumentEntity.review_status == "confirmed",
        )
    )
    documents = session.execute(
        select(Document, Source).join(Source, Source.id == Document.source_id).where(Document.id.in_(linked))
    ).all()
    grouped = {key: [] for key in CHANNELS}
    for document, source in documents:
        key = classify(document, source)
        if key in grouped:
            grouped[key].append(document)
    sources = {s.id: s.name for _, s in documents}
    channels = list(
        session.scalars(
            select(SourceChannel).where(
                SourceChannel.source_id.in_(sources), SourceChannel.status != "retired"
            )
        )
    )
    latest_ids = (
        select(func.max(CollectionRun.id))
        .where(CollectionRun.channel_id.in_([c.id for c in channels]))
        .group_by(CollectionRun.channel_id)
    )
    latest = {
        r.channel_id: r
        for r in session.scalars(select(CollectionRun).where(CollectionRun.id.in_(latest_ids)))
    }
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "data/drc_mvp_sources.json").read_text())
    cadences = {s["source_id"]: s.get("update_cadence", "manual") for s in manifest["sources"]}
    output = {}
    for key, rows in grouped.items():
        source_ids = {d.source_id for d in rows}
        relevant = [c for c in channels if c.source_id in source_ids]
        successes = [utc(c.last_success_at) for c in relevant if c.last_success_at]
        checks = successes + [
            utc(latest[c.id].finished_at) for c in relevant if c.id in latest and latest[c.id].finished_at
        ]
        dates = [d.published_at.date() for d in rows if d.published_at]
        failures = [
            {"source": sources[c.source_id], "code": latest[c.id].error_code}
            for c in relevant
            if c.id in latest and latest[c.id].status in {"failed", "partial"}
        ]
        modes = {cadences.get(c.collector_config.get("manifest_source_id"), "manual") for c in relevant}
        output[key] = {
            "document_count": len(rows),
            "new_this_week": sum(utc(d.first_seen_at) >= week_start for d in rows),
            "last_new_at": max((utc(d.first_seen_at) for d in rows), default=None),
            "last_success_at": max(successes, default=None),
            "last_checked_at": max(checks, default=None),
            "published_from": min(dates, default=None),
            "published_to": max(dates, default=None),
            "failures": failures,
            "update_mode": "部分栏目自动更新，其余材料按需补录"
            if modes & {"daily", "weekly"}
            else "按需补录；未确认持续采集覆盖",
        }
    if iso3 == "COD":
        coverage = collection_coverage(session)
        row = output["frontier_research"]
        updates = next((p for p in coverage["partitions"] if p["partition"] == "updates"), {})
        row.update(
            update_mode="每周增量＋按年份回补",
            collection=coverage,
            last_success_at=updates.get("last_success_at"),
        )
        checks = [
            datetime.fromisoformat(p["last_checked_at"])
            for p in coverage["partitions"]
            if p.get("last_checked_at")
        ]
        row["last_checked_at"] = max(checks, default=None)
        row["failures"] = [
            {"source": "Crossref " + p["partition"], "code": p["error_code"]}
            for p in coverage["partitions"]
            if p.get("error_code")
        ]
    return {
        "country_iso3": iso3,
        "as_of": now,
        "channels": output,
        "new_count_note": "本周新增按首次入库时间统计，仅计入已关联本国的文档，包含历史回补。",
    }
