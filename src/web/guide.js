/* First-run progress lives on the backend, never in browser storage. */
const GUIDE = {state:null, step:1, initialized:false, busy:false, wechatSession:0, modelDirty:false, channelsDirty:false};

function guideMessage(message) { $('#guide-feedback').textContent = message || ''; }

async function refreshOnboarding() {
  GUIDE.state = await api('/api/onboarding');
  if (!GUIDE.initialized) {
    GUIDE.step = GUIDE.state.step;
    $('#guide-qq').checked = GUIDE.state.qqEnabled;
    $('#guide-wechat').checked = GUIDE.state.wechatEnabled;
    const llm = STATE.config.llm;
    $('#guide-model-base').value = llm.primary_base || GUIDE.state.defaultBase;
    $('#guide-model-name').value = llm.primary_model || GUIDE.state.defaultModel;
    $('#guide-provider').value = String(llm.primary_base||'').replace(/\/$/,'') === GUIDE.state.defaultBase && llm.primary_model === GUIDE.state.defaultModel ? 'deepseek' : 'custom';
    GUIDE.initialized = true;
    showGuideProvider(false);
  }
  if(!GUIDE.channelsDirty){$('#guide-qq').checked=GUIDE.state.qqEnabled;$('#guide-wechat').checked=GUIDE.state.wechatEnabled;}
  $('#setup-resume').hidden = GUIDE.state.completed;
  $('#guide-key-hint').textContent = GUIDE.state.keyConfigured ? '已保存 API Key；留空继续使用。更换提供方时请填写对应的新 Key。' : '只保存到本机配置，不写入聊天记录。不要把 Key 发给机器人。';
  if (!GUIDE.busy && !GUIDE.modelDirty) $('#guide-model-status').textContent = GUIDE.state.modelVerified ? '✓ 当前模型测试通过' : (GUIDE.state.keyConfigured ? '已保存 Key，请测试当前模型' : '还未测试');
  renderGuide();
  return GUIDE.state;
}

function showGuideProvider(useDefault=true) {
  const deepseek = $('#guide-provider').value === 'deepseek';
  $('#guide-model-custom').hidden = deepseek;
  $('#guide-model-default').hidden = !deepseek;
  if (deepseek && useDefault) {
    $('#guide-model-base').value = 'https://api.deepseek.com';
    $('#guide-model-name').value = 'deepseek-flash';
  }
}

function guideStepSelected(step) {
  return step !== 3 && step !== 4 || (step === 3 ? GUIDE.state.qqEnabled : GUIDE.state.wechatEnabled);
}

function renderGuide() {
  if (!GUIDE.state) return;
  $$('[data-guide-panel]').forEach(el => el.hidden = Number(el.dataset.guidePanel) !== GUIDE.step);
  $$('[data-guide-step]').forEach(el => {
    const step = Number(el.dataset.guideStep);
    el.classList.toggle('active', step === GUIDE.step);
    el.classList.toggle('done', step < GUIDE.step);
    el.classList.toggle('skipped', !guideStepSelected(step));
    if (step === GUIDE.step) el.setAttribute('aria-current','step'); else el.removeAttribute('aria-current');
  });
  $('#btn-guide-back').hidden = GUIDE.step === 1;
  $('#btn-guide-next').textContent = GUIDE.step === 5 ? '完成配置并启动' : '下一步';
  const qq = !!STATE.qqStatus?.loggedIn;
  $('#guide-review').innerHTML = [
    ['模型', GUIDE.state.modelVerified && !GUIDE.modelDirty ? '✓ 连接测试通过' : '需保存并测试当前模型'],
    ['QQ', !GUIDE.state.qqEnabled ? '未选择' : (qq ? '✓ 已在线' : '需启动网关并登录')],
    ['微信 iLink', !GUIDE.state.wechatEnabled ? '未选择' : (GUIDE.state.wechatLoggedIn ? '✓ 已登录' : '需扫码登录')]
  ].map(([name,text])=>`<li><strong>${esc(name)}</strong> · ${esc(text)}</li>`).join('');
  if (GUIDE.state.wechatLoggedIn) $('#guide-wechat-status').textContent = '✓ 微信凭证已保存，可以进入下一步';
}

async function guideMove(step) {
  GUIDE.state = await api('/api/onboarding', {method:'PATCH',body:JSON.stringify({step,dismissed:false})});
  GUIDE.step = step;
  guideMessage(''); renderGuide();
}

