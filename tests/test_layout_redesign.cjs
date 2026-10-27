'use strict';

// DOM structure and interaction contracts only. Visual alignment is verified in
// the browser separately; these tests deliberately do not pin colours or pixels.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const root = process.env.WXBOT_TEST_ROOT || path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'src/web/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'src/web/app.js'), 'utf8');
const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img',
  'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);

function parseHTML(text) {
  const document = { tag: '#document', attrs: {}, children: [], parent: null };
  const stack = [document];
  for (const match of text.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/?[a-zA-Z][^>]*>/g)) {
    const token = match[0];
    if (token.startsWith('<!')) continue;
    const closing = /^<\//.test(token);
    const tag = token.match(/^<\/?([\w-]+)/)[1].toLowerCase();
    if (closing) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tag === tag) { stack.length = i; break; }
      }
      continue;
    }
    const attrs = {};
    const remainder = token.slice(token.indexOf(tag) + tag.length).replace(/\/?\s*>$/, '');
    for (const attribute of remainder.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
      attrs[attribute[1].toLowerCase()] = attribute[2] ?? attribute[3] ?? attribute[4] ?? '';
    }
    const node = { tag, attrs, children: [], parent: stack.at(-1), offset: match.index };
    stack.at(-1).children.push(node);
    if (!voidTags.has(tag) && !/\/\s*>$/.test(token)) stack.push(node);
  }
  return document;
}

const tree = parseHTML(html);
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
const nodes = descendants(tree);
function byId(id) {
  const found = nodes.filter(node => node.attrs.id === id);
  assert.equal(found.length, 1, `expected exactly one #${id}`);
  return found[0];
}
function hasClass(node, name) { return (node.attrs.class || '').split(/\s+/).includes(name); }
function ancestor(node, predicate) {
  for (let parent = node.parent; parent; parent = parent.parent) if (predicate(parent)) return parent;
  return null;
}
function first(node, predicate, description) {
  const found = descendants(node).find(predicate);
  assert(found, description);
  return found;
}
function section(name) {
  const found = nodes.filter(node => node.attrs['data-settings-section'] === name);
  assert.equal(found.length, 1, `exactly one independently saved ${name} card`);
  return found[0];
}

test('homepage puts runtime and metrics before the companion and gateway tools', () => {
  const overview = byId('page-overview');
  const hero = byId('hero');
  const stats = first(overview, node => hasClass(node, 'stat-grid'), 'homepage statistics grid');
  const tools = first(overview, node => hasClass(node, 'home-tools-grid'), 'homepage tools grid');
  assert(hero.offset < stats.offset, 'runtime status leads the page');
  assert(stats.offset < tools.offset, 'metrics precede secondary tools');
  for (const id of ['home-companion-card', 'home-qq-card']) {
    assert.equal(ancestor(byId(id), node => node === tools), tools, `${id} belongs to the tools grid`);
  }
});

test('companion has a padded body, an explicit switch row and its own primary action', () => {
  const card = byId('home-companion-card');
  const body = first(card, node => hasClass(node, 'companion-body'), 'companion content wrapper');
  const toggle = byId('companion-enabled');
  const switchLabel = ancestor(toggle, node => node.tag === 'label' && hasClass(node, 'switch-row'));
  assert(switchLabel, 'the companion switch has a dedicated accessible label row');
  assert.equal(ancestor(switchLabel, node => node === body), body, 'switch is inside companion body');
  const open = byId('btn-companion-open');
  assert.equal(open.tag, 'button');
  assert(hasClass(open, 'btn-primary'), 'opening the companion is the primary action');
  assert.equal(ancestor(open, node => node === card), card);
  assert(!ancestor(open, node => node === switchLabel), 'opening does not toggle the enable switch');
  for (const id of ['btn-companion-import', 'companion-status']) {
    assert.equal(ancestor(byId(id), node => node === card), card);
  }
});

test('settings categories have connected tab and panel semantics', () => {
  const settings = byId('page-settings');
  const tabs = descendants(settings).filter(node => node.attrs['data-settings-tab']);
  assert.deepEqual(tabs.map(node => node.attrs['data-settings-tab']), ['models', 'conversation', 'data']);
  const tablist = first(settings,
    node => node.attrs.role === 'tablist' && node.attrs['aria-label'] === '设置分类',
    'settings tablist with an accessible name');
  for (const tab of tabs) {
    const group = tab.attrs['data-settings-tab'];
    assert.equal(tab.tag, 'button');
    assert.equal(tab.attrs.role, 'tab');
    assert.equal(ancestor(tab, node => node === tablist), tablist);
    const panel = byId(`settings-panel-${group}`);
    assert.equal(panel.attrs.role, 'tabpanel');
    assert.equal(panel.attrs['data-settings-panel'], group);
    assert.equal(tab.attrs['aria-controls'], panel.attrs.id);
    assert(tab.attrs.id, 'every tab has a label target');
    assert.equal(panel.attrs['aria-labelledby'], tab.attrs.id);
  }
});

