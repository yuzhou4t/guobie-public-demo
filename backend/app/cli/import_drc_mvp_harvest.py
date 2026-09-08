from __future__ import annotations

import json
from pathlib import Path

from app.db.session import get_session_factory
from app.services.drc_mvp_import import import_drc_mvp_harvest


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    with get_session_factory()() as session:
        result = import_drc_mvp_harvest(
            session,
            manifest_path=root / "data" / "drc_mvp_sources.json",
            harvest_path=root / "data" / "drc_mvp_harvest.json",
        )
    print(json.dumps({"status": "ok", **result.to_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
