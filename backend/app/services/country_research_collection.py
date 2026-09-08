"""Bounded COD research backfill and weekly updates with a transactional ledger."""

import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.collectors.country_history import parse_crossref_history
from app.models import CollectionRun, Document, DocumentVersion, Source, SourceChannel, SourceChannelPolicy
from app.services.cod_research_store import store_cod_papers
from app.services.robots_policy import check_robots
from app.services.source_probe import ProbeError, SafeHttpClient

PROFILE = "cod_research_continuous_v1"
START = date(2023, 1, 1)
ENDPOINT = "https://api.crossref.org/works"
ROWS = 100
SCOPE_NOTE = (
    "Crossref 期刊论文；题名检索 Congo，再按明确国别或地区名称复核。"
    "各年使用相同标准；不代表全部学术文献，不按篇数配额截断。"
)


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def register_channels(session: Session, today: date) -> list[SourceChannel]:
    source = session.scalar(select(Source).where(Source.name == "Crossref · 刚果（金）研究更新"))
    if source is None:
        source = Source(
            name="Crossref · 刚果（金）研究更新",
            organization_name="Crossref",
            source_type="metadata_registry",
            country_or_region="COD",
            homepage_url="https://www.crossref.org/",
            status="active",
        )
        session.add(source)
        session.flush()
    channels = []
    # Each year gets an independent checkpoint; newest years receive the first requests.
    for key in ["updates", *map(str, range(today.year, START.year - 1, -1))]:
        entry = (
            ENDPOINT
            + "?"
            + urlencode(
                {
                    "query.title": "Congo",
                    "filter": (
                        "type:journal-article"
                        if key == "updates"
                        else f"type:journal-article,from-pub-date:{key}-01-01,until-pub-date:{key}-12-31"
                    ),
                }
            )
        )
        channel = session.scalar(
            select(SourceChannel).where(
                SourceChannel.source_id == source.id, SourceChannel.entry_url == entry
            )
        )
        if channel is None:
            channel = SourceChannel(
                source_id=source.id,
                name=f"COD 研究 · {'每周增量' if key == 'updates' else key + ' 年回补'}",
                entry_url=entry,
                collector_type="api",
                status="shadow",
                link_role="official",
                collector_config={
                    "profile": PROFILE,
                    "country_iso3": "COD",
                    "partition": key,
                    "scope_note": SCOPE_NOTE,
                },
                poll_interval_seconds=604800 if key == "updates" else 86400,
            )
            channel.policy = SourceChannelPolicy(
                storage_scope="metadata",
                rag_scope="none",
                terms_state="metadata_only",
                notes="采集账本；论文内容权限由逐 DOI 栏目分别核验。",
            )
            session.add(channel)
            session.flush()
        channels.append(channel)
    return channels


def query_url(channel: SourceChannel, state: dict, cursor: str) -> str:
    filters = ["type:journal-article", f"from-pub-date:{state['from']}", f"until-pub-date:{state['to']}"]
    if channel.collector_config["partition"] == "updates":
        filters.extend(
            [f"from-update-date:{state['update_from']}", f"until-update-date:{state['update_to']}"]
        )
    return (
        ENDPOINT
        + "?"
        + urlencode(
            {
                "query.title": "Congo",
                "filter": ",".join(filters),
                "rows": ROWS,
                "cursor": cursor,
            }
        )
    )


def stable_candidate(session: Session, candidate):
    record_url = ENDPOINT + "/" + candidate.doi
    meta = dict(candidate.source_metadata)
    meta["metadata_source_url"] = record_url
    meta["published_at_provenance"] = {**meta.get("published_at_provenance", {}), "source_url": record_url}
    previous = session.scalar(
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.doi == candidate.doi, DocumentVersion.version_no == Document.latest_version_no)
    )
    if previous:
        old = previous.source_metadata
        if (
            previous.title == candidate.title
            and previous.published_at
            and utc(previous.published_at) == utc(candidate.published_at)
            and old.get("authors", []) == list(candidate.authors)
        ):
            meta["verified_at"] = old.get("verified_at", meta["verified_at"])
        if old.get("topics") == meta.get("topics") and old.get("topic_provenance"):
            meta["topic_provenance"] = old["topic_provenance"]
    return replace(
        candidate,
        discovery_url=record_url,
        source_metadata=meta,
        summary_source_url=record_url if candidate.summary else None,
    )


