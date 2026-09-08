"""Collect the approved eight COD indicators on demand; no scheduler or raw assets."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.collectors.cod_basics_scope import COD_BASICS_INDICATORS, COD_BASICS_KEY
from app.collectors.world_bank import WorldBankQuerySpec, parse_world_bank_indicators
from app.db.session import get_session_factory
from app.models import CollectionRun, SourceChannel, StructuredDataset, StructuredSnapshot
from app.services.robots_policy import check_robots
from app.services.source_probe import SafeHttpClient
from app.services.structured_data_store import StructuredCollectionPayload, persist_structured_collection
from app.services.structured_scope import (
    WorldBankIndicator,
    build_world_bank_request_url,
    load_structured_scope,
)


def supplement_scope():
    root = Path(__file__).resolve().parents[3]
    original = load_structured_scope(root / "data/structured_collection_scope.json")
    indicators = tuple(
        WorldBankIndicator(
            code=row[0],
            metric_code=row[1],
            unit=row[2],
            name_zh=row[3],
            currency="USD" if row[0] == "NY.GDP.PCAP.CD" else None,
            price_basis="current" if row[0] == "NY.GDP.PCAP.CD" else None,
        )
        for row in COD_BASICS_INDICATORS
    )
    scope = replace(
        original.world_bank,
        dataset_key=COD_BASICS_KEY,
        name="刚果（金）基础国情补充 · World Bank WDI",
        countries=("COD",),
        start_year=2015,
        end_year=2025,
        indicators=indicators,
        logical_dimension_cells=88,
    )
    digest = hashlib.sha256(json.dumps(COD_BASICS_INDICATORS, ensure_ascii=False).encode()).hexdigest()
    return replace(original, scope_id=COD_BASICS_KEY, scope_sha256=digest, world_bank=scope)


def fetch_indicator(scope, indicator):
    client = SafeHttpClient()
    url = build_world_bank_request_url(scope.world_bank, indicator.code)
    resource = client.fetch(url)
    parsed = parse_world_bank_indicators(
        resource,
        WorldBankQuerySpec(
            indicator_code=indicator.code,
            metric_code=indicator.metric_code,
            unit=indicator.unit,
            currency=indicator.currency,
            price_basis=indicator.price_basis,
            countries=("COD",),
            years=tuple(range(scope.world_bank.start_year, scope.world_bank.end_year + 1)),
            scope_id=scope.world_bank.dataset_key,
        ),
    )
    evidence = {
        "indicator_code": indicator.code,
        "request_url": url,
        "final_url": resource.final_url,
        "http_status": resource.status_code,
        "resource_sha256": resource.sha256,
        "fetched_at": resource.fetched_at.isoformat(),
        "last_updated": parsed.last_updated.isoformat(),
        "returned_row_count": parsed.returned_row_count,
    }
    return parsed, evidence, resource.fetched_at


def collect(scope=None):
    scope = scope or supplement_scope()
    entry = build_world_bank_request_url(scope.world_bank, scope.world_bank.indicators[0].code)
    robots = check_robots(SafeHttpClient(), entry)
    if not robots.allowed:
        raise RuntimeError(f"World Bank robots check: {robots.code}")
    # Fetch/parse the full bounded grid before beginning the database transaction.
    with ThreadPoolExecutor(max_workers=3) as pool:
        fetched = list(pool.map(lambda item: fetch_indicator(scope, item), scope.world_bank.indicators))
    with get_session_factory()() as session:
        original = session.scalar(
            select(StructuredDataset).where(StructuredDataset.dataset_key == "world-bank-indicators-v2")
        )
        if original is None:
            raise RuntimeError("Original World Bank source must already be registered")
        source_channel = session.get(SourceChannel, original.channel_id)
        session.execute(select(SourceChannel).where(SourceChannel.id == source_channel.id).with_for_update())
        channel = session.scalar(
            select(SourceChannel).where(
                SourceChannel.source_id == source_channel.source_id, SourceChannel.entry_url == entry
            )
        )
        if channel is None:
            channel = SourceChannel(
                source_id=source_channel.source_id,
                name=scope.world_bank.name,
                entry_url=entry,
                collector_type="api",
                link_role="official",
                status="shadow",
                collector_config={
                    "profile": "cod_basics_on_demand",
                    "scope_id": scope.world_bank.dataset_key,
                },
            )
            session.add(channel)
            session.flush()
        if channel.status != "shadow":
            raise RuntimeError("COD supplement requires a shadow channel")
        now = datetime.now(UTC)
        run = CollectionRun(channel_id=channel.id, trigger_kind="manual", status="running", started_at=now)
        session.add(run)
        session.flush()
        stored = persist_structured_collection(
            session,
            StructuredCollectionPayload(
                scope=scope,
                channel_id=channel.id,
                collection_run_id=run.id,
                retrieved_at=max(item[2] for item in fetched),
                query_manifest={
                    "scope_id": scope.scope_id,
                    "scope_sha256": scope.scope_sha256,
                    "queries": [item[1] for item in fetched],
                },
                query_urls=tuple(item[1]["request_url"] for item in fetched),
                parse_results=tuple(item[0] for item in fetched),
            ),
        )
        snapshot = session.get(StructuredSnapshot, stored.snapshot_id)
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        run.items_discovered = scope.world_bank.logical_dimension_cells
        run.items_persisted = stored.versions_created
        run.report = {
            "scope_id": scope.world_bank.dataset_key,
            "snapshot_id": snapshot.id,
            "raw_content_stored": False,
            "snapshot_created": stored.snapshot_created,
        }
        session.commit()
        print(
            json.dumps(
                {
                    **run.report,
                    "dataset_id": stored.dataset_id,
                    "observations": scope.world_bank.logical_dimension_cells,
                    "valued": snapshot.valued_count,
                    "source_null": snapshot.source_null_count,
                    "not_returned": snapshot.not_returned_count,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    collect()
