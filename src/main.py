"""Web 服务入口 —— 接收协议网关的消息回调。

    uvicorn src.main:app --host 0.0.0.0 --port 8000

启动流程：
    1. 从环境变量读取配置（LLM Key、网关 Token）
    2. 装配 Orchestrator（适配器 + 存储 + LLM + 触发判定 + 发送器）
    3. 注册 /webhook 接收回调，/healthz 健康检查，/metrics 指标暴露
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .adapters.gewechat import GewechatAdapter
from .core.context import ContextManager
from .core.llm import JudgeLLM, LLMGateway, ProviderCfg
from .core.sender import HumanLikeSender
from .core.trigger import TriggerRouter
from .orchestrator import Orchestrator
from .store_memory import MemoryStore

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

# 全局装配（生产环境请替换 MemoryStore 为 RedisStore + PgStore）
adapter = GewechatAdapter(
    base_url=os.environ["GEWE_BASE_URL"],
    token=os.environ["GEWE_TOKEN"],
    app_id=os.environ["GEWE_APP_ID"],
    bot_wxid=os.environ["BOT_WXID"],
)
store = MemoryStore()

judge_gateway = LLMGateway(ProviderCfg(
    name="judge",
    base_url=os.environ.get("LLM_JUDGE_BASE", "https://open.bigmodel.cn/api/paas/v4"),
    api_key=os.environ.get("LLM_JUDGE_KEY", ""),
    model=os.environ.get("LLM_JUDGE_MODEL", "glm-4.7-flash"),
    timeout=3.0, max_tokens=8,
))
chat_gateway = LLMGateway(
    primary=ProviderCfg(
        name="deepseek",
        base_url=os.environ.get("LLM_PRIMARY_BASE", "https://api.deepseek.com"),
        api_key=os.environ.get("LLM_PRIMARY_KEY", ""),
        model=os.environ.get("LLM_PRIMARY_MODEL", "deepseek-chat"),
        timeout=60.0,
    ),
    fallback=ProviderCfg(
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=os.environ.get("LLM_FALLBACK_KEY", ""),
        model="qwen-plus",
    ),
    usage_sink=store.record_usage,
)

judge = JudgeLLM(judge_gateway, timeout=3.0)
orchestrator = Orchestrator(
    adapter=adapter, store=store, llm=chat_gateway, judge=judge,
    ctx=ContextManager(store),
    router=TriggerRouter(store, judge, persona=os.environ.get("BOT_PERSONA", "")),
    sender=HumanLikeSender(adapter),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await adapter.start()
    log.info("webhook service up")
    yield
    await adapter.stop()


app = FastAPI(title="wx-llm-bot", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """接收协议网关推送的消息。

    必须快速返回 200（网关有重试机制，慢响应会触发重复推送）；
    实际处理在 Orchestrator 内部异步完成。
    """
    payload = await request.json()
    await adapter.handle_webhook(payload)
    return Response(content='{"ret":0}', media_type="application/json")


@app.get("/healthz")
async def healthz() -> dict:
    """健康检查：登录态是最致命的故障点，必须暴露出来给监控。"""
    return {"ok": await adapter.is_healthy(), "platform": adapter.platform}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus 指标暴露点。"""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
