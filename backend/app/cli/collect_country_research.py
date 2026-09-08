"""Maintain COD research metadata in bounded, resumable year partitions."""

import argparse
import fcntl
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models import CollectionRun
from app.services.country_research_collection import (
    collect_partition,
    is_due,
    load_registry,
    register_channels,
    utc,
)
from app.services.source_probe import SafeHttpClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["due", "backfill", "updates"], default="due")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--year", type=int, action="append")
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 20:
        parser.error("page budget must be 1–20 per year")
    url = make_url(get_settings().database_url)
    if url.host not in {"127.0.0.1", "localhost"} or url.get_backend_name() != "postgresql":
        raise RuntimeError("Research collection requires the local PostgreSQL primary")
    root = Path(__file__).resolve().parents[3]
    lock_path = root / "var/locks/country-research.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running"}))
            return 75
        now = datetime.now(UTC)
        outcomes = []
        with get_session_factory()() as session:
            channels = register_channels(session, now.date())
            session.commit()
            for channel in channels:
                latest = session.scalar(
                    select(CollectionRun)
                    .where(CollectionRun.channel_id == channel.id)
                    .order_by(CollectionRun.id.desc())
                    .limit(1)
                )
                if (
                    latest
                    and latest.error_code == "http_429"
                    and channel.next_run_at
                    and utc(channel.next_run_at) > now
                ):
                    print(json.dumps({"status": "rate_limited", "retry_at": str(channel.next_run_at)}))
                    return 1
            client, registry = SafeHttpClient(), load_registry()
            for channel in channels:
                key = channel.collector_config["partition"]
                if args.mode == "updates" and key != "updates":
                    continue
                if args.mode == "backfill" and key == "updates":
                    continue
                if args.year and key not in {str(y) for y in args.year}:
                    continue
                # Explicit backfill resumes incomplete years; never rerun completed ones.
                if (channel.cursor_state or {}).get("complete") and key != "updates":
                    continue
                if args.mode == "due" and not is_due(channel, now):
                    continue
                if channel.status != "shadow":
                    continue
                outcome = collect_partition(
                    session,
                    channel,
                    client=client,
                    now=now,
                    max_pages=args.max_pages,
                    registry=registry,
                    pause=lambda: time.sleep(2),
                )
                outcomes.append(outcome)
                print(json.dumps(outcome, ensure_ascii=False), flush=True)
                if outcome["error_code"] == "http_429":
                    break
        report_dir = root / "var/collection-runs"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / (now.strftime("%Y%m%dT%H%M%SZ") + "-country-research.json")
        report.write_text(json.dumps({"mode": args.mode, "outcomes": outcomes}, ensure_ascii=False, indent=2))
        print(json.dumps({"report": str(report), "runs": len(outcomes)}))
        return int(any(o["status"] == "failed" for o in outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
