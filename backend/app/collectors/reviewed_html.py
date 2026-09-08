from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from selectolax.parser import HTMLParser, Node

from app.collectors.base import (
    CandidateDocument,
    Diagnostic,
    FetchedResource,
    ParseResult,
    absolute_url,
    clean_text,
    official_url,
    parse_datetime,
)


@dataclass(frozen=True, slots=True)
class LinkRule:
    host: str
    path: re.Pattern[str]
    schemes: tuple[str, ...] = ("https",)


@dataclass(frozen=True, slots=True)
class ReviewedHtmlProfile:
    entry_url: str
    accepted_final_urls: tuple[str, ...]
    max_items: int
    language: str
    item_selector: str | None = None
    link_selector: str | None = None
    title_selector: str | None = None
    summary_selector: str | None = None
    date_selector: str | None = None
    type_selector: str | None = None
    required_type: str | None = None
    link_rules: tuple[LinkRule, ...] = ()
    strip_query: bool = False
    allowed_query_keys: tuple[str, ...] = ()
    self_page: bool = False
    withhold_http_canonical: bool = False


def _rule(
    host: str,
    pattern: str,
    *,
    schemes: tuple[str, ...] = ("https",),
) -> LinkRule:
    return LinkRule(host=host, path=re.compile(pattern, re.IGNORECASE), schemes=schemes)


