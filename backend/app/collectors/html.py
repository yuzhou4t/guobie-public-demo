from __future__ import annotations

import json
import re
from ast import literal_eval
from collections.abc import Mapping
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import trafilatura
from selectolax.parser import HTMLParser, Node

from app.collectors.base import (
    CandidateDocument,
    Diagnostic,
    FetchedResource,
    FollowupRequest,
    ParseResult,
    absolute_url,
    clean_text,
    official_url,
    parse_datetime,
    tuple_of_text,
)
from app.collectors.reviewed_html import parse_reviewed_html

_CSSN_HOST = "ejournaliwep.cssn.cn"
_CSSN_ISSUE_PATH = re.compile(r"^/qkjj/sjjjyzz/sjz(?P<year>20\d{2})(?P<issue>0[1-9]|1[0-2])/$")
_CSSN_ISSUE_PAGE_PATH = re.compile(
    r"^/qkjj/sjjjyzz/sjz(?P<year>20\d{2})(?P<issue>0[1-9]|1[0-2])/"
    r"(?:(?:index_(?P<page>[1-9]\d*)\.shtml))?$"
)
_CSSN_ARTICLE_PATH = re.compile(
    r"^/qkjj/sjjjyzz/sjz(?P<year>20\d{2})(?P<issue>0[1-9]|1[0-2])/"
    r"\d{6}/(?P<external_id>t\d{8}_\d+)\.shtml$"
)
_MACRODATAS_HOST = "www.macrodatas.cn"
_MACRODATAS_SEARCH_PATH = re.compile(r"^/list/1/0/0/[^/]+/?$")
_MACRODATAS_ISSUE_PATH = re.compile(r"^/article/(?P<issue_id>\d+)/?$")
_MANAGEMENT_WORLD_ISSUE = re.compile(
    r"《\s*管理世界\s*》\s*(?P<year>20\d{2})\s*年第?\s*(?P<issue>\d{1,2})\s*期"
)
_MANAGEMENT_WORLD_ENTRY = re.compile(
    r"<p[^>]*>\s*(?P<number>\d{2})\s+(?P<title>[\s\S]*?)</p>\s*"
    r"<p[^>]*style=[\"'][^\"']*color:[^\"']*[\"'][^>]*>(?P<authors>[\s\S]*?)</p>",
    re.IGNORECASE,
)
_NCPSD_HOST = "www.ncpssd.cn"
_NCPSD_ISSUE_PATH = "/journal/details"
_NCPSD_DETAIL_ANCHOR = re.compile(
    r"<a\b(?=[^>]*openDetail\(\s*['\"](?P<url>[^'\"]*/Literature/(?:secure/)?articleinfo\?[^'\"]+)['\"](?:\s*,[^)]*)?\))"
    r"[^>]*>[\s\S]*?</a>",
    re.IGNORECASE,
)
_CIE_HOST = "ciejournal.ajcass.com"
_CIE_ARTICLE_PATH = re.compile(r"^/Magazine/Show$", re.IGNORECASE)
_ISS_AFRICA_HOST = "issafrica.org"
_ISS_AFRICA_REPORT_PATH = re.compile(
    r"^/research/africa-report/(?P<external_id>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)/?$"
)


def _selector(config: Mapping[str, Any], name: str, default: str | None = None) -> str | None:
    value = config.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty CSS selector")
    value = value.strip()
    if value.startswith("/") or "xpath(" in value.lower():
        raise ValueError(f"{name} must be a CSS selector, not XPath")
    return value


def _first(node: Node | HTMLParser, selector: str | None) -> Node | None:
    return node.css_first(selector) if selector else None


def _text(node: Node | None) -> str | None:
    return clean_text(node.text(separator=" ", strip=True)) if node else None


def _attribute(node: Node | None, name: str) -> str | None:
    return clean_text(node.attributes.get(name)) if node else None


def _fragment_text(value: str) -> str | None:
    return clean_text(HTMLParser(value).text(separator=" ", strip=True))


def _authors(value: str | None) -> tuple[str, ...]:
    return tuple(part for part in (clean_text(item) for item in re.split(r"[,，;；]", value or "")) if part)


