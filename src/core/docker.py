"""Docker 环境管理 —— 检测、安装引导、启动守护进程、部署 QQ 网关容器。

═══════════════════════════════════════════════════════════════════
设计前提：**尽可能自动，但不假装能全自动。**

能自动完成的：
  · 检测 Docker 是否安装 / 守护进程是否运行
  · 下载安装包
  · 打开安装程序（macOS 挂载 DMG 并打开窗口 / Windows 启动 exe）
  · 启动 Docker Desktop 并轮询等待就绪
  · 拉取镜像、启动容器、等待端口可用

无法自动完成（系统级限制，必须用户操作）：
  · 输入管理员密码
  · 首次启动时同意 Docker 许可协议
  · Windows 上启用 WSL2

所以流程设计为"自动跑到卡点，明确告诉用户下一步做什么，然后继续自动"。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# NapCat（QQ / OneBot 11）容器
NAPCAT_IMAGE = "mlikiowa/napcat-docker:latest"
NAPCAT_CONTAINER = "napcat"
NAPCAT_DATA_VOLUME = "wxbot-napcat-data"      # QQ 登录态，必须持久化
NAPCAT_CONFIG_VOLUME = "wxbot-napcat-config"  # onebot11.json / webui.json
NAPCAT_WEBUI_PORT = 6099
NAPCAT_HTTP_PORT = 3000

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """同步执行命令，返回 (退出码, stdout, stderr)。"""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0,
            env={**os.environ, "PATH": _augmented_path()},
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "命令不存在"
    except subprocess.TimeoutExpired:
        return 124, "", "执行超时"
    except Exception as e:
        return 1, "", str(e)


def _augmented_path() -> str:
    """把常见安装路径并进 PATH。

    桌面应用从 Finder 启动时，PATH 往往只有 /usr/bin:/bin:/usr/sbin:/sbin，
    找不到 /usr/local/bin/docker 或 /opt/homebrew/bin/brew。
    """
    extra = [
        "/usr/local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
        str(Path.home() / ".docker" / "bin"),
        "/Applications/Docker.app/Contents/Resources/bin",
        r"C:\Program Files\Docker\Docker\resources\bin",
    ]
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for p in extra:
        if p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


@dataclass
class DockerState:
    """Docker 环境快照。"""
    installed: bool = False          # docker CLI 是否可用
    running: bool = False            # 守护进程是否在跑
    version: str = ""
    cli_path: str = ""
    desktop_app: bool = False        # macOS: Docker.app 是否存在
    brew_available: bool = False
    platform: str = ""
    arch: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "installed": self.installed, "running": self.running,
            "version": self.version, "cliPath": self.cli_path,
            "desktopApp": self.desktop_app, "brewAvailable": self.brew_available,
            "platform": self.platform, "arch": self.arch, "error": self.error,
        }


class DockerManager:
    """Docker 环境与网关容器的生命周期管理。"""

    def __init__(self, download_dir: Optional[Path] = None):
        self.download_dir = download_dir or Path.home() / "Downloads"
        # 长任务的进度状态，供前端轮询
        self.task: dict = {"running": False, "stage": "", "progress": 0,
                           "message": "", "error": "", "needsUser": False}

    # ══ 状态检测 ══════════════════════════════════════════════

    def detect(self) -> DockerState:
        """检测 Docker 安装与运行状态。"""
        st = DockerState(platform=platform.system(), arch=platform.machine())

        cli = shutil.which("docker", path=_augmented_path())
        if cli:
            st.installed = True
            st.cli_path = cli
            code, out, _ = _run([cli, "--version"], timeout=10)
            if code == 0:
                # 形如 "Docker version 27.3.1, build ce12230"
                st.version = out.split(",")[0].replace("Docker version ", "").strip()

        if IS_MAC:
            st.desktop_app = Path("/Applications/Docker.app").exists()
            if st.desktop_app and not st.installed:
                # 装了 App 但 CLI 没进 PATH，补上
                inner = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
                if inner.exists():
                    st.installed = True
                    st.cli_path = str(inner)

        if IS_MAC or IS_LINUX:
            st.brew_available = shutil.which("brew", path=_augmented_path()) is not None

        # 守护进程
        if st.installed:
            code, _, err = _run([st.cli_path, "info", "--format", "{{.ServerVersion}}"], timeout=15)
            if code == 0:
                st.running = True
            elif "Cannot connect" in err or "connect" in err.lower():
                st.error = "Docker 已安装，但服务未启动"
            else:
                st.error = err[:200]

        # 兜底：端口能通也算在跑（有些环境 CLI 权限不足）
        if not st.running and port_open(NAPCAT_WEBUI_PORT):
            st.running = True

        return st

    # ══ 安装方案 ══════════════════════════════════════════════

    def install_plan(self) -> dict:
        """给出当前平台最优的安装方案，并说明哪些步骤需要用户操作。"""
        st = self.detect()

        if st.installed and st.running:
            return {"needed": False, "message": "Docker 已就绪"}

        if st.installed and not st.running:
            return {
                "needed": False, "action": "start",
                "title": "启动 Docker 服务",
                "message": "Docker 已安装，只需启动服务",
                "automatable": True,
                "steps": [{"text": "点击下方按钮，自动启动 Docker 并等待就绪", "auto": True}],
            }

        if IS_MAC:
            if st.brew_available:
                return {
                    "needed": True, "action": "brew",
                    "title": "安装 Docker",
                    "message": "检测到 Homebrew，可以自动安装",
                    "automatable": True,
                    "command": "brew install --cask docker",
                    "steps": [
                        {"text": "自动打开终端并执行安装命令", "auto": True},
                        {"text": "在弹出的终端里输入你的开机密码", "auto": False},
                        {"text": "安装完成后，首次打开 Docker 需手动同意许可协议", "auto": False},
                        {"text": "完成后回到本页面，应用会自动部署 QQ 网关", "auto": True},
                    ],
                    "note": "不想用 Docker Desktop？也可以在终端执行 "
                            "brew install colima && colima start，无需同意许可协议。",
                }
            return {
                "needed": True, "action": "download-mac",
                "title": "安装 Docker Desktop",
                "message": "未检测到 Homebrew，将自动下载官方安装包",
                "automatable": True,
                "steps": [
                    {"text": "自动下载 Docker Desktop（约 600MB）", "auto": True},
                    {"text": "自动挂载安装包并打开窗口", "auto": True},
                    {"text": "把 Docker 图标拖进「应用程序」文件夹", "auto": False},
                    {"text": "首次打开 Docker 需输入密码并同意许可协议", "auto": False},
                    {"text": "完成后回到本页面，应用会自动部署 QQ 网关", "auto": True},
                ],
                "url": self._docker_download_url(),
            }

        if IS_WIN:
            return {
                "needed": True, "action": "download-win",
                "title": "安装 Docker Desktop",
                "message": "将自动下载官方安装程序",
                "automatable": True,
                "steps": [
                    {"text": "自动下载 Docker Desktop 安装程序（约 600MB）", "auto": True},
                    {"text": "自动启动安装程序", "auto": True},
                    {"text": "按提示完成安装（需管理员权限，保持「使用 WSL2」默认勾选）", "auto": False},
                    {"text": "安装完成后可能需要重启电脑", "auto": False},
                    {"text": "完成后回到本页面，应用会自动部署 QQ 网关", "auto": True},
                ],
                "url": "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe",
            }

        return {
            "needed": True, "action": "manual-linux",
            "title": "安装 Docker",
            "message": "Linux 需要你用包管理器安装（需 sudo 权限）",
            "automatable": False,
            "steps": [
                {"text": "Ubuntu/Debian：sudo apt install docker.io", "auto": False},
                {"text": "CentOS/RHEL：sudo yum install docker", "auto": False},
                {"text": "启动服务：sudo systemctl enable --now docker", "auto": False},
                {"text": "加入 docker 组：sudo usermod -aG docker $USER（需重新登录生效）", "auto": False},
                {"text": "完成后回到本页面，应用会自动部署 QQ 网关", "auto": True},
            ],
        }

    def _docker_download_url(self) -> str:
        arch = platform.machine().lower()
        if arch in ("arm64", "aarch64"):
            return "https://desktop.docker.com/mac/main/arm64/Docker.dmg"
        return "https://desktop.docker.com/mac/main/amd64/Docker.dmg"

    # ══ 长任务：下载 / 安装 / 启动 / 拉起容器 ═══════════════════

    def _set_task(self, **kw) -> None:
        self.task.update(kw)

    async def prepare(self) -> dict:
        """一键准备：按需下载安装包 / 启动守护进程。

        返回后前端可通过 task 字段轮询进度。
        """
        if self.task.get("running"):
            return {"ok": False, "message": "已有任务在执行中"}

        asyncio.create_task(self._prepare_impl())
        return {"ok": True, "message": "已开始准备"}

    async def _prepare_impl(self) -> None:
        self._set_task(running=True, stage="detect", progress=5,
                       message="正在检测 Docker 环境…", error="", needsUser=False)
        try:
            st = self.detect()

            # 情况 1：已装且在跑 —— 直接进入容器阶段
            if st.installed and st.running:
                self._set_task(running=False, stage="ready", progress=100,
                               message="Docker 已就绪")
                return

            # 情况 2：已装未启动 —— 自动启动
            if st.installed and not st.running:
                await self._start_daemon_impl()
                return

            # 情况 3：未安装
            plan = self.install_plan()
            if plan.get("action") == "brew":
                await self._install_via_brew()
            elif plan.get("action") in ("download-mac", "download-win"):
                await self._download_and_open(plan.get("url", ""))
            else:
                self._set_task(running=False, stage="manual", progress=100,
                               needsUser=True,
                               message="需要你手动安装 Docker，请按下方步骤操作")
        except Exception as e:
            log.exception("准备 Docker 失败")
            self._set_task(running=False, stage="error", error=str(e),
                           message=f"准备失败：{e}")

    async def _install_via_brew(self) -> None:
        """用 Homebrew 安装 Docker。"""
        brew = shutil.which("brew", path=_augmented_path())
        self._set_task(stage="install", progress=15,
                       message="正在通过 Homebrew 安装 Docker…",
                       needsUser=True)
        # brew 安装 Docker Desktop 需要输入密码，因此用终端窗口跑，
        # 让用户能看到进度并输入密码
        script = (
            'echo "即将安装 Docker Desktop（需要输入你的开机密码）"; '
            'brew install --cask docker; '
            'echo ""; echo "安装结束。按回车关闭本窗口。"; read -r _'
        )
        self._open_terminal(script)
        self._set_task(running=False, stage="installing", progress=30,
                       needsUser=True,
                       message="已打开终端窗口执行安装。请在窗口中输入密码完成安装，"
                               "安装完成后回到这里点「继续」")

    async def _download_and_open(self, url: str) -> None:
        """下载安装包并打开。"""
        if not url:
            self._set_task(running=False, stage="error", error="下载地址为空")
            return

        name = "Docker.dmg" if IS_MAC else "DockerDesktopInstaller.exe"
        target = self.download_dir / name

        self._set_task(stage="download", progress=10,
                       message=f"正在下载 {name}（约 600MB，请耐心等待）…")
        try:
            import httpx
            from .http import make_client
            # 下载地址是公网，走系统代理没问题
            async with make_client(url, timeout=1800.0) as c:
                async with c.stream("GET", url) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    done = 0
                    with open(target, "wb") as f:
                        async for chunk in r.aiter_bytes(1024 * 256):
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = 10 + int(done / total * 70)
                                self._set_task(
                                    progress=min(pct, 80),
                                    message=f"正在下载 {name}… {done // 1048576}MB / {total // 1048576}MB",
                                )
        except Exception as e:
            self._set_task(running=False, stage="error",
                           error=f"下载失败：{e}",
                           message=f"下载失败：{e}。可手动下载：{url}")
            return

        self._set_task(stage="open", progress=85, message="正在打开安装包…")
        opened = await asyncio.to_thread(self._open_installer, target)

        self._set_task(running=False, stage="installing", progress=90, needsUser=True,
                       message=("已打开安装包。请把 Docker 图标拖进「应用程序」文件夹，"
                                "然后打开 Docker 并同意许可协议。完成后回到这里点「继续」"
                                if IS_MAC else
                                "已启动安装程序。请按提示完成安装（需管理员权限，"
                                "保持「使用 WSL2」默认勾选）。完成后回到这里点「继续」")
                       if opened else
                       f"安装包已下载到 {target}，请手动打开安装。完成后点「继续」")

    def _open_installer(self, path: Path) -> bool:
        """打开安装包。"""
        try:
            if IS_MAC:
                # 挂载 DMG 并打开窗口
                code, _, _ = _run(["hdiutil", "attach", str(path)], timeout=120)
                if code == 0:
                    _run(["open", "-a", "Finder", "/Volumes/Docker"], timeout=20)
                    return True
                return False
            if IS_WIN:
                os.startfile(str(path))          # type: ignore[attr-defined]
                return True
            return False
        except Exception:
            log.exception("打开安装包失败")
            return False

    def _open_terminal(self, script: str) -> bool:
        """在新终端窗口里执行脚本（用于需要输入密码的场景）。"""
        try:
            if IS_MAC:
                # 用 AppleScript 让 Terminal 执行命令，用户能看到进度并输密码
                safe = script.replace('\\', '\\\\').replace('"', '\\"')
                _run(["osascript", "-e",
                      f'tell application "Terminal" to do script "{safe}"'], timeout=20)
                _run(["osascript", "-e", 'tell application "Terminal" to activate'], timeout=20)
                return True
            if IS_WIN:
                _run(["cmd", "/c", "start", "cmd", "/k", script], timeout=20)
                return True
            # Linux：尝试常见终端
            for term in ("gnome-terminal", "konsole", "xterm"):
                if shutil.which(term, path=_augmented_path()):
                    _run([term, "-e", "bash", "-c", script], timeout=20)
                    return True
            return False
        except Exception:
            log.exception("打开终端失败")
            return False

    async def start_daemon(self) -> dict:
        if self.task.get("running"):
            return {"ok": False, "message": "已有任务在执行中"}
        asyncio.create_task(self._start_daemon_impl())
        return {"ok": True, "message": "正在启动 Docker…"}

    async def _start_daemon_impl(self) -> None:
        self._set_task(running=True, stage="daemon", progress=10,
                       message="正在启动 Docker 服务…", error="", needsUser=False)
        try:
            st = self.detect()
            if IS_MAC:
                if st.desktop_app:
                    _run(["open", "-a", "Docker"], timeout=30)
                elif st.brew_available:
                    # colima 之类的轻量方案
                    self._open_terminal("colima start; echo 完成; read -r _")
            elif IS_WIN:
                exe = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
                if Path(exe).exists():
                    os.startfile(exe)            # type: ignore[attr-defined]
            else:
                _run(["sudo", "systemctl", "start", "docker"], timeout=60)

            # 轮询等待就绪（Docker Desktop 冷启动可能要 1-2 分钟）
            for i in range(90):
                await asyncio.sleep(2)
                if self.detect().running:
                    self._set_task(running=False, stage="ready", progress=100,
                                   message="Docker 已启动")
                    return
                self._set_task(progress=min(10 + i, 90),
                               message=f"正在等待 Docker 就绪…（已等待 {(i + 1) * 2} 秒）")

            self._set_task(running=False, stage="timeout", progress=100, needsUser=True,
                           message="等待超时。请手动打开 Docker 应用，"
                                   "首次启动需要同意许可协议。完成后点「继续」")
        except Exception as e:
            log.exception("启动 Docker 失败")
            self._set_task(running=False, stage="error", error=str(e),
                           message=f"启动失败：{e}")

    # ══ NapCat（QQ / OneBot 11）═════════════════════════════════
    #
    # 关键点：NapCat 的 entrypoint **只在配置文件不存在时**
    # 才按环境变量生成 webui.json / onebot11.json，
    # 而 OneBot 配置的写入接口又有 "Not Login" 前置检查。
    # 所以必须在容器**启动前**把配置灌进卷里 —— 见 `seed_napcat_config()`。

    def napcat_status(self) -> dict:
        """NapCat 容器状态快照。"""
        base = {
            "container": NAPCAT_CONTAINER,
            "image": NAPCAT_IMAGE,
            "running": False,
            "exists": False,
            "state": "",
            "webuiPort": NAPCAT_WEBUI_PORT,
            "httpPort": NAPCAT_HTTP_PORT,
            "message": "",
        }
        st = self.detect()
        if not st.installed:
            return {**base, "message": "Docker 未安装"}
        if not st.running:
            # Docker 没跑，但端口通说明容器在别的环境里跑着
            if port_open(NAPCAT_WEBUI_PORT):
                return {**base, "running": True, "message": "NapCat 正在运行（外部 Docker）"}
            return {**base, "message": "Docker 未运行"}

        code, out, _ = _run([st.cli_path, "ps", "-a",
                             "--filter", f"name=^{NAPCAT_CONTAINER}$",
                             "--format", "{{.State}}"], timeout=15)
        if code == 0 and out:
            state = out.strip()
            if state == "running" and port_open(NAPCAT_WEBUI_PORT):
                return {**base, "running": True, "exists": True, "state": state,
                        "message": "NapCat 正在运行"}
            msg = {"exited": "容器已停止", "created": "容器已创建但未启动",
                   "running": "容器运行中（端口未就绪）"}.get(state, f"容器状态：{state}")
            return {**base, "exists": True, "state": state, "message": msg}

        return {**base, "message": "尚未创建 NapCat 容器"}

    def napcat_config_files(self, webui_token: str,
                            ws_url: str) -> tuple[dict, dict]:
        """生成要预置进容器的 two 个配置文件。

        `onebot11.json` 字段来自 NapCat 的 `OneBotConfigSchema`，
        **缺字段会被 typebox 校验拒绝**，所以这里写全。

        网络方案：**反向 WebSocket**（NapCat 主动连宿主机的 /ws/onebot）
          + HTTP 服务（宿主机用它发消息）
        反向 WS 里访问宿主必须用 `host.docker.internal`，不能用 127.0.0.1
        —— 容器里的 127.0.0.1 是容器自己。
        """
        webui = {
            "host": "0.0.0.0",
            "prefix": "",
            "port": NAPCAT_WEBUI_PORT,
            "token": webui_token,
            "loginRate": 3,
        }
        onebot = {
            "network": {
                "httpServers": [{
                    "name": "http-server",
                    "enable": True,
                    "port": NAPCAT_HTTP_PORT,
                    "host": "0.0.0.0",
                    "enableCors": True,
                    "messagePostFormat": "array",
                    "token": "",
                    "debug": False,
                }],
                "httpSseServers": [],
                "httpClients": [],
                "websocketServers": [],
                "websocketClients": [{
                    "name": "wxbot-reverse-ws",
                    "enable": True,
                    "url": ws_url,
                    "reportSelfMessage": False,
                    "messagePostFormat": "array",
                    "token": "",
                    "debug": False,
                    "heartInterval": 30000,
                    "reconnectInterval": 5000,
                }],
                "plugins": [],
            },
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
            "imageDownloadProxy": "",
            "timeout": {
                "baseTimeout": 10000,
                "uploadSpeedKBps": 256,
                "downloadSpeedKBps": 256,
                "maxTimeout": 1800000,
            },
        }
        return webui, onebot

    async def seed_napcat_config(self, webui_token: str, ws_url: str) -> dict:
        """把配置写进 named volume（**必须在容器启动前**执行）。

        为什么不用环境变量？NapCat 的 entrypoint **只在配置文件不存在时**
        才按 `WEBUI_TOKEN` / `MODE` 生成配置；一旦容器跑过一次，
        后面再改环境变量也不会生效。而 OneBot 配置的写入接口又有
        "Not Login" 前置检查，等登录后再写就来不及了。
        所以只能"先灌卷、再启动"。

        macOS/Windows 下 named volume 在虚拟机里，宿主机无法直接写，
        因此用一个一次性的 alpine 容器挂载该卷，再用 `docker cp` 拷进去。
        ⚠️ 不要用 `docker run -i ... sh -c 'cat > file'`：
        `_run()` 走 subprocess 但没接 stdin，文件会是空的。
        """
        st = self.detect()
        if not st.installed or not st.running:
            return {"ok": False, "message": "Docker 未就绪"}

        webui, onebot = self.napcat_config_files(webui_token, ws_url)
        payload = {
            "webui.json": json.dumps(webui, ensure_ascii=False, indent=2),
            "onebot11.json": json.dumps(onebot, ensure_ascii=False, indent=2),
        }

        code, _, err = await asyncio.to_thread(
            _run, [st.cli_path, "pull", "alpine:latest"], 600)
        if code != 0:
            log.warning("拉取 alpine 失败，尝试直接灌卷：%s", err[:200])

        tmpdir = Path(tempfile.mkdtemp(prefix="wxbot-napcat-"))
        try:
            for name, text in payload.items():
                local = tmpdir / name
                local.write_text(text, encoding="utf-8")

            helper = "wxbot-cfg-helper"
            await asyncio.to_thread(_run, [st.cli_path, "rm", "-f", helper], 60)
            code, _, err = await asyncio.to_thread(_run, [
                st.cli_path, "run", "-d", "--name", helper,
                "-v", f"{NAPCAT_CONFIG_VOLUME}:/dst",
                "alpine:latest", "sleep", "120",
            ], 120)
            if code != 0:
                return {"ok": False, "message": f"创建临时容器失败：{err[:200]}"}

            try:
                # ⚠️ 只能「拷目录」不能「拷单个文件」：
                # `docker cp 文件 容器:/dst/webui.json` 在目标已存在时行为不确定，
                # 实测会得到 **0 字节的空文件** —— NapCat 随后按默认配置启动，
                # WebUI Token 与我们对不上，扫码接口直接 401，而且毫无报错。
                code, _, err = await asyncio.to_thread(_run, [
                    st.cli_path, "cp", f"{tmpdir}/.", f"{helper}:/dst",
                ], 60)
                if code != 0:
                    return {"ok": False, "message": f"写入配置失败：{err[:200]}"}

                # 校验真的写进去了（空文件等于没写）
                code, out, err = await asyncio.to_thread(_run, [
                    st.cli_path, "exec", helper, "sh", "-c",
                    "wc -c /dst/webui.json /dst/onebot11.json 2>&1",
                ], 60)
                if code != 0:
                    return {"ok": False, "message": f"校验配置失败：{err[:200]}"}
                sizes = [int(x.split()[0]) for x in out.splitlines()
                         if x.strip() and x.split()[0].isdigit()]
                if len(sizes) < 2 or min(sizes) <= 0:
                    return {"ok": False,
                            "message": f"配置写入后为空，部署中止（{out.strip()[:150]}）"}
            finally:
                await asyncio.to_thread(_run, [st.cli_path, "rm", "-f", helper], 60)

            # 已登录过的账号会有一份 onebot11_<qq>.json 且优先生效，
            # 只写 onebot11.json 是不够的 —— 顺手把旧文件里的地址一起改掉
            await self.sync_napcat_ws_url(ws_url)

            return {"ok": True, "message": "配置已预置"}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    #: 只允许这种形态的反向 WS 地址进入 shell（防注入）
    _WS_URL_RE = re.compile(r"^wss?://[A-Za-z0-9._:\-]+(?::\d+)?/[A-Za-z0-9._/\-]*$")

    async def sync_napcat_ws_url(self, ws_url: str) -> dict:
        """把 NapCat 配置里的反向 WS 地址改成当前后端地址。

        ⚠️ 这是真实踩过的坑：桌面版**每次启动都会挑一个空闲端口**
        （见 desktop/src/main/backend.js 的 findFreePort），而反向 WS 地址是
        **部署那一刻**写进 NapCat 配置的。只要下次启动换了端口，
        网关就会一直去连一个早已没人监听的端口 ——
        表现为「QQ 一条消息都收不到」，而界面上却显示"已连接"（HTTP 探测是通的）。

        还有一个更隐蔽的点：NapCat 登录后会生成**每个账号一份**的配置
        `onebot11_<qq>.json`，它**优先于** `onebot11.json`。
        只改通用的那份是没用的，必须把所有 onebot11*.json 一起改。
        """
        if not self._WS_URL_RE.match(ws_url or ""):
            return {"ok": False, "changed": False,
                    "message": f"反向 WS 地址不合法：{ws_url!r}"}

        st = self.detect()
        if not st.installed or not st.running:
            return {"ok": False, "changed": False, "message": "Docker 未运行"}

        helper = "wxbot-cfg-helper"
        await asyncio.to_thread(_run, [st.cli_path, "rm", "-f", helper], 60)
        code, _, err = await asyncio.to_thread(_run, [
            st.cli_path, "run", "-d", "--name", helper,
            "-v", f"{NAPCAT_CONFIG_VOLUME}:/dst",
            "alpine:latest", "sleep", "120",
        ], 120)
        if code != 0:
            return {"ok": False, "changed": False,
                    "message": f"创建临时容器失败：{err[:200]}"}

        try:
            # ① 先看现在写的是什么（一个文件都没有 = 还没部署过，无需同步）
            code, out, _ = await asyncio.to_thread(_run, [
                st.cli_path, "exec", helper, "sh", "-c",
                "grep -h -o 'ws://[^\"]*' /dst/onebot11*.json 2>/dev/null | sort -u",
            ], 60)
            old_urls = [x.strip() for x in out.splitlines() if x.strip()]
            if not old_urls:
                return {"ok": True, "changed": False,
                        "message": "还没有 NapCat 配置，无需同步"}
            if old_urls == [ws_url]:
                return {"ok": True, "changed": False, "url": ws_url,
                        "message": "反向 WS 地址已是最新"}

            # ② 改所有 onebot11*.json（含每个账号一份的配置）
            code, _, err = await asyncio.to_thread(_run, [
                st.cli_path, "exec", helper, "sh", "-c",
                f"sed -i 's|\"url\": \"ws://[^\"]*\"|\"url\": \"{ws_url}\"|g' "
                "/dst/onebot11*.json",
            ], 60)
            if code != 0:
                return {"ok": False, "changed": False,
                        "message": f"写入配置失败：{err[:200]}"}

            # ③ 校验：凡是原本有 ws 地址的文件，现在都必须指向新地址
            code, out, err = await asyncio.to_thread(_run, [
                st.cli_path, "exec", helper, "sh", "-c",
                f"grep -l '\"{ws_url}\"' /dst/onebot11*.json 2>/dev/null | wc -l; "
                "grep -h -o 'ws://[^\"]*' /dst/onebot11*.json 2>/dev/null | sort -u",
            ], 60)
            lines = [x.strip() for x in out.splitlines() if x.strip()]
            if not lines or not lines[0].isdigit() or int(lines[0]) < 1:
                return {"ok": False, "changed": False,
                        "message": f"配置写入后校验失败（{out.strip()[:150]}）"}
            remaining = lines[1:]
            if any(u != ws_url for u in remaining):
                return {"ok": False, "changed": False,
                        "message": f"仍有旧地址未被替换：{remaining}"}
        finally:
            await asyncio.to_thread(_run, [st.cli_path, "rm", "-f", helper], 60)

        # ④ NapCat 只在启动时读配置 → 必须重启容器才生效
        restarted = False
        code, out, _ = await asyncio.to_thread(_run, [
            st.cli_path, "ps", "--filter", f"name=^{NAPCAT_CONTAINER}$",
            "--format", "{{.Names}}"], 30)
        if code == 0 and out.strip() == NAPCAT_CONTAINER:
            self._set_task(running=True, stage="napcat", progress=50,
                           message="正在让 QQ 网关重新连接…", error="", needsUser=False)
            await asyncio.to_thread(_run, [st.cli_path, "restart", NAPCAT_CONTAINER], 180)
            restarted = True
            self._set_task(running=False, stage="waiting_login", progress=100,
                           message="QQ 网关已重新连接")

        log.info("NapCat 反向 WS 地址已同步为 %s（旧：%s，重启容器=%s）",
                 ws_url, old_urls, restarted)
        msg = "已让 QQ 网关连到当前端口"
        if restarted:
            # ⚠️ 重启容器可能掉 QQ 登录态（实测过），必须提前说清楚
            msg += "并重启了网关；若 QQ 提示未登录，请重新扫码一次"
        return {"ok": True, "changed": True, "restarted": restarted, "url": ws_url,
                "oldUrls": old_urls, "message": msg}

    async def ensure_napcat(self, webui_token: str, ws_url: str,
                            extra_hosts: bool = True) -> dict:
        """拉镜像 → 预置配置 → 启动容器 → 等 WebUI 端口就绪。"""
        if self.task.get("running"):
            return {"ok": False, "message": "已有任务在执行中"}
        asyncio.create_task(self._ensure_napcat_impl(webui_token, ws_url, extra_hosts))
        return {"ok": True, "message": "已开始部署 QQ 网关"}

    async def _ensure_napcat_impl(self, webui_token: str, ws_url: str,
                                  extra_hosts: bool) -> None:
        self._set_task(running=True, stage="napcat", progress=5,
                       message="正在检查 Docker…", error="", needsUser=False)
        try:
            st = self.detect()
            if not st.installed:
                self._set_task(running=False, stage="need_docker", needsUser=True,
                               message="需要先安装 Docker")
                return
            if not st.running:
                self._set_task(running=False, stage="need_daemon", needsUser=True,
                               message="需要先启动 Docker 服务")
                return
            cli = st.cli_path

            self._set_task(progress=10, message="正在准备 NapCat 配置…")
            seed = await self.seed_napcat_config(webui_token, ws_url)
            if not seed.get("ok"):
                self._set_task(running=False, stage="error",
                               error=seed.get("message", ""),
                               message=seed.get("message", "配置预置失败"))
                return

            self._set_task(progress=30, message="正在拉取 NapCat 镜像（首次约 500MB）…")
            code, _, err = await asyncio.to_thread(
                _run, [cli, "pull", NAPCAT_IMAGE], 3600)
            if code != 0:
                self._set_task(running=False, stage="error", error=err[:300],
                               message=f"拉取镜像失败：{err[:200]}")
                return

            # 已有同名容器 → 重新创建（保证配置生效）
            code, out, _ = await asyncio.to_thread(_run, [
                cli, "ps", "-a", "--filter", f"name=^{NAPCAT_CONTAINER}$",
                "--format", "{{.Names}}"], 20)
            if code == 0 and out.strip() == NAPCAT_CONTAINER:
                self._set_task(progress=50, message="正在重建容器（让新配置生效）…")
                await asyncio.to_thread(_run, [cli, "rm", "-f", NAPCAT_CONTAINER], 120)

            self._set_task(progress=70, message="正在启动 NapCat…")
            cmd = [
                cli, "run", "-d",
                "--name", NAPCAT_CONTAINER,
                "--restart", "unless-stopped",
                "-p", f"127.0.0.1:{NAPCAT_WEBUI_PORT}:{NAPCAT_WEBUI_PORT}",
                "-p", f"127.0.0.1:{NAPCAT_HTTP_PORT}:{NAPCAT_HTTP_PORT}",
                "-v", f"{NAPCAT_CONFIG_VOLUME}:/app/napcat/config",
                "-v", f"{NAPCAT_DATA_VOLUME}:/app/.config/QQ",
            ]
            if extra_hosts and not IS_MAC and not IS_WIN:
                # Linux 下容器访问宿主需要显式映射
                cmd += ["--add-host", "host.docker.internal:host-gateway"]
            cmd.append(NAPCAT_IMAGE)

            code, _, err = await asyncio.to_thread(_run, cmd, 180)
            if code != 0:
                self._set_task(running=False, stage="error", error=err[:300],
                               message=f"创建容器失败：{err[:200]}")
                return

            self._set_task(progress=85, message="正在等待 NapCat 就绪…")
            for i in range(90):
                await asyncio.sleep(2)
                if port_open(NAPCAT_WEBUI_PORT):
                    self._set_task(running=False, stage="waiting_login", progress=100,
                                   message="部署完成，请用手机 QQ 扫码登录")
                    return
                self._set_task(progress=min(85 + i // 3, 97),
                               message=f"正在等待 NapCat 就绪…（已等待 {(i + 1) * 2} 秒）")

            self._set_task(running=False, stage="timeout", progress=100, needsUser=True,
                           message="NapCat 启动超时。可执行 docker logs napcat 查看日志")
        except Exception as e:
            log.exception("部署 NapCat 失败")
            self._set_task(running=False, stage="error", error=str(e),
                           message=f"部署失败：{e}")

    async def stop_napcat(self) -> dict:
        st = self.detect()
        if not st.installed:
            return {"ok": False, "message": "Docker 未安装"}
        code, _, err = await asyncio.to_thread(
            _run, [st.cli_path, "stop", NAPCAT_CONTAINER], 60)
        if code == 0:
            return {"ok": True, "message": "NapCat 已停止"}
        return {"ok": False, "message": f"停止失败：{err[:200]}"}

    async def napcat_logs(self, lines: int = 100) -> dict:
        st = self.detect()
        if not st.installed:
            return {"ok": False, "log": "Docker 未安装"}
        code, out, err = await asyncio.to_thread(
            _run, [st.cli_path, "logs", "--tail", str(lines), NAPCAT_CONTAINER], 30)
        return {"ok": code == 0, "log": out or err or "(无日志)"}
