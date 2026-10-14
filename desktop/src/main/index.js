/**
 * Electron 主进程入口 —— 桌面版微信 AI 助手。
 *
 * 架构：
 *   ┌─────────────────────────────────────────────┐
 *   │  Electron 主进程（本文件）                    │
 *   │   ├── WindowManager  窗口（关闭=隐藏）        │
 *   │   ├── TrayManager    托盘（后台入口）         │
 *   │   └── Backend        Python 引擎 sidecar     │
 *   └──────────────────┬──────────────────────────┘
 *                      │ http://127.0.0.1:<首选17800，占用则空闲端口>
 *   ┌──────────────────▼──────────────────────────┐
 *   │  渲染进程（复用仓库里的 src/web 界面）        │
 *   └─────────────────────────────────────────────┘
 *
 * 设计原则：外壳与引擎解耦。外壳只管"把进程管好、把窗口管好"，
 * 换成任何别的本地 HTTP 服务都能复用这套外壳。
 */
'use strict';

const { app, Menu, dialog, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');

const P = require('./paths');
const { Backend } = require('./backend');
const { WindowManager } = require('./window');
const { TrayManager } = require('./tray');
const { registerIpc } = require('./ipc');

// 可选隔离数据目录：测试/便携部署时不污染默认用户配置。
if (process.env.WXBOT_USER_DATA_DIR) {
  const dir = path.resolve(process.env.WXBOT_USER_DATA_DIR);
  fs.mkdirSync(dir, { recursive: true });
  app.setPath('userData', dir);
}

// ── 单实例：重复启动时激活已有窗口，而不是开第二个 ────────────
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

app.isQuitting = false;

const backend = new Backend();
const windowMgr = new WindowManager();
const trayMgr = new TrayManager();

/** 优雅退出：先停后端，再退应用 */
async function quitApp() {
  if (app.isQuitting) return;
  app.isQuitting = true;
  try {
    await backend.stop();
  } catch (e) {
    console.error('[quit] 停止后端失败', e);
  }
  trayMgr.destroy();
  windowMgr.destroy();
  app.quit();
}

const { errorPage, installQuitGuard } = require('./lifecycle');

/** 应用菜单（macOS 顶部菜单栏 / Windows 窗口菜单） */
function buildAppMenu() {
  const isMac = P.platform === 'darwin';
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: '文件',
      submenu: [
        { label: '打开主界面', accelerator: 'CmdOrCtrl+O', click: () => windowMgr.show() },
        { label: '隐藏窗口', accelerator: 'CmdOrCtrl+W', click: () => windowMgr.hide() },
        { type: 'separator' },
        { label: '打开数据目录', click: () => require('electron').shell.openPath(P.userDataDir()) },
        { label: '打开日志目录', click: () => require('electron').shell.openPath(P.logDir()) },
        { type: 'separator' },
        isMac ? { role: 'close' } : { label: '退出', accelerator: 'CmdOrCtrl+Q', click: quitApp },
      ],
    },
    { role: 'editMenu', label: '编辑' },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载界面' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '重启后台服务',
          click: async () => {
            trayMgr.setStatus({ backend: 'starting' });
            const r = await backend.restart();
            trayMgr.setStatus({ backend: r.ok ? 'running' : 'crashed' });
            if (r.ok) windowMgr.reload(`http://127.0.0.1:${r.port}`);
            else dialog.showErrorBox('重启失败', r.error || '未知错误');
          },
        },
        {
          label: '关于',
          click: async () => {
            const parent = windowMgr.win;
            if (!parent || parent.isDestroyed()) return;
            try {
              await dialog.showMessageBox(parent, {
                type: 'info',
                title: '关于',
                message: '微信 AI 助手（桌面版）',
                detail:
                  `版本 ${app.getVersion()}\n` +
                  `平台 ${P.platform} / ${process.arch}\n` +
                  `数据目录 ${P.userDataDir()}`,
              });
            } finally {
              windowMgr.restoreInputFocus(parent);
            }
          },
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── 启动流程 ──────────────────────────────────────────────────

app.whenReady().then(async () => {
  buildAppMenu();

  // 托盘先建起来，保证用户随时有入口（即使后端还没起来）
  trayMgr.create({
    onOpen: () => windowMgr.show(),
    onRestart: async () => {
      trayMgr.setStatus({ backend: 'starting' });
      const r = await backend.restart();
      trayMgr.setStatus({ backend: r.ok ? 'running' : 'crashed' });
      if (r.ok) windowMgr.reload(`http://127.0.0.1:${r.port}`);
    },
    onQuit: quitApp,
  });

  registerIpc({ windowMgr, backend, trayMgr, quit: quitApp });

  // 后端事件 → 托盘状态 + 通知
  backend.on('ready', (port) => trayMgr.setStatus({ backend: 'running' }));
  backend.on('crashed', ({ code, retryInMs }) => {
    trayMgr.setStatus({ backend: 'crashed' });
    trayMgr.notify('后台服务异常', `进程退出（code=${code}），将在 ${Math.round(retryInMs / 1000)} 秒后重启`);
  });
  backend.on('restarted', (r) => {
    if (r.ok) {
      trayMgr.setStatus({ backend: 'running' });
      windowMgr.reload(`http://127.0.0.1:${r.port}`);
    }
  });
  backend.on('error', (msg) => {
    trayMgr.setStatus({ backend: 'crashed' });
    trayMgr.notify('后台服务错误', msg);
  });

  // 先开窗口显示"启动中"，再拉起后端（避免用户对着空白等待）
  windowMgr.create({
    url: 'data:text/html;charset=utf-8,' + encodeURIComponent(
      '<html><head><meta charset="utf-8"><style>body{margin:0;height:100vh;display:grid;' +
      'place-items:center;background:#F4F6F8;font-family:-apple-system,"PingFang SC",sans-serif;' +
      'color:#5A616B;font-size:14px}</style></head><body><div>正在启动后台服务…</div></body></html>',
    ),
  });

  const result = await backend.start();
  if (result.ok) {
    windowMgr.reload(`http://127.0.0.1:${result.port}`);
  } else {
    windowMgr.reload(errorPage(result.error || '未知错误', '详见「帮助 → 打开日志目录」中的 backend.log'));
    trayMgr.setStatus({ backend: 'crashed' });
  }
});

// ── 生命周期事件 ──────────────────────────────────────────────

// 关掉所有窗口不退出 —— 这是"后台静默运行"的关键
app.on('window-all-closed', () => {
  // 故意留空：应用继续在托盘里运行
});

// macOS: 点 Dock 图标重新打开窗口
app.on('activate', () => {
  windowMgr.show();
});

// 第二个实例启动 → 激活已有窗口
app.on('second-instance', () => {
  windowMgr.show();
});

// 真正退出前清理
installQuitGuard(app, quitApp);

// 兜底：进程要退出时确保后端也被停掉，避免留下孤儿进程
process.on('exit', () => {
  try { backend.proc && backend._killTree(backend.proc); } catch (_) { /* 忽略 */ }
});
