from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.db.session import get_session_factory
from app.services.research_capabilities import (
    ResearchCapabilityError,
    prepare_mvp_demo,
    run_capability,
    seed_capability_templates,
)


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Seed or run declarative research capabilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed")
    seed.add_argument(
        "--templates",
        type=Path,
        default=root / "data" / "research_capability_templates.json",
    )
    seed.add_argument("--owner-email")
    prepare = subparsers.add_parser("prepare-mvp")
    prepare.add_argument(
        "--templates",
        type=Path,
        default=root / "data" / "research_capability_templates.json",
    )
    run = subparsers.add_parser("run")
    run.add_argument("--config-id", type=int, required=True)
    run.add_argument("--research-case-id", type=int)
    args = parser.parse_args()

    with get_session_factory()() as session:
        try:
            if args.command == "seed":
                payload = seed_capability_templates(session, args.templates, owner_email=args.owner_email)
            elif args.command == "prepare-mvp":
                payload = prepare_mvp_demo(session, args.templates)
            else:
                capability_run = run_capability(
                    session,
                    config_id=args.config_id,
                    research_case_id=args.research_case_id,
                )
                payload = {"run_id": capability_run.id, "status": capability_run.status}
        except ResearchCapabilityError as exc:
            session.rollback()
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    print(json.dumps({"status": "ok", **payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
