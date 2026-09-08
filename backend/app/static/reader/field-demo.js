/* Local, explicitly selected demo snapshots; never an authenticated user session. */
window.FieldDemo = (() => {
  const e = value => window.ReaderCore.escapeHtml(value ?? '');
  let data, active = 'mine', root;
  async function getData() {
    data ||= await window.ReaderCore.apiFetch('/reader/field-demo');
    return data;
  }
  function dialog(title, body) {
    let el = document.getElementById('fieldDemoDialog');
    if (!el) {el = document.createElement('dialog'); el.id='fieldDemoDialog'; el.className='field-dialog field-demo-dialog'; document.body.append(el);}
    el.innerHTML = `<header><div><small>研究者工作台 · Demo</small><h2>${e(title)}</h2></div><button data-demo-close aria-label="关闭">×</button></header><div class="field-demo-content">${body}</div>`;
    el.querySelector('[data-demo-close]').onclick=()=>el.close();
    if (!el.open) el.showModal();
    return el;
  }
  async function profile() {
    await getData();
    dialog('我的研究资料', '<p>以研究者身份直接浏览 Demo。我的资料使用你提供的两份技术组标准样稿，公共资料为虚构示例。</p><p>研究方向：区域国别研究资料获取、访谈整理与来源核验。</p><p>2 份个人材料 · 3 份公共示例 · S05 与 S03 两份成果</p>');
  }
  async function load(target, iso) {
    root=target;
    try {await getData(); if(data.public_demo) active='shared'; draw();}
    catch (error) {root.innerHTML=`<p>${e(error.message)}</p>`;}
  }
  function draw() {
    root.innerHTML=`<header class="field-demo-hero"><div><small>FIELDWORK LIBRARY</small><h2>田野资料</h2><p>保存现场记录，整理研究发现，回到原始语境。</p></div><span class="field-demo-badge">Demo · 研究者视角</span></header><nav class="field-tabs" aria-label="田野资料分类"><button data-demo-tab="mine" aria-pressed="${active==='mine'}">我的资料 <small>${data.materials.filter(m=>m.visibility==='mine').length}</small></button><button data-demo-tab="shared" aria-pressed="${active==='shared'}">公共资料 <small>${data.materials.filter(m=>m.visibility==='shared').length}</small></button></nav><div class="field-demo-list">${data.materials.filter(m=>m.visibility===active).map(m=>`<article class="field-card"><div class="field-demo-card-meta"><span>${e(m.kind)}</span><small>${e(m.badge)}</small></div><button class="field-title" data-demo-material="${e(m.id)}">${e(m.title)}</button><p>${e(m.introduction)}</p><div class="field-demo-card-footer"><small>${e(m.author)} · ${e(m.status)}</small><button data-demo-material="${e(m.id)}">${active==='mine'?'阅读与整理结果':'查看资料'} →</button></div></article>`).join('')}</div><section class="field-demo-results"><h3>看看材料如何形成成果</h3><div><button data-demo-result="s05"><small>S05 · 访谈与田野材料整理</small><strong>单份访谈样稿整理</strong><span>整理稿、原文对照、研究者笔记与单份备忘录 →</span></button><button data-demo-result="s03"><small>S03 · 政策与现实动态追踪</small><strong>刚果（金）矿业政策追踪简报</strong><span>更新清单、条款变化、新旧版本及异常记录 →</span></button></div></section><p class="field-demo-footnote">${e(data.notice || '个人材料来自你提供的标准样稿；公共资料均为虚构示例，仅用于展示页面。')}</p>`;
    root.querySelectorAll('[data-demo-tab]').forEach(b=>b.onclick=()=>{active=b.dataset.demoTab;draw();});
    root.querySelectorAll('[data-demo-material]').forEach(b=>b.onclick=()=>material(b.dataset.demoMaterial));
    root.querySelectorAll('[data-demo-result]').forEach(b=>{if(!data.results[b.dataset.demoResult])b.remove();else b.onclick=()=>result(b.dataset.demoResult);});
  }
  function material(id, tab='info') {
    const m=data.materials.find(x=>x.id===id); if(!m)return;
    if(m.workspace){const el=dialog(m.title,'<div data-single-record></div>');WorkflowEditor.mount(el.querySelector('[data-single-record]'),m.workspace.run_id);return;}
    const nav=`<nav class="field-tabs" aria-label="材料阅读"><button data-material-tab="info" aria-pressed="${tab==='info'}">材料信息</button><button data-material-tab="text" aria-pressed="${tab==='text'}">原文与整理稿</button><button data-material-tab="annotations" aria-pressed="${tab==='annotations'}">研究标注</button></nav>`;
    let content='';
    if(tab==='info') content=`<p>${e(m.introduction)}</p><dl class="field-demo-info">${Object.entries(m.context).map(([k,v])=>`<div><dt>${e(k)}</dt><dd>${e(v)}</dd></div>`).join('')}</dl><blockquote>${e(m.preview)}</blockquote><button data-read-text>阅读完整材料与整理稿</button>`;
    if(tab==='text') content=`<p>${e(m.text_note)} · ${m.units.length} 个原文单元</p><div class="field-demo-table-wrap"><table class="field-demo-table"><thead><tr><th>原文位置</th><th>原始文本</th><th>整理稿</th></tr></thead><tbody>${m.units.filter(u=>u.text.trim()).map(u=>`<tr id="demo-${e(id)}-${e(u.locator.replace(':','-'))}"><td>${e(u.locator)}</td><td>${e(u.text)}</td><td>${e(m.cleaned?.find(c=>c.locator===u.locator)?.text??u.text)}</td></tr>`).join('')}</tbody></table></div>`;
    if(tab==='annotations') content=`<p>候选标注与真实性核验分开。下面的内容保留原文位置，尚未经研究者确认。</p>${m.annotations.map(a=>`<article class="field-demo-annotation"><div><strong>${e(a.themes.join('、'))}</strong><small>${e(a.statement_type)} · ${{unverified:'未核验',not_applicable:'不适用',conflict:'存在冲突'}[a.verification]||'未核验'}</small></div><blockquote>${e(a.original_text)}</blockquote><p>${e(a.viewpoint)}</p><small>${e(a.note)}</small><p><button data-locator="${e(a.start_locator)}">回到原文 · ${e(a.start_locator)}</button></p></article>`).join('')}`;
    const el=dialog(m.title,nav+content);
    el.querySelectorAll('[data-material-tab]').forEach(b=>b.onclick=()=>material(id,b.dataset.materialTab));
    el.querySelector('[data-read-text]')?.addEventListener('click',()=>material(id,'text'));
    el.querySelectorAll('[data-locator]').forEach(b=>b.onclick=()=>{const loc=b.dataset.locator;material(id,'text');document.getElementById(`demo-${id}-${loc.replace(':','-')}`)?.scrollIntoView({block:'center'});});
  }
  const prose=body=>body.split('\n\n').map(p=>p.startsWith('### ')?`<h4>${e(p.slice(4))}</h4>`:p.startsWith('## ')?`<h3>${e(p.slice(3))}</h3>`:p.startsWith('# ')?`<h2>${e(p.slice(2))}</h2>`:`<p>${e(p).replaceAll('\n','<br>')}</p>`).join('');
  async function result(kind) {
    await getData();
    const r=data.results[kind];
    if(kind==='s05'&&r.workspace){const el=dialog('单份访谈样稿整理','<div data-single-record></div>');await WorkflowEditor.mount(el.querySelector('[data-single-record]'),r.workspace.run_id);return;}
    const details=kind==='s05'?`<section><h3>材料与处理记录</h3>${data.materials.filter(m=>m.visibility==='mine').map(m=>`<p><button data-result-material="${e(m.id)}">${e(m.title)} →</button><small>${m.units.length} 个原文单元 · ${m.annotations.length} 个候选片段 · ${e(m.elapsed_seconds)} 秒</small></p>`).join('')}</section>`:`<section><h3>更新材料清单</h3><div class="field-demo-table-wrap"><table class="field-demo-table"><thead><tr><th>材料</th><th>时间与状态</th><th>本轮记录</th></tr></thead><tbody>${r.observations.map(o=>`<tr><td>${e(o.title)}</td><td>${e(o.date)}<br>${e(o.status)}</td><td>${e(o.note)}</td></tr>`).join('')}</tbody></table></div><h3>Article 241 新旧条款对照</h3><div class="field-demo-table-wrap"><table class="field-demo-table"><thead><tr><th>类别</th><th>2002 年文本</th><th>2018 年修正文本</th></tr></thead><tbody>${r.rate_changes.map(row=>`<tr>${row.map(c=>`<td>${e(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div><details><summary>查看法文原文与来源</summary>${r.sources.map(s=>`<h4>${e(s.title)}</h4><a href="${e(s.source_url)}" target="_blank" rel="noopener">来源文件 ↗</a>${s.units.map(u=>`<p><small>${e(u.locator)}</small></p><blockquote>${e(u.text)}</blockquote>`).join('')}`).join('')}</details><h3>本次模型生成的变化摘要</h3><p>${e(r.generated_summary)}</p></section>`;
    const el=dialog(r.title,`<p class="field-demo-badge">${e(r.label)}</p><article class="field-demo-manuscript">${prose(r.body)}</article>${details}<section><h3>研究者修订</h3><p>可直接修改这份 Demo 正文；保存为当前浏览器的演示草稿。</p><textarea aria-label="成果正文" id="demoResultBody"></textarea><button id="demoSaveResult">保存演示草稿</button><button id="demoDownloadResult">下载 Markdown</button><span id="demoSaveStatus" role="status"></span></section>`);
    const key='guobie-demo-result-'+kind+'-'+data.version;
    let saved='';try{saved=localStorage.getItem(key)||'';}catch(_){}
    const textarea=el.querySelector('#demoResultBody');textarea.value=saved||r.body;
    if(saved)el.querySelector('.field-demo-manuscript').innerHTML=prose(saved);
    el.querySelector('#demoSaveResult').onclick=()=>{try{localStorage.setItem(key,textarea.value);el.querySelector('.field-demo-manuscript').innerHTML=prose(textarea.value);el.querySelector('#demoSaveStatus').textContent='已保存演示草稿';}catch(_){el.querySelector('#demoSaveStatus').textContent='浏览器未允许本地保存，请下载草稿';}};
    el.querySelector('#demoDownloadResult').onclick=()=>{const blob=new Blob([textarea.value],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=r.title+'.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
    el.querySelectorAll('[data-result-material]').forEach(b=>b.onclick=()=>material(b.dataset.resultMaterial,'annotations'));
  }
  return {load,profile,result};
})();
