from __future__ import annotations

import json
import posixpath
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from selectolax.parser import HTMLParser
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import FetchedResource, clean_text
from app.collectors.json_api import crossref_cc_abstract, crossref_direct_abstract
from app.models import (
    Document,
    DocumentAbstractState,
    DocumentVersion,
    Source,
    SourceChannel,
    SourceChannelPolicy,
)
from app.services.document_store import persist_official_abstract, remove_official_abstract
from app.services.robots_policy import check_robots
from app.services.source_probe import ProbeError, SafeHttpClient, classify_content

_RETRY_DELAY = timedelta(days=1)
_UNAVAILABLE_RECHECK_DELAY = timedelta(days=30)
_GENERIC_DESCRIPTION_FRAGMENTS = (
    "international energy agency works with countries around the world",
    "the eiti is the global standard for the good governance",
    "an official website of the united states government",
    "国家哲学社会科学文献中心",
)
_NCPSD_HOST = "www.ncpssd.cn"
_NCPSD_ARTICLE_API_URL = "https://www.ncpssd.cn/articleinfoHandler/getjournalarticletable"
_NCPSD_ARTICLE_LINK = re.compile(
    r"/Literature/(?:secure/)?articleinfo\?[^'\"]*\bid=([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_NCPSD_GCH_BY_ROW = {
    14: "95645X",
    16: "92442X",
    17: "95499X",
    18: "93800A",
}
_ISSUE_LABEL = re.compile(r"(?P<year>20\d{2})\s*年\s*第\s*(?P<issue>\d{1,2})\s*期")
_STALE_SCOPE_BLOCK_REASONS = {
    "policy_not_reviewed",
    "no_reviewed_official_url",
    "outside_reviewed_url_scope",
}
_USGS_PROFILES_REQUIRING_DETAIL_REVALIDATION = {
    "usgs_nmic_news",
    "usgs_nmic_publications",
}


@dataclass(frozen=True, slots=True)
class AbstractBackfillProfile:
    name: str
    hosts_and_prefixes: tuple[tuple[str, str], ...]
    visible_summary_selectors: tuple[str, ...] = ()
    allow_query: bool = False

    def accepts(self, url: str) -> bool:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.fragment
            or (parsed.query and not self.allow_query)
            or not _is_canonical_backfill_path(parsed.path)
        ):
            return False
        host = (parsed.hostname or "").casefold()
        return any(
            host == allowed_host and _path_is_within_prefix(parsed.path, path_prefix)
            for allowed_host, path_prefix in self.hosts_and_prefixes
        )


def _is_canonical_backfill_path(path: str) -> bool:
    if not path.startswith("/") or "%" in path or "\\" in path or "//" in path:
        return False
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    normalized = posixpath.normpath(path)
    if path.endswith("/") and normalized != "/":
        normalized += "/"
    return normalized == path


def _path_is_within_prefix(path: str, path_prefix: str) -> bool:
    boundary = path_prefix.rstrip("/")
    return path == boundary or path.startswith(f"{boundary}/")


