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
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
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
  overview: ['概览', '查看机器人运行状态与今日数据'],
  connect:  ['接入设置', '配置通道并完成登录'],
  memory:   ['人格知识库', '机器人随对话积累的对用户的理解'],
  messages: ['消息记录', '查看收发消息与触发判定结果'],
  settings: ['模型与行为', '配置大模型与机器人性格'],
};

function switchPage(name) {
  if (!PAGE_META[name]) name = 'overview';
  STATE.page = name;
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === name));
  $$('.page').forEach(p => p.classList.toggle('active', p.id === `page-${name}`));
  const [title, sub] = PAGE_META[name];
  $('#page-title').textContent = title;
  $('#page-sub').textContent = sub;
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name === 'memory') loadMemory();
  if (name === 'messages') loadMessages();
  if (name === 'settings' || name === 'connect') loadConfig();

  // 接入设置页需要持续刷新 QQ 网关状态
  if (name === 'connect') startConnectPolling();
  else stopConnectPolling();
}

function startConnectPolling() {
  stopConnectPolling();
  refreshQQStatus();
  dockerPollTimer = setInterval(refreshQQStatus, 3000);
}

function stopConnectPolling() {
  if (dockerPollTimer) {
    clearInterval(dockerPollTimer);
    dockerPollTimer = null;
  }
  // 离开「接入设置」页要停掉扫码轮询，避免定时器泄漏
  stopQQLoginPolling();
}

window.addEventListener('hashchange', () => {
  const name = location.hash.replace('#', '');
  if (name && name !== STATE.page) switchPage(name);
});

/* ══════════ 状态轮询 ══════════ */

