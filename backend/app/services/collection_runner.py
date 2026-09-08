from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import sleep
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.collectors import (
    CandidateDocument,
    ComtradeAvailabilityParseError,
    ComtradeAvailabilityParseResult,
    ComtradeAvailabilityQuerySpec,
    ComtradeDataParseError,
    ComtradeDataParseResult,
    ComtradeDataQuerySpec,
    ComtradeMetadataParseError,
    ComtradeMetadataParseResult,
    ComtradeMetadataQuerySpec,
    Diagnostic,
    FetchedResource,
    FollowupRequest,
    OecdOdaParseError,
    ParseResult,
    UnctadFdiParseError,
    WitsTariffParseError,
    WorldBankParseError,
    WorldBankParseResult,
    WorldBankQuerySpec,
    build_wits_tariff_panel,
    build_wits_tariff_url,
    get_collector,
    parse_comtrade_availability,
    parse_comtrade_data,
    parse_comtrade_metadata,
    parse_oecd_oda,
    parse_unctad_fdi,
    parse_wits_tariff,
    parse_wits_tariff_availability,
    parse_world_bank_indicators,
)
from app.collectors.unctad_fdi import UNCTAD_FDI_BULK_URL
from app.collectors.wits_tariff import WITS_AVAILABILITY_URL
from app.models import CollectionItem, CollectionRun, SourceChannel, SourceChannelPolicy
from app.services.comtrade_data_store import (
    ComtradeCollectionPayload,
    ComtradeDataStoreError,
    persist_comtrade_collection,
)
from app.services.document_store import persist_candidate_metadata
from app.services.oecd_oda_store import (
    OECD_ODA_DATASET_KEY,
    OECD_ODA_SCOPE_ID,
    OECD_ODA_URL,
    OecdOdaStoreError,
    persist_oecd_oda,
)
from app.services.robots_policy import RobotsDecision, check_robots
from app.services.source_dates import source_date_from_url
from app.services.source_probe import (
    ProbeError,
    SafeHttpClient,
    classify_content,
    normalize_http_url,
)
from app.services.structured_data_store import (
    StructuredCollectionPayload,
    StructuredDataStoreError,
    persist_structured_collection,
)
from app.services.structured_scope import (
    StructuredScope,
    StructuredScopeValidationError,
    build_comtrade_data_availability_request_url,
    build_comtrade_metadata_request_url,
    build_comtrade_request_url,
    build_world_bank_request_url,
    load_structured_scope,
)
from app.services.unctad_fdi_store import (
    UNCTAD_FDI_DATASET_KEY,
    UNCTAD_FDI_SCOPE_ID,
    UnctadFdiStoreError,
    persist_unctad_fdi,
)
from app.services.wits_tariff_store import (
    WITS_TARIFF_DATASET_KEY,
    WITS_TARIFF_SCOPE_ID,
    WitsTariffQueryEvidence,
    WitsTariffStoreError,
    persist_wits_tariff,
)

_MAX_DOCUMENT_PERSISTENCE_ATTEMPTS = 3
_DOCUMENT_PERSISTENCE_COMMIT_EVERY = 20


@dataclass(frozen=True, slots=True)
class ChannelOutcome:
    excel_row: int
    source_name: str
    channel_id: int
    channel_name: str
    run_id: int
    status: str
    error_category: str | None
    error_code: str | None
    items_discovered: int
    items_persisted: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "excel_row": self.excel_row,
            "source_name": self.source_name,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "run_id": self.run_id,
            "status": self.status,
            "error_category": self.error_category,
            "error_code": self.error_code,
            "items_discovered": self.items_discovered,
            "items_persisted": self.items_persisted,
        }


@dataclass(frozen=True, slots=True)
class BatchCollectionReport:
    started_at: datetime
    finished_at: datetime
    source_count: int
    channel_count: int
    source_status_counts: dict[str, int]
    channel_status_counts: dict[str, int]
    items_discovered: int
    items_persisted: int
    outcomes: tuple[ChannelOutcome, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "source_count": self.source_count,
            "channel_count": self.channel_count,
            "source_status_counts": self.source_status_counts,
            "channel_status_counts": self.channel_status_counts,
            "items_discovered": self.items_discovered,
            "items_persisted": self.items_persisted,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }


@dataclass(frozen=True, slots=True)
class _ComtradeRequest:
    kind: str
    reporter_iso3: str
    reporter_code: int
    period: int | None
    commodity_code: str | None
    url: str


def collect_catalog_batch(
    session: Session,
    *,
    channel_ids: tuple[int, ...],
    client: SafeHttpClient | None = None,
    trigger_kind: str = "manual",
) -> BatchCollectionReport:
    """Run every checked-in catalog channel once, including audited blocked outcomes."""

    started_at = datetime.now(UTC)
    _validate_trigger_kind(trigger_kind)
    http_client = client or SafeHttpClient()
    if not channel_ids or len(channel_ids) != len(set(channel_ids)):
        raise ValueError("channel_ids must be a non-empty set of unique imported channel IDs")
    loaded_channels = list(
        session.scalars(
            select(SourceChannel)
            .where(SourceChannel.id.in_(channel_ids))
            .options(
                joinedload(SourceChannel.source),
                selectinload(SourceChannel.policy),
            )
        )
    )
    channels_by_id = {channel.id: channel for channel in loaded_channels}
    channels: list[SourceChannel] = []
    for channel_id in channel_ids:
        channel = channels_by_id.get(channel_id)
        if channel is None or _catalog_row(channel) is None:
            raise ValueError(f"imported catalog channel {channel_id} does not exist")
        channels.append(channel)
    channels.sort(key=lambda channel: (_catalog_row(channel) or 0, channel.id))
    ordered_channel_ids = tuple(channel.id for channel in channels)

    outcomes: list[ChannelOutcome] = []
    for channel_id in ordered_channel_ids:
        channel = _get_channel(session, channel_id)
        if channel.collector_config.get("automation_status") == "blocked":
            outcomes.append(record_blocked_channel(session, channel_id, trigger_kind=trigger_kind))
            continue
        try:
            outcomes.append(
                collect_channel(
                    session,
                    channel_id,
                    client=http_client,
                    trigger_kind=trigger_kind,
                )
            )
        except Exception as exc:  # keep the formal batch auditable even after an internal defect
            try:
                session.rollback()
            except OperationalError:
                session.invalidate()
            outcomes.append(
                _record_internal_failure_with_reconnect(
                    session,
                    channel_id,
                    exc,
                    trigger_kind=trigger_kind,
                )
            )

    source_outcomes: dict[int, list[ChannelOutcome]] = defaultdict(list)
    for outcome in outcomes:
        source_outcomes[outcome.excel_row].append(outcome)
    source_statuses = Counter(_source_status(items) for items in source_outcomes.values())
    channel_statuses = Counter(_outcome_status(outcome) for outcome in outcomes)
    return BatchCollectionReport(
        started_at=started_at,
        finished_at=datetime.now(UTC),
        source_count=len(source_outcomes),
        channel_count=len(outcomes),
        source_status_counts=dict(sorted(source_statuses.items())),
        channel_status_counts=dict(sorted(channel_statuses.items())),
        items_discovered=sum(outcome.items_discovered for outcome in outcomes),
        items_persisted=sum(outcome.items_persisted for outcome in outcomes),
        outcomes=tuple(outcomes),
    )


def record_blocked_channel(
    session: Session,
    channel_id: int,
    *,
    trigger_kind: str = "manual",
) -> ChannelOutcome:
    _validate_trigger_kind(trigger_kind)
    channel = _get_channel(session, channel_id)
    reason = str(
        channel.collector_config.get("automation_reason")
        or channel.collector_config.get("reason")
        or "catalog review required"
    )
    return _record_policy_non_fetch(
        session,
        channel,
        code="catalog_blocked",
        reason=reason,
        report={
            "automation_status": channel.collector_config.get("automation_status", "blocked"),
            "decision": "not_fetched",
            "reason": reason,
        },
        trigger_kind=trigger_kind,
    )


def _record_inactive_channel(
    session: Session,
    channel: SourceChannel,
    *,
    source_inactive: bool = False,
    trigger_kind: str = "manual",
) -> ChannelOutcome:
    if source_inactive:
        code = "source_not_runnable"
        reason = f"source status {channel.source.status!r} does not permit automatic collection"
        report = {"source_status": channel.source.status, "decision": "not_fetched"}
    else:
        code = "channel_not_runnable"
        reason = f"channel status {channel.status!r} does not permit automatic collection"
        report = {"channel_status": channel.status, "decision": "not_fetched"}
    return _record_policy_non_fetch(
        session,
        channel,
        code=code,
        reason=reason,
        report=report,
        trigger_kind=trigger_kind,
    )