REVIEWED_HTML_PROFILES: dict[str, ReviewedHtmlProfile] = {
    "iea_critical_minerals": ReviewedHtmlProfile(
        entry_url="https://www.iea.org/topics/critical-minerals",
        accepted_final_urls=("https://www.iea.org/topics/critical-minerals",),
        max_items=20,
        language="en",
        item_selector=(
            "section.m-related-content .m-related-content__item[data-slider-type] > a.m-card[href]"
        ),
        title_selector=".m-card__title",
        date_selector=".m-card__type",
        type_selector=".m-card__type",
        link_rules=(_rule("www.iea.org", r"^/(?:reports|news|commentaries|events)/[a-z0-9][a-z0-9/-]*$"),),
    ),
    "unctad_critical_minerals_classifications": ReviewedHtmlProfile(
        entry_url="https://unctadstat.unctad.org/EN/Classifications.html",
        accepted_final_urls=("https://unctadstat.unctad.org/EN/Classifications.html",),
        max_items=6,
        language="en",
        link_rules=(
            _rule(
                "unctadstat.unctad.org",
                r"^/EN/Classifications/DimCriticalMinerals_"
                r"(?:(?:HS2002|HS2007|HS2012|HS2017|HS2022)_)?Hierarchy\.pdf$",
            ),
        ),
    ),
    "world_bank_metals": ReviewedHtmlProfile(
        entry_url="https://www.worldbank.org/ext/en/topic/metals-and-minerals",
        accepted_final_urls=("https://www.worldbank.org/ext/en/topic/metals-and-minerals",),
        max_items=20,
        language="en",
        link_rules=(
            _rule("blogs.worldbank.org", r"^/en/[a-z0-9][a-z0-9/-]*$"),
            _rule("www.worldbank.org", r"^/en/news/[a-z0-9][a-z0-9/-]*$"),
            _rule(
                "www.worldbank.org",
                r"^/en/topic/extractiveindustries/publication/[a-z0-9][a-z0-9/-]*$",
            ),
            _rule(
                "openknowledge.worldbank.org",
                r"^/entities/publication/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
            ),
        ),
    ),
    "eiti_country_reports": ReviewedHtmlProfile(
        entry_url="https://eiti.org/eiti-country-reports",
        accepted_final_urls=("https://eiti.org/eiti-country-reports",),
        max_items=10,
        language="en",
        item_selector="a.c-card.c-card--list_text_only.c-card--document[href]",
        title_selector=".c-card__heading",
        date_selector=".c-card__meta",
        type_selector=".c-card__content-type",
        required_type="EITI country report",
        link_rules=(_rule("eiti.org", r"^/documents/[a-z0-9][a-z0-9-]*$"),),
    ),
    "daily_maverick_news": ReviewedHtmlProfile(
        entry_url="https://www.dailymaverick.co.za/section/maverick-news/",
        accepted_final_urls=("https://www.dailymaverick.co.za/section/maverick-news",),
        max_items=20,
        language="en",
        item_selector="main article",
        link_selector="a[href*='/article/'], a[href*='/video/']",
        title_selector="h3.font-georgia",
        link_rules=(
            _rule(
                "www.dailymaverick.co.za",
                r"^/(?:article|video)/20\d{2}-\d{2}-\d{2}-[a-z0-9-]+/$",
            ),
        ),
        strip_query=True,
        allowed_query_keys=("dm_source", "dm_medium", "dm_campaign", "dm_position"),
    ),
    "enca_news": ReviewedHtmlProfile(
        entry_url="https://www.enca.com/news",
        accepted_final_urls=("https://www.enca.com/news",),
        max_items=20,
        language="en",
        item_selector=".landing-page-articles-block article[data-history-node-id]",
        link_selector="a[rel='bookmark'], a.tile-body[href]",
        title_selector=".heading span",
        date_selector=".published-date",
        link_rules=(
            _rule(
                "www.enca.com",
                r"^/news-top-stories(?:-videos)?/[a-z0-9][a-z0-9-]*$",
            ),
        ),
    ),
    "peoples_daily_world": ReviewedHtmlProfile(
        entry_url="https://en.people.cn/90777/",
        accepted_final_urls=(
            "http://en.people.cn/90777/index.html",
            "https://en.people.cn/90777",
            "https://en.people.cn/90777/index.html",
        ),
        max_items=20,
        language="en",
        item_selector=".w1280.foreign_d2list.cf li > a[href], .foreign_d2list li > a[href]",
        link_rules=(
            _rule(
                "en.people.cn",
                r"^/n3/20\d{2}/\d{4}/c\d+-\d+\.html$",
                schemes=("http", "https"),
            ),
        ),
        withhold_http_canonical=True,
    ),
    "niser_policy_briefs": ReviewedHtmlProfile(
        entry_url="https://niser.gov.ng/v2/category/briefs/",
        accepted_final_urls=("https://niser.gov.ng/v2/category/briefs",),
        max_items=20,
        language="en",
        item_selector="article.elementor-post.category-briefs",
        link_selector=".elementor-post__title a[href]",
        title_selector=".elementor-post__title",
        date_selector=".elementor-post-date",
        link_rules=(_rule("niser.gov.ng", r"^/v2/(?!category/|wp-)[a-z0-9][a-z0-9-]*/$"),),
    ),
    "iea_data_product": ReviewedHtmlProfile(
        entry_url=("https://www.iea.org/data-and-statistics/data-product/critical-minerals-dataset"),
        accepted_final_urls=(
            "https://www.iea.org/data-and-statistics/data-product/critical-minerals-dataset",
        ),
        max_items=1,
        language="en",
        self_page=True,
        link_rules=(
            _rule(
                "www.iea.org",
                r"^/data-and-statistics/data-product/critical-minerals-dataset$",
            ),
        ),
    ),
    "usgs_nmic_news": ReviewedHtmlProfile(
        entry_url="https://www.usgs.gov/centers/national-minerals-information-center/news",
        accepted_final_urls=("https://www.usgs.gov/centers/national-minerals-information-center/news",),
        max_items=12,
        language="en",
        item_selector=".node.node--type--news.node--view-mode--teaser",
        link_selector=(
            "a.d-link[href^='/news/'], a.d-link[href^='/programs/mineral-resources-program/news/']"
        ),
        title_selector="h4.d-title",
        date_selector="time[datetime]",
        link_rules=(
            _rule("www.usgs.gov", r"^/news/[a-z0-9][a-z0-9/-]*$"),
            _rule(
                "www.usgs.gov",
                r"^/programs/mineral-resources-program/news/[a-z0-9][a-z0-9-]*$",
            ),
        ),
    ),
    "usgs_nmic_publications": ReviewedHtmlProfile(
        entry_url="https://www.usgs.gov/centers/national-minerals-information-center/publications",
        accepted_final_urls=(
            "https://www.usgs.gov/centers/national-minerals-information-center/publications",
        ),
        max_items=12,
        language="en",
        item_selector=".views-row .node.node--type--publication.node--view-mode--list",
        link_selector="h4 > a[href^='/publications/']",
        title_selector="h4 > a > span.usa-sr-only",
        date_selector="time[datetime]",
        link_rules=(_rule("www.usgs.gov", r"^/publications/[a-z0-9][a-z0-9/-]*$"),),
    ),
}


