/**
 * 窗口管理 —— 创建、隐藏、显示。
 *
 * 关键行为：点关闭按钮不退出应用，只把窗口藏起来。
 * 真正的退出只由托盘菜单「退出」或 Cmd/Ctrl+Q 触发。
 * 这样机器人能在后台继续静默运行。
 */
'use strict';

const path = require('path');
const { BrowserWindow, shell, app } = require('electron');

const P = require('./paths');

class WindowManager {
  constructor() {
    this.win = null;
    this._onShow = null;
    this._onHide = null;
  }

  /**
   * @param {object} opts
   * @param {string} opts.url       要加载的地址（本地后端）
   * @param {Function} opts.onShow  窗口显示时回调
   * @param {Function} opts.onHide  窗口隐藏时回调
   */
  create({ url, onShow, onHide }) {
    this._onShow = onShow;
    this._onHide = onHide;

    this.win = new BrowserWindow({
      width: 1280,
      height: 850,
      minWidth: 960,
      minHeight: 640,
      show: false,                       // 等渲染完再显示，避免白屏闪烁
      title: '微信 AI 助手',
      backgroundColor: '#F4F6F8',
      // macOS 用隐藏式标题栏，界面更干净；Windows/Linux 保持标准边框
      titleBarStyle: P.platform === 'darwin' ? 'hiddenInset' : 'default',
      trafficLightPosition: P.platform === 'darwin' ? { x: 16, y: 18 } : undefined,
      icon: this._appIcon(),
      webPreferences: {
        preload: path.join(__dirname, '..', 'preload', 'index.js'),
        contextIsolation: true,          // 安全：渲染进程拿不到 Node
        nodeIntegration: false,
        sandbox: false,                  // preload 需要 require('electron')
        spellcheck: false,
      },
    });

    this.win.loadURL(url);

    this.win.once('ready-to-show', () => {
      this.show();
    });

    // ★ 核心：拦截关闭 → 改为隐藏
    this.win.on('close', (e) => {
      if (!app.isQuitting) {
        e.preventDefault();
        this.hide();
      }
    });

    this.win.on('closed', () => {
      this.win = null;
    });

    // 外部链接用系统浏览器打开，不在应用内跳走
    this.win.webContents.setWindowOpenHandler(({ url: target }) => {
      if (/^https?:\/\//.test(target)) shell.openExternal(target);
      return { action: 'deny' };
    });

    return this.win;
  }

  _appIcon() {
    const png = path.join(P.assetsDir(), 'icon.png');
    return require('fs').existsSync(png) ? png : undefined;
  }

  show() {
    if (!this.win) return;
    if (this.win.isMinimized()) this.win.restore();
    this.win.show();
    this.win.focus();
    if (P.platform === 'darwin' && app.dock) app.dock.show();
    this._onShow && this._onShow();
  }

  hide() {
    if (!this.win) return;
    this.win.hide();
    // macOS 上隐藏窗口后把 Dock 图标也收起来，更像"后台静默运行"
    if (P.platform === 'darwin' && app.dock) app.dock.hide();
    this._onHide && this._onHide();
  }

  toggle() {
    if (!this.win) return;
    if (this.win.isVisible()) this.hide();
    else this.show();
  }

  isVisible() {
    return !!this.win && this.win.isVisible();
  }

  /** 把状态推给渲染进程（例如后端重启后刷新界面） */
  send(channel, payload) {
    if (this.win && !this.win.isDestroyed()) {
      this.win.webContents.send(channel, payload);
    }
  }

  /** 重载界面（后端地址变化时用） */
  reload(url) {
    if (!this.win) return;
    if (url) this.win.loadURL(url);
    else this.win.webContents.reload();
  }

  /** 真正销毁窗口（退出流程里用） */
  destroy() {
    if (this.win && !this.win.isDestroyed()) {
      this.win.destroy();
    }
    this.win = null;
  }
}

module.exports = { WindowManager };