def _record_policy_non_fetch(
    session: Session,
    channel: SourceChannel,
    *,
    code: str,
    reason: str,
    report: dict[str, Any],
    trigger_kind: str,
) -> ChannelOutcome:
    now = datetime.now(UTC)
    run = CollectionRun(
        channel_id=channel.id,
        trigger_kind=trigger_kind,
        status="failed",
        started_at=now,
        heartbeat_at=now,
        finished_at=now,
        error_category="policy",
        error_code=code,
        error_message=reason,
        report=report,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def collect_channel(
    session: Session,
    channel_id: int,
    *,
    client: SafeHttpClient,
    trigger_kind: str = "manual",
) -> ChannelOutcome:
    _validate_trigger_kind(trigger_kind)
    channel = _get_channel(session, channel_id)
    if channel.collector_config.get("automation_status") == "blocked":
        return record_blocked_channel(session, channel.id, trigger_kind=trigger_kind)
    if channel.source.status != "active":
        return _record_inactive_channel(
            session,
            channel,
            source_inactive=True,
            trigger_kind=trigger_kind,
        )
    if channel.status not in {"shadow", "active"}:
        return _record_inactive_channel(session, channel, trigger_kind=trigger_kind)
    if _uses_comtrade_goods(channel):
        return collect_comtrade_channel(
            session,
            channel.id,
            client=client,
            trigger_kind=trigger_kind,
        )
    active_run = session.scalar(
        select(CollectionRun.id).where(
            CollectionRun.channel_id == channel.id,
            CollectionRun.status.in_(("queued", "running")),
        )
    )
    if active_run is not None:
        raise ValueError("channel already has an active collection run")

    now = datetime.now(UTC)
    run = CollectionRun(
        channel_id=channel.id,
        trigger_kind=trigger_kind,
        status="running",
        started_at=now,
        heartbeat_at=now,
        report={"channel_status_before": channel.status},
    )
    session.add(run)
    session.flush()

    policy = channel.policy
    if policy is None:
        policy = SourceChannelPolicy(channel_id=channel.id)
        session.add(policy)
        session.flush()
    if policy.robots_state == "disallowed":
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="robots_policy_disallowed",
            message="stored robots review disallows automatic collection",
        )
    if _terms_disallow_collection(policy.terms_state):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="terms_disallowed",
            message="reviewed source terms disallow automatic collection",
        )
    if policy.storage_scope == "none":
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="storage_scope_none",
            message="reviewed storage policy disallows metadata persistence",
        )
    if policy.storage_scope == "official_abstract" and (
        policy.terms_state != "allowed_with_conditions" or not policy.terms_url or policy.reviewed_at is None
    ):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="official_abstract_policy_incomplete",
            message="official abstract storage requires reviewed conditional terms and a terms URL",
        )
    if policy.storage_scope not in {"metadata", "official_abstract"} or policy.rag_scope != "none":
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="unsupported_storage_policy",
            message="collection supports metadata or reviewed official abstracts and no RAG",
        )

    # Do not keep a database transaction idle while external requests run.
    # The running audit row remains visible, while structured payloads are still
    # persisted later inside their dedicated atomic snapshot transaction.
    session.commit()

    if _uses_world_bank_indicators(channel):
        return _collect_world_bank_indicators(session, channel, run, policy, client)
    if _uses_oecd_oda(channel):
        return _collect_oecd_oda(session, channel, run, policy, client)
    if _uses_wits_tariff(channel):
        return _collect_wits_tariff(session, channel, run, policy, client)
    if _uses_unctad_fdi(channel):
        return _collect_unctad_fdi(session, channel, run, policy, client)

    robots = _check_robots_with_retry(client, channel.entry_url)
    policy.robots_state = robots.state
    if not robots.allowed:
        _add_skipped_item(session, run, channel.entry_url, robots)
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code=robots.code,
            message="robots policy did not authorize this automatic request",
            extra_report={"robots": _robots_report(robots)},
        )

    try:
        if _uses_bounded_pdf_metadata(channel):
            fetcher = client.fetch_pdf_prefix
        elif _uses_cssn_periodical(channel):
            fetcher = client.fetch_without_redirects
        else:
            fetcher = client.fetch
        resource = _fetch_with_retry(fetcher, channel.entry_url)
    except ProbeError as exc:
        _add_failed_item(session, run, channel.entry_url, exc)
        return _finish_failed(
            session,
            channel,
            run,
            category=exc.category,
            code=exc.code,
            message=exc.message,
            extra_report={
                "retryable": exc.retryable,
                "retry_after": exc.retry_after,
                "robots": _robots_report(robots),
            },
        )

    normalized_entry = normalize_http_url(channel.entry_url)
    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=channel.entry_url,
            normalized_url=normalized_entry,
            status="fetched",
            http_status=resource.status_code,
        )
    )
    actual_type = classify_content(resource)
    if actual_type != channel.collector_type:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code="collector_type_mismatch",
            message=f"expected {channel.collector_type}, received {actual_type}",
            extra_report={"robots": _robots_report(robots), "actual_type": actual_type},
        )

    result = get_collector(channel.collector_type).parse(
        resource,
        channel.collector_config,
        link_role=channel.link_role,
    )
    if _uses_management_world(channel) and result.followups:
        result = _fetch_management_world_followups(
            session,
            run,
            channel,
            client,
            result,
        )
        candidates = list(result.candidates)
    elif _uses_cssn_periodical(channel):
        result = _fetch_cssn_periodical_followups(
            session,
            run,
            channel,
            client,
            result,
        )
        candidates = list(result.candidates)
    else:
        if any(f.context.get("followup_profile") == "jpaas_unitbuild" for f in result.followups):
            result = _fetch_auto_list_jpaas_followups(session, run, channel, client, result)
        candidates = list(result.candidates)
        candidates.extend(_followup_candidates(result.followups))
    session.flush()
    seen_item_urls = set(
        session.scalars(select(CollectionItem.normalized_url).where(CollectionItem.run_id == run.id))
    )
    seen_item_urls.add(normalized_entry)
    for candidate in candidates:
        candidate_parent = candidate.source_metadata.get("issue_page_url") or candidate.source_metadata.get(
            "discovered_from_url"
        )
        if not isinstance(candidate_parent, str) or not candidate_parent:
            candidate_parent = resource.final_url
        _add_candidate_item(
            session,
            run,
            candidate,
            seen_item_urls,
            discovered_from_url=candidate_parent,
        )
    diagnostics = [
        {
            "code": diagnostic.code,
            "level": diagnostic.level,
            "message": diagnostic.message,
            **({"locator": diagnostic.locator} if diagnostic.locator else {}),
        }
        for diagnostic in result.diagnostics
    ]
    base_report = {
        "robots": _robots_report(robots),
        "request_url": resource.request_url,
        "final_url": resource.final_url,
        "http_status": resource.status_code,
        "resource_sha256": resource.sha256,
        "resource_sha256_scope": ("range_prefix" if _uses_bounded_pdf_metadata(channel) else "response_body"),
        "actual_type": actual_type,
        "diagnostics": diagnostics,
        "storage_scope": policy.storage_scope,
        "rag_scope": policy.rag_scope,
        "raw_content_stored": False,
    }
    session.commit()
    return _persist_document_candidates(
        session,
        channel_id=channel.id,
        run_id=run.id,
        candidates=candidates,
        document_type=_document_type(channel),
        extractor_name=f"{channel.collector_type}_collector",
        extractor_version=channel.adapter_version,
        seen_at=resource.fetched_at,
        storage_scope=policy.storage_scope,
        terms_url=policy.terms_url,
        reviewed_at=policy.reviewed_at,
        diagnostics_have_error=any(diagnostic.level == "error" for diagnostic in result.diagnostics),
        base_report=base_report,
    )


def _persist_document_candidates(
    session: Session,
    *,
    channel_id: int,
    run_id: int,
    candidates: list[CandidateDocument],
    document_type: str,
    extractor_name: str,
    extractor_version: str,
    seen_at: datetime,
    storage_scope: str,
    terms_url: str | None,
    reviewed_at: datetime | None,
    diagnostics_have_error: bool,
    base_report: dict[str, Any],
) -> ChannelOutcome:
    persisted = 0
    documents_created = 0
    versions_created = 0
    skipped: Counter[str] = Counter()
    for batch_start in range(0, len(candidates), _DOCUMENT_PERSISTENCE_COMMIT_EVERY):
        batch = candidates[batch_start : batch_start + _DOCUMENT_PERSISTENCE_COMMIT_EVERY]
        last_error: OperationalError | None = None
        batch_committed = False
        for attempt in range(1, _MAX_DOCUMENT_PERSISTENCE_ATTEMPTS + 1):
            try:
                channel = _get_channel(session, channel_id)
                run = session.get(CollectionRun, run_id)
                if run is None:
                    raise ValueError(f"collection run {run_id} does not exist")
                if run.status != "running":
                    return _outcome(channel, run)

                batch_persisted = 0
                batch_documents_created = 0
                batch_versions_created = 0
                batch_skipped: Counter[str] = Counter()
                for candidate in batch:
                    outcome = persist_candidate_metadata(
                        session,
                        channel.source_id,
                        candidate,
                        document_type=document_type,
                        extractor_name=extractor_name,
                        extractor_version=extractor_version,
                        seen_at=seen_at,
                        storage_scope=storage_scope,
                        terms_url=terms_url,
                        channel_id=channel.id,
                        reviewed_at=reviewed_at,
                    )
                    if outcome.skipped_reason:
                        batch_skipped[outcome.skipped_reason] += 1
                        continue
                    batch_persisted += 1
                    batch_documents_created += int(outcome.document_created)
                    batch_versions_created += int(outcome.version_created)

                next_persisted = persisted + batch_persisted
                next_documents_created = documents_created + batch_documents_created
                next_versions_created = versions_created + batch_versions_created
                next_skipped = skipped + batch_skipped
                processed = batch_start + len(batch)
                run.items_discovered = len(candidates)
                run.items_persisted = next_persisted
                run.heartbeat_at = datetime.now(UTC)
                run.report = {
                    **base_report,
                    "documents_created": next_documents_created,
                    "versions_created": next_versions_created,
                    "skipped_candidates": dict(next_skipped),
                    "persistence_progress": {
                        "processed_candidates": processed,
                        "total_candidates": len(candidates),
                        "commit_every": _DOCUMENT_PERSISTENCE_COMMIT_EVERY,
                    },
                }
                session.commit()
                persisted = next_persisted
                documents_created = next_documents_created
                versions_created = next_versions_created
                skipped = next_skipped
                batch_committed = True
                break
            except OperationalError as exc:
                last_error = exc
                try:
                    session.rollback()
                except OperationalError:
                    pass
                if attempt < _MAX_DOCUMENT_PERSISTENCE_ATTEMPTS:
                    session.invalidate()
        if not batch_committed:
            if last_error is not None:
                raise last_error
            raise RuntimeError("document persistence batch did not commit")

    last_error = None
    for attempt in range(1, _MAX_DOCUMENT_PERSISTENCE_ATTEMPTS + 1):
        try:
            channel = _get_channel(session, channel_id)
            run = session.get(CollectionRun, run_id)
            if run is None:
                raise ValueError(f"collection run {run_id} does not exist")
            if run.status != "running":
                return _outcome(channel, run)
            run.items_discovered = len(candidates)
            run.items_persisted = persisted
            run.finished_at = datetime.now(UTC)
            run.heartbeat_at = run.finished_at
            run.report = {
                **base_report,
                "documents_created": documents_created,
                "versions_created": versions_created,
                "skipped_candidates": dict(skipped),
                "persistence_progress": {
                    "processed_candidates": len(candidates),
                    "total_candidates": len(candidates),
                    "commit_every": _DOCUMENT_PERSISTENCE_COMMIT_EVERY,
                },
            }
            if not candidates:
                run.status = "failed"
                run.error_category = "quality"
                run.error_code = "no_items_discovered"
                run.error_message = "collector found no candidate materials"
            elif persisted == 0 or diagnostics_have_error or skipped:
                run.status = "partial"
                if persisted == 0:
                    run.error_category = "quality"
                    run.error_code = "no_items_persisted"
                    run.error_message = "no discovered candidate passed the metadata gate"
                elif diagnostics_have_error:
                    run.error_category = "parser"
                    run.error_code = "collector_diagnostics_error"
                    run.error_message = "collector returned error diagnostics alongside accepted candidates"
                else:
                    run.error_category = "quality"
                    run.error_code = "candidates_skipped"
                    run.error_message = "some discovered candidates did not pass the metadata gate"
            else:
                run.status = "succeeded"
            if run.status in {"succeeded", "partial"} and persisted:
                channel.last_success_at = run.finished_at
            session.commit()
            session.refresh(run)
            return _outcome(channel, run)
        except OperationalError as exc:
            last_error = exc
            try:
                session.rollback()
            except OperationalError:
                pass
            if attempt < _MAX_DOCUMENT_PERSISTENCE_ATTEMPTS:
                session.invalidate()
    if last_error is not None:
        raise last_error
    raise RuntimeError("document persistence did not produce a collection outcome")


