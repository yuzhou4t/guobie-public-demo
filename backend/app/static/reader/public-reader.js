/* Public deployment integration; the existing Reader owns all navigation and research UI. */
(() => {
  const e = window.ReaderCore.escapeHtml;
  const api = (path, options = {}) => window.ReaderCore.apiFetch(`/reader/${path}`, options);
  const originalInit = window.ReaderAuth.init;
  let connection = {};
  function status(text, error = false) {
    const node = document.getElementById('publicConnectionStatus');
    if (node) { node.textContent = text; node.classList.toggle('is-error', error); }
  }
  function draw() {
    let section = document.getElementById('publicModelSettings');
    if (!section) {
      section = document.createElement('section'); section.id = 'publicModelSettings'; section.className = 'settings-section';
      document.querySelector('.reader-settings .settings-section')?.after(section);
    }
    section.innerHTML = `<h2>模型连接</h2><p>使用自己的 API 进行研究，连接仅在本次体验保留，最长一小时。</p>
      <form id="publicConnectionForm"><label>接口协议<select name="protocol"><option value="chat_completions">OpenAI 兼容 Chat Completions</option><option value="responses">OpenAI Responses</option></select></label>
      <label>Base URL<input type="url" name="base_url" required value="${e(connection.base_url || 'https://api.deepseek.com')}"></label>
      <label>模型名称<input name="model" required maxlength="150" value="${e(connection.model || 'deepseek-v4-flash')}"></label>
      <label>API Key<input type="password" name="api_key" required minlength="8" maxlength="1024" autocomplete="off"></label>
      <label class="public-api-consent"><input type="checkbox" name="consent" required>确认使用自己的额度向上述服务发送连接测试；主动运行研究时发送所选材料。</label>
      <div class="inline-actions"><button type="submit" class="primary-btn">测试并连接</button><button type="button" class="ghost-btn" data-api-disconnect>清除连接</button><button type="button" class="ghost-btn" data-deepseek>填入 DeepSeek 配置</button></div>
      <p id="publicConnectionStatus" role="status">${connection.connected ? `已连接 ${e(connection.model)}；${new Date(connection.expires_at * 1000).toLocaleTimeString()} 前有效` : '尚未连接 API；可先浏览现有资料与示例成果。'}</p>
      <p class="settings-count-note">DeepSeek 快捷配置使用 Flash 非思考模式，适用于本例短研究任务。未连接时不调用团队的订阅或公用密钥。</p></form>
      <details><summary>清空本次体验</summary><p>删除本会话的项目、草稿、研究记录和 API 凭据。请先导出需要的内容。</p><button type="button" class="ghost-btn" data-demo-reset>清空我的体验数据</button></details>`;
    const form = section.querySelector('form'); form.elements.protocol.value = connection.protocol || 'chat_completions';
    form.onsubmit = async event => {
      event.preventDefault(); const button = event.submitter; button.disabled = true; status('正在测试连接与结构化输出…');
      const payload = Object.fromEntries(new FormData(form)); payload.consent = true;
      try { connection = await api('model-connection/test', {method:'POST', body:JSON.stringify(payload)}); draw(); }
      catch (error) { status(error.message, true); }
      finally { form.elements.api_key.value = ''; button.disabled = false; }
    };
    section.querySelector('[data-api-disconnect]').onclick = async () => {
      try {await api('model-connection', {method:'DELETE'}); connection = {}; draw();} catch(error) {status(error.message,true);}
    };
    section.querySelector('[data-deepseek]').onclick = () => {
      form.elements.protocol.value='chat_completions'; form.elements.base_url.value='https://api.deepseek.com';
      form.elements.model.value='deepseek-v4-flash'; form.elements.consent.checked=false;
    };
    section.querySelector('[data-demo-reset]').onclick = async () => {
      if (!confirm('清空本会话的项目、草稿、记录和 API 凭据？请确认已导出需要的成果。')) return;
      try {await api('demo-session',{method:'DELETE'}); location.hash='#/countries'; location.reload();}
      catch(error) {status(error.message,true);}
    };
  }
  window.ReaderAuth.init = async () => {
    const result = await originalInit();
    if (!result.public_demo) return result;
    document.body.classList.add('public-demo');
    document.getElementById('readerAccountBar')?.remove();
    const info = [...document.querySelectorAll('.reader-settings .settings-section p')].find(p => p.textContent.includes('本机单用户'));
    if (info) info.textContent = '公开体验环境：沿用国别智枢原有页面和研究流程，数据为筛选后的现有样本。每位体验者的项目和 API 连接独立。';
    const link = document.createElement('link'); link.rel='stylesheet'; link.href='./public-reader.css'; document.head.append(link);
    try {connection = await api('model-connection');} catch(error) {console.warn('模型连接状态暂不可用');}
    draw();
    const banner = document.createElement('div'); banner.className='public-demo-notice';
    banner.innerHTML='公开体验 · 刚果（金）精选历史样本 <a href="#/settings">连接自己的 API</a>';
    document.querySelector('.workspace-canvas')?.prepend(banner);
    const researchNote = document.createElement('p');
    researchNote.className = 'public-sample-note';
    researchNote.textContent = '图表统计所选论文样本，支持按年份与研究领域查看；不代表全部研究或全球热度，当年数据尚不完整。';
    document.querySelector('#country-research > .section-heading')?.after(researchNote);
    return result;
  };
})();
