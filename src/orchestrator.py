"""编排层 —— 把适配器 / 触发判定 / 上下文 / LLM / 发送器串成完整链路。

核心链路：
    入站消息 → 幂等 → 加载配置 → 触发判定 → (不回复则归档结束)
                                        → 上下文装配 → LLM 流式收集 → 完整发送 → 归档
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import logging
import re
import time
from typing import Optional

from .core.conversation import ConversationChanged, members, has_video
from .core.media import ImageLoader, MediaError
from .core.context import ContextManager
from .core.llm import LLMGateway, JudgeLLM, ProviderCfg, is_transient_llm_error
from .core.sender import HumanLikeSender, DeliveryError
from .core.trigger import TriggerRouter
from .core.types import WxMessage
from .core.turn_coordinator import TurnCoordinator, ConversationStateError, source_id
from .core.context import ContextWindowExceeded
from .core.turn_preparation import TurnPreparation, prior_turns, recall_query

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, adapter, store, llm: LLMGateway, judge: JudgeLLM,
                 ctx: ContextManager, router: TriggerRouter, sender: HumanLikeSender,
                 memory=None, group_reply_at_sender: bool = True, typing_indicator: bool = True,
                 coordinator=None, reply_decider=None, late_reply_enabled=True):
        from contextvars import ContextVar
        self._turn_resources=ContextVar('turn_resources',default=None)
        self._persona_space=''
        self.adapter = adapter
        self.store = store
        self.llm = llm
        self.judge = judge
        self.ctx = ctx
        self.router = router
        self.sender = sender
        self.images = ImageLoader([getattr(adapter,"http_url", ""), getattr(adapter,"base_url", "")])
        self.memory = memory              # 人格知识库（可选）
        # 群聊回复时是否 @ 提问者（界面上可关，配置必须真正生效）
        self.group_reply_at_sender = bool(group_reply_at_sender)
        self.typing_indicator = bool(typing_indicator)
        self.coordinator = coordinator or TurnCoordinator()
        self.reply_decider = reply_decider
        self.late_reply_enabled = late_reply_enabled
        from contextvars import ContextVar
        self._active_turn = ContextVar('active_turn', default=None)
        self._memory_snapshot = ContextVar('memory_snapshot', default=None)
        self._locks: dict[str, asyncio.Lock] = {}
        self._turn_counter: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._memory_tasks: set[asyncio.Task] = set()
        self._typing_generations: dict[str,int] = {}
        self._typing_locks: dict[str,asyncio.Lock] = {}
        self._typing_cleanup_tasks: set[asyncio.Task] = set()
        self._retired_work: set[asyncio.Task] = set()
        self.preparation = TurnPreparation(self)
        adapter.on_message(self.handle)
        if hasattr(adapter, 'on_turn_preparation'):
            adapter.on_turn_preparation(self.preparation.accept)

    # ── 主链路 ────────────────────────────────────────────────

    @property
    def store(self):
        resources=self._turn_resources.get()
        return resources[0] if resources else self._store
    @store.setter
    def store(self,value):self._store=value
    @property
    def ctx(self):
        resources=self._turn_resources.get()
        return resources[1] if resources else self._ctx
    @ctx.setter
    def ctx(self,value):self._ctx=value
    @property
    def memory(self):
        resources=self._turn_resources.get()
        return resources[2] if resources else self._memory
    @memory.setter
    def memory(self,value):self._memory=value
    @property
    def persona_space(self):
        resources=self._turn_resources.get()
        return resources[3] if resources else self._persona_space
    @persona_space.setter
    def persona_space(self,value):self._persona_space=value

    async def handle(self, msg: WxMessage) -> None:
        resource_marker=self._turn_resources.set((self._store,self._ctx,self._memory,self._persona_space))
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            handler=getattr(self,'inactive_handler',None)
            incoming=getattr(msg,'persona_space','') or getattr(self.adapter,'legacy_space','')
            if handler is not None and incoming and incoming!=self._persona_space:
                await handler(msg)
                return
            await self._handle(msg)
        finally:
            self._tasks.discard(task)
            self._turn_resources.reset(resource_marker)

    async def close(self) -> None:
        # 重启接入通道前先停止旧消息任务，防止向已关闭的适配器继续发送。
        current = asyncio.current_task()
        await self.preparation.close()
        tasks = [t for t in self._tasks | self._memory_tasks if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self._typing_cleanup_tasks,return_exceptions=True)
        if hasattr(self.images,'aclose'):await self.images.aclose()
        contexts={self._ctx,*getattr(self,'_persona_contexts',{}).values()}
        for ctx in contexts:
            if hasattr(ctx,'close'):await ctx.close()
        if self._retired_work:
            await asyncio.wait(self._retired_work, timeout=1.0)
        gateways = (self.llm, getattr(self.judge, "gateway", None),
                    getattr(self.reply_decider, "gateway", None))
        for gateway in {id(g): g for g in gateways}.values():
            if gateway is not None and hasattr(gateway, "aclose"):
                await gateway.aclose()

    async def _handle(self, msg: WxMessage) -> None:
        # ① 幂等去重：网关回调会重试，必须挡掉重复消息
        originals=members(msg)
        seen_ids=[]
        for original in originals:
            sid=source_id(original)
            if await self.store.mark_seen(sid):seen_ids.append(sid)
        if not seen_ids:
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
        except BaseException as exc:
            # 包括等待同会话锁期间的取消；不能把未处理消息留在幂等集合里。
            for sid in seen_ids:await self.store.forget_seen(sid)
            if not isinstance(exc,(ConversationChanged,asyncio.CancelledError)):log.exception("process failed for msg %s", msg.msg_id)
            raise

    async def _process(self, msg: WxMessage) -> None:
        tid=self.coordinator.begin(msg)
        marker=self._active_turn.set(tid)
        memory_marker=self._memory_snapshot.set(self._memory_revision(msg,live=False))
        self.coordinator.enter(msg)
        try:
            self._check_current(msg)
            with self.preparation.bind(msg):
                await self._process_turn(msg,tid)
        except ConversationChanged:
            self.coordinator.finish(tid,'superseded','新来话使未提交旧稿失效')
            await self._cancel_preview(msg)
            raise
        except asyncio.CancelledError:
            # A collector will recover unfinished raw inbox work after restart.
            await self._cancel_preview(msg)
            raise
        except DeliveryError:
            self.coordinator.finish(tid,'uncertain','回复投递故障，请核查原投递状态')
            raise
        finally:
            self.coordinator.leave(msg)
            self._active_turn.reset(marker)
            self._memory_snapshot.reset(memory_marker)
            if hasattr(self.ctx, 'discard'): self.ctx.discard(msg)

    async def _process_turn(self,msg,tid):
        prior=self.coordinator.turns.get(tid,{})
        replay=(self.adapter.get_reply_draft(msg) if hasattr(self.adapter,'get_reply_draft') else None)
        if prior.get('state')=='accepted':
            if prior.get('draft'):await self.ctx.append(msg,prior['draft'])
            return
        if prior.get('state') in ('sending','uncertain') and replay is None:
            # OneBot has no idempotent client token: an unknown network outcome
            # is not permission to regenerate and submit a different answer.
            self.coordinator.finish(tid,'uncertain','原回复提交结果待核查，保留经历，不自动重复发送')
            return
        if self.memory and hasattr(self.memory,'is_suppressed') and self.memory.is_suppressed(msg.platform,msg.sender_id,msg.content):
            self.coordinator.finish(tid,'context_only','明确遗忘屏蔽了旧来话，不重新学习或补回')
            return
        # Admission is durable even if the character chooses to stay quiet.
        if hasattr(self.ctx,'accept_user'):await self.ctx.accept_user(msg)
        if self.memory is not None and hasattr(self.memory,'learner'):
            await self._maybe_learn(msg,'')
        decision = await self.router.decide(msg)
        log.info("trigger layer=%s reply=%s reason=%s",
                 decision.layer, decision.should_reply, decision.reason)

        if not decision.should_reply:
            self.coordinator.finish(tid,'context_only',decision.reason)
            for original in members(msg):await self.store.archive(original, direction="in")
            return

        if self.reply_decider is not None and replay is None:
            history=prior_turns(msg,await self.store.get_recent(msg.ctx_key))
            natural=await self._current_work(msg,self.reply_decider.decide(msg,history))
            self.coordinator.decide(tid,natural,late_enabled=self.late_reply_enabled)
            # Feed the existing message-log/UI hooks without exposing control fields in chat.
            decision.should_reply=natural.action=='reply'
            decision.reason=natural.reason
            if hasattr(self,'observe_decision'):self.observe_decision(decision)
            if natural.action!='reply':
                for original in members(msg):await self.store.archive(original,direction='in')
                return

        started=asyncio.Event()
        generation=self._typing_generations.get(msg.chat_id,0)+1
        self._typing_generations[msg.chat_id]=generation
        typing_task=(asyncio.create_task(self._typing_signal(msg,started,generation))
                     if self.typing_indicator and hasattr(self.adapter,'send_typing') else None)
        try:
            await self._reply(msg)
            self.coordinator.finish(tid)
        finally:
            if typing_task is not None:
                typing_task.cancel();await asyncio.gather(typing_task,return_exceptions=True)
                needs_cleanup = getattr(self.adapter,'typing_cleanup_required',lambda _:False)(msg.chat_id)
                if started.is_set() or needs_cleanup:
                    # Finishing the typing signal must not hold delivery ACK or
                    # the next incoming turn hostage to an optional platform call.
                    cleanup=asyncio.create_task(self._typing_once(msg,False,generation))
                    self._typing_cleanup_tasks.add(cleanup)
                    cleanup.add_done_callback(self._typing_cleanup_tasks.discard)

    async def _reply(self,msg):
        # 重试/重启复用已生成的完整回复，不能重新生成不同文本再当作同一次投递。
        replay = (self.adapter.get_reply_draft(msg)
                  if hasattr(self.adapter, "get_reply_draft") else None)
        if replay is None and hasattr(self.adapter, "validate_reply_context"):
            self.adapter.validate_reply_context(msg)
        # 人格知识库：召回与当前消息相关的长期记忆
        mem_ctx = ""
        if self.memory and getattr(self.memory, "inject", True):
            try:
                query = recall_query(msg,prior_turns(msg,await self.store.get_recent(msg.ctx_key)))
                prepared = self.preparation.memory_context(msg,query)
                if prepared is not None:
                    mem_ctx, used = prepared
                    if used and hasattr(self.memory,'record_usage'):
                        self.memory.record_usage(used)
                else:
                    mem_ctx = self.memory.build_context(
                        user_id=msg.sender_id, chat_id=msg.chat_id, query=query,
                        **({"platform":msg.platform} if getattr(self.memory,"supports_platform_scope",False) else {}),
                        **({'mark_used':True} if hasattr(self.memory,'record_usage') else {}),
                    )
            except Exception:
                log.debug("知识库召回失败", exc_info=True)

        # 装配上下文
        image_error=False
        image_inputs=None
        if replay is None and msg.images:
            try:
                image_inputs=await self._current_work(msg,self._load_images(msg))
                image_error=not any(image_inputs)
                if not all(image_inputs):log.warning('Media partially unavailable message=%s loaded=%s total=%s',msg.msg_id,sum(bool(x) for x in image_inputs),len(msg.images))
            except MediaError:
                image_error=True;log.warning('Image load failed for message %s',msg.msg_id)
        if replay is None and hasattr(self.adapter,'check_reply_current'):self.adapter.check_reply_current(msg)
        context_error=False
        messages=[]
        if replay is None:
            build = (self.ctx.build(msg,memory_context=mem_ctx,image_inputs=image_inputs)
                     if image_inputs else self.ctx.build(msg,memory_context=mem_ctx))
            try:
                messages = await self._current_work(msg, build)
            except ContextWindowExceeded:
                context_error=True
                log.warning('Complete input exceeds configured model/protocol window; retain originals and request smaller parts')

        # 群聊回复时 @ 提问者 —— 否则群里没人知道这条回复是给谁的
        reply_at = ([msg.sender_id]
                    if (msg.is_group and msg.sender_id and self.group_reply_at_sender)
                    else None)

        # 流式生成，完整收集后一次发送，避免一个回复被拆成多个聊天气泡
        try:
            send_options={}
            if isinstance(self.sender,HumanLikeSender) and replay is None:
                tid=self._active_turn.get()
                send_options={'check_current':lambda:self._check_current(msg),
                              'on_submit_text':lambda text:self.coordinator.reserve(tid,kind='reply',text=text) if tid else None}
            if context_error:
                reply = "这一段内容有点多，我们分几段聊，好吗？原来的内容我先留着，你把最想聊的部分单独发来。"
                await self.sender.send(msg.chat_id,reply,at=reply_at,**send_options)
            elif image_error:
                await self._media_failure_grace(msg)
                reply = "这几张图我这次没看清。把最想让我看的那张单独发来，好吗？"
                await self.sender.send(msg.chat_id,reply,at=reply_at,**send_options)
            elif replay is not None:
                reply = replay
                await self.sender.send(msg.chat_id, reply, at=reply_at)
            else:
                capture = getattr(self.llm, 'capture_request', None)
                with capture() if capture else nullcontext({}) as trace:
                    stream = self._guarded_stream(msg,self.llm.stream(messages, chat_id=msg.chat_id))
                    reply = await self.sender.stream_send(msg.chat_id, stream, at=reply_at,**send_options)
                if hasattr(self.ctx, 'capture_request'):
                    self.ctx.capture_request(msg, trace.get('messages'), trace.get('usage'), trace.get('assistant'))
                    usage = trace.get('usage') or {}
                    self.ctx.last_budget.update({'cache_hit_tokens':usage.get('prompt_cache_hit_tokens'),
                                                 'cache_miss_tokens':usage.get('prompt_cache_miss_tokens')})
        except ConversationChanged:
            raise
        except ConversationStateError:
            raise
        except DeliveryError:
            # 发送故障不是LLM故障：由持久收件箱重试，不覆盖原答案、不额外发兜底。
            log.exception("完整回复投递失败，将保留草稿重试")
            raise
        except Exception as error:
            if is_transient_llm_error(error) and hasattr(self.adapter,'get_reply_draft'):
                if hasattr(self.adapter,'mark_generation_pending'):self.adapter.mark_generation_pending(error)
                log.warning('Model unavailable; keep incoming message for retry error_type=%s',type(error).__name__)
                raise
            log.error('LLM generation failed error_type=%s status=%s image_count=%s video=%s',
                      type(error).__name__,getattr(error,'status_code',None),len(msg.images),has_video(msg))
            if msg.images:await self._media_failure_grace(msg)
            reply = ("这几张图我这次没看清。你最想让我看哪一处？也可以把那张单独发来。"
                     if msg.images else "稍等一下，我这边有点忙不过来，你再说一遍？")
            await self.sender.send(msg.chat_id, reply, at=reply_at,**send_options)

        tid=self._active_turn.get()
        if tid:self.coordinator.finish(tid)
        # 落库 + 更新上下文。已获接口接受后，即使历史落盘失败也不重复提交。
        if context_error:
            await self.ctx.append(msg,reply,context_excluded=True)
        else:
            await self.ctx.append(msg, reply)
        for original in members(msg):await self.store.archive(original, direction="in")
        await self.store.archive_reply(msg, reply)

        # 人格知识库：按间隔抽取记忆（每轮都抽太费 token）
        if self.memory is None or not hasattr(self.memory, "learner"):
            await self._maybe_learn(msg, reply)

    async def _load_images(self,msg):
        async def work():
            resolver=None
            if hasattr(self.adapter,'resolve_images'):
                async def resolver(ref):
                    refs=await self.adapter.resolve_images([ref])
                    if not refs:raise MediaError('Image reference missing')
                    return refs[0]
            return await self.images.load_partial(msg.images,resolver=resolver)
        try:return await asyncio.wait_for(work(),timeout=45)
        except (TimeoutError,MediaError) as exc:raise MediaError('Image could not be loaded') from exc

    async def _media_failure_grace(self,msg):
        """Media uploads arrive separately; regroup before committing a failure.
        This applies even at the normal max-wait boundary, not to good replies."""
        check=(getattr(self.adapter,'check_failed_reply_current',None) or
               getattr(self.adapter,'check_reply_current',None))
        if check is None:return
        deadline=time.monotonic()+1.2
        while True:
            check(msg)
            remaining=deadline-time.monotonic()
            if remaining<=0:return
            await asyncio.sleep(min(.05,remaining))

    async def _current_work(self,msg,work):
        task=asyncio.ensure_future(work)
        try:
            while not task.done():
                self._check_current(msg)
                await asyncio.wait({task},timeout=.05)
            self._check_current(msg)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                self._retire(task)
            elif not task.cancelled():
                task.exception()  # consume even when _check_current rejected it

    def _retire(self, task, stream=None):
        """Cancellation cleanup must not hold an obsolete turn's conversation lock."""
        async def cleanup():
            try:
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                if stream is not None and hasattr(stream, 'aclose'):
                    await stream.aclose()
            except Exception:
                log.debug('Retired model stream cleanup failed', exc_info=True)
        job = asyncio.create_task(cleanup())
        self._retired_work.add(job)
        job.add_done_callback(self._retired_work.discard)

    def _check_current(self,msg):
        tid=self._active_turn.get()
        if tid:self.coordinator.check(tid)
        snapshot=self._memory_snapshot.get()
        if snapshot is not None and snapshot!=self._memory_revision(msg):raise ConversationChanged('Forgotten context invalidated unsent draft')
        if hasattr(self.adapter,'check_reply_current'):self.adapter.check_reply_current(msg)

    def _memory_revision(self,msg,*,live=True):
        memory=self._memory if live else self.memory
        if not memory or not hasattr(memory,'identity_key'):return None
        identity=memory.identity_key(msg.platform,msg.sender_id)
        return (str(getattr(memory,'path','')),identity,getattr(memory,'_generation',0),getattr(memory,'_revisions',{}).get(identity,0))

    async def _typing_once(self,msg,on,generation=None):
        if not hasattr(self.adapter,'send_typing'):return False
        try:
            async with self._typing_locks.setdefault(msg.chat_id,asyncio.Lock()):
                if generation is not None and self._typing_generations.get(msg.chat_id)!=generation:return False
                return await asyncio.wait_for(self.adapter.send_typing(msg.chat_id,on),timeout=3.0 if on else .8)
        except Exception:return False

    async def _typing_signal(self,msg,started,generation=None):
        await asyncio.sleep(.45)
        while True:
            if generation is not None and self._typing_generations.get(msg.chat_id)!=generation:return
            if await self._typing_once(msg,True,generation):started.set()
            await asyncio.sleep(4)

    async def _cancel_preview(self,msg):
        if not hasattr(self.adapter,'cancel_turn'):return
        try:await asyncio.wait_for(self.adapter.cancel_turn(msg),.3)
        except (Exception,asyncio.CancelledError):pass

    async def _guarded_stream(self,msg,stream):
        """Cancel an obsolete generation even when the next SSE delta is blocked."""
        pending=None
        try:
            while True:
                pending=asyncio.create_task(anext(stream))
                while not pending.done():
                    self._check_current(msg)
                    await asyncio.wait({pending},timeout=.05)
                self._check_current(msg)
                try:delta=pending.result()
                except StopAsyncIteration:return
                pending=None
                if hasattr(self.adapter,'preview'):
                    try:await asyncio.wait_for(self.adapter.preview(msg,delta),.2)
                    except asyncio.CancelledError:raise
                    except Exception:log.debug('Desktop preview unavailable; retain final delivery')
                self._check_current(msg)
                yield delta
        finally:
            if pending is not None:
                pending.cancel()
                self._retire(pending, stream)
            elif hasattr(stream,'aclose'):
                self._retire(None, stream)

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