def collect_comtrade_channel(
    session: Session,
    channel_id: int,
    *,
    client: SafeHttpClient,
    scope: StructuredScope | None = None,
    trigger_kind: str = "manual",
) -> ChannelOutcome:
    """Run a reviewed Comtrade profile through its dedicated structured-data path."""

    _validate_trigger_kind(trigger_kind)
    channel = _get_channel(session, channel_id)
    from app.services.collection_schedule import rate_limit_cooldown

    latest = session.scalar(
        select(CollectionRun)
        .where(CollectionRun.channel_id == channel.id)
        .order_by(CollectionRun.id.desc())
        .limit(1)
    )
    if latest and rate_limit_cooldown(latest, datetime.now(UTC)):
        return _outcome(channel, latest)
    review_mode = _comtrade_review_mode(channel)
    active_run = session.scalar(
        select(CollectionRun.id).where(
            CollectionRun.channel_id == channel.id,
            CollectionRun.status.in_(("queued", "running")),
        )
    )
    if active_run is not None:
        raise ValueError("channel already has an active collection run")

    now = datetime.now(UTC)
    run_report: dict[str, Any] = {"channel_status_before": channel.status}
    if review_mode is not None:
        run_report["review_mode"] = review_mode
    run = CollectionRun(
        channel_id=channel.id,
        trigger_kind=trigger_kind,
        status="running",
        started_at=now,
        heartbeat_at=now,
        report=run_report,
    )
    session.add(run)
    session.flush()

    if review_mode is None:
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="comtrade_channel_scope_mismatch",
            message="UN Comtrade channel is not in a reviewed collection state",
            extra_report={
                "profile": "un_comtrade_goods_annual_hs",
                "raw_content_stored": False,
            },
        )

    policy = channel.policy
    if policy is None:
        policy = SourceChannelPolicy(channel_id=channel.id)
        session.add(policy)
        session.flush()
    if policy.robots_state == "disallowed":
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="robots_policy_disallowed",
            message="stored robots review disallows automatic collection",
        )
    if _terms_disallow_collection(policy.terms_state):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="terms_disallowed",
            message="reviewed source terms disallow automatic collection",
        )
    if policy.storage_scope != "metadata" or policy.rag_scope != "none":
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="unsupported_storage_policy",
            message="formal collection requires metadata-only document storage and no RAG",
        )
    session.commit()
    return _collect_comtrade_goods(
        session,
        channel,
        run,
        policy,
        client,
        scope,
        review_mode,
    )


def _collect_comtrade_goods(
    session: Session,
    channel: SourceChannel,
    run: CollectionRun,
    policy: SourceChannelPolicy,
    client: SafeHttpClient,
    scope: StructuredScope | None,
    review_mode: str,
) -> ChannelOutcome:
    try:
        scope = scope or load_structured_scope()
        requests = _comtrade_requests(scope)
    except (
        ComtradeAvailabilityParseError,
        ComtradeDataParseError,
        ComtradeMetadataParseError,
        StructuredScopeValidationError,
    ) as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="internal",
            code="structured_scope_invalid",
            message=str(exc),
        )

    comtrade = scope.comtrade
    config = channel.collector_config
    first_data_url = next(request.url for request in requests if request.kind == "data")
    if (
        channel.collector_type != "api"
        or channel.link_role != "official"
        or _catalog_row(channel) != comtrade.source_catalog_row
        or config.get("profile") != "un_comtrade_goods_annual_hs"
        or config.get("scope_id") != scope.scope_id
        or config.get("dataset_key") != comtrade.dataset_key
        or channel.entry_url != first_data_url
    ):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="comtrade_channel_scope_mismatch",
            message="UN Comtrade channel does not match the reviewed collection profile",
            extra_report={
                "profile": "un_comtrade_goods_annual_hs",
                "scope_id": scope.scope_id,
                "raw_content_stored": False,
            },
        )

    availability_by_reporter: dict[int, ComtradeAvailabilityParseResult] = {}
    metadata_by_reporter: dict[int, ComtradeMetadataParseResult] = {}
    availability_results: list[ComtradeAvailabilityParseResult] = []
    metadata_results: list[ComtradeMetadataParseResult] = []
    data_results: list[ComtradeDataParseResult] = []
    fetched_at: list[datetime] = []
    query_evidence: list[dict[str, Any]] = []
    robots_codes: Counter[str] = Counter()
    robots_fetch_count = 0
    cached_absent_robots: RobotsDecision | None = None

    for request in requests:
        if cached_absent_robots is None:
            robots = _check_robots_with_retry(client, request.url)
            robots_fetch_count += 1
            if robots.code == "robots_absent":
                # A missing robots file applies to every path on this one
                # reviewed Comtrade origin; avoid 104 identical 404 fetches.
                cached_absent_robots = robots
        else:
            robots = cached_absent_robots
        policy.robots_state = robots.state
        robots_codes[robots.code] += 1
        if not robots.allowed:
            _add_skipped_item(session, run, request.url, robots)
            return _finish_failed(
                session,
                channel,
                run,
                category="policy",
                code=robots.code,
                message="robots policy did not authorize this UN Comtrade request",
                extra_report=_comtrade_failure_report(
                    scope,
                    query_evidence,
                    robots_codes,
                    review_mode,
                ),
            )

        fetcher = client.fetch_comtrade if request.kind == "data" else client.fetch_without_redirects
        try:
            resource = _fetch_with_retry(
                fetcher,
                request.url,
                retry_delays=(2, 5, 10, 20),
            )
        except ProbeError as exc:
            _add_failed_item(session, run, request.url, exc)
            return _finish_failed(
                session,
                channel,
                run,
                category=exc.category,
                code=exc.code,
                message=exc.message,
                extra_report={
                    **_comtrade_failure_report(
                        scope,
                        query_evidence,
                        robots_codes,
                        review_mode,
                    ),
                    "retryable": exc.retryable,
                },
            )

        normalized_url = normalize_http_url(request.url)
        session.add(
            CollectionItem(
                run_id=run.id,
                request_url=request.url,
                normalized_url=normalized_url,
                status="fetched",
                http_status=resource.status_code,
            )
        )
        if normalize_http_url(resource.final_url) != normalized_url:
            return _finish_failed(
                session,
                channel,
                run,
                category="quality",
                code="comtrade_unexpected_redirect",
                message="UN Comtrade fixed query redirected to an unexpected URL",
                extra_report=_comtrade_failure_report(
                    scope,
                    query_evidence,
                    robots_codes,
                    review_mode,
                ),
            )
        actual_type = classify_content(resource)
        if actual_type != "api":
            return _finish_failed(
                session,
                channel,
                run,
                category="quality",
                code="collector_type_mismatch",
                message=f"expected api, received {actual_type}",
                extra_report={
                    **_comtrade_failure_report(
                        scope,
                        query_evidence,
                        robots_codes,
                        review_mode,
                    ),
                    "actual_type": actual_type,
                },
            )

        try:
            if request.kind == "getDA":
                parsed = parse_comtrade_availability(
                    resource,
                    ComtradeAvailabilityQuerySpec(
                        reporter_iso3=request.reporter_iso3,
                        reporter_code=request.reporter_code,
                    ),
                )
                availability_by_reporter[request.reporter_code] = parsed
                availability_results.append(parsed)
                returned_row_count = parsed.returned_dataset_count
            elif request.kind == "metadata":
                parsed = parse_comtrade_metadata(
                    resource,
                    ComtradeMetadataQuerySpec(
                        reporter_iso3=request.reporter_iso3,
                        reporter_code=request.reporter_code,
                    ),
                )
                metadata_by_reporter[request.reporter_code] = parsed
                metadata_results.append(parsed)
                returned_row_count = parsed.returned_dataset_count
            else:
                availability = availability_by_reporter.get(request.reporter_code)
                metadata = metadata_by_reporter.get(request.reporter_code)
                if availability is None or metadata is None:
                    raise ComtradeDataParseError(
                        "data_identity_context_missing",
                        "UN Comtrade data query is missing its public identity context",
                    )
                assert request.period is not None
                assert request.commodity_code is not None
                parsed = parse_comtrade_data(
                    resource,
                    ComtradeDataQuerySpec(
                        reporter_iso3=request.reporter_iso3,
                        reporter_code=request.reporter_code,
                        period=request.period,
                        commodity_code=request.commodity_code,
                    ),
                    availability,
                    metadata,
                )
                data_results.append(parsed)
                returned_row_count = parsed.returned_row_count
        except (
            ComtradeAvailabilityParseError,
            ComtradeDataParseError,
            ComtradeMetadataParseError,
        ) as exc:
            return _finish_failed(
                session,
                channel,
                run,
                category="parser",
                code=exc.code,
                message=exc.message,
                extra_report=_comtrade_failure_report(
                    scope,
                    query_evidence,
                    robots_codes,
                    review_mode,
                ),
            )

        query_evidence.append(
            {
                "kind": request.kind,
                "reporter_iso3": request.reporter_iso3,
                "period": request.period,
                "commodity_code": request.commodity_code,
                "request_url": resource.request_url,
                "final_url": resource.final_url,
                "http_status": resource.status_code,
                "resource_sha256": resource.sha256,
                "fetched_at": resource.fetched_at.isoformat(),
                "returned_row_count": returned_row_count,
            }
        )
        fetched_at.append(resource.fetched_at)

    manifest = _comtrade_query_manifest(scope, query_evidence)
    query_urls = tuple(request.url for request in requests)
    try:
        with session.begin_nested():
            stored = persist_comtrade_collection(
                session,
                ComtradeCollectionPayload(
                    scope=scope,
                    channel_id=channel.id,
                    collection_run_id=run.id,
                    retrieved_at=max(fetched_at),
                    query_manifest=manifest,
                    query_urls=query_urls,
                    availability_results=tuple(availability_results),
                    metadata_results=tuple(metadata_results),
                    data_results=tuple(data_results),
                ),
            )
    except ComtradeDataStoreError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code=exc.code,
            message=exc.message,
            extra_report=_comtrade_failure_report(
                scope,
                query_evidence,
                robots_codes,
                review_mode,
            ),
        )
    except SQLAlchemyError:
        return _finish_failed(
            session,
            channel,
            run,
            category="storage",
            code="comtrade_persistence_failed",
            message="UN Comtrade structured-data transaction failed",
            extra_report=_comtrade_failure_report(
                scope,
                query_evidence,
                robots_codes,
                review_mode,
            ),
        )

    finished_at = datetime.now(UTC)
    run.items_discovered = comtrade.logical_dimension_cells
    run.items_persisted = comtrade.logical_dimension_cells
    run.finished_at = finished_at
    run.heartbeat_at = finished_at
    run.status = "succeeded"
    run.report = {
        "profile": "un_comtrade_goods_annual_hs",
        "review_mode": review_mode,
        "item_semantics": "logical_dimension_cells",
        "scope_id": scope.scope_id,
        "scope_sha256": scope.scope_sha256,
        "source_catalog_row": comtrade.source_catalog_row,
        "dataset_key": comtrade.dataset_key,
        "query_count": len(requests),
        "query_kind_counts": dict(Counter(request.kind for request in requests)),
        "query_manifest": manifest,
        "query_signature": stored.query_signature,
        "snapshot_hash": stored.snapshot_hash,
        "dataset_id": stored.dataset_id,
        "snapshot_id": stored.snapshot_id,
        "snapshot_created": stored.snapshot_created,
        "reused_snapshot_id": None if stored.snapshot_created else stored.snapshot_id,
        "observations_created": stored.observations_created,
        "versions_created": stored.versions_created,
        "versions_unchanged": stored.versions_unchanged,
        "expected_count": stored.expected_count,
        "returned_count": stored.returned_count,
        "valued_count": stored.valued_count,
        "source_null_count": stored.source_null_count,
        "not_returned_count": stored.not_returned_count,
        "logical_dimension_count": stored.logical_dimension_count,
        "logical_returned_count": stored.logical_returned_count,
        "logical_not_returned_count": stored.logical_not_returned_count,
        "data_row_count": stored.data_row_count,
        "availability_dataset_count": sum(result.returned_dataset_count for result in availability_results),
        "metadata_dataset_count": sum(result.returned_dataset_count for result in metadata_results),
        "metadata_note_count": sum(len(result.notes) for result in metadata_results),
        "data_returned_row_count": sum(result.returned_row_count for result in data_results),
        "data_empty_shard_count": sum(result.returned_row_count == 0 for result in data_results),
        "request_shards": len(data_results),
        "logical_dimension_cells": comtrade.logical_dimension_cells,
        "robots_check_count": sum(robots_codes.values()),
        "robots_fetch_count": robots_fetch_count,
        "robots_code_counts": dict(sorted(robots_codes.items())),
        "storage_scope": policy.storage_scope,
        "rag_scope": policy.rag_scope,
        "credential_transport_only": True,
        "raw_content_stored": False,
        "document_metadata_stored": False,
    }
    channel.last_success_at = finished_at
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def _comtrade_requests(scope: StructuredScope) -> tuple[_ComtradeRequest, ...]:
    comtrade = scope.comtrade
    requests: list[_ComtradeRequest] = []
    for reporter in comtrade.reporters:
        requests.append(
            _ComtradeRequest(
                kind="getDA",
                reporter_iso3=reporter.iso3,
                reporter_code=reporter.source_code,
                period=None,
                commodity_code=None,
                url=build_comtrade_data_availability_request_url(comtrade, reporter.iso3),
            )
        )
    for reporter in comtrade.reporters:
        requests.append(
            _ComtradeRequest(
                kind="metadata",
                reporter_iso3=reporter.iso3,
                reporter_code=reporter.source_code,
                period=None,
                commodity_code=None,
                url=build_comtrade_metadata_request_url(comtrade, reporter.iso3),
            )
        )
    for reporter in comtrade.reporters:
        for period in range(comtrade.start_year, comtrade.end_year + 1):
            for commodity in comtrade.commodities:
                requests.append(
                    _ComtradeRequest(
                        kind="data",
                        reporter_iso3=reporter.iso3,
                        reporter_code=reporter.source_code,
                        period=period,
                        commodity_code=commodity.hs6,
                        url=build_comtrade_request_url(
                            comtrade,
                            reporter.iso3,
                            period,
                            commodity.hs6,
                        ),
                    )
                )
    counts = Counter(request.kind for request in requests)
    data_identities = {
        (request.reporter_code, request.period, request.commodity_code)
        for request in requests
        if request.kind == "data"
    }
    if (
        counts
        != {
            "getDA": comtrade.data_availability_requests,
            "metadata": comtrade.metadata_requests,
            "data": comtrade.request_shards,
        }
        or len(data_identities) != comtrade.request_shards
        or len(set(request.url for request in requests)) != len(requests)
        or comtrade.logical_dimension_cells
        != comtrade.request_shards * len(comtrade.partners) * len(comtrade.flows)
    ):
        raise StructuredScopeValidationError(
            "UN Comtrade request plan does not cover the reviewed 104-query scope"
        )
    return tuple(requests)


