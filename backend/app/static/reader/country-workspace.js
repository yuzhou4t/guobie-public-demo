/* Country-only reading and explicitly selected, versioned context. */
function positionCountryReading() {
  const dialog=document.getElementById("countryReadingDialog"), workspace=document.getElementById("countryWorkspace");
  if(!dialog?.open || !workspace) return;
  const bounds=document.getElementById("countryToolbar").getBoundingClientRect();
  const top=Math.max(0,document.getElementById("countryToolbar").getBoundingClientRect().bottom+12);
  const right=countryChatLayout() && !document.getElementById("countryCopilot").hidden && innerWidth>=1100 ? document.getElementById("countryCopilot").getBoundingClientRect().left : Math.min(bounds.right,innerWidth);
  dialog.style.left=`${Math.max(0,bounds.left)}px`;
  dialog.style.top=`${top}px`;
  dialog.style.width=`${Math.max(280,right-Math.max(0,bounds.left)-12)}px`;
  dialog.style.height=`${Math.max(200,innerHeight-top-16)}px`;
}
function countryReferenceKey(ref) { return JSON.stringify([ref.type,ref.id,ref.snapshot_id,ref.field,ref.start,ref.quote]); }
function addCountryReference(ref, {focus=true}={}) {
  if(!countryChatLayout()) return;
  const session=currentAssistantSession(), refs=session.references ||= [];
  if(refs.some(item=>countryReferenceKey(item)===countryReferenceKey(ref))) { if(focus) focusCopilot(); return; }
  if(refs.length>=8 || (ref.quote || "").length>1500 || refs.reduce((n,item)=>n+Array.from(item.quote || "").length,0)+Array.from(ref.quote || "").length>6000) { showToast("最多 8 条引用；单条选文 1500 字，合计 6000 字。"); return; }
  refs.push(ref); persistAssistantSessions(); renderCountryReferences(); if(focus) focusCopilot();
}
function renderCountryReferences() {
  const refs=currentAssistantSession().references || [], root=document.getElementById("copilotSelectedContext");
  root.hidden=!refs.length;
  document.getElementById("selectedContextCount").textContent=refs.length;
  document.getElementById("selectedContextChips").innerHTML=refs.map((ref,index)=>`<article class="country-reference"><button class="reference-origin" data-reference-open="${index}">${escapeHtml(ref.title || "来源引用")}</button><small>${escapeHtml(ref.source_name || "已登记来源")} · ${ref.type==="document_version" ? "材料版本":"记录"} #${ref.id}</small>${ref.quote ? `<blockquote>${escapeHtml(ref.quote)}</blockquote>`:""}<button class="reference-remove" data-reference-remove="${index}" aria-label="移除此引用">×</button></article>`).join("");
  syncCountrySelection();
}

function countrySelectionHtml(ref) {
  return `<label class="country-select-material"><input type="checkbox" data-country-select-reference="${escapeHtml(JSON.stringify(ref))}" aria-label="选择资料：${escapeHtml(ref.title)}"><span>选择资料</span></label>`;
}

function syncCountrySelection() {
  const active=countryChatLayout() && state.countrySelectingScope===state.assistantScopeId;
  document.body.classList.toggle("country-selecting",active);
  const button=document.getElementById("countrySelectMaterialsBtn"), done=document.getElementById("countrySelectionBar");
  if(button) { button.setAttribute("aria-pressed",String(active)); button.textContent=active ? "完成选择":"＋ 选择资料"; }
  if(done) done.hidden=!active;
  const refs=currentAssistantSession().references || [];
  document.querySelectorAll("[data-country-select-reference]").forEach(input=>{
    const key=countryReferenceKey(JSON.parse(input.dataset.countrySelectReference));
    input.checked=refs.some(ref=>countryReferenceKey(ref)===key);
  });
}

function toggleCountrySelection() {
  const active=state.countrySelectingScope===state.assistantScopeId;
  state.countrySelectingScope=active ? null:state.assistantScopeId;
  if(!active) document.body.classList.remove("country-report-comparing");
  syncCountrySelection();
  if(active) focusCopilot();
  else if(innerWidth<1100) closeCopilot(false,false);
}

