"""Transparent, evidence-linked issue views of the visible research collection.

Rules identify explicit terms, not inferred findings or citation relationships.
The caller owns visibility, identity deduplication and date-window selection.
"""

import re
from collections import Counter

from app.services.research_taxonomy import taxonomy_analysis

# Each group must match. Labels describe the intersection, not a paper's result.
ISSUES = (
    (
        "mine_labor",
        "矿区劳动与社区",
        "采矿与社会",
        (
            r"\b(?:mining|mines?|miners|mineworkers)\b|采矿|矿区",
            r"\blabou?r\b|\bwork(?:ers?|ing)?\b|\bgirls\b|\bwomen\b|communit|劳动|社区",
        ),
    ),
    (
        "mine_land",
        "采矿空间与森林损失",
        "土地与环境",
        (r"\bmining\b|\bmine site\b|采矿", r"deforest|satellite|remote sensing|spatial extent|遥感|森林损失"),
    ),
    (
        "mine_health",
        "矿业暴露与健康",
        "采矿与社会",
        (r"\b(?:mining|miners|cobalt)\b|矿", r"health|uranium|radioactiv|exposure|dysfunction|健康|铀|放射"),
    ),
    (
        "cobalt_trade",
        "钴贸易与责任采购",
        "采矿与社会",
        (r"cobalt|钴", r"trade|exports?|supply|sourcing|procurement|贸易|出口|采购|供应链"),
    ),
    (
        "forest_change",
        "森林覆盖与土地变化",
        "土地与环境",
        (
            r"deforest|forest cover|森林损失|森林覆盖",
            r"dynamics|mapping|quantif|spatial|loss|change|变化|损失|动态",
        ),
    ),
    (
        "forest_policy",
        "森林政策与环境正义",
        "土地与环境",
        (r"deforest|forest degradation|森林", r"polic|governance|justice|政策|治理|正义"),
    ),
    (
        "mpox_spread",
        "猴痘传播与人群风险",
        "传染病防控",
        (r"\bmpox\b|monkeypox|猴痘", r"transmi|spread|risk|epidemiolog|传播|风险"),
    ),
    (
        "mpox_response",
        "猴痘检测与疫苗",
        "传染病防控",
        (r"\bmpox\b|monkeypox|猴痘", r"vaccin|diagnos|testing|rapid.*test|疫苗|检测"),
    ),
    (
        "malaria_resistance",
        "疟疾与媒介耐药",
        "传染病防控",
        (r"malaria|plasmodium|anopheles|疟疾", r"resistan|耐药|抗药"),
    ),
    (
        "care_access",
        "医疗服务与可及性",
        "公共服务",
        (
            r"health care|healthcare|health service|health system|maternal|医疗|卫生服务",
            r"access|utilization|inequal|financ|leadership|可及|不平等|融资",
        ),
    ),
    (
        "violence",
        "性暴力与流离失所",
        "冲突与社会",
        (r"sexual violence|gender.based violence|displac|性暴力|流离失所",),
    ),
    ("food", "粮食安全与生计", "冲突与社会", (r"food security|food insecurity|粮食安全",)),
)

METHODS = (
    (
        "qualitative",
        "访谈与质性",
        r"访谈|质性|话语分析|焦点小组|qualitative|interviews?|focus groups?|ethnograph",
    ),
    ("survey", "调查与横断面", r"问卷|横断面|家庭调查|cross.sectional|survey|questionnaire"),
    ("cohort", "队列与随访", r"队列|随访|纵向研究|cohort|longitudinal|follow.up study"),
    ("model", "计量与建模", r"回归|双重差分|模型|regression|difference.in.differences|modelling|modeling"),
    ("spatial", "遥感与空间", r"遥感|空间动态|景观生态|remote sensing|satellite imagery|geospatial"),
    (
        "experiment",
        "实验与检测",
        r"随机.*试验|测序|质谱|能谱|PCR|randomi[sz]ed.*trial|sequencing|spectrometry|diagnostic accuracy",
    ),
    (
        "review",
        "综述",
        r"文献综述|系统.*综述|systematic review|scoping review|meta.analysis|literature review",
    ),
    ("mixed", "混合方法", r"混合方法|混合横断面|mixed.methods|mixed.cross.sectional"),
)

