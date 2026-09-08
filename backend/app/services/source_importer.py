from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Source, SourceChannel, SourceChannelPolicy
from app.services.collection_plan import CollectionPlan, PlannedChannelPolicy, PlannedSource
from app.services.source_catalog import CatalogSource, SourceCatalog


@dataclass(frozen=True)
class ImportResult:
    sources_upserted: int
    channels_upserted: int
    policies_upserted: int
    channel_ids: tuple[int, ...]


def import_catalog(session: Session, catalog: SourceCatalog, plan: CollectionPlan) -> ImportResult:
    """Upsert the reviewed catalog and plan without deleting unrelated database rows."""

    if catalog.source_file != plan.source_file:
        raise ValueError("catalog and collection plan refer to different source files")
    catalog_by_row = {source.excel_row: source for source in catalog.sources}
    plan_by_row = {source.excel_row: source for source in plan.sources}
    if set(catalog_by_row) != set(plan_by_row):
        raise ValueError("catalog and collection plan excel rows do not match")

    sources = list(session.scalars(select(Source)))
    channels = list(session.scalars(select(SourceChannel).options(selectinload(SourceChannel.policy))))
    sources_by_identity = {(source.name, source.organization_name): source for source in sources}
    channels_by_source_id: dict[int, list[SourceChannel]] = defaultdict(list)
    channels_by_identity: dict[tuple[int, str], SourceChannel] = {}
    source_ids_by_catalog_row: dict[int, set[int]] = defaultdict(set)
    for channel in channels:
        channels_by_source_id[channel.source_id].append(channel)
        channels_by_identity[(channel.source_id, channel.entry_url)] = channel
        excel_row = _catalog_row(channel)
        if excel_row is not None:
            source_ids_by_catalog_row[excel_row].add(channel.source_id)
    sources_by_catalog_row: dict[int, Source] = {}
    sources_by_id = {source.id: source for source in sources}
    for excel_row, source_ids in source_ids_by_catalog_row.items():
        if len(source_ids) > 1:
            raise ValueError(f"catalog row {excel_row} is already linked to multiple sources")
        sources_by_catalog_row[excel_row] = sources_by_id[next(iter(source_ids))]

    channel_count = 0
    policy_count = 0
    channel_ids: list[int] = []
    for excel_row in sorted(catalog_by_row):
        catalog_source = catalog_by_row[excel_row]
        planned_source = plan_by_row[excel_row]
        source, source_created = _upsert_source(
            session,
            catalog_source,
            planned_source,
            sources_by_catalog_row=sources_by_catalog_row,
            sources_by_identity=sources_by_identity,
        )
        session.flush()
        sources_by_catalog_row[excel_row] = source
        prior_catalog_channels = (
            []
            if source_created
            else [
                channel
                for channel in channels_by_source_id[source.id]
                if _catalog_row(channel) == excel_row and channel.status != "retired"
            ]
        )
        was_plan_blocked = bool(prior_catalog_channels) and all(
            channel.status == "blocked" and channel.collector_config.get("automation_status") == "blocked"
            for channel in prior_catalog_channels
        )

        planned_database_channels: list[SourceChannel] = []
        for planned_channel in planned_source.channels:
            config = {
                **planned_channel.collector_config,
                "catalog_excel_row": excel_row,
                "automation_status": planned_source.automation_status,
                "reason": planned_source.reason,
                "priority": catalog_source.priority,
                "source_catalog_sha256": catalog.workbook_sha256,
            }
            channel = channels_by_identity.get((source.id, planned_channel.entry_url))
            if channel is None:
                channel = SourceChannel(
                    source_id=source.id,
                    name=planned_channel.name,
                    entry_url=planned_channel.entry_url,
                    collector_type=planned_channel.collector_type,
                    collector_config=config,
                    adapter_version="v1",
                    link_role=planned_channel.link_role,
                    status=planned_channel.status,
                )
                session.add(channel)
                channels.append(channel)
                channels_by_source_id[source.id].append(channel)
                channels_by_identity[(source.id, planned_channel.entry_url)] = channel
            else:
                channel_was_plan_blocked = (
                    channel.status == "blocked"
                    and channel.collector_config.get("automation_status") == "blocked"
                )
                channel.name = planned_channel.name
                channel.collector_type = planned_channel.collector_type
                channel.collector_config = config
                channel.adapter_version = "v1"
                channel.link_role = planned_channel.link_role
                if planned_channel.status == "blocked":
                    channel.status = planned_channel.status
                elif channel_was_plan_blocked:
                    channel.status = planned_channel.status
                elif channel.status not in {"active", "paused", "blocked"}:
                    channel.status = planned_channel.status

            policy = channel.policy
            if policy is None:
                policy = SourceChannelPolicy(
                    channel=channel,
                    robots_state="unknown",
                    terms_state="unknown",
                    storage_scope=plan.policy_defaults.storage_scope,
                    rag_scope=plan.policy_defaults.rag_scope,
                    notes=planned_source.reason,
                )
                session.add(policy)
            if planned_channel.policy is not None:
                _apply_reviewed_channel_policy(policy, planned_channel.policy)
            elif policy.storage_scope == "official_abstract":
                policy.terms_state = "unknown"
                policy.terms_url = None
                policy.storage_scope = plan.policy_defaults.storage_scope
                policy.rag_scope = plan.policy_defaults.rag_scope
                policy.reviewed_at = None
                policy.notes = planned_source.reason
            planned_database_channels.append(channel)
            channel_count += 1
            policy_count += 1
        computed_status = (
            "active"
            if any(channel.status in {"shadow", "active"} for channel in planned_database_channels)
            else "paused"
        )
        reviewed_plan_unblocked = (
            source.status == "paused" and was_plan_blocked and planned_source.automation_status != "blocked"
        )
        if source_created or source.status == "active" or reviewed_plan_unblocked:
            source.status = computed_status
        session.flush()
        channel_ids.extend(channel.id for channel in planned_database_channels)

    current_channel_ids = set(channel_ids)
    for channel in channels:
        if _catalog_row(channel) is not None and channel.id not in current_channel_ids:
            channel.status = "retired"

    session.flush()
    return ImportResult(
        sources_upserted=len(catalog_by_row),
        channels_upserted=channel_count,
        policies_upserted=policy_count,
        channel_ids=tuple(channel_ids),
    )


