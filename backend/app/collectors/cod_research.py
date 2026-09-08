"""Pure parser for the reviewed COD DOI list; no network or storage access."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.collectors.base import CandidateDocument, FetchedResource, clean_text
from app.collectors.json_api import _crossref_publication_date, crossref_cc_abstract


def parse_cod_paper(
    resource: FetchedResource, entry: dict[str, Any], *, start: date, end: date
) -> CandidateDocument:
    item = json.loads(resource.text())["message"]
    doi = str(item.get("DOI", "")).lower()
    if doi != entry["doi"].lower() or item.get("type") != "journal-article":
        raise ValueError("DOI or publication type does not match reviewed scope")
    title = clean_text((item.get("title") or [None])[0])
    authors = tuple(
        filter(
            None,
            (
                clean_text(" ".join(filter(None, (author.get("given"), author.get("family")))))
                for author in item.get("author", [])
            ),
        )
    )
    journal = clean_text((item.get("container-title") or [None])[0])
    if not title or not authors or not journal:
        raise ValueError("Title, author or journal metadata is missing")
    dates = {
        key: _crossref_publication_date(item.get(key))
        for key in ("published-online", "published-print", "published")
    }
    published, precision, parts = next((row for row in dates.values() if row[0]), (None, None, []))
    if not published or not start <= published.date() <= end:
        raise ValueError("Publication is outside the approved date window")
    abstract, license_metadata, _ = crossref_cc_abstract(item, as_of=resource.fetched_at)
    provenance = {"source_url": entry["evidence_url"], "checked_at": entry["checked_at"]}
    metadata = {
        "journal_title": journal,
        "publisher": item.get("publisher"),
        "verification_status": "出版元数据已核对",
        "verified_at": entry["checked_at"],
        "metadata_source_url": resource.request_url,
        "published_at_provenance": {
            "precision": precision,
            "date_parts": parts,
            "source_url": resource.request_url,
        },
        "published_online": dates["published-online"][2],
        "published_print": dates["published-print"][2],
        "topics": entry["topics"],
        "topic_provenance": {**provenance, "kind": "platform_classification"},
        "keywords": [],  # Crossref subjects are not author keywords.
        "field_provenance": {
            field: entry.get("field_provenance", {}).get(field, provenance)
            for field in (
                "research_object",
                "research_method",
                "sample_scope",
                "data_sources",
                "replication_urls",
            )
            if entry.get(field)
        },
        "relevance_basis": entry["relevance_basis"],
        **{
            key: entry[key]
            for key in (
                "research_object",
                "research_method",
                "sample_scope",
                "data_sources",
                "replication_urls",
            )
            if entry.get(key)
        },
        **license_metadata,
    }
    official_url = item.get("resource", {}).get("primary", {}).get("URL") or item.get("URL")
    if not official_url or not official_url.startswith(("https://", "http://")):
        raise ValueError("Crossref has no registered publication URL")
    # Preserve the already-registered Nature URL, whose DOI was absent from the first import.
    canonical = (
        entry["evidence_url"]
        if entry["evidence_url"].startswith("https://www.nature.com/articles/")
        else official_url
    )
    return CandidateDocument(
        discovery_url=resource.request_url,
        canonical_url=canonical,
        doi=doi,
        title=title,
        authors=authors,
        published_at=published,
        language=item.get("language") or "en",
        summary=abstract,
        summary_kind="api_abstract" if abstract else None,
        summary_source_url=resource.request_url if abstract else None,
        source_metadata=metadata,
    )
