/* Typed material drafts share the existing project permissions and frozen exports. */
window.ProjectMaterials = (() => {
  let dialog;
  let current;
  let catalogCache;
  const esc = value => escapeHtml(String(value ?? ''));
  async function types() {
    if (!catalogCache) catalogCache = (await apiFetch('/reader/material-types')).items;
    return catalogCache;
  }
  // Deliberately small Markdown renderer: HTML is always text, including model/user input.
  function markdown(text) {
    const lines = String(text || '').split('\n');
    const result = [];
    let rows = [];
    function flush() {
      if (!rows.length) return;
      const cells = line => line.replace(/^\s*\||\|\s*$/g, '').split(/(?<!\\)\|/).map(s => esc(s.trim().replace(/\\\|/g, '|')));
      const header = cells(rows[0]);
      const body = rows.slice(1).filter(line => !/^\s*\|?[\s:|\-]+\|?\s*$/.test(line));
      result.push(`<div class="material-table-scroll"><table><thead><tr>${header.map(s=>`<th>${s}</th>`).join('')}</tr></thead><tbody>${body.map(line=>`<tr>${cells(line).map(s=>`<td>${s}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      rows = [];
    }
    for (const line of lines) {
      if (line.trim().startsWith('|')) { rows.push(line); continue; }
      flush();
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) result.push(`<h${heading[1].length}>${esc(heading[2])}</h${heading[1].length}>`);
      else if (line.startsWith('> ')) result.push(`<blockquote>${esc(line.slice(2))}</blockquote>`);
      else if (line.trim()) result.push(`<p>${esc(line)}</p>`);
    }
    flush();
    return result.join('');
  }
  async function ensureDialog() {
    await ProjectWorkbench.openPanel('outputs');
    const area=document.querySelector('#projectContextPanel .pw-panel-content');
    dialog = document.createElement('section');
    dialog.className = 'pw-material-editor';
    area.replaceChildren(dialog);
    dialog.setAttribute('aria-labelledby', 'materialDialogTitle');
    dialog.addEventListener('input',rememberEdits);
    dialog.addEventListener('cancel', event => {
      if (current?.busy) { event.preventDefault(); return; }
      current = null;
    });
    dialog.addEventListener('click', async event => {
      const button = event.target.closest('button');
      if (!button || !current) return;
      if (button.hasAttribute('data-material-close')) {
        if (!current.busy) { await ProjectWorkbench.openPanel('outputs'); }
      } else if (button.hasAttribute('data-material-preview')) {
        const preview = dialog.querySelector('[data-material-preview-body]');
        preview.innerHTML = markdown(dialog.querySelector('[name="markdown"]').value);
        preview.hidden = !preview.hidden;
        dialog.querySelector('[name="markdown"]').closest('label').hidden = !preview.hidden;
        button.textContent = preview.hidden ? '预览' : '返回编辑';
        dialog.querySelector('.material-dialog-body').scrollTop=0;
      } else if (button.hasAttribute('data-material-generate')) {
        await generate();
      } else if (button.hasAttribute('data-material-save-draft')) {
        await saveDraft();
      } else if (button.hasAttribute('data-material-save')) {
        await save();
      }
    });
    return dialog;
  }
  function header(title) {
    return `<header><h2 id="materialDialogTitle">${esc(title)}</h2><button type="button" class="ghost-btn compact" data-material-close>关闭</button></header>`;
  }
  function message(text) {
    const node = dialog.querySelector('[data-material-status]');
    if (node) node.textContent = text;
  }
  function busy(value) {
    current.busy = value;
    dialog.querySelectorAll('button,input,select,textarea').forEach(el => el.disabled = value);
    dialog.setAttribute('aria-busy', String(value));
  }
  async function open(caseId, previous = null) {
    await ensureDialog();
    current = {caseId, previous, busy: false, requestKey: crypto.randomUUID()};
    const session = current;
    dialog.innerHTML = header(previous ? '创建修订稿' : '整理材料') + '<div class="material-dialog-body"><p role="status">正在读取已采用资料…</p></div>';
    try {
      const [choices, data] = await Promise.all([types(), apiFetch(`/reader/research-cases/${caseId}/material-evidence`)]);
      if (current !== session) return;
      current.items = data.items;
      dialog.innerHTML = header('整理材料') + `<div class="material-dialog-body"><label>材料类型<select name="material_type">${choices.map(t=>`<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('')}</select></label><p data-material-input-note>${esc(choices[0].input_requirements)}</p><label data-material-dimensions hidden>比较维度<input name="comparison_dimensions" value="政策目标、政策工具、执行主体" maxlength="400"></label><p>勾选本次使用的已采用资料。基础国情来自项目国家的已登记档案；田野资料仅在项目内整理。</p><fieldset class="material-reference-list"><legend>选择引用（最多 150 条）</legend>${data.items.map((item,i)=>`<label><input type="checkbox" name="material_reference" value="${i}"><span><strong>${esc(item.snapshot.title || '数据记录')}</strong><small>${esc(({document_version:"资料",event_mention:"事件提及",structured_observation_version:"指标数据",field_material:"田野资料",country_profile:"国别档案"})[item.object_type])}${item.snapshot.event_title ? ` · ${esc(item.snapshot.event_title)}` : ""} · ${esc(item.snapshot.source_name)} · ${esc(item.snapshot.published_at || '日期未声明')}${item.basis ? ` · ${esc(item.basis)}` : ''}</small></span></label>`).join('') || '<p>暂无可用资料。请先到项目资料库采用资料。</p>'}</fieldset><p role="status" data-material-status></p></div><footer><button type="button" class="primary-btn" data-material-generate ${data.items.length ? '' : 'disabled'}>生成草稿</button></footer>`;
      dialog.querySelector('[name="material_type"]').addEventListener('change',event=>{dialog.querySelector('[data-material-dimensions]').hidden=event.target.value!=='comparison_matrix';dialog.querySelector('[data-material-input-note]').textContent=choices.find(t=>t.id===event.target.value)?.input_requirements || '';});
      if (previous) {
        const doc = previous.artifact_snapshot.output.document;
        dialog.querySelector('[name="material_type"]').value = doc.material_type;
        dialog.querySelector('[name="comparison_dimensions"]').value = (doc.comparison_dimensions || []).join('、');
        for (const input of dialog.querySelectorAll('[name="material_reference"]')) {
          const item = data.items[Number(input.value)];
          input.checked = doc.citations.some(c=>c.object_type===item.object_type && c.object_id===item.object_id);
        }
        await generate();
      }
    } catch(error) { if(current===session) dialog.innerHTML = header('整理材料') + `<p role="alert">${esc(error.message)}</p>`; }
  }
  async function generate() {
    const session=current;
    const refs = [...dialog.querySelectorAll('[name="material_reference"]:checked')].map(input=>{
      const item = current.items[Number(input.value)];
      return {object_type:item.object_type,object_id:item.object_id};
    });
    if (!refs.length || refs.length > 150) {message('请选择 1–150 条资料。');return;}
    const kind = dialog.querySelector('[name="material_type"]').value;
    const dimensions=dialog.querySelector('[name="comparison_dimensions"]').value.split(/[、,，]/).map(s=>s.trim()).filter(Boolean);
    const request = JSON.stringify({material_type:kind,references:refs,previous_target_id:current.previous?.target_id || null,comparison_dimensions:dimensions});
    if (current.request && current.request !== request) current.requestKey = crypto.randomUUID();
    current.request = request;
    busy(true);message('正在按所选材料类型整理，完成后可编辑和预览…');
    try {
      const result = await apiFetch(`/reader/research-cases/${current.caseId}/material-drafts`, {method:'POST',body:JSON.stringify({...JSON.parse(request),idempotency_key:current.requestKey})});
      if(current!==session)return;
      current.draft = result;
      current.saveKey = crypto.randomUUID();
      editor();
    } catch(error) {message(`${error.message}。所选资料仍保留，可以重试。`);}
    finally {if(current===session)busy(false);}
  }
  function editor(preview = false) {
    let doc = current.draft.document;
    try {
      const local=JSON.parse(localStorage.getItem(`project-material-edit:${current.caseId}:${current.draft.run_id}`) || 'null');
      if(local)doc={...doc,title:local.title,markdown:local.markdown};
    } catch (_) {}
    dialog.innerHTML = header('编辑研究材料') + `<div class="material-dialog-body"><div class="material-meta">${esc(doc.type_label)} · ${esc(localDate(doc.generated_at))} · 作者：${esc(doc.created_by_name || "研究者")}${doc.previous_target_id ? ' · 原稿修订' : ''}</div><p class="material-boundary">${doc.generation_status === 'incomplete' ? '部分栏目尚未完成，请补充证据或研究者判断。' : '草稿待人工复核。'} 保存表示采用此稿，不改变来源复核状态。</p><label>材料标题<input name="material_title" maxlength="300" value="${esc(doc.title)}"></label><label ${preview?'hidden':''}>正文（Markdown）<textarea name="markdown" maxlength="50000" rows="16">${esc(doc.markdown)}</textarea></label><div class="material-preview" data-material-preview-body ${preview?'':'hidden'}>${preview?markdown(doc.markdown):''}</div><details><summary>引用与定位 · ${doc.citations.length} 项</summary>${doc.citations.map(c=>`<p>[${c.number}] ${esc(c.title)} · ${esc(JSON.stringify(c.locator))}${safeUrl(c.source_url) ? ` <a href="${esc(safeUrl(c.source_url))}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ''}</p>`).join('')}</details><p role="status" data-material-status></p></div><footer><button type="button" class="ghost-btn" data-material-preview>${preview?'返回编辑':'预览'}</button><button type="button" class="ghost-btn" data-material-save-draft>保存草稿</button><button type="button" class="primary-btn" data-material-save>确认保存</button></footer>`;
  }
  function rememberEdits() {
    if(!current?.draft)return;
    const title=dialog.querySelector('[name="material_title"]');
    const body=dialog.querySelector('[name="markdown"]');
    if(title && body)localStorage.setItem(`project-material-edit:${current.caseId}:${current.draft.run_id}`,JSON.stringify({title:title.value,markdown:body.value}));
  }
  async function saveDraft() {
    const session=current;
    const title=dialog.querySelector('[name="material_title"]').value.trim();
    const text=dialog.querySelector('[name="markdown"]').value.trim();
    if(!title || !text) {message('请填写标题和正文。');return;}
    busy(true);
    try {
      const result=await apiFetch(`/reader/research-cases/${current.caseId}/material-drafts/${current.draft.run_id}`,{method:'PATCH',body:JSON.stringify({title,markdown:text,expected_revision:current.draft.document.draft_revision || 1})});
      if(current!==session)return;
      current.draft=result;
      localStorage.removeItem(`project-material-edit:${current.caseId}:${current.draft.run_id}`);
      message('草稿已保存，重新进入项目后可继续编辑。');
    } catch(error) {message(`${error.message}。当前编辑仍保留。`);}
    finally {if(current===session)busy(false);}
  }
  async function save() {
    const session=current;
    const title=dialog.querySelector('[name="material_title"]').value.trim();
    const text=dialog.querySelector('[name="markdown"]').value.trim();
    if(!title || !text) {message('请填写标题和正文。');return;}
    busy(true);message('正在复核引用并保存版本…');
    try {
      const result=await apiFetch(`/reader/research-cases/${current.caseId}/output-drafts/${current.draft.run_id}/confirm`, {method:'POST',body:JSON.stringify({idempotency_key:current.saveKey,title,markdown:text})});
      if(current!==session)return;
      const caseId=current.caseId;
      localStorage.removeItem(`project-material-edit:${current.caseId}:${current.draft.run_id}`);
      current=null;
      showToast(`已保存 v${result.version}`);
      await ProjectOutput.openSaved(result.target_id);

    } catch(error) {message(`${error.message}。编辑内容仍保留。`);}
    finally {if(current===session) busy(false);}
  }
  async function panel(root, topic, data) {
    if(!root.isConnected || state.topic?.id!==topic.id) return;
    const cards=data.items.map(item=>{
      const doc=item.artifact_snapshot?.output?.document;
      return `<article class="pw-content-row"><button type="button" data-output-open-saved="${item.target_id}">${esc(doc?.title || item.document_title || `成果 #${item.target_id}`)}</button><small>正式 v${Number(doc?.version || item.document_version || 1)} · ${esc(localDate(item.confirmed_at))}${item.availability?.status==='needs_review'?' · 引用待复核':''}</small></article>`;
    });
    for(const draft of data.drafts || []) cards.push(`<article class="pw-content-row"><button type="button" ${draft.workflow_document?'data-workflow-document':draft.material_type?'data-material-resume':'data-output-open-run'}="${Number(draft.run_id)}">${esc(draft.title || '未命名草稿')}</button><small>草稿${draft.availability==='needs_rights_review'?' · 授权已变，锁定待复核':draft.availability==='needs_review'?' · 引用待复核':''}</small></article>`);
    root.innerHTML=`<div class="pw-content-list">${cards.join('') || emptyState('尚无成果；选好资料后，在对话中继续写作。')}</div>`;
    root.addEventListener('click',async event=>{
      const workflowButton=event.target.closest('[data-workflow-document]');if(workflowButton){await SkillWorkflows.renderRun(Number(workflowButton.dataset.workflowDocument),root);return;}
      const b=event.target.closest('[data-material-resume]');if(!b)return;
      b.disabled=true;
      try {
        const fresh=await apiFetch(`/reader/research-cases/${topic.id}/material-drafts`);
        if(!root.isConnected || state.topic?.id!==topic.id)return;
        const draft=fresh.items.find(d=>d.run_id===Number(b.dataset.materialResume));
        if(!draft?.document) {showToast('草稿已保存或引用状态已变化，请重新打开材料列表。');return;}
        await ensureDialog();current={caseId:topic.id,draft,saveKey:crypto.randomUUID(),busy:false};
        editor(true);
      } catch(error) {showToast(error.message);}
      finally {b.disabled=false;}
    });
  }
  function detail(output,caseId) {
    const doc=output.artifact_snapshot?.output?.document;
    if(!doc) return '<p>当前引用需重新复核，请到项目资料库查看。</p>';
    return `<div class="material-meta">${esc(doc.type_label || '历史成果')} · ${esc(localDate(output.confirmed_at))} · v${Number(doc.version || 1)}</div><article class="material-preview">${markdown(doc.markdown || '')}</article><details><summary>引用回源 · ${(doc.citations || []).length} 项</summary>${(doc.citations || []).map(c=>`<p>[${c.number}] ${esc(c.title)} · ${esc(JSON.stringify(c.locator))}${safeUrl(c.source_url) ? ` <a href="${esc(safeUrl(c.source_url))}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ''}</p>`).join('')}</details><div class="inline-actions"><button type="button" class="ghost-btn compact" data-export-project-output="markdown" data-output-id="${output.target_id}">导出 Markdown</button><button type="button" class="ghost-btn compact" data-export-project-output="json" data-output-id="${output.target_id}">导出 JSON</button></div>`;
  }
  async function openWorkflow(runId) {await ensureDialog();await SkillWorkflows.renderRun(runId,dialog);}
  return {open,panel,detail,markdown,openWorkflow};
})();
