"""Resumable, local-only COD metadata backfill; never invokes the cloud mirror."""

import argparse
import fcntl
import json
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.collectors.country_history import (
    COUNTRY_TITLE,
    next_history_page,
    parse_crossref_history,
    parse_history_listing,
)
from app.collectors.drc_mvp import parse_drc_mvp_detail
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models import Document, DocumentEntity, ResearchEntity
from app.services.cod_research_store import store_cod_papers
from app.services.document_store import persist_candidate_metadata
from app.services.drc_mvp_import import _get_or_create_channel, _get_or_create_source
from app.services.robots_policy import check_robots
from app.services.source_probe import ProbeError, SafeHttpClient

ROOT = Path(__file__).resolve().parents[3]


def sources():
    manifest = json.loads((ROOT / "data/drc_mvp_sources.json").read_text())
    selected = [
        s
        for s in manifest["sources"]
        if s.get("update_cadence") in {"daily", "weekly"}
        and s.get("profile") not in {"single_document", "single_page_metadata"}
    ]
    selected.append(
        {
            "source_id": "crossref_cod_history",
            "name": "Crossref 刚果（金）研究历史",
            "profile": "crossref_history",
            "entry_url": "https://api.crossref.org/works",
        }
    )
    original = next(s for s in manifest["sources"] if s["source_id"].startswith("ebuteli_"))
    selected.append(
        {
            **original,
            "source_id": "ebuteli_history",
            "profile": "ebuteli_reports",
            "entry_url": "https://www.ebuteli.org/publications/rapports",
            "document_type": "report",
        }
    )
    return selected


def write_state(path, payload):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    temp.replace(path)


def fetch(client, url):
    decision = check_robots(client, url)
    if not decision.allowed:
        raise ProbeError("policy", decision.code, "robots did not allow this historical request")
    return client.fetch(url)


def save_metadata(session, source, candidates, resource):
    db_source, _ = _get_or_create_source(session, source)
    channel, _ = _get_or_create_channel(
        session, db_source, source, {"fetches": [{"fetched_at": resource.fetched_at.isoformat()}]}
    )
    country = session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country", ResearchEntity.canonical_key == "COD"
        )
    )
    created, versions, linked = 0, 0, 0
    for item in candidates:
        item = reuse_verified_deskeco_identity(session, item)
        item = replace(item, summary=None, summary_kind=None, summary_source_url=None)
        stored = persist_candidate_metadata(
            session,
            db_source.id,
            item,
            document_type=source.get("document_type")
            or ("report" if source["source_id"] == "un_1533_expert_reports" else "news"),
            extractor_name="country_history",
            storage_scope="metadata",
            channel_id=channel.id,
        )
        created += int(stored.document_created)
        versions += int(stored.version_created)
        # Scoped issuer collections establish country identity; general media require explicit title evidence.
        country_confirmed = source["source_id"] in {
            "un_1533_expert_reports",
            "cami_decisions",
            "ctcpm_public_data",
        } or bool(COUNTRY_TITLE.search(item.title))
        if country_confirmed and not session.get(DocumentEntity, (stored.version.id, country.id, "about")):
            session.add(
                DocumentEntity(
                    document_version_id=stored.version.id,
                    entity_id=country.id,
                    role="about",
                    extraction_method="imported",
                    review_status="confirmed",
                    note="历史回补：来源登记或题名明确关联 COD；未生成事件或项目关联。",
                )
            )
            linked += 1
    return {"documents_created": created, "versions_created": versions, "country_links_created": linked}