function countryArticleHtml(item,pane,actions="",theme="") {
  const link=pane==="research" ? `data-country-read-material="${item.document_version_id}"` : `data-publication-open href="#/countries/${state.country.iso3}/${pane}/${item.document_version_id}"`;
  const tag=pane==="research" ? "button":"a";
  return `<article class="country-article-row" ${projectMaterialAttributes(item)}><div class="article-date"><time>${escapeHtml(readingDate(item.published_at,item.published_at_precision))}</time>${theme ? `<span>${escapeHtml(theme)}</span>`:""}</div><h3><${tag} ${link}>${escapeHtml(item.title)}</${tag}></h3>${item.metadata?.authors?.length ? `<p class="article-authors">${escapeHtml(item.metadata.authors.join(" · "))}</p>`:""}${item.abstract ? `<p class="article-summary">${escapeHtml(item.abstract)}</p>`:""}<footer><span>${escapeHtml(item.source_name)} · v${item.version_no}</span>${actions}<${tag} class="text-link" ${link}>阅读${pane==="research" ? "论文":pane==="reports" ? "报告":"政策"} →</${tag}></footer></article>`;
}

function restoreCountryDataReading() {
  const home=document.querySelector("#country-data .data-indicator-detail");
  if(!home) return;
  for(const id of ["countryDataComparison","countryDataRecords"]) {
    const node=document.getElementById(id);
    if(node && node.parentElement!==home) home.append(node);
    if(node) node.hidden=true;
  }
}

function openCountryDataIndicator({historyEntry=true}={}) {
  if(!selectedCountryDataIndicator()) return;
  if(historyEntry && !history.state?.countryDataReading) history.pushState({countryDataReading:state.selectedDataIndicatorKey,countryIso3:state.country.iso3},"",location.href);
  openCountryReading("指标详情");
  renderCountryDataIndicator();
  const root=document.getElementById("countryEventChainDetail");
  root.replaceChildren();
  for(const id of ["countryDataComparison","countryDataRecords"]) {
    const node=document.getElementById(id); node.hidden=false; root.append(node);
  }
  const article=document.querySelector("#countryDataComparison > article"), definition=document.createElement("div"), comparisons=document.createElement("div");
  definition.id="countryIndicatorDefinition"; comparisons.id="countryIndicatorComparisons";
  [...article.children].filter(node=>node.matches("dl,svg,.reading-note") && node!==article.lastElementChild).forEach(node=>definition.append(node));
  [...article.querySelectorAll(".reading-table-wrap")].forEach(node=>comparisons.append(node));
  if(!comparisons.children.length) comparisons.innerHTML='<p class="reading-note">当前未登记可并列对照的其他来源。不会自动合并、换算或推断差异原因。</p>';
  article.querySelector("p").insertAdjacentHTML("afterend",'<nav class="reading-tabs country-data-tabs" aria-label="指标详情内容"><button data-country-data-tab="definition" aria-pressed="true">定义与来源</button><button data-country-data-tab="records" aria-pressed="false">观测记录</button><button data-country-data-tab="compare" aria-pressed="false">来源对照</button></nav>');
  const nav=article.querySelector(".country-data-tabs"); nav.after(definition,comparisons,document.getElementById("countryDataRecords"));
  article.querySelector("[data-show-data-records]")?.remove();
  comparisons.hidden=true; document.getElementById("countryDataRecords").hidden=true;
}