_BACKFILL_PROFILES = {
    "eiti_country_reports": AbstractBackfillProfile(
        name="eiti_country_reports",
        hosts_and_prefixes=(("eiti.org", "/documents/"),),
    ),
    "iea_critical_minerals": AbstractBackfillProfile(
        name="iea_critical_minerals",
        hosts_and_prefixes=(
            ("www.iea.org", "/reports/"),
            ("www.iea.org", "/news/"),
            ("www.iea.org", "/commentaries/"),
            ("www.iea.org", "/events/"),
        ),
    ),
    "iea_data_product": AbstractBackfillProfile(
        name="iea_data_product",
        hosts_and_prefixes=(("www.iea.org", "/data-and-statistics/data-product/critical-minerals-dataset"),),
        visible_summary_selectors=(".o-hero-data-product__subtitle",),
    ),
    "iss_africa_report": AbstractBackfillProfile(
        name="iss_africa_report",
        hosts_and_prefixes=(("issafrica.org", "/research/africa-report/"),),
    ),
    "world_bank_metals": AbstractBackfillProfile(
        name="world_bank_metals",
        hosts_and_prefixes=(
            ("www.worldbank.org", "/en/news/"),
            ("blogs.worldbank.org", "/en/"),
        ),
    ),
    "usgs_nmic_news": AbstractBackfillProfile(
        name="usgs_nmic_news",
        hosts_and_prefixes=(
            ("www.usgs.gov", "/news/"),
            ("www.usgs.gov", "/programs/mineral-resources-program/news/"),
        ),
        visible_summary_selectors=(
            ".field--name-field-intro .field-intro",
            ".field-intro",
        ),
    ),
    "usgs_nmic_publications": AbstractBackfillProfile(
        name="usgs_nmic_publications",
        hosts_and_prefixes=(("www.usgs.gov", "/publications/"),),
        visible_summary_selectors=(
            ".field--name-field-intro .field-intro",
            ".field-intro",
        ),
    ),
    "erj_ajcass": AbstractBackfillProfile(
        name="erj_ajcass",
        hosts_and_prefixes=(("erj.ajcass.com", "/"),),
        visible_summary_selectors=(".abstract-text", ".summary-content", ".abstract"),
        allow_query=True,
    ),
    "cswe_cssn": AbstractBackfillProfile(
        name="cswe_cssn",
        hosts_and_prefixes=(("cswe.cssn.cn", "/"), ("sjjj.magtech.com.cn", "/")),
        visible_summary_selectors=(".abstract", ".article-abstract"),
        allow_query=True,
    ),
    "iwep_cssn": AbstractBackfillProfile(
        name="iwep_cssn",
        hosts_and_prefixes=(("iwep.cssn.cn", "/"), ("ejournaliwep.cssn.cn", "/")),
        visible_summary_selectors=(".abstract", ".article-abstract"),
        allow_query=True,
    ),
    "mworld_org": AbstractBackfillProfile(
        name="mworld_org",
        hosts_and_prefixes=(("www.mworld.org.cn", "/"), ("www.macrodatas.cn", "/"), ("www.ncpssd.cn", "/")),
        visible_summary_selectors=(".abstract", ".article-abstract", ".info-abstract"),
        allow_query=True,
    ),
    "ciejournal_ajcass": AbstractBackfillProfile(
        name="ciejournal_ajcass",
        hosts_and_prefixes=(("ciejournal.ajcass.com", "/"),),
        visible_summary_selectors=(".abstract-text", ".summary-content", ".abstract"),
        allow_query=True,
    ),
}


@dataclass(frozen=True, slots=True)
class AbstractBackfillReport:
    total_documents: int
    current_documents_with_abstract: int
    states_initialized: int
    attempted: int
    versions_created: int
    status_counts: dict[str, int]
    reason_counts: dict[str, int]
    source_coverage: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_documents": self.total_documents,
            "current_documents_with_abstract": self.current_documents_with_abstract,
            "states_initialized": self.states_initialized,
            "attempted": self.attempted,
            "versions_created": self.versions_created,
            "status_counts": self.status_counts,
            "reason_counts": self.reason_counts,
            "source_coverage": list(self.source_coverage),
        }