def _string_list(config: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = config.get(name, [])
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"{name} must be a list with at most 20 strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 100:
            raise ValueError(f"{name} must contain non-empty strings up to 100 characters")
        result.append(item.strip().casefold())
    return tuple(result)


class HtmlCollector:
    collector_type = "html"

    def parse(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str = "unknown",
    ) -> ParseResult:
        mode = config.get("mode", "detail")
        if mode == "list":
            return self._parse_list(resource, config, link_role=link_role)
        if mode == "auto_list":
            return self._parse_auto_list(resource, config)
        if mode == "management_world":
            return self._parse_management_world(resource, config, link_role=link_role)
        if mode == "cie_current":
            return self._parse_cie_current(resource, config, link_role=link_role)
        if mode == "cssn_periodical":
            return self._parse_cssn_periodical(resource, config, link_role=link_role)
        if mode == "iss_africa_report":
            return self._parse_iss_africa_report(resource, config, link_role=link_role)
        if mode == "reviewed_metadata":
            return parse_reviewed_html(resource, config, link_role=link_role)
        if mode == "detail":
            return self._parse_detail(resource, config, link_role=link_role)
        return ParseResult(
            diagnostics=(
                Diagnostic(
                    code="invalid_html_mode",
                    message=(
                        "HTML mode must be 'list', 'auto_list', 'management_world', "
                        "'cie_current', 'cssn_periodical', 'iss_africa_report', "
                        "'reviewed_metadata', or 'detail'"
                    ),
                    level="error",
                ),
            )
        )

    def _parse_iss_africa_report(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        max_items = config.get("max_items")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_html_config",
                        message="iss_africa_report max_items must be an integer between 1 and 20",
                        level="error",
                    ),
                )
            )

        parsed_resource = urlsplit(resource.final_url)
        if (
            parsed_resource.scheme != "https"
            or (parsed_resource.hostname or "").casefold() != _ISS_AFRICA_HOST
            or parsed_resource.path.rstrip("/") != "/research/africa-report"
            or parsed_resource.query
            or parsed_resource.fragment
        ):
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_iss_africa_host",
                        message="iss_africa_report only accepts the reviewed official listing URL",
                        level="error",
                    ),
                )
            )

        tree = HTMLParser(resource.text())
        candidates: list[CandidateDocument] = []
        diagnostics: list[Diagnostic] = []
        seen_ids: set[str] = set()
        for index, node in enumerate(tree.css("#children_append a.card[href]")):
            target = absolute_url(_attribute(node, "href"), resource.final_url)
            if not target:
                continue
            parsed_target = urlsplit(target)
            match = _ISS_AFRICA_REPORT_PATH.fullmatch(parsed_target.path)
            if (
                parsed_target.scheme != "https"
                or (parsed_target.hostname or "").casefold() != _ISS_AFRICA_HOST
                or match is None
                or parsed_target.query
                or parsed_target.fragment
            ):
                continue
            external_id = match.group("external_id")
            if external_id in seen_ids:
                continue

            title = _text(_first(node, ".card-subtitle"))
            if not title:
                diagnostics.append(
                    Diagnostic(
                        code="iss_africa_title_missing",
                        message="ISS Africa report card has no title",
                        locator=f"#children_append a.card[{index}]",
                    )
                )
                continue
            date_text = _text(_first(node, ".card-date"))
            published_at = None
            authors: tuple[str, ...] = ()
            if date_text:
                date_match = re.fullmatch(
                    r"(?P<date>\d{2}\s+[A-Za-z]{3}\s+\d{4})(?:\s*/\s*by\s+(?P<authors>.+))?",
                    date_text,
                    re.IGNORECASE,
                )
                if date_match:
                    published_at = parse_datetime(f"{date_match.group('date')} 00:00:00 +0000")
                    author_text = re.sub(r"\s+&\s+", ",", date_match.group("authors") or "")
                    authors = _authors(author_text)
                if date_match is None or published_at is None:
                    diagnostics.append(
                        Diagnostic(
                            code="published_date_unparsed",
                            message="ISS Africa report date could not be parsed",
                            locator=f"#children_append a.card[{index}]",
                        )
                    )

            seen_ids.add(external_id)
            target = urlunsplit(
                (parsed_target.scheme, parsed_target.netloc, parsed_target.path.rstrip("/"), "", "")
            )
            summary = _text(_first(node, ".card-text"))
            candidates.append(
                CandidateDocument(
                    discovery_url=target,
                    canonical_url=official_url(target, link_role),
                    external_id=external_id,
                    title=title,
                    authors=authors,
                    published_at=published_at,
                    language="en",
                    media_type="text/html",
                    summary=summary,
                    summary_kind="html_summary" if summary else None,
                    summary_source_url=resource.final_url if summary else None,
                    resource_sha256=resource.sha256,
                    locator=f"#children_append a.card[{index}]",
                    source_metadata={
                        "listing_url": resource.final_url,
                        "metadata_only": True,
                    },
                )
            )
            if len(candidates) >= max_items:
                break

        if not candidates:
            diagnostics.append(
                Diagnostic(
                    code="iss_africa_report_list_empty",
                    message="ISS Africa listing contained no reviewed report cards",
                )
            )
        return ParseResult(candidates=tuple(candidates), diagnostics=tuple(diagnostics))

    def _parse_management_world(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        limits = {
            "max_issues": (1, 2),
            "max_items_per_issue": (1, 20),
            "max_items": (1, 40),
        }
        parsed_limits: dict[str, int] = {}
        for name, (minimum, maximum) in limits.items():
            value = config.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                return ParseResult(
                    diagnostics=(
                        Diagnostic(
                            code="invalid_html_config",
                            message=f"{name} must be an integer between {minimum} and {maximum}",
                            level="error",
                        ),
                    )
                )
            parsed_limits[name] = value
        gch = config.get("ncpssd_gch")
        if gch != "95499X":
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_html_config",
                        message="management_world requires the reviewed NCPSD journal code",
                        level="error",
                    ),
                )
            )

        parsed_resource = urlsplit(resource.final_url)
        host = (parsed_resource.hostname or "").casefold()
        if host == _MACRODATAS_HOST and _MACRODATAS_SEARCH_PATH.fullmatch(parsed_resource.path):
            tree = HTMLParser(resource.text())
            issues: list[tuple[int, int, str]] = []
            seen_urls: set[str] = set()
            for node in tree.css("a[href]"):
                target = absolute_url(_attribute(node, "href"), resource.final_url)
                title = _text(node)
                if not target or not title:
                    continue
                parsed_target = urlsplit(target)
                if (parsed_target.hostname or "").casefold() != _MACRODATAS_HOST:
                    continue
                if _MACRODATAS_ISSUE_PATH.fullmatch(parsed_target.path) is None:
                    continue
                issue_match = _MANAGEMENT_WORLD_ISSUE.search(title)
                if issue_match is None:
                    continue
                year = int(issue_match.group("year"))
                issue = int(issue_match.group("issue"))
                if not 1 <= issue <= 12:
                    continue
                target = urlunsplit((parsed_target.scheme, parsed_target.netloc, parsed_target.path, "", ""))
                if target in seen_urls:
                    continue
                seen_urls.add(target)
                issues.append((year, issue, target))
            issues.sort(reverse=True)
            followups = tuple(
                FollowupRequest(
                    url=target,
                    discovery_url=target,
                    parent_resource_sha256=resource.sha256,
                    context={
                        "discovered_from_url": resource.final_url,
                        "issue_text": f"{year}年第{issue}期",
                        "link_role": "discovery",
                    },
                )
                for year, issue, target in issues[: parsed_limits["max_issues"]]
            )
            diagnostics = ()
            if not followups:
                diagnostics = (
                    Diagnostic(
                        code="management_world_issue_list_empty",
                        message="Macrodatas search contained no reviewed Management World issue links",
                    ),
                )
            return ParseResult(followups=followups, diagnostics=diagnostics)

        issue_path = _MACRODATAS_ISSUE_PATH.fullmatch(parsed_resource.path)
        if host == _MACRODATAS_HOST and issue_path is not None:
            page_text = resource.text()
            page_issue = _MANAGEMENT_WORLD_ISSUE.search(_fragment_text(page_text) or "")
            if page_issue is None:
                return ParseResult(
                    diagnostics=(
                        Diagnostic(
                            code="management_world_issue_missing",
                            message="Macrodatas issue page did not identify a reviewed year and issue",
                            level="error",
                        ),
                    )
                )
            year = int(page_issue.group("year"))
            issue = int(page_issue.group("issue"))
            if not 1 <= issue <= 12:
                return ParseResult(
                    diagnostics=(
                        Diagnostic(
                            code="management_world_issue_invalid",
                            message="Macrodatas issue number is outside the monthly journal range",
                            level="error",
                        ),
                    )
                )
            issue_text = f"{year}年第{issue}期"
            issue_url = urlunsplit(
                (parsed_resource.scheme, parsed_resource.netloc, parsed_resource.path, "", "")
            )
            candidates: list[CandidateDocument] = []
            seen_titles: set[str] = set()
            for match in _MANAGEMENT_WORLD_ENTRY.finditer(page_text):
                title = _fragment_text(match.group("title"))
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                authors = _authors(_fragment_text(match.group("authors")))
                discovery_url = f"{issue_url}#:~:text={quote(title, safe='')}"
                candidates.append(
                    CandidateDocument(
                        discovery_url=discovery_url,
                        canonical_url=None,
                        title=title,
                        authors=authors,
                        issue_text=issue_text,
                        media_type="text/html",
                        resource_sha256=resource.sha256,
                        locator=issue_url,
                        source_metadata={
                            "discovered_from_url": issue_url,
                            "detail_verified": False,
                            "discovery_source": "macrodatas",
                        },
                    )
                )
                if len(candidates) >= parsed_limits["max_items_per_issue"]:
                    break
            diagnostics: list[Diagnostic] = []
            if not candidates:
                diagnostics.append(
                    Diagnostic(
                        code="management_world_articles_empty",
                        message="Macrodatas issue page contained no reviewed directory entries",
                    )
                )
                return ParseResult(diagnostics=tuple(diagnostics))
            resolver_url = (
                f"https://{_NCPSD_HOST}/journal/details?gch={gch}&langType=1&nav=1&years={year}&num={issue}"
            )
            return ParseResult(
                candidates=tuple(candidates),
                followups=(
                    FollowupRequest(
                        url=resolver_url,
                        discovery_url=resolver_url,
                        parent_resource_sha256=resource.sha256,
                        context={
                            "discovered_from_url": issue_url,
                            "issue_text": issue_text,
                            "link_role": "official",
                        },
                    ),
                ),
                diagnostics=tuple(diagnostics),
            )

        if host == _NCPSD_HOST and parsed_resource.path == _NCPSD_ISSUE_PATH:
            query = parse_qs(parsed_resource.query)
            if query.get("gch") != [gch]:
                return ParseResult(
                    diagnostics=(
                        Diagnostic(
                            code="invalid_ncpssd_issue",
                            message="NCPSD issue page does not use the reviewed Management World code",
                            level="error",
                        ),
                    )
                )
            page_text = resource.text()
            matches = list(_NCPSD_DETAIL_ANCHOR.finditer(page_text))
            candidates: list[CandidateDocument] = []
            seen_ids: set[str] = set()
            for index, match in enumerate(matches):
                anchor_html = match.group(0)
                opening_tag = anchor_html.split(">", 1)[0]
                title_match = re.search(r"\btitle=([\"'])([\s\S]*?)\1", opening_tag, re.IGNORECASE)
                title = (
                    _fragment_text(unescape(title_match.group(2)))
                    if title_match
                    else _fragment_text(anchor_html)
                )
                target = absolute_url(unescape(match.group("url")), resource.final_url)
                if not target or not title:
                    continue
                parsed_target = urlsplit(target)
                if (parsed_target.hostname or "").casefold() != _NCPSD_HOST:
                    continue
                if parsed_target.path not in {
                    "/Literature/articleinfo",
                    "/Literature/secure/articleinfo",
                }:
                    continue
                next_index = (
                    matches[index + 1].start()
                    if index + 1 < len(matches)
                    else min(len(page_text), match.start() + 3000)
                )
                context = page_text[match.start() : next_index]
                reader_match = re.search(
                    r"['\"]([^'\"]*/Literature/readurl\?id=[^'\"]+)['\"]",
                    context,
                    re.IGNORECASE,
                )
                reader_url = (
                    absolute_url(unescape(reader_match.group(1)), resource.final_url)
                    if reader_match
                    else None
                )
                external_id = parse_qs(parsed_target.query).get("id", [None])[0]
                if not external_id and reader_url:
                    external_id = parse_qs(urlsplit(reader_url).query).get("id", [None])[0]
                if not external_id or external_id in seen_ids:
                    continue
                seen_ids.add(external_id)
                candidates.append(
                    CandidateDocument(
                        discovery_url=target,
                        canonical_url=official_url(target, link_role),
                        external_id=external_id,
                        title=title,
                        issue_text=(
                            f"{query.get('years', [''])[0]}年第{query.get('num', [''])[0]}期"
                            if query.get("years") and query.get("num")
                            else None
                        ),
                        media_type="text/html",
                        resource_sha256=resource.sha256,
                        locator=target,
                        source_metadata={
                            "official_article_entry": link_role == "official",
                            "official_source": "ncpssd",
                            "reader_url": reader_url,
                        },
                    )
                )
                if len(candidates) >= parsed_limits["max_items"]:
                    break
            diagnostics = ()
            if not candidates:
                diagnostics = (
                    Diagnostic(
                        code="ncpssd_issue_articles_empty",
                        message="NCPSD issue page contained no reviewed article entries",
                    ),
                )
            return ParseResult(candidates=tuple(candidates), diagnostics=diagnostics)

        return ParseResult(
            diagnostics=(
                Diagnostic(
                    code="invalid_management_world_host_or_path",
                    message="management_world mode only accepts its reviewed discovery and resolver URLs",
                    level="error",
                ),
            )
        )

    def _parse_cie_current(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        max_items = config.get("max_items")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_html_config",
                        message="max_items must be an integer between 1 and 20",
                        level="error",
                    ),
                )
            )
        parsed_resource = urlsplit(resource.final_url)
        if (parsed_resource.hostname or "").casefold() != _CIE_HOST or parsed_resource.path not in {"", "/"}:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_cie_entry",
                        message="cie_current mode only accepts the reviewed official homepage",
                        level="error",
                    ),
                )
            )
        tree = HTMLParser(resource.text())
        parsed: list[tuple[int, int, str, CandidateDocument]] = []
        for table in tree.css("table"):
            link = table.css_first("a[href]")
            target = absolute_url(_attribute(link, "href"), resource.final_url)
            title = _text(link)
            if not target or not title:
                continue
            parsed_target = urlsplit(target)
            query = parse_qs(parsed_target.query)
            article_id = query.get("id", [None])[0]
            if (
                (parsed_target.hostname or "").casefold() != _CIE_HOST
                or _CIE_ARTICLE_PATH.fullmatch(parsed_target.path) is None
                or not article_id
                or not article_id.isdigit()
            ):
                continue
            table_text = _text(table) or ""
            issue_match = re.search(
                r"(20\d{2})\s*年\s*[,，]?\s*第\s*(\d{1,2})\s*期",
                table_text,
            )
            if issue_match is None:
                continue
            year = int(issue_match.group(1))
            issue = int(issue_match.group(2))
            if not 1 <= issue <= 12:
                continue
            target = urlunsplit(
                (parsed_target.scheme, parsed_target.netloc, parsed_target.path, parsed_target.query, "")
            )
            author_node = table.css_first("span")
            parsed.append(
                (
                    year,
                    issue,
                    article_id,
                    CandidateDocument(
                        discovery_url=target,
                        canonical_url=official_url(target, link_role),
                        external_id=f"cie:{article_id}",
                        title=title,
                        authors=_authors(_text(author_node)),
                        issue_text=f"{year}年第{issue}期",
                        media_type="text/html",
                        resource_sha256=resource.sha256,
                        locator=target,
                        source_metadata={
                            "official_article_entry": link_role == "official",
                            "current_issue_homepage": resource.final_url,
                            "detail_fetched": False,
                        },
                    ),
                )
            )
        if not parsed:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="cie_current_articles_empty",
                        message="official homepage contained no current-issue article rows",
                    ),
                )
            )
        latest_issue = max((year, issue) for year, issue, _, _ in parsed)
        candidates: list[CandidateDocument] = []
        seen_ids: set[str] = set()
        for year, issue, article_id, candidate in parsed:
            if (year, issue) != latest_issue or article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            candidates.append(candidate)
            if len(candidates) >= max_items:
                break
        return ParseResult(candidates=tuple(candidates))

    def _parse_cssn_periodical(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        limits = {
            "max_issues": (1, 4),
            "max_pages_per_issue": (1, 4),
            "max_items": (1, 20),
        }
        parsed_limits: dict[str, int] = {}
        for name, (minimum, maximum) in limits.items():
            value = config.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                return ParseResult(
                    diagnostics=(
                        Diagnostic(
                            code="invalid_html_config",
                            message=f"{name} must be an integer between {minimum} and {maximum}",
                            level="error",
                        ),
                    )
                )
            parsed_limits[name] = value

        parsed_resource = urlsplit(resource.final_url)
        source_host = (parsed_resource.hostname or "").casefold()
        if source_host != _CSSN_HOST:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_cssn_host",
                        message="CSSN periodical mode only accepts the reviewed official host",
                        level="error",
                    ),
                )
            )
        issue_page_match = _CSSN_ISSUE_PAGE_PATH.fullmatch(parsed_resource.path)
        page_text = resource.text()
        tree = HTMLParser(page_text)
        if issue_page_match is None:
            issues: list[tuple[int, int, str]] = []
            seen: set[str] = set()
            for node in tree.css("a[href]"):
                target = absolute_url(_attribute(node, "href"), resource.final_url)
                if not target:
                    continue
                parsed_target = urlsplit(target)
                if (parsed_target.hostname or "").casefold() != source_host:
                    continue
                match = _CSSN_ISSUE_PATH.fullmatch(parsed_target.path)
                if match is None:
                    continue
                target = urlunsplit((parsed_target.scheme, parsed_target.netloc, parsed_target.path, "", ""))
                if target in seen:
                    continue
                seen.add(target)
                issues.append((int(match.group("year")), int(match.group("issue")), target))
            issues.sort(reverse=True)
            followups = tuple(
                FollowupRequest(
                    url=target,
                    discovery_url=target,
                    parent_resource_sha256=resource.sha256,
                    context={
                        "discovered_from_url": resource.final_url,
                        "issue_text": f"{year}年第{issue}期",
                    },
                )
                for year, issue, target in issues[: parsed_limits["max_issues"]]
            )
            diagnostics = ()
            if not followups:
                diagnostics = (
                    Diagnostic(
                        code="cssn_issue_list_empty",
                        message="CSSN periodical entry contained no reviewed issue links",
                    ),
                )
            return ParseResult(followups=followups, diagnostics=diagnostics)

        year = int(issue_page_match.group("year"))
        issue = int(issue_page_match.group("issue"))
        issue_text = f"{year}年第{issue}期"
        candidates: list[CandidateDocument] = []
        seen_article_ids: set[str] = set()
        for node in tree.css("h4 a[href]"):
            target = absolute_url(_attribute(node, "href"), resource.final_url)
            title = _text(node)
            if not target or not title or "目录" in title:
                continue
            parsed_target = urlsplit(target)
            if (parsed_target.hostname or "").casefold() != source_host:
                continue
            match = _CSSN_ARTICLE_PATH.fullmatch(parsed_target.path)
            if match is None or int(match.group("year")) != year or int(match.group("issue")) != issue:
                continue
            external_id = match.group("external_id")
            if external_id in seen_article_ids:
                continue
            seen_article_ids.add(external_id)
            target = urlunsplit((parsed_target.scheme, parsed_target.netloc, parsed_target.path, "", ""))
            candidates.append(
                CandidateDocument(
                    discovery_url=target,
                    canonical_url=official_url(target, link_role),
                    external_id=external_id,
                    title=title,
                    issue_text=issue_text,
                    media_type="text/html",
                    resource_sha256=resource.sha256,
                    locator=target,
                    source_metadata={
                        "issue_page_url": resource.final_url,
                        "official_article_entry": link_role == "official",
                    },
                )
            )
            if len(candidates) >= parsed_limits["max_items"]:
                break

        followups: list[FollowupRequest] = []
        diagnostics: list[Diagnostic] = []
        if issue_page_match.group("page") is None:
            page_match = re.search(r"\bcountPage\s*=\s*(\d{1,3})(?!\d)", page_text)
            page_count = int(page_match.group(1)) if page_match else 1
            if page_match is None and re.search(r"\bcountPage\s*=", page_text):
                diagnostics.append(
                    Diagnostic(
                        code="invalid_cssn_page_count",
                        message="CSSN issue page contained an invalid page count",
                    )
                )
            page_count = min(page_count, parsed_limits["max_pages_per_issue"])
            for page_number in range(2, page_count + 1):
                target = absolute_url(f"index_{page_number - 1}.shtml", resource.final_url)
                if target:
                    followups.append(
                        FollowupRequest(
                            url=target,
                            discovery_url=target,
                            parent_resource_sha256=resource.sha256,
                            context={
                                "discovered_from_url": resource.final_url,
                                "issue_text": issue_text,
                            },
                        )
                    )

        if not candidates:
            diagnostics.append(
                Diagnostic(
                    code="cssn_issue_articles_empty",
                    message="CSSN issue page contained no reviewed article links",
                )
            )
        return ParseResult(
            candidates=tuple(candidates),
            followups=tuple(followups),
            diagnostics=tuple(diagnostics),
        )

    def _parse_auto_list(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
    ) -> ParseResult:
        """Discover a small, same-host set of likely material links.

        This is intentionally conservative. It is used only for the first
        metadata-only shadow pass when a source has no reviewed CSS selector.
        """

        try:
            max_links = int(config.get("max_links", 20))
        except (TypeError, ValueError):
            max_links = 0
        if not 1 <= max_links <= 20:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_html_config",
                        message="max_links must be an integer between 1 and 20",
                        level="error",
                    ),
                )
            )

        try:
            include_path_markers = _string_list(config, "include_path_markers")
            exclude_path_markers = _string_list(config, "exclude_path_markers")
            allowed_hosts = _string_list(config, "allowed_hosts")
        except ValueError as exc:
            return ParseResult(
                diagnostics=(Diagnostic(code="invalid_html_config", message=str(exc), level="error"),)
            )

        text_content = resource.text()
        if config.get("platform") == "jpaas" and text_content.strip().startswith("{"):
            try:
                data = json.loads(text_content)
                data_html = data.get("data", {}).get("html") if isinstance(data, dict) else None
                if isinstance(data_html, str):
                    text_content = data_html
            except json.JSONDecodeError:
                pass

        tree = HTMLParser(text_content)
        nodes = tree.css("main a[href], article a[href], [role='main'] a[href]")
        if not nodes:
            nodes = tree.css("a[href]")

        source_host = (urlsplit(resource.final_url).hostname or "").lower()
        allowed_host_set = {source_host, *allowed_hosts}
        source_url = resource.final_url.rstrip("/")
        ignored_text = {
            "home",
            "about",
            "contact",
            "login",
            "sign in",
            "subscribe",
            "menu",
            "more",
            "skip to content",
            "首页",
            "上一页",
            "下一页",
            "尾页",
            "登录",
            "注册",
            "更多",
        }
        ignored_suffixes = (
            ".css",
            ".gif",
            ".ico",
            ".jpeg",
            ".jpg",
            ".js",
            ".png",
            ".svg",
            ".webp",
            ".zip",
        )
        followups: list[FollowupRequest] = []
        seen: set[str] = set()
        for node in nodes:
            target = absolute_url(_attribute(node, "href"), resource.final_url)
            for script in node.css("script"):
                script.decompose()
            title = _text(node) or _attribute(node, "title") or _attribute(node, "aria-label")
            if not target or not title:
                continue
            parsed = urlsplit(target)
            if parsed.hostname is None:
                continue
            if parsed.hostname.lower() not in allowed_host_set or parsed.fragment:
                continue
            path = f"{parsed.path}?{parsed.query}".casefold()
            if include_path_markers and not any(marker in path for marker in include_path_markers):
                continue
            if any(marker in path for marker in exclude_path_markers):
                continue
            normalized_target = target.rstrip("/")
            if normalized_target == source_url or normalized_target in seen:
                continue
            if title.casefold() in ignored_text or len(title) < 4:
                continue
            if parsed.path.lower().endswith(ignored_suffixes):
                continue
            seen.add(normalized_target)
            followups.append(
                FollowupRequest(
                    url=target,
                    discovery_url=target,
                    parent_resource_sha256=resource.sha256,
                    context={"discovered_from_url": resource.final_url, "title": title},
                )
            )
            if len(followups) >= max_links:
                break

        if not followups and config.get("platform") == "jpaas":
            for script_node in tree.css("script[src*='unitbuild.js']"):
                api_rel_url = _attribute(script_node, "url")
                query_data_str = _attribute(script_node, "querydata")
                if api_rel_url and query_data_str:
                    try:
                        query_dict = literal_eval(query_data_str)
                    except (SyntaxError, ValueError):
                        continue
                    if not isinstance(query_dict, dict) or not all(
                        isinstance(key, str) and isinstance(value, (str, int, float, bool))
                        for key, value in query_dict.items()
                    ):
                        continue
                    api_url = absolute_url(f"{api_rel_url}?{urlencode(query_dict)}", resource.final_url)
                    api_parts = urlsplit(api_url or "")
                    if (
                        api_parts.hostname != source_host
                        or "/api-gateway/jpaas-publish-server/" not in api_parts.path
                        or api_parts.fragment
                    ):
                        continue
                    followups.append(
                        FollowupRequest(
                            url=api_url,
                            discovery_url=api_url,
                            parent_resource_sha256=resource.sha256,
                            context={
                                "discovered_from_url": resource.final_url,
                                "followup_profile": "jpaas_unitbuild",
                                "link_role": "official",
                            },
                        )
                    )
                    break

        diagnostics: list[Diagnostic] = []
        if not followups:
            diagnostics.append(
                Diagnostic(
                    code="html_auto_list_empty",
                    message="no conservative same-host material links were discovered",
                )
            )
        return ParseResult(followups=tuple(followups), diagnostics=tuple(diagnostics))

    def _parse_list(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        del link_role
        try:
            item_selector = _selector(config, "item_selector")
            link_selector = _selector(config, "link_selector", "a[href]")
            title_selector = _selector(config, "title_selector")
            summary_selector = _selector(config, "summary_selector")
            published_selector = _selector(config, "published_selector")
            if item_selector is None:
                raise ValueError("item_selector is required in list mode")
            tree = HTMLParser(resource.text())
            item_nodes = tree.css(item_selector)
        except (ValueError, TypeError) as exc:
            return ParseResult(
                diagnostics=(Diagnostic(code="invalid_html_config", message=str(exc), level="error"),)
            )

        followups: list[FollowupRequest] = []
        diagnostics: list[Diagnostic] = []
        seen: set[str] = set()
        link_attribute = str(config.get("link_attribute", "href"))
        for index, item in enumerate(item_nodes):
            locator = f"{item_selector}[{index}]"
            try:
                link_node = _first(item, link_selector)
                target = absolute_url(_attribute(link_node, link_attribute), resource.final_url)
                if not target:
                    diagnostics.append(
                        Diagnostic(
                            code="list_item_url_missing",
                            message="HTML list item has no usable HTTP(S) link",
                            locator=locator,
                        )
                    )
                    continue
                if target in seen:
                    continue
                seen.add(target)
                title = _text(_first(item, title_selector)) or _text(link_node)
                summary = _text(_first(item, summary_selector))
                published_text = _text(_first(item, published_selector))
            except (ValueError, TypeError) as exc:
                diagnostics.append(
                    Diagnostic(
                        code="list_item_parse_failed",
                        message=str(exc),
                        locator=locator,
                    )
                )
                continue

            context = {
                key: value
                for key, value in {
                    "discovered_from_url": resource.final_url,
                    "title": title,
                    "summary": summary,
                    "published_text": published_text,
                }.items()
                if value is not None
            }
            followups.append(
                FollowupRequest(
                    url=target,
                    discovery_url=target,
                    parent_resource_sha256=resource.sha256,
                    context=context,
                )
            )

        if not item_nodes:
            diagnostics.append(
                Diagnostic(code="html_list_empty", message="item_selector matched no elements")
            )
        return ParseResult(followups=tuple(followups), diagnostics=tuple(diagnostics))

    def _parse_detail(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str,
    ) -> ParseResult:
        try:
            tree = HTMLParser(resource.text())
            title_selector = _selector(config, "title_selector")
            summary_selector = _selector(config, "summary_selector")
            published_selector = _selector(config, "published_selector")
            updated_selector = _selector(config, "updated_selector")
            author_selector = _selector(config, "author_selector")
            canonical_selector = _selector(config, "canonical_selector", 'link[rel="canonical"]')
        except (ValueError, TypeError) as exc:
            return ParseResult(
                diagnostics=(Diagnostic(code="invalid_html_config", message=str(exc), level="error"),)
            )

        extracted: dict[str, Any] = {}
        try:
            extracted_json = trafilatura.extract(
                resource.text(),
                url=resource.final_url,
                output_format="json",
                include_comments=False,
                include_tables=True,
            )
            if extracted_json:
                value = json.loads(extracted_json)
                if isinstance(value, dict):
                    extracted = value
        except (TypeError, ValueError, json.JSONDecodeError):
            extracted = {}

        canonical_attribute = str(config.get("canonical_attribute", "href"))
        canonical_value = _attribute(_first(tree, canonical_selector), canonical_attribute)
        candidate_canonical = absolute_url(
            canonical_value or extracted.get("url") or resource.final_url,
            resource.final_url,
        )
        title = (
            _text(_first(tree, title_selector))
            or clean_text(extracted.get("title"))
            or _text(_first(tree, "title"))
        )
        body_text = clean_text(extracted.get("text"))
        summary = _text(_first(tree, summary_selector)) or clean_text(extracted.get("description"))
        published_value = _text(_first(tree, published_selector)) or extracted.get("date")
        updated_value = _text(_first(tree, updated_selector))
        author_value = _text(_first(tree, author_selector)) or extracted.get("author")
        diagnostics: list[Diagnostic] = []
        if not body_text:
            diagnostics.append(
                Diagnostic(
                    code="article_text_empty",
                    message="Trafilatura did not extract article text",
                )
            )
        if published_value and parse_datetime(published_value) is None:
            diagnostics.append(
                Diagnostic(
                    code="published_date_unparsed",
                    message="HTML publication date could not be parsed",
                )
            )

        candidate = CandidateDocument(
            discovery_url=resource.request_url,
            canonical_url=official_url(candidate_canonical, link_role),
            external_id=clean_text(extracted.get("id")),
            title=title,
            authors=tuple_of_text(author_value),
            published_at=parse_datetime(published_value),
            source_updated_at=parse_datetime(updated_value),
            summary=summary,
            summary_kind="html_summary" if summary else None,
            summary_source_url=resource.final_url if summary else None,
            body_text=body_text,
            language=clean_text(extracted.get("language")),
            media_type="text/html",
            resource_sha256=resource.sha256,
            locator=resource.final_url,
            source_metadata={
                key: value for key, value in {"site_name": extracted.get("sitename")}.items() if value
            },
        )
        return ParseResult(candidates=(candidate,), diagnostics=tuple(diagnostics))
