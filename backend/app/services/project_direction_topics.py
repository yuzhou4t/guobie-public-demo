"""Evidence-backed reading directions, matched against recorded paper metadata.

These are editable research prompts, not inferred findings. Rules never rewrite the
library's topic labels or establish policy, event, or data citation relationships.
"""

import html
import re
import unicodedata

from app.services.project_directions import direction_proposal
from app.services.research_taxonomy import DOMAINS, direction_taxonomy

# key, theme, title, subject terms, two concrete questions, suggested approach, needed material
TOPICS = (
    (
        "vaccination",
        "健康",
        "疫苗接种的组织障碍与社区信任",
        r"vaccin\w*|immuniz\w*|immunis\w*|疫苗|接种",
        "接种服务的组织方式、可达性与接受意愿如何区分？",
        "不同地区和人群的接种障碍有哪些可比较的证据？",
        "比较接种调查的抽样与问卷口径，结合访谈梳理组织和社会因素。",
        "地区接种记录、服务覆盖口径、调查问卷与访谈材料",
    ),
    (
        "outbreak-response",
        "健康",
        "传染病暴发中的监测与公共卫生应对",
        r"ebola\w*|mpox|cholera|outbreak\w*|epidem(?:ic\w*|ie\w*)|疫情|埃博拉|猴痘|霍乱",
        "病例监测、风险沟通与社区参与分别如何组织？",
        "跨地区应对研究的观察时期与评价指标能否比较？",
        "建立疫情与应对措施时间线，按地区、时期和研究设计比较文献。",
        "疫情通报、监测定义、措施实施时间和地区卫生资源资料",
    ),
    (
        "maternal-child",
        "健康",
        "妇幼健康与基层孕产服务的可及性",
        r"maternal|neonatal|maternity|pregnan\w*|infant\w*|paediatr\w*|pediatr\w*|child\w*.*(?:health|mortality|nutrit|vaccin|immun)|(?:health|mortality|nutrit).*child\w*|妇幼|孕产|新生儿",
        "费用、转诊距离与机构条件怎样进入妇幼健康研究？",
        "不同人群的服务利用和健康结局采用了哪些测量口径？",
        "按社区、基层机构和转诊医院分层比较样本与服务环节。",
        "孕产服务记录、转诊路径、费用政策及样本纳入标准",
    ),
    (
        "health-access",
        "健康",
        "医疗服务可及性与卫生体系组织",
        r"health\s*(?:care|system\w*|service\w*|insurance)|hospital\w*|soins|systeme de sante|医疗|卫生体系",
        "人员、费用和机构分布怎样影响服务覆盖的研究设计？",
        "政策规定的覆盖范围与实际服务利用应如何分别核对？",
        "梳理卫生体系制度与机构分布，对照服务利用研究的范围。",
        "卫生机构和人员统计、支付制度、服务利用调查",
    ),
    (
        "malaria-control",
        "健康",
        "疟疾防控的地区差异与实施条件",
        r"malaria|paludis\w*|plasmodium|疟疾",
        "防控研究如何区分传播环境、干预覆盖与人群差异？",
        "不同地区的检测、治疗和预防指标是否具有可比性？",
        "按地区与防控环节建立研究比较矩阵，标注诊断和抽样差异。",
        "地区监测数据、防控覆盖、诊断标准和调查时段",
    ),
    (
        "mental-health",
        "健康",
        "心理健康、社会压力与支持服务",
        r"mental health|psycholog\w*|trauma\w*|post.?traumatic|心理|创伤",
        "研究如何记录社会压力、心理状态与支持服务？",
        "不同量表、样本与调查环境对结论适用范围有何限制？",
        "比较量表和抽样设计，分别整理经历、健康状况与服务条件。",
        "量表版本、调查伦理与抽样说明、支持服务资料",
    ),
    (
        "forest-land",
        "环境",
        "森林变化、土地利用与保护治理",
        r"forest\w*|deforest\w*|land.?use|foret\w*|森林|土地利用",
        "研究如何识别森林变化及其土地利用背景？",
        "保护制度、道路与居民点资料能够支持哪些空间比较？",
        "对照遥感研究的分辨率和分类体系，再比较地方治理材料。",
        "土地覆盖图、保护地边界、道路与居民点资料及方法说明",
    ),
    (
        "water-pollution",
        "环境",
        "水质污染与流域环境治理",
        r"water quality|water pollution|river\w*|watershed\w*|qualite.*eau|水质|水污染|流域",
        "污染测量的采样地点、季节与标准如何影响比较？",
        "现有资料能否区分生活、工业及其他污染来源？",
        "建立采样点与指标台账，比较研究范围及监管责任。",
        "水质采样记录、检测标准、流域边界和污染源资料",
    ),
    (
        "climate-adaptation",
        "环境",
        "气候变化与地方适应策略",
        r"climat\w*|drought\w*|flood\w*|气候|干旱|洪水",
        "研究如何界定气候暴露与家庭、产业或地方的适应行动？",
        "不同时间尺度和地区的脆弱性指标可否比较？",
        "对照气候资料与地方调查，明确观察尺度及适应指标。",
        "气候序列、灾害记录、地方适应计划与调查样本",
    ),
    (
        "biodiversity",
        "环境",
        "生物多样性保护与社区资源利用",
        r"biodivers\w*|conserv\w*|phytodivers\w*|protected area\w*|生物多样性|保护地",
        "生态调查与社区资源利用研究分别覆盖哪些对象？",
        "保护措施、利用权利与生计诉求应如何并列比较？",
        "结合物种调查与社区研究，区分生态指标和制度观察。",
        "生态调查、保护规划、利用权利及社区调查资料",
    ),
    (
        "artisanal-mining",
        "矿产",
        "手工采矿的正规化与劳动条件",
        r"artisanal|small.scale min\w*|exploitation artisanale|手工采矿|小规模采矿",
        "正规化措施涉及哪些许可、组织和劳动条件？",
        "矿工与企业、合作社及监管者的材料如何交叉核对？",
        "比较矿区制度与劳动调查，按主体建立权利和责任矩阵。",
        "矿区许可、合作社与劳动调查、监管实施记录",
    ),
    (
        "cobalt-chain",
        "矿产",
        "铜钴供应链与本地加工条件",
        r"cobalt|copper|cuivre|铜|钴",
        "研究如何描述开采、加工、出口等环节及其参与主体？",
        "贸易和产业资料能否支持对本地加工条件的比较？",
        "绘制供应链环节及资料对应表，核对贸易与产业统计口径。",
        "产销和贸易记录、加工项目资料、规则文本及企业披露",
    ),
    (
        "mining-exposure",
        "矿产",
        "矿区重金属暴露与环境健康",
        r"heavy metal\w*|metal exposure|trace element\w*|toxic\w*|contamin\w*|重金属|污染暴露",
        "研究如何区分环境浓度、人体暴露与健康观察？",
        "矿区与对照地区的采样和测量条件能否比较？",
        "对照暴露研究的采样、检测和对照设计，不将相关性直接解释为因果。",
        "采样位置、检测限、暴露指标及对照样本说明",
    ),
    (
        "mineral-traceability",
        "矿产",
        "冲突矿产追溯与供应链尽责治理",
        r"conflict mineral\w*|traceab\w*|due diligence|冲突矿产|可追溯|尽责",
        "追溯和尽责制度分别覆盖哪些矿种、主体与环节？",
        "制度文本、审计材料与地方研究之间有哪些可核对差异？",
        "比较制度适用范围和审计依据，建立来源与主张对照表。",
        "认证和尽责规则、审计资料、矿区与贸易链条调查",
    ),
    (
        "displacement",
        "冲突",
        "人口流离失所、安置与公共服务",
        r"displac\w*|refugee\w*|deplace\w*|refugie\w*|难民|流离失所",
        "研究如何区分流离失所者、返乡者与接收社区？",
        "安置环境和公共服务的差异有哪些可比较资料？",
        "比较调查人群与安置类型，标注迁移阶段和观察时段。",
        "人口与安置统计、调查定义、接收社区及服务资料",
    ),
    (
        "armed-governance",
        "冲突",
        "武装冲突中的地方秩序与治理主体",
        r"armed|rebel\w*|militia\w*|m23|armed conflict|武装|叛乱",
        "研究怎样记录冲突地区的权威、规则与资源控制？",
        "不同主体的叙述与可定位事件证据如何区分？",
        "结合事件时间线与地方研究，按主体和空间范围比较材料。",
        "多来源事件记录、地方制度和社区研究材料",
    ),
    (
        "peacebuilding",
        "冲突",
        "维和行动与地方和平建设",
        r"peace\w*|peacekeep\w*|monusco|paix|维和|和平建设",
        "国际行动与地方和平实践分别设定哪些目标和任务？",
        "如何区分任务授权、实施过程与地方反馈？",
        "按行动阶段整理授权、实施与评估材料，比较不同研究视角。",
        "任务授权、行动报告、地方调查与独立评估",
    ),
    (
        "gender-violence",
        "冲突",
        "性别暴力与保护机制",
        r"sexual violence|gender.based violence|violence sexuelle|性别暴力|性暴力",
        "研究使用什么定义和方法记录暴力及保护服务？",
        "不同样本和支持渠道的覆盖边界应如何说明？",
        "比较资料定义、伦理和取样方式，梳理保护服务的责任链。",
        "匿名化调查、伦理说明、保护政策与服务覆盖资料",
    ),
    (
        "agriculture-food",
        "生计",
        "农业生产与粮食安全条件",
        r"agricultur\w*|food security|food insecur\w*|crop\w*|farming|粮食|农业|作物",
        "研究如何描述生产条件、市场联系与家庭粮食获取？",
        "产量、收入和粮食安全指标对应哪些不同观察尺度？",
        "对照农业与家庭调查，按生产环节和统计期比较指标。",
        "农业产量、价格、家庭调查及粮食安全指标定义",
    ),
    (
        "household-livelihoods",
        "生计",
        "家庭收入、非正规就业与生计选择",
        r"livelihood\w*|informal|household income|emploi|生计|非正规|家庭收入",
        "家庭如何组合收入来源、劳动与资源利用活动？",
        "不同调查对非正规就业和家庭收入的定义是否一致？",
        "比较家庭调查和访谈中的收入构成与季节性安排。",
        "家庭收支、就业调查、访谈与季节性资料",
    ),
    (
        "urban-development",
        "经济发展",
        "城市扩张、基础设施与公共服务",
        r"urban\w*|infrastructur\w*|transport\w*|城市|基础设施|交通",
        "研究如何描述城市扩张、设施布局和服务分布？",
        "中心城区、边缘地区和不同城市的资料可否比较？",
        "按空间尺度比较城市研究，核对人口、设施和服务指标。",
        "城市规划、人口与设施统计、空间数据和服务调查",
    ),
    (
        "energy-access",
        "经济发展",
        "电力可及性与能源转型条件",
        r"energy|electricity|electrification|hydropower|energie|电力|能源|水电",
        "电力覆盖、可靠性与可负担性在研究中如何分别测量？",
        "能源项目和地方生产生活条件有哪些可对照的材料？",
        "对照能源制度、项目资料和用户调查，区分设施建设与实际用能。",
        "电力覆盖、停电和价格资料、项目记录及用户调查",
    ),
    (
        "investment-industry",
        "经济发展",
        "外商投资与产业发展条件",
        r"foreign direct investment|investment|industrial\w*|investissement\w*|投资|产业|工业化",
        "投资项目与产业活动的关联在研究中如何界定？",
        "项目级与宏观统计能分别回答哪些地方发展问题？",
        "按行业和地区梳理项目，再对照投资、就业与产业统计口径。",
        "投资统计、项目清单、产业和就业记录",
    ),
    (
        "public-finance",
        "治理",
        "公共财政、税收与地方资源分配",
        r"fiscal\w*|tax(?:es|ation|able)?|taxe(?:s)?|public financ\w*|budget\w*|revenue\w*|财政|税收|预算",
        "中央与地方财政权责和资源分配如何记录？",
        "预算、执行与审计资料的时期和范围能否对齐？",
        "建立财政制度与收支口径对照表，区分预算安排和实际执行。",
        "预算决算、税费规则、财政转移与审计报告",
    ),
    (
        "decentralization",
        "治理",
        "分权改革与地方政策执行",
        r"decentrali\w*|local govern\w*|local authorit\w*|分权|地方治理|地方政府",
        "改革如何分配中央、地方和基层机构的职责？",
        "地方执行研究反映了哪些资源与组织条件？",
        "比较改革文本和地区案例，沿职责、资源与执行环节梳理证据。",
        "分权法规、地方预算、人事职责和案例访谈",
    ),
    (
        "political-institutions",
        "治理",
        "政治制度、选举与公共问责",
        r"election\w*|electoral\w*|democra(?:cy|tization|tisation)|presidential\w*|constitution\w*|accountab\w*|选举|问责|宪政",
        "研究如何界定制度安排、政治参与与问责机制？",
        "制度规则与实践资料之间有哪些可核验的比较维度？",
        "按制度环节比较法律文本、选举资料与案例研究。",
        "制度文本、选举与议会资料、问责记录及案例调查",
    ),
    (
        "customary-law",
        "治理",
        "习惯规范、土地权利与司法实践",
        r"customary|coutumi\w*|land right\w*|land tenure|justice|legal plural\w*|习惯法|土地权|司法",
        "国家法律与地方规范分别如何安排权利和争议解决？",
        "不同类型案件与社区研究提供了哪些实践证据？",
        "比较规则文本、司法案例与社区调查，标注规范适用范围。",
        "法律与习惯规范、匿名化案例、土地和社区调查",
    ),
    (
        "regional-trade",
        "贸易",
        "区域一体化与跨境贸易规则",
        (
            r"trade(?![ -]?offs?\b)|commerce|cross.border|regional integration|afcfta|libre.echange"
            r"|贸易|区域一体化|跨境"
        ),
        "区域协定、通关安排与实际贸易研究分别关注哪些环节？",
        "不同贸易走廊、商品和时期的统计口径能否比较？",
        "对照制度时间线与贸易记录，按商品和走廊比较材料。",
        "区域协定、关税和通关规则、贸易统计及边境调查",
    ),
    (
        "education-access",
        "教育",
        "教育机会、学校条件与地区差异",
        r"educat\w*|school\w*|scolai\w*|enseignement|教育|学校|入学",
        "研究如何区分入学机会、学校条件与学习结果？",
        "城乡和不同地区的样本、学段及测量指标可否比较？",
        "按地区与学段梳理教育调查，比较设施、师资和学生资料。",
        "教育统计、学校与家庭调查、学段定义及测量工具",
    ),
    (
        "digital-services",
        "经济发展",
        "数字技术采用与公共服务条件",
        r"digital|artificial intelligence|mobile money|numerique|数字|人工智能|移动支付",
        "研究中的数字应用解决什么具体问题，涉及哪些使用者？",
        "基础设施、组织能力和实际采用之间如何区分？",
        "比较应用场景和采用研究，核对评价指标及用户范围。",
        "应用项目资料、网络覆盖、用户调查与评价设计",
    ),
)


