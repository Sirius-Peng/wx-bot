/* ═══════════════════════════════════════════════════════════════
   微信 AI 助手 · 管理界面
   ═══════════════════════════════════════════════════════════════ */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

let STATE = {
  config: null,
  page: 'overview',
  msgFilter: '',
  qrTimer: null,
  pollTimer: null,
  qqStatus: null,
  qqLoginTimer: null,
  ilinkStatus: null,
  dockerStatus: null,
};

/* ══════════ 工具 ══════════ */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 401) {
    throw new Error('管理会话已过期，请刷新页面并重新登录');
  }
  if (!res.ok) { let error; try { error=await res.json(); } catch (_) {} throw new Error(typeof error?.detail==='string'?error.detail:`HTTP ${res.status}`); }
  return res.json();
}

function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s, transform .3s';
    el.style.opacity = '0';
    el.style.transform = 'translateX(20px)';
    setTimeout(() => el.remove(), 320);
  }, 2600);
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function fmtUptime(sec) {
  if (!sec) return '—';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

const ICON_EMPTY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 10h10M7 14h6"/></svg>`;

/* ══════════ 导航 ══════════ */

const PAGE_META = {
  guide: ['上手引导', '按步骤连接模型、QQ 与微信'],
  overview: ['概览', '查看机器人运行状态与今日数据'],
  connect:  ['接入设置', '配置通道并完成登录'],
  memory:   ['人格知识库', '对用户的了解：经历慢慢沉淀，人格始终优先'],
  messages: ['消息记录', '查看收发消息与触发判定结果'],
  statistics: ['对话统计', '以气泡图和排行查看高频词与常聊话题'],
  'memory-graph': ['记忆拓扑', '从对话来源到长期记忆，直观看见关联'],
  settings: ['模型与行为', '配置大模型与机器人性格'],
};

function switchPage(name) {
  if (!PAGE_META[name]) name = 'overview';
  STATE.page = name;
  $('#btn-start').hidden = !!STATE.status?.running || name === 'guide';
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === name));
  $$('.page').forEach(p => p.classList.toggle('active', p.id === `page-${name}`));
  const [title, sub] = PAGE_META[name];
  $('#page-title').textContent = title;
  $('#page-sub').textContent = sub;
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name === 'memory') loadMemory();
  if (name === 'messages') loadMessages();
  if (name === 'statistics') loadStatistics();
  if (name === 'memory-graph') loadMemoryGraph();
  else leaveMemoryGraph();
  if (name === 'settings' || name === 'connect') loadConfig();
  if (name === 'guide') refreshOnboarding().catch(e=>guideMessage(e.message));
  else GUIDE.wechatSession++;

  // 接入设置页需要持续刷新 QQ 网关状态
  if (name === 'connect') startConnectPolling();
  else stopConnectPolling();
}

// ⚠️ 必须先声明：删掉「Docker 面板」那段时把 let 声明一起删了，
//    结果首次进入「接入设置」页就读到未声明变量 → ReferenceError，
//    整页状态轮询起不来（界面一直显示"等待中"，控制台报错）。
let connectPollTimer = null;

function startConnectPolling() {
  stopConnectPolling();
  refreshQQStatus();
  loadQQAccounts();
  connectPollTimer = setInterval(refreshQQStatus, 3000);
}

function stopConnectPolling() {
  if (connectPollTimer) {
    clearInterval(connectPollTimer);
    connectPollTimer = null;
  }
  // 离开「接入设置」页要停掉扫码轮询，避免定时器泄漏
  if ($('#qq-login-modal').hidden && !STATE.qqLoginPending) stopQQLoginPolling();
}

window.addEventListener('hashchange', () => {
  const name = location.hash.replace('#', '');
  if (name && name !== STATE.page) switchPage(name);
});

/* ══════════ 状态轮询 ══════════ */

async function refreshStatus() {
  if (STATE.page === 'memory') loadMemory();
  let s;
  try { s = await api('/api/status'); }
  catch { return; }

  STATE.status = s;
  const running = s.running;
  const stats = s.stats || {};

  // 侧边栏状态
  const chip = $('#side-status');
  chip.className = 'status-chip' + (running ? ' on' : (s.last_error ? ' err' : ''));
  chip.querySelector('span').textContent =
    running ? '运行中' : (s.last_error ? '启动失败' : '未运行');

  // 顶部按钮
  $('#btn-start').hidden = running || STATE.page === 'guide';
  $('#btn-stop').hidden = !running;

  // 英雄区
  const hero = $('#hero');
  hero.classList.toggle('on', running);
  $('#hero-pulse').classList.toggle('on', running);
  $('#hero-title').textContent = running ? '机器人正在运行' : '机器人未运行';
  const rule = s.qq_running
    ? '私聊先听完再自然接话，QQ 群里 @ 它才回'
    : '先听完整段表达，再自然接话';
  $('#hero-desc').textContent = running
    ? `正在接收消息：${rule}`
    : (s.last_error || '完成接入设置后点击「启动机器人」即可开始工作');
  if (running && s.wechat_delivery) {
    const d = s.wechat_delivery;
    const label = { api_accepted: '接口已接受', retry_pending: '发送网络异常，等待重试', generation_retry_pending: '模型网络异常，保留消息待重试', missing_context: '缺少会话令牌', idle: '等待消息' }[d.last_status] || d.last_status;
    if(d.typing?.last_status==='on')$('#hero-desc').textContent += '；微信正在输入';
    $('#hero-desc').textContent += `；微信排队 ${d.pending_inbound}，待重试 ${d.pending_replies}，${label}（提交 ${d.last_send_ms}ms）`;
  }
  if (running && s.conversation) $('#hero-desc').textContent += '；' + conversationStatusText(s.conversation);
  if (running && s.last_error) $('#hero-desc').textContent += `；${s.last_error}`;


  const platforms = s.platforms || [];
  const platNames = platforms.map(p => p === 'wechat' ? '微信' : 'QQ').join(' · ');
  $('#hero-platform').textContent = platNames || '—';

  const caps = [];
  if (s.wechat_running) caps.push('微信私聊');
  if (s.qq_running) caps.push('QQ 私聊 + 群聊');
  $('#hero-groupcap').textContent = running ? (caps.join(' · ') || '—') : '—';

  $('#hero-uptime').textContent = fmtUptime(s.uptime);

  // 统计
  $('#st-in').textContent = stats.total_in || 0;
  $('#st-in-foot').textContent = `群 ${stats.group_count || 0} · 私聊 ${stats.private_count || 0}`;
  $('#st-out').textContent = stats.total_out || 0;
  $('#st-out-foot').textContent = `回复率 ${stats.reply_rate || 0}%`;
  $('#st-silent').textContent = stats.silent || 0;
  $('#st-chats').textContent = (stats.group_count || 0) + (stats.private_count || 0);
  $('#st-chats-foot').textContent = `群 ${stats.group_count || 0} · 私聊 ${stats.private_count || 0}`;
}

function conversationStatusText(status) {
  const parts = [];
  if (status.waiting) parts.push(`等待说完 ${status.waiting}`);
  if (status.deferred) parts.push(`稍后再判断 ${status.deferred}`);
  if (status.replying) parts.push(`正在回应 ${status.replying}`);
  if (status.silent) parts.push(`主动静默 ${status.silent}（已保留上下文）`);
  if (status.uncertain) parts.push(`投递结果待确认 ${status.uncertain}（不重复提交）`);
  if (status.state_error) parts.push('会话状态读取异常，请检查日志');
  return parts.join(' · ') || '等待新消息';
}

/* ══════════ 概览：最近动态 ══════════ */

async function loadRecent() {
  const { messages } = await api('/api/messages?limit=8');
  const box = $('#recent-list');
  if (!messages.length) {
    box.innerHTML = `<div class="empty">${ICON_EMPTY}<span>还没有消息，启动机器人后这里会显示实时动态</span></div>`;
    return;
  }
  box.innerHTML = messages.map(m => feedItem(m)).join('');
}

function feedItem(m) {
  const out = m.direction === 'out';
  const who = out ? '机器人' : (m.sender || '未知');
  const initial = out ? 'AI' : (who[0] || '?');
  const tag = m.trigger_layer
    ? `<span class="tag ${out ? 'on' : ''}">${esc(m.trigger_layer)}${m.trigger_reason ? ' · ' + esc(m.trigger_reason) : ''}</span>`
    : '';
  const scope = m.chat_type === 'group' ? '<span class="tag info">群聊</span>' : '<span class="tag">私聊</span>';
  return `<div class="feed-item">
    <div class="feed-avatar ${out ? 'out' : 'in'}">${esc(initial)}</div>
    <div class="feed-body">
      <div class="feed-head">
        <span class="feed-who">${esc(who)}</span>
        ${scope}${tag}${!out && m.sender_id ? `<span class="tag" title="身份关联时使用此发送者 ID">${esc(m.platform || '')} · ${esc(m.sender_id)}</span>` : ''}
        <span class="feed-time">${esc(m.time || '')}</span>
      </div>
      <div class="feed-text ${out ? 'out' : ''}">${esc(m.content)}</div>
    </div>
  </div>`;
}

async function loadStorage() {
  try {
    const s = await api('/api/data/status');
    $('#storage-path').value = s.data_dir;
    $('#storage-path').title = s.data_dir;
    const migration = s.migration;
    const text = migration.status === 'migrated' ? `旧数据已迁移：${migration.imported_files} 个文件；原目录和迁移备份均保留。` : '当前使用独立本地数据目录。';
    $('#storage-status').textContent = text + (s.custom_path ? ' 你已指定自定义目录，请保留此目录。' : '') + (migration.other_backups ? ` 另有 ${migration.other_backups} 份旧数据已备份，未混入当前记忆。` : '');
    $('#btn-open-storage').hidden = !window.wxbotDesktop;
    renderImportStatus(s.import || {status:'none'});
  } catch (e) { $('#storage-status').textContent = '数据位置读取失败：' + e.message; }
}

