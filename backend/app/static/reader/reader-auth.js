/* Session credentials stay in HttpOnly cookies; invitation tokens stay in fragments. */
window.ReaderAuth = (() => {
  let status = null;
  const e = value => window.ReaderCore.escapeHtml(value);
  const api = (path, body) => window.ReaderCore.apiFetch(`/reader/auth/${path}`, body ? {method:'POST', body:JSON.stringify(body)} : {});
  function panel() {
    let dialog=document.getElementById('readerAccountDialog');
    if(!dialog) { dialog=document.createElement('dialog'); dialog.id='readerAccountDialog'; dialog.className='field-dialog'; document.body.append(dialog); }
    return dialog;
  }
  function draw(mode) {
    const dialog=panel(), invited=location.hash.startsWith('#/invite/');
    if(mode==='manage') {
      dialog.innerHTML=`<h2>平台账号</h2><p>${e(status.user.display_name)} · ${e(status.user.email)}</p>${status.user.role==='admin'?'<button data-account-invite>邀请成员</button>':''}<button data-account-logout>退出登录</button><button data-account-close>关闭</button>`;
      dialog.querySelector('[data-account-invite]')?.addEventListener('click',()=>draw('invite'));
      dialog.querySelector('[data-account-close]').onclick=()=>dialog.close();
      dialog.querySelector('[data-account-logout]').onclick=async()=>{await api('logout',{});sessionStorage.removeItem(window.ReaderCore.READER_KEY_STORAGE_KEY);location.reload();};
      if(!dialog.open)dialog.showModal(); return;
    }
    mode ||= invited ? 'accept'  : status?.initialized ? 'login' : 'setup';
    dialog.innerHTML=`<form id="readerAccountForm"><h2>${{setup:'建立负责人账号',login:'登录国别智枢',accept:'接受平台邀请',invite:'邀请成员'}[mode]}</h2><p>${mode==='setup'?'建立账号后，已有资料归属保留；访问项目和田野资料须登录。':mode==='invite'?'生成链接后由你复制分发，邀请三天内有效。':''}</p>${mode!=='accept'?'<label>邮箱<input name="email" type="email" autocomplete="username" required></label>':''}${['setup','invite'].includes(mode)?'<label>显示名称<input name="display_name" required maxlength="120"></label>':''}${mode!=='invite'?'<label>密码<input name="password" type="password" minlength="12" maxlength="256" autocomplete="'+(mode==='login'?'current-password':'new-password')+'" required placeholder="至少 12 个字符"></label>':''}<p role="alert" id="readerAccountError"></p><footer><button type="submit" class="primary-btn">${mode==='invite'?'生成邀请链接':mode==='login'?'登录':'确认建立账号'}</button><button type="button" class="ghost-btn" data-account-close>关闭</button></footer><div id="readerInvitationResult"></div></form>`;
    dialog.querySelector('[data-account-close]').onclick=()=>dialog.close();
    dialog.querySelector('form').onsubmit=async event=>{
      event.preventDefault(); const button=event.submitter; button.disabled=true;
      const values=Object.fromEntries(new FormData(event.currentTarget));
      if(mode==='accept') values.token=location.hash.slice('#/invite/'.length);
      try {
        const result=await api({accept:'accept-invitation',invite:'invitations'}[mode]||mode,values);
        if(mode==='invite') {
          const root=dialog.querySelector('#readerInvitationResult');
          root.innerHTML='<label>邀请链接<input readonly id="readerInvitationUrl"></label><button type="button" id="readerCopyInvitation">复制链接</button>';
          root.querySelector('input').value=result.invitation_url;
          root.querySelector('button').onclick=async()=>{ await navigator.clipboard.writeText(result.invitation_url); root.querySelector('button').textContent='已复制'; };
        } else { dialog.close(); if(mode==='accept') history.replaceState(null,'','#/countries'); location.reload(); }
      } catch(error) { dialog.querySelector('#readerAccountError').textContent=error.message; }
      finally { button.disabled=false; }
    };
    if(!dialog.open) dialog.showModal();
  }
  async function init() {
    status=await api('status');
    let bar=document.getElementById('readerAccountBar');
    if(status.demo_available&&!status.user) {
      bar?.remove();
      if(location.hash.startsWith('#/invite/')) draw();
      return status;
    }
    if(!bar) {bar=document.createElement('div');bar.id='readerAccountBar'; document.getElementById('appHeader')?.append(bar); if(!bar.isConnected)document.body.append(bar);}
    bar.innerHTML=`<button data-account-login>${status.user?'账号':status.initialized?'登录':'建立账号'}</button>`;
    bar.querySelector('[data-account-login]').onclick=()=>draw(status.user?'manage':undefined);
    if(location.hash.startsWith('#/invite/') || (status.initialized&&!status.user&&!status.demo_available)) draw();
    return status;
  }
  return {init, open:draw, get status(){return status;}};
})();
