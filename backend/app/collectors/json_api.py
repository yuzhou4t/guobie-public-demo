from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.parser import HTMLParser

from app.collectors.base import (
    CandidateDocument,
    Diagnostic,
    FetchedResource,
    ParseResult,
    absolute_url,
    clean_text,
    official_url,
    parse_datetime,
    slug_to_title,
    tuple_of_text,
)

_DOI_PATTERN = re.compile(r"(?:https?://doi\.org/|doi:\s*)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I)
_AJCASS_FIELD_MAP = {
    "external_id": "id",
    "title": "title",
    "discovery_url": "_discovery_url",
    "canonical_url": "_canonical_url",
    "authors": "authors",
    "issue_text": "_issue_text",
}
_DSPACE_FIELD_MAP = {
    "external_id": "_external_id",
    "title": "_title",
    "discovery_url": "_discovery_url",
    "canonical_url": "_canonical_url",
    "doi": "_doi",
    "authors": "_authors",
    "published_at": "_published_at",
    "source_updated_at": "_source_updated_at",
    "issue_text": "_issue_text",
    "language": "_language",
    "media_type": "_media_type",
}
_AERC_WORDPRESS_FIELD_MAP = {
    "external_id": "_external_id",
    "title": "_title",
    "discovery_url": "_url",
    "canonical_url": "_url",
    "published_at": "_published_at",
    "source_updated_at": "_source_updated_at",
    "media_type": "_media_type",
}
_AERC_DSPACE_HOST = "publication.aercafricalibrary.org"
_AERC_WORDPRESS_HOST = "aercafrica.org"
_CROSSREF_API_HOST = "api.crossref.org"
_CARNEGIE_FIELD_MAP = {
    "external_id": "_external_id",
    "title": "_title",
    "discovery_url": "_url",
    "canonical_url": "_url",
    "published_at": "_published_at",
    "source_updated_at": "_source_updated_at",
    "media_type": "_media_type",
}
_CARNEGIE_HOST = "carnegieendowment.org"


def _at_path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for key in path.split("."):
        if not key or not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _mapped(item: Mapping[str, Any], field_map: Mapping[str, Any], name: str) -> Any:
    path = field_map.get(name)
    return _at_path(item, path) if isinstance(path, str) else None


def _doi(value: Any) -> str | None:
    match = _DOI_PATTERN.search(str(value or ""))
    return match.group(1).rstrip(".,;") if match else None


def _is_crossref_works_resource(resource: FetchedResource) -> bool:
    parsed = urlsplit(resource.final_url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == _CROSSREF_API_HOST
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and parsed.path.rstrip("/").endswith("/works")
    )


def _is_creative_commons_license(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").casefold() == "creativecommons.org"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 80, 443}
        and parsed.path.casefold().startswith("/licenses/")
        and parsed.path.casefold() != "/licenses/"
    )


def _crossref_license_start(value: Any) -> tuple[datetime | None, bool]:
    if value is None:
        return None, True
    if not isinstance(value, Mapping):
        return None, False
    parsed = parse_datetime(value.get("date-time") or value.get("timestamp"))
    if parsed is None:
        date_parts = value.get("date-parts")
        parts = date_parts[0] if isinstance(date_parts, list) and date_parts else None
        if isinstance(parts, list) and parts:
            try:
                parsed = datetime(
                    int(parts[0]),
                    int(parts[1]) if len(parts) > 1 else 1,
                    int(parts[2]) if len(parts) > 2 else 1,
                    tzinfo=UTC,
                )
            except (TypeError, ValueError):
                parsed = None
    return parsed, parsed is not None


def crossref_cc_abstract(
    item: Mapping[str, Any],
    *,
    as_of: datetime,
) -> tuple[str | None, dict[str, str], datetime | None]:
    abstract = clean_text(item.get("abstract"))
    licenses = item.get("license")
    if not abstract or not isinstance(licenses, list):
        return None, {}, None
    normalized_as_of = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)
    license_metadata: dict[str, str] = {}
    next_license_start: datetime | None = None
    for license_item in licenses:
        if not isinstance(license_item, Mapping):
            continue
        license_url = clean_text(license_item.get("URL"))
        content_version = clean_text(license_item.get("content-version"))
        if not _is_creative_commons_license(license_url) or (content_version or "").casefold() != "vor":
            continue
        start_at, start_is_valid = _crossref_license_start(license_item.get("start"))
        if not start_is_valid:
            continue
        if start_at is not None and start_at > normalized_as_of:
            if next_license_start is None or start_at < next_license_start:
                next_license_start = start_at
            continue
        license_metadata = {
            "abstract_license_url": license_url or "",
            "abstract_license_content_version": "vor",
            **({"abstract_license_start_at": start_at.isoformat()} if start_at is not None else {}),
        }
        break
    if not license_metadata:
        return None, {}, next_license_start
    if "<" in abstract and ">" in abstract:
        abstract = clean_text(HTMLParser(abstract).text(separator=" ", strip=True))
    return abstract, license_metadata, None


