/* ============================================================
   国别智枢 · 资源门户数据层（50 信源 · 5 类 × 10）
   生成自 data/source_catalog.json，分组依据
   《国别智枢_采集进度与人工核验说明_50信源完整分组版》(2026-07-15)。
   字段规范与新增流程见 docs/外部资源与数据库导航元数据规范表.md。
   ============================================================ */

    const ICONS = {
      wb: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#0072bc"/><path d="M16 4C9.37 4 4 9.37 4 16s5.37 12 12 12 12-5.37 12-12S22.63 4 16 4zm0 2.5c2.4 0 4.6 1 6.2 2.6L16 15.3 9.8 9.1C11.4 7.5 13.6 6.5 16 6.5zm-9.5 9.5c0-2.4 1-4.6 2.6-6.2l6.2 6.2-6.2 6.2C7.5 20.6 6.5 18.4 6.5 16zm9.5 9.5c-2.4 0-4.6-1-6.2-2.6l6.2-6.2 6.2 6.2c-1.6 1.6-3.8 2.6-6.2 2.6zm9.5-9.5c0 2.4-1 4.6-2.6 6.2l-6.2-6.2 6.2-6.2c1.6 1.6 2.6 3.8 2.6 6.2z" fill="#fff"/></svg>`,
      un: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#009edb"/><path d="M16 7a9 9 0 1 0 9 9 9 9 0 0 0-9-9zm0 2.2a6.8 6.8 0 1 1-6.8 6.8 6.8 6.8 0 0 1 6.8-6.8zm0 2a4.8 4.8 0 1 0 4.8 4.8 4.8 4.8 0 0 0-4.8-4.8z" fill="#fff"/><circle cx="16" cy="16" r="2" fill="#009edb"/></svg>`,
      imf: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#002d62"/><text x="50%" y="56%" dominant-baseline="middle" text-anchor="middle" font-weight="800" font-size="10" fill="#ffd700">IMF</text></svg>`,
      oecd: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#006699"/><path d="M10 16l4-7h8l-4 7 4 7h-8z" fill="#ffcc00"/></svg>`,
      unctad: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#0077b6"/><path d="M9 16a7 7 0 0 1 14 0" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/><circle cx="16" cy="16" r="3" fill="#90e0ef"/></svg>`,
      wits: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#1e3a8a"/><text x="50%" y="56%" dominant-baseline="middle" text-anchor="middle" font-weight="800" font-size="8.5" fill="#60a5fa">WITS</text></svg>`,
      stats_gov: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#c2410c"/><rect x="9" y="16" width="3" height="7" rx="1" fill="#fff"/><rect x="14.5" y="11" width="3" height="12" rx="1" fill="#fff"/><rect x="20" y="7" width="3" height="16" rx="1" fill="#fff"/></svg>`,
      usgs: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#15803d"/><path d="M16 6l9 17H7z" fill="#4ade80"/><circle cx="16" cy="17" r="2.5" fill="#15803d"/></svg>`,
      iea: `<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="14" fill="#0a2e5c"/><path d="M10 10h12M10 16h8M10 22h12" stroke="#7fd4ff" stroke-width="2.5" stroke-linecap="round"/></svg>`
    };

    // 单字母/缩写徽章图标生成器（无定制 iconKey 时的规范回退）
    function monoIcon(label, color) {
      const FS = {1: 11, 2: 10, 3: 8.5};
      const fs = FS[label.length] || 7.5;
      const cjk = /[\u4e00-\u9fff]/.test(label);
      const font = cjk ? "'Noto Serif SC', serif" : "'Plus Jakarta Sans', sans-serif";
      return `<svg viewBox=\"0 0 32 32\" fill=\"none\"><circle cx=\"16\" cy=\"16\" r=\"14\" fill=\"${color}\"/><text x=\"50%\" y=\"56%\" dominant-baseline=\"middle\" text-anchor=\"middle\" font-family=\"${font}\" font-weight=\"700\" font-size=\"${fs}\" fill=\"#ffffff\">${label}</text></svg>`;
    }

    function iconFor(item) {
      if (item.iconKey && ICONS[item.iconKey]) return ICONS[item.iconKey];
      return monoIcon(item.mono || item.name.slice(0, 1), item.monoColor || '#e26d46');
    }

    const CAT_LABELS = { official: '官方机构', academic: '学术文献', media: '新闻媒体', thinktank: '研究机构', data: '数据与工具' };
    const CAT_SPOT = {
      official: 'rgba(255, 138, 92, 0.16)',
      academic: 'rgba(177, 140, 255, 0.16)',
      media: 'rgba(251, 113, 133, 0.15)',
      thinktank: 'rgba(34, 211, 238, 0.14)',
      data: 'rgba(77, 163, 255, 0.16)'
    };
    const STATUS_LABELS = { shadow: '内部验收', manual: '人工导入', auth: '授权推进' };
    function spotColor(item) { return CAT_SPOT[item.cat] || 'rgba(226, 109, 70, 0.14)'; }

    // 50 个真实信源（id 对应原始清单 Excel 行号，便于溯源）
    const CATALOG = [{"id": "row44", "cat": "data", "name": "World Bank Open Data", "by": "World Bank ✓", "kicker": "全球发展、世界经济、贫困、贸易、人口、教育、能…", "desc": "宏观经济、发展经济学、人口、贫困、教育、卫生、能源、环境、贸易、金融、外债、农业、基础设施、城市发展、数字发展。World Bank Open Data 提供免费开放的全球发展数据，…", "cov": "全球", "span": "1960 — 至今", "gran": "国家-年度", "status": "shadow", "prio": "P0", "authlv": "A", "rss": false, "api": true, "region": "intl", "url": "https://data.worldbank.org/", "path": "data/row44", "routes": [], "bib": "@misc{row44_2026,\n  author = {World Bank},\n  title = {World Bank Open Data},\n  year = {2026},\n  url = {https://data.worldbank.org/}\n}", "iconKey": "wb", "stata": "wbopendata, country(COD ZWE ZMB ZAF) indicator(NY.GDP.MKTP.CD) clear"}];

    const LINEAR_GROUPS = [
  {
    "id": "sec-official",
    "catKey": "official",
    "nav": "官方机构",
    "title": "官方机构 Official Institutions",
    "heroTitle": "Resource Governance & Official Mineral Policy Fronts",
    "subtitle": "政府官网、专题页与 PDF 报告目录：中国部委、国际组织与非洲资源国矿业治理入口。",
    "items": [
      "row4",
      "row5",
      "row6",
      "row7",
      "row8",
      "row9",
      "row10",
      "row11",
      "row12",
      "row13"
    ]
  },
  {
    "id": "sec-academic",
    "catKey": "academic",
    "nav": "学术文献",
    "title": "学术文献 Scholarly Journals",
    "heroTitle": "Chinese-English Top Journals & Replicable Empirical Fronts",
    "subtitle": "中文顶刊官方平台与国际出版页面：HTML Meta、摘要回填与复现数据索引。",
    "items": [
      "row14",
      "row15",
      "row16",
      "row17",
      "row18",
      "row19",
      "row20",
      "row21",
      "row22",
      "row23"
    ]
  },
  {
    "id": "sec-media",
    "catKey": "media",
    "nav": "新闻媒体",
    "title": "新闻媒体 News Media",
    "heroTitle": "Global Media Wall for Minerals, Trade & Geopolitics",
    "subtitle": "RSS、新闻列表页与公开文章入口；受条款限制的来源保持暂停。",
    "items": [
      "row24",
      "row25",
      "row26",
      "row27",
      "row28",
      "row29",
      "row30",
      "row31",
      "row32",
      "row33"
    ]
  },
  {
    "id": "sec-thinktank",
    "catKey": "thinktank",
    "nav": "研究机构",
    "title": "研究机构 Think Tanks",
    "heroTitle": "China, Africa & Global Think-Tank Research Networks",
    "subtitle": "智库官网、报告目录、RSS 与公开 API；按机构入口分别适配。",
    "items": [
      "row34",
      "row35",
      "row36",
      "row37",
      "row38",
      "row39",
      "row40",
      "row41",
      "row42",
      "row43"
    ]
  },
  {
    "id": "sec-data",
    "catKey": "data",
    "nav": "数据与工具",
    "title": "数据与工具 Data & Tools",
    "heroTitle": "Structured Statistical APIs & Official Data Files",
    "subtitle": "5 个结构化数据集影子复核完成，其余按文件、授权或入口处理。",
    "items": [
      "row44",
      "row45",
      "row46",
      "row47",
      "row48",
      "row49",
      "row50",
      "row51",
      "row52",
      "row53"
    ]
  }
];
