from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from app.collectors.base import (
    CandidateDocument,
    Diagnostic,
    FetchedResource,
    ParseResult,
    parse_datetime,
)

_UN_REPORT_ID = re.compile(r"^S/(20\d{2})/(\d+)$", re.IGNORECASE)
_DATED_PATH = re.compile(r"^/(20\d{2})/(\d{2})/(\d{2})/")
_DATE_META_KEYS = {
    "article:published_time",
    "date",
    "datepublished",
    "dc.date",
    "dc.date.issued",
    "dcterms.created",
    "dcterms.date",
    "parsely-pub-date",
    "pubdate",
}


def parse_drc_mvp_listing(
    resource: FetchedResource,
    config: Mapping[str, Any],
    *,
    link_role: str = "unknown",
) -> ParseResult:
    """Parse bounded, reviewed DRC MVP listing metadata without retaining page text."""

    profile = str(config.get("profile") or "").strip()
    if profile == "single_document":
        return ParseResult(candidates=(_parse_single_document(resource, config, link_role),))
    if resource.content_type.split(";", 1)[0].strip().casefold() not in {
        "text/html",
        "application/xhtml+xml",
    }:
        return ParseResult(
            diagnostics=(
                Diagnostic(
                    code="drc_mvp_unsupported_content_type",
                    message="DRC MVP listing parser accepts HTML only",
                    level="error",
                ),
            )
        )

    max_items = _bounded_max_items(config.get("max_items"))
    tree = HTMLParser(resource.text())
    if profile == "single_page_metadata":
        manifest_published_at = parse_datetime(config.get("published_at"))
        manifest_precision = str(config.get("published_at_precision") or "unknown")
        candidate = CandidateDocument(
            discovery_url=resource.final_url,
            canonical_url=_https_url(resource.final_url) if link_role == "official" else None,
            external_id=str(config.get("external_id") or urlsplit(resource.final_url).path),
            title=str(config.get("title") or _html_title(tree) or "官方页面"),
            published_at=manifest_published_at,
            language=str(config.get("language") or "en"),
            media_type="text/html",
            resource_sha256=resource.sha256,
            source_metadata={
                "profile": profile,
                "published_at_provenance": {
                    "precision": manifest_precision,
                    "method": "manifest_review" if manifest_published_at else "unknown",
                },
            },
        )
        candidates = [parse_drc_mvp_detail(resource, candidate)]
    elif profile == "un_expert_reports":
        candidates = _parse_un_reports(tree, resource, config, max_items, link_role)
    elif profile == "radio_okapi_rubaya":
        candidates = _parse_radio_okapi(tree, resource, max_items, link_role)
    elif profile in {"path_allowlist", "path_prefix", "keyword_links"}:
        candidates = _parse_reviewed_links(tree, resource, config, max_items, link_role)
    else:
        return ParseResult(
            diagnostics=(
                Diagnostic(
                    code="drc_mvp_profile_unknown",
                    message=f"unknown DRC MVP listing profile: {profile or '(empty)'}",
                    level="error",
                ),
            )
        )

    diagnostics: tuple[Diagnostic, ...] = ()
    if not candidates:
        diagnostics = (
            Diagnostic(
                code="drc_mvp_no_items",
                message="reviewed DRC MVP listing yielded no matching metadata",
            ),
        )
    return ParseResult(candidates=tuple(candidates), diagnostics=diagnostics)


def _parse_single_document(
    resource: FetchedResource,
    config: Mapping[str, Any],
    link_role: str,
) -> CandidateDocument:
    published_at = parse_datetime(config.get("published_at"))
    precision = str(config.get("published_at_precision") or "unknown")
    reviewed_canonical_url = str(config.get("canonical_url") or "").strip()
    return CandidateDocument(
        discovery_url=resource.final_url,
        canonical_url=(
            _https_url(reviewed_canonical_url or resource.final_url) if link_role == "official" else None
        ),
        external_id=str(config.get("external_id") or urlsplit(resource.final_url).path),
        title=str(config.get("title") or "官方文件"),
        published_at=published_at,
        language=str(config.get("language") or "en"),
        media_type=resource.content_type.split(";", 1)[0],
        resource_sha256=resource.sha256,
        source_metadata={
            "profile": "single_document",
            "detail_retrieved_at": resource.fetched_at.isoformat(),
            "published_at_provenance": {"precision": precision, "method": "manifest_review"},
        },
    )


