"""Fetch only the registered COD papers; never save raw HTTP responses or full text."""

import argparse
import json
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from app.collectors.base import CandidateDocument, parse_datetime
from app.collectors.cod_research import parse_cod_paper
from app.db.session import get_session_factory
from app.services.cod_research_store import store_cod_papers
from app.services.robots_policy import check_robots
from app.services.source_probe import ProbeError, SafeHttpClient

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = ROOT / "data/cod_research_papers.json"
HARVEST = ROOT / "data/cod_research_harvest.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--import-harvest", action="store_true")
    args = parser.parse_args()
    registry = json.loads(REGISTRY.read_text())
    start, end = date.fromisoformat(registry["start_date"]), date.fromisoformat(registry["end_date"])
    if registry["country_iso3"] != "COD" or start != date(2022, 1, 1) or end > date.today():
        raise ValueError("Invalid research scope")
    candidates, failures = [], []
    if args.import_harvest:
        payload = json.loads(HARVEST.read_text())
        if payload["scope_id"] != registry["scope_id"]:
            raise ValueError("Harvest scope mismatch")
        allowed = {entry["doi"] for entry in registry["papers"]}
        for row in payload["papers"]:
            row["published_at"] = parse_datetime(row["published_at"])
            if row["doi"] not in allowed or not start <= row["published_at"].date() <= end:
                raise ValueError("Unregistered paper")
            candidates.append(CandidateDocument(**row))
        failures = payload["failures"]
    else:
        client = SafeHttpClient()
        if not check_robots(client, "https://api.crossref.org/works").allowed:
            raise RuntimeError("Crossref robots does not allow this collection")
        for entry in registry["papers"]:
            url = "https://api.crossref.org/works/" + entry["doi"]
            try:
                resource = client.fetch(url)
                candidate = parse_cod_paper(resource, entry, start=start, end=end)
                candidates.append(candidate)
                print(f"Verified {candidate.doi}", flush=True)
            except (ValueError, RuntimeError, ProbeError) as error:
                failures.append({"doi": entry["doi"], "reason": str(error)})
                print(f"Failed {entry['doi']}: {error}", flush=True)
            time.sleep(3)
        HARVEST.write_text(
            json.dumps(
                {
                    "scope_id": registry["scope_id"],
                    "papers": [asdict(row) for row in candidates],
                    "failures": failures,
                    "raw_content_stored": False,
                },
                ensure_ascii=False,
                default=str,
                indent=2,
            )
            + "\n"
        )
    if len({row.doi for row in candidates}) != len(candidates):
        raise ValueError("Duplicate DOI in harvest")
    if not args.fetch_only:
        with get_session_factory()() as session:
            result = store_cod_papers(session, candidates)
            session.commit()
            print(json.dumps({**result, "failures": failures}, ensure_ascii=False))
    else:
        print(json.dumps({"verified": len(candidates), "failures": failures}, ensure_ascii=False))


if __name__ == "__main__":
    main()