async function backupStorage() {
  const button = $('#btn-storage-backup');button.disabled = true;
  $('#storage-status').textContent = '正在创建并校验本地备份…';
  try {
    const result = await api('/api/data/backup', {method:'POST'});
    $('#storage-status').textContent = result.message + '：' + result.path;
    const link = $('#storage-download');link.href = result.download_url;link.download = result.name;link.hidden = false;
    toast('本地备份已保存', 'ok');
  } catch (e) { $('#storage-status').textContent = '备份失败，原数据保持不变：' + e.message; }
  finally { button.disabled = false; }
}

/* Explicit full-directory import is staged; only a backend restart publishes it. */
const IMPORT = {preview:null, epoch:0, busy:false, queued:false};
function invalidateImportPreview() {
  IMPORT.epoch++;IMPORT.preview=null;
  $('#import-preview').hidden=true;$('#import-confirm').checked=false;
  $('#btn-import-prepare').disabled=true;
}
function renderImportStatus(report) {
  IMPORT.queued = ['queued','applying'].includes(report.status);
  $('#btn-import-choose').hidden = !window.wxbotDesktop?.data;
  $('#btn-import-cancel').hidden = !IMPORT.queued;
  $('#btn-import-restart').hidden = !IMPORT.queued || !window.wxbotDesktop;
  $('#import-source').disabled = IMPORT.queued;
  $('#btn-import-preview').disabled = IMPORT.queued || IMPORT.busy;
  $('#btn-import-choose').disabled = IMPORT.queued || IMPORT.busy;
  if (IMPORT.queued) {
    invalidateImportPreview();
    $('#import-status').textContent = report.message + (window.wxbotDesktop ? '' : ' 请退出并重新启动后端进程；“重启机器人”不等于重启后台。');
  } else if (report.status === 'imported' || report.status === 'failed') {
    $('#import-status').textContent = report.message + (report.backup ? ` 导入前备份：${report.backup}` : '');
  }
}
async function chooseImportDirectory() {
  if (IMPORT.busy || IMPORT.queued) return;
  const result = await window.wxbotDesktop?.data.chooseLegacyDir();
  if (!result || result.canceled) return;
  $('#import-source').value = result.path;invalidateImportPreview();
  await previewImportDirectory();
}
async function previewImportDirectory() {
  if (IMPORT.busy || IMPORT.queued) return;
  invalidateImportPreview();
  const epoch = IMPORT.epoch, source = $('#import-source').value.trim();
  IMPORT.busy=true;$('#btn-import-preview').disabled=true;$('#btn-import-choose').disabled=true;
  $('#import-status').textContent='正在读取旧目录并校验文件，请稍候…';
  try {
    const result = await api('/api/data/import/preview',{method:'POST',body:JSON.stringify({source})});
    if (epoch !== IMPORT.epoch) return;
    IMPORT.preview=result;$('#import-preview').hidden=false;
    const included = [['persona','人格及设置'],['memory','长期记忆'],['messages','聊天记录'],['wechat','微信状态'],['contexts','近期上下文']].filter(([key])=>result.includes[key]).map(([,label])=>label);
    $('#import-summary').textContent=`${result.source}：${result.files} 个文件、${result.directories} 个目录、${(result.bytes/1024/1024).toFixed(2)} MiB。包含：${included.join('、')}。`;
    $('#import-status').textContent=result.message;
  } catch(e) { if (epoch===IMPORT.epoch) $('#import-status').textContent='预览失败：'+e.message; }
  finally {IMPORT.busy=false;$('#btn-import-preview').disabled=IMPORT.queued;$('#btn-import-choose').disabled=IMPORT.queued;}
}
async function prepareImportDirectory() {
  if (IMPORT.busy || IMPORT.queued || !IMPORT.preview || !$('#import-confirm').checked) return;
  if (typeof DIRTY_SECTIONS !== 'undefined' && DIRTY_SECTIONS.size && !await confirmAction('有尚未保存的设置草稿。整套导入并重启后，这些草稿将被丢弃。继续？')) return;
  IMPORT.busy=true;$('#btn-import-prepare').disabled=true;
  $('#import-source').disabled=true;$('#btn-import-preview').disabled=true;$('#btn-import-choose').disabled=true;
  $('#import-status').textContent='正在创建并校验旧数据快照；当前配置尚未替换…';
  try {
    const result=await api('/api/data/import/prepare',{method:'POST',body:JSON.stringify({token:IMPORT.preview.token,confirm_replace:true})});
    renderImportStatus(result.import);
  } catch(e) {invalidateImportPreview();$('#import-status').textContent='准备失败，当前数据未改变：'+e.message;}
  finally {IMPORT.busy=false;$('#import-source').disabled=IMPORT.queued;$('#btn-import-preview').disabled=IMPORT.queued;$('#btn-import-choose').disabled=IMPORT.queued;}
}
async function cancelImportDirectory() {
  if (IMPORT.busy) return;
  IMPORT.busy=true;$('#btn-import-cancel').disabled=true;
  try {
    const result=await api('/api/data/import/cancel',{method:'POST'});
    renderImportStatus({status:'none'});$('#import-status').textContent=result.message;
  } catch(e) {$('#import-status').textContent='取消失败：'+e.message;}
  finally {IMPORT.busy=false;$('#btn-import-cancel').disabled=false;$('#btn-import-preview').disabled=IMPORT.queued;$('#btn-import-choose').disabled=IMPORT.queued;}
}
async function restartForImport() {
  if (IMPORT.busy || !window.wxbotDesktop || !IMPORT.queued) return;
  IMPORT.busy=true;$('#btn-import-restart').disabled=true;$('#btn-import-cancel').disabled=true;
  $('#import-status').textContent='正在停止后台、备份当前数据并导入；完成后界面自动重新加载…';
  try {
    const result=await window.wxbotDesktop.backend.restart();
    if (!result.ok) throw new Error(result.error || '请查看后台日志');
  } catch(e) {$('#import-status').textContent='后台重启未完成，导入快照已保留：'+e.message;}
  finally {IMPORT.busy=false;$('#btn-import-restart').disabled=false;$('#btn-import-cancel').disabled=false;}
}

/* ══════════ 接入设置 ══════════ */

const DIRTY_SECTIONS = new Set();
const SECTION_FIELDS = {
  primary: ['llm', ['primary_base','primary_model','primary_key']],
  fallback: ['llm', ['fallback_base','fallback_model','fallback_key']],
  judge: ['llm', ['judge_base','judge_model','judge_key']],
  vision: ['llm', ['vision_base','vision_model','vision_key']],
  search: ['llm', ['search_enabled','search_key','search_model','context_timezone']],
  persona: ['bot', ['name','persona','persona_profile','persona_relationship']],
  proactive: ['bot', ['proactive_enabled','proactive_min_hours','proactive_max_hours','proactive_daily_limit','proactive_quiet_start','proactive_quiet_end']],
  reply: ['bot', ['reply_mode','late_reply_enabled','group_reply_at_sender','message_merge_window','message_merge_max_wait','humanized_wait','typing_indicator','message_pause_timeout']],
  memory: ['bot', ['memory_enabled','memory_inject','memory_extract_interval']],
  channels: ['adapter', ['wechat_enabled','qq_enabled','wechat_type','onebot_http_url','onebot_token','bot_qq','bot_nickname','napcat_image_mirror']]
};
function sectionStatus(section, text, dirty = false) {
  const el = $(`[data-save-status="${section}"]`);
  if (el) { el.textContent = text; el.classList.toggle('dirty', dirty); }
}
function markSectionDirty(section) { DIRTY_SECTIONS.add(section); sectionStatus(section, '有修改，尚未保存', true); }
function captureDrafts() {
  return [...DIRTY_SECTIONS].flatMap(section => [...document.querySelectorAll(`[data-settings-section="${section}"] input, [data-settings-section="${section}"] select, [data-settings-section="${section}"] textarea`)].filter(el=>el.id).map(el=>({id:el.id,value:el.value,checked:el.checked})));
}
const CHANNELS = { wechat: true, qq: false };

