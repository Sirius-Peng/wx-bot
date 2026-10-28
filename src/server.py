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
import time
import uuid
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
from .core.data_import import inspect_import, prepare_import, cancel_import, import_status

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
from .core.companion_models import CompanionModels
from .adapters.desktop import DesktopAdapter
companion_models=CompanionModels(DATA_DIR)
_companion_lock=asyncio.Lock()
_model_test_lock = asyncio.Lock()
_backup_lock = asyncio.Lock()
_import_previews = {}


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
    await runtime.resume_gateway()
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
    await runtime.close_gateway()
    await runtime.close_onebot_ingress()


app = FastAPI(title="微信 AI 助手", lifespan=lifespan)

@app.get('/companion')
async def companion_page(request:Request):
    response=FileResponse(WEB_DIR/'companion.html')
    if admin_auth.can_bootstrap(request):admin_auth.set_cookie(request,response)
    response.headers['Cache-Control']='no-store'
    return response

@app.get('/api/companion/status')
async def companion_status():
    return {'enabled':runtime.cfg.companion.enabled,'model_id':runtime.cfg.companion.model_id,
            'models':companion_models.models(),'owner_id':runtime.cfg.companion.owner_id,
            'persona_space':runtime.persona_spaces.active_key,
            'local_decision':'disabled','decision_backend':'cloud'}

@app.post('/api/companion/model/import')
async def companion_import(request:Request):
    body=await request.json()
    try:
        async with _companion_lock:
            result=await asyncio.to_thread(companion_models.import_model,body.get('path',''),body.get('manifest',''))
            if result.get('id'):
                cfg=config_store.load();cfg.companion.model_id=result['id'];config_store.save(cfg);runtime.apply_config(cfg)
        return result
    except (OSError,ValueError,TypeError,KeyError) as error:raise HTTPException(400,str(error))

@app.get('/api/companion/models/{mid}/{resource:path}')
async def companion_resource(mid:str,resource:str):
    try:return FileResponse(companion_models.resource(mid,resource))
    except (OSError,ValueError):raise HTTPException(404,'模型资源不存在')

@app.get('/api/diagnostics')
async def diagnostics():
    # Keep the legacy field stable, but never imply that an offline worker is
    # loading or retrying. Only cloud admission and deterministic rules run.
    return {'local_decision':{'ready':False,'reason':'disabled','backend':'cloud'},
            'decision_backend':'cloud',
            'usage':runtime.usage_diagnostics(),
            'channels':{o.adapter.platform:{'budget':o.ctx.last_budget,
                'requests':list(getattr(o.llm,'metrics',[])),
                'decision':getattr(o.reply_decider,'last_status',{}),
                'decision_requests':list(getattr(getattr(o.reply_decider,'gateway',None),'metrics',[]))}
                for o in runtime._orchestrators()}}