def parse_reviewed_html(
    resource: FetchedResource,
    config: Mapping[str, Any],
    *,
    link_role: str,
) -> ParseResult:
    profile_name = config.get("profile")
    profile = REVIEWED_HTML_PROFILES.get(profile_name) if isinstance(profile_name, str) else None
    if profile is None:
        return _error("invalid_reviewed_html_profile", "reviewed HTML profile is not recognized")

    max_items = config.get("max_items")
    if (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or not 1 <= max_items <= profile.max_items
    ):
        return _error(
            "invalid_html_config",
            f"{profile_name} max_items must be an integer between 1 and {profile.max_items}",
        )
    if not _accepted_page(resource.final_url, profile.accepted_final_urls):
        return _error(
            "invalid_reviewed_html_entry",
            f"{profile_name} only accepts its reviewed official listing URL",
        )
    if link_role != "official":
        return _error(
            "invalid_reviewed_html_role",
            "reviewed HTML profiles require an official link role",
        )

    if profile_name == "world_bank_metals":
        return _parse_world_bank(resource, profile, max_items)
    if profile_name == "unctad_critical_minerals_classifications":
        return _parse_unctad_critical_minerals_classifications(resource, profile, max_items)
    if profile.self_page:
        return _parse_iea_data_product(resource, profile, max_items)
    return _parse_listing(resource, profile_name, profile, max_items)


def _parse_unctad_critical_minerals_classifications(
    resource: FetchedResource,
    profile: ReviewedHtmlProfile,
    max_items: int,
) -> ParseResult:
    tree = HTMLParser(resource.text())
    candidates: list[CandidateDocument] = []
    seen: set[str] = set()
    for index, item in enumerate(tree.css("li")):
        title = _text(item)
        if not title or "critical minerals" not in title.casefold():
            continue
        for link in item.css("a[href]"):
            target = _reviewed_target(
                _attribute(link, "href"),
                resource.final_url,
                profile.link_rules,
                strip_query=False,
                allowed_query_keys=(),
            )
            if target is None or target in seen:
                continue
            seen.add(target)
            external_id = urlsplit(target).path.rsplit("/", 1)[-1].removesuffix(".pdf")
            candidates.append(
                CandidateDocument(
                    discovery_url=target,
                    canonical_url=target,
                    external_id=external_id,
                    title=f"{title} (PDF)",
                    language=profile.language,
                    media_type="application/pdf",
                    resource_sha256=resource.sha256,
                    locator=f"li[{index}]",
                    source_metadata={
                        "listing_url": resource.final_url,
                        "metadata_only": True,
                        "profile": "unctad_critical_minerals_classifications",
                    },
                )
            )
            if len(candidates) >= max_items:
                return ParseResult(candidates=tuple(candidates))

    diagnostics: tuple[Diagnostic, ...] = ()
    if not candidates:
        diagnostics = (
            Diagnostic(
                code="reviewed_html_list_empty",
                message="UNCTAD classification page contained no reviewed critical-minerals PDFs",
            ),
        )
    return ParseResult(candidates=tuple(candidates), diagnostics=diagnostics)


def _parse_listing(
    resource: FetchedResource,
    profile_name: str,
    profile: ReviewedHtmlProfile,
    max_items: int,
) -> ParseResult:
    assert profile.item_selector is not None
    tree = HTMLParser(resource.text())
    candidates: list[CandidateDocument] = []
    diagnostics: list[Diagnostic] = []
    seen: set[str] = set()
    for index, item in enumerate(tree.css(profile.item_selector)):
        link = item.css_first(profile.link_selector) if profile.link_selector else item
        target = _reviewed_target(
            _attribute(link, "href"),
            resource.final_url,
            profile.link_rules,
            strip_query=profile.strip_query,
            allowed_query_keys=profile.allowed_query_keys,
        )
        if target is None or target in seen:
            continue

        material_type = _text(item.css_first(profile.type_selector)) if profile.type_selector else None
        if profile.required_type and (material_type or "").casefold() != profile.required_type.casefold():
            continue
        if profile.title_selector:
            title = _text(item.css_first(profile.title_selector))
        else:
            title = _attribute(link, "title") or _text(link)
        if not title or len(title) < 4:
            diagnostics.append(
                Diagnostic(
                    code="reviewed_item_title_missing",
                    message="reviewed listing item has no usable title",
                    locator=f"{profile.item_selector}[{index}]",
                )
            )
            continue

        date_node = item.css_first(profile.date_selector) if profile.date_selector else None
        date_text = _date_value(date_node)
        published_at = _parse_reviewed_date(date_text, urlsplit(target).path)
        summary = _text(item.css_first(profile.summary_selector)) if profile.summary_selector else None
        metadata: dict[str, Any] = {
            "listing_url": resource.final_url,
            "metadata_only": True,
            "profile": profile_name,
        }
        normalized_type = _material_type(material_type)
        if normalized_type:
            metadata["material_type"] = normalized_type
        canonical_withheld = profile.withhold_http_canonical and "http" in {
            urlsplit(resource.final_url).scheme.casefold(),
            urlsplit(target).scheme.casefold(),
        }
        if canonical_withheld:
            metadata["canonical_withheld"] = "insecure_http_transport"

        seen.add(target)
        candidates.append(
            CandidateDocument(
                discovery_url=target,
                canonical_url=None if canonical_withheld else official_url(target, "official"),
                title=title,
                published_at=published_at,
                summary=summary,
                summary_kind="html_summary" if summary else None,
                summary_source_url=resource.final_url if summary else None,
                language=profile.language,
                media_type=(
                    "application/pdf" if urlsplit(target).path.lower().endswith(".pdf") else "text/html"
                ),
                resource_sha256=resource.sha256,
                locator=f"{profile.item_selector}[{index}]",
                source_metadata=metadata,
            )
        )
        if len(candidates) >= max_items:
            break

    if not candidates:
        diagnostics.append(
            Diagnostic(
                code="reviewed_html_list_empty",
                message=f"{profile_name} contained no reviewed metadata records",
            )
        )
    return ParseResult(candidates=tuple(candidates), diagnostics=tuple(diagnostics))


