/**
 * 跨平台路径解析 —— 所有"文件该放哪"的判断都集中在这里。
 *
 * 开发态与打包态的文件布局完全不同，把差异收敛到这一个模块，
 * 其他模块只管调用，不用关心自己跑在哪里。
 */
'use strict';

const path = require('path');
const os = require('os');
const fs = require('fs');
const { app } = require('electron');

/** 是否运行在打包后的应用里 */
const isPackaged = () => app.isPackaged;

/** 是否开发模式（可用 WXBOT_DEV=1 强制开启） */
const isDev = () => !app.isPackaged || process.env.WXBOT_DEV === '1';

/**
 * 用户数据目录 —— 配置、聊天记录、日志都放这里。
 * 与代码目录分离，卸载/重装应用不会丢数据。
 */
function userDataDir() {
  const dir = path.join(app.getPath('userData'), 'data');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** 日志目录 */
function logDir() {
  const dir = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/**
 * 定位 Python 后端可执行文件。
 *
 * 打包态：resources/backend/wxbot-server[.exe]（由 PyInstaller 产出的单文件）
 * 开发态：退回系统 Python，直接跑仓库里的 src.server
 */
function resolveBackend() {
  if (isPackaged()) {
    const exe = process.platform === 'win32' ? 'wxbot-server.exe' : 'wxbot-server';
    const bundled = path.join(process.resourcesPath, 'backend', exe);
    if (fs.existsSync(bundled)) {
      return { kind: 'binary', command: bundled, args: [] };
    }
    // 打包了但后端缺失 —— 明确报错，不要静默失败
    return { kind: 'missing', command: null, args: [], expected: bundled };
  }

  // 开发态：用仓库根目录的 Python 跑
  const repoRoot = path.resolve(__dirname, '..', '..', '..');
  const py = process.env.WXBOT_PYTHON || findSystemPython();
  return {
    kind: 'python',
    command: py,
    args: ['-m', 'src.server'],
    cwd: repoRoot,
  };
}

/** 依次尝试常见的 Python 解释器路径 */
function findSystemPython() {
  const candidates = [
    process.env.WXBOT_PYTHON,
    path.resolve(__dirname, '..', '..', '..', '.venv', 'bin', 'python3'),
    path.resolve(__dirname, '..', '..', '..', '.venv', 'Scripts', 'python.exe'),
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3',
    '/usr/bin/python3',
    'python3',
    'python',
  ].filter(Boolean);

  for (const c of candidates) {
    try {
      if (c.includes(path.sep) && !fs.existsSync(c)) continue;
      return c;
    } catch (_) { /* 忽略，继续找下一个 */ }
  }
  return 'python3';
}

/** 前端静态资源目录（复用仓库里已有的 src/web） */
function webRoot() {
  if (isPackaged()) return path.join(process.resourcesPath, 'web');
  return path.resolve(__dirname, '..', '..', '..', 'src', 'web');
}

/** 托盘 / 应用图标目录 */
function assetsDir() {
  if (isPackaged()) return path.join(process.resourcesPath, 'assets');
  return path.resolve(__dirname, '..', '..', 'assets');
}

module.exports = {
  isPackaged, isDev, userDataDir, logDir,
  resolveBackend, webRoot, assetsDir, findSystemPython,
  platform: process.platform,
  homeDir: os.homedir(),
};
