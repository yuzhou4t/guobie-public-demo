/* Review and inspect project evidence inside the shared right panel. */
window.ProjectLibrary = (() => {
  const labels={pending:'待审阅',accepted:'已采用',rejected:'已排除',needs_revision:'待核实'};
  const types={tracking_observation:'追踪观察',document_version:'论文 / 文档',event_mention:'事件提及',structured_observation_version:'结构化观测'};
  const key=item=>`${item.object_type}:${item.object_id}`;
  function render(root,data,topic,options={}) {
    const accepted=options.filter==='accepted';
    const canReview=(topic.current_member?.permissions || []).includes('revise');
    const rows=data.items.filter(item=>options.filter==='accepted' ? item.decision==='accepted' : item.decision!=='accepted' && (options.showRejected || item.decision!=='rejected'));
    const selected=new Set();
    let note='';
    let query='';
    let reviewing=false;
    function actions(index) {
      return ['accepted','rejected','needs_revision'].map(decision=>`<button type="button" data-library-review="${index}" data-decision="${decision}">${index==='batch' ? '批量' : ''}${({accepted:'采用',rejected:'排除',needs_revision:'待核实'})[decision]}</button>`).join('');
    }
    function dates(snapshot) {
      return snapshot.published_at ? publishedLabel(snapshot.published_at,snapshot.published_at_precision) : snapshot.period || '日期未声明';
    }
    function meta(item) {
      return `<small>${labels[item.decision]} · ${escapeHtml(item.snapshot?.source_name || types[item.object_type] || '资料')} · ${escapeHtml(dates(item.snapshot || {}))}</small>`;
    }
    function details(item) {
      const s=item.snapshot || {};
      const fields=[['资料类型',types[item.object_type]],['发表日期 / 统计期',dates(s)],['匹配依据',(s.matched_terms || []).join(' · ')],['资料可用程度',s.boundary || '仅提供已登记的来源与版本信息，需继续核对内容。'],['审阅备注',item.note],['数值',s.value ?? s.value_text ?? s.missing_reason],['单位',s.unit],['许可',s.license]];
      return `<dl class="pw-evidence-details">${fields.filter(([,v])=>v!==undefined && v!==null && v!=='').map(([name,value])=>`<dt>${name}</dt><dd>${escapeHtml(String(value))}</dd>`).join('')}</dl><details><summary>来源版本与定位</summary><pre>${escapeHtml(JSON.stringify(s.locator || {},null,2))}</pre></details>${safeUrl(s.source_url || s.canonical_url) ? `<a href="${escapeHtml(safeUrl(s.source_url || s.canonical_url))}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a>` : ''}`;
    }
    function controls(index) {
      return canReview ? `<label class="pw-review-note">审阅备注<textarea data-library-note maxlength="2000" rows="2" placeholder="记录采用依据或需要核实的问题">${escapeHtml(note)}</textarea></label><div class="pw-review-actions">${actions(index)}</div>` : '';
    }
    function syncSelection() {
      const visible=[...root.querySelectorAll('[data-library-select]')].filter(input=>!input.closest('article').hidden);
      const all=root.querySelector('[data-library-all]');
      if(all) {const n=visible.filter(input=>selected.has(Number(input.dataset.librarySelect))).length;all.checked=visible.length>0 && n===visible.length;all.indeterminate=n>0 && n<visible.length;}
      const visibleSelected=visible.filter(input=>selected.has(Number(input.dataset.librarySelect))).length;
      const hiddenSelected=selected.size-visibleSelected;
      const count=root.querySelector('[data-library-selected]');if(count)count.textContent=`已选 ${selected.size} 项${hiddenSelected?`（筛选外 ${hiddenSelected} 项）`:''}`;
      const clear=root.querySelector('[data-library-clear]');if(clear)clear.hidden=!selected.size;
      root.querySelectorAll('[data-library-review="batch"]').forEach(button=>button.disabled=reviewing || !selected.size);
    }
    function filterRows() {
      root.querySelectorAll('.pw-evidence-list .pw-library-item').forEach((row,index)=>row.hidden=!(rows[index].snapshot?.title || '').toLowerCase().includes(query.trim().toLowerCase()));
      syncSelection();
    }
    function list() {
      root.innerHTML=`<div class="pw-library-top"><input type="search" data-library-query aria-label="筛选题名" value="${escapeHtml(query)}" placeholder="搜索资料题名">${accepted?'':'<button type="button" data-review-expand aria-expanded="false">展开筛选</button>'}</div>${!accepted && data.counts.rejected ? `<label class="pw-evidence-check pw-excluded-toggle"><input type="checkbox" data-library-rejected ${options.showRejected?'checked':''}> 显示已排除 ${data.counts.rejected} 项</label>` : ''}${!accepted && rows.length && canReview ? `<div class="pw-review-toolbar"><label><input type="checkbox" data-library-all> 全选当前结果</label><span data-library-selected>已选 ${selected.size} 项</span><button type="button" data-library-clear hidden>清空选择</button><div class="pw-review-actions">${actions('batch')}</div></div>` : ''}<div data-library-status role="status"></div><div class="pw-evidence-list">${rows.map((item,index)=>`<article class="pw-library-item">${canReview && !accepted ? `<input type="checkbox" data-library-select="${index}" ${selected.has(index)?'checked':''} aria-label="选择 ${escapeHtml(item.snapshot?.title || '资料')}">` : ''}<div><button type="button" class="pw-evidence-title" data-library-open="${index}">${escapeHtml(item.snapshot?.title || '资料')}</button>${meta(item)}</div></article>`).join('') || emptyState('当前没有此类资料')}</div>`;
      filterRows();
      const expand=root.querySelector('[data-review-expand]');
      if(expand && document.querySelector('.pw-review-wide')) {expand.textContent='返回侧栏';expand.setAttribute('aria-expanded','true');}
    }
    function show(index) {
      const item=rows[index];
      if(!item)return;
      root.innerHTML=`<button type="button" class="ghost-btn compact" data-library-back>← 返回资料列表</button><article class="pw-evidence-detail"><h3>${escapeHtml(item.snapshot?.title || '资料')}</h3>${meta(item)}${details(item)}${accepted?'':controls(index)}<div data-library-status role="status"></div></article>`;
    }
    list();
    if(options.focus) {const index=rows.findIndex(item=>key(item)===options.focus);if(index>=0)show(index);}
    root.oninput=event=>{
      if(event.target.matches('[data-library-note]'))note=event.target.value;
      if(event.target.matches('[data-library-query]')) {
        query=event.target.value;
        filterRows();
      }
    };
    root.onchange=event=>{
      if(event.target.matches('[data-library-rejected]')) {render(root,data,topic,{...options,showRejected:event.target.checked});return;}
      if(event.target.matches('[data-library-select]')) {
        const index=Number(event.target.dataset.librarySelect);
        if(event.target.checked)selected.add(index);else selected.delete(index);
        syncSelection();
      }
      if(event.target.matches('[data-library-all]')) {
        const visible=[...root.querySelectorAll('[data-library-select]')].filter(input=>!input.closest('article').hidden);
        visible.forEach(input=>{const i=Number(input.dataset.librarySelect);if(event.target.checked)selected.add(i);else selected.delete(i);input.checked=event.target.checked;});
        syncSelection();
      }
    };
    root.onclick=async event=>{
      if(event.target.closest('[data-library-clear]')) {selected.clear();root.querySelectorAll('[data-library-select]').forEach(input=>input.checked=false);syncSelection();return;}
      if(event.target.closest('[data-library-back]')) {list();return;}
      const open=event.target.closest('[data-library-open]');
      if(open) {show(Number(open.dataset.libraryOpen));return;}
      const button=event.target.closest('[data-library-review]');
      if(!button)return;
      const indices=button.dataset.libraryReview==='batch' ? [...selected] : [Number(button.dataset.libraryReview)];
      const status=root.querySelector('[data-library-status]');
      if(!indices.length){status.textContent='请先选择资料。';return;}
      if(reviewing)return;
      reviewing=true;
      root.querySelectorAll('[data-library-review]').forEach(item=>item.disabled=true);
      const decision=button.dataset.decision;
      const reviewNote=note;
      let completed=0;
      let replaced=false;
      const failures=[];
      try {
        let result;
        for(let offset=0;offset<indices.length;offset+=100) {
        const batch=indices.slice(offset,offset+100);
        result=await apiFetch(`/reader/research-cases/${topic.id}/library/reviews`,{method:'POST',body:JSON.stringify({items:batch.map(index=>({object_type:rows[index].object_type,object_id:rows[index].object_id,expected_decision:rows[index].decision})),decision,note:reviewNote,allow_partial:true})});
        failures.push(...(result.failures || []));
        const failed=new Set((result.failures || []).map(key));
        completed+=result.reviewed_count ?? batch.length;
        for(const index of batch) {if(!failed.has(key(rows[index]))) {selected.delete(index);rows[index].decision=decision;}}
        root.querySelectorAll('[data-library-select]').forEach(input=>input.checked=selected.has(Number(input.dataset.librarySelect)));
        status.textContent=`已保存 ${completed} / ${indices.length} 项`;
        }
        const refreshed=await apiFetch(`/reader/research-cases/${topic.id}`);
        if(state.topic?.id!==topic.id)return;
        state.topic=refreshed;
        ProjectWorkbench.updateCounts(result);
        ProjectOnboarding.refreshMaterials(document.querySelector('[data-project-task-form]'),state.topic);
        if(root.isConnected) {replaced=true;render(root,result,state.topic,{filter:options.filter,showRejected:options.showRejected});root.querySelector('[data-library-status]').innerHTML=`<p>已${decision==='accepted'?'采用':'保存'} ${completed} 项${failures.length?`；${failures.length} 项未采用，仍保留在候选资料中`:''}。</p>${completed && decision==='accepted'?'<button type="button" data-pw-panel="accepted">查看已采用资料</button>':''}${failures.length?`<details open><summary>查看未采用原因</summary><ul>${failures.map(item=>`<li><strong>${escapeHtml(item.title)}</strong>：${escapeHtml(item.reason)}</li>`).join('')}</ul></details>`:''}`;}
      } catch(error) {if(root.isConnected){status.textContent=`已保存 ${completed} 项；剩余未保存：${error.message}。请刷新列表核对后重试。`;root.querySelectorAll('[data-library-review]').forEach(item=>item.disabled=false);}}
      finally {reviewing=false;if(root.isConnected && !replaced)syncSelection();}
    };
  }
  async function openEvidence(kind,id) {
    const topic=state.topic;
    const data=await apiFetch(`/reader/research-cases/${topic.id}/library`);
    const item=data.items.find(row=>row.object_type===kind && row.object_id===Number(id));
    if(!item){showToast('该资料当前不在项目内。');return;}
    const filter=item.decision==='accepted'?'accepted':'candidates';
    await ProjectWorkbench.openPanel(filter);
    render(document.querySelector('#projectContextPanel .pw-panel-content'),data,topic,{filter,focus:key(item),showRejected:item.decision==='rejected'});
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-project-evidence]');
    if(button) {const [kind,id]=button.dataset.projectEvidence.split(':');openEvidence(kind,id).catch(error=>showToast(error.message));}
  });
  return {render,openEvidence};
})();
