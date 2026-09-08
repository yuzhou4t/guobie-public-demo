(function bootstrapReaderCore() {
  const COUNTRY_FLAGS = { COD: "🇨🇩", ZAF: "🇿🇦", ZMB: "🇿🇲", ZWE: "🇿🇼" };
  const SIDEBAR_STORAGE_KEY = "guobie-reader-sidebar-collapsed";
  const ASSISTANT_WIDTH_STORAGE_KEY = "guobie-reader-assistant-width";
  const ASSISTANT_SESSION_STORAGE_KEY = "guobie-reader-assistant-sessions";
  const ASSISTANT_AUTO_DISMISSED_KEY = "guobie-reader-assistant-auto-dismissed";
  const RECENT_COUNTRY_STORAGE_KEY = "guobie-reader-recent-countries";
  const READER_KEY_STORAGE_KEY = "guobie-reader-member-key";

  const configuredApiBase = document.querySelector('meta[name="guobie-api-base"]')?.content.trim() || "";
  const API = configuredApiBase ? configuredApiBase.replace(/\/$/, "") : `${window.location.origin}/api/v1`;
  const SKILL_META = {
    "topic-digest": { task: "围绕专题冻结证据包，生成带逐段事实与来源引用的日报或周报。", inputs: "专题、时间范围、已复核材料与事件", outputs: "周报正文、事件脉络、立场差异、证据缺口；写回专题产物" },
    "event-evidence-matrix": { task: "按原子事件并列展示来源报道、立场主张和数字差异，不输出系统裁决。", inputs: "研究专题或事件集", outputs: "多源对照表；差异只并列，不取平均" },
    "country-brief": { task: "按国家、主题和时间范围汇总结构化指标、研究事件与精选材料。", inputs: "国家、主题、时间范围", outputs: "覆盖摘要与最近事件，带来源" },
    "policy-dynamics": { task: "整理政策新增材料、版本变化和影响线索。", inputs: "国家、主题、关键词、时间范围", outputs: "政策阅读队列、版本变化、证据缺口" },
    "event-timeline": { task: "按已登记时间精度整理事件脉络并保留冲突。", inputs: "国家、事件或专题", outputs: "事件编年表、来源数、证据缺口" },
    "field-material-organizer": { task: "整理用户已提供的转写与文件引用。", inputs: "转写条目、文件引用、隐私模式", outputs: "带时间戳摘录、角色、方法和隐私标记" },
    "interdisciplinary-evidence": { task: "对专题材料进行多标签跨学科组织。", inputs: "专题、材料、学科标签", outputs: "多标签候选、分类依据、缺口" },
    "material-relevance-ranking": { task: "按可调整权重生成可解释材料优先级。", inputs: "专题、查询、关键词、权重", outputs: "候选排序、单项得分、排除原因" },
    "contradictory-evidence-context": { task: "并列矛盾材料的时间、地点、来源与观察主体。", inputs: "专题、对照材料", outputs: "语境对照卡、差异解释候选" },
    "country-comparison": { task: "比较口径可对齐的指标和研究者指定维度的材料。", inputs: "国家、指标、时期、材料维度", outputs: "可比表、材料并列、不可比项" },
    "material-condition-assessment": { task: "评估研究资料的覆盖、来源、时间、语言与访问条件。", inputs: "专题、研究问题、材料和数据", outputs: "资料覆盖、待补清单、待专家判断问题" },
  };
  const EVENT_TYPE_LABELS = { conflict: "冲突与安全", policy: "政策与法规", trade: "贸易与投资", diplomacy: "外交与国际关系", social: "社会与民生", environment: "环境与资源", accident: "事故与灾害", other: "其他事件" };
  const DATE_PRECISION_LABELS = { day: "精确到日", month: "精确到月", year: "精确到年", unknown: "日期精度未明" };
  const ENTITY_ROLE_LABELS = { actor: "行动主体", target: "涉及对象", about: "相关主体", policy: "政策主体", location: "发生地点", organization: "相关机构", person: "相关人物", commodity: "涉及资源", place: "相关地点", country: "相关国家" };
  const RELATION_TYPE_LABELS = { related: "相关事件", precedes: "前序事件", follows: "后续事件", causes: "已登记因果", responds_to: "回应事件" };
  const DOCUMENT_TYPE_LABELS = { report: "报告", news: "新闻", policy: "政策文件", article: "文章", dataset: "数据集", journal_article: "期刊论文", working_paper: "工作论文", field_note: "田野札记", interview_transcript: "访谈转写" };
  const SOURCE_TYPE_LABELS = { official: "官方来源", academic: "学术来源", media: "媒体来源", thinktank: "研究机构", data: "数据平台", user: "用户资料" };
  const ADMIN1_NAME_ZH = {
    "CD-HU": "上韦莱省", "CD-IT": "伊图里省", "CD-TO": "乔波省", "CD-BU": "下韦莱省", "CD-MO": "蒙加拉省", "CD-NU": "北乌班吉省", "CD-TU": "楚阿帕省", "CD-EQ": "赤道省", "CD-HK": "上加丹加省", "CD-HL": "上洛马米省", "CD-BC": "中刚果省", "CD-KG": "宽果省", "CD-SA": "桑库鲁省", "CD-SU": "南乌班吉省", "CD-TA": "坦噶尼喀省", "CD-KS": "开赛省", "CD-KE": "东开赛省", "CD-MA": "马涅马省", "CD-NK": "北基伍省", "CD-SK": "南基伍省", "CD-KC": "中开赛省", "CD-LO": "洛马米省", "CD-LU": "卢阿拉巴省", "CD-KN": "金沙萨市", "CD-KL": "奎卢省", "CD-MN": "马伊恩东贝省",
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  function iconSvg(name, className = "ui-icon") {
    return `<svg class="${escapeHtml(className)}" aria-hidden="true"><use href="./assets/lucide-sprite.svg#${escapeHtml(name)}"></use></svg>`;
  }

  function displayEnum(dictionary, value, fallback = "未登记") {
    return dictionary[value] || value || fallback;
  }

  function createInitialState() {
    return {
      bootstrap: null, catalog: [], summary: {}, coverageSummary: {}, country: null, countryReports: [],
      view: "countries", catalogLetter: "ALL", countryCatalogMode: "all", recentCountryIso3s: [], routeSerial: 0,
      worldMap: null, worldLayer: null, selectedMapIso3: "COD", topic: null, topics: [],
      countryMap: null, countryMapLayer: null, countryBaseMapLayer: null, countryMapIso3: null, countryMapAttribution: null,
      countryMapLabelLayer: null, countryMapLabels: [], countryMapHoverKey: null,
      countryAdminLevel: "ADM1", countryAdminSelectedKey: null, countryMapMode: "administrative", countryPane: "overview",
      pendingTopicLink: null, pendingTopicSuggestion: null, topicSuggestions: [],
      pendingTopicCountryIso3: null,
      activeEventId: null, activeCapabilityConfigId: null, activeCapabilityRunId: null,
      eventPane: "overview", topicPane: "overview", eventFilterCountry: "COD", eventFilterWindow: "all", eventFilterType: "", eventFilterQuery: "", eventFilterSource: "",
      eventFilterMinSources: "", eventFilterKind: "report", eventRows: [], eventReports: [], eventDetail: null, eventEvidence: null, eventPreviewKey: null,
      capabilities: [], capabilityConfigs: [], assistantCapabilitiesLoaded: false, activeCapabilitySlug: null, capabilityPane: "overview", skillFilterQuery: "", skillCategory: "all", skillSection: "public", topicFeedWindow: "all", topicFrontiers: [], projectObjectRoute: null,
      topicFieldMaterials: [], fieldUploadScope: "topic",
      assistantScopeId: null, assistantContext: null, assistantSessions: {}, assistantBusy: false,
      assistantFocus: null, assistantWidth: 360, assistantLastTrigger: null, assistantElapsedTimer: null,
      assistantSidebarWasCollapsed: null, assistantAutoDismissed: false, selectedMaterials: [], projectMaterialCandidates: [], projectMaterialSelection: [],
      policyExpanded: false, selectedTrendKey: null, selectedTrendPoint: null, topicMaterialPreviewId: null,
      countryChannelMaterials: {}, countryDataCatalog: null, selectedDataIndicatorKey: null, selectedCountryEventId: null,
      materialSearchResults: [], materialSearchFacets: {}, materialSearchBusy: false,
      materialSearchType: "all", routeQuery: new URLSearchParams(), researchWindowYears: 3,
      countryEventFilters: { timeBasis: "occurred", type: "", query: "", perspective: "" },
      countryEventPage: 0, countryReportPage: 0,
    };
  }

  function formatApiErrorDetail(value, fallback = "请求失败") {
    if (value === null || value === undefined || value === "") return fallback;
    if (typeof value === "string") return value.trim() || fallback;
    if (Array.isArray(value)) {
      const messages = value.map((item) => formatApiErrorDetail(item, "")).filter(Boolean);
      return messages.join("；") || fallback;
    }
    if (typeof value === "object") {
      const location = Array.isArray(value.loc)
        ? value.loc.filter((item) => !["body", "query", "path"].includes(String(item))).join(" / ")
        : "";
      const messageValue = value.message ?? value.msg ?? value.detail ?? value.error ?? value.reason;
      if (messageValue !== undefined) {
        const message = formatApiErrorDetail(messageValue, fallback);
        return location ? `${location}：${message}` : message;
      }
      try { return JSON.stringify(value); } catch (_) { return fallback; }
    }
    return String(value);
  }

  class ReaderApiError extends Error {
    constructor(message, code, status = null) {
      super(formatApiErrorDetail(message));
      this.name = "ReaderApiError";
      this.code = code;
      this.status = status;
    }
  }

  async function apiFetch(path, options = {}) {
    const { headers = {}, ...requestOptions } = options;
    let response;
    const csrf = document.cookie.split("; ").find(row=>row.startsWith("guobie_csrf="))?.split("=").slice(1).join("=") || "";
    const sessionHeaders = csrf ? {"X-CSRF-Token": decodeURIComponent(csrf)} : {};
    try {
      const readerKey = window.sessionStorage.getItem(READER_KEY_STORAGE_KEY) || "";
      response = await fetch(`${API}${path}`, { ...requestOptions, credentials: "same-origin", headers: { "Content-Type": "application/json", ...sessionHeaders, ...(readerKey ? { "X-Reader-Key": readerKey } : {}), ...headers } });
    } catch (_) {
      throw new ReaderApiError("本地 Reader 后端未运行或连接已中断；模型尚未收到本次问题。请重新打开「国别智枢」后再试。", "api_unreachable");
    }
    if (response.status === 401 && !window.ReaderAuth?.status?.initialized && !path.startsWith("/reader/auth/") && !window.sessionStorage.getItem(READER_KEY_STORAGE_KEY)) {
      const supplied = window.prompt("请输入本机专题成员访问密钥。密钥只保存在当前浏览器会话。", "")?.trim();
      if (supplied) {
        window.sessionStorage.setItem(READER_KEY_STORAGE_KEY, supplied);
        response = await fetch(`${API}${path}`, { ...requestOptions, credentials: "same-origin", headers: { "Content-Type": "application/json", ...sessionHeaders, "X-Reader-Key": supplied, ...headers } });
      }
    }
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        detail = formatApiErrorDetail(payload.detail ?? payload.message ?? payload.error, detail);
      } catch (_) { /* keep status */ }
      const code = response.status === 401 ? "api_key_required" : response.status === 404 ? "endpoint_missing" : response.status === 503 ? "service_unavailable" : "api_error";
      const guidance = response.status === 401 && window.ReaderAuth?.status?.initialized ? "请先登录平台账号。" : response.status === 401 ? "当前浏览器会话没有有效的专题成员密钥；请在协作与权限页输入密钥。" : response.status === 404 ? "接口不存在；请确认 Reader 与后端来自同一版本。" : detail;
      throw new ReaderApiError(guidance, code, response.status);
    }
    return response.json();
  }

  window.ReaderCore = Object.freeze({
    COUNTRY_FLAGS,
    SIDEBAR_STORAGE_KEY,
    ASSISTANT_WIDTH_STORAGE_KEY,
    ASSISTANT_SESSION_STORAGE_KEY,
    ASSISTANT_AUTO_DISMISSED_KEY,
    RECENT_COUNTRY_STORAGE_KEY,
    READER_KEY_STORAGE_KEY,
    API,
    SKILL_META,
    EVENT_TYPE_LABELS,
    DATE_PRECISION_LABELS,
    ENTITY_ROLE_LABELS,
    RELATION_TYPE_LABELS,
    DOCUMENT_TYPE_LABELS,
    SOURCE_TYPE_LABELS,
    ADMIN1_NAME_ZH,
    ReaderApiError,
    formatApiErrorDetail,
    apiFetch,
    escapeHtml,
    iconSvg,
    displayEnum,
    createInitialState,
  });
}());