# Specific shared descriptors; country names and broad topics cannot form edges.
CLUES = (
    ("手工采矿", r"\bartisanal (?:cobalt |gold |diamond )?min(?:ing|es?)\b|手工.*矿"),
    ("钴", r"\bcobalt\b|钴"),
    ("森林损失", r"deforest|森林损失"),
    ("猴痘", r"\bmpox\b|monkeypox|猴痘"),
    ("性暴力", r"sexual violence|性暴力"),
    ("流离失所", r"displac|流离失所"),
    ("粮食安全", r"food (?:in)?security|粮食安全"),
    ("疟疾", r"malaria|plasmodium|疟疾"),
    ("医疗服务", r"health care|healthcare|health service|医疗|卫生服务"),
)

# Reviewed reading comparisons, supported by the titles of both cited works.
# Applied only while both exact works remain visible in the requested window.
READING_PAIRS = (
    (
        "10.1093/sexmed/qfad052",
        "10.1038/s41467-026-75910-z",
        "矿业风险：矿工与出口产品",
        "2023 年研究铜钴矿工的健康问题；2026 年追踪钴氢氧化物出口中的铀。"
        "可从矿区人群与供应链环节两个尺度对读。",
        "追踪出口产品中的铀，将矿业健康问题连接到供应链环节。",
    ),
    (
        "10.1038/s41893-024-01421-8",
        "10.1016/j.rsase.2026.102172",
        "土地影响：森林损失与矿点变化",
        "2024 年研究采矿引发的森林损失；2026 年使用开源遥感量化矿点演变。"
        "可对照土地影响与矿点动态两种观察尺度。",
        "使用开源遥感量化手工矿点演变，提供空间变化的观察方法。",
    ),
    (
        "10.3201/eid2911.230606",
        "10.1016/s1473-3099(26)00141-6",
        "猴痘防控：传播路径与检测工具",
        "2023 年论文报告宫内传播案例；2026 年研究比较五种快速检测。"
        "现有资料支持传播路径与诊断性能两条阅读线索。",
        "比较五种快速检测的诊断性能，与传播路径研究相互参照。",
    ),
)


def _text(value):
    return re.sub(r"<[^>]*>", "", str(value or ""))


