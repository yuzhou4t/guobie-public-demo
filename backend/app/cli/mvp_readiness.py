from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastapi.encoders import jsonable_encoder

from app.api.research import get_country_catalog, get_country_readiness, get_reader_bootstrap
from app.db.session import get_session_factory


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Generate the evidence-backed MVP readiness report.")
    parser.add_argument("--output", type=Path, default=root / "var" / "mvp-readiness.json")
    args = parser.parse_args()

    with get_session_factory()() as session:
        payload = {
            "bootstrap": get_reader_bootstrap(session),
            "catalog": get_country_catalog(session)["summary"],
            "country": get_country_readiness("COD", session),
        }
    encoded = jsonable_encoder(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(encoded, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "ok", "output": str(args.output)}, ensure_ascii=False))
    return 0 if encoded["bootstrap"]["demo_seeded"] and encoded["country"]["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
