from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import feedparser

from app.collectors.base import (
    CandidateDocument,
    Diagnostic,
    FetchedResource,
    ParseResult,
    absolute_url,
    clean_text,
    official_url,
    parse_datetime,
    tuple_of_text,
)

_DOI_PATTERN = re.compile(r"(?:https?://doi\.org/|doi:\s*)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: Any) -> str | None:
    text = clean_text(value)
    if not text or "<" not in text:
        return text
    parser = _TextExtractor()
    parser.feed(text)
    return clean_text(" ".join(parser.parts))


def _entry_link(entry: Mapping[str, Any]) -> Any:
    if entry.get("link"):
        return entry["link"]
    for link in entry.get("links", []):
        if isinstance(link, Mapping) and link.get("rel", "alternate") == "alternate":
            return link.get("href")
    return None


def _entry_doi(entry: Mapping[str, Any]) -> str | None:
    values = (
        entry.get("doi"),
        entry.get("dc_identifier"),
        entry.get("prism_doi"),
        entry.get("id"),
    )
    for value in values:
        match = _DOI_PATTERN.search(str(value or ""))
        if match:
            return match.group(1).rstrip(".,;")
    return None


class RssCollector:
    collector_type = "rss"

    def parse(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str = "unknown",
    ) -> ParseResult:
        profile = config.get("profile")
        if profile == "cnn_news_sitemap":
            return self._parse_cnn_news_sitemap(resource, config, link_role=link_role)
        if profile == "carnegie_sitemap":
            return self._parse_carnegie_sitemap(resource, config, link_role=link_role)
        if profile not in {None, "chatham_critical_minerals"}:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_rss_profile",
                        message=(
                            "RSS profile must be 'cnn_news_sitemap', 'carnegie_sitemap', or "
                            "'chatham_critical_minerals' when provided"
                        ),
                        level="error",
                    ),
                )
            )

        max_items: int | None = None
        if profile == "chatham_critical_minerals":
            parsed_resource = urlsplit(resource.final_url)
            max_items = config.get("max_items")
            if (
                parsed_resource.scheme != "https"
                or (parsed_resource.hostname or "").casefold() != "www.chathamhouse.org"
                or parsed_resource.path != "/path/83/feed.xml"
                or parsed_resource.query
                or parsed_resource.fragment
                or isinstance(max_items, bool)
                or not isinstance(max_items, int)
                or not 1 <= max_items <= 20
            ):
                return ParseResult(
                    diagnostics=(
                        Diagnostic(
                            code="invalid_chatham_rss_config",
                            message=(
                                "chatham_critical_minerals requires the reviewed official "
                                "expert-comments feed and max_items between 1 and 20"
                            ),
                            level="error",
                        ),
                    )
                )

        feed = feedparser.parse(resource.body)
        diagnostics: list[Diagnostic] = []
        candidates: list[CandidateDocument] = []

        if feed.get("bozo"):
            diagnostics.append(
                Diagnostic(
                    code="malformed_feed",
                    message=str(feed.get("bozo_exception") or "feed parser reported malformed XML"),
                    level="warning" if feed.entries else "error",
                )
            )

        feed_language = clean_text(feed.get("feed", {}).get("language"))
        feed_title = clean_text(feed.get("feed", {}).get("title"))
        for index, entry in enumerate(feed.entries):
            title = _plain_text(entry.get("title"))
            if profile == "chatham_critical_minerals" and "critical mineral" not in (title or "").casefold():
                continue
            discovery_url = absolute_url(_entry_link(entry), resource.final_url)
            if not discovery_url:
                diagnostics.append(
                    Diagnostic(
                        code="entry_url_missing",
                        message="feed entry has no usable HTTP(S) link",
                        locator=f"entries.{index}",
                    )
                )
                continue
            if profile == "chatham_critical_minerals":
                parsed_discovery = urlsplit(discovery_url)
                try:
                    discovery_port = parsed_discovery.port
                except ValueError:
                    continue
                if (
                    parsed_discovery.scheme != "https"
                    or (parsed_discovery.hostname or "").casefold() != "www.chathamhouse.org"
                    or parsed_discovery.username is not None
                    or parsed_discovery.password is not None
                    or discovery_port not in {None, 443}
                    or parsed_discovery.fragment
                ):
                    continue

            published_value = None
            if "published_parsed" in entry:
                published_value = entry["published_parsed"]
            elif "published" in entry:
                published_value = entry["published"]
            updated_value = None
            if "updated_parsed" in entry:
                updated_value = entry["updated_parsed"]
            elif "updated" in entry:
                updated_value = entry["updated"]
            published_at = parse_datetime(published_value)
            source_updated_at = parse_datetime(updated_value)
            if published_value and published_at is None:
                diagnostics.append(
                    Diagnostic(
                        code="published_date_unparsed",
                        message="feed entry publication date could not be parsed",
                        locator=f"entries.{index}",
                    )
                )

            content = entry.get("content") or []
            content_value = content[0].get("value") if content and isinstance(content[0], Mapping) else None
            summary = (
                None
                if profile == "chatham_critical_minerals"
                else _plain_text(entry.get("summary") or entry.get("description"))
            )
            body_text = None if profile == "chatham_critical_minerals" else _plain_text(content_value)
            authors = tuple_of_text(entry.get("authors")) or tuple_of_text(entry.get("author"))

            candidates.append(
                CandidateDocument(
                    discovery_url=discovery_url,
                    canonical_url=official_url(discovery_url, link_role),
                    external_id=clean_text(entry.get("id") or entry.get("guid")),
                    title=title,
                    doi=_entry_doi(entry),
                    authors=authors,
                    published_at=published_at,
                    source_updated_at=source_updated_at,
                    issue_text=clean_text(
                        entry.get("prism_issue") or entry.get("issue") or entry.get("dc_date")
                    ),
                    summary=summary,
                    summary_kind="feed_summary" if summary else None,
                    summary_source_url=resource.final_url if summary else None,
                    body_text=body_text,
                    language=clean_text(entry.get("language")) or feed_language,
                    media_type=(
                        "application/pdf"
                        if discovery_url.lower().split("?", 1)[0].endswith(".pdf")
                        else "text/html"
                    ),
                    resource_sha256=resource.sha256,
                    locator=f"entries.{index}",
                    source_metadata={
                        key: value
                        for key, value in {
                            "feed_title": feed_title,
                            "profile": profile,
                        }.items()
                        if value is not None
                    },
                )
            )
            if max_items is not None and len(candidates) >= max_items:
                break

        if not feed.entries:
            diagnostics.append(
                Diagnostic(code="feed_empty", message="feed contains no entries", level="warning")
            )
        elif profile == "chatham_critical_minerals" and not candidates:
            diagnostics.append(
                Diagnostic(
                    code="chatham_critical_minerals_empty",
                    message="expert-comments feed contains no title-level critical-minerals matches",
                )
            )
        return ParseResult(candidates=tuple(candidates), diagnostics=tuple(diagnostics))

    def _parse_carnegie_sitemap(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        """Parse a standard Carnegie Endowment XML sitemap (research or posts).

        The entry_url must be a /sitemaps/*.xml URL on carnegieendowment.org.
        Titles are inferred from the URL slug (final path segment, kebab → Title Case).
        ``max_items`` (1–50) is required; ``include_path_markers`` (list of strings)
        optionally restricts which URLs are retained.
        """
        max_items = config.get("max_items")
        include_path_markers: list[str] = list(config.get("include_path_markers") or [])
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 50:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_carnegie_sitemap_config",
                        message="carnegie_sitemap requires max_items between 1 and 50",
                        level="error",
                    ),
                )
            )

        source_host = (urlsplit(resource.final_url).hostname or "").casefold()
        if source_host not in {"carnegieendowment.org", "www.carnegieendowment.org"}:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_sitemap_host",
                        message="carnegie_sitemap must remain on carnegieendowment.org",
                        level="error",
                    ),
                )
            )

        try:
            root = ET.fromstring(resource.body)
        except ET.ParseError as exc:
            return ParseResult(
                diagnostics=(Diagnostic(code="malformed_sitemap", message=str(exc), level="error"),)
            )
        if _local_name(root.tag) != "urlset":
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_sitemap_root",
                        message=f"expected urlset, got {_local_name(root.tag)}",
                        level="error",
                    ),
                )
            )

        candidates: list[CandidateDocument] = []
        diagnostics: list[Diagnostic] = []
        seen: set[str] = set()

        for index, item in enumerate(root):
            if _local_name(item.tag) != "url":
                continue
            loc_text: str | None = None
            lastmod_text: str | None = None
            for node in item:
                name = _local_name(node.tag)
                text = clean_text(node.text)
                if not text:
                    continue
                if name == "loc" and loc_text is None:
                    loc_text = text
                elif name == "lastmod" and lastmod_text is None:
                    lastmod_text = text

            discovery_url = absolute_url(loc_text, resource.final_url)
            if not discovery_url:
                continue

            parsed_url = urlsplit(discovery_url)
            # Only keep URLs on the reviewed host, skip alternate-language paths (e.g. /ar/, /ru/)
            if (parsed_url.hostname or "").casefold() not in {
                "carnegieendowment.org",
                "www.carnegieendowment.org",
            }:
                continue
            path = parsed_url.path
            # Skip non-English paths (prefixed with /ar/, /ru/, /zh/, etc.)
            path_parts = [p for p in path.split("/") if p]
            if path_parts and len(path_parts[0]) == 2 and path_parts[0].isalpha():
                continue

            # Apply optional path marker filter
            if include_path_markers and not any(m in path for m in include_path_markers):
                continue

            if discovery_url in seen:
                continue
            seen.add(discovery_url)

            # Infer title from URL slug (last non-empty path segment)
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            title = _slug_to_title(slug) if slug else None
            if not title:
                diagnostics.append(
                    Diagnostic(
                        code="sitemap_title_missing",
                        message="could not infer title from URL slug",
                        locator=f"url[{index}]",
                    )
                )
                continue

            # Parse date from lastmod or from URL path (YYYY/MM pattern)
            published_at = parse_datetime(lastmod_text)
            if published_at is None:
                # Try to extract date from URL path like /research/2026/07/slug
                date_match = re.search(r"/(\d{4})/(\d{2})/", path)
                if date_match:
                    published_at = parse_datetime(f"{date_match.group(1)}-{date_match.group(2)}-01")

            candidates.append(
                CandidateDocument(
                    discovery_url=discovery_url,
                    canonical_url=official_url(discovery_url, link_role),
                    title=title,
                    published_at=published_at,
                    media_type="text/html",
                    resource_sha256=resource.sha256,
                    locator=f"url[{index}]",
                    source_metadata={"profile": "carnegie_sitemap"},
                )
            )
            if len(candidates) >= max_items:
                break

        if not candidates:
            diagnostics.append(
                Diagnostic(
                    code="sitemap_no_matching_items",
                    message="sitemap contains no matching entries",
                )
            )
        return ParseResult(candidates=tuple(candidates), diagnostics=tuple(diagnostics))

    def _parse_cnn_news_sitemap(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        path_marker = config.get("include_path_marker")
        max_items = config.get("max_items")
        if (
            not isinstance(path_marker, str)
            or not path_marker.startswith("/")
            or isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not 1 <= max_items <= 50
        ):
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_rss_config",
                        message=(
                            "cnn_news_sitemap requires an absolute include_path_marker and "
                            "max_items between 1 and 50"
                        ),
                        level="error",
                    ),
                )
            )

        try:
            root = ET.fromstring(resource.body)
        except ET.ParseError as exc:
            return ParseResult(
                diagnostics=(Diagnostic(code="malformed_sitemap", message=str(exc), level="error"),)
            )
        if _local_name(root.tag) != "urlset":
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_sitemap_root",
                        message="CNN news sitemap root must be urlset",
                        level="error",
                    ),
                )
            )

        source_host = (urlsplit(resource.final_url).hostname or "").casefold()
        if source_host != "www.cnn.com":
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_sitemap_host",
                        message="CNN news sitemap must remain on the reviewed www.cnn.com host",
                        level="error",
                    ),
                )
            )
        candidates: list[CandidateDocument] = []
        diagnostics: list[Diagnostic] = []
        seen: set[str] = set()
        for index, item in enumerate(root):
            if _local_name(item.tag) != "url":
                continue
            fields: dict[str, str] = {}
            for node in item.iter():
                name = _local_name(node.tag)
                text = clean_text(node.text)
                if text and name in {"loc", "title", "publication_date", "language"}:
                    fields.setdefault(name, text)

            discovery_url = absolute_url(fields.get("loc"), resource.final_url)
            if not discovery_url:
                continue
            parsed_url = urlsplit(discovery_url)
            if (
                (parsed_url.hostname or "").casefold() != source_host
                or path_marker.casefold() not in parsed_url.path.casefold()
                or discovery_url in seen
            ):
                continue
            title = clean_text(fields.get("title"))
            if not title:
                diagnostics.append(
                    Diagnostic(
                        code="sitemap_title_missing",
                        message="matching sitemap entry has no news title",
                        locator=f"url[{index}]",
                    )
                )
                continue
            seen.add(discovery_url)
            candidates.append(
                CandidateDocument(
                    discovery_url=discovery_url,
                    canonical_url=official_url(discovery_url, link_role),
                    title=title,
                    published_at=parse_datetime(fields.get("publication_date")),
                    language=clean_text(fields.get("language")),
                    media_type="text/html",
                    resource_sha256=resource.sha256,
                    locator=f"url[{index}]",
                    source_metadata={"profile": "cnn_news_sitemap"},
                )
            )
            if len(candidates) >= max_items:
                break

        if not candidates:
            diagnostics.append(
                Diagnostic(
                    code="sitemap_no_matching_items",
                    message="news sitemap contains no entries matching the reviewed path marker",
                )
            )
        return ParseResult(candidates=tuple(candidates), diagnostics=tuple(diagnostics))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _slug_to_title(slug: str) -> str:
    """Convert a URL slug (kebab-case final path segment) to a readable title."""
    return slug.replace("-", " ").title()
