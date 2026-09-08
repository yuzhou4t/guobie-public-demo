from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class StructuredDatasetRead(BaseModel):
    id: int
    channel_id: int
    dataset_key: str
    name: str
    scope_id: str
    frequency: str
    channel_status: str
    latest_snapshot_id: int | None
    created_at: datetime
    updated_at: datetime


class StructuredSnapshotRead(BaseModel):
    id: int
    dataset_id: int
    collection_run_id: int
    query_manifest: dict[str, Any]
    query_signature: str
    snapshot_hash: str
    provider_version: str
    source_metadata: dict[str, Any]
    expected_count: int
    returned_count: int
    valued_count: int
    source_null_count: int
    not_returned_count: int
    retrieved_at: datetime


class StructuredObservationRead(BaseModel):
    id: int
    observation_key: str
    snapshot_id: int
    observation_version_id: int
    version_no: int
    country_iso3: str
    partner_iso3: str | None
    indicator_code: str | None
    commodity_classification: str | None
    source_dataset_code: str | None
    commodity_code: str | None
    trade_flow: str | None
    partner2_code: str | None
    customs_code: str | None
    mot_code: str | None
    frequency: str
    period: int
    metric_code: str
    value: Decimal | None
    unit: str | None
    currency: str | None
    price_basis: str | None
    source_url: str
    source_release_date: date | None
    source_status: str
    missing_reason: str | None
    is_reported: bool | None
    is_aggregate: bool | None
    quality_flags: dict[str, Any]


class StructuredObservationPage(BaseModel):
    dataset_id: int
    snapshot_id: int
    limit: int
    offset: int
    items: list[StructuredObservationRead]
