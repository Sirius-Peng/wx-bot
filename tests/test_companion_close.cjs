'use strict';

// Exercise the shipped main/preload/renderer code without Electron or a model key.
// WXBOT_TEST_ROOT also lets this suite probe an isolated pre-fix source snapshot.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');
const ROOT = process.env.WXBOT_TEST_ROOT || path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(ROOT, file), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function managerFixture() {
  const windows = [], timers = new Map(), writes = new Map();
  let timerId = 0;
  const app = { isQuitting: false, quitCalls: 0, quit() { this.quitCalls++; } };
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.visible = false; this.destroyed = false;
      this.shows = 0; this.urls = []; this.focused = false; this.mouseIgnored = false;
      this.bounds = { x: 20, y: 30, width: 380, height: 620 };
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
      this.webContents.isDestroyed = () => this.destroyed;
      windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    isVisible() { return this.visible; }
    isFocused() { return this.focused; }
    show() { assert(!this.destroyed, 'a destroyed companion cannot be shown'); this.visible = true; this.shows++; }
    hide() { this.visible = false; this.focused = false; }
    focus() { this.focused = true; }
    setIgnoreMouseEvents(value) { this.mouseIgnored = value; }
    setAlwaysOnTop(value) { this.onTop = value; }
    loadURL(url) { this.urls.push(url); }
    getBounds() { return { ...this.bounds }; }
    close() {
      let prevented = false;
      this.emit('close', { preventDefault() { prevented = true; } });
      if (!prevented) this.destroy();
    }
    destroy() {
      if (this.destroyed) return;
      this.destroyed = true; this.visible = false; this.emit('closed');
    }
  }
  const storage = {
    readFileSync() { throw new Error('empty synthetic profile'); },
    mkdirSync() {},
    writeFileSync(file, value) { writes.set(file, value); },
    renameSync(from, to) { writes.set(to, writes.get(from)); writes.delete(from); },
  };
  const module = { exports: {} };
  vm.runInNewContext(read('desktop/src/main/companion.js'), {
    module, URL, console, __dirname: path.join(ROOT, 'desktop/src/main'),
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    require: name => name === 'electron' ? {
      BrowserWindow, app,
      screen: { getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }) },
    } : name === 'fs' ? storage : name === './paths' ? {
      userDataDir: () => '/synthetic-companion-close-profile',
    } : require(name),
  });
  const manager = new module.exports.CompanionWindow();
  return { manager, windows, timers, writes, app, ready: win => win.emit('ready-to-show') };
}

test('hide before ready is respected instead of reopening the companion', () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800');
  const win = h.windows[0]; h.manager.hide(); h.ready(win);
  assert.equal(win.visible, false);
  assert.equal(win.destroyed, false, 'legacy hide remains restorable');
});

test('close before ready destroys the companion and does not show it later', () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800');
  const win = h.windows[0]; h.manager.close(); h.ready(win);
  assert.equal(win.destroyed, true); assert.equal(win.shows, 0);
  assert.equal(h.manager.win, null); assert.equal(h.app.quitCalls, 0);
});

test('close releases the window, persists bounds and leaves the app running', () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800');
  const win = h.windows[0]; h.ready(win); win.bounds = { x: 144, y: 76, width: 400, height: 640 };
  win.emit('move'); h.manager.close();
  assert.equal(win.destroyed, true); assert.equal(h.manager.win, null);
  assert.equal(h.app.isQuitting, false); assert.equal(h.app.quitCalls, 0);
  assert.equal(h.timers.size, 0, 'close cancels deferred bounds writes');
  const value = h.writes.get(path.join('/synthetic-companion-close-profile', 'companion-window.json'));
  assert(value, 'closing flushes the latest bounds before destruction');
  assert.deepEqual(JSON.parse(value), win.bounds);
  assert.doesNotThrow(() => h.manager.close(), 'repeated close is idempotent');
});

test('a stale ready event cannot show a reopened replacement window', () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800');
  const old = h.windows[0]; h.manager.close(); h.manager.show('http://127.0.0.1:17800');
  const current = h.windows[1]; h.ready(old);
  assert.equal(current.shows, 0); assert.equal(h.manager.win, current);
  h.ready(current); assert.equal(current.visible, true);
  assert.equal(h.windows.length, 2);
});

test('legacy hide and show restore the same window and clear click-through', () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800');
  const win = h.windows[0]; h.ready(win); h.manager.through(true); h.manager.hide();
  h.manager.show('http://127.0.0.1:17800');
  assert.equal(h.windows.length, 1); assert.equal(win.visible, true);
  assert.equal(win.mouseIgnored, false); assert.equal(win.focused, true);
});