@app.websocket('/ws/companion')
async def companion_socket(websocket:WebSocket):
    # HTTP middleware does not run for a websocket handshake.
    origin=websocket.headers.get('origin','')
    from urllib.parse import urlsplit
    if (urlsplit(origin).netloc!=websocket.headers.get('host','') or
        urlsplit(origin).scheme not in ('http','https') or not admin_auth.authenticated(websocket)):
        await websocket.close(code=1008);return
    if not runtime.cfg.companion.enabled:
        await websocket.close(code=1008,reason='桌面伙伴未启用');return
    await websocket.accept();queue=asyncio.Queue(maxsize=256)
    async with _companion_lock:
        adapter=runtime.desktop_adapter
        if adapter is None:
            adapter=DesktopAdapter(DATA_DIR,runtime.cfg.companion.owner_id)
            adapter.on_message(runtime.handle_desktop)
            try:await runtime.register_desktop(adapter)
            except ValueError as error:
                await websocket.send_json({'version':1,'type':'error','message':str(error)})
                await websocket.close();return
        adapter.listeners.add(queue)
    async def outgoing():
        while True:await websocket.send_json(await queue.get())
    sender=asyncio.create_task(outgoing())
    try:
        while True:
            body=await websocket.receive_json()
            if body.get('type')=='sync':
                if body.get('persona_space') is not None and body['persona_space']!=adapter.current_persona_space():
                    await queue.put({'version':1,'type':'space_changed','persona_space':adapter.current_persona_space()})
                for event in adapter.snapshot(int(body.get('after',0))):await queue.put(event)
            elif body.get('type')=='message':
                try:
                    result=await adapter.submit(body.get('text'),body.get('client_id'),body.get('persona_space'))
                    await queue.put({'version':1,'type':'ack',**result})
                except ValueError as error:
                    await queue.put({'version':1,'type':'error','message':str(error),
                                     'persona_space':adapter.current_persona_space()})
    except (WebSocketDisconnect,ValueError,RuntimeError):pass
    finally:
        adapter.listeners.discard(queue);sender.cancel();await asyncio.gather(sender,return_exceptions=True)


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
#  点一个按钮，程序校验内置网关、写配置、启动原生进程、等待二维码。
#  部署是**非阻塞**的，前端轮询 /api/qq/status 看进度。
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/qq/status")
async def api_qq_status() -> dict:
    """QQ 通道状态：本机进程、外部接入、登录态分别显示。"""
    return await runtime.qq_status()


@app.post("/api/qq/setup")
async def api_qq_setup() -> dict:
    """启动后端托管的内置 QQ 网关。"""
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

@app.get('/api/proactive')
async def api_proactive_status() -> dict:
    return runtime.proactive.status()


