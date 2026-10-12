"""Web 服务 —— 管理界面 + 控制 API + 消息回调。

启动：
    python -m src.server
    或双击 start.command / start.bat

浏览器自动打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config_store import AppConfig, ConfigStore
from .runtime import BotRuntime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wxbot")

DATA_DIR = Path(os.environ.get("WXBOT_DATA_DIR", "./data"))


def _resolve_web_dir() -> Path:
    """定位网页界面目录。

    三种运行环境下的位置完全不同：
      1. 环境变量显式指定（Electron 打包后由 extraResources 提供）
      2. PyInstaller 冻结环境（资源解压在 sys._MEIPASS）
      3. 源码直接运行（就在本文件旁边）
    """
    env = os.environ.get("WXBOT_WEB_DIR")
    if env and Path(env).is_dir():
        return Path(env)

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for cand in (base / "web", base / "src" / "web"):
            if cand.is_dir():
                return cand

    return Path(__file__).parent / "web"


WEB_DIR = _resolve_web_dir()
HOST = os.environ.get("WXBOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("WXBOT_PORT", "8000"))

config_store = ConfigStore(DATA_DIR / "config.json")
runtime = BotRuntime(config_store, DATA_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 60)
    log.info("  微信 AI 助手已启动")
    log.info("  管理界面： http://%s:%s", HOST, PORT)
    log.info("=" * 60)
    # 若上次是运行状态，尝试自动恢复
    # ⚠️ 自动启动失败不能拖垮服务 —— 用户可能还没配完，需要在界面里手动启动
    cfg = config_store.load()
    if cfg.llm.primary_key and (cfg.adapter.wechat_enabled or cfg.adapter.qq_enabled):
        try:
            res = await runtime.start()
            log.info("自动启动机器人：%s", res.get("message"))
        except Exception:
            log.warning("自动启动失败，用户可检查配置后手动启动", exc_info=True)
    yield
    await runtime.stop()


app = FastAPI(title="微信 AI 助手", lifespan=lifespan)


# ═══════════════════════════════════════════════════════════════════
#  页面
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ═══════════════════════════════════════════════════════════════════
#  状态与启停
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/status")
async def api_status() -> dict:
    return runtime.status()


@app.post("/api/bot/start")
async def api_start() -> dict:
    return await runtime.start()


@app.post("/api/bot/stop")
async def api_stop() -> dict:
    return await runtime.stop()


@app.post("/api/bot/restart")
async def api_restart() -> dict:
    return await runtime.restart()


# ═══════════════════════════════════════════════════════════════════
#  QQ / OneBot 11 接入
#
#  两种上报方式都支持，用户按部署习惯选：
#    · 反向 WebSocket：网关连到 ws://<本机>:<端口>/ws/onebot
#    · HTTP POST：网关把事件 POST 到 /onebot/event
# ═══════════════════════════════════════════════════════════════════

@app.websocket("/ws/onebot")
async def ws_onebot(websocket: WebSocket) -> None:
    """OneBot 反向 WebSocket —— 网关主动连过来推送事件。

    ⚠️ 是否接受连接看**配置**，而不是看机器人是否已启动：
    网关通常在本程序刚起来、用户还没点「启动机器人」时就连过来了，
    这时若拒绝连接，网关会不断重连、刷满日志。
    """
    await websocket.accept()

    cfg = config_store.load()
    if not cfg.adapter.qq_enabled:
        await websocket.close(code=1008, reason="当前未启用 QQ 通道")
        return

    # 校验 Access Token
    token = cfg.adapter.onebot_token
    if token:
        auth = websocket.headers.get("authorization", "")
        if auth not in (f"Bearer {token}", token):
            await websocket.close(code=1008, reason="Access Token 不正确")
            return

    log.info("OneBot 反向 WebSocket 已连接")
    warned = False
    try:
        while True:
            raw = await websocket.receive_text()
            adapter = runtime.qq_adapter
            if adapter is None or getattr(adapter, "platform", "") != "onebot":
                if not warned:
                    log.warning("收到 QQ 消息，但机器人尚未启动，消息将被忽略 —— "
                                "请在管理界面点「启动机器人」")
                    warned = True
                continue
            warned = False
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await adapter.handle_event(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("OneBot WebSocket 异常", exc_info=True)
    finally:
        adapter = runtime.qq_adapter
        if adapter is not None and hasattr(adapter, "detach_ws"):
            adapter.detach_ws(websocket)
        log.info("OneBot 反向 WebSocket 已断开")


@app.post("/onebot/event")
async def onebot_event(request: Request) -> Response:
    """OneBot HTTP POST 上报。"""
    cfg = config_store.load()
    if not cfg.adapter.qq_enabled:
        return JSONResponse({"status": "failed", "retcode": 100,
                             "message": "当前未启用 QQ 通道"})

    token = cfg.adapter.onebot_token
    if token:
        auth = request.headers.get("authorization", "")
        if auth not in (f"Bearer {token}", token):
            return JSONResponse({"status": "failed", "retcode": 403,
                                 "message": "Access Token 不正确"})

    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"status": "failed", "retcode": 100, "message": "无效的 JSON"})

    adapter = runtime.qq_adapter
    if adapter is None or getattr(adapter, "platform", "") != "onebot":
        return JSONResponse({"status": "failed", "retcode": 100,
                             "message": "机器人尚未启动，请先在管理界面点「启动机器人」"})

    await adapter.handle_event(event)
    return JSONResponse({"status": "ok", "retcode": 0, "data": None})


# ═══════════════════════════════════════════════════════════════════
#  QQ / OneBot 连接测试
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/qq/test")
async def api_qq_test(request: Request) -> dict:
    b = {}
    try:
        b = await request.json()
    except Exception:
        pass
    return await runtime.test_onebot(
        http_url=b.get("http_url", ""), token=b.get("token", ""),
    )


@app.post("/api/qq/detect")
async def api_qq_detect() -> dict:
    """自动探测本地 OneBot / NapCat 网关。"""
    return await runtime.detect_napcat()


# ═══════════════════════════════════════════════════════════════════
#  QQ / NapCat 一键部署 + 扫码登录
#
#  与 GeWe 一样：点一个按钮，程序拉镜像、灌配置、起容器、出二维码。
#  部署是**非阻塞**的，前端轮询 /api/qq/status 看进度。
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/qq/status")
async def api_qq_status() -> dict:
    """QQ 通道状态：Docker + NapCat 容器 + 登录态。前端轮询它。"""
    return await runtime.qq_status()


@app.post("/api/qq/setup")
async def api_qq_setup() -> dict:
    """一键部署 QQ 网关（NapCat Docker）。"""
    return await runtime.qq_setup()


@app.post("/api/qq/stop")
async def api_qq_stop() -> dict:
    return await runtime.qq_stop()


@app.get("/api/qq/logs")
async def api_qq_logs(lines: int = 100) -> dict:
    return await runtime.qq_logs(lines)


@app.post("/api/qq/login/qrcode")
async def api_qq_login_qrcode() -> dict:
    """获取 QQ 登录二维码（返回 SVG data URL，离线生成）。"""
    return await runtime.qq_login_qrcode()


@app.get("/api/qq/login/check")
async def api_qq_login_check() -> dict:
    return await runtime.qq_login_check()


# ═══════════════════════════════════════════════════════════════════
#  人格知识库
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/memory")
async def api_memory_list(scope: str = "", owner_id: str = "",
                          category: str = "", keyword: str = "") -> dict:
    return runtime.memory_list(scope, owner_id, category, keyword)


@app.get("/api/memory/stats")
async def api_memory_stats() -> dict:
    return runtime.memory_stats()


@app.post("/api/memory")
async def api_memory_add(request: Request) -> dict:
    b = await request.json()
    return runtime.memory_add(
        scope=b.get("scope", "user"), owner_id=str(b.get("owner_id", "")),
        category=b.get("category", "fact"), content=b.get("content", ""),
        keywords=b.get("keywords") or [],
    )


@app.delete("/api/memory/{item_id}")
async def api_memory_delete(item_id: str) -> dict:
    return runtime.memory_delete(item_id)


@app.delete("/api/memory")
async def api_memory_clear(scope: str = "", owner_id: str = "") -> dict:
    return runtime.memory_clear(scope, owner_id)


# ═══════════════════════════════════════════════════════════════════
#  Docker 环境与网关容器（GeWe 恒定走本地 Docker）
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/docker/status")
async def api_docker_status() -> dict:
    """Docker 与网关的完整状态，前端轮询它来展示进度。"""
    return runtime.docker_status()


@app.post("/api/docker/prepare")
async def api_docker_prepare() -> dict:
    """一键准备 Docker：未装则下载安装包，装了没启动则启动。"""
    return await runtime.docker_prepare()


@app.post("/api/docker/start")
async def api_docker_start() -> dict:
    return await runtime.docker_start()


@app.post("/api/gateway/setup")
async def api_gateway_setup() -> dict:
    """拉取并启动网关容器，成功后自动回填网关地址。"""
    r = await runtime.gateway_setup()
    # 容器起来后立刻把地址写进配置，用户不用手填
    asyncio.create_task(_apply_url_when_ready())
    return r


async def _apply_url_when_ready() -> None:
    """等网关端口就绪后回填地址（最多等 3 分钟）。"""
    for _ in range(90):
        await asyncio.sleep(2)
        from .core.docker import port_open
        if port_open(2531):
            runtime.apply_gateway_url()
            return


@app.post("/api/gateway/stop")
async def api_gateway_stop() -> dict:
    return await runtime.gateway_stop()


@app.get("/api/gateway/logs")
async def api_gateway_logs(lines: int = 100) -> dict:
    return await runtime.gateway_logs(lines)


# ═══════════════════════════════════════════════════════════════════
#  网关连接（自动探测 + 连通性测试）
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/gateway/token")
async def api_gateway_token(request: Request) -> dict:
    """向本地网关自动换取 GeWe Token，省去用户去控制台复制。

    ⚠️ 部分网关版本未开放该接口（上游仓库已声明停止提供镜像），
    失败时会返回可操作的提示，前端要照实展示、不能承诺"一定拿得到"。
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return await runtime.fetch_gewe_token(base_url=body.get("base_url", ""))


