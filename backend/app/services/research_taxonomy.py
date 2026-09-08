"""Shared, evidence-backed country research taxonomy; never rewrites source metadata."""

import html
import re
import unicodedata
from collections import Counter

VERSION = "2026.09.07"
# Keep the IDs used by country_space_templates.json.
DOMAINS = (
    ("economy", "经济发展"),
    ("governance", "政治治理"),
    ("resources_environment", "资源环境"),
    ("population_social", "人口社会"),
    ("international_relations", "国际关系"),
)
# Each tuple is an AND of explicit descriptors. Source/journal names and abstracts
# are deliberately excluded. Broad mineral/metal words alone are not mining evidence.
TOPICS = (
    (
        "macro_fiscal",
        "economy",
        "宏观经济与公共财政",
        (
            r"macroeconom\w*|public financ\w*|fiscal\w*|taxation|budget\w*|inflation|monetary|"
            r"sovereign debt|economic growth|宏观|公共财政|财政|税收|预算|通货膨胀|货币政策|经济增长",
        ),
    ),
    (
        "trade_finance",
        "economy",
        "贸易投资与金融",
        (
            r"trade(?![ -]?offs?\b)|exports?|imports?|investment\w*|financial|banking|foreign direct|"
            r"commerce|investissement|贸易|投资|金融|出口|进口|银行",
        ),
    ),
    (
        "industry_technology",
        "economy",
        "产业发展与技术转型",
        (
            r"industr\w*|manufactur\w*|supply chain\w*|value chain\w*|digital adoption|mobile money|"
            r"technolog\w*.*(?:adoption|transfer|innovation)|产业|工业|制造业|供应链|价值链|技术转型|技术创新|移动支付",
        ),
    ),
    (
        "labor_livelihood",
        "economy",
        "劳动就业与家庭生计",
        (
            r"labou?r\w*|employment|unemployment|livelihood\w*|household income|informal econom\w*|"
            r"informal work|wages?|劳动|就业|生计|家庭收入|工资|非正规经济",
        ),
    ),
    (
        "urban_infrastructure",
        "economy",
        "城市基础设施与区域发展",
        (r"urban\w*|infrastructur\w*|transport\w*|regional development|城市|基础设施|交通|区域发展",),
    ),
    (
        "institutions_accountability",
        "governance",
        "政治制度与公共问责",
        (
            r"political institution\w*|election\w*|electoral\w*|democracy|democrati[sz]ation|"
            r"constitution\w*|accountab\w*|corruption|政治制度|选举|宪政|问责|腐败",
        ),
    ),
    (
        "policy_implementation",
        "governance",
        "政策制定与执行",
        (
            r"policy (?:making|implementation|design|process)|policymaking|public polic\w*|regulat\w*|"
            r"政策制定|政策执行|公共政策|监管",
        ),
    ),
    (
        "law_property",
        "governance",
        "法律产权与地方规范",
        (
            r"customary|coutumi\w*|land rights?|land tenure|legal plural\w*|property rights?|judicial|习惯法|"
            r"地方规范|产权|土地权|司法",
        ),
    ),
    (
        "local_governance",
        "governance",
        "地方治理与分权",
        (r"decentrali\w*|local govern\w*|local authorit\w*|地方治理|地方政府|分权",),
    ),
    (
        "minerals_supply",
        "resources_environment",
        "矿产资源与供应链",
        (
            r"mining|mines?|miners?|mineworkers?|conflict minerals?|mineral (?:supply|resources?|trade|"
            r"extraction)|exploitation miniere|采矿|矿区|矿工|矿产资源|冲突矿产|矿产供应链",
        ),
    ),
    (
        "energy_transition",
        "resources_environment",
        "能源转型与可及性",
        (r"energy|electricity|electrification|hydropower|renewable|energie|电力|能源|水电",),
    ),
    (
        "land_biodiversity",
        "resources_environment",
        "土地森林与生物多样性",
        (
            r"forest\w*|deforest\w*|biodivers\w*|land.?use|protected areas?|forets?|"
            r"森林|土地利用|生物多样性|保护地",
        ),
    ),
    (
        "climate_water",
        "resources_environment",
        "气候水资源与污染",
        (
            r"climat\w*|drought\w*|flood\w*|water quality|water pollution|watershed\w*|pollut\w*|"
            r"气候|干旱|洪水|水质|水资源|流域|污染",
        ),
    ),
    (
        "agriculture_food",
        "resources_environment",
        "农业与粮食系统",
        (r"agricultur\w*|food (?:in)?secur\w*|crop\w*|farming|农业|粮食|作物",),
    ),
    (
        "population_migration",
        "population_social",
        "人口结构与迁移",
        (r"demograph\w*|migration|migrant\w*|displac\w*|refugee\w*|人口结构|人口迁移|移民|难民|流离失所",),
    ),
    (
        "healthcare",
        "population_social",
        "公共卫生与医疗服务",
        (
            r"health\w*|medical|maternal|neonatal|mortality|vaccin\w*|mpox|monkeypox|malaria|ebola|cholera|"
            r"mental health|卫生|医疗|健康|孕产|疫苗|猴痘|疟疾|埃博拉|霍乱",
        ),
    ),
    (
        "education",
        "population_social",
        "教育与人力资本",
        (r"educat\w*|school\w*|human capital|scolai\w*|enseignement|教育|学校|人力资本|入学",),
    ),
    (
        "welfare_inequality",
        "population_social",
        "社会福利、贫困与不平等",
        (r"social (?:welfare|protection)|poverty|inequal\w*|社会福利|社会保障|贫困|不平等",),
    ),
    (
        "community_gender",
        "population_social",
        "社区、性别与家庭",
        (r"communit\w*|gender|household\w*|family|families|sexual violence|社区|性别|家庭|性暴力",),
    ),
    (
        "diplomacy",
        "international_relations",
        "外交与对外政策",
        (r"diploma(?:cy|tic)|foreign polic\w*|外交|对外政策",),
    ),
    (
        "regional_cooperation",
        "international_relations",
        "区域合作与跨境关系",
        (r"regional (?:integration|cooperation)|cross.border|afcfta|区域合作|区域一体化|跨境",),
    ),
    (
        "trade_rules",
        "international_relations",
        "国际贸易规则与发展合作",
        (
            r"trade (?:agreement\w*|rules?|polic\w*)|development cooperation|free trade|libre.echange|"
            r"贸易规则|贸易协定|自由贸易|发展合作",
        ),
    ),
    (
        "security_peace",
        "international_relations",
        "安全冲突与和平建设",
        (r"armed|rebel\w*|militia\w*|peace\w*|monusco|warfare|武装|叛乱|安全冲突|维和|和平建设",),
    ),
    (
        "organizations_aid",
        "international_relations",
        "国际组织与外部援助",
        (
            r"international organi[sz]ation\w*|foreign aid|development assistance|humanitarian aid|国际组织|"
            r"外部援助|对外援助|发展援助|人道援助",
        ),
    ),
)
LABELS = dict(DOMAINS)
TOPIC_BY_ID = {row[0]: row for row in TOPICS}
COMPILED = [(row, [re.compile(r"(?<![a-z])(?:" + p + r")(?![a-z])", re.I) for p in row[3]]) for row in TOPICS]
LEGACY_GROUPS = {
    "采矿与社会": "资源环境",
    "土地与环境": "资源环境",
    "传染病防控": "人口社会",
    "公共服务": "人口社会",
    "冲突与社会": "国际关系",
}
# Stable direction keys remain unchanged; their grouping uses the same taxonomy.
DIRECTION_TOPICS = {
    "vaccination": "healthcare",
    "outbreak-response": "healthcare",
    "maternal-child": "healthcare",
    "health-access": "healthcare",
    "malaria-control": "healthcare",
    "mental-health": "healthcare",
    "forest-land": "land_biodiversity",
    "water-pollution": "climate_water",
    "climate-adaptation": "climate_water",
    "biodiversity": "land_biodiversity",
    "artisanal-mining": "labor_livelihood",
    "cobalt-chain": "industry_technology",
    "mining-exposure": "minerals_supply",
    "mineral-traceability": "minerals_supply",
    "displacement": "population_migration",
    "armed-governance": "security_peace",
    "peacebuilding": "security_peace",
    "gender-violence": "community_gender",
    "agriculture-food": "agriculture_food",
    "household-livelihoods": "labor_livelihood",
    "urban-development": "urban_infrastructure",
    "energy-access": "energy_transition",
    "investment-industry": "industry_technology",
    "public-finance": "macro_fiscal",
    "decentralization": "local_governance",
    "political-institutions": "institutions_accountability",
    "customary-law": "law_property",
    "regional-trade": "trade_rules",
    "education-access": "education",
    "digital-services": "industry_technology",
}
# Only exact source-topic aliases supplement term matching; these are not findings.
ALIASES = {
    "经济": "macro_fiscal",
    "经济发展": "macro_fiscal",
    "金融": "trade_finance",
    "贸易": "trade_finance",
    "矿产": "minerals_supply",
    "健康": "healthcare",
    "教育": "education",
    "生计": "labor_livelihood",
    "冲突": "security_peace",
    "森林": "land_biodiversity",
    "治理": "policy_implementation",
}