def is_due(channel: SourceChannel, now: datetime) -> bool:
    if channel.status != "shadow":
        return False
    if channel.next_run_at and utc(channel.next_run_at) > now:
        return False
    state = channel.cursor_state or {}
    if channel.collector_config["partition"] != "updates":
        return not state.get("complete")
    return not channel.last_success_at or utc(channel.last_success_at) + timedelta(days=7) <= now


def collect_partition(
    session: Session,
    channel: SourceChannel,
    *,
    client: SafeHttpClient,
    now: datetime,
    max_pages: int,
    registry: dict,
    pause=lambda: None,
) -> dict:
    """Commit each page and its cursor together. Partial runs never advance the watermark."""
    state = dict(channel.cursor_state or {})
    partition = channel.collector_config["partition"]
    if not state or (partition == "updates" and state.get("complete")):
        previous = state
        start = START if partition == "updates" else date(int(partition), 1, 1)
        end = now.date() if partition == "updates" else min(now.date(), date(int(partition), 12, 31))
        state = {
            "from": str(start),
            "to": str(end),
            "cursor": "*",
            "complete": False,
            "pages": 0,
            "records_checked": 0,
            "accepted": 0,
            "documents_created": 0,
            "last_new_at": previous.get("last_new_at"),
        }
        if partition == "updates":
            watermark = previous.get("update_to")
            since = (
                utc(datetime.fromisoformat(watermark)) - timedelta(days=1)
                if watermark
                else now - timedelta(days=8)
            )
            state.update(
                update_from=since.strftime("%Y-%m-%dT%H:%M:%S"), update_to=now.strftime("%Y-%m-%dT%H:%M:%S")
            )
    run = CollectionRun(
        channel_id=channel.id,
        trigger_kind="scheduled",
        status="running",
        started_at=now,
        heartbeat_at=now,
        report={"profile": PROFILE, "partition": partition},
    )
    channel.cursor_state = state
    session.add(run)
    session.commit()
    counts = {"documents_created": 0, "versions_created": 0, "accepted": 0, "records_checked": 0}
    manifests = []
    try:
        for _ in range(max_pages):
            url = query_url(channel, state, state["cursor"])
            decision = check_robots(client, url)
            if not decision.allowed:
                raise ProbeError("policy", decision.code, "robots does not allow this research request")
            resource = client.fetch(url)
            message = json.loads(resource.text())["message"]
            raw_items = message.get("items")
            if not isinstance(raw_items, list) or len(raw_items) > ROWS:
                raise ValueError("invalid Crossref page shape")
            candidates, skipped, cursor = parse_crossref_history(
                resource, date.fromisoformat(state["from"]), date.fromisoformat(state["to"]), registry
            )
            # Stable DOI identities avoid one new source channel for every cursor/window.
            candidates = [stable_candidate(session, c) for c in candidates]
            result = store_cod_papers(session, candidates)
            checked_at = resource.fetched_at.isoformat()
            next_state = {
                **state,
                "cursor": cursor,
                "complete": len(raw_items) < ROWS,
                "last_checked_at": checked_at,
                "pages": state["pages"] + 1,
                "records_checked": state["records_checked"] + len(raw_items),
                "accepted": state["accepted"] + len(candidates),
                "documents_created": state["documents_created"] + result["documents_created"],
                "provider_results": message.get("total-results"),
            }
            if not next_state["complete"] and (not cursor or cursor == state["cursor"]):
                raise ValueError("Crossref cursor did not advance; coverage remains incomplete")
            if result["documents_created"]:
                next_state["last_new_at"] = checked_at
            for key in ("documents_created", "versions_created"):
                counts[key] += result[key]
            counts["accepted"] += len(candidates)
            counts["records_checked"] += len(raw_items)
            manifests.append(
                {
                    "url": resource.final_url,
                    "sha256": resource.sha256,
                    "fetched_at": checked_at,
                    "records": len(raw_items),
                    "accepted": len(candidates),
                    "excluded": dict(Counter(s["reason"] for s in skipped)),
                    "documents_created": result["documents_created"],
                }
            )
            channel.cursor_state = next_state
            run.heartbeat_at = resource.fetched_at
            run.items_discovered = counts["records_checked"]
            run.items_persisted = counts["accepted"]
            run.report = {
                "profile": PROFILE,
                "partition": partition,
                **counts,
                "pages": manifests,
                "raw_content_stored": False,
            }
            session.commit()
            state = next_state
            if state["complete"]:
                break
            pause()
        run.status = "succeeded" if state["complete"] else "partial"
        if state["complete"]:
            channel.last_success_at = max(now, datetime.now(UTC))
        channel.next_run_at = None if state["complete"] else datetime.now(UTC) + timedelta(hours=12)
    except (ProbeError, ValueError, KeyError, SQLAlchemyError) as error:
        session.rollback()
        run.status = "failed"
        run.error_category = (
            "storage" if isinstance(error, SQLAlchemyError) else getattr(error, "category", "parser")
        )
        run.error_code = getattr(error, "code", "invalid_crossref_page")
        run.error_message = (
            "database page write failed; checkpoint not advanced"
            if isinstance(error, SQLAlchemyError)
            else str(error)
        )
        channel.next_run_at = datetime.now(UTC) + timedelta(
            days=1 if run.error_code == "http_429" else 0, hours=6
        )
        run.report = {**(run.report or {}), "failed_request_url": url}
        if isinstance(error, ProbeError) and error.retry_after:
            run.report = {**run.report, "retry_after": error.retry_after}
            try:
                value = error.retry_after
                until = (
                    datetime.now(UTC) + timedelta(seconds=int(value))
                    if value.isdigit()
                    else utc(parsedate_to_datetime(value))
                )
                channel.next_run_at = max(utc(channel.next_run_at), until)
            except (ValueError, TypeError, OverflowError):
                pass
    run.finished_at = max(now, datetime.now(UTC))
    session.commit()
    return {
        "run_id": run.id,
        "partition": partition,
        "status": run.status,
        **{k: (run.report or {}).get(k, 0) for k in counts},
        "error_code": run.error_code,
        "complete": (channel.cursor_state or {}).get("complete", False),
    }


