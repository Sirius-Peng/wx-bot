"""Web 服务 —— 管理界面 + 控制 API + 消息回调。

启动：
    python -m src.server
    或双击 start.command / start.bat

浏览器自动打开 http://127.0.0.1:8000

两条通道：
  · 微信：腾讯 iLink Bot API（本项目仅私聊；可用性依平台与账号状态）
          —— 消息靠后端长轮询拉取，不需要任何回调接口
  · QQ ：OneBot 11（NapCat 等），支持反向 WebSocket 与 HTTP POST 两种上报
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core.admin_auth import AdminAuth
from .core.personality import public_profiles
from .core.onboarding import OnboardingStore, model_fingerprint, test_primary_model
from .config_store import AppConfig, ConfigStore
from .runtime import BotRuntime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wxbot")

from .core.storage import resolve_data_dir, initialize_storage, create_backup

DATA_DIR = resolve_data_dir()
STORAGE_REPORT = initialize_storage(DATA_DIR)


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
admin_auth = AdminAuth(DATA_DIR, HOST)
onboarding_store = OnboardingStore(DATA_DIR / "onboarding.json")
_model_test_lock = asyncio.Lock()
_backup_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 60)
    log.info("  微信 AI 助手已启动")
    log.info("  管理界面： http://%s:%s", HOST, PORT)
    log.info("=" * 60)
    # 若上次是运行状态，尝试自动恢复
    # ⚠️ 自动启动失败不能拖垮服务 —— 用户可能还没配完，需要在界面里手动启动
    cfg = config_store.load()
    if cfg.adapter.qq_enabled and cfg.adapter.onebot_token:
        await runtime.ensure_onebot_ingress()
    setup_completed = onboarding_store.load(cfg)['completed']
    if setup_completed and cfg.llm.primary_key and cfg.bot.memory_enabled:
        runtime.start_learning()
    if setup_completed and cfg.llm.primary_key and (cfg.adapter.wechat_enabled or cfg.adapter.qq_enabled):
        try:
            res = await runtime.start()
            log.info("自动启动机器人：%s", res.get("message"))
        except Exception:
            log.warning("自动启动失败，用户可检查配置后手动启动", exc_info=True)
    yield
    await runtime.stop()
    await runtime.close_onebot_ingress()


app = FastAPI(title="微信 AI 助手", lifespan=lifespan)


@app.middleware("http")
async def protect_management(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        if not admin_auth.same_origin(request):
            return JSONResponse({"ok": False, "message": "请求来源不匹配"}, status_code=403)
        if request.url.path != "/api/auth/login" and not admin_auth.authenticated(request):
            return JSONResponse({"ok": False, "message": "请先登录管理界面"}, status_code=401)
    return await call_next(request)


@app.post("/api/auth/login")
async def api_admin_login(request: Request) -> Response:
    try:
        body = await request.json()
        token = body.get("token", "")
    except (ValueError, AttributeError):
        token = ""
    if not isinstance(token, str) or not admin_auth.matches(token):
        return JSONResponse({"ok": False, "message": "管理 Token 不正确"}, status_code=401)
    response = JSONResponse({"ok": True})
    admin_auth.set_cookie(request, response)
    return response


# ═══════════════════════════════════════════════════════════════════
#  页面
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
async def index(request: Request) -> FileResponse:
    response = FileResponse(WEB_DIR / "index.html")
    if admin_auth.can_bootstrap(request):
        admin_auth.set_cookie(request, response)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


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
    # 让适配器记住这条连接：界面上要显示"网关有没有把消息推过来"
    runtime.attach_onebot_ws(websocket)
    warned = False
    try:
        while True:
            raw = await websocket.receive_text()
            try:event=json.loads(raw)
            except json.JSONDecodeError:continue
            if not isinstance(event,dict):continue
            runtime.note_onebot_event('ws')
            adapter = runtime.qq_adapter
            if adapter is not None and hasattr(adapter,'attach_ws') and getattr(adapter,'_ws',None) is not websocket:adapter.attach_ws(websocket)
            if adapter is None or getattr(adapter, "platform", "") != "onebot":
                if not warned:
                    log.warning("收到 QQ 消息，但机器人尚未启动，消息将被忽略 —— "
                                "请在管理界面点「启动机器人」")
                    warned = True
                continue
            warned = False
            await adapter.handle_event(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("OneBot WebSocket 异常", exc_info=True)
    finally:
        runtime.detach_onebot_ws(websocket)
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

    if not isinstance(event,dict):return JSONResponse({'status':'failed','retcode':100})
    runtime.note_onebot_event('http')
    adapter = runtime.qq_adapter
    if adapter is None or getattr(adapter, "platform", "") != "onebot":
        return JSONResponse({"status": "failed", "retcode": 100,
                             "message": "机器人尚未启动，请先在管理界面点「启动机器人」"})

    await adapter.handle_event(event)
    return JSONResponse({"status": "ok", "retcode": 0, "data": None})


# ═══════════════════════════════════════════════════════════════════
#  QQ / OneBot 连接测试与探测
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
#  点一个按钮，程序拉镜像、灌配置、起容器、出二维码。
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


@app.post("/api/qq/sync-ws")
async def api_qq_sync_ws() -> dict:
    """让 QQ 网关重新连到当前端口（端口变了/收不到消息时点一下）。"""
    return await runtime.qq_sync_ws_url()


@app.post("/api/qq/stop")
async def api_qq_stop() -> dict:
    return await runtime.qq_stop()


@app.get('/api/qq/login/accounts')
async def api_qq_accounts():return await runtime.qq_quick_accounts()

@app.post('/api/qq/login/quick')
async def api_qq_quick(request:Request):
    body=await request.json()
    if not isinstance(body,dict):raise HTTPException(400,'登录参数格式错误')
    return await runtime.qq_quick_login(str(body.get('qq') or ''),body.get('remember') is True)

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
#  微信官方 iLink 通道
#
#  消息由后端长轮询拉取（见 runtime._start_ilink_poller），
#  这里只负责登录与登录态查询。
# ═══════════════════════════════════════════════════════════════════

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
#  人格知识库
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/memory")
async def api_memory_list(scope: str = "", owner_id: str = "",
                          category: str = "", keyword: str = "", include_history: bool = False) -> dict:
    return runtime.memory_list(scope, owner_id, category, keyword, include_history)


@app.get("/api/memory/stats")
async def api_memory_stats() -> dict:
    return runtime.memory_stats()


@app.post("/api/memory/learning/retry")
async def api_memory_retry() -> dict:
    return runtime.memory_retry_learning()


@app.post("/api/memory/identities")
async def api_memory_link(request: Request) -> dict:
    body = await request.json()
    try:
        identity = runtime._ensure_memory().link_accounts(
            str(body.get('wechat_id', '')), str(body.get('qq_id', '')), str(body.get('label', '')))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {'ok': True, 'identity': identity}


@app.delete("/api/memory/identities/{identity_id}")
async def api_memory_unlink(identity_id: str) -> dict:
    return {'ok': runtime._ensure_memory().unlink_accounts(identity_id)}


@app.post("/api/memory")
async def api_memory_add(request: Request) -> dict:
    b = await request.json()
    return runtime.memory_add(
        scope=b.get("scope", "user"), owner_id=str(b.get("owner_id", "")),
        category=b.get("category", "fact"), content=b.get("content", ""),
        keywords=b.get("keywords") or [], platform=b.get("platform", ""),
    )


@app.delete("/api/memory/{item_id}")
async def api_memory_delete(item_id: str) -> dict:
    return runtime.memory_delete(item_id)


@app.delete("/api/memory")
async def api_memory_clear(scope: str = "", owner_id: str = "") -> dict:
    return runtime.memory_clear(scope, owner_id)


# ═══════════════════════════════════════════════════════════════════
#  Docker 环境（QQ 网关一键部署要用）
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/docker/status")
async def api_docker_status() -> dict:
    """Docker 环境状态，前端轮询它来展示安装进度。"""
    return runtime.docker_status()


@app.post("/api/docker/prepare")
async def api_docker_prepare() -> dict:
    """一键准备 Docker：未装则下载安装包，装了没启动则启动。"""
    return await runtime.docker_prepare()


# ═══════════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/persona/profiles")
async def api_persona_profiles() -> dict:
    return {"profiles": public_profiles()}


@app.get("/api/config")
async def api_get_config() -> dict:
    return config_store.masked(config_store.load())


@app.get('/api/onboarding')
async def api_onboarding():
    cfg = config_store.load()
    return onboarding_store.public(cfg, runtime.ilink_status().get('loggedIn', False))


@app.patch('/api/onboarding')
async def api_onboarding_progress(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or set(body) - {'step', 'dismissed'}:
        raise HTTPException(400, '引导进度格式错误')
    if 'step' in body and (type(body['step']) is not int or not 1 <= body['step'] <= 5):
        raise HTTPException(400, '引导步骤需要为1到5')
    if 'dismissed' in body and type(body['dismissed']) is not bool:
        raise HTTPException(400, '引导显示设置格式错误')
    cfg = config_store.load(); state = onboarding_store.load(cfg)
    state.update(body); onboarding_store.save(state)
    return onboarding_store.public(cfg, runtime.ilink_status().get('loggedIn', False))


@app.post('/api/llm/test')
async def api_test_primary_model():
    if _model_test_lock.locked():
        return {'ok': False, 'code': 'busy', 'message': '模型连接测试正在进行，请稍候'}
    async with _model_test_lock:
        cfg = config_store.load()
        result = await test_primary_model(cfg)
        # Persist only a fingerprint, not the key or model-generated test output.
        latest = config_store.load(); state = onboarding_store.load(latest)
        state['model_receipt'] = model_fingerprint(cfg.llm) if result.get('ok') else ''
        onboarding_store.save(state)
        if result.get('ok') and model_fingerprint(latest.llm) != state['model_receipt']:
            return {'ok': False, 'code': 'changed', 'message': '模型设置已变化，请对当前配置重新测试'}
        return result


@app.post('/api/onboarding/complete')
async def api_onboarding_complete(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or set(body) - {'start'} or type(body.get('start', True)) is not bool:
        raise HTTPException(400, '完成引导的参数格式错误')
    cfg = config_store.load(); state = onboarding_store.load(cfg); missing = []
    if not cfg.llm.primary_key:
        missing.append('填写并保存 API Key')
    if not state['model_receipt'] or state['model_receipt'] != model_fingerprint(cfg.llm):
        missing.append('测试当前模型连接')
    if not (cfg.adapter.wechat_enabled or cfg.adapter.qq_enabled):
        missing.append('至少选择一个聊天通道')
    if cfg.adapter.wechat_enabled and not runtime.ilink_status().get('loggedIn'):
        missing.append('完成微信 iLink 扫码登录')
    if cfg.adapter.qq_enabled and not (await runtime.probe_qq()).get('loggedIn'):
        missing.append('启动 QQ 网关并完成登录')
    if missing:
        return {'ok': False, 'missing': missing, 'message': '还差这些步骤：' + '；'.join(missing)}
    if body.get('start', True):
        result = await runtime.start()
        if not result.get('ok'):
            return {'ok': False, 'missing': [], 'message': result.get('message') or '机器人启动未完成'}
    state.update(completed=True, dismissed=False, step=5)
    onboarding_store.save(state)
    return {'ok': True, 'message': '配置完成，可以开始聊天了', 'started': body.get('start', True)}


@app.get('/api/data/status')
async def api_data_status():
    return {'data_dir':str(DATA_DIR), 'backup_dir':str(DATA_DIR/'.backups'),
            'custom_path':bool(os.environ.get('WXBOT_DATA_DIR') or os.environ.get('WXBOT_USER_DATA_DIR')) and os.environ.get('WXBOT_DEFAULT_DATA_DIR')!='1',
            'migration':{'status':STORAGE_REPORT.get('status','unknown'),
                         'imported_files':STORAGE_REPORT.get('imported_files',0),
                         'other_backups':len(STORAGE_REPORT.get('other_backups',[]))},
            'retained_on_uninstall':True if not (os.environ.get('WXBOT_DATA_DIR') or os.environ.get('WXBOT_USER_DATA_DIR')) or os.environ.get('WXBOT_DEFAULT_DATA_DIR')=='1' else None}


@app.post('/api/data/backup')
async def api_data_backup():
    if _backup_lock.locked():raise HTTPException(409,'已有备份正在创建，请稍候')
    async with _backup_lock:
        try:
            file=await asyncio.to_thread(create_backup, DATA_DIR, {'ilink':runtime._ilink_state_dir()})
        except (OSError,ValueError,RuntimeError):
            log.error('Local backup did not complete; existing data retained')
            raise HTTPException(500,'备份未完成，原数据保持不变。请检查目录空间、权限，或停止旧版本后重试。')
    return {'ok':True,'name':file.name,'path':str(file),'download_url':'/api/data/backup/'+file.name,
            'message':'本地备份已创建并通过 SHA256 校验'}


@app.get('/api/data/backup/{name}')
async def api_data_backup_download(name:str):
    import re
    if not re.fullmatch(r'wxbot-(?:backup|migration)-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\.zip',name):raise HTTPException(404,'备份不存在')
    file=DATA_DIR/'.backups'/name
    if not file.is_file() or file.is_symlink() or (DATA_DIR/'.backups').is_symlink():raise HTTPException(404,'备份不存在')
    return FileResponse(file,media_type='application/zip',filename=name,headers={'Cache-Control':'no-store'})


CONFIG_SECTIONS={
 'primary':('llm',{'primary_base','primary_key','primary_model'}),
 'fallback':('llm',{'fallback_base','fallback_key','fallback_model'}),
 'judge':('llm',{'judge_base','judge_key','judge_model'}),
 'vision':('llm',{'vision_base','vision_key','vision_model'}),
 'search':('llm',{'search_enabled','search_key','search_model','context_timezone'}),
 'persona':('bot',{'name','persona','persona_profile','persona_relationship'}),
 'reply':('bot',{'group_reply_at_sender','message_merge_window','message_merge_max_wait','humanized_wait','typing_indicator','message_pause_timeout'}),
 'memory':('bot',{'memory_enabled','memory_inject','memory_extract_interval'}),
 'channels':('adapter',{'wechat_enabled','qq_enabled','wechat_type','onebot_http_url','onebot_token','bot_qq','bot_nickname','napcat_image_mirror'})}

@app.patch('/api/config/section/{section}')
async def api_config_section(section:str,request:Request):
    from dataclasses import asdict
    if section not in CONFIG_SECTIONS:raise HTTPException(404,'设置分区不存在')
    values=await request.json();root,allowed=CONFIG_SECTIONS[section]
    if not isinstance(values,dict) or set(values)-allowed:raise HTTPException(400,'包含其他分区或未知设置')
    old=config_store.load();raw=asdict(old)
    for key,value in values.items():
        original=raw[root][key]
        if isinstance(original,str):
            if not isinstance(value,str):raise HTTPException(400,'文本设置格式错误')
            value=value.strip()
            if (key.endswith('_key') or key.endswith('_token')) and '•' in value:value=original
        elif isinstance(original,bool):
            if not isinstance(value,bool):raise HTTPException(400,'开关设置需要布尔值')
        elif isinstance(original,(float,int)):
            import math
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise HTTPException(400,'数字设置格式错误')
        if key=='context_timezone':
            from .core.reality import is_valid_timezone
            if not is_valid_timezone(value):raise HTTPException(400,'请填写有效的 IANA 时区，如 Asia/Shanghai')
        if key=='search_model' and not value:raise HTTPException(400,'搜索模型名称不能为空')
        if key=='napcat_image_mirror':
            import re
            if value and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]{1,5})?',value):raise HTTPException(400,'镜像源请填写域名，可附端口，不含协议或路径')
        raw[root][key]=value
    cfg=config_store._from_dict(raw);config_store.save(cfg);restart=runtime.apply_config(cfg)
    return {'ok':True,'section':section,'requires_restart':restart,'config':config_store.masked(cfg),
            'message':'本区设置已保存'+('，通道需重启机器人生效' if restart else '并应用')}

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
    requires_restart = runtime.apply_config(cfg)
    return {"ok": True, "requires_restart": requires_restart,
            "message": ("配置已保存，通道设置将在重启机器人后生效"
                        if requires_restart else "配置已保存并应用")}


def _merge_config(old: AppConfig, incoming: dict) -> AppConfig:
    cfg = config_store._from_dict(incoming)     # 先按前端值构造

    # 掩码字段 → 沿用旧值
    def keep_masked(new_val: str, old_val: str) -> str:
        return old_val if (not new_val or "•" in new_val) else new_val

    inc_llm = incoming.get("llm") or {}
    inc_ad = incoming.get("adapter") or {}
    for f in ("primary_key", "fallback_key", "judge_key", "vision_key", "search_key"):
        setattr(cfg.llm, f, keep_masked(inc_llm.get(f, ""), getattr(old.llm, f, "")))
    for f in ('vision_key','search_key'):
        if f in inc_llm:
            value=inc_llm.get(f) or ''
            setattr(cfg.llm,f,getattr(old.llm,f) if '•' in value else value)
    # Older whole-config clients must not reset newly introduced search/time fields.
    for f in ('search_enabled','search_key','search_model','context_timezone'):
        if f not in inc_llm:setattr(cfg.llm,f,getattr(old.llm,f))
    for f in ("onebot_token", "napcat_webui_token"):
        setattr(cfg.adapter, f,
                keep_masked(inc_ad.get(f, ""), getattr(old.adapter, f, "")))
    return cfg


# ═══════════════════════════════════════════════════════════════════
#  消息日志
# ═══════════════════════════════════════════════════════════════════

@app.get('/api/statistics')
async def api_statistics(days:int=30,platform:str='',direction:str='in',chat_type:str='',limit:int=20,min_count:int=2):
    if days not in (0,7,30,90) or platform not in ('','wechat','qq') or direction not in ('in','out','all') or chat_type not in ('','private','group') or not 1<=limit<=50 or not 1<=min_count<=100:
        raise HTTPException(400,'统计筛选参数无效')
    try:
        return await asyncio.to_thread(runtime.analytics.query,days,platform,direction,chat_type,limit,min_count,runtime.cfg.llm.context_timezone)
    except (OSError,RuntimeError) as error:
        log.warning('Local statistics unavailable error_type=%s',type(error).__name__)
        raise HTTPException(503,'本地统计暂未完成，请检查日志读取权限及运行依赖后重试')


@app.get("/api/messages")
async def api_messages(limit: int = 100, chat_id: str = "",
                       chat_type: str = "", direction: str = "") -> dict:
    return {"messages": runtime.log.query(limit, chat_id, chat_type, direction)}


@app.delete("/api/messages")
async def api_clear_messages() -> dict:
    runtime.log.clear()
    return {"ok": True}


@app.post("/api/app/shutdown")
async def api_app_shutdown(request: Request) -> Response:
    try:
        local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        local = False
    if not local:
        return JSONResponse({"ok": False, "message": "仅本机可关闭桌面后台"}, status_code=403)
    server = getattr(app.state, "desktop_server", None)
    if server is None:
        return JSONResponse({"ok": False, "message": "当前后台未由桌面入口托管"}, status_code=503)
    asyncio.get_running_loop().call_later(0.05, setattr, server, "should_exit", True)
    return JSONResponse({"ok": True})


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
    # 端口可能顺延，必须告诉 runtime —— QQ 网关的反向 WS 地址要用它
    runtime.set_http_port(port)
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
    server = uvicorn.Server(uvicorn.Config(
        app, host=HOST, port=port, log_level="info", access_log=False))
    app.state.desktop_server = server
    server.run()


if __name__ == "__main__":
    main()