async function showCountryDataTab(tab) {
  document.getElementById("countryIndicatorDefinition").hidden=tab!=="definition";
  document.getElementById("countryIndicatorComparisons").hidden=tab!=="compare";
  document.getElementById("countryDataRecords").hidden=tab!=="records";
  document.querySelectorAll("[data-country-data-tab]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.countryDataTab===tab)));
  if(tab==="records") await loadCountryDataRecords();
}
function quoteAttributes(ref, field) {
  return `data-quote-type="${ref.type}" data-quote-id="${ref.id}" data-quote-field="${field}" data-quote-title="${escapeHtml(ref.title)}" data-quote-source="${escapeHtml(ref.source_name || "已登记来源")}"`;
}
async function openCountryMaterial(id, pane) {
  const request=openCountryReading(pane==="policies" ? "政策阅读":"资料阅读"), root=document.getElementById("countryEventChainDetail");
  root.innerHTML=detailSkeleton("正在读取材料版本…");
  try {
    const item=await apiFetch(`/reader/materials/${id}`);
    if(request!==state.countryReadingRequest) return;
    if(!item.countries?.some(country=>country.iso3===state.country.iso3)) throw new Error("材料不属于当前国家的确认资料。");
    const ref={type:"document_version",id,title:item.title,source_name:item.source_name};
    state.countryReadingMaterial=item;
    requestAnimationFrame(positionCountryReading);
    root.innerHTML=`<article class="country-material-reading"><header><small>${escapeHtml(item.source_name)} · ${escapeHtml(publishedLabel(item.published_at,item.published_at_precision))} · v${item.version_no}</small><h2 ${quoteAttributes(ref,"title")}>${escapeHtml(item.title)}</h2><button class="ghost-btn" data-country-add-material="${id}">加入 AI 上下文</button></header>${item.abstract ? `<h3>已有摘要</h3><p ${quoteAttributes(ref,"abstract")}>${escapeHtml(item.abstract)}</p>`:""}<h3>原始来源</h3>${sourceActionHtml(item)}${formatLocator(item.evidence_locators?.length ? item.evidence_locators : item.evidence_locator)!=="未记录" ? `<p>${escapeHtml(formatLocator(item.evidence_locators?.length ? item.evidence_locators : item.evidence_locator))}</p>`:""}<h3>引用信息</h3><p>${escapeHtml(generateCitationData(item).gbt || "待登记")}</p>${item.events?.length ? `<h3>关联事件</h3>${item.events.map(event=>`<p><a href="#/countries/${state.country.iso3}/events/${event.id}">${escapeHtml(event.title)}</a></p>`).join("")}`:""}</article>`;
    root.querySelector("[data-country-add-material]").textContent="引用此资料";
    const metadata=item.metadata || {};
    const fields=[["研究对象",metadata.research_object,"research_object"],["方法",metadata.research_method || metadata.method,"research_method"],["样本范围",metadata.sample_scope,"sample_scope"],["数据来源",metadata.data_sources,"data_sources"]];
    if(fields.some(([,value])=>value)) root.querySelector("article").insertAdjacentHTML("beforeend",`<h3>已有研究元数据</h3><dl class="country-paper-metadata">${fields.filter(([,value])=>value).map(([label,value,key])=>{const proof=metadata.field_provenance?.[key]; return `<dt>${label}</dt><dd>${escapeHtml(value)}${safeUrl(proof?.source_url) ? ` <a href="${escapeHtml(proof.source_url)}" target="_blank" rel="noopener noreferrer">核验依据 ↗</a>`:""}</dd>`;}).join("")}</dl>`);
  } catch(error) { if(request===state.countryReadingRequest) root.innerHTML=errorState(error.message); }
}
async function loadCountryCollectionStatus(iso3) {
  const targets={event_dynamics:"recent-signals",policy_center:"minerals-policy",frontier_research:"country-research",think_tank_report:"country-reports"};
  try {
    const payload=await apiFetch(`/reader/countries/${iso3}/collection-status`);
    if(state.country?.iso3!==iso3) return;
    for(const [key,item] of Object.entries(payload.channels)) {
      const section=document.getElementById(targets[key]);
      if(!section) continue;
      let root=section.querySelector(".country-collection-status");
      if(!root) { root=document.createElement("details");root.className="country-collection-status";section.querySelector(".section-heading")?.after(root); }
      if(key==="frontier_research") (document.getElementById("frontierCollectionStatus") || document.getElementById("countryResearchGaps")).append(root);
      const time=value=>value ? new Date(value).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "尚无记录";
      root.innerHTML=`<summary>${item.update_mode.startsWith("按需补录") ? (key==="policy_center" ? "政策原文按需补录" : "按需补录 · 未接入持续更新") : "更新情况"} · 本周首次入库 ${Number(item.new_this_week)} 条 · ${item.update_mode.startsWith("按需补录") ? "最近补录" : "最近检查"} ${escapeHtml(time(item.update_mode.startsWith("按需补录") ? item.last_new_at : item.last_checked_at))}${item.failures.length ? ` · ${item.failures.length} 项异常`:""}</summary><p>${escapeHtml(item.update_mode)} · 最近成功 ${escapeHtml(time(item.last_success_at))} · 最近新增 ${escapeHtml(time(item.last_new_at))}</p><p>已收录发表时间：${escapeHtml(item.published_from || "未登记")}—${escapeHtml(item.published_to || "未登记")}。${escapeHtml(payload.new_count_note)}</p>${item.failures.length ? `<ul>${item.failures.map(f=>`<li>${escapeHtml(f.source)}：${escapeHtml(f.code || "未完成")}</li>`).join("")}</ul>`:""}`;
    }
  } catch(error) { if(state.country?.iso3===iso3) console.warn("国别更新状态暂不可用",error.message); }
}
async function loadResearchOverview() {
  const iso3=state.country?.iso3, years=Number(state.researchWindowYears || 3), key=`${iso3}:${years}`;
  state.researchOverviewRequest=key;
  try {
    const data=await apiFetch(`/reader/countries/${iso3}/research-trends?years=${years}`);
    if(state.researchOverviewRequest!==key || state.country?.iso3!==iso3) return;
    state.researchOverview=data; renderResearchOverview();
  } catch(error) { if(state.researchOverviewRequest===key) document.getElementById("countryResearchEvolution").innerHTML=errorState(error.message); }
}
function researchChartScale(maximum) {
  if(maximum<=4) return {max:4,step:1};
  const raw=maximum/4, power=10**Math.floor(Math.log10(raw));
  const step=[1,2,5,10].map(n=>n*power).find(n=>n>=raw);
  return {max:Math.ceil(maximum/step)*step,step};
}
function researchAnnualChart(data,width) {
  const rows=data.annual_counts, scale=researchChartScale(Math.max(0,...rows.map(r=>r.count)));
  const left=44,right=width-24,top=28,bottom=154,slot=(right-left)/Math.max(rows.length,1);
  const y=value=>bottom-value/scale.max*(bottom-top);
  const ticks=Array.from({length:scale.max/scale.step+1},(_,i)=>i*scale.step);
  return `<section class="research-annual-chart"><h3>年度收录量</h3><p class="research-chart-subtitle">单位：篇 · 柱形高度表示本站当年收录量</p><svg viewBox="0 0 ${width} 209" aria-label="年度收录量柱状图" role="group">${ticks.map(v=>`<line class="research-gridline" x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}"/><text class="research-axis-label" x="${left-10}" y="${y(v)+5}" text-anchor="end">${v}</text>`).join("")}<line class="research-chart-axis" x1="${left}" x2="${right}" y1="${bottom}" y2="${bottom}"/>${rows.map((r,i)=>{const x=left+slot*(i+.5),bar=Math.min(64,slot*.5); return `<g role="button" tabindex="0" data-research-chart-point data-research-year="${r.year}" aria-label="查看 ${r.year} 年的 ${r.count} 篇论文${r.partial ? '，非完整年度':''}"><title>${r.year}：${r.count} 篇${r.partial ? '（非完整年度）':''}</title><rect class="research-bar-hit" x="${left+slot*i}" y="12" width="${slot}" height="194"/><rect class="research-bar${r.partial ? ' is-partial':''}" x="${x-bar/2}" y="${y(r.count)}" width="${bar}" height="${bottom-y(r.count)}"/><text class="research-bar-value" x="${x}" y="${y(r.count)-8}" text-anchor="middle">${r.count}</text><text class="research-axis-label" x="${x}" y="180" text-anchor="middle">${r.year}</text>${r.partial ? `<text class="research-partial-label" x="${x}" y="200" text-anchor="middle">部分年度</text>`:''}</g>`;}).join("")}</svg></section>`;
}
function renderResearchOverview() { renderResearchFrontier(); }