@app.patch('/api/proactive/contact')
async def api_proactive_contact(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {'id', 'enabled'} or not isinstance(body['id'], str) or not isinstance(body['enabled'], bool):
        raise HTTPException(400, '联系人设置格式错误')
    if body['id'] not in runtime.proactive.accounts:
        raise HTTPException(404, '请先在微信或QQ中与机器人私聊')
    try:
        runtime.proactive.set_contact(body['id'], body['enabled'])
    except ValueError:
        raise HTTPException(409, '主动联系记录需要恢复')
    return {'ok': True, **runtime.proactive.status()}


@app.get("/api/memory")
async def api_memory_list(scope: str = "", owner_id: str = "",
                          category: str = "", keyword: str = "", include_history: bool = False) -> dict:
    return runtime.memory_list(scope, owner_id, category, keyword, include_history)


@app.get("/api/memory/stats")
async def api_memory_stats() -> dict:
    return runtime.memory_stats()


@app.get('/api/memory/graph')
async def api_memory_graph(response:Response,platform:str='',person:str='',phase:str='',keyword:str='',include_history:bool=False,limit:int=160):
    response.headers['Cache-Control']='no-store'
    if (platform not in ('','ilink','onebot') or phase not in ('','candidate','stable','temporary','dormant','pinned')
        or len(person)>100 or len(keyword)>120 or not 20<=limit<=300):
        raise HTTPException(400,'记忆拓扑筛选参数无效')
    try:
        return await asyncio.to_thread(runtime.memory_graph,platform=platform,person=person,phase=phase,
                                       keyword=keyword,include_history=include_history,limit=limit)
    except (OSError,ValueError,TypeError,KeyError):
        log.warning('Memory topology snapshot unavailable')
        raise HTTPException(503,'记忆拓扑暂未读取完成，请检查本地数据文件后刷新')


@app.get("/api/memory/forgotten")
async def api_memory_forgotten() -> dict:
    return {'items': runtime._ensure_memory().suppressions}


@app.delete("/api/memory/forgotten/{marker_id}")
async def api_memory_allow_relearning(marker_id: str) -> dict:
    if not runtime.memory_allow_relearning(marker_id):
        raise HTTPException(404, '遗忘标记不存在')
    return {'ok': True, 'id': marker_id, 'message': '已允许今后重新学习，未恢复旧记忆'}


@app.post("/api/memory/learning/retry")
async def api_memory_retry() -> dict:
    return runtime.memory_retry_learning()


@app.post("/api/memory/identities")
async def api_memory_link(request: Request) -> dict:
    body = await request.json()
    try:
        memory=runtime._ensure_memory()
        identity = (memory.link_identity_accounts(body['accounts'],str(body.get('label',''))) if 'accounts' in body else
                    memory.link_accounts(str(body.get('wechat_id','')),str(body.get('qq_id','')),str(body.get('label',''))))
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


@app.patch("/api/memory/{item_id}")
async def api_memory_pin(item_id: str, request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {'pinned'} or not isinstance(body['pinned'], bool):
        raise HTTPException(400, '固定设置需要 pinned 布尔值')
    if not runtime._ensure_memory().set_pinned(item_id, body['pinned']):
        raise HTTPException(404, '记忆不存在')
    return {'ok': True, 'id': item_id, 'pinned': body['pinned']}


@app.delete("/api/memory/{item_id}")
async def api_memory_delete(item_id: str) -> dict:
    return runtime.memory_delete(item_id)


@app.delete("/api/memory")
async def api_memory_clear(scope: str = "", owner_id: str = "") -> dict:
    return runtime.memory_clear(scope, owner_id)


# ═══════════════════════════════════════════════════════════════════
#  旧 Docker API 兼容入口（已停用，不执行 Docker）
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/docker/status")
async def api_docker_status() -> dict:
    """兼容旧客户端；当前网关不需要 Docker。"""
    return runtime.docker_status()


@app.post("/api/docker/prepare")
async def api_docker_prepare() -> dict:
    """兼容旧客户端；不下载、安装或启动 Docker。"""
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
            'import':import_status(DATA_DIR),
            'retained_on_uninstall':True if not (os.environ.get('WXBOT_DATA_DIR') or os.environ.get('WXBOT_USER_DATA_DIR')) or os.environ.get('WXBOT_DEFAULT_DATA_DIR')=='1' else None}


def _local_import(request):
    try:
        local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        local = False
    if not local: raise HTTPException(403, '旧目录导入仅支持从运行后台的本机管理界面操作')


@app.post('/api/data/import/preview')
async def api_data_import_preview(request: Request):
    _local_import(request)
    if _backup_lock.locked(): raise HTTPException(409, '已有数据任务正在执行，请稍候')
    async with _backup_lock:
        try:
            body = await request.json()
            result = await asyncio.to_thread(inspect_import, body.get('source'), DATA_DIR)
        except (ValueError, AttributeError, TypeError) as exc:
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else '请选择旧数据目录')
        except OSError:
            raise HTTPException(400, '旧目录读取失败，请检查路径、空间和权限')
        now = time.monotonic()
        expired = [k for k,v in _import_previews.items() if now-v['at'] > 900]
        for k in expired: _import_previews.pop(k, None)
        if len(_import_previews) >= 16: _import_previews.pop(next(iter(_import_previews)))
        token = uuid.uuid4().hex
        _import_previews[token] = {**result, 'at': now}
        return {**result, 'ok': True, 'token': token}


@app.post('/api/data/import/prepare')
async def api_data_import_prepare(request: Request):
    _local_import(request)
    try: body = await request.json()
    except ValueError: raise HTTPException(400, '请先预览并确认导入')
    if not isinstance(body, dict) or body.get('confirm_replace') is not True:
        raise HTTPException(400, '请确认替换当前数据后再导入')
    token = body.get('token')
    preview = _import_previews.get(token) if isinstance(token, str) else None
    if not preview or time.monotonic()-preview['at'] > 900:
        raise HTTPException(409, '导入预览已过期，请重新预览')
    if _backup_lock.locked(): raise HTTPException(409, '已有数据任务正在执行，请稍候')
    async with _backup_lock:
        try:
            report = await asyncio.to_thread(prepare_import, preview['source'], DATA_DIR, preview['fingerprint'])
        except ValueError as exc: raise HTTPException(409, str(exc))
        except (OSError, RuntimeError): raise HTTPException(500, '旧数据准备未完成，当前数据未改变；请检查空间、权限后重试')
        _import_previews.pop(token, None)
    return {'ok': True, 'requires_backend_restart': True, 'import': report, 'message': report['message']}