def _comtrade_query_manifest(
    scope: StructuredScope,
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "scope_id": scope.scope_id,
        "scope_sha256": scope.scope_sha256,
        "queries": list(queries),
    }


def _comtrade_failure_report(
    scope: StructuredScope,
    query_evidence: list[dict[str, Any]],
    robots_codes: Counter[str],
    review_mode: str,
) -> dict[str, Any]:
    return {
        "profile": "un_comtrade_goods_annual_hs",
        "review_mode": review_mode,
        "scope_id": scope.scope_id,
        "scope_sha256": scope.scope_sha256,
        "completed_query_count": len(query_evidence),
        "completed_query_kind_counts": dict(
            sorted(Counter(query["kind"] for query in query_evidence).items())
        ),
        "query_manifest": _comtrade_query_manifest(scope, query_evidence),
        "robots_check_count": sum(robots_codes.values()),
        "robots_code_counts": dict(sorted(robots_codes.items())),
        "credential_transport_only": True,
        "raw_content_stored": False,
        "document_metadata_stored": False,
    }


def _collect_oecd_oda(
    session: Session,
    channel: SourceChannel,
    run: CollectionRun,
    policy: SourceChannelPolicy,
    client: SafeHttpClient,
) -> ChannelOutcome:
    config = channel.collector_config
    if (
        channel.collector_type != "api"
        or channel.link_role != "official"
        or _catalog_row(channel) != 46
        or config.get("scope_id") != OECD_ODA_SCOPE_ID
        or config.get("dataset_key") != OECD_ODA_DATASET_KEY
        or channel.entry_url != OECD_ODA_URL
    ):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="oecd_oda_channel_scope_mismatch",
            message="OECD ODA channel does not match the reviewed shadow scope",
            extra_report={"profile": "oecd_oda", "scope_id": OECD_ODA_SCOPE_ID},
        )

    robots = _check_robots_with_retry(client, OECD_ODA_URL)
    policy.robots_state = robots.state
    if not robots.allowed:
        _add_skipped_item(session, run, OECD_ODA_URL, robots)
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code=robots.code,
            message="robots policy did not authorize the OECD ODA request",
            extra_report={
                "profile": "oecd_oda",
                "scope_id": OECD_ODA_SCOPE_ID,
                "robots": _robots_report(robots),
                "raw_content_stored": False,
            },
        )

    try:
        resource = _fetch_with_retry(client.fetch_oecd_oda, OECD_ODA_URL)
    except ProbeError as exc:
        _add_failed_item(session, run, OECD_ODA_URL, exc)
        return _finish_failed(
            session,
            channel,
            run,
            category=exc.category,
            code=exc.code,
            message=exc.message,
            extra_report={
                "profile": "oecd_oda",
                "scope_id": OECD_ODA_SCOPE_ID,
                "robots": _robots_report(robots),
                "retryable": exc.retryable,
                "raw_content_stored": False,
            },
        )

    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=OECD_ODA_URL,
            normalized_url=normalize_http_url(OECD_ODA_URL),
            status="fetched",
            http_status=resource.status_code,
        )
    )
    if resource.final_url != OECD_ODA_URL:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code="oecd_oda_unexpected_redirect",
            message="OECD fixed query redirected to an unexpected URL",
            extra_report={"profile": "oecd_oda", "raw_content_stored": False},
        )
    try:
        parsed = parse_oecd_oda(resource)
    except OecdOdaParseError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="parser",
            code=exc.code,
            message=exc.message,
            extra_report={"profile": "oecd_oda", "raw_content_stored": False},
        )

    try:
        with session.begin_nested():
            stored = persist_oecd_oda(
                session,
                channel_id=channel.id,
                collection_run_id=run.id,
                retrieved_at=resource.fetched_at,
                query_url=OECD_ODA_URL,
                resource_sha256=resource.sha256,
                parsed=parsed,
            )
    except OecdOdaStoreError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code=exc.code,
            message=exc.message,
            extra_report={"profile": "oecd_oda", "raw_content_stored": False},
        )
    except SQLAlchemyError:
        return _finish_failed(
            session,
            channel,
            run,
            category="storage",
            code="oecd_oda_persistence_failed",
            message="OECD ODA structured-data transaction failed",
            extra_report={"profile": "oecd_oda", "raw_content_stored": False},
        )

    finished_at = datetime.now(UTC)
    run.items_discovered = 40
    run.items_persisted = 40
    run.finished_at = finished_at
    run.heartbeat_at = finished_at
    run.status = "succeeded"
    run.report = {
        "profile": "oecd_oda",
        "item_semantics": "logical_dimension_cells",
        "scope_id": OECD_ODA_SCOPE_ID,
        "source_catalog_row": 46,
        "dataset_key": OECD_ODA_DATASET_KEY,
        "query_count": 1,
        "query_manifest": {
            "request_url": resource.request_url,
            "final_url": resource.final_url,
            "http_status": resource.status_code,
            "resource_sha256": resource.sha256,
            "fetched_at": resource.fetched_at.isoformat(),
        },
        "query_signature": stored.query_signature,
        "snapshot_hash": stored.snapshot_hash,
        "dataset_id": stored.dataset_id,
        "snapshot_id": stored.snapshot_id,
        "snapshot_created": stored.snapshot_created,
        "observations_created": stored.observations_created,
        "versions_created": stored.versions_created,
        "versions_unchanged": stored.versions_unchanged,
        "expected_count": 40,
        "returned_count": 40,
        "valued_count": 40,
        "source_null_count": 0,
        "not_returned_count": 0,
        "robots": _robots_report(robots),
        "storage_scope": policy.storage_scope,
        "rag_scope": policy.rag_scope,
        "raw_content_stored": False,
        "document_metadata_stored": False,
    }
    channel.last_success_at = finished_at
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def _collect_unctad_fdi(
    session: Session,
    channel: SourceChannel,
    run: CollectionRun,
    policy: SourceChannelPolicy,
    client: SafeHttpClient,
) -> ChannelOutcome:
    config = channel.collector_config
    base_report: dict[str, Any] = {
        "profile": "unctad_fdi",
        "scope_id": UNCTAD_FDI_SCOPE_ID,
        "source_catalog_row": 50,
        "dataset_key": UNCTAD_FDI_DATASET_KEY,
        "scope_origin": "agent_proposed_user_authorized_2026-07-15",
        "raw_content_stored": False,
        "document_metadata_stored": False,
    }
    if (
        channel.collector_type != "api"
        or channel.link_role != "official"
        or _catalog_row(channel) != 50
        or config.get("scope_id") != UNCTAD_FDI_SCOPE_ID
        or config.get("dataset_key") != UNCTAD_FDI_DATASET_KEY
        or channel.entry_url != UNCTAD_FDI_BULK_URL
    ):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="unctad_fdi_channel_scope_mismatch",
            message="UNCTAD FDI channel does not match the authorized shadow scope",
            extra_report=base_report,
        )

    robots = _check_robots_with_retry(client, UNCTAD_FDI_BULK_URL)
    policy.robots_state = robots.state
    if not robots.allowed:
        _add_skipped_item(session, run, UNCTAD_FDI_BULK_URL, robots)
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code=robots.code,
            message="robots policy did not authorize the UNCTAD FDI bulk request",
            extra_report={**base_report, "robots": _robots_report(robots)},
        )

    try:
        resource = _fetch_with_retry(client.fetch_unctad_fdi_bulk, UNCTAD_FDI_BULK_URL)
    except ProbeError as exc:
        _add_failed_item(session, run, UNCTAD_FDI_BULK_URL, exc)
        return _finish_failed(
            session,
            channel,
            run,
            category=exc.category,
            code=exc.code,
            message=exc.message,
            extra_report={
                **base_report,
                "robots": _robots_report(robots),
                "retryable": exc.retryable,
            },
        )

    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=UNCTAD_FDI_BULK_URL,
            normalized_url=normalize_http_url(UNCTAD_FDI_BULK_URL),
            status="fetched",
            http_status=resource.status_code,
        )
    )
    if resource.final_url != UNCTAD_FDI_BULK_URL:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code="unctad_fdi_unexpected_redirect",
            message="UNCTAD FDI fixed bulk file redirected to an unexpected URL",
            extra_report=base_report,
        )
    try:
        panel = parse_unctad_fdi(resource)
    except UnctadFdiParseError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="parser",
            code=exc.code,
            message=exc.message,
            extra_report=base_report,
        )

    try:
        with session.begin_nested():
            stored = persist_unctad_fdi(
                session,
                channel_id=channel.id,
                collection_run_id=run.id,
                retrieved_at=resource.fetched_at,
                query_url=UNCTAD_FDI_BULK_URL,
                resource_sha256=resource.sha256,
                panel=panel,
            )
    except UnctadFdiStoreError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code=exc.code,
            message=exc.message,
            extra_report=base_report,
        )
    except SQLAlchemyError:
        return _finish_failed(
            session,
            channel,
            run,
            category="storage",
            code="unctad_fdi_persistence_failed",
            message="UNCTAD FDI structured-data transaction failed",
            extra_report=base_report,
        )

    finished_at = datetime.now(UTC)
    run.items_discovered = 160
    run.items_persisted = 160
    run.finished_at = finished_at
    run.heartbeat_at = finished_at
    run.status = "succeeded"
    run.report = {
        **base_report,
        "item_semantics": "logical_dimension_cells_including_explicit_missing",
        "panel_status": "complete" if panel.not_returned_count == 0 else "incomplete",
        "query_count": 1,
        "query_manifest": {
            "request_url": resource.request_url,
            "final_url": resource.final_url,
            "http_status": resource.status_code,
            "resource_sha256": resource.sha256,
            "fetched_at": resource.fetched_at.isoformat(),
            "archive_member": "US_FdiFlowsStock.csv",
        },
        "query_signature": stored.query_signature,
        "snapshot_hash": stored.snapshot_hash,
        "dataset_id": stored.dataset_id,
        "snapshot_id": stored.snapshot_id,
        "snapshot_created": stored.snapshot_created,
        "observations_created": stored.observations_created,
        "versions_created": stored.versions_created,
        "versions_unchanged": stored.versions_unchanged,
        "expected_count": 160,
        "returned_count": panel.returned_count,
        "valued_count": panel.valued_count,
        "source_null_count": panel.source_null_count,
        "not_returned_count": panel.not_returned_count,
        "robots": _robots_report(robots),
        "storage_scope": policy.storage_scope,
        "rag_scope": policy.rag_scope,
    }
    channel.last_success_at = finished_at
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def _collect_wits_tariff(
    session: Session,
    channel: SourceChannel,
    run: CollectionRun,
    policy: SourceChannelPolicy,
    client: SafeHttpClient,
) -> ChannelOutcome:
    config = channel.collector_config
    base_report: dict[str, Any] = {
        "profile": "wits_tariff",
        "scope_id": WITS_TARIFF_SCOPE_ID,
        "source_catalog_row": 49,
        "dataset_key": WITS_TARIFF_DATASET_KEY,
        "panel_status": "incomplete",
        "sparse_panel_accepted": True,
        "raw_content_stored": False,
        "document_metadata_stored": False,
    }
    if (
        channel.collector_type != "api"
        or channel.link_role != "official"
        or _catalog_row(channel) != 49
        or config.get("scope_id") != WITS_TARIFF_SCOPE_ID
        or config.get("dataset_key") != WITS_TARIFF_DATASET_KEY
        or channel.entry_url != WITS_AVAILABILITY_URL
    ):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="wits_tariff_channel_scope_mismatch",
            message="WITS tariff channel does not match the reviewed shadow scope",
            extra_report=base_report,
        )

    robots_codes: Counter[str] = Counter()
    query_evidence: list[WitsTariffQueryEvidence] = []
    query_manifest: list[dict[str, Any]] = []

    robots = _check_robots_with_retry(client, WITS_AVAILABILITY_URL)
    robots_codes[robots.code] += 1
    policy.robots_state = robots.state
    if not robots.allowed:
        _add_skipped_item(session, run, WITS_AVAILABILITY_URL, robots)
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code=robots.code,
            message="robots policy did not authorize the WITS availability request",
            extra_report={
                **base_report,
                "robots_code_counts": dict(robots_codes),
                "completed_query_count": 0,
            },
        )
    try:
        availability_resource = _fetch_with_retry(
            client.fetch_wits_availability,
            WITS_AVAILABILITY_URL,
        )
    except ProbeError as exc:
        _add_failed_item(session, run, WITS_AVAILABILITY_URL, exc)
        return _finish_failed(
            session,
            channel,
            run,
            category=exc.category,
            code=exc.code,
            message=exc.message,
            extra_report={
                **base_report,
                "robots_code_counts": dict(robots_codes),
                "completed_query_count": 0,
                "retryable": exc.retryable,
            },
        )
    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=WITS_AVAILABILITY_URL,
            normalized_url=normalize_http_url(WITS_AVAILABILITY_URL),
            status="fetched",
            http_status=availability_resource.status_code,
        )
    )
    if availability_resource.final_url != WITS_AVAILABILITY_URL:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code="wits_availability_unexpected_redirect",
            message="WITS availability query redirected to an unexpected URL",
            extra_report=base_report,
        )
    try:
        availability = parse_wits_tariff_availability(availability_resource)
    except WitsTariffParseError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="parser",
            code=exc.code,
            message=exc.message,
            extra_report=base_report,
        )
    query_evidence.append(
        WitsTariffQueryEvidence(
            kind="availability",
            request_url=WITS_AVAILABILITY_URL,
            resource_sha256=availability_resource.sha256,
            fetched_at=availability_resource.fetched_at,
        )
    )
    query_manifest.append(
        {
            "kind": "availability",
            "request_url": WITS_AVAILABILITY_URL,
            "resource_sha256": availability_resource.sha256,
            "fetched_at": availability_resource.fetched_at.isoformat(),
        }
    )

    shards = {}
    for schedule in availability.schedules:
        url = build_wits_tariff_url(schedule.source_reporter_code, schedule.period)
        robots = _check_robots_with_retry(client, url)
        robots_codes[robots.code] += 1
        policy.robots_state = robots.state
        if not robots.allowed:
            _add_skipped_item(session, run, url, robots)
            return _finish_failed(
                session,
                channel,
                run,
                category="policy",
                code=robots.code,
                message="robots policy did not authorize a WITS tariff request",
                extra_report={
                    **base_report,
                    "robots_code_counts": dict(sorted(robots_codes.items())),
                    "completed_query_count": len(query_evidence),
                    "failed_request_url": url,
                },
            )
        try:
            resource = _fetch_with_retry(client.fetch_wits_tariff, url)
        except ProbeError as exc:
            _add_failed_item(session, run, url, exc)
            return _finish_failed(
                session,
                channel,
                run,
                category=exc.category,
                code=exc.code,
                message=exc.message,
                extra_report={
                    **base_report,
                    "robots_code_counts": dict(sorted(robots_codes.items())),
                    "completed_query_count": len(query_evidence),
                    "failed_request_url": url,
                    "retryable": exc.retryable,
                },
            )
        session.add(
            CollectionItem(
                run_id=run.id,
                request_url=url,
                normalized_url=normalize_http_url(url),
                status="fetched",
                http_status=resource.status_code,
            )
        )
        if resource.final_url != url:
            return _finish_failed(
                session,
                channel,
                run,
                category="quality",
                code="wits_tariff_unexpected_redirect",
                message="WITS tariff query redirected to an unexpected URL",
                extra_report={**base_report, "failed_request_url": url},
            )
        try:
            shard = parse_wits_tariff(resource, schedule=schedule)
        except WitsTariffParseError as exc:
            return _finish_failed(
                session,
                channel,
                run,
                category="parser",
                code=exc.code,
                message=exc.message,
                extra_report={**base_report, "failed_request_url": url},
            )
        shards[(schedule.source_reporter_code, schedule.period)] = shard
        query_evidence.append(
            WitsTariffQueryEvidence(
                kind="tariff",
                request_url=url,
                resource_sha256=resource.sha256,
                fetched_at=resource.fetched_at,
            )
        )
        query_manifest.append(
            {
                "kind": "tariff",
                "request_url": url,
                "resource_sha256": resource.sha256,
                "fetched_at": resource.fetched_at.isoformat(),
            }
        )

    try:
        panel = build_wits_tariff_panel(availability, shards)
    except WitsTariffParseError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="parser",
            code=exc.code,
            message=exc.message,
            extra_report={**base_report, "completed_query_count": len(query_evidence)},
        )

    try:
        with session.begin_nested():
            stored = persist_wits_tariff(
                session,
                channel_id=channel.id,
                collection_run_id=run.id,
                retrieved_at=max(item.fetched_at for item in query_evidence),
                panel=panel,
                query_evidence=tuple(query_evidence),
            )
    except WitsTariffStoreError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code=exc.code,
            message=exc.message,
            extra_report={**base_report, "completed_query_count": len(query_evidence)},
        )
    except SQLAlchemyError:
        return _finish_failed(
            session,
            channel,
            run,
            category="storage",
            code="wits_tariff_persistence_failed",
            message="WITS tariff structured-data transaction failed",
            extra_report={**base_report, "completed_query_count": len(query_evidence)},
        )

    finished_at = datetime.now(UTC)
    run.items_discovered = 84
    run.items_persisted = 84
    run.finished_at = finished_at
    run.heartbeat_at = finished_at
    run.status = "succeeded"
    run.report = {
        **base_report,
        "item_semantics": "logical_dimension_cells_including_explicit_missing",
        "query_count": len(query_evidence),
        "query_manifest": query_manifest,
        "query_signature": stored.query_signature,
        "snapshot_hash": stored.snapshot_hash,
        "dataset_id": stored.dataset_id,
        "snapshot_id": stored.snapshot_id,
        "snapshot_created": stored.snapshot_created,
        "observations_created": stored.observations_created,
        "versions_created": stored.versions_created,
        "versions_unchanged": stored.versions_unchanged,
        "available_schedule_count": panel.available_schedule_count,
        "candidate_cell_count": panel.candidate_cell_count,
        "expected_count": 84,
        "returned_count": panel.returned_count,
        "valued_count": panel.valued_count,
        "source_null_count": panel.source_null_count,
        "not_returned_count": panel.not_returned_count,
        "robots_check_count": sum(robots_codes.values()),
        "robots_code_counts": dict(sorted(robots_codes.items())),
        "storage_scope": policy.storage_scope,
        "rag_scope": policy.rag_scope,
    }
    channel.last_success_at = finished_at
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def _collect_world_bank_indicators(
    session: Session,
    channel: SourceChannel,
    run: CollectionRun,
    policy: SourceChannelPolicy,
    client: SafeHttpClient,
) -> ChannelOutcome:
    try:
        scope = load_structured_scope()
        world_bank = scope.world_bank
        query_urls = tuple(
            build_world_bank_request_url(world_bank, indicator.code) for indicator in world_bank.indicators
        )
    except StructuredScopeValidationError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="internal",
            code="structured_scope_invalid",
            message=exc.args[0],
        )

    config = channel.collector_config
    if (
        channel.collector_type != "api"
        or channel.link_role != "official"
        or _catalog_row(channel) != world_bank.source_catalog_row
        or config.get("scope_id") != scope.scope_id
        or config.get("dataset_key") != world_bank.dataset_key
        or channel.entry_url != query_urls[0]
    ):
        return _finish_failed(
            session,
            channel,
            run,
            category="policy",
            code="world_bank_channel_scope_mismatch",
            message="World Bank channel does not match the reviewed structured-data scope",
            extra_report={"profile": "world_bank_indicators", "scope_id": scope.scope_id},
        )

    parse_results: list[WorldBankParseResult] = []
    query_manifest: list[dict[str, Any]] = []
    robots_checks: list[dict[str, Any]] = []
    fetched_at: list[datetime] = []
    for indicator, url in zip(world_bank.indicators, query_urls, strict=True):
        robots = _check_robots_with_retry(client, url)
        policy.robots_state = robots.state
        robots_checks.append(
            {
                "indicator_code": indicator.code,
                **_robots_report(robots),
            }
        )
        if not robots.allowed:
            _add_skipped_item(session, run, url, robots)
            return _finish_failed(
                session,
                channel,
                run,
                category="policy",
                code=robots.code,
                message="robots policy did not authorize this World Bank request",
                extra_report={
                    "profile": "world_bank_indicators",
                    "scope_id": scope.scope_id,
                    "scope_sha256": scope.scope_sha256,
                    "completed_query_count": len(parse_results),
                    "query_manifest": _world_bank_query_manifest(scope, query_manifest),
                    "robots_checks": robots_checks,
                    "raw_content_stored": False,
                },
            )

        try:
            resource = _fetch_with_retry(client.fetch, url)
        except ProbeError as exc:
            _add_failed_item(session, run, url, exc)
            return _finish_failed(
                session,
                channel,
                run,
                category=exc.category,
                code=exc.code,
                message=exc.message,
                extra_report={
                    "profile": "world_bank_indicators",
                    "scope_id": scope.scope_id,
                    "scope_sha256": scope.scope_sha256,
                    "completed_query_count": len(parse_results),
                    "query_manifest": _world_bank_query_manifest(scope, query_manifest),
                    "retryable": exc.retryable,
                    "robots_checks": robots_checks,
                    "raw_content_stored": False,
                },
            )

        normalized_url = normalize_http_url(url)
        session.add(
            CollectionItem(
                run_id=run.id,
                request_url=url,
                normalized_url=normalized_url,
                status="fetched",
                http_status=resource.status_code,
            )
        )
        evidence = {
            "indicator_code": indicator.code,
            "request_url": resource.request_url,
            "final_url": resource.final_url,
            "http_status": resource.status_code,
            "resource_sha256": resource.sha256,
            "fetched_at": resource.fetched_at.isoformat(),
        }
        query_manifest.append(evidence)
        if normalize_http_url(resource.final_url) != normalized_url:
            return _finish_failed(
                session,
                channel,
                run,
                category="quality",
                code="world_bank_unexpected_redirect",
                message="World Bank fixed query redirected to an unexpected URL",
                extra_report={
                    "profile": "world_bank_indicators",
                    "scope_id": scope.scope_id,
                    "completed_query_count": len(parse_results),
                    "query_manifest": _world_bank_query_manifest(scope, query_manifest),
                    "robots_checks": robots_checks,
                    "raw_content_stored": False,
                },
            )
        actual_type = classify_content(resource)
        if actual_type != "api":
            return _finish_failed(
                session,
                channel,
                run,
                category="quality",
                code="collector_type_mismatch",
                message=f"expected api, received {actual_type}",
                extra_report={
                    "profile": "world_bank_indicators",
                    "scope_id": scope.scope_id,
                    "completed_query_count": len(parse_results),
                    "query_manifest": _world_bank_query_manifest(scope, query_manifest),
                    "actual_type": actual_type,
                    "robots_checks": robots_checks,
                    "raw_content_stored": False,
                },
            )

        query_spec = WorldBankQuerySpec(
            indicator_code=indicator.code,
            metric_code=indicator.metric_code,
            unit=indicator.unit,
            currency=indicator.currency,
            price_basis=indicator.price_basis,
            source_id=int(world_bank.source_id),
        )
        try:
            parsed = parse_world_bank_indicators(resource, query_spec)
        except WorldBankParseError as exc:
            return _finish_failed(
                session,
                channel,
                run,
                category="parser",
                code=exc.code,
                message=exc.message,
                extra_report={
                    "profile": "world_bank_indicators",
                    "scope_id": scope.scope_id,
                    "completed_query_count": len(parse_results),
                    "query_manifest": _world_bank_query_manifest(scope, query_manifest),
                    "robots_checks": robots_checks,
                    "raw_content_stored": False,
                },
            )
        parse_results.append(parsed)
        fetched_at.append(resource.fetched_at)
        evidence.update(
            {
                "last_updated": parsed.last_updated.isoformat(),
                "returned_row_count": parsed.returned_row_count,
            }
        )

    manifest = _world_bank_query_manifest(scope, query_manifest)
    try:
        with session.begin_nested():
            stored = persist_structured_collection(
                session,
                StructuredCollectionPayload(
                    scope=scope,
                    channel_id=channel.id,
                    collection_run_id=run.id,
                    retrieved_at=max(fetched_at),
                    query_manifest=manifest,
                    query_urls=query_urls,
                    parse_results=tuple(parse_results),
                ),
            )
    except StructuredDataStoreError as exc:
        return _finish_failed(
            session,
            channel,
            run,
            category="quality",
            code=exc.code,
            message=exc.message,
            extra_report={
                "profile": "world_bank_indicators",
                "scope_id": scope.scope_id,
                "scope_sha256": scope.scope_sha256,
                "completed_query_count": len(parse_results),
                "query_manifest": manifest,
                "robots_checks": robots_checks,
                "raw_content_stored": False,
            },
        )
    except SQLAlchemyError:
        return _finish_failed(
            session,
            channel,
            run,
            category="storage",
            code="structured_persistence_failed",
            message="World Bank structured-data transaction failed",
            extra_report={
                "profile": "world_bank_indicators",
                "scope_id": scope.scope_id,
                "scope_sha256": scope.scope_sha256,
                "completed_query_count": len(parse_results),
                "query_manifest": manifest,
                "robots_checks": robots_checks,
                "raw_content_stored": False,
            },
        )

    coverage = _world_bank_coverage(parse_results, world_bank.countries)
    valued_count = sum(row["valued_count"] for row in coverage)
    source_null_count = sum(row["source_null_count"] for row in coverage)
    not_returned_count = sum(row["not_returned_count"] for row in coverage)
    returned_count = valued_count + source_null_count
    finished_at = datetime.now(UTC)
    run.items_discovered = world_bank.logical_dimension_cells
    run.items_persisted = world_bank.logical_dimension_cells
    run.finished_at = finished_at
    run.heartbeat_at = finished_at
    run.status = "succeeded"
    run.report = {
        "profile": "world_bank_indicators",
        "item_semantics": "logical_dimension_cells",
        "scope_id": scope.scope_id,
        "scope_sha256": scope.scope_sha256,
        "source_catalog_row": world_bank.source_catalog_row,
        "dataset_key": world_bank.dataset_key,
        "official_dataset": {
            "id": world_bank.official_dataset.id,
            "name": world_bank.official_dataset.name,
            "license_name": world_bank.official_dataset.license_name,
            "license_url": world_bank.official_dataset.license_url,
            "catalog_url": world_bank.official_dataset.catalog_url,
        },
        "query_count": len(query_urls),
        "query_manifest": manifest,
        "query_signature": stored.query_signature,
        "snapshot_hash": stored.snapshot_hash,
        "dataset_id": stored.dataset_id,
        "snapshot_id": stored.snapshot_id,
        "snapshot_created": stored.snapshot_created,
        "reused_snapshot_id": None if stored.snapshot_created else stored.snapshot_id,
        "observations_created": stored.observations_created,
        "versions_created": stored.versions_created,
        "versions_unchanged": stored.versions_unchanged,
        "expected_count": world_bank.logical_dimension_cells,
        "returned_count": returned_count,
        "valued_count": valued_count,
        "source_null_count": source_null_count,
        "not_returned_count": not_returned_count,
        "coverage": coverage,
        "provider_versions": sorted({result.last_updated.isoformat() for result in parse_results}),
        "robots_checks": robots_checks,
        "storage_scope": policy.storage_scope,
        "rag_scope": policy.rag_scope,
        "raw_content_stored": False,
        "document_metadata_stored": False,
    }
    channel.last_success_at = finished_at
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def _world_bank_coverage(
    parse_results: list[WorldBankParseResult],
    countries: tuple[str, ...],
) -> list[dict[str, Any]]:
    coverage: list[dict[str, Any]] = []
    for result in parse_results:
        for country in countries:
            observations = [item for item in result.observations if item.country_iso3 == country]
            valued_count = sum(item.value is not None for item in observations)
            source_null_count = sum(item.missing_reason == "source_null" for item in observations)
            not_returned_count = sum(item.missing_reason == "not_returned" for item in observations)
            coverage.append(
                {
                    "indicator_code": result.query_spec.indicator_code,
                    "country_iso3": country,
                    "expected_count": len(result.query_spec.years),
                    "returned_count": valued_count + source_null_count,
                    "valued_count": valued_count,
                    "source_null_count": source_null_count,
                    "not_returned_count": not_returned_count,
                }
            )
    return coverage


