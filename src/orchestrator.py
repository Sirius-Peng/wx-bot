"""编排层 —— 把适配器 / 触发判定 / 上下文 / LLM / 发送器串成完整链路。

核心链路：
    入站消息 → 幂等 → 加载配置 → 触发判定 → (不回复则归档结束)
                                        → 上下文装配 → LLM 流式 → 分段发送 → 归档
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .core.context import ContextManager
from .core.llm import LLMGateway, JudgeLLM, ProviderCfg
from .core.sender import HumanLikeSender
from .core.trigger import TriggerRouter
from .core.types import WxMessage

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, adapter, store, llm: LLMGateway, judge: JudgeLLM,
                 ctx: ContextManager, router: TriggerRouter, sender: HumanLikeSender,
                 memory=None, group_reply_at_sender: bool = True):
        self.adapter = adapter
        self.store = store
        self.llm = llm
        self.judge = judge
        self.ctx = ctx
        self.router = router
        self.sender = sender
        self.memory = memory              # 人格知识库（可选）
        # 群聊回复时是否 @ 提问者（界面上可关，配置必须真正生效）
        self.group_reply_at_sender = bool(group_reply_at_sender)
        self._locks: dict[str, asyncio.Lock] = {}
        self._turn_counter: dict[str, int] = {}
        adapter.on_message(self.handle)

    # ── 主链路 ────────────────────────────────────────────────

    async def handle(self, msg: WxMessage) -> None:
        # ① 幂等去重：网关回调会重试，必须挡掉重复消息
        if not await self.store.mark_seen(msg.msg_id):
            log.debug("duplicate msg %s, skip", msg.msg_id)
            return

        # ② 同会话串行，防止上下文竞争
        lock = self._locks.setdefault(msg.ctx_key, asyncio.Lock())
        async with lock:
            try:
                await self._process(msg)
            except Exception:
                log.exception("process failed for msg %s", msg.msg_id)

    async def _process(self, msg: WxMessage) -> None:
        decision = await self.router.decide(msg)
        log.info("trigger layer=%s reply=%s reason=%s",
                 decision.layer, decision.should_reply, decision.reason)

        if not decision.should_reply:
            await self.store.archive(msg, direction="in")
            return

        # 人格知识库：召回与当前消息相关的长期记忆
        mem_ctx = ""
        if self.memory and getattr(self.memory, "enabled", False):
            try:
                mem_ctx = self.memory.build_context(
                    user_id=msg.sender_id, chat_id=msg.chat_id, query=msg.content,
                )
            except Exception:
                log.debug("知识库召回失败", exc_info=True)

        # 装配上下文
        messages = await self.ctx.build(msg, memory_context=mem_ctx)

        # 群聊回复时 @ 提问者 —— 否则群里没人知道这条回复是给谁的
        reply_at = ([msg.sender_id]
                    if (msg.is_group and msg.sender_id and self.group_reply_at_sender)
                    else None)

        # 流式生成 + 分段发送（微信无原生流式，分段是最接近的体验）
        try:
            stream = self.llm.stream(messages, chat_id=msg.chat_id)
            reply = await self.sender.stream_send(msg.chat_id, stream, at=reply_at)
        except Exception:
            log.exception("LLM failed, sending fallback")
            reply = "稍等一下，我这边有点忙不过来，你再说一遍？"
            await self.sender.send(msg.chat_id, reply, at=reply_at)

        # 落库 + 更新上下文
        await self.ctx.append(msg, reply)
        await self.store.archive(msg, direction="in")
        await self.store.archive_reply(msg, reply)

        # 人格知识库：按间隔抽取记忆（每轮都抽太费 token）
        await self._maybe_learn(msg, reply)

    async def _maybe_learn(self, msg: WxMessage, reply: str) -> None:
        """按配置的间隔，从最近的对话中抽取长期记忆。"""
        if not self.memory or not getattr(self.memory, "enabled", False):
            return

        interval = max(1, int(getattr(self.memory, "extract_interval", 3)))
        n = self._turn_counter.get(msg.ctx_key, 0) + 1
        if n < interval:
            self._turn_counter[msg.ctx_key] = n
            return
        self._turn_counter[msg.ctx_key] = 0

        try:
            history = await self.store.get_recent(msg.ctx_key)
            await self.memory.observe(
                chat_id=msg.chat_id, chat_type=msg.chat_type,
                user_id=msg.sender_id, user_name=msg.sender_name or msg.sender_id,
                history=history[-6:],
            )
        except Exception:
            log.debug("知识库抽取失败", exc_info=True)

    # ── 后台任务 ──────────────────────────────────────────────

    async def health_loop(self, interval: int = 30) -> None:
        """健康检查循环：登录态失效是最致命的故障，必须秒级发现。"""
        while True:
            try:
                ok = await self.adapter.is_healthy()
                if not ok:
                    log.error("!!! gateway unhealthy, need re-login !!!")
                    # 实际实现：推送告警（企微机器人 / 钉钉 / 邮件）
            except Exception:
                log.exception("health check failed")
            await asyncio.sleep(interval)

    async def run(self) -> None:
        await self.adapter.start()
        await asyncio.gather(
            self.health_loop(),
            self.store.flush_loop(),   # 定期把 Redis 热数据刷盘到 PG
        )
