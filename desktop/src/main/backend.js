/**
 * Python 后端进程管理 —— 拉起 sidecar、健康检查、日志落盘、崩溃重启。
 *
 * 这是 Electron 外壳与 Python 引擎之间唯一的耦合点：
 *   外壳只负责"把这个进程管好"，不关心它内部是什么。
 *   换成任何别的本地 HTTP 服务，这个模块都能复用。
 */
'use strict';

const { spawn } = require('child_process');
const net = require('net');
const fs = require('fs');
const path = require('path');
const http = require('http');
const { EventEmitter } = require('events');

const P = require('./paths');

/** 尽量占住指定端口；被占用则返回 null */
function tryPort(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', () => resolve(null));
    srv.listen(port, '127.0.0.1', () => {
      srv.close(() => resolve(port));
    });
  });
}

/** 让系统随便给一个空闲端口 */
function findFreePort() {
  return tryPort(0);
}

/** 后端默认端口。
 *
 *  ⚠️ 为什么不能每次都随机：QQ 网关（NapCat）里的**反向 WebSocket 地址**
 *  是写在它自己配置里的，指向"后端所在端口"。端口每次变，网关就会去连一个
 *  没人监听的端口 —— QQ 消息一条都收不到（HTTP 探测却是通的，界面显示正常）。
 *  而让 NapCat 重新读配置需要重启容器，重启容器会**掉登录态、要重新扫码**。
 *  所以这里优先固定端口，只有确实被占用时才退化成随机端口。
 */
const PREFERRED_PORT = Number(process.env.WXBOT_PREFERRED_PORT || 17800);

/** 探测后端是否已就绪 */
function probe(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/healthz', timeout: timeoutMs },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Backend extends EventEmitter {
  constructor() {
    super();
    this.proc = null;
    this.port = null;
    this.state = 'stopped';   // stopped | starting | running | crashed
    this.restarts = 0;
    this.maxRestarts = 5;
    this._logStream = null;
    this._stopping = false;
  }

  get url() {
    return this.port ? `http://127.0.0.1:${this.port}` : null;
  }

  get isRunning() {
    return this.state === 'running';
  }

  /** 打开日志文件流 */
  _openLog() {
    if (this._logStream) return this._logStream;
    const file = path.join(P.logDir(), 'backend.log');
    this._logStream = fs.createWriteStream(file, { flags: 'a' });
    this._logStream.write(
      `\n${'='.repeat(60)}\n[${new Date().toISOString()}] backend starting\n`,
    );
    return this._logStream;
  }

  /**
   * 启动后端并等待就绪。
   * @returns {Promise<{ok: boolean, port?: number, error?: string}>}
   */
  async start() {
    if (this.state === 'running' || this.state === 'starting') {
      return { ok: true, port: this.port };
    }

    const target = P.resolveBackend();
    if (target.kind === 'missing') {
      const error =
        `未找到内置后端程序。\n期望路径：${target.expected}\n` +
        '请先执行 npm run build:backend 生成后端二进制。';
      this.state = 'crashed';
      this.emit('error', error);
      return { ok: false, error };
    }

    this.state = 'starting';
    this._stopping = false;
    // 先试固定端口（保持 QQ 网关配置长期有效），占用则退化为随机端口
    this.port = (await tryPort(PREFERRED_PORT)) || (await findFreePort());

    const env = {
      ...process.env,
      WXBOT_HOST: '127.0.0.1',
      WXBOT_PORT: String(this.port),
      WXBOT_DATA_DIR: P.userDataDir(),
      WXBOT_NO_BROWSER: '1',
      PYTHONUNBUFFERED: '1',
      PYTHONDONTWRITEBYTECODE: '1',
    };

    const log = this._openLog();
    log.write(`[spawn] ${target.command} ${target.args.join(' ')} (port=${this.port})\n`);

    try {
      this.proc = spawn(target.command, target.args, {
        cwd: target.cwd || process.cwd(),
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      });
    } catch (e) {
      this.state = 'crashed';
      const error = `后端启动失败：${e.message}`;
      this.emit('error', error);
      return { ok: false, error };
    }

    this.proc.stdout.on('data', (d) => log.write(d));
    this.proc.stderr.on('data', (d) => log.write(d));

    this.proc.on('error', (e) => {
      log.write(`[error] ${e.message}\n`);
      this.emit('error', `后端进程错误：${e.message}`);
    });

    this.proc.on('exit', (code, signal) => {
      log.write(`[exit] code=${code} signal=${signal}\n`);
      const wasStopping = this._stopping;
      this.proc = null;

      if (wasStopping) {
        this.state = 'stopped';
        this.emit('stopped');
        return;
      }

      // 非预期退出 → 尝试重启（带退避）
      this.state = 'crashed';
      if (this.restarts < this.maxRestarts) {
        this.restarts += 1;
        const delay = Math.min(1000 * 2 ** this.restarts, 15000);
        this.emit('crashed', { code, restarts: this.restarts, retryInMs: delay });
        setTimeout(() => {
          if (!this._stopping) this.start().then((r) => this.emit('restarted', r));
        }, delay);
      } else {
        this.emit('error', `后端连续 ${this.maxRestarts} 次异常退出，已停止重试。详见日志。`);
      }
    });

    // 等待就绪（最多 40 秒 —— 首次启动要初始化依赖）
    const deadline = Date.now() + 40000;
    while (Date.now() < deadline) {
      if (this.proc === null) {
        return { ok: false, error: '后端进程已退出，请查看日志' };
      }
      if (await probe(this.port)) {
        this.state = 'running';
        this.restarts = 0;
        log.write(`[ready] http://127.0.0.1:${this.port}\n`);
        this.emit('ready', this.port);
        return { ok: true, port: this.port };
      }
      await sleep(400);
    }

    this.state = 'crashed';
    const error = '后端启动超时（40 秒），请查看日志';
    this.emit('error', error);
    return { ok: false, error };
  }

  /** 优雅停止 */
  async stop() {
    if (!this.proc) {
      this.state = 'stopped';
      return;
    }
    this._stopping = true;
    const proc = this.proc;

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        try { proc.kill('SIGKILL'); } catch (_) { /* 已退出 */ }
        resolve();
      }, 5000);

      proc.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });

      try {
        proc.kill('SIGTERM');
      } catch (_) {
        clearTimeout(timer);
        resolve();
      }
    });
  }

  /** 重启（用于"应用内重启后端"） */
  async restart() {
    await this.stop();
    this.restarts = 0;
    await sleep(600);
    return this.start();
  }

  /** 读取日志尾部（供界面展示） */
  tailLog(lines = 200) {
    const file = path.join(P.logDir(), 'backend.log');
    if (!fs.existsSync(file)) return '(暂无日志)';
    try {
      const all = fs.readFileSync(file, 'utf8').split('\n');
      return all.slice(-lines).join('\n');
    } catch (e) {
      return `(读取日志失败：${e.message})`;
    }
  }
}

module.exports = { Backend, findFreePort, tryPort, probe, PREFERRED_PORT };