def _world_bank_query_manifest(
    scope: StructuredScope,
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "scope_id": scope.scope_id,
        "scope_sha256": scope.scope_sha256,
        "queries": list(queries),
    }


def _followup_candidates(followups: tuple) -> list[CandidateDocument]:
    candidates: list[CandidateDocument] = []
    for followup in followups:
        title = followup.context.get("title")
        summary = followup.context.get("summary")
        published_at, date_provenance = source_date_from_url(followup.discovery_url)
        candidates.append(
            CandidateDocument(
                discovery_url=followup.discovery_url,
                canonical_url=None,
                title=str(title).strip() if title else None,
                published_at=published_at,
                summary=str(summary).strip() if summary else None,
                summary_kind="html_summary" if summary else None,
                summary_source_url=(
                    str(followup.context.get("discovered_from_url"))
                    if summary and followup.context.get("discovered_from_url")
                    else None
                ),
                media_type="text/html",
                resource_sha256=followup.parent_resource_sha256,
                source_metadata={
                    "discovered_from_url": followup.context.get("discovered_from_url"),
                    "detail_verified": False,
                    **({"published_at_provenance": date_provenance} if date_provenance is not None else {}),
                },
            )
        )
    return candidates


def _fetch_cssn_periodical_followups(
    session: Session,
    run: CollectionRun,
    channel: SourceChannel,
    client: SafeHttpClient,
    root_result: ParseResult,
) -> ParseResult:
    max_items = int(channel.collector_config["max_items"])
    max_fetches = int(channel.collector_config["max_issues"]) * int(
        channel.collector_config["max_pages_per_issue"]
    )
    collector = get_collector("html")
    queue = list(root_result.followups)
    diagnostics = list(root_result.diagnostics)
    candidates = list(root_result.candidates)
    seen_urls = {normalize_http_url(channel.entry_url)}
    seen_candidates = {_candidate_identity(candidate) for candidate in candidates}
    attempted = 0
    fetched = 0

    while queue and attempted < max_fetches and len(candidates) < max_items:
        followup = queue.pop(0)
        try:
            normalized = normalize_http_url(followup.url)
        except ProbeError as exc:
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    message=exc.message,
                    level="error",
                    locator=followup.url,
                )
            )
            continue
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        attempted += 1
        robots = _check_robots_with_retry(client, followup.url)
        if not robots.allowed:
            session.add(
                CollectionItem(
                    run_id=run.id,
                    request_url=followup.url,
                    normalized_url=normalized,
                    discovered_from_url=str(followup.context.get("discovered_from_url") or channel.entry_url),
                    status="skipped",
                    error_category="policy",
                    error_code=robots.code,
                    error_message="robots policy did not authorize this followup request",
                )
            )
            diagnostics.append(
                Diagnostic(
                    code=robots.code,
                    message="robots policy did not authorize this followup request",
                    level="error",
                    locator=followup.url,
                )
            )
            continue
        try:
            resource = _fetch_with_retry(client.fetch_without_redirects, followup.url)
        except ProbeError as exc:
            session.add(
                CollectionItem(
                    run_id=run.id,
                    request_url=followup.url,
                    normalized_url=normalized,
                    discovered_from_url=str(followup.context.get("discovered_from_url") or channel.entry_url),
                    status="failed",
                    error_category=exc.category,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    message=exc.message,
                    level="error",
                    locator=followup.url,
                )
            )
            continue
        fetched += 1

        session.add(
            CollectionItem(
                run_id=run.id,
                request_url=followup.url,
                normalized_url=normalized,
                discovered_from_url=str(followup.context.get("discovered_from_url") or channel.entry_url),
                status="fetched",
                http_status=resource.status_code,
            )
        )
        actual_type = classify_content(resource)
        if actual_type != "html":
            diagnostics.append(
                Diagnostic(
                    code="followup_type_mismatch",
                    message=f"expected html, received {actual_type}",
                    level="error",
                    locator=followup.url,
                )
            )
            continue

        nested = collector.parse(
            resource,
            channel.collector_config,
            link_role=channel.link_role,
        )
        diagnostics.extend(nested.diagnostics)
        queue.extend(nested.followups)
        for candidate in nested.candidates:
            identity = _candidate_identity(candidate)
            if identity in seen_candidates:
                continue
            seen_candidates.add(identity)
            candidates.append(candidate)
            if len(candidates) >= max_items:
                break

    diagnostics.append(
        Diagnostic(
            code="bounded_followups_fetched",
            message=f"fetched {fetched} of {attempted} bounded CSSN issue pages considered",
            level="info",
        )
    )
    return ParseResult(candidates=tuple(candidates), diagnostics=tuple(diagnostics))


