/* Durable project output editing; citation identity is always server-owned. */
window.ProjectOutput = (() => {
  let current = null;
  const base = () => `/reader/research-cases/${state.topic.id}`;
  const key = run => `project-output-edit:${state.topic.id}:${run}`;
  const requestKey = task => `project-output-request:${state.topic.id}:${task}`;
  async function root() {
    await ProjectWorkbench.openPanel('outputs');
    const area = document.querySelector('#projectContextPanel .pw-panel-content');
    area.innerHTML = '<button type="button" class="ghost-btn compact" data-pw-panel="outputs">← 返回成果列表</button><div data-output-editor></div>';
    return area.querySelector('[data-output-editor]');
  }
  function render(area, data, saved = false, editing = false) {
    current = {data, saved, area};
    const doc = data.document;
    let local = null;
    try { local = JSON.parse(localStorage.getItem(key(data.run_id)) || 'null'); } catch (_) {}
    const edited = !saved && local ? local : doc;
    if(!saved && local?.expected_revision) data.document = {...doc, edit_revision:local.expected_revision};
    area.innerHTML = `<h3>${escapeHtml(edited.title)}</h3><p>${saved ? `正式版本 v${Number(doc.version)}` : `草稿 · 修订 ${Number(doc.edit_revision || 1)}`}</p>
      <div role="status" data-output-status>${local && !saved ? '已恢复本地未提交编辑；请保存草稿后确认。' : ''}</div>${!saved && local ? '<button type="button" data-output-server>放弃本地修改，读取服务器版本</button>' : ''}
      ${saved || !editing ? '' : `<label>标题<input data-output-title maxlength="300" value="${escapeHtml(edited.title)}"></label><label>正文（Markdown）<textarea data-output-markdown rows="14" maxlength="50000">${escapeHtml(edited.markdown)}</textarea></label>`}
      <div class="inline-actions">${saved ? '<button type="button" data-output-copy>另建草稿继续编辑</button><button type="button" data-output-export="markdown">下载 Markdown</button><button type="button" data-output-export="json">下载 JSON</button>' : !editing ? '<button type="button" data-output-edit>编辑草稿</button>' : '<button type="button" data-output-save>保存草稿</button><button type="button" data-output-confirm>确认保存为正式版本</button><button type="button" data-output-generate-new>按当前计划重新生成</button>'}</div>
      <h4>正文预览</h4><div data-output-preview>${ProjectMaterials.markdown(edited.markdown)}</div>
      <details><summary>引用来源 · ${doc.citations?.length || 0} 项</summary>${(doc.citations || []).map(c=>`<article><b>[${Number(c.number)}] ${escapeHtml(c.title)}</b><p>${escapeHtml(c.source_name || '')}</p><small>${escapeHtml(JSON.stringify(c.locator || {}))}</small></article>`).join('')}</details>`;
    area.querySelectorAll('input,textarea').forEach(input => input.addEventListener('input', () => {
      const edit = values(area);
      localStorage.setItem(key(data.run_id), JSON.stringify({...edit,expected_revision:data.document.edit_revision || 1}));
      area.querySelector('[data-output-preview]').innerHTML = ProjectMaterials.markdown(edit.markdown);
      area.querySelector('[data-output-status]').textContent = '修改保存在本机，尚未提交为正式成果。';
    }));
  }
  function values(area) { return {title:area.querySelector('[data-output-title]').value, markdown:area.querySelector('[data-output-markdown]').value}; }
  async function open(run) {
    const area = await root();
    try {
      const data = await apiFetch(`${base()}/output-drafts/${run}`);
      if(data.target_id) return openSaved(data.target_id, area);
      localStorage.setItem(`project-output-last:${state.topic.id}`, String(run));
      render(area,data);
    } catch(e) {area.innerHTML=errorState(e.message);}
  }
  async function openSaved(target, area = null) {
    area ||= await root();
    const item = await apiFetch(`${base()}/outputs/${target}`);
    if (["field_research_workflow", "policy_tracking_workflow"].includes(item.artifact_snapshot?.type)) {
      SkillWorkflows.renderSaved(area,item.artifact_snapshot,item.capability_run_id);
      return;
    }
    if(item.availability?.status==='needs_review' || !item.artifact_snapshot?.output?.document) {
      area.innerHTML = `<h3>${escapeHtml(item.document_title || '历史成果')}</h3>${ProjectWorkbench.outputStatus(item)}<p>请核对项目资料后重新生成。</p>`;
      return;
    }
    if(item.artifact_snapshot.output.document.material_type) {
      area.innerHTML = ProjectMaterials.detail(item,state.topic.id);
      return;
    }
    render(area,{run_id:item.capability_run_id,target_id:target,document:item.artifact_snapshot.output.document},true);
  }
  async function prepare() {
    const form=document.querySelector('[data-project-task-form]');
    if(!form || form.dataset.pwBusy==='true' || form.dataset.psBusy==='true')return;
    const taskId=form.elements.conversation_id.value;
    if(!taskId){showToast('请先确认研究计划并采用资料。');return;}
    const task=await apiFetch(`${base()}/tasks/${taskId}`);
    if(!form.isConnected || form.elements.conversation_id.value!==taskId)return;
    if(!form.elements.research_mode.querySelector('option[value="write"]'))form.elements.research_mode.add(new Option('根据已采用资料写作','write'));
    form.elements.research_mode.value='write';
    form.elements.research_mode.dispatchEvent(new Event('change',{bubbles:true}));
    form.elements.question.value=`请根据当前项目已采用的资料，撰写《${state.topic.title}》的${task.plan?.output_type || '研究成果草稿'}。\n\n研究问题：${state.topic.research_question || '按已确认研究计划'}\n研究范围与结构：${task.plan?.summary || '按已确认研究计划'}\n\n要求：围绕研究问题组织正文，关键事实注明引用来源；区分来源观点、研究判断与待核实事项。仅有书目信息时，不推断正文结论。完成后将草稿放入右侧成果栏，供我审阅和编辑。`;
    form.elements.question.dispatchEvent(new Event('input',{bubbles:true}));
    ProjectWorkbench.closePanel(false);form.elements.question.focus();
  }
  async function generate(fresh = false, instruction = '') {
    const taskId = document.querySelector('[data-project-task-form]')?.elements.conversation_id.value;
    const area = await root();
    if(!taskId) {area.innerHTML='<p>请先规划研究、执行计划并审阅资料。</p>';return;}
    const task = await apiFetch(`${base()}/tasks/${taskId}`);
    let request = fresh ? null : localStorage.getItem(requestKey(taskId));
    if(!request) {request=crypto.randomUUID();localStorage.setItem(requestKey(taskId),request);}
    area.innerHTML='<p role="status">正在准备成果，可离开面板，稍后继续读取。</p><button data-output-resume>检查生成状态</button>';
    try {
      let result;
      try { result = await apiFetch(`${base()}/tasks/${taskId}/output-requests/${request}`); }
      catch(e) { if(e.status !== 404) throw e; }
      if(!result) result = await apiFetch(`${base()}/tasks/${taskId}/output-draft`,{method:'POST',body:JSON.stringify({plan_revision:task.revision,idempotency_key:request,instruction})});
      if(result.status==='completed' || result.run_id) { await ProjectWorkbench.openPanel('outputs'); return true; }
      if(result.status==='failed') area.innerHTML=`<p role="status">${escapeHtml(result.error || '生成失败')}</p><button data-output-generate-new>重新生成</button>`;
      else area.querySelector('p').textContent='生成请求已登记，尚未结束。稍后点击检查状态。';
    } catch(e) { area.innerHTML=`${errorState(e.message)}<button data-output-resume>查询已登记请求</button><button data-output-generate-new>重新生成</button>`; }
  }
  document.addEventListener('submit',async event=>{
    const form=event.target.closest('[data-project-task-form]');
    if(!form || form.elements.research_mode.value!=='write')return;
    event.preventDefault();event.stopImmediatePropagation();
    if(form.dataset.pwBusy==='true' || form.dataset.psBusy==='true')return;
    const instruction=form.elements.question.value.trim();if(!instruction)return;
    const button=form.querySelector('[type="submit"]');button.disabled=true;ProjectWorkbench.setBusy(form,true);
    const thread=form.closest('.project-conversation').querySelector('.project-task-thread');
    const progress=document.createElement('article');progress.className='is-running';
    progress.innerHTML=`<strong>正在根据已采用资料写作</strong><p>${escapeHtml(instruction)}</p><p role="status">正在整理正文与引用，完成后进入右侧成果栏…</p>`;thread.append(progress);thread.scrollTop=thread.scrollHeight;
    try {
      if(await generate(true,instruction)) {
        form.elements.question.value='';localStorage.removeItem(form.dataset.promptDraftKey);
        const task=await apiFetch(`${base()}/tasks/${form.elements.conversation_id.value}`);ProjectPlanUI.render(task,form);
        await loadProjectTasks(Number(form.dataset.projectTaskForm));
      } else progress.querySelector('[role="status"]').textContent='本轮写作尚未完成，原提示词已保留；请查看右侧提示。';
    } catch(error) {progress.querySelector('[role="status"]').textContent=`写作未完成：${error.message}。原提示词已保留。`;}
    finally {button.disabled=false;ProjectWorkbench.setBusy(form,false);progress.classList.remove('is-running');}
  },true);
  document.addEventListener('click', async event => {
    const button=event.target.closest('[data-output-generate],[data-output-generate-new],[data-output-resume],[data-output-open-last],[data-output-open-run],[data-output-open-saved],[data-output-server],[data-output-edit],[data-output-save],[data-output-confirm],[data-output-copy],[data-output-export]');
    if(!button)return;
    button.disabled=true;
    try {
      if(button.dataset.outputOpenSaved)return await openSaved(button.dataset.outputOpenSaved);
      if(button.dataset.outputOpenRun)return await open(button.dataset.outputOpenRun);
      if(button.hasAttribute('data-output-open-last')) return await open(localStorage.getItem(`project-output-last:${state.topic.id}`));
      if(button.matches('[data-output-generate],[data-output-generate-new]')) return await prepare();
      if(button.hasAttribute('data-output-resume')) return await generate();
      const {area,data}=current;
      if(button.hasAttribute('data-output-edit')) {render(area,data,false,true);return;}
      const caseId=state.topic.id;
      const baseUrl=base();
      if(button.hasAttribute('data-output-server')) {localStorage.removeItem(key(data.run_id));return await open(data.run_id);}
      if(button.hasAttribute('data-output-copy')) {
        const copyKey=`project-output-copy:${state.topic.id}:${data.target_id}`;
        let request=localStorage.getItem(copyKey);
        if(!request) {request=crypto.randomUUID();localStorage.setItem(copyKey,request);}
        const result=await apiFetch(`${baseUrl}/outputs/${data.target_id}/draft`,{method:'POST',body:JSON.stringify({idempotency_key:request})});
        localStorage.removeItem(copyKey);
        return await open(result.run_id);
      }
      if(button.dataset.outputExport) {
        const format=button.dataset.outputExport;
        const result=await apiFetch(`${baseUrl}/outputs/${data.target_id}/exports`,{method:'POST',body:JSON.stringify({format,idempotency_key:`output-${data.target_id}-${format}`})});
        const url=URL.createObjectURL(new Blob([result.content],{type:result.media_type}));
        const a=document.createElement('a');a.href=url;a.download=`${data.document.title}.${format==='markdown'?'md':'json'}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);return;
      }
      const edit=values(area);
      const updated=await apiFetch(`${baseUrl}/output-drafts/${data.run_id}`,{method:'PATCH',body:JSON.stringify({...edit,expected_revision:data.document.edit_revision || 1})});
      if(current?.data.run_id===data.run_id)current.data=updated;
      localStorage.removeItem(key(data.run_id));
      if(button.hasAttribute('data-output-confirm')) {
        const result=await apiFetch(`${baseUrl}/output-drafts/${data.run_id}/confirm`,{method:'POST',body:JSON.stringify({...edit,expected_revision:updated.document.edit_revision,idempotency_key:`confirm-output-${data.run_id}`})});
        if(state.topic?.id===caseId && area.isConnected)await openSaved(result.target_id,area);
      } else if(state.topic?.id===caseId && area.isConnected) {render(area,updated,false,true);area.querySelector('[data-output-status]').textContent='草稿已保存，可继续编辑或确认正式版本。';}
    } catch(e) {
      const status=document.querySelector('[data-output-status]');
      if(status)status.textContent=`${e.message}；本地编辑保留，可重新打开草稿核对。`;
    } finally {button.disabled=false;}
  });
  return {open,openSaved,generate};
})();