test('native close does not keep an invisible companion process alive', () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800');
  const win = h.windows[0]; h.ready(win); win.close();
  assert.equal(win.destroyed, true); assert.equal(h.manager.win, null);
  assert.equal(h.app.quitCalls, 0);
});

for (const input of [
  { type: 'keyDown', key: 'Escape' },
  { type: 'keyDown', key: 'w', control: true },
  { type: 'keyDown', key: 'W', meta: true },
]) test(`native companion shortcut closes only the companion: ${JSON.stringify(input)}`, () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800'); const win = h.windows[0];
  h.ready(win); let prevented = false;
  win.webContents.emit('before-input-event', { preventDefault() { prevented = true; } }, input);
  assert.equal(prevented, true); assert.equal(win.destroyed, true); assert.equal(h.app.quitCalls, 0);
});

for (const input of [
  { type: 'keyDown', key: 'w' },
  { type: 'keyUp', key: 'Escape' },
  { type: 'keyDown', key: 'Escape', isComposing: true },
]) test(`typing and IME do not accidentally close the companion: ${JSON.stringify(input)}`, () => {
  const h = managerFixture(); h.manager.show('http://127.0.0.1:17800'); const win = h.windows[0];
  h.ready(win); win.webContents.emit('before-input-event', { preventDefault() {} }, input);
  assert.equal(win.destroyed, false);
});

function ipcFixture() {
  const handlers = {}, calls = [];
  const mainFrame = { url: 'http://127.0.0.1:17800/' };
  const companionFrame = { url: 'http://127.0.0.1:17800/companion' };
  const main = { webContents: { mainFrame } }, companion = { webContents: { mainFrame: companionFrame } };
  const module = { exports: {} };
  vm.runInNewContext(read('desktop/src/main/ipc.js'), {
    module, URL, require: name => name === 'electron' ? {
      ipcMain: { handle: (name, handler) => { handlers[name] = handler; } },
    } : {},
  });
  module.exports.registerIpc({ windowMgr: { win: main }, companionMgr: { win: companion, close: () => calls.push('close') },
    backend: { url: 'http://127.0.0.1:17800' }, trayMgr: {}, quit() { calls.push('quit'); } });
  const event = win => ({ sender: win.webContents, senderFrame: win.webContents.mainFrame });
  return { handlers, calls, main, companion, event };
}

test('main and companion top-level same-origin renderers can invoke companion:close', async () => {
  const h = ipcFixture(); assert.equal(typeof h.handlers['companion:close'], 'function');
  await h.handlers['companion:close'](h.event(h.companion));
  await h.handlers['companion:close'](h.event(h.main));
  assert.deepEqual(h.calls, ['close', 'close']);
});

test('foreign window, frame and origin do not gain companion close privileges', async () => {
  const h = ipcFixture(); assert.equal(typeof h.handlers['companion:close'], 'function');
  const event = h.event(h.companion);
  await h.handlers['companion:close']({ sender: { mainFrame: event.senderFrame }, senderFrame: event.senderFrame });
  await h.handlers['companion:close']({ ...event, senderFrame: { url: event.senderFrame.url } });
  event.senderFrame.url = 'https://example.invalid/companion';
  await h.handlers['companion:close'](event);
  assert.deepEqual(h.calls, []);
});

test('the isolated preload exposes close separately from hide', async () => {
  let bridge; const calls = [];
  vm.runInNewContext(read('desktop/src/preload/index.js'), { require: () => ({
    contextBridge: { exposeInMainWorld(name, value) { assert.equal(name, 'wxbotDesktop'); bridge = value; } },
    ipcRenderer: { invoke: async (...args) => { calls.push(args); }, on() {}, removeListener() {} },
  }) });
  assert.equal(typeof bridge.companion.close, 'function');
  await bridge.companion.close(); await bridge.companion.hide();
  assert.deepEqual(calls, [['companion:close'], ['companion:hide']]);
});