@app.post('/api/data/import/cancel')
async def api_data_import_cancel(request: Request):
    _local_import(request)
    if _backup_lock.locked(): raise HTTPException(409, '已有数据任务正在执行，请稍候')
    async with _backup_lock:
        try: return await asyncio.to_thread(cancel_import, DATA_DIR)
        except ValueError as exc: raise HTTPException(409, str(exc))


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
    if not re.fullmatch(r'wxbot-(?:backup|migration|import|preimport)-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\.zip',name):raise HTTPException(404,'备份不存在')
    file=DATA_DIR/'.backups'/name
    if not file.is_file() or file.is_symlink() or (DATA_DIR/'.backups').is_symlink():raise HTTPException(404,'备份不存在')
    return FileResponse(file,media_type='application/zip',filename=name,headers={'Cache-Control':'no-store'})


CONFIG_SECTIONS={
 'voice':('voice',{'enabled','provider','base_url','api_key','fish_model','model_id','reference_audio','reference_text','prompt_language','text_language','reply_rate','cooldown_seconds','max_chars','timeout_seconds','clear_api_key'}),
 'primary':('llm',{'primary_base','primary_key','primary_model','context_window_tokens','context_compact_ratio'}),
 'fallback':('llm',{'fallback_base','fallback_key','fallback_model'}),
 'judge':('llm',{'judge_base','judge_key','judge_model'}),
 'vision':('llm',{'vision_base','vision_key','vision_model'}),
 'search':('llm',{'search_enabled','search_backend','search_key','search_model','context_timezone'}),
 'persona':('bot',{'name','persona','persona_profile','persona_relationship'}),
 'proactive':('bot',{'proactive_enabled','proactive_min_hours','proactive_max_hours','proactive_daily_limit','proactive_quiet_start','proactive_quiet_end'}),
 'reply':('bot',{'chat_mode','reply_mode','late_reply_enabled','group_reply_at_sender','message_merge_window','message_merge_max_wait','humanized_wait','typing_indicator','message_pause_timeout'}),
 'companion':('companion',{'enabled','model_id','owner_id','always_on_top','click_through'}),
 'memory':('bot',{'memory_enabled','memory_inject','memory_extract_interval'}),
 'channels':('adapter',{'wechat_enabled','qq_enabled','wechat_type','onebot_http_url','onebot_token','bot_qq','bot_nickname','napcat_image_mirror','napcat_mode'})}

def _validate_session_settings(values):
    if 'search_backend' in values and (not isinstance(values['search_backend'],str) or values['search_backend'] not in ('tools','native')):
        raise HTTPException(400,'搜索方式需要 tools 或 native')
    if 'context_window_tokens' in values:
        value=values['context_window_tokens']
        if isinstance(value,bool) or not isinstance(value,int) or value<0:
            raise HTTPException(400,'上下文窗口需要填写非负整数，0 表示自动识别')
    if 'context_compact_ratio' in values:
        value=values['context_compact_ratio']
        if (isinstance(value,bool) or not isinstance(value,(int,float))
                or not .1<=value<=.8):
            raise HTTPException(400,'上下文压缩阈值需要在 10% 到 80% 之间')

