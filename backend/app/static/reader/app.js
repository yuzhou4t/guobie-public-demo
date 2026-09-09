const {
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
} = window.ReaderCore;
const {
  safeUrl,
  localDate,
  scopeLabel,
  publishedLabel,
  formatLocator,
  formatNumber,
  emptyState,
  errorState,
  detailSkeleton,
  sourceActionHtml,
} = window.ReaderFormatters;
const {
  numericRouteId,
  eventPaneFromRoute,
  countryPaneFromRoute,
  topicPaneFromRoute,
  capabilityPaneFromRoute,
} = window.ReaderRouting;

const state = createInitialState();
function countryReadingState() {
  state.countryReading ||= {};
  return state.countryReading[state.country.iso3] ||= { layer: "reports", reports: 12, chains: 12, policy: 12, source: "", compared: [], scroll: {} };
}

function openCountryReading(title) {
  restoreCountryDataReading();
  const dialog = document.getElementById("countryReadingDialog");
  document.getElementById("countryReadingTitle").textContent = title;
  state.countryReadingRequest = (state.countryReadingRequest || 0) + 1;
  if (!dialog.open) {
    state.countryReadingFocus = document.activeElement;
    dialog.show();
    requestAnimationFrame(positionCountryReading);
  }
  return state.countryReadingRequest;
}

function closeCountryReading(restoreRoute = true) {
  if(restoreRoute && history.state?.countryDataReading) history.back();
  restoreCountryDataReading();
  state.countryReadingRequest = (state.countryReadingRequest || 0) + 1;
  document.getElementById("countryReadingDialog").close();
  state.countryReadingFocus?.focus({ preventScroll: true });
  if (restoreRoute && /^#\/countries\/[A-Z]{3}\/(reports|policies|events)\/\d+/.test(location.hash)) navigate(`/countries/${state.country.iso3}/${state.countryPane}`);
}

function readingDate(value, precision) {
  return value ? publishedLabel(value, precision) : "日期未注明";
}

function readingFeed(rows, dateFor, cardFor) {
  return `<div class="reading-feed">${rows.map((row) => {
    const date = dateFor(row);
    return cardFor(row).replace(/(<article[^>]*>)/, `$1<time class="article-date">${escapeHtml(date)}</time>`);
  }).join("")}</div>`;
}

function renderCountryRecentSignals() {
  if (!state.country) return;
  const view = countryReadingState();
  const filters = state.countryEventFilters || {};
  const timeValue = (item) => ({ occurred: item.start_at, reported: item.reported_at, recorded: item.recorded_at, updated: item.updated_at })[filters.timeBasis] || item.start_at;
  const query = String(filters.query || "").trim().toLowerCase();
  const events = (state.country.events || []).filter((item) => (!filters.type || item.event_type === filters.type) && (!query || `${item.title} ${item.place || ""}`.toLowerCase().includes(query))).sort((a,b) => String(timeValue(b) || "").localeCompare(String(timeValue(a) || "")));
  const chains = new Map();
  events.forEach((item) => {
    const key = item.series_key || `event:${item.id}`;
    if (!chains.has(key)) chains.set(key, { ...item, nodes: [] });
    chains.get(key).nodes.push(item);
  });
  const rows = [...chains.values()];
  const reportQuery = (view.reportQuery || "").trim().toLowerCase();
  const allReports = state.countryReports || [];
  const filterRoot = document.getElementById("countryLatestFilters");
  filterRoot.hidden = view.layer !== "reports";
  document.getElementById("countryLatestQuery").value = view.reportQuery || "";
  const sourceSelect = document.getElementById("countryLatestSource");
  sourceSelect.innerHTML = '<option value="">全部来源</option>' + [...new Set(allReports.map(item=>item.source_name).filter(Boolean))].map(name=>`<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  sourceSelect.value = view.reportSource || "";
  const reports = allReports.filter(item=>(!view.reportSource || item.source_name === view.reportSource) && (!reportQuery || `${item.title} ${item.source_name}`.toLowerCase().includes(reportQuery))).sort((a,b) => String(b.published_at || "").localeCompare(String(a.published_at || "")));
  const latest = document.getElementById("countryLatestReports");
  latest.hidden = view.layer !== "reports";
  document.getElementById("countryReviewedPanel").hidden = view.layer !== "chains";
  document.querySelectorAll("[data-country-feed]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.countryFeed === view.layer)));
  latest.innerHTML = `<p class="reading-note">${reports.length} 条报道 · 来源已核验，事件聚合与复核由平台完成</p>${reports.length ? readingFeed(reports.slice(0,view.reports), item => readingDate(item.published_at,item.published_at_precision), item => {
    const url = safeUrl(item.source_url || item.canonical_url || item.discovery_url);
    const reading = item.document_version_id && item.review_status === "confirmed";
    return `<article class="reading-card" ${item.document_version_id ? projectMaterialAttributes(item) : ""}><h3>${reading ? `<button class="reading-title-button" data-country-read-material="${item.document_version_id}">${escapeHtml(item.title)}</button>` : escapeHtml(item.title)}</h3>${item.abstract ? `<p>${escapeHtml(item.abstract)}</p>` : ""}<footer><span>${escapeHtml(item.source_name || "来源未注明")} · 报道${item.linked_events?.length ? "已关联事件":"待聚合"}</span>${reading ? `<button class="text-link" data-country-read-material="${item.document_version_id}">阅读资料 →</button>` : url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">阅读原始报道 ↗</a>` : "原链接待恢复"}</footer></article>`;
  }) : emptyState("当前没有来源已核验的最新报道。")}${reports.length > view.reports ? '<button class="list-toggle" data-reading-more="reports">加载更多报道</button>' : ""}`;
  document.getElementById("countrySignals").innerHTML = rows.length ? readingFeed(rows.slice(0,view.chains), item => readingDate(timeValue(item),item.date_precision), item => `<article class="reading-card" ${projectEventAttributes(item)}><h3><button class="reading-title-button" data-country-event-id="${item.id}">${escapeHtml(item.title)}</button></h3>${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}<footer><span>${escapeHtml(displayEnum(EVENT_TYPE_LABELS,item.event_type))} · 已复核 · ${item.nodes.length} 个节点 · ${Number(item.source_count || 0)} 个确认来源</span><button class="text-link" data-country-event-id="${item.id}">查看事件链与证据 →</button></footer></article>`) + (rows.length > view.chains ? '<button class="list-toggle" data-reading-more="chains">加载更多事件链</button>' : "") : emptyState("当前筛选下没有已复核事件链。");
  document.getElementById("countryCoverage").innerHTML = `<span>${state.country.summary.events} 个已复核事件</span><a href="#/resources">来源与数据覆盖</a>`;
}

function publicationScroller() {
  const pane=document.querySelector(".country-pane-viewport");
  return pane && ["auto","scroll"].includes(getComputedStyle(pane).overflowY) ? pane : document.scrollingElement;
}
function closePublicationDirectory(pane) {
  const section=document.getElementById(pane==="reports" ? "country-reports":"minerals-policy");
  section.querySelector(".publication-workbench").classList.remove("directory-open");
  section.querySelector("[data-publication-directory]").setAttribute("aria-expanded","false");
}
async function syncPublicationReading(pane, versionId, serial) {
  if (versionId !== null) return openCountryMaterial(versionId, pane);
  const prefix=pane==="reports" ? "countryReports":"countryPolicies";
  const list=document.getElementById(`${prefix}List`);
  list.hidden=false;
  document.getElementById(`${prefix}Reading`).hidden=true;
  const view=countryReadingState();
  requestAnimationFrame(()=> {
    if(serial!==state.routeSerial) return;
    publicationScroller().scrollTop=view.publicationScroll?.[pane] || 0;
    [...list.querySelectorAll("[data-publication-open]")].find(item=>item.getAttribute("href")===view.publicationFocus)?.focus({preventScroll:true});
  });
}

function institutionName(id, fallback) {
  const profile=(state.country.source_profiles || []).find(item=>String(item.source_id)===String(id));
  return profile?.organization || fallback;
}

function renderCountryReportChannel() {
  if (!state.country) return;
  const view = countryReadingState();
  const key=`${state.country.iso3}:think_tank_report`;
  const rows = state.countryChannelMaterials[key] || [];
  const payload = state.countryChannelPages?.[key];
  const institutions = payload?.facets?.sources || [];
  const reportSource=document.getElementById("countryReportSourceFilter");
  if(reportSource) reportSource.innerHTML=`<option value="">全部机构</option>${institutions.map(item=>`<option value="${item.source_id}" ${String(item.source_id)===view.source ? "selected":""}>${escapeHtml(institutionName(item.source_id,item.source_name))} · ${item.count}</option>`).join("")}`;
  const reportQuery=document.getElementById("countryReportQuery");
  if(reportQuery && reportQuery.value!==(view.reportQueryText || "")) reportQuery.value=view.reportQueryText || "";
  document.getElementById("countryThinkTankReportCount").textContent = `${payload?.total || 0} 份报告`;
  document.getElementById("countryThinkTankReports").innerHTML = rows.length ? rows.map(item=>countryArticleHtml(item,"reports",`<label class="publication-select"><input type="checkbox" aria-label="加入报告对照：${escapeHtml(item.title)}" data-compare-report="${item.document_version_id}" ${view.compared.includes(item.document_version_id) ? "checked" : ""}>报告对照</label>`)).join("") + (rows.length < payload.total ? '<button class="list-toggle" data-publication-more="think_tank_report">加载更多报告</button>' : "") : emptyState("当前机构没有可展示的报告。");
  updateReportCompareSelection();
  state.reportInstitutionFacets ||= {};
  if(!view.source && !view.reportQueryText) state.reportInstitutionFacets[state.country.iso3]=institutions;
  const roster=state.reportInstitutionFacets[state.country.iso3] || institutions;
  const institutionsActive=view.reportView==="institutions";
  document.getElementById("countryReportsList").hidden=institutionsActive;
  document.getElementById("countryInstitutionSection").hidden=!institutionsActive;
  const reportFilters=document.querySelector("#countryReportToolbar .country-latest-filters");
  if(reportFilters) reportFilters.hidden=institutionsActive;
  const reportCompare=document.getElementById("countryReportCompareMode");
  if(reportCompare) reportCompare.hidden=institutionsActive;
  document.querySelectorAll("[data-report-view]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.reportView===(view.reportView || "reports"))));
  document.getElementById("countrySourceProfileCount").textContent=`${roster.length} 家发布机构`;
  const selected=roster.find(item=>String(item.source_id)===view.reportInstitutionId);
  const profiles=state.country.source_profiles || [];
  const profileFor=item=>profiles.find(profile=>String(profile.source_id)===String(item.source_id)) || {source_id:item.source_id,source_name:item.source_name,facts:[]};
  const tags=profile=>(profile.facts || []).filter(fact=>["机构性质","所在地","办公地点","研究领域","职责"].includes(fact.label)).slice(0,3).map(fact=>`<span>${escapeHtml(fact.value)}</span>`).join("");
  document.getElementById("countrySourceProfiles").innerHTML=selected ? (()=>{
    const profile=profileFor(selected);
    return `<article class="institution-detail"><button class="text-link" data-institution-back>← 全部机构</button><h3>${escapeHtml(profile.organization || selected.source_name)}</h3><div class="institution-tags">${tags(profile)}</div><dl>${(profile.facts || []).filter(fact=>fact.value).map(fact=>`<div><dt>${escapeHtml(fact.label)}</dt><dd>${escapeHtml(fact.value)}${safeUrl(fact.source_url) ? ` <a href="${escapeHtml(safeUrl(fact.source_url))}" target="_blank" rel="noopener noreferrer">官网依据 ↗</a>`:""}${fact.checked_at ? `<small>核对于 ${escapeHtml(fact.checked_at)}</small>`:""}</dd></div>`).join("")}</dl><button class="ghost-btn" data-institution-reports="${selected.source_id}">查看该机构报告 →</button></article>`;
  })() : `<div class="institution-directory">${roster.map(item=>{const profile=profileFor(item);return `<article class="institution-card"><h3><button data-institution-open="${item.source_id}">${escapeHtml(profile.organization || item.source_name)}</button></h3><div class="institution-tags">${tags(profile)}</div><footer><span>${item.count} 份收录报告</span><button class="text-link" data-institution-open="${item.source_id}">机构资料 →</button></footer></article>`;}).join("")}</div>`;

}

function updateReportCompareSelection() {
  const selected = countryReadingState().compared;
  document.querySelector(".report-compare-toolbar").hidden = selected.length === 0;
  document.getElementById("reportSelectionCount").textContent = `已选 ${selected.length} / 4 份 · 选择 2–4 份报告进行对照`;
  document.querySelector("[data-compare-reports]").disabled = selected.length < 2;
}

function openReportComparison() {
  const rows = Object.values(state.reportComparisonItems || {}).filter(item=>countryReadingState().compared.includes(item.document_version_id));
  if (rows.length < 2 || rows.length > 4) return;
  openCountryReading("多报告并列对照");
  document.getElementById("countryEventChainDetail").innerHTML = `<div class="report-comparison-grid">${rows.map(item=>`<article class="reading-card"><h3>${escapeHtml(item.title)}</h3><dl><div><dt>机构</dt><dd>${escapeHtml(item.source_name)}</dd></div><div><dt>发布日期</dt><dd>${escapeHtml(readingDate(item.published_at,item.published_at_precision))}</dd></div><div><dt>研究范围</dt><dd>${escapeHtml(item.metadata?.sample_scope || item.metadata?.research_object || "未登记")}</dd></div><div><dt>版本</dt><dd>v${Number(item.version_no || 1)}</dd></div><div><dt>已有摘要</dt><dd>${escapeHtml(item.abstract || "未保存摘要")}</dd></div></dl>${sourceActionHtml(item) || ""}</article>`).join("")}</div>`;
}

function policyCategory(item) {
  if (["law", "regulation", "policy", "policy_document", "decree", "legal_document", "official_notice"].includes(item.document_type)) return "政策原文";
  if (item.metadata?.policy_material_kind === "official_explanation") return "官方解读";
  if (["news", "news_article"].includes(item.document_type) || ["media", "news_media"].includes(item.source_type)) return "相关报道";
  return "性质未注明";
}
function renderPolicy() {
  const items = [...(state.country.policy_items || [])].sort((a,b)=>String(b.published_at || "").localeCompare(String(a.published_at || "")));
  const view=countryReadingState();
  const categories=["政策原文","官方解读","相关报道","性质未注明"].map(name=>({name,count:items.filter(item=>policyCategory(item)===name).length})).filter(item=>item.count);
  document.getElementById("countryPolicyCategories").innerHTML=`<button type="button" data-policy-category="" aria-pressed="${!view.policyCategory}">全部材料</button>`+categories.map(item=>`<button type="button" data-policy-category="${item.name}" aria-pressed="${view.policyCategory===item.name}">${escapeHtml(item.name)}</button>`).join("");
  const sources=[...new Set(items.map(item=>item.source_name).filter(Boolean))];
  const policySource=document.getElementById("countryPolicySource");
  if(policySource) {
    policySource.innerHTML='<option value="">全部来源</option>'+sources.map(name=>`<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
    policySource.value=view.policySource || "";
  }
  const policyQuery=document.getElementById("countryPolicyQuery");
  if(policyQuery && policyQuery.value!==(view.policyQuery || "")) policyQuery.value=view.policyQuery || "";
  const filtered=items.filter(item=>(!view.policyCategory || policyCategory(item)===view.policyCategory) && (!view.policySource || item.source_name===view.policySource) && (!view.policyQuery || `${item.title} ${item.source_name}`.toLowerCase().includes(view.policyQuery.toLowerCase())));
  const visible=filtered.slice(0,view.policy);
  document.getElementById("policyMaterialCount").textContent = `${filtered.length} 份材料`;
  document.getElementById("policyContent").innerHTML = visible.length ? visible.map(item=>countryArticleHtml(item,"policies","",policyCategory(item))).join("")+(filtered.length>visible.length ? '<button class="list-toggle" data-reading-more="policy">加载更多政策</button>':"") : emptyState("暂无已确认政策材料。");
  renderPolicyLifecycle();
}

state.assistantHistoryOpen = false;
state.assistantHistoryItems = [];
state.assistantHistoryTotal = 0;
state.assistantHistoryHasMore = false;
state.assistantHistoryLoading = false;
state.assistantHistoryError = "";
state.assistantHistoryScopeId = null;
let eventSearchTimer = null;
const ASSISTANT_SIDE_MIN_WIDTH = 1120;
function countryChatLayout() { return state.assistantContext?.space === "country" && Boolean(state.country) && state.assistantContext.country_iso3 === state.country.iso3; }
const ASSISTANT_PINNED_MIN_WIDTH = 1280;

function assistantMessageId() {
  return `assistant-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeAssistantSession(value = {}) {
  return {
    conversationId: value.conversationId || null,
    activeRunId: Number(value.activeRunId) || null,
    status: value.status || "idle",
    lastQuestion: value.lastQuestion || "",
    lastSequence: Number(value.lastSequence) || 0,
    references: Array.isArray(value.references) ? value.references : [],
    referencesRestored: Array.isArray(value.references),
    messages: Array.isArray(value.messages) ? value.messages : [],
    scrollTop: Number(value.scrollTop) || 0,
    resuming: false,
    hydrated: Boolean(value.hydrated),
    capabilityConfigIds: Array.isArray(value.capabilityConfigIds) ? value.capabilityConfigIds.map(Number).filter(Number.isInteger).slice(0, 3) : [],
  };
}

function restoreAssistantState() {
  try {
    const savedWidth = Number(window.sessionStorage.getItem(ASSISTANT_WIDTH_STORAGE_KEY));
    if (Number.isFinite(savedWidth) && savedWidth > 0) state.assistantWidth = Math.min(520, Math.max(320, savedWidth));
    const savedSessions = JSON.parse(window.sessionStorage.getItem(ASSISTANT_SESSION_STORAGE_KEY) || "{}");
    state.assistantAutoDismissed = window.sessionStorage.getItem(ASSISTANT_AUTO_DISMISSED_KEY) === "1";
    state.recentCountryIso3s = JSON.parse(window.sessionStorage.getItem(RECENT_COUNTRY_STORAGE_KEY) || "[]").filter((value) => /^[A-Z]{3}$/.test(value)).slice(0, 6);
    Object.entries(savedSessions).forEach(([scopeId, saved]) => {
      const session = normalizeAssistantSession(saved);
      if (session.activeRunId && session.status === "running") {
        session.messages = [
          ...(session.lastQuestion ? [{ id: assistantMessageId(), role: "user", html: escapeHtml(session.lastQuestion) }] : []),
          { id: assistantMessageId(), role: "assistant", pending: true, runId: session.activeRunId, stages: [{ key: "restore", label: "正在恢复后台运行", state: "current" }], sections: [], citations: [] },
        ];
      }
      state.assistantSessions[scopeId] = session;
    });
  } catch (_) { /* session storage unavailable or invalid */ }
}

function persistAssistantSessions() {
  try {
    const saved = {};
    Object.entries(state.assistantSessions).forEach(([scopeId, session]) => {
      if (session.conversationId || session.activeRunId || session.capabilityConfigIds.length || session.references?.length) {
        saved[scopeId] = {
          references: session.references || [],
          conversationId: session.conversationId,
          activeRunId: session.activeRunId,
          status: session.status,
          lastQuestion: session.lastQuestion,
          lastSequence: session.lastSequence,
          scrollTop: session.scrollTop,
          capabilityConfigIds: session.capabilityConfigIds,
        };
      }
    });
    window.sessionStorage.setItem(ASSISTANT_SESSION_STORAGE_KEY, JSON.stringify(saved));
  } catch (_) { /* session storage unavailable */ }
}

function setSidebarCollapsed(collapsed, persist = true) {
  const toggle = document.getElementById("sidebarToggle");
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const preference = document.getElementById("settingsCompactNav");
  if (preference) preference.checked = collapsed;
  if (toggle) {
    const label = collapsed ? "展开左侧导航" : "收起左侧导航";
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    toggle.querySelector("span").textContent = collapsed ? "›" : "‹";
  }
  if (persist) {
    try { window.localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "1" : "0"); } catch (_) { /* local preference unavailable */ }
  }
  window.setTimeout(() => {
    state.worldMap?.invalidateSize();
    state.countryMap?.invalidateSize();
    if (state.countryMapLayer && !state.countryAdminSelectedKey) {
      state.countryMap?.fitBounds(state.countryMapLayer.getBounds(), { padding: [18, 18] });
    }
    syncCountryMapLabels();
  }, 220);
}

function initSidebarToggle() {
  document.getElementById("settingsCompactNav")?.addEventListener("change", event => setSidebarCollapsed(event.target.checked));
  let collapsed = false;
  try { collapsed = window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1"; } catch (_) { /* use expanded default */ }
  setSidebarCollapsed(collapsed, false);
  document.getElementById("sidebarToggle")?.addEventListener("click", () => {
    setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
  });
}

function isMaterialSelected(versionId) {
  if (!versionId) return false;
  return (state.selectedMaterials || []).some((m) => String(m.versionId) === String(versionId));
}

function toggleSelectMaterial(versionId, title, sourceName) {
  if (!versionId) return;
  if (countryChatLayout()) { addCountryReference({type:"document_version",id:Number(versionId),title,source_name:sourceName}); return; }
  if (!state.selectedMaterials) state.selectedMaterials = [];
  const idx = state.selectedMaterials.findIndex((m) => String(m.versionId) === String(versionId));
  if (idx >= 0) {
    state.selectedMaterials.splice(idx, 1);
  } else {
    state.selectedMaterials.push({ versionId, title: title || "未命名材料", sourceName: sourceName || "信源" });
  }
  renderSelectedContextChips();
  document.querySelectorAll(`[data-toggle-select-material="${versionId}"]`).forEach((btn) => {
    const isSel = isMaterialSelected(versionId);
    btn.classList.toggle("is-selected", isSel);
    btn.textContent = isSel ? "已选入 AI" : "选入 AI";
    const card = btn.closest(".material-card, article");
    if (card) card.classList.toggle("is-selected", isSel);
  });
  if (state.selectedMaterials.length > 0 && document.getElementById("countryCopilot").hidden) {
    openCopilot();
  }
  renderTopicMaterialActionBar();
}

function renderSelectedContextChips() {
  if (countryChatLayout()) return renderCountryReferences();
  const container = document.getElementById("copilotSelectedContext");
  const chipsEl = document.getElementById("selectedContextChips");
  const countEl = document.getElementById("selectedContextCount");
  if (!container || !chipsEl) return;
  const list = state.selectedMaterials || [];
  if (countEl) countEl.textContent = list.length;
  if (!list.length) {
    container.hidden = true;
    chipsEl.innerHTML = "";
    renderAssistantPromptGuide();
    return;
  }
  container.hidden = false;
  chipsEl.innerHTML = list.map((item) => `
    <span class="selected-context-chip" title="${escapeHtml(item.title)}">
      <span>${iconSvg("file-text")} ${escapeHtml(item.title)}</span>
      <button type="button" data-remove-selected-chip="${item.versionId}" aria-label="移除此材料">×</button>
    </span>
  `).join("");
  renderAssistantPromptGuide();
}

function projectMaterialAttributes(item) {
  const id = Number(item?.document_version_id);
  if (!Number.isInteger(id) || id <= 0) return "";
  return `data-project-material-version="${id}" data-project-material-title="${escapeHtml(item.title || "未命名材料")}" data-project-material-source="${escapeHtml(item.source_name || item.source?.name || "来源未登记")}"`;
}

function projectEventAttributes(item) {
  const id = Number(item?.id);
  if (!Number.isInteger(id) || id <= 0) return "";
  return `data-project-event-id="${id}" data-project-event-title="${escapeHtml(item.title || "未命名事件")}" data-project-event-source="${Number(item.source_count || 0)} 个确认来源"`;
}

function collectProjectMaterialCandidates(containerId) {
  const root = document.getElementById(containerId);
  if (!root) return [];
  const candidates = [];
  const seen = new Set();
  root.querySelectorAll("[data-project-material-version]").forEach((element) => {
    const versionId = Number(element.dataset.projectMaterialVersion);
    if (!Number.isInteger(versionId) || seen.has(versionId)) return;
    seen.add(versionId);
    candidates.push({ key: `material:${versionId}`, type: "material", versionId, title: element.dataset.projectMaterialTitle || "未命名材料", sourceName: element.dataset.projectMaterialSource || "来源未登记" });
  });
  root.querySelectorAll("[data-project-event-id]").forEach((element) => {
    const eventId = Number(element.dataset.projectEventId);
    const key = `event:${eventId}`;
    if (!Number.isInteger(eventId) || seen.has(key)) return;
    seen.add(key);
    candidates.push({ key, type: "event", eventId, title: element.dataset.projectEventTitle || "未命名事件", sourceName: element.dataset.projectEventSource || "已复核事件" });
  });
  return candidates;
}

function syncBulkProjectMaterialSubmit() {
  const selected = state.projectMaterialSelection || [];
  const candidates = state.projectMaterialCandidates || [];
  const target = document.getElementById("bulkProjectTarget");
  const submit = document.getElementById("bulkProjectMaterialSubmit");
  const count = document.getElementById("bulkProjectMaterialCount");
  const toggleAll = document.querySelector("[data-toggle-all-project-materials]");
  if (count) count.textContent = `已选 ${selected.length} / ${candidates.length} 项`;
  if (toggleAll) toggleAll.textContent = candidates.length && selected.length === candidates.length ? "清空" : "全选";
  if (submit) submit.disabled = !Number(target?.value || 0) || !selected.length;
}

function renderBulkProjectMaterialSummary() {
  const root = document.getElementById("bulkProjectMaterialSummary");
  if (!root) return;
  const selectedIds = new Set((state.projectMaterialSelection || []).map((item) => item.key));
  const candidates = state.projectMaterialCandidates || [];
  root.innerHTML = candidates.length
    ? candidates.map((item) => `<label class="bulk-project-material-option"><input type="checkbox" data-bulk-project-item="${escapeHtml(item.key)}"${selectedIds.has(item.key) ? " checked" : ""}><span><strong>${escapeHtml(item.title)}</strong><small>${item.type === "event" ? `已复核事件 #${Number(item.eventId)}` : `固定资料版本 #${Number(item.versionId)}`} · ${escapeHtml(item.sourceName || "来源未登记")}</small></span></label>`).join("")
    : emptyState("本页没有可加入项目的已确认资料或已复核事件。");
  syncBulkProjectMaterialSubmit();
}

function closeBulkProjectMaterialDialog() {
  const dialog = document.getElementById("bulkProjectMaterialDialog");
  if (dialog?.open) dialog.close();
}

async function openBulkProjectMaterialDialog(containerId) {
  state.projectMaterialCandidates = collectProjectMaterialCandidates(containerId);
  state.projectMaterialSelection = [];
  if (!state.projectMaterialCandidates.length) {
    showToast("本页没有可加入项目的已确认资料或已复核事件");
    return;
  }
  const dialog = document.getElementById("bulkProjectMaterialDialog");
  const target = document.getElementById("bulkProjectTarget");
  const submit = document.getElementById("bulkProjectMaterialSubmit");
  const status = document.getElementById("bulkProjectMaterialStatus");
  renderBulkProjectMaterialSummary();
  target.innerHTML = `<option value="">正在读取可编辑项目…</option>`;
  submit.disabled = true;
  status.textContent = "正在核对项目范围与权限…";
  if (!dialog.open) dialog.showModal();
  try {
    state.topics = await apiFetch("/reader/research-cases");
    const currentCountry = resolveTopicCountryIso3("material");
    const topics = state.topics.filter((item) => item.status !== "archived").sort((left, right) => {
      const leftRank = Number(left.id) === Number(state.topic?.id) ? 0 : left.scope?.country_iso3 === currentCountry ? 1 : 2;
      const rightRank = Number(right.id) === Number(state.topic?.id) ? 0 : right.scope?.country_iso3 === currentCountry ? 1 : 2;
      return leftRank - rightRank;
    });
    target.innerHTML = topics.length
      ? `<option value="">请选择目标项目</option>${topics.map((item) => `<option value="${Number(item.id)}">${escapeHtml(item.title)} · ${escapeHtml(scopeLabel(item.scope))}</option>`).join("")}`
      : `<option value="">暂无可用项目</option>`;
    const preferred = topics.find((item) => Number(item.id) === Number(state.topic?.id)) || topics.find((item) => item.scope?.country_iso3 === currentCountry) || topics[0];
    if (preferred) target.value = String(preferred.id);
    syncBulkProjectMaterialSubmit();
    status.textContent = topics.length
      ? "系统会继续校验项目编辑权限及资料或事件的确认状态；重复对象不会再次写入。"
      : "当前没有可加入的项目，请先创建项目，再返回继续加入。";
    window.setTimeout(() => (preferred ? target : document.querySelector(".bulk-project-create-link"))?.focus(), 0);
  } catch (error) {
    target.innerHTML = `<option value="">项目列表读取失败</option>`;
    status.textContent = `项目列表读取失败：${error.message}`;
  }
}

function toggleBulkProjectMaterial(key, checked) {
  const candidate = (state.projectMaterialCandidates || []).find((item) => item.key === key);
  if (!candidate) return;
  state.projectMaterialSelection = (state.projectMaterialSelection || []).filter((item) => item.key !== key);
  if (checked) state.projectMaterialSelection.push(candidate);
  syncBulkProjectMaterialSubmit();
}

function toggleAllBulkProjectMaterials() {
  const candidates = state.projectMaterialCandidates || [];
  state.projectMaterialSelection = (state.projectMaterialSelection || []).length === candidates.length ? [] : [...candidates];
  renderBulkProjectMaterialSummary();
}

async function submitBulkProjectMaterials(event) {
  event.preventDefault();
  const target = document.getElementById("bulkProjectTarget");
  const submit = document.getElementById("bulkProjectMaterialSubmit");
  const status = document.getElementById("bulkProjectMaterialStatus");
  const caseId = Number(target.value || 0);
  const selected = [...(state.projectMaterialSelection || [])];
  if (!caseId || !selected.length) {
    showToast(caseId ? "请先选择资料" : "请选择目标项目");
    return;
  }
  const topic = (state.topics || []).find((item) => Number(item.id) === caseId);
  submit.disabled = true;
  submit.textContent = `正在加入 0 / ${selected.length}…`;
  const failures = [];
  let createdCount = 0;
  let existingCount = 0;
  for (let index = 0; index < selected.length; index += 1) {
    const item = selected[index];
    try {
      const result = item.type === "event"
        ? await apiFetch(`/reader/research-cases/${caseId}/events`, { method: "POST", body: JSON.stringify({ event_id: Number(item.eventId) }) })
        : await apiFetch(`/reader/research-cases/${caseId}/materials`, { method: "POST", body: JSON.stringify({ document_version_id: Number(item.versionId) }) });
      if (result.created) createdCount += 1;
      else existingCount += 1;
    } catch (error) {
      failures.push({ ...item, error: error.message });
    }
    submit.textContent = `正在加入 ${index + 1} / ${selected.length}…`;
  }
  state.projectMaterialSelection = failures;
  if (failures.length) {
    state.projectMaterialCandidates = failures;
    renderBulkProjectMaterialSummary();
    status.textContent = `已新增 ${createdCount} 项，${existingCount} 项原已在项目中；${failures.length} 项失败并保留待重试：${failures.map((item) => item.error).join("；")}`;
    showToast(`已处理 ${createdCount + existingCount} 项，${failures.length} 项失败`);
  } else {
    closeBulkProjectMaterialDialog();
    showToast(`已将 ${createdCount + existingCount} 项内容纳入《${topic?.title || "目标项目"}》${existingCount ? `（其中 ${existingCount} 项原已存在）` : ""}`);
    if (Number(state.topic?.id) === caseId) await loadTopics(caseId, state.topicPane, state.routeSerial);
  }
  submit.disabled = false;
  submit.textContent = failures.length ? "重试失败项" : "确认加入";
}

function closeProjectContextDialog() {
  const dialog = document.getElementById("projectContextDialog");
  if (dialog?.open) dialog.close();
}

function currentDataSliceCandidate() {
  const selected = selectedCountryDataIndicator();
  if (!selected || !state.country?.iso3) return null;
  const { dataset, indicator } = selected;
  const latestRecord = [...(indicator.records || [])].sort((left, right) => String(right.snapshot_retrieved_at || "").localeCompare(String(left.snapshot_retrieved_at || "")))[0];
  const sourceField = indicator.source_field || {};
  return {
    datasetId: Number(dataset.id),
    snapshotId: Number(latestRecord?.snapshot_id) || null,
    label: `${state.country.name || state.country.iso3} · ${indicator.label} · ${dataset.name}`,
    sourceName: dataset.source?.name || "来源未登记",
    periodLabel: `${indicator.period_from}—${indicator.period_to}`,
    filterSpec: {
      country_iso3: state.country.iso3,
      indicator_key: indicator.key,
      indicator_code: sourceField.indicator_code || indicator.indicator_code || null,
      metric_code: sourceField.metric_code || indicator.metric_code || null,
      source_dataset_code: sourceField.source_dataset_code || null,
      commodity_classification: sourceField.commodity_classification || null,
      commodity_code: sourceField.commodity_code || null,
      trade_flow: sourceField.trade_flow || null,
      period_from: indicator.period_from,
      period_to: indicator.period_to,
    },
  };
}

async function openProjectContextDialog(kind) {
  const countryIso3 = state.country?.iso3;
  if (!countryIso3) {
    showToast("请先打开一个国家空间");
    return;
  }
  const dataSlice = kind === "data" ? currentDataSliceCandidate() : null;
  if (kind === "data" && !dataSlice) {
    showToast("请先在数据目录中选择一个可用指标");
    return;
  }
  state.pendingProjectContext = { kind, countryIso3, dataSlice };
  const dialog = document.getElementById("projectContextDialog");
  const target = document.getElementById("projectContextTarget");
  const title = document.getElementById("projectContextTitle");
  const description = document.getElementById("projectContextDescription");
  const summary = document.getElementById("projectContextSummary");
  const status = document.getElementById("projectContextStatus");
  const submit = document.getElementById("projectContextSubmit");
  title.textContent = kind === "data" ? "将当前指标加入项目" : "将国家概览加入项目";
  description.textContent = kind === "data"
    ? "保存当前国家、指标口径、时期范围与固定数据快照，项目中会显示为可供 AI 引用的数据切片。"
    : "国家概览不会被复制成一份文档，而会作为项目的国家上下文，供项目内对话和 Skill 判断范围。";
  summary.innerHTML = kind === "data"
    ? `<strong>${escapeHtml(dataSlice.label)}</strong><span>${escapeHtml(dataSlice.periodLabel)} · ${escapeHtml(dataSlice.sourceName)}</span><small>${dataSlice.snapshotId ? `固定快照 #${dataSlice.snapshotId}` : "使用已保存观测，快照待登记"}</small>`
    : `<strong>${escapeHtml(state.country.name || countryIso3)} · ${escapeHtml(countryIso3)}</strong><span>国家概览、基础事实和当前国别范围</span><small>仅可加入同一国家的项目，不改变底层国家资料。</small>`;
  target.innerHTML = `<option value="">正在读取同国项目…</option>`;
  status.textContent = "正在核对项目范围与编辑权限…";
  submit.disabled = true;
  submit.textContent = "确认加入";
  if (!dialog.open) dialog.showModal();
  try {
    state.topics = await apiFetch("/reader/research-cases");
    const topics = state.topics.filter((item) => item.status !== "archived" && item.scope?.country_iso3 === countryIso3);
    target.innerHTML = topics.length
      ? `<option value="">请选择同国项目</option>${topics.map((item) => `<option value="${Number(item.id)}">${escapeHtml(item.title)}</option>`).join("")}`
      : `<option value="">暂无同国项目</option>`;
    const preferred = topics.find((item) => Number(item.id) === Number(state.topic?.id)) || topics[0];
    if (preferred) target.value = String(preferred.id);
    submit.disabled = !preferred;
    status.textContent = topics.length
      ? "只显示与当前国家一致且未归档的项目；确认后才会写入项目范围。"
      : "当前没有同国项目，请先创建项目，再返回继续加入。";
    window.setTimeout(() => (preferred ? target : document.querySelector(".project-context-dialog .bulk-project-create-link"))?.focus(), 0);
  } catch (error) {
    target.innerHTML = `<option value="">项目列表读取失败</option>`;
    status.textContent = `项目列表读取失败：${error.message}`;
  }
}

async function submitProjectContext(event) {
  event.preventDefault();
  const pending = state.pendingProjectContext;
  const target = document.getElementById("projectContextTarget");
  const submit = document.getElementById("projectContextSubmit");
  const status = document.getElementById("projectContextStatus");
  const caseId = Number(target.value || 0);
  if (!pending || !caseId) {
    showToast("请选择目标项目");
    return;
  }
  const topic = (state.topics || []).find((item) => Number(item.id) === caseId);
  submit.disabled = true;
  submit.textContent = "正在加入…";
  try {
    let created = true;
    if (pending.kind === "data") {
      const dataSlice = pending.dataSlice;
      const result = await apiFetch(`/reader/research-cases/${caseId}/data-slices`, {
        method: "POST",
        body: JSON.stringify({
          dataset_id: dataSlice.datasetId,
          snapshot_id: dataSlice.snapshotId,
          label: dataSlice.label,
          filter_spec: dataSlice.filterSpec,
        }),
      });
      created = result.created;
    } else {
      const detail = await apiFetch(`/reader/research-cases/${caseId}`);
      const bindings = detail.context_bindings || {};
      created = bindings.country_iso3 !== pending.countryIso3;
      if (created) {
        await apiFetch(`/reader/research-cases/${caseId}/context`, {
          method: "PATCH",
          body: JSON.stringify({
            expected_revision: Number(detail.scope_revision || 1),
            country_iso3: pending.countryIso3,
            event_ids: bindings.event_ids || [],
            document_version_ids: bindings.document_version_ids || [],
            data_slice_ids: bindings.data_slice_ids || [],
            field_material_ids: bindings.field_material_ids || [],
          }),
        });
      }
    }
    closeProjectContextDialog();
    const objectLabel = pending.kind === "data" ? "指标数据切片" : "国家概览上下文";
    showToast(created ? `已将${objectLabel}加入《${topic?.title || "目标项目"}》` : `${objectLabel}已在该项目中`);
    if (Number(state.topic?.id) === caseId) await loadTopics(caseId, state.topicPane, state.routeSerial);
  } catch (error) {
    status.textContent = `加入失败：${error.message}`;
    showToast(`加入失败：${error.message}`);
  } finally {
    submit.disabled = !Number(target.value || 0);
    submit.textContent = "确认加入";
  }
}

function materialCard(item, options = {}) {
  const typeLabel = displayEnum(DOCUMENT_TYPE_LABELS, item.document_type, displayEnum(SOURCE_TYPE_LABELS, item.source_type, "材料"));
  const detail = item.document_version_id ? `<button type="button" class="text-link material-link" data-material-version="${item.document_version_id}">查看版本、定位与关联对象</button>` : "";
  const remove = item.topic_removable ? `<button type="button" class="danger-link" data-remove-topic-material="${item.document_id}">移出项目</button>` : "";
  const projectAttributes = options.addable ? projectMaterialAttributes(item) : "";
  const isSelected = isMaterialSelected(item.document_version_id);
  const selectBtn = item.document_version_id ? `<button type="button" class="material-select-btn ${isSelected ? "is-selected" : ""}" data-toggle-select-material="${item.document_version_id}" data-material-title="${escapeHtml(item.title || "材料")}" data-source-name="${escapeHtml(item.source_name || "信源")}">${isSelected ? "已选入 AI" : "选入 AI"}</button>` : "";
  return `<article class="material-card ${isSelected ? "is-selected" : ""}" ${projectAttributes}><header><strong>${escapeHtml(item.title || "未命名材料")}</strong><span class="precision">${escapeHtml(typeLabel)} · ${escapeHtml(displayEnum(DATE_PRECISION_LABELS, item.published_at_precision))}</span></header><p>发布时间：${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))}<br>采集时间：${escapeHtml(localDate(item.observed_at))}<br>来源：${escapeHtml(item.source_name || "未知来源")}${item.linked_version_count > 1 ? `<br>关联版本：${item.linked_version_count} 个（逻辑材料仅展示一次）` : ""}</p><div class="inline-actions">${selectBtn}${detail}${remove}</div>${sourceActionHtml(item)}</article>`;
}

function workspaceTabsHtml(items, active) {
  return items.map((item) => `<a class="workspace-tab${item.id === active ? " is-active" : ""}" href="${escapeHtml(item.href)}">${escapeHtml(item.label)}</a>`).join("");
}

function copilotToggleHtml() {
  return `<button type="button" class="assistant-toggle" data-copilot-toggle aria-expanded="false" aria-controls="countryCopilot"><i aria-hidden="true"></i><span data-copilot-label>AI 研究</span></button>`;
}

function evidenceMatrixHtml(groups, methodNote, sources = []) {
  if (!groups?.length) return emptyState("当前事件尚无可比较主张组");
  const sourceByClaimId = new Map(sources.flatMap((source) => (source.claims || []).map((claim) => [Number(claim.id), source.document || {}])));
  const rows = groups.map((group) => {
    const claims = group.claims || [];
    const cells = claims.map((claim) => {
      const sourceDocument = sourceByClaimId.get(Number(claim.id)) || {};
      const versionId = claim.document_version_id || sourceDocument.document_version_id;
      const isGov = (claim.claimant || claim.source || "").includes("政府") || (claim.claimant || claim.source || "").includes("部") || (claim.claimant || claim.source || "").includes("ARECOMS");
      const isUn = (claim.claimant || claim.source || "").includes("联合国") || (claim.claimant || claim.source || "").includes("UN");
      const tagClass = isGov ? "is-gov" : isUn ? "is-un" : "is-media";
      const sourceKind = isGov ? "政府／官方" : isUn ? "国际组织" : "媒体／研究来源";
      return `
        <td>
          <div class="stance-tag ${tagClass}"><span>${escapeHtml(sourceKind)}</span>${escapeHtml(claim.claimant || claim.source || "来源")}</div>
          <div class="stance-value">${escapeHtml(claim.value_text || claim.position_summary || "未给出明确数值")}</div>
          ${claim.position_summary && claim.value_text ? `<small class="stance-position">立场：${escapeHtml(claim.position_summary)}</small>` : ""}
          ${claim.quote ? `<p class="stance-quote">“${escapeHtml(claim.quote)}”</p>` : ""}
          ${versionId ? `<div class="stance-source-action"><button type="button" class="text-link" data-material-version="${versionId}" aria-label="打开${escapeHtml(sourceDocument.title || claim.claimant || "当前来源")}的出处与版本">出处与版本定位 ↗</button></div>` : ""}
        </td>
      `;
    }).join("");
    return `
      <tr>
        <th class="stance-dimension">
          <strong>${escapeHtml(group.comparison_label || group.comparison_key)}</strong>
          <div><span class="precision ${group.has_difference ? "is-different" : "is-consistent"}">${group.has_difference ? "存在口径差异" : "各方表述一致"}</span></div>
        </th>
        ${cells}
      </tr>
    `;
  }).join("");

  return `
    <div class="stance-matrix-wrapper">
      <div class="stance-matrix-head">
        <h3>多来源立场与关键事实对照</h3>
        <span>按维度客观并列各方主张 · 不做系统主观裁判</span>
      </div>
      <div class="stance-table-scroll">
        <table class="stance-table">
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    </div>
    <p class="method-note stance-method-note">${escapeHtml(methodNote || "来源主张并列展示；系统不取平均、不判定真伪或权威性，仅提供可追溯的出处定位。")}</p>
  `;
}

function availabilityLabel(value, templatePreview = false, specialized = false) {
  return value === "deep_ready" ? "深度" : value === "data_partial" ? "部分" : specialized ? "专题档案" : templatePreview ? "基础档案" : "基础档案";
}

function hasTemplatePreview(item) { return Boolean(item?.template_preview_ready || item?.template?.preview_enabled || item?.country_template?.preview_enabled); }
function hasSpecializedProfile(item) { return Number(item?.template?.extension_topic_count ?? item?.country_template?.extension_topics?.length ?? 0) > 0; }
function canEnterCountry(item) { return item?.availability === "deep_ready" || item?.availability === "data_partial" || hasTemplatePreview(item); }

function countryFlag(iso2, iso3) {
  if (COUNTRY_FLAGS[iso3]) return COUNTRY_FLAGS[iso3];
  if (!/^[A-Z]{2}$/.test(iso2 || "")) return "◉";
  return String.fromCodePoint(...iso2.split("").map((character) => 127397 + character.charCodeAt(0)));
}

function membershipLabel(value) { return value === "observer" ? "联合国观察员国" : value === "member" ? "联合国会员国" : "成员身份待登记"; }

function diagnosticCards(payload) {
  const cards = [
    ["API", payload.api_ready, payload.app_version || "版本未知"],
    ["数据库", payload.database_ready, payload.database_ready ? "连接正常" : "连接失败"],
    ["迁移结构", payload.schema_ready, payload.migration_version || (payload.schema_ready ? "所需表已存在" : "结构未就绪")],
    ["演示数据", payload.demo_seeded, `${payload.event_count || 0} 事件 · ${payload.topic_count || 0} 专题 · ${payload.capability_count || 0} 能力`],
  ];
  return `<div class="diagnostic-grid">${cards.map(([label, ready, detail]) => `<article class="diagnostic-card is-${ready ? "ready" : "blocked"}"><span>${escapeHtml(label)}</span><strong>${ready ? "正常" : "待处理"}</strong><small>${escapeHtml(detail)}</small></article>`).join("")}</div>`;
}

function renderBootstrap(payload) {
  const diagnostic = document.getElementById("startupDiagnostic");
  const problems = payload.problems || [];
  const coreReady = payload.api_ready && payload.database_ready && payload.schema_ready;
  const compatible=payload.reader_contract_version === 2;
  diagnostic.hidden = coreReady && compatible;
  diagnostic.className = `startup-diagnostic is-${coreReady ? "warning" : "blocked"}`;
  diagnostic.innerHTML = `<div><b>${coreReady ? "运行环境可用，演示数据仍需补齐" : "Reader 启动诊断未通过"}</b><span>${problems.map((item) => escapeHtml(item.message)).join(" · ") || "系统已就绪"}</span></div><a href="#/system-status">查看诊断</a>`;
  if (!compatible) diagnostic.innerHTML = `<div role="alert"><b>页面与服务版本不匹配</b><span>请重新打开本机启动入口。下载和引用已停用。</span></div>`;
  const statusRoot = document.getElementById("systemStatus");
  const sourceFamilies = (payload.source_families || []).map((item) => `<article><b>${escapeHtml(displayEnum(SOURCE_TYPE_LABELS, item.source_type, "其他来源"))}</b><span>${Number(item.source_count || 0)} 个信源</span><time>最近成功 ${escapeHtml(item.last_success_at ? localDate(item.last_success_at) : "尚无成功记录")}</time></article>`).join("");
  statusRoot.innerHTML = `<div class="diagnostic-grid"><article class="diagnostic-card is-ready"><span>国家与数据覆盖</span><strong>${Number(payload.country_count || 0)} 国 · ${Number(payload.dataset_count || 0)} 数据集</strong><small>${Number(payload.event_count || 0)} 个已复核事件 · ${Number(payload.topic_count || 0)} 个专题</small></article><article class="diagnostic-card is-ready"><span>已登记信源</span><strong>${Number(payload.source_count || 0)} 个信源</strong><small>${Number(payload.source_channel_count || 0)} 个采集栏目</small></article><article class="diagnostic-card is-${Number(payload.failed_source_count || 0) || problems.length ? "blocked" : "ready"}"><span>失败信源与缺口</span><strong>${Number(payload.failed_source_count || 0)} 个失败栏目</strong><small>${problems.length ? `${problems.length} 项平台缺口需处理` : "当前未登记平台阻塞"}</small></article><article class="diagnostic-card is-${payload.capability_count ? "ready" : "blocked"}"><span>Skill 就绪度</span><strong>${Number(payload.capability_count || 0)} 项能力</strong><small>${Number(payload.capability_runs_ready || 0)} 项已有成功且非空产物</small></article><article class="diagnostic-card is-${payload.reviewed_material_count ? "ready" : "blocked"}"><span>证据覆盖</span><strong>${Number(payload.reviewed_material_count || 0)} 份已确认材料</strong><small>与待聚合材料和用户资料分开统计</small></article><article class="diagnostic-card is-${payload.latest_collection_success_at ? "ready" : "blocked"}"><span>最近成功采集</span><strong>${escapeHtml(payload.latest_collection_success_at ? localDate(payload.latest_collection_success_at) : "尚无成功记录")}</strong><small>来源发布时间与采集时间分别记录</small></article></div>${sourceFamilies ? `<section class="source-freshness-panel"><div class="section-heading"><div><h3>来源族新鲜度</h3><p>只展示已登记来源及其最近成功采集时间。</p></div></div><div class="source-freshness-grid">${sourceFamilies}</div></section>` : emptyState("尚无可展示的来源族采集记录。")}${problems.length ? `<div class="diagnostic-problems">${problems.map((item) => `<article><b>数据或能力缺口</b><p>${escapeHtml(item.message)}</p></article>`).join("")}</div>` : `<div class="empty-state">当前没有登记的数据覆盖、采集或 Skill 阻塞。</div>`}<p class="method-note">本页不展示模型、供应商或开发调试信息；详细工程诊断仅保留在本机日志。</p>`;

  document.querySelector(".system-dot")?.classList.toggle("is-ready", coreReady);
  const runtimeNote = document.getElementById("assistantRuntimeNote");
  if (runtimeNote) runtimeNote.textContent = "基于已复核证据回答 · 可自由追问";
}

function renderBootstrapFailure(error) {
  const payload = { api_ready: false, database_ready: false, schema_ready: false, demo_seeded: false, problems: [{ code: error.code || "startup_failed", message: error.message }] };
  state.bootstrap = payload;
  renderBootstrap(payload);
}

async function loadBootstrap() {
  if (window.location.protocol === "file:") throw new ReaderApiError("不能直接打开 index.html；请双击项目根目录的“打开国别智枢.command”。", "invalid_runtime");
  state.bootstrap = await apiFetch("/reader/bootstrap");
  renderBootstrap(state.bootstrap);
}

async function loadCatalog() {
  const payload = await apiFetch("/reader/countries/catalog");
  state.catalog = payload.countries || [];
  state.summary = payload.summary || {};
  state.coverageSummary = payload.coverage_summary || {};

  const countryOptions = document.getElementById("countrySearchOptions");
  if (countryOptions) countryOptions.innerHTML = state.catalog.map((item) => `<option value="${escapeHtml(item.iso3)}" label="${escapeHtml(item.name_zh)} · ${escapeHtml(item.name_en || "")}"></option>`).join("");
  renderGlobalOverview();
  renderCatalog();
  await renderWorldMap();
}

function resolveCountryIso3Input(rawValue) {
  const value = String(rawValue || "").trim();
  if (!value) return "";
  const normalized = value.toLowerCase();
  const exact = state.catalog.find((item) => item.iso3.toLowerCase() === normalized || String(item.name_zh || "").toLowerCase() === normalized || String(item.name_en || "").toLowerCase() === normalized);
  if (exact) return exact.iso3;
  const matches = state.catalog.filter((item) => [item.iso3, item.name_zh, item.name_en].some((candidate) => String(candidate || "").toLowerCase().includes(normalized)));
  return matches.length === 1 ? matches[0].iso3 : null;
}

function commitEventCountryFilter(input) {
  const countryIso3 = resolveCountryIso3Input(input.value);
  if (countryIso3 === null) {
    showToast("请输入准确的国家名、英文名或 ISO3，并从建议中选择");
    input.value = state.eventFilterCountry;
    return false;
  }
  state.eventFilterCountry = countryIso3;
  input.value = countryIso3;
  loadEvents(null, "overview", state.routeSerial);
  return true;
}

function renderGlobalOverview() {
  const summary = state.summary || {};
  const coverage = state.coverageSummary || {};
  const metrics = document.getElementById("globalOverviewMetrics");
  const topics = document.getElementById("globalOverviewTopics");
  const freshness = document.getElementById("globalOverviewFreshness");
  const regionSelect = document.getElementById("overviewTopicRegion");
  if (freshness) freshness.textContent = summary.latest_collection_at ? `最近采录 ${localDate(summary.latest_collection_at)}` : "最近采录时间待核验";
  if (metrics) {
    const rows = [
      ["已覆盖国家", Number(coverage.covered_countries || 0), "有确认材料、已复核事件或数据快照"],
      ["缺口国家", Number(coverage.gap_countries || 0), "只有基础目录，专题证据待补"],
      ["最近更新国家", Number(coverage.recently_updated_countries || 0), `近 ${Number(coverage.recent_window_days || 90)} 天有证据更新`],
      ["待核验国家", Number(coverage.pending_verification_countries || 0), "存在待审材料、提及或草稿事件"],
    ];
    metrics.innerHTML = rows.map(([label, value, note]) => `<article><span>${escapeHtml(label)}</span><strong>${Number(value).toLocaleString("zh-CN")}</strong><small>${escapeHtml(note)}</small></article>`).join("");
  }
  if (regionSelect) {
    const selected = regionSelect.value || "all";
    const regions = [...new Set(state.catalog.map((item) => item.region_zh).filter(Boolean))].sort((left, right) => left.localeCompare(right, "zh-CN"));
    regionSelect.innerHTML = `<option value="all">全部区域</option>${regions.map((region) => `<option value="${escapeHtml(region)}">${escapeHtml(region)}</option>`).join("")}`;
    regionSelect.value = regions.includes(selected) ? selected : "all";
  }
  if (topics) {
    const region = regionSelect?.value || "all";
    const windowDays = document.getElementById("overviewTopicWindow")?.value || "90";
    const sourceFilter = document.getElementById("overviewTopicSource")?.value || "all";
    const cutoff = windowDays === "all" ? null : Date.now() - Number(windowDays) * 86400000;
    const rows = state.catalog.filter((item) => (region === "all" || item.region_zh === region) && (!cutoff || (item.last_evidence_at && new Date(item.last_evidence_at).getTime() >= cutoff)));
    if (sourceFilter !== "all") {
      topics.innerHTML = `<span class="overview-topic-empty">国家目录聚合尚未返回“${escapeHtml(sourceFilter)}”来源分项；不以总量代替该筛选结果。</span>`;
      return;
    }
    const tagIndex = new Map();
    rows.forEach((item) => (item.template?.focus_tags || []).forEach((tag) => {
      const current = tagIndex.get(tag) || { tag, materialCount: 0, eventCount: 0, countryCount: 0 };
      current.countryCount += 1;
      current.materialCount += Number(item.coverage?.confirmed_materials || 0);
      current.eventCount += Number(item.coverage?.reviewed_events || 0);
      tagIndex.set(tag, current);
    }));
    const focusTags = [...tagIndex.values()].sort((left, right) => (right.materialCount + right.eventCount) - (left.materialCount + left.eventCount) || left.tag.localeCompare(right.tag, "zh-CN")).slice(0, 6);
    topics.innerHTML = focusTags.length ? focusTags.map((item) => `<article><strong>${escapeHtml(item.tag)}</strong><span>${item.countryCount} 国</span><small>材料 ${item.materialCount} · 已复核事件 ${item.eventCount} · 人工优先级未登记</small></article>`).join("") : `<span class="overview-topic-empty">当前筛选下没有已登记议题；不使用模拟议题补齐。</span>`;
  }
}

function previewCountry(item) {
  if (!item) return;
  state.selectedMapIso3 = item.iso3;
  renderMapSelection(item);
}

function selectCountry(item) {
  if (!item) return;
  state.selectedMapIso3 = item.iso3;
  rememberCountry(item.iso3);
  renderMapSelection(item);
  if (canEnterCountry(item)) navigate(`/countries/${item.iso3}`);
  else showToast(`${item.name_zh}：暂无入口`);
}

function rememberCountry(iso3) {
  state.recentCountryIso3s = [iso3, ...(state.recentCountryIso3s || []).filter((value) => value !== iso3)].slice(0, 6);
  try { window.sessionStorage.setItem(RECENT_COUNTRY_STORAGE_KEY, JSON.stringify(state.recentCountryIso3s)); } catch (_) { /* session preference unavailable */ }
}

function renderMapSelection(item) {
  const root = document.getElementById("worldMapSelection");
  if (!root || !item) return;
  root.classList.remove("is-expanded");
  const ready = item.availability === "deep_ready";
  const specialized = hasSpecializedProfile(item);
  const enterable = canEnterCountry(item);
  const label = ready ? "进入国别空间 ➔" : "打开国别空间 ➔";
  const flag = countryFlag(item.iso2, item.iso3);
  const statusBadge = ready
    ? '<span class="map-selection-status is-ready"><i aria-hidden="true"></i>深度示范样板</span>'
    : specialized
      ? '<span class="map-selection-status is-topic"><i aria-hidden="true"></i>专题档案</span>'
      : '<span class="map-selection-status"><i aria-hidden="true"></i>基础国情档案</span>';
  const profile = item.profile_completeness || {};
  const coverage = item.coverage || {};
  const tags = item.template?.focus_tags || [];
  const blurb = `${Number(coverage.confirmed_materials || 0)} 份确认材料 · ${Number(coverage.reviewed_events || 0)} 个已复核事件 · ${Number(coverage.structured_observations || 0).toLocaleString("zh-CN")} 条规范化观测`;
  root.innerHTML = `
    <div class="map-selection-main">
      <div class="map-selection-heading">
        <h3>${flag} ${escapeHtml(item.name_zh)}</h3>
        ${statusBadge}
      </div>
      <span class="map-selection-meta is-compact">${escapeHtml(item.iso3)} · ${escapeHtml(item.name_en || "")} · 画像完整度 ${Number(profile.percent || 0)}%</span>
      <div class="map-selection-details" id="mapSelectionDetails-${escapeHtml(item.iso3)}">
        <span class="map-selection-meta">${membershipLabel(item.membership)}</span>
        <div class="map-selection-evidence"><span>${escapeHtml(blurb)}</span><span>重点议题：${tags.length ? escapeHtml(tags.slice(0, 3).join("、")) : "待登记"}</span><span>风险与争议：仅展示有证据内容，不按数量推断</span></div>
      </div>
    </div>
    <div class="map-selection-action">
      <button type="button" class="ghost-btn map-selection-toggle" data-map-selection-toggle aria-expanded="false" aria-controls="mapSelectionDetails-${escapeHtml(item.iso3)}">展开详情</button>
      ${enterable ? `<button type="button" class="primary-btn" data-select-country="${escapeHtml(item.iso3)}">${label}</button>` : '<button type="button" class="ghost-btn" disabled>暂无入口</button>'}
    </div>
  `;
  if (state.worldLayer) state.worldLayer.eachLayer((layer) => {
    const selected = layer.feature?.properties?.iso3 === item.iso3;
    if (selected) layer.bringToFront();
    layer.setStyle(worldFeatureStyle(layer.feature, selected));
  });
}

function worldFeatureStyle(feature, selected = false) {
  const item = state.catalog.find((row) => row.iso3 === feature?.properties?.iso3);
  const ready = item?.availability === "deep_ready";
  const partial = item?.availability === "data_partial";
  const specialized = hasSpecializedProfile(item);
  return {
    color: selected ? "#174dad" : ready ? "#087a55" : specialized ? "#6d50b8" : "#8ca0b9",
    weight: selected ? 2.2 : ready || specialized ? 1.4 : .65,
    fillColor: ready ? "#20b884" : specialized ? "#aa91ee" : partial ? "#78a4ef" : "#d7e0eb",
    fillOpacity: selected ? .92 : ready ? .8 : .66,
  };
}

async function renderWorldMap() {
  const root = document.getElementById("worldMap");
  if (!root || state.worldMap || typeof window.L === "undefined") return;
  let response = await fetch("./assets/world-countries-simplified.geojson");
  if (!response.ok) response = await fetch("/reader/assets/world-countries-simplified.geojson");
  if (!response.ok) throw new ReaderApiError(`世界地图资产 HTTP ${response.status}`, "map_asset_failed");
  const geojson = await response.json();
  state.worldMap = window.L.map(root, { zoomControl: true, attributionControl: true, minZoom: 1, maxZoom: 7, worldCopyJump: false, dragging: true, touchZoom: true, doubleClickZoom: true, scrollWheelZoom: true, boxZoom: true, keyboard: true }).setView([15, 8], 2);
  state.worldMap.attributionControl.setPrefix(false);
  state.worldMap.attributionControl.addAttribution("Natural Earth · datasets/geo-countries · PDDL");
  state.worldLayer = window.L.geoJSON(geojson, {
    style: (feature) => worldFeatureStyle(feature, feature.properties?.iso3 === state.selectedMapIso3),
    onEachFeature: (feature, layer) => {
      const item = state.catalog.find((row) => row.iso3 === feature.properties?.iso3);
      if (!item) return;
      layer.bindTooltip(`${escapeHtml(item.name_zh)} · ${availabilityLabel(item.availability, hasTemplatePreview(item), hasSpecializedProfile(item))}`, { sticky: true, className: "country-map-tooltip" });
      layer.on({
        click: () => previewCountry(item),
        mouseover: () => layer.setStyle({ weight: 2, color: "#2568e8", fillOpacity: .88 }),
        mouseout: () => layer.setStyle(worldFeatureStyle(feature, feature.properties?.iso3 === state.selectedMapIso3)),
        add: () => {
          const element = layer.getElement?.();
          if (!element) return;
          element.setAttribute("tabindex", "0");
          element.setAttribute("role", "button");
          element.setAttribute("aria-label", `${item.name_zh}，${availabilityLabel(item.availability, hasTemplatePreview(item), hasSpecializedProfile(item))}`);
          element.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); previewCountry(item); } });
        },
      });
    },
  }).addTo(state.worldMap);
  try {
    state.worldMap.fitBounds(state.worldLayer.getBounds(), { padding: [8, 8] });
  } catch (_) {}
  renderMapSelection(state.catalog.find((item) => item.iso3 === state.selectedMapIso3));
  [60, 200, 500].forEach((delay) => window.setTimeout(() => {
    if (state.worldMap) {
      state.worldMap.invalidateSize();
    }
  }, delay));
}

function renderCatalog() {
  const root = document.getElementById("countryCatalogList");
  const query = document.getElementById("countryCatalogSearch").value.trim().toLowerCase();
  const rows = state.catalog.filter((item) => {
    const matchesQuery = [item.iso3, item.name_zh, item.name_en].some((value) => String(value).toLowerCase().includes(query));
    const matchesMode = state.countryCatalogMode === "deep" ? item.availability === "deep_ready" : state.countryCatalogMode === "recent" ? state.recentCountryIso3s.includes(item.iso3) : true;
    return matchesQuery && matchesMode;
  }).sort((left, right) => {
    const recentLeft = state.recentCountryIso3s.indexOf(left.iso3); const recentRight = state.recentCountryIso3s.indexOf(right.iso3);
    if (state.countryCatalogMode === "recent") return recentLeft - recentRight;
    const rank = (item) => item.availability === "deep_ready" ? 0 : hasSpecializedProfile(item) ? 1 : item.availability === "data_partial" ? 2 : 3;
    return rank(left) - rank(right) || String(left.name_zh || left.name_en).localeCompare(String(right.name_zh || right.name_en), "zh-CN");
  });
  const visibleRows = rows.slice(0, 6);
  document.querySelectorAll("[data-country-mode]").forEach((button) => button.classList.toggle("is-active", button.dataset.countryMode === state.countryCatalogMode));
  document.getElementById("catalogCount").textContent = query || state.countryCatalogMode !== "all" ? `匹配 ${rows.length} 国` : `${state.catalog.length || 195} 国可搜索`;
  root.innerHTML = visibleRows.length ? visibleRows.map((item) => {
    const ready = item.availability === "deep_ready";
    const partial = item.availability === "data_partial";
    const preview = hasTemplatePreview(item);
    const specialized = hasSpecializedProfile(item);
    const coverage = item.coverage?.structured_observations || 0;
    return `<button type="button" class="country-row${ready ? " is-ready" : specialized ? " is-template" : partial ? " is-partial" : ""}" data-select-country="${escapeHtml(item.iso3)}"><span>${escapeHtml(item.iso3)}</span><div><strong>${escapeHtml(item.name_zh)}</strong><small>${escapeHtml(item.name_en || "")}</small></div><b>${availabilityLabel(item.availability, preview, specialized)}</b></button>`;
  }).join("") : `<div class="catalog-empty"><strong>${state.countryCatalogMode === "recent" ? "本次会话尚未访问国家" : "没有匹配的国家"}</strong><span>${state.countryCatalogMode === "recent" ? "从地图或推荐国家中进入一个国家后，这里会保留最近记录。" : "请直接输入中文、英文国名或 ISO3。"}</span></div>`;
}

function navigate(path) {
  const nextHash = `#${path.startsWith("/") ? path : `/${path}`}`;
  if (window.location.hash === nextHash) route(); else window.location.hash = nextHash;
}

function replaceRoute(path) {
  const nextHash = `#${path.startsWith("/") ? path : `/${path}`}`;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${nextHash}`);
}

function renderSidebarContext() {
  const root = document.getElementById("sidebarContext");
  if (!root) return;
  if (state.view === "countries" && state.country) {
    root.hidden = true;
    root.innerHTML = "";
    return;
  }
  if (state.view === "events" && state.eventDetail) {
    root.innerHTML = `<a href="#/countries/global-events">国别空间 · 全球动态</a> / ${escapeHtml(state.eventDetail.title)}`;
    root.hidden = false;
    return;
  }
  if (state.view === "projects" && state.topic) {
    root.innerHTML = `<a href="#/projects">项目</a> / ${escapeHtml(state.topic.title)}`;
    root.hidden = false;
    return;
  }
  if (state.view === "capabilities" && (state.activeCapabilitySlug || state.activeCapabilityRunId)) {
    root.innerHTML = `<a href="#/capabilities">能力</a> / ${escapeHtml(state.activeCapabilitySlug || "运行")}`;
    root.hidden = false;
    return;
  }
  root.hidden = true;
  root.innerHTML = "";
}

function showView(view, navSpace = view) {
  state.view = view;
  document.body.classList.toggle("country-workbench-active", view === "countries" && Boolean(state.country));
  document.body.classList.toggle("project-workbench-active", view === "projects" && Boolean(state.topic));
  document.body.classList.toggle("capability-space-active", view === "capabilities");
  document.querySelectorAll("[data-view]").forEach((section) => { section.hidden = section.dataset.view !== view; section.classList.toggle("is-active", section.dataset.view === view); });
  document.querySelectorAll("[data-route-space]").forEach((item) => item.classList.toggle("is-active", item.dataset.routeSpace === navSpace));
  renderSidebarContext();
  window.scrollTo({ top: 0 });
}

async function route() {
  if (document.getElementById("countryReadingDialog").open) closeCountryReading(false);
  const serial = ++state.routeSerial;
  state.assistantFocus = null;
  const rawRoute = window.location.hash.replace(/^#\/?/, "");
  const [routePath, routeQuery = ""] = rawRoute.split("?", 2);
  state.routeQuery = new URLSearchParams(routeQuery);
  let parts = routePath.split("/").filter(Boolean);
  if (parts[0] === "research") parts = ["countries", ...parts.slice(1)];
  if (parts[0] === "report") parts = ["capabilities"];
  if (parts[0] === "topics") {
    const legacyPane = { library: "evidence", materials: "evidence", activity: "tasks", feed: "tasks", events: "tasks", runs: "tasks", "policy-graph": "outputs", collaboration: "settings" }[parts[2]] || parts[2];
    parts = ["projects", parts[1], legacyPane].filter(Boolean);
    replaceRoute(`/${parts.join("/")}`);
  }
  if (parts[0] === "invite") { window.ReaderAuth.open(); return; }
  const space = parts[0] || "countries";
  if (!window.location.hash) { navigate("/countries"); return; }
  if (space === "countries") {
    if (parts[1] === "global-events") {
      showView("events", "countries");
      state.eventFilterCountry = "";
      await loadEvents(null, "overview", serial);
      syncAssistantContext();
      maybeAutoOpenAssistant(false);
      return;
    }
    if (parts[1] === "search") {
      showView("countries");
      openGlobalSearch();
      syncAssistantContext();
      maybeAutoOpenAssistant(false);
      return;
    }
    if (parts[2] === "events" && numericRouteId(parts[3]) !== null) {
      showView("countries");
      await openCountry(parts[1].toUpperCase(), "events", serial);
      syncAssistantContext(); maybeAutoOpenAssistant(true);
      state.selectedCountryEventId = numericRouteId(parts[3]);
      await renderCountryEventChainDetail(state.selectedCountryEventId);
      return;
    }
    showView("countries");
    if (parts[1]) await openCountry(parts[1].toUpperCase(), countryPaneFromRoute(parts), serial); else openGlobal();
    if (state.country && ["reports","policies"].includes(parts[2])) await syncPublicationReading(parts[2],numericRouteId(parts[3]),serial);
    syncAssistantContext();
    maybeAutoOpenAssistant(Boolean(parts[1]));
    return;
  }
  if (space === "events") {
    showView("events", "countries");
    state.eventFilterCountry = numericRouteId(parts[1]) === null ? "" : state.eventFilterCountry;
    await loadEvents(numericRouteId(parts[1]), eventPaneFromRoute(parts), serial);
    if (numericRouteId(parts[1]) !== null && state.eventDetail) {
      replaceRoute(`/countries/${state.eventDetail.country?.iso3 || state.eventFilterCountry || "COD"}/events/${parts[1]}${parts[2] ? `/${eventPaneFromRoute(parts)}` : ""}`);
    } else replaceRoute("/countries/global-events");
    syncAssistantContext();
    maybeAutoOpenAssistant(numericRouteId(parts[1]) !== null);
    return;
  }
  if (space === "projects") {
    showView("projects");
    const projectPane = ReaderRouting.projectPaneFromRoute(parts);
    const directObjectKinds = { runs: "run", materials: "material", events: "event", field: "field_material", members: "member" };
    state.projectObjectRoute = null;
    if (directObjectKinds[parts[2]] && parts[3]) {
      state.projectObjectRoute = { kind: directObjectKinds[parts[2]], id: parts[3] };
    } else if (parts[3]) {
      state.projectObjectRoute = { kind: projectPane === "tasks" ? "task" : projectPane === "outputs" ? "output" : parts[3], id: parts[4] || parts[3] };
    }
    await loadTopics(numericRouteId(parts[1]), projectPane, serial);
    document.body.classList.toggle("project-workbench-active", numericRouteId(parts[1]) !== null);
    syncAssistantContext();
    maybeAutoOpenAssistant(false);
    return;
  }
  if (space === "capabilities") {
    state.activeCapabilitySlug = parts[1] === "catalog" && parts[2] ? parts[2] : null;
    state.activeCapabilityRunId = parts[1] === "runs" && parts[2] ? Number(parts[2]) : null;
    showView("capabilities");
    await loadCapabilityCatalog();
    if (parts[1] === "workflows") await SkillWorkflows.open(parts[2], Number(parts[3]) || null);
    else if (parts[1] === "runs" && parts[2]) await viewCapabilityRun(parts[2]);
    else if (parts[1] === "catalog" && parts[2]) await openCapabilityCatalogItem(parts[2]);
    else if (parts[1] === "mine") await renderCapabilityMine(parts[2] || null);
    else if (parts[1] === "create") renderCapabilityCreate(parts[2] || "1");
    else if (parts[1] === "history") renderCapabilityHistory(parts[2] || null);
    else if (parts[1]) { replaceRoute(`/capabilities/history/${parts[1]}`); renderCapabilityHistory(parts[1]); }
    else showCapabilityHall();
    renderSidebarContext();
    syncAssistantContext();
    maybeAutoOpenAssistant(Boolean(parts[1]));
    return;
  }
  if (space === "resources") { showView(space); renderResourcePortal(); syncAssistantContext(); maybeAutoOpenAssistant(false); return; }
  if (space === "settings" || space === "system-status") { showView("system-status", "settings"); syncAssistantContext(); return; }
  if (space === "methods") { showView(space); syncAssistantContext(); maybeAutoOpenAssistant(false); return; }
  navigate("/countries");
}

function openGlobal() {
  state.country = null;
  document.body.classList.remove("country-workbench-active");
  document.getElementById("globalHub").hidden = false;
  document.getElementById("globalSearchView").hidden = true;
  document.getElementById("countryWorkspace").hidden = true;
  renderSidebarContext();
  window.scrollTo({ top: 0 });
  if (!state.worldMap) {
    renderWorldMap();
  } else {
    [50, 200, 500].forEach((delay) => window.setTimeout(() => {
      if (state.worldMap) {
        state.worldMap.invalidateSize();
      }
    }, delay));
  }
}

function openGlobalSearch() {
  state.country = null;
  document.body.classList.remove("country-workbench-active");
  document.getElementById("globalHub").hidden = true;
  document.getElementById("countryWorkspace").hidden = true;
  document.getElementById("globalSearchView").hidden = false;
  renderSidebarContext();
  window.scrollTo({ top: 0 });
  hydrateUnifiedSearchFromRoute();
  window.setTimeout(() => document.getElementById("materialSearchForm")?.requestSubmit(), 0);
}

async function openCountry(iso3, pane = "overview", serial = state.routeSerial) {
  const item = state.catalog.find((country) => country.iso3 === iso3);
  if (!item) { showToast("国家目录中不存在该 ISO3"); navigate("/countries"); return; }
  if (!canEnterCountry(item)) {
    state.selectedMapIso3 = item.iso3;
    renderMapSelection(item);
    showToast(`${item.name_zh}：模板已定义，数据待接入`);
    navigate("/countries");
    return;
  }
  document.getElementById("globalHub").hidden = true;
  document.getElementById("globalSearchView").hidden = true;
  rememberCountry(iso3);
  const workspace = document.getElementById("countryWorkspace");
  workspace.hidden = false;
  document.body.classList.add("country-workbench-active");
  state.countryPane = pane;
  if (state.country?.iso3 === iso3) {
    renderCountryPane();
    renderSidebarContext();
    if (pane === "overview") {
      if (state.countryMap) [40, 180].forEach((delay) => window.setTimeout(() => { state.countryMap?.invalidateSize(); syncCountryMapLabels(); }, delay));
      else renderAdmMap();
    }
    return;
  }
  workspace.classList.add("country-is-loading");
  workspace.classList.remove("country-has-error");
  document.getElementById("countryToolbarName").textContent = "正在读取国家空间…";
  const framework = document.getElementById("frameworkState");
  framework.hidden = false;
  framework.innerHTML = detailSkeleton("正在读取国家档案、指标和证据状态…");
  try {
    const [country, reports] = await Promise.all([
      apiFetch(`/reader/countries/${iso3}`),
      apiFetch(`/reader/event-reports?country_iso3=${encodeURIComponent(iso3)}`),
    ]);
    if (serial !== state.routeSerial) return;
    state.country = country;
    state.countryReports = reports;
    workspace.classList.remove("country-is-loading", "country-has-error");
    renderCountry();
  } catch (error) {
    workspace.classList.remove("country-is-loading");
    workspace.classList.add("country-has-error");
    document.getElementById("frameworkState").hidden = false;
    document.getElementById("frameworkState").innerHTML = errorState(error.message, true);
  }
}

function renderCountry() {
  const country = state.country;
  const preview = hasTemplatePreview(country);
  const specialized = hasSpecializedProfile(country);
  state.policyExpanded = false;
  state.selectedTrendKey = null;
  state.selectedTrendPoint = null;
  const flag = countryFlag(country.iso2, country.iso3);

  const flagEl = document.getElementById("countryToolbarFlag");
  if (flagEl) flagEl.textContent = flag;
  document.getElementById("countryToolbarName").textContent = `${country.name} · ${country.iso3}`;
  document.getElementById("countryAvailabilityLabel").textContent = availabilityLabel(country.availability, preview, specialized);
  document.getElementById("countryFreshness").textContent = `最近采集 ${localDate(country.updated_at)}`;
  renderCountryProfileCompleteness();
  renderCountryBasicFacts();
  renderCountryRecentChanges();
  renderCountrySourceMix();
  renderCountryConflictSummary();
  renderStrategicSignals();
  renderKpis();
  renderTrends();
  renderCountryTopics();
  renderTrade();
  renderPolicy();
  renderCountryRecentSignals();
  loadCountryChannelMaterials(country.iso3);
  loadCountryDataCatalog(country.iso3);
  renderCountryProvenanceBar();
  renderSidebarContext();
  renderQaPresets();
  renderAssistantConversation();
  renderCountryPane();
  if (state.countryPane === "overview") renderAdmMap();
  syncHeaderMetrics();

  const framework = document.getElementById("frameworkState");
  if (framework) {
    framework.hidden = true;
    framework.innerHTML = templateContractHtml(country);
  }
}

function renderCountryPane() {
  if (!state.country) return;
  const iso3 = state.country.iso3;
  const pane = state.countryPane || "overview";
  const hrefs = {
    overview: `#/countries/${iso3}`,
    events: `#/countries/${iso3}/events`,
    research: `#/countries/${iso3}/research`,
    reports: `#/countries/${iso3}/reports`,
    policies: `#/countries/${iso3}/policies`,
    field: `#/countries/${iso3}/field`,
    data: `#/countries/${iso3}/data`,
  };
  document.querySelectorAll("#countryAnchorNav [data-country-pane]").forEach((link) => {
    const itemPane = link.dataset.countryPane;
    link.href = hrefs[itemPane];
    link.classList.toggle("is-active", itemPane === pane);
    if (itemPane === pane) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-country-pane-content]").forEach((section) => {
    section.hidden = section.dataset.countryPaneContent !== pane;
  });
  if (pane === "field") window.FieldLibrary?.load(iso3);
  document.getElementById("country-provenance").hidden = false;
  renderQaPresets();
  document.querySelector(".country-pane-viewport")?.scrollTo({ top: 0 });
}

function materialRightsLabel(rights = {}) {
  if (rights.status !== "reviewed") return "权利待审 · 摘要不展示";
  return `${rights.storage_scope || "存储范围未登记"} · ${rights.terms_state || "条款待核验"}`;
}

function materialEvidenceCardHtml(item, { addable = true } = {}) {
  const rights = item.rights || {};
  const locator = item.locator || {};
  const sourceUrl = safeUrl(locator.canonical_url || locator.discovery_url);
  const abstract = item.abstract || (rights.status === "reviewed" ? "该版本没有可展示摘要。" : "精确采集通道不可追溯或权利待审，摘要已保守隐藏。");
  return `<article class="material-evidence-card" ${addable ? projectMaterialAttributes(item) : ""}><header><div><span class="feed-status ${rights.status === "reviewed" ? "is-reviewed" : "is-report"}">${escapeHtml(item.evidence_status || "confirmed")} · ${escapeHtml(materialRightsLabel(rights))}</span><h3>${escapeHtml(item.title || "未命名材料")}</h3></div><span class="precision">${escapeHtml(displayEnum(DOCUMENT_TYPE_LABELS, item.document_type, "材料"))}</span></header><p>${escapeHtml(abstract)}</p><dl><div><dt>来源</dt><dd>${escapeHtml(item.source_name || "未登记")}</dd></div><div><dt>语言</dt><dd>${escapeHtml(item.language || "待核验")}</dd></div><div><dt>发布 / 观察</dt><dd>${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))} / ${escapeHtml(localDate(item.observed_at))}</dd></div><div><dt>版本</dt><dd>v${Number(item.version_no || 1)} · ${escapeHtml(item.authority_level || "权威级别待核验")}</dd></div></dl><footer>${sourceUrl ? `<a class="text-link" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">打开${locator.canonical_url ? "正式来源" : "发现入口"} ↗</a>` : `<span>原始定位待核验</span>`}<button type="button" class="text-link" data-material-version="${Number(item.document_version_id)}">查看版本</button></footer></article>`;
}

async function loadPublicationChannel(iso3, channel, append=false) {
  const key=`${iso3}:${channel}`, isReports=channel==="think_tank_report";
  const root=document.getElementById(isReports ? "countryThinkTankReports":"countryResearchMaterials");
  state.channelRequests ||= {};
  const request=state.channelRequests[key]=(state.channelRequests[key] || 0)+1;
  const params=new URLSearchParams({country_iso3:iso3,channel,limit:"50",offset:String(append ? (state.countryChannelMaterials[key] || []).length : 0)});
  if(!isReports) {
    const view=countryReadingState();
    if(view.researchTopic) params.set("topic",view.researchTopic);
    const years=Number(state.researchWindowYears || 3), end=new Date(), start=new Date(end);
    start.setUTCFullYear(end.getUTCFullYear()-years,0,1);
    start.setUTCHours(0,0,0,0); end.setUTCHours(23,59,59,999);
    if(view.researchYear) { params.set("published_from",new Date(Math.max(start,new Date(`${view.researchYear}-01-01T00:00:00Z`))).toISOString()); params.set("published_to",new Date(Math.min(end,new Date(`${view.researchYear}-12-31T23:59:59Z`))).toISOString()); }
    else { params.set("published_from",start.toISOString()); params.set("published_to",end.toISOString()); }
  }
  const view=countryReadingState();
  if(isReports && view.reportQueryText) params.set("q",view.reportQueryText);
  if(isReports && view.source) params.set("source_id",view.source);
  if(!isReports && view.researchQuery) params.set("q",view.researchQuery);
  if(!isReports && view.researchSource) params.set("source_id",view.researchSource);
  if(!append) root.innerHTML=detailSkeleton("正在读取资料…");
  try {
    const payload=await apiFetch(`/reader/search/materials?${params}`);
    if(state.country?.iso3!==iso3 || state.channelRequests[key]!==request) return;
    state.countryChannelPages ||= {}; state.countryChannelPages[key]=payload;
    state.countryChannelMaterials[key]=append ? [...state.countryChannelMaterials[key],...payload.items] : payload.items;
    if(isReports) { state.reportComparisonItems ||= {}; payload.items.forEach(item=>state.reportComparisonItems[item.document_version_id]=item); renderCountryReportChannel(); }
    else { renderCountryResearchChannel(); loadResearchOverview(); }
  } catch(error) { if(state.country?.iso3===iso3 && state.channelRequests[key]===request) root.innerHTML=errorState(error.message); }
}
async function loadCountryChannelMaterials(iso3) {
  await Promise.all([loadPublicationChannel(iso3,"frontier_research"),loadPublicationChannel(iso3,"think_tank_report"),loadCountryCollectionStatus(iso3)]);
}

function hydrateUnifiedSearchFromRoute() {
  const params = state.routeQuery || new URLSearchParams();
  state.materialSearchType = params.get("object_type") || "all";
  document.querySelectorAll("[data-search-type]").forEach((button) => button.classList.toggle("is-active", button.dataset.searchType === state.materialSearchType));
  const fields = {
    q: "materialSearchQuery", country_iso3: "materialSearchCountry", topic: "materialSearchTopic",
    actor: "materialSearchActor", language: "materialSearchLanguage", source_type: "materialSearchSourceType",
    date_basis: "materialSearchDateBasis", date_from: "materialSearchFrom", date_to: "materialSearchTo",
  };
  Object.entries(fields).forEach(([key, id]) => {
    const input = document.getElementById(id);
    if (input) input.value = params.get(key) || (key === "date_basis" ? "published" : "");
  });
  syncUnifiedSearchFilterAvailability();
}

function syncUnifiedSearchFilterAvailability() {
  const countryOnly = state.materialSearchType === "country";
  ["materialSearchActor", "materialSearchLanguage", "materialSearchSourceType", "materialSearchDateBasis", "materialSearchFrom", "materialSearchTo"].forEach((id) => {
    const input = document.getElementById(id);
    if (!input) return;
    input.disabled = countryOnly;
    input.closest("label").hidden = countryOnly;
    if (countryOnly) input.value = id === "materialSearchDateBasis" ? "published" : "";
  });
}

function unifiedSearchRoute(params) {
  const visible = new URLSearchParams(params);
  visible.delete("limit");
  visible.delete("offset");
  const query = visible.toString();
  return `/countries/search${query ? `?${query}` : ""}`;
}

function unifiedObjectLabel(value) {
  return ({ country: "国家", event: "事件", policy: "政策", report: "报告", paper: "论文", data: "数据", material: "材料" })[value] || value || "对象";
}

function unifiedCitationItem(item) {
  return {
    title: item.title,
    source_name: item.source?.name,
    publisher: item.source?.name,
    published_at: item.published_at || item.occurred_at || item.updated_at,
    canonical_url: item.locator?.canonical_url,
    source_url: item.locator?.canonical_url,
    document_version_id: String(item.object_key || "item").replace(/\W+/g, "_"),
  };
}

function unifiedSearchCardHtml(item) {
  const citation = generateCitationData(unifiedCitationItem(item));
  const countries = (item.country || []).map((country) => country.name || country.iso3).filter(Boolean).join("、") || "全球/未登记";
  const rights = item.rights || {};
  const locator = item.locator || null;
  const route = locator?.route;
  const sourceUrl = safeUrl(locator?.canonical_url);
  const openTarget = route || sourceUrl;
  const dateLabel = item.published_at ? `发布 ${localDate(item.published_at)}` : item.occurred_at ? `发生 ${localDate(item.occurred_at)}` : "发布时间待核验";
  const restricted = item.access_state === "review_required";
  const [projectKind, projectId] = String(item.object_key || "").split(":");
  const projectAction = item.actions?.can_add_to_project
    ? projectKind === "document-version"
      ? ""
      : `<button type="button" class="ghost-btn compact" data-add-search-project="${escapeHtml(item.object_key)}" data-link-label="${escapeHtml(item.title || "对象")}">加入项目</button>`
    : "";
  const projectAttributes = projectKind === "document-version" && item.actions?.can_add_to_project ? projectMaterialAttributes({ document_version_id: projectId, title: item.title, source_name: item.source?.name }) : "";
  return `<article class="unified-search-card${restricted ? " is-restricted" : ""}" data-search-object="${escapeHtml(item.object_key)}" ${projectAttributes}>
    <header><div><span class="object-type-chip">${escapeHtml(unifiedObjectLabel(item.object_type))}</span><h3>${escapeHtml(item.title || "未命名对象")}</h3></div><span class="access-chip">${escapeHtml(item.access_state || "unknown")}</span></header>
    ${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}
    <dl><div><dt>国家</dt><dd>${escapeHtml(countries)}</dd></div><div><dt>来源</dt><dd>${escapeHtml(item.source?.name || "未登记")}</dd></div><div><dt>时间</dt><dd>${escapeHtml(dateLabel)} · 录入 ${escapeHtml(localDate(item.recorded_at))} · 更新 ${escapeHtml(localDate(item.updated_at))}</dd></div><div><dt>版本 / 证据</dt><dd>${escapeHtml(item.version || "待核验")} · ${escapeHtml(item.evidence_status || "待核验")} · 权利 ${escapeHtml(rights.status || "unknown")}</dd></div></dl>
    <footer><div class="citation-tools"><button type="button" class="text-link" data-copy-search-citation="${escapeHtml(item.object_key)}" data-citation-text="${escapeHtml(citation.gbt)}">复制引用</button><button type="button" class="text-link" data-download-search-citation="bibtex" data-citation-key="${escapeHtml(item.object_key)}">BibTeX</button><button type="button" class="text-link" data-download-search-citation="ris" data-citation-key="${escapeHtml(item.object_key)}">RIS</button></div><div class="inline-actions">${openTarget ? `<a class="text-link" href="${escapeHtml(openTarget)}"${sourceUrl ? ' target="_blank" rel="noopener noreferrer"' : ""}>${sourceUrl ? "跳转原文定位" : "打开对象"}</a>` : `<span title="来源未提供合法定位">定位不足，无法跳转</span>`}${projectAction}</div></footer>
  </article>`;
}

async function submitMaterialSearch(event) {
  event.preventDefault();
  if (state.materialSearchBusy) return;
  state.materialSearchBusy = true;
  const form = new FormData(event.currentTarget);
  const countryValue = String(form.get("country_iso3") || "").trim();
  const countryIso3 = resolveCountryIso3Input(countryValue);
  if (countryIso3 === null) {
    state.materialSearchBusy = false;
    showToast("请输入准确的国家名、英文名或 ISO3，并从建议中选择");
    document.getElementById("materialSearchCountry")?.focus();
    return;
  }
  if (countryIso3) document.getElementById("materialSearchCountry").value = countryIso3;
  const params = new URLSearchParams();
  for (const [key, rawValue] of form.entries()) {
    const value = String(rawValue).trim();
    if (key === "country_iso3") {
      if (countryIso3) params.set(key, countryIso3);
    } else if (value) params.set(key, value);
  }
  params.set("object_type", state.materialSearchType || "all");
  params.set("limit", state.materialSearchType === "country" ? "195" : "100");
  replaceRoute(unifiedSearchRoute(params));
  state.routeQuery = new URLSearchParams(params);
  const root = document.getElementById("materialSearchResults");
  const summary = document.getElementById("materialSearchSummary");
  const exportButton = document.getElementById("materialSearchExport");
  const referencesButton = document.getElementById("materialSearchReferences");
  root.innerHTML = detailSkeleton("正在联合检索国家、事件、材料与结构化数据…");
  summary.textContent = "正在检索；受限内容不会把摘要或定位写入页面。";
  try {
    const payload = await apiFetch(`/reader/search?${params}`);
    state.materialSearchResults = payload.items || [];
    state.materialSearchFacets = payload.facets || {};
    summary.textContent = `找到 ${Number(payload.total || 0)} 个可检索对象，本页 ${state.materialSearchResults.length} 个；受限正文不进入结果。`;
    root.innerHTML = state.materialSearchResults.length ? state.materialSearchResults.map((item) => unifiedSearchCardHtml(item)).join("") : emptyState("没有匹配的对象。请缩短关键词或放宽筛选条件。");
    exportButton.disabled = !state.materialSearchResults.length;
    referencesButton.disabled = !state.materialSearchResults.length;
  } catch (error) {
    state.materialSearchResults = [];
    root.innerHTML = errorState(error.message);
    summary.textContent = "检索失败；未返回不完整结果。";
    exportButton.disabled = true;
    referencesButton.disabled = true;
  } finally { state.materialSearchBusy = false; }
}

function downloadSearchFile(content, filename, type) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([content], { type }));
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}

function exportMaterialSearchMetadata() {
  if (!state.materialSearchResults.length) return;
  const columns = ["object_type", "object_key", "title", "country", "source_name", "language", "published_at", "occurred_at", "recorded_at", "updated_at", "version", "evidence_status", "access_state", "rights_status", "locator"];
  const quote = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const lines = [columns.join(","), ...state.materialSearchResults.filter((item) => item.actions?.can_export_metadata).map((item) => [item.object_type, item.object_key, item.title, (item.country || []).map((country) => country.iso3).join("|"), item.source?.name, item.language, item.published_at, item.occurred_at, item.recorded_at, item.updated_at, item.version, item.evidence_status, item.access_state, item.rights?.status, item.locator?.canonical_url || item.locator?.route].map(quote).join(","))];
  downloadSearchFile(`\uFEFF${lines.join("\n")}`, `国别智枢_统一检索_${new Date().toISOString().slice(0, 10)}.csv`, "text/csv;charset=utf-8");
}

function exportMaterialSearchReferences() {
  const references = state.materialSearchResults.filter((item) => item.actions?.can_cite).map((item) => generateCitationData(unifiedCitationItem(item)).ris);
  if (!references.length) return;
  downloadSearchFile(references.join("\n"), `国别智枢_参考文献_${new Date().toISOString().slice(0, 10)}.ris`, "application/x-research-info-systems");
}

function renderCountryProvenanceBar() {
  const root = document.getElementById("countryProvenanceBar");
  if (!root || !state.country) return;
  const country = state.country;
  const summary = country.summary || {};
  const datasets = country.datasets || [];
  const datasetNames = datasets.map((d) => d.name || d.dataset_key).slice(0, 3).join("、") || "World Bank、UN Comtrade、UNCTAD";
  const updatedDate = localDate(country.updated_at);

  root.innerHTML = `
    <article class="provenance-card is-highlight">
      <span>${iconSvg("check-circle")} 真实性与合规保证</span>
      <strong>白名单安全采录 · 证据边界明确</strong>
      <small>所有外部请求均经过安全获取层与公网地址复核；缺失指标、事实和更新时间不得自动补写。</small>
    </article>
    <article class="provenance-card">
      <span>${iconSvg("clock")} 数据时效与采录时间</span>
      <strong>最新采录：${escapeHtml(updatedDate)}</strong>
      <small>时序数据严格区分历史统计期与入库采集时间，来源未提供日期时不以采集时间冒充。</small>
    </article>
    <article class="provenance-card">
      <span>${iconSvg("chart")} 结构化数据原子快照</span>
      <strong>${Number(summary.structured_observations || 0).toLocaleString("zh-CN")} 条观测入库</strong>
      <small>覆盖 ${escapeHtml(datasetNames)}${datasets.length > 3 ? " 等" : ""}；按快照 Hash 不可变归档。</small>
    </article>
    <article class="provenance-card">
      <span>${iconSvg("landmark")} 官方信源与确认材料</span>
      <strong>${Number(summary.materials || 0)} 份材料 · ${Number(summary.events || 0)} 个已核验事件</strong>
      <small>官方公报、多源报道与学术文献统一版本管理；缺失值与数值 0 严格分离。</small>
    </article>
  `;
}

function templateContractHtml(country) {
  const profile = country.country_template || {};
  const fixedModules = (country.modules || []).filter((item) => item.kind !== "extension");
  const topics = profile.extension_topics || [];
  return `<div class="template-preview-heading"><div><p class="eyebrow">COUNTRY TEMPLATE CONTRACT</p><h2>固定国别模板 + 国家专题延伸</h2><p>${escapeHtml(profile.profile_description || "展示统一研究框架和国家研究重点。")}</p></div><span class="status-chip">模板配置 · 非事实结论</span></div><div class="template-contract-grid"><article><b>${fixedModules.length} 项固定模块</b><p>${fixedModules.map((item) => escapeHtml(item.label)).join(" · ")}</p></article><article><b>${topics.length} 项国家专题</b><p>${topics.length ? topics.map((item) => escapeHtml(item.label)).join(" · ") : "尚未配置；不影响基本国情、地图、指标、趋势、来源和 AI 页面展示。"}</p></article></div>`;
}

function renderCountryBasicFacts() {
  const country = state.country;
  const source = country.basic_facts_source || {};
  const facts = [
    ...(country.country_template?.basic_fact_slots || []).map((item) => [
      item.label,
      country.basic_facts?.[item.key] || "资料异常",
    ]),
    ["区域", country.subregion_zh || country.region || "暂无"],
    ["英文国名", country.name_en || "暂无"],
    ["联合国身份", membershipLabel(country.membership)],
    ["代码", `${country.iso2 || "—"} / ${country.iso3} · M49 ${country.m49 || "—"}`],
  ];
  const sourceUrl = safeUrl(source.snapshot_url);
  const sourceLabel = `${source.name || "基础国情注册表"} · 固定版本 ${String(source.revision || "").slice(0, 8) || "已登记"} · ${source.license || "许可已登记"}`;
  document.getElementById("countryBasicFacts").innerHTML = `${facts.map(([label, value]) => `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`).join("")}<article class="basic-facts-source"><span>基础档案来源</span>${sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(sourceLabel)} ↗</a>` : `<strong>${escapeHtml(sourceLabel)}</strong>`}</article>`;
}

function renderCountryProfileCompleteness() {
  const profile = state.country.profile_completeness || {};
  const dimensions = profile.dimensions || [];
  const score = document.getElementById("countryProfileScore");
  const root = document.getElementById("countryProfileDimensions");
  if (score) score.textContent = `${Number(profile.percent || 0)}%`;
  if (!root) return;
  root.innerHTML = dimensions.length ? dimensions.map((item) => `<article class="profile-dimension is-${escapeHtml(item.status || "gap")}"><header><strong>${escapeHtml(item.label)}</strong><span>${Number(item.filled || 0)} / ${Number(item.total || 0)}</span></header><div><i style="width:${Math.max(0, Math.min(100, Number(item.percent || 0)))}%"></i></div><small>${item.missing?.length ? `缺少：${escapeHtml(item.missing.join("、"))}` : "登记槽位已覆盖"}</small></article>`).join("") : emptyState("六维画像槽位尚未登记。");
}

function renderCountryRecentChanges() {
  const root = document.getElementById("countryRecentChanges");
  if (!root) return;
  const rows = state.country.recent_changes || [];
  root.innerHTML = rows.length ? rows.slice(0, 8).map((item) => `<article><span>${escapeHtml(unifiedObjectLabel(item.object_type))}</span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(localDate(item.changed_at))} · ${escapeHtml(item.evidence_status || "待核验")}</small></article>`).join("") : emptyState("近90天没有已登记的证据变化。");
}

function renderCountrySourceMix() {
  const root = document.getElementById("countrySourceMix");
  if (!root) return;
  const rows = state.country.source_mix || [];
  const total = rows.reduce((sum, item) => sum + Number(item.material_count || 0), 0);
  root.innerHTML = rows.length ? rows.map((item) => `<article><header><strong>${escapeHtml(item.source_type || "unknown")}</strong><span>${Number(item.material_count || 0)} 份</span></header><div><i style="width:${total ? Math.round(Number(item.material_count || 0) * 100 / total) : 0}%"></i></div></article>`).join("") : emptyState("确认材料尚未形成来源结构。");
}

function renderCountryConflictSummary() {
  const root = document.getElementById("countryConflictSummary");
  if (!root) return;
  const groups = state.country.conflict_summary || [];
  root.innerHTML = groups.length ? groups.map((group) => `<article class="conflict-claim-group"><header><div><span>${escapeHtml(group.comparison_label || group.comparison_key)}</span><strong>${escapeHtml(group.event_title)}</strong></div><a href="#/countries/${state.country.iso3}/events/${group.event_id}">核对事件</a></header><div>${(group.values || []).map((claim) => `<p><b>${escapeHtml(claim.claimant)}</b><span>${escapeHtml(claim.value || (claim.numeric_value != null ? `${claim.numeric_value} ${claim.unit || ""}` : "值未登记"))}</span></p>`).join("")}</div><small>${escapeHtml(group.method_note || "冲突主张并列展示，不自动裁决。")}</small></article>`).join("") : emptyState("当前没有达到并列展示条件的冲突主张。");
}

function researchMaterialCardHtml(item) {
  const metadata = item.metadata || {};
  const rights = item.rights || {};
  const sourceUrl = safeUrl(item.locator?.canonical_url || item.locator?.discovery_url);
  return `<article class="research-material-card" ${projectMaterialAttributes(item)}><header><div><span>${escapeHtml(item.language || "语言待核验")} · ${escapeHtml(item.evidence_status || "confirmed")}</span><h3>${escapeHtml(item.title)}</h3></div><span>${escapeHtml(item.document_type || "论文")}</span></header><p>${escapeHtml(item.abstract || (rights.status === "reviewed" ? "摘要未登记。" : "权利待审，摘要不展示。"))}</p><dl><div><dt>研究对象</dt><dd>${escapeHtml(metadata.research_object || "待核验")}</dd></div><div><dt>方法</dt><dd>${escapeHtml(metadata.research_method || metadata.method || "待核验")}</dd></div><div><dt>样本范围</dt><dd>${escapeHtml(metadata.sample_scope || "待核验")}</dd></div><div><dt>数据来源</dt><dd>${escapeHtml(metadata.data_sources || item.source_name || "待核验")}</dd></div><div><dt>全文 / 译文</dt><dd>${escapeHtml(metadata.full_text_status || "未登记")} / ${escapeHtml(metadata.translation_status || "未登记")}</dd></div><div><dt>版本与定位</dt><dd>v${Number(item.version_no || 1)} · ${sourceUrl ? "可回源" : "定位待核验"}</dd></div></dl><footer><span>${escapeHtml(item.source_name || "来源未登记")} · ${escapeHtml(localDate(item.published_at))}</span><div>${sourceUrl ? `<a class="text-link" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">查看论文 ↗</a>` : ""}<button type="button" class="text-link" data-material-version="${Number(item.document_version_id)}">查看版本与来源</button></div></footer></article>`;
}

function renderCountryResearchChannel() {
  if (!state.country) return;
  const key=`${state.country.iso3}:frontier_research`;
  const rows=state.countryChannelMaterials[key] || [];
  const page=state.countryChannelPages?.[key];
  const view=countryReadingState();
  const sources=page?.facets?.sources || [];
  state.researchSourceFacets ||= {};
  if(!view.researchSource && !view.researchQuery) state.researchSourceFacets[state.country.iso3]=sources;
  const roster=state.researchSourceFacets[state.country.iso3] || sources;
  const researchSource=document.getElementById("countryResearchSource");
  if(researchSource) {
    researchSource.innerHTML='<option value="">全部来源</option>'+roster.map(item=>`<option value="${item.source_id}" ${String(item.source_id)===String(view.researchSource || "") ? "selected":""}>${escapeHtml(item.source_name)}${item.count!=null ? ` · ${item.count}`:""}</option>`).join("");
  }
  const researchQuery=document.getElementById("countryResearchQuery");
  if(researchQuery && researchQuery.value!==(view.researchQuery || "")) researchQuery.value=view.researchQuery || "";
  document.getElementById("countryResearchMaterials").innerHTML=rows.length ? rows.map(item=>countryArticleHtml(item,"research","",(item.metadata?.topics || []).join(" · "))).join("") : emptyState("当前时间窗口尚无已收录论文。");
  if(page && rows.length<page.total) document.getElementById("countryResearchMaterials").insertAdjacentHTML("beforeend",'<button class="list-toggle" data-publication-more="frontier_research">加载更多论文</button>');
  renderResearchOverview();
}

function renderCountryTopics() {
  const profile = state.country.country_template || {};
  const topics = profile.extension_topics || [];
  const title = state.country.modules?.find((item) => item.key === "minerals")?.label || "国家专题延伸";
  document.getElementById("countryExtensionTitle").textContent = title;
  document.getElementById("countryExtensionRule").textContent = profile.extension_rule?.definition || "国家专题只用于跨模块的国家特有研究问题。";
  document.getElementById("countryTopicContent").innerHTML = topics.length
    ? topics.map((item) => `<article><div class="topic-card-heading"><span>国家议题 · 跨模块研究命题</span><h3>${escapeHtml(item.label)}</h3></div><div class="topic-card-detail"><p>${escapeHtml(item.description)}</p><div class="topic-connections">${(item.connected_modules || []).map((module) => `<em>${escapeHtml(module)}</em>`).join("")}</div>${item.boundary ? `<p class="topic-boundary"><b>与固定板块的边界：</b>${escapeHtml(item.boundary)}</p>` : ""}</div><div class="inline-actions"><button type="button" class="text-link" data-qa-question="${escapeHtml(item.question)}">让 AI 检查证据缺口</button><a class="text-link" href="#/projects">在项目空间继续研究</a></div></article>`).join("")
    : emptyState("当前国家尚未配置独有专题；九项通用模块已可使用，后续可按产业、资源、社会或区域议题扩展。 ");
}

function renderStrategicSignals() {
  const indicators = (state.country.key_indicators || []).filter((item) => item.value !== null).slice(0,4);
  const signals = indicators.map((item) => ({ title: `${item.label} · ${item.period || "时期未明"}`, text: `${formatNumber(item.value,item.unit)}；来源 ${item.source_name}，采集 ${localDate(item.updated_at || state.country.updated_at)}。` }));
  if (!signals.length && state.country.summary?.structured_observations) signals.push({ title: "已有结构化数据覆盖", text: `当前数据库包含 ${Number(state.country.summary.structured_observations).toLocaleString("zh-CN")} 条观测，但尚未配置统一指标卡。` });
  document.getElementById("strategicSignals").innerHTML = signals.length ? `<div class="signal-list">${signals.map((item) => `<article><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.text)}</p></article>`).join("")}</div>` : emptyState("暂无可证实的研究要点");
}

function renderKpis() {
  const items = state.country.key_indicators || [];
  const itemByKey = new Map(items.map((item) => [item.key, item]));
  const slots = state.country.country_template?.indicator_slots || [];
  const cards = slots.length
    ? slots.map((slot) => itemByKey.get(slot.key) || { ...slot, value: null, period: null, unit: null, source_name: slot.source_hint, missing_reason: "not_connected" })
    : items;
  document.getElementById("indicatorFreshness").textContent = `指标最近采集 ${localDate(items.map((item) => item.updated_at).filter(Boolean).sort().at(-1) || state.country.updated_at)}`;

  document.getElementById("countryKpis").innerHTML = cards.length
    ? cards.map((item) => {
        const latest = item.latest_period || item.period;
        const sourceMeta = item.source_metadata || {};
        const sourceDate = sourceMeta.retrieved_at || item.updated_at || state.country.updated_at;
        const sourceLink = safeUrl(item.source_url)
          ? `<a href="${escapeHtml(safeUrl(item.source_url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.source_name)} ↗</a>`
          : `<span>${escapeHtml(item.value === null ? "数据待接入" : item.source_name || "官方数据")}</span>`;

        return `<article class="kpi-card${item.value === null ? " is-empty" : ""}">
          <div class="kpi-head">
            <span class="kpi-label">${escapeHtml(item.label)}</span>
            <span class="kpi-period">${latest ? `截至 ${escapeHtml(latest)}` : "待接入"}</span>
          </div>
          <div class="kpi-value-row">
            <strong class="kpi-value">${item.value === null ? "暂无数据" : escapeHtml(formatNumber(item.value, item.unit))}</strong>
          </div>
          <div class="kpi-foot">
            <span class="kpi-source-text">信源：${sourceLink}</span>
            <span class="kpi-date">${localDate(sourceDate)}</span>
          </div>
        </article>`;
      }).join("")
    : emptyState("该国家尚未配置统一指标卡");
}

function renderTrends() {
  const series = (state.country.key_indicators || []).filter((item) => (item.series || []).filter((point) => point.value !== null).length >= 2);
  const slots = (state.country.country_template?.indicator_slots || []).slice(0, 6);
  const selected = series.find((item) => item.key === state.selectedTrendKey) || series[0];
  if (!series.length) {
    document.getElementById("trendContent").innerHTML = slots.map((item) => `<article class="trend-item is-empty"><header><strong>${escapeHtml(item.label)}</strong><span>十年趋势 · 数据待接入</span></header><div class="trend-placeholder" aria-label="${escapeHtml(item.label)}趋势数据待接入"><i></i><i></i><i></i><i></i><i></i></div><p>接入按年份、来源版本和缺失原因记录的数据后自动生成。</p></article>`).join("");
    return;
  }
  if (state.selectedTrendKey !== selected.key) {
    state.selectedTrendKey = selected.key;
    state.selectedTrendPoint = null;
  }
  document.getElementById("trendContent").innerHTML = `<div class="trend-workspace"><div class="trend-switcher" role="tablist" aria-label="选择趋势指标">${series.map((item) => `<button type="button" role="tab" aria-selected="${item.key === selected.key}" class="${item.key === selected.key ? "is-active" : ""}" data-trend-key="${escapeHtml(item.key)}"><span>${escapeHtml(item.label)}</span><small>${escapeHtml(item.series.filter((point) => point.value !== null).at(-1)?.period || "—")}</small></button>`).join("")}</div>${trendChartHtml(selected)}</div>`;
}

function formatTrendAxis(value) {
  const absolute = Math.abs(value);
  if (absolute >= 1000) return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: absolute < 10 ? 2 : 1 });
}

function trendUnitLabel(unit) {
  return ({ person: "人", USD: "美元", percent: "%", percent_of_GDP: "%（占 GDP）", million_USD: "百万美元" })[unit] || unit || "数值";
}

function populationTableHtml(item, valid, selectedIndex) {
  const points = valid || [];
  return `<section class="population-data-table" aria-label="${escapeHtml(item.label)}年度数据表"><header><strong>${escapeHtml(item.label)} · 历年观测表</strong><span>${escapeHtml(item.source_name || "官方数据")}</span></header><div class="population-table-scroll"><table><thead><tr><th>年份</th><th>观测值</th><th>年增量</th><th>同比</th></tr></thead><tbody>${points.map((point, i) => { const prev = i > 0 ? points[i - 1] : null; const diff = prev ? point.numericValue - prev.numericValue : null; const rate = prev && prev.numericValue ? (diff / Math.abs(prev.numericValue)) * 100 : null; const isSel = point.index === selectedIndex; return `<tr class="${isSel ? "is-selected" : ""}" tabindex="0" role="button" data-trend-point="${point.index}" aria-label="选择 ${escapeHtml(point.period)} 年观测"><td>${escapeHtml(point.period)}</td><td><b>${escapeHtml(formatNumber(point.numericValue, item.unit))}</b></td><td>${diff === null ? "—" : `${diff > 0 ? "+" : ""}${escapeHtml(formatNumber(diff, item.unit))}`}</td><td>${rate === null ? "—" : `<span class="${rate >= 0 ? "trend-up" : "trend-down"}">${rate > 0 ? "+" : ""}${rate.toFixed(1)}%</span>`}</td></tr>`; }).join("")}</tbody></table></div></section>`;
}

function trendChartHtml(item) {
  const points = item.series || [];
  const valid = points.map((point, index) => ({ ...point, index, numericValue: Number(point.value) })).filter((point) => point.value !== null && Number.isFinite(point.numericValue));
  const values = valid.map((point) => point.numericValue);
  let minimum = Number(item.axes?.y?.domain?.[0]); let maximum = Number(item.axes?.y?.domain?.[1]);
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) {
    minimum = Math.min(...values); maximum = Math.max(...values);
    const rawSpan = maximum - minimum;
    const padding = rawSpan ? rawSpan * .1 : Math.max(Math.abs(maximum) * .1, 1);
    minimum -= padding; maximum += padding;
  }
  const chart = { left: 86, right: 870, top: 24, bottom: 278 };
  const x = (index) => chart.left + (points.length <= 1 ? (chart.right - chart.left) / 2 : (index / (points.length - 1)) * (chart.right - chart.left));
  const y = (value) => chart.bottom - ((value - minimum) / (maximum - minimum || 1)) * (chart.bottom - chart.top);
  const segments = []; let segment = [];
  points.forEach((point, index) => {
    const numericValue = Number(point.value);
    if (point.value === null || !Number.isFinite(numericValue)) { if (segment.length) segments.push(segment); segment = []; return; }
    segment.push({ index, numericValue });
  });
  if (segment.length) segments.push(segment);
  const requestedPoint = state.selectedTrendPoint === null ? Number.NaN : Number(state.selectedTrendPoint);
  const selectedIndex = valid.some((point) => point.index === requestedPoint) ? requestedPoint : valid.at(-1).index;
  state.selectedTrendPoint = selectedIndex;
  const selected = valid.find((point) => point.index === selectedIndex) || valid.at(-1);
  const previous = [...valid].reverse().find((point) => point.index < selected.index);
  const change = previous ? selected.numericValue - previous.numericValue : null;
  const yTicks = Array.from({ length: 5 }, (_, index) => maximum - ((maximum - minimum) * index) / 4);
  const xTickIndexes = points.map((_, index) => index).filter((index) => points.length <= 10 || index === 0 || index === points.length - 1 || index % 2 === 0);
  const unit = selected.unit || item.unit;
  const sourceUrl = safeUrl(item.source_url);
  const missingCount = points.length - valid.length;
  const isPopulation = item.key === "population";
  const sourceMeta = item.source_metadata || {};
  const representationNote = isPopulation
    ? `年度离散观测 · 非拟合/预测 · 截至 ${valid.at(-1)?.period || "实际最新期"}`
    : (item.series_analysis?.representation_note || "各点是来源返回的离散年度观测值；线段只连接相邻有效观测，不是拟合函数或预测。");
  const populationTable = populationTableHtml(item, valid, selectedIndex);
  return `<article class="trend-chart-card is-population"><header><div><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(valid[0]?.period)}—${escapeHtml(valid.at(-1)?.period)} · ${escapeHtml(item.source_name)}</span></div><em>纵轴：${escapeHtml(trendUnitLabel(unit))} · 区间自适应${item.axes?.y?.non_zero_origin ? " · 非零起点" : ""}</em></header><div class="trend-chart-viewport"><svg class="trend-line-chart" viewBox="0 0 900 330" role="img" aria-label="${escapeHtml(item.label)}，${escapeHtml(valid[0]?.period)}至${escapeHtml(valid.at(-1)?.period)}年度观测图">${yTicks.map((tick) => `<g class="trend-y-tick"><line x1="${chart.left}" x2="${chart.right}" y1="${y(tick)}" y2="${y(tick)}"></line><text x="${chart.left - 12}" y="${y(tick) + 4}" text-anchor="end">${escapeHtml(formatTrendAxis(tick))}</text></g>`).join("")}<line class="trend-axis" x1="${chart.left}" x2="${chart.left}" y1="${chart.top}" y2="${chart.bottom}"></line><line class="trend-axis" x1="${chart.left}" x2="${chart.right}" y1="${chart.bottom}" y2="${chart.bottom}"></line>${segments.map((row) => `<path class="trend-line" d="${row.map((point, index) => `${index ? "L" : "M"}${x(point.index).toFixed(2)},${y(point.numericValue).toFixed(2)}`).join(" ")}"></path>`).join("")}${xTickIndexes.map((index) => `<g class="trend-x-tick"><line x1="${x(index)}" x2="${x(index)}" y1="${chart.bottom}" y2="${chart.bottom + 6}"></line><text x="${x(index)}" y="${chart.bottom + 23}" text-anchor="middle">${escapeHtml(points[index].period)}</text></g>`).join("")}${valid.map((point) => `<g class="trend-point${point.index === selected.index ? " is-selected" : ""}" role="button" tabindex="0" data-trend-point="${point.index}" aria-label="${escapeHtml(`${point.period}，${formatNumber(point.numericValue, point.unit || unit)}`)}"><title>${escapeHtml(`${point.period}：${formatNumber(point.numericValue, point.unit || unit)}`)}</title><circle class="trend-point-hit" cx="${x(point.index)}" cy="${y(point.numericValue)}" r="16"></circle><circle class="trend-point-dot" cx="${x(point.index)}" cy="${y(point.numericValue)}" r="${point.index === selected.index ? 6 : 4.5}"></circle></g>`).join("")}</svg></div><p class="trend-observation-note">${escapeHtml(representationNote)}${!isPopulation && item.axes?.y?.non_zero_origin ? " · 纵轴为非零起点" : ""}</p><div class="trend-point-detail" aria-live="polite"><div><span>当前观测点</span><strong>${escapeHtml(selected.period)}</strong></div><div><span>精确值</span><strong>${escapeHtml(formatNumber(selected.numericValue, unit))}</strong></div><div><span>较上一有效期</span><strong>${change === null ? "首个有效值" : `${change > 0 ? "+" : ""}${escapeHtml(formatNumber(change, unit))}`}</strong></div><div><span>数据完整性</span><strong>${missingCount ? `${missingCount} 期缺失，线段已断开` : `${valid.length} 期均有值`}</strong></div></div>${populationTable}<footer><span>发布 ${escapeHtml(sourceMeta.publication_at ? localDate(sourceMeta.publication_at) : "来源未声明")} · 采集 ${escapeHtml(localDate(sourceMeta.retrieved_at || item.updated_at || state.country.updated_at))}</span>${sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.source_name)} ↗</a>` : `<span>${escapeHtml(item.source_name)}</span>`}</footer></article>`;
}

function renderTrade() {
  const datasets = (state.country.datasets || []).filter((item) => item.dataset_key === "un-comtrade-goods-annual-hs");
  document.getElementById("tradeContent").innerHTML = datasets.length ? `<div class="ledger">${datasets.map((item) => `<article><header><strong>${escapeHtml(item.name)}</strong><span class="precision">部分接入</span></header><p>${Number(item.observations).toLocaleString("zh-CN")} 条结构化观测已入库。当前页只声明覆盖，不把记录数量冒充贸易结论；具体流向、矿种与数值须由受控趋势能力生成。</p></article>`).join("")}</div>` : emptyState("暂无达到展示门槛的关键矿产贸易结果");
}

function materialList(items, className = "", options = {}) {
  return `<div class="item-list${className ? ` ${className}` : ""}">${items.map((item) => materialCard(item, options)).join("")}</div>`;
}

function renderPolicyLifecycle() {
  const stages = ["草案", "征求意见", "审议", "发布", "生效", "修订", "废止", "执行细则"];
  const items = state.country.policy_items || [];
  const counts = new Map(stages.map((stage) => [stage, 0]));
  items.forEach((item) => {
    const stage = item.metadata?.lifecycle_status;
    if (counts.has(stage)) counts.set(stage, counts.get(stage) + 1);
  });
  const lifecycle = document.getElementById("policyLifecycle");
  if (lifecycle) { lifecycle.hidden = ![...counts.values()].some(Boolean); lifecycle.innerHTML = stages.filter(stage => counts.get(stage)).map((stage) => `<article class="${counts.get(stage) ? "has-evidence" : ""}"><span>${escapeHtml(stage)}</span><strong>${counts.get(stage) || 0}</strong><small>${counts.get(stage) ? "已登记材料" : "待核验"}</small></article>`).join(""); }
  const impact = document.getElementById("policyImpactContent");
  if (impact) impact.innerHTML = `<div class="policy-evidence-columns"><article><strong>政策文本</strong><span>${items.length} 份已确认材料</span></article><article><strong>执行案例</strong><span>未单独登记则为未知</span></article><article><strong>效果证据</strong><span>不由法条或材料数量推断</span></article></div><p class="method-note">关联事件、指标变化和研究讨论必须保留独立来源；当前只展示已有证据关系。</p>`;
}

async function loadCountryDataCatalog(iso3) {
  const root = document.getElementById("countryDataCatalog");
  if (root) root.innerHTML = detailSkeleton("正在读取数据目录…");
  try {
    const catalog = await apiFetch(`/reader/countries/${encodeURIComponent(iso3)}/data-catalog`);
    if (state.country?.iso3 !== iso3) return;
    if (state.bootstrap?.reader_contract_version !== 2 || !Array.isArray(catalog.categories) || !Array.isArray(catalog.source_comparisons) || (catalog.datasets || []).some(dataset=>!["view","download","citation"].every(action=>typeof dataset.permissions?.[action]?.allowed === "boolean"))) {
      state.countryDataCatalog=null;
      document.getElementById("countryDataIndex").innerHTML="";
      document.getElementById("countryDataComparison").innerHTML=errorState("页面与服务版本不匹配，请重新打开本机启动入口。下载和引用已停用。");
      document.getElementById("countryDataRecords").innerHTML="";
      root.innerHTML="";
      return;
    }
    state.countryDataCatalog = catalog;
    const keys = state.countryDataCatalog.datasets?.flatMap((dataset) => (dataset.indicators || []).map((indicator) => `${dataset.id}:${indicator.key}`)) || [];
    if (!keys.includes(state.selectedDataIndicatorKey)) state.selectedDataIndicatorKey = keys[0] || null;
    renderCountryDataCatalog();
  } catch (error) {
    if (state.country?.iso3 !== iso3) return;
    state.countryDataCatalog = null;
    if (root) root.innerHTML = errorState(error.message);
    const records = document.getElementById("countryDataRecords");
    if (records) records.innerHTML = errorState(error.message);
  }
}

function selectedCountryDataIndicator() {
  const key = state.selectedDataIndicatorKey;
  for (const dataset of state.countryDataCatalog?.datasets || []) {
    const indicator = (dataset.indicators || []).find((item) => `${dataset.id}:${item.key}` === key);
    if (indicator) return { dataset, indicator };
  }
  return null;
}

function countryDataQuery(selected) {
  const { dataset, indicator } = selected;
  const params = new URLSearchParams({ snapshot_id: dataset.snapshot_id, country_iso3: state.country.iso3, series_key: indicator.key });
  Object.entries(indicator.source_field || {}).forEach(([key,value]) => { if (value !== null && value !== undefined && !["frequency","country_iso3"].includes(key)) params.set(key,value); });
  return params;
}

function readingDataChart(indicator) {
  const points = [...indicator.records].sort((a,b)=>a.period-b.period);
  const valid = points.filter(row=>row.value !== null && Number.isFinite(Number(row.value)));
  if (valid.length < 2 || indicator.units.length !== 1 || new Set(points.map(row=>row.period)).size !== points.length) return "";
  const low = Math.min(...valid.map(row=>Number(row.value))), high = Math.max(...valid.map(row=>Number(row.value)));
  const span = high-low || 1;
  const x = index=>72+index/Math.max(1,points.length-1)*590, y = value=>190-(Number(value)-low)/span*145;
  let path="", connected=false;
  points.forEach((row,index)=> { if (row.value === null) { connected=false; return; } path+=`${connected ? "L":"M"}${x(index)},${y(row.value)} `; connected=true; });
  return `<svg class="reading-data-chart" viewBox="0 0 720 235" role="img" aria-label="${escapeHtml(indicator.label)}，${indicator.period_from}至${indicator.period_to}年实际观测"><line x1="72" y1="190" x2="662" y2="190" stroke="#dce6eb"/><text x="4" y="48">${escapeHtml(formatNumber(high))}</text><text x="4" y="190">${escapeHtml(formatNumber(low))}</text><path d="${path}" fill="none" stroke="#269ba4" stroke-width="2.5"/>${points.map((row,index)=>row.value === null ? "" : `<circle cx="${x(index)}" cy="${y(row.value)}" r="4" fill="#269ba4"><title>${row.period}：${escapeHtml(row.value)} ${escapeHtml(row.unit)}</title></circle>`).join("")}<text x="72" y="218">${points[0].period}</text><text x="632" y="218">${points.at(-1).period}</text></svg><p class="reading-note">原始年度观测 · 缺失处断开 · 纵轴随数值范围变化，非零起点 · 单位：${escapeHtml(indicator.units[0])}</p>`;
}

function renderCountryDataCatalog() {
  const catalog = state.countryDataCatalog;
  if (!catalog || catalog.country_iso3 !== state.country.iso3) return;
  const view = countryReadingState();
  const categories = catalog.categories || [];
  view.category ||= "全部";
  const query = document.getElementById("countryDataCatalogSearch").value.trim().toLowerCase();
  const updated=catalog.datasets.map(dataset=>dataset.updated_at).filter(Boolean).sort().at(-1);
  if(updated) document.getElementById("indicatorFreshness").textContent=`指标最近采集 ${localDate(updated)}`;
  const allRows = catalog.datasets.flatMap(dataset=>dataset.indicators.map(indicator=>({dataset,indicator})));
  const missingOnly=allRows.filter(({indicator})=>!indicator.records.some(row=>row.value!==null));
  const rows=view.dataIncludeMissing ? allRows:allRows.filter(row=>!missingOnly.includes(row));
  const categoryRows = rows.filter(({dataset,indicator})=>(view.category === "全部" || indicator.theme === view.category) && (!query || `${indicator.label} ${indicator.key} ${dataset.name} ${dataset.source.name}`.toLowerCase().includes(query)));
  const catalogRoot=document.getElementById("countryDataCatalog");
  if(catalogRoot) catalogRoot.innerHTML="";
  const categorySelect=document.getElementById("countryDataCategoryFilter");
  if(categorySelect) {
    categorySelect.innerHTML=[{name:"全部"},...categories].map(item=>`<option value="${escapeHtml(item.name)}" ${item.name===view.category ? "selected":""}>${item.name==="全部" ? "全部目录":escapeHtml(item.name)} · ${rows.filter(({indicator})=>item.name==="全部" || indicator.theme===item.name).length}</option>`).join("");
  }
  const providers = [...new Set(categoryRows.map(row=>row.dataset.source.name))];
  if (!providers.includes(view.dataSource)) view.dataSource="";
  const sourceSelect=document.getElementById("countryDataSourceFilter");
  if(sourceSelect) {
    sourceSelect.innerHTML='<option value="">全部来源</option>'+providers.map(name=>`<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
    sourceSelect.value=view.dataSource;
  }
  const latestPeriod=({indicator})=>indicator.records.find(row=>row.value!==null)?.period || indicator.period_to;
  const filtered=categoryRows.filter(row=>!view.dataSource || row.dataset.source.name===view.dataSource).sort((a,b)=>String(latestPeriod(b)).localeCompare(String(latestPeriod(a))));
  if (!selectedCountryDataIndicator()) state.selectedDataIndicatorKey=filtered.length ? `${filtered[0].dataset.id}:${filtered[0].indicator.key}` : null;
  const indicatorSelect=document.getElementById("countryDataIndicatorFilter");
  if(indicatorSelect) {
    const selectedKey=state.selectedDataIndicatorKey || "";
    indicatorSelect.innerHTML=`<option value="">全部指标 · ${filtered.length}</option>`+filtered.map(({dataset,indicator})=>{
      const key=`${dataset.id}:${indicator.key}`;
      return `<option value="${escapeHtml(key)}" ${key===selectedKey ? "selected":""}>${escapeHtml(indicator.label)}</option>`;
    }).join("");
  }
  document.getElementById("countryDataIndex").innerHTML = `<div class="data-directory-summary"><p class="reading-note">${filtered.length} 项来源指标 · 点击查看定义、来源与观测记录</p>${missingOnly.length ? `<label><input type="checkbox" id="countryDataIncludeMissing" ${view.dataIncludeMissing ? "checked":""}>显示仅有缺测记录的指标（${missingOnly.length}）</label>`:""}</div>${filtered.length ? `<div class="country-indicator-table-wrap"><table class="country-indicator-table"><thead><tr><th>指标</th><th>来源库</th><th>最近有值 / 单位</th><th>数值年份 / 记录范围</th></tr></thead><tbody>${filtered.map(({dataset,indicator})=>{
    const newest=indicator.records[0], latest=indicator.records.find(row=>row.value!==null) || newest, ref=latest && latest.value!==null && dataset.permissions.citation.allowed ? {type:"observation",id:latest.observation_version_id,snapshot_id:dataset.snapshot_id,title:indicator.label,source_name:dataset.source.name} : null;
    return `<tr><td>${ref ? countrySelectionHtml(ref) : ""}<button data-country-data-indicator="${escapeHtml(`${dataset.id}:${indicator.key}`)}">${escapeHtml(indicator.label)}</button><small>${escapeHtml(indicator.theme)}</small></td><td>${escapeHtml(dataset.source.name)}${dataset.name!==dataset.source.name ? `<small>${escapeHtml(dataset.name)}</small>`:""}</td><td>${latest ? (latest.value===null ? "来源未提供数值" : escapeHtml(formatNumber(latest.value,latest.unit))) : "未登记"}</td><td>${latest?.value!==null && latest ? latest.period : "—"}<small>${indicator.period_from}—${indicator.period_to}</small>${latest && newest && latest.period!==newest.period ? `<small>${newest.period} 年来源未提供</small>`:""}</td></tr>`;
  }).join("")}</tbody></table></div>` : emptyState("没有匹配指标，请调整分类、来源或关键词。")}`;
  syncCountrySelection();
}

function renderCountryDataIndicator() {
  const catalog=state.countryDataCatalog;
  const selected = selectedCountryDataIndicator();
  const comparison = document.getElementById("countryDataComparison"), records = document.getElementById("countryDataRecords");
  records.innerHTML = "";
  state.dataRecordRequest = (state.dataRecordRequest || 0)+1;
  if (!selected) { comparison.innerHTML=""; return; }
  const {dataset,indicator} = selected;
  const latest = indicator.records.find(row=>row.value !== null);
  const fields = indicator.source_field;
  const dimensions = [["指标代码",fields.indicator_code],["原始度量",fields.metric_code],["来源库代码",indicator.source_dataset_codes?.filter(Boolean).join("、")],["报告国",fields.country_iso3],["伙伴国",fields.partner_iso3],["商品编码",fields.commodity_code],["分类版本",fields.commodity_classification],["方向",fields.trade_flow],["第二伙伴",fields.partner2_code],["海关制度",fields.customs_code],["运输方式",fields.mot_code]].filter(([,value])=>value !== null && value !== undefined && value !== "");
  const peers = catalog.source_comparisons.filter(group=>group.sources.some(source=>source.dataset_id===dataset.id && source.indicator_key===indicator.key));
  const rights=dataset.permissions;
  const params=countryDataQuery(selected);
  comparison.innerHTML = `<article><header><span>${escapeHtml(indicator.theme)} / 指标详情</span><h3>${escapeHtml(indicator.label)}</h3></header><p><strong>${latest ? `${escapeHtml(formatNumber(latest.value,latest.unit))} · ${latest.period}` : "尚无有效观测值"}</strong></p><dl><div><dt>定义</dt><dd>${escapeHtml(indicator.definition)}${safeUrl(indicator.definition_url) ? ` <a href="${escapeHtml(indicator.definition_url)}" target="_blank" rel="noopener noreferrer">口径依据 ↗</a>` : ""}</dd></div><div><dt>单位 / 统计期</dt><dd>${indicator.units.map(escapeHtml).join(" / ")} · ${indicator.period_from}—${indicator.period_to}</dd></div><div><dt>地区层级</dt><dd>${indicator.geography_level === "country" ? "国家" : escapeHtml(indicator.geography_level)}</dd></div><div><dt>来源库 / 更新</dt><dd>${escapeHtml(dataset.name)}<br>来源发布：${escapeHtml(localDate(dataset.source_published_at || indicator.records.find(row=>row.source_release_date)?.source_release_date))}<br>平台采集：${escapeHtml(localDate(dataset.updated_at))} · 快照 ${dataset.snapshot_id}</dd></div><div><dt>来源原始字段</dt><dd>${Object.entries(indicator.original_fields || {}).map(([name,value])=>`${escapeHtml(name)} → ${escapeHtml(value)}`).join("<br>") || "映射未登记"}</dd></div><div><dt>标准化维度</dt><dd>${dimensions.map(([name,value])=>`${escapeHtml(name)}：${escapeHtml(value)}`).join("<br>")}</dd></div><div><dt>许可</dt><dd>${escapeHtml(dataset.license)}${safeUrl(dataset.license_url) ? ` <a href="${escapeHtml(dataset.license_url)}" target="_blank" rel="noopener noreferrer">许可原文 ↗</a>` : ""}<br>下载：${rights.download.allowed ? "可下载，保留署名" : "未获许可"}<br>引用：${rights.citation.allowed ? "可引用，注明来源" : "未获许可"}</dd></div></dl>${readingDataChart(indicator)}${peers.map(group=>`<section class="reading-table-wrap"><h4>多来源并列</h4><p>${escapeHtml(group.difference_reason)}</p><table><thead><tr><th>来源</th><th>定义</th><th>地区</th><th>统计期</th><th>值 / 单位</th></tr></thead><tbody>${group.sources.flatMap(source=>source.records.map(row=>`<tr><td>${escapeHtml(source.source_name)}</td><td>${escapeHtml(source.definition)}</td><td>${escapeHtml(row.country_iso3 || "未登记")} / ${escapeHtml(row.partner_iso3 || "—")}</td><td>${row.period}</td><td>${row.value === null ? "缺失" : escapeHtml(row.value)} ${escapeHtml(row.unit || "")}</td></tr>`)).join("")}</tbody></table></section>`).join("")}<div class="reading-tabs"><button data-show-data-records>查看来源观测记录（${indicator.records.length} 条）</button>${rights.download.allowed ? `<a class="ghost-btn link-button" href="${API}/structured-datasets/${dataset.id}/export?${escapeHtml(params.toString())}&format=csv">下载指标 CSV</a>` : `<button disabled title="${escapeHtml(rights.download.reason)}">下载尚未获准</button>`}<button data-cite-data ${rights.citation.allowed ? "":"disabled"}>生成引用</button>${latest && rights.citation.allowed ? `<button class="ghost-btn" data-country-add-object="${escapeHtml(JSON.stringify({type:"observation",id:latest.observation_version_id,snapshot_id:dataset.snapshot_id,title:indicator.label,source_name:dataset.source.name}))}">引用此观测</button>`:""}</div><p class="reading-note">来源观测经过规范化保存；原始文件仅在实际归档且取得权限后提供。多来源不自动合并、换算或排行。</p></article>`;
  document.getElementById("indicatorFreshness").textContent = `所选指标采集 ${localDate(dataset.updated_at)}`;
}

async function loadCountryDataRecords(offset=0) {
  const selected=selectedCountryDataIndicator(); if (!selected) return;
  const request=state.dataRecordRequest=(state.dataRecordRequest || 0)+1;
  const root=document.getElementById("countryDataRecords");
  root.innerHTML=detailSkeleton("正在读取固定快照的来源记录…");
  const params=countryDataQuery(selected); params.set("limit","20"); params.set("offset",String(offset));
  try {
    const page=await apiFetch(`/structured-datasets/${selected.dataset.id}/observations?${params}`);
    if(request!==state.dataRecordRequest) return;
    root.innerHTML=`<section class="normalized-records"><header><div><h3>来源观测记录</h3><p>快照 ${page.snapshot_id} · 规范化记录，不是原始 HTTP 响应</p></div><span>${offset+1}—${offset+page.items.length}</span></header><div><table><thead><tr><th>统计期</th><th>数值</th><th>单位</th><th>报告国 / 伙伴国</th><th>商品 / 分类 / 方向</th><th>来源库代码</th><th>版本</th><th>状态</th><th>原始来源</th></tr></thead><tbody>${page.items.map(row=>`<tr><td>${row.period}</td><td>${row.value===null ? "缺失":escapeHtml(row.value)}</td><td>${escapeHtml(row.unit || "未注明")}</td><td>${escapeHtml(row.country_iso3)} / ${escapeHtml(row.partner_iso3 || "—")}</td><td>${escapeHtml([row.commodity_code,row.commodity_classification,row.trade_flow].filter(Boolean).join(" / ") || "—")}</td><td>${escapeHtml(row.source_dataset_code || "—")}</td><td>v${row.version_no}</td><td>${escapeHtml(row.missing_reason || row.source_status)}</td><td>${safeUrl(row.source_url) ? `<a href="${escapeHtml(safeUrl(row.source_url))}" target="_blank" rel="noopener noreferrer">回源 ↗</a>`:"—"}</td></tr>`).join("")}</tbody></table></div><div class="reading-tabs"><button data-data-offset="${offset-20}" ${offset===0 ? "disabled":""}>上一页</button><button data-data-offset="${offset+20}" ${offset+page.items.length>=selected.indicator.records.length ? "disabled":""}>下一页</button></div></section>`;
  } catch(error) { if(request===state.dataRecordRequest) root.innerHTML=errorState(error.message); }
}

async function renderCountryEventChainDetail(eventId) {
  const root = document.getElementById("countryEventChainDetail");
  if (!root) return;
  const request = openCountryReading("事件链与证据");
  root.innerHTML = detailSkeleton("正在读取事件链、关系与确认来源…");
  try {
    const [detail, evidence] = await Promise.all([apiFetch(`/reader/events/${eventId}`), apiFetch(`/reader/events/${eventId}/evidence`)]);
    if (request !== state.countryReadingRequest || Number(state.selectedCountryEventId) !== Number(eventId)) return;
    const perspective = state.countryEventFilters?.perspective || "";
    const sources = (evidence.sources || []).filter((item) => !perspective || item.perspective_group === perspective);
    const actors = (detail.entities || []).filter((item) => item.role === "actor").map((item) => item.name);
    const places = (detail.entities || []).filter((item) => ["place", "location"].includes(item.role)).map((item) => item.name);
    const relations = detail.relations || [];
    root.innerHTML = `<article class="event-chain-detail"><header><div><span class="feed-status is-reviewed">已复核事件链</span><h3 ${quoteAttributes({type:"event",id:detail.id,title:detail.title,source_name:"已复核事件"},"title")}>${escapeHtml(detail.title)}</h3></div><a href="#/events/${detail.id}">完整证据台</a></header><p ${quoteAttributes({type:"event",id:detail.id,title:detail.title,source_name:"已复核事件"},"summary")}>${escapeHtml(detail.summary || "事件摘要待登记；以下只展示已保存的节点和关系。")}</p><button class="ghost-btn" data-country-add-object="${escapeHtml(JSON.stringify({type:"event",id:detail.id,title:detail.title,source_name:"已复核事件"}))}">引用此事件</button><dl class="event-chain-facts"><div><dt>发生 / 报道</dt><dd>${escapeHtml(localDate(detail.start_at))} / ${escapeHtml(localDate(sources[0]?.times?.reported_at))}</dd></div><div><dt>录入 / 更新</dt><dd>${escapeHtml(localDate(detail.recorded_at))} / ${escapeHtml(localDate(detail.updated_at))}</dd></div><div><dt>主体</dt><dd>${actors.length ? escapeHtml(actors.join("、")) : "待核验"}</dd></div><div><dt>地点</dt><dd>${places.length ? escapeHtml(places.join("、")) : "待核验"}</dd></div></dl><section><h4>事件节点</h4><div class="event-chain-timeline">${(detail.series_timeline || [detail]).map((item) => `<article class="${item.current ? "is-current" : ""}"><time>${escapeHtml(localDate(item.start_at))}</time><strong>${escapeHtml(item.title)}</strong><span>${Number(item.source_count || 0)} 个确认来源</span></article>`).join("")}</div></section><section><h4>多源视角</h4>${sources.length ? `<div class="perspective-source-list">${sources.map((item) => `<article><span>${escapeHtml(item.perspective_group === "unclassified" ? "未分类" : item.perspective_group)} · ${escapeHtml(item.perspective_method)}</span><strong>${escapeHtml(item.document?.source_name || "来源未登记")}</strong><p>${escapeHtml(item.mention_summary || "来源提及摘要待核验")}</p><small>报道 ${escapeHtml(localDate(item.times?.reported_at))} · 录入 ${escapeHtml(localDate(item.times?.recorded_at))} · 更新 ${escapeHtml(localDate(item.times?.updated_at))}</small></article>`).join("")}</div>` : emptyState("当前视角没有人工标记来源；未知来源不自动分类。")}</section><section><h4>影响与后续关系</h4>${relations.length ? relations.map((relation) => `<article><strong>${escapeHtml(relation.relation_type)}</strong><span>${escapeHtml(relation.event?.title || "关联事件")}</span><small>${escapeHtml(relation.note || "关系说明待核验")}</small></article>`).join("") : `<p>没有已登记关系；不推断因果或后续。</p>`}</section><footer><span>${escapeHtml(evidence.deduplication?.method_note || "报道量已按版本和来源去重。")} 热度不等同风险判断。请使用页面上方“内容加入项目”统一选择。</span></footer></article>`;
    root.insertAdjacentHTML("beforeend", `<section class="reading-evidence"><h3>完整证据对照</h3>${evidenceMatrixHtml(evidence.comparison_groups, evidence.method_note, evidence.sources)}</section>`);
  } catch (error) { if (request === state.countryReadingRequest) root.innerHTML = errorState(error.message); }
}

function fieldMaterialTypeLabel(value) {
  return ({ field_note: "田野札记", interview_transcript: "访谈转写", photo: "现场照片", supporting_document: "辅助文档" })[value] || value;
}

function fieldMaterialPrivacyLabel(value) {
  return ({ restricted: "受限", anonymized: "已匿名化", shareable: "内部可共享" })[value] || value;
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function fieldMaterialCardHtml(item) {
  return `<article class="field-material-card"><header><div><span>${escapeHtml(fieldMaterialTypeLabel(item.material_type))}</span><h3>${escapeHtml(item.title)}</h3></div><em>用户提供 · 未复核</em></header><p>${escapeHtml(item.original_filename)} · ${escapeHtml(formatBytes(item.byte_size))}${item.captured_on ? ` · 采集 ${escapeHtml(item.captured_on)}` : ""}</p>${item.method_note ? `<small>${escapeHtml(item.method_note)}</small>` : ""}<div class="field-material-meta"><span>${escapeHtml(fieldMaterialPrivacyLabel(item.privacy_level))}</span><span>SHA-256 ${escapeHtml(String(item.sha256 || "").slice(0, 10))}…</span></div><footer><a class="text-link" href="${escapeHtml(item.download_url)}" download>下载原文件</a><button type="button" class="ghost-btn compact" data-organize-field-material="${item.id}">用 S05 登记整理</button></footer></article>`;
}

function renderTopicFieldMaterials() {
  const root = document.getElementById("topicFieldMaterialResults");
  if (!root) return;
  root.innerHTML = state.topicFieldMaterials.length
    ? state.topicFieldMaterials.map(fieldMaterialCardHtml).join("")
    : emptyState("当前项目还没有用户上传的田野资料。");
}

async function loadTopicFieldMaterials() {
  const root = document.getElementById("topicFieldMaterialResults");
  if (!root || !state.topic?.id) return;
  root.innerHTML = `<div class="empty-state">正在读取项目田野资料…</div>`;
  try {
    state.topicFieldMaterials = await apiFetch(`/reader/field-materials?research_case_id=${encodeURIComponent(state.topic.id)}`);
    renderTopicFieldMaterials();
  } catch (error) {
    root.innerHTML = error.status === 403 ? emptyState("当前只读环境不开放私有田野资料。") : errorState(error.message);
  }
}

function syncFieldMaterialTopicOptions(countryIso3, selectedTopicId = null) {
  const select = document.getElementById("fieldMaterialTopic");
  if (!select) return;
  const compatible = (state.topics || []).filter((item) => item.scope?.country_iso3 === countryIso3);
  select.innerHTML = compatible.length
    ? compatible.map((item) => `<option value="${item.id}"${Number(item.id) === Number(selectedTopicId) ? " selected" : ""}>${escapeHtml(item.title)}</option>`).join("")
    : `<option value="">当前国家暂无可上传项目</option>`;
}

async function openFieldMaterialDialog(scope = "topic") {
  if (scope !== "topic" || !state.topic?.id) {
    showToast("田野资料只能从项目详情页上传");
    return;
  }
  state.fieldUploadScope = "topic";
  if (!(state.topics || []).length) {
    try { state.topics = await apiFetch("/reader/research-cases"); } catch (_) { state.topics = []; }
  }
  const form = document.getElementById("fieldMaterialForm");
  const dialog = document.getElementById("fieldMaterialDialog");
  form.reset();
  const countryIso3 = state.topic.scope?.country_iso3 || "COD";
  const country = state.catalog.find((item) => item.iso3 === countryIso3);
  document.getElementById("fieldMaterialCountryLabel").value = `${country?.name_zh || countryIso3} · ${countryIso3}`;
  syncFieldMaterialTopicOptions(countryIso3, state.topic.id);
  document.getElementById("fieldMaterialTopic").disabled = true;
  document.getElementById("fieldMaterialFileSummary").textContent = "支持 TXT、Markdown、PDF、JPG、PNG；单个文件不超过 10 MB。";
  document.getElementById("fieldMaterialStatus").textContent = "";
  document.getElementById("fieldMaterialDialogTitle").textContent = "上传到项目田野资料";
  if (!dialog.open) dialog.showModal();
}

function closeFieldMaterialDialog() {
  const dialog = document.getElementById("fieldMaterialDialog");
  if (dialog.open) dialog.close();
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("浏览器读取文件失败"));
    reader.readAsDataURL(file);
  });
}

async function submitFieldMaterial(event) {
  event.preventDefault();
  const file = document.getElementById("fieldMaterialFile").files?.[0];
  const submit = document.getElementById("fieldMaterialSubmit");
  const statusRoot = document.getElementById("fieldMaterialStatus");
  if (!file) { statusRoot.textContent = "请先选择一个文件。"; return; }
  if (file.size > 10 * 1024 * 1024) { statusRoot.textContent = "单个文件不得超过 10 MB。"; return; }
  submit.disabled = true;
  submit.textContent = "正在校验并上传…";
  statusRoot.textContent = "正在读取本地文件；内容不会发送到外部模型。";
  try {
    const topicId = Number(document.getElementById("fieldMaterialTopic").value || 0) || null;
    if (!topicId) throw new ReaderApiError("请选择一个项目后再上传。", "topic_required");
    const payload = await apiFetch("/reader/field-materials", {
      method: "POST",
      body: JSON.stringify({
        title: document.getElementById("fieldMaterialTitle").value.trim(),
        filename: file.name,
        content_type: file.type || "application/octet-stream",
        content_base64: await fileToBase64(file),
        material_type: document.getElementById("fieldMaterialType").value,
        privacy_level: document.getElementById("fieldMaterialPrivacy").value,
        country_iso3: state.topic?.scope?.country_iso3 || null,
        research_case_id: topicId,
        captured_on: document.getElementById("fieldMaterialCapturedOn").value || null,
        method_note: document.getElementById("fieldMaterialMethodNote").value.trim(),
        authorization_confirmed: document.getElementById("fieldMaterialAuthorization").checked,
      }),
    });
    closeFieldMaterialDialog();
    showToast(`已上传“${payload.title}”并标记为用户提供 · 未复核`);
    if (state.topic?.id === payload.research_case_id) await loadTopicFieldMaterials();
  } catch (error) {
    statusRoot.textContent = `上传失败：${error.message}`;
  } finally {
    submit.disabled = false;
    submit.textContent = "确认上传";
  }
}

async function organizeFieldMaterial(materialId, button) {
  const item = state.topicFieldMaterials.find((row) => Number(row.id) === Number(materialId));
  if (!item) return;
  if (!(state.capabilities || []).length) state.capabilities = await apiFetch("/reader/capabilities");
  const template = state.capabilities.find((row) => row.slug === "field-material-organizer" && row.status === "active");
  const config = template?.configs?.[0];
  if (!config) throw new ReaderApiError("S05 尚未安装活动配置。", "capability_missing");
  const fileRef = { upload_id: item.id, title: item.title, filename: item.original_filename, content_type: item.content_type, privacy: item.privacy_level, evidence_status: item.evidence_status, captured_on: item.captured_on };
  await runCapability(config.id, item.research_case_id || "", button, { file_refs: [fileRef], privacy_mode: item.privacy_level });
}

function countryAdminLevelLabel(level) {
  return ({ ADM1: "省级", ADM2: "领地 / 城市", ADM3: "历史三级参考" })[level] || level;
}

function countryAdminFeatureKey(feature) {
  const properties = feature?.properties || {};
  return properties.feature_id || properties.pcode || properties.shapeID || properties.shapeISO || `${properties.shapeType || "ADM"}:${properties.shapeName || "unknown"}`;
}

function countryAdminDisplayName(feature) {
  const properties = feature?.properties || {};
  return properties.display_name_zh || properties.shapeName || "未命名行政单元";
}

function normalizeCountryAdminFeature(feature, meta) {
  const properties = feature.properties || (feature.properties = {});
  const sourceName = properties.source_name || properties.shapeName || "未命名行政单元";
  const pcode = properties.pcode || properties.shapeISO || properties.shapeID || null;
  properties.feature_id = properties.feature_id || pcode || `${meta.level}:${sourceName}`;
  properties.pcode = pcode;
  properties.source_name = sourceName;
  properties.display_name_zh = properties.display_name_zh || (meta.level === "ADM1" ? ADMIN1_NAME_ZH[pcode] : "") || sourceName;
  properties.admin_level = meta.level;
  properties.represented_year = meta.represented_year;
  properties.label_point = properties.label_point || pointOnSurface(feature.geometry);
  return feature;
}

function countryAdminFeatureStyle(feature, selected = false, dimmed = false, hovered = false) {
  const capital = /kinshasa/i.test(feature?.properties?.shapeName || "");
  const contextualBasemap = state.countryMapMode === "research";
  return {
    color: selected || hovered ? "#174dad" : "#4775bd",
    weight: selected ? 2.8 : hovered ? 2.2 : .9,
    fillColor: selected ? "#2468e5" : hovered ? "#6d9ee8" : capital ? "#85afea" : "#cfe0fb",
    fillOpacity: dimmed ? .025 : contextualBasemap ? (selected ? .28 : hovered ? .2 : .08) : (selected ? .84 : hovered ? .76 : .62),
    opacity: dimmed ? .25 : 1,
  };
}

function geometryPolygons(geometry) {
  if (!geometry) return [];
  if (geometry.type === "Polygon") return [geometry.coordinates];
  if (geometry.type === "MultiPolygon") return geometry.coordinates || [];
  return [];
}

function ringSignedArea(ring = []) {
  let sum = 0;
  for (let index = 0; index < ring.length - 1; index += 1) sum += ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1];
  return sum / 2;
}

function ringCentroid(ring = []) {
  const area = ringSignedArea(ring);
  if (!ring.length || Math.abs(area) < 1e-12) return null;
  let x = 0; let y = 0;
  for (let index = 0; index < ring.length - 1; index += 1) {
    const cross = ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1];
    x += (ring[index][0] + ring[index + 1][0]) * cross;
    y += (ring[index][1] + ring[index + 1][1]) * cross;
  }
  return [x / (6 * area), y / (6 * area)];
}

function pointInRing(point, ring = []) {
  let inside = false;
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index, index += 1) {
    const [xi, yi] = ring[index]; const [xj, yj] = ring[previous];
    const intersects = ((yi > point[1]) !== (yj > point[1])) && (point[0] < ((xj - xi) * (point[1] - yi)) / ((yj - yi) || Number.EPSILON) + xi);
    if (intersects) inside = !inside;
  }
  return inside;
}

function pointInPolygon(point, polygon = []) {
  return Boolean(polygon[0] && pointInRing(point, polygon[0]) && !polygon.slice(1).some((hole) => pointInRing(point, hole)));
}

function pointOnSurface(geometry) {
  const polygons = geometryPolygons(geometry).filter((polygon) => polygon?.[0]?.length);
  if (!polygons.length) return null;
  const polygon = [...polygons].sort((left, right) => Math.abs(ringSignedArea(right[0])) - Math.abs(ringSignedArea(left[0])))[0];
  const ring = polygon[0];
  const xs = ring.map((point) => point[0]); const ys = ring.map((point) => point[1]);
  const minX = Math.min(...xs); const maxX = Math.max(...xs); const minY = Math.min(...ys); const maxY = Math.max(...ys);
  const candidates = [ringCentroid(ring), [(minX + maxX) / 2, (minY + maxY) / 2]].filter(Boolean);
  for (const candidate of candidates) if (pointInPolygon(candidate, polygon)) return candidate;
  let best = ring[0]; let bestDistance = -1;
  for (let row = 1; row < 10; row += 1) {
    for (let column = 1; column < 10; column += 1) {
      const candidate = [minX + ((maxX - minX) * column) / 10, minY + ((maxY - minY) * row) / 10];
      if (!pointInPolygon(candidate, polygon)) continue;
      const distance = Math.min(candidate[0] - minX, maxX - candidate[0], candidate[1] - minY, maxY - candidate[1]);
      if (distance > bestDistance) { best = candidate; bestDistance = distance; }
    }
  }
  return best;
}

function syncCountryMapStyles() {
  const hoverKey = state.countryMapHoverKey;
  state.countryMapLayer?.eachLayer((layer) => {
    const key = countryAdminFeatureKey(layer.feature);
    layer.setStyle(countryAdminFeatureStyle(layer.feature, key === state.countryAdminSelectedKey, Boolean(hoverKey && key !== hoverKey), key === hoverKey));
  });
}

function labelCollision(left, right) {
  return !(left.right < right.left || left.left > right.right || left.bottom < right.top || left.top > right.bottom);
}

function syncCountryMapLabels() {
  const map = state.countryMap;
  if (!map || !state.countryMapLabels.length) return;
  const minimumZoom = { ADM1: 4, ADM2: 6, ADM3: 8 }[state.countryAdminLevel] || 4;
  const bounds = map.getBounds();
  const occupied = [];
  const prioritized = [...state.countryMapLabels].sort((left, right) => {
    const leftPriority = Number(left.key === state.countryAdminSelectedKey) * 4 + Number(left.key === state.countryMapHoverKey) * 3;
    const rightPriority = Number(right.key === state.countryAdminSelectedKey) * 4 + Number(right.key === state.countryMapHoverKey) * 3;
    return rightPriority - leftPriority || right.area - left.area;
  });
  prioritized.forEach((label) => {
    const element = label.marker.getElement?.();
    if (!element) return;
    const forced = label.key === state.countryAdminSelectedKey || label.key === state.countryMapHoverKey;
    const inView = bounds.contains(label.marker.getLatLng());
    let visible = inView && (forced || map.getZoom() >= minimumZoom);
    const point = map.latLngToContainerPoint(label.marker.getLatLng());
    const width = Math.min(150, Math.max(52, label.name.length * 13 + 18));
    const box = { left: point.x - width / 2, right: point.x + width / 2, top: point.y - 13, bottom: point.y + 13 };
    if (visible && !forced && occupied.some((other) => labelCollision(box, other))) visible = false;
    element.hidden = !visible;
    element.classList.toggle("is-selected", label.key === state.countryAdminSelectedKey);
    element.classList.toggle("is-hover", label.key === state.countryMapHoverKey);
    if (visible) occupied.push(box);
  });
}

function selectCountryAdminFeature(feature, layer, meta) {
  state.countryAdminSelectedKey = countryAdminFeatureKey(feature);
  syncCountryMapStyles();
  syncCountryMapLabels();
  renderAdmMapInspector(meta, feature);
  try { state.countryMap.fitBounds(layer.getBounds(), { padding: [36, 36], maxZoom: 9 }); } catch (_) {}
}

function renderAdmMapInspector(meta, feature = null) {
  const root = document.getElementById("admMapInspector");
  if (!root) return;
  if (!feature) { root.hidden = true; root.innerHTML = ""; return; }
  root.hidden = false;
  const properties = feature.properties || {};
  root.innerHTML = `<strong>${escapeHtml(countryAdminDisplayName(feature))}</strong><span>${escapeHtml(meta?.level || "")} · ${escapeHtml(meta?.represented_year || "")}${properties.pcode ? ` · ${escapeHtml(properties.pcode)}` : ""}</span>`;
}

async function fetchReaderAsset(path) {
  let response = await fetch(`./${path}`);
  if (!response.ok) response = await fetch(`/reader/${path}`);
  if (!response.ok) throw new Error(`地图资产 HTTP ${response.status}`);
  return response.json();
}

function syncCountryBasemap() {
  const map = state.countryMap;
  const meta = state.country?.map?.basemap;
  document.querySelectorAll("[data-map-mode]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.mapMode === state.countryMapMode));
  });
  if (!map || !meta || typeof window.protomapsL?.leafletLayer !== "function") return;
  if (!state.countryBaseMapLayer) {
    state.countryBaseMapLayer = window.protomapsL.leafletLayer({
      url: meta.url,
      flavor: "light",
      lang: "fr",
      attribution: meta.attribution,
      maxDataZoom: meta.max_zoom,
    });
  }
  const hasLayer = map.hasLayer(state.countryBaseMapLayer);
  if (state.countryMapMode === "research" && !hasLayer) state.countryBaseMapLayer.addTo(map);
  if (state.countryMapMode !== "research" && hasLayer) map.removeLayer(state.countryBaseMapLayer);
  syncCountryMapStyles();
  state.countryMapLayer?.bringToFront?.();
  state.countryMapLabelLayer?.bringToFront?.();
}

function resetCountryMapView() {
  if (!state.countryMap || !state.countryMapLayer) return;
  state.countryAdminSelectedKey = null;
  state.countryMapHoverKey = null;
  syncCountryMapStyles();
  state.countryMap.invalidateSize({ pan: false });
  state.countryMap.fitBounds(state.countryMapLayer.getBounds(), { padding: [18, 18] });
  const meta = (state.country?.map?.levels || []).find((item) => item.level === state.countryAdminLevel);
  renderAdmMapInspector(meta || null);
}

async function renderAdmMap() {
  const root = document.getElementById("admMap");
  const iso3 = state.country?.iso3;
  const countryName = state.country?.name;
  const status = document.getElementById("countryMapStatus");
  const attribution = document.getElementById("admMapAttribution");
  const switcher = document.getElementById("admLevelSwitch");
  if (!root || !iso3) return;
  const levels = state.country?.map?.levels || [];
  const available = levels.map((item) => item.level);
  if (!available.includes(state.countryAdminLevel)) state.countryAdminLevel = available[0] || "ADM1";
  state.countryAdminSelectedKey = null;
  if (switcher) {
    switcher.innerHTML = levels.length
      ? levels.map((item) => `<button type="button" data-adm-level="${escapeHtml(item.level)}" aria-pressed="${item.level === state.countryAdminLevel}" title="${escapeHtml(item.level)} · ${Number(item.unit_count || 0).toLocaleString("zh-CN")} 单元 · ${escapeHtml(item.represented_year)}">${escapeHtml(item.level === "ADM1" ? "省级" : item.level === "ADM2" ? "市级" : "三级")}</button>`).join("")
      : `<span>更细行政区边界尚未登记</span>`;
  }
  try {
    if (!levels.length || typeof window.L === "undefined") {
      if (state.countryMap) {
        state.countryMap.remove();
        state.countryMap = null;
        state.countryMapLayer = null;
        state.countryBaseMapLayer = null;
        state.countryMapLabelLayer = null;
        state.countryMapLabels = [];
        state.countryMapIso3 = null;
      }
      root.innerHTML = `<div class="empty-state">正在加载已登记国家边界…</div>`;
      const world = await fetchReaderAsset("assets/world-countries-simplified.geojson");
      const feature = (world.features || []).find((item) => item.properties?.iso3 === iso3);
      if (!feature) throw new Error("世界地图资产中未找到该国家边界");
      root.innerHTML = geoJsonSvg({ type: "FeatureCollection", features: [feature] }, { ariaLabel: `${countryName}国家边界轮廓`, legend: "国家轮廓；更细行政区数据待接入", showLabels: false });
      if (status) status.textContent = "国家轮廓已登记";
      if (attribution) attribution.textContent = "国家轮廓来自内部交互世界地图资产；更细行政区尚未登记，公开使用待版图复核。";
      renderAdmMapInspector(null);
      return;
    }
    const meta = levels.find((item) => item.level === state.countryAdminLevel) || levels[0];
    if (!state.countryMap || state.countryMapIso3 !== iso3) {
      if (state.countryMap) state.countryMap.remove();
      root.innerHTML = "";
      state.countryMap = window.L.map(root, { zoomControl: true, attributionControl: true, minZoom: 4, maxZoom: 11, zoomSnap: 0.25, zoomDelta: 0.5, scrollWheelZoom: true, keyboard: true });
      state.countryBaseMapLayer = null;
      state.countryMap.attributionControl.setPrefix(false);
      window.L.control.scale({ imperial: false, maxWidth: 110 }).addTo(state.countryMap);
      state.countryMapIso3 = iso3;
      syncCountryBasemap();
    }
    if (state.countryMapLayer) state.countryMap.removeLayer(state.countryMapLayer);
    if (state.countryMapLabelLayer) state.countryMap.removeLayer(state.countryMapLabelLayer);
    if (state.countryMapAttribution) state.countryMap.attributionControl.removeAttribution(state.countryMapAttribution);
    root.classList.add("is-loading");
    const geojson = await fetchReaderAsset(meta.asset_path);
    (geojson.features || []).forEach((feature) => normalizeCountryAdminFeature(feature, meta));
    state.countryMapLayer = window.L.geoJSON(geojson, {
      style: (feature) => countryAdminFeatureStyle(feature, false),
      onEachFeature: (feature, layer) => {
        const name = countryAdminDisplayName(feature);
        layer.on({
          click: () => selectCountryAdminFeature(feature, layer, meta),
          mouseover: () => { state.countryMapHoverKey = countryAdminFeatureKey(feature); syncCountryMapStyles(); syncCountryMapLabels(); },
          mouseout: () => { state.countryMapHoverKey = null; syncCountryMapStyles(); syncCountryMapLabels(); },
          add: () => {
            const element = layer.getElement?.();
            if (!element) return;
            element.setAttribute("tabindex", "0");
            element.setAttribute("role", "button");
            element.setAttribute("aria-label", `${name}，${meta.level} 行政单元`);
            element.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); layer.fire("click"); } });
          },
        });
      },
    }).addTo(state.countryMap);
    state.countryMapLabelLayer = window.L.layerGroup().addTo(state.countryMap);
    state.countryMapLabels = (geojson.features || []).map((feature) => {
      const key = countryAdminFeatureKey(feature);
      const name = countryAdminDisplayName(feature);
      const labelPoint = feature.properties?.label_point;
      if (!Array.isArray(labelPoint)) return null;
      const marker = window.L.marker([labelPoint[1], labelPoint[0]], {
        keyboard: true,
        title: `${name} · ${feature.properties?.pcode || key}`,
        icon: window.L.divIcon({ className: "country-admin-label-wrapper", html: `<span class="country-admin-label" data-feature-id="${escapeHtml(key)}">${escapeHtml(name)}</span>`, iconSize: null }),
      }).addTo(state.countryMapLabelLayer);
      marker.on({
        click: () => {
          let polygonLayer = null;
          state.countryMapLayer.eachLayer((candidate) => { if (countryAdminFeatureKey(candidate.feature) === key) polygonLayer = candidate; });
          if (polygonLayer) selectCountryAdminFeature(feature, polygonLayer, meta);
        },
        mouseover: () => { state.countryMapHoverKey = key; syncCountryMapStyles(); syncCountryMapLabels(); },
        mouseout: () => { state.countryMapHoverKey = null; syncCountryMapStyles(); syncCountryMapLabels(); },
      });
      return { marker, key, name, area: Math.max(...geometryPolygons(feature.geometry).map((polygon) => Math.abs(ringSignedArea(polygon[0]))), 0) };
    }).filter(Boolean);
    state.countryMap.off("zoomend moveend", syncCountryMapLabels);
    state.countryMap.on("zoomend moveend", syncCountryMapLabels);
    syncCountryBasemap();
    state.countryMapAttribution = `${meta.attribution || "geoBoundaries"} · ${meta.license || "许可待登记"}`;
    state.countryMap.attributionControl.addAttribution(state.countryMapAttribution);
    state.countryMap.invalidateSize({ pan: false });
    state.countryMap.fitBounds(state.countryMapLayer.getBounds(), { padding: [18, 18] });
    window.setTimeout(syncCountryMapLabels, 50);
    root.classList.remove("is-loading");
    if (status) status.textContent = "";
    if (attribution) { attribution.hidden = true; attribution.textContent = ""; }
    renderAdmMapInspector(meta);
    [40, 180].forEach((delay) => window.setTimeout(() => state.countryMap?.invalidateSize(), delay));
  } catch (error) {
    root.classList.remove("is-loading");
    if (!state.countryMap) root.innerHTML = errorState(error.message);
    else showToast(`行政区图层加载失败：${error.message}`);
  }
}

function geoJsonSvg(geojson, options = {}) {
  const { ariaLabel = "国家或行政区边界", legend = "边界数据已登记", showLabels = true } = options;
  const polygons = []; const features = []; const points = [];
  for (const feature of geojson.features || []) {
    const geometry = feature.geometry || {}; const parts = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates || []; const name = feature.properties?.shapeName || "ADM1"; const featurePoints = [];
    for (const polygon of parts) { polygons.push({ rings: polygon, name }); for (const ring of polygon) for (const point of ring) { points.push(point); featurePoints.push(point); } }
    if (featurePoints.length) features.push({ name, points: featurePoints });
  }
  if (!points.length) return emptyState("地图资产没有可绘制边界");
  const xs = points.map((point) => point[0]); const ys = points.map((point) => point[1]); const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys); const width = 900, height = 600, pad = 28; const scale = Math.min((width-pad*2)/(maxX-minX),(height-pad*2)/(maxY-minY)); const project = ([x,y]) => [pad+(x-minX)*scale,height-pad-(y-minY)*scale];
  const paths = []; const labels = [];
  for (const polygon of polygons) { const d = polygon.rings.map((ring) => ring.map((point,index) => { const [x,y] = project(point); return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`; }).join(" ")+" Z").join(" "); paths.push(`<path class="${/kinshasa/i.test(polygon.name) ? "capital-region" : ""}" d="${d}" tabindex="0"><title>${escapeHtml(polygon.name)}</title></path>`); }
  if (showLabels) for (const feature of features) { const center = feature.points.reduce((sum,point) => [sum[0]+point[0],sum[1]+point[1]],[0,0]).map((value) => value/feature.points.length); const [x,y] = project(center); labels.push(`<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="middle">${escapeHtml(feature.name)}</text>`); }
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(ariaLabel)}"><g>${paths.join("")}${labels.join("")}</g><g transform="translate(28 566)"><circle r="5" fill="#2468e5"></circle><text x="10" y="4" text-anchor="start">${escapeHtml(legend)}</text></g></svg>`;
}

function paneQaPresets(country, pane = "overview") {
  if (!country) return [];
  const name = country.name_zh || country.name || "该国";
  const map = {
    overview: [
      { label: `${name}核心国情与战略态势梳理`, question: `请结合当前国别概览，系统梳理${name}的区位特征、政治经济基本面与主要战略态势。` },
      { label: `研判对${name}投资合作关键风险与机遇`, question: `请根据当前国情概况，分析我国机构与企业在${name}开展经贸合作面临的主要风险与潜在机遇。` },
      { label: `支柱产业与资源禀赋特征`, question: `请分析${name}的核心资源禀赋、主要支柱产业及当前经济发展动能。` },
      { label: `双边经贸往来与战略互信现状`, question: `请结合最新概况，总结我国与${name}的双边经贸合作重点与战略合作现状。` },
    ],
    events: [
      { label: `研判近期热点动态对政经局势影响`, question: `请结合当前最新报道与动态，评估近期重点事件对${name}政治稳定及经济走向的影响。` },
      { label: `梳理关键已复核事件链发展脉络`, question: `请梳理${name}近期的关键事件线索，分析事件起因、发展节点及后续演变趋势。` },
      { label: `对比不同视角对近期事件的研判差异`, question: `请对比官方、媒体与智库视角，分析各方对${name}近期动态关注焦点的异同。` },
      { label: `预警潜在安全与市场突发风险`, question: `基于近期高频动态，有哪些可能进一步发酵的社会安全或市场准入风险需要提前预警？` },
    ],
    research: [
      { label: `梳理学界对${name}核心研究主题演变`, question: `请结合前沿学术论文，总结学界围绕${name}研究的核心议题及近年来演进趋势。` },
      { label: `分析主流学者采用的研究方法与视角`, question: `关于${name}的现有前沿研究主要采用了哪些研究方法？存在哪些研究分歧或理论创新？` },
      { label: `挖掘前沿文献共识与知识盲区 (Gaps)`, question: `在前沿文献中，学者们对${name}有哪些高度共识？目前还存在哪些亟待深化的研究空白？` },
      { label: `提炼前沿学术成果对对外国策的启示`, question: `学术界对${name}的最新实证研究，对我国制定对该国合作策略有哪些前瞻性启示？` },
    ],
    reports: [
      { label: `汇总国内外主流智库核心研判观点`, question: `请结合智库报告，归纳各家机构对${name}中长期战略走向和治理能力的评估。` },
      { label: `对比不同智库对该国走势的情景预测`, question: `不同智库在评估${name}未来走势时，有哪些主要的观点分歧与情景假设？` },
      { label: `提取智库针对重点领域的政策建言`, question: `各大智库针对${name}的重点领域（如产业、法治、营商环境）提出了哪些具体政策建言？` },
      { label: `智库研判对企业合规出海的指导建议`, question: `针对${name}的智库研判中，有哪些关于合规风险与本地化经营的关键提示？` },
    ],
    policies: [
      { label: `梳理该国最新政策法规及外资监管导向`, question: `请梳理${name}近期出台的重要政策与法规，分析其对外国投资与外贸合作的监管导向。` },
      { label: `解析关键产业与矿业法案供应链影响`, question: `请重点分析${name}的核心产业与矿产开发法规变动，对上下游供应链带来哪些合规要求与风险？` },
      { label: `评估政策生命周期与执行不确定性`, question: `根据政策生命周期与历史执行记录，该国政策落地存在哪些执行偏差或不确定性风险？` },
      { label: `制定应对该国新规的合规应对建议`, question: `企业或机构应如何调整合规策略，以妥善应对${name}近期的政策与法制调整？` },
    ],
    data: [
      { label: `解读宏观经济与人口核心指标趋势`, question: `请结合当前数据指标，解读${name}的GDP、通胀、外债及人口发展趋势。` },
      { label: `核验指标口径与多机构数据差异`, question: `对于${name}的重点统计指标，世界银行、IMF等国际机构与本国口径有何差异？数据置信度如何？` },
      { label: `基于时序数据研判宏观经济韧性`, question: `从近5-10年的时间序列数据看，${name}在面对外部冲击时的宏观经济韧性表现如何？` },
      { label: `提取最值得警惕的异常波动指标`, question: `在当前监测的指标集合中，哪些指标近期出现了显著异动或值得特别警惕的信号？` },
    ],
  };
  return map[pane] || map.overview;
}

function renderQaPresets() {
  const pane = state.countryPane || "overview";
  const items = state.country ? paneQaPresets(state.country, pane) : (state.country?.qa_presets?.length ? state.country.qa_presets : assistantPresets().slice(0, 4));
  const html = items.map((item) => item.action === "topic-onboarding" ? `<button type="button" data-topic-onboarding>${escapeHtml(item.label)}</button>` : `<button type="button" data-qa-question="${escapeHtml(item.question)}">${escapeHtml(item.label)}</button>`).join("");
  const presets = document.getElementById("copilotQaPresets");
  if (presets) {
    presets.innerHTML = html;
    presets.hidden = currentAssistantSession().messages.length > 0 || !html;
  }
}

function applyAssistantWidth(width = state.assistantWidth, persist = true) {
  const panel = document.getElementById("countryCopilot");
  const compactSplit = window.innerWidth < ASSISTANT_PINNED_MIN_WIDTH;
  const countryChat = countryChatLayout() || /^#\/countries\/[A-Z]{3}(?:\/|$)/.test(location.hash);
  const minimumWorkspace = countryChat ? 600 : compactSplit ? 700 : 720;
  const maximumPanel = countryChat ? 520 : compactSplit ? 360 : 460;
  const availableMax = Math.max(countryChat ? 320 : 340, window.innerWidth - 100 - minimumWorkspace);
  const nextWidth = Math.min(maximumPanel, availableMax, Math.max(countryChat ? 320 : 340, Number(width) || (countryChat ? 360 : 380)));
  state.assistantWidth = nextWidth;
  document.documentElement.style.setProperty("--assistant-panel-width", `${nextWidth}px`);
  const resizer = document.getElementById("copilotResizer");
  if (resizer) { resizer.setAttribute("aria-valuenow", String(Math.round(nextWidth))); resizer.setAttribute("aria-valuemin", String(countryChat ? 320 : 340)); resizer.setAttribute("aria-valuemax", String(Math.min(maximumPanel,availableMax))); }
  if (persist) try { window.sessionStorage.setItem(ASSISTANT_WIDTH_STORAGE_KEY, String(nextWidth)); } catch (_) { /* session preference unavailable */ }
  if (panel && window.innerWidth >= (countryChat ? 1100 : ASSISTANT_SIDE_MIN_WIDTH)) panel.style.width = `${nextWidth}px`;
}

function syncAssistantLayout() {
  const panel = document.getElementById("countryCopilot");
  const layout = document.getElementById("mainContent");
  if (!panel || !layout) return;
  const countryChat = countryChatLayout();
  document.body.classList.toggle("country-chat-workspace", countryChat);
  const fullscreen = window.innerWidth < (countryChat ? 1100 : 768);
  const pinned = window.innerWidth >= (countryChat ? 1100 : ASSISTANT_PINNED_MIN_WIDTH);
  const sideDrawer = !countryChat && window.innerWidth >= ASSISTANT_SIDE_MIN_WIDTH && window.innerWidth < ASSISTANT_PINNED_MIN_WIDTH;
  const bottomDrawer = !countryChat && window.innerWidth >= 768 && window.innerWidth < ASSISTANT_SIDE_MIN_WIDTH;
  const drawer = sideDrawer || bottomDrawer;
  panel.setAttribute("role", pinned || sideDrawer ? "complementary" : "dialog");
  panel.setAttribute("aria-modal", String(fullscreen));
  panel.classList.toggle("is-fullscreen", fullscreen);
  panel.classList.toggle("is-drawer", drawer);
  panel.classList.toggle("is-side-drawer", sideDrawer);
  panel.classList.toggle("is-bottom-drawer", bottomDrawer);

  const isOpen = !panel.hidden;
  document.body.classList.toggle("assistant-side-open", isOpen && (pinned || sideDrawer));
  document.body.classList.toggle("assistant-bottom-open", isOpen && bottomDrawer);
  if (isOpen && (pinned || sideDrawer)) {
    if (state.assistantSidebarWasCollapsed === null) {
      state.assistantSidebarWasCollapsed = document.body.classList.contains("sidebar-collapsed");
    }
    setSidebarCollapsed(true, false);
    panel.classList.toggle("is-pinned", pinned);
    layout.classList.add("is-research-mode");
  } else {
    panel.classList.remove("is-pinned");
    layout.classList.remove("is-research-mode");
    if (state.assistantSidebarWasCollapsed !== null) {
      setSidebarCollapsed(state.assistantSidebarWasCollapsed, false);
      state.assistantSidebarWasCollapsed = null;
    }
  }

  if (pinned || sideDrawer) applyAssistantWidth(state.assistantWidth, false);
  else panel.style.removeProperty("width");
  renderSelectedContextChips();
  positionCountryReading();
  window.setTimeout(() => {
    state.worldMap?.invalidateSize();
    state.countryMap?.invalidateSize();
    if (state.countryMapLayer && !state.countryAdminSelectedKey) {
      state.countryMap?.fitBounds(state.countryMapLayer.getBounds(), { padding: [18, 18] });
    }
    syncCountryMapLabels();
  }, 220);
}

function setCopilotOpen(isOpen, focusInput = false) {
  const panel = document.getElementById("countryCopilot");
  const layout = document.getElementById("mainContent");
  if (!panel || !layout) return;
  const session = currentAssistantSession();
  if (!isOpen) session.scrollTop = document.getElementById("copilotBody")?.scrollTop || 0;
  if (!isOpen) persistAssistantSessions();
  panel.hidden = !isOpen;
  layout.classList.toggle("is-research-mode", isOpen);
  document.body.classList.toggle("assistant-open", isOpen);
  document.body.classList.toggle("assistant-fullscreen-open", isOpen && window.innerWidth < 768);
  syncAssistantLayout();
  document.querySelectorAll("[data-copilot-toggle]").forEach((button) => button.setAttribute("aria-expanded", String(isOpen)));
  document.querySelectorAll("[data-copilot-label]").forEach((label) => { label.textContent = isOpen ? "关闭研究模式" : "AI 研究"; });
  syncAssistantRunBar();
  renderSelectedContextChips();
  if (isOpen) window.setTimeout(() => {
    const body = document.getElementById("copilotBody");
    if (body) body.scrollTop = session.scrollTop || body.scrollTop;
    if (focusInput) document.getElementById("assistantQuestion")?.focus({ preventScroll: true });
  }, 80);
}

function openCopilot(focusInput = false, explicit = true) {
  if (explicit) {
    state.assistantAutoDismissed = false;
    try { window.sessionStorage.removeItem(ASSISTANT_AUTO_DISMISSED_KEY); } catch (_) { /* session preference unavailable */ }
  }
  setCopilotOpen(true, focusInput);
}
function maybeAutoOpenAssistant(isResearchRoute) {
  if (!isResearchRoute) {
    if (!document.getElementById("countryCopilot")?.hidden) closeCopilot(false, false);
    return;
  }
  if (window.innerWidth < (countryChatLayout() ? 1100 : ASSISTANT_PINNED_MIN_WIDTH)) return;
  if (state.assistantAutoDismissed) return;
  if (document.getElementById("countryCopilot")?.hidden) openCopilot(false, false);
}

function closeCopilot(restoreFocus = true, manual = true) {
  if (manual) {
    state.assistantAutoDismissed = true;
    try { window.sessionStorage.setItem(ASSISTANT_AUTO_DISMISSED_KEY, "1"); } catch (_) { /* session preference unavailable */ }
  }
  setCopilotOpen(false);
  if (restoreFocus) window.setTimeout(() => state.assistantLastTrigger?.focus(), 0);
}

function focusCopilot() {
  openCopilot(true);
  const input = document.getElementById("assistantQuestion");
  window.setTimeout(() => {
    input?.focus({ preventScroll: true });
  }, 220);
}

function initCopilotResizer() {
  const resizer = document.getElementById("copilotResizer");
  if (!resizer) return;
  let startX = 0;
  let startWidth = state.assistantWidth;
  const move = (event) => applyAssistantWidth(startWidth + startX - event.clientX, false);
  const stop = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
    applyAssistantWidth(state.assistantWidth, true);
  };
  resizer.addEventListener("pointerdown", (event) => {
    if (window.innerWidth < 1100) return;
    event.preventDefault();
    resizer.focus();
    startX = event.clientX;
    startWidth = state.assistantWidth;
    resizer.setPointerCapture?.(event.pointerId);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  });
  resizer.addEventListener("keydown", (event) => {
    if (window.innerWidth < 1100 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? (countryChatLayout() ? 320 : 340) : event.key === "End" ? (countryChatLayout() ? 520 : 460) : state.assistantWidth + (event.key === "ArrowLeft" ? 10 : -10);
    applyAssistantWidth(next, true);
  });
}

function assistantContextForRoute() {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "invite") { window.ReaderAuth.open(); return; }
  const space = parts[0] || "countries";
  let context;
  if (space === "countries" && parts[1] === "global-events") context = { space: "event", country_iso3: null, event_id: null };
  else if (space === "countries" && parts[1] && !["search", "global-events"].includes(parts[1])) context = { space: "country", country_iso3: parts[1].toUpperCase() };
  else if (space === "countries") context = { space: "country", country_iso3: null };
  else if (space === "events") context = { space: "event", country_iso3: state.eventDetail?.country?.iso3 || state.eventFilterCountry || null, event_id: numericRouteId(parts[1]) };
  else if (space === "projects") context = { space: "topic", country_iso3: state.topic?.scope?.country_iso3 || document.getElementById("topicFrontierCountry")?.value || null, research_case_id: numericRouteId(parts[1]) };
  else if (space === "capabilities") context = { space: "capability", country_iso3: state.topic?.scope?.country_iso3 || null, capability_config_id: state.activeCapabilityConfigId, capability_run_id: parts[1] === "runs" && parts[2] ? Number(parts[2]) : state.activeCapabilityRunId };
  else if (space === "resources" || space === "methods") context = { space: "resource", country_iso3: null };
  else context = { space: "system", country_iso3: null };
  return { ...context, ...(state.assistantFocus || {}) };
}

function setAssistantFocus(element) {
  const focusType = element?.dataset.focusType;
  const focusKey = element?.dataset.focusKey;
  const focusLabel = element?.dataset.focusLabel;
  state.assistantFocus = focusType && focusKey ? { focus_type: focusType, focus_key: String(focusKey), focus_label: focusLabel || focusKey } : null;
  syncAssistantContext();
}

function assistantScopeId(context) {
  return [context.space, context.country_iso3 || "", context.event_id || "", context.research_case_id || "", context.capability_config_id || "", context.capability_run_id || ""].join(":");
}

function currentAssistantSession() {
  if (!state.assistantSessions[state.assistantScopeId]) state.assistantSessions[state.assistantScopeId] = normalizeAssistantSession();
  return state.assistantSessions[state.assistantScopeId];
}

function countryAssistantHistoryAvailable(context = state.assistantContext) {
  return Boolean(context?.space === "country" && context.country_iso3);
}

function syncAssistantHistoryView() {
  const panel = document.getElementById("countryCopilot");
  const historyPanel = document.getElementById("assistantHistoryPanel");
  const historyButton = document.getElementById("copilotHistoryBtn");
  const available = countryAssistantHistoryAvailable();
  if (!available) state.assistantHistoryOpen = false;
  if (historyButton) {
    historyButton.hidden = !available;
    historyButton.setAttribute("aria-pressed", String(available && state.assistantHistoryOpen));
  }
  if (historyPanel) historyPanel.hidden = !available || !state.assistantHistoryOpen;
  panel?.classList.toggle("is-history-open", available && state.assistantHistoryOpen);
}

function renderAssistantHistory() {
  const root = document.getElementById("assistantHistoryList");
  const meta = document.getElementById("assistantHistoryMeta");
  if (!root || !meta) return;
  if (state.assistantHistoryLoading) {
    meta.textContent = "正在读取数据库中的已保存记录";
    root.innerHTML = `<div class="assistant-history-state">正在读取历史对话…</div>`;
    return;
  }
  if (state.assistantHistoryError) {
    meta.textContent = "历史记录读取失败";
    root.innerHTML = `<div class="assistant-history-state is-error">${escapeHtml(state.assistantHistoryError)}<button type="button" class="text-link" data-reload-assistant-history>重试</button></div>`;
    return;
  }
  const items = state.assistantHistoryItems || [];
  const activeId = currentAssistantSession().conversationId;
  meta.textContent = `${state.assistantContext?.country_iso3 || "当前国家"} · ${items.length} / ${state.assistantHistoryTotal || items.length} 段已保存对话`;
  root.innerHTML = items.length
    ? `${items.map((item) => `<button type="button" class="assistant-history-item${item.conversation_id === activeId ? " is-active" : ""}" data-open-assistant-conversation="${escapeHtml(item.conversation_id)}"><span><strong>${escapeHtml(item.title || "未命名对话")}</strong><small>${Number(item.turn_count || 0)} 轮 · ${escapeHtml(runStatusLabel(item.status))}</small></span><time>${escapeHtml(localDate(item.updated_at))}</time></button>`).join("")}${state.assistantHistoryHasMore ? `<button type="button" class="assistant-history-more" data-load-more-assistant-history>加载更早对话</button>` : ""}`
    : `<div class="assistant-history-state"><strong>还没有历史对话</strong><span>从当前国家空间开始提问后，系统会自动保存。</span></div>`;
}

async function loadCountryAssistantHistory({ append = false } = {}) {
  const context = state.assistantContext;
  if (!countryAssistantHistoryAvailable(context)) return;
  const scopeId = state.assistantScopeId;
  const offset = append ? state.assistantHistoryItems.length : 0;
  state.assistantHistoryScopeId = scopeId;
  state.assistantHistoryLoading = true;
  state.assistantHistoryError = "";
  renderAssistantHistory();
  try {
    const payload = await apiFetch(`/reader/countries/${encodeURIComponent(context.country_iso3)}/assistant-conversations?limit=30&offset=${offset}`);
    if (scopeId !== state.assistantScopeId || state.assistantHistoryScopeId !== scopeId) return;
    state.assistantHistoryItems = append ? [...state.assistantHistoryItems, ...(payload.items || [])] : (payload.items || []);
    state.assistantHistoryTotal = Number(payload.total ?? state.assistantHistoryItems.length);
    state.assistantHistoryHasMore = Boolean(payload.has_more);
  } catch (error) {
    if (scopeId !== state.assistantScopeId || state.assistantHistoryScopeId !== scopeId) return;
    if (!append) state.assistantHistoryItems = [];
    state.assistantHistoryError = error.message;
  } finally {
    if (scopeId === state.assistantScopeId && state.assistantHistoryScopeId === scopeId) {
      state.assistantHistoryLoading = false;
      renderAssistantHistory();
    }
  }
}

async function openCountryAssistantHistory() {
  if (!countryAssistantHistoryAvailable()) {
    showToast("请先打开一个国家空间");
    return;
  }
  state.assistantHistoryOpen = true;
  syncAssistantHistoryView();
  await loadCountryAssistantHistory();
}

function closeAssistantHistory() {
  state.assistantHistoryOpen = false;
  syncAssistantHistoryView();
}

function startNewAssistantConversation() {
  if (currentAssistantSession().status === "running") {
    showToast("运行仍在后台进行，完成后才能新建对话");
    return;
  }
  state.assistantSessions[state.assistantScopeId] = normalizeAssistantSession();
  state.countrySelectingScope = null;
  persistAssistantSessions();
  closeAssistantHistory();
  renderSelectedContextChips();
  renderAssistantConversation();
  syncAssistantRunBar();
  document.getElementById("assistantQuestion")?.focus();
}

async function activateAssistantHistory(conversationId) {
  if (currentAssistantSession().status === "running") {
    showToast("当前对话仍在运行，完成后才能切换历史");
    return;
  }
  const session = normalizeAssistantSession({ conversationId });
  state.countrySelectingScope = null;
  state.assistantSessions[state.assistantScopeId] = session;
  persistAssistantSessions();
  closeAssistantHistory();
  renderAssistantConversation();
  await hydrateAssistantConversation(session);
}

function syncAssistantContext() {
  const context = assistantContextForRoute();
  const nextScopeId = assistantScopeId(context);
  if (state.assistantScopeId && state.assistantScopeId !== nextScopeId) {
    state.selectedMaterials = [];
    state.assistantHistoryOpen = false;
    state.assistantHistoryItems = [];
    state.assistantHistoryTotal = 0;
    state.assistantHistoryHasMore = false;
    state.assistantHistoryError = "";
  }
  state.assistantContext = context;
  state.assistantScopeId = nextScopeId;
  document.getElementById("copilotHistoryBtn").textContent = countryChatLayout() ? "☷ 对话" : "历史";
  document.getElementById("copilotClearBtn").textContent = countryChatLayout() ? "＋ 新建" : "新对话";
  document.getElementById("copilotCloseBtn").textContent = countryChatLayout() ? "×" : "关闭";
  const labels = { country: "国别空间", event: "国别空间 · 事件", topic: "项目空间", capability: "能力空间", resource: "资源导航", system: "系统状态" };
  const responsibilities = {
    country: "解释指标、来源与国别线索",
    event: "核对多源事实、时间线与证据缺口",
    topic: "澄清问题、整理材料并形成待确认草稿或产物",
    capability: "检查输入、解释运行结果与写回边界",
    resource: "定位正式来源、版本与引用路径",
    system: "说明覆盖、新鲜度与运行缺口",
  };
  const object = context.focus_label || (context.event_id ? `事件 #${context.event_id}` : context.research_case_id ? `项目 #${context.research_case_id}` : context.capability_run_id ? `运行 #${context.capability_run_id}` : context.country_iso3 && context.space === "country" ? context.country_iso3 : context.space === "country" ? "全球国别目录" : "当前页面");
  document.getElementById("assistantContextTitle").textContent = `${labels[context.space]} · AI 研究模式`;
  document.getElementById("assistantScopeChip").textContent = object;
  document.getElementById("assistantContextMeta").textContent = `${responsibilities[context.space]} · ${object} 上下文独立保存`;
  syncAssistantHistoryView();
  renderQaPresets();
  renderAssistantCapabilityPicker();
  void loadAssistantCapabilityPicker(nextScopeId);
  renderAssistantConversation();
  syncAssistantRunBar();
  const session = currentAssistantSession();
  if (session.activeRunId && session.status === "running" && !session.resuming) resumeAssistantRun(session);
  else if (session.conversationId && !session.hydrated) hydrateAssistantConversation(session);
}

function assistantCapabilityOptions(context = state.assistantContext) {
  if (!context || !["country", "event", "topic"].includes(context.space)) return [];
  const supportedScopes = context.space === "topic" ? ["topic", "research_case"] : [context.space];
  return (state.capabilityConfigs || []).filter((item) => {
    if (!item.catalog_key || !item.verified_usable || !supportedScopes.some((scope) => (item.supported_scopes || []).includes(scope))) return false;
    if (item.scope_type === "personal") return true;
    return context.space === "topic" && Number(item.research_case_id) === Number(context.research_case_id);
  });
}

function assistantCapabilityName(configId) {
  return (state.capabilityConfigs || []).find((item) => Number(item.id) === Number(configId))?.name || `能力 #${configId}`;
}

function assistantCurrentSurface(context = state.assistantContext) {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const pane = parts.at(-1);
  if (context?.focus_label) return context.focus_label;
  if (context?.space === "event") return state.eventDetail?.title || (context.event_id ? `事件 #${context.event_id}` : "事件动态");
  if (context?.space === "topic") {
    const paneLabels = { overview: "项目概览", tasks: "任务台", materials: "材料库", evidence: "证据审阅", outputs: "研究产物" };
    return `${state.topic?.title || `项目 #${context.research_case_id || "当前"}`} · ${paneLabels[pane] || "项目工作台"}`;
  }
  if (context?.space === "country") {
    const paneLabels = { overview: "国家概览", events: "事件动态", research: "前沿研究", reports: "智库报告", policies: "政策中心", data: "数据聚合" };
    const country = state.country?.iso3 === context.country_iso3 ? state.country : (state.catalog || []).find((item) => item.iso3 === context.country_iso3);
    return `${country?.name || context.country_iso3 || "全球国别目录"} · ${paneLabels[pane] || "国家概览"}`;
  }
  return "当前页面";
}

function assistantCapabilityRecommendation(item, context = state.assistantContext) {
  const surface = assistantCurrentSurface(context);
  const routeText = window.location.hash.toLowerCase();
  const topicText = `${state.topic?.title || ""} ${state.topic?.research_question || ""}`;
  const signal = `${surface} ${routeText} ${topicText}`;
  const key = item.catalog_key;
  let score = 0;
  let reason = "与当前研究范围相符";
  if (key === "country-indicator-alignment") {
    score = context?.space === "country" ? 90 : 58;
    if (/数据|指标|口径|比较|人口|贸易|gdp|data/.test(signal)) score += 35;
    reason = `适合核对${surface}中的定义、单位、时期和来源版本`;
  } else if (key === "policy-change-tracking") {
    score = context?.space === "country" ? 78 : context?.space === "event" ? 92 : 55;
    if (/政策|法规|变化|事件|动态|版本|policy|event/.test(signal)) score += 35;
    reason = `适合追踪${surface}的政策版本、时间线和证据缺口`;
  } else if (key === "team-weekly-research-deposit") {
    score = context?.space === "topic" ? 96 : 40;
    if (/周报|任务|材料|证据|产物|汇总|沉淀|tasks|materials|evidence|outputs/.test(signal)) score += 30;
    reason = `适合把${surface}的已确认进展整理成候选周报和资料清单`;
  } else if ((item.supported_scopes || []).includes(context?.space)) {
    score = 45;
  }
  return { item, score, reason };
}

function assistantCapabilityRecommendations(options, context = state.assistantContext) {
  return options
    .map((item) => assistantCapabilityRecommendation(item, context))
    .filter((entry) => entry.score > 0)
    .sort((left, right) => right.score - left.score || Number(left.item.id) - Number(right.item.id))
    .slice(0, 2);
}

function assistantPromptRecommendations() {
  const session = currentAssistantSession();
  const selected = session.capabilityConfigIds
    .map((id) => (state.capabilityConfigs || []).find((item) => Number(item.id) === Number(id)))
    .filter(Boolean);
  if (!selected.length) return [];
  const surface = assistantCurrentSurface();
  const materials = state.selectedMaterials || [];
  const prompts = [];
  if (selected.length > 1) {
    const names = selected.map((item) => `“${item.name}”`).join(" → ");
    prompts.push({ label: "组合执行", question: `请按选择顺序使用 ${names} 分析${surface}：先分别完成各 Skill 的检查，再合并共同结论；请保留来源、版本和证据缺口，不要把待确认内容写成正式结论。` });
  }
  selected.forEach((item) => {
    if (item.catalog_key === "country-indicator-alignment") {
      prompts.push({ label: "核对指标口径", question: `请使用“${item.name}”核对${surface}涉及的主要指标，逐项检查定义、单位、统计时期、来源和版本；不可比或缺失项请单列并说明原因。` });
    } else if (item.catalog_key === "policy-change-tracking") {
      prompts.push({ label: "追踪政策变化", question: `请使用“${item.name}”梳理${surface}的近期政策变化，按发布日期和版本差异整理时间线，并区分已复核事实、待确认线索与证据缺口。` });
    } else if (item.catalog_key === "team-weekly-research-deposit") {
      prompts.push({ label: "生成周报候选", question: `请使用“${item.name}”整理${surface}的本周候选周报，分为新增、更新、待确认和下一步，并为每项保留来源与证据状态。` });
    } else {
      prompts.push({ label: `使用${item.name}`, question: `请使用“${item.name}”分析${surface}，先说明本轮允许读取的输入和检查步骤，再给出带来源的候选结论与证据缺口。` });
    }
  });
  if (materials.length) {
    const titles = materials.slice(0, 2).map((item) => `《${item.title}》`).join("和");
    prompts.unshift({ label: "结合已选材料", question: `请按选择顺序使用${selected.map((item) => `“${item.name}”`).join("、")}，结合左侧已选的${materials.length}份材料（包括${titles}）分析${surface}；对比事实、数字和版本差异，并单列证据缺口。` });
  }
  const seen = new Set();
  return prompts.filter((item) => {
    if (seen.has(item.question)) return false;
    seen.add(item.question);
    return true;
  }).slice(0, 3);
}

function renderAssistantPromptGuide() {
  const guide = document.getElementById("assistantPromptGuide");
  const root = document.getElementById("assistantPromptChoices");
  const contextLabel = document.getElementById("assistantPromptContext");
  if (!guide || !root || !contextLabel || !state.assistantScopeId) return;
  const prompts = assistantPromptRecommendations();
  guide.hidden = !prompts.length;
  if (!prompts.length) {
    root.innerHTML = "";
    return;
  }
  contextLabel.textContent = `依据 ${assistantCurrentSurface()}${state.selectedMaterials?.length ? ` + ${state.selectedMaterials.length} 份材料` : ""}`;
  root.innerHTML = prompts.map((item) => `<button type="button" data-assistant-prompt="${escapeHtml(item.question)}"><b>${escapeHtml(item.label)}</b><span>${escapeHtml(item.question)}</span></button>`).join("");
}

function renderAssistantCapabilityPicker() {
  const root = document.getElementById("assistantCapabilityPicker");
  const choices = document.getElementById("assistantCapabilityChoices");
  const count = document.getElementById("assistantCapabilityCount");
  const recommendation = document.getElementById("assistantCapabilityRecommendation");
  if (!root || !choices || !count || !recommendation) return;
  const context = state.assistantContext;
  root.hidden = !context || !["country", "event", "topic"].includes(context.space);
  if (root.hidden) return;
  const options = assistantCapabilityOptions(context);
  const session = currentAssistantSession();
  const optionIds = new Set(options.map((item) => Number(item.id)));
  if (state.assistantCapabilitiesLoaded) session.capabilityConfigIds = session.capabilityConfigIds.filter((id) => optionIds.has(Number(id)));
  const selected = session.capabilityConfigIds;
  const recommended = assistantCapabilityRecommendations(options, context);
  const recommendationRank = new Map(recommended.map((entry, index) => [Number(entry.item.id), index + 1]));
  const sortedOptions = [...options].sort((left, right) => (recommendationRank.get(Number(left.id)) || 99) - (recommendationRank.get(Number(right.id)) || 99));
  recommendation.hidden = !recommended.length;
  recommendation.innerHTML = recommended.length ? `<span>根据当前页面推荐</span><b>${escapeHtml(assistantCurrentSurface(context))}</b><small>${recommended.map((entry, index) => `${index === 0 ? "首选" : "备选"}“${entry.item.name}”：${entry.reason}`).join("；")}</small>` : "";
  choices.innerHTML = sortedOptions.length ? sortedOptions.map((item) => {
    const order = selected.indexOf(Number(item.id));
    const rank = recommendationRank.get(Number(item.id));
    return `<label class="assistant-capability-choice${order >= 0 ? " is-selected" : ""}${rank ? " is-recommended" : ""}"><input type="checkbox" data-assistant-capability-config value="${item.id}"${order >= 0 ? " checked" : ""}><span><b>${escapeHtml(item.name)}${rank ? `<em>${rank === 1 ? "首选" : "推荐"}</em>` : ""}</b><small>${escapeHtml(capabilityCategoryLabel(item.category))} · v${escapeHtml(item.version)}</small></span><i data-assistant-capability-order>${order >= 0 ? order + 1 : "+"}</i></label>`;
  }).join("") : `<p class="assistant-capability-empty">当前空间没有已实跑验证的能力包。<a href="#/capabilities/mine">查看我的能力</a></p>`;
  count.textContent = selected.length ? `已选 ${selected.length} / 3` : countryChatLayout() ? "可选" : "未选择 · 点击展开";
  count.title = selected.map(assistantCapabilityName).join(" → ") || "选择本轮要使用的 Skill";
  renderAssistantPromptGuide();
}

async function loadAssistantCapabilityPicker(scopeId) {
  try {
    const payload = await apiFetch("/reader/capability-configs");
    if (scopeId !== state.assistantScopeId) return;
    state.capabilityConfigs = payload.items || [];
    state.assistantCapabilitiesLoaded = true;
    renderAssistantCapabilityPicker();
  } catch (_) {
    if (scopeId !== state.assistantScopeId) return;
    state.assistantCapabilitiesLoaded = true;
    renderAssistantCapabilityPicker();
  }
}

function syncAssistantCapabilitySelection(input) {
  const session = currentAssistantSession();
  const configId = Number(input.value);
  let selected = session.capabilityConfigIds.filter((id) => Number(id) !== configId);
  if (input.checked) {
    if (selected.length >= 3) {
      input.checked = false;
      showToast("本轮最多组合 3 项能力");
    } else {
      selected.push(configId);
    }
  }
  session.capabilityConfigIds = selected;
  persistAssistantSessions();
  renderAssistantCapabilityPicker();
}

async function hydrateAssistantConversation(session) {
  if (!session.conversationId || session.hydrated || session.status === "running") return;
  const scopeId = state.assistantScopeId;
  session.hydrated = true;
  try {
    const payload = await apiFetch(`/reader/assistant/conversations/${session.conversationId}`);
    const turns = payload.turns || [];
    const latestTurn = turns.at(-1);
    if (!session.referencesRestored && !session.references?.length) session.references = latestTurn?.references || [];
    session.referencesRestored = true;
    const latestRunning = latestTurn?.status === "running";
    let pendingMessage = null;
    session.messages = turns.flatMap((turn) => {
      const restoreRunningTurn = latestRunning && turn === latestTurn;
      const assistantMessage = restoreRunningTurn
        ? {
          id: assistantMessageId(), role: "assistant", runId: Number(turn.run_id), pending: true,
          startedAt: Date.parse(turn.created_at) || Date.now(),
          stages: [...assistantStagesFromStoredTurn(turn), { key: "restore", label: "正在恢复后台运行", state: "current" }],
          sections: [], citations: [], currentStage: "正在恢复后台运行",
        }
        : {
          id: assistantMessageId(), role: "assistant", runId: turn.run_id,
          stages: assistantStagesFromStoredTurn(turn),
          html: assistantStoredTurnHtml(turn),
        };
      if (restoreRunningTurn) pendingMessage = assistantMessage;
      return [{ id: assistantMessageId(), role: "user", html: escapeHtml(turn.question) + countrySentReferencesHtml(turn.references || []) }, assistantMessage];
    });
    session.activeRunId = pendingMessage ? Number(latestTurn.run_id) : null;
    session.status = pendingMessage ? "running" : (latestTurn?.status || "idle");
    session.lastQuestion = latestTurn?.question || "";
    session.lastSequence = 0;
    persistAssistantSessions();
    const isCurrent = state.assistantScopeId === scopeId && state.assistantSessions[scopeId] === session;
    if (!isCurrent) return;
    renderAssistantConversation();
    syncAssistantRunBar();
    if (pendingMessage) void resumeAssistantRun(session, pendingMessage);
  } catch (_) { session.hydrated = false; }
}

function assistantStoredTurnHtml(turn) {
  if (turn.status === "failed") {
    return `<div class="assistant-stored-state is-failed"><strong>本轮运行失败</strong><span>${escapeHtml(turn.error?.message || "未保存可展示的回答，请重新提问。")}</span></div>`;
  }
  if (turn.status === "running") {
    return `<div class="assistant-stored-state"><strong>本轮仍标记为运行中</strong><span>可以返回当前对话查看运行状态，或稍后重新打开。</span></div>`;
  }
  return assistantResponseHtml({ run_id: turn.run_id, runtime: turn.runtime, artifact: turn.artifact, proposed_actions: turn.artifact?.proposed_actions || [] }, { stages: assistantStagesFromStoredTurn(turn), runId: turn.run_id });
}

function assistantStagesFromStoredTurn(turn) {
  const plan = turn.plan || {};
  const stages = [{ key: "understand", label: "已理解问题并锁定研究范围", state: "done" }];
  if ((plan.public_steps || []).length || (plan.selected_capabilities || []).length) stages.push({ key: "plan", label: plan.scope_summary || "研究计划已完成", detail: (plan.public_steps || []).join(" · "), state: "done" });
  (turn.tool_trace || []).forEach((item) => stages.push({ key: `tool:${item.capability}`, label: `${assistantCapabilityLabel(item.capability)}已完成`, detail: `${item.fact_count || 0} 条事实 · ${item.evidence_count || 0} 份证据`, state: "done" }));
  if (turn.status === "succeeded") stages.push({ key: "validated", label: "回答与引用已完成校验", state: "done" });
  return stages;
}

function assistantPresets() {
  if (state.assistantContext?.space === "country" && !state.assistantContext.country_iso3) return [{ label: "全球覆盖", question: "当前全球国别目录和深度数据覆盖如何？" }, { label: "可用国家", question: "现在可以进入哪个深度国家？" }];
  if (state.assistantContext?.space === "country") return state.country?.iso3 === "COD" ? [{ label: "5 分钟速览", question: "请用已复核证据概括刚果（金）的宏观底账、近期事件和主要证据缺口。" }, { label: "近期变化", question: "刚果（金）当前哪些指标、政策或事件值得继续跟踪？" }, { label: "建立项目", question: "基于当前证据，最适合建立什么研究项目？" }] : [{ label: "基础国情", question: "请概括这个国家的首都、货币、官方语言、面积和当前可用资料。" }, { label: "证据边界", question: "除了基础国情档案，这个国家还缺哪些研究证据？" }, { label: "建立项目", question: "请根据我接下来的研究兴趣，帮我梳理一个项目标题和研究问题。" }];
  return {
    event: state.assistantContext?.event_id ? [{ label: "核验结论", question: "这条事件有哪些已确认事实、来源差异和证据缺口？" }, { label: "整理时间线", question: "请按时间精度整理这条事件的多来源时间线。" }, { label: "纳入项目", question: "请判断这条事件是否适合纳入项目，并整理为待确认的项目草稿。" }] : [{ label: "近期变化", question: "当前筛选范围内最近有哪些已复核事件和新增来源？" }, { label: "多源覆盖", question: "当前事件资料的官方、媒体与学术来源覆盖还缺什么？" }],
    topic: state.assistantContext?.research_case_id ? [{ label: "当前进展", question: "这个项目目前有哪些已确认材料、事件和证据缺口？" }, { label: "对比已选材料", question: "请比较已选项目材料的事实、数字与立场差异，并指出证据缺口。" }, { label: "产物准备", question: "项目周报或简报是否具备生成条件，仍有哪些限制？" }] : [{ label: "开始建项", action: "topic-onboarding" }, { label: "整理近期材料", question: "请基于近期已复核事件和已确认材料提出可持续跟踪的研究问题。" }, { label: "证据缺口", question: "当前可建立项目的证据覆盖还缺什么？" }],
    capability: [{ label: "输入检查", question: "当前 Skill 需要哪些输入，哪些条件已经就绪？" }, { label: "运行解释", question: "最近一次 Skill 运行的状态、产物、引用和限制是什么？" }, { label: "写回边界", question: "这个 Skill 的结果可以写回哪里，确认前需要检查什么？" }],
    resource: [{ label: "引用回源", question: "如何从当前结论回到对应来源、版本和证据定位？" }, { label: "覆盖缺口", question: "当前国别研究信源覆盖还缺什么？" }],
    system: [{ label: "就绪度", question: "当前 MVP readiness 的主要缺口是什么？" }],
  }[state.assistantContext?.space] || [];
}

function renderAssistantConversation() {
  const root = document.getElementById("assistantAnswer");
  if (!root) return;
  if (countryChatLayout()) renderCountryReferences();
  const messages = currentAssistantSession().messages;
  messages.forEach((message) => { if (!message.id) message.id = assistantMessageId(); });
  root.innerHTML = messages.length
    ? messages.map((message) => `<article class="chat-message is-${escapeHtml(message.role)}${message.pending ? " is-pending" : ""}" data-message-id="${escapeHtml(message.id)}"><span>${message.role === "user" ? "你" : "国别智枢"}</span><div class="chat-bubble">${message.pending ? assistantPendingHtml(message) : message.html}</div></article>`).join("")
    : `<div class="assistant-answer-empty" hidden></div>`;
  const presets = document.getElementById("copilotQaPresets");
  if (presets) presets.hidden = messages.length > 0 || !presets.innerHTML;
  const body = document.getElementById("copilotBody");
  window.requestAnimationFrame(() => { if (body && !state.assistantHistoryOpen) body.scrollTop = body.scrollHeight; });
}

function updateAssistantMessage(message, scroll = true) {
  const article = document.querySelector(`[data-message-id="${message.id}"]`);
  if (!article) { renderAssistantConversation(); return; }
  article.classList.toggle("is-pending", Boolean(message.pending));
  const bubble = article.querySelector(".chat-bubble");
  if (bubble) bubble.innerHTML = message.pending ? assistantPendingHtml(message) : message.html;
  if (scroll) {
    const body = document.getElementById("copilotBody");
    window.requestAnimationFrame(() => { if (body) body.scrollTop = body.scrollHeight; });
  }
}

function assistantDomId(value) { return String(value || "answer").replace(/[^a-zA-Z0-9_-]/g, "-"); }

function assistantOriginLabel(origin) {
  if (origin === "live_web") return "联网未复核";
  if (origin === "calculation") return "确定性计算";
  if (origin === "reference_registry") return "参考档案";
  return "库内已复核";
}

function assistantArtifactHtml(artifact, citationPrefix = "answer") {
  const citationItems = artifact.citations || [];
  const citations = new Map(citationItems.map((item, index) => [item.evidence_id, { item, number: index + 1 }]));
  const sections = artifact.sections || [];
  if (!sections.length) return "";
  const prefix = assistantDomId(citationPrefix);
  const answer = sections.map((section) => `<section class="answer-section"><h4>${escapeHtml(section.title)}</h4>${(section.claims || []).map((claim) => {
    const markers = [...new Set(claim.evidence_ids || [])].map((id) => {
      const citation = citations.get(id);
      if (!citation) return "";
      const target = `citation-${prefix}-${citation.number}`;
      const src = citation.item || {};
      const srcTitle = escapeHtml(src.title || src.source_name || "证据出处");
      const srcName = escapeHtml(src.source_name || "未知机构");
      const srcDate = escapeHtml(publishedLabel(src.published_at, src.published_at_precision));
      const srcOrigin = escapeHtml(assistantOriginLabel(src.origin));
      const srcUrl = escapeHtml(safeUrl(src.source_url));
      return `<button type="button" class="citation-marker" data-citation-ref="${target}" data-cite-title="${srcTitle}" data-cite-source="${srcName}" data-cite-date="${srcDate}" data-cite-origin="${srcOrigin}" data-cite-url="${srcUrl}" aria-label="查看引用 ${citation.number}">[${citation.number}]</button>`;
    }).join("");
    const origin = claim.origin || "database";
    const boundary = origin === "database" ? "" : `<small class="claim-origin is-${escapeHtml(origin)}">${escapeHtml(assistantOriginLabel(origin))}</small>`;
    return `<p class="answer-claim"><span>${escapeHtml(claim.text)}</span>${markers}${boundary}</p>`;
  }).join("")}</section>`).join("");
  const sources = citationItems.length ? `<details class="assistant-sources"><summary><span>来源与证据</span><small>${citationItems.length} 条</small></summary><ol>${citationItems.map((source, index) => {
    const number = index + 1;
    const url = safeUrl(source.source_url);
    const title = source.title || source.source_name || source.evidence_id;
    const date = publishedLabel(source.published_at, source.published_at_precision);
    const label = url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(title)}</a>` : `<span>${escapeHtml(title)}</span>`;
    return `<li id="citation-${prefix}-${number}"><b>${number}</b><div>${label}<small>${escapeHtml(source.source_name)} · ${escapeHtml(date)} · ${escapeHtml(assistantOriginLabel(source.origin))}</small></div></li>`;
  }).join("")}</ol></details>` : "";
  return `${answer}${sources}`;
}

function assistantTraceHtml(message = {}, open = false) {
  const stages = message.stages || [];
  if (!stages.length) return "";
  const done = stages.filter((stage) => stage.state === "done").length;
  return `<details class="agent-process"${open ? " open" : ""}><summary><span>${open ? "正在研究" : "研究过程"}</span><small>${done}/${stages.length} 步</small></summary><div class="assistant-trace">${stages.map((stage) => `<div class="assistant-trace-row is-${escapeHtml(stage.state || "done")}"><i aria-hidden="true"></i><span>${escapeHtml(stage.label)}</span>${stage.detail ? `<small>${escapeHtml(stage.detail)}</small>` : ""}</div>`).join("")}</div></details>`;
}

function assistantResponseHtml(payload, message = {}) {
  const artifact = payload.artifact || {};
  const content = assistantArtifactHtml(artifact, `run-${payload.run_id || message.id}`) || `<p class="assistant-inline-note">当前问题没有可用的已复核事实，你仍可以换一个角度继续问。</p>`;
  const limitations = (artifact.limitations || []).length ? `<details class="assistant-limitations"><summary>证据边界 <small>${artifact.limitations.length} 条</small></summary><ul>${artifact.limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>` : "";
  const actions = (payload.proposed_actions || artifact.proposed_actions || []).length ? `<div class="assistant-actions">${(payload.proposed_actions || artifact.proposed_actions).map((action) => `<button type="button" data-confirm-assistant-action="${escapeHtml(action.action_id)}" data-assistant-action-type="${escapeHtml(action.type)}" data-assistant-run-id="${payload.run_id}">${escapeHtml(action.label)} · ${action.type === "topic_draft_prepare" ? "先看草稿" : "需确认"}</button>`).join("")}</div>` : "";
  const online = payload.online_status?.state && payload.online_status.state !== "not_needed" ? ` · 联网 ${escapeHtml(payload.online_status.state)}` : "";
  const relatedQuestions = (payload.related_questions || artifact.related_questions || [])
    .map((question) => String(question || "").trim())
    .filter((question) => question.length <= 28 && !/factitem|evidence_id|toolresult|agentrun/i.test(question))
    .slice(0, 1);
  const related = relatedQuestions.length ? `<div class="assistant-followups"><span>可选追问</span>${relatedQuestions.map((question) => `<button type="button" data-qa-question="${escapeHtml(question)}">${escapeHtml(question)}</button>`).join("")}</div>` : "";
  return `${assistantTraceHtml(message, false)}${content}<p class="answer-meta">研究运行 #${payload.run_id} · 基于允许的证据范围${online}</p>${related}${actions}${limitations}`;
}

function assistantPendingHtml(message) {
  const partial = assistantArtifactHtml({ sections: message.sections || [], citations: message.citations || [] }, message.id);
  return `${assistantTraceHtml(message, true)}${partial || `<div class="assistant-waiting">已校验的内容会逐段显示在这里。</div>`}`;
}

function assistantCapabilityLabel(capability) {
  return {
    country_snapshot: "国别概览",
    policy_timeline: "政策时间线",
    event_evidence_compare: "事件证据核验",
    freshness_check: "数据新鲜度",
    live_search: "联网检索",
    compatibility: "兼容模式",
  }[capability] || String(capability || "研究能力").replaceAll("_", " ");
}

function assistantStageLabel(eventName, payload = {}) {
  if (eventName === "run.started") return "已理解问题并锁定当前研究范围";
  if (eventName === "plan.ready") return payload.scope_summary || `已形成 ${Number(payload.steps?.length || payload.capabilities?.length || 0)} 步研究计划`;
  if (eventName === "capability.started") return payload.capability === "live_search" ? "正在检索联网即时信息" : `正在执行${assistantCapabilityLabel(payload.capability)}`;
  if (eventName === "capability.completed") return payload.capability === "live_search" ? "联网信息已完成分区" : `${assistantCapabilityLabel(payload.capability)}已完成`;
  if (eventName === "evidence.validated") return payload.stage === "final_artifact" ? "全部事实—证据映射已通过校验" : "库内事实与证据映射已校验";
  if (eventName === "answer.section" || eventName === "answer.delta") return "正在流式呈现已验证答案";
  if (eventName === "heartbeat") return "受控研究仍在运行";
  return "正在运行受控研究";
}

function recordAssistantStage(message, eventName, payload = {}) {
  message.stages ||= [];
  message.stages.forEach((stage) => { if (stage.state === "current") stage.state = "done"; });
  const capabilityKey = payload.capability ? `:${payload.capability}` : payload.stage ? `:${payload.stage}` : "";
  const key = `${eventName.replace(".started", "").replace(".completed", "")}${capabilityKey}`;
  const existing = message.stages.find((stage) => stage.key === key);
  const label = assistantStageLabel(eventName, payload);
  const detail = payload.fact_count !== undefined ? `${payload.fact_count} 条事实 · ${payload.evidence_count || 0} 份证据` : "";
  const planDetail = eventName === "plan.ready" ? (payload.steps || []).slice(0, 3).join(" · ") : "";
  if (existing) {
    existing.label = label;
    existing.detail = detail || planDetail || existing.detail;
    existing.state = eventName.endsWith(".started") ? "current" : "done";
  } else if (eventName !== "heartbeat") {
    message.stages.push({ key, label, detail: detail || planDetail, state: eventName.endsWith(".started") || eventName === "run.started" || eventName === "answer.section" || eventName === "answer.delta" ? "current" : "done" });
  }
  const seen = new Set();
  message.stages = [...message.stages].reverse().filter((stage) => {
    const identity = stage.label;
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  }).reverse();
  message.currentStage = label;
}

function syncAssistantRunBar() {
  const bar = document.getElementById("assistantRunBar");
  const status = document.getElementById("assistantRunStatus");
  const elapsed = document.getElementById("assistantRunElapsed");
  const submit = document.querySelector("#assistantForm button[type='submit']");
  if (!bar || !status || !elapsed) return;
  const session = currentAssistantSession();
  const running = session.status === "running";
  bar.hidden = !running;
  const message = [...session.messages].reverse().find((item) => item.pending);
  status.textContent = message?.currentStage || "后台运行中";
  const updateElapsed = () => { elapsed.textContent = `${Math.max(0, Math.round((Date.now() - (message?.startedAt || Date.now())) / 1000))} 秒`; };
  window.clearInterval(state.assistantElapsedTimer);
  state.assistantElapsedTimer = null;
  if (running) { updateElapsed(); state.assistantElapsedTimer = window.setInterval(updateElapsed, 1000); }
  if (submit) { submit.disabled = running; submit.textContent = running ? "运行中" : "发送"; }
}

async function streamAssistantRequest(body, onEvent, path = "/reader/assistant/stream") {
  let response;
  try {
    const headers = { "Content-Type": "application/json", "Accept": "text/event-stream" };
    const readerKey = window.sessionStorage.getItem(READER_KEY_STORAGE_KEY);
    if (readerKey) headers["X-Reader-Key"] = readerKey;
    response = await fetch(`${API}${path}`, { method: "POST", headers, body: JSON.stringify(body) });
  } catch (_) { throw new ReaderApiError("流式连接未建立。", "stream_unreachable"); }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      detail = formatApiErrorDetail(payload.detail ?? payload.message ?? payload.error, detail);
    } catch (_) { /* keep status */ }
    throw new ReaderApiError(detail, response.status === 404 ? "endpoint_missing" : "stream_error", response.status);
  }
  if (!response.body) throw new ReaderApiError("当前浏览器不支持流式读取。", "stream_unsupported");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const parseBlock = (block) => {
    if (!block.trim()) return;
    if (block.trimStart().startsWith(":")) { onEvent("heartbeat", {}); return; }
    const eventName = block.split("\n").find((line) => line.startsWith("event: "))?.slice(7);
    const data = block.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
    if (eventName && data) onEvent(eventName, JSON.parse(data));
  };
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      parseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
    if (done) break;
  }
  if (buffer.trim()) parseBlock(buffer);
}

function completeAssistantMessage(session, message, payload) {
  session.conversationId = payload.conversation_id || session.conversationId;
  session.activeRunId = null;
  session.status = "succeeded";
  message.pending = false;
  message.runId = payload.run_id;
  (message.stages || []).forEach((stage) => { stage.state = "done"; });
  message.html = assistantResponseHtml(payload, message);
  message.currentStage = "回答与引用已完成";
  state.assistantBusy = false;
  persistAssistantSessions();
  updateAssistantMessage(message);
  document.getElementById("assistantStatus").textContent = "回答与引用已完成校验。";
  syncAssistantRunBar();
}

function failAssistantMessage(session, message, error) {
  session.activeRunId = null;
  session.status = "failed";
  message.pending = false;
  message.html = `<div class="assistant-inline-error"><strong>本轮没有完成</strong><span>${escapeHtml(error.message || String(error))}</span><small>可以直接修改问题后继续，不影响此前对话。</small></div>`;
  state.assistantBusy = false;
  persistAssistantSessions();
  updateAssistantMessage(message);
  document.getElementById("assistantStatus").textContent = "本次运行失败，未写入未完成答案。";
  syncAssistantRunBar();
}

function handleAssistantStreamEvent(session, message, eventName, envelope) {
  if (eventName === "heartbeat") {
    syncAssistantRunBar();
    return;
  }
  if (Number(envelope.sequence || 0) <= session.lastSequence) return;
  session.lastSequence = Number(envelope.sequence || 0);
  const payload = envelope.payload || {};
  if (eventName === "run.started") {
    session.conversationId = envelope.conversation_id;
    session.activeRunId = envelope.run_id;
    message.runId = envelope.run_id;
  }
  if (eventName === "answer.section") {
    message.sections ||= [];
    message.sections[Number(payload.index || 0)] = payload.section;
    const citationMap = new Map((message.citations || []).map((item) => [item.evidence_id, item]));
    (payload.citations || []).forEach((item) => citationMap.set(item.evidence_id, item));
    message.citations = [...citationMap.values()];
  }
  if (eventName === "answer.delta") {
    const section = message.sections?.[Number(payload.index || 0)];
    const claim = section?.claims?.[Number(payload.claim_index || 0)];
    if (claim) claim.text = `${claim.text || ""}${payload.delta || ""}`;
  }
  if ((!eventName.startsWith("run.") || eventName === "run.started") && eventName !== "answer.delta") recordAssistantStage(message, eventName, payload);
  persistAssistantSessions();
  document.getElementById("assistantStatus").textContent = assistantStageLabel(eventName, payload);
  if (eventName === "run.completed") { completeAssistantMessage(session, message, payload.response); return; }
  if (eventName === "run.failed") { failAssistantMessage(session, message, new Error(payload.message || "AI 运行失败")); return; }
  updateAssistantMessage(message);
  syncAssistantRunBar();
}

async function resumeAssistantRun(session, message = null) {
  if (!session.activeRunId || session.resuming) return;
  session.resuming = true;
  const target = message || [...session.messages].reverse().find((item) => item.pending) || { id: assistantMessageId(), role: "assistant", pending: true, runId: session.activeRunId, startedAt: Date.now(), stages: [], sections: [], citations: [] };
  if (!session.messages.includes(target)) session.messages.push(target);
  recordAssistantStage(target, "run.started", {});
  target.currentStage = "正在恢复后台运行";
  renderAssistantConversation();
  try {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      const statusPayload = await apiFetch(`/reader/assistant/runs/${session.activeRunId}`);
      if (statusPayload.status === "succeeded" && statusPayload.response) { completeAssistantMessage(session, target, statusPayload.response); return; }
      if (statusPayload.status === "failed") {
        const error = new Error(statusPayload.error?.message || "AI 运行失败");
        error.confirmedFailure = true;
        throw error;
      }
      if (statusPayload.progress?.payload) {
        recordAssistantStage(target, statusPayload.progress.event, statusPayload.progress.payload);
      } else {
        target.currentStage = "后台运行中";
      }
      updateAssistantMessage(target, false);
      syncAssistantRunBar();
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
    target.currentStage = "后台运行仍在继续；稍后重新打开当前页面可恢复";
    updateAssistantMessage(target, false);
    persistAssistantSessions();
    syncAssistantRunBar();
  } catch (error) {
    if (error.confirmedFailure) {
      failAssistantMessage(session, target, error);
    } else {
      target.currentStage = "暂时无法读取后台状态；稍后重新打开当前页面可恢复";
      updateAssistantMessage(target, false);
      persistAssistantSessions();
      syncAssistantRunBar();
    }
  } finally {
    session.resuming = false;
  }
}

async function askAssistant(question) {
  const session = currentAssistantSession();
  if (session.status === "running" || state.assistantBusy) { showToast("已有一项受控研究正在后台运行"); return; }
  state.assistantBusy = true;
  session.status = "running";
  session.lastQuestion = question;
  session.lastSequence = 0;
  const message = { id: assistantMessageId(), role: "assistant", pending: true, runId: null, startedAt: Date.now(), stages: [], sections: [], citations: [] };
  let effectiveQuestion = question;
  if (!countryChatLayout() && state.selectedMaterials?.length > 0 && !question.startsWith("[参考已选材料")) {
    const materialSummary = state.selectedMaterials.map((m) => `《${m.title}》（${m.sourceName}）`).join("、");
    effectiveQuestion = `[参考已选材料：${materialSummary}]\n${question}`;
  }
  const selectedCapabilityIds = session.capabilityConfigIds.filter((id) => assistantCapabilityOptions().some((item) => Number(item.id) === Number(id)));
  const selectedCapabilityNames = selectedCapabilityIds.map(assistantCapabilityName);
  const selectedCapabilityNote = selectedCapabilityNames.length ? `<small class="assistant-selected-capabilities">本轮能力：${selectedCapabilityNames.map(escapeHtml).join(" → ")}</small>` : "";
  session.messages.push({ id: assistantMessageId(), role: "user", html: `${escapeHtml(question)}${selectedCapabilityNote}${countryChatLayout() ? countrySentReferencesHtml(session.references || []) : ""}` });
  session.messages.push(message);
  persistAssistantSessions();
  recordAssistantStage(message, "run.started", {});
  renderAssistantConversation();
  syncAssistantRunBar();
  const requestBody = { references: countryChatLayout() ? (session.references || []).map(({type,id,snapshot_id,field,quote,start})=>({type,id,snapshot_id,field,quote,start})) : [], conversation_id: session.conversationId, context: { ...state.assistantContext, capability_config_ids: selectedCapabilityIds }, question: effectiveQuestion, online_mode: document.getElementById("assistantOnlineMode").value, answer_mode: "research_chat" };
  let streamStarted = false;
  try {
    await streamAssistantRequest(requestBody, (eventName, envelope) => {
      if (eventName === "run.started") streamStarted = true;
      handleAssistantStreamEvent(session, message, eventName, envelope);
    });
  } catch (error) {
    if (streamStarted && session.activeRunId) {
      message.currentStage = "流式连接中断，正在读取后台运行状态";
      recordAssistantStage(message, "run.started", {});
      await resumeAssistantRun(session, message);
      return;
    }
    try {
      recordAssistantStage(message, "capability.started", { capability: "compatibility", description: "流式接口不可用，正在切换兼容模式" });
      updateAssistantMessage(message);
      const payload = await apiFetch("/reader/assistant/ask", { method: "POST", body: JSON.stringify(requestBody) });
      completeAssistantMessage(session, message, payload);
      showToast("流式接口不可用，本轮已使用兼容模式完成");
    } catch (fallbackError) {
      failAssistantMessage(session, message, fallbackError);
    }
  }
}

function renderEventFilters(rows, reports) {
  const root = document.getElementById("eventFilters");
  if (!root) return;
  const types = [...new Set(rows.map((item) => item.event_type).filter(Boolean))].sort();
  root.innerHTML = `<label class="event-query-field">事件名 / 关键词<input id="eventQueryFilter" type="search" value="${escapeHtml(state.eventFilterQuery)}" placeholder="例如：关键矿产、冲突、矿区"></label><label class="event-country-field">国家<input id="eventCountryFilter" type="search" list="countrySearchOptions" value="${escapeHtml(state.eventFilterCountry)}" placeholder="全部国家 / 搜索国家" autocomplete="off"></label><label>时间<select id="eventWindowFilter"><option value="all"${state.eventFilterWindow === "all" ? " selected" : ""}>全部日期</option><option value="7"${state.eventFilterWindow === "7" ? " selected" : ""}>近 7 天</option><option value="30"${state.eventFilterWindow === "30" ? " selected" : ""}>近 30 天</option></select></label><label>来源 / 机构<input id="eventSourceNameFilter" type="search" value="${escapeHtml(state.eventFilterSource)}" placeholder="来源名称"></label>${state.eventFilterKind === "event" ? `<label>事件类型<select id="eventTypeFilter"><option value="">全部类型</option>${types.map((item) => `<option value="${escapeHtml(item)}"${state.eventFilterType === item ? " selected" : ""}>${escapeHtml(displayEnum(EVENT_TYPE_LABELS, item))}</option>`).join("")}</select></label><label>来源数<select id="eventSourceFilter"><option value=""${state.eventFilterMinSources === "" ? " selected" : ""}>不限</option><option value="2"${state.eventFilterMinSources === "2" ? " selected" : ""}>多源（≥2）</option></select></label>` : `<span class="filter-note">按来源声明日期排序；未知日期不补造。</span>`}`;
}

function eventEntryKey(kind, item) {
  if (kind === "event") return `event:${item.id}`;
  if (item.document_version_id !== null && item.document_version_id !== undefined && item.document_version_id !== "") {
    return `report:version-${item.document_version_id}`;
  }
  const sourceIdentity = item.source_key || item.canonical_url || item.source_url || item.discovery_url || item.title || "candidate";
  return `report:source-${encodeURIComponent(sourceIdentity)}`;
}

function renderEventPreview(entries) {
  const root = document.getElementById("eventPreview");
  if (!root) return;
  if (!entries.length) { root.innerHTML = emptyState("当前筛选条件下没有可预览内容"); return; }
  let entry = entries.find(({ kind, item }) => eventEntryKey(kind, item) === state.eventPreviewKey);
  if (!entry) {
    entry = entries[0];
    state.eventPreviewKey = eventEntryKey(entry.kind, entry.item);
  }
  document.querySelectorAll("[data-event-preview-key]").forEach((row) => row.classList.toggle("is-selected", row.dataset.eventPreviewKey === state.eventPreviewKey));
  const item = entry.item;
  if (entry.kind === "event") {
    const sourceCount = Number(item.source_count || 0);
    const gaps = sourceCount < 2 ? "独立确认来源少于 2 个，需继续补证。" : "已达到多来源基础门槛，仍需在详情页核对口径差异。";
    root.innerHTML = `<header><span class="feed-status is-reviewed">已复核事件</span><small>${escapeHtml(item.start_at ? localDate(item.start_at) : "日期未知")}</small></header><div class="event-preview-body"><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.summary || "当前事件尚无独立摘要；请进入详情查看已登记事实和来源。")}</p><dl><div><dt>事件类型</dt><dd>${escapeHtml(displayEnum(EVENT_TYPE_LABELS, item.event_type))}</dd></div><div><dt>确认来源</dt><dd>${sourceCount} 个</dd></div><div><dt>证据提示</dt><dd>${escapeHtml(gaps)}</dd></div></dl></div><div class="event-preview-actions"><button type="button" class="primary-btn" data-event-id="${item.id}">打开证据工作台</button><button type="button" class="ghost-btn" data-add-event-topic="${item.id}" data-link-label="${escapeHtml(item.title)}">加入项目</button></div>`;
    return;
  }
  const country = item.countries?.[0]; const linkedCount = item.linked_events?.length || 0;
  const hasArchivedVersion = item.document_version_id !== null && item.document_version_id !== undefined && item.document_version_id !== "";
  const statusLabel = hasArchivedVersion ? `确认材料 · ${linkedCount ? "已关联事件" : "待聚合"}` : "来源已核验 · 待平台聚合";
  const description = item.abstract || (hasArchivedVersion
    ? "该材料已确认关联国家，但尚未被人工复核为独立事件。"
    : "该报道的来源与链接已经核验，平台尚未完成归档和事件聚合；当前不能作为事件结论。");
  const actions = hasArchivedVersion
    ? `<button type="button" class="primary-btn" data-material-version="${item.document_version_id}">查看版本与来源</button>${sourceActionHtml(item)}`
    : (sourceActionHtml(item) || `<span class="disabled-note">原始报道链接待恢复</span>`);
  root.innerHTML = `<header><span class="feed-status is-report">${escapeHtml(statusLabel)}</span><small>${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))}</small></header><div class="event-preview-body"><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(description)}</p><dl><div><dt>来源</dt><dd>${escapeHtml(item.source_name || "来源未登记")}</dd></div><div><dt>国家</dt><dd>${escapeHtml(country ? `${country.name || country.iso3} · ${country.iso3}` : "未关联")}</dd></div><div><dt>证据提示</dt><dd>${linkedCount ? `已关联 ${linkedCount} 个事件，可进入详情核对。` : "尚未聚合为事件，不能作为事件结论。"}</dd></div></dl></div><div class="event-preview-actions">${actions}</div>`;
}

function renderEventHall(rows, reports = state.eventReports) {
  const listRoot = document.getElementById("eventList");
  const withinWindow = (date) => {
    if (state.eventFilterWindow === "all") return true;
    if (!date) return false;
    const time = new Date(date).getTime();
    return Number.isFinite(time) && Date.now() - time <= Number(state.eventFilterWindow) * 86400000;
  };
  const query = state.eventFilterQuery.trim().toLowerCase();
  const sourceQuery = state.eventFilterSource.trim().toLowerCase();
  const filteredEvents = rows.filter((item) => withinWindow(item.start_at) && (!query || `${item.title || ""} ${item.summary || ""}`.toLowerCase().includes(query)) && (!state.eventFilterType || item.event_type === state.eventFilterType) && (!state.eventFilterMinSources || Number(item.source_count || 0) >= Number(state.eventFilterMinSources)));
  const filteredReports = reports.filter((item) => withinWindow(item.published_at) && (!query || `${item.title || ""} ${item.abstract || ""}`.toLowerCase().includes(query)) && (!sourceQuery || `${item.source_name || ""}`.toLowerCase().includes(sourceQuery)));
  const entries = (state.eventFilterKind === "event"
    ? filteredEvents.map((item) => ({ kind: "event", date: item.start_at, item }))
    : filteredReports.map((item) => ({ kind: "report", date: item.published_at, item })))
    .sort((left, right) => {
      if (!left.date && !right.date) return 0;
      if (!left.date) return 1;
      if (!right.date) return -1;
      return String(right.date).localeCompare(String(left.date));
    });
  renderEventFilters(rows, reports);
  document.querySelectorAll("[data-event-layer]").forEach((button) => {
    const active = button.dataset.eventLayer === state.eventFilterKind;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const summary = document.getElementById("eventFeedSummary");
  const sourceNames = [...new Set(filteredReports.map((item) => item.source_name).filter(Boolean))];
  const sourceTypes = [...new Set(filteredReports.map((item) => item.source_type || item.document_type).filter(Boolean))];
  const archivedReportCount = filteredReports.filter((item) => item.document_version_id !== null && item.document_version_id !== undefined && item.document_version_id !== "").length;
  const candidateReportCount = filteredReports.length - archivedReportCount;
  const diversity = `当前材料池覆盖 ${sourceNames.length} 个来源机构${sourceTypes.length ? `、${sourceTypes.length} 类来源` : ""}；${archivedReportCount} 条已归档可回到固定版本${candidateReportCount ? `，${candidateReportCount} 条来源已核验候选可打开原始报道` : ""}。`;
  if (summary) summary.innerHTML = state.eventFilterKind === "event"
    ? `<strong>${entries.length} 条已复核事件</strong><span>事件已完成人工复核，所列来源材料均已确认关联。</span><em>${escapeHtml(diversity)}</em>`
    : `<strong>${entries.length} 条近期报道与材料</strong><span>平台负责归档、聚类和复核；来源已核验候选不会交给科研人员清洗。</span><em>${escapeHtml(diversity)}</em>`;
  listRoot.innerHTML = entries.length ? entries.map(({ kind, item }) => {
    const key = eventEntryKey(kind, item);
    if (kind === "event") return `<article class="event-compact-row is-reviewed${key === state.eventPreviewKey ? " is-selected" : ""}" data-event-preview-key="${escapeHtml(key)}" ${projectEventAttributes(item)}><button type="button" class="event-row-select" data-preview-event="${item.id}"><span class="feed-status is-reviewed">已复核事件</span><time>${escapeHtml(item.start_at ? localDate(item.start_at) : "日期未知")}</time><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(displayEnum(EVENT_TYPE_LABELS, item.event_type))} · ${Number(item.source_count || 0)} 个确认来源</small></button><div class="event-row-actions"><button type="button" class="text-link" data-event-id="${item.id}">打开</button></div></article>`;
    const country = item.countries?.[0]; const linkedCount = item.linked_events?.length || 0;
    const hasArchivedVersion = item.document_version_id !== null && item.document_version_id !== undefined && item.document_version_id !== "";
    const statusLabel = hasArchivedVersion ? `确认材料 · ${linkedCount ? "已关联" : "待聚合"}` : "来源已核验 · 待平台聚合";
    const actions = hasArchivedVersion
      ? `<button type="button" class="text-link" data-material-version="${item.document_version_id}">查看版本</button>`
      : (sourceActionHtml(item) || `<span class="disabled-note">原链接待恢复</span>`);
    return `<article class="event-compact-row is-report${key === state.eventPreviewKey ? " is-selected" : ""}" data-event-preview-key="${escapeHtml(key)}" ${hasArchivedVersion ? projectMaterialAttributes(item) : ""}><button type="button" class="event-row-select" data-preview-report-key="${escapeHtml(key)}"><span class="feed-status is-report">${escapeHtml(statusLabel)}</span><time>${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))}</time><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.source_name)}${country ? ` · ${escapeHtml(country.iso3)}` : ""}</small></button><div class="event-row-actions">${actions}</div></article>`;
  }).join("") : emptyState("当前筛选条件下暂无动态");
  renderEventPreview(entries);
}

function renderEventWorkspace(event, evidence, pane) {
  const toolbar = document.getElementById("eventToolbar");
  const tabs = document.getElementById("eventTabs");
  const detailRoot = document.getElementById("eventDetail");
  const matrixConfig = state.capabilities.find((item) => item.slug === "event-evidence-matrix")?.configs?.[0];
  const countryIso3 = event.country?.iso3 || state.eventFilterCountry || "COD";
  const eventBase = `#/countries/${countryIso3}/events/${event.id}`;
  toolbar.innerHTML = `<div class="toolbar-left"><a href="#/countries/global-events">← 返回全球动态</a><div class="toolbar-divider"></div><div class="country-current-badge"><strong>${escapeHtml(event.title)}</strong><small>${Number(evidence.source_count || 0)} 个确认来源</small></div></div><div class="toolbar-center"><p class="object-eyebrow">国别空间 · 事件核验台 · 不取平均、不判定真伪</p></div><div class="toolbar-right"><span class="country-freshness">事件日期 ${escapeHtml(event.start_at ? localDate(event.start_at) : "未知")}</span><button type="button" class="primary-btn compact" data-add-event-topic="${event.id}" data-link-label="${escapeHtml(event.title)}">加入项目</button>${matrixConfig?.can_run ? `<button type="button" class="ghost-btn" data-run-capability="${matrixConfig.id}" data-case-id="${matrixConfig.config?.research_case_id || ""}">生成证据矩阵</button>` : ""}${copilotToggleHtml()}</div>`;
  tabs.innerHTML = workspaceTabsHtml([
    { id: "overview", label: "事件概览", href: eventBase },
    { id: "timeline", label: "时间线", href: `${eventBase}/timeline` },
    { id: "evidence", label: "证据对比", href: `${eventBase}/evidence` },
    { id: "related", label: "关联与缺口", href: `${eventBase}/related` },
  ], pane);
  const seriesTimeline = (event.series_timeline || []).map((item) => `<article class="series-event${item.current ? " is-current" : ""}"><time>${escapeHtml(item.start_at ? localDate(item.start_at) : "日期未知")}</time><div><strong>${escapeHtml(item.title)}</strong><span>${Number(item.source_count || 0)} 个确认来源${item.current ? " · 当前事件" : ""}</span></div>${item.current ? "" : `<button type="button" class="text-link" data-event-id="${item.id}">打开</button>`}</article>`).join("");
  const relations = (event.relations || []).map((item) => `<article class="linked-object-row"><a href="#/events/${item.event.id}"><b>${escapeHtml(item.event.title)}</b><span>${escapeHtml(displayEnum(RELATION_TYPE_LABELS, item.relation_type, "已登记关系"))} · ${escapeHtml(item.direction === "outgoing" ? "指向" : "来自")}</span></a></article>`).join("");
  if (pane === "timeline") {
    const sources = evidence.sources || [];
    const typeOptions = [...new Set(sources.map((item) => item.document?.document_type || item.document?.source_type).filter(Boolean))].sort();
    const cards = sources.map((source) => materialCard(source.document || {}, { addable: true }));
    detailRoot.innerHTML = `<section class="report-section"><h3>事件编年表</h3>${seriesTimeline ? `<div class="series-timeline">${seriesTimeline}</div>` : emptyState("当前没有登记同系列事件")}<p class="method-note">${escapeHtml(event.series_method_note || "")}</p></section><section class="report-section"><div class="section-heading"><div><h3>多来源材料</h3><p>逐一打开固定版本与原文定位。</p></div><label>材料类型<select id="eventMaterialTypeFilter"><option value="">全部</option>${typeOptions.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(displayEnum({ report: "报告", news: "新闻", policy: "政策文件", article: "文章", dataset: "数据集" }, item))}</option>`).join("")}</select></label></div><div id="eventMaterialResults">${cards.length ? `<div class="item-list">${cards.join("")}</div>` : emptyState("暂无已确认的时间线材料")}</div></section>`;
    return;
  }
  if (pane === "evidence") {
    detailRoot.innerHTML = `<p class="method-note">同一比较键下并列各来源原话或数值。系统不取平均，也不判定哪一条更真实。</p>${evidenceMatrixHtml(evidence.comparison_groups, evidence.method_note, evidence.sources)}`;
    return;
  }
  if (pane === "related") {
    const gaps = [];
    if (Number(evidence.source_count || 0) < 2) gaps.push("独立确认来源少于 2 个。");
    if (!(event.relations || []).length) gaps.push("尚未登记关联事件；系统不会自动推断因果。");
    if (!(evidence.comparison_groups || []).length) gaps.push("尚无可并列的事实、数字或立场比较组。");
    detailRoot.innerHTML = `<section class="report-section"><h3>已登记关联</h3>${relations ? `<div class="linked-object-list">${relations}</div>` : emptyState("当前没有登记事件关系")}</section><section class="report-section"><h3>证据缺口</h3>${gaps.length ? `<ul class="evidence-gap-list">${gaps.map((gap) => `<li>${escapeHtml(gap)}</li>`).join("")}</ul>` : `<p>当前基础证据链完整；仍需持续复核新材料。</p>`}</section>`;
    return;
  }
  const entities = (event.entities || []).map((item) => `<span class="entity-chip"><b>${escapeHtml(item.name)}</b> · ${escapeHtml(displayEnum(ENTITY_ROLE_LABELS, item.role, "相关主体"))}</span>`).join("");
  detailRoot.innerHTML = `${event.country?.iso3 ? `<div class="object-links"><a href="#/countries/${escapeHtml(event.country.iso3)}">进入${escapeHtml(event.country.name)}国别空间</a></div>` : ""}<section class="report-section"><h3>事件概览</h3><p>${escapeHtml(event.summary || "当前事件尚无独立摘要。")}</p><div class="event-overview-meta"><span><b>类型</b>${escapeHtml(displayEnum(EVENT_TYPE_LABELS, event.event_type))}</span><span><b>时间</b>${escapeHtml(event.start_at ? localDate(event.start_at) : "日期未知")}</span><span><b>时间精度</b>${escapeHtml(displayEnum(DATE_PRECISION_LABELS, event.date_precision))}</span><span><b>确认来源</b>${Number(evidence.source_count || 0)}</span></div>${entities ? `<div class="entity-chip-list">${entities}</div>` : emptyState("尚未登记人物、机构、地点或政策实体")}<details class="developer-details"><summary>技术信息</summary><pre>${escapeHtml(JSON.stringify({ event_type: event.event_type, date_precision: event.date_precision }, null, 2))}</pre></details></section><p class="method-note">未登记关系时不推断因果；来源主张不会被平均或覆盖。</p>`;
}

async function loadEvents(eventId = null, pane = "overview", serial = state.routeSerial) {
  state.activeEventId = eventId ? Number(eventId) : null;
  state.eventPane = pane;
  const listRoot = document.getElementById("eventList");
  const detailRoot = document.getElementById("eventDetail");
  const hall = document.getElementById("eventHall");
  const workspace = document.getElementById("eventWorkspace");
  const showingDetail = Boolean(eventId);
  listRoot.hidden = showingDetail;
  if (hall) hall.hidden = showingDetail;
  if (workspace) workspace.hidden = !showingDetail;
  detailRoot.hidden = !showingDetail;
  const activeRoot = showingDetail ? detailRoot : listRoot;
  activeRoot.innerHTML = `<div class="empty-state">${showingDetail ? "正在读取事件时间线与证据…" : "正在读取已复核事件…"}</div>`;
  try {
    if (!showingDetail) {
      const params = new URLSearchParams();
      if (state.eventFilterCountry) params.set("country_iso3", state.eventFilterCountry);
      if (state.eventFilterQuery.trim()) params.set("q", state.eventFilterQuery.trim());
      if (state.eventFilterType) params.set("event_type", state.eventFilterType);
      if (state.eventFilterWindow !== "all") params.set("date_from", new Date(Date.now() - Number(state.eventFilterWindow) * 86400000).toISOString());
      const query = params.toString() ? `?${params}` : "";
      const [rows, reports] = await Promise.all([apiFetch(`/reader/events${query}`), apiFetch(`/reader/event-reports${query}`)]);
      if (serial !== state.routeSerial) return;
      state.eventRows = rows;
      state.eventReports = reports;
      state.eventDetail = null;
      state.eventEvidence = null;
      renderEventHall(rows, reports);
      renderSidebarContext();
      return;
    }
    const [event, evidence] = await Promise.all([apiFetch(`/reader/events/${eventId}`), apiFetch(`/reader/events/${eventId}/evidence`)]);
    if (serial !== state.routeSerial) return;
    state.eventDetail = event;
    state.eventEvidence = evidence;
    if (!state.capabilities.length) {
      try { state.capabilities = await apiFetch("/reader/capabilities"); } catch (_) { state.capabilities = []; }
    }
    renderEventWorkspace(event, evidence, pane);
    renderSidebarContext();
  } catch (error) { activeRoot.innerHTML = errorState(error.message); }
}

function renderTopicHall(rows) {
  const listRoot = document.getElementById("topicList");
  const ranked = [...rows].sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at));
  listRoot.innerHTML = ranked.length ? ranked.map((item) => {
    const gaps = [];
    if (!item.research_question) gaps.push("缺研究问题");
    if (!Number(item.document_count)) gaps.push("缺确认材料");
    if (!Number(item.event_count)) gaps.push("缺已核验事件");
    if (!item.latest_run) gaps.push("尚无产物");
    const latest = item.latest_run ? `最新产物：${runStatusLabel(item.latest_run.status)} · ${localDate(item.latest_run.finished_at || item.latest_run.created_at)}` : "最新产物：尚无";
    return `<article class="topic-project-card"><header><div><span>${escapeHtml(scopeLabel(item.scope))}</span><h2>${escapeHtml(item.title)}</h2></div><time>${escapeHtml(localDate(item.updated_at))}</time></header><p class="topic-research-question">${escapeHtml(item.research_question || "尚未登记研究问题")}</p><div class="topic-project-metrics"><span><b>${Number(item.document_count || 0)}</b> 材料</span><span><b>${Number(item.event_count || 0)}</b> 事件</span><span><b>${Number(item.pending_review_count || 0)}</b> 待审</span><span><b>${Number(item.formal_output_count || 0)}</b> 成果</span></div><div class="topic-gap-line"><b>${gaps.length ? "待补环节" : "基础闭环已建立"}</b><span>${escapeHtml(gaps.length ? gaps.join(" · ") : "范围与证据链可进入任务审阅")}</span></div><footer><span>${escapeHtml(latest)}</span><div class="project-card-actions">${item.can_delete ? `<button type="button" class="ghost-btn compact" data-delete-project="${Number(item.id)}">删除项目</button>` : ""}<a class="primary-btn compact link-button" href="#/projects/${item.id}/tasks">继续研究</a></div></footer></article>`;
  }).join("") : emptyState("暂无项目；选择研究方向或自主填写主题，创建后即可预览提示词并开始研究。");
}

async function collapseProjectCardElement(element) {
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches && element.animate) {
    const height=element.getBoundingClientRect().height;
    element.style.overflow='hidden';
    await element.animate([
      {height:`${height}px`,opacity:1,transform:'translateY(0) scale(1)'},
      {height:`${height}px`,opacity:0,transform:'translateY(-6px) scale(.985)',offset:.45},
      {height:'0px',opacity:0,paddingTop:'0px',paddingBottom:'0px',marginTop:'0px',marginBottom:'0px',borderWidth:'0px'}
    ],{duration:280,easing:'cubic-bezier(.22,1,.36,1)',fill:'forwards'}).finished.catch(()=>{});
  }
  element.remove();
}

document.addEventListener("click", async event => {
  const cancel = event.target.closest("[data-cancel-project-delete]");
  if (cancel) {
    const section=cancel.closest("[data-project-delete-confirm]");
    const trigger=section.closest('.topic-project-card').querySelector('[data-delete-project]');
    await collapseProjectCardElement(section);
    trigger?.focus();
    return;
  }
  const trigger = event.target.closest("[data-delete-project]");
  if (trigger) {
    const card = trigger.closest(".topic-project-card");
    if (card.querySelector("[data-project-delete-confirm]")) return;
    card.insertAdjacentHTML("beforeend", `<section data-project-delete-confirm><p>删除后项目将从列表移除，所有成员无法再打开。项目记录保留，国别资料不受影响。</p><button type="button" class="ghost-btn compact" data-cancel-project-delete>取消</button><button type="button" class="ghost-btn compact" data-confirm-project-delete="${Number(trigger.dataset.deleteProject)}">确认删除项目</button><p role="status"></p></section>`);
    card.querySelector("[data-cancel-project-delete]").focus();
    return;
  }
  const confirm = event.target.closest("[data-confirm-project-delete]");
  if (!confirm) return;
  const section = confirm.closest("[data-project-delete-confirm]");
  const cancelButton = section.querySelector("[data-cancel-project-delete]");
  confirm.disabled = true; cancelButton.disabled = true;
  confirm.classList.add("is-deleting"); confirm.textContent="正在删除…";
  const id = Number(confirm.dataset.confirmProjectDelete);
  try {
    await apiFetch(`/reader/research-cases/${id}`, {method:"DELETE"});
    state.topics = state.topics.filter(item => Number(item.id) !== id);
    await collapseProjectCardElement(confirm.closest(".topic-project-card"));
    if (!state.topics.length) renderTopicHall([]);
    showToast("项目已删除，记录已保留。");
  } catch (error) {
    section.querySelector('[role="status"]').textContent = error.message;
    confirm.disabled = false; cancelButton.disabled = false;
    confirm.classList.remove("is-deleting"); confirm.textContent="确认删除项目";
  }
});

function renderTopicFrontiers(payload) {
  const root = document.getElementById("topicFrontierList");
  if (!root) return;
  state.topicFrontiers = payload?.items || [];
  const coverage = document.getElementById("topicFrontierCoverage");
  const trends = payload.trends || {};
  coverage.textContent = `${trends.from || ""} 至 ${trends.to || ""} · ${state.topicFrontiers.length} 个可选方向 · 收录 ${Number(trends.total || 0)} 篇 · ${Number(trends.unclassified_count || 0)} 篇未登记主题标签。${(payload.limitations || []).join(" ")}`;
  root.innerHTML = state.topicFrontiers.length ? state.topicFrontiers.map(item => `<button type="button" class="frontier-title-card" data-open-frontier="${escapeHtml(item.suggestion_id)}"><span class="frontier-title-meta">${escapeHtml(item.title)} · 匹配 ${Number(item.count)} 篇</span><strong>${escapeHtml(item.direction_title || item.title)}</strong><span class="frontier-title-question">${escapeHtml(item.research_question)}</span><span class="frontier-title-more">查看方向 <span aria-hidden="true">↗</span></span></button>`).join("") : emptyState("当前筛选下没有已登记学术主题；不会生成虚构热点。可调整国家、关键词或时间范围。");
}

function openFrontierDetail(suggestionId) {
  const item = state.topicFrontiers.find(row => row.suggestion_id === suggestionId);
  if (!item) return;
  const dialog = document.createElement("dialog");
  dialog.className = "frontier-detail-dialog";
  dialog.setAttribute("aria-label", item.direction_title || item.title);
  dialog.innerHTML = '<header><span>推荐研究方向</span><button type="button" data-close-frontier aria-label="关闭推荐详情">×</button></header>' + `
    <article class="topic-frontier-card academic-frontier-card">
      <header><span>方向建议 · ${escapeHtml(item.title)}</span><span>${Number(item.count)} 篇</span></header>
      <h3>${escapeHtml(item.direction_title || item.title)}</h3><h4>可细化的研究问题</h4><ol>${(item.subquestions || [item.research_question]).map(question => `<li>${escapeHtml(question)}</li>`).join("")}</ol>${item.suggested_approach ? `<h4>建议研究路径</h4><p>${escapeHtml(item.suggested_approach)}</p><h4>需要核对的材料</h4><p>${escapeHtml(item.material_needs)}</p>` : ""}<p><b>推荐理由：</b>${escapeHtml(item.recommendation_reason || "根据本站收录资料推荐")}</p><p class="method-note">${(item.evidence_gaps || []).map(escapeHtml).join("；")}</p>
      <h4>相关论文 · 展示 ${Number((item.papers || []).length)} 篇</h4><p class="method-note">${escapeHtml(item.matching_basis || "按已登记主题归类，需进一步阅读原文。")}</p><ul class="frontier-paper-list">${(item.papers || []).map(paper => `<li><button type="button" class="text-link" data-material-version="${Number(paper.document_version_id)}">${escapeHtml(paper.title)}</button><small>${escapeHtml(paper.source_name || "来源未登记")} · ${escapeHtml(publishedLabel(paper.published_at, paper.published_at_precision))}</small></li>`).join("")}</ul>
      <details><summary>政策、事件与数据关联 · ${Number((item.related?.policies || []).length + (item.related?.events || []).length)} 项</summary><p>${escapeHtml(item.relation_note)}</p>${[["policies", "政策事件"], ["events", "相关事件"]].map(([key, label]) => `<h4>${label}</h4>${item.related?.[key]?.length ? `<ul class="frontier-paper-list">${item.related[key].map(row => `<li><a class="text-link" href="${escapeHtml(row.url)}">${escapeHtml(row.title)}</a><small>${escapeHtml(row.country_iso3)} · ${escapeHtml(publishedLabel(row.date, row.date_precision))}</small><small>登记提及 ${(row.evidence_links || []).map(link => `#${Number(link.event_mention_id)} / 文献版本 #${Number(link.document_version_id)}`).join("、")}</small></li>`).join("")}</ul>` : `<p>当前筛选下未找到已登记关联。</p>`}`).join("")}<h4>可引用数据候选</h4>${(item.candidate_data || []).map(data=>`<p><a href="${escapeHtml(data.country_url)}">${escapeHtml(data.title)}</a> · ${escapeHtml(data.source_name)} · ${escapeHtml(data.unit || "")}<small>快照 ${Number(data.locator?.snapshot_id)} / 观测版本 ${Number(data.object_id)}</small></p>`).join("")}<p>${escapeHtml(item.data_relation_note)}</p></details>
      <footer><a class="text-link" href="#/countries/search?q=${encodeURIComponent(item.title)}">继续查找资料</a><button type="button" class="primary-btn compact" data-create-from-frontier="${escapeHtml(item.suggestion_id)}">选择此方向</button></footer>
    </article>`;
  dialog.addEventListener("click", event => {
    if (event.target.closest("[data-close-frontier], [data-material-version], [data-create-from-frontier], a")) dialog.close();
  });
  dialog.addEventListener("close", () => dialog.remove(), {once:true});
  document.body.append(dialog);
  dialog.showModal();
}

let topicFrontierRequest = 0;
async function loadTopicFrontiers() {
  const root = document.getElementById("topicFrontierList");
  if (!root) return;
  const request = ++topicFrontierRequest;
  const select = document.getElementById("topicFrontierCountry");
  if (select.options.length === 1 && state.catalog?.length) {
    select.insertAdjacentHTML("beforeend", state.catalog.map(item => `<option value="${escapeHtml(item.iso3)}">${escapeHtml(item.name_zh)} · ${escapeHtml(item.iso3)}</option>`).join(""));
  }
  root.innerHTML = `<div class="empty-state">正在从国别空间共用资料库读取学术主题…</div>`;
  document.getElementById("topicFrontierCoverage").textContent = "";
  const params = new URLSearchParams({ years: document.getElementById("topicFrontierYears").value });
  if (select.value) params.set("country_iso3", select.value);
  const q = document.getElementById("topicFrontierQuery").value.trim();
  if (q) params.set("q", q);
  try {
    const payload = await apiFetch(`/reader/project-research-frontiers?${params}`);
    if (request === topicFrontierRequest) renderTopicFrontiers(payload);
  } catch (error) { if (request === topicFrontierRequest) root.innerHTML = errorState(error.message); }
}

async function openFrontierDraft(suggestionId) {
  const item = state.topicFrontiers.find(row => row.suggestion_id === suggestionId);
  if (!item) return;
  await openTopicLinkDialog("create", null, "", item.country_iso3 || document.getElementById("topicFrontierCountry").value || "COD");
  ProjectOnboarding.seed(item);
}

function topicGapAlertHtml(topic) {
  const docs = topic.documents || [];
  const events = topic.events || [];
  const slices = topic.data_slices || [];
  const runs = topic.capability_runs || [];

  const gapItems = [];
  const hasThirdParty = docs.some((d) => (d.source_name || "").includes("联合国") || (d.source_name || "").includes("UN") || (d.source_name || "").includes("IEA") || (d.source_name || "").includes("World Bank"));
  if (!hasThirdParty) gapItems.push({ label: "缺少第三方国际组织材料", tip: "建议补充联合国专家组报告或 World Bank 评估以平衡多方视角。" });
  if (!slices.length) gapItems.push({ label: "缺少结构化贸易/投资数据切片", tip: "建议关联 UN Comtrade 或 UNCTAD FDI 时序数据，提供量化证据。" });
  if (!events.length) gapItems.push({ label: "尚未关联已核验多源事件", tip: "建议从国别空间将 reviewed 事件加入项目。" });
  if (!runs.length) gapItems.push({ label: "尚未生成首期项目周报", tip: "现有材料已可运行能力并生成带引用的周报草稿。" });

  if (!gapItems.length) return "";
  return `
    <section class="topic-gap-alert">
      <div class="gap-alert-icon">${iconSvg("sparkles")}</div>
      <div class="gap-alert-body">
        <strong>项目证据链完整度诊断</strong>
        <p>系统已分析当前项目已归集的 ${docs.length} 份材料与 ${events.length} 个事件，识别出以下关键证据缺口：</p>
        <div class="gap-alert-tags">
          ${gapItems.map((item) => `<span class="gap-tag" title="${escapeHtml(item.tip)}">${iconSvg("alert-triangle")} ${escapeHtml(item.label)}</span>`).join("")}
        </div>
      </div>
    </section>
  `;
}

function collaborationRoleLabel(role) {
  return ({ owner: "负责人", reviewer: "复核者", editor: "编辑者", viewer: "查看者" })[role] || role;
}

async function renderTopicPolicyGraph(topicId) {
  const root = document.getElementById("topicPolicyGraphRoot");
  if (!root) return;
  root.innerHTML = detailSkeleton(3);
  try {
    const payload = await apiFetch(`/reader/research-cases/${topicId}/policy-impact-graph`);
    const graph = payload.graph;
    if (!graph) {
      root.innerHTML = `${emptyState("尚未生成政策关系候选图。系统只组织已复核事件和确认主张，不把时间先后当作因果。")}<button type="button" class="primary-btn" data-generate-policy-graph="${topicId}">生成候选图</button>`;
      return;
    }
    const labels = Object.fromEntries((graph.nodes || []).map((node) => [node.id, node.label]));
    const reviewStates = graph.edge_review_states || {};
    const edges = (graph.edges || []).map((edge) => `<article class="policy-graph-edge is-${escapeHtml(reviewStates[edge.id] || "pending")}">
      <div class="policy-edge-flow"><span>${escapeHtml(labels[edge.from] || edge.from)}</span><b>${escapeHtml(edge.relation || "关联")}</b><span>${escapeHtml(labels[edge.to] || edge.to)}</span></div>
      <p>${escapeHtml(displayEnum({ observed: "已观察事实", claimed: "来源主张", candidate_unreviewed: "待核验推测" }, edge.status))} · ${escapeHtml(edge.generation_rule || "处理规则未登记")}</p>
      <small>${escapeHtml(edge.evidence?.source || edge.evidence?.note || "当前只登记证据缺口")}${edge.evidence?.locator ? ` · ${escapeHtml(capabilityValueLabel(edge.evidence.locator))}` : ""}</small>
      <div class="inline-actions"><button type="button" class="ghost-btn compact" data-review-policy-edge="${escapeHtml(edge.id)}" data-review-state="accepted">研究者确认</button><button type="button" class="ghost-btn compact" data-review-policy-edge="${escapeHtml(edge.id)}" data-review-state="rejected">拒绝关系</button><span>${escapeHtml(displayEnum({ pending: "待判断", accepted: "研究者已接受", rejected: "研究者已拒绝" }, reviewStates[edge.id] || "pending"))}</span></div>
    </article>`).join("");
    root.dataset.runId = String(payload.latest_run?.id || "");
    root.dataset.revisionNo = String(payload.revision?.revision_no || 0);
    root.dataset.graph = JSON.stringify(graph);
    root.innerHTML = `<div class="section-heading"><div><p class="eyebrow">CANDIDATE RELATION GRAPH</p><h3>政策影响关系候选图</h3></div><button type="button" class="ghost-btn" data-generate-policy-graph="${topicId}">按当前证据重新生成</button></div><p class="method-note">${escapeHtml(graph.method_note || "")}</p>${edges ? `<div class="policy-graph-list">${edges}</div>` : emptyState("当前没有具备证据或明确缺口的候选关系。")} ${capabilityGapHtml(graph)}`;
  } catch (error) { root.innerHTML = errorState(error.message); }
}

async function renderTopicCollaboration(topicId) {
  const root = document.getElementById("topicCollaborationRoot");
  if (!root) return;
  root.innerHTML = detailSkeleton(3);
  try {
    const [payload, auth] = await Promise.all([
      apiFetch(`/reader/research-cases/${topicId}/collaboration`),
      apiFetch("/reader/auth/status"),
    ]);
    const canManage = (payload.current_member?.permissions || []).includes("manage_members");
    const members = (payload.members || []).map((member) => {
      const keys = (member.access_keys || []).map((key) => `<span class="member-key-row"><code>${escapeHtml(key.key_prefix)}…</code><small>${escapeHtml(key.label)} · ${key.status === "active" ? "有效" : "已吊销"}</small>${canManage && member.role !== "owner" && key.status === "active" ? `<button type="button" class="danger-link" data-revoke-member-key="${key.id}" data-member-id="${member.id}">吊销密钥</button>` : ""}</span>`).join("");
      return `<article class="collaboration-member"><div><strong>${escapeHtml(member.display_name)}</strong><span>${escapeHtml(member.email)} · ${escapeHtml(collaborationRoleLabel(member.role))}</span>${keys ? `<div class="member-key-list">${keys}</div>` : ""}</div>${canManage && member.role !== "owner" ? `<label>角色<select data-member-role="${member.id}">${["reviewer", "editor", "viewer"].map((role) => `<option value="${role}"${role === member.role ? " selected" : ""}>${collaborationRoleLabel(role)}</option>`).join("")}</select></label><button type="button" class="danger-link" data-remove-member="${member.id}">移除</button>` : `<span class="status-chip">${escapeHtml(collaborationRoleLabel(member.role))}</span>`}</article>`;
    }).join("");
    const contributions = (payload.contributions || []).map((item) => `<li><time>${escapeHtml(localDate(item.created_at))}</time><b>${escapeHtml(item.actor)}</b><span>${escapeHtml(item.action_type)} · ${escapeHtml(item.object_type)} #${escapeHtml(item.object_key)}</span></li>`).join("");
    root.innerHTML = `<section class="report-section"><div class="section-heading"><div><p class="eyebrow">LOCAL COLLABORATION</p><h3>成员身份与权限</h3></div><span class="status-chip">${escapeHtml(collaborationRoleLabel(payload.current_member?.role))}</span></div><p class="method-note">${escapeHtml(payload.public_publishing?.note || "")}</p>${!auth.initialized && auth.bootstrap_allowed ? `<button type="button" class="primary-btn" data-bootstrap-reader-key>初始化负责人密钥</button>` : ""}${auth.initialized ? "" : `<div class="reader-key-entry"><label>当前会话密钥<input id="readerSessionKey" type="password" autocomplete="off" placeholder="gbr_…" value="${escapeHtml(window.sessionStorage.getItem(READER_KEY_STORAGE_KEY) || "")}"></label><button type="button" class="ghost-btn" data-save-reader-key>保存到本次会话</button></div>`}<div class="collaboration-members">${members}</div></section>${canManage ? `<section class="report-section"><h3>${auth.initialized ? "添加已有平台账号" : "邀请本机成员"}</h3><form id="researchMemberInviteForm" class="member-invite-form"><input name="display_name" required placeholder="姓名"><input name="email" type="email" required placeholder="邮箱"><select name="role"><option value="editor">编辑者</option><option value="reviewer">复核者</option><option value="viewer">查看者</option></select><button type="submit" class="primary-btn">${auth.initialized ? "加入项目" : "生成个人密钥"}</button></form><div id="issuedMemberKey" class="issued-key" hidden></div></section>` : ""}<section class="report-section"><h3>贡献记录</h3>${contributions ? `<ol class="contribution-timeline">${contributions}</ol>` : emptyState("尚无贡献记录。")}</section>`;
  } catch (error) { root.innerHTML = errorState(error.message); }
}

async function loadProjectTasks(caseId) {
  const root = document.getElementById("projectTaskList");
  if (!root) return;
  try {
    const payload = await apiFetch(`/reader/research-cases/${caseId}/tasks`);
    const conversations = (payload.items || []).filter((item) => item.kind === "conversation");
    const activeId = state.projectConversationId || "";
    root.innerHTML = conversations.length ? `<div class="project-task-list">${conversations.map((item) => `<div class="pw-history-entry"><a class="project-task-row${String(item.id) === activeId ? " is-active" : ""}" href="#/projects/${caseId}/tasks/${encodeURIComponent(item.id)}"><div><strong>${escapeHtml(item.title || "未命名对话")}</strong><span>${Number(item.turn_count || 0)} 轮 · ${escapeHtml(runStatusLabel(item.status))}</span></div><time>${escapeHtml(localDate(item.updated_at))}</time></a><button type="button" data-pw-delete-task="${escapeHtml(item.id)}" aria-label="删除对话：${escapeHtml(item.title || "未命名对话")}">删除</button></div>`).join("")}</div>` : `<div class="project-chat-empty"><strong>还没有对话</strong><span>从右侧输入第一个问题，系统会自动保存。</span></div>`;
  } catch (error) { root.innerHTML = errorState(error.message); }
}

async function loadProjectConversation(caseId, conversationId) {
  const thread = document.querySelector(".project-task-thread");
  const title = document.querySelector("[data-project-conversation-title]");
  const meta = document.querySelector("[data-project-conversation-meta]");
  if (!thread || !conversationId) return;
  thread.innerHTML = `<div class="project-chat-loading">正在恢复此前对话与研究规划…</div>`;
  try {
    const task = await apiFetch(`/reader/research-cases/${caseId}/tasks/${encodeURIComponent(conversationId)}`);
    if (task.kind !== "conversation") throw new Error("这不是可继续的研究对话");
    if (task.messages) { ProjectPlanUI.render(task, document.querySelector("[data-project-task-form]")); return; }
    const turns = task.turns || [];
    if (title) title.textContent = turns[0]?.question || "研究对话";
    if (meta) meta.textContent = `${turns.length} 轮对话 · 已保留项目范围与引用`;
    thread.innerHTML = turns.map((turn) => {
      const user = `<article class="is-user"><strong>你</strong><p>${escapeHtml(turn.question)}</p><small>${escapeHtml(localDate(turn.created_at))}</small></article>`;
      if (turn.status === "succeeded") return `${user}<article class="is-assistant"><strong>项目 AI</strong>${assistantResponseHtml({ run_id: turn.run_id, artifact: turn.artifact || {} })}</article>`;
      if (turn.status === "running") return `${user}<article class="is-running"><strong>项目 AI 正在研究</strong><p>这轮对话仍在运行，可以稍后继续查看。</p></article>`;
      return `${user}<article class="is-error"><strong>本轮没有完成</strong><p>此前内容已经保留，你可以修改问题后继续。</p></article>`;
    }).join("") || `<div class="project-chat-empty"><strong>开始这段对话</strong><span>输入问题后会自动保存到左侧。</span></div>`;
    thread.scrollTop = thread.scrollHeight;
  } catch (error) { thread.innerHTML = errorState(error.message); }
}

function projectEvidenceLabel(item) {
  if (item.object_type === "document_version") {
    const material = (state.topic?.documents || []).find((row) => Number(row.document_version_id) === Number(item.object_id));
    return material?.title || `材料版本 #${item.object_id}`;
  }
  if (item.object_type === "event_mention") {
    const event = (state.topic?.events || []).find((row) => Number(row.id) === Number(item.event_id));
    return `${event?.title || `事件 #${item.event_id}`} · 确认提及 #${item.object_id}`;
  }
  return item.title || `${item.object_type} #${item.object_id}`;
}

async function loadProjectEvidence(caseId) {
  const root = document.getElementById("projectEvidenceReviewList");
  if (!root) return;
  try {
    const payload = await apiFetch(`/reader/research-cases/${caseId}/evidence`);
    const latest = new Map();
    (payload.reviews || []).forEach((review) => {
      const key = `${review.object_type}:${review.object_id || "gap"}`;
      if (!latest.has(key)) latest.set(key, review);
    });
    const canReview = (state.topic?.current_member?.permissions || []).includes("revise");
    const rows = Object.entries(payload.groups || {}).flatMap(([evidenceType, items]) => (items || []).map((item) => ({ ...item, evidence_type: evidenceType })));
    root.innerHTML = rows.length ? `<div class="project-evidence-review-list">${rows.map((item) => {
      const review = latest.get(`${item.object_type}:${item.object_id || "gap"}`);
      const status = review?.decision || "unreviewed";
      return `<article><div><span>${escapeHtml(item.evidence_type)} · ${escapeHtml(item.object_type)}</span><strong>${escapeHtml(projectEvidenceLabel(item))}</strong><small>${escapeHtml(review?.note || (item.evidence_type === "field" ? "田野资料仅在项目权限内可见" : "平台来源已按全局规则处理；此处只决定本项目是否采用"))}</small></div><b class="status-chip">${escapeHtml(displayEnum({ accepted: "项目已采用", rejected: "项目不采用", needs_revision: "需项目内补充", unreviewed: "项目未决定" }, status))}</b>${canReview ? `<div class="inline-actions"><button type="button" class="ghost-btn compact" data-project-evidence-review="accepted" data-evidence-type="${escapeHtml(item.evidence_type)}" data-object-type="${escapeHtml(item.object_type)}" data-object-id="${escapeHtml(item.object_id || "")}">采用</button><button type="button" class="ghost-btn compact" data-project-evidence-review="needs_revision" data-evidence-type="${escapeHtml(item.evidence_type)}" data-object-type="${escapeHtml(item.object_type)}" data-object-id="${escapeHtml(item.object_id || "")}">需补充</button><button type="button" class="danger-link" data-project-evidence-review="rejected" data-evidence-type="${escapeHtml(item.evidence_type)}" data-object-type="${escapeHtml(item.object_type)}" data-object-id="${escapeHtml(item.object_id || "")}">不采用</button></div>` : ""}</article>`;
    }).join("")}</div><p class="method-note">${escapeHtml(payload.rule || "")}</p>` : emptyState("当前项目没有可采用的直接或田野证据。");
  } catch (error) { root.innerHTML = errorState(error.message); }
}

async function loadProjectOutputs(caseId) {
  const root = document.getElementById("projectFormalOutputs");
  if (!root) return;
  try {
    const payload = await apiFetch(`/reader/research-cases/${caseId}/outputs`);
    root.innerHTML = payload.items?.length ? `<div class="project-formal-output-list">${payload.items.map((item) => `<article><a href="#/projects/${caseId}/outputs/${item.target_id}"><strong>${escapeHtml(item.document_title || `正式成果 #${item.target_id}`)}</strong><span>来自能力运行 #${item.capability_run_id} · ${escapeHtml(localDate(item.confirmed_at))}</span></a>${ProjectWorkbench.outputStatus(item)}<div class="inline-actions"><button type="button" class="ghost-btn compact" data-export-project-output="markdown" data-output-id="${item.target_id}" ${item.availability?.status === "needs_review" ? "disabled" : ""}>Markdown</button><button type="button" class="ghost-btn compact" data-export-project-output="json" data-output-id="${item.target_id}" ${item.availability?.status === "needs_review" ? "disabled" : ""}>JSON</button><button type="button" class="ghost-btn compact" data-export-project-output="csv" data-output-id="${item.target_id}" ${item.availability?.status === "needs_review" ? "disabled" : ""}>CSV</button></div></article>`).join("")}</div>` : emptyState("尚无人工确认写回的正式成果；下方能力运行不等于成果。");
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function syncProjectCapabilitySelection(form, changedInput = null) {
  const choices = [...form.querySelectorAll('[name="capability_config_ids"]')];
  if (changedInput?.checked && !changedInput.dataset.selectionOrder) {
    const highest = Math.max(0, ...choices.map((item) => Number(item.dataset.selectionOrder || 0)));
    changedInput.dataset.selectionOrder = String(highest + 1);
  }
  if (changedInput && !changedInput.checked) delete changedInput.dataset.selectionOrder;
  let selected = choices.filter((item) => item.checked).sort((left, right) => Number(left.dataset.selectionOrder) - Number(right.dataset.selectionOrder));
  if (selected.length > 3) {
    changedInput.checked = false;
    delete changedInput.dataset.selectionOrder;
    showToast("本轮最多组合 3 项能力");
    selected = choices.filter((item) => item.checked).sort((left, right) => Number(left.dataset.selectionOrder) - Number(right.dataset.selectionOrder));
  }
  selected.forEach((item, index) => {
    item.dataset.selectionOrder = String(index + 1);
  });
  choices.forEach((item) => {
    const order = selected.indexOf(item);
    item.closest("label")?.classList.toggle("is-selected", order >= 0);
    const marker = item.closest("label")?.querySelector("[data-capability-order]");
    if (marker) marker.textContent = order >= 0 ? String(order + 1) : "+";
  });
  const count = form.querySelector("[data-capability-selection-count]");
  if (count) count.textContent = `${selected.length} / 3 · 按选择顺序`;
  window.ProjectWorkbench?.syncTags(form, selected);
  return selected;
}

function renderTopicWorkspace(topic, pane) {
  const toolbar = document.getElementById("topicToolbar");
  const tabs = document.getElementById("topicTabs");
  const detailRoot = document.getElementById("topicDetail");
  detailRoot.classList.toggle("is-task-pane", pane === "tasks");
  detailRoot.closest(".project-taskbench")?.classList.toggle("is-chat-workbench", pane === "tasks");
  toolbar.innerHTML = `<div class="toolbar-left"><a class="ghost-btn" href="#/projects">← 项目大厅</a><div class="toolbar-divider"></div><div class="country-current-badge topic-toolbar-title"><strong title="${escapeHtml(topic.title)}">${escapeHtml(topic.title)}</strong><small>范围 v${Number(topic.scope_revision || 1)} · ${escapeHtml(scopeLabel(topic.scope))}</small></div></div><div class="toolbar-right"><span class="country-freshness">更新 ${escapeHtml(localDate(topic.updated_at))}</span>${pane === "tasks" ? "" : `<button type="button" class="ghost-btn compact project-context-toggle" data-project-context-toggle aria-expanded="false" aria-controls="projectContextPanel">项目上下文</button>`}<button type="button" class="primary-btn compact" data-generate-topic-digest="${topic.id}">生成团队周报</button></div>`;
  const navigation = [
    { id: "overview", label: "项目概览", meta: "研究闭环" },
    { id: "tasks", label: "工作台", meta: `${Number(topic.task_summary?.conversation_count || 0)} 个对话` },
    { id: "evidence", label: "证据审阅", meta: `${Number(topic.document_count || 0) + Number(topic.event_count || 0)} 项` },
    { id: "outputs", label: "研究材料与成果", meta: `${Number(topic.formal_output_count || 0)} 份正式` },
    { id: "settings", label: "设置与成员", meta: escapeHtml(collaborationRoleLabel(topic.current_member?.role || "viewer")) },
  ];
  tabs.innerHTML = navigation.map((item) => `<a class="${item.id === pane ? "is-active" : ""}" href="#/projects/${topic.id}/${item.id}"><span>${escapeHtml(item.label)}</span><small>${item.meta}</small></a>`).join("");
  const identity = document.getElementById("projectIdentity");
  if (identity) identity.innerHTML = `<span>${escapeHtml((topic.scope?.country_iso3 || "GLOBAL"))}</span><strong>${escapeHtml(topic.title)}</strong><small>${escapeHtml(topic.research_question || "待确认研究问题")}</small>`;
  const counts = document.getElementById("projectNavCounts");
  if (counts) counts.innerHTML = `<span><b>${Number(topic.pending_review_count || 0)}</b>待审阅</span><span><b>${Number(topic.formal_output_count || 0)}</b>正式成果</span>`;
  const contextRoot = document.getElementById("projectContextPanel");
  if (contextRoot) contextRoot.innerHTML = `<button type="button" class="project-context-close" data-project-context-close aria-label="关闭项目上下文">关闭 ×</button><section><header><span>PROJECT CONTEXT</span><strong>当前范围</strong></header><dl><div><dt>@ 国家</dt><dd>${escapeHtml(topic.scope?.country_iso3 || "未限定")}</dd></div><div><dt>@ 事件</dt><dd>${Number(topic.event_count || 0)} 个 reviewed</dd></div><div><dt>@ 材料</dt><dd>${Number(topic.document_count || 0)} 份 confirmed</dd></div><div><dt># 能力</dt><dd>${Number(topic.capability_runs?.length || 0)} 次运行</dd></div></dl><a class="ghost-btn compact link-button project-context-capability" href="#/capabilities?project=${topic.id}">为当前项目选能力</a></section><section><header><span>GOVERNANCE</span><strong>权利与运行</strong></header><ul><li>角色：${escapeHtml(collaborationRoleLabel(topic.current_member?.role || "viewer"))}</li><li>田野资料默认受限</li><li>外部 AI 默认禁用</li><li>成果需人工确认写回</li></ul></section><section><header><span>LINEAGE</span><strong>成果血缘</strong></header><p>范围 v${Number(topic.scope_revision || 1)} · 配置版本 · 输入快照 · 证据定位 · 确认人与时间。</p></section>`;
  const question = `<div class="topic-question-banner"><span>正在研究的问题</span><p>${escapeHtml(topic.research_question || "尚未登记研究问题")}</p></div>`;
  if (pane === "overview") {
    const gaps = [];
    if (!(topic.documents || []).length) gaps.push("补充已确认材料");
    if (!(topic.events || []).length) gaps.push("关联至少一个已核验事件");
    if (!(topic.capability_runs || []).length) gaps.push("形成第一份可回源产物");
    const latest = topic.latest_run;
    const latestHtml = latest ? `<button type="button" class="object-link-card" data-view-topic-run="${latest.id}"><b>最新产物 · 运行 #${latest.id}</b><span>${escapeHtml(runStatusLabel(latest.status))} · ${escapeHtml(localDate(latest.finished_at || latest.created_at))}</span></button>` : emptyState("当前项目尚无产物");
    const gapAlert = topicGapAlertHtml(topic);
    const workflow = topic.workflow || { steps: [], next_action: null };
    const workflowStates = new Map((workflow.steps || []).map((step) => [step.id, step.state]));
    const evidenceDone = workflowStates.get("evidence_included") === "done";
    const taskDone = workflowStates.get("skill_run") === "done";
    const outputDone = workflowStates.get("output_available") === "done";
    const nextGuideStep = !evidenceDone ? "evidence" : !taskDone ? "tasks" : "outputs";
    const guideSteps = [
      { id: "evidence", number: 1, title: "整理项目证据", detail: "检查已纳入的材料、事件和数据，决定哪些证据用于本项目。", href: `#/projects/${topic.id}/evidence`, done: evidenceDone },
      { id: "tasks", number: 2, title: "开始研究对话", detail: "直接与项目 AI 对话，需要时再选择资料范围或组合本轮 Skill。", href: `#/projects/${topic.id}/tasks`, done: taskDone },
      { id: "outputs", number: 3, title: "审核研究材料与成果", detail: "检查引用和证据缺口，确认后才写回为正式成果。", href: `#/projects/${topic.id}/outputs`, done: outputDone },
    ];
    const gettingStarted = `<section class="project-getting-started"><div class="section-heading"><div><p class="eyebrow">HOW TO USE THIS PROJECT</p><h3>这个项目怎么用</h3></div><span>证据 → 任务与 Skill → 人工确认成果</span></div><div class="project-guide-steps">${guideSteps.map((step) => `<a class="${step.done ? "is-done" : step.id === nextGuideStep ? "is-next" : ""}" href="${step.href}"><i>${step.done ? "✓" : step.number}</i><span><b>${escapeHtml(step.title)}</b><small>${escapeHtml(step.detail)}</small></span><em>${step.id === nextGuideStep ? "从这里继续" : step.done ? "已完成" : "稍后进行"}</em></a>`).join("")}</div></section>`;
    const workflowHtml = workflow.steps?.length ? `<section class="project-workflow"><div class="section-heading"><div><p class="eyebrow">RESEARCH LOOP</p><h3>研究闭环</h3></div><span>${workflow.complete ? "闭环已完成" : "等待人工推进"}</span></div><ol>${workflow.steps.map((step) => `<li class="is-${escapeHtml(step.state)}"><span>${step.state === "done" ? "✓" : step.state === "ready" ? "→" : "·"}</span><div><b>${escapeHtml(step.label)}</b><small>${Number(step.count || 0)} 项</small></div></li>`).join("")}</ol>${workflow.next_action ? `<div class="workflow-next"><div><b>下一步：${escapeHtml(workflow.next_action.label)}</b>${(workflow.next_action.blocking_reasons || []).map((reason) => `<p>${escapeHtml(reason)}</p>`).join("")}</div><a class="primary-btn compact link-button" href="${escapeHtml(workflow.next_action.href)}">前往处理</a></div>` : ""}</section>` : "";
    detailRoot.innerHTML = `${question}${gettingStarted}${workflowHtml}${gapAlert}<div class="topic-overview-grid"><section><h3>证据覆盖</h3><div class="report-facts"><div><strong>${Number(topic.document_count || 0)}</strong><span>确认材料</span></div><div><strong>${Number(topic.event_count || 0)}</strong><span>已核验事件</span></div><div><strong>${Number((topic.data_slices || []).length)}</strong><span>数据切片</span></div></div></section><section><h3>缺失环节与下一步</h3>${gaps.length ? `<ol>${gaps.map((gap) => `<li>${escapeHtml(gap)}</li>`).join("")}</ol>` : `<p>现有材料、事件和产物已形成基础闭环；下一步应复核最新产物中的证据缺口。</p>`}</section><section><h3>最新产物</h3>${latestHtml}</section></div>`;
    return;
  }
  if (pane === "tasks") {
    const activeConversationId = state.projectConversationId || "";
    const eventOptions = (topic.events || []).map((item) => `<option value="${item.id}">${escapeHtml(item.title)}</option>`).join("");
    const materialOptions = (topic.documents || []).map((item) => `<option value="${item.document_version_id}">${escapeHtml(item.title || `材料 #${item.document_version_id}`)}</option>`).join("");
    const sliceOptions = (topic.data_slices || []).map((item) => `<option value="${item.id}">${escapeHtml(item.label)}</option>`).join("");
    const availableCapabilities = (state.capabilityConfigs || []).filter((item) => (
      item.catalog_key
      && !["research-s03", "research-s05"].includes(item.catalog_key)
      && item.can_run
      && (!item.research_case_id || Number(item.research_case_id) === Number(topic.id))
    ));
    const capabilityChoices = availableCapabilities.map((item) => `<label class="project-capability-choice"><input type="checkbox" name="capability_config_ids" value="${item.id}"><span><b>${escapeHtml(item.name)}</b><small>${escapeHtml((item.input_schema?.allowed_fields || []).map(schemaFieldLabel).slice(0, 2).join("、") || "当前范围")} → ${escapeHtml(item.output_schema?.type || "候选产物")}</small></span><i data-capability-order>+</i></label>`).join("");
    const capabilityPicker = `<details class="project-capability-picker"><summary><span># 为本轮选择 Skill</span><small data-capability-selection-count>0 / 3 · 可选</small></summary><div class="project-capability-choices">${capabilityChoices || `<p>当前没有可运行的 Skill。<a href="#/capabilities?project=${topic.id}">去能力市场下载并配置</a></p>`}</div><p>不选择也可以直接对话；选择后 AI 会结合 Skill 组织本轮研究。</p></details>`;
    const contextPicker = `<details class="project-chat-context"><summary><span>添加项目资料范围</span><small>国家、事件、材料或数据切片</small></summary><div class="project-reference-selectors"><label><span>@国家</span><select name="country_reference"><option value="${escapeHtml(topic.scope?.country_iso3 || "")}">${escapeHtml(topic.scope?.country_iso3 || "未设定")}</option></select></label><label><span>@事件</span><select name="event_reference"><option value="">不指定</option>${eventOptions}</select></label><label><span>@材料</span><select name="material_reference"><option value="">不指定</option>${materialOptions}</select></label><label><span>@数据切片</span><select name="data_slice_reference"><option value="">不指定</option>${sliceOptions}</select></label></div></details>`;
    const materials = (topic.documents || []).slice(0, 4).map((item) => `<a class="project-chat-material" href="#/projects/${topic.id}/evidence"><strong>${escapeHtml(item.title || "未命名材料")}</strong><span>${escapeHtml(item.source_name || "来源未登记")}</span></a>`).join("");
    const activeTitle = activeConversationId ? "正在恢复对话…" : "新对话";
    const activeMeta = activeConversationId ? "读取此前规划与回答" : "第一轮发送后自动保存";
    detailRoot.innerHTML = `<div class="project-chat-workbench"><aside class="project-chat-library"><header><div><p class="eyebrow">PROJECT CHAT</p><h2>研究对话</h2></div><a class="primary-btn compact link-button" href="#/projects/${topic.id}/tasks">＋ 新建</a></header><div id="projectTaskList">${detailSkeleton(3)}</div><section class="project-chat-plan"><header><strong>当前研究规划</strong><a href="#/projects/${topic.id}/overview">查看项目</a></header><p>${escapeHtml(topic.research_question || "尚未登记研究问题")}</p><div><span>${escapeHtml(topic.scope?.country_iso3 || "全球")}</span><span>${Number(topic.event_count || 0)} 个事件</span><span>${Number(topic.document_count || 0)} 份资料</span></div></section><section class="project-chat-materials"><header><strong>已整理资料</strong><a href="#/projects/${topic.id}/evidence">全部 ${Number(topic.document_count || 0)}</a></header>${materials || `<p>项目还没有已确认资料。</p>`}</section></aside><section class="project-conversation"><header><div><p class="eyebrow">LIVE RESEARCH CHAT</p><h2 data-project-conversation-title>${escapeHtml(activeTitle)}</h2><span data-project-conversation-meta>${escapeHtml(activeMeta)}</span></div><button type="button" class="ghost-btn compact project-context-toggle" data-project-context-toggle aria-expanded="false" aria-controls="projectContextPanel">项目上下文</button></header><div class="project-task-thread">${activeConversationId ? `<div class="project-chat-loading">正在恢复此前对话与研究规划…</div>` : `<div class="project-chat-welcome"><span>项目 AI</span><h3>从这个项目继续研究</h3><p>我会保留这段对话，并使用当前项目已经整理的资料。你也可以在发送前临时选择具体资料或 Skill。</p><div><button type="button" data-project-prompt="梳理当前项目已经确认的事实、证据缺口和下一步研究计划。">梳理现状与下一步</button><button type="button" data-project-prompt="根据当前项目资料，列出最值得继续追问的三个研究问题。">推荐后续问题</button></div></div>`}</div><form class="project-task-composer" data-project-task-form="${topic.id}"><input type="hidden" name="conversation_id" value="${escapeHtml(activeConversationId)}"><textarea name="question" rows="3" required placeholder="继续问当前项目，例如：梳理已确认事实、证据缺口和下一步计划…"></textarea><div class="project-chat-options">${contextPicker}${capabilityPicker}</div><footer><span>对话自动保存；正式成果仍需人工确认</span><button type="submit" class="primary-btn">发送</button></footer></form></section></div>`;
    loadProjectTasks(topic.id);
    if (activeConversationId) loadProjectConversation(topic.id, activeConversationId);
    return;
  }
  if (pane === "evidence") {
    const sourceOptions = [...new Set((topic.documents || []).map((item) => item.source_type).filter(Boolean))].sort();
    const typeOptions = [...new Set((topic.documents || []).map((item) => item.document_type).filter(Boolean))].sort();
    detailRoot.innerHTML = `${question}${topicGapAlertHtml(topic)}<section class="evidence-type-strip"><span><b>direct</b>${Number(topic.document_count || 0) + Number(topic.event_count || 0)} 直接证据</span><span><b>field</b>默认受限</span><span><b>inference</b>必须链接证据</span><span><b>gap</b>待补采</span></section><section class="report-section field-material-section"><div class="section-heading"><div><p class="eyebrow">FIELD MATERIALS</p><h3>田野资料</h3></div><button type="button" class="ghost-btn compact" data-open-field-upload data-upload-scope="topic">上传资料</button></div><p class="method-note">默认受限、未复核，外部 AI 禁用；授权撤销后不可继续访问。</p><div class="field-material-list" id="topicFieldMaterialResults"></div></section><section class="report-section"><div class="section-heading"><div><p class="eyebrow">CONFIRMED EVIDENCE</p><h3>已确认材料与定位</h3></div><span id="topicMaterialCount"></span></div><div class="filter-bar"><label>检索<input id="topicMaterialQuery" type="search" placeholder="标题或来源"></label><label>来源类型<select id="topicSourceFilter"><option value="">全部</option>${sourceOptions.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(displayEnum(SOURCE_TYPE_LABELS, item))}</option>`).join("")}</select></label><label>材料类型<select id="topicTypeFilter"><option value="">全部</option>${typeOptions.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(displayEnum(DOCUMENT_TYPE_LABELS, item))}</option>`).join("")}</select></label></div><div class="topic-material-actionbar" id="topicMaterialActionbar" hidden></div><div class="topic-library-workbench"><div class="topic-material-list" id="topicMaterialResults"></div><aside class="topic-material-preview" id="topicMaterialPreview" aria-live="polite"></aside></div></section>`;
    detailRoot.insertAdjacentHTML("beforeend", `<section class="report-section"><div class="section-heading"><div><p class="eyebrow">PROJECT ADOPTION</p><h3>项目证据采用</h3></div><span>不修改平台全局复核状态</span></div><div id="projectEvidenceReviewList">${detailSkeleton(3)}</div></section>`);
    renderTopicMaterials();
    loadTopicFieldMaterials();
    loadProjectEvidence(topic.id);
    return;
  }
  if (pane === "outputs") {
    const runs = (topic.capability_runs || []).map((item) => `<button type="button" class="object-link-card" data-view-topic-run="${item.id}"><b>能力运行 #${item.id}</b><span>${escapeHtml(runStatusLabel(item.status))} · ${escapeHtml(localDate(item.finished_at || item.created_at))}</span></button>`).join("");
    detailRoot.innerHTML = `${question}<section class="report-section"><h3>研究产出</h3><p class="method-note">只展示真实保存的冻结产物；目录名称不代表已完成数量。</p><div class="research-output-catalog"><span>周报</span><span>简报</span><span>证据矩阵</span><span>国别比较表</span><span>田野材料摘录</span><span>历史冻结产物</span></div>${runs ? `<div class="object-link-grid">${runs}</div>` : emptyState("当前项目尚无保存的能力产物")}</section><section class="run-result" id="topicRunResult" hidden></section>`;
    const outputSection = detailRoot.querySelector(".report-section");
    outputSection?.insertAdjacentHTML("beforebegin", `<section class="report-section"><div class="section-heading"><div><p class="eyebrow">FORMAL OUTPUTS</p><h3>已确认正式成果</h3></div><span>冻结范围、证据和确认记录</span></div><div id="projectFormalOutputs">${detailSkeleton(2)}</div></section>`);
    const outputTitle = outputSection?.querySelector("h3");
    if (outputTitle) outputTitle.textContent = "候选运行（尚未写回）";
    loadProjectOutputs(topic.id);
    if (state.activeCapabilityRunId) window.setTimeout(() => viewCapabilityRun(state.activeCapabilityRunId), 0);
    return;
  }
  if (pane === "settings") {
    detailRoot.innerHTML = `${question}<div id="topicCollaborationRoot">${detailSkeleton(3)}</div>`;
    window.setTimeout(() => renderTopicCollaboration(topic.id), 0);
    return;
  }
}

async function renderProjectDeepObject(caseId, pane, objectRoute) {
  if (!objectRoute) return;
  if (pane === "tasks" && objectRoute.kind === "task") return;
  const root = document.querySelector(".pw-panel-content") || document.getElementById("topicDetail");
  if (!root) return;
  const kind = { materials: "material", events: "event", runs: "run", members: "member" }[objectRoute.kind] || objectRoute.kind;
  const anchor = document.createElement("section");
  anchor.className = "project-deep-object";
  anchor.innerHTML = `<p>正在恢复对象上下文…</p>`;
  root.prepend(anchor);
  try {
    if (kind === "task") {
      const task = await apiFetch(`/reader/research-cases/${caseId}/tasks/${encodeURIComponent(objectRoute.id)}`);
      anchor.innerHTML = `<header><span>TASK</span><strong>${escapeHtml(task.id)}</strong></header>${task.kind === "conversation" ? (task.turns || []).map((turn) => `<article><b>${escapeHtml(turn.question)}</b><span>${escapeHtml(runStatusLabel(turn.status))} · ${escapeHtml(localDate(turn.created_at))}</span></article>`).join("") : `<article><b>能力运行 #${task.run?.id}</b><span>${escapeHtml(runStatusLabel(task.run?.status))}</span></article>`}`;
    } else if (kind === "run") {
      const run = await apiFetch(`/reader/capability-runs/${Number(objectRoute.id)}`);
      anchor.innerHTML = `<header><span>RUN</span><strong>运行 #${run.id}</strong></header><article><b>${escapeHtml(runStatusLabel(run.status))}</b><span>${escapeHtml(run.review_status || "pending")} · 候选结果未写回</span></article>`;
    } else if (kind === "output") {
      await ProjectOutput.openSaved(Number(objectRoute.id), anchor);
    } else if (kind === "material") {
      const item = (state.topic?.documents || []).find((row) => [row.document_id, row.document_version_id].map(String).includes(String(objectRoute.id)));
      if (!item) throw new Error("材料不在当前项目范围");
      anchor.innerHTML = `<header><span>MATERIAL</span><strong>${escapeHtml(item.title || `材料 #${objectRoute.id}`)}</strong></header><article><b>${escapeHtml(item.source_name || "来源未登记")}</b><span>${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))} · 版本 ${escapeHtml(item.version_no || "-")}</span></article>`;
    } else if (kind === "event") {
      const item = (state.topic?.events || []).find((row) => String(row.id) === String(objectRoute.id));
      if (!item) throw new Error("事件不在当前项目范围");
      anchor.innerHTML = `<header><span>EVENT</span><strong>${escapeHtml(item.title)}</strong></header><article><b>${escapeHtml(item.review_status || "reviewed")}</b><span>${escapeHtml(localDate(item.start_at))} · <a href="#/countries/${escapeHtml(state.topic?.scope?.country_iso3 || "COD")}/events/${item.id}">查看事件证据</a></span></article>`;
    } else {
      anchor.innerHTML = `<header><span>${escapeHtml(String(kind || "OBJECT").toUpperCase())}</span><strong>对象 #${escapeHtml(objectRoute.id)}</strong></header><p>对象路由已保留；请在当前项目权限内继续处理。</p>`;
    }
  } catch (error) { anchor.innerHTML = errorState(error.message); }
}

async function loadTopics(topicId = null, pane = "overview", serial = state.routeSerial) {
  const currentForm = document.querySelector('[data-project-task-form]');
  const reuseConversation = String(state.topic?.id) === String(topicId) && currentForm
    && !state.routeQuery.has("new") && state.projectObjectRoute?.kind !== "task";
  state.topicPane = pane;
  const listRoot = document.getElementById("topicList");
  const detailRoot = document.getElementById("topicDetail");
  const hall = document.getElementById("topicHall");
  const workspace = document.getElementById("topicWorkspace");
  const showingDetail = Boolean(topicId);
  listRoot.hidden = Boolean(topicId);
  if (hall) hall.hidden = showingDetail;
  if (workspace) workspace.hidden = !showingDetail;
  const showingMine = !showingDetail && window.location.hash.split("?")[0] === "#/projects/mine";
  document.getElementById("myProjectsSection").hidden = !showingMine;
  document.getElementById("projectFrontiersSection").hidden = showingMine;
  document.getElementById("projectHallTitle").textContent = "项目空间";
  document.getElementById("projectHallDescription").textContent = "选择有依据的研究方向，确定主题与概览，再用 AI 丰富并持续研究。";
  const hallSwitch = document.getElementById("projectHallSwitch");
  hallSwitch.hidden = false;
  hallSwitch.textContent = showingMine ? "推荐研究方向" : "我的项目";
  hallSwitch.href = showingMine ? "#/projects" : "#/projects/mine";
  if (!showingDetail && !showingMine) loadTopicFrontiers();
  listRoot.innerHTML = `<div class="empty-state">正在读取研究项目…</div>`;
  detailRoot.hidden = true;
  try {
    const rows = await apiFetch("/reader/research-cases");
    if (serial !== state.routeSerial) return;
    state.topics = rows;
    renderTopicHall(rows);
    if (!topicId) {
      state.topic = null;
      renderSidebarContext();
      return;
    }
    const topic = await apiFetch(`/reader/research-cases/${topicId}`);
    if (serial !== state.routeSerial) return;
    state.topic = topic;
    try {
      const configs = await apiFetch("/reader/capability-configs");
      state.capabilityConfigs = configs.items || [];
    } catch (_) { state.capabilityConfigs = []; }
    detailRoot.hidden = false;
    if (reuseConversation) {
      state.projectConversationId = currentForm.elements.conversation_id.value || "";
      if (pane === "tasks") ProjectWorkbench.closePanel(false);
      else await ProjectWorkbench.openPanel(pane);
    } else {
      state.projectConversationId = state.projectObjectRoute?.kind === "task"
        && !String(state.projectObjectRoute.id).startsWith("capability-") ? String(state.projectObjectRoute.id) : "";
      if (!state.projectConversationId && !state.routeQuery.has("new")) {
        const history = await apiFetch(`/reader/research-cases/${topic.id}/tasks`);
        if (serial !== state.routeSerial) return;
        const recent = (history.items || []).filter(item => item.kind === "conversation").sort((a,b)=>new Date(b.updated_at)-new Date(a.updated_at))[0];
        state.projectConversationId = recent?.id || "";
      }
      renderTopicWorkspace(topic, "tasks");
      await ProjectWorkbench.mount(topic, pane);
    }
    if (state.projectObjectRoute?.kind !== "task") await renderProjectDeepObject(topic.id, pane, state.projectObjectRoute);
    renderSidebarContext();
  } catch (error) { (topicId ? detailRoot : listRoot).innerHTML = errorState(error.message); if (topicId) detailRoot.hidden = false; }
}

function renderTopicMaterials() {
  const root = document.getElementById("topicMaterialResults");
  if (!root || !state.topic) return;
  const query = (document.getElementById("topicMaterialQuery")?.value || "").trim().toLowerCase();
  const sourceType = document.getElementById("topicSourceFilter")?.value || "";
  const documentType = document.getElementById("topicTypeFilter")?.value || "";
  const rows = (state.topic.documents || []).filter((item) => {
    const text = `${item.title || ""} ${item.source_name || ""}`.toLowerCase();
    return (!query || text.includes(query)) && (!sourceType || item.source_type === sourceType) && (!documentType || item.document_type === documentType);
  });
  const count = document.getElementById("topicMaterialCount");
  if (count) count.textContent = `${rows.length} / ${(state.topic.documents || []).length} 份`;
  if (!rows.some((item) => String(item.document_version_id) === String(state.topicMaterialPreviewId))) state.topicMaterialPreviewId = rows[0]?.document_version_id || null;
  root.innerHTML = rows.length ? rows.map((item) => {
    const selected = isMaterialSelected(item.document_version_id);
    const active = String(item.document_version_id) === String(state.topicMaterialPreviewId);
    return `<article class="topic-material-row${selected ? " is-selected" : ""}${active ? " is-active" : ""}"><button type="button" class="topic-material-open" data-preview-topic-material="${item.document_version_id}"><span>${escapeHtml(displayEnum(DOCUMENT_TYPE_LABELS, item.document_type, "材料"))} · ${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))}</span><strong>${escapeHtml(item.title || "未命名材料")}</strong><small>${escapeHtml(item.source_name || "来源未登记")}</small></button><button type="button" class="material-select-btn${selected ? " is-selected" : ""}" data-toggle-select-material="${item.document_version_id}" data-material-title="${escapeHtml(item.title || "材料")}" data-source-name="${escapeHtml(item.source_name || "信源")}">${selected ? "已选" : "选择"}</button></article>`;
  }).join("") : emptyState("没有符合当前筛选条件的已关联材料");
  renderTopicMaterialPreview(rows);
  renderTopicMaterialActionBar();
}

function renderTopicMaterialPreview(filteredRows = null) {
  const root = document.getElementById("topicMaterialPreview");
  if (!root || !state.topic) return;
  const rows = filteredRows || state.topic.documents || [];
  const item = rows.find((row) => String(row.document_version_id) === String(state.topicMaterialPreviewId));
  if (!item) { root.innerHTML = emptyState("选择左侧材料查看版本与证据摘要"); return; }
  const selected = isMaterialSelected(item.document_version_id);
  root.innerHTML = `<header><span class="precision">${escapeHtml(displayEnum(DOCUMENT_TYPE_LABELS, item.document_type, "材料"))}</span><small>${escapeHtml(displayEnum(DATE_PRECISION_LABELS, item.published_at_precision))}</small></header><h3>${escapeHtml(item.title || "未命名材料")}</h3><p>${escapeHtml(item.abstract || item.summary || "当前版本没有可展示摘要，请打开固定版本核对原文定位。")}</p><dl><div><dt>来源</dt><dd>${escapeHtml(item.source_name || "未登记")}</dd></div><div><dt>发布时间</dt><dd>${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))}</dd></div><div><dt>采集时间</dt><dd>${escapeHtml(localDate(item.observed_at))}</dd></div><div><dt>版本状态</dt><dd>${Number(item.linked_version_count || 1)} 个关联版本 · 已确认</dd></div></dl><div class="topic-material-preview-actions"><button type="button" class="primary-btn" data-material-version="${item.document_version_id}">查看版本与定位</button><button type="button" class="ghost-btn material-select-btn${selected ? " is-selected" : ""}" data-toggle-select-material="${item.document_version_id}" data-material-title="${escapeHtml(item.title || "材料")}" data-source-name="${escapeHtml(item.source_name || "信源")}">${selected ? "取消选择" : "选择材料"}</button><button type="button" class="danger-link" data-remove-topic-material="${item.document_id}">移出项目</button></div>`;
}

function renderTopicMaterialActionBar() {
  const root = document.getElementById("topicMaterialActionbar");
  if (!root || !state.topic) return;
  const topicIds = new Set((state.topic.documents || []).map((item) => String(item.document_version_id)));
  const selected = (state.selectedMaterials || []).filter((item) => topicIds.has(String(item.versionId)));
  root.hidden = !selected.length;
  root.innerHTML = selected.length ? `<strong>已选择 ${selected.length} 份项目材料</strong><span>右侧 AI 将只使用当前项目与所选材料。</span><div><button type="button" class="primary-btn compact" data-topic-selected-ai>在右侧 AI 中整理</button></div>` : "";
}

function renderTopicFeed() {
  const root = document.getElementById("topicFeedResults");
  if (!root || !state.topic) return;
  const docs = state.topic.documents || [];
  const windowDays = state.topicFeedWindow === "7" ? 7 : state.topicFeedWindow === "30" ? 30 : null;
  const now = Date.now();
  const dated = [];
  const unknown = [];
  for (const item of docs) {
    if (!item.published_at) { unknown.push(item); continue; }
    const time = new Date(item.published_at).getTime();
    if (Number.isNaN(time)) { unknown.push(item); continue; }
    if (!windowDays || now - time <= windowDays * 86400000) dated.push(item);
  }
  const datedHtml = dated.length ? materialList(dated.map((item) => ({ ...item, topic_removable: true }))) : emptyState(windowDays ? "该窗口内没有来源声明了发布时间的新材料；未把首次采集时间当作更新。" : "当前项目没有带来源发布日期的材料流。");
  const unknownHtml = !windowDays && unknown.length ? `<section class="report-section"><h3>来源未声明发布时间</h3><p class="method-note">这些材料可以在资料库中查找，但不计入“近 7 天 / 近 30 天动态”。</p>${materialList(unknown.map((item) => ({ ...item, topic_removable: true })))}</section>` : "";
  root.innerHTML = `${datedHtml}${unknownHtml}`;
}

function runStatusLabel(value) {
  return { succeeded: "已生成", insufficient_data: "证据不足", failed: "运行失败", running: "正在运行", queued: "等待运行", planning: "计划讨论中", awaiting_confirmation: "待确认计划", review: "待审阅资料", completed: "已完成", cancelled: "已停止" }[value] || value || "未知状态";
}

function schemaFieldLabel(value) {
  return ({ country_iso3: "国家范围", country_iso3s: "对比国家", topic_slugs: "研究主题", topic_slug: "研究主题", keywords: "关键词", query: "材料筛选问题", weights: "排序权重", comparison_question: "矛盾对照问题", comparison_dimensions: "材料比较维度", date_from: "开始日期", date_to: "结束日期", period_from: "起始时期", period_to: "结束时期", event_id: "事件", event_ids: "事件集合", policy_event_ids: "政策事件", document_version_ids: "文档版本", research_case_id: "专题", research_question: "研究问题", file_refs: "田野资料", source_urls: "授权临时链接", privacy_mode: "隐私模式", indicator_codes: "指标", dataset_keys: "数据集", discipline_labels: "学科标签", source_ids: "信源范围", source_language: "原文语言", target_language: "目标语言", language: "材料语言条件", frequency: "产物频率", template_version: "产物模板", topic_relevance: "主题相关", country_match: "国家匹配", time_coverage: "时间覆盖", source_type: "来源类型", originality: "原始性", deduplication: "去重", existing_evidence: "已有证据" })[value] || value;
}

const SKILL_ARRAY_FIELDS = new Set(["country_iso3s", "topic_slugs", "keywords", "event_ids", "policy_event_ids", "document_version_ids", "indicator_codes", "dataset_keys", "discipline_labels", "source_ids", "comparison_dimensions"]);
const SKILL_NUMBER_ARRAY_FIELDS = new Set(["event_ids", "policy_event_ids", "document_version_ids", "source_ids"]);

function capabilityFieldHtml(key, value) {
  const label = escapeHtml(schemaFieldLabel(key));
  if (key === "research_case_id") {
    const options = (state.topics || []).map((topic) => `<option value="${Number(topic.id)}"${Number(value) === Number(topic.id) ? " selected" : ""}>${escapeHtml(topic.title)}</option>`).join("");
    return `<label><span>${label}</span><select data-skill-field="${key}"><option value="">请选择项目</option>${options}</select></label>`;
  }
  if (key === "research_question") return `<label class="skill-field-wide"><span>${label}</span><textarea data-skill-field="${key}" rows="3">${escapeHtml(value || "")}</textarea></label>`;
  if (["date_from", "date_to"].includes(key)) return `<label><span>${label}</span><input type="date" data-skill-field="${key}" value="${escapeHtml(value || "")}"></label>`;
  if (["source_language", "target_language"].includes(key)) return `<label><span>${label}</span><select data-skill-field="${key}">${[["fr", "法语"], ["zh-CN", "简体中文"], ["en", "英语"]].map(([code, name]) => `<option value="${code}"${value === code ? " selected" : ""}>${name}</option>`).join("")}</select></label>`;
  if (key === "privacy_mode") return `<label><span>${label}</span><select data-skill-field="${key}"><option value="restricted">受限</option><option value="anonymized"${value === "anonymized" ? " selected" : ""}>已匿名</option><option value="shareable"${value === "shareable" ? " selected" : ""}>可共享</option></select></label>`;
  if (key === "weights" || key === "source_urls") return `<label class="skill-field-wide"><span>${label}</span><textarea data-skill-field="${key}" data-json="true" rows="4">${escapeHtml(JSON.stringify(value || (key === "source_urls" ? [] : {}), null, 2))}</textarea><small>${key === "source_urls" ? "每项需包含 url、title 和 access_authorized: true" : "权重会归一化，结果公开每项得分依据"}</small></label>`;
  const shown = Array.isArray(value) ? value.map((item) => typeof item === "object" ? JSON.stringify(item) : item).join(", ") : value ?? "";
  return `<label><span>${label}</span><input type="text" data-skill-field="${key}" data-array="${SKILL_ARRAY_FIELDS.has(key) ? "true" : "false"}" value="${escapeHtml(shown)}" placeholder="${SKILL_ARRAY_FIELDS.has(key) ? "多项用逗号分隔" : "请输入"}"></label>`;
}

function readCapabilityOverrides(form) {
  const overrides = {};
  form.querySelectorAll("[data-skill-field]").forEach((field) => {
    const key = field.dataset.skillField;
    const raw = field.value.trim();
    if (!raw) { overrides[key] = SKILL_ARRAY_FIELDS.has(key) ? [] : null; return; }
    if (key === "research_case_id") { overrides[key] = Number(raw); return; }
    if (field.dataset.json === "true") { overrides[key] = JSON.parse(raw); return; }
    if (field.dataset.array === "true") {
      overrides[key] = raw.split(/[,，]/).map((item) => item.trim()).filter(Boolean).map((item) => SKILL_NUMBER_ARRAY_FIELDS.has(key) ? Number(item) : item);
      return;
    }
    overrides[key] = raw;
  });
  return overrides;
}

function skillReadiness(item) {
  const config = item.configs?.[0];
  if (item.status === "draft") return { key: "draft", label: "等待审核" };
  if (config?.can_run) return { key: "ready", label: "输入已就绪" };
  return { key: "blocked", label: (config?.prerequisites || []).length ? `缺少必需输入：${config.prerequisites.map(schemaFieldLabel).join("、")}` : "缺少活动配置或必需证据" };
}

function documentSkillCode(item) {
  const code = String(item.skill_manifest?.skill_code || "").toUpperCase();
  return /^S\d{2}$/.test(code) ? code : "";
}

function documentSkillRows() {
  return (state.capabilities || [])
    .filter((item) => documentSkillCode(item))
    .sort((left, right) => documentSkillCode(left).localeCompare(documentSkillCode(right), "zh-CN", { numeric: true }));
}

function skillCategoryTag(item) {
  const code = documentSkillCode(item);
  const manifest = item.skill_manifest || {};
  if (/^S(?:0[1-9]|10)$/.test(code)) return "平台精选";
  if (["event-evidence-matrix", "topic-digest"].includes(item.slug)) return "组合流程";
  if (manifest.category === "general_improved" || item.slug.includes("general")) return "学科改良";
  if (manifest.category === "co_build") return "学者共建";
  return "能力扩展";
}

function skillIconName(item) {
  const code = documentSkillCode(item);
  if (code && { S01: "globe", S02: "book-open", S03: "newspaper", S04: "clock", S05: "file-text", S06: "layers", S07: "chart", S08: "landmark", S09: "check-circle", S10: "search" }[code]) {
    return { S01: "globe", S02: "book-open", S03: "newspaper", S04: "clock", S05: "file-text", S06: "layers", S07: "chart", S08: "landmark", S09: "check-circle", S10: "search" }[code];
  }
  if (item.slug === "event-evidence-matrix") return "layers";
  if (item.slug === "topic-digest") return "zap";
  if (item.slug.includes("policy") || item.slug.includes("dynamics")) return "newspaper";
  if (item.slug.includes("comparison") || item.slug.includes("trade")) return "chart";
  if (item.slug.includes("material") || item.slug.includes("document")) return "file-text";
  return "sparkles";
}

function skillColorClass(item) {
  const code = documentSkillCode(item);
  if (code && { S01: "icon-s01", S02: "icon-s02", S03: "icon-s03", S04: "icon-s04", S05: "icon-s05", S06: "icon-s06", S07: "icon-s07", S08: "icon-s08", S09: "icon-s09" }[code]) {
    return { S01: "icon-s01", S02: "icon-s02", S03: "icon-s03", S04: "icon-s04", S05: "icon-s05", S06: "icon-s06", S07: "icon-s07", S08: "icon-s08", S09: "icon-s09" }[code];
  }
  if (["event-evidence-matrix", "topic-digest"].includes(item.slug)) return "icon-workflow";
  if (item.skill_manifest?.category === "general_improved") return "icon-general";
  if (item.skill_manifest?.category === "co_build") return "icon-custom";
  return "icon-s01";
}

function skillDisplayName(item) {
  return String(item.name || "未命名 Skill").replace(/^S\d{2}\s*/i, "");
}

function getInstalledSkillSlugs() {
  try {
    const raw = localStorage.getItem("guobie_installed_skills");
    if (raw) return JSON.parse(raw);
  } catch (_) {}
  return [
    "country-background-dossier",
    "key-frontiers-policy",
    "event-timeline-extraction",
    "fieldwork-interview-dossier",
    "cross-country-comparison",
    "event-evidence-matrix",
    "topic-digest"
  ];
}

function isSkillInstalled(slug) {
  const list = getInstalledSkillSlugs();
  return list.includes(slug);
}

function toggleInstallSkill(slug) {
  let list = getInstalledSkillSlugs();
  const index = list.indexOf(slug);
  let installed = false;
  if (index >= 0) {
    list.splice(index, 1);
    installed = false;
  } else {
    list.push(slug);
    installed = true;
  }
  localStorage.setItem("guobie_installed_skills", JSON.stringify(list));
  return installed;
}

function skillMarketItemHtml(item) {
  const config = item.configs?.[0];
  const latest = config?.latest_run;
  const manifest = item.skill_manifest || {};
  const isDraft = item.status === "draft";
  const readiness = skillReadiness(item);
  const summary = manifest.purpose || item.description || "文档定义的区域国别研究能力。";
  const code = documentSkillCode(item);
  const tag = skillCategoryTag(item);
  const colorClass = skillColorClass(item);
  const iconName = skillIconName(item);
  const scopes = manifest.supported_scopes || [];
  const scopeLabels = scopes.map((s) => ({ country: "国别", topic: "项目", event: "事件", document: "文档" }[s] || s)).join("、");
  const installed = isSkillInstalled(item.slug);

  let action = "";
  let statusBadge = "";

  if (isDraft) {
    statusBadge = `<span class="skill-tag-badge is-draft">规范草稿</span>`;
    action = `<button type="button" class="skill-market-action is-draft" data-open-skill="${escapeHtml(item.slug)}">查看规范</button>`;
  } else if (installed) {
    statusBadge = `<span class="skill-tag-badge is-installed"><svg class="ui-icon" style="width:11px;height:11px;" aria-hidden="true"><use href="./assets/lucide-sprite.svg#check"></use></svg>已安装</span>`;
    action = `<button type="button" class="skill-market-action is-installed" data-open-skill="${escapeHtml(item.slug)}"><svg class="ui-icon" aria-hidden="true"><use href="./assets/lucide-sprite.svg#settings"></use></svg>配置调用</button>`;
  } else {
    statusBadge = `<span class="skill-tag-badge is-available">${escapeHtml(tag)}</span>`;
    action = `<button type="button" class="skill-market-action is-get" data-install-skill="${escapeHtml(item.slug)}"><svg class="ui-icon" aria-hidden="true"><use href="./assets/lucide-sprite.svg#download-cloud"></use></svg>获取安装</button>`;
  }

  return `<article class="skill-market-item${isDraft ? " is-draft" : ""}${installed ? " is-installed" : ""}" data-skill-slug="${escapeHtml(item.slug)}">
    <div class="skill-market-icon ${colorClass}">${iconSvg(iconName)}</div>
    <div class="skill-market-copy">
      <div class="skill-market-title">
        <h3>${escapeHtml(skillDisplayName(item))}</h3>
        ${code ? `<span class="skill-code-badge">${escapeHtml(code)}</span>` : ""}
        ${statusBadge}
      </div>
      <p>${escapeHtml(summary)}</p>
      <div class="skill-market-meta-row">
        <span class="is-${escapeHtml(readiness.key)}">${escapeHtml(readiness.label)}</span>
        <span>·</span>
        <span>${latest ? `最近运行 #${latest.id}` : `v${escapeHtml(item.version)}`}</span>
        ${scopeLabels ? `<span>· 适用: ${escapeHtml(scopeLabels)}</span>` : ""}
      </div>
    </div>
    <div class="skill-market-actions">
      ${action}
    </div>
  </article>`;
}

function installedSkillHtml(item) {
  const code = documentSkillCode(item);
  const colorClass = skillColorClass(item);
  const iconName = skillIconName(item);
  return `<button type="button" class="installed-skill-button" data-open-skill="${escapeHtml(item.slug)}" title="${escapeHtml(item.display_name || item.name)}">
    <span class="skill-market-icon ${colorClass}">${iconSvg(iconName)}</span>
    <span><b>${escapeHtml(code || "FLOW")}</b><small>${escapeHtml(skillDisplayName(item))}</small></span>
  </button>`;
}

function isOfficialSkill(item) {
  const code = documentSkillCode(item);
  return Boolean(item.catalog_visibility === "skill_catalog" && code && /^S(?:0[1-9]|10)$/.test(code));
}

function renderCapabilityHallRows() {
  const root = document.getElementById("capabilityGrid");
  const workflowRoot = document.getElementById("communityCapabilityGrid");
  const myRoot = document.getElementById("myCapabilityGrid");
  const installedRoot = document.getElementById("skillInstalledStrip");
  const installedCount = document.getElementById("skillInstalledCount");
  const visibleCountEl = document.getElementById("skillVisibleCount");
  const workflowCountEl = document.getElementById("communitySkillCount");
  if (!root) return;

  const query = state.skillFilterQuery.trim().toLowerCase();
  const activeCategory = state.skillCategory || "all";

  const matchesQuery = (item) => {
    const manifest = item.skill_manifest || {};
    const text = `${item.name || ""} ${item.slug || ""} ${item.description || ""} ${manifest.skill_code || ""} ${manifest.purpose || ""} ${(manifest.supported_scopes || []).join(" ")}`.toLowerCase();
    return !query || text.includes(query);
  };

  const matchesCategory = (item) => {
    if (activeCategory === "all") return true;
    const code = documentSkillCode(item);
    const manifest = item.skill_manifest || {};
    if (activeCategory === "popular") {
      return ["S01", "S02", "S04", "S05", "S07", "S08"].includes(code);
    }
    if (activeCategory === "vertical") {
      return /^S(?:0[1-9]|10)$/.test(code);
    }
    if (activeCategory === "workflow") {
      return ["event-evidence-matrix", "topic-digest"].includes(item.slug) || manifest.category === "workflow";
    }
    if (activeCategory === "general") {
      return code === "S02" || manifest.category === "general_improved" || manifest.category === "co_build" || item.slug.includes("general") || item.slug.includes("community");
    }
    return true;
  };

  const allRows = (state.capabilities || [])
    .filter((item) => item.catalog_visibility === "skill_catalog")
    .sort((left, right) => documentSkillCode(left).localeCompare(documentSkillCode(right), "zh-CN", { numeric: true }));

  const publicRows = allRows.filter((item) => matchesQuery(item) && matchesCategory(item));
  const officialRows = publicRows.filter(isOfficialSkill);
  const workflowRows = [];

  const installedRows = allRows.filter((item) => isSkillInstalled(item.slug));
  const myRows = installedRows.filter(matchesQuery);

  root.innerHTML = officialRows.length ? officialRows.map(skillMarketItemHtml).join("") : emptyState("没有匹配的官方标准 Skill");
  if (workflowRoot) {
    workflowRoot.closest(".skill-group-block")?.setAttribute("hidden", "");
  }
  if (myRoot) myRoot.innerHTML = myRows.length ? myRows.map(skillMarketItemHtml).join("") : emptyState("当前没有已启用的研究 Skill；可在平台能力中选择并配置。");
  if (installedRoot) installedRoot.innerHTML = installedRows.length ? installedRows.map(installedSkillHtml).join("") : emptyState("暂无已安装能力");
  if (installedCount) installedCount.textContent = `${installedRows.length} 个已启用`;
  if (visibleCountEl) visibleCountEl.textContent = `共 ${officialRows.length} 项官方标准能力`;
  if (workflowCountEl) workflowCountEl.textContent = "内部流程不在 Skill 广场展示";
}

async function loadCapabilities(autoOpenLatest = false) {
  const root = document.getElementById("capabilityGrid");
  const installedRoot = document.getElementById("skillInstalledStrip");
  if (root) root.innerHTML = `<div class="empty-state">正在读取研究 Skill…</div>`;
  if (installedRoot) installedRoot.innerHTML = `<div class="empty-state">正在读取已安装能力…</div>`;
  try {
    const [rows, topics] = await Promise.all([
      apiFetch("/reader/capabilities"),
      apiFetch("/reader/research-cases"),
    ]);
    state.capabilities = rows;
    state.topics = topics;
    renderCapabilityHallRows();
    const digest = rows.find((item) => item.slug === "topic-digest")?.configs?.[0]?.latest_run;
    if (digest && autoOpenLatest) await viewCapabilityRun(digest.id);
  } catch (error) { if (root) root.innerHTML = errorState(error.message); }
}

function openCustomSkillModal() {
  const dialog = document.getElementById("skillCustomDialog");
  if (!dialog) return;
  document.getElementById("skillCustomForm")?.reset();
  const status = document.getElementById("customSkillStatus");
  if (status) status.textContent = "";
  dialog.showModal();
}

function closeCustomSkillModal() {
  document.getElementById("skillCustomDialog")?.close();
}

async function submitCustomSkill(event) {
  event.preventDefault();
  const status = document.getElementById("customSkillStatus");
  const submitBtn = document.getElementById("customSkillSubmit");
  const name = document.getElementById("customSkillName")?.value.trim();
  const slug = document.getElementById("customSkillSlug")?.value.trim();
  const scope = document.getElementById("customSkillScope")?.value || "topic";
  const category = document.getElementById("customSkillCategory")?.value || "co_build";
  const purpose = document.getElementById("customSkillPurpose")?.value.trim();
  const steps = (document.getElementById("customSkillSteps")?.value || "").split("\n").map((s) => s.trim()).filter(Boolean);
  const checkpoints = (document.getElementById("customSkillCheckpoints")?.value || "").split("\n").map((s) => s.trim()).filter(Boolean);

  if (!name || !slug || !purpose) {
    if (status) status.textContent = "请完整填写 Skill 标题、标识和用途。";
    return;
  }

  const manifest = {
    category: category,
    source_channel: category === "general_improved" ? "学科改良通用能力" : "学者共建",
    localization_sources: ["区域国别研究场景"],
    supported_scopes: [scope],
    purpose: purpose,
    steps: steps.length ? steps : ["冻结所选研究对象与时间范围", "组织已复核材料与证据", "输出带引用产物与缺口"],
    human_checkpoints: checkpoints.length ? checkpoints : ["运行前确认输入快照", "写回前人工复核"],
    writeback_targets: [scope === "country" ? "country" : scope === "event" ? "event" : "research_case"],
    permissions: ["read_reviewed_evidence"],
    evidence_policy: "只使用 reviewed/confirmed 库内证据；联网信息仅作未复核补充。",
    cost_risk: { cost: "低", risk: "证据不足时输出缺口" }
  };

  const payload = {
    slug: slug,
    version: "0.1.0",
    name: name,
    description: purpose,
    input_schema: { allowed_fields: [scope === "country" ? "country_iso3" : scope === "event" ? "event_ids" : "research_case_id"], required: [scope === "country" ? "country_iso3" : "research_case_id"] },
    output_schema: { type: `${slug.replace(/-/g, "_")}_output`, source_trace_required: true },
    manifest: manifest
  };

  if (submitBtn) submitBtn.disabled = true;
  if (status) status.textContent = "正在提交草稿…";

  try {
    const res = await apiFetch("/reader/skills/drafts", { method: "POST", body: JSON.stringify(payload) });
    showToast(`Skill 草稿「${name}」已创建，等待平台审核`);
    closeCustomSkillModal();
    await loadCapabilities();
  } catch (error) {
    if (status) status.textContent = `保存失败：${error.message}`;
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function openUploadSkillModal() {
  const dialog = document.getElementById("skillUploadDialog");
  if (!dialog) return;
  document.getElementById("skillUploadForm")?.reset();
  const status = document.getElementById("skillUploadStatus");
  if (status) status.textContent = "";
  dialog.showModal();
}

function closeUploadSkillModal() {
  document.getElementById("skillUploadDialog")?.close();
}

async function submitUploadSkill(event) {
  event.preventDefault();
  const status = document.getElementById("skillUploadStatus");
  const submitBtn = document.getElementById("skillUploadSubmit");
  const rawJson = document.getElementById("skillUploadJson")?.value.trim();

  if (!rawJson) {
    if (status) status.textContent = "请粘贴 JSON 配置。";
    return;
  }

  let payload;
  try {
    payload = JSON.parse(rawJson);
  } catch (err) {
    if (status) status.textContent = `JSON 格式错误：${err.message}`;
    return;
  }

  if (submitBtn) submitBtn.disabled = true;
  if (status) status.textContent = "正在校验并导入…";

  try {
    const res = await apiFetch("/reader/skills/drafts", { method: "POST", body: JSON.stringify(payload) });
    showToast(`Skill 规范「${res.name || payload.name}」已成功导入`);
    closeUploadSkillModal();
    await loadCapabilities();
  } catch (error) {
    if (status) status.textContent = `导入失败：${error.message}`;
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function openManageSkillsModal() {
  const dialog = document.getElementById("skillManageDialog");
  const listEl = document.getElementById("skillManageList");
  if (!dialog || !listEl) return;

  const activeRows = (state.capabilities || []).filter((item) => item.status !== "draft");
  listEl.innerHTML = activeRows.length
    ? activeRows.map((item) => {
        const config = item.configs?.[0];
        const latest = config?.latest_run;
        const colorClass = skillColorClass(item);
        const iconName = skillIconName(item);
        const code = documentSkillCode(item);
        return `<div class="skill-manage-item">
          <div class="skill-market-icon ${colorClass}">${iconSvg(iconName)}</div>
          <div class="skill-manage-item-info">
            <strong>${escapeHtml(item.display_name || item.name)} ${code ? `(${escapeHtml(code)})` : ""}</strong>
            <small>${latest ? `最近运行 #${latest.id} · ${escapeHtml(runStatusLabel(latest.status))}` : "尚未运行"}</small>
          </div>
          <button type="button" class="ghost-btn compact" data-open-skill="${escapeHtml(item.slug)}" data-close-manage>打开</button>
        </div>`;
      }).join("")
    : emptyState("当前没有已启用的 Skill");

  dialog.showModal();
}

function closeManageSkillsModal() {
  document.getElementById("skillManageDialog")?.close();
}

function showCapabilityWorkspace(title, freshness = "") {
  document.getElementById("capabilityHall").hidden = true;
  document.getElementById("capabilityWorkspace").hidden = false;
  document.getElementById("capabilityToolbar").innerHTML = `<div class="toolbar-left"><a class="ghost-btn" href="#/capabilities">← 返回专属能力库</a><div class="toolbar-divider"></div><div class="country-current-badge"><strong>${escapeHtml(title)}</strong></div></div><div class="toolbar-center"><p class="object-eyebrow">产物写回项目，不替代研究判断</p></div><div class="toolbar-right">${freshness ? `<span class="country-freshness">${escapeHtml(freshness)}</span>` : ""}${copilotToggleHtml()}</div>`;
}

function openCapabilitySkill(slug, pane = "overview") {
  const item = (state.capabilities || []).find((row) => row.slug === slug);
  const hall = document.getElementById("capabilityHall");
  const workspace = document.getElementById("capabilityWorkspace");
  const root = document.getElementById("capabilityRunResult");
  if (!item) {
    showToast("未找到该区域国别能力");
    hall.hidden = false;
    workspace.hidden = true;
    return;
  }
  state.activeCapabilitySlug = slug;
  state.capabilityPane = pane;
  const config = item.configs?.[0];
  state.activeCapabilityConfigId = config?.id || null;
  document.getElementById("capabilityTabs").hidden = false;
  const meta = SKILL_META[slug] || {};
  const manifest = item.skill_manifest || {};
  showCapabilityWorkspace(item.name, item.status === "draft" ? "等待审核" : config?.latest_run ? `上次运行 ${runStatusLabel(config.latest_run.status)}` : "尚未运行");
  document.getElementById("capabilityTabs").innerHTML = workspaceTabsHtml([
    { id: "overview", label: "说明", href: `#/capabilities/${slug}` },
    { id: "io", label: "输入输出", href: `#/capabilities/${slug}/io` },
    { id: "configure", label: "配置运行", href: `#/capabilities/${slug}/configure` },
    { id: "runs", label: "运行记录", href: `#/capabilities/${slug}/runs` },
    { id: "review", label: "文档依据", href: `#/capabilities/${slug}/review` },
  ], pane);
  root.hidden = false;
  if (pane === "io") {
    root.innerHTML = `<section class="report-section"><h3>输入、输出与人工检查点</h3><div class="skill-io-grid"><article><span>输入对象</span><strong>${escapeHtml((item.input_schema?.allowed_fields || []).map(schemaFieldLabel).join("、") || "当前研究作用域")}</strong></article><article><span>输出类型</span><strong>${escapeHtml(item.output_schema?.type || "带引用研究产物")}</strong></article><article><span>允许写回</span><strong>${escapeHtml((manifest.writeback_targets || []).map(schemaFieldLabel).join("、") || "不写回")}</strong></article></div><h4>人工检查点</h4><ol class="skill-steps">${(manifest.human_checkpoints || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("") || "<li>运行前检查输入快照，运行后确认是否写回。</li>"}</ol><details class="developer-details"><summary>查看 Schema 高级信息</summary><pre>${escapeHtml(JSON.stringify({ input_schema: item.input_schema, output_schema: item.output_schema }, null, 2))}</pre></details></section>`;
  } else if (pane === "configure") {
    const configEntries = Object.entries(config?.config || {});
    const allowedFields = (item.input_schema?.allowed_fields || []).filter((key) => !["file_refs", "transcript_items", "template_version"].includes(key));
    const readiness = skillReadiness(item);
    const primaryRunAction = slug === "field-material-organizer" && !config?.can_run
      ? `<a class="primary-btn link-button" href="#/projects">去项目材料与田野选择资料</a><span>该 Skill 只从同一项目内冻结用户明确选择的资料。</span>`
      : `<button type="submit" class="primary-btn">预览输入快照并运行</button>`;
    const runActions = item.status === "draft"
      ? `<span>等待平台审核，当前不可执行。</span>`
      : `${primaryRunAction}<button type="button" class="ghost-btn" data-use-skill="${escapeHtml(manifest.starter_prompt || meta.task || item.description)}" data-focus-type="skill" data-focus-key="${item.id}" data-focus-label="${escapeHtml(item.display_name || item.name)}">交给 AI 协助配置</button>`;
    root.innerHTML = `<form class="report-section" data-skill-config-form="${config?.id || ""}"><div class="skill-run-heading"><div><h3>配置与运行检查</h3><p>直接修改中文表单；确认时预览并冻结本次输入，不会自动写回。</p></div><span class="status-chip is-${escapeHtml(readiness.key)}">${escapeHtml(readiness.label)}</span></div><div class="skill-config-form">${allowedFields.length ? allowedFields.map((key) => capabilityFieldHtml(key, config?.config?.[key])).join("") : emptyState("当前 Skill 不需额外配置")}</div><div class="skill-run-assurance"><article><span>证据政策</span><strong>${escapeHtml(manifest.evidence_policy || "只使用当前作用域内允许的证据")}</strong></article><article><span>预计成本与风险</span><strong>${escapeHtml(Object.values(manifest.cost_risk || {}).join(" · ") || "运行前检查输入，证据不足时不生成正式产物")}</strong></article></div>${configEntries.length ? `<details class="developer-details"><summary>高级信息：Schema 与当前值</summary><pre>${escapeHtml(JSON.stringify({ input_schema: item.input_schema, config: config?.config || {} }, null, 2))}</pre></details>` : ""}${(manifest.steps || []).length ? `<h4>运行步骤</h4><ol class="skill-steps">${manifest.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>` : ""}<div class="run-review-actions">${runActions}<span>建议下一 Skill 不会自动触发。</span></div></form>`;
  } else if (pane === "runs") {
    root.innerHTML = `<section class="report-section"><h3>运行记录</h3>${config?.latest_run ? `<button type="button" class="object-link-card" data-view-capability-run="${config.latest_run.id}"><b>运行 #${config.latest_run.id}</b><span>${escapeHtml(runStatusLabel(config.latest_run.status))} · ${escapeHtml(localDate(config.latest_run.finished_at || config.latest_run.created_at))}</span></button>` : emptyState("当前配置尚无运行记录")}<p class="method-note">每次运行保存冻结输入、状态、引用和人工写回记录。</p></section>`;
  } else if (pane === "review") {
    root.innerHTML = `<section class="report-section"><h3>需求文档依据与开放边界</h3><div class="skill-review-grid"><article><span>功能编号</span><strong>${escapeHtml(documentSkillCode(item) || "非 S 编号流程")}</strong></article><article><span>当前状态</span><strong>${escapeHtml(item.status === "draft" ? "规范草稿 · 执行器未开放" : "已按文档实现并可运行")}</strong></article><article><span>来源渠道</span><strong>${escapeHtml(manifest.source_channel || "需求文档")}</strong></article><article><span>权限</span><strong>${escapeHtml((manifest.permissions || []).join("、") || "无外部执行权限")}</strong></article><article><span>人工检查点</span><strong>${escapeHtml((manifest.human_checkpoints || []).join("、") || "运行与写回均需确认")}</strong></article><article><span>成本与风险</span><strong>${escapeHtml(Object.values(manifest.cost_risk || {}).join(" · ") || "待登记")}</strong></article></div><p class="method-note">当前需求目录为 S01–S11，协作权限单列为平台功能；内部执行器的运行不等于完整需求验收。</p></section>`;
  } else {
    root.innerHTML = `<section class="report-section"><h3>Skill 说明</h3><p>${escapeHtml(manifest.purpose || meta.task || item.description)}</p><div class="skill-overview-grid"><article><span>编号与版本</span><strong>${escapeHtml(manifest.skill_code || "平台组合流程")} · v${escapeHtml(item.version)}</strong></article><article><span>来源</span><strong>${escapeHtml(manifest.source_channel || "平台自研")}</strong></article><article><span>适用空间</span><strong>${escapeHtml((manifest.supported_scopes || []).join("、") || "当前研究场景")}</strong></article><article><span>证据规则</span><strong>${escapeHtml(manifest.evidence_policy || "使用当前作用域内已复核证据")}</strong></article></div><div class="inline-actions"><a class="primary-btn link-button" href="#/capabilities/${escapeHtml(slug)}/configure">进入配置运行</a><a class="ghost-btn link-button" href="#/capabilities/${escapeHtml(slug)}/io">查看输入输出</a></div></section>`;
  }
  renderSidebarContext();
}

function capabilityValidationLabel(value) {
  return ({ verified: "已验证", pending: "待验证", failed: "未通过" })[value] || value || "待验证";
}

function capabilityCategoryLabel(value) {
  return ({ research_workflow: "研究工作流", data_collection: "数据与采集", field_collaboration: "田野与协作" })[value] || value;
}

function currentCapabilityContext() {
  const projectId = Number(state.routeQuery?.get("project") || 0) || null;
  const country = (state.routeQuery?.get("country") || "").toUpperCase();
  const project = projectId ? (state.topics || []).find((item) => Number(item.id) === projectId) : null;
  return project ? { type: "research_case", key: String(project.id), label: project.title, query: `project=${project.id}` } : country ? { type: "country", key: country, label: country, query: `country=${encodeURIComponent(country)}` } : null;
}

function renderCapabilityContextBanner() {
  const main = document.querySelector(".capability-market-main");
  if (!main) return;
  main.querySelector(".capability-context-banner")?.remove();
  const context = currentCapabilityContext();
  if (!context) return;
  main.insertAdjacentHTML("afterbegin", `<section class="capability-context-banner"><div><span>${context.type === "research_case" ? "当前项目" : "当前国别"}</span><strong>${escapeHtml(context.label)}</strong><small>仅展示可用于此上下文的能力；复制后进入“我的能力”配置、预览和运行。</small></div><a href="#/capabilities">清除上下文</a></section>`);
}

function showCapabilityHall() {
  state.activeCapabilitySlug = null;
  state.activeCapabilityRunId = null;
  document.getElementById("capabilityHall").hidden = false;
  document.getElementById("capabilityWorkspace").hidden = true;
  document.getElementById("capabilityRunResult").hidden = true;
  renderCapabilityContextBanner();
  renderCapabilityCatalogRows();
}

function downloadedCapabilityConfig(item, context = currentCapabilityContext()) {
  return (state.capabilityConfigs || []).find((config) => {
    if (config.catalog_key !== item.catalog_key) return false;
    if (context?.type === "research_case") return config.scope_type === "research_case" && Number(config.research_case_id) === Number(context.key);
    return config.scope_type === "personal";
  });
}

function capabilityInitialConfig(item, context = currentCapabilityContext()) {
  if (context?.type !== "country") return {};
  const fields = item.input_schema?.allowed_fields || [];
  if (fields.includes("country_iso3")) return { country_iso3: context.key };
  if (fields.includes("country_iso3s")) return { country_iso3s: [context.key] };
  return {};
}

async function directDownloadCapability(catalogKey, button) {
  const item = (state.capabilityCatalog || []).find((entry) => entry.catalog_key === catalogKey);
  if (!item) return;
  const context = currentCapabilityContext();
  const existing = downloadedCapabilityConfig(item, context);
  if (existing) {
    navigate(`/capabilities/mine/${existing.id}`);
    return;
  }
  const projectId = context?.type === "research_case" ? Number(context.key) : null;
  button.disabled = true;
  button.textContent = "下载中…";
  try {
    const payload = await apiFetch(`/reader/capabilities/catalog/${encodeURIComponent(catalogKey)}/copies`, {
      method: "POST",
      body: JSON.stringify({
        name: `${item.name}·我的配置`,
        scope_type: projectId ? "research_case" : "personal",
        research_case_id: projectId,
        config: capabilityInitialConfig(item, context),
      }),
    });
    state.capabilityConfigs.unshift({ ...payload, catalog_key: item.catalog_key });
    renderCapabilityCatalogRows();
    showToast(payload.can_run ? "已下载到我的能力；请进入配置并完成实跑验证" : "已下载为待验证草稿");
  } catch (error) {
    showToast(`下载失败：${error.message}`);
    button.disabled = false;
    button.textContent = "直接下载";
  }
}

function renderCapabilityCatalogRows() {
  const root = document.getElementById("capabilityGrid");
  if (!root) return;
  const query = (document.getElementById("skillQueryFilter")?.value || state.skillFilterQuery || "").trim().toLowerCase();
  const validation = document.getElementById("capabilityValidationFilter")?.value || "";
  const scope = document.getElementById("capabilityScopeFilter")?.value || "";
  const category = state.skillCategory || "all";
  const context = currentCapabilityContext();
  const items = (state.capabilityCatalog || []).filter((item) => {
    const matchesQuery = !query || `${item.name} ${item.description} ${item.catalog_key}`.toLowerCase().includes(query);
    const matchesContext = !context || (item.supported_scopes || []).includes(context.type);
    return matchesQuery && matchesContext && (!validation || item.validation_status === validation) && (!scope || (item.supported_scopes || []).includes(scope)) && (category === "all" || item.category === category);
  });
  document.getElementById("skillVisibleCount").textContent = `${items.length} / ${(state.capabilityCatalog || []).length} 项`;
  const contextQuery = context ? `?${context.query}` : "";
  root.innerHTML = items.length ? items.map((item) => {
    const downloaded = downloadedCapabilityConfig(item, context);
    const demoCode = window.ReaderAuth?.status?.demo_available && ["research-s03", "research-s05"].includes(item.catalog_key) ? item.catalog_key.slice(-3) : "";
    const downloadAction = demoCode
      ? `<button class="capability-download-action" data-demo-result-code="${demoCode}">查看 Demo 成果</button>`
      : downloaded
      ? `<a class="capability-downloaded-link" href="#/capabilities/mine/${downloaded.id}">已下载 · 去配置</a>`
      : `<button type="button" class="capability-download-action" data-direct-download-capability="${escapeHtml(item.catalog_key)}">直接下载</button>`;
    return `<article class="capability-catalog-card is-${escapeHtml(item.validation_status)}"><header><span>${escapeHtml(capabilityCategoryLabel(item.category))}</span><b>${escapeHtml(capabilityValidationLabel(item.validation_status))}</b></header><h3>${escapeHtml(item.skill_code || "")} ${escapeHtml(item.display_name || item.name)}</h3><p>${escapeHtml(item.requirement_contract?.explanation || item.description)}</p><p class="capability-card-scenario"><strong>应用场景</strong> ${escapeHtml(item.requirement_contract?.application_scene || "")}</p><div class="capability-card-tags">${(item.supported_scopes || []).map((scopeName) => `<span>${escapeHtml(scopeName === "research_case" ? "项目" : scopeName === "country" ? "国别" : scopeName === "event" ? "事件" : scopeName === "document" ? "资料" : scopeName)}</span>`).join("")}<span>v${escapeHtml(item.version)}</span><span>${escapeHtml(item.maintenance_status === "maintained" ? "持续维护" : item.maintenance_status || "持续维护")}</span></div><dl><div><dt>权限</dt><dd>范围内受控证据</dd></div><div><dt>成本</dt><dd>运行前估算</dd></div></dl><footer><span>${item.can_run ? "可运行 · 写回需确认" : `禁止运行 · ${Number(item.missing_acceptance?.length || 0)} 项缺口`}</span><div class="capability-card-actions">${downloadAction}<a href="#/capabilities/catalog/${escapeHtml(item.catalog_key)}${contextQuery}">查看详情 →</a></div></footer></article>`;
  }).join("") : emptyState("没有匹配的能力。");
  root.querySelectorAll("[data-demo-result-code]").forEach(button => {
    button.onclick = () => window.FieldDemo.result(button.dataset.demoResultCode);
  });
}

async function loadCapabilityCatalog() {
  const root = document.getElementById("capabilityGrid");
  if (root) root.innerHTML = `<div class="empty-state">正在读取区域国别研究 Skills…</div>`;
  try {
    const [catalog, adapter, topics, configs] = await Promise.all([
      apiFetch("/reader/capabilities/catalog"),
      apiFetch("/reader/capabilities"),
      apiFetch("/reader/research-cases"),
      apiFetch("/reader/capability-configs"),
    ]);
    state.capabilityCatalog = catalog.items || [];
    state.capabilities = adapter || [];
    state.topics = topics || [];
    state.capabilityConfigs = configs.items || [];
    renderCapabilityContextBanner();
    renderCapabilityCatalogRows();
  } catch (error) {
    if (root) root.innerHTML = errorState(error.message);
  }
}

function showCapabilityDocument(title, meta = "") {
  document.getElementById("capabilityHall").hidden = true;
  document.getElementById("capabilityWorkspace").hidden = false;
  document.getElementById("capabilityTabs").hidden = true;
  document.getElementById("capabilityToolbar").innerHTML = `<div class="toolbar-left"><a class="ghost-btn" href="#/capabilities">← 能力空间</a><div class="toolbar-divider"></div><div class="country-current-badge"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(meta)}</small></div></div><div class="toolbar-right">${copilotToggleHtml()}</div>`;
  const root = document.getElementById("capabilityRunResult");
  root.hidden = false;
  return root;
}

async function openCapabilityCatalogItem(catalogKey) {
  const root = showCapabilityDocument("能力详情", catalogKey);
  root.innerHTML = detailSkeleton(4);
  try {
    const item = await apiFetch(`/reader/capabilities/catalog/${encodeURIComponent(catalogKey)}`);
    state.activeCapabilitySlug = catalogKey;
    const context = currentCapabilityContext();
    const contract = item.requirement_contract || {};
    const requirementHtml = Object.entries({application_scene: "应用场景", explanation: "具体说明", english_name: "英文名称", scenario: "研究场景", inputs: "输入", actions: "处理动作", outputs: "输出", writeback: "结果关联位置", boundary: "使用边界", implementation_note: "当前支持与待验收范围", priority: "实施次序"}).filter(([key]) => contract[key]).map(([key, label]) => `<section><h2>${label}</h2><p>${escapeHtml(contract[key])}</p></section>`).join("");
    root.innerHTML = `<div class="capability-detail-layout"><main><header class="capability-detail-hero"><div><span>${escapeHtml(capabilityCategoryLabel(item.category))} · v${escapeHtml(item.version)}</span><h1>${escapeHtml(item.display_name || item.name)}</h1><p>${escapeHtml(item.skill_code || "")} · ${escapeHtml(item.name)}</p><p>${escapeHtml(item.description)}</p></div><b class="is-${escapeHtml(item.validation_status)}">${escapeHtml(capabilityValidationLabel(item.validation_status))}</b></header>${requirementHtml}<section><h2>证据、权利与人工检查</h2><p>${escapeHtml(item.evidence_policy || "只读取当前范围受控证据。")}</p><ol class="skill-steps">${(item.human_checkpoints || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol></section><section><h2>工程验证状态</h2>${item.missing_acceptance?.length ? `<ul class="evidence-gap-list">${item.missing_acceptance.map((gap) => `<li>${escapeHtml(gap)}</li>`).join("")}</ul>` : `<p class="readiness is-ready">已通过服务端白名单与运行门禁工程检查；候选结果仍须人工审阅，尚不代表专家验收或正式写回。</p>`}</section></main><aside><section><h3>复制为私有副本</h3><p>复制后进入“我的能力”，先绑定国家或项目、补齐声明式输入并运行预览；预览通过后才运行候选任务。复制时会冻结模板、清单和执行计划版本，待验证能力只能保存为草稿。</p><form data-copy-capability-form="${escapeHtml(item.catalog_key)}"><label>副本名称<input name="name" required value="${escapeHtml(item.display_name || item.name)}·我的配置"></label><label>范围<select name="scope_type"><option value="personal">个人</option><option value="research_case">项目</option></select></label><label>项目<select name="research_case_id"><option value="">个人副本无需选择</option>${(state.topics || []).map((topic) => `<option value="${topic.id}">${escapeHtml(topic.title)}</option>`).join("")}</select></label><button type="submit" class="primary-btn">下载为私有副本</button></form></section><section><h3>版本与维护</h3><dl><div><dt>维护者</dt><dd>${escapeHtml(item.maintainer)}</dd></div><div><dt>维护状态</dt><dd>${escapeHtml(item.maintenance_status)}</dd></div><div><dt>运行权限</dt><dd>${item.can_run ? "已开放" : "未开放"}</dd></div></dl></section></aside></div>`;
    if (["research-s03", "research-s05"].includes(catalogKey)) root.querySelector("aside").insertAdjacentHTML("afterbegin", `<section><h3>专用研究工作流</h3><a class="primary-btn" href="#/capabilities/workflows/${catalogKey.slice(-3)}">进入${catalogKey.endsWith("s05") ? "访谈与田野材料整理" : "专题追踪"}</a></section>`);
    if (["research-s03", "research-s05"].includes(catalogKey)) root.querySelector("[data-copy-capability-form]")?.closest("section").remove();
    if (window.ReaderAuth?.status?.demo_available && ["research-s03", "research-s05"].includes(catalogKey)) {
      root.querySelector("aside").insertAdjacentHTML("afterbegin", `<section><h3>已完成的 Demo 成果</h3><button class="primary-btn" data-demo-result>直接查看${catalogKey.endsWith("s05") ? "两份样稿整理结果" : "政策追踪简报"}</button></section>`);
      root.querySelector("[data-demo-result]").onclick = () => window.FieldDemo.result(catalogKey.slice(-3));
    }
    const copyForm = root.querySelector("[data-copy-capability-form]");
    if (copyForm && context?.type === "research_case") {
      copyForm.elements.scope_type.value = "research_case";
      copyForm.elements.research_case_id.value = context.key;
    }
    if (copyForm && context?.type === "country") {
      const fields = item.input_schema?.allowed_fields || [];
      copyForm.dataset.initialConfig = JSON.stringify(fields.includes("country_iso3") ? { country_iso3: context.key } : fields.includes("country_iso3s") ? { country_iso3s: [context.key] } : {});
    }
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function capabilityRunHistoryHtml(items = []) {
  if (!items.length) return `<p class="method-note">尚无运行记录。</p>`;
  return `<div class="capability-run-history">${items.map((item) => `<button type="button" class="object-link-card" data-view-capability-run="${item.id}"><b>运行 #${item.id}</b><span>${escapeHtml(runStatusLabel(item.status))} · ${escapeHtml(localDate(item.finished_at || item.created_at))}</span></button>`).join("")}</div>`;
}

async function renderCapabilityMine(configId = null) {
  const root = showCapabilityDocument("我的能力", "我已配置的研究能力");
  root.innerHTML = detailSkeleton(3);
  try {
    const payload = await apiFetch("/reader/capability-configs");
    const scopeFilter = state.routeQuery?.get("scope") || "all";
    const allItems = (payload.items || []).filter((item) => Boolean(item.catalog_key));
    const items = allItems.filter((item) => scopeFilter === "all" || (scopeFilter === "personal" ? item.scope_type === "personal" : item.scope_type === "research_case"));
    let selected = null;
    let revisions = { items: [] };
    let runs = { items: [] };
    let schedules = { items: [] };
    let audit = { items: [] };
    if (configId) {
      selected = await apiFetch(`/reader/capability-configs/${configId}`);
      if (selected.config?.workflow_version === 1) { const kind = (selected.catalog_key || selected.template?.catalog_key).slice(-3); replaceRoute(`/capabilities/workflows/${kind}/${configId}`); await SkillWorkflows.open(kind, Number(configId)); return; }
      if (!selected.catalog_key && !selected.template?.catalog_key) {
        replaceRoute(`/capabilities/history/config-${configId}`);
        renderCapabilityHistory(`config-${configId}`);
        renderSidebarContext();
        return;
      }
      [revisions, runs, schedules, audit] = await Promise.all([
        apiFetch(`/reader/capability-configs/${configId}/revisions`),
        apiFetch(`/reader/capability-configs/${configId}/runs`),
        apiFetch("/reader/capability-schedules"),
        apiFetch(`/reader/capability-audit-events?config_id=${configId}`),
      ]);
      schedules.items = (schedules.items || []).filter((item) => Number(item.config_id) === Number(configId));
    }
    const navigation = `<nav><a class="${scopeFilter === "all" ? "is-active" : ""}" href="#/capabilities/mine">全部 ${allItems.length}</a><a class="${scopeFilter === "personal" ? "is-active" : ""}" href="#/capabilities/mine?scope=personal">个人</a><a class="${scopeFilter === "project" ? "is-active" : ""}" href="#/capabilities/mine?scope=project">项目</a></nav>`;
    if (!selected) {
      const cards = items.map((item) => {
        const scopeLabel = item.scope_type === "research_case" ? `项目 #${item.research_case_id}` : "个人能力";
        const statusLabel = item.verified_usable ? "已验证可用" : item.can_run ? "待实跑验证" : item.status === "draft" ? "草稿" : item.status === "paused" ? "已暂停" : item.status;
        const inputLabel = (item.input_schema?.allowed_fields || []).map(schemaFieldLabel).slice(0, 3).join("、") || "当前范围";
        const outputLabel = item.output_schema?.type || "带引用候选产物";
        const useLabel = item.verified_usable ? "对话可选 · 写回需确认" : item.can_run ? "可运行测试 · 验证前不加入对话" : "当前不可加入对话";
        return `<article class="capability-catalog-card capability-own-card ${item.verified_usable ? "is-verified" : ""}"><header><span>${escapeHtml(scopeLabel)}</span><b>${escapeHtml(statusLabel)}</b></header><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.description || "已配置的研究能力。")}</p><div class="capability-card-tags"><span>v${escapeHtml(item.version || "-")}</span><span>${escapeHtml(capabilityCategoryLabel(item.category))}</span>${item.verified_run ? `<span>验证运行 #${item.verified_run.id}</span>` : item.latest_run ? `<span>最近运行 #${item.latest_run.id}</span>` : ""}</div><dl><div><dt>输入</dt><dd>${escapeHtml(inputLabel)}</dd></div><div><dt>输出</dt><dd>${escapeHtml(outputLabel)}</dd></div></dl><footer><span>${useLabel}</span><a href="#/capabilities/mine/${item.id}">查看能力 →</a></footer></article>`;
      }).join("");
      root.innerHTML = `<div class="capability-mine-page">${navigation}<header class="capability-mine-heading"><div><p class="eyebrow">MY CAPABILITIES</p><h1>我的能力</h1><p>这里展示全部已下载配置；只有当前配置版本成功产出过非空结果的能力，才会进入 AI 对话选择器。</p></div><a class="primary-btn link-button" href="#/capabilities/create/1">创建自定义能力</a></header><div class="capability-catalog-grid capability-own-grid">${cards || emptyState("当前筛选下还没有能力；可先去能力市场复制一项。")}</div></div>`;
      return;
    }
    const template = selected.template || {};
    const inputLabel = (template.input_schema?.allowed_fields || []).map(schemaFieldLabel).join("、") || "当前范围";
    const outputLabel = template.output_schema?.type || "带引用候选产物";
    const actionButtons = ["active", "published"].includes(selected.status) ? `<button type="button" class="primary-btn compact" data-run-capability="${selected.id}" data-case-id="${selected.research_case_id || ""}">运行候选任务</button><button type="button" class="ghost-btn compact" data-capability-status="paused" data-config-id="${selected.id}">暂停</button>` : selected.status === "paused" ? `<button type="button" class="ghost-btn compact" data-capability-status="active" data-config-id="${selected.id}">恢复</button>` : `<button type="button" class="ghost-btn compact" data-capability-status="pending_review" data-config-id="${selected.id}">提交审阅</button>`;
    const availabilityLabel = selected.verified_usable ? "已验证可用" : selected.can_run ? "待实跑验证" : escapeHtml(selected.status);
    root.innerHTML = `<div class="capability-mine-page">${navigation}<div class="capability-detail-layout capability-own-detail"><main><header class="capability-detail-hero"><div><span>${selected.scope_type === "research_case" ? `项目 #${selected.research_case_id}` : "个人能力"} · v${escapeHtml(template.version || "-")}</span><h1>${escapeHtml(selected.name)}</h1><p>${escapeHtml(template.description || "已配置的研究能力。")}</p></div><b class="is-${escapeHtml(template.validation_status || "pending")}">${availabilityLabel}</b></header><section><h2>这个能力怎么工作</h2><div class="skill-io-grid"><article><span>输入</span><strong>${escapeHtml(inputLabel)}</strong></article><article><span>输出</span><strong>${escapeHtml(outputLabel)}</strong></article><article><span>当前版本验证</span><strong>${selected.verified_run ? `#${selected.verified_run.id} · 已产出非空结果` : "尚未通过实跑"}</strong></article></div></section><section><h2>使用方式</h2><p>只有当前配置版本实跑成功后，才可在对话中加入“本轮能力组合”。AI 会同时读取项目证据与本配置；每项真实运行仍需你单独确认，候选结果不会自动写回项目。</p></section><section><h2>配置与版本</h2><form class="capability-config-editor" data-capability-config-form="${selected.id}"><label>配置名称<input name="name" required value="${escapeHtml(selected.name)}"></label><label>声明式配置 JSON<textarea name="config" rows="8">${escapeHtml(JSON.stringify(selected.config || {}, null, 2))}</textarea></label><button type="submit" class="ghost-btn">保存新版本</button></form></section></main><aside><section><h3>运行控制</h3><div class="capability-config-status"><b>${escapeHtml(selected.status)}</b><span>配置 v${Number(selected.latest_revision || 0)}</span><span>${Number(selected.run_count || 0)} 次运行</span></div><div class="inline-actions"><button type="button" class="ghost-btn compact" data-preview-capability-config="${selected.id}" data-case-id="${selected.research_case_id || ""}">运行前预览</button>${actionButtons}<button type="button" class="danger-link" data-capability-status="archived" data-config-id="${selected.id}">归档</button></div><div id="capabilityConfigPreview"></div></section><details class="capability-advanced"><summary>高级：调度、版本与审计</summary><section class="capability-control-section"><h3>受控调度</h3><form data-capability-schedule-form="${selected.id}"><select name="cadence"><option value="weekly">每周</option><option value="daily">每日</option><option value="monthly">每月</option></select><input name="next_run_at" type="datetime-local" required><button type="submit" class="ghost-btn compact">创建调度</button></form><p>${(schedules.items || []).map((item) => `${escapeHtml(item.cadence)} · ${escapeHtml(localDate(item.next_run_at))} · ${escapeHtml(item.status)}`).join("<br>") || "尚无调度"}</p><p>${(revisions.items || []).slice(0, 3).map((item) => `配置 v${item.revision_no} · ${escapeHtml(localDate(item.created_at))}`).join("<br>") || "尚无版本"}</p><p>${(runs.items || []).slice(0, 3).map((item) => `运行 #${item.id} · ${escapeHtml(runStatusLabel(item.status))}`).join("<br>") || "尚无运行"}</p><p>${(audit.items || []).slice(0, 4).map((item) => `${escapeHtml(item.action)} · ${escapeHtml(localDate(item.created_at))}`).join("<br>") || "尚无审计记录"}</p></section></details></aside></div></div>`;
    const controlSection = root.querySelector(".capability-own-detail > aside > section");
    controlSection?.insertAdjacentHTML("afterend", `<section><h3>完整运行历史</h3>${capabilityRunHistoryHtml(runs.items)}<p class="method-note">每次运行均保留冻结输入、配置版本、状态和候选产物；点击可查看完整记录。</p></section>`);
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function renderCapabilityCreate(step) {
  const current = Math.min(4, Math.max(1, Number(step) || 1));
  const root = showCapabilityDocument("创建自定义能力", `第 ${current} / 4 步 · 只接受声明式 JSON`);
  const labels = ["基本信息", "输入与输出", "步骤与权限", "预检与提交"];
  root.innerHTML = `<div class="capability-create-layout"><main><ol class="capability-create-steps">${labels.map((label, index) => `<li class="${index + 1 === current ? "is-active" : index + 1 < current ? "is-done" : ""}"><span>${index + 1}</span>${escapeHtml(label)}</li>`).join("")}</ol><section><p class="eyebrow">STEP ${current}</p><h1>${escapeHtml(labels[current - 1])}</h1>${current === 1 ? `<div class="skill-config-form"><label><span>能力名称</span><input placeholder="例如：法语政策版本差异复核"></label><label><span>稳定 catalog_key</span><input placeholder="french-policy-version-review"></label><label class="skill-field-wide"><span>用途</span><textarea rows="5" placeholder="说明何时使用、使用什么受控证据。"></textarea></label></div>` : `<textarea class="capability-json-editor" rows="16" spellcheck="false" placeholder='{"input_schema":{},"output_schema":{},"steps":[],"human_checkpoints":[]}'></textarea>`}<div class="inline-actions"><a class="ghost-btn link-button" href="#/capabilities/create/${Math.max(1, current - 1)}">上一步</a><a class="primary-btn link-button" href="#/capabilities/create/${Math.min(4, current + 1)}">${current === 4 ? "提交草稿审阅" : "下一步"}</a></div></section></main><aside><h2>持续合规检查</h2><ul class="compliance-checklist"><li class="is-ready">仅声明式 JSON</li><li>禁止代码、Shell 与数据库指令</li><li>禁止任意网络地址</li><li>禁止账号、Cookie、Token</li><li>禁止私有数据绑定与未授权正文</li><li>写回必须人工确认</li></ul><p>预检通过只能提交草稿，不代表公共发布或运行权限已开放。</p></aside></div>`;
}

function renderCapabilityHistory(key = null) {
  const root = showCapabilityDocument("历史执行器", "历史配置与内部工作流 · 只读兼容");
  root.innerHTML = `<section class="report-section"><h2>${key ? `历史链接：${escapeHtml(key)}` : "历史执行器与运行快照"}</h2><p>保留旧配置与运行快照；当前研究目录按需求文档的 S01–S11 展示，历史组合流程不计入研究 Skills。</p><div class="research-output-catalog">${Array.from({ length: 10 }, (_, index) => `<span>S${String(index + 1).padStart(2, "0")}</span>`).join("")}</div><p class="method-note">旧链接使用 history.replace 进入此只读页，历史运行和项目上下文不会丢失。</p></section>`;
}

async function viewCapabilityRun(runId) {
  state.activeCapabilityRunId = Number(runId);
  const topicRoot = document.getElementById("topicRunResult");
  if (state.view === "projects" && topicRoot) {
    topicRoot.hidden = false;
    topicRoot.innerHTML = `<div class="empty-state">正在读取产物…</div>`;
    try { renderCapabilityArtifact(await apiFetch(`/reader/capability-runs/${runId}`), topicRoot); } catch (error) { topicRoot.innerHTML = errorState(error.message); }
    return;
  }
  const root = document.getElementById("capabilityRunResult");
  showCapabilityWorkspace(`运行 #${runId}`);
  document.getElementById("capabilityTabs").hidden = true;
  root.hidden = false;
  root.innerHTML = `<div class="empty-state">正在读取产物…</div>`;
  try { renderCapabilityArtifact(await apiFetch(`/reader/capability-runs/${runId}`), root); } catch (error) { root.innerHTML = errorState(error.message); }
  renderSidebarContext();
}

function capabilityGapHtml(output) {
  const gaps = output.evidence_gaps || [];
  return gaps.length ? `<section class="report-section"><h3>证据缺口</h3><ul class="evidence-gap-list">${gaps.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>` : "";
}

function compactEvidenceTitles(items) {
  const shown = (items || []).slice(0, 5).map((item) => escapeHtml(item.title)).join("、");
  const remaining = Math.max(0, (items || []).length - 5);
  return `${shown}${remaining ? ` <small>另 ${remaining} 份，完整清单保留在运行记录中</small>` : ""}`;
}

function capabilityValueLabel(value) {
  if (Array.isArray(value)) return value.join("、") || "无";
  if (value && typeof value === "object") return Object.entries(value).map(([key, item]) => `${key === "from" ? "起" : key === "to" ? "止" : key} ${item}`).join(" · ");
  return value ?? "无";
}

function renderCapabilityArtifact(payload, target = null) {
  if (["field_research_workflow", "policy_tracking_workflow"].includes(payload.output?.type)) { SkillWorkflows.renderRun(payload.id, target || document.getElementById("capabilityRunResult")); return; }
  const root = target || document.getElementById("capabilityRunResult"); const output = payload.output || {}; let content = "";
  if (output.type === "country_brief") content = `<section class="report-section"><h3>国别画像证据覆盖</h3><div class="report-facts"><div><strong>${Number(output.structured_observation_count || 0).toLocaleString("zh-CN")}</strong><span>结构化观测</span></div><div><strong>${Number(output.event_count || 0)}</strong><span>已复核事件</span></div><div><strong>${Number(output.confirmed_material_count || 0)}</strong><span>确认材料</span></div></div></section><section class="report-section"><h3>六个主题</h3><div class="skill-theme-grid">${(output.themes || []).map((theme) => `<article><header><strong>${escapeHtml(theme.name)}</strong><span>${Number(theme.count || 0)} 项</span></header>${theme.items?.length ? `<ul>${theme.items.slice(0, 4).map((item) => `<li>${escapeHtml(item.title || item.indicator_code || item.kind)}${item.period ? ` · ${escapeHtml(item.period)}` : ""}</li>`).join("")}</ul>` : `<p>当前证据缺口</p>`}</article>`).join("")}</div></section>${output.conflicts?.length ? `<section class="report-section"><h3>来源冲突</h3><ul class="evidence-gap-list">${output.conflicts.map((item) => `<li>${escapeHtml(item.comparison_key)}：${item.values.map(escapeHtml).join(" ／ ")}</li>`).join("")}</ul></section>` : ""}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "bilingual_policy_verification") content = `<section class="report-section"><h3>原文—候选译文对照</h3>${output.segments?.length ? `<div class="bilingual-segments">${output.segments.map((item) => `<article><header><strong>${escapeHtml(item.source_id)}</strong><span>${escapeHtml(item.locator)}</span></header><div><p lang="fr">${escapeHtml(item.original_excerpt)}</p><p>${escapeHtml(item.translated_excerpt)}</p></div>${item.ambiguities?.length ? `<small>歧义：${item.ambiguities.map(escapeHtml).join("、")}</small>` : ""}</article>`).join("")}</div>` : emptyState("未生成通过原文定位校验的候选译文")}</section>${output.glossary?.length || output.ambiguity_items?.length ? `<section class="report-section"><h3>术语与待确认疑点</h3>${output.glossary?.length ? `<div class="tag-row">${output.glossary.map((item) => `<span class="status-chip">${escapeHtml(capabilityValueLabel(item))}</span>`).join("")}</div>` : ""}${output.ambiguity_items?.length ? `<ul class="evidence-gap-list">${output.ambiguity_items.map((item) => `<li>${escapeHtml(item.source_id)} · ${escapeHtml(item.locator)}：${escapeHtml(item.note)}</li>`).join("")}</ul>` : ""}<p class="method-note">术语和译文均为候选内容，研究者修订后会形成新版本，不覆盖原文。</p></section>` : ""}<section class="report-section"><h3>来源链</h3><div class="item-list">${(output.source_chain || []).map((item) => `<article><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.institution || "机构未登记")} · ${item.verification_status === "user_link_unreviewed" ? "用户链接·未复核 · " : ""}SHA-256 ${escapeHtml((item.sha256 || "").slice(0, 12))}…</p></article>`).join("")}</div></section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "policy_dynamics") content = `<section class="report-section"><h3>与上次同配置快照比较</h3><div class="report-facts"><div><strong>${Number(output.changes?.added?.length || 0)}</strong><span>新增</span></div><div><strong>${Number(output.changes?.updated?.length || 0)}</strong><span>更新</span></div><div><strong>${Number(output.changes?.removed?.length || 0)}</strong><span>退出</span></div></div><p class="method-note">${output.comparison_run_id ? `比较基准：运行 #${Number(output.comparison_run_id)}。` : "尚无同配置成功快照，当前结果作为基线。"} 无正文授权时只比较哈希、元数据、允许摘要与定位摘录。</p></section><section class="report-section"><h3>政策动态与阅读队列</h3>${output.materials?.length ? `<div class="item-list">${output.materials.slice(0, 12).map((item) => `<article><header><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.source)}</span></header><p>版本 ${Number(item.version_no || 1)} · 发布 ${escapeHtml(item.published_at ? localDate(item.published_at) : "来源未声明")}</p><button type="button" class="text-link" data-material-version="${item.document_version_id}">打开固定版本</button></article>`).join("")}</div>${output.materials.length > 12 ? `<p class="method-note">另 ${output.materials.length - 12} 份材料保留在冻结运行记录中。</p>` : ""}` : emptyState("当前没有已确认的政策材料")}</section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "event_timeline") content = `<section class="report-section"><h3>事件编年表</h3>${output.items?.length ? `<div class="series-timeline">${output.items.map((item) => `<article class="series-event"><time>${escapeHtml(item.start_at ? localDate(item.start_at) : "日期未知")}</time><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(displayEnum(DATE_PRECISION_LABELS, item.date_precision))} · ${Number(item.confirmed_source_mentions || 0)} 个已确认来源 · ${escapeHtml(item.place || "地点待核验")}</span></div><button type="button" class="text-link" data-event-id="${item.event_id}">打开</button></article>`).join("")}</div>` : emptyState("当前没有已复核事件")}</section>${output.conflict_groups?.length ? `<section class="report-section"><h3>保留的来源冲突</h3><div class="ledger">${output.conflict_groups.map((group) => `<article><strong>${escapeHtml(group.comparison_key)}</strong>${group.values.map((value) => `<p>${escapeHtml(value)}</p>`).join("")}</article>`).join("")}</div></section>` : ""}${output.merge_candidates?.length ? `<section class="report-section"><h3>跨事件合并候选</h3><ul>${output.merge_candidates.map((item) => `<li>${escapeHtml(item.reason || item.title || item)}</li>`).join("")}</ul><p class="method-note">只提供候选，不自动修改事件库。</p></section>` : ""}${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (["field_material_notes", "field_material_organization"].includes(output.type)) content = `<section class="report-section"><h3>逐字原文—整理稿对照</h3>${output.excerpts?.length ? `<div class="transcript-comparison">${output.excerpts.map((item) => `<article><header><strong>${escapeHtml(item.speaker || item.role || "角色未标注")}</strong><span>${escapeHtml(capabilityValueLabel(item.locator || item.timestamp || "定位未提供"))}</span></header><div><section><small>逐字原文</small><p>${escapeHtml(item.verbatim_text || item.text || "")}</p></section><section><small>候选整理稿</small><p>${escapeHtml(item.organized_text || item.text || "")}</p></section></div><footer>${(item.edits || []).map((edit) => escapeHtml(edit.type || edit)).join("、") || "未做语言清理"} · ${(item.themes || []).map(escapeHtml).join("、") || "主题待复核"}</footer></article>`).join("")}</div>` : emptyState("本次只冻结文件引用，尚未生成可定位整理稿。")}</section>${output.file_refs?.length ? `<section class="report-section"><h3>已冻结的用户资料引用</h3><div class="item-list">${output.file_refs.map((item) => `<article><header><strong>${escapeHtml(item.title || item.filename || `上传资料 #${item.upload_id}`)}</strong><span>用户提供 · 未复核</span></header><p>${escapeHtml(item.filename || "文件名未登记")} · ${escapeHtml(fieldMaterialPrivacyLabel(item.privacy))}</p></article>`).join("")}</div></section>` : ""}${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "interdisciplinary_evidence") content = `<section class="report-section"><h3>跨学科证据矩阵</h3><div class="discipline-matrix">${(output.matrix || []).map((row) => `<article><header><strong>${escapeHtml(row.discipline)}</strong><span>${Object.values(row.evidence || {}).reduce((sum, items) => sum + items.length, 0)} 份</span></header>${Object.entries(row.evidence || {}).map(([usage, items]) => items.length ? `<div class="discipline-group"><b>${escapeHtml(displayEnum({ support: "支持", refute: "反驳", background: "背景", to_verify: "待核验" }, usage))}</b>${items.slice(0, 5).map((item) => { const match = (item.classification_candidates || []).find((candidate) => candidate.discipline === row.discipline); return `<p><span>${escapeHtml(item.title)}</span><small>分类依据：${escapeHtml(match?.basis || "待研究者确认")}</small></p>`; }).join("")}${items.length > 5 ? `<small>另 ${items.length - 5} 份保留在运行结果中</small>` : ""}</div>` : "").join("")}</article>`).join("")}</div><p class="method-note">同一材料可有多个候选标签；研究者可通过“修改候选结果”确认或删改。</p></section>${output.discipline_gaps?.length ? `<section class="report-section"><h3>学科覆盖缺口</h3><p>${output.discipline_gaps.map(escapeHtml).join("、")}</p></section>` : ""}${output.unclassified?.length ? `<section class="report-section"><h3>待研究者分类</h3><ul>${output.unclassified.map((item) => `<li>${escapeHtml(item.title)}</li>`).join("")}</ul></section>` : ""}${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "material_relevance_ranking") content = `<section class="report-section"><h3>材料候选优先级</h3><p class="method-note">查询：${escapeHtml(output.query)}。排序不是学术质量裁决，低排名材料仍保留。</p><div class="material-ranking-list">${(output.ranked_materials || []).map((item, index) => `<article class="${item.included ? "" : "is-excluded"}"><b>${index + 1}</b><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.source)} · ${escapeHtml(item.published_at ? localDate(item.published_at) : "时间未登记")}</span><small>${item.exclusion_reason ? escapeHtml(item.exclusion_reason) : `命中：${(item.matched_keywords || []).map(escapeHtml).join("、") || "专题基础匹配"}`}</small></div><strong>${Number(item.score || 0).toFixed(1)}</strong></article>`).join("")}</div></section><section class="report-section"><h3>公开权重</h3><div class="weight-grid">${Object.entries(output.weights || {}).map(([key, value]) => `<span><b>${escapeHtml(schemaFieldLabel(key))}</b>${Math.round(Number(value) * 100)}%</span>`).join("")}</div></section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "contradictory_evidence_context") content = `<section class="report-section"><h3>矛盾证据语境对照</h3><div class="context-comparison-grid">${(output.context_cards || []).map((item) => `<article><header><strong>${escapeHtml(item.source)}</strong><span>${escapeHtml(displayEnum({ support: "支持", refute: "反驳", background: "背景", to_verify: "待核验" }, item.position))}</span></header><p>${escapeHtml(item.original_excerpt || "原文摘录未登记")}</p><small>${escapeHtml(item.time || "时间未登记")} · ${escapeHtml(item.place || "地点未登记")} · ${escapeHtml(item.observer || "观察主体未登记")}</small></article>`).join("")}</div></section><section class="report-section"><h3>差异解释候选</h3>${(output.difference_candidates || []).map((item) => `<article class="evidence-candidate"><strong>${escapeHtml(item.difference_type)}</strong><p>${escapeHtml(item.interpretation)}</p><small>匹配依据：${escapeHtml(item.basis)}</small></article>`).join("") || emptyState("尚无足够材料生成差异解释候选。")}</section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "country_comparison") content = `<section class="report-section"><h3>可比指标</h3><p class="method-note">共 ${Number(output.comparable_rows?.length || 0)} 个完全对齐的指标时期组合；表内先展示前 20 项。</p>${output.comparable_rows?.length ? `<div class="comparison-table-wrap"><table><thead><tr><th>指标</th><th>时期</th><th>单位</th>${(output.countries || []).map((country) => `<th>${escapeHtml(country)}</th>`).join("")}<th>绝对差异</th><th>相对差异</th></tr></thead><tbody>${output.comparable_rows.slice(0, 20).map((item) => `<tr><td>${escapeHtml(item.indicator_code)}</td><td>${escapeHtml(item.period)}</td><td>${escapeHtml(item.unit || "未登记")}</td>${(output.countries || []).map((country) => `<td>${item.values?.[country] === null || item.values?.[country] === undefined ? "缺失" : escapeHtml(formatNumber(item.values[country]))}</td>`).join("")}<td>${escapeHtml(formatNumber(item.statistics?.absolute_difference))}</td><td>${escapeHtml(formatNumber(item.statistics?.relative_difference))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("没有时期、概念、单位和来源全部对齐的指标")}</section>${output.material_comparison?.length ? `<section class="report-section"><h3>材料并列比较</h3><p class="method-note">研究者指定维度：${(output.comparison_dimensions || []).map(escapeHtml).join("、") || "未指定，仅并列来源"}。系统不做机械排名或差异原因推断。</p><div class="item-list">${output.material_comparison.map((item) => `<article><header><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.country_or_region || "国家范围待确认")}</span></header><p>${escapeHtml(item.excerpt || "摘要未登记")}</p><small>${escapeHtml(item.source)} · ${escapeHtml(item.published_at ? localDate(item.published_at) : "时间未登记")} · ${escapeHtml(displayEnum({ support: "支持", refute: "反驳", background: "背景", to_verify: "待核验" }, item.usage_type))}</small></article>`).join("")}</div></section>` : ""}<section class="report-section"><h3>不可比项</h3>${output.not_comparable?.length ? `<p class="method-note">共 ${output.not_comparable.length} 项；展示前 20 项，完整结果保留在本次运行 JSON。</p><ul class="evidence-gap-list">${output.not_comparable.slice(0, 20).map((item) => `<li>${escapeHtml(item.indicator_code || "未标注指标")} · ${escapeHtml(item.period)}：${escapeHtml(item.reason)}</li>`).join("")}</ul>` : `<p>未登记额外不可比项。</p>`}</section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "policy_impact") content = `<section class="report-section"><h3>政策影响链</h3>${output.policy_events?.length ? `<div class="item-list">${output.policy_events.map((item) => `<article><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.summary || "无独立摘要")}</p></article>`).join("")}</div>` : emptyState("没有已复核政策事件")}<div class="impact-edges">${(output.edges || []).map((edge) => `<article><span class="status-chip">${escapeHtml(displayEnum({ observed: "已观察事实", claimed: "来源主张", candidate_unreviewed: "待核验推断" }, edge.status))}</span><strong>${escapeHtml(edge.relation)}</strong><small>${escapeHtml(edge.evidence?.source || edge.evidence?.note || "证据缺口")}</small></article>`).join("")}</div></section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "topic_feasibility") content = `<section class="report-section"><h3>选题可行性</h3><blockquote>${escapeHtml(output.research_question)}</blockquote><div class="feasibility-grid">${(output.dimensions || []).map((item) => `<article><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(capabilityValueLabel(item.value))}</strong><small>${escapeHtml(displayEnum({ ready: "证据基础可用", limited: "需补充", expert_review_required: "专家复核", candidate: "候选" }, item.status))}</small></article>`).join("")}</div></section><section class="report-section"><h3>方法与资料需求</h3><div class="skill-run-assurance"><article><span>方法建议</span><strong>${(output.method_suggestions || []).map(escapeHtml).join("、") || "待专家确认"}</strong></article><article><span>资料需求</span><strong>${(output.data_needs || []).map(escapeHtml).join("、") || "当前自动检查未发现硬性缺口"}</strong></article></div></section><section class="report-section"><h3>候选题目与待确认问题</h3><ul>${(output.candidate_titles || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul><ol>${(output.confirmation_questions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol><p class="method-note">未自动创建项目；需进入现有人工确认建项流程。</p></section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "material_condition_assessment") content = `<section class="report-section"><h3>研究资料条件评估</h3><blockquote>${escapeHtml(output.research_question)}</blockquote><div class="feasibility-grid">${(output.dimensions || []).map((item) => `<article><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(capabilityValueLabel(item.value))}</strong><small>${escapeHtml(displayEnum({ ready: "当前有覆盖", limited: "需补充", expert_review_required: "待专家判断" }, item.status))}</small></article>`).join("")}</div></section><section class="report-section"><h3>访问与语言条件</h3><ul>${(output.access_limits || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul><p>${escapeHtml(capabilityValueLabel(output.language_conditions))}</p></section><section class="report-section"><h3>待补资料与待专家判断</h3><p>${(output.data_needs || []).map(escapeHtml).join("、") || "当前自动检查未发现硬性缺口"}</p><ol>${(output.expert_questions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol><p class="method-note">系统不输出可行/不可行结论，不自动创建专题。</p></section>${capabilityGapHtml(output)}<p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "event_evidence_matrix") content = `<section class="report-section"><h3>事件证据矩阵</h3>${output.events?.length ? `<div class="ledger">${output.events.map((item) => `<article><header><strong>${escapeHtml(item.title)}</strong><span>${Number(item.distinct_sources)} 个来源</span></header><p>${Number(item.source_mentions)} 条确认提及 · ${Number(item.claims)} 项主张 · ${Number(item.comparison_groups)} 个比较组</p><button type="button" class="ghost-btn" data-event-evidence="${item.event_id}">打开证据差异</button></article>`).join("")}</div>` : emptyState("暂无达到门槛的事件")}</section><p class="method-note">${escapeHtml(output.method_note)}</p>`;
  else if (output.type === "topic_digest" && output.schema_version === "2.0") {
    const aiDigest = output.ai_digest ? assistantResponseHtml({ artifact: output.ai_digest, run_id: output.agent_run_id, runtime: output.ai_digest.runtime, proposed_actions: [] }) : emptyState("本次没有通过校验的 AI 周报正文；请查看上方运行状态。");
    const events = (output.event_timeline || []).map((item) => `<article><header><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.start_at ? localDate(item.start_at) : "日期未知")}</span></header><p>${escapeHtml(item.summary || "无独立摘要")}</p><button type="button" class="ghost-btn" data-event-id="${item.event_id}">查看事件证据</button></article>`).join("");
    const differences = (output.claim_differences || []).map((group) => `<article><header><strong>${escapeHtml(group.comparison_key)}</strong><span class="precision">来源口径不同</span></header>${(group.claims || []).map((claim) => `<p><b>${escapeHtml(claim.source)}</b>：${escapeHtml(claim.value_text)}${claim.position_summary ? ` · ${escapeHtml(claim.position_summary)}` : ""}</p>`).join("")}</article>`).join("");
    const materials = (output.new_materials || []).map((item) => { const url = safeUrl(item.source_url); return `<article><header><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.source)}</span></header><p>来源发布时间：${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))}</p><button type="button" class="text-link material-link" data-material-version="${item.document_version_id}">查看固定版本</button>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>` : ""}</article>`; }).join("");
    const gaps = (output.evidence_gaps || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    content = `<section class="report-section"><h3>本期 AI 专题周报</h3>${aiDigest}</section><section class="report-section"><h3>关联事件脉络</h3>${events ? `<div class="item-list">${events}</div>` : emptyState("冻结快照没有关联事件")}</section><section class="report-section"><h3>多源立场或数字差异</h3>${differences ? `<div class="ledger">${differences}</div>` : emptyState("冻结快照没有可并列的差异组")}</section><section class="report-section"><h3>近 7 天新增材料</h3>${materials ? `<div class="item-list">${materials}</div>` : emptyState("本期没有来源明确声明发布时间的新增材料")}</section><section class="report-section"><h3>证据缺口与方法限制</h3>${gaps ? `<ul class="evidence-gap-list">${gaps}</ul>` : emptyState("本期未登记额外证据缺口")}<p class="method-note">${(output.method_limitations || []).map(escapeHtml).join(" · ")}</p></section>`;
  }
  else if (output.type === "topic_digest") content = `<section class="report-section"><h3>${output.frequency === "weekly" ? "本期专题周报" : "本期专题材料"}</h3><p class="method-note">这是 1.0 历史产物；仅按当时模板展示已存储材料，不改写历史运行。</p>${output.materials?.length ? `<div class="item-list">${output.materials.map((item) => { const url = safeUrl(item.canonical_url || item.source_url); return `<article><header><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.source)}</span></header>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>` : ""}</article>`; }).join("")}</div>` : emptyState("历史产物没有已确认材料")}</section>`;
  else content = emptyState("该运行没有可读产物");
  const terminalNote = payload.status === "insufficient_data" ? `<div class="readiness is-partial"><b>证据不足，未生成正式产物</b><p>${escapeHtml(payload.error_message || "输入快照没有达到该能力的最低证据门槛。")}</p></div>` : payload.status === "failed" ? errorState(payload.error_message || "能力运行失败") : "";
  const input = payload.input_snapshot ? `<details class="developer-details"><summary>查看输入快照与开发详情</summary><pre>${escapeHtml(JSON.stringify(payload.input_snapshot, null, 2))}</pre></details>` : "";
  const backToTopic = payload.research_case_id ? `<a class="text-link" href="#/projects/${payload.research_case_id}/outputs">返回所属项目</a>` : "";
  const capability = (state.capabilities || []).find((item) => item.configs?.some((config) => Number(config.id) === Number(payload.config_id)));
  const writebackOption = (payload.writeback_options || [])[0] || null;
  const writebackType = writebackOption?.target_type || "";
  const writebackKey = writebackOption?.target_key || "";
  const suggested = (state.capabilities || []).find((item) => item.skill_manifest?.skill_code === output.suggested_next_skill);
  const suggestedAction = suggested ? `<a class="ghost-btn link-button" href="#/capabilities/${escapeHtml(suggested.slug)}">建议下一步：${escapeHtml(output.suggested_next_skill)}</a>` : `<span>建议下一 Skill 仅供参考，不会自动触发。</span>`;
  const actions = `<div class="run-review-actions"><a class="ghost-btn link-button" href="#/capabilities/${escapeHtml(capability?.slug || "")}/configure">修改配置 / 重新运行</a>${payload.status === "succeeded" ? `<button type="button" class="ghost-btn" data-revise-run="${payload.id}">修改候选结果</button>` : ""}${payload.status === "succeeded" && writebackType ? `<button type="button" class="primary-btn" data-confirm-run-writeback="${payload.id}" data-target-type="${writebackType}" data-target-key="${escapeHtml(writebackKey)}">${escapeHtml(writebackOption.label || "确认写回")}</button>` : ""}<button type="button" class="ghost-btn" data-ignore-run>忽略结果</button>${suggestedAction}</div>`;
  root.innerHTML = `<header class="report-header"><div><p class="eyebrow">CAPABILITY RUN #${payload.id}</p><h2>可信能力产物</h2></div><span class="status-chip">${escapeHtml(runStatusLabel(payload.status))} · ${escapeHtml(localDate(payload.finished_at))}</span></header>${backToTopic}${terminalNote}${content}${actions}${input}`;
}

function generateCitationData(item) {
  const author = (item.metadata?.authors || []).join("、") || item.publisher || item.source_name || "作者未注明";
  const title = item.title || "未命名文献";
  const pubDate = item.published_at && item.published_at_precision!=="unknown" ? String(item.published_at).slice(0, item.published_at_precision==="year" ? 4 : item.published_at_precision==="month" ? 7 : 10) : "";
  const pubYear = item.published_at ? new Date(item.published_at).getUTCFullYear() : "n.d.";
  const url = safeUrl(item.canonical_url || item.source_url || "");
  const accessDate = new Date().toLocaleDateString("sv-SE");

  const gbt = `${author}. ${title}[EB/OL]. ${pubDate ? `(${pubDate})` : ""}[${accessDate}]. ${url || "内部存证"}.`;
  const apa = `${author}. (${pubYear}). ${title}.${url ? ` Retrieved ${accessDate}, from ${url}` : ""}`;
  const bibtex = `@misc{guobie_doc_${item.document_version_id || "item"},
  title = {${title}},
  author = {${author}},
  year = {${pubYear}},
  url = {${url}}
}`;
  const ris = `TY  - ELEC\nTI  - ${title}\nAU  - ${author}\nPY  - ${pubYear}\nUR  - ${url}\nER  - `;

  return { gbt, apa, bibtex, ris };
}

let activeCitationFormat = "gbt";
let currentMaterialCitations = null;

async function openMaterial(versionId) {
  if (state.assistantContext?.space === "country" && state.country) return openCountryMaterial(versionId);
  const dialog = document.getElementById("materialDrawer");
  const body = document.getElementById("materialDrawerBody");
  const request = state.materialReadingRequest = (state.materialReadingRequest || 0) + 1;
  body.innerHTML = `<div class="empty-state">正在读取材料版本…</div>`;
  if (!dialog.open) dialog.showModal();
  try {
    const item = await apiFetch(`/reader/materials/${versionId}`);
    if (request !== state.materialReadingRequest || !dialog.open) return;
    document.getElementById("materialDrawerTitle").textContent = item.title || "材料详情";
    const url = safeUrl(item.source_url);
    const associations = [
      ...(item.countries || []).map((row) => `<a href="#/countries/${escapeHtml(row.iso3)}">${escapeHtml(row.name)}</a>`),
      ...(item.events || []).map((row) => `<a href="#/events/${row.id}">${escapeHtml(row.title)}</a>`),
      ...(item.topics || []).map((row) => `<a href="#/projects/${row.id}">${escapeHtml(row.title)}</a>`),
    ].join("");
    const locators = item.evidence_locators?.length ? item.evidence_locators : item.evidence_locator;
    const canonical = safeUrl(item.canonical_url);
    const discovery = safeUrl(item.discovery_url);
    const sourceLink = canonical
      ? `<a class="ghost-btn link-button" href="${escapeHtml(canonical)}" target="_blank" rel="noopener noreferrer">打开正式来源 ↗</a>`
      : discovery
        ? `<a class="ghost-btn link-button" href="${escapeHtml(discovery)}" target="_blank" rel="noopener noreferrer">打开发现页 ↗</a>`
        : url
          ? `<a class="ghost-btn link-button" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>`
          : `<span class="empty-state">该材料尚无已核验正式链接</span>`;

    currentMaterialCitations = generateCitationData(item);
    const citeText = currentMaterialCitations[activeCitationFormat] || currentMaterialCitations.gbt;

    body.innerHTML = `
      <div class="material-meta-grid">
        <article>
          <span>发布来源</span>
          <strong>${escapeHtml(item.source_name)}</strong>
        </article>
        <article>
          <span>来源发布时间</span>
          <strong>${escapeHtml(publishedLabel(item.published_at, item.published_at_precision))}</strong>
          <small>${escapeHtml(displayEnum(DATE_PRECISION_LABELS, item.published_at_precision))}</small>
        </article>
        <article>
          <span>系统采录时间</span>
          <strong>${escapeHtml(localDate(item.observed_at))}</strong>
          <small>与来源发布时间严格分开</small>
        </article>
        <article>
          <span>不可变版本</span>
          <strong>v${Number(item.version_no)} · #${Number(item.document_version_id)}</strong>
          <small>逻辑材料全局唯一</small>
        </article>
      </div>

      <section class="report-section compact-section">
        <h3>已有摘要与结构化摘录</h3>
        <p>${item.abstract ? escapeHtml(item.abstract) : "本站仅收录元数据，不保存正文。"}</p>
      </section>

      <section class="report-section compact-section">
        <h3>复核与证据定位 (Locator)</h3>
        <p>${escapeHtml(item.review_status === "confirmed" ? "材料关联已确认" : item.review_status === "reviewed" ? "已人工复核" : "未复核")} · ${escapeHtml(formatLocator(locators))}</p>
      </section>

      <section class="citation-copy-section">
        <div class="citation-copy-header">
          <h4>学术规范引文生成 (Citation Exporter)</h4>
          <div class="citation-format-buttons" id="materialCitationButtons">
            <button type="button" class="citation-format-btn ${activeCitationFormat === "gbt" ? "is-active" : ""}" data-cite-fmt="gbt">GB/T 7714</button>
            <button type="button" class="citation-format-btn ${activeCitationFormat === "apa" ? "is-active" : ""}" data-cite-fmt="apa">APA 7th</button>
            <button type="button" class="citation-format-btn ${activeCitationFormat === "bibtex" ? "is-active" : ""}" data-cite-fmt="bibtex">BibTeX</button>
            <button type="button" class="citation-format-btn" data-cite-fmt="ris">.ris (Zotero)</button>
          </div>
        </div>
        <div class="citation-text-box" id="materialCitationBox">${escapeHtml(citeText)}</div>
        <div class="citation-action-row">
          <button type="button" class="copy-cite-btn" id="materialCopyCiteBtn">一键复制引用</button>
        </div>
      </section>

      <section class="report-section compact-section">
        <h3>多空间关联对象</h3>
        ${associations ? `<div class="object-links">${associations}</div>` : emptyState("当前版本尚未关联国家、事件或项目")}
      </section>

      <details class="developer-details">
        <summary>技术证据与 SHA256</summary>
        <p>SHA256: ${escapeHtml(item.content_sha256 || "未记录")}</p>
      </details>

      <div class="drawer-actions">
        ${sourceLink}
      </div>
    `;

    const citeBox = document.getElementById("materialCitationBox");
    const fmtButtons = document.getElementById("materialCitationButtons");
    if (fmtButtons) {
      fmtButtons.addEventListener("click", (e) => {
        const btn = e.target.closest(".citation-format-btn");
        if (!btn || !currentMaterialCitations) return;
        const fmt = btn.dataset.citeFmt;
        if (fmt === "ris") {
          const blob = new Blob([currentMaterialCitations.ris], { type: "application/x-research-info-systems" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = `citation-${item.document_version_id || "doc"}.ris`;
          a.click();
          URL.revokeObjectURL(a.href);
          showToast("已下载 .ris 引文文件（可直接拖入 Zotero / EndNote）！");
          return;
        }
        activeCitationFormat = fmt;
        fmtButtons.querySelectorAll(".citation-format-btn").forEach((b) => b.classList.toggle("is-active", b === btn));
        if (citeBox) citeBox.textContent = currentMaterialCitations[fmt] || "";
      });
    }

    const copyCiteBtn = document.getElementById("materialCopyCiteBtn");
    if (copyCiteBtn) {
      copyCiteBtn.addEventListener("click", () => {
        if (!currentMaterialCitations) return;
        const text = currentMaterialCitations[activeCitationFormat] || currentMaterialCitations.gbt;
        navigator.clipboard.writeText(text).then(() => showToast(`已复制 ${activeCitationFormat.toUpperCase()} 格式引文！`));
      });
    }
  } catch (error) { if (request === state.materialReadingRequest && dialog.open) body.innerHTML = errorState(error.message); }
}

function syncHeaderMetrics() {
  const root = document.documentElement;
  const toolbar = document.getElementById("countryToolbar");
  root.style.setProperty("--header-height", "0px");
  if (toolbar?.offsetHeight) root.style.setProperty("--toolbar-height", `${toolbar.offsetHeight}px`);
}

async function runCapability(configId, caseId, button, inputOverrides = {}) {
  const template = (state.capabilities || []).find((item) => item.configs?.some((config) => Number(config.id) === Number(configId)));
  const config = template?.configs?.find((item) => Number(item.id) === Number(configId));
  const inputPreview = JSON.stringify({
    skill: template?.name || `配置 #${configId}`,
    scope: caseId ? { research_case_id: Number(caseId) } : null,
    config: config?.config || {},
    input_overrides: inputOverrides,
    evidence_policy: template?.skill_manifest?.evidence_policy || "使用当前作用域内已复核证据",
  }, null, 2);
  if (!window.confirm(`运行前输入快照预览（确认后冻结）：\n\n${inputPreview.slice(0, 1600)}\n\n是否开始运行？`)) return;
  if (button) button.disabled = true;
  try {
    const body = { config_id: Number(configId), input_overrides: inputOverrides }; if (caseId) body.research_case_id = Number(caseId);
    const payload = await apiFetch("/reader/capability-runs", { method: "POST", body: JSON.stringify(body) });
    state.activeCapabilityRunId = payload.id;
    if (payload.research_case_id && (state.view === "projects" || state.view === "events")) {
      navigate(`/projects/${payload.research_case_id}/outputs`);
      return;
    }
    navigate(`/capabilities/runs/${payload.id}`);
  } catch (error) {
    showToast(`能力运行失败：${error.message}`);
    if (state.view === "capabilities") {
      const root = document.getElementById("capabilityRunResult");
      showCapabilityWorkspace("运行失败");
      root.hidden = false;
      root.innerHTML = errorState(error.message);
    }
  } finally { if (button) button.disabled = false; }
}

function resolveTopicCountryIso3(kind, explicitIso3 = null) {
  if (explicitIso3) return explicitIso3;
  if (kind === "event" && state.eventDetail?.country?.iso3) return state.eventDetail.country.iso3;
  return state.country?.iso3 || state.topic?.scope?.country_iso3 || state.assistantContext?.country_iso3 || state.eventFilterCountry || state.selectedMapIso3 || "COD";
}

function syncTopicSubmitState() {
  const submit = document.getElementById("topicLinkSubmit");
  const hasExistingTopic = Number(document.getElementById("existingTopicSelect")?.value || 0) > 0;
  const hasDraft = Boolean(document.getElementById("newTopicTitle")?.value.trim());
  submit.disabled = !(hasExistingTopic || hasDraft);
  submit.textContent = hasExistingTopic && !hasDraft ? "加入项目" : "确认创建项目";
}

function resetTopicComposer() {
  state.pendingTopicSuggestion = null;
  state.topicSuggestions = [];
  document.getElementById("newTopicTitle").value = "";
  document.getElementById("newTopicQuestion").value = "";
  document.getElementById("topicDraftFields").hidden = true;
  const panel = document.getElementById("topicSuggestionPanel");
  panel.hidden = true;
  panel.innerHTML = "";
  syncTopicSubmitState();
}

function showManualTopicDraft() {
  const intent = document.getElementById("topicResearchIntent").value.trim();
  state.pendingTopicSuggestion = null;
  document.getElementById("topicDraftFields").hidden = false;
  if (intent) {
    document.getElementById("newTopicTitle").value ||= intent.replace(/^(我想研究|我想了解|帮我研究|请研究|围绕|关于)/, "").replace(/[，。！？,.!?]+$/g, "").slice(0, 80);
    document.getElementById("newTopicQuestion").value ||= intent;
  }
  const panel = document.getElementById("topicSuggestionPanel");
  panel.hidden = false;
  panel.innerHTML = `<div class="empty-state">填写项目标题即可进入；研究问题、概览和资料可以进入后与 AI 一起完善。</div>`;
  syncTopicSubmitState();
  document.getElementById("newTopicTitle").focus();
}

async function openTopicLinkDialog(kind = "create", id = null, label = "", countryIso3 = null) {
  const dialog = document.getElementById("topicLinkDialog");
  const existingField = document.getElementById("existingTopicField");
  const divider = document.getElementById("topicDialogDivider");
  state.pendingTopicLink = kind === "create" ? null : { kind, id: Number(id), label };
  state.pendingTopicSuggestion = null;
  state.topicSuggestions = [];
  state.pendingTopicCountryIso3 = resolveTopicCountryIso3(kind, countryIso3);
  const countrySelect = document.getElementById("topicCountrySelect");
  countrySelect.innerHTML = state.catalog.map((item) => `<option value="${escapeHtml(item.iso3)}">${escapeHtml(item.name_zh)} · ${escapeHtml(item.iso3)}</option>`).join("");
  countrySelect.value = state.pendingTopicCountryIso3;
  countrySelect.disabled = kind !== "create";
  document.getElementById("topicLinkTitle").textContent = kind === "create" ? "新建研究项目" : "加入项目";
  document.getElementById("topicLinkTarget").textContent = kind === "create" ? "先填写项目标题和国家范围，进入后再与 AI 逐步完善研究问题和概览。" : `将“${label || "当前对象"}”关联到现有项目，或转到右侧 AI 整理为新项目。`;
  existingField.hidden = kind === "create";
  divider.hidden = kind === "create";
  document.getElementById("topicResearchIntent").value = "";
  document.getElementById("existingTopicSelect").innerHTML = `<option value="">正在读取同国项目…</option>`;
  resetTopicComposer();
  ProjectOnboarding.reset();
  if (kind === "create") showManualTopicDraft();
  if (!dialog.open) dialog.showModal();
  try {
    state.topics = await apiFetch("/reader/research-cases");
    const compatibleTopics = state.topics.filter((item) => item.scope?.country_iso3 === state.pendingTopicCountryIso3);
    document.getElementById("existingTopicSelect").innerHTML = `<option value="">请选择同国项目</option>${compatibleTopics.map((item) => `<option value="${item.id}">${escapeHtml(item.title)}</option>`).join("")}`;
  } catch (error) {
    document.getElementById("existingTopicSelect").innerHTML = `<option value="">项目列表读取失败</option>`;
    showToast(`项目列表读取失败：${error.message}`);
  }
  syncTopicSubmitState();
  window.setTimeout(() => (kind === "create" ? document.getElementById("newTopicTitle") : document.getElementById("existingTopicSelect"))?.focus(), 0);
}

function startTopicCreationInAssistant() {
  const pending = state.pendingTopicLink;
  const suggestion = state.pendingTopicSuggestion;
  const selectedCountry = document.getElementById("topicCountrySelect")?.value || document.getElementById("topicFrontierCountry")?.value || resolveTopicCountryIso3(pending?.kind || "create");
  const dialog = document.getElementById("topicLinkDialog");
  if (dialog.open) closeTopicLinkDialog();
  openCopilot(true);
  const input = document.getElementById("assistantQuestion");
  if (suggestion) {
    input.value = `请继续修改刚才的项目草稿《${suggestion.title}》。保留当前已复核证据范围，我希望：`;
  } else {
    const session = currentAssistantSession();
    const lastMessage = session.messages.at(-1);
    if (!lastMessage?.topicOnboarding) {
      session.messages.push({
        id: assistantMessageId(), role: "assistant", topicOnboarding: true,
        html: `<p>可以。先告诉我三个要点：最关心的问题、时间或地区范围，以及希望形成的产物。也可以只写一句研究意图，我会结合当前已复核证据整理成待确认草稿。</p>`,
      });
      persistAssistantSessions();
      renderAssistantConversation();
    }
    input.value = pending?.kind === "event"
      ? `我想围绕当前事件新建一个关于 ${selectedCountry} 的项目，重点研究：`
      : pending?.kind === "material"
        ? `我想围绕当前材料新建一个关于 ${selectedCountry} 的项目，重点研究：`
        : `我想新建一个关于 ${selectedCountry} 的项目，重点研究：`;
  }
  input.focus({ preventScroll: true });
  input.setSelectionRange(input.value.length, input.value.length);
}

async function openAssistantTopicDraft(runId, button) {
  button.disabled = true;
  button.textContent = "AI 正在整理项目草稿…";
  try {
    const draft = await apiFetch(`/reader/assistant/runs/${runId}/topic-draft`, { method: "POST", body: "{}" });
    const kind = draft.seed_event_id ? "event" : "create";
    await openTopicLinkDialog(kind, draft.seed_event_id, draft.seed_event_title || "当前事件", draft.country_iso3);
    document.getElementById("topicResearchIntent").value = draft.research_intent || draft.research_question || "";
    state.topicSuggestions = [draft];
    applyTopicSuggestion(draft.suggestion_id);
    document.getElementById("topicLinkTitle").textContent = "确认 AI 整理的项目草稿";
    document.getElementById("topicLinkTarget").textContent = `右侧 AI 已结合当前对象、最近 ${Number(draft.conversation_turns_used || 1)} 轮对话和已复核证据生成草稿；确认前不会建立正式关联。`;
    button.disabled = false;
    button.textContent = "重新打开项目草稿";
  } catch (error) {
    button.disabled = false;
    button.textContent = `整理失败：${error.message}`;
  }
}

function renderTopicSuggestions() {
  const panel = document.getElementById("topicSuggestionPanel");
  const selectedId = state.pendingTopicSuggestion?.suggestion_id;
  if (!state.topicSuggestions.length) {
    document.getElementById("topicDraftFields").hidden = true;
    panel.innerHTML = `<div class="empty-state">当前意图没有匹配到足够的已复核事件或确认材料。请换一种说法，或选择“手动定义题目”先建立研究框架。</div>`;
    syncTopicSubmitState();
    return;
  }
  panel.innerHTML = `<header><div><strong>项目草稿与证据清单</strong><p>可以直接编辑题目，也可以回到右侧 AI 继续沟通修改。</p></div><span>库内证据 · 最终由你确认</span></header><div class="topic-suggestion-list">${state.topicSuggestions.map((item) => {
    const selected = selectedId === item.suggestion_id;
    const chosenEvents = new Set(state.pendingTopicSuggestion?.event_ids || item.event_ids);
    const chosenVersions = new Set(state.pendingTopicSuggestion?.document_version_ids || item.document_version_ids);
    const materials = item.materials || (item.document_version_ids || []).map((versionId) => ({ document_version_id: versionId, title: `固定材料版本 #${versionId}`, source_space: "country" }));
    const members = selected ? `<div class="topic-suggestion-members"><span>纳入的已复核事件</span>${(item.events || []).map((event) => `<label><input type="checkbox" data-topic-suggestion-event="${event.id}"${chosenEvents.has(event.id) ? " checked" : ""}>${escapeHtml(event.title)}</label>`).join("") || `<small>当前证据簇尚未关联已复核事件</small>`}<span>纳入的确认材料</span>${materials.map((material) => `<label><input type="checkbox" data-topic-suggestion-material="${material.document_version_id}"${chosenVersions.has(material.document_version_id) ? " checked" : ""}>${escapeHtml(material.title)} · ${material.source_space === "event" ? "事件空间" : material.source_space === "country_event" ? "国别＋事件空间" : "国别空间"}</label>`).join("") || `<small>当前候选没有确认材料</small>`}</div>` : "";
    const sourceSummary = item.source_summary || { reviewed_events: (item.event_ids || []).length, event_materials: 0, country_materials: (item.document_version_ids || []).length };
    const draftAction = state.topicSuggestions.length > 1 ? `<button type="button" class="ghost-btn" data-apply-topic-suggestion="${escapeHtml(item.suggestion_id)}">${selected ? "已采用" : "采用这个"}</button>` : `<span class="status-chip">当前草稿</span>`;
    return `<article class="topic-suggestion-card${selected ? " is-selected" : ""}"><div><span class="status-chip">${item.review_band === "recommended" ? "推荐" : "可探索"} · ${Math.round(Number(item.confidence || 0) * 100)}%</span><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.research_question)}</p><small>${(item.reasons || []).map(escapeHtml).join(" · ")}</small>${(item.evidence_gaps || []).length ? `<small class="is-gap">${item.evidence_gaps.map(escapeHtml).join(" · ")}</small>` : ""}</div>${draftAction}<div class="topic-suggestion-source-summary"><span>${Number(sourceSummary.reviewed_events || 0)} 条已复核事件</span><span>${Number(sourceSummary.event_materials || 0)} 份事件材料</span><span>${Number(sourceSummary.country_materials || 0)} 份国别材料</span></div>${members}</article>`;
  }).join("")}</div>`;
}

function applyTopicSuggestion(suggestionId) {
  const suggestion = state.topicSuggestions.find((item) => item.suggestion_id === suggestionId);
  if (!suggestion) return;
  state.pendingTopicSuggestion = {
    ...suggestion,
    event_ids: [...suggestion.event_ids],
    document_version_ids: [...suggestion.document_version_ids],
  };
  if (state.pendingTopicLink?.kind === "event" && !state.pendingTopicSuggestion.event_ids.includes(state.pendingTopicLink.id)) state.pendingTopicSuggestion.event_ids.push(state.pendingTopicLink.id);
  if (state.pendingTopicLink?.kind === "material" && !state.pendingTopicSuggestion.document_version_ids.includes(state.pendingTopicLink.id)) state.pendingTopicSuggestion.document_version_ids.push(state.pendingTopicLink.id);
  document.getElementById("newTopicTitle").value = suggestion.title;
  document.getElementById("newTopicQuestion").value = suggestion.research_question;
  document.getElementById("topicDraftFields").hidden = false;
  renderTopicSuggestions();
  syncTopicSubmitState();
}

function closeTopicLinkDialog() {
  const dialog = document.getElementById("topicLinkDialog");
  if (dialog.open) dialog.close();
  state.pendingTopicLink = null;
  state.pendingTopicSuggestion = null;
  state.topicSuggestions = [];
  state.pendingTopicCountryIso3 = null;
  document.getElementById("topicCountrySelect").disabled = false;
}

async function submitTopicLink(event) {
  event.preventDefault();
  const submit = document.getElementById("topicLinkSubmit");
  const title = document.getElementById("newTopicTitle").value.trim();
  const researchQuestion = document.getElementById("newTopicQuestion").value.trim();
  const researchIntent = document.getElementById("topicResearchIntent").value.trim();
  const countryIso3 = document.getElementById("topicCountrySelect").value;
  let caseId = Number(document.getElementById("existingTopicSelect").value || 0);
  if (!title && !caseId) {
    showToast(state.pendingTopicLink ? "请选择已有项目，或填写新项目。" : "请填写项目标题。");
    return;
  }
  submit.disabled = true;
  submit.textContent = "正在保存…";
  let createdCase = false;
  try {
    if (title) {
      const selectedSuggestion = state.pendingTopicSuggestion;
      const suggestionPayload = { suggestion_id: selectedSuggestion?.suggestion_id, title, research_question: researchQuestion, country_iso3: countryIso3, source_run_id: selectedSuggestion?.source_run_id, event_ids: selectedSuggestion?.event_ids || [], document_version_ids: selectedSuggestion?.document_version_ids || [] };
      if (researchIntent.length >= 4) suggestionPayload.research_intent = researchIntent;
      const created = selectedSuggestion
        ? await apiFetch("/reader/research-cases/from-suggestion", { method: "POST", body: JSON.stringify(suggestionPayload) })
        : await apiFetch("/reader/research-cases", { method: "POST", body: JSON.stringify({ title, research_question: researchQuestion, country_iso3: countryIso3, ...ProjectOnboarding.payload() }) });
      caseId = Number(created.id);
      createdCase = true;
    }
    const pending = state.pendingTopicLink;
    if (pending?.kind === "event" && !state.pendingTopicSuggestion) {
      const result = await apiFetch(`/reader/research-cases/${caseId}/events`, { method: "POST", body: JSON.stringify({ event_id: pending.id }) });
      showToast(result.created ? "事件已加入项目" : "该事件已在项目中");
    } else if (pending?.kind === "material" && !state.pendingTopicSuggestion) {
      const result = await apiFetch(`/reader/research-cases/${caseId}/materials`, { method: "POST", body: JSON.stringify({ document_version_id: pending.id }) });
      showToast(result.created ? "材料已加入项目" : "该材料版本已在项目中");
    } else {
      showToast("项目已创建");
    }
    closeTopicLinkDialog();
    if (createdCase || !pending) navigate(`/projects/${caseId}/tasks?new=1&prompt=1`);
  } catch (error) {
    showToast(`保存失败：${error.message}`);
  } finally {
    submit.disabled = false;
    syncTopicSubmitState();
  }
}

async function removeTopicEvent(eventId, button) {
  if (!state.topic) return;
  button.disabled = true;
  try {
    await apiFetch(`/reader/research-cases/${state.topic.id}/events`, { method: "DELETE", body: JSON.stringify({ event_id: Number(eventId) }) });
    showToast("已移除事件关联，底层事件未删除");
    await loadTopics(state.topic.id, state.topicPane, state.routeSerial);
  } catch (error) { showToast(`移除失败：${error.message}`); button.disabled = false; }
}

async function removeTopicMaterial(documentId, button) {
  if (!state.topic) return;
  button.disabled = true;
  try {
    await apiFetch(`/reader/research-cases/${state.topic.id}/materials/${Number(documentId)}`, { method: "DELETE" });
    showToast("已移除材料关联，底层材料和版本未删除");
    await loadTopics(state.topic.id, state.topicPane, state.routeSerial);
  } catch (error) { showToast(`移除失败：${error.message}`); button.disabled = false; }
}

async function generateTopicDigest(caseId, button) {
  button.disabled = true;
  button.textContent = "正在冻结证据并生成…";
  try {
    const capabilities = await apiFetch("/reader/capabilities");
    const config = capabilities.find((item) => item.catalog_key === "team-weekly-research-deposit")?.configs?.find((item) => Number(item.research_case_id) === Number(caseId));
    if (!config) throw new ReaderApiError("请先从能力市场将“团队周报与资料沉淀”复制到当前项目。", "capability_missing");
    const payload = await apiFetch("/reader/capability-runs", { method: "POST", body: JSON.stringify({ config_id: Number(config.id), research_case_id: Number(caseId) }) });
    state.activeCapabilityRunId = payload.id;
    navigate(`/projects/${caseId}/outputs`);
  } catch (error) {
    showToast(`周报生成失败：${error.message}`);
    button.disabled = false;
    button.textContent = "生成团队周报";
  }
}

function showToast(message) { const toast = document.getElementById("toast"); toast.textContent = message; toast.hidden = false; window.setTimeout(() => { toast.hidden = true; }, 2400); }

document.addEventListener("click", async (event) => {
  const projectPrompt = event.target.closest("[data-project-prompt]");
  if (projectPrompt) {
    const form = document.querySelector("[data-project-task-form]");
    const textarea = form?.elements.question;
    if (textarea) {
      textarea.value = projectPrompt.dataset.projectPrompt || "";
      textarea.focus();
    }
    return;
  }
  const contextToggle = event.target.closest("[data-project-context-toggle]");
  if (contextToggle) {
    const panel = document.getElementById("projectContextPanel");
    const open = !panel?.classList.contains("is-mobile-open");
    panel?.classList.toggle("is-mobile-open", open);
    contextToggle.setAttribute("aria-expanded", String(open));
    return;
  }
  if (event.target.closest("[data-project-context-close]")) {
    document.getElementById("projectContextPanel")?.classList.remove("is-mobile-open");
    document.querySelector("[data-project-context-toggle]")?.setAttribute("aria-expanded", "false");
    return;
  }
  const evidenceReviewButton = event.target.closest("[data-project-evidence-review]");
  if (evidenceReviewButton) {
    const note = window.prompt("记录本项目采用理由（不会改动平台全局复核状态）。", "") ?? null;
    if (note === null) return;
    evidenceReviewButton.disabled = true;
    try {
      await apiFetch(`/reader/research-cases/${state.topic.id}/evidence-reviews`, { method: "POST", body: JSON.stringify({ evidence_type: evidenceReviewButton.dataset.evidenceType, object_type: evidenceReviewButton.dataset.objectType, object_id: Number(evidenceReviewButton.dataset.objectId || 0) || null, decision: evidenceReviewButton.dataset.projectEvidenceReview, note, linked_evidence: [] }) });
      await loadProjectEvidence(state.topic.id);
      showToast("已保存项目采用决定；平台来源与事件复核状态未改变。");
    } catch (error) { evidenceReviewButton.disabled = false; showToast(`保存失败：${error.message}`); }
    return;
  }
  const exportOutputButton = event.target.closest("[data-export-project-output]");
  if (exportOutputButton) {
    exportOutputButton.disabled = true;
    try {
      const format = exportOutputButton.dataset.exportProjectOutput;
      const targetId = Number(exportOutputButton.dataset.outputId);
      const idempotencyKey = `reader:export:${state.topic.id}:${targetId}:${format}`;
      const payload = await apiFetch(`/reader/research-cases/${state.topic.id}/outputs/${targetId}/exports`, { method: "POST", body: JSON.stringify({ format, idempotency_key: idempotencyKey }) });
      const content = payload.content || JSON.stringify(payload.artifact || {}, null, 2);
      const blob = new Blob([content], { type: payload.media_type || "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `project-${state.topic.id}-output-${targetId}.${format === "markdown" ? "md" : format}`;
      anchor.click();
      URL.revokeObjectURL(url);
      showToast("已生成受控冻结导出。");
    } catch (error) { showToast(`导出失败：${error.message}`); }
    finally { exportOutputButton.disabled = false; }
    return;
  }
  if (event.target.closest("[data-retry-route]")) { await route(); return; }
  const feedTab = event.target.closest("[data-country-feed]");
  if (feedTab) {
    const view = countryReadingState();
    const pane = document.querySelector(".country-pane-viewport");
    const scroller = pane && ["auto", "scroll"].includes(getComputedStyle(pane).overflowY) ? pane : document.scrollingElement;
    view.scroll[view.layer] = scroller?.scrollTop || 0;
    view.layer = feedTab.dataset.countryFeed;
    renderCountryRecentSignals();
    if (scroller) scroller.scrollTop = view.scroll[view.layer] || 0;
    return;
  }
  const readingMore = event.target.closest("[data-reading-more]");
  if (readingMore) { countryReadingState()[readingMore.dataset.readingMore] += 12; if (readingMore.dataset.readingMore === "policy") renderPolicy(); else renderCountryRecentSignals(); return; }
  if (event.target.closest("[data-close-country-reading]")) { closeCountryReading(); return; }
  const reportSource = event.target.closest("[data-report-source]");
  if (reportSource) { countryReadingState().source = reportSource.dataset.reportSource; await loadPublicationChannel(state.country.iso3,"think_tank_report"); if(state.publicationReadingId) navigate(`/countries/${state.country.iso3}/reports`); closePublicationDirectory("reports"); return; }
  const policyCategoryButton=event.target.closest("[data-policy-category]");
  if(policyCategoryButton) { countryReadingState().policyCategory=policyCategoryButton.dataset.policyCategory; countryReadingState().policy=12; renderPolicy(); if(state.publicationReadingId) navigate(`/countries/${state.country.iso3}/policies`); closePublicationDirectory("policies"); return; }
  const morePublications=event.target.closest("[data-publication-more]");
  if(morePublications) { await loadPublicationChannel(state.country.iso3,morePublications.dataset.publicationMore,true); return; }
  const directory=event.target.closest("[data-publication-directory]");
  if(directory) { const workbench=directory.closest(".publication-workbench"); const open=workbench.classList.toggle("directory-open"); directory.setAttribute("aria-expanded",String(open)); return; }
  const publicationLink=event.target.closest("[data-publication-open]");
  if(publicationLink && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button===0) { event.preventDefault(); const view=countryReadingState(); view.publicationScroll ||= {}; view.publicationScroll[state.countryPane]=publicationScroller().scrollTop; view.publicationFocus=publicationLink.getAttribute("href"); navigate(view.publicationFocus.slice(1)); return; }
  if (event.target.closest("[data-compare-reports]")) { openReportComparison(); return; }
  const policyReading = event.target.closest("[data-policy-reading]");
  if (policyReading) { await openMaterial(Number(policyReading.dataset.policyReading)); return; }
  const countryEventButton = event.target.closest("[data-country-event-id]");
  if (countryEventButton) {
    state.selectedCountryEventId = Number(countryEventButton.dataset.countryEventId);
    document.querySelectorAll("[data-country-event-id]").forEach((button) => button.classList.toggle("is-selected", button === countryEventButton));
    navigate(`/countries/${state.country.iso3}/events/${state.selectedCountryEventId}`);
    return;
  }
  const providerButton = event.target.closest("[data-data-provider]");
  if (providerButton) { countryReadingState().dataProvider=Number(providerButton.dataset.dataProvider); state.selectedDataIndicatorKey=null; renderCountryDataCatalog(); return; }
  const categoryButton = event.target.closest("[data-data-category]");
  if (categoryButton) { countryReadingState().category = categoryButton.dataset.dataCategory; document.getElementById("countryDataCatalogSearch").value=""; state.selectedDataIndicatorKey=null; renderCountryDataCatalog(); return; }
  if (event.target.closest("[data-show-data-records]")) { await loadCountryDataRecords(); return; }
  const dataOffset = event.target.closest("[data-data-offset]");
  if (dataOffset) { await loadCountryDataRecords(Number(dataOffset.dataset.dataOffset)); return; }
  if (event.target.closest("[data-cite-data]")) {
    const selected=selectedCountryDataIndicator(); if (!selected) return;
    try {
      const params=countryDataQuery(selected); params.set("format","citation");
      const result=await apiFetch(`/structured-datasets/${selected.dataset.id}/export?${params}`);
      openCountryReading("指标引用");
      document.getElementById("countryEventChainDetail").innerHTML=`<p>${escapeHtml(result.citation)}</p><p>许可：${escapeHtml(result.license || "未注明")}</p>${result.source_urls.map(url=>`<p>${escapeHtml(url)}</p>`).join("")}`;
    } catch(error) { showToast(error.message); } return;
  }
  const countryDataButton = event.target.closest("[data-country-data-indicator]");
  if (countryDataButton) {
    state.selectedDataIndicatorKey = countryDataButton.dataset.countryDataIndicator;
    openCountryDataIndicator();
    return;
  }
  const researchWindowButton = event.target.closest("[data-research-window]");
  if (researchWindowButton) {
    state.researchWindowYears = Number(researchWindowButton.dataset.researchWindow || 3);
    countryReadingState().researchYear=null;
    document.querySelectorAll("[data-research-window]").forEach((button) => button.classList.toggle("is-active", button === researchWindowButton));
    await loadPublicationChannel(state.country.iso3,"frontier_research");
    return;
  }
  const capabilityCategory = event.target.closest("[data-capability-category]");
  if (capabilityCategory) {
    state.skillCategory = capabilityCategory.dataset.capabilityCategory;
    document.querySelectorAll("[data-capability-category]").forEach((button) => button.classList.toggle("is-active", button === capabilityCategory));
    renderCapabilityCatalogRows();
    return;
  }
  const searchTypeButton = event.target.closest("[data-search-type]");
  if (searchTypeButton) {
    state.materialSearchType = searchTypeButton.dataset.searchType || "all";
    document.querySelectorAll("[data-search-type]").forEach((button) => button.classList.toggle("is-active", button === searchTypeButton));
    syncUnifiedSearchFilterAvailability();
    document.getElementById("materialSearchForm")?.requestSubmit();
    return;
  }
  if (event.target.closest("#mobileSearchFilterToggle")) {
    const sidebar = document.querySelector("#globalSearchView .material-search-sidebar");
    const button = document.getElementById("mobileSearchFilterToggle");
    const open = sidebar?.classList.toggle("is-mobile-open");
    if (button) {
      button.setAttribute("aria-expanded", String(Boolean(open)));
      button.textContent = open ? "收起筛选" : "展开筛选";
      button.blur();
    }
    if (window.innerWidth <= 767) {
      requestAnimationFrame(() => {
        document.documentElement.scrollLeft = 0;
        document.body.scrollLeft = 0;
        document.querySelector(".main")?.scrollTo({ left: 0 });
      });
    }
    return;
  }
  if (event.target.closest("#materialSearchReset")) {
    document.getElementById("materialSearchForm")?.reset();
    state.materialSearchType = "all";
    document.querySelectorAll("[data-search-type]").forEach((button) => button.classList.toggle("is-active", button.dataset.searchType === "all"));
    syncUnifiedSearchFilterAvailability();
    replaceRoute("/countries/search");
    state.routeQuery = new URLSearchParams();
    document.getElementById("materialSearchForm")?.requestSubmit();
    return;
  }
  if (event.target.closest("#materialSearchCopyLink")) {
    try { await navigator.clipboard.writeText(window.location.href); showToast("检索链接已复制"); }
    catch (_) { showToast("浏览器未允许复制，请从地址栏复制"); }
    return;
  }
  if (event.target.closest("#materialSearchShare")) {
    if (navigator.share) {
      try { await navigator.share({ title: "国别智枢统一检索", url: window.location.href }); }
      catch (error) { if (error.name !== "AbortError") showToast("系统分享未完成"); }
    } else {
      try { await navigator.clipboard.writeText(window.location.href); showToast("当前浏览器不支持系统分享，已复制链接"); }
      catch (_) { showToast("当前浏览器不支持系统分享"); }
    }
    return;
  }
  if (event.target.closest("#materialSearchExport")) { exportMaterialSearchMetadata(); return; }
  if (event.target.closest("#materialSearchReferences")) { exportMaterialSearchReferences(); return; }
  const copySearchCitation = event.target.closest("[data-copy-search-citation]");
  if (copySearchCitation) {
    try { await navigator.clipboard.writeText(copySearchCitation.dataset.citationText || ""); showToast("已复制 GB/T 7714 引用"); }
    catch (_) { showToast("浏览器未允许复制引用"); }
    return;
  }
  const downloadSearchCitation = event.target.closest("[data-download-search-citation]");
  if (downloadSearchCitation) {
    const item = state.materialSearchResults.find((row) => row.object_key === downloadSearchCitation.dataset.citationKey);
    if (!item) return;
    const citations = generateCitationData(unifiedCitationItem(item));
    const format = downloadSearchCitation.dataset.downloadSearchCitation;
    downloadSearchFile(citations[format] || "", `citation-${String(item.object_key).replace(/\W+/g, "-")}.${format === "bibtex" ? "bib" : "ris"}`, format === "bibtex" ? "application/x-bibtex" : "application/x-research-info-systems");
    return;
  }
  const addSearchProject = event.target.closest("[data-add-search-project]");
  if (addSearchProject) {
    const [kind, rawId] = String(addSearchProject.dataset.addSearchProject || "").split(":");
    if (kind === "event") await openTopicLinkDialog("event", rawId, addSearchProject.dataset.linkLabel);
    else if (kind === "document-version") await openTopicLinkDialog("material", rawId, addSearchProject.dataset.linkLabel);
    else showToast("该对象当前只能查看，尚不能加入项目");
    return;
  }
  const skillSectionButton = event.target.closest("[data-skill-section]");
  if (skillSectionButton) {
    const section = skillSectionButton.dataset.skillSection;
    state.skillSection = section;
    document.querySelectorAll("[data-skill-section]").forEach((button) => button.classList.toggle("is-active", button === skillSectionButton));
    document.querySelectorAll("[data-skill-panel]").forEach((panel) => { panel.hidden = panel.dataset.skillPanel !== section; });
    return;
  }
  const skillCatButton = event.target.closest("[data-skill-cat]");
  if (skillCatButton) {
    state.skillCategory = skillCatButton.dataset.skillCat;
    document.querySelectorAll("[data-skill-cat]").forEach((button) => button.classList.toggle("is-active", button === skillCatButton));
    renderCapabilityHallRows();
    return;
  }
  if (event.target.closest("#openCustomSkillModalBtn, [data-action='open-custom-modal']")) {
    openCustomSkillModal();
    return;
  }
  if (event.target.closest("[data-close-custom-skill]")) {
    closeCustomSkillModal();
    return;
  }
  if (event.target.closest("#openUploadSkillModalBtn, [data-action='open-upload-modal']")) {
    openUploadSkillModal();
    return;
  }
  if (event.target.closest("[data-close-upload-skill]")) {
    closeUploadSkillModal();
    return;
  }
  if (event.target.closest("#skillManageGearBtn")) {
    openManageSkillsModal();
    return;
  }
  if (event.target.closest("[data-close-manage-skill]")) {
    closeManageSkillsModal();
    return;
  }
  if (event.target.closest("[data-close-manage]")) {
    closeManageSkillsModal();
  }
  const fieldUploadButton = event.target.closest("[data-open-field-upload]");
  if (fieldUploadButton) { await openFieldMaterialDialog(fieldUploadButton.dataset.uploadScope || "country"); return; }
  if (event.target.closest("[data-close-field-upload]")) { closeFieldMaterialDialog(); return; }
  const organizeFieldButton = event.target.closest("[data-organize-field-material]");
  if (organizeFieldButton) {
    try { await organizeFieldMaterial(organizeFieldButton.dataset.organizeFieldMaterial, organizeFieldButton); }
    catch (error) { showToast(`S05 运行失败：${error.message}`); organizeFieldButton.disabled = false; }
    return;
  }
  if (event.target.closest("#topicFrontierRefresh")) { await loadTopicFrontiers(); return; }
  const frontierDetailButton = event.target.closest("[data-open-frontier]");
  if (frontierDetailButton) { openFrontierDetail(frontierDetailButton.dataset.openFrontier); return; }
  const frontierButton = event.target.closest("[data-create-from-frontier]");
  if (frontierButton) { await openFrontierDraft(frontierButton.dataset.createFromFrontier); return; }
  const createTopicButton = event.target.closest("[data-create-topic]");
  if (createTopicButton) {
    await openTopicLinkDialog("create");
    return;
  }
  if (event.target.closest("[data-topic-return-ai]")) { startTopicCreationInAssistant(); return; }
  if (event.target.closest("[data-topic-manual]")) { showManualTopicDraft(); return; }
  const suggestionButton = event.target.closest("[data-apply-topic-suggestion]"); if (suggestionButton) { applyTopicSuggestion(suggestionButton.dataset.applyTopicSuggestion); return; }
  const useSkillButton = event.target.closest("[data-use-skill]"); if (useSkillButton) {
    state.assistantLastTrigger = useSkillButton;
    setAssistantFocus(useSkillButton);
    openCopilot(true);
    const input = document.getElementById("assistantQuestion");
    input.value = useSkillButton.dataset.useSkill;
    input.focus();
    return;
  }
  const mapSelectionToggle = event.target.closest("[data-map-selection-toggle]");
  if (mapSelectionToggle) {
    const selection = mapSelectionToggle.closest(".map-selection");
    const expanded = selection?.classList.toggle("is-expanded") || false;
    mapSelectionToggle.setAttribute("aria-expanded", String(expanded));
    mapSelectionToggle.textContent = expanded ? "收起详情" : "展开详情";
    return;
  }
  const countryButton = event.target.closest("[data-select-country]"); if (countryButton) { selectCountry(state.catalog.find((item) => item.iso3 === countryButton.dataset.selectCountry)); return; }
  const countryModeButton = event.target.closest("[data-country-mode]"); if (countryModeButton) { state.countryCatalogMode = countryModeButton.dataset.countryMode; state.catalogLetter = "ALL"; renderCatalog(); return; }
  const letterButton = event.target.closest("[data-country-letter]"); if (letterButton) { state.catalogLetter = letterButton.dataset.countryLetter; renderCatalog(); return; }
  if (event.target.closest("#backToGlobal")) { navigate("/countries"); return; }
  const copilotTrigger = event.target.closest("[data-copilot-toggle]");
  if (copilotTrigger) { state.assistantLastTrigger = copilotTrigger; document.getElementById("countryCopilot").hidden ? openCopilot(true) : closeCopilot(); return; }
  const focusTrigger = event.target.closest("[data-focus-copilot]"); if (focusTrigger) { state.assistantLastTrigger = focusTrigger; focusCopilot(); return; }

  if (event.target.closest("#copilotCloseBtn")) { closeCopilot(); return; }
  if (event.target.closest("#copilotHistoryBtn")) {
    if (state.assistantHistoryOpen) closeAssistantHistory();
    else await openCountryAssistantHistory();
    return;
  }
  if (event.target.closest("[data-new-assistant-conversation]")) { startNewAssistantConversation(); return; }
  if (event.target.closest("[data-reload-assistant-history]")) { await loadCountryAssistantHistory(); return; }
  if (event.target.closest("[data-load-more-assistant-history]")) { await loadCountryAssistantHistory({ append: true }); return; }
  const historyConversation = event.target.closest("[data-open-assistant-conversation]");
  if (historyConversation) {
    await activateAssistantHistory(historyConversation.dataset.openAssistantConversation);
    return;
  }
  if (event.target.closest("#copilotClearBtn")) {
    startNewAssistantConversation();
    return;
  }
  const toggleMatBtn = event.target.closest("[data-toggle-select-material]");
  if (toggleMatBtn) {
    toggleSelectMaterial(toggleMatBtn.dataset.toggleSelectMaterial, toggleMatBtn.dataset.materialTitle, toggleMatBtn.dataset.sourceName);
    return;
  }
  const removeChipBtn = event.target.closest("[data-remove-selected-chip]");
  if (removeChipBtn) {
    toggleSelectMaterial(removeChipBtn.dataset.removeSelectedChip);
    return;
  }
  if (event.target.closest("#clearSelectedContextBtn")) {
    state.selectedMaterials = [];
    renderSelectedContextChips();
    document.querySelectorAll(".material-select-btn.is-selected").forEach((b) => { b.classList.remove("is-selected"); b.textContent = "+ 选入 AI"; });
    document.querySelectorAll(".material-card.is-selected, article.is-selected").forEach((c) => c.classList.remove("is-selected"));
    renderTopicMaterialActionBar();
    if (state.topicPane === "materials") renderTopicMaterials();
    showToast("已清空选中的参考材料");
    return;
  }
  const scrollTarget = event.target.closest("[data-scroll-target]"); if (scrollTarget) { document.getElementById(scrollTarget.dataset.scrollTarget)?.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  if (event.target.closest("[data-focus-resource-search]")) { document.getElementById("resourceSearchInput")?.focus(); return; }
  const trendKeyButton = event.target.closest("[data-trend-key]"); if (trendKeyButton) { state.selectedTrendKey = trendKeyButton.dataset.trendKey; state.selectedTrendPoint = null; renderTrends(); return; }
  const trendPointButton = event.target.closest("[data-trend-point]"); if (trendPointButton) { state.selectedTrendPoint = Number(trendPointButton.dataset.trendPoint); renderTrends(); return; }
  const admLevelButton = event.target.closest("[data-adm-level]"); if (admLevelButton) { state.countryAdminLevel = admLevelButton.dataset.admLevel; state.countryAdminSelectedKey = null; renderAdmMap(); return; }
  const mapModeButton = event.target.closest("[data-map-mode]"); if (mapModeButton) { state.countryMapMode = mapModeButton.dataset.mapMode; syncCountryBasemap(); const meta = (state.country?.map?.levels || []).find((item) => item.level === state.countryAdminLevel); const status = document.getElementById("countryMapStatus"); if (status && meta) status.textContent = `${meta.level} · ${Number(meta.unit_count || 0).toLocaleString("zh-CN")} 个真实单元 · ${state.countryMapMode === "research" ? "研究底图" : "纯行政区"}`; return; }
  if (event.target.closest("[data-map-reset]")) { resetCountryMapView(); return; }
  if (event.target.closest("[data-topic-onboarding]")) { startTopicCreationInAssistant(); return; }
  const assistantPromptButton = event.target.closest("[data-assistant-prompt]"); if (assistantPromptButton) {
    const input = document.getElementById("assistantQuestion");
    input.value = assistantPromptButton.dataset.assistantPrompt;
    input.focus({ preventScroll: true });
    input.setSelectionRange(input.value.length, input.value.length);
    showToast("已按当前页面填入提示词，请确认后发送");
    return;
  }
  const qaButton = event.target.closest("[data-qa-question]"); if (qaButton) {
    const input = document.getElementById("assistantQuestion");
    state.assistantLastTrigger = qaButton;
    setAssistantFocus(qaButton);
    openCopilot();
    input.value = qaButton.dataset.qaQuestion;
    input.focus({ preventScroll: true });
    input.setSelectionRange(input.value.length, input.value.length);
    return;
  }
  const citationButton = event.target.closest("[data-citation-ref]"); if (citationButton) {
    const target = document.getElementById(citationButton.dataset.citationRef);
    const sources = target?.closest("details");
    if (sources) sources.open = true;
    target?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  const evidenceButton = event.target.closest("[data-event-evidence]"); if (evidenceButton) { navigate(`/events/${evidenceButton.dataset.eventEvidence}/differences`); return; }
  const eventLayerButton = event.target.closest("[data-event-layer]"); if (eventLayerButton) {
    state.eventFilterKind = eventLayerButton.dataset.eventLayer;
    state.eventFilterType = "";
    state.eventFilterMinSources = "";
    renderEventHall(state.eventRows, state.eventReports);
    return;
  }
  const previewEventButton = event.target.closest("[data-preview-event]"); if (previewEventButton) { state.eventPreviewKey = `event:${previewEventButton.dataset.previewEvent}`; renderEventHall(state.eventRows, state.eventReports); return; }
  const previewReportButton = event.target.closest("[data-preview-report-key]"); if (previewReportButton) { state.eventPreviewKey = previewReportButton.dataset.previewReportKey; renderEventHall(state.eventRows, state.eventReports); return; }
  const previewTopicMaterial = event.target.closest("[data-preview-topic-material]"); if (previewTopicMaterial) { state.topicMaterialPreviewId = previewTopicMaterial.dataset.previewTopicMaterial; renderTopicMaterials(); return; }
  if (event.target.closest("[data-topic-selected-ai]")) {
    state.assistantLastTrigger = event.target.closest("[data-topic-selected-ai]");
    openCopilot(true);
    const input = document.getElementById("assistantQuestion");
    input.value = "请比较已选项目材料的事实、数字与立场差异，并指出证据缺口。";
    input.focus({ preventScroll: true });
    return;
  }
  const directDownloadButton = event.target.closest("[data-direct-download-capability]");
  if (directDownloadButton) {
    await directDownloadCapability(directDownloadButton.dataset.directDownloadCapability, directDownloadButton);
    return;
  }
  const installSkillBtn = event.target.closest("[data-install-skill]");
  if (installSkillBtn) {
    const slug = installSkillBtn.dataset.installSkill;
    const installed = toggleInstallSkill(slug);
    const item = (state.capabilities || []).find((c) => c.slug === slug);
    const name = item ? skillDisplayName(item) : slug;
    showToast(installed ? `✓ 已成功安装「${name}」到我的工作台` : `已从我的工作台移除「${name}」`);
    renderCapabilityHallRows();
    return;
  }
  const openSkillButton = event.target.closest("[data-open-skill]"); if (openSkillButton) { navigate(`/capabilities/${openSkillButton.dataset.openSkill}`); return; }
  const topicRunButton = event.target.closest("[data-view-topic-run]"); if (topicRunButton) { state.activeCapabilityRunId = Number(topicRunButton.dataset.viewTopicRun); navigate(`/projects/${state.topic.id}/outputs`); return; }
  const addEventButton = event.target.closest("[data-add-event-topic]"); if (addEventButton) { await openTopicLinkDialog("event", addEventButton.dataset.addEventTopic, addEventButton.dataset.linkLabel); return; }
  const projectMaterialPicker = event.target.closest("[data-open-project-material-picker]");
  if (projectMaterialPicker) { await openBulkProjectMaterialDialog(projectMaterialPicker.dataset.openProjectMaterialPicker); return; }
  const projectContextButton = event.target.closest("[data-open-project-context]");
  if (projectContextButton) { await openProjectContextDialog(projectContextButton.dataset.openProjectContext); return; }
  if (event.target.closest("[data-toggle-all-project-materials]")) { toggleAllBulkProjectMaterials(); return; }
  if (event.target.closest("[data-close-bulk-project-material]")) { closeBulkProjectMaterialDialog(); return; }
  if (event.target.closest("[data-close-project-context]")) { closeProjectContextDialog(); return; }
  const removeEventButton = event.target.closest("[data-remove-topic-event]"); if (removeEventButton) { await removeTopicEvent(removeEventButton.dataset.removeTopicEvent, removeEventButton); return; }
  const removeMaterialButton = event.target.closest("[data-remove-topic-material]"); if (removeMaterialButton) { await removeTopicMaterial(removeMaterialButton.dataset.removeTopicMaterial, removeMaterialButton); return; }
  const generateDigestButton = event.target.closest("[data-generate-topic-digest]"); if (generateDigestButton) { await generateTopicDigest(generateDigestButton.dataset.generateTopicDigest, generateDigestButton); return; }
  const generatePolicyGraph = event.target.closest("[data-generate-policy-graph]");
  if (generatePolicyGraph) {
    generatePolicyGraph.disabled = true;
    try {
      await apiFetch(`/reader/research-cases/${generatePolicyGraph.dataset.generatePolicyGraph}/policy-impact-graph/runs`, { method: "POST", body: "{}" });
      await renderTopicPolicyGraph(Number(generatePolicyGraph.dataset.generatePolicyGraph));
    } catch (error) { showToast(`关系图生成失败：${error.message}`); generatePolicyGraph.disabled = false; }
    return;
  }
  const reviewPolicyEdge = event.target.closest("[data-review-policy-edge]");
  if (reviewPolicyEdge) {
    const root = document.getElementById("topicPolicyGraphRoot");
    const graph = JSON.parse(root?.dataset.graph || "{}");
    const runId = Number(root?.dataset.runId || 0);
    const baseRevisionNo = Number(root?.dataset.revisionNo || 0);
    if (!runId || !graph.type) return;
    graph.edge_review_states = { ...(graph.edge_review_states || {}), [reviewPolicyEdge.dataset.reviewPolicyEdge]: reviewPolicyEdge.dataset.reviewState };
    const note = window.prompt("可选：记录研究者判断依据或修改说明。", "") || "";
    try {
      await apiFetch(`/reader/capability-runs/${runId}/revisions`, { method: "POST", body: JSON.stringify({ artifact: graph, note, base_revision_no: baseRevisionNo }) });
      await renderTopicPolicyGraph(state.topic.id);
      showToast("已保存研究者对该关系的判断，未改写底层事实状态。");
    } catch (error) { showToast(`保存失败：${error.message}`); }
    return;
  }
  if (event.target.closest("[data-bootstrap-reader-key]")) {
    try {
      const payload = await apiFetch("/reader/auth/bootstrap", { method: "POST", body: JSON.stringify({ label: "负责人浏览器会话" }) });
      window.sessionStorage.setItem(READER_KEY_STORAGE_KEY, payload.access_key);
      await renderTopicCollaboration(state.topic.id);
      showToast("负责人密钥已初始化，原始值只保存在本次会话。");
    } catch (error) { showToast(`初始化失败：${error.message}`); }
    return;
  }
  if (event.target.closest("[data-save-reader-key]")) {
    const value = document.getElementById("readerSessionKey")?.value.trim() || "";
    if (value) window.sessionStorage.setItem(READER_KEY_STORAGE_KEY, value); else window.sessionStorage.removeItem(READER_KEY_STORAGE_KEY);
    await renderTopicCollaboration(state.topic.id);
    return;
  }
  const removeMemberButton = event.target.closest("[data-remove-member]");
  if (removeMemberButton) {
    if (!window.confirm("确认移除该成员并立即吊销其本机访问密钥？")) return;
    try {
      await apiFetch(`/reader/research-cases/${state.topic.id}/members/${removeMemberButton.dataset.removeMember}`, { method: "DELETE" });
      await renderTopicCollaboration(state.topic.id);
    } catch (error) { showToast(`移除失败：${error.message}`); }
    return;
  }
  const revokeMemberKeyButton = event.target.closest("[data-revoke-member-key]");
  if (revokeMemberKeyButton) {
    if (!window.confirm("确认立即吊销该成员密钥？已打开的页面下次请求将失效。")) return;
    try {
      await apiFetch(`/reader/research-cases/${state.topic.id}/members/${revokeMemberKeyButton.dataset.memberId}/keys/${revokeMemberKeyButton.dataset.revokeMemberKey}`, { method: "DELETE" });
      await renderTopicCollaboration(state.topic.id);
      showToast("成员密钥已吊销。");
    } catch (error) { showToast(`吊销失败：${error.message}`); }
    return;
  }
  if (event.target.closest("[data-close-topic-dialog]")) { closeTopicLinkDialog(); return; }
  const eventButton = event.target.closest("[data-event-id]"); if (eventButton) { navigate(`/events/${eventButton.dataset.eventId}`); return; }
  const topicButton = event.target.closest("[data-topic-id]"); if (topicButton) { navigate(`/projects/${topicButton.dataset.topicId}`); return; }
  if (event.target.closest("[data-expand-policy]")) { state.policyExpanded = !state.policyExpanded; renderPolicy(); return; }
  const viewRunButton = event.target.closest("[data-view-capability-run]"); if (viewRunButton) { navigate(`/capabilities/runs/${viewRunButton.dataset.viewCapabilityRun}`); return; }
  const rerunButton = event.target.closest("[data-rerun-config]"); if (rerunButton) { await runCapability(rerunButton.dataset.rerunConfig, rerunButton.dataset.caseId, rerunButton); return; }
  const reviseRunButton = event.target.closest("[data-revise-run]");
  if (reviseRunButton) {
    try {
      const payload = await apiFetch(`/reader/capability-runs/${reviseRunButton.dataset.reviseRun}`);
      const edited = window.prompt("请修改候选结果 JSON。原始运行保持不变，修改后将新增一个人工修订版。", JSON.stringify(payload.output, null, 2));
      if (edited === null) return;
      const artifact = JSON.parse(edited);
      const note = window.prompt("请记录本次修订说明。", "研究者人工修订") || "";
      const revision = await apiFetch(`/reader/capability-runs/${payload.id}/revisions`, { method: "POST", body: JSON.stringify({ artifact, note }) });
      const writeback = document.querySelector(`[data-confirm-run-writeback="${payload.id}"]`);
      if (writeback) { writeback.dataset.revisionId = String(revision.id); writeback.textContent = `确认写回修订 #${revision.revision_no}`; }
      showToast(`已保存人工修订 #${revision.revision_no}；原始运行未被覆盖。`);
    } catch (error) { showToast(`修订失败：${error.message}`); }
    return;
  }
  const writebackButton = event.target.closest("[data-confirm-run-writeback]"); if (writebackButton) {
    if (!window.confirm(`确认把本次冻结产物关联到 ${writebackButton.dataset.targetType} ${writebackButton.dataset.targetKey}？`)) return;
    writebackButton.disabled = true;
    try {
      const revisionId = Number(writebackButton.dataset.revisionId || 0) || null;
      const idempotencyKey = `reader:${writebackButton.dataset.confirmRunWriteback}:${writebackButton.dataset.targetType}:${writebackButton.dataset.targetKey}:${revisionId || "original"}`;
      const result = await apiFetch(`/reader/capability-runs/${writebackButton.dataset.confirmRunWriteback}/writeback`, { method: "POST", body: JSON.stringify({ target_type: writebackButton.dataset.targetType, target_key: writebackButton.dataset.targetKey, idempotency_key: idempotencyKey, revision_id: revisionId }) });
      writebackButton.textContent = result.idempotent_replay ? "已写回（重复确认）" : "已确认写回";
      if (state.topic?.id && document.getElementById("projectFormalOutputs")) await loadProjectOutputs(state.topic.id);
      showToast(result.idempotent_replay ? "幂等返回已有写回记录" : "已记录人工确认写回");
    } catch (error) { writebackButton.disabled = false; showToast(`写回失败：${error.message}`); }
    return;
  }
  const ignoreRunButton = event.target.closest("[data-ignore-run]"); if (ignoreRunButton) { ignoreRunButton.closest(".run-review-actions")?.remove(); showToast("本次结果已忽略，未写回任何对象"); return; }
  const previewConfigButton = event.target.closest("[data-preview-capability-config]");
  if (previewConfigButton) {
    const root = document.getElementById("capabilityConfigPreview");
    try {
      const caseId = Number(previewConfigButton.dataset.caseId || 0) || null;
      const preview = await apiFetch(`/reader/capability-configs/${previewConfigButton.dataset.previewCapabilityConfig}/preview`, { method: "POST", body: JSON.stringify({ research_case_id: caseId, input_overrides: {} }) });
      if (root) root.innerHTML = `<section class="capability-preview-result ${preview.can_run ? "is-ready" : "is-blocked"}"><strong>${preview.can_run ? "可运行" : "当前不可运行"}</strong><p>${preview.missing_inputs?.length ? `缺少：${escapeHtml(preview.missing_inputs.join("、"))}` : "权限、范围、权利和模型策略已通过预检。"}</p><small>写回仍需人工确认</small></section>`;
    } catch (error) { if (root) root.innerHTML = errorState(error.message); }
    return;
  }
  const configStatusButton = event.target.closest("[data-capability-status]");
  if (configStatusButton) {
    if (configStatusButton.dataset.capabilityStatus === "archived" && !window.confirm("确认归档这个配置？历史版本和运行仍保留。")) return;
    try {
      await apiFetch(`/reader/capability-configs/${configStatusButton.dataset.configId}/status`, { method: "POST", body: JSON.stringify({ status: configStatusButton.dataset.capabilityStatus }) });
      await renderCapabilityMine(Number(configStatusButton.dataset.configId));
    } catch (error) { showToast(`状态更新失败：${error.message}`); }
    return;
  }
  const runButton = event.target.closest("[data-run-capability]"); if (runButton) await runCapability(runButton.dataset.runCapability,runButton.dataset.caseId,runButton);
  const actionButton = event.target.closest("[data-confirm-assistant-action]"); if (actionButton) {
    if (actionButton.dataset.assistantActionType === "topic_draft_prepare") {
      await openAssistantTopicDraft(actionButton.dataset.assistantRunId, actionButton);
      return;
    }
    if (!window.confirm("确认创建一次真实能力运行？")) return;
    actionButton.disabled = true;
    actionButton.textContent = "正在运行…";
    try {
      const result = await apiFetch(`/reader/assistant/actions/${actionButton.dataset.assistantRunId}/${actionButton.dataset.confirmAssistantAction}/confirm`, { method: "POST", body: "{}" });
      actionButton.textContent = `${runStatusLabel(result.action.execution_result?.status)} · 运行 #${result.action.execution_result?.capability_run_id}`;
      showToast(result.idempotent_replay ? "已返回同一运行结果" : "能力运行已完成");
    } catch (error) { actionButton.disabled = false; actionButton.textContent = `运行失败：${error.message}`; }
    return;
  }
  const materialButton = event.target.closest("[data-material-version]"); if (materialButton) { await openMaterial(materialButton.dataset.materialVersion); return; }
  if (event.target.closest("[data-close-material]")) document.getElementById("materialDrawer").close();
});

document.getElementById("countryCatalogSearch").addEventListener("input",() => { state.catalogLetter = "ALL"; renderCatalog(); });
document.getElementById("topicLinkForm").addEventListener("submit", submitTopicLink);
document.getElementById("bulkProjectMaterialForm").addEventListener("submit", submitBulkProjectMaterials);
document.getElementById("bulkProjectMaterialForm").addEventListener("change", (event) => {
  if (event.target.matches("[data-bulk-project-item]")) toggleBulkProjectMaterial(event.target.dataset.bulkProjectItem, event.target.checked);
  if (event.target.matches("#bulkProjectTarget")) syncBulkProjectMaterialSubmit();
});
document.getElementById("projectContextForm").addEventListener("submit", submitProjectContext);
document.getElementById("projectContextTarget").addEventListener("change", (event) => {
  document.getElementById("projectContextSubmit").disabled = !Number(event.target.value || 0);
});
document.getElementById("materialSearchForm")?.addEventListener("submit", submitMaterialSearch);
document.getElementById("fieldMaterialForm").addEventListener("submit", submitFieldMaterial);
document.getElementById("skillCustomForm")?.addEventListener("submit", submitCustomSkill);
document.getElementById("skillUploadForm")?.addEventListener("submit", submitUploadSkill);
document.addEventListener("submit", async (event) => {
  const taskForm = event.target.closest("[data-project-task-form]");
  if (taskForm) {
    event.preventDefault();
    if (ProjectSkillUI.handlesInput(taskForm)) { await ProjectSkillUI.submitSupplement(taskForm); return; }
    if (taskForm.elements.research_mode?.value === "plan") { await ProjectPlanUI.submit(taskForm); return; }
    const caseId = Number(taskForm.dataset.projectTaskForm);
    const taskData = new FormData(taskForm);
    const question = taskData.get("question")?.toString().trim();
    if (!question) return;
    const references = [];
    const countryReference = taskData.get("country_reference")?.toString();
    if (countryReference) references.push({ type: "country", id: countryReference, label: countryReference });
    const referenceFields = [["event_reference", "event"], ["material_reference", "document_version"], ["data_slice_reference", "data_slice"]];
    referenceFields.forEach(([field, type]) => {
      const value = taskData.get(field)?.toString();
      const select = taskForm.elements[field];
      if (value) references.push({ type, id: Number(value), label: select?.selectedOptions?.[0]?.textContent || "" });
    });
    taskForm.querySelectorAll('[data-prompt-material]:checked').forEach(input=>{if(!references.some(ref=>ref.type==='document_version' && Number(ref.id)===Number(input.dataset.promptMaterial))) references.push({type:'document_version',id:Number(input.dataset.promptMaterial),label:input.dataset.title});});
    const selectedCapabilities = syncProjectCapabilitySelection(taskForm);
    const capabilityConfigIds = selectedCapabilities.map((item) => Number(item.value));
    const capabilityNames = selectedCapabilities.map((item) => item.closest("label")?.querySelector("b")?.textContent || `能力 #${item.value}`);
    let conversationId = taskData.get("conversation_id")?.toString() || null;
    const thread = taskForm.closest(".project-conversation")?.querySelector(".project-task-thread");
    const submit = taskForm.querySelector("button[type=submit]");
    submit.disabled = true;
    ProjectWorkbench.setBusy(taskForm, true);
    thread?.querySelector(".project-chat-welcome, .project-chat-empty")?.remove();
    if (thread) thread.insertAdjacentHTML("beforeend", `<article class="is-user"><strong>研究者</strong><p>${escapeHtml(question)}</p>${capabilityNames.length ? `<small>本轮能力组合：${capabilityNames.map(escapeHtml).join(" → ")}</small>` : ""}</article><article class="is-running" data-project-running><strong>受控研究正在运行</strong><p>正在校验项目范围、能力状态与证据权限…</p></article>`);
    let chatCompleted = false;
    try {
      await streamAssistantRequest(
        { conversation_id: conversationId, question, references, capability_config_ids: capabilityConfigIds, online_mode: "off" },
        (eventName, envelope) => {
          const running = thread?.querySelector("[data-project-running]");
          const payload = envelope.payload || {};
          if (eventName === "run.started" && envelope.conversation_id) {
            conversationId = envelope.conversation_id;
            ProjectOnboarding.bindTask(taskForm,conversationId);
            taskForm.elements.conversation_id.value = conversationId;
            ProjectWorkbench.setBusy(taskForm, true, true);
            taskForm.elements.conversation_id.defaultValue = conversationId;
            state.projectObjectRoute = { kind: "task", id: conversationId };
            window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/projects/${caseId}/tasks/${encodeURIComponent(conversationId)}`);
            const heading = document.querySelector("[data-project-conversation-title]");
            const meta = document.querySelector("[data-project-conversation-meta]");
            if (heading) heading.textContent = question;
            if (meta) meta.textContent = "正在保存并生成回答…";
          } else if (eventName === "run.failed") {
            throw new Error(payload.message || "AI 对话未完成，原问题已保留");
          } else if (eventName === "run.completed") {
            chatCompleted = true;
            const response = payload.response || {};
            if (running) running.outerHTML = `<article class="is-assistant"><strong>项目研究结果</strong>${assistantResponseHtml(response, { id: `project-${response.run_id}`, stages: [] })}<small>本结果尚未写回项目。</small></article>`;
            const meta = document.querySelector("[data-project-conversation-meta]");
            if (meta) meta.textContent = "对话已保存 · 可以继续追问";
          } else if (running && eventName !== "heartbeat") {
            running.querySelector("p").textContent = assistantStageLabel(eventName, payload);
          }
        },
        `/reader/research-cases/${caseId}/tasks/stream`,
      );
      if (!chatCompleted) throw new Error("连接已结束但未收到完成结果，刷新可恢复已保存进度");
      localStorage.removeItem(taskForm.dataset.promptDraftKey);
      taskForm.reset();
      syncProjectCapabilitySelection(taskForm);
      if (conversationId) {
        taskForm.elements.conversation_id.value = conversationId;
        taskForm.elements.conversation_id.defaultValue = conversationId;
      }
      await loadProjectTasks(caseId);
      if (thread) thread.scrollTop = thread.scrollHeight;
    } catch (error) {
      const running = thread?.querySelector("[data-project-running]");
      if (running) running.outerHTML = `<article class="is-error"><strong>对话未完成</strong><p>${escapeHtml(error.message)}</p></article>`;
    } finally { submit.disabled = false; ProjectWorkbench.setBusy(taskForm, false); }
    return;
  }
  const configForm = event.target.closest("[data-capability-config-form]");
  if (configForm) {
    event.preventDefault();
    const data = new FormData(configForm);
    const submit = configForm.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const config = JSON.parse(data.get("config")?.toString() || "{}");
      await apiFetch(`/reader/capability-configs/${configForm.dataset.capabilityConfigForm}`, { method: "PATCH", body: JSON.stringify({ name: data.get("name"), config }) });
      await renderCapabilityMine(Number(configForm.dataset.capabilityConfigForm));
      showToast("已保存新的不可变配置版本。");
    } catch (error) { showToast(`保存失败：${error.message}`); }
    finally { submit.disabled = false; }
    return;
  }
  const scheduleForm = event.target.closest("[data-capability-schedule-form]");
  if (scheduleForm) {
    event.preventDefault();
    const data = new FormData(scheduleForm);
    const submit = scheduleForm.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const nextRunAt = new Date(data.get("next_run_at")?.toString() || "");
      if (Number.isNaN(nextRunAt.getTime())) throw new Error("请选择下次运行时间");
      await apiFetch(`/reader/capability-configs/${scheduleForm.dataset.capabilityScheduleForm}/schedules`, { method: "POST", body: JSON.stringify({ cadence: data.get("cadence"), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai", next_run_at: nextRunAt.toISOString(), idempotency_key: `schedule:${scheduleForm.dataset.capabilityScheduleForm}:${data.get("cadence")}:${nextRunAt.toISOString()}` }) });
      await renderCapabilityMine(Number(scheduleForm.dataset.capabilityScheduleForm));
      showToast("调度已保存；每次运行只生成待审候选结果。");
    } catch (error) { showToast(`调度创建失败：${error.message}`); }
    finally { submit.disabled = false; }
    return;
  }
  const copyForm = event.target.closest("[data-copy-capability-form]");
  if (copyForm) {
    event.preventDefault();
    const data = new FormData(copyForm);
    const scopeType = data.get("scope_type");
    const researchCaseId = Number(data.get("research_case_id") || 0) || null;
    if (scopeType === "research_case" && !researchCaseId) { showToast("请选择项目。"); return; }
    const submit = copyForm.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const initialConfig = JSON.parse(copyForm.dataset.initialConfig || "{}");
      const payload = await apiFetch(`/reader/capabilities/catalog/${encodeURIComponent(copyForm.dataset.copyCapabilityForm)}/copies`, { method: "POST", body: JSON.stringify({ name: data.get("name"), scope_type: scopeType, research_case_id: scopeType === "research_case" ? researchCaseId : null, config: initialConfig }) });
      showToast(payload.can_run ? "已创建私有副本；请先补齐配置并运行预览" : "已保存为待验证草稿");
      navigate(`/capabilities/mine/${payload.id}`);
    } catch (error) { showToast(`复制失败：${error.message}`); }
    finally { submit.disabled = false; }
    return;
  }
  const memberForm = event.target.closest("#researchMemberInviteForm");
  if (memberForm) {
    event.preventDefault();
    const data = new FormData(memberForm);
    try {
      const payload = await apiFetch(`/reader/research-cases/${state.topic.id}/members`, { method: "POST", body: JSON.stringify({ display_name: data.get("display_name"), email: data.get("email"), role: data.get("role"), key_label: `${data.get("display_name")} 本机密钥` }) });
      const target = document.getElementById("issuedMemberKey");
      target.hidden = false;
      target.innerHTML = payload.access_key ? `<strong>仅显示一次，请立即交给成员</strong><code>${escapeHtml(payload.access_key)}</code>` : "成员已加入项目，使用自己的平台账号登录。";
      memberForm.reset();
      showToast(payload.access_key ? "成员已添加，个人密钥只显示一次。" : "成员已加入项目。");
    } catch (error) { showToast(`邀请失败：${error.message}`); }
    return;
  }
  const form = event.target.closest("[data-skill-config-form]");
  if (!form) return;
  event.preventDefault();
  const configId = Number(form.dataset.skillConfigForm || 0);
  if (!configId) { showToast("当前 Skill 没有可运行配置"); return; }
  const overrides = readCapabilityOverrides(form);
  const caseId = Number(overrides.research_case_id || 0) || null;
  const submit = form.querySelector("button[type=submit]");
  await runCapability(configId, caseId, submit, overrides);
});
document.getElementById("fieldMaterialFile").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  const summary = document.getElementById("fieldMaterialFileSummary");
  if (!file) { summary.textContent = "支持 TXT、Markdown、PDF、JPG、PNG；单个文件不超过 10 MB。"; return; }
  summary.textContent = `${file.name} · ${formatBytes(file.size)} · ${file.type || "类型待后端校验"}`;
  const title = document.getElementById("fieldMaterialTitle");
  if (!title.value.trim()) title.value = file.name.replace(/\.[^.]+$/, "");
});
document.addEventListener("input",(event) => {
  if (event.target.matches("#countryLatestQuery")) { const view=countryReadingState(); view.reportQuery=event.target.value; view.reports=12; renderCountryRecentSignals(); return; }
  if (event.target.matches("#countryPolicyQuery")) {
    const view=countryReadingState(); view.policyQuery=event.target.value; view.policy=12;
    window.clearTimeout(eventSearchTimer);
    eventSearchTimer=window.setTimeout(()=>renderPolicy(), 220);
    return;
  }
  if (event.target.matches("#countryReportQuery")) {
    const view=countryReadingState(); view.reportQueryText=event.target.value;
    window.clearTimeout(eventSearchTimer);
    eventSearchTimer=window.setTimeout(()=>loadPublicationChannel(state.country.iso3,"think_tank_report"), 250);
    return;
  }
  if (event.target.matches("#countryResearchQuery")) {
    const view=countryReadingState(); view.researchQuery=event.target.value;
    window.clearTimeout(eventSearchTimer);
    eventSearchTimer=window.setTimeout(()=>loadPublicationChannel(state.country.iso3,"frontier_research"), 250);
    return;
  }
  if (event.target.matches("#countryDataCatalogSearch")) { renderCountryDataCatalog(); return; }
  if (event.target.matches("#countryEventQuery")) {
    state.countryEventFilters.query = event.target.value;
    state.countryEventPage = 0;
    window.clearTimeout(eventSearchTimer);
    eventSearchTimer = window.setTimeout(() => renderCountryRecentSignals(), 220);
    return;
  }
  if (event.target.matches("#topicMaterialQuery")) renderTopicMaterials();
  if (event.target.matches("#newTopicTitle, #newTopicQuestion")) syncTopicSubmitState();
  if (event.target.matches("#eventQueryFilter, #eventSourceNameFilter")) {
    state.eventFilterQuery = document.getElementById("eventQueryFilter")?.value || "";
    state.eventFilterSource = document.getElementById("eventSourceNameFilter")?.value || "";
    window.clearTimeout(eventSearchTimer);
    eventSearchTimer = window.setTimeout(() => loadEvents(null, "overview", state.routeSerial), 280);
  }
  if (event.target.matches("#skillQueryFilter")) { state.skillFilterQuery = event.target.value; renderCapabilityCatalogRows(); }
  if (event.target.matches("#methodSearchInput")) {
    const query = event.target.value.trim().toLowerCase();
    let visible = 0;
    document.querySelectorAll("#methodCards [data-method-keywords]").forEach((card) => {
      const matches = !query || `${card.dataset.methodKeywords || ""} ${card.textContent || ""}`.toLowerCase().includes(query);
      card.hidden = !matches;
      if (matches) visible += 1;
    });
    document.getElementById("methodSearchEmpty").hidden = visible > 0;
  }
});
document.addEventListener("change", async (event) => {
  if (event.target.matches("#countryLatestSource")) { const view=countryReadingState(); view.reportSource=event.target.value; view.reports=12; renderCountryRecentSignals(); return; }
  if (event.target.matches("#countryPolicySource")) { const view=countryReadingState(); view.policySource=event.target.value; view.policy=12; renderPolicy(); return; }
  if (event.target.matches("#countryResearchSource")) { countryReadingState().researchSource=event.target.value; loadPublicationChannel(state.country.iso3,"frontier_research"); return; }

  if (event.target.matches("[data-assistant-capability-config]")) {
    syncAssistantCapabilitySelection(event.target);
    return;
  }
  if (event.target.matches('[name="capability_config_ids"]')) {
    const form = event.target.closest("[data-project-task-form]");
    if (form) syncProjectCapabilitySelection(form, event.target);
    return;
  }
  if (event.target.matches("#overviewTopicRegion, #overviewTopicWindow, #overviewTopicSource")) { renderGlobalOverview(); return; }
  if (event.target.matches("#countryDataSourceFilter")) { countryReadingState().dataSource=event.target.value; renderCountryDataCatalog(); return; }
  if (event.target.matches("#countryDataCategoryFilter")) { countryReadingState().category=event.target.value || "全部"; document.getElementById("countryDataCatalogSearch").value=""; state.selectedDataIndicatorKey=null; renderCountryDataCatalog(); return; }
  if (event.target.matches("#countryDataIndicatorFilter")) {
    const key=event.target.value;
    if(!key) { state.selectedDataIndicatorKey=null; return; }
    state.selectedDataIndicatorKey=key;
    openCountryDataIndicator();
    return;
  }
  if (event.target.matches("[data-compare-report]")) {
    const selected = countryReadingState().compared;
    const id = Number(event.target.dataset.compareReport);
    if (event.target.checked && !selected.includes(id)) {
      if (selected.length === 4) { event.target.checked = false; showToast("最多选择 4 份报告"); return; }
      selected.push(id);
    } else if (!event.target.checked) countryReadingState().compared = selected.filter(value=>value !== id);
    updateReportCompareSelection(); return;
  }
  if (event.target.matches("#countryEventTimeBasis, #countryEventType, #countryEventPerspective")) {
    state.countryEventFilters.timeBasis = document.getElementById("countryEventTimeBasis")?.value || "occurred";
    state.countryEventFilters.type = document.getElementById("countryEventType")?.value || "";
    state.countryEventFilters.perspective = document.getElementById("countryEventPerspective")?.value || "";
    state.countryEventPage = 0;
    renderCountryRecentSignals();
    return;
  }
  if (event.target.matches("#capabilityValidationFilter, #capabilityScopeFilter")) { renderCapabilityCatalogRows(); return; }
  if (event.target.matches("[data-member-role]")) {
    try {
      await apiFetch(`/reader/research-cases/${state.topic.id}/members/${event.target.dataset.memberRole}`, { method: "PATCH", body: JSON.stringify({ role: event.target.value }) });
      await renderTopicCollaboration(state.topic.id);
    } catch (error) { showToast(`角色更新失败：${error.message}`); }
    return;
  }
  if (event.target.matches("#topicCountrySelect")) {
    state.pendingTopicCountryIso3 = event.target.value;
    const compatibleTopics = state.topics.filter((item) => item.scope?.country_iso3 === state.pendingTopicCountryIso3);
    document.getElementById("existingTopicSelect").innerHTML = `<option value="">请选择同国项目</option>${compatibleTopics.map((item) => `<option value="${item.id}">${escapeHtml(item.title)}</option>`).join("")}`;
    resetTopicComposer();
  }
  if (event.target.matches("#existingTopicSelect")) syncTopicSubmitState();
  if (event.target.matches("[data-topic-suggestion-event]") && state.pendingTopicSuggestion) {
    const eventId = Number(event.target.dataset.topicSuggestionEvent);
    const selected = new Set(state.pendingTopicSuggestion.event_ids || []);
    event.target.checked ? selected.add(eventId) : selected.delete(eventId);
    state.pendingTopicSuggestion.event_ids = [...selected];
  }
  if (event.target.matches("[data-topic-suggestion-material]") && state.pendingTopicSuggestion) {
    const versionId = Number(event.target.dataset.topicSuggestionMaterial);
    const selected = new Set(state.pendingTopicSuggestion.document_version_ids || []);
    event.target.checked ? selected.add(versionId) : selected.delete(versionId);
    state.pendingTopicSuggestion.document_version_ids = [...selected];
  }
  if (event.target.matches("#topicSourceFilter, #topicTypeFilter")) renderTopicMaterials();
  if (event.target.matches("#topicFeedWindow")) { state.topicFeedWindow = event.target.value; renderTopicFeed(); }
  if (event.target.matches("#eventCountryFilter, #eventWindowFilter, #eventTypeFilter, #eventSourceFilter")) {
    if (event.target.matches("#eventCountryFilter")) {
      commitEventCountryFilter(event.target);
      return;
    }
    state.eventFilterWindow = document.getElementById("eventWindowFilter")?.value || "all";
    state.eventFilterType = document.getElementById("eventTypeFilter")?.value || "";
    state.eventFilterMinSources = document.getElementById("eventSourceFilter")?.value || "";
    loadEvents(null, "overview", state.routeSerial);
  }
  if (event.target.matches("#eventMaterialTypeFilter")) {
    const selected = event.target.value;
    const root = document.getElementById("eventMaterialResults");
    if (!root || !state.eventEvidence) return;
    const sources = (state.eventEvidence.sources || []).filter((source) => {
      const type = source.document?.document_type || source.document?.source_type || "";
      return !selected || type === selected;
    });
    root.innerHTML = sources.length ? `<div class="item-list">${sources.map((source) => materialCard(source.document || {}, { addable: true })).join("")}</div>` : emptyState("没有符合当前筛选条件的材料");
  }
});
document.getElementById("assistantForm").addEventListener("submit",async (event) => { event.preventDefault(); const input = document.getElementById("assistantQuestion"); const question = input.value.trim(); if (!question) return; input.value = ""; await askAssistant(question); });
document.getElementById("assistantQuestion").addEventListener("keydown",(event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); document.getElementById("assistantForm").requestSubmit(); } });
document.getElementById("assistantStopBtn").addEventListener("click", async () => {
  const session = currentAssistantSession();
  if (!session.activeRunId) return;
  try {
    await apiFetch(`/reader/assistant/runs/${session.activeRunId}/cancel`, { method: "POST", body: "{}" });
    const message = [...session.messages].reverse().find((item) => item.pending);
    if (message) failAssistantMessage(session, message, new Error("本轮研究已停止；此前完成的对话仍保留。"));
  } catch (error) { showToast(`停止失败：${error.message}`); }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.matches("#materialSearchCountry")) {
    event.preventDefault();
    document.getElementById("materialSearchForm")?.requestSubmit();
    return;
  }
  if (event.key === "Enter" && event.target.matches("#eventCountryFilter")) {
    event.preventDefault();
    commitEventCountryFilter(event.target);
    return;
  }
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-trend-point]")) {
    event.preventDefault();
    event.target.click();
    return;
  }
  if (event.key === "Escape" && document.querySelector(".publication-workbench.directory-open")) {
    const toggle=document.querySelector(".publication-workbench.directory-open [data-publication-directory]");
    closePublicationDirectory(toggle.dataset.publicationDirectory); toggle.focus(); event.preventDefault(); return;
  }
  if (event.defaultPrevented || event.key !== "Escape" || document.getElementById("countryCopilot")?.hidden || document.querySelector("dialog[open]")) return;
  event.preventDefault();
  closeCopilot();
});

  // 顶部快速选国搜索框交互
  const quickSearchInput = document.getElementById("countryQuickSearchInput");
  const quickSearchList = document.getElementById("countryQuickList");
  if (quickSearchInput && quickSearchList) {
    function renderQuickList(q) {
      const norm = (q || "").trim().toLowerCase();
      if (!norm) {
        quickSearchList.hidden = true;
        quickSearchList.innerHTML = "";
        return;
      }
      const matches = state.catalog.filter((c) =>
        c.iso3.toLowerCase().includes(norm) ||
        (c.name_zh && c.name_zh.toLowerCase().includes(norm)) ||
        (c.name_en && c.name_en.toLowerCase().includes(norm))
      ).slice(0, 8);

      if (!matches.length) {
        quickSearchList.innerHTML = '<div style="padding:10px;font-size:11px;color:#64748b;text-align:center;">未找到匹配国家</div>';
      } else {
        quickSearchList.innerHTML = matches.map((c) => `
          <button type="button" class="country-quick-item" data-select-country="${c.iso3}">
            <span class="country-quick-code">${c.iso3}</span>
            <span class="country-quick-name"><strong>${escapeHtml(c.name_zh)}</strong><small>${escapeHtml(c.name_en)}</small></span>
            <span class="country-quick-status ${c.availability === "deep_ready" ? "is-ready" : ""}">${availabilityLabel(c.availability, hasTemplatePreview(c))}</span>
          </button>
        `).join("");
      }
      quickSearchList.hidden = false;
    }

    quickSearchInput.addEventListener("input", (e) => renderQuickList(e.target.value));
    quickSearchInput.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        quickSearchList.hidden = true;
        e.target.blur();
      }
    });
  }

  
const resState = {
  cat: "all",
  reg: "all",
  query: ""
};


let currentResItem = null;

function openResourceModal(itemId) {
  if (typeof CATALOG === "undefined") return;
  const item = CATALOG.find((c) => c.id === itemId);
  if (!item) return;

  currentResItem = item;
  const modal = document.getElementById("resourceDetailModal");
  if (!modal) return;

  document.getElementById("resModalIcon").innerHTML = iconFor(item);
  document.getElementById("resModalTitle").textContent = item.name;
  document.getElementById("resModalBy").textContent = item.by || "权威已验证信源";
  document.getElementById("resModalStatus").textContent = STATUS_LABELS[item.status] || "已接入";

  const kickerEl = document.getElementById("resModalKicker");
  if (item.kicker) {
    kickerEl.textContent = item.kicker;
    kickerEl.hidden = false;
  } else {
    kickerEl.hidden = true;
  }

  document.getElementById("resModalDesc").textContent = item.desc || "暂无简介。";
  document.getElementById("resModalCov").textContent = item.cov || "全球";
  document.getElementById("resModalSpan").textContent = item.span || "滚动更新";
  document.getElementById("resModalGran").textContent = item.gran || "报告/动态";
  document.getElementById("resModalPrio").textContent = `${item.prio || "P0"} · 优先接入`;

  // Routes
  const routesSection = document.getElementById("resModalRoutesSection");
  const routesList = document.getElementById("resModalRoutesList");
  if (item.routes && item.routes.length) {
    routesList.innerHTML = item.routes.map((r) => `
      <a class="res-route-item" href="${escapeHtml(safeUrl(r.url))}" target="_blank" rel="noopener noreferrer">
        <strong>${escapeHtml(r.name)}</strong>
        <span>直达 ↗</span>
      </a>
    `).join("");
    routesSection.hidden = false;
  } else {
    routesSection.hidden = true;
  }

  // BibTeX
  document.getElementById("resModalBib").textContent = item.bib || `@misc{${item.id}_2026,
  title = {${item.name}},
  url = {${item.url}}
}`;

  // Main Link
  const mainLink = document.getElementById("resModalMainLink");
  if (item.url) {
    mainLink.href = safeUrl(item.url);
    mainLink.hidden = false;
  } else {
    mainLink.hidden = true;
  }

  modal.showModal();
}

function closeResourceModal() {
  const modal = document.getElementById("resourceDetailModal");
  if (modal) modal.close();
}


const CATEGORY_DEFINITIONS = [
  { key: "official", name: "官方机构", icon: "landmark", desc: "部委公报、权威政策动态与国际组织矿产倡议" },
  { key: "academic", name: "学术文献", icon: "book-open", desc: "社科院顶刊与全球权威经济学同行评议期刊" },
  { key: "media", name: "新闻媒体", icon: "newspaper", desc: "全球关键矿产与非洲矿业权威行业观察" },
  { key: "thinktank", name: "研究机构", icon: "building", desc: "国际顶级安全智库与区域国别政策研究机构" },
  { key: "data", name: "数据与工具", icon: "chart", desc: "世界银行、联合国海关与宏观经贸数据库" }
];

function renderResourcePortal() {
  const container = document.getElementById("resourceCardsGrid");
  if (!container || typeof CATALOG === "undefined") return;

  // Filter only connected/verified sources (shadow or manual)
  const connectedCatalog = CATALOG.filter((item) => item.status === "shadow" || item.status === "manual");

  const query = resState.query.trim().toLowerCase();
  const filtered = connectedCatalog.filter((item) => {
    const matchCat = resState.cat === "all" || item.cat === resState.cat;
    const matchReg = resState.reg === "all" || item.region === resState.reg;
    const matchQ = !query ||
      item.name.toLowerCase().includes(query) ||
      (item.by && item.by.toLowerCase().includes(query)) ||
      (item.kicker && item.kicker.toLowerCase().includes(query)) ||
      (item.desc && item.desc.toLowerCase().includes(query)) ||
      (item.cov && item.cov.toLowerCase().includes(query));
    return matchCat && matchReg && matchQ;
  });

  if (!filtered.length) {
    container.innerHTML = '<div style="grid-column:1/-1;padding:48px 24px;text-align:center;color:#64748b;font-size:13px;background:#ffffff;border:1px dashed #cbd5e1;border-radius:14px;">未检索到匹配的已接入权威信源</div>';
    return;
  }

  // If viewing all categories and no specific search query, group by Category sections
  if (resState.cat === "all" && !query && resState.reg === "all") {
    container.innerHTML = CATEGORY_DEFINITIONS.map((catDef) => {
      const catItems = filtered.filter((i) => i.cat === catDef.key);
      if (!catItems.length) return "";
      const cardsHtml = catItems.map((item, idx) => renderSingleResourceCard(item, idx)).join("");
      return `
        <div class="resource-sec-block" style="grid-column: 1 / -1;">
          <div class="resource-sec-header">
            <div class="resource-sec-title-group">
              <h2>${iconSvg(catDef.icon)} ${escapeHtml(catDef.name)} <span class="count-badge">${catItems.length} 信源</span></h2>
              <p>${escapeHtml(catDef.desc)}</p>
            </div>
          </div>
          <div class="resource-cards-grid">
            ${cardsHtml}
          </div>
        </div>
      `;
    }).join("");
  } else {
    // Flattened grid with staggered animation
    container.innerHTML = filtered.map((item, idx) => renderSingleResourceCard(item, idx)).join("");
  }
}

function renderSingleResourceCard(item, idx) {
  const iconSvg = iconFor(item);
  const catName = CAT_LABELS[item.cat] || item.cat;
  const url = safeUrl(item.url);
  const delay = Math.min((idx % 6) * 50, 300);

  return `
    <article class="resource-card" data-res-id="${escapeHtml(item.id)}" style="animation-delay: ${delay}ms;">
      <div class="res-card-header">
        <div class="res-card-icon-squircle">${iconSvg}</div>
        <div class="res-card-title-group">
          <div class="res-card-title-line">
            <h3>${escapeHtml(item.name)}</h3>
            <span class="res-pill-connected"><i aria-hidden="true"></i>已接入</span>
          </div>
          <span class="res-card-by">${escapeHtml(item.by)}</span>
        </div>
      </div>

      <p class="res-card-desc">${escapeHtml(item.desc)}</p>

      <div class="res-card-badges-row">
        <span class="res-pill-cat">${escapeHtml(catName)}</span>
        ${item.kicker ? `<span class="res-kicker-tag">${escapeHtml(item.kicker)}</span>` : ""}
      </div>

      <div class="res-card-meta">
        <span>覆盖：${escapeHtml(item.cov || "全球")} · ${escapeHtml(item.gran || "动态")}</span>
        <div>
          ${url ? `<a class="res-card-link-btn" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();">官方直达 ↗</a>` : ""}
        </div>
      </div>
    </article>
  `;
}

function initResourcePortalListeners() {
  const catTabs = document.getElementById("resourceCategoryTabs");
  if (catTabs) {
    catTabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".res-tab");
      if (!btn) return;
      resState.cat = btn.dataset.cat;
      catTabs.querySelectorAll(".res-tab").forEach((b) => b.classList.toggle("is-active", b === btn));
      renderResourcePortal();
    });
  }

  const regChips = document.getElementById("resourceRegionChips");
  if (regChips) {
    regChips.addEventListener("click", (e) => {
      const btn = e.target.closest(".res-chip");
      if (!btn) return;
      resState.reg = btn.dataset.reg;
      regChips.querySelectorAll(".res-chip").forEach((b) => b.classList.toggle("is-active", b === btn));
      renderResourcePortal();
    });
  }

  const searchInput = document.getElementById("resourceSearchInput");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      resState.query = e.target.value;
      renderResourcePortal();
    });
  }
}

  
  const gridEl = document.getElementById("resourceCardsGrid");
  if (gridEl) {
    gridEl.addEventListener("click", (e) => {
      const link = e.target.closest("a");
      if (link) return; // allow direct link click
      const card = e.target.closest(".resource-card");
      if (card && card.dataset.resId) {
        openResourceModal(card.dataset.resId);
      }
    });
  }

  const closeBtn = document.getElementById("resModalCloseBtn");
  if (closeBtn) closeBtn.addEventListener("click", closeResourceModal);

  const copyBibBtn = document.getElementById("resCopyBibBtn");
  if (copyBibBtn) {
    copyBibBtn.addEventListener("click", () => {
      if (currentResItem && currentResItem.bib) {
        navigator.clipboard.writeText(currentResItem.bib).then(() => showToast("已复制 BibTeX 引用！"));
      }
    });
  }

  const modalEl = document.getElementById("resourceDetailModal");
  if (modalEl) {
    modalEl.addEventListener("click", (e) => {
      if (e.target === modalEl) closeResourceModal();
    });
  }

  window.addEventListener("hashchange",route);
  window.addEventListener("resize", () => {
    syncHeaderMetrics();
    syncAssistantLayout();
    document.body.classList.toggle("assistant-fullscreen-open", !document.getElementById("countryCopilot").hidden && window.innerWidth < 768);
  });

/* ==========================================================================
   AI 引用角标悬浮透视 (Citation Hover Inspector)
   ========================================================================== */
function initCitationHover() {
  const card = document.getElementById("citationHoverCard");
  if (!card) return;

  document.addEventListener("mouseover", (e) => {
    const marker = e.target.closest(".citation-marker");
    if (!marker) return;
    const title = marker.dataset.citeTitle || "证据出处";
    const source = marker.dataset.citeSource || "未知机构";
    const date = marker.dataset.citeDate || "";
    const origin = marker.dataset.citeOrigin || "库内已复核";
    const url = marker.dataset.citeUrl || "";

    card.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;">
        <strong style="font-size:12px;color:#0f172a;">${title}</strong>
        <span style="font-size:10px;padding:1px 5px;border-radius:4px;background:#e0f2fe;color:#0369a1;font-weight:700;">${origin}</span>
      </div>
      <small style="color:#64748b;">${source} ${date ? `· ${date}` : ""}</small>
      ${url ? `<a href="${url}" target="_blank" rel="noopener noreferrer" style="color:#2568e8;font-size:11px;text-decoration:none;margin-top:2px;">打开来源 ↗</a>` : ""}
    `;

    const rect = marker.getBoundingClientRect();
    card.style.top = `${Math.min(window.innerHeight - 120, Math.max(10, rect.bottom + 6))}px`;
    card.style.left = `${Math.min(window.innerWidth - 330, Math.max(10, rect.left - 40))}px`;
    card.hidden = false;
  });

  document.addEventListener("mouseout", (e) => {
    const marker = e.target.closest(".citation-marker");
    if (marker && !e.relatedTarget?.closest("#citationHoverCard")) {
      card.hidden = true;
    }
  });

  card.addEventListener("mouseleave", () => {
    card.hidden = true;
  });
}

/* ==========================================================================
   全局学术聚合检索 (Command Palette Modal · Cmd+K)
   ========================================================================== */
let cmdPaletteFilter = "all";
let cmdPaletteSelectedIndex = 0;
let cmdPaletteFilteredResults = [];

function openCommandPalette() {
  const modal = document.getElementById("commandPaletteModal");
  if (!modal) return;
  modal.showModal();
  const input = document.getElementById("cmdPaletteInput");
  if (input) {
    input.value = "";
    input.focus();
  }
  cmdPaletteFilter = "all";
  updateCommandPaletteTabs();
  renderCommandPaletteResults("");
}

function closeCommandPalette() {
  const modal = document.getElementById("commandPaletteModal");
  if (modal) modal.close();
}

function updateCommandPaletteTabs() {
  const tabs = document.getElementById("cmdPaletteTabs");
  if (!tabs) return;
  tabs.querySelectorAll(".cmd-tab").forEach((b) => b.classList.toggle("is-active", b.dataset.filter === cmdPaletteFilter));
}

function buildCommandPaletteIndex() {
  const items = [];

  // 1. Countries
  (state.catalog || []).forEach((c) => {
    items.push({
      type: "country",
      icon: iconSvg("globe"),
      title: `${c.name_zh} (${c.iso3})`,
      desc: `${c.name_en || ""} · ${availabilityLabel(c.availability, hasTemplatePreview(c))} · ${membershipLabel(c.membership)}`,
      badge: "国别",
      badgeClass: "is-country",
      action: () => navigate(`/countries/${c.iso3}`),
      keywords: `${c.name_zh} ${c.name_en || ""} ${c.iso3} 国别 国家`,
    });
  });

  // 2. Events
  (state.eventRows || []).forEach((e) => {
    items.push({
      type: "event",
      icon: iconSvg("clock"),
      title: e.title,
      desc: `${e.start_at ? localDate(e.start_at) : "日期未明"} · ${Number(e.source_count || 0)} 个确认来源`,
      badge: "事件",
      badgeClass: "is-event",
      action: () => navigate(`/events/${e.id}`),
      keywords: `${e.title} 事件 event`,
    });
  });

  // 3. Topics
  (state.topics || []).forEach((t) => {
    items.push({
      type: "topic",
      icon: iconSvg("file-text"),
      title: t.title,
      desc: `${t.research_question || "研究项目"} · ${Number(t.document_count || 0)} 份材料`,
      badge: "项目",
      badgeClass: "is-topic",
      action: () => navigate(`/projects/${t.id}`),
      keywords: `${t.title} ${t.research_question || ""} 项目 project topic`,
    });
  });

  // 4. 研究 Skill
  (state.capabilities || []).forEach((s) => {
    items.push({
      type: "skill",
      icon: iconSvg("layers"),
      title: s.name,
      desc: s.description || "区域国别研究 Skill",
      badge: "Skill",
      badgeClass: "is-skill",
      action: () => navigate(`/capabilities`),
      keywords: `${s.name} ${s.description || ""} skill 能力`,
    });
  });

  // 5. Resources (CATALOG)
  if (typeof CATALOG !== "undefined") {
    CATALOG.forEach((r) => {
      items.push({
        type: "resource",
        icon: iconSvg("landmark"),
        title: r.name,
        desc: `${r.by || ""} · ${r.desc || ""}`,
        badge: "信源",
        badgeClass: "is-resource",
        action: () => {
          navigate("/resources");
          window.setTimeout(() => openResourceModal(r.id), 200);
        },
        keywords: `${r.name} ${r.by || ""} ${r.kicker || ""} ${r.desc || ""} 信源 资源 数据库`,
      });
    });
  }

  return items;
}

function renderCommandPaletteResults(query = "") {
  const container = document.getElementById("cmdPaletteResults");
  if (!container) return;
  const allItems = buildCommandPaletteIndex();
  const q = (query || "").trim().toLowerCase();

  cmdPaletteFilteredResults = allItems.filter((item) => {
    const matchType = cmdPaletteFilter === "all" || item.type === cmdPaletteFilter;
    const matchQ = !q || item.keywords.toLowerCase().includes(q) || item.title.toLowerCase().includes(q) || item.desc.toLowerCase().includes(q);
    return matchType && matchQ;
  }).slice(0, 30);

  cmdPaletteSelectedIndex = Math.min(cmdPaletteSelectedIndex, Math.max(0, cmdPaletteFilteredResults.length - 1));

  if (!cmdPaletteFilteredResults.length) {
    container.innerHTML = `<div style="padding:32px 16px;text-align:center;color:#64748b;font-size:12px;">未检索到匹配结果，请尝试其他关键词</div>`;
    return;
  }

  container.innerHTML = cmdPaletteFilteredResults.map((item, idx) => `
    <div class="cmd-result-item ${idx === cmdPaletteSelectedIndex ? "is-selected" : ""}" data-result-idx="${idx}">
      <div class="cmd-result-left">
        <span class="cmd-result-icon">${item.icon}</span>
        <div class="cmd-result-content">
          <span class="cmd-result-title">${escapeHtml(item.title)}</span>
          <span class="cmd-result-desc">${escapeHtml(item.desc)}</span>
        </div>
      </div>
      <span class="cmd-result-badge ${item.badgeClass}">${item.badge}</span>
    </div>
  `).join("");
}

function initCommandPalette() {
  const trigger = document.getElementById("globalSearchTrigger");
  if (trigger) trigger.addEventListener("click", openCommandPalette);

  const closeBtn = document.getElementById("cmdPaletteCloseBtn");
  if (closeBtn) closeBtn.addEventListener("click", closeCommandPalette);

  const modal = document.getElementById("commandPaletteModal");
  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeCommandPalette();
    });
  }

  const input = document.getElementById("cmdPaletteInput");
  if (input) {
    input.addEventListener("input", (e) => {
      cmdPaletteSelectedIndex = 0;
      renderCommandPaletteResults(e.target.value);
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (cmdPaletteFilteredResults.length) {
          cmdPaletteSelectedIndex = (cmdPaletteSelectedIndex + 1) % cmdPaletteFilteredResults.length;
          renderCommandPaletteResults(input.value);
        }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (cmdPaletteFilteredResults.length) {
          cmdPaletteSelectedIndex = (cmdPaletteSelectedIndex - 1 + cmdPaletteFilteredResults.length) % cmdPaletteFilteredResults.length;
          renderCommandPaletteResults(input.value);
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        const selected = cmdPaletteFilteredResults[cmdPaletteSelectedIndex];
        if (selected) {
          closeCommandPalette();
          selected.action();
        }
      } else if (e.key === "Escape") {
        closeCommandPalette();
      }
    });
  }

  const tabs = document.getElementById("cmdPaletteTabs");
  if (tabs) {
    tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".cmd-tab");
      if (!btn) return;
      cmdPaletteFilter = btn.dataset.filter || "all";
      updateCommandPaletteTabs();
      cmdPaletteSelectedIndex = 0;
      renderCommandPaletteResults(input ? input.value : "");
    });
  }

  const results = document.getElementById("cmdPaletteResults");
  if (results) {
    results.addEventListener("click", (e) => {
      const itemEl = e.target.closest(".cmd-result-item");
      if (!itemEl) return;
      const idx = Number(itemEl.dataset.resultIdx);
      const selected = cmdPaletteFilteredResults[idx];
      if (selected) {
        closeCommandPalette();
        selected.action();
      }
    });
  }

  // Global key shortcut Cmd+K / Ctrl+K
  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      const modal = document.getElementById("commandPaletteModal");
      if (modal?.open) closeCommandPalette();
      else openCommandPalette();
    }
  });
}

async function init() {
  restoreAssistantState();
  initSidebarToggle();
  initCopilotResizer();
  initResourcePortalListeners();
  initCitationHover();
  initCommandPalette();
  syncHeaderMetrics();
  applyAssistantWidth(state.assistantWidth, false);
  syncAssistantLayout();
  try { await window.ReaderAuth.init(); } catch(error) { showToast(error.message); }
  try { await loadBootstrap(); } catch (error) { renderBootstrapFailure(error); }
  try { await loadCatalog(); } catch (error) { document.getElementById("countryCatalogList").innerHTML = errorState(error.message); }
  await route();
}
init();

document.getElementById("countryReadingDialog").addEventListener("cancel", event => { event.preventDefault(); closeCountryReading(); });

document.getElementById("countryReadingDialog").addEventListener("click", event => { if (event.target.closest('a[href^="#/"]')) closeCountryReading(); });

window.addEventListener("hashchange", () => { const material = document.getElementById("materialDrawer"); if (material.open) material.close(); });