def backfill_reviewed_abstracts(
    session: Session,
    *,
    client: SafeHttpClient,
    limit: int = 20,
    rows: tuple[int, ...] | None = None,
    retry_due: bool = False,
    recheck_retryable: bool = False,
    recheck_unavailable: bool = False,
    recheck_blocked: bool = False,
    now: datetime | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    commit_every: int | None = None,
    skip_checked_since: datetime | None = None,
) -> AbstractBackfillReport:
    if isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("abstract backfill limit must be between 1 and 500")
    if commit_every is not None and (isinstance(commit_every, bool) or not 1 <= commit_every <= limit):
        raise ValueError("abstract backfill commit interval must be between 1 and limit")
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("abstract backfill time must be timezone-aware")
    if skip_checked_since is not None and skip_checked_since.tzinfo is None:
        raise ValueError("abstract backfill resume time must be timezone-aware")
    session.flush()
    selected_rows = set(rows) if rows is not None else None

    selected_source_ids: set[int] | None = None
    if selected_rows is not None:
        selected_source_ids = {
            source_id
            for source_id, collector_config in session.execute(
                select(SourceChannel.source_id, SourceChannel.collector_config)
            )
            if collector_config.get("catalog_excel_row") in selected_rows
        }

    channels = list(
        session.scalars(
            select(SourceChannel)
            .join(Source, Source.id == SourceChannel.source_id)
            .join(SourceChannelPolicy, SourceChannelPolicy.channel_id == SourceChannel.id)
            .where(
                Source.status == "active",
                SourceChannel.status.in_(("shadow", "active")),
                SourceChannelPolicy.terms_state == "allowed_with_conditions",
                SourceChannelPolicy.terms_url.is_not(None),
                SourceChannelPolicy.storage_scope == "official_abstract",
                SourceChannelPolicy.rag_scope == "none",
                SourceChannelPolicy.reviewed_at.is_not(None),
            )
            .options(selectinload(SourceChannel.policy))
            .order_by(SourceChannel.id)
        )
    )
    channels_by_source: dict[int, list[SourceChannel]] = {}
    for channel in channels:
        if channel.collector_config.get("automation_status") == "blocked":
            continue
        channels_by_source.setdefault(channel.source_id, []).append(channel)

    document_versions = list(
        session.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                and_(
                    DocumentVersion.document_id == Document.id,
                    DocumentVersion.version_no == Document.latest_version_no,
                ),
            )
            .order_by(Document.id)
        )
    )
    states = {state.document_id: state for state in session.scalars(select(DocumentAbstractState))}
    initialized = 0
    attempted = 0
    versions_created = 0
    ncpssd_issue_cache: dict[str, tuple[tuple[str, str], ...]] = {}
    ncpssd_api_robots = None

    for document, current in document_versions:
        if selected_source_ids is not None and document.source_id not in selected_source_ids:
            continue
        state = states.get(document.id)
        if state is None:
            state = DocumentAbstractState(
                document_id=document.id,
                status="pending",
                attempt_count=0,
            )
            session.add(state)
            states[document.id] = state
            initialized += 1
        if _at_or_after(state.last_checked_at, skip_checked_since):
            continue

        source_channels = channels_by_source.get(document.source_id, [])
        target_url = document.canonical_url or document.discovery_url
        matched = _matched_channel(source_channels, target_url, doi=document.doi)
        if matched is None and current.abstract:
            matched = _provenance_channel(source_channels, current)
        any_reviewed_channel = source_channels[0] if source_channels else None
        needs_revalidation = bool(
            current.abstract and matched is not None and _abstract_needs_revalidation(current, matched)
        )

        if current.abstract and (matched is None or _provenance_channel_policy_revoked(session, current)):
            removal = remove_official_abstract(
                session,
                document,
                reason="abstract_policy_no_longer_matches",
                seen_at=checked_at,
            )
            versions_created += int(removal.version_created)
            _block(
                state,
                "policy_not_reviewed" if not source_channels else "outside_reviewed_url_scope",
                checked_at,
            )
            continue
        if current.abstract and not needs_revalidation:
            state.channel_id = (
                (matched or any_reviewed_channel).id if (matched or any_reviewed_channel) else None
            )
            state.status = "stored"
            state.reason_code = None
            state.next_retry_at = None
            continue
        if not source_channels:
            _block(state, "policy_not_reviewed", checked_at)
            continue
        if target_url is None and (
            matched is None or not (_is_crossref_channel(matched) or _is_ncpssd_backfill_channel(matched))
        ):
            state.channel_id = any_reviewed_channel.id if any_reviewed_channel else None
            _block(state, "no_reviewed_official_url", checked_at)
            continue
        if matched is None:
            state.channel_id = any_reviewed_channel.id if any_reviewed_channel else None
            if any(_profile_for_channel(channel) is not None for channel in source_channels):
                _block(state, "outside_reviewed_url_scope", checked_at)
            else:
                _unavailable(state, "not_in_current_feed_window", checked_at)
            continue

        state.channel_id = matched.id
        if state.status == "blocked" and state.reason_code in _STALE_SCOPE_BLOCK_REASONS:
            state.status = "pending"
            state.reason_code = None
            state.next_retry_at = None
        if needs_revalidation:
            removal = remove_official_abstract(
                session,
                document,
                reason=_abstract_revalidation_reason(matched),
                seen_at=checked_at,
            )
            versions_created += int(removal.version_created)
            if removal.version is not None:
                current = removal.version
        if state.status == "blocked" and not recheck_blocked:
            continue
        if state.status == "unavailable":
            if not recheck_unavailable and (
                not retry_due or _next_retry_is_future(state.next_retry_at, checked_at)
            ):
                continue
        if state.status == "retryable":
            if not recheck_retryable and (
                not retry_due or _next_retry_is_future(state.next_retry_at, checked_at)
            ):
                continue
        if attempted >= limit:
            if state.status not in {"retryable", "unavailable", "blocked"}:
                state.status = "pending"
                state.reason_code = None
            continue

        if commit_every is not None and attempted > 0 and attempted % commit_every == 0:
            session.commit()
        attempted += 1
        if progress_callback is not None:
            progress_callback(attempted, limit)
        state.attempt_count += 1
        state.last_checked_at = checked_at
        profile = _profile_for_channel(matched)
        is_crossref = _is_crossref_channel(matched)
        is_ncpssd = _is_ncpssd_backfill_channel(matched)
        assert profile is not None or is_crossref or is_ncpssd
        article_url: str | None = None
        article_id: str | None = None
        if is_ncpssd:
            try:
                article_id, article_url = _resolve_ncpssd_article(
                    client,
                    matched,
                    target_url=target_url,
                    issue_text=current.issue_date_text,
                    title=current.title,
                    issue_cache=ncpssd_issue_cache,
                )
            except ProbeError as exc:
                _record_probe_error(state, exc, checked_at)
                continue
            if article_id is None or article_url is None:
                _unavailable(state, "platform_article_not_indexed", checked_at)
                continue
        request_url = (
            _NCPSD_ARTICLE_API_URL
            if is_ncpssd
            else (_crossref_work_url(document.doi) if is_crossref else target_url)
        )
        assert request_url is not None

        robots = (
            ncpssd_api_robots
            if is_ncpssd and ncpssd_api_robots is not None
            else check_robots(client, request_url)
        )
        if is_ncpssd and ncpssd_api_robots is None:
            ncpssd_api_robots = robots
        if not robots.allowed:
            if robots.state == "disallowed":
                _block(state, "robots_disallowed", checked_at)
            else:
                _retry(state, "robots_unavailable", checked_at)
            continue
        try:
            resource = (
                client.fetch_ncpssd_article_metadata(article_id)
                if is_ncpssd and article_id is not None
                else client.fetch_without_redirects(request_url)
            )
        except ProbeError as exc:
            _record_probe_error(state, exc, checked_at)
            continue
        abstract_metadata: dict[str, str] = {
            "abstract_fetch_policy_version": ("ncpssd-form-post-v1" if is_ncpssd else "no_redirects-v1")
        }
        if is_crossref:
            gate = str(matched.collector_config.get("abstract_license_gate") or "source_provided")
            summary, license_metadata, reason, retry_at = _extract_crossref_abstract(
                resource,
                expected_doi=document.doi,
                license_gate=gate,
            )
            abstract_metadata.update(license_metadata)
            if reason is not None:
                if reason == "license_not_yet_active" and retry_at is not None:
                    _retry_at(state, reason, checked_at, retry_at)
                elif reason == "abstract_missing_or_unlicensed":
                    _unavailable(state, reason, checked_at)
                else:
                    _block(state, reason, checked_at)
                continue
            summary_kind = "api_abstract"
        elif is_ncpssd:
            assert article_id is not None and article_url is not None
            summary, reason = _extract_ncpssd_abstract(
                resource,
                expected_article_id=article_id,
                expected_title=current.title,
            )
            if reason is not None:
                if reason == "source_abstract_missing":
                    _unavailable(state, reason, checked_at)
                else:
                    _block(state, reason, checked_at)
                continue
            abstract_metadata.update(
                {
                    "abstract_platform": "ncpssd",
                    "abstract_platform_article_id": article_id,
                    "abstract_platform_api_url": resource.final_url,
                }
            )
            summary_kind = "api_abstract"
        else:
            assert profile is not None
            if classify_content(resource) != "html" or not profile.accepts(resource.final_url):
                _block(state, "mime_or_url_rejected", checked_at)
                continue
            summary = _extract_html_summary(resource, profile, title=current.title)
            summary_kind = "html_summary"
        if summary is None:
            _unavailable(state, "source_abstract_missing", checked_at)
            continue

        policy = matched.policy
        if (
            policy is None
            or policy.terms_state != "allowed_with_conditions"
            or policy.terms_url is None
            or policy.storage_scope != "official_abstract"
            or policy.rag_scope != "none"
            or policy.reviewed_at is None
        ):
            _block(state, "policy_not_reviewed", checked_at)
            continue
        outcome = persist_official_abstract(
            session,
            document,
            summary,
            summary_kind=summary_kind,
            summary_source_url=article_url or resource.final_url,
            channel_id=matched.id,
            terms_url=policy.terms_url,
            reviewed_at=policy.reviewed_at,
            seen_at=resource.fetched_at,
            license_metadata=abstract_metadata,
        )
        versions_created += int(outcome.version_created)
        state.status = "stored"
        state.reason_code = None
        state.next_retry_at = None

    session.flush()
    document_statuses = {
        document.id: states[document.id].status if document.id in states else "pending"
        for document, _current in document_versions
    }
    status_counts = Counter(document_statuses.values())
    reason_counts = Counter(state.reason_code for state in states.values() if state.reason_code is not None)
    current_with_abstract = (
        session.scalar(
            select(func.count(Document.id))
            .join(
                DocumentVersion,
                and_(
                    DocumentVersion.document_id == Document.id,
                    DocumentVersion.version_no == Document.latest_version_no,
                ),
            )
            .where(DocumentVersion.abstract.is_not(None))
        )
        or 0
    )
    source_names = dict(session.execute(select(Source.id, Source.name)).all())
    coverage: dict[int, Counter[str]] = {}
    for document, _current in document_versions:
        counter = coverage.setdefault(document.source_id, Counter())
        counter["total"] += 1
        counter[document_statuses[document.id]] += 1
    source_coverage = tuple(
        {
            "source_id": source_id,
            "source_name": source_names[source_id],
            **dict(sorted(counter.items())),
        }
        for source_id, counter in sorted(
            coverage.items(),
            key=lambda item: source_names[item[0]].casefold(),
        )
    )
    return AbstractBackfillReport(
        total_documents=len(document_versions),
        current_documents_with_abstract=current_with_abstract,
        states_initialized=initialized,
        attempted=attempted,
        versions_created=versions_created,
        status_counts=dict(sorted(status_counts.items())),
        reason_counts=dict(sorted(reason_counts.items())),
        source_coverage=source_coverage,
    )


