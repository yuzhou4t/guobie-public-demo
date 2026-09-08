from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.db.session import get_session_factory
from app.services.research_evidence_import import (
    ResearchEvidenceImportError,
    import_research_evidence_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import reviewed research evidence rows that resolve to exact document versions."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with get_session_factory()() as session:
        try:
            result = import_research_evidence_csv(
                session,
                args.input,
                owner_email=args.owner_email,
                reviewer=args.reviewer,
                dry_run=args.dry_run,
            )
        except ResearchEvidenceImportError as exc:
            session.rollback()
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    print(json.dumps({"status": "ok", **result.to_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