def parse_drc_mvp_detail(
    resource: FetchedResource,
    candidate: CandidateDocument,
) -> CandidateDocument:
    """Enrich one candidate from an already-fetched official HTML detail page."""

    if resource.content_type.split(";", 1)[0].strip().casefold() not in {
        "text/html",
        "application/xhtml+xml",
    }:
        return candidate
    tree = HTMLParser(resource.text())
    published_at = candidate.published_at
    precision = _candidate_precision(candidate)
    if published_at is None:
        published_at, precision = _html_published_at(tree)
    title = candidate.title or _html_title(tree)
    metadata = dict(candidate.source_metadata)
    metadata.update(
        {
            "detail_resource_sha256": resource.sha256,
            "detail_retrieved_at": resource.fetched_at.isoformat(),
            "published_at_provenance": {
                "precision": precision,
                "method": (
                    metadata.get("published_at_provenance", {}).get("method")
                    if candidate.published_at
                    else "official_html_metadata"
                ),
            },
        }
    )
    return replace(
        candidate,
        canonical_url=_https_url(resource.final_url),
        title=title,
        published_at=published_at,
        resource_sha256=resource.sha256,
        source_metadata=metadata,
    )


def _parse_un_reports(
    tree: HTMLParser,
    resource: FetchedResource,
    config: Mapping[str, Any],
    max_items: int,
    link_role: str,
) -> list[CandidateDocument]:
    candidates: list[CandidateDocument] = []
    seen: set[str] = set()
    published_dates = {
        str(key).upper(): value for key, value in dict(config.get("published_dates") or {}).items()
    }
    for link in tree.css("a"):
        title = _clean_text(link.text(strip=True))
        match = _UN_REPORT_ID.fullmatch(title)
        if match is None:
            continue
        discovery_url = urljoin(resource.final_url, (link.attributes.get("href") or "").strip())
        parsed = urlsplit(discovery_url)
        if parsed.hostname not in {"undocs.org", "www.undocs.org"}:
            continue
        canonical_url = _https_url(discovery_url) if link_role == "official" else None
        if discovery_url in seen:
            continue
        seen.add(discovery_url)
        published_at = parse_datetime(published_dates.get(title.upper()))
        metadata: dict[str, Any] = {
            "profile": "un_expert_reports",
            "report_year": match.group(1),
        }
        if published_at is not None:
            metadata["published_at_provenance"] = {
                "precision": "day",
                "method": "manifest_review",
            }
        candidates.append(
            CandidateDocument(
                discovery_url=discovery_url,
                canonical_url=canonical_url,
                external_id=title.upper(),
                title=f"联合国刚果（金）专家组报告 {title.upper()}",
                published_at=published_at,
                language="en",
                media_type="application/pdf",
                resource_sha256=resource.sha256,
                source_metadata=metadata,
            )
        )
        if len(candidates) >= max_items:
            break
    return candidates


def _parse_radio_okapi(
    tree: HTMLParser,
    resource: FetchedResource,
    max_items: int,
    link_role: str,
) -> list[CandidateDocument]:
    candidates: list[CandidateDocument] = []
    seen: set[str] = set()
    for link in tree.css("a"):
        title = _clean_text(link.text(strip=True))
        discovery_url = urljoin(resource.final_url, (link.attributes.get("href") or "").strip())
        parsed = urlsplit(discovery_url)
        date_match = _DATED_PATH.match(parsed.path)
        if (
            parsed.hostname not in {"radiookapi.net", "www.radiookapi.net"}
            or date_match is None
            or "rubaya" not in f"{title} {parsed.path}".casefold()
            or not title
        ):
            continue
        canonical_url = _https_url(discovery_url) if link_role == "official" else None
        canonical_key = canonical_url or discovery_url
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        published_at = _date_from_match(date_match)
        candidates.append(
            CandidateDocument(
                discovery_url=discovery_url,
                canonical_url=canonical_url,
                external_id=parsed.path.rstrip("/").rsplit("/", 1)[-1],
                title=title,
                published_at=published_at,
                language="fr",
                media_type="text/html",
                resource_sha256=resource.sha256,
                source_metadata={
                    "profile": "radio_okapi_rubaya",
                    "published_at_provenance": {"precision": "day", "method": "url_path"},
                },
            )
        )
        if len(candidates) >= max_items:
            break
    return candidates


