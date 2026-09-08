from __future__ import annotations

import json
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError

from app.cli.collect_catalog import _catalog_collection_lock
from app.db.session import get_advisory_lock_engine, get_engine, get_session_factory
from app.services.source_dates import SourceDateBackfillReport, backfill_source_dates

_BATCH_SIZE = 250
_MAX_BATCH_ATTEMPTS = 3


def _run_backfill_batches() -> tuple[SourceDateBackfillReport, int]:
    seen_at = datetime.now(UTC)
    after_document_id: int | None = None
    scanned = 0
    updated = 0
    versions_created = 0
    projections_repaired = 0
    precision_counts: Counter[str] = Counter()
    batches = 0

    while True:
        for attempt in range(1, _MAX_BATCH_ATTEMPTS + 1):
            try:
                with get_session_factory()() as session:
                    report = backfill_source_dates(
                        session,
                        seen_at=seen_at,
                        after_document_id=after_document_id,
                        limit=_BATCH_SIZE,
                    )
                    session.commit()
                break
            except DBAPIError:
                get_engine().dispose()
                if attempt == _MAX_BATCH_ATTEMPTS:
                    raise
                time.sleep(attempt * 2)

        if report.scanned == 0:
            break
        batches += 1
        scanned += report.scanned
        updated += report.updated
        versions_created += report.versions_created
        projections_repaired += report.projections_repaired
        precision_counts.update(report.precision_counts)
        after_document_id = report.last_document_id
        if report.scanned < _BATCH_SIZE:
            break

    return (
        SourceDateBackfillReport(
            scanned=scanned,
            updated=updated,
            versions_created=versions_created,
            projections_repaired=projections_repaired,
            precision_counts=dict(sorted(precision_counts.items())),
            last_document_id=after_document_id,
        ),
        batches,
    )


def main() -> int:
    with _catalog_collection_lock(get_advisory_lock_engine()) as lock_acquired:
        if not lock_acquired:
            print(
                json.dumps(
                    {
                        "error": "collection_already_running",
                        "message": "another catalog collection or backfill holds the database lock",
                        "exit_code": 75,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 75
        report, batches = _run_backfill_batches()
    print(json.dumps({**asdict(report), "batches": batches, "exit_code": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