def _matched_channel(
    channels: list[SourceChannel],
    target_url: str | None,
    *,
    doi: str | None,
) -> SourceChannel | None:
    for channel in channels:
        if _is_ncpssd_backfill_channel(channel):
            return channel
    if target_url is not None:
        for channel in channels:
            profile = _profile_for_channel(channel)
            if profile is not None and profile.accepts(target_url):
                return channel
    if doi is not None:
        for channel in channels:
            if _is_crossref_channel(channel):
                return channel
    return None


_ROW_PROFILES = {
    14: "erj_ajcass",
    15: "cswe_cssn",
    16: "iwep_cssn",
    17: "mworld_org",
    18: "ciejournal_ajcass",
}


def _profile_for_channel(channel: SourceChannel) -> AbstractBackfillProfile | None:
    row = channel.collector_config.get("catalog_excel_row")
    if isinstance(row, int) and row in _ROW_PROFILES:
        return _BACKFILL_PROFILES.get(_ROW_PROFILES[row])
    profile_name = channel.collector_config.get("profile")
    if not profile_name or not isinstance(profile_name, str) or profile_name not in _BACKFILL_PROFILES:
        profile_name = channel.collector_config.get("mode")
    return _BACKFILL_PROFILES.get(profile_name) if isinstance(profile_name, str) else None


