"""Pinned, offline NapCat Node runtime, owned by the backend (never Docker).

The Windows release archive includes its own Node and QQ native libraries.
Only immutable runtime files are replaced; work/profile contain login state.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ntpath
import os
import platform
import re
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from .napcat import NapCatWebUI, merge_managed_onebot
from .native_process import OwnedProcess
from .fileio import read_snapshot

VERSION = '4.18.41'
ARCHIVE = 'NapCat.Shell.Windows.Node.zip'
ARCHIVE_SIZE = 124089498
ARCHIVE_SHA256 = '51fbb10e7bcd56b0c2a6de86c37811e7518330e48a137ac689ab1e4d8627d3f0'
ARCHIVE_URL = f'https://github.com/NapNeko/NapCatQQ/releases/download/v{VERSION}/{ARCHIVE}'
HTTP_PORT = 3000
WEBUI_PORT = 6099
REQUIRED_FILES = {'node.exe': {'bytes': 80511640, 'sha256': '7447c4ece014aa41fb2ff866c993c708e5a8213a00913cc2ac5049ea3ffc230d'}, 'index.js': {'bytes': 639, 'sha256': '5de31bef6cc1b48cef0229a8b22b32b3211e41253c3e75a9547e996becf9b388'}, 'wrapper.node': {'bytes': 104663600, 'sha256': 'a1e59891e743c271d641ee011f47aa887d9f7dfae6b3bb9292a03af759dec203'}, 'QQNT.dll': {'bytes': 481280, 'sha256': '2f9ae01c30fa624535439cab9ccc2ba56d95e5f2ec85e6182789cb3c9089f3b7'}, 'package.json': {'bytes': 486, 'sha256': '56387b6ec1b4d129d3790c09fb5c5949a6e92f01cae162d7def4a49d9ed402e3'}, 'config.json': {'bytes': 159, 'sha256': '0b4e7719005a7065be522c943b351a20f5d30182f719f7e2db9925d5a71273e8'}, 'napcat/napcat.mjs': {'bytes': 3134858, 'sha256': '7c66d0239d7effe73ab6e4920eddef732a8066fb47cd19539541c72055c5f0aa'}}
REQUIRED = tuple(REQUIRED_FILES)
FILES_MANIFEST_SHA256 = '2fbab136ceb8095aa4df92c22c59a07d4861722b7ddbfeac700497b8e8861ef0'


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('网关数据目录含符号链接，请选择独立目录')
    fd, name = tempfile.mkstemp(prefix='.wxbot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def bundle_directory():
    override = os.environ.get('WXBOT_NAPCAT_BUNDLE')
    if override:
        return Path(override)
    if getattr(sys, 'frozen', False):
        # Windows onedir: Resources/backend/wxbot-server/wxbot-server.exe
        return Path(sys.executable).resolve().parent.parent.parent / 'napcat'
    return Path(__file__).resolve().parents[2] / 'desktop/resources/napcat'


def native_config(webui_token, ws_url, access_token, http_port=HTTP_PORT, webui_port=WEBUI_PORT):
    """Full upstream schema; loopback and separate random tokens by default."""
    webui = {'host': '127.0.0.1', 'prefix': '', 'port': webui_port,
             'token': webui_token, 'loginRate': 3}
    onebot = {
        'network': {
            'httpServers': [{'name': 'http-server', 'enable': True, 'port': http_port,
                             'host': '127.0.0.1', 'enableCors': False,
                             'messagePostFormat': 'array', 'token': access_token, 'debug': False}],
            'httpSseServers': [], 'httpClients': [], 'websocketServers': [],
            'websocketClients': [{'name': 'wxbot-reverse-ws', 'enable': True, 'url': ws_url,
                'reportSelfMessage': False, 'messagePostFormat': 'array', 'token': access_token,
                'debug': False, 'heartInterval': 30000, 'reconnectInterval': 5000}],
            'plugins': []},
        'musicSignUrl': '', 'enableLocalFile2Url': False, 'parseMultMsg': False,
        'imageDownloadProxy': '', 'timeout': {'baseTimeout': 10000, 'uploadSpeedKBps': 256,
            'downloadSpeedKBps': 256, 'maxTimeout': 1800000}}
    return webui, onebot


def extract_runtime(archive: Path, destination: Path, cancelled: threading.Event):
    """Verify before execution, reject unsafe zip entries, atomically install."""
    if archive.is_symlink() or not archive.is_file():
        raise ValueError('安装包缺少内置 QQ 网关，请重新安装完整版本')
    if archive.stat().st_size != ARCHIVE_SIZE:
        raise ValueError('内置 QQ 网关大小校验失败')
    with archive.open('rb') as file:
        if hashlib.file_digest(file, 'sha256').hexdigest() != ARCHIVE_SHA256:
            raise ValueError('内置 QQ 网关 SHA256 校验失败')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError('网关运行目录含符号链接')
    temporary = Path(tempfile.mkdtemp(prefix='.napcat-install-', dir=destination.parent))
    try:
        seen = set(); total = 0; files = {}
        with zipfile.ZipFile(archive) as source:
            if len(source.infolist()) > 15000:
                raise ValueError('网关归档文件数量异常')
            for item in source.infolist():
                if cancelled.is_set():
                    raise InterruptedError('网关安装已取消')
                name = item.filename.replace('\\', '/')
                rel = Path(name)
                mode = item.external_attr >> 16
                if (not name or rel.is_absolute() or '..' in rel.parts or ntpath.splitdrive(name)[0]
                        or ':' in name or stat.S_ISLNK(mode) or name.casefold() in seen):
                    raise ValueError('网关归档路径无效')
                seen.add(name.casefold()); total += item.file_size
                if total > 1024 * 1024 * 1024:
                    raise ValueError('网关归档解压体积异常')
                target = temporary / rel
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True); continue
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with source.open(item) as inp, target.open('wb') as out:
                    while chunk := inp.read(256 * 1024):
                        if cancelled.is_set(): raise InterruptedError('网关安装已取消')
                        out.write(chunk); digest.update(chunk)
                files[name] = {'size': item.file_size, 'sha256': digest.hexdigest()}
        if not all((temporary / name).is_file() for name in REQUIRED):
            raise ValueError('网关归档缺少运行文件')
        atomic_json(temporary / '.wxbot-runtime.json', {'version': VERSION,
            'archive_sha256': ARCHIVE_SHA256, 'files': files})
        if cancelled.is_set(): raise InterruptedError('网关安装已取消')
        # An unrecognised directory is user data, never delete/overwrite it.
        if destination.exists():
            raise ValueError('网关版本目录已存在但未通过校验，请备份后检查该目录')
        os.replace(temporary, destination)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)


class NativeNapCatManager:
    def __init__(self, data_dir: Path, *, bundle_dir=None, process=None, platform_name=None):
        self.root = Path(data_dir).resolve() / 'napcat'
        self.runtime = self.root / 'runtime' / ('v' + VERSION)
        self.work = self.root / 'work'
        self.profile = self.root / 'profile'
        self.bundle = Path(bundle_dir) if bundle_dir is not None else bundle_directory()
        self.process = process if process is not None else OwnedProcess(output_filter=self._redact)
        self.platform_name = platform_name or sys.platform
        self.supported = self.platform_name.startswith('win') and platform.machine().lower() in ('amd64', 'x86_64')
        self.task = {'running': False, 'stage': '', 'progress': 0, 'message': '', 'error': '', 'needsUser': False}
        self._job = None; self._monitor = None; self._closed = False
        self._cancelled = threading.Event(); self._lock = asyncio.Lock()
        self._tokens = (); self._on_ports = None
        self.state = self._load_state()
        self.http_port = self.state.get('http_port', HTTP_PORT)
        self.webui_port = self.state.get('webui_port', WEBUI_PORT)

    def _load_state(self):
        try:
            data = json.loads((self.root / 'manager.json').read_text(encoding='utf-8'))
            if not isinstance(data, dict): return {}
            return {**{k: v for k, v in data.items() if k == 'autostart' and isinstance(v, bool)},
                    **{k: v for k, v in data.items() if k in ('http_port', 'webui_port')
                       and type(v) is int and 0 < v < 65536}}
        except (OSError, ValueError): return {}

    def _save(self, **values):
        self.state.update(values)
        atomic_json(self.root / 'manager.json', self.state)

    @property
    def autostart(self): return self.state.get('autostart') is True

    def installed(self):
        try:
            manifest = json.loads((self.runtime / '.wxbot-runtime.json').read_text(encoding='utf-8'))
            return (not self.runtime.is_symlink() and manifest.get('archive_sha256') == ARCHIVE_SHA256
                    and all((self.runtime / file).is_file() and not (self.runtime / file).is_symlink()
                            and (self.runtime / file).stat().st_size == expected['bytes']
                            for file, expected in REQUIRED_FILES.items()))
        except (KeyError, OSError, ValueError, TypeError): return False

    def _verified_install(self):
        if not self.installed(): return False
        try:
            files=json.loads((self.runtime/'.wxbot-runtime.json').read_text(encoding='utf-8'))['files']
            digest=hashlib.sha256(json.dumps(files,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if digest!=FILES_MANIFEST_SHA256:return False
            actual={}
            for path in self.runtime.rglob('*'):
                if path.is_symlink():return False
                if path.is_file():
                    name=path.relative_to(self.runtime).as_posix()
                    # QQ's native SDK writes this exact non-executable diagnostic
                    # next to wrapper.node even when cwd is the separate work dir.
                    if name not in ('.wxbot-runtime.json','beacon_report.log'):actual[name]=path
            if set(actual)!=set(files):return False
            for name, expected in files.items():
                file=actual[name]
                if file.stat().st_size!=expected['size']:return False
                with file.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=expected['sha256']:return False
            return True
        except (OSError,ValueError,KeyError,TypeError):return False

    def status(self, mode='native'):
        running = self.process.running
        return {'mode': mode, 'supported': self.supported, 'installed': self.installed(),
            'running': running, 'processRunning': running, 'owned': running, 'needsQQ': False,
            'platform': self.platform_name, 'version': VERSION, 'requiresDocker': False,
            'httpPort': self.http_port, 'webuiPort': self.webui_port,
            'message': ('使用用户配置的外部 OneBot 网关' if mode == 'external' else
                        '内置网关正在运行' if running else '内置网关仅支持 Windows x64；此平台可连接外部 OneBot' if not self.supported else
                        '内置 QQ 网关，无需安装 QQ、Node 或 Docker')}

    @staticmethod
    def _free_port(preferred, excluded=()):
        with socket.socket() as sock:
            try:
                if preferred in excluded: raise OSError('duplicate')
                sock.bind(('127.0.0.1', preferred))
            except OSError:
                sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def _write_config(self, webui_token, ws_url, access_token):
        config = self.work / 'config'; config.mkdir(parents=True, exist_ok=True)
        webui, template = native_config(webui_token, ws_url, access_token, self.http_port, self.webui_port)
        web_path = config / 'webui.json'
        if web_path.exists():
            previous = json.loads(web_path.read_text(encoding='utf-8'))
            if not isinstance(previous, dict): raise ValueError('WebUI 配置格式错误')
            webui = {**previous, **webui}
        targets = [config / 'onebot11.json', *config.glob('onebot11_*.json')]
        # Validate all inputs before writing anything. Preserve third-party networks.
        prepared = []
        for file in targets:
            before = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
            if not isinstance(before, dict): raise ValueError('OneBot 配置格式错误')
            prepared.append((file, merge_managed_onebot(before, template) if before else template))
        atomic_json(web_path, webui)
        for file, value in prepared: atomic_json(file, value)

    def _environment(self):
        profile = self.profile.resolve()
        roaming = profile / 'AppData' / 'Roaming'; local = profile / 'AppData' / 'Local'
        for folder in (profile / 'Documents', roaming, local, self.work): folder.mkdir(parents=True, exist_ok=True)
        env = {**{k:v for k,v in os.environ.items() if not k.upper().startswith('NAPCAT_')}, 'USERPROFILE': str(profile), 'HOME': str(profile),
            'HOMEDRIVE': ntpath.splitdrive(str(profile))[0],
            'HOMEPATH': ntpath.splitdrive(str(profile))[1],
            'APPDATA': str(roaming), 'LOCALAPPDATA': str(local),
            'NAPCAT_WORKDIR': str(self.work.resolve()), 'NAPCAT_DISABLE_MULTI_PROCESS': '1',
            'NAPCAT_DISABLE_PIPE': '1', 'NAPCAT_DISABLE_UPDATE': '1'}
        # Do not inherit arbitrary Node preload/debug hooks into the signed runtime.
        for key in ('NODE_OPTIONS', 'NODE_PATH', 'ELECTRON_RUN_AS_NODE'): env.pop(key, None)
        return env

    async def ensure(self, webui_token, ws_url, access_token='', on_ports=None):
        if not self.supported:
            return {'ok': False, 'stage': 'unsupported', 'message': self.status()['message']}
        async with self._lock:
            self._closed = False
            if self._job and not self._job.done():
                return {'ok': True, 'pending': True, 'message': '本机网关正在准备，请稍候'}
            if self.process.running:
                return {'ok': True, 'pending': False, 'message': '本机网关已在运行'}
            self._tokens = (webui_token, access_token); self._on_ports = on_ports
            self._cancelled = threading.Event()
            self.task.update(running=True, stage='extract', progress=5,
                             message='正在校验并准备内置 QQ 网关', error='', needsUser=False)
            self._job = asyncio.create_task(self._start(webui_token, ws_url, access_token))
            return {'ok': True, 'pending': True, 'message': '正在准备并启动本机 QQ 网关'}

    async def _start(self, webui_token, ws_url, access_token):
        try:
            for folder in (self.root, self.root/'runtime', self.work, self.work/'config', self.profile, self.root/'logs'):
                if folder.is_symlink(): raise ValueError('网关数据目录含符号链接')
            if not await asyncio.to_thread(self._verified_install):
                await asyncio.to_thread(extract_runtime, self.bundle / ARCHIVE, self.runtime, self._cancelled)
            if self._cancelled.is_set(): return
            # Never interpret another process on the default port as our gateway.
            self.webui_port = self._free_port(self.webui_port)
            self.http_port = self._free_port(self.http_port, (self.webui_port,))
            self._save(http_port=self.http_port, webui_port=self.webui_port)
            self._write_config(webui_token, ws_url, access_token)
            if self._on_ports: self._on_ports(self.http_port, self.webui_port)
            self.task.update(stage='starting', progress=70, message='内置网关正在启动，等待 WebUI 就绪')
            self._prepare_log()
            await self.process.start([str(self.runtime / 'node.exe'), str(self.runtime / 'index.js')],
                self.work, self._environment(), self.root / 'logs' / 'process.log')
            client = NapCatWebUI(webui_token, port=self.webui_port)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if self._cancelled.is_set(): return
                if not self.process.running: raise RuntimeError('本机网关提前退出，请查看网关日志')
                try: result = await asyncio.wait_for(client.login(), timeout=2)
                except TimeoutError: result = {}
                if result.get('ok'):
                    self._save(autostart=True)
                    self.task.update(running=False, stage='waiting_login', progress=100, message='网关已就绪，可快速登录或扫码')
                    self._monitor = asyncio.create_task(self._watch())
                    return
                await asyncio.sleep(.4)
            raise TimeoutError('本机网关启动超时，请查看网关日志后重试')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.process.stop()
            self.task.update(running=False, stage='error', error=self._redact(str(error)),
                             message=self._redact(str(error)))

    def _prepare_log(self):
        path=self.root/'logs'/'process.log'
        try:
            if path.stat().st_size>=7*1024*1024:
                tail,_=read_snapshot(path,256*1024)
                with path.open('wb') as stream:stream.write(tail)
        except FileNotFoundError:pass

    async def _watch(self):
        while not self._closed and self.process.running:
            await asyncio.sleep(2)
        if not self._closed and not self.process.running:
            self.task.update(running=False, stage='error', message='本机网关已退出，点击启动可重试', error='gateway_exited')

    async def sync_config(self, webui_token, ws_url, access_token):
        self._tokens=(webui_token,access_token)
        if not self.installed(): return {'ok': True, 'changed': False, 'message': '网关未安装，将在首次启动时写入地址'}
        self._write_config(webui_token, ws_url, access_token)
        if self.process.running:
            template = native_config(webui_token, ws_url, access_token, self.http_port, self.webui_port)[1]
            result = await NapCatWebUI(webui_token, port=self.webui_port).sync_onebot(template)
            if result.get('ok'): return result
            return {'ok': True, 'changed': True, 'pending': True, 'message': '地址已保存，网关登录或下次启动时生效'}
        return {'ok': True, 'changed': True, 'message': '本机网关地址已同步'}

    def _redact(self, text):
        for token in self._tokens:
            if token: text = text.replace(token, '[已隐藏]')
        text = re.sub(r'https?://\S+', '[网关链接已隐藏]', text)
        return re.sub(r'(?i)(credential|token|authorization)([\s"\x27:=]+)[^\s,}]+', r'\1\2[已隐藏]', text)

    async def logs(self, lines=100):
        try:
            data, _ = await asyncio.to_thread(read_snapshot, self.root / 'logs' / 'process.log', 512 * 1024)
        except FileNotFoundError: return {'ok': True, 'log': '本机网关尚无日志'}
        except OSError: return {'ok':False,'log':'网关日志暂时不可读，请检查本地目录权限'}
        text = data.decode('utf-8', errors='replace')
        return {'ok': True, 'log': self._redact('\n'.join(text.splitlines()[-max(1, min(2000, int(lines))):]))}

    def _collect_native_log(self):
        source=self.runtime/'beacon_report.log'
        if source.is_symlink() or not source.is_file():return
        target=self.work/'native-beacon-report.log'
        try:
            if target.is_symlink():return
            self.work.mkdir(parents=True,exist_ok=True)
            old,_=read_snapshot(target,256*1024) if target.exists() else (b'',0)
            tail,_=read_snapshot(source,256*1024)
            # This is an upstream diagnostic, not an executable/cache manifest.
            with target.open('wb') as stream:stream.write(old+tail)
            source.unlink()
        except OSError:pass  # Preserve the original diagnostic on a write failure.

    async def stop(self, *, remember=True):
        async with self._lock:
            self._closed = True; self._cancelled.set()
            if self._job and not self._job.done():
                self._job.cancel(); await asyncio.gather(self._job, return_exceptions=True)
            if self._monitor:
                self._monitor.cancel(); await asyncio.gather(self._monitor, return_exceptions=True)
            await self.process.stop()
            self._collect_native_log()
            if remember and self.root.exists(): self._save(autostart=False)
            self.task.update(running=False, stage='stopped', progress=0, message='本机网关已停止，登录数据仍保留', error='')
            return {'ok': True, 'message': self.task['message']}

    async def close(self):
        return await self.stop(remember=False)
