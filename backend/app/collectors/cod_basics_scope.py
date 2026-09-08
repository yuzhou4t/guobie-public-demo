"""User-approved COD supplement, independent of the original four-country grid."""

COD_BASICS_KEY = "world-bank-cod-country-basics-v1"
COD_BASICS_YEARS = tuple(range(2015, 2026))
# code, metric, unit, label, theme, definition
COD_BASICS_INDICATORS = (
    (
        "AG.SRF.TOTL.K2",
        "surface_area",
        "km²",
        "国土面积",
        "基础国情",
        "国土总面积，包括内陆水体和部分沿海水道；年度变动可能来自数据修订。",
    ),
    (
        "EN.POP.DNST",
        "population_density",
        "人/km²",
        "人口密度",
        "基础国情",
        "年中人口除以陆地面积，不以包含水域的国土总面积作为分母。",
    ),
    (
        "SG.GEN.PARL.ZS",
        "women_parliament_seats",
        "%",
        "女性议员席位比例",
        "政治治理",
        "女性占国家单院制议会或两院制下院席位的比例，不包含上院。",
    ),
    (
        "NY.GDP.PCAP.CD",
        "gdp_per_capita_current_usd",
        "USD/人",
        "人均 GDP（现价美元）",
        "经济发展",
        "现价美元 GDP 除以年中人口；不是购买力平价或不变价序列。",
    ),
    (
        "SP.URB.TOTL.IN.ZS",
        "urban_population_share",
        "%",
        "城镇人口占比",
        "人口社会",
        "按各国统计机构的城镇定义划分的城镇人口占总人口比例。",
    ),
    (
        "SP.DYN.LE00.IN",
        "life_expectancy_at_birth",
        "年",
        "出生时预期寿命",
        "人口社会",
        "假定出生时年龄别死亡率持续不变，新生儿预期可生存的年数。",
    ),
    (
        "EG.ELC.ACCS.ZS",
        "electricity_access_share",
        "%",
        "电力可及率",
        "人口社会",
        "能够获得电力的人口占总人口比例。",
    ),
    (
        "AG.LND.FRST.ZS",
        "forest_area_share",
        "%",
        "森林覆盖率",
        "资源环境",
        "森林面积占陆地面积比例；森林定义遵循 FAO 与来源元数据。",
    ),
)

# User requested recent-year supplementation on 2026-09-05; keep the original grid intact.
COD_RECENT_KEY = "world-bank-cod-core-2025-v1"
COD_RECENT_YEARS = (2025,)