async function guideNext() {
  if (GUIDE.busy) return;
  GUIDE.busy=true; $('#btn-guide-next').disabled=true;
  try {
    if (GUIDE.step===1) {
      const qq=$('#guide-qq').checked, wechat=$('#guide-wechat').checked;
      if (!qq&&!wechat) {guideMessage('请选择 QQ、微信或两个都选。');return;}
      const result=await api('/api/config/section/channels',{method:'PATCH',body:JSON.stringify({qq_enabled:qq,wechat_enabled:wechat})});
      STATE.config=result.config; GUIDE.channelsDirty=false; await loadConfig(); await refreshOnboarding();
    } else if (GUIDE.step===2) {
      await refreshOnboarding();
      if (!GUIDE.state.modelVerified || GUIDE.modelDirty) {guideMessage('请先点击「保存并测试模型」，通过后再继续。');return;}
    } else if (GUIDE.step===3 && GUIDE.state.qqEnabled) {
      const st=await refreshQQStatus();
      if (!st?.loggedIn) {guideMessage('请先启动网关并完成 QQ 登录；扫码后还需在手机确认。');return;}
    } else if (GUIDE.step===4 && GUIDE.state.wechatEnabled) {
      await refreshOnboarding();
      if (!GUIDE.state.wechatLoggedIn) {guideMessage('请先完成微信扫码，等待登录确认。');return;}
    } else if (GUIDE.step===5) {
      guideMessage('正在检查配置并启动机器人…');
      const result=await api('/api/onboarding/complete',{method:'POST',body:JSON.stringify({start:true})});
      if (!result.ok) {guideMessage(result.message);return;}
      await refreshOnboarding();await refreshStatus();switchPage('overview');toast(result.message,'ok');return;
    }
    let step=GUIDE.step+1;
    while (step<5&&!guideStepSelected(step)) step++;
    await guideMove(step);
    if(step===3) await refreshQQStatus();
  } catch(e) {guideMessage('本步骤未完成：'+e.message);}
  finally {GUIDE.busy=false;$('#btn-guide-next').disabled=false;}
}

async function saveGuideModel() {
  if (GUIDE.busy) return;
  GUIDE.busy=true;$('#btn-guide-model').disabled=true;
  const fields=['guide-provider','guide-model-base','guide-model-name','guide-model-key'].map(id=>$('#'+id));fields.forEach(el=>el.disabled=true);
  const label=$('#guide-model-status');label.textContent='正在保存并测试…';
  try {
    const base=$('#guide-model-base').value.trim(), model=$('#guide-model-name').value.trim(), key=$('#guide-model-key').value.trim();
    if (!key && (!GUIDE.state.keyConfigured || base.replace(/\/$/,'') !== String(STATE.config.llm.primary_base||'').replace(/\/$/,''))) throw new Error('请填写此提供方对应的完整 API Key');
    const body={primary_base:base,primary_model:model};if(key)body.primary_key=key;
    const saved=await api('/api/config/section/primary',{method:'PATCH',body:JSON.stringify(body)});
    STATE.config=saved.config;$('#guide-model-key').value='';
    const tested=await api('/api/llm/test',{method:'POST'});
    label.textContent=tested.message;
    if(tested.ok){GUIDE.modelDirty=false;guideMessage('模型已连接，点击下一步。');}
    else guideMessage(tested.message);
    await loadConfig();await refreshOnboarding();
  } catch(e){label.textContent='测试未通过：'+e.message;guideMessage(e.message);}
  finally{GUIDE.busy=false;$('#btn-guide-model').disabled=false;fields.forEach(el=>el.disabled=false);}
}

