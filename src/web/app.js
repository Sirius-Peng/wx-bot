/* ═══════════════════════════════════════════════════════════════
   微信 AI 助手 · 管理界面
   ═══════════════════════════════════════════════════════════════ */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

let STATE = {
  config: null,
  groups: [],
  page: 'overview',
  msgFilter: '',
  editingGroup: null,
  qrTimer: null,
  pollTimer: null,
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
  connect:  ['接入设置', '配置微信通道并扫码登录'],
  groups:   ['群聊管理', '为每个群单独设置响应策略'],
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
  if (name === 'groups') loadGroups();
  if (name === 'messages') loadMessages();
  if (name === 'settings' || name === 'connect') loadConfig();
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
  $('#hero-desc').textContent = running
    ? '正在监听微信消息，按触发策略智能回复'
    : (s.last_error || '完成接入设置后点击「启动机器人」即可开始工作');
  $('#hero-platform').textContent = s.platform || '—';
  $('#hero-groupcap').textContent = running
    ? (s.supports_group ? '支持群聊' : '仅私聊')
    : '—';
  $('#hero-uptime').textContent = fmtUptime(s.uptime);

  // 统计
  $('#st-in').textContent = stats.total_in || 0;
  $('#st-in-foot').textContent = `群 ${stats.group_count || 0} · 私聊 ${stats.private_count || 0}`;
  $('#st-out').textContent = stats.total_out || 0;
  $('#st-out-foot').textContent = `回复率 ${stats.reply_rate || 0}%`;
  $('#st-silent').textContent = stats.silent || 0;
  $('#st-chats').textContent = (stats.group_count || 0) + (stats.private_count || 0);
  $('#st-chats-foot').textContent = `群 ${stats.group_count || 0} · 私聊 ${stats.private_count || 0}`;

  // 群数量徽标
  const badge = $('#nav-group-count');
  if (stats.group_count) {
    badge.hidden = false;
    badge.textContent = stats.group_count;
  } else {
    badge.hidden = true;
  }
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

/* ══════════ 群聊回复策略 ══════════ */

const MODE_HINT = {
  mention_only: {
    title: '仅 @ 机器人时回复',
    body: '私聊<b>一律回复</b>；群聊<b>只有 @ 了机器人才回复</b>，其余消息一概不回。<br>最省心、最不容易打扰群友，推荐绝大多数场景使用。',
  },
  smart: {
    title: '智能判断（自动找时机插话）',
    body: '除 @ 外，还会用启发式打分 + 小模型判定主动找时机插话，<br>最终约 <b>1~2%</b> 的群消息会被回复。适合希望机器人更活跃的场景。',
  },
};

let groupMode = 'mention_only';

function applyGroupMode(mode) {
  if (!MODE_HINT[mode]) mode = 'mention_only';
  groupMode = mode;
  $$('#group-mode button').forEach(b =>
    b.classList.toggle('active', b.dataset.value === mode));
  const hint = MODE_HINT[mode];
  $('#mode-hint').innerHTML = `<strong>${hint.title}</strong><p>${hint.body}</p>`;
  // 回复概率与静默时段只在智能模式下有意义
  $('#smart-only-fields').hidden = mode !== 'smart';
}

/* ══════════ 接入设置 ══════════ */

let adapterType = 'gewechat';

async function loadConfig() {
  const c = await api('/api/config');
  STATE.config = c;

  // 通道
  adapterType = c.adapter.type || 'gewechat';
  $$('#adapter-type button').forEach(b =>
    b.classList.toggle('active', b.dataset.value === adapterType));
  $('#hybrid-enabled').checked = !!c.adapter.hybrid_enabled;
  $('#gewe-base').value = c.adapter.gewe_base_url || '';
  $('#gewe-token').value = c.adapter.gewe_token || '';
  $('#gewe-appid').value = c.adapter.gewe_app_id || '';
  $('#bot-wxid').value = c.adapter.bot_wxid || '';
  $('#bot-nickname').value = c.adapter.bot_nickname || '';

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

  // 行为
  $('#bot-name').value = c.bot.name || '';
  $('#bot-persona').value = c.bot.persona || '';
  $('#group-at-sender').checked = !!c.bot.group_reply_at_sender;
  applyGroupMode(c.bot.group_reply_mode || 'mention_only');
  const prob = Math.round((c.bot.reply_prob ?? 0.7) * 100);
  $('#reply-prob').value = prob;
  $('#reply-prob-val').textContent = prob + '%';
  $('#quiet-hours').value = (c.bot.quiet_hours || []).join(',');

  // 官方通道时隐藏 GeWe 卡片
  $('#card-gewe').style.display = adapterType === 'gewechat' ? '' : 'none';
}

function collectConfig() {
  const c = JSON.parse(JSON.stringify(STATE.config));
  c.adapter.type = adapterType;
  c.adapter.hybrid_enabled = $('#hybrid-enabled').checked;
  c.adapter.gewe_base_url = $('#gewe-base').value.trim();
  c.adapter.gewe_token = $('#gewe-token').value.trim();
  c.adapter.gewe_app_id = $('#gewe-appid').value.trim();
  c.adapter.bot_wxid = $('#bot-wxid').value.trim();
  c.adapter.bot_nickname = $('#bot-nickname').value.trim();

  c.llm.primary_base = $('#primary-base').value.trim();
  c.llm.primary_model = $('#primary-model').value.trim();
  c.llm.primary_key = $('#primary-key').value.trim();
  c.llm.fallback_base = $('#fallback-base').value.trim();
  c.llm.fallback_model = $('#fallback-model').value.trim();
  c.llm.fallback_key = $('#fallback-key').value.trim();
  c.llm.judge_base = $('#judge-base').value.trim();
  c.llm.judge_model = $('#judge-model').value.trim();
  c.llm.judge_key = $('#judge-key').value.trim();

  c.bot.name = $('#bot-name').value.trim();
  c.bot.persona = $('#bot-persona').value.trim();
  c.bot.group_reply_mode = groupMode;
  c.bot.group_reply_at_sender = $('#group-at-sender').checked;
  c.bot.reply_prob = Number($('#reply-prob').value) / 100;
  c.bot.quiet_hours = $('#quiet-hours').value
    .split(',').map(x => parseInt(x.trim(), 10)).filter(x => !isNaN(x));
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
  const r = await api('/api/login/qrcode', { method: 'POST' });
  if (!r.ok) {
    box.innerHTML = `<div class="qr-empty"><span>${esc(r.message || '获取失败')}</span></div>`;
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
  STATE.qrTimer = setInterval(checkLogin, 2500);
}

async function checkLogin() {
  const r = await api('/api/login/check');
  if (r.status === 'confirmed') {
    clearInterval(STATE.qrTimer);
    $('#qr-status').textContent = '✅ 登录成功';
    toast('登录成功', 'ok');
    await loadConfig();
    await refreshStatus();
  } else if (r.status === 'failed') {
    clearInterval(STATE.qrTimer);
    $('#qr-status').textContent = '登录失败：' + (r.message || '');
  } else {
    $('#qr-status').textContent = r.message || '等待扫码…';
  }
}

/* ══════════ 群管理 ══════════ */

const MODE_LABEL = {
  '': '跟随全局',
  smart: '智能判断',
  mention_only: '仅 @ 响应',
  keyword_only: '仅关键词',
};

async function loadGroups() {
  const { groups } = await api('/api/groups');
  STATE.groups = groups;
  const box = $('#groups-list');
  if (!groups.length) {
    box.innerHTML = `<div class="empty">${ICON_EMPTY}<span>还没有发现群聊<br>把机器人拉进微信群并有人在群里说话后，这里会自动出现</span></div>`;
    return;
  }
  box.innerHTML = groups.map(g => {
    const initial = (g.name || '?')[0];
    const tags = [
      `<span class="tag ${g.enabled ? 'on' : 'off'}">${g.enabled ? '已启用' : '已停用'}</span>`,
      `<span class="tag">${esc(MODE_LABEL[g.mode] || g.mode)}</span>`,
      `<span class="tag">冷却 ${g.cooldown_sec}s</span>`,
      `<span class="tag">每小时 ${g.hourly_limit} 条</span>`,
      `<span class="tag">回复率 ${Math.round(g.reply_prob * 100)}%</span>`,
      !g.configured ? '<span class="tag info">待配置</span>' : '',
    ].join('');
    return `<div class="group-row">
      <div class="group-avatar">${esc(initial)}</div>
      <div class="group-main">
        <div class="group-name">${esc(g.name || g.chat_id)}</div>
        <div class="group-meta">${tags}</div>
      </div>
      <div class="group-actions">
        <button class="btn btn-ghost btn-sm" data-edit="${esc(g.chat_id)}">设置</button>
      </div>
    </div>`;
  }).join('');

  $$('#groups-list [data-edit]').forEach(b =>
    b.addEventListener('click', () => openGroupModal(b.dataset.edit)));
}

function openGroupModal(chatId) {
  const g = STATE.groups.find(x => x.chat_id === chatId);
  if (!g) return;
  STATE.editingGroup = g;

  $('#gm-title').textContent = `群聊设置 · ${g.name || chatId}`;
  $('#gm-enabled').checked = !!g.enabled;
  $('#gm-wake').value = (g.wake_words || []).join(', ');
  $('#gm-cooldown').value = g.cooldown_sec;
  $('#gm-hourly').value = g.hourly_limit;
  const prob = Math.round((g.reply_prob ?? 0.7) * 100);
  $('#gm-prob').value = prob;
  $('#gm-prob-val').textContent = prob + '%';
  $('#gm-persona').value = g.persona || '';
  $$('#gm-mode button').forEach(b =>
    b.classList.toggle('active', b.dataset.value === (g.mode ?? '')));
  $('#gm-prob-field').hidden = (g.mode || groupMode) !== 'smart';

  $('#group-modal').hidden = false;
}

async function saveGroup() {
  const g = STATE.editingGroup;
  if (!g) return;
  const mode = ($('#gm-mode button.active') || {}).dataset?.value ?? '';
  const body = {
    name: g.name || '',
    enabled: $('#gm-enabled').checked,
    mode,
    wake_words: $('#gm-wake').value.split(',').map(s => s.trim()).filter(Boolean),
    cooldown_sec: Number($('#gm-cooldown').value) || 0,
    hourly_limit: Number($('#gm-hourly').value) || 60,
    reply_prob: Number($('#gm-prob').value) / 100,
    persona: $('#gm-persona').value.trim(),
  };
  try {
    await api(`/api/groups/${encodeURIComponent(g.chat_id)}`, {
      method: 'PUT', body: JSON.stringify(body),
    });
    toast('群设置已保存', 'ok');
    $('#group-modal').hidden = true;
    await loadGroups();
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
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

/* ══════════ 事件绑定 ══════════ */

function bind() {
  $$('.nav-item').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.page)));
  $$('[data-goto]').forEach(b => b.addEventListener('click', () => switchPage(b.dataset.goto)));

  $('#btn-start').addEventListener('click', startBot);
  $('#btn-stop').addEventListener('click', stopBot);
  $('#btn-refresh').addEventListener('click', async () => {
    await refreshStatus();
    await loadRecent();
    if (STATE.page === 'groups') await loadGroups();
    if (STATE.page === 'messages') await loadMessages();
    toast('已刷新');
  });

  // 通道类型
  $$('#adapter-type button').forEach(b => b.addEventListener('click', () => {
    adapterType = b.dataset.value;
    $$('#adapter-type button').forEach(x => x.classList.toggle('active', x === b));
    $('#card-gewe').style.display = adapterType === 'gewechat' ? '' : 'none';
  }));
  $('#btn-save-connect').addEventListener('click', saveConfig);

  // 群聊回复策略
  $$('#group-mode button').forEach(b =>
    b.addEventListener('click', () => applyGroupMode(b.dataset.value)));

  // 扫码
  $('#btn-get-qr').addEventListener('click', getQR);
  $('#btn-check-login').addEventListener('click', checkLogin);

  // 群
  $('#btn-refresh-groups').addEventListener('click', loadGroups);
  $('#btn-add-group').addEventListener('click', () => {
    const cid = prompt('请输入群 ID（形如 12345678@chatroom）');
    if (!cid) return;
    STATE.groups.push({
      chat_id: cid.trim(), name: cid.trim(), configured: false, enabled: true,
      mode: 'smart', wake_words: ['小助手'], cooldown_sec: 60,
      hourly_limit: 8, reply_prob: 0.7, persona: '',
    });
    openGroupModal(cid.trim());
  });
  $('#gm-close').addEventListener('click', () => $('#group-modal').hidden = true);
  $('#gm-cancel').addEventListener('click', () => $('#group-modal').hidden = true);
  $('#gm-save').addEventListener('click', saveGroup);
  $('#gm-prob').addEventListener('input', e => $('#gm-prob-val').textContent = e.target.value + '%');
  $$('#gm-mode button').forEach(b => b.addEventListener('click', () => {
    $$('#gm-mode button').forEach(x => x.classList.toggle('active', x === b));
    // 回复概率只在智能模式下有意义
    $('#gm-prob-field').hidden = (b.dataset.value || groupMode) !== 'smart';
  }));
  $('#group-modal').addEventListener('click', e => {
    if (e.target.id === 'group-modal') $('#group-modal').hidden = true;
  });

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
  $('#reply-prob').addEventListener('input', e =>
    $('#reply-prob-val').textContent = e.target.value + '%');

  // 键盘
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') $('#group-modal').hidden = true;
  });
}

/* ══════════ 启动 ══════════ */

async function init() {
  bind();
  await loadConfig();
  // 支持通过 #groups / #messages 等直达页面
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