async function loadConfig() {
  const c = await api('/api/config');
  const drafts = captureDrafts();
  STATE.config = c;
  const a = c.adapter || {};

  // 通道复选
  CHANNELS.wechat = !!a.wechat_enabled;
  CHANNELS.qq = !!a.qq_enabled;
  $$('#adapter-type .pcard').forEach(el => {
    const cb = el.querySelector('input[type=checkbox]');
    if (cb) cb.checked = (cb.value === 'wechat') ? CHANNELS.wechat : CHANNELS.qq;
  });
  renderPlatformCards();

  // QQ / OneBot
  $('#onebot-http').value = a.onebot_http_url || '';
  $('#onebot-token').value = a.onebot_token || '';
  $('#bot-qq').value = a.bot_qq || '';
  $('#qq-nickname').value = a.bot_nickname || '';
  $('#napcat-image-mirror').value = a.napcat_image_mirror ?? 'docker.m.daocloud.io';

  // 模型
  $('#primary-base').value = c.llm.primary_base || '';
  $('#primary-model').value = c.llm.primary_model || '';
  $('#primary-key').value = c.llm.primary_key || '';
  $('#fallback-base').value = c.llm.fallback_base || '';
  $('#fallback-model').value = c.llm.fallback_model || '';
  $('#fallback-key').value = c.llm.fallback_key || '';
  $('#judge-base').value = c.llm.judge_base || '';
  $('#judge-model').value = c.llm.judge_model || '';
  $('#judge-key').value = c.llm.judge_key || '';

  $('#vision-base').value = c.llm.vision_base || '';
  $('#vision-model').value = c.llm.vision_model || '';
  $('#vision-key').value = c.llm.vision_key || '';
  $('#search-enabled').checked = c.llm.search_enabled !== false;
  $('#search-key').value = c.llm.search_key || '';
  $('#search-model').value = c.llm.search_model || 'deepseek-flash';
  $('#context-timezone').value = c.llm.context_timezone || 'Asia/Shanghai';

  // 机器人行为
  $('#bot-name').value = c.bot.name || '';
  $('#bot-persona').value = c.bot.persona || '';
  $('#persona-profile').value = c.bot.persona_profile || 'custom';
  $('#persona-relationship').value = c.bot.persona_relationship || 'neutral';
  $('#group-at-sender').checked = c.bot.group_reply_at_sender !== false;
  $('#memory-enabled').checked = c.bot.memory_enabled !== false;
  $('#memory-inject').checked = c.bot.memory_inject !== false;
  $('#memory-interval').value = c.bot.memory_extract_interval || 3;
  $('#reply-mode').value = c.bot.reply_mode || 'natural';
  $('#late-reply-enabled').checked = c.bot.late_reply_enabled !== false;
  $('#message-merge-window').value = c.bot.message_merge_window ?? 4;
  $('#message-merge-max-wait').value = c.bot.message_merge_max_wait ?? 10;
  $('#humanized-wait').checked = c.bot.humanized_wait !== false;
  $('#typing-indicator').checked = c.bot.typing_indicator !== false;
  $('#message-pause-timeout').value = c.bot.message_pause_timeout ?? 120;
  $('#proactive-enabled').checked = c.bot.proactive_enabled === true;
  for (const [id, field, fallback] of [['min-hours','min_hours',3],['max-hours','max_hours',8],['daily-limit','daily_limit',2],['quiet-start','quiet_start',23],['quiet-end','quiet_end',8]]) {
    $('#proactive-'+id).value = c.bot['proactive_'+field] ?? fallback;
  }
  loadProactive();

  // 官方通道的登录态存在后端文件里，单独取一次
  if (CHANNELS.wechat) {
    try {
      STATE.ilinkStatus = await api('/api/ilink/status');
    } catch (_) { STATE.ilinkStatus = null; }
  } else {
    STATE.ilinkStatus = null;
  }

  for (const draft of drafts) { const el = $('#'+draft.id); if(el){el.value=draft.value;el.checked=draft.checked;} }
  CHANNELS.wechat = $('#adapter-type input[value="wechat"]').checked;
  CHANNELS.qq = $('#adapter-type input[value="qq"]').checked;
  renderPlatformCards();
  applyChannels();
  updateWizardStatus(c);
}

/** 平台卡片：显式 .is-selected 类驱动选中外观，禁用态置灰 */
function renderPlatformCards() {
  $$('#adapter-type .pcard').forEach(el => {
    const cb = el.querySelector('input[type=checkbox]');
    if (!cb) return;
    const on = cb.checked;
    el.classList.toggle('is-selected', on);
    el.classList.toggle('is-disabled', !on);
  });
}

/** 根据通道勾选状态显示/隐藏对应步骤 */
function applyChannels() {
  const wechatOn = CHANNELS.wechat;
  const qqOn = CHANNELS.qq;

  $('#step-2-qq').hidden = !qqOn;      // QQ 网关部署
  $('#step-3').hidden = !wechatOn;     // 微信扫码登录；QQ 在网关侧登录

  // 只勾了微信时，扫码登录就是第 2 步
  const num = $('#step3-num');
  if (num) num.textContent = qqOn ? '3' : '2';

  const title = $('#step3-title');
  const sub = $('#step3-sub');
  const hint = $('#qr-hint');
  if (title) title.textContent = '扫码登录微信';
  if (sub) sub.textContent = '腾讯官方 iLink 接口，在本应用内直接扫码，无需命令行';
  if (hint) {
    hint.textContent = '用手机微信扫描二维码。这是腾讯官方通道，合规零封号，' +
      '目前官方只开放私聊。二维码几分钟内有效，过期后重新获取即可。';
  }

  // QQ 的反向 WS 地址
  const wsEl = $('#qq-ws-url');
  if (wsEl) {
    wsEl.textContent = `ws://${location.hostname}:${location.port}/ws/onebot`;
  }
}

/** 更新向导各步骤的状态徽标 */
function updateWizardStatus(cfg) {
  const a = (cfg || STATE.config || {}).adapter || {};
  const loggedIn = !!(STATE.ilinkStatus && STATE.ilinkStatus.loggedIn);
  const loginBadge = $('#login-badge');
  if (loginBadge) {
    // 官方 iLink 的凭证存在本地文件里，不看配置字段
    if (loggedIn) {
      loginBadge.className = 'wz-status ok';
      loginBadge.textContent = '已登录';
    } else {
      loginBadge.className = 'wz-status';
      loginBadge.textContent = '未登录';
    }
  }
  // 二维码区的文案必须与徽标一致 —— 否则会出现"已登录"旁边写着"尚未登录"
  const qrStatus = $('#qr-status');
  if (qrStatus && !STATE.qrTimer) {
    qrStatus.textContent = loggedIn ? '已登录，无需重复扫码' : '尚未登录';
  }

  // QQ 通道状态：优先用后端实时状态（容器/登录态），没有再退回配置判断
  const qqBadge = $('#qq-badge');
  if (qqBadge) {
    const live = STATE.qqStatus;
    if (live && (live.napcat || live.task)) {
      renderQQBadge(live);
    } else if (a.bot_qq) {
      qqBadge.className = 'wz-status ok';
      qqBadge.textContent = `已连接 ${a.bot_qq}`;
    } else if (a.onebot_http_url) {
      qqBadge.className = 'wz-status';
      qqBadge.textContent = '待连接';
    } else {
      qqBadge.className = 'wz-status';
      qqBadge.textContent = '待配置';
    }
  }
}

function collectConfig() {
  const c = JSON.parse(JSON.stringify(STATE.config));
  // 通道（微信只剩官方 iLink 一条，无需再选类型）
  c.adapter.wechat_enabled = CHANNELS.wechat;
  c.adapter.qq_enabled = CHANNELS.qq;
  c.adapter.wechat_type = 'ilink';
  c.adapter.onebot_http_url = $('#onebot-http').value.trim();
  c.adapter.onebot_token = $('#onebot-token').value.trim();
  c.adapter.napcat_image_mirror = $('#napcat-image-mirror').value.trim();
  c.adapter.bot_qq = $('#bot-qq').value.trim();
  c.adapter.bot_nickname = $('#qq-nickname').value.trim() ||
    STATE.config.bot.name;

  // 模型
  c.llm.primary_base = $('#primary-base').value.trim();
  c.llm.primary_model = $('#primary-model').value.trim();
  c.llm.primary_key = $('#primary-key').value.trim();
  c.llm.fallback_base = $('#fallback-base').value.trim();
  c.llm.fallback_model = $('#fallback-model').value.trim();
  c.llm.fallback_key = $('#fallback-key').value.trim();
  c.llm.judge_base = $('#judge-base').value.trim();
  c.llm.judge_model = $('#judge-model').value.trim();
  c.llm.judge_key = $('#judge-key').value.trim();

  c.llm.vision_base = $('#vision-base').value.trim();
  c.llm.vision_model = $('#vision-model').value.trim();
  c.llm.vision_key = $('#vision-key').value.trim();
  c.llm.search_enabled = $('#search-enabled').checked;
  c.llm.search_key = $('#search-key').value.trim();
  c.llm.search_model = $('#search-model').value.trim();
  c.llm.context_timezone = $('#context-timezone').value.trim();

  // 机器人行为
  c.bot.name = $('#bot-name').value.trim() || '小助手';
  c.bot.persona = $('#bot-persona').value.trim();
  c.bot.persona_profile = $('#persona-profile').value;
  c.bot.persona_relationship = $('#persona-relationship').value;
  c.bot.group_reply_at_sender = $('#group-at-sender').checked;
  c.bot.reply_mode = $('#reply-mode').value;
  c.bot.late_reply_enabled = $('#late-reply-enabled').checked;
  c.bot.message_merge_window = Math.min(10, Math.max(0, Number($('#message-merge-window').value)));
  c.bot.message_merge_max_wait = Math.max(c.bot.message_merge_window, Math.min(60, Math.max(1, Number($('#message-merge-max-wait').value) || 10)));
  c.bot.humanized_wait = $('#humanized-wait').checked;
  c.bot.typing_indicator = $('#typing-indicator').checked;
  c.bot.message_pause_timeout = Math.min(600, Math.max(5, Number($('#message-pause-timeout').value) || 120));
  c.bot.proactive_enabled = $('#proactive-enabled').checked;
  for (const [id, field] of [['min-hours','min_hours'],['max-hours','max_hours'],['daily-limit','daily_limit'],['quiet-start','quiet_start'],['quiet-end','quiet_end']]) {
    c.bot['proactive_'+field] = Number($('#proactive-'+id).value);
  }
  c.bot.memory_enabled = $('#memory-enabled').checked;
  c.bot.memory_inject = $('#memory-inject').checked;
  c.bot.memory_extract_interval =
    Math.min(50, Math.max(3, Number($('#memory-interval').value) || 3));
  return c;
}

