/* Human-authored brief and visible prompt; no model calls on selection or navigation. */
window.ProjectOnboarding = (() => {
  let candidates = [];
  let createRequest = null;
  function reset() {
    candidates = [];
    document.getElementById('newTopicOptional')?.removeAttribute('open');
    createRequest = crypto.randomUUID();
    for (const id of ['newTopicPurpose','newTopicOverview']) document.getElementById(id).value='';
    document.getElementById('newTopicOutcome').value='';
    document.getElementById('newTopicPeriod').value='近三年';
    document.getElementById('newTopicMaterialOptions').innerHTML='<p>可先创建项目，之后添加资料。</p>';
  }
  function seed(item) {
    document.getElementById('newTopicTitle').value=item.direction_title || item.title;
    document.getElementById('newTopicQuestion').value=item.research_question;
    document.getElementById('newTopicPurpose').value=item.research_question;
    document.getElementById('newTopicPeriod').value=({'1':'近一年','3':'近三年','5':'近五年'})[document.getElementById('topicFrontierYears').value];
    candidates=item.papers || [];
    document.getElementById('newTopicMaterialOptions').innerHTML=candidates.map(p=>`<label><input type="checkbox" data-onboarding-material="${Number(p.document_version_id)}"><span>${escapeHtml(p.title)}<small>${escapeHtml(p.source_name || '')}</small></span></label>`).join('') || '<p>暂无候选论文，可以先建项。</p>';
    syncTopicSubmitState();
  }
  function payload() {
    const question=document.getElementById('newTopicQuestion').value.trim();
    const purpose=document.getElementById('newTopicPurpose').value.trim() || question;
    const period=document.getElementById('newTopicPeriod').value;
    return {idempotency_key:createRequest,brief_original:document.getElementById('newTopicOverview').value.trim() || question,
      brief_confirmed:{purpose,scope:`${document.getElementById('topicCountrySelect').value} · ${period}`,period,outcome:document.getElementById('newTopicOutcome').value.trim() || '待讨论'},
      candidate_document_version_ids:candidates.map(p=>Number(p.document_version_id)),
      selected_document_version_ids:[...document.querySelectorAll('[data-onboarding-material]:checked')].map(input=>Number(input.dataset.onboardingMaterial))};
  }
  function prompt(topic, form) {
    const b=topic.brief?.confirmed || {};
    return `请协助梳理以下研究方向，主题、研究范围与材料采用由我决定。\n\n主题：${topic.title}\n核心问题：${topic.research_question || '尚未确定，请与我讨论'}\n目的：${b.purpose || topic.research_question || '待讨论'}\n范围：${b.scope || topic.scope?.country_iso3 || '待补充'}\n初步概览（人工原稿）：${topic.brief?.original || '尚未填写，请仅提出建议'}\n期望成果：${b.outcome || '待讨论'}\n所选材料：${[...(form?.querySelectorAll('[data-prompt-material]:checked') || [])].map(input=>input.dataset.title).join('；') || '尚未指定，先梳理研究问题与资料缺口'}\n\n请输出：方向概览、问题拆解、可选研究路径、资料条件与缺口、下一步建议。事实必须标明已选材料来源；没有证据时标注待核实，不编造结论。丰富内容仅作为候选，不替我决定主题，不改写原稿。第一步整理可审阅的研究计划，检索和成果保存仍由我确认。`;
  }
  function setup(form) {
    const topic=state.topic;
    form.dataset.autoPrompt=prompt(topic,form);
    const key=`project-prompt-draft:${topic.id}:${form.elements.conversation_id.value || 'new'}`;
    form.dataset.promptDraftKey=key;
    let saved=null;
    try { saved=JSON.parse(localStorage.getItem(key) || 'null'); } catch {}
    form.dataset.localPromptDraft=String(Boolean(saved));
    const revision=Number(topic.brief?.revision || 1);
    const documents=topic.documents || [];
    form.querySelector('.project-chat-context .pw-picker-content').insertAdjacentHTML('beforeend',`<fieldset class="prompt-material-list"><legend>本轮材料 · 可多选</legend>${documents.map(doc=>`<label><input type="checkbox" data-prompt-material="${Number(doc.document_version_id)}" data-title="${escapeHtml(doc.title)}"${state.routeQuery.has('prompt') ? ' checked' : ''}><span>${escapeHtml(doc.title)}</span></label>`).join('') || '<p>在项目资料库采用材料后，可在此选择。</p>'}</fieldset>`);
    form.insertAdjacentHTML('afterbegin',`<div class="project-prompt-header"><button type="button" class="ghost-btn compact" data-compose-research-prompt>组装研究提示词</button><span data-prompt-note>主题与概览 v${revision} · 可编辑后运行</span></div>`);
    form.querySelector('[data-compose-research-prompt]').onclick=()=>{
      form.elements.research_mode.value='plan';
      form.elements.research_mode.dispatchEvent(new Event('change',{bubbles:true}));
      form.elements.question.value=prompt(state.topic,form);save();form.elements.question.focus();
    };
    function save(){localStorage.setItem(form.dataset.promptDraftKey,JSON.stringify({text:form.elements.question.value,mode:form.elements.research_mode.value,revision,
      materials:[...form.querySelectorAll('[data-prompt-material]:checked')].map(input=>input.dataset.promptMaterial),
      selectors:Object.fromEntries([...form.querySelectorAll('.project-reference-selectors select')].map(input=>[input.name,input.value]))}));}
    form.addEventListener('change',event=>{if(event.target.matches('[data-prompt-material],.project-reference-selectors select,select[name="research_mode"]')) save();});
    if(saved){
      form.querySelectorAll('[data-prompt-material]').forEach(input=>input.checked=(saved.materials || []).includes(input.dataset.promptMaterial));
      for(const [name,value] of Object.entries(saved.selectors || {})){const input=form.elements[name];if(input && [...input.options].some(option=>option.value===value)) input.value=value;}
    }
    form.elements.question.addEventListener('input',save);
    if(saved?.text){form.elements.question.value=saved.text;form.elements.research_mode.value=saved.mode || 'plan';if(saved.revision!==revision)form.querySelector('[data-prompt-note]').textContent='概览已改变，请重新组装并核对提示词';}
    else if(!form.elements.conversation_id.value) {form.elements.question.value=prompt(topic,form);form.elements.research_mode.value='plan';}
    form.elements.research_mode.dispatchEvent(new Event('change',{bubbles:true}));
    ProjectWorkbench.syncTags(form);
  }
  function bindTask(form,id) {
    const previous=form.dataset.promptDraftKey;
    const next=`project-prompt-draft:${form.dataset.projectTaskForm}:${id}`;
    if(previous && previous!==next){const value=localStorage.getItem(previous);if(value)localStorage.setItem(next,value);localStorage.removeItem(previous);}
    form.dataset.promptDraftKey=next;
  }
  function refreshMaterials(form,topic) {
    if(!form) return;
    const selected=new Set([...form.querySelectorAll('[data-prompt-material]:checked')].map(i=>i.dataset.promptMaterial));
    const root=form.querySelector('.prompt-material-list');
    if(root) root.innerHTML='<legend>本轮材料 · 可多选</legend>'+ (topic.documents || []).map(doc=>`<label><input type="checkbox" data-prompt-material="${Number(doc.document_version_id)}" data-title="${escapeHtml(doc.title)}"${selected.has(String(doc.document_version_id)) ? ' checked' : ''}><span>${escapeHtml(doc.title)}</span></label>`).join('');
    const promptNote=form.querySelector('[data-prompt-note]');
    if(promptNote) promptNote.textContent='项目资料已更新；选择本轮材料后，重新核对提示词。';
  }
  async function editBrief(root,topic) {
    const b=topic.brief?.confirmed || {};
    root.insertAdjacentHTML('afterbegin',`<details class="project-brief-editor"><summary>编辑概览 / AI 丰富后采用</summary><form data-brief-edit><label>人工原稿<textarea name="original" maxlength="6000" required>${escapeHtml(topic.brief?.original || topic.research_question)}</textarea></label><label>研究目的<textarea name="purpose" maxlength="2000" required>${escapeHtml(b.purpose || topic.research_question)}</textarea></label><label>范围<input name="scope" maxlength="2000" required value="${escapeHtml(b.scope || topic.scope?.country_iso3)}"></label><label>期望成果<input name="outcome" maxlength="1000" value="${escapeHtml(b.outcome || '待讨论')}"></label><button type="button" data-brief-suggest>AI 丰富建议</button><button type="submit">保存人工确认版本</button><div data-brief-candidate></div><p role="status"></p></form></details>`);
    const form=root.querySelector('[data-brief-edit]'),status=form.querySelector('[role=status]');
    const data=()=>({...Object.fromEntries(new FormData(form)),expected_revision:topic.brief?.revision || 1});
    form.querySelector('[data-brief-suggest]').onclick=async event=>{
      event.target.disabled=true;status.textContent='正在整理候选，原稿保持可见…';
      try {const result=await apiFetch(`/reader/research-cases/${topic.id}/brief/suggestion`,{method:'POST',body:JSON.stringify(data())});const candidate=form.querySelector('[data-brief-candidate]');candidate.innerHTML=`<p>${result.mode==='assistant'?'AI 候选 · 编辑后采用':'未调用模型 · 当前原稿'}</p>${['purpose','scope','outcome'].map(k=>`<label>${({purpose:'研究目的',scope:'范围',outcome:'成果'})[k]}<textarea data-brief-value="${k}">${escapeHtml(result.suggestion[k])}</textarea></label>`).join('')}<button type="button" data-adopt-brief>将候选放入编辑区</button>`;candidate.querySelector('button').onclick=()=>{candidate.querySelectorAll('[data-brief-value]').forEach(i=>form.elements[i.dataset.briefValue].value=i.value);status.textContent='候选已放入编辑区，点击保存后才成为确认版本。';};status.textContent='候选尚未保存。';}catch(error){status.textContent=error.message;}finally{event.target.disabled=false;}
    };
    form.onsubmit=async event=>{event.preventDefault();event.stopPropagation();const button=form.querySelector('[type=submit]');button.disabled=true;try{topic.brief=await apiFetch(`/reader/research-cases/${topic.id}/brief`,{method:'PATCH',body:JSON.stringify(data())});topic.research_question=topic.brief.confirmed.purpose;status.textContent='已保存。范围已改变，下一轮请重新组装提示词并确认计划。';}catch(error){status.textContent=error.message;}finally{button.disabled=false;}};
  }
  function finishSkillInput(form) {
    if(form.elements.question.value!==form.dataset.autoPrompt)return;
    form.elements.question.value='';
    form.elements.research_mode.value='chat';
    form.dataset.localPromptDraft='false';
    form.elements.question.dispatchEvent(new Event('input',{bubbles:true}));
    form.elements.research_mode.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return {reset,seed,payload,setup,editBrief,refreshMaterials,bindTask,finishSkillInput};
})();
