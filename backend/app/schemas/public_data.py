from datetime import datetime

from pydantic import BaseModel


class PublicSummaryRead(BaseModel):
    sources: int
    current_channels: int
    documents: int
    current_abstracts: int
    structured_datasets: int
    structured_observations: int
    latest_collection_finished_at: datetime | None


class PublicDocumentRead(BaseModel):
    id: int
    source_id: int
    source_name: str
    document_type: str
    title: str
    language: str
    published_at: datetime | None
    issue_date_text: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    latest_version_no: int
    canonical_url: str | None
    discovery_url: str | None
    doi: str | None
    abstract: str | None
    abstract_status: str | None


class PublicDocumentPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[PublicDocumentRead]


class PublicCollectionRunRead(BaseModel):
    id: int
    source_id: int
    source_name: str
    channel_id: int
    channel_name: str
    trigger_kind: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    items_discovered: int
    items_persisted: int
    error_category: str | None
    error_code: str | None
    retry_count: int