async function loadProactive() {
  const container = $('#proactive-contacts');
  if (!container) return;
  try {
    const data = await api('/api/proactive');
    container.replaceChildren();
    if (data.state_error) { container.textContent = '主动联系记录读取异常，已暂停发送，请保留 proactive.json 并检查后台日志。'; return; }
    if (!data.contacts.length) { container.textContent = '还没有私聊联系人。请先通过微信或 QQ 发一条消息，再点击刷新联系人。'; return; }
    for (const person of data.contacts) {
      const label = document.createElement('label'); label.className = 'switch-row';
      const info = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = person.accounts.map(a=>`${a.name || a.owner_id}（${a.channel} · ${a.owner_id}）`).join(' ↔ ');
      const status = document.createElement('span');
      status.textContent = person.muted ? '对方已要求暂停主动联系' : !person.enabled ? '尚未允许' : !data.enabled ? '联系人已允许；总开关关闭' : person.awaiting_reply ? (person.last_status === 'accepted' ? '已联系，等待对方回复；不会连续追加' : '发送结果待确认；不会重复提交') : person.next_at ? `计划不早于 ${new Date(person.next_at*1000).toLocaleString()}；聊天或免打扰时延后` : '已允许，启动机器人后统一规划';
      info.append(title, status);
      const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.checked = person.enabled && !person.muted;
      toggle.setAttribute('aria-label', `允许主动联系 ${title.textContent}`);
      toggle.addEventListener('change', async()=>{
        toggle.disabled = true;
        try { await api('/api/proactive/contact',{method:'PATCH',body:JSON.stringify({id:person.id,enabled:toggle.checked})}); toast('联系人设置已保存','ok'); await loadProactive(); }
        catch(e) { toggle.checked = person.enabled && !person.muted; toast('保存失败：'+e.message,'err'); }
        finally { toggle.disabled = false; }
      });
      label.append(info,toggle); container.append(label);
    }
  } catch(e) { container.textContent = '联系人加载失败：'+e.message; }
}

async function saveSection(section, quiet = false) {
  const button = $(`[data-save-section="${section}"]`);
  if (button) button.disabled = true;
  try {
    const [root, fields] = SECTION_FIELDS[section];
    const all = collectConfig();
    const beforeValues = Object.fromEntries(fields.map(key => [key, all[root][key]]));
    // Send deltas only: setup/login may update tokens/account while a form is open.
    // Unchanged stale fields must not overwrite those fresh backend values.
    const values = Object.fromEntries(fields.filter(key => JSON.stringify(all[root][key]) !== JSON.stringify(STATE.config[root][key])).map(key => [key, all[root][key]]));
    const saved = await api(`/api/config/section/${section}`, {method:'PATCH',body:JSON.stringify(values)});
    STATE.config = saved.config;
    const latest = collectConfig();
    const editedWhileSaving = fields.some(key => JSON.stringify(latest[root][key]) !== JSON.stringify(beforeValues[key]));
    if (!editedWhileSaving) { DIRTY_SECTIONS.delete(section); sectionStatus(section, '已保存'); }
    else { markSectionDirty(section); sectionStatus(section, '已保存上一版；新修改尚未保存', true); }
    // Re-render only persisted sections; other cards retain their unsaved drafts.
    await loadConfig();
    if (saved.requires_restart && STATE.status?.running) {
      const action = saved.config.adapter.qq_enabled || saved.config.adapter.wechat_enabled ? 'restart' : 'stop';
      const result = await api(`/api/bot/${action}`, {method:'POST'});
      if (!result.ok) throw new Error(result.message || '通道重启未完成');
    }
    if (!quiet) toast(saved.message || '本区设置已保存', 'ok');
    return true;
  } catch(e) { sectionStatus(section, '保存失败，修改已保留', true); DIRTY_SECTIONS.add(section); toast('保存失败：'+e.message,'err'); return false; }
  finally { if(button) button.disabled = false; }
}

/* ══════════ 扫码登录 ══════════ */

async function getQR() {
  const box = $('#qr-box');
  box.innerHTML = '<div class="qr-empty"><span>正在获取二维码…</span></div>';
  let r;
  try {
    // ⚠️ 先落盘再取二维码：后端是按**保存的配置**判断该不该走官方通道的。
    // 用户可能刚勾了通道但还没点「保存」，不先保存就会报"通道未启用"。
    if (!await saveSection('channels', true)) throw new Error('通道设置未保存');
    r = await api('/api/login/qrcode', { method: 'POST' });
  } catch (e) {
    box.innerHTML = `<div class="qr-empty"><span>获取失败：${esc(e.message)}</span></div>`;
    toast('获取二维码失败：' + e.message, 'err');
    return;
  }
  if (!r.ok) {
    let html = `<div class="qr-empty"><span>${esc(r.message || '获取失败')}</span>`;
    if (r.hint) {
      html += `<p style="margin-top:8px;font-size:12px;color:var(--text-2);line-height:1.6;">${esc(r.hint)}</p>`;
    }
    html += '</div>';
    box.innerHTML = html;
    $('#qr-status').textContent = r.message || '获取失败';
    toast(r.message || '获取二维码失败', 'err');
    return;
  }
  const src = r.image || '';
  if (src.startsWith('data:') || src.startsWith('http')) {
    box.innerHTML = `<img src="${esc(src)}" alt="登录二维码">`;
  } else if (src) {
    box.innerHTML = `<img src="data:image/png;base64,${esc(src)}" alt="登录二维码">`;
  } else {
    box.innerHTML = `<div class="qr-empty"><span>二维码数据为空</span></div>`;
  }
  $('#qr-status').textContent = '等待扫码…';

  clearInterval(STATE.qrTimer);
  // 官方通道的查询接口会 hold 住连接（最长 35 秒），间隔放宽一点
  STATE.qrTimer = setInterval(checkLogin, 3500);
}

async function checkLogin() {
  const r = await api('/api/login/check');
  if (r.status === 'confirmed') {
    clearInterval(STATE.qrTimer);
    $('#qr-status').textContent = '✅ 登录成功';
    toast(r.message || '登录成功', 'ok');
    await loadConfig();          // 会把官方通道的登录态一起刷出来
    await refreshStatus();
  } else if (r.status === 'failed') {
    clearInterval(STATE.qrTimer);
    $('#qr-status').textContent = '登录失败：' + (r.message || '');
  } else {
    $('#qr-status').textContent = r.message || '等待扫码…';
  }
}

/* ══════════ 消息记录 ══════════ */

async function loadMessages() {
  const q = new URLSearchParams({ limit: '200' });
  if (STATE.msgFilter === 'group' || STATE.msgFilter === 'private') {
    q.set('chat_type', STATE.msgFilter);
  } else if (STATE.msgFilter === 'out') {
    q.set('direction', 'out');
  }
  const { messages } = await api('/api/messages?' + q.toString());
  const box = $('#messages-list');
  if (!messages.length) {
    box.innerHTML = `<div class="empty">${ICON_EMPTY}<span>暂无消息记录</span></div>`;
    return;
  }
  box.innerHTML = messages.map(m => feedItem(m)).join('');
}

/* ══════════ 启停 ══════════ */

async function startBot() {
  $('#btn-start').disabled = true;
  $('#btn-start').textContent = '启动中…';
  try {
    const r = await api('/api/bot/start', { method: 'POST' });
    toast(r.message, r.ok ? 'ok' : 'err');
  } catch (e) {
    toast('启动失败：' + e.message, 'err');
  }
  $('#btn-start').disabled = false;
  $('#btn-start').textContent = '启动机器人';
  await refreshStatus();
}

async function stopBot() {
  const r = await api('/api/bot/stop', { method: 'POST' });
  toast(r.message, 'ok');
  await refreshStatus();
}

/* ══════════ 环境检查清单 ══════════ */

function setCheck(key, state, desc, btn) {
  const item = document.querySelector(`.check-item[data-key="${key}"]`);
  if (!item) return;
  item.className = 'check-item' + (state ? ' ' + state : '');
  const icon = item.querySelector('.check-icon');
  icon.textContent = state === 'ok' ? '✓' : state === 'err' ? '✕' : state === 'busy' ? '…' : '○';
  const d = item.querySelector('.check-desc');
  if (d) d.textContent = desc || '';

  // 行内按钮：只有需要时才显示
  const inlineBtn = item.querySelector('button');
  if (inlineBtn) {
    if (btn) {
      inlineBtn.hidden = false;
      inlineBtn.textContent = btn.label;
      inlineBtn.onclick = btn.action;
    } else {
      inlineBtn.hidden = true;
      inlineBtn.onclick = null;
    }
  }
}

/** 复制文本到剪贴板 */
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  const old = btn.textContent;
  btn.textContent = '已复制';
  setTimeout(() => { btn.textContent = old; }, 1500);
}

/* ══════════ QQ / OneBot ══════════ */