def _provenance_channel(
    channels: list[SourceChannel],
    current: DocumentVersion,
) -> SourceChannel | None:
    provenance = current.source_metadata.get("abstract_provenance")
    if not isinstance(provenance, Mapping):
        return None
    provenance_channel_id = provenance.get("channel_id")
    source_url = provenance.get("source_url")
    for channel in channels:
        if provenance_channel_id == channel.id or source_url == channel.entry_url:
            return channel
    return None


def _is_crossref_channel(channel: SourceChannel) -> bool:
    parsed = urlsplit(channel.entry_url)
    return (
        channel.collector_type == "api"
        and parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == "api.crossref.org"
        and parsed.port in {None, 443}
        and parsed.path.rstrip("/").endswith("/works")
        and channel.collector_config.get("abstract_license_gate") in {"creative_commons", "source_provided"}
    )


def _is_ncpssd_backfill_channel(channel: SourceChannel) -> bool:
    row = channel.collector_config.get("catalog_excel_row")
    return (
        isinstance(row, int)
        and row in _NCPSD_GCH_BY_ROW
        and channel.collector_config.get("ncpssd_gch") == _NCPSD_GCH_BY_ROW[row]
    )


def _crossref_work_url(doi: str | None) -> str | None:
    return f"https://api.crossref.org/works/{quote(doi, safe='')}" if doi else None


