"""Adversarial review of stable sessions; isolated deterministic fixtures, no API calls."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from src.core.context import ContextManager, LLMCompactor
from src.core.conversation import ConversationChanged
from src.core.llm import LLMGateway, ProviderCfg
from src.core.sender import HumanLikeSender
from src.core.trigger import TriggerRouter
from src.core.types import WxMessage
from src.core.web_search import SEARCH_RULES
from src.orchestrator import Orchestrator
from src.store_memory import MemoryStore


def msg(mid='current', text='现在继续聊吧。'):
    return WxMessage(mid, 'desktop', 'private', 'local', 'local', '测试用户', 'bot', text)


class Adapter:
    platform = 'desktop'
    def __init__(self): self.changed = False; self.sent = []
    def on_message(self, callback): self.callback = callback
    def check_reply_current(self, _):
        if self.changed: raise ConversationChanged('new input supersedes draft')
    async def send_text(self, chat, text, at=None): self.sent.append(text); return 'accepted'


class SessionReview(unittest.IsolatedAsyncioTestCase):
    async def context(self, **kwargs):
        store = MemoryStore(); context = ContextManager(store, **kwargs)
        context.configure_session(context_window_tokens=1_000_000)
        self.addAsyncCleanup(context.close)
        return store, context

    async def test_normal_prefix_is_exact_and_missing_usage_stays_unknown(self):
        store, context = await self.context()
        first = msg('first', '今天路上遇到一只猫。')
        original = copy.deepcopy(await context.build(first))
        await context.append(first, '嗯，继续说。')
        persisted = await store.get_session(first.ctx_key)
        self.assertNotIn('prompt_tokens', persisted)
        following = await context.build(msg('next', '它有蓝色的项圈。'))
        self.assertEqual(following[:len(original)], original)
        self.assertIsNone(context.last_budget['cache_hit_tokens'])
        self.assertIsNone(context.last_budget['cache_miss_tokens'])

    async def test_captured_search_tool_protocol_is_kept_with_call_ids(self):
        store, context = await self.context()
        message = msg(); base = await context.build(message)
        actual = [*base,
            {'role':'assistant','content':None,'tool_calls':[{'id':'call-1','type':'function',
                'function':{'name':'web_search','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'call-1','content':'{"summary":"公开依据"}'}]
        context.capture_request(message, actual, {'prompt_tokens':100})
        await context.append(message, '根据正文概括。')
        persisted = await store.get_session(message.ctx_key)
        self.assertEqual(persisted['messages'][:-1], actual)
        self.assertEqual(persisted['prompt_tokens'], 100)

    async def test_post_forget_delivery_completion_cannot_reintroduce_paraphrased_reply(self):
        store, context = await self.context()
        message = msg('inflight', '说说刚才的工作吧。')
        await store.push(message.ctx_key, {'user':'我是工程师','assistant':'我知道了。',
            'turn_id':'old','source_ids':['old'], 'ts':1})
        await context.accept_user(message)
        await context.build(message)
        # Delivery has started, then explicit forgetting arrives before ACK.
        store.suppress({'id':'forget-role','accounts':[{'platform':'desktop','owner_id':'local'}],
            'content':'用户是工程师','evidence':'我是工程师','fact_key':'occupation'})
        old_answer = '那些高精度机械的日常，说起来挺辛苦。'
        await context.append(message, old_answer)
        self.assertNotIn(old_answer, json.dumps(await store.get_recent(message.ctx_key), ensure_ascii=False),
            'An obsolete assistant paraphrase must not re-enter usable raw context after a forgetting revision')
        following = json.dumps(await context.build(msg('next')), ensure_ascii=False)
        self.assertNotIn(old_answer, following)

    async def test_image_prefix_uses_actual_vision_provider_and_window(self):
        store, context = await self.context()
        image_message = msg('image', '这张图你怎么看？')
        image_message.images = [{'url':'fixture-reference-only'}]
        image = {'type':'image_url','image_url':{'url':'data:image/png;base64,Zml4dHVyZQ=='}}
        await context.accept_user(image_message)
        first = await context.build(image_message, image_inputs=[image])
        context.capture_request(image_message, first)
        await context.append(image_message, '图里有一只猫。')
        context.image_context_window_tokens = 131072
        context.image_output_reserve_tokens = 700
        current = await context.build(msg('text-only', '现在换个话题，聊聊音乐。'))
        primary = ProviderCfg('primary','https://chat.example','fixture','main')
        vision = ProviderCfg('vision','https://vision.example','fixture','vision')
        gateway = LLMGateway(primary, vision=vision)
        self.addAsyncCleanup(gateway.aclose)
        gateway._call = AsyncMock(return_value='聊聊音乐。')
        await gateway.chat(current, chat_id='local')
        # Full stable prefixes still contain the real image. The actual vision
        # provider's policy must therefore be used until explicit compaction.
        self.assertIs(gateway._call.call_args.args[0], vision)
        self.assertEqual(context.last_budget['context_window'], 131072)
        self.assertEqual(context.last_budget['output_reserved'], 700)

    async def test_current_search_failure_never_uses_historical_tool_summary(self):
        gateway = LLMGateway(ProviderCfg('main','https://chat.example','fixture','main'))
        self.addAsyncCleanup(gateway.aclose)
        gateway._call = AsyncMock(side_effect=RuntimeError('review unavailable fixture'))
        old_summary = '旧闻：去年北方城市发生了暴雪并影响交通，相关安排随后已经恢复。'
        work = [
            {'role':'user','content':'去年北方的天气怎么样？'},
            {'role':'tool','tool_call_id':'old-call','content':json.dumps({'ok':True,'summary':old_summary})},
            {'role':'assistant','content':'那是去年的天气。'},
            {'role':'user','content':'现在南方的天气预报是什么？'},
            {'role':'system','content':SEARCH_RULES},
            {'role':'tool','tool_call_id':'new-call','content':json.dumps({'ok':True,'summary':'新讯','sources':[]})}]
        sources = [{'url':'https://example.com/current','title':'当前预报','snippet':'待核对的片段'}]
        answer = await gateway._review_search_answer(gateway.primary, work, '短', sources, 'local')
        self.assertNotIn(old_summary, answer,
            'Full-prefix sessions contain old tool messages; current evidence fallback must be turn-local')

    async def test_group_compacted_session_forget_uses_original_source_owner(self):
        store, context = await self.context()
        group_message = WxMessage('now', 'onebot', 'group', 'g_456', '123', '甲', 'bot', '请继续。', is_at_me=True)
        old_summary = '你有设计高精度机械产品的经历，大家刚才聊到了这段职业背景。'
        await store.set_session(group_message.ctx_key, {
            'fingerprint':context._session_fingerprint(),
            'messages':[{'role':'system','content':context.system_prompt},
                        {'role':'user','content':context._reference('summary', old_summary)}],
            'covered_source_ids':['onebot:group:g_456:123:old'], 'turn_count':1,
        }, expected_revision=store.revision)
        # Original raw evidence was compacted away. Only its source owner and a
        # lossy paraphrase remain; substring matching cannot prove it harmless.
        store.suppress({'id':'forget-role','accounts':[{'platform':'onebot','owner_id':'123'}],
            'content':'用户是工程师','evidence':'我是工程师','fact_key':'occupation'})
        following = json.dumps(await context.build(group_message), ensure_ascii=False)
        self.assertNotIn(old_summary, following,
            'Group prefixes must respect forgetting of an original source owner even after lossy compaction')

    async def test_oversize_input_is_completed_once_and_kept_unread_without_poisoning_next_turn(self):
        store = MemoryStore(); context = ContextManager(store)
        context.configure_session(context_window_tokens=4096, output_reserve_tokens=64)
        adapter = Adapter()
        class Gateway:
            calls = 0
            async def stream(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError('oversized originals must not be silently sent/truncated')
                yield ''
            async def aclose(self): pass
        gateway = Gateway()
        orch = Orchestrator(adapter, store, gateway, None, context, TriggerRouter(),
                            HumanLikeSender(adapter), typing_indicator=False)
        self.addAsyncCleanup(orch.close)
        original = '这是一段要保持完整的超长来话。'*1800
        message = msg('too-large', original)
        await orch.handle(message)
        await orch.handle(message)
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn('分', adapter.sent[0])
        raw = await store.get_recent(message.ctx_key)
        self.assertEqual(raw[0]['user'], original)
        self.assertTrue(raw[0].get('context_excluded'))
        following = json.dumps(await context.build(msg('small', '那先说这一句。')), ensure_ascii=False)
        self.assertNotIn(original, following)
        self.assertIn('尚未完整读取', following)
        self.assertIn('那先说这一句。', following)

    async def test_compaction_preserves_system_user_tool_roles_and_tool_call_id(self):
        summarize = AsyncMock(return_value='按来源区分后的摘要。')
        store, context = await self.context(compactor=NS(summarize=summarize))
        base = [
            {'role':'system','content':context.system_prompt},
            {'role':'system','content':'历史程序时间，不是用户陈述'},
            {'role':'user','content':'用户说自己喜欢散步'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'call-role','type':'function',
                'function':{'name':'web_search','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'call-role','content':'{"summary":"公开检索资料"}'}]
        result = await context._compress_session(msg(), [], base, set())
        self.assertIsNotNone(result)
        turns = summarize.call_args.args[0]
        self.assertEqual([turn.get('source_role') for turn in turns], ['system','user','assistant','tool'])
        self.assertEqual(turns[-1]['tool_call_id'], 'call-role')
        gateway = NS(complete_once=AsyncMock(return_value='有来源的摘要。'))
        await LLMCompactor(gateway).summarize(turns)
        prompt = gateway.complete_once.call_args.args[0]
        self.assertIn('工具资料，不是用户亲口陈述', prompt)
        self.assertIn('tool_call_id=call-role', prompt)
        self.assertIn('程序/时间资料，不是用户陈述', prompt)

    async def test_body_budget_compacts_old_media_without_changing_current_image(self):
        summarize = AsyncMock(return_value='旧图中是一只猫，已经回答过。')
        store, context = await self.context(compactor=NS(summarize=summarize))
        context.image_token_reserve = 1024
        old = msg('old-image', '看看旧图。'); old.images = [{'url':'old-ref'}]
        old_image = {'type':'image_url','image_url':{'url':'data:image/png;base64,'+'A'*20000}}
        await context.accept_user(old)
        await context.build(old, image_inputs=[old_image])
        await context.append(old, '旧图有一只猫。')
        current = msg('new-image', '这次看这张新图。'); current.images = [{'url':'new-ref'}]
        new_image = {'type':'image_url','image_url':{'url':'data:image/png;base64,TkVXX0lNQUdF'}}
        context.max_request_bytes = 6000  # Small synthetic byte limit, not a provider claim.
        messages = await context.build(current, image_inputs=[new_image])
        images = [part for item in messages if isinstance(item.get('content'),list)
                  for part in item['content'] if part.get('type') == 'image_url']
        self.assertEqual(images, [new_image])
        self.assertTrue(context.last_budget['compacted'])
        self.assertEqual(context._protocol_overflow(messages), '')
        summary_input = json.dumps(summarize.call_args.args[0], ensure_ascii=False)
        self.assertNotIn('A'*1000, summary_input)
        self.assertIn('历史图片输入', summary_input)

    async def test_new_input_cancels_compaction_before_old_summary_writes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def summarize(_): entered.set(); await release.wait(); return '旧一轮压缩摘要'
        store = MemoryStore(); context = ContextManager(store, compactor=NS(summarize=summarize))
        context.configure_session(context_window_tokens=4096, compact_ratio=.4, output_reserve_tokens=64)
        message = msg('first', '请继续。')
        for index in range(10):
            await store.push(message.ctx_key, {'user':str(index)+'历史资料。'*70,'assistant':'旧回应。',
                'turn_id':'old-'+str(index),'source_ids':['old-'+str(index)],'ts':index})
        adapter = Adapter()
        class Gateway:
            async def stream(self, *_args, **_kwargs): yield '不应发送旧稿'
            async def aclose(self): pass
        orch = Orchestrator(adapter, store, Gateway(), None, context, TriggerRouter(),
                            HumanLikeSender(adapter), typing_indicator=False)
        self.addAsyncCleanup(orch.close)
        task = asyncio.create_task(orch.handle(message))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            adapter.changed = True
            await asyncio.sleep(.16)
            self.assertTrue(task.done(),
                'Context build/compaction must be guarded by new-input cancellation, not hold the lock up to 60s')
            with self.assertRaises(ConversationChanged): await task
            self.assertFalse(await store.get_summary(message.ctx_key))
        finally:
            release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__': unittest.main()