function researchCoverageHtml(data) {
  const collection=data.collection;
  if(!collection) return "";
  const labels={pending:"待回补",running:"回补中",partial:"回补未完成",failed:"本次检查失败",succeeded:"本轮检索已查完"};
  const rows=collection.partitions.filter(p=>p.partition!=="updates" && Number(p.partition)>=Number(data.from.slice(0,4)));
  return `<details class="research-collection-coverage"><summary>查看逐年采集覆盖与缺口</summary><p>${escapeHtml(collection.scope_note)}</p><div class="research-matrix-wrap"><table class="research-matrix"><thead><tr><th>年份</th><th>检查进度</th><th>已检查候选记录</th><th>本轮范围</th></tr></thead><tbody>${rows.map(p=>`<tr><th>${escapeHtml(p.partition)}</th><td>${escapeHtml(labels[p.status] || p.status)}</td><td>${Number(p.records_checked)} / ${p.provider_results==null ? "待查询":Number(p.provider_results)}</td><td>${escapeHtml(p.from || "待开始")}—${escapeHtml(p.to || "待开始")}</td></tr>`).join("")}</tbody></table></div><p>候选记录包含被国别、日期或元数据质量规则排除的论文；检索查完不代表全球文献完整覆盖。</p></details>`;
}

function initCountryWorkspace() {
  const form=document.getElementById("assistantForm"), picker=document.getElementById("assistantCapabilityPicker");
  const tools=document.createElement("div"); tools.className="country-compose-tools";
  tools.innerHTML='<button type="button" id="countrySelectMaterialsBtn" aria-pressed="false">＋ 选择资料</button>';
  picker.before(tools); tools.append(picker);
  form.prepend(document.getElementById("copilotSelectedContext"));
  const menu=document.createElement("div"); menu.className="country-skill-menu";
  [...picker.children].filter(node=>node.tagName!=="SUMMARY").forEach(node=>menu.append(node)); picker.append(menu);
  const head=document.querySelector("#countryCopilot > header"), title=document.createElement("strong");
  title.className="country-chat-title"; title.textContent="国别助手";
  head.prepend(title); head.prepend(document.getElementById("copilotHistoryBtn"));
  document.getElementById("copilotHistoryBtn").textContent="☷ 对话";
  document.getElementById("copilotClearBtn").textContent="＋ 新建";
  document.getElementById("copilotCloseBtn").textContent="×";
  document.getElementById("assistantHistoryPanel").querySelector("header").insertAdjacentHTML("beforeend",'<button type="button" class="text-link" data-close-country-history>返回对话</button>');
  const selectionBar=document.createElement("div"); selectionBar.id="countrySelectionBar"; selectionBar.hidden=true;
  selectionBar.innerHTML='<span>勾选要交给 AI 参考的资料；也可在详情中选文提问。</span><button type="button" data-finish-country-selection>完成选择</button>';
  document.getElementById("countryAnchorNav").after(selectionBar);
  if(!document.getElementById("countryDataSourceFilter")) {
    const sourceFilter=document.createElement("select"); sourceFilter.id="countryDataSourceFilter"; sourceFilter.setAttribute("aria-label","按数据来源筛选"); sourceFilter.innerHTML='<option value="">全部来源</option>';
    document.getElementById("countryDataCatalogSearch").parentElement.after(sourceFilter);
  }
  restoreCountryDataReading();
  document.querySelector("#country-data .section-heading h2").textContent="数据目录";
  const tabs=document.getElementById("countryFeedTabs"), filters=document.getElementById("countryLatestFilters");
  if(tabs && filters && !tabs.closest(".country-feed-toolbar")) {
    const toolbar=document.createElement("div"); toolbar.className="country-feed-toolbar"; tabs.before(toolbar); toolbar.append(tabs,filters);
    const advanced=document.querySelector(".country-event-filters"), details=document.createElement("details"); details.id="countryEventAdvanced"; details.innerHTML='<summary>事件筛选</summary>'; details.append(advanced); toolbar.append(details);
  }
  const institutionSection=document.getElementById("countryInstitutionSection");
  const reportsList=document.getElementById("countryReportsList");
  if(institutionSection && reportsList && institutionSection.previousElementSibling!==reportsList) reportsList.after(institutionSection);
  const question=document.getElementById("assistantQuestion");
  const resizeComposer=()=>{ if(!question) return; question.style.height="auto"; question.style.height=`${Math.min(140, Math.max(48, question.scrollHeight))}px`; };
  question?.addEventListener("input", resizeComposer);
  resizeComposer();
  const quoteButton=document.createElement("button"); quoteButton.id="countryQuoteButton"; quoteButton.textContent="引用并提问"; quoteButton.hidden=true; document.body.append(quoteButton);
  let selected=null;
  document.addEventListener("selectionchange",()=>{
    const selection=getSelection(); selected=null; quoteButton.hidden=true;
    if(!countryChatLayout() || !selection?.rangeCount || selection.isCollapsed) return;
    const range=selection.getRangeAt(0), node=range.commonAncestorContainer, element=(node.nodeType===1 ? node:node.parentElement)?.closest("[data-quote-type]");
    if(!element || !element.contains(range.startContainer) || !element.contains(range.endContainer)) return;
    const prefix=range.cloneRange(); prefix.selectNodeContents(element); prefix.setEnd(range.startContainer,range.startOffset);
    const d=element.dataset; selected={type:d.quoteType,id:Number(d.quoteId),field:d.quoteField,title:d.quoteTitle,source_name:d.quoteSource,quote:range.toString(),start:Array.from(prefix.toString()).length};
    const rect=range.getBoundingClientRect(); quoteButton.style.left=`${Math.max(8,Math.min(innerWidth-140,rect.left))}px`; quoteButton.style.top=`${Math.max(8,rect.top-40)}px`; quoteButton.hidden=false;
  });
  quoteButton.addEventListener("pointerdown",event=>event.preventDefault());
  quoteButton.addEventListener("click",()=>{ if(selected) addCountryReference(selected); quoteButton.hidden=true; });
  document.addEventListener("keydown",event=>{ if(event.key==="Escape" && document.getElementById("countryReadingDialog").open) { event.preventDefault(); closeCountryReading(); } });
  const chartRoot=document.getElementById("countryResearchEvolution");
  const describePoint=event=>{const point=event.target.closest("[data-chart-description]"); if(point && document.getElementById("researchChartReadout")) document.getElementById("researchChartReadout").textContent=point.dataset.chartDescription+" · 点击查看论文";};
  chartRoot.addEventListener("change",event=>{ if(event.target.id==="frontierMethodYear") { countryReadingState().frontierMethodYear=event.target.value; renderResearchOverview(); document.getElementById("frontierMethodYear")?.focus({preventScroll:true}); } });
  chartRoot.addEventListener("pointerover",describePoint);
  chartRoot.addEventListener("focusin",describePoint);
  chartRoot.addEventListener("keydown",event=>{const point=event.target.closest("[data-research-chart-point]"); if(point && ["Enter"," "].includes(event.key)) { event.preventDefault(); point.dispatchEvent(new MouseEvent("click",{bubbles:true})); } });
  let chartWidth=0;
  new ResizeObserver(entries=>{const width=Math.round(entries[0].contentRect.width); if(width>0 && Math.abs(width-chartWidth)>10) { chartWidth=width; if(state.researchOverview) renderResearchOverview(); } }).observe(chartRoot);
  document.addEventListener("click",async event=>{
    if(!event.target.closest("#assistantCapabilityPicker")) picker.open=false;
    const target=event.target.closest("button,[data-research-chart-point]"); if(!target) return;
    if(handleResearchFrontierClick(target)) return;
    if(target.id==="countrySelectMaterialsBtn" || target.matches("[data-finish-country-selection]")) return toggleCountrySelection();
    if(target.matches("[data-close-country-history]")) return closeAssistantHistory();
    if(target.matches("[data-report-view]")) { countryReadingState().reportView=target.dataset.reportView; countryReadingState().reportInstitutionId=null; renderCountryReportChannel(); return; }
    if(target.matches("[data-institution-open]")) { countryReadingState().reportInstitutionId=target.dataset.institutionOpen; renderCountryReportChannel(); return; }
    if(target.matches("[data-institution-back]")) { countryReadingState().reportInstitutionId=null; renderCountryReportChannel(); return; }
    if(target.matches("[data-institution-reports]")) { const view=countryReadingState(); view.reportView="reports"; view.source=target.dataset.institutionReports; await loadPublicationChannel(state.country.iso3,"think_tank_report"); return; }
    if(target.id==="countryReportCompareMode") { const active=document.body.classList.toggle("country-report-comparing"); target.setAttribute("aria-pressed",String(active)); if(active) { state.countrySelectingScope=null; syncCountrySelection(); } return; }
    if(target.matches("[data-country-data-tab]")) return showCountryDataTab(target.dataset.countryDataTab);
    if(target.matches("[data-country-read-material]")) return openCountryMaterial(Number(target.dataset.countryReadMaterial));
    if(target.matches("[data-country-add-material]")) { const item=state.countryReadingMaterial; addCountryReference({type:"document_version",id:item.document_version_id,title:item.title,source_name:item.source_name}); }
    if(target.matches("[data-country-add-object]")) addCountryReference(JSON.parse(target.dataset.countryAddObject));
    if(target.matches("[data-reference-remove]")) { currentAssistantSession().references.splice(Number(target.dataset.referenceRemove),1); persistAssistantSessions(); renderCountryReferences(); }
    if(target.matches("[data-reference-open]")) { const ref=currentAssistantSession().references[Number(target.dataset.referenceOpen)]; if(innerWidth<1100) closeCopilot(false,false); if(ref.type==="document_version") openCountryMaterial(ref.id); else if(ref.type==="event") { state.selectedCountryEventId=ref.id; renderCountryEventChainDetail(ref.id); } else {
      const dataset=state.countryDataCatalog?.datasets.find(item=>item.snapshot_id===ref.snapshot_id);
      const indicator=dataset?.indicators.find(item=>item.records.some(row=>row.observation_version_id===ref.id));
      if(indicator) { state.selectedDataIndicatorKey=`${dataset.id}:${indicator.key}`; openCountryDataIndicator(); }
      else showToast("此引用属于先前快照，当前目录没有对应记录；发送时仍会验证原快照。");
    } }
    if(target.id==="clearSelectedContextBtn") { currentAssistantSession().references=[]; persistAssistantSessions(); renderCountryReferences(); }
    if(target.matches("[data-research-topic],[data-research-year],[data-research-reset]")) {
      const view=countryReadingState();
      if(target.hasAttribute("data-research-reset")) { view.researchTopic=null; view.researchYear=null; }
      else { view.researchTopic=target.dataset.researchTopic || null; view.researchYear=target.dataset.researchYear ? Number(target.dataset.researchYear):null; }
      await loadPublicationChannel(state.country.iso3,"frontier_research");
      document.getElementById("countryResearchEvolution").scrollIntoView({block:"start"});
    }
  });
  document.addEventListener("change",event=>{
    if(event.target.id==="countryReportSourceFilter") { countryReadingState().source=event.target.value; loadPublicationChannel(state.country.iso3,"think_tank_report"); return; }
    if(event.target.id==="countryDataIncludeMissing") { countryReadingState().dataIncludeMissing=event.target.checked; renderCountryDataCatalog(); return; }
    const input=event.target.closest("[data-country-select-reference]"); if(!input) return;
    const ref=JSON.parse(input.dataset.countrySelectReference);
    if(input.checked) addCountryReference(ref,{focus:false});
    else { const session=currentAssistantSession(); session.references=(session.references || []).filter(item=>countryReferenceKey(item)!==countryReferenceKey(ref)); persistAssistantSessions(); renderCountryReferences(); }
    syncCountrySelection();
  });
  const enhance=()=>{
    document.getElementById("countryEventAdvanced").hidden=countryReadingStateSafe()?.layer!=="chains";
    document.querySelectorAll('#countryWorkspace article[data-project-material-version], #countryWorkspace article[data-project-event-id]').forEach(article=>{
      if(article.dataset.countrySelectable) return;
      article.dataset.countrySelectable="true";
      const material=article.dataset.projectMaterialVersion, id=Number(material || article.dataset.projectEventId), title=article.dataset.projectMaterialTitle || article.dataset.projectEventTitle;
      const ref={type:material ? "document_version":"event",id,title,source_name:article.dataset.projectMaterialSource || "已复核事件"};
      article.insertAdjacentHTML("afterbegin",countrySelectionHtml(ref));
      const heading=article.querySelector("h3"); if(heading && heading.textContent===title) { const el=heading.querySelector("button,a") || heading; el.dataset.quoteType=ref.type; el.dataset.quoteId=id; el.dataset.quoteField="title"; el.dataset.quoteTitle=title; el.dataset.quoteSource=ref.source_name; }
    });
    syncCountrySelection();
  };
  new MutationObserver(enhance).observe(document.getElementById("countryWorkspace"),{childList:true,subtree:true});
  window.addEventListener("resize",positionCountryReading);
  window.addEventListener("popstate",()=>{
    if(history.state?.countryDataReading && history.state.countryIso3===state.country?.iso3) { state.selectedDataIndicatorKey=history.state.countryDataReading; openCountryDataIndicator({historyEntry:false}); }
    else if(document.getElementById("countryIndicatorDefinition")) closeCountryReading(false);
  });
  new ResizeObserver(positionCountryReading).observe(document.getElementById("countryToolbar"));
}
function countryReadingStateSafe() { return state.country ? countryReadingState():null; }
document.addEventListener("DOMContentLoaded",initCountryWorkspace);

function countrySentReferencesHtml(refs) { return refs.map(ref=>`<blockquote class="sent-reference">${escapeHtml(ref.quote || ref.title || "来源引用")}<small>${escapeHtml(ref.source_name || "")} · #${ref.id}</small></blockquote>`).join(""); }
