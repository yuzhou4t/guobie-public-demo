/* Persistent planning discussions; no retrieval is triggered by a planning turn. */
window.ProjectPlanUI = (() => {
  let recoveryTimer;
  function followSavedPlan(task,form) {
    clearTimeout(recoveryTimer);
    form.dataset.planPending=String(Boolean(task.plan_in_progress));
    ProjectWorkbench.setBusy(form,Boolean(task.plan_in_progress),true);
    if(!task.plan_in_progress)return;
    const startedAt=task.messages?.[task.messages.length-1]?.at;
    updatePlanProgress(form,{stage:'questions',message:'计划正在后台生成，离开页面不会停止处理。',started_at:startedAt,immediate:true});
    const poll=async()=>{
      if(!form.isConnected || form.elements.conversation_id.value!==task.id)return;
      try {
        const saved=await apiFetch(`/reader/research-cases/${form.dataset.projectTaskForm}/tasks/${encodeURIComponent(task.id)}`);
        if(!saved.plan_in_progress){render(saved,form);await loadProjectTasks(Number(form.dataset.projectTaskForm));return;}
        updatePlanProgress(form,{stage:'questions',message:'计划正在后台生成，离开页面不会停止处理。',started_at:startedAt});
      } catch { updatePlanProgress(form,{stage:'questions',message:'暂时无法读取状态，正在重连；请勿重复提交。',immediate:true}); }
      recoveryTimer=setTimeout(poll,2000);
    };
    recoveryTimer=setTimeout(poll,2000);
  }
  const defaultPublicProcess=[
    {stage:'scope',label:'理解范围',summary:'核对研究对象、时间范围与材料边界'},
    {stage:'questions',label:'整理计划',summary:'整理核心问题、成果要求与待确认事项'},
    {stage:'search',label:'撰写草稿',summary:'形成候选关键词、检索路径与资料分组'},
    {stage:'constraints',label:'检查约束',summary:'检查范围、证据条件与执行边界'},
  ];
  function updatePlanProgress(form,payload={}) {
    if(!form.isConnected) return;
    const thread=form.closest('.project-conversation').querySelector('.project-task-thread');
    const follow=thread.scrollHeight-thread.scrollTop-thread.clientHeight<100;
    let card=thread.querySelector('[data-plan-public-progress]');
    if(!card){
      card=document.createElement('article');
      card.className='is-running pw-public-process';
      card.dataset.planPublicProgress='';
      card.dataset.startedAt=String(Date.parse(payload.started_at) || Date.now());
      card.innerHTML='<header><strong><span class="pw-plan-spinner" aria-hidden="true"></span>正在梳理研究计划</strong><span data-plan-elapsed>0 秒</span></header><p data-plan-live-status role="status">正在提交研究要求…</p><ol></ol><div data-plan-stream-text aria-label="正在生成的计划草稿"><div class="pw-plan-skeleton" aria-hidden="true"><i></i><i></i><i></i></div></div><small>计划生成中，完成后可修改；尚未执行检索。</small>';
      thread.append(card);
    }
    const stage=payload.stage==='constraints' ? 'constraints' : card.dataset.hasPreview ? 'search' : payload.stage;
    const active=Math.max(0,defaultPublicProcess.findIndex(step=>step.stage===stage));
    card.querySelector('ol').innerHTML=defaultPublicProcess.map((step,index)=>`<li data-state="${index<active?'done':index===active?'current':'pending'}"><span>${escapeHtml(step.label)}</span></li>`).join('');
    card.querySelector('[data-plan-live-status]').textContent=card.dataset.hasPreview ? '正在撰写计划草稿…' : payload.message || '正在理解研究范围…';
    card.querySelector('[data-plan-elapsed]').textContent=`${Math.floor((Date.now()-Number(card.dataset.startedAt))/1000)} 秒`;
    if(follow || payload.immediate) thread.scrollTop=thread.scrollHeight;
  }
  function previewPlan(form,preview) {
    updatePlanProgress(form,{stage:'search'});
    const thread=form.closest('.project-conversation').querySelector('.project-task-thread');
    const follow=thread.scrollHeight-thread.scrollTop-thread.clientHeight<100;
    const card=thread.querySelector('[data-plan-public-progress]');
    if(!card)return;
    card.dataset.hasPreview='true';
    card.querySelector('[data-plan-live-status]').textContent='正在撰写计划草稿…';
    const root=card.querySelector('[data-plan-stream-text]');
    root.innerHTML=`${preview.summary ? `<p>${escapeHtml(preview.summary)}</p>` : ''}${[['steps','研究步骤'],['research_paths','可选路径'],['evidence_gaps','待核实事项']].map(([key,label])=>preview[key]?.length ? `<h4>${label}</h4><ol>${preview[key].map(text=>`<li>${escapeHtml(text)}</li>`).join('')}</ol>` : '').join('')}<span class="pw-stream-cursor" aria-hidden="true"></span>`;
    if(follow)thread.scrollTop=thread.scrollHeight;
  }
  function executionRecords(task) {
    const history=task.execution_history || [];
    if(!history.length)return task.plan?.confirmed_at ? '<p class="method-note">历史运行未记录检索统计。</p>' : '';
    return history.map(run=>`<article class="pw-execution-record"><strong>计划 v${Number(run.plan_revision)} · ${run.status==='completed'?'检索完成':run.status==='failed'?'部分检索未完成':'检索记录'}</strong><p>${Number(run.raw_count || 0)} 条匹配 → ${Number(run.unique_count || 0)} 条去重资料 · ${Number(run.existing_count || 0)} 条已在项目内 · ${Number(run.new_pending_count || 0)} 条新增待选</p><details><summary>查看检索过程与范围</summary><p>${escapeHtml(run.country_iso3)} · ${escapeHtml(String(run.published_from).slice(0,10))} 至 ${escapeHtml(String(run.published_to).slice(0,10))}</p><ol>${(run.queries || []).map(query=>`<li>${escapeHtml(query.label)} · ${escapeHtml(query.term)}：${Number(query.count)} 条匹配${query.at_limit?'（本次达到50条上限）':''}</li>`).join('')}</ol><small>仅记录公开的执行步骤和检索结果；采用状态以右侧资料库为准。</small></details>${(run.matches || []).length ? `<div class="pw-evidence-suggestions">${run.matches.filter(item=>item.object_type==='document_version').slice(0,5).map(item=>`<button type="button" data-project-evidence="${escapeHtml(item.object_type)}:${Number(item.object_id)}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.source_name || '')} · ${escapeHtml(item.published_at ? publishedLabel(item.published_at,item.published_at_precision) : '日期未声明')}</small><small>匹配：${item.terms.map(escapeHtml).join(' · ')} · 来源与书目信息，内容待核对</small></button>`).join('')}</div><button type="button" data-pw-panel="candidates">打开候选资料，选择并审阅</button>` : '<p>当前条件未命中资料。请修改下方关键词或时间范围后重新确认计划。</p>'}</article>`).join('');
  }
  function render(task, form) {
    if (!form?.isConnected || !form.elements.research_mode) return;
    form.dataset.planRevision = String(task.revision || 0);
    if(form.dataset.localPromptDraft !== 'true') form.elements.research_mode.value = task.plan ? 'plan' : 'chat';
    const thread = form.closest('.project-conversation').querySelector('.project-task-thread');
    const plan = task.plan;
    if(!form.dataset.referenceRestored && form.dataset.localPromptDraft !== 'true' && plan){
      const ids=new Set((plan.references || []).filter(ref=>ref.type==='document_version').map(ref=>String(ref.id)));
      form.querySelectorAll('[data-prompt-material]').forEach(input=>input.checked=ids.has(input.dataset.promptMaterial));
      form.dataset.referenceRestored='true';ProjectWorkbench.syncTags(form);
    }
    const entries = (task.messages || []).map(message=>({at:message.at,html:message.skill_turn ? ProjectSkillUI.messageHtml(message) : message.skill_note ? ProjectSkillUI.noteHtml(message) : `<article class="${message.role === 'user' ? 'is-user' : 'is-assistant'}"><strong>${message.role === 'user' ? '你' : '计划助手'}${message.plan_revision ? ` · 计划 v${Number(message.plan_revision)}` : ''}</strong><p>${escapeHtml(message.text)}</p>${Object.keys(message.answers || {}).length ? `<dl>${Object.entries(message.answers).map(([key,value])=>`<dt>${escapeHtml(({focus:"研究主题",period:"时间范围",output:"预期成果"})[key] || key)}</dt><dd>${escapeHtml(value)}</dd>`).join('')}</dl>` : ''}</article>`}));
    for(const turn of task.turns || []) {
      entries.push({at:turn.created_at,html:`<article class="is-user"><strong>你</strong><p>${escapeHtml(turn.question)}</p></article>`});
      const result = turn.status === 'succeeded' ? assistantResponseHtml({run_id:turn.run_id,artifact:turn.artifact || {}}) : `<p>${turn.status === 'running' ? '这轮对话仍在运行。' : '这轮对话未完成，原问题已保留。'}</p>`;
      entries.push({at:turn.finished_at || turn.updated_at || turn.created_at,html:`<article class="${turn.status === 'failed' ? 'is-error' : 'is-assistant'}"><strong>项目 AI</strong>${result}</article>`});
    }
    const timestamp=value=>Date.parse(value && !/(Z|[+-]\d{2}:\d{2})$/.test(value) ? value+'Z' : value) || 0;
    entries.sort((a,b)=>timestamp(a.at)-timestamp(b.at));
    thread.innerHTML=entries.map(entry=>entry.html).join('');
    if (plan) {
      const publicProcess=Array.isArray(plan.public_process) ? plan.public_process : [];
      thread.insertAdjacentHTML('beforeend', `<article class="pw-plan-card"><strong>${plan.planner === 'assistant' ? 'AI 研究计划' : '未调用模型 · 规则草案'} v${Number(plan.revision)} · ${plan.confirmed_at ? '已确认' : '尚未执行'}</strong><p>${escapeHtml(plan.summary)}</p>${publicProcess.length ? `<details class="pw-public-process-summary"><summary>公开过程摘要</summary><ol>${publicProcess.map(step=>`<li><span>${escapeHtml(step.label || '')}</span><small>${escapeHtml(step.summary || '')}</small></li>`).join('')}</ol><p>这些是可公开的工作步骤，不包含模型隐藏推理。</p></details>` : ''}<p>检索词：${(plan.search_terms || []).map(escapeHtml).join(" · ")}（可在下方输入修改意见）</p><ol>${(plan.steps || []).map(step=>`<li>${escapeHtml(step)}</li>`).join('')}</ol>${(plan.research_paths || []).length ? `<h4>可选研究路径</h4><ul>${plan.research_paths.map(x=>`<li>${escapeHtml(x)}</li>`).join('')}</ul>` : ''}${(plan.evidence_gaps || []).length ? `<h4>资料条件与缺口</h4><ul>${plan.evidence_gaps.map(x=>`<li>${escapeHtml(x)}</li>`).join('')}</ul>` : ''}${plan.prompt_snapshot ? `<details><summary>本轮提示词与输入版本</summary><p>${escapeHtml(plan.prompt_snapshot)}</p><small>概览 v${Number(plan.brief_snapshot?.revision || 1)} · ${(plan.references || []).length} 个选择的引用</small></details>` : ''}${plan.limitation ? `<p>${escapeHtml(plan.limitation)}</p>` : ''}<p>${plan.confirmed_at ? `本版计划已确认 · ${runStatusLabel(task.status)}` : plan.questions?.length ? '请补充下面的问题，再继续整理计划。' : '讨论已保存，检索仍需单独确认。'}</p></article>`);
    }
    thread.insertAdjacentHTML('beforeend',executionRecords(task));
    if(plan && ['review','completed'].includes(task.status)) thread.insertAdjacentHTML('beforeend','<div class="pw-next-actions"><button type="button" data-output-generate>根据已采用资料写作</button></div>');
    const card=thread.querySelector('.pw-plan-card');
    if(card)card.insertAdjacentHTML('beforeend',`<details class="pw-plan-refine"><summary>修改关键词、时间范围或成果要求</summary><label>研究重点<input data-plan-focus value="${escapeHtml(plan.answers?.focus || task.title)}" maxlength="500"></label><label>检索时间<select data-plan-period>${[1,3,5].map(n=>`<option value="近${n===1?'一':n===3?'三':'五'}年" ${Number(plan.years)===n?'selected':''}>近${n}年</option>`).join('')}</select></label><label>成果要求<input data-plan-outcome value="${escapeHtml(plan.output_type || '本周研究进展周报')}" maxlength="500"></label><button type="button" data-plan-refine>带入修改要求，重新梳理计划</button></details>`);
    if(card && !plan.questions?.length && ['awaiting_confirmation','failed','cancelled'].includes(task.status)) {
      card.insertAdjacentHTML('beforeend',`<button type="button" class="primary-btn compact" data-plan-execute="${Number(plan.revision)}">确认计划 v${Number(plan.revision)} 并检索</button><p>从国别空间数据库查询，结果先进入待审阅资料。</p>`);

    }
    let questions = form.querySelector('[data-plan-questions]');
    if (!questions) { questions=document.createElement('div');questions.dataset.planQuestions='';form.prepend(questions); }
    questions.hidden=false;
    form.elements.question.required=!(plan?.questions || []).length;
    questions.innerHTML = (plan?.questions || []).map((question,index)=>`<label>${escapeHtml(question.question)}<input type="text" data-plan-answer="${escapeHtml(question.id)}" maxlength="500" list="pw-plan-options-${index}" placeholder="选择建议或自由填写"><datalist id="pw-plan-options-${index}">${(question.options || []).map(option=>`<option value="${escapeHtml(option)}"></option>`).join('')}</datalist><span class="pw-plan-options">${(question.options || []).map(option=>`<button type="button" data-plan-option="${escapeHtml(option)}">${escapeHtml(option)}</button>`).join('')}</span></label>`).join('');
    if (task.error) thread.insertAdjacentHTML('beforeend',`<article class="is-error"><p>${escapeHtml(task.error)}</p></article>`);
    const title=document.querySelector('[data-project-conversation-title]');
    const meta=document.querySelector('[data-project-conversation-meta]');
    if(title) title.textContent=task.title;
    if(meta) meta.textContent=task.plan_in_progress ? '研究计划正在后台生成' : plan ? `计划讨论 · v${Number(task.revision)} · ${task.status === 'cancelled' ? '已停止' : task.status === 'failed' ? '未完成，可重试' : '已保存'}` : `${Number(task.turn_count)} 轮会话 · 已自动保存`;
    form.elements.research_mode.dispatchEvent(new Event('change',{bubbles:true}));
    const lastMessage=task.messages?.[task.messages.length-1];
    if(task.plan_in_progress || lastMessage?.operation_id || lastMessage?.plan_revision)followSavedPlan(task,form);
    else {clearTimeout(recoveryTimer);form.dataset.planPending='false';}
    ProjectSkillUI.mount(task,form);
    thread.scrollTop=thread.scrollHeight;
  }
  async function submit(form) {
    const question=form.elements.question.value.trim() || (form.querySelector('[data-plan-answer]') ? '按照所选范围继续完善计划。' : '');
    if (!question && !form.querySelector('[data-plan-answer]')) return;
    const answers={...JSON.parse(form.dataset.planEdits || '{}'),...Object.fromEntries([...form.querySelectorAll('[data-plan-answer]')].filter(input=>input.value.trim()).map(input=>[input.dataset.planAnswer,input.value.trim()]))};
    const caseId=Number(form.dataset.projectTaskForm);
    const button=form.querySelector('[type="submit"]');
    button.disabled=true;
    ProjectWorkbench.setBusy(form,true);
    form.closest('.project-conversation').querySelector('[data-plan-public-progress]')?.remove();
    const previousQuestions=form.querySelector('[data-plan-questions]');
    if(previousQuestions) previousQuestions.hidden=true;
    const thread=form.closest('.project-conversation').querySelector('.project-task-thread');
    const previousPlan=thread.querySelector('.pw-plan-card');
    const archived=document.createElement('details');archived.className='pw-submitted-plan';
    archived.innerHTML=`<summary>已提交本轮选择 · 展开查看计划与选择</summary><p>${escapeHtml(question)}</p><dl>${Object.entries(answers).map(([key,value])=>`<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join('')}</dl>`;
    if(previousPlan) {previousPlan.before(archived);archived.append(previousPlan);}
    else if(Object.keys(answers).length) thread.append(archived);
    updatePlanProgress(form,{stage:'scope',message:'正在提交研究要求…',immediate:true});
    const progressTimer=setInterval(()=>{const card=form.closest('.project-conversation')?.querySelector('[data-plan-public-progress]');if(card)card.querySelector('[data-plan-elapsed]').textContent=`${Math.floor((Date.now()-Number(card.dataset.startedAt))/1000)} 秒`;},1000);
    const references=[...form.querySelectorAll('.project-reference-selectors select')].filter(input=>input.value).map(input=>({type:({country_reference:"country",event_reference:"event",material_reference:"document_version",data_slice_reference:"data_slice"})[input.name],id:input.value,label:input.selectedOptions[0].textContent}));
    form.querySelectorAll('[data-prompt-material]:checked').forEach(input=>{if(!references.some(ref=>ref.type==='document_version' && Number(ref.id)===Number(input.dataset.promptMaterial))) references.push({type:'document_version',id:Number(input.dataset.promptMaterial),label:input.dataset.title});});
    const payload={mode:'plan',question,answers,references,conversation_id:form.elements.conversation_id.value || crypto.randomUUID(),expected_revision:Number(form.dataset.planRevision || 0),request_id:crypto.randomUUID(),capability_config_ids:syncProjectCapabilitySelection(form).map(input=>Number(input.value)),online_mode:'off'};
    let completed=false;
    let retainInput=false;
    function attach(taskId) {
      ProjectOnboarding.bindTask(form,taskId);
      form.elements.conversation_id.value=taskId;
      state.projectObjectRoute={kind:'task',id:taskId};
      window.history.replaceState(null,'',`${location.pathname}${location.search}#/projects/${caseId}/tasks/${taskId}`);
    }
    try {
      await streamAssistantRequest(payload,(name,envelope)=>{
        if(name==='run.started') {
          ProjectOnboarding.bindTask(form,envelope.conversation_id);
          form.elements.conversation_id.value=envelope.conversation_id;
          state.projectObjectRoute={kind:'task',id:envelope.conversation_id};
          window.history.replaceState(null,'',`${location.pathname}${location.search}#/projects/${caseId}/tasks/${envelope.conversation_id}`);
          ProjectWorkbench.setBusy(form,true,true);
          const thread=form.closest('.project-conversation').querySelector('.project-task-thread');
          thread.querySelector('.project-chat-welcome')?.remove();
          updatePlanProgress(form,envelope.payload);
        }
        if(name==='project.progress') updatePlanProgress(form,envelope.payload);
        if(name==='project.plan.preview') previewPlan(form,envelope.payload);
        if(name==='project.task') { clearInterval(progressTimer);attach(envelope.payload.id);render(envelope.payload,form);completed=true;retainInput=['failed','cancelled'].includes(envelope.payload.status) || Boolean(envelope.payload.plan?.limitation); }
      },`/reader/research-cases/${caseId}/tasks/stream`);
      if(!completed) throw new Error('连接已结束，尚未收到计划结果。刷新可读取已保存进度。');
      if (completed && !retainInput) { form.elements.question.value='';localStorage.removeItem(form.dataset.promptDraftKey); }
      delete form.dataset.planEdits;
      await loadProjectTasks(caseId);
    } catch(error) {
      clearInterval(progressTimer);
      try {
        const saved=await apiFetch(`/reader/research-cases/${caseId}/tasks/${payload.conversation_id}`);
        attach(saved.id);
        render(saved,form);
        await loadProjectTasks(caseId);
        showToast(saved.plan_in_progress?'页面连接已重连，计划仍在后台生成。':'已重新读取保存的计划，请核对结果。');
      } catch { showToast(`计划未完成：${error.message}。输入内容仍保留。`); }
    }
    finally {
      clearInterval(progressTimer);
      const card=form.closest('.project-conversation')?.querySelector('[data-plan-public-progress]');
      if(card && form.dataset.planPending!=='true'){card.classList.remove('is-running');card.querySelector('[data-plan-live-status]').textContent='生成已结束；请核对已保存结果，原输入仍保留。';card.querySelector('.pw-plan-skeleton')?.remove();card.querySelector('.pw-stream-cursor')?.remove();}
      if(!completed || retainInput) {
        const questions=form.querySelector('[data-plan-questions]');
        if(questions) {questions.hidden=false;questions.querySelectorAll('[data-plan-answer]').forEach(input=>{if(answers[input.dataset.planAnswer])input.value=answers[input.dataset.planAnswer];});}
        if(archived.isConnected && previousPlan) {archived.before(previousPlan);archived.remove();}
      }
      button.disabled=form.dataset.planPending==='true';ProjectWorkbench.setBusy(form,form.dataset.planPending==='true',true);
    }
  }
  document.addEventListener('click',async event=>{
    const refine=event.target.closest('[data-plan-refine]');
    if(refine) {
      const area=refine.closest('.pw-plan-refine');
      const form=document.querySelector('[data-project-task-form]');
      const edits={focus:area.querySelector('[data-plan-focus]').value,period:area.querySelector('[data-plan-period]').value,output:area.querySelector('[data-plan-outcome]').value};
      form.dataset.planEdits=JSON.stringify(edits);
      form.elements.research_mode.value='plan';
      form.elements.research_mode.dispatchEvent(new Event('change',{bubbles:true}));
      if(!form.elements.question.value.trim())form.elements.question.value=`请按以下范围修订计划：${edits.focus}；${edits.period}；${edits.output}。请给出简短中英文检索词。`;
      form.elements.question.focus();return;
    }
    const option=event.target.closest('[data-plan-option]');
    if(option) {option.closest('label').querySelector('input').value=option.dataset.planOption;return;}
    const trigger=event.target.closest('[data-plan-execute]');
    if(!trigger) return;
    const form=trigger.closest('.project-conversation').querySelector('[data-project-task-form]');
    const caseId=Number(form.dataset.projectTaskForm);
    const taskId=form.elements.conversation_id.value;
    const button=form.querySelector('[type="submit"]');
    trigger.disabled=true;button.disabled=true;ProjectWorkbench.setBusy(form,true);
    let completed=false;
    try {
      await streamAssistantRequest({idempotency_key:crypto.randomUUID()},(name,envelope)=>{
        if(name==='run.started') ProjectWorkbench.setBusy(form,true,true);
        if(name==='project.progress') {
          document.querySelector('[data-project-conversation-meta]').textContent=envelope.payload.message;
          const thread=form.closest('.project-conversation').querySelector('.project-task-thread');
          let log=thread.querySelector('[data-live-execution]');
          if(!log){thread.insertAdjacentHTML('beforeend','<article class="is-running" data-live-execution><strong>执行计划 · 库内检索</strong><ol></ol></article>');log=thread.querySelector('[data-live-execution]');}
          log.querySelector('ol').insertAdjacentHTML('beforeend',`<li>${escapeHtml(envelope.payload.message)}</li>`);
          thread.scrollTop=thread.scrollHeight;
        }
        if(name==='project.task') {render(envelope.payload,form);completed=true;}
      },`/reader/research-cases/${caseId}/tasks/${taskId}/plans/${trigger.dataset.planExecute}/execute`);
      if(!completed) throw new Error('未收到检索结果');
      await loadProjectTasks(caseId);
      if(window.matchMedia('(min-width:1101px)').matches)await ProjectWorkbench.openPanel('candidates');
    } catch(error) {
      try {render(await apiFetch(`/reader/research-cases/${caseId}/tasks/${taskId}`),form);} catch {}
      showToast(`检索未完成：${error.message}。已保存内容可继续查看。`);
    } finally {trigger.disabled=false;button.disabled=false;ProjectWorkbench.setBusy(form,false);}
  });
  document.addEventListener('change',event=>{
    if(event.target.name !== 'research_mode') return;
    const form=event.target.closest('form');
    const questions=form.querySelector('[data-plan-questions]');
    if(questions) questions.hidden=event.target.value !== 'plan';
    form.elements.question.required=event.target.value !== 'plan' || !form.querySelector('[data-plan-answer]');
    form.elements.question.maxLength=['plan','write'].includes(event.target.value) ? 12000 : 500;
    form.querySelector('[type=submit]').textContent=event.target.value === 'plan' ? '开始梳理' : '发送';
    form.elements.question.placeholder=event.target.value === 'plan' ? '说明研究目标或补充修改意见，发送后仅整理计划…' : event.target.value === 'write' ? '修改写作要求，点击发送后开始生成成果草稿…' : '继续问当前项目…';
  });
  return {render,submit};
})();
