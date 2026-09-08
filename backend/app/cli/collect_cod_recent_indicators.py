"""Collect the five registered core indicators for COD in 2025 only."""

import hashlib
from dataclasses import replace
from pathlib import Path

from app.cli.collect_cod_basics import collect
from app.collectors.cod_basics_scope import COD_RECENT_KEY
from app.db.session import get_engine
from app.services.structured_scope import load_structured_scope


def recent_scope():
    root = Path(__file__).resolve().parents[3]
    original = load_structured_scope(root / "data/structured_collection_scope.json")
    scope = replace(
        original.world_bank,
        dataset_key=COD_RECENT_KEY,
        name="刚果（金）核心指标 · World Bank 2025 年补充",
        countries=("COD",),
        start_year=2025,
        end_year=2025,
        logical_dimension_cells=5,
    )
    return replace(
        original,
        world_bank=scope,
        scope_id=COD_RECENT_KEY,
        scope_sha256=hashlib.sha256(repr(scope).encode()).hexdigest(),
    )


if __name__ == "__main__":
    if get_engine().url.host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise ValueError("Recent-year supplementation is limited to the local database")
    collect(recent_scope())