def crossref_direct_abstract(item: Mapping[str, Any]) -> str | None:
    abstract = clean_text(item.get("abstract"))
    if not abstract:
        return None
    if "<" in abstract and ">" in abstract:
        abstract = clean_text(HTMLParser(abstract).text(separator=" ", strip=True))
    return abstract


def _crossref_publication_date(value: Any) -> tuple[datetime | None, str | None, list[int]]:
    date_parts = value.get("date-parts") if isinstance(value, Mapping) else None
    parts = date_parts[0] if isinstance(date_parts, list) and date_parts else None
    if not isinstance(parts, list) or not 1 <= len(parts) <= 3:
        return None, None, []
    try:
        normalized = [int(part) for part in parts]
        published_at = datetime(
            normalized[0],
            normalized[1] if len(normalized) > 1 else 1,
            normalized[2] if len(normalized) > 2 else 1,
            tzinfo=UTC,
        )
    except (TypeError, ValueError):
        return None, None, []
    precision = {1: "year", 2: "month", 3: "day"}[len(normalized)]
    return published_at, precision, normalized


def _ajcass_current_items(
    payload: Any,
    config: Mapping[str, Any],
    diagnostics: list[Diagnostic],
) -> list[Mapping[str, Any]] | None:
    site_origin = config.get("site_origin")
    try:
        parsed_origin = urlsplit(site_origin) if isinstance(site_origin, str) else None
        origin_port = parsed_origin.port if parsed_origin else None
    except ValueError:
        parsed_origin = None
        origin_port = None
    if (
        parsed_origin is None
        or parsed_origin.scheme not in {"http", "https"}
        or not parsed_origin.hostname
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or origin_port not in {None, 80, 443}
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        diagnostics.append(
            Diagnostic(
                code="invalid_api_config",
                message="ajcass_current site_origin must be an HTTP(S) origin",
                level="error",
            )
        )
        return None

    max_items = config.get("max_items", 20)
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
        diagnostics.append(
            Diagnostic(
                code="invalid_api_config",
                message="ajcass_current max_items must be an integer between 1 and 20",
                level="error",
            )
        )
        return None

    channels = _at_path(payload, "data.channels")
    if not isinstance(channels, list):
        diagnostics.append(
            Diagnostic(
                code="items_path_not_found",
                message="ajcass_current data.channels is not a list",
                level="error",
                locator="data.channels",
            )
        )
        return None

    origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
    items: list[Mapping[str, Any]] = []
    for channel_index, channel in enumerate(channels):
        issue_items = channel.get("issueInfoList") if isinstance(channel, Mapping) else None
        if not isinstance(issue_items, list):
            diagnostics.append(
                Diagnostic(
                    code="ajcass_issue_list_missing",
                    message="AJCass channel has no issueInfoList array",
                    locator=f"data.channels[{channel_index}]",
                )
            )
            continue
        for raw_item in issue_items:
            if not isinstance(raw_item, Mapping):
                continue
            article_id = str(raw_item.get("id") or "").strip()
            if not article_id.isdigit():
                continue
            item = dict(raw_item)
            item["_discovery_url"] = f"{origin}/#/issueDetail?id={article_id}"
            year = str(raw_item.get("year") or "").strip()
            issue = str(raw_item.get("issue") or "").strip()
            if re.fullmatch(r"\d{4}", year):
                item["_issue_text"] = f"{year}年第{int(issue)}期" if re.fullmatch(r"\d{1,2}", issue) else year
            file_url = absolute_url(raw_item.get("filePath"), f"{origin}/")
            if file_url:
                parsed_file = urlsplit(file_url)
                if (
                    parsed_file.scheme.casefold() == parsed_origin.scheme.casefold()
                    and parsed_file.netloc.casefold() == parsed_origin.netloc.casefold()
                    and parsed_file.path.casefold().endswith(".pdf")
                ):
                    item["_canonical_url"] = file_url
            items.append(item)
            if len(items) >= max_items:
                return items
    return items


def _metadata_values(metadata: Mapping[str, Any], key: str) -> list[str]:
    raw_values = metadata.get(key)
    if not isinstance(raw_values, list):
        return []
    values: list[str] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, Mapping):
            continue
        value = clean_text(raw_value.get("value"))
        if value:
            values.append(value)
    return values


