from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class PartnerApiInfo(BaseModel):
    name: str
    version: str
    documentation_path: str
    authentication_header: str
    publication_policy: str


class PartnerSummaryRead(BaseModel):
    document_release_id: int
    document_manifest_sha256: str
    document_published_at: datetime
    documents: int
    current_abstracts: int
    structured_datasets: int
    structured_observations: int
    total_items: int


class PartnerDocumentRead(BaseModel):
    document_id: int
    document_version_id: int
    version_no: int
    content_sha256: str
    source_id: int
    source_name: str
    organization_name: str
    source_type: str
    document_type: str
    title: str
    authors: list[str]
    language: str
    published_at: datetime | None
    issue_date_text: str | None
    source_updated_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    canonical_url: str | None
    discovery_url: str | None
    external_id: str | None
    doi: str | None
    abstract: str | None
    abstract_status: str | None
    abstract_provenance: dict[str, Any] | None
    published_at_provenance: dict[str, Any] | None
    extractor_name: str
    extractor_version: str
    extracted_at: datetime


class PartnerDocumentPage(BaseModel):
    release_id: int
    manifest_sha256: str
    total: int
    limit: int
    offset: int
    next_offset: int | None
    items: list[PartnerDocumentRead]


class PartnerDatasetRead(BaseModel):
    dataset_key: str
    name: str
    frequency: str
    scope_id: str
    published_snapshot_id: int
    snapshot_hash: str
    provider_version: str
    retrieved_at: datetime
    published_at: datetime
    expected_count: int
    returned_count: int
    valued_count: int
    source_null_count: int
    not_returned_count: int
    notes: str


class PartnerDatasetDimensions(BaseModel):
    dataset_key: str
    snapshot_id: int
    countries: list[str]
    partners: list[str]
    indicators: list[str]
    commodity_classifications: list[str]
    commodity_codes: list[str]
    trade_flows: list[str]
    metrics: list[str]
    periods: list[int]
    units: list[str]


class PartnerObservationRead(BaseModel):
    observation_key: str
    country_iso3: str
    partner_iso3: str | None
    indicator_code: str | None
    commodity_classification: str | None
    source_dataset_code: str | None
    commodity_code: str | None
    trade_flow: str | None
    frequency: str
    period: int
    metric_code: str
    value: Decimal | None
    unit: str | None
    currency: str | None
    price_basis: str | None
    source_url: str
    source_status: str
    missing_reason: str | None
    is_reported: bool | None
    is_aggregate: bool | None
    quality_flags: dict[str, Any]


class PartnerObservationPage(BaseModel):
    dataset_key: str
    snapshot_id: int
    total: int
    limit: int
    offset: int
    next_offset: int | None
    items: list[PartnerObservationRead]