test('all original fields and save buttons stay in their independently saved card and category', () => {
  const groups = {
    models: {
      primary: ['primary-base', 'primary-model', 'primary-key', 'context-window-tokens', 'context-compact-percent'],
      fallback: ['fallback-base', 'fallback-model', 'fallback-key'],
      judge: ['judge-base', 'judge-model', 'judge-key'],
      vision: ['vision-base', 'vision-model', 'vision-key'],
      search: ['search-enabled', 'search-backend', 'search-model', 'search-key', 'context-timezone'],
    },
    conversation: {
      reply: ['chat-mode', 'group-at-sender', 'message-merge-window', 'message-merge-max-wait',
        'reply-mode', 'late-reply-enabled', 'humanized-wait', 'typing-indicator', 'message-pause-timeout'],
      proactive: ['proactive-enabled', 'proactive-min-hours', 'proactive-max-hours', 'proactive-daily-limit',
        'proactive-quiet-start', 'proactive-quiet-end'],
      memory: ['memory-enabled', 'memory-inject', 'memory-interval'],
    },
  };
  for (const [group, cards] of Object.entries(groups)) {
    const panel = byId(`settings-panel-${group}`);
    for (const [name, ids] of Object.entries(cards)) {
      const card = section(name);
      assert.equal(ancestor(card, node => node === panel), panel, `${name} stays in ${group}`);
      for (const id of ids) {
        const field = byId(id);
        assert.equal(ancestor(field, node => node.attrs['data-settings-section']), card, `${id} card ownership`);
      }
      const buttons = descendants(card).filter(node => node.attrs['data-save-section'] === name);
      assert.equal(buttons.length, 1, `${name} retains its own save button`);
      assert(descendants(card).some(node => node.attrs['data-save-status'] === name), `${name} save feedback`);
    }
  }
  const data = byId('settings-panel-data');
  for (const id of ['card-user-data', 'card-data-import', 'card-desktop']) {
    assert.equal(ancestor(byId(id), node => node === data), data);
  }
  const persona = section('persona');
  assert.equal(ancestor(persona, node => node.attrs.id === 'page-memory'), byId('page-memory'));
});

test('reply title precedes chat-mode and the selector is inside the reply field grid', () => {
  const reply = section('reply');
  const heading = first(reply, node => hasClass(node, 'card-head'), 'reply card heading');
  const mode = byId('chat-mode');
  assert(heading.offset < mode.offset, 'card title must not be pushed below an unpadded field');
  const grid = ancestor(mode, node => hasClass(node, 'field-grid'));
  assert(grid, 'reply speed belongs to a standard padded field grid');
  assert.equal(ancestor(grid, node => node.attrs['data-settings-section']), reply);
});

// Extract actual declarations without evaluating application startup/network code.
function balancedEnd(text, start, open, close) {
  let depth = 0, quote = null, lineComment = false, blockComment = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i], next = text[i + 1];
    if (lineComment) { if (c === '\n') lineComment = false; continue; }
    if (blockComment) { if (c === '*' && next === '/') { blockComment = false; i++; } continue; }
    if (quote) { if (c === '\\') i++; else if (c === quote) quote = null; continue; }
    if (c === '/' && next === '/') { lineComment = true; i++; continue; }
    if (c === '/' && next === '*') { blockComment = true; i++; continue; }
    if (c === '"' || c === "'" || c === '`') { quote = c; continue; }
    if (c === open) depth++;
    if (c === close && --depth === 0) return i;
  }
  throw new Error(`unbalanced ${open} in extracted declaration`);
}
function declaration(name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert(match, `production declares ${name}()`);
  const parameters = source.indexOf('(', match.index);
  const body = source.indexOf('{', balancedEnd(source, parameters, '(', ')') + 1);
  return source.slice(match.index, balancedEnd(source, body, '{', '}') + 1);
}