def _dspace_search_items(
    payload: Any,
    config: Mapping[str, Any],
    resource: FetchedResource,
    diagnostics: list[Diagnostic],
) -> list[Mapping[str, Any]] | None:
    max_items = config.get("max_items")
    if isinstance(max_items, bool) or max_items != 20:
        diagnostics.append(
            Diagnostic(
                code="invalid_api_config",
                message="dspace_search max_items must be exactly 20",
                level="error",
            )
        )
        return None

    parsed_resource = urlsplit(resource.final_url)
    query = parse_qs(parsed_resource.query, keep_blank_values=True)
    if (
        parsed_resource.scheme != "https"
        or (parsed_resource.hostname or "").casefold() != _AERC_DSPACE_HOST
        or parsed_resource.path != "/server/api/discover/search/objects"
        or parsed_resource.fragment
        or query
        != {
            "size": ["20"],
            "page": ["0"],
            "sort": ["dc.date.accessioned,DESC"],
        }
    ):
        diagnostics.append(
            Diagnostic(
                code="invalid_dspace_host",
                message="dspace_search only accepts the reviewed AERC DSpace search endpoint",
                level="error",
            )
        )
        return None

    objects = _at_path(payload, "_embedded.searchResult._embedded.objects")
    if not isinstance(objects, list):
        diagnostics.append(
            Diagnostic(
                code="items_path_not_found",
                message="dspace_search response has no embedded objects list",
                level="error",
                locator="_embedded.searchResult._embedded.objects",
            )
        )
        return None

    items: list[Mapping[str, Any]] = []
    for index, wrapper in enumerate(objects):
        item = _at_path(wrapper, "_embedded.indexableObject")
        if not isinstance(item, Mapping) or item.get("type") != "item":
            continue
        if item.get("discoverable") is False or item.get("withdrawn") is True:
            continue

        uuid = clean_text(item.get("uuid") or item.get("id"))
        if not uuid or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            uuid,
            re.IGNORECASE,
        ):
            diagnostics.append(
                Diagnostic(
                    code="dspace_item_uuid_invalid",
                    message="DSpace item has no valid UUID",
                    locator=f"_embedded.searchResult._embedded.objects[{index}]",
                )
            )
            continue

        metadata = item.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        api_self = _at_path(item, "_links.self.href")
        parsed_api_self = urlsplit(api_self) if isinstance(api_self, str) else None
        expected_api_path = f"/server/api/core/items/{uuid}"
        if (
            parsed_api_self is None
            or parsed_api_self.scheme != "https"
            or (parsed_api_self.hostname or "").casefold() != _AERC_DSPACE_HOST
            or parsed_api_self.path != expected_api_path
            or parsed_api_self.query
            or parsed_api_self.fragment
        ):
            diagnostics.append(
                Diagnostic(
                    code="dspace_item_self_invalid",
                    message="DSpace item self link is not the reviewed same-host API path",
                    locator=f"_embedded.searchResult._embedded.objects[{index}]",
                )
            )
            continue

        titles = _metadata_values(metadata, "dc.title")
        authors = _metadata_values(metadata, "dc.contributor.author")
        issued = _metadata_values(metadata, "dc.date.issued")
        languages = _metadata_values(metadata, "dc.language.iso") or _metadata_values(metadata, "dc.language")
        dois = _metadata_values(metadata, "dc.identifier.doi")
        prepared: dict[str, Any] = {
            "_external_id": uuid,
            "_title": clean_text(item.get("name")) or (titles[0] if titles else None),
            "_discovery_url": api_self,
            "_canonical_url": f"https://{_AERC_DSPACE_HOST}/items/{uuid}",
            "_authors": authors,
            "_published_at": issued[0] if issued else None,
            "_source_updated_at": item.get("lastModified"),
            "_issue_text": issued[0] if issued else None,
            "_language": languages[0] if languages else None,
            "_media_type": "text/html",
        }
        if dois:
            prepared["_doi"] = dois[0]
        items.append(prepared)
        if len(items) >= max_items:
            break
    return items