/** 一键部署 NapCat（非阻塞，靠轮询 /api/qq/status 看进度）*/
let qqStartPending = null;
async function setupQQ(options = {}) {
  if(qqStartPending)return qqStartPending;
  qqStartPending=startQQGateway(options);
  try{return await qqStartPending;}finally{qqStartPending=null;}
}
async function startQQGateway({saveForm=true}={}) {
  const buttons=['btn-home-qq','btn-guide-qq','btn-setup-qq'].map(id=>$('#'+id));
  const labels=buttons.map(btn=>btn.textContent);
  buttons.forEach(btn=>{btn.disabled=true;btn.textContent='正在启动 QQ 网关…';});
  reportQQProgress('正在检查 QQ 网关…');
  try {
    if(saveForm) {if(!await saveSection('channels',true))return false;}
    else if(!STATE.config.adapter.qq_enabled) {
      const r=await api('/api/config/section/channels',{method:'PATCH',body:JSON.stringify({qq_enabled:true})});
      STATE.config=r.config;await loadConfig();
    }
    let st=await refreshQQStatus();
    if(!st)throw new Error('网关状态获取未完成，请重试');
    if(st.httpReachable&&!st.napcat?.running) {
      reportQQProgress(st.loggedIn?'外部 QQ 网关已在线，无需启动本机容器':'外部网关已响应，请在该网关中登录 QQ');
      return !!st.loggedIn;
    }
    const docker=st.docker||{};
    if(!docker.installed && !await confirmAction('需要安装 Docker Desktop 来运行 QQ 网关。\n\n请按安装窗口提示完成安装与首次启动，再回到本应用继续。是否开始准备？')){reportQQProgress('尚未开始安装，准备好后可点击本按钮继续');return false;}
    if(!docker.running) {
      const prepared=await api('/api/docker/prepare',{method:'POST'});
      if(!prepared.ok)throw new Error(prepared.message||'Docker 准备未完成');
      st=await pollQQStatus();
      if(!st?.docker?.running){reportQQProgress(st?.task?.message||'请完成 Docker 安装并启动 Docker Desktop，再点击本按钮继续');return false;}
    }
    const r=await api('/api/qq/setup',{method:'POST'});
    if(!r.ok)throw new Error(r.message||'网关启动未完成');
    st=await pollQQStatus();
    if(!st || st.task?.stage==='error'||st.task?.stage==='timeout')throw new Error(st?.task?.message||'网关尚未就绪，请查看接入设置中的日志');
    if(st.task?.needsUser){reportQQProgress(st.task.message||'请按提示完成安装后继续');return false;}
    if(!st.napcat?.running&&st.task?.stage!=='waiting_login')throw new Error('网关还未运行，请稍后继续');
    // Reload saved tokens from setup, never submit stale fields from hidden forms.
    await loadConfig();await refreshOnboarding();
    if(st.loggedIn){await finishQQLogin();toast('QQ 已在线，无需重复扫码','ok');}
    else {reportQQProgress('网关已启动，正在准备登录…');await beginQQLogin();}
    return true;
  } catch(e){reportQQProgress('网关启动未完成：'+e.message);toast(e.message,'err');return false;}
  finally{buttons.forEach((btn,i)=>{btn.disabled=false;btn.textContent=labels[i];});}
}

/** 静默保存配置（不提示、不刷新表单），用于自动化流程中途落盘 */
async function saveConfigQuiet() { return await saveSection('channels', true); }

/** 刷新 QQ 状态并渲染 */
let qqStatusPending = null;
async function refreshQQStatus() {
  if (qqStatusPending) return qqStatusPending;
  qqStatusPending = fetchQQStatus();
  try { return await qqStatusPending; } finally { qqStatusPending = null; }
}
async function fetchQQStatus() {
  let st;
  try {
    st = await api('/api/qq/status');
  } catch (_) {
    return null;
  }
  STATE.qqStatus = st;
  renderQQChecklist(st);
  renderQQProgress(st.task);
  renderQQBadge(st);
  renderHomeQQ(st);
  return st;
}

function renderQQChecklist(st) {
  const d=st.docker||{}, n=st.napcat||{}, task=st.task||{};
  const external=st.httpReachable&&!n.running;
  const busy=!!task.running;
  setCheck('qq-docker', external?'ok':(d.running?'ok':(d.installed?'err':'')), external?'使用已连接的外部网关，无需本机 Docker':(d.running?`已就绪 · v${d.version||'?'}`:(d.installed?'已安装但未启动':'尚未安装')));
  setCheck('qq-container', (n.running||external)?'ok':(busy?'busy':''), external?`OneBot 已响应 · ${st.gatewayName||'外部网关'} ${st.gatewayVersion||''}`:(n.running?`正在运行${st.gatewayVersion?' · '+st.gatewayVersion:''}`:(busy?'启动中…':n.message||'等待网关启动')));
  setCheck('qq-login', st.loggedIn?'ok':((n.running||external)?'err':''), st.loggedIn?`已在线 ${st.botQq||''}`:(st.online===false?'QQ 已离线，请重新上线':((n.running||external)?'可快速登录或扫码':'等待网关就绪')));
  const ws=!!st.reverseWsConnected, http=st.messageTransport==='http_post';
  const receiving=!!st.messageReceiverStarted;
  let desc=ws?`反向 WS 已连接 · ${st.wsConnections||1} 个连接 · ${st.eventCount||0} 个事件`:(http?`HTTP 上报已到达 · 上次 ${new Date(st.lastHttpEventAt).toLocaleTimeString()}`:(busy?'等待网关启动':(st.loggedIn&&!receiving?'网关已在线，启动机器人后接收消息':'等待网关消息连接')));
  if((ws||http)&&!receiving) desc+='；机器人未启动';
  setCheck('qq-reverse-ws',(ws||http)?'ok':(busy?'busy':(st.loggedIn&&receiving?'err':'')),desc,(!(ws||http)&&receiving&&!busy)?{label:'重新对接',action:syncQQWsUrl}:null);
  if(st.wsUrl) $('#qq-ws-url').textContent=st.wsUrl;
}

/** 让网关重新连到当前端口（端口变了就会收不到消息）*/
async function syncQQWsUrl() {
  const box = $('#qq-test-status');
  box.className = 'test-status';
  box.innerHTML = '<div class="ts-line">正在重新对接…</div>';
  try {
    const r = await api('/api/qq/sync-ws', { method: 'POST' });
    box.className = 'test-status ' + (r.ok ? 'ok' : 'err');
    box.innerHTML = `<div class="ts-line"><span class="ts-icon">${r.ok ? '✓' : '✗'}</span>` +
      `<span>${esc(r.message || '')}</span></div>` +
      (r.ok ? '<div class="ts-hint">网关重连需要几秒，稍后这一项会变成「已连接」。' +
        (r.restarted ? '若 QQ 提示未登录，请在上面重新扫码一次。' : '') + '</div>' : '');
    toast(r.message || (r.ok ? '已重新对接' : '对接失败'), r.ok ? 'ok' : 'err');
    await refreshQQStatus();
  } catch (e) {
    box.className = 'test-status err';
    box.innerHTML = `<div class="ts-line"><span class="ts-icon">✗</span><span>失败：${esc(e.message)}</span></div>`;
  }
}

function renderQQProgress(task) {
  const box = $('#qq-progress');
  const fill = $('#qq-progress-fill');
  const text = $('#qq-progress-text');
  if (!box) return;
  // 任务结束后**收起进度条**，只在真正进行中或出错/超时时显示 ——
  // 否则部署完成后会留一条满格的绿条，看着像还没结束
  const show = !!task && (task.running || task.stage === 'error' || task.stage === 'timeout');
  if (!show) { box.hidden = true; return; }
  box.hidden = false;
  fill.style.width = (task.progress || 0) + '%';
  text.textContent = task.message || task.stage || '';
  text.style.color = task.stage === 'error' ? 'var(--danger)' : '';
  if (task.stage === 'waiting_login') {
    // 只等扫码，不是"已登录"——文案不能说过头
    text.textContent = '网关已启动，可快速登录或扫码';
    text.style.color = '';
  }
}

function renderQQBadge(st) {
  const badge = $('#qq-badge');
  if (!badge) return;
  const n = st.napcat || {};
  const task = st.task || {};
  if (task.running) { badge.className = 'wz-status warn'; badge.textContent = '部署中…'; }
  // 登录了但推送通道没连上 = 收不到消息，不能显示成"一切正常"
  else if (st.loggedIn && (st.reverseWsConnected || st.messageTransport === 'http_post')) {
    badge.className = 'wz-status ok'; badge.textContent = '已登录';
  } else if (st.loggedIn) {
    badge.className = 'wz-status warn'; badge.textContent = st.messageReceiverStarted ? '等待消息连接' : '已在线 · 待启动机器人';
  } else if (n.running) { badge.className = 'wz-status warn'; badge.textContent = '待登录'; }
  else { badge.className = 'wz-status'; badge.textContent = '待配置'; }
}

/** 轮询 QQ 部署任务 */
async function pollQQStatus(maxMs = 30 * 60 * 1000) {
  const t0 = Date.now();
  let st = null;
  while (Date.now() - t0 < maxMs) {
    st = await refreshQQStatus();
    if (!st) break;
    reportQQProgress(st.task?.message || (st.loggedIn?'QQ 已在线':'正在准备网关…'));
    if (!(st.task || {}).running) return st;
    await new Promise(r => setTimeout(r, 1500));
  }
  return st || await refreshQQStatus();
}