def _validate_voice_settings(values):
    import math
    from .core.voice import FISH_MODELS
    from urllib.parse import urlsplit
    if 'provider' in values and values['provider'] not in ('fish','gpt_sovits'):raise HTTPException(400,'语音服务类型无效')
    if 'fish_model' in values and (not isinstance(values['fish_model'],str) or values['fish_model'] not in FISH_MODELS):raise HTTPException(400,'请选择支持的 Fish 模型')
    for name,lo,hi in [('reply_rate',0,1),('cooldown_seconds',0,3600),('max_chars',20,500),('timeout_seconds',3,60)]:
        if name not in values:continue
        value=values[name]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
            raise HTTPException(400,'语音数值设置超出范围')
        if name=='max_chars' and not isinstance(value,int):raise HTTPException(400,'语音字数上限需要整数')
    if 'clear_api_key' in values and not isinstance(values['clear_api_key'],bool):raise HTTPException(400,'清除Key开关需要布尔值')
    if 'base_url' in values:
        try:
            if not isinstance(values['base_url'],str):raise ValueError('not a URL')
            url=urlsplit(values['base_url']);url.port
            valid=url.scheme in ('http','https') and bool(url.hostname) and not url.username and not url.password and not url.query and not url.fragment
        except (ValueError,TypeError):valid=False
        if not valid:raise HTTPException(400,'语音服务地址格式错误，不含账号、查询参数或片段')

@app.get('/api/voice/status')
async def api_voice_status():return runtime.voice.status()

@app.post('/api/voice/preview')
async def api_voice_preview(request:Request):
    try:
        body=await request.json()
        if not isinstance(body,dict) or set(body)-{'text'}:raise ValueError('试听请求格式错误')
        audio=await runtime.voice.preview(body.get('text',''))
        return Response(audio.data,media_type=audio.mime_type,headers={'Cache-Control':'no-store',
            'Content-Disposition':'inline; filename="voice-preview.wav"','X-Content-Type-Options':'nosniff'})
    except ValueError as error:raise HTTPException(400,str(error))
    except asyncio.CancelledError:raise
    except Exception as error:
        log.warning('Voice preview ended error_type=%s',type(error).__name__)
        raise HTTPException(502,'语音试听未完成，请检查语音服务状态、Key、额度和音色ID')

@app.patch('/api/config/section/{section}')
async def api_config_section(section:str,request:Request):
    from dataclasses import asdict
    if section not in CONFIG_SECTIONS:raise HTTPException(404,'设置分区不存在')
    values=await request.json();root,allowed=CONFIG_SECTIONS[section]
    if not isinstance(values,dict) or set(values)-allowed:raise HTTPException(400,'包含其他分区或未知设置')
    if root=='llm':_validate_session_settings(values)
    if root=='voice':_validate_voice_settings(values)
    old=config_store.load();raw=asdict(old)
    clear_voice_key=values.pop('clear_api_key',False) if root=='voice' else False
    for key,value in values.items():
        original=raw[root][key]
        if isinstance(original,str):
            if not isinstance(value,str):raise HTTPException(400,'文本设置格式错误')
            value=value.strip()
            if (key.endswith('_key') or key.endswith('_token')) and '•' in value:value=original
            if root=='voice' and key=='api_key' and not value:value=original
        elif isinstance(original,bool):
            if not isinstance(value,bool):raise HTTPException(400,'开关设置需要布尔值')
        elif isinstance(original,(float,int)):
            import math
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise HTTPException(400,'数字设置格式错误')
        if key=='context_timezone':
            from .core.reality import is_valid_timezone
            if not is_valid_timezone(value):raise HTTPException(400,'请填写有效的 IANA 时区，如 Asia/Shanghai')
        if key=='reply_mode' and value not in ('natural','always'):raise HTTPException(400,'回复方式需要 natural 或 always')
        if key=='search_model' and not value:raise HTTPException(400,'搜索模型名称不能为空')
        if key=='napcat_mode' and value not in ('native','external'):raise HTTPException(400,'QQ 网关方式需要 native 或 external')
        if key=='napcat_image_mirror':
            import re
            if value and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]{1,5})?',value):raise HTTPException(400,'镜像源请填写域名，可附端口，不含协议或路径')
        raw[root][key]=value
    if clear_voice_key:
        raw['voice']['api_key']=''
        if raw['voice']['provider']=='fish':raw['voice']['enabled']=False
    cfg=config_store._from_dict(raw)
    if root=='voice':
        from .core.voice import validate_voice_config
        try:validate_voice_config(cfg.voice,require_ready=cfg.voice.enabled)
        except (ValueError,TypeError) as error:raise HTTPException(400,str(error))
    config_store.save(cfg)
    await runtime.reconcile_gateway(cfg)
    restart=runtime.apply_config(cfg)
    return {'ok':True,'section':section,'requires_restart':restart,'config':config_store.masked(cfg),
            'message':'本区设置已保存'+('，通道需重启机器人生效' if restart else '并应用')}