def _provenance_channel_policy_revoked(
    session: Session,
    current: DocumentVersion,
) -> bool:
    provenance = current.source_metadata.get("abstract_provenance")
    if not isinstance(provenance, Mapping):
        return False
    provenance_channel_id = provenance.get("channel_id")
    if provenance_channel_id is not None:
        policy = session.scalar(
            select(SourceChannelPolicy).where(SourceChannelPolicy.channel_id == provenance_channel_id)
        )
        if policy is not None and (policy.terms_state == "disallowed" or policy.storage_scope == "metadata"):
            return True
    return False


def _abstract_needs_revalidation(current: DocumentVersion, channel: SourceChannel) -> bool:
    provenance = current.source_metadata.get("abstract_provenance")
    if not isinstance(provenance, Mapping):
        return True
    if _is_crossref_channel(channel):
        if channel.collector_config.get("abstract_license_gate") == "creative_commons":
            return provenance.get("license_content_version") != "vor"
        return provenance.get("fetch_policy_version") != "crossref-direct-v1"
    if _is_ncpssd_backfill_channel(channel):
        return provenance.get("fetch_policy_version") != "ncpssd-form-post-v1"
    profile = _profile_for_channel(channel)
    if profile is None:
        return False
    source_url = provenance.get("source_url")
    if profile.name in _USGS_PROFILES_REQUIRING_DETAIL_REVALIDATION and source_url == channel.entry_url:
        return True
    return bool(
        provenance.get("content_kind") == "html_summary"
        and source_url != channel.entry_url
        and provenance.get("fetch_policy_version") != "no_redirects-v1"
    )


def _abstract_revalidation_reason(channel: SourceChannel) -> str:
    if _is_crossref_channel(channel):
        if channel.collector_config.get("abstract_license_gate") == "creative_commons":
            return "crossref_license_gate_strengthened"
        return "crossref_abstract_policy_changed"
    if _is_ncpssd_backfill_channel(channel):
        return "ncpssd_identity_gate_strengthened"
    return "html_summary_selector_strengthened"


def _resolve_ncpssd_article(
    client: SafeHttpClient,
    channel: SourceChannel,
    *,
    target_url: str | None,
    issue_text: str | None,
    title: str,
    issue_cache: dict[str, tuple[tuple[str, str], ...]],
) -> tuple[str | None, str | None]:
    direct_id = _ncpssd_article_id(target_url)
    if direct_id is not None:
        return direct_id, _ncpssd_article_url(direct_id)

    match = _ISSUE_LABEL.search(issue_text or "")
    gch = channel.collector_config.get("ncpssd_gch")
    if match is None or not isinstance(gch, str):
        return None, None
    issue = int(match.group("issue"))
    if not 1 <= issue <= 12:
        return None, None
    issue_url = (
        f"https://{_NCPSD_HOST}/journal/details?gch={gch}&langType=1&nav=1"
        f"&years={match.group('year')}&num={issue}"
    )
    articles = issue_cache.get(issue_url)
    if articles is None:
        robots = check_robots(client, issue_url)
        if not robots.allowed:
            raise ProbeError(
                "policy" if robots.state == "disallowed" else "network",
                "robots_disallowed" if robots.state == "disallowed" else "robots_unavailable",
                "robots policy did not authorize the NCPSD issue request",
                retryable=robots.state != "disallowed",
            )
        resource = client.fetch_without_redirects(issue_url)
        if classify_content(resource) != "html" or resource.final_url != issue_url:
            raise ProbeError(
                "policy",
                "ncpssd_issue_rejected",
                "NCPSD issue response did not match the reviewed URL and MIME",
            )
        articles = _ncpssd_issue_articles(resource)
        issue_cache[issue_url] = articles
    matches = [
        (article_id, article_title)
        for article_id, article_title in articles
        if _reviewed_title_matches(title, article_title)
    ]
    if len(matches) != 1:
        return None, None
    article_id, _article_title = matches[0]
    return article_id, _ncpssd_article_url(article_id)