def _apply_reviewed_channel_policy(
    policy: SourceChannelPolicy,
    planned: PlannedChannelPolicy,
) -> None:
    reviewed_at = datetime.fromisoformat(planned.reviewed_at)
    current_reviewed_at = policy.reviewed_at
    if current_reviewed_at is not None and current_reviewed_at.tzinfo is None:
        current_reviewed_at = current_reviewed_at.replace(tzinfo=UTC)
    if current_reviewed_at is not None and current_reviewed_at > reviewed_at:
        return
    policy.terms_state = planned.terms_state
    policy.terms_url = planned.terms_url
    policy.storage_scope = planned.storage_scope
    policy.rag_scope = planned.rag_scope
    policy.reviewed_at = reviewed_at
    policy.notes = planned.notes


def _upsert_source(
    session: Session,
    catalog_source: CatalogSource,
    planned_source: PlannedSource,
    *,
    sources_by_catalog_row: dict[int, Source],
    sources_by_identity: dict[tuple[str, str], Source],
) -> tuple[Source, bool]:
    fields = catalog_source.fields
    catalog_name = _text(fields["信源名称"])
    name = planned_source.source_name_override or catalog_name
    organization_name = _text(fields["所属机构"])
    source = sources_by_catalog_row.get(planned_source.excel_row)
    if source is None:
        source = sources_by_identity.get((name, organization_name))
    if source is None and name != catalog_name:
        source = sources_by_identity.get((catalog_name, organization_name))
    if source is None:
        source_created = True
        source = Source(name=name, organization_name=organization_name, source_type="")
        session.add(source)
    else:
        source_created = False

    previous_identity = (source.name, source.organization_name)
    source.name = name
    source.organization_name = organization_name
    source.source_type = _text(fields["一级类型"])
    source.country_or_region = _text(fields["国家/地区"])
    source.primary_language = _text(fields["主要语言"])
    source.authority_level = _text(fields["权威性等级"], default="unrated")
    source.homepage_url = planned_source.official_homepage_url or _safe_homepage(
        fields["官网URL"], planned_source
    )
    if sources_by_identity.get(previous_identity) is source:
        sources_by_identity.pop(previous_identity)
    sources_by_identity[(source.name, source.organization_name)] = source
    return source, source_created


def _safe_homepage(value: object, planned_source: PlannedSource) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    homepage = value.strip()
    try:
        host = urlsplit(homepage).hostname
    except ValueError:
        return None
    if host is None:
        return None
    official_hosts = {
        urlsplit(channel.entry_url).hostname
        for channel in planned_source.channels
        if channel.link_role == "official"
    }
    return homepage if host.lower() in {item.lower() for item in official_hosts if item} else None


def _text(value: object, *, default: str = "") -> str:
    if value is None:
        return default
    cleaned = str(value).strip()
    return cleaned or default


def _catalog_row(channel: SourceChannel) -> int | None:
    value = channel.collector_config.get("catalog_excel_row")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