function runtime() {
  const byNode = new Map();
  const document = { activeElement: null };
  function matches(node, selector) {
    return selector.split(',').some(branch => {
      let remainder = branch.trim();
      const attrs = [...remainder.matchAll(/\[([^\]=]+)(?:=["']?([^\]"']*)["']?)?\]/g)];
      if (attrs.some(([, key, value]) => !Object.hasOwn(node.attrs, key) || (value !== undefined && node.attrs[key] !== value))) return false;
      remainder = remainder.replace(/\[[^\]]*\]/g, '');
      const id = remainder.match(/#([\w-]+)/);
      if (id && node.id !== id[1]) return false;
      if ([...remainder.matchAll(/\.([\w-]+)/g)].some(([, name]) => !node.classList.contains(name))) return false;
      const tag = remainder.match(/^[\w-]+/);
      return !tag || tag[0].toLowerCase() === node.tag;
    });
  }
  function convert(node) {
    const classes = new Set((node.attrs.class || '').split(/\s+/).filter(Boolean));
    const listeners = new Map();
    let ownText = '';
    const element = {
      tag: node.tag, attrs: { ...node.attrs }, id: node.attrs.id || '', children: [], parentElement: null,
      dataset: Object.fromEntries(Object.entries(node.attrs).filter(([key]) => key.startsWith('data-'))
        .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value])),
      hidden: Object.hasOwn(node.attrs, 'hidden'), tabIndex: Number(node.attrs.tabindex ?? 0),
      open: Object.hasOwn(node.attrs, 'open'),
      value: node.attrs.value || '', checked: Object.hasOwn(node.attrs, 'checked'), scrollTop: 0,
      ownerDocument: document, focusCount: 0,
      classList: {
        contains: name => classes.has(name), add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        toggle(name, force) { const on = force ?? !classes.has(name); if (on) classes.add(name); else classes.delete(name); return on; },
      },
      setAttribute(name, value) { this.attrs[name] = String(value); if (name === 'tabindex') this.tabIndex = Number(value); },
      getAttribute(name) { return this.attrs[name] ?? null; },
      removeAttribute(name) { delete this.attrs[name]; },
      addEventListener(name, handler) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(handler); },
      matches(selector) { return matches(this, selector); },
      closest(selector) { for (let el = this; el; el = el.parentElement) if (matches(el, selector)) return el; return null; },
      focus() { document.activeElement = this; this.focusCount++; },
      scrollTo(options, y) { this.scrollTop = typeof options === 'number' ? y : options.top; },
      appendChild(child) {
        if (child.parentElement) child.parentElement.children = child.parentElement.children.filter(el => el !== child);
        child.parentElement = this;
        this.children.push(child);
        return child;
      },
      append(...children) {
        for (const child of children) this.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
      },
      replaceChildren(...children) {
        for (const child of this.children) child.parentElement = null;
        this.children = [];
        ownText = '';
        this.append(...children);
      },
      querySelectorAll(selector) { return flatten(this).filter(el => matches(el, selector)); },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
      fire(type, properties = {}) {
        const event = { type, target: this, currentTarget: this, defaultPrevented: false,
          preventDefault() { this.defaultPrevented = true; }, ...properties };
        for (let el = this; el; el = el.parentElement) {
          event.currentTarget = el;
          for (const handler of listeners.get(type) || []) if (el === this) handler(event);
          if (el !== this) el._deliver(type, event);
        }
        return event;
      },
      _deliver(type, event) { for (const handler of listeners.get(type) || []) handler(event); },
    };
    Object.defineProperties(element, {
      textContent: {
        get() { return ownText + this.children.map(child => child.textContent).join(''); },
        set(value) { this.replaceChildren(); ownText = String(value ?? ''); },
      },
      // Deliberately reject HTML sinks so the diagnosis test exercises the
      // textContent/createElement path instead of pretending to sanitize HTML.
      innerHTML: {
        get() { return ''; },
        set() { throw new Error('runtime fixture rejects HTML sinks; use textContent and DOM nodes'); },
      },
    });
    byNode.set(node, element);
    element.children = node.children.map(child => { const el = convert(child); el.parentElement = element; return el; });
    return element;
  }
  function flatten(node) { return node.children.flatMap(child => [child, ...flatten(child)]); }
  const documentElement = convert(tree);
  Object.assign(document, {
    querySelectorAll: selector => documentElement.querySelectorAll(selector),
    querySelector: selector => documentElement.querySelector(selector),
    getElementById: id => documentElement.querySelector(`#${id}`),
    createElement: tag => convert({ tag: tag.toLowerCase(), attrs: {}, children: [] }),
    createTextNode: value => { const node = convert({ tag: '#text', attrs: {}, children: [] }); node.textContent = value; return node; },
  });
  let configLoads = 0, networkCalls = 0;
  const state = { page: 'settings', settingsGroup: 'models', status: { running: false }, config: {} };
  const dirty = new Set(['primary']);
  const context = {
    console, document, window: {}, STATE: state, DIRTY_SECTIONS: dirty, Set, Map, Array, Object,
    $: selector => document.querySelector(selector), $$: selector => document.querySelectorAll(selector),
    loadConfig() { configLoads++; }, api() { networkCalls++; throw new Error('category navigation must be local'); },
    fetch() { networkCalls++; throw new Error('category navigation must not fetch'); },
  };
  vm.createContext(context);
  vm.runInContext(`${declaration('setSettingsGroup')}\n${declaration('bindSettingsNavigation')}`, context);
  return { context, document, state, dirty, configLoads: () => configLoads, networkCalls: () => networkCalls,
    get: id => document.getElementById(id) };
}