def _ncpssd_article_id(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != _NCPSD_HOST
        or parsed.port not in {None, 443}
        or parsed.path not in {"/Literature/articleinfo", "/Literature/secure/articleinfo"}
    ):
        return None
    values = parse_qs(parsed.query).get("id", [])
    if len(values) != 1:
        return None
    article_id = values[0]
    return article_id if article_id.isascii() and article_id.isalnum() else None


def _ncpssd_article_url(article_id: str) -> str:
    return f"https://{_NCPSD_HOST}/Literature/articleinfo?id={article_id}&type=journalArticle"


def _ncpssd_issue_articles(resource: FetchedResource) -> tuple[tuple[str, str], ...]:
    tree = HTMLParser(resource.text())
    articles: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in tree.css("a"):
        # NCPSD 期号页改版后文章链接放在 onclick 的 openDetail(...) 内，
        # href 只是 javascript:void (0)；兼容两种形态，onclick 优先。
        match = None
        for attribute in ("onclick", "href"):
            raw_target = unescape(node.attributes.get(attribute) or "")
            match = _NCPSD_ARTICLE_LINK.search(raw_target)
            if match is not None:
                break
        if match is None:
            continue
        article_id = match.group(1)
        title = clean_text(node.attributes.get("title")) or clean_text(node.text(separator=" ", strip=True))
        if title is None or article_id in seen:
            continue
        seen.add(article_id)
        articles.append((article_id, title))
    return tuple(articles)


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


def _reviewed_title_matches(expected: str, response: str) -> bool:
    """Return whether a reviewed title is compatible with the NCPSD platform title.

    平台标题可能比库内标题更完整：库内标题可能带平台不展示的专题前缀
    （以“丨”分隔），或被来源页面截断（尾部多个英文句点）。只有精确、
    去专题前缀或“以句点结尾的截断前缀”三种形态才接受；期号页匹配的
    唯一性由调用方约束。
    """

    expected_key = _reviewed_title_key(expected)
    response_key = _reviewed_title_key(response)
    if expected_key == response_key:
        return True
    section_marker = "丨"
    if section_marker in expected:
        segment = _reviewed_title_key(expected.split(section_marker)[-1])
        if segment and segment == response_key:
            return True
    stripped = expected_key.rstrip(".")
    if expected_key.endswith(".") and len(stripped) >= 10 and response_key.startswith(stripped):
        return True
    return False