def reuse_verified_deskeco_identity(session, candidate):
    """Reuse an existing verified URL for Drupal's equivalent index.php route."""
    url = urlsplit(candidate.canonical_url or candidate.discovery_url)
    if url.hostname != "deskeco.com" or not url.path.startswith("/index.php/"):
        return candidate
    known_url = urlunsplit(url._replace(path=url.path[len("/index.php") :]))
    known = session.scalar(select(Document).where(Document.canonical_url == known_url))
    if known is None:
        return candidate
    return replace(candidate, canonical_url=known.canonical_url, discovery_url=known.discovery_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", choices=["COD"], default="COD")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2023, 9, 5))
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=date(2026, 9, 5))
    parser.add_argument("--source", action="append")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="revisit selected listing roots without removing prior audit pages",
    )
    parser.add_argument("--state", type=Path, default=ROOT / "var/country-history-20230905-20260905.json")
    args = parser.parse_args()
    if args.start > args.end or args.end > datetime.now(UTC).date() or not 1 <= args.max_pages <= 20:
        parser.error("invalid date window or page budget (1–20)")
    selected = sources()
    if args.source:
        unknown = set(args.source) - {s["source_id"] for s in selected}
        if unknown:
            parser.error(f"unregistered sources: {sorted(unknown)}")
        selected = [s for s in selected if s["source_id"] in args.source]
    if args.dry_run:
        print(
            json.dumps(
                {
                    "country": "COD",
                    "from": str(args.start),
                    "to": str(args.end),
                    "sources": [
                        {k: s.get(k) for k in ("source_id", "profile", "entry_url")} for s in selected
                    ],
                    "max_pages_per_source": args.max_pages,
                    "raw_content_stored": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    url = make_url(get_settings().database_url)
    if url.host not in {None, "127.0.0.1", "localhost"} or url.get_backend_name() != "postgresql":
        raise RuntimeError("Historical imports require the local PostgreSQL primary")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = (
            json.loads(args.state.read_text())
            if args.state.exists()
            else {"country": "COD", "from": str(args.start), "to": str(args.end), "sources": {}}
        )
        if (state["country"], state["from"], state["to"]) != ("COD", str(args.start), str(args.end)):
            raise ValueError("Checkpoint belongs to a different scope")
        registry = {
            p["doi"].lower(): p
            for p in json.loads((ROOT / "data/cod_research_papers.json").read_text())["papers"]
        }
        client = SafeHttpClient()
        for source in selected:
            key = source["source_id"]
            progress = state["sources"].setdefault(
                key,
                {
                    "pages": [],
                    "next": source["entry_url"],
                    "status": "pending",
                    "documents_created": 0,
                    "versions_created": 0,
                },
            )
            if args.recheck:
                progress.update(status="pending", next=source["entry_url"])
                progress.pop("cursor", None)
            if progress["status"] == "listing_exhausted":
                print(key, "already exhausted", flush=True)
                continue
            progress["status"] = "running"
            print(key, "starting from checkpoint", flush=True)
            for _ in range(args.max_pages):
                page_url = progress["next"]
                if source["profile"] == "crossref_history":
                    page_url = (
                        source["entry_url"]
                        + "?"
                        + urlencode(
                            {
                                "query.bibliographic": "Democratic Republic Congo",
                                "filter": (
                                    f"from-pub-date:{args.start},until-pub-date:{args.end},"
                                    "type:journal-article"
                                ),
                                "rows": 100,
                                "cursor": progress.get("cursor", "*"),
                            }
                        )
                    )
                try:
                    resource = fetch(client, page_url)
                    skipped = []
                    if source["profile"] == "crossref_history":
                        candidates, skipped, cursor = parse_crossref_history(
                            resource, args.start, args.end, registry
                        )
                        next_url = (
                            source["entry_url"] if cursor and cursor != progress.get("cursor") else None
                        )
                    else:
                        candidates = parse_history_listing(resource, source)
                        enriched = []
                        for candidate in candidates:
                            if not candidate.published_at:
                                try:
                                    candidate = parse_drc_mvp_detail(
                                        fetch(client, candidate.canonical_url or candidate.discovery_url),
                                        candidate,
                                    )
                                except (ProbeError, ValueError) as exc:
                                    skipped.append({"url": candidate.discovery_url, "reason": str(exc)})
                                time.sleep(1)
                            if (
                                candidate.published_at
                                and args.start <= candidate.published_at.date() <= args.end
                            ):
                                enriched.append(candidate)
                            else:
                                skipped.append(
                                    {
                                        "url": candidate.discovery_url,
                                        "reason": "unknown_date"
                                        if not candidate.published_at
                                        else "outside_date_window",
                                    }
                                )
                        candidates = enriched
                        next_url = next_history_page(resource)
                        cursor = None
                    with get_session_factory()() as session:
                        result = (
                            store_cod_papers(session, candidates)
                            if source["profile"] == "crossref_history"
                            else save_metadata(session, source, candidates, resource)
                        )
                        session.commit()
                    progress["documents_created"] += result["documents_created"]
                    progress["versions_created"] += result["versions_created"]
                    progress["pages"].append(
                        {
                            "url": resource.final_url,
                            "fetched_at": resource.fetched_at.isoformat(),
                            "sha256": resource.sha256,
                            "items": len(candidates),
                            "dates": [c.published_at.date().isoformat() for c in candidates],
                            "skipped": skipped,
                            "result": result,
                        }
                    )
                    progress.update(
                        next=next_url, cursor=cursor, status="partial" if next_url else "listing_exhausted"
                    )
                    if source["profile"] != "crossref_history" and not next_url:
                        progress["coverage_note"] = (
                            "可发现目录已遍历；没有可验证下一页，不代表来源三年内容完整。"
                        )
                    progress.pop("error", None)
                    write_state(args.state, state)
                    print(
                        key,
                        "pages",
                        len(progress["pages"]),
                        "new",
                        result["documents_created"],
                        progress["status"],
                        flush=True,
                    )
                    if not next_url:
                        break
                    time.sleep(2)
                except (ProbeError, ValueError, RuntimeError, SQLAlchemyError) as exc:
                    message = (
                        "database write failed; page not committed"
                        if isinstance(exc, SQLAlchemyError)
                        else str(exc)
                    )
                    progress.update(
                        status="failed",
                        error={
                            "code": getattr(exc, "code", type(exc).__name__),
                            "message": message,
                            "at": datetime.now(UTC).isoformat(),
                        },
                    )
                    write_state(args.state, state)
                    print(key, "failed", message, flush=True)
                    break
        write_state(args.state, state)
        print(
            json.dumps(
                {
                    "checkpoint": str(args.state),
                    "sources": {
                        k: {
                            field: v.get(field)
                            for field in ("status", "documents_created", "versions_created", "error")
                        }
                        for k, v in state["sources"].items()
                    },
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