async function refreshStatus() {
  let s;
  try { s = await api('/api/status'); }
  catch { return; }

  const running = s.running;
  const stats = s.stats || {};

  // 侧边栏状态
  const chip = $('#side-status');
  chip.className = 'status-chip' + (running ? ' on' : (s.last_error ? ' err' : ''));
  chip.querySelector('span').textContent =
    running ? '运行中' : (s.last_error ? '启动失败' : '未运行');

  // 顶部按钮
  $('#btn-start').hidden = running;
  $('#btn-stop').hidden = !running;

  // 英雄区
  const hero = $('#hero');
  hero.classList.toggle('on', running);
  $('#hero-pulse').classList.toggle('on', running);
  $('#hero-title').textContent = running ? '机器人正在运行' : '机器人未运行';
  const rule = s.qq_running
    ? '私聊必回，QQ 群里 @ 它才回'
    : '私聊消息会自动回复';
  $('#hero-desc').textContent = running
    ? `正在接收消息：${rule}`
    : (s.last_error || '完成接入设置后点击「启动机器人」即可开始工作');

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
        ${scope}${tag}
        <span class="feed-time">${esc(m.time || '')}</span>
      </div>
      <div class="feed-text ${out ? 'out' : ''}">${esc(m.content)}</div>
    </div>
  </div>`;
}

/* ══════════ 接入设置 ══════════ */

const CHANNELS = { wechat: true, qq: false };

async function loadConfig() {
  const c = await api('/api/config');
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

  // 机器人行为
  $('#bot-name').value = c.bot.name || '';
  $('#bot-persona').value = c.bot.persona || '';
  $('#group-at-sender').checked = c.bot.group_reply_at_sender !== false;
  $('#memory-enabled').checked = c.bot.memory_enabled !== false;
  $('#memory-inject').checked = c.bot.memory_inject !== false;
  $('#memory-interval').value = c.bot.memory_extract_interval || 3;

  // 官方通道的登录态存在后端文件里，单独取一次
  if (CHANNELS.wechat) {
    try {
      STATE.ilinkStatus = await api('/api/ilink/status');
    } catch (_) { STATE.ilinkStatus = null; }
  } else {
    STATE.ilinkStatus = null;
  }

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
  c.adapter.bot_qq = $('#bot-qq').value.trim();
  c.adapter.bot_nickname = $('#qq-nickname').value.trim() ||
    $('#bot-name').value.trim();

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

  // 机器人行为
  c.bot.name = $('#bot-name').value.trim() || '小助手';
  c.bot.persona = $('#bot-persona').value.trim();
  c.bot.group_reply_at_sender = $('#group-at-sender').checked;
  c.bot.memory_enabled = $('#memory-enabled').checked;
  c.bot.memory_inject = $('#memory-inject').checked;
  c.bot.memory_extract_interval =
    Math.min(50, Math.max(1, Number($('#memory-interval').value) || 3));
  return c;
}

async function saveConfig() {
  try {
    const c = collectConfig();
    await api('/api/config', { method: 'PUT', body: JSON.stringify(c) });
    toast('配置已保存', 'ok');
    await loadConfig();
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  }
}

/* ══════════ 扫码登录 ══════════ */

async function getQR() {
  const box = $('#qr-box');
  box.innerHTML = '<div class="qr-empty"><span>正在获取二维码…</span></div>';
  let r;
  try {
    // ⚠️ 先落盘再取二维码：后端是按**保存的配置**判断该不该走官方通道的。
    // 用户可能刚勾了通道但还没点「保存」，不先保存就会报"通道未启用"。
    await api('/api/config', { method: 'PUT', body: JSON.stringify(collectConfig()) });
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
async function setupQQ() {
  const btn = $('#btn-setup-qq');
  const st0 = STATE.qqStatus || {};
  const dk = st0.docker || {};

  // 需要装 Docker 时才弹确认框
  if (!dk.installed) {
    const ok = confirm(
      '即将在你的电脑上安装 Docker。\n\n' +
      '过程中需要你操作的地方：\n· 按提示完成安装（可能要输入密码）\n\n' +
      '安装包约 600MB，是否继续？'
    );
    if (!ok) return;
  }

  btn.disabled = true;
  $('#qq-detect-hint').textContent = '正在自动部署，请稍候…';
  let failure = '';

  try {
    await api('/api/config', { method: 'PUT', body: JSON.stringify(collectConfig()) });

    // ① 先把 Docker 环境弄好
    if (!(dk.installed && dk.running)) {
      await api('/api/docker/prepare', { method: 'POST' });
      await pollQQStatus();
      const st = STATE.qqStatus || {};
      if (!(st.docker && st.docker.running)) {
        failure = 'Docker 还没就绪';
        toast('需要你先把 Docker 装好', 'err');
        return;
      }
    }

    // ② 部署 NapCat
    const r = await api('/api/qq/setup', { method: 'POST' });
    if (!r.ok) { failure = r.message; toast(r.message || '部署失败', 'err'); return; }

    const st = await pollQQStatus();
    const task = st.task || {};
    if (task.stage === 'error') {
      failure = task.message || task.error;
      toast('部署失败：' + failure, 'err');
      return;
    }
    if (task.needsUser) { toast('需要你手动完成一步操作', 'err'); return; }

    // ③ 部署完成后自动勾选 QQ 通道，并直接出二维码
    if (task.stage === 'waiting_login' || (st.napcat && st.napcat.running)) {
      CHANNELS.qq = true;
      $('#qq-qr-wrap').hidden = false;
      $('#qq-detect-hint').textContent = '部署完成。请用手机 QQ 扫码登录。';
      // 先保存（把 QQ 通道勾上、地址回填），再取二维码
      await saveConfigQuiet();
      await getQQQr();
      toast('QQ 网关已部署，请扫码登录', 'ok');
    } else {
      failure = task.message || '还没部署完成';
      toast('还没部署完成，请看下方提示', 'err');
    }
  } catch (e) {
    failure = e.message;
    toast('部署失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    if (failure) {
      $('#qq-detect-hint').textContent = '没部署成功：' + failure;
    } else if (!$('#qq-detect-hint').textContent.includes('完成')) {
      $('#qq-detect-hint').textContent =
        '自动拉取 NapCat 镜像、写好网络配置并启动，全程无需你操作';
    }
  }
}

/** 静默保存配置（不提示、不刷新表单），用于自动化流程中途落盘 */
async function saveConfigQuiet() {
  try {
    await api('/api/config', { method: 'PUT', body: JSON.stringify(collectConfig()) });
    await loadConfig();
    return true;
  } catch (e) {
    toast('保存配置失败：' + e.message, 'err');
    return false;
  }
}

/** 刷新 QQ 状态并渲染 */
async function refreshQQStatus() {
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
  return st;
}

function renderQQChecklist(st) {
  const d = st.docker || {};
  const n = st.napcat || {};
  const task = st.task || {};

  setCheck('qq-docker',
    d.installed ? (d.running ? 'ok' : 'err') : 'err',
    d.installed ? (d.running ? `已就绪 · v${d.version || '?'}` : '已安装但未启动') : '未安装');

  const busy = task.running && task.stage === 'napcat';
  setCheck('qq-container',
    n.running ? 'ok' : (busy ? 'busy' : (d.running ? 'err' : '')),
    n.running ? '正在运行 · 127.0.0.1:6099' : (busy ? '部署中…' : (n.message || '等待中')));

  setCheck('qq-login',
    st.loggedIn ? 'ok' : (n.running ? 'err' : ''),
    st.loggedIn ? ('已登录 ' + (st.botQq || '')) : (n.running ? '未登录，请扫码' : '等待网关就绪'));
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
    text.textContent = '部署完成，请用手机 QQ 扫码登录';
    text.style.color = '';
  }
}

function renderQQBadge(st) {
  const badge = $('#qq-badge');
  if (!badge) return;
  const n = st.napcat || {};
  const task = st.task || {};
  if (st.loggedIn) { badge.className = 'wz-status ok'; badge.textContent = '已登录'; }
  else if (task.running) { badge.className = 'wz-status warn'; badge.textContent = '部署中…'; }
  else if (n.running) { badge.className = 'wz-status warn'; badge.textContent = '待扫码'; }
  else { badge.className = 'wz-status'; badge.textContent = '待配置'; }
}

/** 轮询 QQ 部署任务 */
async function pollQQStatus(maxMs = 30 * 60 * 1000) {
  const t0 = Date.now();
  let st = null;
  while (Date.now() - t0 < maxMs) {
    st = await refreshQQStatus();
    if (!st) break;
    if (!(st.task || {}).running) return st;
    await new Promise(r => setTimeout(r, 1500));
  }
  return st || await refreshQQStatus();
}

/** 获取 QQ 登录二维码 */
async function getQQQr() {
  const box = $('#qq-qr-box');
  const wrap = $('#qq-qr-wrap');
  const status = $('#qq-qr-status');
  if (!box) return;
  wrap.hidden = false;
  box.innerHTML = '<div class="qr-empty"><span>正在获取二维码…</span></div>';
  try {
    const r = await api('/api/qq/login/qrcode', { method: 'POST' });
    if (!r.ok) {
      box.innerHTML = `<div class="qr-empty"><span>${esc(r.message || '获取失败')}</span>` +
        (r.hint ? `<p style="margin-top:8px;font-size:12px;color:var(--text-2);line-height:1.6;">${esc(r.hint)}</p>` : '') +
        '</div>';
      status.textContent = r.message || '获取失败';
      toast(r.message || '获取二维码失败', 'err');
      return;
    }
    box.innerHTML = `<img src="${esc(r.image)}" alt="QQ 登录二维码">`;
    status.textContent = r.message || '等待扫码…';
    startQQLoginPolling();
  } catch (e) {
    box.innerHTML = `<div class="qr-empty"><span>获取失败：${esc(e.message)}</span></div>`;
    toast('获取二维码失败：' + e.message, 'err');
  }
}

/** 扫码状态轮询：3 秒一次、上限 5 分钟；切页/异常都要停表 */
function startQQLoginPolling() {
  stopQQLoginPolling();
  const t0 = Date.now();
  STATE.qqLoginTimer = setInterval(async () => {
    if (Date.now() - t0 > 5 * 60 * 1000) {
      stopQQLoginPolling();
      $('#qq-qr-status').textContent = '二维码已过期，请重新获取';
      return;
    }
    if (STATE.page !== 'connect') { stopQQLoginPolling(); return; }
    try {
      const r = await api('/api/qq/login/check');
      if (r.loggedIn) {
        stopQQLoginPolling();
        $('#qq-qr-status').textContent = '✅ 登录成功' + (r.qq ? `（${r.qq}）` : '');
        toast('QQ 登录成功', 'ok');
        await loadConfig();
        await refreshQQStatus();
      } else {
        $('#qq-qr-status').textContent = r.message || '等待扫码…';
      }
    } catch (_) { /* 单次失败不打断轮询 */ }
  }, 3000);
}

function stopQQLoginPolling() {
  if (STATE.qqLoginTimer) { clearInterval(STATE.qqLoginTimer); STATE.qqLoginTimer = null; }
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
    await api('/api/config', { method: 'PUT', body: JSON.stringify(collectConfig()) });
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
  let data;
  try {
    data = await api('/api/memory?' + q.toString());
  } catch (e) {
    toast('读取知识库失败：' + e.message, 'err');
    return;
  }
  memoryItems = data.items || [];
  renderMemoryStats(data.stats || {});
  renderMemoryList(memoryItems);

  const badge = $('#nav-memory-count');
  if (badge) {
    const n = (data.stats || {}).total || 0;
    badge.hidden = !n;
    badge.textContent = n;
  }
}

function renderMemoryStats(s) {
  $('#mem-total').textContent = s.total || 0;
  $('#mem-owners').textContent = `${s.owners || 0} 个对象`;
  $('#mem-conf').textContent = s.avgConfidence ? Math.round(s.avgConfidence * 100) + '%' : '—';
  $('#mem-enabled').textContent = s.enabled ? '已开启' : '已关闭';
  const cats = s.byCategory || {};
  const n = Object.keys(cats).length;
  $('#mem-cats').textContent = n;
  const labelMap = s.categories || {};
  $('#mem-cats-foot').textContent = n
    ? Object.entries(cats).map(([k, v]) => `${labelMap[k] || k} ${v}`).join(' · ')
    : '—';
}

function renderMemoryList(items) {
  const box = $('#memory-list');
  if (!items.length) {
    box.innerHTML = `<div class="empty">
      ${ICON_EMPTY}
      <span>还没有记忆<br>机器人运行后会自动从对话中积累，也可以点「手动添加」补录</span>
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
          <span>使用 ${it.hits || 0} 次</span>
          <span>更新于 ${esc(it.updatedAt || '')}</span>
        </div>
      </div>
      <div class="mem-actions">
        <button class="btn btn-ghost btn-sm" data-del="${esc(it.id)}">删除</button>
      </div>
    </div>`).join('');

  $$('#memory-list [data-del]').forEach(b =>
    b.addEventListener('click', async () => {
      if (!confirm('确定删除这条记忆？')) return;
      await api(`/api/memory/${encodeURIComponent(b.dataset.del)}`, { method: 'DELETE' });
      toast('已删除', 'ok');
      loadMemory();
    }));
}

let memCategory = 'fact';

function openMemoryModal() {
  $('#mm-content').value = '';
  $('#mm-owner').value = '';
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
        scope: 'user', owner_id: owner, category: memCategory, content,
      }),
    });
    toast('已添加，机器人下次回答就会参考', 'ok');
    $('#memory-modal').hidden = true;
    loadMemory();
  } catch (e) {
    toast('添加失败：' + e.message, 'err');
  }
}

/* ══════════ 事件绑定 ══════════ */

function bind() {
  $$('.nav-item').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.page)));
  $$('[data-goto]').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.goto)));

  $('#btn-start').addEventListener('click', startBot);
  $('#btn-stop').addEventListener('click', stopBot);
  $('#btn-refresh').addEventListener('click', async () => {
    await refreshStatus();
    await loadRecent();
    if (STATE.page === 'messages') await loadMessages();
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
  $('#btn-qq-qr').addEventListener('click', async () => {
    const wrap = $('#qq-qr-wrap');
    wrap.hidden = false;
    wrap.scrollIntoView({ behavior: 'smooth', block: 'center' });
    await getQQQr();
  });
  $('#btn-qq-logs').addEventListener('click', toggleQQLogs);
  $('#btn-save-qq').addEventListener('click', async () => {
    await saveConfig();
    await loadConfig();
    toast('已保存', 'ok');
  });
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
  $('#mm-close').addEventListener('click', () => $('#memory-modal').hidden = true);
  $('#mm-cancel').addEventListener('click', () => $('#memory-modal').hidden = true);
  $('#memory-modal').addEventListener('click', e => {
    if (e.target.id === 'memory-modal') $('#memory-modal').hidden = true;
  });
  $('#mm-content').addEventListener('keydown', e => {
    // Ctrl/Cmd + Enter 直接提交
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') saveMemory();
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
    if (!confirm('确定清空所有消息记录？')) return;
    await api('/api/messages', { method: 'DELETE' });
    toast('已清空', 'ok');
    loadMessages();
  });

  // 设置
  $('#btn-save-settings').addEventListener('click', saveConfig);

  // 键盘
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') $('#memory-modal').hidden = true;
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

  $('#desk-quit').addEventListener('click', () => {
    if (confirm('确定要退出应用吗？\n\n退出后后台机器人也会停止，不再回复消息。')) {
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

/* ══════════ 启动 ══════════ */

async function init() {
  bind();
  await initDesktop();          // 桌面版才生效，浏览器里直接跳过
  await loadConfig();
  // 支持通过 #memory / #messages 等直达页面
  const hash = location.hash.replace('#', '');
  if (hash && PAGE_META[hash]) switchPage(hash);
  await refreshStatus();
  await loadRecent();
  STATE.pollTimer = setInterval(() => {
    refreshStatus();
    if (STATE.page === 'overview') loadRecent();
  }, 3000);
}

init().catch(e => {
  console.error(e);
  toast('界面初始化失败，请刷新页面', 'err');
});