def normalized(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


COMPILED = [(spec, re.compile(r"(?<![a-z])(?:" + spec[3] + r")(?![a-z])")) for spec in TOPICS]


def paper_text(item: dict) -> str:
    metadata = item.get("metadata") or {}
    keywords = metadata.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    return normalized(" ".join([item.get("title") or "", metadata.get("research_object") or "", *keywords]))


def direction_groups(items: list[dict], query: str | None = None) -> tuple[list[dict], list[dict]]:
    """Return concrete groups plus their filtered corpus, without changing stored topics."""
    unique = {}
    for item in items:
        identity = (item.get("doi") or "").casefold() or str(item["document_id"])
        unique.setdefault(identity, item)
    items = list(unique.values())
    texts = {p["document_version_id"]: paper_text(p) for p in items}
    needle = normalized((query or "").strip())
    matched_specs = [spec for spec, _ in COMPILED if needle and needle in normalized(" ".join(spec[1:3]))]
    query_patterns = [pattern for spec, pattern in COMPILED if spec in matched_specs]
    if needle:
        items = [
            p
            for p in items
            if needle
            in normalized(
                " ".join(
                    [
                        p.get("title") or "",
                        p.get("source_name") or "",
                        *(p.get("metadata") or {}).get("topics", []),
                    ]
                )
            )
            or any(pattern.search(texts[p["document_version_id"]]) for pattern in query_patterns)
        ]
    rows = []
    represented_themes = set()
    for spec, pattern in COMPILED:
        key, theme, title, _, question, followup, approach, needs = spec
        members = [p for p in items if pattern.search(texts[p["document_version_id"]])]
        if key == "vaccination":
            members = [
                p
                for p in members
                if re.search(
                    r"accept|coverage|status|hesita|barrier|uptake|zero.dose|under.vaccin|programm|campaign|community|survey|awareness|perception|access|knowledge|determinant|utili[sz]|接种|疫苗",
                    texts[p["document_version_id"]],
                )
                and not re.search(
                    r"veterinar|cattle|bovine|livestock|theileria|muguga|poultry|兽医|牲畜",
                    texts[p["document_version_id"]],
                )
            ]
        if key in {"artisanal-mining", "mineral-traceability"}:
            members = [
                p
                for p in members
                if re.search(
                    r"\bmin(?:e|es|ing|eral\w*)\b|minier|coltan|tantal|cobalt|采矿|矿产|矿区",
                    texts[p["document_version_id"]],
                )
            ]
        if key == "cobalt-chain":
            members = [
                p
                for p in members
                if re.search(
                    r"supply|chain|trade|export|process|extract|refin|batter|invest|production|govern|industrial|出口|加工|供应|产业",
                    texts[p["document_version_id"]],
                )
            ]
        if key == "mining-exposure":
            members = [
                p
                for p in members
                if re.search(
                    r"mining|mine[s ]|minier|cobalt|copper|cuivre|矿|铜|钴",
                    texts[p["document_version_id"]],
                )
            ]
        if not members:
            continue
        represented_themes.add(theme)
        rows.append(
            {
                "direction_key": key,
                **direction_taxonomy(key, theme),
                "title": direction_taxonomy(key, theme)["domain_label"],
                "direction_title": title,
                "research_question": question,
                "subquestions": [question, followup],
                "suggested_approach": approach,
                "material_needs": needs,
                "direction_reason": "该方向将研究对象限定为“" + title + "”，可先从以下问题比较现有研究。",
                "matching_basis": "按已登记题名、研究对象或关键词匹配；属于选题线索，需进一步阅读原文。",
                "members": members,
            }
        )
    themes = sorted({t for p in items for t in (p.get("metadata") or {}).get("topics", [])})
    for theme in themes:
        if theme in represented_themes:
            continue
        title, question, reason = direction_proposal(theme)
        rows.append(
            {
                "direction_key": "theme:" + theme,
                **direction_taxonomy("theme:" + theme, theme),
                "title": direction_taxonomy("theme:" + theme, theme)["domain_label"],
                "direction_title": title,
                "research_question": question,
                "subquestions": [question],
                "suggested_approach": "先比较代表论文的研究问题、对象与方法，再缩小研究范围。",
                "material_needs": "代表论文原文、研究设计与数据可得性说明",
                "direction_reason": reason,
                "matching_basis": "来自已有主题标签；当前未进一步细分。",
                "members": [p for p in items if theme in (p.get("metadata") or {}).get("topics", [])],
            }
        )
    order = {key: index for index, (key, _) in enumerate(DOMAINS)}
    rows.sort(key=lambda row: order.get(row["domain_id"], len(order)))
    return rows, items
