/* Skills execute inside the current project conversation; the server owns each turn. */
window.ProjectSkillUI = (() => {
  const esc = value => escapeHtml(String(value ?? ''));
  const names = {s05:'区域国别访谈与田野材料整理',s03:'对象国政策与现实动态追踪'};
  const api = (path, body) => apiFetch(`/reader/skill-workflows${path}`, body === undefined ? {} : {method:'POST',body:JSON.stringify(body)});
  const split = value => String(value || '').split(/[;；,，\n]/).map(x=>x.trim()).filter(Boolean);
  const busy = turn => ['queued','running','pausing'].includes(turn.status);
  let pollTimer, elapsedTimer;
  function field(name,label,value='',type='text') { return `<label>${label}<input name="${name}" type="${type}" value="${esc(value)}"></label>`; }
  function setup(form) {
    form.querySelector('footer').insertAdjacentHTML('beforeend','<button type="button" class="ps-pause primary-btn compact" data-ps-pause hidden aria-label="暂停处理" title="暂停处理"><span class="ps-spinner" aria-hidden="true"></span><span aria-hidden="true">■</span></button><button type="button" class="ghost-btn compact" data-ps-resume hidden>继续处理</button>');
    form.querySelector('[data-ps-pause]').onclick=()=>pause(form);
    form.querySelector('[data-ps-resume]').onclick=()=>resume(form);
    form.elements.question.addEventListener('input',()=>syncComposer(form));
    const choices=form.querySelector('.project-capability-choices');
    if(!choices || choices.querySelector('[data-project-skill]'))return;
    choices.insertAdjacentHTML('afterbegin',`<div class="ps-skill-choices">${Object.entries(names).map(([kind,name])=>`<button type="button" data-project-skill="${kind}"><span class="ps-skill-icon">${kind.toUpperCase()}</span><span><b>${name}</b><small>${kind==='s05'?'提交访谈材料，整理文本与研究标注':'设置来源与主题，核对更新和版本差异'}</small></span><span aria-hidden="true">＋</span></button>`).join('')}</div>`);
    choices.querySelectorAll('[data-project-skill]').forEach(button=>button.onclick=()=>open(form,button.dataset.projectSkill));
    const picker=form.querySelector('.project-capability-picker');
    picker.querySelector('.pw-picker-content > p').textContent='选择上方 Skill，提交资料后在本会话中处理。';
    choices.querySelectorAll(':scope > p').forEach(p=>p.remove());
  }
  async function open(form,kind,configId=null,supplement=null) {
    if(form.dataset.psBusy==='true' || form.dataset.pwBusy==='true') {showToast('请等待当前会话完成，再使用 Skill。');return;}
    const caseId=Number(form.dataset.projectTaskForm);
    const conversationId=form.elements.conversation_id.value || crypto.randomUUID();
    form.querySelector('.project-capability-picker').open=false;
    const modal=document.createElement('dialog');modal.className='ps-dialog';
    modal.innerHTML=`<header><div><small>${kind.toUpperCase()} · 当前项目会话</small><h2>${names[kind]}</h2></div><button type="button" data-ps-close aria-label="关闭 Skill 输入">×</button></header><p>正在读取当前项目的材料…</p>`;
    document.body.append(modal);modal.showModal();
    modal.querySelector('[data-ps-close]').onclick=()=>modal.close();modal.addEventListener('close',()=>modal.remove(),{once:true});
    try {
      const [context,materialList]=await Promise.all([api('/context'),api(`/cases/${caseId}/materials`)]);
      if(!modal.isConnected)return;
      const configs=context.configs.filter(c=>c.kind===kind && c.research_case_id===caseId);
      const materials=materialList.items;
      modal.querySelector('p').remove();
      modal.insertAdjacentHTML('beforeend',`<form class="ps-input"><p class="ps-project-label">归属项目：${esc(state.topic?.title || '当前项目')} · 结果返回本会话</p><label>使用已有配置（可选）<select name="existing"><option value="">本轮新建</option>${configs.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select></label><label>本轮标题<input name="title" required maxlength="150" value="${names[kind]} · ${esc(new Date().toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}))}"></label>
        ${kind==='s03'?`<div class="ps-fields">${field('country_iso3','国家代码（或填写研究区域）',state.topic?.scope?.country_iso3 || '')}${field('regions','研究区域')}${field('topic_name','追踪主题')}${field('keywords','关键词或别名（分号分隔）')}</div><details class="ps-options"><summary>指定来源与时间范围</summary><label>平台已配置来源<select name="source_ids" multiple size="4">${context.sources.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></label><div class="ps-sources"></div><button type="button" data-add-source>添加公开文件链接</button><div class="ps-fields">${field('date_from','起始日期','','date')}${field('date_to','截止日期','','date')}</div></details>`:''}
        <fieldset class="ps-materials"><legend>选择本项目已有材料</legend>${materials.map(m=>`<label class="ps-material"><input type="checkbox" name="material_ids" value="${m.id}"><span><b>${esc(m.title)}</b><small>${esc(m.context?.sample_note || m.original_filename)}</small></span></label>`).join('') || '<p>还没有材料，请在下方上传或粘贴文本。</p>'}</fieldset>
        <details class="ps-options" ${materials.length?'':'open'}><summary>上传新材料或粘贴文本</summary><div class="ps-dropzone" data-ps-dropzone><label>拖放文件到这里，或点击选择<input name="files" type="file" multiple accept=".txt,.md,.docx,.pdf,.jpg,.jpeg,.png"></label><small>支持 TXT、DOCX、MD、PDF、JPG、PNG，每个文件不超过 10 MB</small></div><div data-ps-files></div><p data-ps-file-status role="status"></p><label>或者粘贴原始文本<textarea name="text" rows="4" placeholder="保留原文，不需要预先整理成标注格式"></textarea></label>${field('material_title','材料标题（粘贴文本时使用）')}<div class="ps-fields">${field('material_regions','材料研究区域（独立于项目）')}${field('speaker_role','受访者角色')}${field('captured_on','采集日期（可留空）','','date')}${field('location','采集地点（可留空）')}</div>${field('sample_note','材料说明（样稿节选请注明）')}<p>新材料默认仅负责人和研究成员可见；缺失的时间、地点保持待补。</p><label class="ps-check"><input type="checkbox" name="authorized">我确认可在本项目中使用提交的材料</label></details>
        ${kind==='s05'?'<label>处理要求<textarea name="instruction" rows="2">保留原意和省略号，整理访谈文本，提取完整语义片段与候选研究标注；不替研究者裁决。</textarea></label>':''}
        <p class="ps-input-status" role="status"></p><footer><span>确认后回到会话，处理完成后在会话中审阅。</span><button class="primary-btn" type="submit">确定并开始处理</button></footer></form>`);
      const input=modal.querySelector('form');
      const chosenFiles=new Map();
      const fileKey=file=>`${file.name}:${file.size}:${file.lastModified}`;
      const dropzone=input.querySelector('[data-ps-dropzone]');
      function showFiles() {
        input.querySelector('[data-ps-files]').innerHTML=[...chosenFiles].map(([key,file])=>`<div class="ps-selected-file"><span>✓ ${esc(file.name)} · ${Math.ceil(file.size/1024)} KB</span><button type="button" data-ps-remove-file="${esc(key)}" aria-label="移除 ${esc(file.name)}">移除</button></div>`).join('');
      }
      function chooseFiles(files) {
        const errors=[];
        for(const file of files) {
          if(!/\.(txt|md|docx|pdf|jpg|jpeg|png)$/i.test(file.name)) {errors.push(`${file.name}：不支持此格式`);continue;}
          if(file.size>10*1024*1024) {errors.push(`${file.name}：超过 10 MB`);continue;}
          if(chosenFiles.size >= (kind==='s05'?10:15) && !chosenFiles.has(fileKey(file))) {errors.push('已达到本轮文件数量上限');continue;}
          chosenFiles.set(fileKey(file),file);
        }
        showFiles();
        input.querySelector('[data-ps-file-status]').textContent=errors.join('；') || `已选 ${chosenFiles.size} 个文件，确认后上传并用于本轮处理。`;
      }
      input.elements.files.onchange=()=>{chooseFiles(input.elements.files.files);input.elements.files.value='';};
      for(const eventName of ['dragenter','dragover']) dropzone.addEventListener(eventName,event=>{event.preventDefault();dropzone.classList.add('is-dragging');});
      dropzone.addEventListener('dragleave',event=>{if(!dropzone.contains(event.relatedTarget))dropzone.classList.remove('is-dragging');});
      dropzone.addEventListener('drop',event=>{event.preventDefault();dropzone.classList.remove('is-dragging');chooseFiles(event.dataTransfer.files);});
      input.addEventListener('click',event=>{const remove=event.target.closest('[data-ps-remove-file]');if(remove){chosenFiles.delete(remove.dataset.psRemoveFile);showFiles();input.querySelector('[data-ps-file-status]').textContent=`已选 ${chosenFiles.size} 个文件。`;}});
      const requestId=crypto.randomUUID();let savedId=null;const uploaded=new Map();
      function loadConfig(id) {
        const selected=configs.find(c=>c.id===Number(id));savedId=selected?.id || null;
        if(!selected)return;
        const c=selected.config;input.elements.title.value=selected.name;
        input.querySelectorAll('[name="material_ids"]').forEach(el=>el.checked=(c.material_ids || []).includes(Number(el.value)));
        if(kind==='s05')input.elements.instruction.value=c.instruction || '';
        else {
          for(const key of ['country_iso3','topic_name','date_from','date_to'])input.elements[key].value=c[key] || '';
          for(const key of ['regions','keywords'])input.elements[key].value=(c[key] || []).join('；');
          [...input.elements.source_ids.options].forEach(o=>o.selected=(c.source_ids || []).includes(Number(o.value)));
          input.querySelector('.ps-sources').innerHTML=(c.sources || []).map(SkillWorkflows.sourceRow).join('');
        }
      }
      input.elements.existing.onchange=e=>loadConfig(e.target.value);
      if(configId){input.elements.existing.value=String(configId);loadConfig(configId);}
      if(supplement){input.elements.files.closest('details').open=true;input.elements.material_title.value=`补充原文 · ${supplement.title}`;input.elements.sample_note.value='原文件未覆盖；补充文本需人工核对并确认关联。';}
      input.querySelector('[data-add-source]')?.addEventListener('click',()=>input.querySelector('.ps-sources').insertAdjacentHTML('beforeend',SkillWorkflows.sourceRow()));
      input.addEventListener('click',e=>{if(e.target.closest('.wf-remove-source'))e.target.closest('.wf-source').remove();});
      input.onsubmit=async event=>{
        event.preventDefault();const button=event.submitter,status=input.querySelector('.ps-input-status');button.disabled=true;
        try {
          if(!form.isConnected || form.elements.conversation_id.value && form.elements.conversation_id.value!==conversationId)throw new Error('会话已切换，请在目标会话重新选择 Skill。');
          const d=new FormData(input);const selected=d.getAll('material_ids').map(Number);
          const files=[...chosenFiles.values()];const pasted=String(d.get('text') || '').trim();
          if((files.length || pasted) && !input.elements.authorized.checked)throw new Error('请确认提交材料的使用范围。');
          if(kind==='s05' && !selected.length && !files.length && !pasted)throw new Error('请选择材料、上传文件或粘贴文本。');
          if(files.length+selected.length+(pasted?1:0)>(kind==='s05'?10:15))throw new Error('本轮材料过多，请分批处理。');
          status.textContent='正在保存本轮输入…';
          const uploads=[...files.map(f=>({key:`${f.name}:${f.size}:${f.lastModified}`,name:f.name,type:f.type,file:f})),...(pasted?[{key:`text:${d.get('text')}`,name:'访谈文本.txt',type:'text/plain',text:String(d.get('text'))}]:[])];
          for(const item of uploads){
            if(!uploaded.has(item.key)){
              if(item.file?.size>10*1024*1024)throw new Error('单个文件请控制在 10 MB 内。');
              const bytes=item.file?new Uint8Array(await item.file.arrayBuffer()):new TextEncoder().encode(item.text);
              let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
              const m=await api('/materials',{research_case_id:caseId,title:item.file?item.name:String(d.get('material_title') || d.get('title')),filename:item.name,content_type:item.type || 'application/octet-stream',content_base64:btoa(binary),material_type:/image\//.test(item.type || '')?'photo':kind==='s05'?'interview_transcript':'supporting_document',privacy_level:'restricted',sensitivity:'unknown',authorization_confirmed:true,captured_on:d.get('captured_on') || null,context:{regions:split(d.get('material_regions')),speaker_role:d.get('speaker_role'),location:d.get('location'),sample_note:d.get('sample_note'),language:kind==='s05'?'zh':'fr',usage_scope:'当前项目内部研究'}});
              uploaded.set(item.key,m.id);
            }
            selected.push(uploaded.get(item.key));
          }
          const config={workflow_version:1,material_ids:[...new Set(selected)]};
          if(kind==='s05')config.instruction=d.get('instruction');
          else Object.assign(config,{country_iso3:String(d.get('country_iso3')).trim().toUpperCase() || null,regions:split(d.get('regions')),topic_name:d.get('topic_name'),keywords:split(d.get('keywords')),date_from:d.get('date_from') || null,date_to:d.get('date_to') || null,source_ids:d.getAll('source_ids').map(Number),sources:[...input.querySelectorAll('.wf-source')].map(row=>({url:row.querySelector('[name="url"]').value,title:row.querySelector('[name="title"]').value,source_name:row.querySelector('[name="source_name"]').value,published_at:row.querySelector('[name="published_at"]').value || null,article:row.querySelector('[name="article"]').value || null,compare_content:row.querySelector('[name="compare_content"]').checked,language:'fr'}))});
          const saved=await api('/configs',{kind,research_case_id:caseId,config_id:savedId,name:String(d.get('title')),config});savedId=saved.id;
          const task=await api(`/cases/${caseId}/turns`,{conversation_id:conversationId,request_id:requestId,config_id:savedId,title:d.get('title')});
          modal.close();ProjectWorkbench.closePanel(false);ProjectOnboarding.bindTask(form,task.id);form.elements.conversation_id.value=task.id;
          ProjectOnboarding.finishSkillInput(form);
          state.projectConversationId=task.id;state.projectObjectRoute={kind:'task',id:task.id};
          window.history.replaceState(null,'',`${location.pathname}${location.search}#/projects/${caseId}/tasks/${task.id}`);
          ProjectPlanUI.render(task,form);await loadProjectTasks(caseId);
        }catch(error){status.textContent=error.message;status.classList.add('is-error');}finally{button.disabled=false;}
      };
    }catch(error){modal.insertAdjacentHTML('beforeend',`<p class="is-error">${esc(error.message)}</p>`);}
  }
  function messageHtml(message) {
    const t=message.skill_turn;
    return `<article class="is-assistant ps-turn" data-project-skill-turn="${esc(t.request_id)}"><header><span class="ps-skill-icon">${esc(t.kind.toUpperCase())}</span><div><strong>${esc(names[t.kind])}</strong><p>${esc(t.title)}</p></div><span class="ps-turn-state"></span></header><div class="ps-progress" role="status" aria-live="polite"></div><div class="ps-result"></div></article>`;
  }
  function progress(node,turn) {
    const active=busy(turn), failed=turn.status==='failed', paused=turn.status==='paused';
    node.classList.toggle('is-processing',active);
    node.querySelector('.ps-turn-state').textContent=turn.status==='pausing'?'正在暂停':active?'正在处理':paused?'已暂停':failed?'未完成':'候选结果';
    node.querySelector('.ps-progress').innerHTML=active?`<div class="ps-processing-line"><span class="ps-spinner" aria-hidden="true"></span><b>${turn.status==='pausing'?'正在结束当前处理，输入会保留':turn.status==='queued'?'输入已保存，正在准备调用 Skill':turn.events[turn.events.length-1]?.label || (turn.kind==='s05'?'正在调用田野访谈 Skill，整理文本与研究标注':'正在调用专题追踪 Skill，获取来源与核对变化')}</b></div><p>已选择 ${Number(turn.material_count)} 份材料 · <span data-skill-elapsed="${esc(turn.started_at || turn.events[0].at)}"></span> · 可以留在这里，也可稍后返回。</p>`:failed?`<p class="ps-error">${esc(turn.error || '处理未完成，请重试。')}</p>`:'<p>处理完成。以下是本轮候选结果，确认后可保存到项目。</p>';
    node.querySelector('.ps-progress').insertAdjacentHTML('beforeend',`<details class="ps-run-events" ${active || failed ? 'open' : ''}><summary>公开处理步骤</summary><ol>${turn.events.map(e=>`<li><time>${esc(new Date(e.at).toLocaleTimeString('zh-CN',{hour12:false}))}</time> ${esc(e.label)}</li>`).join('')}</ol></details>`);
    if(paused)node.querySelector('.ps-progress').innerHTML='<p>已暂停。本轮未确认内容不保存，输入和补充要求已保留；继续时重新处理。</p>';
  }
  async function result(node,turn,form) {
    const root=node.querySelector('.ps-result');
    if(busy(turn))return;
    if(['failed','paused'].includes(turn.status)) {root.innerHTML=`<button type="button" class="ghost-btn" data-retry-skill>检查输入并重试</button><button type="button" class="ghost-btn" data-resume-skill>按原输入继续处理</button>`;root.querySelector('[data-retry-skill]').onclick=()=>open(form,turn.kind,turn.config_id);root.querySelector('[data-resume-skill]').onclick=()=>resume(form);return;}
    if(!turn.run_id || root.dataset.loaded===String(turn.run_id))return;
    root.dataset.loaded=String(turn.run_id);root.innerHTML='<p>正在读取本轮结果…</p>';
    try{
      const run=await api(`/runs/${turn.run_id}`);if(!root.isConnected)return;
      const fieldRun=turn.kind==='s05';
      const accessFailures=(run.output.observations || []).filter(o=>o.snapshot.access_status!=='accessible').length;
      const extractionFailures=(run.output.observations || []).filter(o=>o.snapshot.extraction_status==='extraction_failed').length;
      root.innerHTML=fieldRun?`<div class="ps-result-summary"><b>已整理 ${run.output.material_ids.length} 份材料</b><span>${run.segments.length} 个候选语义片段</span><span>原文与整理稿分别保留</span></div><div class="ps-preview">${run.segments.slice(0,3).map(s=>`<section><div><b>${esc(s.annotation.themes.join(' · '))}</b><small>${esc(s.annotation.statement_type)}</small></div><blockquote>${esc(s.original_text)}</blockquote><p>${esc(s.annotation.viewpoint)}</p></section>`).join('')}</div>`:`<div class="ps-result-summary"><b>${accessFailures?'追踪完成，部分来源未能访问':run.output.no_updates?'本轮记录没有新增或变化':'本轮追踪完成'}</b><span>新增 ${run.output.counts.new} · 更新 ${run.output.counts.updated} · 未变化 ${run.output.counts.unchanged} · 范围外 ${run.output.counts.excluded}</span>${accessFailures || extractionFailures?`<span>访问失败 ${accessFailures} · 正文提取失败 ${extractionFailures}，请展开核验。</span>`:""}</div><div class="ps-preview">${run.output.observations.slice(0,4).map(o=>`<section><b>${esc(o.snapshot.title)}</b><p>${esc(o.snapshot.source_name)} · ${esc(o.snapshot.published_at || '发布日期待核验')}</p></section>`).join('')}</div>`;
      if(fieldRun && run.output.limitations?.length)root.insertAdjacentHTML('beforeend',`<details class="ps-run-events" open><summary>处理说明与待审阅事项（${run.output.limitations.length}）</summary><ul>${run.output.limitations.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>`);
      root.insertAdjacentHTML('beforeend',`<details class="ps-review"><summary>${fieldRun?`查看全部 ${run.segments.length} 个片段，修改与确认`:'查看全部材料、差异与处理决定'}</summary><div class="ps-review-body"></div></details><div class="ps-result-actions">${fieldRun?'<button type="button" class="ghost-btn" data-skill-original>对照原文与整理稿</button>':''}<button type="button" class="ghost-btn" data-skill-project>查看项目成果</button><button type="button" class="ghost-btn" data-repeat-skill>再次使用此 Skill</button></div>`);
      root.addEventListener('workflow:retry',()=>open(form,turn.kind,turn.config_id));root.addEventListener('workflow:supplement',e=>open(form,turn.kind,turn.config_id,e.detail.snapshot));
      const review=root.querySelector('.ps-review');review.addEventListener('toggle',()=>{if(review.open&&!review.dataset.loaded){review.dataset.loaded='true';SkillWorkflows.renderRun(turn.run_id,review.querySelector('.ps-review-body')).catch(e=>{review.dataset.loaded='';review.querySelector('.ps-review-body').textContent=e.message;});}});
      root.querySelector('[data-skill-original]')?.addEventListener('click',()=>SkillWorkflows.showMaterial(run.output.material_ids[0]));
      root.querySelector('[data-skill-project]').textContent=fieldRun?'打开研究备忘录':'打开本轮简报';root.querySelector('[data-skill-project]').onclick=()=>ProjectMaterials.openWorkflow(turn.run_id);
      root.querySelector('[data-repeat-skill]').onclick=()=>open(form,turn.kind,turn.config_id);
    }catch(error){root.dataset.loaded='';root.innerHTML=`<p>${esc(error.message)}</p>`;}
  }
  function mount(task,form) {
    clearTimeout(pollTimer);clearInterval(elapsedTimer);
    if(!form?.isConnected)return;
    const turns=(task.messages || []).filter(m=>m.skill_turn).map(m=>m.skill_turn);
    if(!turns.length)return;
    const thread=form.closest('.project-conversation').querySelector('.project-task-thread');
    const active=turns.some(busy);form.dataset.psBusy=String(active);
    form.dataset.psState=(turns.find(busy) || turns[turns.length-1]).status;
    form.dataset.psKind=turns[turns.length-1].kind;
    form.dataset.psMessageCount=String((task.messages || []).length);
    syncComposer(form);
    for(const turn of turns){const node=[...thread.querySelectorAll('[data-project-skill-turn]')].find(n=>n.dataset.projectSkillTurn===turn.request_id);if(node){progress(node,turn);result(node,turn,form);}}
    const tick=()=>thread.querySelectorAll('[data-skill-elapsed]').forEach(el=>{const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(el.dataset.skillElapsed))/1000));el.textContent=`已用时 ${seconds} 秒`;});tick();
    if(active){
      elapsedTimer=setInterval(()=>{if(form.isConnected)tick();else clearInterval(elapsedTimer);},1000);
      pollTimer=setTimeout(async()=>{
        if(!form.isConnected || form.elements.conversation_id.value!==task.id)return;
        try{const updated=await apiFetch(`/reader/research-cases/${form.dataset.projectTaskForm}/tasks/${encodeURIComponent(task.id)}`);if(String(updated.messages.length)!==form.dataset.psMessageCount)ProjectPlanUI.render(updated,form);else mount(updated,form);if(!updated.messages.some(m=>m.skill_turn && busy(m.skill_turn))){await loadProjectTasks(Number(form.dataset.projectTaskForm));}}catch(error){const status=thread.querySelector('.is-processing .ps-progress');if(status)status.textContent='暂时无法读取进度，输入已保存。正在重新连接…';if(form.isConnected)pollTimer=setTimeout(()=>mount(task,form),3000);}
      },2000);
    }
  }
  function noteHtml(message) {
    const label={queued:'补充已排队；本轮完成后处理，暂停时保留待继续',applied:'补充已进入下一轮处理',recorded:'已记入会话；调整下轮追踪配置时参考'}[message.skill_note.status];
    return `<article class="is-user ps-note"><strong>你</strong><p>${esc(message.text)}</p><small>${esc(label)}</small></article>`;
  }
  function handlesInput(form) {return ['queued','running','pausing','paused','failed'].includes(form.dataset.psState);}
  function syncComposer(form) {
    const state=form.dataset.psState,active=['queued','running','pausing'].includes(state),canResume=['paused','failed'].includes(state);
    const pauseButton=form.querySelector('[data-ps-pause]'),resumeButton=form.querySelector('[data-ps-resume]'),submit=form.querySelector('[type="submit"]');
    if(!pauseButton)return;
    const typed=Boolean(form.elements.question.value.trim());
    pauseButton.hidden=!active;pauseButton.disabled=state==='pausing';resumeButton.hidden=!canResume;
    if(active || canResume){
      form.querySelector('[data-pw-stop]').hidden=true;submit.hidden=!typed;submit.disabled=false;
      submit.textContent=form.dataset.psKind==='s03'?'记录补充':'发送补充';
      form.elements.question.placeholder=active?'可补充要求，本轮完成后进入下一轮；也可先暂停再补充…':'补充要求后继续处理，或检查原始输入…';
    } else if(form.dataset.pwBusy!=='true') {submit.hidden=false;submit.disabled=false;submit.textContent=form.elements.research_mode.value==='plan'?'开始梳理':'发送';form.elements.question.placeholder='继续问当前项目…';}
  }
  function taskPath(form,action) {return `/cases/${form.dataset.projectTaskForm}/tasks/${encodeURIComponent(form.elements.conversation_id.value)}/${action}`;}
  async function pause(form) {
    const button=form.querySelector('[data-ps-pause]');button.disabled=true;
    try{ProjectPlanUI.render(await api(taskPath(form,'pause'),{}),form);}catch(error){button.disabled=false;showToast(error.message);}
  }
  async function resume(form) {
    if(form.dataset.psResuming==='true')return;
    if(form.elements.question.value.trim()){showToast('请先发送补充内容，再继续处理。');return;}
    form.dataset.psResuming='true';form.dataset.psResumeId ||= crypto.randomUUID();
    try{const task=await api(taskPath(form,'resume'),{request_id:form.dataset.psResumeId});delete form.dataset.psResumeId;ProjectPlanUI.render(task,form);await loadProjectTasks(Number(form.dataset.projectTaskForm));}catch(error){showToast(error.message);}finally{form.dataset.psResuming='false';}
  }
  async function submitSupplement(form) {
    const text=form.elements.question.value.trim();if(!text)return;
    const submit=form.querySelector('[type="submit"]');submit.disabled=true;
    if(form.dataset.psNoteText!==text){form.dataset.psNoteText=text;form.dataset.psNoteId=crypto.randomUUID();}
    try{const task=await api(taskPath(form,'messages'),{request_id:form.dataset.psNoteId,text});form.elements.question.value='';form.elements.question.dispatchEvent(new Event('input',{bubbles:true}));delete form.dataset.psNoteId;delete form.dataset.psNoteText;ProjectPlanUI.render(task,form);}catch(error){showToast(error.message);}finally{submit.disabled=false;}
  }
  return {setup,open,messageHtml,noteHtml,mount,handlesInput,submitSupplement};
})();
