"""Web 服务 —— 管理界面 + 控制 API + 消息回调。

启动：
    python -m src.server
    或双击 start.command / start.bat

浏览器自动打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
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
    if config_store.load().llm.primary_key:
        res = await runtime.start()
        log.info("自动启动机器人：%s", res.get("message"))
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
#  扫码登录
# ═══════════════════════════════════════════════════════════════════

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
    if not runtime.adapter:
        return JSONResponse({"ret": 200, "msg": "bot not running"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ret": 200, "msg": "bad payload"})

    handler = getattr(runtime.adapter, "handle_webhook", None)
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