def _parse_reviewed_links(
    tree: HTMLParser,
    resource: FetchedResource,
    config: Mapping[str, Any],
    max_items: int,
    link_role: str,
) -> list[CandidateDocument]:
    profile = str(config["profile"])
    allowed_hosts = {str(value).casefold() for value in config.get("allowed_hosts", ())}
    allowed_paths = {str(value) for value in config.get("allowed_paths", ())}
    path_prefixes = tuple(str(value) for value in config.get("allowed_path_prefixes", ()))
    title_keywords = tuple(str(value).casefold() for value in config.get("title_keywords", ()))
    candidates: list[CandidateDocument] = []
    seen: set[str] = set()

    for link in tree.css("a"):
        title = _clean_text(link.text(strip=True))
        discovery_url = urljoin(resource.final_url, (link.attributes.get("href") or "").strip())
        parsed = urlsplit(discovery_url)
        host = (parsed.hostname or "").casefold()
        if not title or host not in allowed_hosts:
            continue
        if profile == "path_allowlist" and parsed.path not in allowed_paths:
            continue
        if profile == "path_prefix" and not parsed.path.startswith(path_prefixes):
            continue
        if profile == "keyword_links" and not any(
            keyword in f"{title} {parsed.path}".casefold() for keyword in title_keywords
        ):
            continue
        canonical_url = _https_url(discovery_url) if link_role == "official" else None
        canonical_key = canonical_url or discovery_url
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidates.append(
            CandidateDocument(
                discovery_url=discovery_url,
                canonical_url=canonical_url,
                external_id=parsed.path.strip("/") or host,
                title=title,
                language="fr",
                media_type="text/html",
                resource_sha256=resource.sha256,
                source_metadata={"profile": profile},
            )
        )
        if len(candidates) >= max_items:
            break
    return candidates


def _bounded_max_items(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 20
    return max(1, min(parsed, 50))


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "http" and parsed.port is None:
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _date_from_match(match: re.Match[str]) -> datetime | None:
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _candidate_precision(candidate: CandidateDocument) -> str:
    provenance = candidate.source_metadata.get("published_at_provenance")
    if isinstance(provenance, Mapping):
        precision = str(provenance.get("precision") or "unknown")
        if precision in {"day", "month", "year", "unknown"}:
            return precision
    return "day" if candidate.published_at else "unknown"


def _html_published_at(tree: HTMLParser) -> tuple[datetime | None, str]:
    values: list[str] = []
    for node in tree.css("meta"):
        key = (node.attributes.get("property") or node.attributes.get("name") or "").casefold()
        content = (node.attributes.get("content") or "").strip()
        if key in _DATE_META_KEYS and content:
            values.append(content)
    for node in tree.css("time"):
        value = (node.attributes.get("datetime") or "").strip()
        if value:
            values.append(value)
    for value in values:
        parsed = parse_datetime(value)
        if parsed is None:
            continue
        if re.fullmatch(r"\d{4}", value.strip()):
            return parsed, "year"
        if re.fullmatch(r"\d{4}-\d{2}", value.strip()):
            return parsed, "month"
        return parsed, "day"
    return None, "unknown"


def _html_title(tree: HTMLParser) -> str | None:
    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        node = tree.css_first(selector)
        if node is not None:
            title = _clean_text(node.attributes.get("content") or "")
            if title:
                return title
    node = tree.css_first("h1") or tree.css_first("title")
    return _clean_text(node.text(strip=True)) if node is not None else None
