/* Project shell: one navigation rail; panels never replace the conversation DOM. */
window.ProjectWorkbench = (() => {
  let panelSerial = 0;
  window.matchMedia('(max-width:640px)').addEventListener('change',event=>{
    if(event.matches) document.querySelector('.pw-history-wrap')?.removeAttribute('open');
  });
  let panelTrigger = null;
  const labels = {overview: '项目概览', candidates: '候选资料', accepted: '已采用资料', outputs: '成果', settings: '设置与成员'};
  function updateCounts(library, outputs) {
    const counts = {candidates: library.items.filter(item=>['pending','needs_revision'].includes(item.decision)).length, accepted: library.counts.accepted};
    if(outputs) counts.outputs=outputs.items.length+(outputs.drafts || []).length;
    for(const [pane,count] of Object.entries(counts)) {
      const badge=document.querySelector(`[data-shelf-count="${pane}"]`);
      if(badge) badge.textContent=count;
    }
  }
  function closePanel(restoreFocus = true) {
    panelSerial++;
    const panel = document.getElementById('projectContextPanel');
    panel.hidden = true;
    document.querySelector('.project-taskbench')?.classList.remove('pw-panel-open','pw-overview-open','pw-review-wide');
    document.querySelectorAll('[data-pw-panel]').forEach(button => button.setAttribute('aria-expanded','false'));
    if (restoreFocus && panelTrigger?.isConnected) panelTrigger.focus();
  }
  async function openPanel(pane) {
    if(pane==='evidence') pane='candidates';
    if (document.activeElement?.matches("[data-pw-panel]")) panelTrigger = document.activeElement;
    const serial = ++panelSerial;
    const topic = state.topic;
    if (!topic) return;
    const panel = document.getElementById('projectContextPanel');
    panel.hidden = false;
    document.querySelector('.project-taskbench')?.classList.add('pw-panel-open');
    panel.dataset.pane=pane;
    document.querySelector('.project-taskbench')?.classList.remove('pw-review-wide');
    panel.innerHTML = `<header class="pw-panel-header"><h2>项目内容</h2><button type="button" class="ghost-btn compact" data-pw-panel="overview">项目概览</button><button type="button" class="ghost-btn compact" data-pw-close aria-label="收起项目内容">×</button></header><nav class="pw-shelf-tabs" aria-label="项目资料与成果">${['candidates','accepted','outputs'].map(key=>`<button type="button" data-pw-panel="${key}" aria-current="${key===pane ? 'page' : 'false'}">${labels[key]} <span data-shelf-count="${key}">—</span></button>`).join('')}</nav><div class="pw-panel-content">${detailSkeleton(2)}</div>`;
    document.querySelectorAll('[data-pw-panel]').forEach(button => button.setAttribute('aria-expanded',String(button.dataset.pwPanel === pane)));
    panel.querySelector('[data-pw-close]').focus();
    const root = panel.querySelector('.pw-panel-content');
    try {
      const [library,outputs]=await Promise.all([apiFetch(`/reader/research-cases/${topic.id}/library`),apiFetch(`/reader/research-cases/${topic.id}/outputs`)]);
      if(serial!==panelSerial) return;
      updateCounts(library,outputs);
      if (pane === 'overview') {
        const latest = await apiFetch(`/reader/research-cases/${topic.id}/brief/overview`);
        if(serial!==panelSerial || state.topic?.id!==topic.id) return;
        topic.brief=latest.brief;
        const brief = topic.brief?.confirmed || {};
        const overview=latest.overview;
        const dynamic=overview ? `<section class="pw-dynamic-overview"><h3>AI 动态概览</h3><p class="muted">随研究计划自动更新 · 计划 v${Number(overview.plan_revision)} · ${escapeHtml(new Date(overview.updated_at).toLocaleString('zh-CN'))}</p><p>${escapeHtml(overview.summary)}</p><details><summary>计划安排与待核实事项</summary><ol>${overview.steps.map(step=>`<li>${escapeHtml(step)}</li>`).join('')}</ol>${overview.evidence_gaps.length ? `<h4>待补充证据</h4><ul>${overview.evidence_gaps.map(gap=>`<li>${escapeHtml(gap)}</li>`).join('')}</ul>` : ''}<p>预期成果：${escapeHtml(overview.output_type)}</p></details><p class="muted">这是 AI 对研究安排的整理，尚未核实的事项不作为研究结论。${overview.brief_changed ? '项目说明已修改，下一次讨论计划后会同步更新。' : ''}</p></section>` : '<p class="muted">进入对话与 AI 讨论后，这里会随研究计划自动丰富概览；现在无需填写完整。</p>';
        root.innerHTML = `<h3>${escapeHtml(topic.title)}</h3>${dynamic}<h4>项目说明</h4><dl class="pw-brief">${[['研究目的',brief.purpose || topic.research_question],['研究范围',brief.scope || topic.scope?.country_iso3],['预期成果',brief.outcome]].map(([key,value])=>`<dt>${key}</dt><dd>${escapeHtml(value || '尚未填写')}</dd>`).join('')}</dl><details class="pw-original"><summary>查看说明原文 · v${Number(topic.brief?.revision || 1)}</summary><p>${escapeHtml(topic.brief?.original || '历史项目尚未保存说明原文')}</p></details><p>研究计划与执行记录保留在中间对话中。</p>`;
        ProjectOnboarding.editBrief(root,topic);
      } else if (pane === 'settings') {
        root.innerHTML = '<div id="topicCollaborationRoot"></div>';
        await renderTopicCollaboration(topic.id);
      } else if (pane === 'candidates' || pane === 'accepted') {
        ProjectLibrary.render(root,library,topic,{filter:pane});
      } else if (pane === 'outputs') {
        await ProjectMaterials.panel(root,topic,outputs);

      }
    } catch(error) { if(serial === panelSerial) root.innerHTML = errorState(error.message); }
  }
  function outputStatus(item) {
    const status = item.availability?.status;
    const label = {available:'引用当前可用',needs_review:'引用已失效 · 请重新生成',legacy_unverified:'历史成果 · 引用尚未完整核验'}[status];
    return label ? `<p role="status">${escapeHtml(label)}${item.availability?.reason ? `：${escapeHtml(item.availability.reason)}` : ''}</p>` : '';
  }
  async function mount(topic,pane) {
    const workspace = document.getElementById('topicWorkspace');
    if(workspace.dataset.activeProject !== String(topic.id) || !document.body.classList.contains('project-workbench-active')) setSidebarCollapsed(true,false);
    workspace.dataset.activeProject=String(topic.id);
    workspace.classList.add('pw-shell');
    const list = document.getElementById('projectTaskList');
    const rail = document.querySelector('.project-taskbench-nav');
    rail.innerHTML = `<label class="pw-switch">切换项目<select data-pw-project aria-label="切换项目">${state.topics.map(item=>`<option value="${item.id}"${item.id === topic.id ? ' selected' : ''}>${escapeHtml(item.title)}</option>`).join('')}</select></label><a class="primary-btn compact link-button" href="#/projects/${topic.id}/tasks?new=1">＋ 新研究</a><nav id="topicTabs" aria-label="项目工作面">${Object.entries(labels).filter(([key])=>key!=='settings').map(([key,label])=>`<button type="button" data-pw-panel="${key}" aria-expanded="false">${label}</button>`).join('')}</nav><details class="pw-history-wrap" ${window.matchMedia("(min-width:641px)").matches ? "open" : ""}><summary>对话历史</summary><div class="pw-history"></div></details><button type="button" class="pw-settings" data-pw-panel="settings" aria-expanded="false">设置与成员</button>`;
    rail.querySelector('.pw-history').append(list);
    document.querySelector('.project-chat-library')?.remove();
    document.getElementById('topicToolbar').innerHTML = `<a class="ghost-btn compact link-button" href="#/projects/mine" data-project-exit aria-label="返回我的项目" title="返回我的项目">←</a><span>${escapeHtml(topic.title)}</span>`;
    const contextToggle = document.querySelector('[data-project-context-toggle]');
    rail.querySelector('#topicTabs').hidden=true;
    if (contextToggle) {contextToggle.removeAttribute('data-project-context-toggle'); contextToggle.setAttribute('data-pw-panel','candidates');contextToggle.textContent = '项目内容';}
    closePanel(false);
    setupComposer();
    if (pane !== 'tasks') await openPanel(pane);
    else if(window.matchMedia('(min-width:1101px)').matches) await openPanel('candidates');

  }
  function syncTags(form, selected = null) {
    const root = form.querySelector('[data-pw-tags]');
    if (!root) return;
    const references = [...form.querySelectorAll('.project-reference-selectors select')].filter(input=>input.value);
    const skills = selected || [...form.querySelectorAll('[name="capability_config_ids"]:checked')].sort((a,b)=>Number(a.dataset.selectionOrder)-Number(b.dataset.selectionOrder));
    root.innerHTML = references.map(input=>`<button type="button" class="pw-selection-tag" data-pw-remove-reference="${input.name}" title="移除引用：${escapeHtml(input.selectedOptions[0].textContent)}">@ ${escapeHtml(input.selectedOptions[0].textContent)} <span aria-hidden="true">×</span></button>`).join('') + skills.map(input=>`<button type="button" class="pw-selection-tag" data-pw-remove-skill="${Number(input.value)}" title="移除 Skill：${escapeHtml(input.closest('label').querySelector('b').textContent)}"># ${escapeHtml(input.closest('label').querySelector('b').textContent)} <span aria-hidden="true">×</span></button>`).join('');
    const materials=[...form.querySelectorAll('[data-prompt-material]:checked')];
    root.innerHTML += materials.map(input=>`<button type="button" class="pw-selection-tag" data-pw-remove-material="${Number(input.dataset.promptMaterial)}" title="${escapeHtml(input.dataset.title)}">@ ${escapeHtml(input.dataset.title)} ×</button>`).join('');
    root.hidden = !references.length && !skills.length && !materials.length;
  }
  function setupComposer() {
    const form = document.querySelector('[data-project-task-form]');
    if (!form) return;
    form.querySelector('textarea').maxLength = 12000;
    const country = form.elements.country_reference;
    if (country && ![...country.options].some(option=>!option.value)) country.insertAdjacentHTML('afterbegin','<option value="">不指定引用</option>');
    const options = form.querySelector('.project-chat-options');
    const footer = form.querySelector('footer');
    footer.querySelector('span')?.remove();
    form.insertAdjacentHTML('afterbegin','<div class="pw-selected-tags" data-pw-tags hidden aria-live="polite"></div>');
    footer.prepend(options);
    options.insertAdjacentHTML('afterbegin','<select class="pw-chat-mode" name="research_mode" aria-label="研究模式"><option value="chat">对话</option><option value="plan">方向梳理 / 计划</option></select>');
    if(!form.elements.conversation_id.value) {
      form.elements.research_mode.value='plan';
      const welcome=document.querySelector('.project-chat-welcome');
      if(welcome) {
        welcome.querySelector('h3').textContent='先确定研究计划';
        welcome.querySelector('p').textContent='讨论目标与范围，确认计划后执行检索；从候选中选用资料，再据此写作周报。';
        welcome.querySelector('span').textContent='计划 → 检索 → 选材 → 写作';
      }
      const title=document.querySelector('[data-project-conversation-title]');
      if(title)title.textContent='新研究';
    }
    const contextSummary = form.querySelector('.project-chat-context summary');
    contextSummary.innerHTML = '<span>＋ 资料引用</span>';
    form.querySelector('.project-capability-picker summary > span').textContent = '# Skill';
    options.querySelectorAll('details').forEach(details=>{
      const content=document.createElement('div');
      content.className='pw-picker-content';
      [...details.children].filter(child=>child.tagName!=='SUMMARY').forEach(child=>content.append(child));
      details.append(content);
    });
    footer.insertAdjacentHTML('beforeend','<button type="button" class="ghost-btn compact" data-pw-stop hidden>停止</button>');
    form.querySelectorAll('[name="capability_config_ids"]').forEach(input=>input.setAttribute('aria-label',input.closest('label').querySelector('b').textContent));
    syncTags(form);
    ProjectOnboarding.setup(form);
    ProjectSkillUI.setup(form);
  }
  function setBusy(form,busy,ready=false) {
    const submit = form.querySelector('[type="submit"]');
    const stop = form.querySelector('[data-pw-stop]');
    if (!stop) return;
    submit.hidden = busy;
    stop.hidden = !busy;
    stop.disabled = busy && !ready;
    form.dataset.pwBusy = String(busy);
  }
  document.addEventListener('click',async event=>{
    const recommendation = event.target.closest('[data-pw-recommend]');
    if(recommendation) {
      const form = document.querySelector('[data-project-task-form]');
      closePanel(false);
      form.elements.research_mode.value='plan';
      form.elements.research_mode.dispatchEvent(new Event('change',{bubbles:true}));
      if(!form.elements.question.value.trim()) form.elements.question.value=recommendation.dataset.pwRecommend+'：'+state.topic.title;
      form.elements.question.focus();
    }
    const cancelDelete = event.target.closest('[data-pw-cancel-delete]');
    if(cancelDelete) {
      const entry=cancelDelete.closest('.pw-history-entry');
      const button=entry.querySelector('[data-pw-delete-task]');
      delete button.dataset.confirmDelete; button.textContent='删除';
      const actions=cancelDelete.closest('.pw-delete-actions');
      actions.before(button); actions.remove();
      entry.querySelector('.pw-delete-note').remove();
    }
    const deletion = event.target.closest('[data-pw-delete-task]');
    if(deletion) {
      if(deletion.dataset.confirmDelete !== 'true') {
        deletion.dataset.confirmDelete='true'; deletion.textContent='确认删除';
        deletion.insertAdjacentHTML('beforebegin','<p class="pw-delete-note">删除此对话？资料与成果会保留。</p><div class="pw-delete-actions"><button type="button" data-pw-cancel-delete>取消</button></div>');
        deletion.previousElementSibling.append(deletion);
        return;
      }
      deletion.disabled=true;
      try {
        await apiFetch(`/reader/research-cases/${state.topic.id}/tasks/${encodeURIComponent(deletion.dataset.pwDeleteTask)}`,{method:'DELETE'});
        if(document.querySelector('[data-project-task-form]')?.elements.conversation_id.value === deletion.dataset.pwDeleteTask) {
          state.projectConversationId='';
          state.projectObjectRoute=null;
          window.history.replaceState(null,'',`${location.pathname}${location.search}#/projects/${state.topic.id}/overview`);
          // A deleted active conversation must not be reused by navigation.
          document.querySelector('[data-project-task-form]')?.remove();
          await loadTopics(state.topic.id,'overview');
        } else await loadProjectTasks(state.topic.id);
        showToast('对话已删除，资料与成果已保留');
      } catch(error) {deletion.disabled=false;showToast(error.message);}
    }
    const expand=event.target.closest('[data-review-expand]');
    if(expand) {
      const wide=document.querySelector('.project-taskbench').classList.toggle('pw-review-wide');
      expand.textContent=wide?'返回侧栏':'展开筛选';
      expand.setAttribute('aria-expanded',String(wide));
    }
    const open=event.target.closest('[data-pw-panel]');
    if(open) {event.preventDefault();openPanel(open.dataset.pwPanel);}
    if(event.target.closest('[data-pw-close]')) closePanel();
    const form = event.target.closest('[data-project-task-form]');
    const material=event.target.closest('[data-pw-remove-material]');
    if(form && material){const input=form.querySelector(`[data-prompt-material="${Number(material.dataset.pwRemoveMaterial)}"]`);if(input){input.checked=false;input.dispatchEvent(new Event('change',{bubbles:true}));}}
    const ref = event.target.closest('[data-pw-remove-reference]');
    const skill = event.target.closest('[data-pw-remove-skill]');
    if (form && ref) { form.elements[ref.dataset.pwRemoveReference].value=''; syncTags(form); }
    if (form && skill) {
      const input = [...form.querySelectorAll('[name="capability_config_ids"]')].find(item=>item.value===skill.dataset.pwRemoveSkill);
      if (input) {input.checked=false;syncProjectCapabilitySelection(form,input);}
    }
    const stop = event.target.closest('[data-pw-stop]');
    if (form && stop) {
      stop.disabled=true;
      try {
        await apiFetch(`/reader/research-cases/${form.dataset.projectTaskForm}/tasks/${encodeURIComponent(form.elements.conversation_id.value)}/cancel`,{method:'POST'});
        showToast('已请求停止，本轮不会自动重启');
      } catch(error) {stop.disabled=false;showToast(`停止失败：${error.message}`);}
    }
  });
  document.addEventListener('change',event=>{
    if(event.target.matches('.project-reference-selectors select,[data-prompt-material]')) syncTags(event.target.closest('form'));
    if(event.target.matches('[data-pw-project]')) window.location.hash=`#/projects/${Number(event.target.value)}/overview`;
  });
  document.addEventListener('keydown',event=>{
    const panel=document.getElementById('projectContextPanel');
    if(event.key==='Escape' && document.body.classList.contains('project-workbench-active') && panel && !panel.hidden && !document.querySelector('dialog[open]')) {event.preventDefault();closePanel();}
  });
  return {mount,openPanel,closePanel,syncTags,setBusy,outputStatus,updateCounts};
})();
