/**
 * 系统托盘 —— 后台运行时的唯一前台入口。
 *
 * 菜单结构刻意保持极简：用户关闭窗口后，能凭直觉找到"怎么再打开"和"怎么彻底退出"。
 * 状态行用 disabled 项展示，让用户一眼看到机器人是否在跑。
 */
'use strict';

const path = require('path');
const fs = require('fs');
const { Tray, Menu, nativeImage, app } = require('electron');

const P = require('./paths');

class TrayManager {
  constructor() {
    this.tray = null;
    this.status = { backend: 'stopped', bot: 'unknown' };
  }

  /**
   * @param {object} handlers
   * @param {Function} handlers.onOpen    打开主界面
   * @param {Function} handlers.onRestart 重启后端
   * @param {Function} handlers.onQuit    退出应用
   */
  create({ onOpen, onRestart, onQuit }) {
    this._handlers = { onOpen, onRestart, onQuit };

    const icon = this._loadIcon();
    if (!icon || icon.isEmpty()) {
      // 没有图标就不创建托盘，但应用仍可运行（界面内提供退出入口）
      console.warn('[tray] 图标缺失，跳过托盘创建');
      return null;
    }

    this.tray = new Tray(icon);
    this.tray.setToolTip('微信 AI 助手');
    this._rebuildMenu();

    // 单击托盘图标 = 显示/隐藏主界面（Windows/Linux 习惯）
    this.tray.on('click', () => this._handlers.onOpen());
    // macOS 上单击默认弹出菜单，双击才触发 click
    this.tray.on('double-click', () => this._handlers.onOpen());

    return this.tray;
  }

  _loadIcon() {
    const dir = P.assetsDir();
    // macOS 模板图：纯黑+透明通道，系统会自动适配深浅色菜单栏
    const candidates = P.platform === 'darwin'
      ? ['trayTemplate.png', 'tray.png', 'icon.png']
      : ['tray.png', 'icon.png'];

    for (const name of candidates) {
      const p = path.join(dir, name);
      if (fs.existsSync(p)) {
        const img = nativeImage.createFromPath(p);
        if (!img.isEmpty()) {
          return P.platform === 'darwin'
            ? img.resize({ width: 18, height: 18 })
            : img.resize({ width: 16, height: 16 });
        }
      }
    }
    return null;
  }

  _statusLabel() {
    const b = { running: '运行中', starting: '启动中…', stopped: '已停止', crashed: '异常' };
    const backend = b[this.status.backend] || this.status.backend;
    return `服务状态：${backend}`;
  }

  _rebuildMenu() {
    if (!this.tray) return;

    const menu = Menu.buildFromTemplate([
      { label: '打开主界面', click: () => this._handlers.onOpen() },
      { type: 'separator' },
      { label: this._statusLabel(), enabled: false },
      { label: '重启后台服务', click: () => this._handlers.onRestart() },
      { type: 'separator' },
      { label: '退出', click: () => this._handlers.onQuit() },
    ]);

    this.tray.setContextMenu(menu);
  }

  /** 更新状态并刷新菜单 */
  setStatus(patch) {
    this.status = { ...this.status, ...patch };
    this._rebuildMenu();
    if (this.tray) {
      this.tray.setToolTip(`微信 AI 助手 · ${this._statusLabel()}`);
    }
  }

  /** 弹一条系统通知（后端异常时提醒用户） */
  notify(title, body) {
    if (this.tray) {
      try {
        this.tray.displayBalloon?.({ title, content: body });
      } catch (_) { /* 平台不支持则忽略 */ }
    }
  }

  destroy() {
    if (this.tray) {
      this.tray.destroy();
      this.tray = null;
    }
  }
}

module.exports = { TrayManager };
