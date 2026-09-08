from __future__ import annotations

import json
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db.session import get_engine

REQUIRED_RUNTIME_TABLES = frozenset(
    {
        "entities",
        "document_entities",
        "collection_runs",
        "research_cases",
        "capability_templates",
    }
)


def schema_readiness() -> dict[str, object]:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())
    engine = get_engine()
    with engine.connect() as connection:
        current_heads = set(MigrationContext.configure(connection).get_current_heads())
        tables = set(inspect(connection).get_table_names())
    missing_tables = sorted(REQUIRED_RUNTIME_TABLES - tables)
    return {
        "ready": current_heads == expected_heads and not missing_tables,
        "current_heads": sorted(current_heads),
        "expected_heads": sorted(expected_heads),
        "missing_tables": missing_tables,
    }


def main() -> int:
    try:
        payload = schema_readiness()
    except Exception as exc:  # pragma: no cover - exercised by the operational shell
        payload = {
            "ready": False,
            "error": type(exc).__name__,
            "message": "database schema readiness check could not connect or inspect the database",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 69
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ready"] else 78


if __name__ == "__main__":
    raise SystemExit(main())