function assertGroup(fixture, name) {
  const tabs = fixture.document.querySelectorAll('[data-settings-tab]');
  const panels = fixture.document.querySelectorAll('[data-settings-panel]');
  for (const tab of tabs) {
    const active = tab.dataset.settingsTab === name;
    assert.equal(tab.getAttribute('aria-selected'), String(active));
    assert.equal(tab.tabIndex, active ? 0 : -1, 'only the active tab belongs to the Tab sequence');
  }
  for (const panel of panels) assert.equal(panel.hidden, panel.dataset.settingsPanel !== name);
}

test('settings category clicks only change local presentation and preserve unsaved fields', () => {
  const fixture = runtime();
  const base = fixture.get('primary-base');
  const key = fixture.get('primary-key');
  base.value = 'https://unsaved.example.invalid/v1';
  key.value = 'fixture-unsaved-key';
  fixture.context.bindSettingsNavigation();
  const content = fixture.document.querySelector('.content');
  for (const group of ['conversation', 'data', 'models']) {
    content.scrollTop = 220;
    fixture.document.querySelector(`[data-settings-tab="${group}"]`).fire('click');
    assertGroup(fixture, group);
    assert.equal(content.scrollTop, 0, 'a different category opens at its heading');
    assert.equal(base.value, 'https://unsaved.example.invalid/v1');
    assert.equal(key.value, 'fixture-unsaved-key');
    assert(fixture.dirty.has('primary'), 'switching a category does not clear a draft');
    content.scrollTop = 150;
    fixture.document.querySelector(`[data-settings-tab="${group}"]`).fire('click');
    assert.equal(content.scrollTop, 150, 'clicking the current category does not jump the viewport');
  }
  assert.equal(fixture.configLoads(), 0, 'switching categories does not reload configuration');
  assert.equal(fixture.networkCalls(), 0, 'category navigation is entirely local');
});

test('settings categories support Arrow keys, Home and End with focus and wraparound', () => {
  const fixture = runtime();
  fixture.context.bindSettingsNavigation();
  fixture.context.setSettingsGroup('models', { focus: true });
  assertGroup(fixture, 'models');
  for (const [key, expected] of [['ArrowRight', 'conversation'], ['ArrowRight', 'data'],
    ['ArrowRight', 'models'], ['ArrowLeft', 'data'], ['Home', 'models'], ['End', 'data'],
    ['ArrowDown', 'models'], ['ArrowUp', 'data']]) {
    const selected = fixture.document.querySelector('[aria-selected="true"]');
    const event = selected.fire('keydown', { key });
    assert(event.defaultPrevented, `${key} must not scroll the page while selecting a category`);
    assertGroup(fixture, expected);
    assert.equal(fixture.document.activeElement.dataset.settingsTab, expected);
  }
  assert.equal(fixture.configLoads(), 0);
  assert.equal(fixture.networkCalls(), 0);
});