def frontier_analysis(items: list[dict]) -> dict:
    papers, memberships = {}, {}
    for item in sorted(items, key=lambda i: (str(i["published_at"]), str(i["document_id"]))):
        metadata = item.get("metadata") or {}
        title = _text(item.get("title"))
        fields = {"title": title, "research_object": _text(metadata.get("research_object"))}
        text = " · ".join(fields.values())
        issue_ids = []
        evidence = {}
        for key, _label, _group, patterns in ISSUES:
            if all(re.search(pattern, text, re.I) for pattern in patterns):
                issue_ids.append(key)
                evidence[key] = [
                    {"field": field, "text": value}
                    for field, value in fields.items()
                    if value and any(re.search(pattern, value, re.I) for pattern in patterns)
                ]
        if not issue_ids:
            continue
        method_fields = {
            "research_method": _text(metadata.get("research_method") or metadata.get("method")),
            "title": title,
        }
        method_evidence = {}
        for key, _label, pattern in METHODS:
            for field, value in method_fields.items():
                if value and re.search(pattern, value, re.I):
                    method_evidence[key] = {"field": field, "text": value}
                    break
        key = str(item["document_version_id"])
        papers[key] = {
            "id": item["document_version_id"],
            "doi": (item.get("doi") or "").lower(),
            "document_id": item["document_id"],
            "title": title,
            "source": item.get("source_name"),
            "date": str(item["published_at"])[:10],
            "precision": item.get("published_at_precision") or "unknown",
            "year": int(str(item["published_at"])[:4]),
            "issues": issue_ids,
            "issue_evidence": evidence,
            "methods": method_evidence,
            "clues": [label for label, pattern in CLUES if re.search(pattern, text, re.I)],
        }
        for issue in issue_ids:
            memberships.setdefault(issue, []).append(key)

    issues, nodes = [], []
    for key, label, group, _patterns in ISSUES:
        ids = memberships.get(key, [])
        if not ids:
            continue
        years = sorted({papers[i]["year"] for i in ids})
        issues.append({"id": key, "label": label, "group": group, "papers": ids, "years": years})
        for year in years:
            members = [i for i in ids if papers[i]["year"] == year]
            nodes.append(
                {"id": f"{key}:{year}", "issue": key, "label": label, "year": year, "papers": members}
            )

    edges = []
    for source in nodes:
        for target in nodes:
            if target["year"] != source["year"] + 1:
                continue
            # Different issue labels may be related only through a shared explicit descriptor.
            source_clues = {clue for i in source["papers"] for clue in papers[i]["clues"]}
            target_clues = {clue for i in target["papers"] for clue in papers[i]["clues"]}
            shared = sorted(source_clues & target_clues)
            if not shared:
                continue
            pairs = []
            for clue in shared:
                a = next(i for i in reversed(source["papers"]) if clue in papers[i]["clues"])
                b = next(i for i in target["papers"] if clue in papers[i]["clues"])
                if papers[a]["document_id"] != papers[b]["document_id"]:
                    pairs.append({"clue": clue, "source": a, "target": b})
            if pairs:
                edges.append(
                    {
                        "id": f"{source['id']}>{target['id']}",
                        "source": source["id"],
                        "target": target["id"],
                        "evidence": pairs,
                    }
                )

    observations, representatives = [], []
    for group in dict.fromkeys(i["group"] for i in issues):
        members = [i for i in issues if i["group"] == group]
        ids = sorted({p for i in members for p in i["papers"]}, key=lambda p: (papers[p]["date"], p))
        if not ids:
            continue
        later = papers[ids[-1]]
        first = next(
            (
                papers[p]
                for p in ids
                if papers[p]["year"] < later["year"] and set(papers[p]["clues"]) & set(later["clues"])
            ),
            later,
        )
        first_label = next(i["label"] for i in members if i["id"] in first["issues"])
        later_label = next(
            (i["label"] for i in members if i["id"] in later["issues"] and i["id"] not in first["issues"]),
            first_label,
        )
        if later["year"] > first["year"]:
            observations.append(
                {
                    "title": group,
                    "text": (
                        f"{first['year']} 年的「{first_label}」与 "
                        f"{later['year']} 年的「{later_label}」提供了跨期阅读线索。"
                    )
                    if first_label != later_label
                    else (
                        f"「{first_label}」在 {first['year']} 和 {later['year']} 年均有研究，"
                        "可对照其研究对象与方法。"
                    ),
                    "papers": list(dict.fromkeys([str(first["id"]), str(later["id"])])),
                }
            )
        representative = later
        representatives.append(
            {"paper": str(representative["id"]), "reason": f"{group} · {later_label}：用于对照上述跨期议题。"}
        )
        if len(observations) == 3:
            break

    by_doi = {p["doi"]: key for key, p in papers.items() if p["doi"]}
    reviewed, selected = [], []
    for early, late, title, text, reason in READING_PAIRS:
        if early in by_doi and late in by_doi:
            reviewed.append({"title": title, "text": text, "papers": [by_doi[early], by_doi[late]]})
            selected.append({"paper": by_doi[late], "reason": reason})
    if reviewed:
        observations = reviewed
        representatives = selected

    return {
        "version": 1,
        "taxonomy": taxonomy_analysis(items, METHODS),
        "issues": issues,
        "papers": papers,
        "nodes": nodes,
        "edges": edges,
        "observations": observations,
        "representatives": representatives[:3],
        "method_categories": [{"id": key, "label": label} for key, label, _pattern in METHODS],
        "coverage": {
            "matched": len(papers),
            "total": len(items),
            "method_matched": sum(bool(p["methods"]) for p in papers.values()),
            "issue_counts": dict(Counter(i for p in papers.values() for i in p["issues"])),
        },
        "note": (
            "依据题名、已登记研究对象与方法识别议题；允许交叉归类。"
            "跨期线索用于比较研究切入点，不代表整个领域的转向、引用或因果关系。"
        ),
    }
