"""编排层 —— 把适配器 / 触发判定 / 上下文 / LLM / 发送器串成完整链路。

核心链路：
    入站消息 → 幂等 → 加载配置 → 触发判定 → (不回复则归档结束)
                                        → 上下文装配 → LLM 流式收集 → 完整发送 → 归档
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import logging
from typing import Optional

from .core.context import ContextManager
from .core.llm import LLMGateway, JudgeLLM, ProviderCfg
from .core.sender import HumanLikeSender, DeliveryError
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
        self._tasks: set[asyncio.Task] = set()
        self._memory_tasks: set[asyncio.Task] = set()
        adapter.on_message(self.handle)

    # ── 主链路 ────────────────────────────────────────────────

    async def handle(self, msg: WxMessage) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._handle(msg)
        finally:
            self._tasks.discard(task)

    async def close(self) -> None:
        # 重启接入通道前先停止旧消息任务，防止向已关闭的适配器继续发送。
        current = asyncio.current_task()
        tasks = [t for t in self._tasks | self._memory_tasks if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        gateways = {self.llm, getattr(self.judge, "gateway", None)}
        for gateway in gateways:
            if gateway is not None and hasattr(gateway, "aclose"):
                await gateway.aclose()

    async def _handle(self, msg: WxMessage) -> None:
        # ① 幂等去重：网关回调会重试，必须挡掉重复消息
        if not await self.store.mark_seen(msg.msg_id):
            log.debug("duplicate msg %s, skip", msg.msg_id)
            return

        # ② 同会话串行，防止上下文竞争
        lock = self._locks.setdefault(msg.ctx_key, asyncio.Lock())
        try:
            async with lock:
                binding = (self.adapter.bind_reply_context(msg)
                           if hasattr(self.adapter, "bind_reply_context") else nullcontext())
                with binding:
                    await self._process(msg)
        except BaseException:
            # 包括等待同会话锁期间的取消；不能把未处理消息留在幂等集合里。
            await self.store.forget_seen(msg.msg_id)
            log.exception("process failed for msg %s", msg.msg_id)
            raise

    async def _process(self, msg: WxMessage) -> None:
        decision = await self.router.decide(msg)
        log.info("trigger layer=%s reply=%s reason=%s",
                 decision.layer, decision.should_reply, decision.reason)

        if not decision.should_reply:
            await self.store.archive(msg, direction="in")
            return

        # 用户事实在收件时就排队，回复/投递失败不应阻止知识库积累。
        if self.memory is not None and hasattr(self.memory, "learner"):
            await self._maybe_learn(msg, "")

        # 重试/重启复用已生成的完整回复，不能重新生成不同文本再当作同一次投递。
        replay = (self.adapter.get_reply_draft(msg)
                  if hasattr(self.adapter, "get_reply_draft") else None)
        if replay is None and hasattr(self.adapter, "validate_reply_context"):
            self.adapter.validate_reply_context(msg)
        # 人格知识库：召回与当前消息相关的长期记忆
        mem_ctx = ""
        if self.memory and getattr(self.memory, "inject", True):
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

        # 流式生成，完整收集后一次发送，避免一个回复被拆成多个聊天气泡
        try:
            if replay is not None:
                reply = replay
                await self.sender.send(msg.chat_id, reply, at=reply_at)
            else:
                stream = self.llm.stream(messages, chat_id=msg.chat_id)
                reply = await self.sender.stream_send(msg.chat_id, stream, at=reply_at)
        except DeliveryError:
            # 发送故障不是LLM故障：由持久收件箱重试，不覆盖原答案、不额外发兜底。
            log.exception("完整回复投递失败，将保留草稿重试")
            raise
        except Exception:
            log.exception("LLM failed, sending fallback")
            reply = "稍等一下，我这边有点忙不过来，你再说一遍？"
            await self.sender.send(msg.chat_id, reply, at=reply_at)

        # 落库 + 更新上下文
        await self.ctx.append(msg, reply)
        await self.store.archive(msg, direction="in")
        await self.store.archive_reply(msg, reply)

        # 人格知识库：按间隔抽取记忆（每轮都抽太费 token）
        if self.memory is None or not hasattr(self.memory, "learner"):
            await self._maybe_learn(msg, reply)

    async def _maybe_learn(self, msg: WxMessage, reply: str) -> None:
        """按配置的间隔，从最近的对话中抽取长期记忆。"""
        if not self.memory or not getattr(self.memory, "enabled", False):
            return

        if hasattr(self.memory, "learner"):
            try:
                await self.memory.learner.enqueue(msg, reply)
            except Exception:
                log.warning("自动积累排队失败，请查看知识库状态", exc_info=True)
            return
        interval = max(1, int(getattr(self.memory, "extract_interval", 3)))
        n = self._turn_counter.get(msg.ctx_key, 0) + 1
        if n < interval:
            self._turn_counter[msg.ctx_key] = n
            return
        self._turn_counter[msg.ctx_key] = 0

        if len(self._memory_tasks) >= 2:
            self._turn_counter[msg.ctx_key] = n  # 下轮再尝试，不让后台任务无界增长。
            return
        history = await self.store.get_recent(msg.ctx_key)
        task = asyncio.create_task(self._learn(msg, history[-6:]))
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    async def _learn(self, msg: WxMessage, history: list[dict]) -> None:
        try:
            await asyncio.wait_for(self.memory.observe(
                chat_id=msg.chat_id, chat_type=msg.chat_type,
                user_id=msg.sender_id, user_name=msg.sender_name or msg.sender_id,
                history=history), timeout=10.0)
        except Exception:
            log.debug("知识库抽取失败", exc_info=True)

    async def wait_for_memory(self) -> None:
        """供显式同步/测试使用，正常回复不等待后台抽取。"""
        await asyncio.gather(*self._memory_tasks, return_exceptions=True)
        if self.memory is not None and hasattr(self.memory, "learner"):
            await self.memory.learner.wait_idle()

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