test('responsive tab orientation changes without touching configuration or the selected category', () => {
  const fixture = runtime();
  const listeners = new Map();
  const media = { matches: false, addEventListener: (type, listener) => listeners.set(type, listener) };
  fixture.context.window.matchMedia = () => media;
  fixture.context.bindSettingsNavigation();
  fixture.context.setSettingsGroup('conversation');
  const tablist = fixture.document.querySelector('[role="tablist"][aria-label="设置分类"]');
  assert.equal(tablist.getAttribute('aria-orientation'), 'vertical');
  assert.equal(typeof listeners.get('change'), 'function', 'orientation follows resize events');
  media.matches = true;
  listeners.get('change')();
  assert.equal(tablist.getAttribute('aria-orientation'), 'horizontal');
  assertGroup(fixture, 'conversation');
  media.matches = false;
  listeners.get('change')();
  assert.equal(tablist.getAttribute('aria-orientation'), 'vertical');
  assertGroup(fixture, 'conversation');
  assert(fixture.dirty.has('primary'));
  assert.equal(fixture.configLoads(), 0);
  assert.equal(fixture.networkCalls(), 0);
});

test('invalid categories fall back safely without scrolling another page', () => {
  const fixture = runtime();
  fixture.context.bindSettingsNavigation();
  fixture.context.setSettingsGroup('data');
  fixture.state.page = 'overview';
  const content = fixture.document.querySelector('.content');
  content.scrollTop = 420;
  fixture.context.setSettingsGroup('not-a-category');
  assertGroup(fixture, 'models');
  assert.equal(content.scrollTop, 420, 'a hidden settings panel cannot scroll the current page');
  assert.equal(fixture.configLoads(), 0);
  assert.equal(fixture.networkCalls(), 0);
});

test('changing the main page resets scroll, while revisiting the same page preserves position', () => {
  const fixture = runtime();
  const noop = () => {};
  Object.assign(fixture.context, {
    PAGE_META: { overview: ['首页', '状态'], settings: ['设置', '配置'], memory: ['记忆', '了解'],
      messages: ['消息', '记录'], connect: ['连接', '通道'], guide: ['引导', '配置'] },
    location: { hash: '#settings' }, history: { replaceState: noop }, GUIDE: { wechatSession: 0 },
    loadMemory: noop, loadStatistics: noop, loadMessages: noop, startConnectPolling: noop,
    stopConnectPolling: noop, refreshOnboarding: () => Promise.resolve(), guideMessage: noop,
  });
  vm.runInContext(declaration('switchPage'), fixture.context);
  const content = fixture.document.querySelector('.content');
  content.scrollTop = 500;
  fixture.context.switchPage('overview');
  assert.equal(content.scrollTop, 0);
  content.scrollTop = 300;
  fixture.context.switchPage('settings');
  assert.equal(content.scrollTop, 0, 'returning to settings begins at its header');
  content.scrollTop = 250;
  fixture.context.switchPage('settings');
  assert.equal(content.scrollTop, 250, 'same-page refresh must not jump the viewport');
});

function statusRuntime(initial) {
  const fixture = runtime();
  let status = initial;
  let calls = 0;
  fixture.state.page = 'overview';
  fixture.context.api = async url => {
    assert.equal(url, '/api/status', 'homepage status does not call configuration or model APIs');
    calls++;
    return structuredClone(status);
  };
  fixture.context.loadMemory = () => { throw new Error('overview must not reload the memory page'); };
  vm.runInContext(`${declaration('fmtUptime')}\n${declaration('conversationStatusText')}\n${declaration('refreshStatus')}`, fixture.context);
  return { ...fixture, setStatus: value => { status = value; }, calls: () => calls,
    refresh: () => fixture.context.refreshStatus() };
}

function cleanStatus() {
  return {
    running: true, platforms: ['desktop'], desktop_running: true, wechat_running: false, qq_running: false,
    uptime: 17, last_error: '',
    stats: { total_in: 8, total_out: 5, silent: 3, group_count: 1, private_count: 2, reply_rate: 62.5 },
  };
}

function detailedStatus(error = '') {
  return {
    ...cleanStatus(), platforms: ['wechat', 'qq', 'desktop'], wechat_running: true, qq_running: true,
    wechat_delivery: { pending_inbound: 7, pending_replies: 2, last_status: 'retry_pending',
      last_send_ms: 91337, typing: { last_status: 'on' } },
    conversation: { waiting: 2, deferred: 1, replying: 0, silent: 3, uncertain: 1 }, last_error: error,
  };
}