@app.put("/api/config")
async def api_put_config(request: Request) -> dict:
    """保存配置。

    ⚠️ 密钥字段前端回传的是掩码（••••1234），
    收到掩码时必须保留原值，否则会把用户的 Key 覆盖成掩码串。
    """
    incoming = await request.json()
    if isinstance(incoming,dict) and isinstance(incoming.get('llm'),dict):
        _validate_session_settings(incoming['llm'])
    if isinstance(incoming,dict) and 'voice' in incoming:
        if not isinstance(incoming['voice'],dict):raise HTTPException(400,'语音设置格式错误')
        _validate_voice_settings(incoming['voice'])
    old = config_store.load()
    cfg = _merge_config(old, incoming)
    if isinstance(incoming,dict) and 'voice' in incoming:
        from .core.voice import validate_voice_config
        try:validate_voice_config(cfg.voice,require_ready=cfg.voice.enabled)
        except (ValueError,TypeError) as error:raise HTTPException(400,str(error))
    config_store.save(cfg)
    await runtime.reconcile_gateway(cfg)
    requires_restart = runtime.apply_config(cfg)
    return {"ok": True, "requires_restart": requires_restart,
            "message": ("配置已保存，通道设置将在重启机器人后生效"
                        if requires_restart else "配置已保存并应用")}


def _merge_config(old: AppConfig, incoming: dict) -> AppConfig:
    cfg = config_store._from_dict(incoming)     # 先按前端值构造

    # 掩码字段 → 沿用旧值
    def keep_masked(new_val: str, old_val: str) -> str:
        return old_val if (not new_val or "•" in new_val) else new_val

    inc_voice=incoming.get('voice') or {}
    from dataclasses import fields
    for field in fields(old.voice):
        if field.name not in inc_voice:setattr(cfg.voice,field.name,getattr(old.voice,field.name))
    cfg.voice.api_key=keep_masked(inc_voice.get('api_key',''),old.voice.api_key)
    inc_bot = incoming.get('bot') or {}
    from .core.proactive import PROACTIVE_FIELDS
    for field in PROACTIVE_FIELDS:
        if field not in inc_bot: setattr(cfg.bot, field, getattr(old.bot, field))
    for field in ('reply_mode', 'late_reply_enabled'):
        if field not in inc_bot: setattr(cfg.bot, field, getattr(old.bot, field))
    inc_llm = incoming.get("llm") or {}
    inc_ad = incoming.get("adapter") or {}
    for f in ("primary_key", "fallback_key", "judge_key", "vision_key", "search_key"):
        setattr(cfg.llm, f, keep_masked(inc_llm.get(f, ""), getattr(old.llm, f, "")))
    for f in ('vision_key','search_key'):
        if f in inc_llm:
            value=inc_llm.get(f) or ''
            setattr(cfg.llm,f,getattr(old.llm,f) if '•' in value else value)
    # Older whole-config clients must not reset newly introduced search/time fields.
    for f in ('search_enabled','search_backend','search_key','search_model','context_timezone','context_window_tokens','context_compact_ratio'):
        if f not in inc_llm:setattr(cfg.llm,f,getattr(old.llm,f))
    for f in ("onebot_token", "napcat_webui_token"):
        setattr(cfg.adapter, f,
                keep_masked(inc_ad.get(f, ""), getattr(old.adapter, f, "")))
    if 'napcat_mode' not in inc_ad:cfg.adapter.napcat_mode=old.adapter.napcat_mode
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