async function loadQQAccounts() {
  const select=$('#qq-quick-account'), hint=$('#qq-login-hint'), old=select.value;
  try {
    const r=await api('/api/qq/login/accounts');
    const accounts=r.accounts||[];
    STATE.qqAutoLoginQq=r.autoLoginQq||'';
    select.innerHTML='<option value="">'+(accounts.length?'选择已登录过的账号':'暂无保存的账号')+'</option>'+accounts.map(a=>`<option value="${esc(a.qq)}">${esc(a.nickname||'QQ')} · ${esc(a.qq)}</option>`).join('');
    select.value=accounts.some(a=>a.qq===old)?old:(r.autoLoginQq||accounts[0]?.qq||'');
    $('#btn-qq-quick').disabled=!r.ok||!accounts.length;
    hint.textContent=r.message||'账号列表已刷新';
    return accounts;
  } catch(e) { hint.textContent='账号列表获取失败：'+e.message; return []; }
}
async function quickLoginQQ(options = {}) {
  const qq=typeof options.qq==='string'?options.qq:$('#qq-quick-account').value;
  const remember=typeof options.remember==='boolean'?options.remember:$('#qq-quick-remember').checked;
  const buttons=[$('#btn-qq-quick'),$('#btn-qq-choice-quick')];
  if(!qq){toast('请先选择已保存的账号','err');return false;}
  buttons.forEach(btn=>btn.disabled=true);
  try {
    const r=await api('/api/qq/login/quick',{method:'POST',body:JSON.stringify({qq,remember})});
    $('#qq-login-hint').textContent=$('#qq-choice-status').textContent=r.message||'';
    reportQQProgress(r.message||'正在确认 QQ 登录状态…');
    if(!r.ok){toast(r.message||'请尝试扫码','err');return false;}
    $('#qq-choice-modal').hidden=true;
    if(r.loggedIn)await finishQQLogin();else startQQLoginPolling();
    return true;
  }catch(e){toast(e.message,'err');return false;}
  finally{buttons.forEach(btn=>btn.disabled=false);}
}
/** Wait until QR is generated AND decoded; never open an empty modal. */
let qqQrPending = null;
let qqQrGeneration = 0;
async function getQQQr() {
  if(qqQrPending)return qqQrPending;
  const generation=++qqQrGeneration;
  qqQrPending=requestQQQr(generation);
  try{return await qqQrPending;}finally{qqQrPending=null;}
}
async function requestQQQr(generation) {
  const btn=$('#btn-qq-qr'), hint=$('#qq-login-hint');
  btn.disabled=true;$('#btn-qq-get-qr').disabled=true;
  $('#qq-login-modal').hidden=true;
  hint.textContent='正在等待网关生成二维码…';reportQQProgress(hint.textContent);
  try {
    const r=await api('/api/qq/login/qrcode',{method:'POST'});
    if(generation!==qqQrGeneration)return false;
    if(r.loggedIn){await finishQQLogin();return true;}
    if(!r.ok||!r.image){hint.textContent=r.message||'二维码尚未准备好，请稍后再试';reportQQProgress(hint.textContent);toast(hint.textContent,'err');return false;}
    const img=new Image();img.alt='QQ 登录二维码';img.src=r.image;await img.decode();
    if(generation!==qqQrGeneration)return false;
    $('#qq-qr-box').replaceChildren(img);
    $('#qq-qr-status').textContent=r.message||'等待扫码';
    $('#qq-login-modal').hidden=false;$('#btn-close-qq-modal').focus();
    hint.textContent='二维码已准备好，请在弹窗扫码';reportQQProgress(hint.textContent);
    startQQLoginPolling();return true;
  }catch(e){hint.textContent='获取二维码失败：'+e.message;reportQQProgress(hint.textContent);toast(hint.textContent,'err');return false;}
  finally{btn.disabled=false;$('#btn-qq-get-qr').disabled=false;}
}
function closeQQDialogs() {qqQrGeneration++;$('#qq-login-modal').hidden=true;$('#qq-choice-modal').hidden=true;stopQQLoginPolling();}
function startQQLoginPolling() {
  stopQQLoginPolling();
  STATE.qqLoginPending=true;
  const t0=Date.now();let querying=false;
  STATE.qqLoginTimer=setInterval(async()=>{
    if(querying)return;
    if(Date.now()-t0>5*60*1000){stopQQLoginPolling();$('#qq-login-hint').textContent=$('#qq-qr-status').textContent='登录等待超时，请重试快速登录或扫码';reportQQProgress('QQ 登录等待超时，可在接入设置重试');return;}
    querying=true;
    try{
      const r=await api('/api/qq/login/check');
      $('#qq-login-hint').textContent=$('#qq-qr-status').textContent=r.message||'等待登录确认';
      reportQQProgress(r.message||'等待手机 QQ 确认');
      if(r.loggedIn){
        await finishQQLogin();toast('QQ 登录成功','ok');
      }
    }catch(_){/* transient query failures don't declare success */}
    finally{querying=false;}
  },1500);
}
function stopQQLoginPolling() {
  STATE.qqLoginPending=false;
  if(STATE.qqLoginTimer){clearInterval(STATE.qqLoginTimer);STATE.qqLoginTimer=null;}
}

/** 探测已有 OneBot 网关 */
async function detectQQ() {
  const btn = $('#btn-detect-qq');
  const hint = $('#qq-detect-hint');
  const statusBox = $('#qq-test-status');
  btn.disabled = true;
  hint.textContent = '正在扫描本机常见端口…';
  statusBox.className = 'test-status';
  statusBox.innerHTML = '';
  try {
    const r = await api('/api/qq/detect', { method: 'POST' });
    if (r.ok && r.found && r.found.length) {
      const item = r.found[0];
      hint.textContent = `发现网关：${item.url}${item.qq ? '（' + item.qq + '）' : ''}`;
      toast('已自动填充网关地址', 'ok');
      await loadConfig();
      await testQQ();
    } else {
      hint.textContent = '没探测到运行中的网关。可以直接点「一键部署 QQ 网关」。';
      statusBox.className = 'test-status err';
      statusBox.innerHTML = '<div class="ts-line"><span class="ts-icon">✗</span><span>未探测到 OneBot 网关</span></div>' +
        '<div class="ts-hint">点左侧「一键部署 QQ 网关」让程序自动装一个。</div>';
    }
  } catch (e) {
    hint.textContent = '探测失败：' + e.message;
  }
  btn.disabled = false;
}

async function testQQ() {
  const btn = $('#btn-test-qq');
  const box = $('#qq-test-status');
  btn.disabled = true;
  btn.textContent = '测试中…';
  box.className = 'test-status';
  box.innerHTML = '<div class="ts-line">正在连接…</div>';

  try {
    const tok = $('#onebot-token').value.trim();
    if (!await saveSection('channels', true)) throw new Error('通道设置未保存');
    const r = await api('/api/qq/test', {
      method: 'POST',
      body: JSON.stringify({
        http_url: $('#onebot-http').value.trim(),
        // 掩码值不要回传（那会把真实 Token 覆盖成 ••••1234）
        token: tok.includes('•') ? '' : tok,
      }),
    });
    if (r.ok) {
      box.className = 'test-status ok';
      box.innerHTML = `
        <div class="ts-line"><span class="ts-icon">✓</span><span>${esc(r.message)}</span></div>
        ${r.hint ? `<div class="ts-hint">${esc(r.hint)}</div>` : ''}`;
    } else {
      box.className = 'test-status err';
      box.innerHTML = `
        <div class="ts-line"><span class="ts-icon">✗</span><span>${esc(r.message)}</span></div>
        ${r.detail ? `<div class="ts-detail">${esc(r.detail)}</div>` : ''}
        ${r.hint ? `<div class="ts-hint">${esc(r.hint)}</div>` : ''}`;
    }
    await loadConfig();
  } catch (e) {
    box.className = 'test-status err';
    box.innerHTML = `<div class="ts-line"><span class="ts-icon">✗</span><span>测试失败：${esc(e.message)}</span></div>`;
  }
  btn.disabled = false;
  btn.textContent = '测试连接';
}

/** 查看 / 隐藏 QQ 网关日志 */
async function toggleQQLogs() {
  const box = $('#qq-logview');
  const btn = $('#btn-qq-logs');
  if (!box.hidden) { box.hidden = true; btn.textContent = '查看网关日志'; return; }
  box.hidden = false;
  box.textContent = '正在读取日志…';
  btn.textContent = '收起日志';
  try {
    const r = await api('/api/qq/logs?lines=120');
    box.textContent = r.log || '(无日志)';
  } catch (e) {
    box.textContent = '读取失败：' + e.message;
  }
}

/* ══════════ 人格知识库 ══════════ */

let memoryItems = [];

async function loadMemory() {
  const kw = ($('#mem-search') || {}).value || '';
  const q = new URLSearchParams();
  if (kw) q.set('keyword', kw);
  if ($('#mem-history').checked) q.set('include_history', 'true');
  let data;
  try {
    data = await api('/api/memory?' + q.toString());
  } catch (e) {
    toast('读取知识库失败：' + e.message, 'err');
    return;
  }
  memoryItems = data.items || [];
  renderMemoryStats({...data.stats, categories:data.categories});
  renderMemoryList(memoryItems);
  await loadForgotten();
  $('#identity-list').innerHTML = (data.identities || []).map(it => `<div>${it.accounts.map(a => `${a.platform === 'ilink' ? '微信' : 'QQ'} ${esc(a.owner_id)}`).join(' ↔ ')} <button class="btn btn-ghost btn-sm" data-unlink="${esc(it.id)}">解除关联</button></div>`).join('') || '尚未关联；手动通用记忆始终对两个通道生效。';
  $$('#identity-list [data-unlink]').forEach(b => b.addEventListener('click', async () => {
    if (!await confirmAction('解除后按原账号分别使用记忆，来源记录会保留。确定解除？')) return;
    await api(`/api/memory/identities/${encodeURIComponent(b.dataset.unlink)}`, {method:'DELETE'});
    await loadMemory();
  }));

  const badge = $('#nav-memory-count');
  if (badge) {
    const n = (data.stats || {}).total || 0;
    badge.hidden = !n;
    badge.textContent = n;
  }
}