def _parse_world_bank(
    resource: FetchedResource,
    profile: ReviewedHtmlProfile,
    max_items: int,
) -> ParseResult:
    tree = HTMLParser(resource.text())
    components = [
        *tree.css(".news-banner"),
        *tree.css(".research-publications-cards > div"),
        *tree.css(".mini-cards.mini-card-with-desc > div"),
    ]
    candidates: list[CandidateDocument] = []
    seen: set[str] = set()
    for index, component in enumerate(components):
        target = None
        link = None
        for anchor in component.css("a[href]"):
            reviewed = _reviewed_target(
                _attribute(anchor, "href"),
                resource.final_url,
                profile.link_rules,
                strip_query=False,
                allowed_query_keys=(),
            )
            if reviewed is not None:
                target = reviewed
                link = anchor
                break
        if target is None or link is None or target in seen:
            continue

        title = _world_bank_title(component)
        if not title:
            continue
        published_at = _parse_reviewed_date(None, urlsplit(target).path)
        summary = _world_bank_summary(component)
        seen.add(target)
        candidates.append(
            CandidateDocument(
                discovery_url=target,
                canonical_url=target,
                title=title,
                published_at=published_at,
                summary=summary,
                summary_kind="html_summary" if summary else None,
                summary_source_url=resource.final_url if summary else None,
                language="en",
                media_type="text/html",
                resource_sha256=resource.sha256,
                locator=f"reviewed-world-bank-component[{index}]",
                source_metadata={
                    "listing_url": resource.final_url,
                    "metadata_only": True,
                    "profile": "world_bank_metals",
                },
            )
        )
        if len(candidates) >= max_items:
            break

    diagnostics: tuple[Diagnostic, ...] = ()
    if not candidates:
        diagnostics = (
            Diagnostic(
                code="reviewed_html_list_empty",
                message="world_bank_metals contained no reviewed metadata records",
            ),
        )
    return ParseResult(candidates=tuple(candidates), diagnostics=diagnostics)


def _world_bank_title(component: Node) -> str | None:
    children = _element_children(component)
    component_classes = set((component.attributes.get("class") or "").split())
    parent_classes = set((component.parent.attributes.get("class") or "").split())
    if "news-banner" in component_classes:
        title_index, type_index, allowed_types = 3, 2, {"blog", "immersive story"}
    elif "research-publications-cards" in parent_classes:
        title_index, type_index, allowed_types = 2, 0, {"report"}
    elif "mini-card-with-desc" in parent_classes:
        title_index, type_index, allowed_types = 2, 1, {"blog"}
    else:
        return None
    if len(children) <= max(title_index, type_index):
        return None
    material_type = _text(children[type_index])
    if not material_type or material_type.casefold() not in allowed_types:
        return None
    return _text(children[title_index])


def _world_bank_summary(component: Node) -> str | None:
    for paragraph in component.css("p"):
        if paragraph.css_first("a[href]") is not None:
            continue
        value = _text(paragraph)
        if value:
            return value
    return None


def _element_children(node: Node) -> list[Node]:
    children: list[Node] = []
    child = node.child
    while child is not None:
        if child.tag != "-text":
            children.append(child)
        child = child.next
    return children