def _fetch_management_world_followups(
    session: Session,
    run: CollectionRun,
    channel: SourceChannel,
    client: SafeHttpClient,
    root_result: ParseResult,
) -> ParseResult:
    max_items = int(channel.collector_config["max_items"])
    max_fetches = int(channel.collector_config["max_issues"]) * 2
    collector = get_collector("html")
    queue = list(root_result.followups)
    diagnostics = list(root_result.diagnostics)
    discovery_candidates: list[CandidateDocument] = []
    official_candidates: list[CandidateDocument] = []
    seen_urls = {normalize_http_url(channel.entry_url)}
    seen_discovery = set()
    seen_official = set()
    attempted = 0
    fetched = 0

    while queue and attempted < max_fetches:
        followup = queue.pop(0)
        try:
            normalized = normalize_http_url(followup.url)
        except ProbeError as exc:
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    message=exc.message,
                    level="error",
                    locator=followup.url,
                )
            )
            continue
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        attempted += 1
        robots = _check_robots_with_retry(client, followup.url)
        if not robots.allowed:
            session.add(
                CollectionItem(
                    run_id=run.id,
                    request_url=followup.url,
                    normalized_url=normalized,
                    discovered_from_url=str(followup.context.get("discovered_from_url") or channel.entry_url),
                    status="skipped",
                    error_category="policy",
                    error_code=robots.code,
                    error_message="robots policy did not authorize this followup request",
                )
            )
            diagnostics.append(
                Diagnostic(
                    code=robots.code,
                    message="robots policy did not authorize this followup request",
                    level="error",
                    locator=followup.url,
                )
            )
            continue
        try:
            resource = _fetch_with_retry(client.fetch_without_redirects, followup.url)
        except ProbeError as exc:
            session.add(
                CollectionItem(
                    run_id=run.id,
                    request_url=followup.url,
                    normalized_url=normalized,
                    discovered_from_url=str(followup.context.get("discovered_from_url") or channel.entry_url),
                    status="failed",
                    error_category=exc.category,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    message=exc.message,
                    level="error",
                    locator=followup.url,
                )
            )
            continue
        fetched += 1
        session.add(
            CollectionItem(
                run_id=run.id,
                request_url=followup.url,
                normalized_url=normalized,
                discovered_from_url=str(followup.context.get("discovered_from_url") or channel.entry_url),
                status="fetched",
                http_status=resource.status_code,
            )
        )
        actual_type = classify_content(resource)
        if actual_type != "html":
            diagnostics.append(
                Diagnostic(
                    code="followup_type_mismatch",
                    message=f"expected html, received {actual_type}",
                    level="error",
                    locator=followup.url,
                )
            )
            continue
        followup_role = str(followup.context.get("link_role") or channel.link_role)
        nested = collector.parse(
            resource,
            channel.collector_config,
            link_role=followup_role,
        )
        diagnostics.extend(nested.diagnostics)
        queue.extend(nested.followups)
        for candidate in nested.candidates:
            identity = _candidate_identity(candidate)
            if followup_role == "official":
                if identity in seen_official or len(official_candidates) >= max_items:
                    continue
                seen_official.add(identity)
                official_candidates.append(candidate)
            else:
                if identity in seen_discovery or len(discovery_candidates) >= max_items:
                    continue
                seen_discovery.add(identity)
                discovery_candidates.append(candidate)

    official_by_title: dict[tuple[str, str], list[CandidateDocument]] = defaultdict(list)
    for candidate in official_candidates:
        if candidate.title and candidate.canonical_url:
            official_by_title[(candidate.issue_text or "", _reviewed_title_key(candidate.title))].append(
                candidate
            )

    matched = 0
    merged: list[CandidateDocument] = []
    for candidate in discovery_candidates:
        if not candidate.title:
            merged.append(candidate)
            continue
        matches = official_by_title.get(
            (candidate.issue_text or "", _reviewed_title_key(candidate.title)),
            [],
        )
        if len(matches) != 1:
            merged.append(candidate)
            continue
        official = matches[0]
        matched += 1
        merged.append(
            replace(
                candidate,
                canonical_url=official.canonical_url,
                external_id=official.external_id,
                source_metadata={
                    **candidate.source_metadata,
                    "detail_verified": True,
                    "official_source": "ncpssd",
                    "official_resolver_url": official.locator,
                    "reader_url": official.source_metadata.get("reader_url"),
                },
            )
        )

    diagnostics.append(
        Diagnostic(
            code="bounded_followups_fetched",
            message=f"fetched {fetched} of {attempted} bounded Management World pages considered",
            level="info",
        )
    )
    diagnostics.append(
        Diagnostic(
            code="official_title_matches",
            message=f"resolved {matched} of {len(discovery_candidates)} discovery records by exact title",
            level="info",
        )
    )
    return ParseResult(candidates=tuple(merged), diagnostics=tuple(diagnostics))


