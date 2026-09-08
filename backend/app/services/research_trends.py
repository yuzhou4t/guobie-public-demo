"""Evidence-linked counts over the complete visible research collection."""

from collections import Counter
from datetime import date, datetime

from app.services.research_frontier import frontier_analysis


def research_trends(items: list[dict], years: int, today: date) -> dict:
    start = date(today.year - years, 1, 1)
    known, unknown, identities = [], [], set()
    for item in items:
        identity = (item.get("doi") or "").lower() or str(item["document_id"])
        if identity in identities:
            continue
        identities.add(identity)
        value = item.get("published_at")
        if not value or item.get("published_at_precision") == "unknown":
            unknown.append(item)
            continue
        published = value.date() if isinstance(value, datetime) else date.fromisoformat(str(value)[:10])
        if start <= published <= today:
            known.append(item)
    annual = Counter(str(item["published_at"])[:4] for item in known)
    topics = sorted({topic for item in known for topic in item.get("metadata", {}).get("topics", [])})

    def citation(item):
        return {
            key: item.get(key)
            for key in (
                "document_id",
                "document_version_id",
                "title",
                "source_name",
                "published_at",
                "published_at_precision",
                "metadata",
            )
        }

    series = []
    for topic in topics:
        members = [i for i in known if topic in i.get("metadata", {}).get("topics", [])]
        counts = Counter(str(i["published_at"])[:4] for i in members)
        series.append(
            {
                "topic": topic,
                "count": len(members),
                "years": [
                    {
                        "year": year,
                        "count": counts[str(year)],
                        "total": annual[str(year)],
                        "share": round(counts[str(year)] / annual[str(year)], 4)
                        if annual[str(year)]
                        else None,
                    }
                    for year in range(start.year, today.year + 1)
                ],
                "representative_papers": [citation(i) for i in members[:3]],
                "methods": sorted(
                    {
                        i.get("metadata", {}).get("research_method") or i.get("metadata", {}).get("method")
                        for i in members
                    }
                    - {None, ""}
                ),
                "research_objects": sorted(
                    {i.get("metadata", {}).get("research_object") for i in members} - {None, ""}
                ),
            }
        )

    all_methods = sorted(
        {i.get("metadata", {}).get("research_method") or i.get("metadata", {}).get("method") for i in known}
        - {None, ""}
    )
    method_series = []
    for method in all_methods:
        members = [
            i
            for i in known
            if (i.get("metadata", {}).get("research_method") or i.get("metadata", {}).get("method")) == method
        ]
        counts = Counter(str(i["published_at"])[:4] for i in members)
        method_series.append(
            {
                "topic": method,
                "count": len(members),
                "years": [
                    {
                        "year": year,
                        "count": counts[str(year)],
                        "total": annual[str(year)],
                        "share": round(counts[str(year)] / annual[str(year)], 4)
                        if annual[str(year)]
                        else None,
                    }
                    for year in range(start.year, today.year + 1)
                ],
                "representative_papers": [citation(i) for i in members[:3]],
            }
        )

    all_sources = sorted({i.get("source_name") for i in known if i.get("source_name")})
    source_series = []
    for source in all_sources:
        members = [i for i in known if i.get("source_name") == source]
        counts = Counter(str(i["published_at"])[:4] for i in members)
        source_series.append(
            {
                "topic": source,
                "count": len(members),
                "years": [
                    {
                        "year": year,
                        "count": counts[str(year)],
                        "total": annual[str(year)],
                        "share": round(counts[str(year)] / annual[str(year)], 4)
                        if annual[str(year)]
                        else None,
                    }
                    for year in range(start.year, today.year + 1)
                ],
                "representative_papers": [citation(i) for i in members[:3]],
            }
        )

    return {
        "frontier": frontier_analysis(known),
        "from": start,
        "to": today,
        "years": years,
        "total": len(known),
        "unknown_date_count": len(unknown),
        "annual_counts": [
            {"year": y, "count": annual[str(y)], "partial": y == today.year}
            for y in range(start.year, today.year + 1)
        ],
        "topics": series,
        "methods": method_series,
        "sources": source_series,
        "unclassified_count": sum(not i.get("metadata", {}).get("topics") for i in known),
        "coverage": {
            "languages": dict(Counter(i.get("language") or "unknown" for i in known)),
            "sources": dict(Counter(i.get("source_name") or "unknown" for i in known)),
            "missing_method": sum(
                not (i.get("metadata", {}).get("research_method") or i.get("metadata", {}).get("method"))
                for i in known
            ),
        },
        "method_note": (
            "本站收录分布；主题可交叉分类。"
            "完整历年加当年截至日；收录及分类覆盖未经可比性验收，不能据此推断学术趋势。"
        ),
    }