def _parse_iea_data_product(
    resource: FetchedResource,
    profile: ReviewedHtmlProfile,
    max_items: int,
) -> ParseResult:
    if max_items != 1:
        return _error("invalid_html_config", "iea_data_product max_items must be exactly 1")
    tree = HTMLParser(resource.text())
    title = _text(tree.css_first(".o-hero-data-product__title"))
    canonical_node = tree.css_first("link[rel='canonical']")
    target = _reviewed_target(
        _attribute(canonical_node, "href") or resource.final_url,
        resource.final_url,
        profile.link_rules,
        strip_query=False,
        allowed_query_keys=(),
    )
    if not title or target is None:
        return _error(
            "reviewed_page_metadata_missing",
            "IEA data product page is missing its reviewed title or canonical URL",
        )

    metadata: dict[str, Any] = {
        "content_type": "data_product",
        "metadata_only": True,
        "profile": "iea_data_product",
    }
    access = _text(tree.css_first(".o-hero-data-product__label .a-label"))
    if access:
        metadata["access"] = access
    for footer in tree.css(".m-hero__footer-content"):
        label = _text(footer.css_first(".m-hero__footer-title"))
        if not label:
            continue
        value = _text(footer.css_first(".a-link__label")) or _text(footer)
        if value and value.casefold().startswith(label.casefold()):
            value = clean_text(value[len(label) :])
        if not value:
            continue
        if label.casefold() == "licence":
            metadata["license"] = value
        elif label.casefold() == "last updated":
            metadata["last_updated_text"] = value

    summary = _text(tree.css_first(".o-hero-data-product__subtitle"))
    if summary is None:
        description = tree.css_first('meta[name="description"]')
        summary = clean_text(description.attributes.get("content")) if description else None

    return ParseResult(
        candidates=(
            CandidateDocument(
                discovery_url=target,
                canonical_url=target,
                title=title,
                summary=summary,
                summary_kind="html_summary" if summary else None,
                summary_source_url=resource.final_url if summary else None,
                language="en",
                media_type="text/html",
                resource_sha256=resource.sha256,
                locator=".o-hero-data-product__title",
                source_metadata=metadata,
            ),
        )
    )


def _accepted_page(url: str, accepted_urls: tuple[str, ...]) -> bool:
    identity = _page_identity(url)
    return any(identity == _page_identity(item) for item in accepted_urls)


def _page_identity(url: str) -> tuple[str, str, str, str, str]:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    return (
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        path,
        parsed.query,
        parsed.fragment,
    )


def _reviewed_target(
    value: str | None,
    base_url: str,
    rules: tuple[LinkRule, ...],
    *,
    strip_query: bool,
    allowed_query_keys: tuple[str, ...],
) -> str | None:
    try:
        target = absolute_url(value, base_url)
        parsed = urlsplit(target) if target else None
    except ValueError:
        return None
    if not target:
        return None
    assert parsed is not None
    scheme = parsed.scheme.casefold()
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    host = (parsed.hostname or "").casefold()
    if not any(
        rule.host == host and scheme in rule.schemes and rule.path.fullmatch(parsed.path) for rule in rules
    ):
        return None
    if parsed.query:
        query_keys = tuple(key for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
        if not strip_query or not query_keys or any(key not in allowed_query_keys for key in query_keys):
            return None
    path = quote(parsed.path, safe="/%:@-._~!$&'()*+,;=")
    return urlunsplit((scheme, host, path, "", ""))


def _date_value(node: Node | None) -> str | None:
    if node is None:
        return None
    for name in ("datetime", "timestamp", "content"):
        value = _attribute(node, name)
        if value:
            return value
    return _text(node)


def _parse_reviewed_date(value: str | None, path: str) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is not None:
        return parsed
    text = value or ""
    candidates: list[tuple[str, tuple[str, ...]]] = [
        (r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})\b", ("%d %b %Y", "%d %B %Y")),
        (r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+20\d{2})\b", ("%b %d, %Y", "%B %d, %Y")),
        (r"\b(20\d{2}-\d{2}-\d{2})\b", ("%Y-%m-%d",)),
        (r"\b(20\d{2}/\d{2}/\d{2})\b", ("%Y/%m/%d",)),
    ]
    for source in (text, path):
        for pattern, formats in candidates:
            match = re.search(pattern, source)
            if match is None:
                continue
            for date_format in formats:
                try:
                    return datetime.strptime(match.group(1), date_format).replace(tzinfo=UTC)
                except ValueError:
                    continue
    return None


def _material_type(value: str | None) -> str | None:
    if not value:
        return None
    return clean_text(re.split(r"\s+[—–-]\s+", value, maxsplit=1)[0])


def _text(node: Node | None) -> str | None:
    return clean_text(node.text(separator=" ", strip=True)) if node else None


def _attribute(node: Node | None, name: str) -> str | None:
    return clean_text(node.attributes.get(name)) if node else None


def _error(code: str, message: str) -> ParseResult:
    return ParseResult(diagnostics=(Diagnostic(code=code, message=message, level="error"),))
