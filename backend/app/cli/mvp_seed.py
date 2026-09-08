from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db.session import get_session_factory
from app.services.mvp_seed import export_cod_structured_seed, import_cod_structured_seed


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Export or import the offline COD MVP seed package.")
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("--path", type=Path, default=root / "data" / "mvp_seed_v2.json")
    args = parser.parse_args()
    with get_session_factory()() as session:
        if args.action == "export":
            result = export_cod_structured_seed(session, args.path)
        else:
            result = import_cod_structured_seed(session, args.path)
    print(json.dumps({"status": "ok", "path": str(args.path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