def normalized(value):
    value = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


def classify(item: dict) -> dict:
    metadata = item.get("metadata") or {}
    fields = {"title": item.get("title") or "", "research_object": metadata.get("research_object") or ""}
    for key in ("keywords", "topics"):
        value = metadata.get(key) or []
        fields[key] = " · ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    texts = {key: normalized(value) for key, value in fields.items()}
    combined = " · ".join(texts.values())
    evidence = {}
    for (key, _domain, _label, _), patterns in COMPILED:
        if all(pattern.search(combined) for pattern in patterns):
            evidence[key] = [
                {"field": field, "text": fields[field]}
                for field, text in texts.items()
                if text and any(p.search(text) for p in patterns)
            ]
    raw_topics = metadata.get("topics") or []
    for raw in raw_topics if isinstance(raw_topics, list) else [raw_topics]:
        if raw in ALIASES:
            evidence.setdefault(ALIASES[raw], [{"field": "topics", "text": str(raw)}])
    topic_ids = [row[0] for row in TOPICS if row[0] in evidence]
    domain_ids = [key for key, _ in DOMAINS if any(TOPIC_BY_ID[t][1] == key for t in topic_ids)]
    return {"topic_ids": topic_ids, "domain_ids": domain_ids, "evidence": evidence, "version": VERSION}


