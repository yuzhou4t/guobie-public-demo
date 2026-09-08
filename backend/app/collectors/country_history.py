"""Pure historical-page parsing. No requests, database access or raw-page retention."""

import json
import re
from dataclasses import replace
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

from app.collectors.base import CandidateDocument, FetchedResource
from app.collectors.cod_research import parse_cod_paper
from app.collectors.drc_mvp import parse_drc_mvp_listing

COUNTRY_TITLE = re.compile(
    r"democratic republic of (?:the )?congo|r[ée]publique d[ée]mocratique du congo|"
    r"\b(?:DRC|DR Congo|D\.R\. Congo|RDC|Kinshasa|Rubaya|Katanga|Lualaba|Kivu)\b|刚果[（(]?金",
    re.I,
)
TOPIC_RULES = {
    "矿产": r"mining|mineral|cobalt|copper|coltan|extractiv|\bmine",
    "环境": r"environment|forest|pollution|biodiversity",
    "健康": r"health|disease|malaria|epidemic|ebola",
    "治理": r"governance|politic|regulat|corruption",
    "生计": r"livelihood|labor|labour|poverty|household",
    "冲突": r"conflict|violence|armed|war\b",
    "经济发展": r"economic|trade|investment|financ",
}


def parse_crossref_history(
    resource: FetchedResource, start: date, end: date, registry: dict
) -> tuple[list, list, str | None]:
    message = json.loads(resource.text())["message"]
    candidates, skipped = [], []
    for item in message.get("items", []):
        title = " ".join(item.get("title", []))
        doi = str(item.get("DOI") or "").lower()
        if not COUNTRY_TITLE.search(title):
            skipped.append({"doi": doi, "reason": "country_not_explicit_in_title"})
            continue
        entry = registry.get(doi) or {
            "doi": doi,
            "topics": [topic for topic, rule in TOPIC_RULES.items() if re.search(rule, title, re.I)],
            "evidence_url": item.get("URL", ""),
            "checked_at": resource.fetched_at.date().isoformat(),
            "relevance_basis": ("出版元数据题名明确关联刚果（金）或已登记地区；主题按公开题名规则归类。"),
        }
        try:
            candidate = parse_cod_paper(
                replace(resource, body=json.dumps({"message": item}).encode()), entry, start=start, end=end
            )
            if candidate.summary and not str(
                candidate.source_metadata.get("abstract_license_url") or ""
            ).startswith("https://"):
                candidate = replace(
                    candidate,
                    summary=None,
                    summary_kind=None,
                    summary_source_url=None,
                    source_metadata={
                        **candidate.source_metadata,
                        "abstract_omission_reason": "license_https_not_verified",
                    },
                )
                skipped.append({"doi": doi, "reason": "abstract_only_omitted_license_https_not_verified"})
            candidates.append(candidate)
        except (ValueError, KeyError) as exc:
            skipped.append({"doi": doi, "reason": str(exc)})
    cursor = message.get("next-cursor") if message.get("items") else None
    return candidates, skipped, cursor


def next_history_page(resource: FetchedResource) -> str | None:
    tree = HTMLParser(resource.text())
    for node in tree.css(
        'a[rel="next"], .pager__item--next a, .pager-next a, a.next, '
        'li.pagination-next a, a[aria-label="Next page"]'
    ):
        url = urljoin(resource.final_url, node.attributes.get("href", ""))
        if urlsplit(url).hostname == urlsplit(resource.final_url).hostname and url != resource.final_url:
            return url
    for node in tree.css("a[href]"):
        if re.fullmatch(r"(?:suivant|next)\s*[›»→]?", node.text(strip=True), re.I):
            url = urljoin(resource.final_url, node.attributes["href"])
            if urlsplit(url).hostname == urlsplit(resource.final_url).hostname and url != resource.final_url:
                return url
    return None


def parse_history_listing(resource: FetchedResource, config: dict):
    if config.get("profile") == "un_expert_reports":
        dates = {}
        for node in HTMLParser(resource.text()).css("a[href]"):
            code = node.text(strip=True).upper()
            if not re.fullmatch(r"S/20\d{2}/\d+", code):
                continue
            row_text = node.parent.parent.text(separator=" ", strip=True)
            match = re.search(r"\b(\d{1,2} [A-Z][a-z]+ 20\d{2})\b", row_text)
            if match:
                dates[code] = datetime.strptime(match.group(1), "%d %B %Y").date().isoformat()
        config = {**config, "published_dates": dates}
    if config.get("profile") != "ebuteli_reports":
        rows = list(
            parse_drc_mvp_listing(resource, {**config, "max_items": 100}, link_role="official").candidates
        )
        if config.get("profile") == "un_expert_reports":
            rows = [
                replace(
                    row,
                    source_metadata={
                        **row.source_metadata,
                        "published_at_provenance": {
                            "precision": "day",
                            "method": "official_listing_date",
                            "source_url": resource.final_url,
                        },
                    },
                )
                if row.published_at
                else row
                for row in rows
            ]
        return rows
    seen, rows = set(), []
    for node in HTMLParser(resource.text()).css("a[href]"):
        url = urljoin(resource.final_url, node.attributes["href"])
        if (
            urlsplit(url).hostname != urlsplit(resource.final_url).hostname
            or "/publications/rapports/" not in url
            or url in seen
        ):
            continue
        title = node.text(strip=True)
        if len(title) < 20:
            continue
        seen.add(url)
        rows.append(
            CandidateDocument(
                discovery_url=url,
                canonical_url=url,
                title=title,
                language="fr",
                source_metadata={"profile": "ebuteli_reports"},
            )
        )
    return rows