def _reviewed_title_key(value: str) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", value)
        .replace("﹕", ":")
        .replace("：", ":")
        .replace("‐", "-")
        .replace("‑", "-")
        .replace("‒", "-")
        .replace("–", "-")
        .replace("—", "-"),
    )


def _fetch_with_retry(
    fetcher: Callable[[str], FetchedResource],
    url: str,
    *,
    retry_delays: tuple[float, ...] = (0,),
) -> FetchedResource:
    for delay in (0, *retry_delays):
        if delay:
            sleep(delay)
        try:
            return fetcher(url)
        except ProbeError as exc:
            if exc.code == "http_429" or not (exc.retryable and exc.category in {"http", "network"}):
                raise
            last_error = exc
    raise last_error


def _check_robots_with_retry(client: SafeHttpClient, url: str) -> RobotsDecision:
    decision = check_robots(client, url)
    if decision.code in {
        "robots_connection_failed",
        "robots_dns_resolution_failed",
        "robots_timeout",
    }:
        return check_robots(client, url)
    return decision


def _candidate_identity(candidate: CandidateDocument) -> tuple[str, str]:
    if candidate.external_id:
        return ("external_id", candidate.external_id)
    return ("url", candidate.canonical_url or candidate.discovery_url)


def _uses_bounded_pdf_metadata(channel: SourceChannel) -> bool:
    return channel.collector_type == "pdf" and channel.collector_config.get("profile") == "bounded_metadata"