async function guideWechatQR() {
  if ($('#btn-guide-wechat').disabled) return;
  const session=++GUIDE.wechatSession, btn=$('#btn-guide-wechat'), text=$('#guide-wechat-status');
  btn.disabled=true;text.textContent='正在获取微信二维码…';
  try {
    const status=await api('/api/ilink/status');
    if(status.loggedIn){text.textContent='✓ 微信已登录，无需重复扫码';await refreshOnboarding();return;}
    const r=await api('/api/login/qrcode',{method:'POST'});
    if(!r.ok||!r.image){text.textContent=r.message||'二维码未准备好，请重试';return;}
    const img=new Image();img.alt='微信 iLink 登录二维码';img.src=r.image.startsWith('data:')||r.image.startsWith('http')?r.image:'data:image/png;base64,'+r.image;await img.decode();
    if(session!==GUIDE.wechatSession)return;
    $('#guide-wechat-qr').replaceChildren(img);$('#guide-wechat-qr').hidden=false;
    text.textContent='请用手机微信扫码并确认';
    pollGuideWechat(session);
  }catch(e){text.textContent='获取失败：'+e.message;}
  finally{btn.disabled=false;}
}
async function pollGuideWechat(session) {
  const deadline=Date.now()+3*60*1000;
  while(session===GUIDE.wechatSession&&Date.now()<deadline&&STATE.page==='guide') {
    try {
      const r=await api('/api/login/check');
      if(session!==GUIDE.wechatSession)return;
      $('#guide-wechat-status').textContent=r.message||'等待手机微信确认';
      if(r.status==='confirmed') {await refreshOnboarding();$('#guide-wechat-status').textContent=GUIDE.state.wechatLoggedIn?'✓ 微信凭证已保存，可以进入下一步':'手机已确认，但凭证未保存，请重新扫码';return;}
      if(r.status==='failed'){return;}
    }catch(e){if(session===GUIDE.wechatSession)$('#guide-wechat-status').textContent='查询暂未完成，正在重试…';}
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  if(session===GUIDE.wechatSession&&STATE.page==='guide')$('#guide-wechat-status').textContent='登录等待超时，请重新获取二维码';
}

function reportQQProgress(message) {
  for (const id of ['home-qq-status','guide-qq-status','qq-detect-hint']) $('#'+id).textContent=message;
}
function renderHomeQQ(st) {
  if(!st)return;
  let text=st.task?.running?(st.task.message||'网关正在启动…'):(st.loggedIn?'QQ 已在线'+(st.messageReceiverStarted?' · 机器人已启动':' · 机器人待启动'):(st.napcat?.running?'网关已运行，等待 QQ 登录':'网关尚未运行，点击右侧按钮即可启动'));
  if(st.loggedIn&&(st.reverseWsConnected||st.messageTransport==='http_post'))text+=' · 消息链路已到达';
  $('#home-qq-status').textContent=text;
  if(!qqStartPending&&!STATE.qqLoginPending)$('#guide-qq-status').textContent=text;
  renderGuide();
}

async function finishQQLogin() {
  stopQQLoginPolling();$('#qq-login-modal').hidden=true;$('#qq-choice-modal').hidden=true;
  if(STATE.config.adapter.napcat_webui_token)await syncQQWsUrl();
  await loadConfig();await refreshStatus();
  if(STATE.status?.running&&!STATE.status.qq_running){const r=await api('/api/bot/restart',{method:'POST'});if(!r.ok)reportQQProgress(r.message);}
  await refreshQQStatus();await refreshOnboarding();
  reportQQProgress('QQ 已登录，网关可继续复用');
}

async function beginQQLogin() {
  const accounts=await loadQQAccounts();
  if(STATE.qqAutoLoginQq&&accounts.some(a=>a.qq===STATE.qqAutoLoginQq)) {
    const ok=await quickLoginQQ({qq:STATE.qqAutoLoginQq,remember:false});if(ok)return;
  }
  if(accounts.length) {
    $('#qq-choice-account').innerHTML=accounts.map(a=>`<option value="${esc(a.qq)}">${esc(a.nickname||'QQ')} · ${esc(a.qq)}</option>`).join('');
    $('#qq-choice-status').textContent='选择账号快速登录，或使用二维码。';$('#qq-choice-modal').hidden=false;
  } else await getQQQr();
}

function bindOnboarding() {
  $('#btn-home-qq').addEventListener('click',()=>setupQQ({saveForm:false}));
  $('#btn-guide-qq').addEventListener('click',()=>setupQQ({saveForm:false}));
  $('#btn-guide-next').addEventListener('click',guideNext);
  $('#btn-guide-back').addEventListener('click',async()=>{if(GUIDE.busy)return;let step=GUIDE.step-1;while(step>2&&!guideStepSelected(step))step--;try{await guideMove(step);}catch(e){guideMessage(e.message);}});
  $('#btn-guide-later').addEventListener('click',async()=>{try{GUIDE.state=await api('/api/onboarding',{method:'PATCH',body:JSON.stringify({dismissed:true,step:GUIDE.step})});GUIDE.wechatSession++;switchPage('overview');$('#setup-resume').hidden=false;}catch(e){guideMessage(e.message);}});
  for(const id of ['guide-qq','guide-wechat'])$('#'+id).addEventListener('change',()=>GUIDE.channelsDirty=true);
  $('#btn-guide-model').addEventListener('click',saveGuideModel);
  $('#btn-guide-wechat').addEventListener('click',guideWechatQR);
  $('#guide-provider').addEventListener('change',()=>{showGuideProvider();GUIDE.modelDirty=true;$('#guide-model-status').textContent='设置已修改，请保存并测试';});
  for(const id of ['guide-model-base','guide-model-name','guide-model-key'])$('#'+id).addEventListener('input',()=>{GUIDE.modelDirty=true;$('#guide-model-status').textContent='设置已修改，请保存并测试';});
  $('#btn-qq-choice-close').addEventListener('click',()=>{$('#qq-choice-modal').hidden=true;stopQQLoginPolling();});
  $('#btn-qq-choice-qr').addEventListener('click',()=>{$('#qq-choice-modal').hidden=true;getQQQr();});
  $('#btn-qq-choice-quick').addEventListener('click',()=>quickLoginQQ({qq:$('#qq-choice-account').value,remember:$('#qq-choice-remember').checked}));
}