@app.post("/api/gateway/detect")
async def api_gateway_detect() -> dict:
    """自动探测常见的网关地址，省去用户自己猜地址。"""
    return await runtime.detect_gateway()


@app.post("/api/gateway/test")
async def api_gateway_test(request: Request) -> dict:
    """测试网关连通性与 Token 有效性，失败时给出可操作的提示。"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return await runtime.test_gateway(
        base_url=body.get("base_url", ""),
        token=body.get("token", ""),
        app_id=body.get("app_id", ""),
    )


# ═══════════════════════════════════════════════════════════════════
#  扫码登录
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  WeChatPadPro 微信协议端（群聊通道，接替已停服的 GeWe）
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/wpp/status")
async def api_wpp_status() -> dict:
    """协议端状态：可执行文件、可达性、授权码、登录态。"""
    return await runtime.wpp_status()


@app.post("/api/wpp/authkey")
async def api_wpp_authkey() -> dict:
    """用管理员密钥生成授权码（调用业务接口的前置步骤）。"""
    return await runtime.wpp_generate_auth_key()


@app.post("/api/wpp/login/qrcode")
async def api_wpp_login_qrcode() -> dict:
    return await runtime.wpp_login_qrcode()


@app.get("/api/wpp/login/check")
async def api_wpp_login_check() -> dict:
    return await runtime.wpp_login_check()


@app.get("/api/ilink/status")
async def api_ilink_status() -> dict:
    """官方 iLink 通道是否已有可用凭证（前端用它显示"已登录"徽标）。"""
    return runtime.ilink_status()


@app.post("/api/login/qrcode")
async def api_login_qrcode() -> dict:
    return await runtime.login_qrcode()


@app.get("/api/login/check")
async def api_login_check(captcha: str = "") -> dict:
    return await runtime.login_check(captcha)


# ═══════════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/config")
async def api_get_config() -> dict:
    return config_store.masked(config_store.load())


@app.put("/api/config")
async def api_put_config(request: Request) -> dict:
    """保存配置。

    ⚠️ 密钥字段前端回传的是掩码（••••1234），
    收到掩码时必须保留原值，否则会把用户的 Key 覆盖成掩码串。
    """
    incoming = await request.json()
    old = config_store.load()
    cfg = _merge_config(old, incoming)
    config_store.save(cfg)
    runtime.cfg = cfg
    return {"ok": True, "message": "配置已保存"}


def _merge_config(old: AppConfig, incoming: dict) -> AppConfig:
    cfg = config_store._from_dict(incoming)     # 先按前端值构造

    # 掩码字段 → 沿用旧值
    def keep_masked(new_val: str, old_val: str) -> str:
        return old_val if (not new_val or "•" in new_val) else new_val

    cfg.llm.primary_key = keep_masked(incoming.get("llm", {}).get("primary_key", ""), old.llm.primary_key)
    cfg.llm.fallback_key = keep_masked(incoming.get("llm", {}).get("fallback_key", ""), old.llm.fallback_key)
    cfg.llm.judge_key = keep_masked(incoming.get("llm", {}).get("judge_key", ""), old.llm.judge_key)
    cfg.adapter.gewe_token = keep_masked(
        incoming.get("adapter", {}).get("gewe_token", ""), old.adapter.gewe_token
    )
    cfg.adapter.onebot_token = keep_masked(
        incoming.get("adapter", {}).get("onebot_token", ""), old.adapter.onebot_token
    )
    cfg.adapter.napcat_webui_token = keep_masked(
        incoming.get("adapter", {}).get("napcat_webui_token", ""),
        old.adapter.napcat_webui_token,
    )
    for f in ("wpp_admin_key", "wpp_auth_key"):
        setattr(cfg.adapter, f, keep_masked(
            incoming.get("adapter", {}).get(f, ""), getattr(old.adapter, f, "")))
    # 群配置：合并而非覆盖，避免前端未提交的群被删掉
    for cid, item in old.groups.items():
        cfg.groups.setdefault(cid, item)
    return cfg


# ═══════════════════════════════════════════════════════════════════
#  群管理
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/groups")
async def api_groups() -> dict:
    cfg = config_store.load()
    # 从消息日志里发现"实际见过"的群，方便用户直接开启
    seen: dict[str, str] = {}
    for e in runtime.log.query(limit=2000, chat_type="group"):
        seen.setdefault(e["chat_id"], e.get("chat_name") or "")
    groups = []
    for cid, name in seen.items():
        item = cfg.groups.get(cid)
        groups.append({
            "chat_id": cid,
            "name": name or (item.name if item else "") or cid,
            "configured": item is not None,
            "enabled": item.enabled if item else False,
            "mode": item.mode if item else "smart",
            "wake_words": item.wake_words if item else ["小助手"],
            "cooldown_sec": item.cooldown_sec if item else 60,
            "hourly_limit": item.hourly_limit if item else 8,
            "reply_prob": item.reply_prob if item else 0.7,
            "persona": item.persona if item else "",
        })
    # 已配置但还没收到消息的群也列出来
    for cid, item in cfg.groups.items():
        if cid not in seen:
            groups.append({
                "chat_id": cid, "name": item.name or cid, "configured": True,
                "enabled": item.enabled, "mode": item.mode,
                "wake_words": item.wake_words, "cooldown_sec": item.cooldown_sec,
                "hourly_limit": item.hourly_limit, "reply_prob": item.reply_prob,
                "persona": item.persona,
            })
    return {"groups": groups}


@app.put("/api/groups/{chat_id}")
async def api_put_group(chat_id: str, request: Request) -> dict:
    return runtime.upsert_group(chat_id, await request.json())


@app.delete("/api/groups/{chat_id}")
async def api_delete_group(chat_id: str) -> dict:
    return runtime.remove_group(chat_id)


# ═══════════════════════════════════════════════════════════════════
#  消息日志
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/messages")
async def api_messages(limit: int = 100, chat_id: str = "",
                       chat_type: str = "", direction: str = "") -> dict:
    return {"messages": runtime.log.query(limit, chat_id, chat_type, direction)}


@app.delete("/api/messages")
async def api_clear_messages() -> dict:
    runtime.log.clear()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
#  协议网关回调
# ═══════════════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """接收 GeWe 网关推送的消息。

    必须快速返回 —— 网关有重试机制，慢响应会触发重复推送。
    """
    if not runtime.wechat_adapter:
        return JSONResponse({"ret": 200, "msg": "bot not running"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ret": 200, "msg": "bad payload"})

    handler = getattr(runtime.wechat_adapter, "handle_webhook", None)
    if handler is None:
        return JSONResponse({"ret": 200, "msg": "adapter has no webhook"})
    try:
        result = await handler(payload)
        return JSONResponse(result or {"ret": 200})
    except Exception:
        log.exception("webhook 处理异常")
        return JSONResponse({"ret": 200, "msg": "error logged"})


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "running": runtime.running}


# ═══════════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════════

def _pick_port(preferred: int) -> int:
    """端口被占用时自动顺延，避免用户遇到"启动失败"而不知所措。"""
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, p)) != 0:
                return p
    return preferred


def main() -> None:
    import uvicorn

    port = _pick_port(PORT)
    url = f"http://{HOST}:{port}"
    if os.environ.get("WXBOT_NO_BROWSER") == "1":
        # 由 Electron 等外壳托管，不主动开浏览器
        log.info("服务已就绪：%s", url)
    else:
        log.info("打开浏览器访问：%s", url)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(app, host=HOST, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