def _aerc_wordpress_publication_items(
    payload: Any,
    config: Mapping[str, Any],
    resource: FetchedResource,
    diagnostics: list[Diagnostic],
) -> list[Mapping[str, Any]] | None:
    max_items = config.get("max_items")
    if isinstance(max_items, bool) or max_items != 20:
        diagnostics.append(
            Diagnostic(
                code="invalid_api_config",
                message="aerc_wordpress_publications max_items must be exactly 20",
                level="error",
            )
        )
        return None

    parsed_resource = urlsplit(resource.final_url)
    query = parse_qs(parsed_resource.query, keep_blank_values=True)
    if (
        parsed_resource.scheme != "https"
        or (parsed_resource.hostname or "").casefold() != _AERC_WORDPRESS_HOST
        or parsed_resource.path != "/wp-json/wp/v2/publications"
        or parsed_resource.fragment
        or query
        != {
            "per_page": ["20"],
            "page": ["1"],
            "orderby": ["date"],
            "order": ["desc"],
            "_fields": ["id,date,modified,link,slug,title,type,status"],
        }
    ):
        diagnostics.append(
            Diagnostic(
                code="invalid_aerc_wordpress_endpoint",
                message="aerc_wordpress_publications only accepts the reviewed official endpoint",
                level="error",
            )
        )
        return None
    if not isinstance(payload, list):
        diagnostics.append(
            Diagnostic(
                code="items_path_not_found",
                message="AERC WordPress publications response is not a list",
                level="error",
                locator="$",
            )
        )
        return None

    items: list[Mapping[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            continue
        item_id = item.get("id")
        title = _at_path(item, "title.rendered")
        raw_url = clean_text(item.get("link"))
        parsed_url = urlsplit(raw_url) if raw_url else None
        if (
            isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or item_id <= 0
            or item.get("type") != "publications"
            or item.get("status") != "publish"
            or parsed_url is None
            or parsed_url.scheme != "https"
            or (parsed_url.hostname or "").casefold() != _AERC_WORDPRESS_HOST
            or not parsed_url.path.startswith("/publications/")
            or parsed_url.query
            or parsed_url.fragment
        ):
            diagnostics.append(
                Diagnostic(
                    code="aerc_wordpress_item_invalid",
                    message="AERC WordPress item failed the reviewed metadata contract",
                    locator=f"$[{index}]",
                )
            )
            continue
        items.append(
            {
                "_external_id": str(item_id),
                "_title": unescape(clean_text(title) or ""),
                "_url": raw_url,
                "_published_at": item.get("date"),
                "_source_updated_at": item.get("modified"),
                "_media_type": "text/html",
            }
        )
        if len(items) >= max_items:
            break
    return items


def _carnegie_research_api_items(
    payload: Any,
    config: Mapping[str, Any],
    resource: FetchedResource,
    diagnostics: list[Diagnostic],
) -> list[Mapping[str, Any]] | None:
    max_items = config.get("max_items", 20)
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 50:
        diagnostics.append(
            Diagnostic(
                code="invalid_carnegie_api_config",
                message="carnegie_research_api requires max_items between 1 and 50",
                level="error",
            )
        )
        return None

    parsed_resource = urlsplit(resource.final_url)
    if (parsed_resource.hostname or "").casefold() not in {_CARNEGIE_HOST, f"www.{_CARNEGIE_HOST}"}:
        diagnostics.append(
            Diagnostic(
                code="invalid_api_host",
                message="carnegie_research_api must remain on carnegieendowment.org",
                level="error",
            )
        )
        return None

    docs = payload.get("docs") if isinstance(payload, Mapping) else None
    if not isinstance(docs, list):
        diagnostics.append(
            Diagnostic(
                code="invalid_carnegie_api_payload",
                message="carnegie_research_api payload must contain a 'docs' array",
                level="error",
            )
        )
        return None

    items: list[dict[str, Any]] = []
    include_path_markers: list[str] = list(config.get("include_path_markers") or [])
    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        path_info = doc.get("path")
        canonical_path = ""
        slug = ""
        if isinstance(path_info, Mapping):
            canonical_path = str(path_info.get("canonicalPath") or path_info.get("path") or "").strip()
            slug = str(path_info.get("slug") or "").strip()
        if not canonical_path and slug:
            canonical_path = f"/research/{slug}"
        if not canonical_path:
            continue

        if include_path_markers and not any(marker in canonical_path for marker in include_path_markers):
            continue

        full_url = urljoin("https://carnegieendowment.org", canonical_path)
        title = slug_to_title(slug) if slug else None
        if not title:
            continue

        created_at = doc.get("createdAt")
        updated_at = doc.get("updatedAt")
        doc_id = str(doc.get("id") or "") or None

        items.append(
            {
                "_external_id": doc_id,
                "_title": title,
                "_url": full_url,
                "_published_at": created_at,
                "_source_updated_at": updated_at,
                "_media_type": "text/html",
            }
        )
        if len(items) >= max_items:
            break

    if not items:
        diagnostics.append(
            Diagnostic(
                code="api_no_matching_items",
                message="carnegie_research_api returned no matching research items",
            )
        )
    return items


class JsonApiCollector:
    collector_type = "api"

    def parse(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str = "unknown",
    ) -> ParseResult:
        diagnostics: list[Diagnostic] = []
        candidates: list[CandidateDocument] = []
        try:
            payload = json.loads(resource.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ParseResult(
                diagnostics=(Diagnostic(code="invalid_json", message=str(exc), level="error"),)
            )

        profile = config.get("profile")
        if profile not in {
            None,
            "ajcass_current",
            "aerc_wordpress_publications",
            "dspace_search",
            "carnegie_research_api",
        }:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_api_config",
                        message=f"unsupported API profile: {profile}",
                        level="error",
                    ),
                )
            )

        items_path = config.get("items_path", "")
        field_map = config.get("field_map")
        prepared_items: list[Mapping[str, Any]] | None = None
        if profile == "ajcass_current":
            items_path = "data.channels[].issueInfoList"
            field_map = _AJCASS_FIELD_MAP
            prepared_items = _ajcass_current_items(payload, config, diagnostics)
            if prepared_items is None:
                return ParseResult(diagnostics=tuple(diagnostics))
        elif profile == "dspace_search":
            items_path = "_embedded.searchResult._embedded.objects"
            field_map = _DSPACE_FIELD_MAP
            prepared_items = _dspace_search_items(payload, config, resource, diagnostics)
            if prepared_items is None:
                return ParseResult(diagnostics=tuple(diagnostics))
        elif profile == "aerc_wordpress_publications":
            items_path = "$"
            field_map = _AERC_WORDPRESS_FIELD_MAP
            prepared_items = _aerc_wordpress_publication_items(payload, config, resource, diagnostics)
            if prepared_items is None:
                return ParseResult(diagnostics=tuple(diagnostics))
        elif profile == "carnegie_research_api":
            items_path = "docs"
            field_map = _CARNEGIE_FIELD_MAP
            prepared_items = _carnegie_research_api_items(payload, config, resource, diagnostics)
            if prepared_items is None:
                return ParseResult(diagnostics=tuple(diagnostics))
        if not isinstance(items_path, str) or not isinstance(field_map, Mapping):
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_api_config",
                        message="items_path must be a string and field_map must be an object",
                        level="error",
                    ),
                )
            )

        items = prepared_items if prepared_items is not None else _at_path(payload, items_path)
        if isinstance(items, Mapping):
            items = [items]
        if not isinstance(items, list):
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="items_path_not_found",
                        message="items_path did not resolve to an object or list",
                        level="error",
                        locator=items_path or "$",
                    ),
                )
            )

        for index, item in enumerate(items):
            locator = f"{items_path or '$'}[{index}]"
            if not isinstance(item, Mapping):
                diagnostics.append(
                    Diagnostic(
                        code="item_not_object",
                        message="API item is not an object",
                        locator=locator,
                    )
                )
                continue

            external_id = clean_text(_mapped(item, field_map, "external_id"))
            title = clean_text(_mapped(item, field_map, "title"))
            raw_discovery_url = (
                _mapped(item, field_map, "discovery_url")
                or _mapped(item, field_map, "url")
                or _mapped(item, field_map, "canonical_url")
            )
            discovery_url = absolute_url(raw_discovery_url, resource.final_url) or resource.final_url
            raw_canonical_url = _mapped(item, field_map, "canonical_url")
            if "canonical_url" not in field_map:
                raw_canonical_url = raw_canonical_url or _mapped(item, field_map, "url")
            canonical_url = official_url(absolute_url(raw_canonical_url, resource.final_url), link_role)

            if not any((external_id, title, raw_discovery_url)):
                diagnostics.append(
                    Diagnostic(
                        code="item_has_no_identity",
                        message="API item has no mapped external_id, title, or URL",
                        locator=locator,
                    )
                )
                continue

            published_value = _mapped(item, field_map, "published_at")
            updated_value = _mapped(item, field_map, "source_updated_at")
            crossref_date_precision = None
            crossref_date_parts: list[int] = []
            if _is_crossref_works_resource(resource):
                published_at, crossref_date_precision, crossref_date_parts = _crossref_publication_date(
                    published_value
                )
            else:
                published_at = parse_datetime(published_value)
            source_updated_at = parse_datetime(updated_value)
            if published_value is not None and published_value != "" and published_at is None:
                diagnostics.append(
                    Diagnostic(
                        code="published_date_unparsed",
                        message="mapped publication date could not be parsed",
                        locator=locator,
                    )
                )

            crossref_license_metadata: dict[str, Any] = {}
            if _is_crossref_works_resource(resource):
                if config.get("abstract_license_gate") == "creative_commons":
                    summary, crossref_license_metadata, _next_license_start = crossref_cc_abstract(
                        item,
                        as_of=resource.fetched_at,
                    )
                else:
                    # 用户确认后（2026-08-04）英文期刊 Crossref 摘要不再要求逐条 CC VoR
                    # 许可；仍只保存 Crossref 显式返回的 abstract 字段，不提取正文或生成内容。
                    summary = crossref_direct_abstract(item)
                if published_at is not None and crossref_date_precision is not None:
                    crossref_license_metadata["published_at_provenance"] = {
                        "source": "crossref",
                        "field": str(field_map.get("published_at") or ""),
                        "precision": crossref_date_precision,
                        "date_parts": crossref_date_parts,
                        "source_url": resource.final_url,
                    }
            else:
                summary = clean_text(_mapped(item, field_map, "summary"))
            if profile == "dspace_search" and published_at is not None:
                raw_issued = clean_text(_mapped(item, field_map, "published_at"))
                precision = (
                    "day"
                    if raw_issued and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_issued)
                    else "month"
                    if raw_issued and re.fullmatch(r"\d{4}-\d{2}", raw_issued)
                    else "year"
                )
                crossref_license_metadata["published_at_provenance"] = {
                    "source": "aerc_dspace",
                    "field": "dc.date.issued",
                    "precision": precision,
                    "raw_value": raw_issued,
                    "source_url": resource.final_url,
                }
            if profile == "carnegie_research_api":
                crossref_license_metadata["profile"] = "carnegie_research_api"
            candidates.append(
                CandidateDocument(
                    discovery_url=discovery_url,
                    canonical_url=canonical_url,
                    external_id=external_id,
                    title=title,
                    doi=_doi(_mapped(item, field_map, "doi")),
                    authors=tuple_of_text(_mapped(item, field_map, "authors")),
                    published_at=published_at,
                    source_updated_at=source_updated_at,
                    issue_text=clean_text(
                        _mapped(item, field_map, "issue_text") or _mapped(item, field_map, "issue_date_text")
                    ),
                    summary=summary,
                    summary_kind="api_abstract" if summary else None,
                    summary_source_url=resource.final_url if summary else None,
                    body_text=clean_text(_mapped(item, field_map, "body_text")),
                    language=clean_text(_mapped(item, field_map, "language")),
                    media_type=clean_text(_mapped(item, field_map, "media_type")),
                    resource_sha256=resource.sha256,
                    locator=locator,
                    source_metadata=crossref_license_metadata,
                )
            )

        if not items:
            diagnostics.append(Diagnostic(code="api_items_empty", message="API response contains no items"))
        return ParseResult(candidates=tuple(candidates), diagnostics=tuple(diagnostics))
