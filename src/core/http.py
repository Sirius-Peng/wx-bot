"""统一 HTTP 客户端工厂 —— 处理代理、超时与错误翻译。

═══════════════════════════════════════════════════════════════════
为什么需要这一层？

httpx 默认 trust_env=True，会读取 HTTP_PROXY / HTTPS_PROXY 环境变量，
并且**对 127.0.0.1 这样的本机地址也走代理**。

后果：用户机器上只要装了代理软件（Clash / Surge / 公司网络），
访问本机 GeWe 网关（http://127.0.0.1:2531）的请求就会先发给代理，
代理连不上后端便返回 502 Bad Gateway —— 用户看到"获取二维码失败：HTTP 502"，
完全不知道问题出在哪。

所以：**内网/本机地址必须绕过代理，公网地址才走代理。**
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import threading
import certifi
from urllib.parse import urlparse

import httpx

# 内网主机名后缀
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home")

# 额外需要绕过代理的主机名（来自 NO_PROXY 环境变量）
def _no_proxy_hosts() -> set[str]:
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def is_local_host(host: str) -> bool:
    """判断主机名是否属于本机 / 内网，这类地址不应走代理。"""
    if not host:
        return False

    h = host.strip().lower().strip("[]")   # 去掉 IPv6 方括号

    # 显式列在 NO_PROXY 里的，按用户意愿绕过
    if h in _no_proxy_hosts():
        return True

    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True

    if h.endswith(_LOCAL_SUFFIXES):
        return True

    # 私有 IP 段（RFC1918 + 链路本地）
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass

    # 主机名不含点（如 "gateway"）通常是内网短名
    if "." not in h:
        return True

    return False


def should_bypass_proxy(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return is_local_host(host)


# Cache only immutable TLS configuration, never sockets/clients or event-loop state.
# Re-reading the CA bundle on each local gateway call costs hundreds of ms on Windows.
_TLS_CONTEXTS = {}
_TLS_LOCK = threading.Lock()

def _ca_revision(path):
    try:
        info = os.stat(path)
        return (info.st_size, info.st_mtime_ns)
    except OSError:
        return None


def verified_context(trust_env=True):
    cafile = os.environ.get('SSL_CERT_FILE') if trust_env else None
    capath = os.environ.get('SSL_CERT_DIR') if trust_env and not cafile else None
    if not cafile and not capath:
        cafile = certifi.where()
    key = (cafile, capath, _ca_revision(cafile or capath), os.environ.get('SSLKEYLOGFILE'))
    with _TLS_LOCK:
        if key not in _TLS_CONTEXTS:
            context = ssl.create_default_context(cafile=cafile, capath=capath)
            # Never downgrade certificate/hostname validation for a speed improvement.
            if len(_TLS_CONTEXTS) >= 8:
                _TLS_CONTEXTS.clear()
            _TLS_CONTEXTS[key] = context
        return _TLS_CONTEXTS[key]


def make_client(url: str = "", timeout: float = 20.0) -> httpx.AsyncClient:
    """创建 HTTP 客户端。

    @param url 目标地址。用于判断是否需要绕过代理。
               留空则按"尊重系统代理"处理。
    """
    bypass = should_bypass_proxy(url) if url else False
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        verify=verified_context(trust_env=not bypass),
        trust_env=not bypass,      # 关键：内网地址彻底忽略代理环境变量
        follow_redirects=True,
    )


# ═══════════════════════════════════════════════════════════════════
#  错误翻译 —— 把技术异常转成用户能看懂的话
# ═══════════════════════════════════════════════════════════════════

def explain_error(err: Exception, url: str = "") -> str:
    """把 httpx / 系统异常翻译成可操作的中文提示。"""
    target = _friendly_target(url)

    if isinstance(err, httpx.ConnectTimeout):
        return f"连接 {target} 超时。请确认该服务已启动、地址与端口填写正确。"

    if isinstance(err, httpx.ReadTimeout):
        return f"{target} 响应超时。服务可能正在忙，请稍后重试。"

    if isinstance(err, httpx.ConnectError):
        msg = str(err).lower()
        if "connection refused" in msg or "connect call failed" in msg:
            return (
                f"无法连接 {target}（连接被拒绝）。\n"
                "通常说明该地址上没有服务在运行 —— 请确认网关已启动，"
                "或地址 / 端口填写是否正确。"
            )
        if "name or service not known" in msg or "nodename nor servname" in msg:
            return f"无法解析域名 {target}。请检查地址是否拼写正确、网络是否正常。"
        if "certificate" in msg or "ssl" in msg:
            return f"与 {target} 的安全连接失败（证书问题）。若为内网服务，请改用 http:// 而非 https://。"
        return f"无法连接 {target}。请检查地址与网络设置。"

    if isinstance(err, httpx.HTTPStatusError):
        code = err.response.status_code
        if code == 401:
            return "鉴权失败（401）。Token 不正确或已失效，请重新获取。"
        if code == 403:
            return "访问被拒绝（403）。请确认该 Token 有对应权限。"
        if code == 404:
            return f"接口不存在（404）。{target} 可能不是有效的网关地址。"
        if code == 502:
            return (
                f"网关返回 502（{target}）。\n"
                "如果你本机装了代理软件，请把该地址加入代理绕过列表 —— "
                "本机地址不应经过代理。"
            )
        if code >= 500:
            return f"{target} 服务端错误（{code}）。请稍后重试或检查网关状态。"
        return f"{target} 返回异常状态码 {code}。"

    if isinstance(err, socket.gaierror):
        return f"无法解析地址 {target}。"

    # 兜底：保留原始信息，但明确标出是哪个地址出的问题
    return f"{target} 请求失败：{err}"


def _friendly_target(url: str) -> str:
    """把 URL 缩成用户看得懂的形式（只保留 主机:端口）。"""
    if not url:
        return "目标服务"
    try:
        p = urlparse(url)
        if p.port:
            return f"{p.hostname}:{p.port}"
        return p.hostname or url
    except Exception:
        return url


def classify_status(status: int) -> str:
    """给状态码一个简短分类，用于前端展示。"""
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status == 502:
        return "proxy"
    if status >= 500:
        return "server"
    return "unknown"