class Element {
  constructor() {
    this.listeners = new Map(); this.value = ''; this.textContent = ''; this.children = [];
    this.disabled = false; this.hidden = false; this.style = {};
    const classes = new Set(); this.classList = {
      contains: name => classes.has(name), toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    };
  }
  addEventListener(name, fn) { const list = this.listeners.get(name) || []; list.push(fn); this.listeners.set(name, list); }
  emit(name, event = {}) { this[`on${name}`]?.(event); for (const fn of this.listeners.get(name) || []) fn(event); }
  appendChild(node) { this.children.push(node); node.parent = this; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  setAttribute(name, value) { this[name] = value; }
}

function rendererFixture(bridge, options = {}) {
  const elements = new Map(), sockets = [], timers = new Map(), document = new Element(), window = new Element();
  let timer = 0, browserCloseCalls = 0;
  const element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  document.getElementById = element; document.createElement = () => new Element();
  document.querySelectorAll = () => [element('text'), element('send')];
  window.wxbotDesktop = bridge ? { companion: bridge } : undefined;
  window.close = () => { browserCloseCalls++; };
  class Socket {
    static OPEN = 1;
    constructor() { this.readyState = 1; this.closeCalls = 0; this.sent = []; sockets.push(this); }
    send(text) { this.sent.push(text); }
    close() { this.closeCalls++; this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  let resolveFetch;
  const response = { ok: true, json: async () => ({ models: [], persona_space: 'test-space' }) };
  const fetch = options.delayedInit ? () => new Promise(resolve => { resolveFetch = resolve; }) : async () => response;
  const context = vm.createContext({
    console, document, window, WebSocket: Socket, URL, crypto: { randomUUID: () => 'one-local-message' },
    location: { protocol: 'http:', host: '127.0.0.1:17800', href: 'http://127.0.0.1:17800/companion' },
    fetch, setTimeout: callback => { const id = ++timer; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(read('src/web/companion.js'), context);
  return { context, element, elements, sockets, timers, document, window,
    closeCalls: () => browserCloseCalls, resolveInit: () => resolveFetch(response) };
}

test('the close control is labelled as close and stays outside the drag region', () => {
  const html = read('src/web/companion.html'), css = read('src/web/companion.css');
  assert(/<button\b[^>]*\bid=["']hide["'][^>]*>[\s\S]*?关闭[\s\S]*?<\/button>/.test(html));
  assert(/header\s+div\s*,\s*button\s*,\s*textarea\s*\{[^}]*-webkit-app-region\s*:\s*no-drag/.test(css));
});

test('renderer close stops reconnects and invokes the explicit desktop close API once', async () => {
  let calls = 0; const h = rendererFixture({ close: async () => { calls++; } }); await flush();
  const socket = h.sockets[0]; socket.onclose({ code: 1006 }); assert.equal(h.timers.size, 1);
  h.element('hide').emit('click'); await flush();
  assert.equal(calls, 1); assert.equal(socket.closeCalls, 1); assert.equal(h.timers.size, 0);
  h.element('hide').emit('click'); await flush(); assert.equal(calls, 1, 'duplicate close is harmless');
  const before = socket.sent.length; h.element('text').value = 'closed session must not send';
  h.element('chat').emit('submit', { preventDefault() {} }); assert.equal(socket.sent.length, before);
});

test('renderer close remains compatible with an older preload exposing only hide', async () => {
  let calls = 0; const h = rendererFixture({ hide: async () => { calls++; } }); await flush();
  h.element('hide').emit('click'); await flush(); assert.equal(calls, 1);
  assert.equal(h.sockets[0].closeCalls, 0, 'legacy hide must leave the reusable renderer session alive');
  assert.equal(vm.runInContext('closed', h.context), false);
});

test('a normal browser popup closes without requiring the Electron bridge', async () => {
  const h = rendererFixture(); await flush(); h.element('hide').emit('click'); await flush();
  assert.equal(h.closeCalls(), 1); assert.equal(h.sockets[0].closeCalls, 1);
  assert.equal(h.element('text').disabled, true, 'if the browser refuses tab closure, input stays stopped');
  assert.match(h.element('state').textContent + h.element('error').textContent, /关闭|结束/);
});

test('renderer Escape closes the current companion without hijacking IME composition', async () => {
  let calls = 0; const h = rendererFixture({ close: async () => { calls++; } }); await flush();
  h.document.emit('keydown', { key: 'Escape', isComposing: true, preventDefault() {} });
  assert.equal(calls, 0);
  h.document.emit('keydown', { key: 'Escape', preventDefault() {} }); await flush(); assert.equal(calls, 1);
});

test('closing while status is loading never opens a late WebSocket session', async () => {
  const h = rendererFixture(undefined, { delayedInit: true });
  h.element('hide').emit('click'); h.resolveInit(); await flush();
  assert.equal(h.sockets.length, 0); assert.equal(h.closeCalls(), 1);
});

// Extract only the companion binding; do not run management-page startup.
function companionBindingFixture({ enabled = true, fail = false, desktop = true } = {}) {
  const source = read('src/web/app.js');
  const start = source.indexOf('function bindCompanion(');
  assert(start >= 0, 'companion settings binding exists');
  const code = 'let companionPopup=null;\n' + source.slice(start); // This declaration intentionally remains at end of app.js.
  const elements = new Map(), calls = [], popups = [];
  const element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  element('companion-enabled').checked = enabled;
  const companion = { close: async () => calls.push('close'), show: () => calls.push('show') };
  const window = { wxbotDesktop: desktop ? { companion } : undefined,
    open: () => { const popup = { closed: false, close() { this.closed = true; calls.push('popup-close'); } }; popups.push(popup); return popup; } };
  const context = vm.createContext({ $, window, STATE: { config: {} }, toast() {},
    api: async (_url, options) => { calls.push(['config', JSON.parse(options.body).enabled]); if (fail) throw Error('save failed'); return { config: {} }; },
    switchPage() {}, document: { createElement: () => new Element() },
  });
  function $(selector) { return element(selector.slice(1)); }
  vm.runInContext(code + '\nbindCompanion();', context);
  return { element, calls, popups };
}

test('disabling the saved companion setting closes its existing desktop window', async () => {
  const h = companionBindingFixture(); const toggle = h.element('companion-enabled'); toggle.checked = false;
  toggle.emit('change', { target: toggle }); await flush();
  assert.deepEqual(h.calls, [['config', false], 'close']);
});

test('failed disable retains the enabled setting and does not close the window', async () => {
  const h = companionBindingFixture({ fail: true }); const toggle = h.element('companion-enabled'); toggle.checked = false;
  toggle.emit('change', { target: toggle }); await flush();
  assert.deepEqual(h.calls, [['config', false]]); assert.equal(toggle.checked, true);
});

test('disabling from browser management closes the popup it opened', async () => {
  const h = companionBindingFixture({ desktop: false }); h.element('btn-companion-open').emit('click');
  assert.equal(h.popups.length, 1); const toggle = h.element('companion-enabled'); toggle.checked = false;
  toggle.emit('change', { target: toggle }); await flush(); assert.equal(h.popups[0].closed, true);
});

test('tray supplies a close action usable when the companion ignores mouse input', () => {
  const module = { exports: {} }; let template, closes = 0;
  vm.runInNewContext(read('desktop/src/main/tray.js'), {
    module, console, require: name => name === 'electron' ? {
      Menu: { buildFromTemplate(value) { template = value; return value; } },
    } : name === './paths' ? {} : require(name),
  });
  const tray = new module.exports.TrayManager(); tray.tray = { setContextMenu() {} };
  tray._handlers = { onOpen() {}, onCompanion() {}, onCloseCompanion() { closes++; }, onQuit() {}, onRestart() {} };
  tray._rebuildMenu();
  const action = template.find(item => /关闭桌面伙伴/.test(item.label));
  assert(action, 'close stays accessible outside the click-through companion'); action.click(); assert.equal(closes, 1);
});

function applicationFixture() {
  const state = { menu: null, tray: null, companions: [], windows: [] };
  const app = new EventEmitter(); Object.assign(app, { requestSingleInstanceLock: () => true, whenReady: async () => {}, quit() {} });
  class Backend extends EventEmitter { constructor() { super(); this.port = 17800; } get url() { return 'http://127.0.0.1:17800'; } async start() { return { ok: true, port: 17800 }; } }
  class WindowManager {
    constructor() { this.hides = 0; state.windows.push(this); }
    create() {} reload() {} show() {} hide() { this.hides++; }
  }
  class CompanionWindow {
    constructor() { this.focused = false; this.closes = 0; this.win = { isFocused: () => this.focused }; state.companions.push(this); }
    show() {} reload() {} close() { this.closes++; }
  }
  class TrayManager { create(value) { state.tray = value; } setStatus() {} notify() {} }
  const modules = { electron: { app, Menu: { buildFromTemplate: value => value, setApplicationMenu: value => { state.menu = value; } }, dialog: {}, nativeImage: {} },
    './paths': { platform: 'win32' }, './backend': { Backend }, './window': { WindowManager }, './tray': { TrayManager }, './companion': { CompanionWindow },
    './ipc': { registerIpc() {} }, './lifecycle': { errorPage: () => '', installQuitGuard() {} } };
  vm.runInNewContext(read('desktop/src/main/index.js'), { require: name => modules[name] || require(name),
    process: { env: {}, on() {}, exit() {} }, console, __dirname: path.join(ROOT, 'desktop/src/main') });
  return state;
}

test('application close shortcut targets the focused companion rather than hiding the main page', async () => {
  const h = applicationFixture(); await flush(); const companion = h.companions[0], main = h.windows[0];
  const close = h.menu.find(menu => menu.label === '文件').submenu.find(item => item.accelerator === 'CmdOrCtrl+W');
  assert(close, 'application exposes its familiar window close shortcut'); companion.focused = true; close.click();
  assert.equal(companion.closes, 1); assert.equal(main.hides, 0);
  companion.focused = false; close.click(); assert.equal(main.hides, 1);
  assert.equal(typeof h.tray.onCloseCompanion, 'function'); h.tray.onCloseCompanion(); assert.equal(companion.closes, 2);
});