async function loadForgotten() {
  const box = $('#memory-forgotten');
  try {
    const data = await api('/api/memory/forgotten');
    box.innerHTML = (data.items || []).map(item => `<div class="memory-item"><div class="mem-main"><div class="mem-content">${esc(item.content || item.fact_key || '此范围内已清空的了解')}</div><div class="mem-meta">遗忘于 ${esc(fmtMemoryTime(item.created_at))}</div></div><button class="btn btn-ghost btn-sm" data-relearn="${esc(item.id)}">允许今后重新学习</button></div>`).join('') || '<div class="hint" style="padding:0 20px 16px">尚无明确遗忘标记。</div>';
    $$('#memory-forgotten [data-relearn]').forEach(button => button.addEventListener('click', async () => {
      if (!await confirmAction('允许今后的新对话重新学习这部分内容？不会恢复旧记忆。')) return;
      button.disabled = true;
      try {
        await api(`/api/memory/forgotten/${encodeURIComponent(button.dataset.relearn)}`, {method:'DELETE'});
        toast('已允许今后重新学习，旧记忆保持删除', 'ok');
        await loadForgotten();
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
      finally { button.disabled = false; }
    }));
  } catch (e) { box.textContent = '读取遗忘标记失败：' + e.message; }
}

function renderMemoryStats(s) {
  $('#mem-total').textContent = s.total || 0;
  $('#mem-total').title = `另有 ${s.historical || 0} 条历史或过期信息；形成中 ${s.phases?.candidate || 0}，已熟悉 ${s.phases?.stable || 0}，已淡化 ${s.phases?.dormant || 0}，已固定 ${s.phases?.pinned || 0}`;
  $('#mem-owners').textContent = `${s.owners || 0} 个对象`;
  $('#mem-conf').textContent = s.avgConfidence ? Math.round(s.avgConfidence * 100) + '%' : '—';
  $('#mem-enabled').textContent = s.enabled ? '已开启' : '已关闭';
  const learning = s.learning || {};
  const labels = { idle: '等待对话', queued: '等待空闲整理', collecting: '积累经历中', consolidating: '后台整理中', extracting: '后台整理中', learned: '候选已整理', empty: '本批次没有长期信息', retry_wait: '抽取失败，等待重试', save_failed: '写盘失败', disabled: '自动积累已关闭', unconfigured: '抽取模型未配置' };
  const detail = `${labels[learning.state] || '等待对话'} · 待整理 ${learning.pending_turns || 0} 轮 · 已完成 ${learning.completed || 0} 批 · 模型 ${learning.model || '—'}`;
  $('#memory-learning-status').textContent = detail + (learning.last_error ? ` · ${learning.last_error}` : '');
  const cats = s.byCategory || {};
  const n = Object.keys(cats).length;
  $('#mem-cats').textContent = n;
  const labelMap = s.categories || {};
  $('#mem-cats-foot').textContent = n
    ? Object.entries(cats).map(([k, v]) => `${labelMap[k] || k} ${v}`).join(' · ')
    : '—';
}

function fmtMemoryTime(value) {
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN');
}
function memoryEvidenceMarkup(item) {
  const sources = Array.isArray(item.evidence_sources) ? item.evidence_sources : [];
  const count = item.evidence_count ?? sources.length;
  const episodes = item.independent_episodes ?? new Set(sources.map(s => s.episode_id).filter(Boolean)).size;
  const detail = sources.slice(-3).map(s => `${fmtMemoryTime(s.observed_at)}：${s.text || ''}`).join('；') || item.evidence || '';
  return `<span title="${esc(detail)}">用户依据 ${Number(count) || 0} 次 · 独立对话 ${Number(episodes) || 0} 次</span>`;
}

function renderMemoryList(items) {
  const box = $('#memory-list');
  if (!items.length) {
    box.innerHTML = `<div class="empty">
      ${ICON_EMPTY}
      <span>还没有记忆<br>经过长期互动会慢慢形成了解，也可以点「手动添加」立即固定</span>
    </div>`;
    return;
  }
  box.innerHTML = items.map(it => `
    <div class="memory-item" data-id="${esc(it.id)}">
      <span class="mem-cat ${esc(it.category)}">${esc(it.categoryLabel || it.category)}</span>
      <div class="mem-main">
        <div class="mem-content">${esc(it.content)}</div>
        <div class="mem-meta">
          ${(it.keywords || []).slice(0, 6).map(k => `<span class="kw">${esc(k)}</span>`).join('')}
          <span class="mem-conf">
            置信度
            <span class="bar"><i style="width:${Math.round((it.confidence || 0) * 100)}%"></i></span>
            ${Math.round((it.confidence || 0) * 100)}%
          </span>
          <span>${esc(it.phaseLabel || '形成中')}</span>
          ${it.status && it.status !== 'active' ? `<span>${esc(it.stateLabel || '历史版本')}</span>` : ''}
          <span>来源 ${esc(it.platform === 'ilink' ? '微信' : it.platform === 'onebot' ? 'QQ' : '手动/旧版')} · ${esc(it.owner_id || '通用')}</span>
          ${memoryEvidenceMarkup(it)}
          <span>使用 ${it.hits || 0} 次</span>
          <span>用户证据 ${esc(it.last_user_evidence_at ? fmtMemoryTime(it.last_user_evidence_at) : (it.updatedAt || '未知'))}</span>
          <span>当前强度 ${Math.round((it.strength ?? it.confidence ?? 0) * 100)}%</span>
        </div>
      </div>
      <div class="mem-actions">
        <button class="btn btn-ghost btn-sm" data-pin="${esc(it.id)}" data-pinned="${it.pinned ? 'true' : 'false'}">${it.pinned ? '解除固定' : '固定'}</button>
        <button class="btn btn-ghost btn-sm" data-del="${esc(it.id)}">删除</button>
      </div>
    </div>`).join('');

  $$('#memory-list [data-pin]').forEach(b => b.addEventListener('click', async () => {
    const pinned = b.dataset.pinned !== 'true';
    b.disabled = true;
    try {
      await api(`/api/memory/${encodeURIComponent(b.dataset.pin)}`, {method:'PATCH', body:JSON.stringify({pinned})});
      toast(pinned ? '已固定，直到手动修改或删除' : '已解除固定，恢复渐进生命周期', 'ok');
      await loadMemory();
    } catch (e) { toast('保存失败：' + e.message, 'err'); }
    finally { b.disabled = false; }
  }));
  $$('#memory-list [data-del]').forEach(b =>
    b.addEventListener('click', async () => {
      if (!await confirmAction('确定遗忘这条内容？已忘内容会停止用于上下文、摘要和待回复，旧数据导入也不会自动重新学回。聊天记录的物理删除需另行操作。')) return;
      await api(`/api/memory/${encodeURIComponent(b.dataset.del)}`, { method: 'DELETE' });
      toast('已删除', 'ok');
      loadMemory();
    }));
}

let memCategory = 'fact';

function openMemoryModal() {
  $('#mm-content').value = '';
  $('#mm-owner').value = '';
  $('#mm-platform').value = '';
  memCategory = 'fact';
  $$('#mm-category button').forEach(b =>
    b.classList.toggle('active', b.dataset.value === 'fact'));
  $('#memory-modal').hidden = false;
  setTimeout(() => $('#mm-content').focus(), 30);
}

async function saveMemory() {
  const content = $('#mm-content').value.trim();
  if (!content) {
    toast('请先写清楚要记住什么', 'err');
    $('#mm-content').focus();
    return;
  }
  const owner = $('#mm-owner').value.trim() || 'global';
  try {
    await api('/api/memory', {
      method: 'POST',
      body: JSON.stringify({
        scope: 'user', owner_id: owner, category: memCategory, content, platform: $('#mm-platform').value,
      }),
    });
    toast('已保存，将在相关话题中参考', 'ok');
    $('#memory-modal').hidden = true;
    loadMemory();
  } catch (e) {
    toast('添加失败：' + e.message, 'err');
  }
}

/* ══════════ 事件绑定 ══════════ */

function bind() {
  bindStatistics();
  bindMemoryGraph();
  $('#btn-storage-backup').addEventListener('click',backupStorage);
  $('#import-source').addEventListener('input',invalidateImportPreview);
  $('#btn-import-choose').addEventListener('click',chooseImportDirectory);
  $('#btn-import-preview').addEventListener('click',previewImportDirectory);
  $('#import-confirm').addEventListener('change',()=>{$('#btn-import-prepare').disabled=IMPORT.busy || IMPORT.queued || !IMPORT.preview || !$('#import-confirm').checked;});
  $('#btn-import-prepare').addEventListener('click',prepareImportDirectory);
  $('#btn-import-cancel').addEventListener('click',cancelImportDirectory);
  $('#btn-import-restart').addEventListener('click',restartForImport);
  $('#btn-copy-storage').addEventListener('click',()=>copyText($('#storage-path').value,$('#btn-copy-storage')));
  $('#btn-open-storage').addEventListener('click',()=>window.wxbotDesktop?.shell.openDataDir());
  $('#mem-history').addEventListener('change', loadMemory);
  $('#btn-identity-link').addEventListener('click', async () => {
    try {
      await api('/api/memory/identities', {method:'POST', body:JSON.stringify({wechat_id:$('#identity-wechat').value.trim(), qq_id:$('#identity-qq').value.trim()})});
      toast('已关联，两个平台将共享此人的长期记忆', 'ok');
      await loadMemory();
    } catch(e) { toast(e.message, 'err'); }
  });
  $$('.nav-item').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.page)));
  $$('[data-goto]').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.goto)));

  $('#btn-start').addEventListener('click', startBot);
  $('#btn-stop').addEventListener('click', stopBot);
  $('#btn-refresh').addEventListener('click', async () => {
    await refreshStatus();
    await loadRecent();
    if (STATE.page === 'messages') await loadMessages();
    if (STATE.page === 'statistics') await loadStatistics();
    if (STATE.page === 'memory-graph') await loadMemoryGraph();
    if (STATE.page === 'memory') await loadMemory();
    toast('已刷新');
  });

  // 平台卡片：勾选 / 取消
  $$('#adapter-type input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      CHANNELS[cb.value] = cb.checked;
      renderPlatformCards();
      applyChannels();
    });
  });
  // 整张卡片可点（label 已带 checkbox，这里只处理键盘可达性）
  $$('#adapter-type .pcard-head').forEach(el => {
    el.addEventListener('keydown', (e) => {
      if (e.key !== ' ' && e.key !== 'Enter') return;
      const cb = el.querySelector('input[type=checkbox]');
      if (!cb) return;
      e.preventDefault();
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });

  // QQ / OneBot
  $('#btn-setup-qq').addEventListener('click', setupQQ);
  $('#btn-detect-qq').addEventListener('click', detectQQ);
  $('#btn-test-qq').addEventListener('click', testQQ);
  $('#btn-qq-get-qr').addEventListener('click', getQQQr);
  $('#btn-qq-qr').addEventListener('click', getQQQr);
  $('#btn-qq-accounts').addEventListener('click', loadQQAccounts);
  $('#btn-qq-quick').addEventListener('click', quickLoginQQ);
  $('#btn-close-qq-modal').addEventListener('click',closeQQDialogs);
  $('#qq-login-modal').addEventListener('click', e=>{if(e.target.id==='qq-login-modal')closeQQDialogs();});
  $('#btn-qq-logs').addEventListener('click', toggleQQLogs);
  $('#btn-copy-ws').addEventListener('click', () => {
    const el = $('#qq-ws-url');
    copyText(el.textContent.trim(), $('#btn-copy-ws'));
  });

  // 微信官方通道扫码
  $('#btn-get-qr').addEventListener('click', getQR);
  $('#btn-check-login').addEventListener('click', checkLogin);

  // 人格知识库
  $('#btn-mem-refresh').addEventListener('click', loadMemory);
  $('#btn-mem-add').addEventListener('click', openMemoryModal);
  let memTimer = null;
  $('#mem-search').addEventListener('input', () => {
    clearTimeout(memTimer);
    memTimer = setTimeout(loadMemory, 300);
  });
  $$('#mm-category button').forEach(b => b.addEventListener('click', () => {
    memCategory = b.dataset.value;
    $$('#mm-category button').forEach(x => x.classList.toggle('active', x === b));
  }));
  $('#mm-save').addEventListener('click', saveMemory);
  $('#btn-mem-retry').addEventListener('click', async () => {
    try {
      const result = await api('/api/memory/learning/retry', { method: 'POST' });
      toast(result.ok ? `已重新排队 ${result.queued} 批` : result.message, result.ok ? 'ok' : 'err');
      await loadMemory();
    } catch (error) { toast('重试失败：' + error.message, 'err'); }
  });
  $('#mm-close').addEventListener('click', () => $('#memory-modal').hidden = true);
  $('#mm-cancel').addEventListener('click', () => $('#memory-modal').hidden = true);
  $('#memory-modal').addEventListener('click', e => {
    if (e.target.id === 'memory-modal') $('#memory-modal').hidden = true;
  });
  $('#mm-content').addEventListener('keydown', e => {
    // Ctrl/Cmd + Enter 直接提交
    if (!e.isComposing && e.keyCode !== 229 && (e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); saveMemory(); }
  });

  // 代码块复制按钮
  $$('[data-copy]').forEach(b =>
    b.addEventListener('click', (e) => {
      e.preventDefault();
      copyText(b.dataset.copy, b);
    }));

  // 消息筛选
  $$('#msg-filter button').forEach(b => b.addEventListener('click', () => {
    STATE.msgFilter = b.dataset.value;
    $$('#msg-filter button').forEach(x => x.classList.toggle('active', x === b));
    loadMessages();
  }));
  $('#btn-clear-msgs').addEventListener('click', async () => {
    if (!await confirmAction('确定清空所有消息记录？')) return;
    await api('/api/messages', { method: 'DELETE' });
    toast('已清空', 'ok');
    loadMessages();
  });

  $('#btn-forgotten-refresh').addEventListener('click', loadForgotten);
  $('#btn-proactive-refresh').addEventListener('click', loadProactive);

  // 设置
  $$('[data-save-section]').forEach(btn=>btn.addEventListener('click',()=>saveSection(btn.dataset.saveSection)));
  $$('[data-settings-section]').forEach(card=>{
    for(const event of ['input','change'])card.addEventListener(event,e=>{if(e.target.id && e.target.matches('input,select,textarea'))markSectionDirty(card.dataset.settingsSection);});
  });
  $('#btn-phrolova-preset').addEventListener('click', async () => {
    try {
      const data = await api('/api/persona/profiles');
      const preset = data.profiles.find(p => p.id === 'phrolova');
      $('#bot-name').value = preset.default_name;
      $('#bot-persona').value = preset.default_persona;
      $('#persona-profile').value = preset.id;
      $('#persona-relationship').value = 'neutral';
      markSectionDirty('persona');
      toast('弗洛洛预设已填入，请点本卡片的保存人格设置', 'ok');
    } catch (error) { toast('预设读取失败：' + error.message, 'err'); }
  });

  // 键盘
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { $('#memory-modal').hidden = true; closeQQDialogs(); }
  });
}