def _extract_ncpssd_abstract(
    resource: FetchedResource,
    *,
    expected_article_id: str,
    expected_title: str,
) -> tuple[str | None, str | None]:
    if classify_content(resource) != "api" or resource.final_url != _NCPSD_ARTICLE_API_URL:
        return None, "mime_or_url_rejected"
    try:
        payload = json.loads(resource.text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "parse_failed"
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return None, "parse_failed"
    response_id = clean_text(data.get("lngid"))
    if response_id is not None and response_id != expected_article_id:
        return None, "identity_mismatch"
    response_title = clean_text(data.get("titlec") or data.get("title_c") or data.get("title"))
    if response_title is not None and not _reviewed_title_matches(expected_title, response_title):
        return None, "identity_mismatch"
    summary = clean_text(data.get("remarkc"))
    if summary is None or not 20 <= len(summary) <= 4000:
        return None, "source_abstract_missing"
    return summary, None


def _extract_crossref_abstract(
    resource: FetchedResource,
    *,
    expected_doi: str | None,
    license_gate: str = "source_provided",
) -> tuple[str | None, dict[str, str], str | None, datetime | None]:
    if classify_content(resource) != "api":
        return None, {}, "mime_or_url_rejected", None
    try:
        payload = json.loads(resource.text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {}, "parse_failed", None
    message = payload.get("message") if isinstance(payload, Mapping) else None
    if not isinstance(message, Mapping):
        return None, {}, "parse_failed", None
    response_doi = clean_text(message.get("DOI"))
    if expected_doi is None or (response_doi or "").casefold() != expected_doi.casefold():
        return None, {}, "identity_mismatch", None
    if license_gate == "creative_commons":
        summary, license_metadata, next_license_start = crossref_cc_abstract(
            message,
            as_of=resource.fetched_at,
        )
        if summary is None:
            if next_license_start is not None:
                return None, {}, "license_not_yet_active", next_license_start
            return None, {}, "abstract_missing_or_unlicensed", None
        return summary, license_metadata, None, None
    # source_provided 门控：用户确认（2026-08-04）后不再要求逐条 CC VoR 许可，
    # 仍只保存 Crossref 显式返回的 abstract 字段。
    summary = crossref_direct_abstract(message)
    if summary is None:
        return None, {}, "abstract_missing_or_unlicensed", None
    return summary, {"abstract_fetch_policy_version": "crossref-direct-v1"}, None, None


def _extract_html_summary(
    resource: FetchedResource,
    profile: AbstractBackfillProfile,
    *,
    title: str,
) -> str | None:
    tree = HTMLParser(resource.text())
    candidates: list[str | None] = []
    for selector in (
        'meta[name="citation_abstract"]',
        'meta[name="dc.description"]',
        'meta[name="description"]',
        'meta[property="og:description"]',
        'meta[name="og:description"]',
    ):
        node = tree.css_first(selector)
        candidates.append(node.attributes.get("content") if node else None)
    for selector in profile.visible_summary_selectors:
        node = tree.css_first(selector)
        candidates.append(node.text(separator=" ", strip=True) if node else None)
    normalized_title = clean_text(title)
    for candidate in candidates:
        summary = clean_text(candidate)
        normalized_summary = (summary or "").casefold()
        if (
            summary is not None
            and 20 <= len(summary) <= 4000
            and summary.casefold() != (normalized_title or "").casefold()
            and not any(fragment in normalized_summary for fragment in _GENERIC_DESCRIPTION_FRAGMENTS)
        ):
            return summary
    return None


def _record_probe_error(
    state: DocumentAbstractState,
    error: ProbeError,
    checked_at: datetime,
) -> None:
    if error.retryable:
        _retry(state, "network_retryable", checked_at)
    elif error.code == "too_many_redirects":
        _block(state, "redirect_requires_review", checked_at)
    elif error.code in {"http_401", "http_403", "http_451"}:
        _block(state, "access_denied", checked_at)
    else:
        _block(state, "mime_or_size_rejected", checked_at)


def _retry(state: DocumentAbstractState, reason: str, checked_at: datetime) -> None:
    state.status = "retryable"
    state.reason_code = reason
    state.last_checked_at = checked_at
    state.next_retry_at = checked_at + _RETRY_DELAY


def _retry_at(
    state: DocumentAbstractState,
    reason: str,
    checked_at: datetime,
    next_retry_at: datetime,
) -> None:
    state.status = "retryable"
    state.reason_code = reason
    state.last_checked_at = checked_at
    state.next_retry_at = next_retry_at


def _next_retry_is_future(value: datetime | None, checked_at: datetime) -> bool:
    if value is None:
        return False
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized > checked_at.astimezone(UTC)


def _at_or_after(value: datetime | None, threshold: datetime | None) -> bool:
    if value is None or threshold is None:
        return False
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized >= threshold.astimezone(UTC)


def _block(state: DocumentAbstractState, reason: str, checked_at: datetime) -> None:
    state.status = "blocked"
    state.reason_code = reason
    state.last_checked_at = checked_at
    state.next_retry_at = None


def _unavailable(state: DocumentAbstractState, reason: str, checked_at: datetime) -> None:
    state.status = "unavailable"
    state.reason_code = reason
    state.last_checked_at = checked_at
    state.next_retry_at = checked_at + _UNAVAILABLE_RECHECK_DELAY
