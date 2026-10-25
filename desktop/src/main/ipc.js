/**
 * IPC 接口 —— 渲染进程（网页界面）与主进程之间的唯一通道。
 *
 * 安全约定：
 *   · 渲染进程不开 nodeIntegration，拿不到 fs / child_process
 *   · 只能调用这里显式注册的方法，白名单式暴露
 */
'use strict';

const { ipcMain, shell, app, dialog } = require('electron');
const P = require('./paths');

/**
 * @param {object} ctx
 * @param {object} ctx.windowMgr  WindowManager
 * @param {object} ctx.backend    Backend
 * @param {object} ctx.trayMgr    TrayManager
 * @param {Function} ctx.quit     退出应用的回调
 */
function registerIpc({ windowMgr, backend, trayMgr, companionMgr, quit }) {
  const trusted=event=>{
    const allowed=[windowMgr.win,companionMgr?.win].filter(Boolean);
    if(!allowed.some(win=>event.sender===win.webContents)||event.senderFrame!==event.sender.mainFrame)return false;
    try{return new URL(event.senderFrame.url).origin===backend.url;}catch(_){return false;}
  };
  if(companionMgr){
    ipcMain.handle('companion:show',event=>{if(trusted(event))companionMgr.show(backend.url);});
    ipcMain.handle('companion:reload',event=>{if(trusted(event))companionMgr.reload(backend.url);});
    ipcMain.handle('companion:hide',event=>{if(trusted(event))companionMgr.hide();});
    ipcMain.handle('companion:top',(event,value)=>{if(trusted(event))companionMgr.top(value===true);});
    ipcMain.handle('companion:through',(event,value)=>{if(trusted(event))companionMgr.through(value===true);});
    ipcMain.handle('companion:chooseModel',async event=>{
      if(!trusted(event))return {canceled:true};
      const parent=windowMgr.win&&event.sender===windowMgr.win.webContents?windowMgr.win:companionMgr.win;
      try{
        const result=await dialog.showOpenDialog(parent,{title:'选择Live2D模型目录',properties:['openDirectory']});
        return {canceled:result.canceled,path:result.canceled?'':result.filePaths[0]};
      }finally{if(parent&&!parent.isDestroyed()&&parent.isVisible()){parent.focus();parent.webContents.focus();}}
    });
  }
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
    if (r.ok) {
      const url = `http://127.0.0.1:${r.port}`;
      windowMgr.reload(url);
      companionMgr?.reload(url);
    }
    return r;
  });

  ipcMain.handle('backend:log', (_e, lines = 200) => ({
    log: backend.tailLog(lines),
  }));

  // ── 打开本地目录（方便用户备份/排查）──────────────────────
  ipcMain.handle('data:chooseLegacyDir', async (event) => {
    // Only the app's top-level local management page may open this chooser.
    if (!windowMgr.win || event.sender !== windowMgr.win.webContents ||
        event.senderFrame !== event.sender.mainFrame) return { canceled: true };
    try {
      const url = new URL(event.senderFrame.url);
      if (url.origin !== backend.url || url.pathname !== '/') return { canceled: true };
    } catch (_) { return { canceled: true }; }
    const parent = windowMgr.win;
    try {
      const result = await dialog.showOpenDialog(parent, {
        title: '选择旧版数据目录（包含 config.json）',
        buttonLabel: '选择此数据目录',
        properties: ['openDirectory'],
      });
      return { canceled: result.canceled, path: result.canceled ? '' : result.filePaths[0] };
    } finally {
      windowMgr.restoreInputFocus(parent);
    }
  });

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