/* ══════════ 桌面版集成 ══════════ */

async function initDesktop() {
  const desk = window.wxbotDesktop;
  if (!desk) return;              // 浏览器模式：不显示桌面功能区

  document.body.classList.add('is-electron');
  let info = {};
  try { info = await desk.info(); } catch (_) { /* 拿不到就降级 */ }
  if (info.platform === 'darwin') document.body.classList.add('is-mac');

  $('#card-desktop').hidden = false;
  $('#desk-version').textContent = `v${info.version || '—'} · ${info.platform || ''}/${info.arch || ''}`;
  $('#desk-datadir').textContent = info.dataDir || '—';
  $('#desk-datadir').title = info.dataDir || '';
  updateBackendLabel(info.backendState);

  $('#desk-hide').addEventListener('click', () => {
    desk.hide();
    toast('已隐藏到后台，机器人继续运行');
  });

  $('#desk-quit').addEventListener('click', async () => {
    if (await confirmAction('确定要退出应用吗？\n\n退出后后台机器人也会停止，不再回复消息。')) {
      desk.quit();
    }
  });

  $('#desk-restart').addEventListener('click', async () => {
    toast('正在重启后台服务…');
    const r = await desk.backend.restart();
    toast(r.ok ? '后台服务已重启' : `重启失败：${r.error || '未知错误'}`,
          r.ok ? 'ok' : 'err');
    updateBackendLabel(r.ok ? 'running' : 'crashed');
  });

  $('#desk-data').addEventListener('click', () => desk.shell.openDataDir());
  $('#desk-logdir').addEventListener('click', () => desk.shell.openLogDir());

  // 运行日志折叠面板
  const logBox = $('#desk-logview');
  const logBtn = $('#desk-log');
  logBtn.addEventListener('click', async () => {
    if (!logBox.hidden) {
      logBox.hidden = true;
      logBtn.textContent = '查看运行日志';
      return;
    }
    const { log } = await desk.backend.log(300);
    logBox.textContent = log || '(暂无日志)';
    logBox.hidden = false;
    logBox.scrollTop = logBox.scrollHeight;
    logBtn.textContent = '收起日志';
  });

  // 主进程事件
  desk.on('backend:crashed', (p) => {
    updateBackendLabel('crashed');
    toast(`后台服务异常退出，${Math.round((p && p.retryInMs || 0) / 1000)} 秒后自动重启`, 'err');
  });
  desk.on('backend:restarted', () => updateBackendLabel('running'));
  desk.on('backend:ready', () => updateBackendLabel('running'));

  // 周期性同步后端状态
  setInterval(async () => {
    try {
      const s = await desk.backend.status();
      updateBackendLabel(s.state);
    } catch (_) { /* 忽略 */ }
  }, 5000);
}

function updateBackendLabel(state) {
  const map = { running: '运行中', starting: '启动中…', stopped: '已停止', crashed: '异常' };
  const el = $('#desk-backend');
  if (el) el.textContent = map[state] || state || '—';
}

async function loginAdmin() {
  // Token 只提交给同源后端；不写 localStorage、不放 URL、不显示在日志中。
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:#F4F6F8;display:grid;place-items:center';
  overlay.innerHTML = `<form style="width:340px;background:white;padding:28px;border-radius:14px">
    <h2>登录管理界面</h2>
    <p>请输入 WXBOT_ADMIN_TOKEN 或数据目录 admin-token 文件中的管理 Token。</p>
    <input type="password" required autocomplete="off" aria-label="管理 Token" style="width:100%;margin:14px 0">
    <button type="submit" class="btn btn-primary">登录</button><p role="alert"></p>
  </form>`;
  document.body.appendChild(overlay);
  const form = overlay.querySelector('form');
  const input = overlay.querySelector('input');
  input.focus();
  await new Promise(resolve => {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const button = form.querySelector('button');
      button.disabled = true;
      try {
        const response = await fetch('/api/auth/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: input.value }),
        });
        if (!response.ok) throw new Error('登录失败，请检查管理 Token');
        input.value = '';
        overlay.remove();
        resolve();
      } catch (error) {
        form.querySelector('[role=alert]').textContent = error.message;
      } finally { button.disabled = false; }
    });
  });
}

/* ══════════ 启动 ══════════ */

async function init() {
  bind();
  bindOnboarding();
  await initDesktop();          // 桌面版才生效，浏览器里直接跳过
  const authCheck = await fetch('/api/config');
  if (authCheck.status === 401) {
    await loginAdmin();
  } else if (!authCheck.ok) {
    throw new Error(`HTTP ${authCheck.status}`);
  }
  await loadConfig();
  await loadStorage();
  const onboarding = await refreshOnboarding();
  // 支持通过 #memory / #messages 等直达页面
  const hash = location.hash.replace('#', '');
  if (onboarding.shouldOpen) switchPage('guide');
  else if (hash && PAGE_META[hash]) switchPage(hash);
  await refreshStatus();
  if(STATE.config.adapter.qq_enabled)refreshQQStatus();
  await loadRecent();
  STATE.pollTimer = setInterval(() => {
    refreshStatus();
    if (STATE.page === 'overview') {loadRecent();if(STATE.config?.adapter.qq_enabled)refreshQQStatus();}
    if (STATE.page === 'guide') {refreshOnboarding().catch(()=>{});if(GUIDE.state?.qqEnabled&&GUIDE.step>=3)refreshQQStatus();}
  }, 3000);
}

init().catch(e => {
  console.error(e);
  toast('界面初始化失败，请刷新页面', 'err');
});
