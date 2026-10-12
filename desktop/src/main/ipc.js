/**
 * IPC 接口 —— 渲染进程（网页界面）与主进程之间的唯一通道。
 *
 * 安全约定：
 *   · 渲染进程不开 nodeIntegration，拿不到 fs / child_process
 *   · 只能调用这里显式注册的方法，白名单式暴露
 */
'use strict';

const { ipcMain, shell, app } = require('electron');
const P = require('./paths');

/**
 * @param {object} ctx
 * @param {object} ctx.windowMgr  WindowManager
 * @param {object} ctx.backend    Backend
 * @param {object} ctx.trayMgr    TrayManager
 * @param {Function} ctx.quit     退出应用的回调
 */
function registerIpc({ windowMgr, backend, trayMgr, quit }) {
  // ── 应用信息 ──────────────────────────────────────────────
  ipcMain.handle('app:info', () => ({
    version: app.getVersion(),
    name: app.getName(),
    platform: P.platform,
    isPackaged: P.isPackaged(),
    isDev: P.isDev(),
    dataDir: P.userDataDir(),
    logDir: P.logDir(),
    backendUrl: backend.url,
    backendState: backend.state,
    arch: process.arch,
  }));

  // ── 生命周期 ──────────────────────────────────────────────
  ipcMain.handle('app:hide', () => {
    windowMgr.hide();
    return { ok: true };
  });

  ipcMain.handle('app:quit', () => {
    quit();
    return { ok: true };
  });

  // ── 后端管理 ──────────────────────────────────────────────
  ipcMain.handle('backend:status', () => ({
    state: backend.state,
    port: backend.port,
    url: backend.url,
    restarts: backend.restarts,
  }));

  ipcMain.handle('backend:restart', async () => {
    trayMgr.setStatus({ backend: 'starting' });
    const r = await backend.restart();
    trayMgr.setStatus({ backend: r.ok ? 'running' : 'crashed' });
    if (r.ok) windowMgr.reload(`http://127.0.0.1:${r.port}`);
    return r;
  });

  ipcMain.handle('backend:log', (_e, lines = 200) => ({
    log: backend.tailLog(lines),
  }));

  // ── 打开本地目录（方便用户备份/排查）──────────────────────
  ipcMain.handle('shell:openDataDir', () => {
    shell.openPath(P.userDataDir());
    return { ok: true };
  });

  ipcMain.handle('shell:openLogDir', () => {
    shell.openPath(P.logDir());
    return { ok: true };
  });

  ipcMain.handle('shell:openExternal', (_e, url) => {
    if (typeof url === 'string' && /^https?:\/\//.test(url)) {
      shell.openExternal(url);
      return { ok: true };
    }
    return { ok: false, error: '仅允许打开 http/https 链接' };
  });
}

module.exports = { registerIpc };