def load_registry() -> dict:
    path = Path(__file__).resolve().parents[3] / "data/cod_research_papers.json"
    return {p["doi"].lower(): p for p in json.loads(path.read_text())["papers"]}


def collection_coverage(session: Session) -> dict:
    channels = [
        c for c in session.scalars(select(SourceChannel)) if c.collector_config.get("profile") == PROFILE
    ]
    channels.sort(key=lambda c: c.collector_config["partition"], reverse=True)
    rows = []
    for channel in channels:
        latest = session.scalar(
            select(CollectionRun)
            .where(CollectionRun.channel_id == channel.id)
            .order_by(CollectionRun.id.desc())
            .limit(1)
        )
        state = channel.cursor_state or {}
        rows.append(
            {
                "partition": channel.collector_config["partition"],
                "status": latest.status if latest else "pending",
                "from": state.get("from"),
                "to": state.get("to"),
                "complete": state.get("complete", False),
                "last_checked_at": state.get("last_checked_at"),
                "last_success_at": channel.last_success_at,
                "last_new_at": state.get("last_new_at"),
                "next_run_at": channel.next_run_at,
                "records_checked": state.get("records_checked", 0),
                "accepted": state.get("accepted", 0),
                "provider_results": state.get("provider_results"),
                "error_code": latest.error_code if latest else None,
            }
        )
    return {
        "scope_note": SCOPE_NOTE,
        "cadence": "weekly",
        "partitions": rows,
        "trend_comparable": False,
        "note": "当前展示本站收录分布；检索覆盖与主题分类未经可比性验收，不推断学术趋势。",
    }