test('homepage maps desktop separately from QQ and preserves runtime statistics', async () => {
  const fixture = statusRuntime(cleanStatus());
  await fixture.refresh();
  for (const id of ['hero-platform', 'hero-groupcap']) {
    const label = fixture.get(id).textContent;
    assert(/桌面|desktop/i.test(label), `${id} identifies the desktop channel`);
    assert(!/QQ/i.test(label), `${id} must not label a desktop-only session as QQ`);
  }
  assert.equal(fixture.get('st-in').textContent, '8');
  assert.equal(fixture.get('st-out').textContent, '5');
  assert.equal(fixture.get('st-silent').textContent, '3');
  assert.equal(fixture.get('st-chats').textContent, '3');
  assert.equal(fixture.get('btn-start').hidden, true);
  assert.equal(fixture.get('btn-stop').hidden, false);
  fixture.setStatus(detailedStatus());
  await fixture.refresh();
  const platform = fixture.get('hero-platform').textContent;
  assert(/微信|wechat/i.test(platform));
  assert(/QQ/i.test(platform));
  assert(/桌面|desktop/i.test(platform));
  assert.equal(fixture.calls(), 2);
});

test('homepage keeps diagnostic details out of the short description and inserts errors as text', async () => {
  const detailsNode = byId('hero-runtime-details');
  assert.equal(detailsNode.tag, 'details');
  assert(!Object.hasOwn(detailsNode.attrs, 'open'), 'diagnostic disclosure is initially collapsed');
  assert.equal(ancestor(detailsNode, node => node.attrs.id === 'hero'), byId('hero'));
  const summaryNode = byId('hero-diagnostic-summary');
  assert.equal(summaryNode.tag, 'span');
  assert(ancestor(summaryNode, node => node.tag === 'summary'));
  const malicious = 'fixture-error <img src=x onerror=alert(1)> ' + '网络详情'.repeat(100);
  const fixture = statusRuntime(detailedStatus(malicious));
  await fixture.refresh();
  const details = fixture.get('hero-runtime-details');
  const list = fixture.get('hero-diagnostics');
  const short = fixture.get('hero-desc').textContent;
  assert.equal(details.hidden, false);
  assert.equal(details.open, true, 'a new error is visible without making the user search for it');
  assert(list.children.length >= 3, 'delivery, conversation and errors have distinct diagnostic rows');
  assert(list.children.every(child => child.tag === 'li'));
  assert(list.textContent.includes(malicious), 'the complete error remains available inside details');
  assert(list.textContent.includes('91337'), 'delivery timing lives in details');
  assert(!short.includes(malicious), 'long errors must not distort the summary layout');
  assert(!short.includes('91337'), 'delivery timing is not appended to the short summary');
  assert.equal(list.querySelectorAll('img').length, 0, 'error markup is rendered as literal text');
  assert(fixture.get('hero-diagnostic-summary').textContent.trim());
  fixture.setStatus({ ...cleanStatus(), running: false, desktop_running: false, last_error: malicious });
  await fixture.refresh();
  assert(!fixture.get('hero-desc').textContent.includes(malicious), 'stopped errors also use the disclosure');
  assert(fixture.get('hero-diagnostics').textContent.includes(malicious));
  assert.equal(fixture.get('btn-start').hidden, false);
  assert.equal(fixture.get('btn-stop').hidden, true);
});

test('homepage polling preserves user disclosure choice and only opens on a newly observed error', async () => {
  const fixture = statusRuntime(cleanStatus());
  const details = fixture.get('hero-runtime-details');
  await fixture.refresh();
  assert.equal(details.hidden, true, 'empty diagnostics do not leave an empty disclosure');
  fixture.setStatus(detailedStatus());
  await fixture.refresh();
  assert.equal(details.hidden, false);
  assert.equal(details.open, false, 'normal delivery/conversation diagnostics stay folded by default');
  details.open = true;
  await fixture.refresh();
  assert.equal(details.open, true, 'polling does not collapse a disclosure opened by the user');
  details.open = false;
  await fixture.refresh();
  assert.equal(details.open, false, 'polling does not expand a disclosure closed by the user');
  fixture.setStatus(detailedStatus('fixture first error'));
  await fixture.refresh();
  assert.equal(details.open, true);
  assert.equal(fixture.state.overviewError, 'fixture first error');
  details.open = false;
  await fixture.refresh();
  await fixture.refresh();
  assert.equal(details.open, false, 'the same error does not repeatedly reopen details');
  fixture.setStatus(detailedStatus('fixture different error'));
  await fixture.refresh();
  assert.equal(details.open, true, 'a distinct new error is surfaced');
  fixture.setStatus(cleanStatus());
  await fixture.refresh();
  assert.equal(details.hidden, true);
  assert.equal(fixture.get('hero-diagnostics').children.length, 0, 'old error rows are cleared after recovery');
  assert.equal(fixture.state.overviewError, '', 'a later occurrence can be recognised after recovery');
});
