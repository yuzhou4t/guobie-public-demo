from __future__ import annotations

import sys
from datetime import datetime

from app.db.session import get_session_factory
from app.services.collection_schedule import (
    BEIJING,
    due_collection_groups,
    recover_stale_collection_runs,
)
from app.services.stable_collection_scope import load_stable_collection_scope


def main() -> int:
    scope = load_stable_collection_scope()
    group_rows = {
        "metadata-daily": scope.metadata_excel_rows_for_cadence("daily"),
        "metadata-weekly": scope.metadata_excel_rows_for_cadence("weekly"),
        "metadata-monthly": scope.metadata_excel_rows_for_cadence("monthly"),
        "structured": scope.structured_excel_rows,
    }
    with get_session_factory()() as session:
        now = datetime.now(BEIJING)
        recovered = recover_stale_collection_runs(session, now=now)
        session.commit()
        groups = due_collection_groups(session, group_rows, now=now)
    if recovered:
        print(
            f"recovered stale collection runs: {','.join(str(run_id) for run_id in recovered)}",
            file=sys.stderr,
        )
    print("\n".join(groups))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
