/* Evidence-linked frontier views. Uses the Reader's existing material dialog. */
function frontierButton(label, attrs, extra='') {
  return `<button type="button" ${attrs} ${extra}>${escapeHtml(label)}</button>`;
}
function frontierPapers(ids, analysis, reason='') {
  return [...new Set(ids)].map(id=>analysis.papers[id]).filter(Boolean).map(p=>`<article class="frontier-paper"><div><small>${escapeHtml(p.date)} · ${escapeHtml(p.source || '')}</small><h4>${frontierButton(p.title,`data-country-read-material="${p.id}"`)}</h4>${reason ? `<p>${escapeHtml(reason)}</p>`:''}</div>${frontierButton('阅读 ↗',`data-country-read-material="${p.id}"`,'class="frontier-read"')}</article>`).join('');
}
function frontierIssues(analysis, view) {
  return analysis.issues.filter(i=>!view.frontierGroup || i.group===view.frontierGroup);
}
function frontierGroups(analysis,view) {
  return `<div class="frontier-groups" role="group" aria-label="议题领域">${['',...new Set(analysis.issues.map(i=>i.group))].map(group=>frontierButton(group || '全部领域',`data-frontier-group="${escapeHtml(group)}" aria-pressed="${(view.frontierGroup || '')===group}"`)).join('')}</div>`;
}
function frontierTimeline(data,view) {
  const a=data.frontier, issues=frontierIssues(a,view), years=data.annual_counts.map(r=>r.year);
  const width=Math.max(650,Math.min(940,document.getElementById('countryResearchEvolution').clientWidth || 760));
  const left=180,right=width-22,top=55,row=43,height=top+issues.length*row+16;
  const plotDate=p=>p.precision==='year'?`${p.year}-01-01`:p.precision==='month'?p.date.slice(0,7)+'-01':p.date;
  const beginning=Date.parse(data.from),ending=Date.parse(data.to),x=date=>left+(Date.parse(date)-beginning)/(ending-beginning || 1)*(right-left);
  return `<div class="frontier-chart-scroll" tabindex="0" aria-label="议题时间线，可横向滚动"><svg class="frontier-timeline" viewBox="0 0 ${width} ${height}" style="min-width:${width}px" role="group" aria-label="议题时间线：研究发表时间与已收录跨度">${years.map(year=>`<line x1="${x(year+'-01-01')}" x2="${x(year+'-01-01')}" y1="35" y2="${height-10}" class="frontier-grid"/><text x="${x(year+'-01-01')}" y="22" text-anchor="middle">${year}${year===Number(data.to.slice(0,4))?' *':''}</text>`).join('')}${issues.map((issue,j)=>{const y=top+j*row,dated=issue.papers.map(id=>a.papers[id]).sort((p,q)=>p.date.localeCompare(q.date));return `<g><text x="0" y="${y+6}" class="frontier-row-label">${escapeHtml(issue.label)}</text>${dated.length>1?`<line x1="${x(plotDate(dated[0]))}" x2="${x(plotDate(dated.at(-1)))}" y1="${y}" y2="${y}" class="frontier-span"/>`:''}${dated.map(p=>`<g role="button" tabindex="0" data-research-chart-point data-frontier-paper="${p.id}" aria-label="${escapeHtml(p.title)}，${escapeHtml(p.date)}，查看研究"><title>${escapeHtml(p.date)} · ${escapeHtml(p.title)}${p.precision==='year'?'（仅年份准确）':p.precision==='month'?'（仅月份准确）':''}</title><circle cx="${x(plotDate(p))}" cy="${y}" r="8" class="frontier-paper-hit"/><circle cx="${x(plotDate(p))}" cy="${y}" r="3.5" class="frontier-dot${p.precision==='year'?' is-year':''}"/></g>`).join('')}</g>`;}).join('')}</svg></div><p class="frontier-note">每个点对应一项研究；细线标记本站已收录的首末发表时间，不表示中间持续活跃。空心点仅年份准确，放在该年起点。* 当年截至 ${escapeHtml(data.to)}。点击点位查看论文。</p>`;
}
function frontierEvolution(data,view) {
  const a=data.frontier,issues=frontierIssues(a,view),issueIds=new Set(issues.map(i=>i.id));
  const years=data.annual_counts.map(r=>r.year),nodes=a.nodes.filter(n=>issueIds.has(n.issue));
  const width=Math.max(720,years.length*215),row=58,top=54,height=top+issues.length*row+10;
  const x=year=>18+years.indexOf(year)*(width-178)/(years.length-1 || 1),y=issue=>top+issues.findIndex(i=>i.id===issue)*row;
  const positions=Object.fromEntries(nodes.map(n=>[n.id,{x:x(n.year),y:y(n.issue),node:n}]));
  const slots={};
  const edges=a.edges.filter(e=>positions[e.source] && positions[e.target]).map(e=>{
    const s=positions[e.source],t=positions[e.target],start=s.x+152,end=t.x,mid=(start+end)/2;
    const cy=(s.y+t.y)/2+18,key=`${mid}:${cy}`,ordinal=slots[key] || 0;slots[key]=ordinal+1;
    const shift=ordinal ? Math.ceil(ordinal/2)*12*(ordinal%2?1:-1):0;
    return {...e,mid,cy:cy+shift,path:`M${start},${s.y+18} C${mid},${s.y+18+shift*4/3} ${mid},${t.y+18+shift*4/3} ${end},${t.y+18}`,
      label:`${s.node.year} ${s.node.label} → ${t.node.year} ${t.node.label}：${e.evidence.map(p=>p.clue).join('、')}，查看两端论文`,cross:s.node.issue!==t.node.issue};
  });
  return `<div class="frontier-chart-scroll" tabindex="0" aria-label="主题演化，可横向滚动"><svg class="frontier-evolution" viewBox="0 0 ${width} ${height}" style="min-width:${width}px" role="group" aria-label="主题演化：相邻年份共同议题线索">${years.map(year=>`<text x="${x(year)+76}" y="24" text-anchor="middle">${year}</text>`).join('')}${edges.map(e=>`<g data-research-chart-point data-frontier-edge="${escapeHtml(e.id)}"><title>${escapeHtml(e.label)}</title><path d="${e.path}" class="frontier-edge-hit"/><path d="${e.path}" class="frontier-edge${e.cross?' is-cross':''}"/></g>`).join('')}${edges.map(e=>`<g role="button" tabindex="0" data-research-chart-point data-frontier-edge="${escapeHtml(e.id)}" aria-label="${escapeHtml(e.label)}"><title>${escapeHtml(e.label)}</title><circle cx="${e.mid}" cy="${e.cy}" r="8" class="frontier-paper-hit"/><circle cx="${e.mid}" cy="${e.cy}" r="3.5" class="frontier-edge-center"/></g>`).join('')}${nodes.map(n=>`<g role="button" tabindex="0" data-research-chart-point data-frontier-node="${n.id}" aria-label="${escapeHtml(n.label)}，${n.year}，${n.papers.length} 篇"><rect x="${x(n.year)}" y="${y(n.issue)}" width="152" height="38" rx="7" class="frontier-node"/><text x="${x(n.year)+76}" y="${y(n.issue)+24}" text-anchor="middle" class="frontier-node-label">${escapeHtml(n.label)}</text></g>`).join('')}</svg></div><p class="frontier-note">等宽线连接相邻年份中含共同议题词的研究，虚线表示不同议题间的共同线索；不是篇数流量或引用传承。点击节点读论文，点击连线或线上圆点核对两端依据。</p>${!edges.length?'<p class="frontier-empty">所选范围尚无可连接的相邻年度证据；保留节点，不补画关联。</p>':''}`;
}
function frontierMethods(data,view) {
  const a=data.frontier,allIssues=frontierIssues(a,view),year=view.frontierMethodYear || '';
  const scopeIds=[...new Set(allIssues.flatMap(i=>i.papers))];
  const ids=scopeIds.filter(id=>!year || a.papers[id].year===Number(year));
  const methods=a.method_categories.filter(m=>scopeIds.some(id=>a.papers[id].methods[m.id]));
  const issues=allIssues.filter(i=>i.papers.some(id=>ids.includes(id) && Object.keys(a.papers[id].methods).length));
  const counts=allIssues.flatMap(i=>methods.map(m=>i.papers.filter(id=>a.papers[id].methods[m.id]).length));
  const max=Math.max(1,...counts);
  return `<div class="frontier-method-heading"><div><h3>哪些议题采用了哪些方法</h3><p class="frontier-note">只统计题名明确提到或已登记的方法；允许一篇论文归入多类。</p></div><label>发表年份<select id="frontierMethodYear">${['',...data.annual_counts.map(r=>r.year)].map(y=>`<option value="${y}" ${String(year)===String(y)?'selected':''}>${y || '全部年份'}</option>`).join('')}</select></label></div>${methods.length && issues.length ? `<div class="frontier-chart-scroll" tabindex="0" aria-label="研究方法热力图，可横向滚动"><table class="frontier-heatmap" style="min-width:${Math.max(350,170+methods.length*85)}px"><caption>颜色越深，具有该方法依据的论文越多 · 跨年固定色阶 · 单位：篇</caption><thead><tr><th scope="col">研究议题</th>${methods.map(m=>`<th scope="col">${escapeHtml(m.label)}</th>`).join('')}</tr></thead><tbody>${issues.map(i=>`<tr><th scope="row">${escapeHtml(i.label)}</th>${methods.map(m=>{const n=i.papers.filter(id=>ids.includes(id) && a.papers[id].methods[m.id]).length;return `<td>${n?frontierButton(n,`data-frontier-method="${i.id}:${m.id}" aria-label="${escapeHtml(i.label)}，${escapeHtml(m.label)}，${n} 篇" style="--heat:${.12+.78*n/max};--ink:${n/max>.5?'white':'#16445b'}"`):'<span aria-label="没有已识别记录">—</span>'}</td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`:'<p class="frontier-empty">该年份尚无可识别的方法依据，可切换年份查看已有记录。</p>'}<p class="frontier-note">当前领域与年度范围内，${ids.filter(id=>Object.keys(a.papers[id].methods).length).length} / ${ids.length} 篇已识别议题的论文具有方法依据。「—」表示没有已识别记录，不代表研究没有采用该方法。</p>`;
}
function frontierDrilldown(selection, data,view) {
  const a=data.frontier, ids=selection.ids.filter(id=>a.papers[id]),limit=view.frontierLimit || 12;
  return `<div class="frontier-results"><header>${frontierButton('← 返回趋势总览','data-frontier-back')}<h3>${escapeHtml(selection.label)}</h3><p>${ids.length} 篇关联研究</p></header>${selection.edges ? `<div class="frontier-evidence">${selection.edges.map(pair=>`<p><strong>共同线索：${escapeHtml(pair.clue)}</strong><br>${escapeHtml(a.papers[pair.source]?.date || '')} → ${escapeHtml(a.papers[pair.target]?.date || '')} · 依据两端题名或已登记研究对象</p>`).join('')}</div>`:''}${ids.slice(0,limit).map(id=>{const p=a.papers[id],method=selection.method && p.methods[selection.method];const proofs=selection.issue ? p.issue_evidence[selection.issue] || []:selection.edges ? Object.values(p.issue_evidence).flat().filter((e,i,all)=>all.findIndex(v=>v.field===e.field && v.text===e.text)===i):[];return frontierPapers([id],a)+((method || proofs.length)?`<details class="frontier-proof"><summary>查看识别依据</summary>${[...proofs,...(method?[method]:[])].map(e=>`<p><strong>${{title:'题名',research_object:'已登记研究对象',research_method:'已登记方法'}[e.field] || e.field}</strong>：${escapeHtml(e.text)}</p>`).join('')}</details>`:'');}).join('')}${ids.length>limit?frontierButton('继续显示关联研究','data-frontier-more'):''}</div>`;
}
function renderResearchFrontier() {
  const data=state.researchOverview,root=document.getElementById('countryResearchEvolution');
  if(!data || data.country_iso3!==state.country?.iso3 || data.years!==Number(state.researchWindowYears || 3)) { root.innerHTML=detailSkeleton('正在分析完整可见研究…');return; }
  const view=countryReadingState(), a=data.frontier, materials=document.getElementById('countryResearchMaterials'),toolbar=document.getElementById('countryResearchToolbar');
  const status=document.querySelector('#country-research .country-collection-status');
  // Keep this asynchronous status node through re-renders, including drilldowns.
  if(status) document.getElementById('countryResearchGaps').append(status);
  document.getElementById('countryResearchGaps').hidden=true;
  if(!a) { root.innerHTML='<p class="frontier-empty">趋势分析接口正在更新，请刷新后重试。</p>';return; }
  if(a.taxonomy) { renderCountryTaxonomy(data, view, status, materials, toolbar, root); return; }
  if(view.frontierSelection?.scope!==`${data.country_iso3}:${data.from}:${data.to}`) view.frontierSelection=null;
  const legacyDrill=Boolean(view.researchTopic || view.researchYear),drilled=Boolean(view.frontierSelection);
  materials.hidden=drilled || view.frontierTab==='methods';toolbar.hidden=materials.hidden;
  root.after(toolbar);
  if(drilled) {root.innerHTML=frontierDrilldown(view.frontierSelection,data,view);return;}
  if(legacyDrill) {materials.hidden=false;toolbar.hidden=false;root.innerHTML=`<div class="research-results-heading">${frontierButton('← 返回趋势总览','data-research-reset')}<h3>${escapeHtml(view.researchTopic || '全部议题')}${view.researchYear ? ` · ${view.researchYear}`:''}</h3></div>`;return;}
  const methods=view.frontierTab==='methods', evolution=view.frontierChart==='evolution';
  const observations=a.observations.length ? a.observations.map((o,i)=>`<article><span class="frontier-number">0${i+1}</span><div><h4>${escapeHtml(o.title)}</h4><p>${escapeHtml(o.text)}</p>${frontierButton('对照研究 ↗',`data-frontier-observation="${i}"`)}</div></article>`).join(''):'<p class="frontier-empty">当前可见证据尚不足以形成跨期对照，可从议题图查看已有研究。</p>';
  root.innerHTML=`<nav class="frontier-tabs" aria-label="前沿分析视图">${frontierButton('趋势总览',`data-frontier-tab="overview" aria-pressed="${!methods}"`)}${frontierButton('研究方法',`data-frontier-tab="methods" aria-pressed="${methods}"`)}</nav>${!methods?`<section class="frontier-insights"><header><h3>趋势解读</h3><span>本站收录 · ${escapeHtml(data.from)}—${escapeHtml(data.to)}</span></header><div>${observations}</div><p class="frontier-note">这些对照来自本站题名与已登记研究对象，展示研究切入点，不据此推断整个领域的转向。</p></section>`:''}<section class="frontier-main-chart">${!methods?`<header class="frontier-chart-heading"><h3>${evolution?'主题演化':'议题时间线'}</h3><div role="group" aria-label="趋势图类型">${frontierButton('议题时间线',`data-frontier-chart="timeline" aria-pressed="${!evolution}"`)}${frontierButton('主题演化',`data-frontier-chart="evolution" aria-pressed="${evolution}"`)}</div></header>`:''}${frontierGroups(a,view)}${methods ? frontierMethods(data,view):a.issues.length ? (evolution?frontierEvolution(data,view):frontierTimeline(data,view)):'<p class="frontier-empty">当前范围没有匹配到可展示议题的论文。</p>'}</section>${!methods?`<section class="frontier-representatives"><h3>代表研究</h3><p class="frontier-note">按跨期对照议题选取，用于阅读比较，不作质量排名。</p>${a.representatives.map(r=>frontierPapers([r.paper],a,r.reason)).join('')}</section>`:''}<details class="frontier-coverage"><summary>收录覆盖 · ${data.total} 篇 · 已识别议题 ${a.coverage.matched} 篇</summary><p>${escapeHtml(a.note)}</p><p>${escapeHtml(data.method_note)}${data.unknown_date_count?` 另有 ${data.unknown_date_count} 篇日期不明，未纳入时间图。`:''}</p>${researchAnnualChart(data,Math.max(360,Math.min(root.clientWidth || 700,950)))}${researchCoverageHtml(data)}<div id="frontierCollectionStatus"></div></details>${!methods?'<h3 class="frontier-all-papers">全部论文</h3>':''}`;
  if(status) document.getElementById('frontierCollectionStatus')?.append(status);
}
function handleResearchFrontierClick(target) {
  if(!target.closest('#countryResearchEvolution')) return false;
  const data=state.researchOverview,a=frontierCurrentAnalysis(data),view=countryReadingState();
  if(!a) return false;
  let selection=null;
  if(target.hasAttribute('data-frontier-tab')) view.frontierTab=target.dataset.frontierTab;
  else if(target.hasAttribute('data-frontier-chart')) view.frontierChart=target.dataset.frontierChart;
  else if(target.hasAttribute('data-frontier-group')) view.frontierGroup=target.dataset.frontierGroup;
  else if(target.hasAttribute('data-frontier-topic')) {const issue=a.issues.find(i=>i.id===target.dataset.frontierTopic);if(issue) selection={label:issue.label,ids:issue.papers,issue:issue.id};}
  else if(target.hasAttribute('data-frontier-unclassified')) selection={label:'待分类论文',ids:data.frontier.taxonomy.unclassified};
  else if(target.hasAttribute('data-frontier-node')) {const node=a.nodes.find(n=>n.id===target.dataset.frontierNode);if(node) selection={label:`${node.label} · ${node.year}`,ids:node.papers,issue:node.issue};}
  else if(target.hasAttribute('data-frontier-paper')) {const p=a.papers[target.dataset.frontierPaper];if(p) selection={label:p.title,ids:[String(p.id)],issue:p.issues[0]};}
  else if(target.hasAttribute('data-frontier-edge')) {const edge=a.edges.find(e=>e.id===target.dataset.frontierEdge);if(edge) selection={label:`${a.nodes.find(n=>n.id===edge.source).label} → ${a.nodes.find(n=>n.id===edge.target).label}`,ids:[...new Set(edge.evidence.flatMap(e=>[e.source,e.target]))],edges:edge.evidence};}
  else if(target.hasAttribute('data-frontier-method')) {const [issue,method]=target.dataset.frontierMethod.split(':'),node=a.issues.find(i=>i.id===issue);if(node) selection={label:`${node.label} · ${a.method_categories.find(m=>m.id===method).label}${view.frontierMethodYear?` · ${view.frontierMethodYear}`:''}`,ids:node.papers.filter(id=>a.papers[id].methods[method] && (!view.frontierMethodYear || a.papers[id].year===Number(view.frontierMethodYear))),issue,method};}
  else if(target.hasAttribute('data-frontier-observation')) {const o=a.observations[Number(target.dataset.frontierObservation)];if(o) selection={label:o.title,ids:o.papers};}
  else if(target.hasAttribute('data-frontier-back')) {view.frontierSelection=null;}
  else if(target.hasAttribute('data-frontier-more')) {view.frontierLimit=(view.frontierLimit || 12)+12;}
  else return false;
  if(selection) {view.frontierSelection={...selection,scope:`${data.country_iso3}:${data.from}:${data.to}`};view.frontierLimit=12;view.frontierReturn=Object.fromEntries([...target.attributes].filter(a=>a.name.startsWith('data-frontier-')).map(a=>[a.name,a.value]));}
  renderResearchOverview();
  const root=document.getElementById('countryResearchEvolution');
  if(selection) {root.scrollIntoView({block:'start'});root.querySelector('[data-frontier-back]')?.focus({preventScroll:true});}
  else if(target.hasAttribute('data-frontier-back') && view.frontierReturn) {const [attr,value]=Object.entries(view.frontierReturn)[0] || [];[...root.querySelectorAll(`[${attr}]`)].find(el=>el.getAttribute(attr)===value && el.matches('button,[role=button]'))?.focus();}
  else {const attr=[...target.attributes].find(a=>a.name.startsWith('data-frontier-'));if(attr) [...root.querySelectorAll(`[${attr.name}]`)].find(el=>el.getAttribute(attr.name)===attr.value)?.focus({preventScroll:true});}
  return true;
}

function frontierCurrentAnalysis(data) {
  const legacy=data?.frontier, taxonomy=legacy?.taxonomy;
  if(!taxonomy) return legacy;
  return {...taxonomy, method_categories:legacy.method_categories, nodes:[], edges:[], observations:[]};
}
function renderCountryTaxonomy(data, view, status, materials, toolbar, root) {
  const a=frontierCurrentAnalysis(data), scope=`${data.country_iso3}:${data.from}:${data.to}:${a.version}`;
  if(view.frontierSelection && ![scope,`${data.country_iso3}:${data.from}:${data.to}`].includes(view.frontierSelection.scope)) view.frontierSelection=null;
  if(view.frontierGroup && !a.domains.some(d=>d.label===view.frontierGroup)) view.frontierGroup=a.legacy_groups[view.frontierGroup] || '';
  materials.hidden=Boolean(view.frontierGroup || view.frontierSelection);toolbar.hidden=materials.hidden;
  root.after(toolbar);
  if(view.frontierSelection) { root.innerHTML=frontierDrilldown(view.frontierSelection,{...data,frontier:a},view); return; }
  const domain=a.domains.find(d=>d.label===view.frontierGroup), methods=view.frontierTab==='methods';
  const coverage=a.coverage, pct=n=>n===null?'—':`${(n*100).toFixed(1)}%`;
  const bars=d=>data.annual_counts.map(y=>{const point=d.years.find(p=>p.year===y.year),count=point?.count || 0,max=Math.max(1,...d.years.map(p=>p.count));return `<span class="taxonomy-year"><i style="--bar:${count/max}" aria-hidden="true"></i><span>${y.year}${y.partial?'*':''}</span><b>${count}</b></span>`;}).join('');
  const overview=`<div class="taxonomy-domains">${a.domains.map(d=>`<button type="button" class="taxonomy-domain" data-frontier-group="${escapeHtml(d.label)}"><span class="taxonomy-domain-title">${escapeHtml(d.label)}<span aria-hidden="true">↗</span></span><span class="taxonomy-domain-count"><strong>${d.count}</strong> 篇 <small>${pct(d.share)}</small></span><span class="taxonomy-years">${bars(d)}</span></button>`).join('')}</div>`;
  const topics=domain ? a.issues.filter(i=>i.domain_id===domain.id):[];
  const detail=domain ? `<div class="research-results-heading">${frontierButton('← 五类国别研究','data-frontier-group=""')}<h3>${escapeHtml(domain.label)} <small>${domain.count} 篇</small></h3></div><nav class="frontier-tabs" aria-label="领域分析">${frontierButton('专题与时间分布',`data-frontier-tab="overview" aria-pressed="${!methods}"`)}${frontierButton('研究方法',`data-frontier-tab="methods" aria-pressed="${methods}"`)}</nav>${methods?frontierMethods({...data,frontier:a},view):`<div class="taxonomy-topics">${topics.map(i=>`<button type="button" data-frontier-topic="${i.id}" ${i.count?'':'disabled'}><span>${escapeHtml(i.label)}</span><strong>${i.count} 篇</strong></button>`).join('')}</div>${domain.count?frontierTimeline({...data,frontier:a},view):'<p class="frontier-empty">当前时段尚无可靠分类到此领域的论文。</p>'}`}`:overview;
  root.innerHTML=`<header class="taxonomy-heading"><div><h3>国别研究分布</h3><p>经济发展、政治治理、资源环境、人口社会与国际关系</p></div><span>${escapeHtml(data.from)}—${escapeHtml(data.to)}</span></header><div class="taxonomy-coverage"><span>收录 <b>${coverage.total}</b> 篇</span><span>已分类 <b>${coverage.classified}</b> 篇 · ${pct(coverage.share)}</span>${frontierButton(`待分类 ${coverage.unclassified} 篇`,'data-frontier-unclassified')}</div>${detail}<p class="frontier-note">${escapeHtml(a.note)} * 当年截至 ${escapeHtml(data.to)}，不作全年推算。</p><details class="frontier-coverage"><summary>分类依据与收录范围</summary><p>依据题名、已登记研究对象、关键词与原始主题识别；期刊来源与研究方法单独筛选。点击专题可查看论文及识别依据。${escapeHtml(data.method_note || '')}</p>${data.unknown_date_count?`<p>另有 ${data.unknown_date_count} 篇日期不明，未纳入年度分布。</p>`:''}<div id="frontierCollectionStatus"></div></details>${!domain?'<h3 class="frontier-all-papers">全部论文</h3>':''}`;
  if(status) document.getElementById('frontierCollectionStatus')?.append(status);
}