def _uses_cssn_periodical(channel: SourceChannel) -> bool:
    return channel.collector_type == "html" and channel.collector_config.get("mode") == "cssn_periodical"


def _uses_management_world(channel: SourceChannel) -> bool:
    return channel.collector_type == "html" and channel.collector_config.get("mode") == "management_world"


def _uses_world_bank_indicators(channel: SourceChannel) -> bool:
    return (
        channel.collector_type == "api" and channel.collector_config.get("profile") == "world_bank_indicators"
    )


def _uses_oecd_oda(channel: SourceChannel) -> bool:
    return channel.collector_type == "api" and channel.collector_config.get("profile") == "oecd_oda"


def _uses_wits_tariff(channel: SourceChannel) -> bool:
    return channel.collector_type == "api" and channel.collector_config.get("profile") == "wits_tariff"


def _uses_unctad_fdi(channel: SourceChannel) -> bool:
    return channel.collector_type == "api" and channel.collector_config.get("profile") == "unctad_fdi"


def _uses_comtrade_goods(channel: SourceChannel) -> bool:
    return (
        channel.collector_type == "api"
        and channel.collector_config.get("profile") == "un_comtrade_goods_annual_hs"
    )


def _comtrade_review_mode(channel: SourceChannel) -> str | None:
    state = (
        channel.source.status,
        channel.status,
        channel.collector_config.get("automation_status"),
    )
    if state == ("paused", "blocked", "blocked"):
        return "explicit_blocked_channel"
    if state == ("active", "shadow", "metadata_only"):
        return "catalog_shadow_channel"
    return None


def _document_type(channel: SourceChannel) -> str:
    configured = channel.collector_config.get("document_type")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return {
        "api": "dataset_record",
        "html": "web_material",
        "pdf": "report",
        "rss": "article",
    }[channel.collector_type]


def _add_candidate_item(
    session: Session,
    run: CollectionRun,
    candidate: CandidateDocument,
    seen_urls: set[str],
    *,
    discovered_from_url: str,
) -> None:
    try:
        normalized = normalize_http_url(candidate.canonical_url or candidate.discovery_url)
    except ProbeError:
        return
    if normalized in seen_urls:
        return
    seen_urls.add(normalized)
    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=candidate.discovery_url,
            normalized_url=normalized,
            discovered_from_url=discovered_from_url,
            status="discovered",
        )
    )


def _add_skipped_item(
    session: Session,
    run: CollectionRun,
    url: str,
    robots: RobotsDecision,
) -> None:
    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=url,
            normalized_url=normalize_http_url(url),
            status="skipped",
            error_category="policy",
            error_code=robots.code,
            error_message="robots policy did not authorize this automatic request",
        )
    )


def _add_failed_item(session: Session, run: CollectionRun, url: str, error: ProbeError) -> None:
    if error.code == "http_429":
        run.report = {**(run.report or {}), "retry_after": error.retry_after}
    session.add(
        CollectionItem(
            run_id=run.id,
            request_url=url,
            normalized_url=normalize_http_url(url),
            status="failed",
            error_category=error.category,
            error_code=error.code,
            error_message=error.message,
        )
    )


def _finish_failed(
    session: Session,
    channel: SourceChannel,
    run: CollectionRun,
    *,
    category: str,
    code: str,
    message: str,
    extra_report: dict[str, Any] | None = None,
) -> ChannelOutcome:
    now = datetime.now(UTC)
    run.status = "failed"
    run.finished_at = now
    run.heartbeat_at = now
    run.error_category = category
    run.error_code = code
    run.error_message = message
    run.report = {**run.report, **(extra_report or {})}
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def _record_internal_failure(
    session: Session,
    channel_id: int,
    exc: Exception,
    *,
    trigger_kind: str = "manual",
) -> ChannelOutcome:
    _validate_trigger_kind(trigger_kind)
    channel = session.scalar(
        select(SourceChannel).where(SourceChannel.id == channel_id).options(joinedload(SourceChannel.source))
    )
    if channel is None:
        raise ValueError(f"source channel {channel_id} does not exist")
    now = datetime.now(UTC)
    run = session.scalar(
        select(CollectionRun)
        .where(
            CollectionRun.channel_id == channel.id,
            CollectionRun.status.in_(("queued", "running")),
        )
        .order_by(CollectionRun.id.desc())
    )
    if run is None:
        run = CollectionRun(
            channel_id=channel.id,
            trigger_kind=trigger_kind,
            started_at=now,
        )
        session.add(run)
    run.status = "failed"
    run.heartbeat_at = now
    run.finished_at = now
    run.error_category = "internal"
    run.error_code = "unhandled_collection_error"
    run.error_message = str(exc)[:1000]
    run.report = {**(run.report or {}), "exception_type": type(exc).__name__}
    session.commit()
    session.refresh(run)
    return _outcome(channel, run)


def _record_internal_failure_with_reconnect(
    session: Session,
    channel_id: int,
    exc: Exception,
    *,
    trigger_kind: str,
) -> ChannelOutcome:
    last_error: OperationalError | None = None
    for _attempt in range(3):
        try:
            return _record_internal_failure(
                session,
                channel_id,
                exc,
                trigger_kind=trigger_kind,
            )
        except OperationalError as connection_error:
            last_error = connection_error
            session.invalidate()
    if last_error is not None:
        raise last_error
    raise RuntimeError("internal collection failure could not be recorded")


def _get_channel(session: Session, channel_id: int) -> SourceChannel:
    channel = session.get(SourceChannel, channel_id)
    if channel is None:
        raise ValueError(f"source channel {channel_id} does not exist")
    return channel


def _validate_trigger_kind(trigger_kind: str) -> None:
    if trigger_kind not in {"manual", "scheduled", "probe", "retry"}:
        raise ValueError(f"unsupported collection trigger kind: {trigger_kind}")


def _catalog_row(channel: SourceChannel) -> int | None:
    value = channel.collector_config.get("catalog_excel_row")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _robots_report(decision: RobotsDecision) -> dict[str, Any]:
    return {
        "state": decision.state,
        "allowed": decision.allowed,
        "code": decision.code,
        "url": decision.robots_url,
    }


def _outcome(channel: SourceChannel, run: CollectionRun) -> ChannelOutcome:
    return ChannelOutcome(
        excel_row=_catalog_row(channel) or 0,
        source_name=channel.source.name,
        channel_id=channel.id,
        channel_name=channel.name,
        run_id=run.id,
        status=run.status,
        error_category=run.error_category,
        error_code=run.error_code,
        items_discovered=run.items_discovered,
        items_persisted=run.items_persisted,
    )


def _outcome_status(outcome: ChannelOutcome) -> str:
    if outcome.error_code in {
        "catalog_blocked",
        "channel_not_runnable",
        "source_not_runnable",
    }:
        return "blocked"
    return outcome.status


def _source_status(outcomes: list[ChannelOutcome]) -> str:
    states = {_outcome_status(outcome) for outcome in outcomes}
    if states == {"blocked"}:
        return "blocked"
    if states == {"succeeded"}:
        return "succeeded"
    if "succeeded" in states or "partial" in states:
        return "partial"
    return "failed"


def _terms_disallow_collection(terms_state: str) -> bool:
    return terms_state.strip().casefold() in {
        "blocked",
        "denied",
        "disallowed",
        "forbidden",
        "不允许",
        "禁止",
    }


def _fetch_auto_list_jpaas_followups(
    session: Session,
    run: CollectionRun,
    channel: SourceChannel,
    client: SafeHttpClient,
    root_result: ParseResult,
) -> ParseResult:
    collector = get_collector("html")
    queue = list(root_result.followups)
    diagnostics = list(root_result.diagnostics)
    candidates = list(root_result.candidates)
    followups: list[FollowupRequest] = []

    root_host = (urlsplit(channel.entry_url).hostname or "").casefold()
    for followup in queue:
        followup_parts = urlsplit(followup.url)
        if (
            followup.context.get("followup_profile") != "jpaas_unitbuild"
            or (followup_parts.hostname or "").casefold() != root_host
            or "/api-gateway/jpaas-publish-server/" not in followup_parts.path
        ):
            followups.append(followup)
            continue
        discovered_from_url = str(followup.context.get("discovered_from_url") or channel.entry_url)
        try:
            normalized = normalize_http_url(followup.url)
            robots = _check_robots_with_retry(client, followup.url)
            if not robots.allowed:
                _add_skipped_item(session, run, followup.url, robots)
                diagnostics.append(
                    Diagnostic(
                        code=robots.code,
                        message="robots policy did not authorize this JPAAS request",
                        level="error",
                        locator=followup.url,
                    )
                )
                continue
            resource = _fetch_with_retry(client.fetch, followup.url)
            session.add(
                CollectionItem(
                    run_id=run.id,
                    request_url=followup.url,
                    normalized_url=normalized,
                    discovered_from_url=discovered_from_url,
                    status="fetched",
                    http_status=resource.status_code,
                )
            )
            api_result = collector.parse(resource, channel.collector_config, link_role=channel.link_role)
            candidates.extend(api_result.candidates)
            followups.extend(api_result.followups)
            diagnostics.extend(api_result.diagnostics)
        except ProbeError as exc:
            _add_failed_item(session, run, followup.url, exc)
            diagnostics.append(
                Diagnostic(
                    code=exc.code,
                    message=exc.message,
                    level="error",
                    locator=followup.url,
                )
            )

    return ParseResult(
        candidates=tuple(candidates), followups=tuple(followups), diagnostics=tuple(diagnostics)
    )