def taxonomy_analysis(items: list[dict], methods: tuple) -> dict:
    """The caller has selected the date window; defensively deduplicate identities."""
    unique = {}
    for item in items:
        identity = (item.get("doi") or "").casefold() or str(item["document_id"])
        unique.setdefault(identity, item)
    papers = {}
    for item in unique.values():
        result = classify(item)
        metadata = item.get("metadata") or {}
        method_evidence = {}
        for key, _, pattern in methods:
            for field, value in (
                ("research_method", metadata.get("research_method") or metadata.get("method")),
                ("title", item.get("title")),
            ):
                if value and re.search(pattern, str(value), re.I):
                    method_evidence[key] = {"field": field, "text": str(value)}
                    break
        papers[str(item["document_version_id"])] = {
            "id": item["document_version_id"],
            "document_id": item["document_id"],
            "title": item.get("title"),
            "source": item.get("source_name"),
            "date": str(item["published_at"])[:10],
            "precision": item.get("published_at_precision") or "unknown",
            "year": int(str(item["published_at"])[:4]),
            "issues": result["topic_ids"],
            "domains": result["domain_ids"],
            "issue_evidence": result["evidence"],
            "methods": method_evidence,
        }
    total = len(papers)
    years = sorted({p["year"] for p in papers.values()})
    annual = Counter(p["year"] for p in papers.values())

    def series(ids):
        counts = Counter(papers[i]["year"] for i in ids)
        return [
            {
                "year": y,
                "count": counts[y],
                "total": annual[y],
                "share": round(counts[y] / annual[y], 4) if annual[y] else None,
            }
            for y in years
        ]

    domains = []
    for key, label in DOMAINS:
        ids = [k for k, p in papers.items() if key in p["domains"]]
        domains.append(
            {
                "id": key,
                "label": label,
                "papers": ids,
                "count": len(ids),
                "share": round(len(ids) / total, 4) if total else None,
                "years": series(ids),
            }
        )
    issues = []
    for key, domain, label, _ in TOPICS:
        ids = [k for k, p in papers.items() if key in p["issues"]]
        issues.append(
            {
                "id": key,
                "label": label,
                "domain_id": domain,
                "group": LABELS[domain],
                "papers": ids,
                "count": len(ids),
                "years": series(ids),
            }
        )
    unclassified = [key for key, p in papers.items() if not p["issues"]]
    return {
        "version": VERSION,
        "domains": domains,
        "issues": issues,
        "papers": papers,
        "legacy_groups": LEGACY_GROUPS,
        "unclassified": unclassified,
        "coverage": {
            "total": total,
            "classified": total - len(unclassified),
            "unclassified": len(unclassified),
            "share": round((total - len(unclassified)) / total, 4) if total else None,
        },
        "note": "分母为当前国家与时段内去重论文总数；允许跨领域归类，占比之和可超过 100%。",
    }


def direction_taxonomy(key: str, theme: str) -> dict:
    topic_id = DIRECTION_TOPICS.get(key) or ALIASES.get(theme)
    row = TOPIC_BY_ID.get(topic_id)
    return {
        "taxonomy_version": VERSION,
        "domain_id": row[1] if row else None,
        "domain_label": LABELS[row[1]] if row else "待分类",
        "topic_id": topic_id,
        "topic_label": row[2] if row else theme,
        "legacy_theme": theme,
    }
