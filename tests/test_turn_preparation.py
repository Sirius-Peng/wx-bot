"""Quiet-window work is disposable: one cancellable cloud request, no mutation, typing or early delivery."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from src.adapters.desktop import DesktopAdapter
from src.adapters.ilink import ILinkAdapter
from src.adapters.onebot import OneBotAdapter
from src.core.context import ContextManager
from src.core.conversation import MessageBuffer, merge_messages, members
from src.core.memory import PersonaMemory
from src.core.reply_decision import ReplyDecider
from src.core.sender import HumanLikeSender
from src.core.trigger import TriggerRouter
from src.core.turn_preparation import TurnPreparation, prior_turns, recall_query
from src.core.types import WxMessage
from src.orchestrator import Orchestrator
from src.runtime import BotRuntime
from src.store_memory import MemoryStore


def msg(text='今天路上看到一棵树', mid='1', owner='local'):
    return WxMessage(mid, 'desktop', 'private', owner, owner, '我', 'bot', text)


def confident():
    return {'action': 'reply', 'probabilities': {'reply': .98, 'defer': .01, 'context_only': .01},
            'calibrated': True, 'abstain': False, 'reason': 'confident'}


class FakeMemory:
    enabled = inject = supports_platform_scope = True
    _generation = 0
    _revisions = {}
    def __init__(self):
        self.calls = []
        self.used = []
        self.item = SimpleNamespace(id='green', content='绿茶', pinned=False,
                                    deadline=lambda: time.time()+86400, expires_at=0)
    def identity_key(self, platform, owner): return platform+':'+owner
    def build_context(self, **kwargs):
        self.calls.append(kwargs)
        return '相关参考：绿茶'
    def recall(self, *args, **kwargs): return [self.item]
    def record_usage(self, ids): self.used.extend(ids)


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = DesktopAdapter(self.tmp.name)
        self.store = MemoryStore()
        self.cloud = SimpleNamespace(complete_once=AsyncMock(return_value='R'))
        self.memory = FakeMemory()
        self.orch = Orchestrator(self.adapter, self.store, None, None, ContextManager(self.store),
                                 None, None, memory=self.memory,
                                 reply_decider=ReplyDecider(self.cloud))
        self.prep = self.orch.preparation
        self.prep.debounce = 0

    async def asyncTearDown(self):
        await self.orch.close()
        self.tmp.cleanup()

    async def prepared(self, message):
        self.prep.accept(message)
        entry = self.prep.entries[self.prep.key(message)]
        await entry.task
        return entry

    async def test_one_cloud_but_no_typing_delivery_admission_or_usage_during_preparation(self):
        await self.prepared(msg())
        self.cloud.complete_once.assert_awaited_once()
        self.assertEqual(self.adapter.events, [])
        self.assertFalse(self.store._ctx)
        self.assertEqual(self.memory.used, [])
        self.assertFalse(any(call.get('mark_used', False) for call in self.memory.calls))

    async def test_exact_state_one_cloud_request_and_one_shot_handoff(self):
        message = msg()
        await self.prepared(message)
        with self.prep.bind(message):
            result = await self.orch.reply_decider.decide(message)
        self.assertEqual(result.source, 'cloud')
        self.assertTrue(self.orch.reply_decider.last_status['prepared'])
        self.cloud.complete_once.assert_awaited_once()
        with self.prep.bind(message):
            exhausted = await self.orch.reply_decider.decide(message)
        self.assertEqual(self.cloud.complete_once.await_count, 1)
        self.assertEqual(exhausted.source, 'conservative')
        self.assertEqual(self.orch.reply_decider.last_status['reason'], 'burst_budget_exhausted')

    async def test_failed_preparation_does_not_issue_another_paid_request(self):
        self.cloud.complete_once.side_effect = TimeoutError()
        message = msg()
        await self.prepared(message)
        with self.prep.bind(message):
            result = await self.orch.reply_decider.decide(message)
        self.assertEqual((result.action, result.source), ('reply', 'conservative'))
        self.cloud.complete_once.assert_awaited_once()

    async def test_final_orchestrator_reuses_preparation_and_delivers_one_bubble(self):
        class LLM:
            async def stream(self, messages, **kwargs):
                yield '听到了。'
        self.orch.llm = LLM()
        self.orch.router = TriggerRouter()
        self.orch.sender = HumanLikeSender(self.adapter)
        self.orch.typing_indicator = False
        message = msg()
        await self.prepared(message)
        self.assertEqual(self.adapter.events, [])
        await self.orch.handle(message)
        self.cloud.complete_once.assert_awaited_once()
        self.assertEqual([e['type'] for e in self.adapter.events], ['completed'])
        self.assertEqual(self.adapter.events[0]['text'], '听到了。')
        self.assertEqual(len(self.memory.calls), 1)
        self.assertEqual(self.memory.used, ['green'])

    async def test_protected_input_never_runs_cloud_in_quiet_window(self):
        await self.prepared(msg('请帮我看看这张照片'))
        self.cloud.complete_once.assert_not_awaited()

    async def test_new_input_cancels_old_request_and_preserves_full_batch(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def infer(prompt, **kwargs):
            if '补充第二段' not in prompt:
                started.set()
                try: await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return 'R'
        self.cloud.complete_once.side_effect = infer
        a, b = msg('第一段'), msg('补充第二段', '2')
        self.prep.accept(a)
        old = self.prep.entries[self.prep.key(a)].task
        await started.wait()
        merged = merge_messages([a, b])
        await self.prepared(merged)
        await asyncio.wait_for(cancelled.wait(), .3)
        with self.assertRaises(asyncio.CancelledError): await old
        with self.prep.bind(merged): result = await self.orch.reply_decider.decide(merged)
        self.assertEqual(self.cloud.complete_once.await_count, 1)
        self.assertEqual((result.action,result.source), ('reply','conservative'))
        self.assertEqual(self.orch.reply_decider.last_status['reason'], 'burst_budget_exhausted')
        self.assertEqual(merged.content, '第一段\n补充第二段')
        self.assertEqual(len(members(merged)), 2)

    async def test_real_forgetting_invalidates_memory_and_cloud_result(self):
        memory = PersonaMemory(Path(self.tmp.name)/'memory.json')
        try:
            item = memory.add_manual('user', 'local', 'preference', '喜欢绿茶', ['绿茶'], platform='desktop')
            self.orch.memory = memory
            message = msg('今天又喝了绿茶')
            await self.prepared(message)
            with self.prep.bind(message):
                self.assertIn('绿茶', self.prep.memory_context(message, message.content)[0])
                memory.delete(item['id'])
                self.assertIsNone(self.prep.memory_context(message, message.content))
                result = await self.orch.reply_decider.decide(message)
            self.assertEqual(self.cloud.complete_once.await_count, 1)
            self.assertEqual((result.action,result.source), ('reply','conservative'))
        finally:
            await memory.learner.close()
            await memory.close_usage()

    async def test_persona_memory_and_provider_changes_reject_prepared_result(self):
        self.cloud.primary = SimpleNamespace(model='model-v1', api_key='fixture', base_url='https://example.invalid')
        message = msg()
        for change in (lambda: setattr(self.orch.ctx, 'system_prompt', '新的人格'),
                       lambda: setattr(self.memory, '_generation', self.memory._generation+1),
                       lambda: setattr(self.cloud.primary, 'model', 'model-v2'),
                       lambda: setattr(self.cloud.primary, 'api_key', 'fixture-new')):
            await self.prepared(message)
            with self.prep.bind(message):
                change()
                self.assertIsNone(self.prep.memory_context(message, message.content))
                before = self.cloud.complete_once.await_count
                result = await self.orch.reply_decider.decide(message)
                self.assertEqual(self.cloud.complete_once.await_count, before)
                self.assertEqual((result.action,result.source), ('reply','conservative'))
                self.assertEqual(self.orch.reply_decider.last_status['reason'], 'burst_budget_exhausted')

    async def test_ids_image_order_other_owner_cannot_reuse_preparation(self):
        message = msg()
        await self.prepared(message)
        for changed in (replace(message, msg_id='different'), replace(message, sender_id='other'),
                        replace(message, images=[{'url':'one'}, {'url':'two'}])):
            with self.prep.bind(changed): self.assertIsNone(self.prep.current.get())
        a = replace(message, images=[{'url':'one'}, {'url':'two'}])
        b = replace(message, images=[{'url':'two'}, {'url':'one'}])
        self.assertNotEqual(self.prep.signature(a), self.prep.signature(b))

    async def test_changed_actual_history_cannot_reuse_model_result(self):
        message = msg()
        await self.prepared(message)
        with self.prep.bind(message):
            result = await self.orch.reply_decider.decide(message, [{'user':'新历史'}])
        self.assertEqual(self.cloud.complete_once.await_count, 1)
        self.assertEqual((result.action,result.source), ('reply','conservative'))
        self.assertEqual(self.orch.reply_decider.last_status['reason'], 'burst_budget_exhausted')

    async def test_accepted_current_user_excluded_so_prepared_state_matches(self):
        message = msg()
        await self.store.push(message.ctx_key, {'user':'真正的上一轮', 'assistant':'答复', 'turn_id':'old'})
        await self.prepared(message)
        await self.orch.ctx.accept_user(message)
        recent = prior_turns(message, await self.store.get_recent(message.ctx_key))
        self.assertEqual([t['user'] for t in recent], ['真正的上一轮'])
        with self.prep.bind(message): await self.orch.reply_decider.decide(message, recent)
        self.cloud.complete_once.assert_awaited_once()

    async def test_entries_bounded_and_expire(self):
        await self.prep.close()
        now = [0.]
        self.prep = self.orch.preparation = TurnPreparation(self.orch, ttl=.01, limit=2,
                                                          clock=lambda:now[0], debounce=0)
        for owner in ('a', 'b', 'c'): await self.prepared(msg(owner=owner))
        self.assertEqual(len(self.prep.entries), 2)
        now[0] = 1.
        with self.prep.bind(msg(owner='c')): self.assertIsNone(self.prep.current.get())
        self.assertFalse(self.prep.entries)

    async def test_memory_lifecycle_deadline_expires_before_cache_ttl(self):
        self.memory.item.deadline = lambda: time.time()-.001
        message = msg()
        await self.prepared(message)
        with self.prep.bind(message): self.assertIsNone(self.prep.memory_context(message, message.content))

    async def test_followup_query_uses_previous_real_user(self):
        message = msg('后来呢')
        await self.store.push(message.ctx_key, {'user':'之前的绿茶事情', 'turn_id':'old'})
        await self.prepared(message)
        await self.orch.ctx.accept_user(message)
        recent = prior_turns(message, await self.store.get_recent(message.ctx_key))
        self.assertEqual(recall_query(message, recent), '后来呢 之前的绿茶事情')
        with self.prep.bind(message): self.assertIsNotNone(self.prep.memory_context(message, recall_query(message, recent)))

    async def test_forget_callback_cancels_preparation(self):
        await self.prepared(msg())
        runtime = SimpleNamespace(memory=self.memory, _memory_cache={}, _orchestrators=lambda:[self.orch],
                                  data_dir=Path(self.tmp.name),persona_spaces=SimpleNamespace(context_paths=lambda:[]),
                                  _purge_suppressed_spaces=lambda accounts:None)
        BotRuntime._on_memory_forget(runtime, {'id':'barrier','accounts':[], 'content':'绿茶'})
        self.assertFalse(self.prep.entries)

    async def test_close_cancels_inflight(self):
        started = asyncio.Event()
        async def infer(*args, **kwargs):
            started.set()
            await asyncio.sleep(30)
        self.cloud.complete_once.side_effect = infer
        self.prep.accept(msg())
        task = self.prep.entries[self.prep.key(msg())].task
        await started.wait()
        await self.prep.close()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.prep.entries)

    async def test_inflight_is_shared_without_duplicate_request(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def infer(*args, **kwargs):
            started.set()
            await release.wait()
            return 'R'
        self.cloud.complete_once.side_effect = infer
        message = msg()
        self.prep.accept(message)
        await started.wait()
        async def final():
            with self.prep.bind(message): return await self.orch.reply_decider.decide(message)
        task = asyncio.create_task(final())
        await asyncio.sleep(.01)
        self.cloud.complete_once.assert_awaited_once()
        release.set()
        self.assertEqual((await task).action, 'reply')
        self.cloud.complete_once.assert_awaited_once()

    async def test_quiet_window_hides_decision_latency(self):
        async def infer(*args, **kwargs):
            await asyncio.sleep(.12)
            return 'R'
        self.cloud.complete_once.side_effect = infer
        self.prep.debounce = .02
        message = msg()
        self.prep.accept(message)
        await asyncio.sleep(.2)
        started = time.monotonic()
        with self.prep.bind(message): result = await self.orch.reply_decider.decide(message)
        self.assertEqual(result.source, 'cloud')
        self.assertLess(time.monotonic()-started, .04)
        self.cloud.complete_once.assert_awaited_once()

    async def test_debounce_coalesces_fragments_before_spending(self):
        self.prep.debounce = .06
        originals = []
        for i in range(3):
            originals.append(msg('散步时看到的树'+str(i), str(i)))
            self.prep.accept(merge_messages(originals))
            await asyncio.sleep(.015)
        entry = self.prep.entries[self.prep.key(originals[0])]
        await entry.task
        self.cloud.complete_once.assert_awaited_once()
        prompt = self.cloud.complete_once.call_args.args[0]
        for original in originals: self.assertIn(original.content, prompt)

    async def test_prepared_timeout_retains_single_attempt(self):
        async def infer(*args, **kwargs): await asyncio.sleep(30)
        self.cloud.complete_once.side_effect = infer
        self.orch.reply_decider.timeout = .04
        message = msg()
        self.prep.accept(message)
        await asyncio.sleep(.01)
        started = time.monotonic()
        with self.prep.bind(message): result = await self.orch.reply_decider.decide(message)
        self.assertEqual((result.action, result.source), ('reply', 'conservative'))
        self.assertLess(time.monotonic()-started, .08)
        self.cloud.complete_once.assert_awaited_once()


class PendingSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_callback_observes_full_durable_pending_text(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = DesktopAdapter(root)
            adapter.configure_burst(2,10)
            snapshots = []
            adapter.on_turn_preparation(snapshots.append)
            await adapter.submit('第一段', '1')
            await adapter.submit('接着第二段', '2')
            self.assertEqual(snapshots[-1].content, '第一段\n接着第二段')
            self.assertEqual([m.msg_id for m in members(snapshots[-1])], ['1','2'])
            again = DesktopAdapter(root)
            again.configure_burst(2,10)
            self.assertEqual(len(next(iter(again.buffer.pending.values()))), 2)

    async def test_onebot_snapshot_preserves_parts_ids_and_image_order(self):
        adapter = OneBotAdapter.__new__(OneBotAdapter)
        adapter._bursts = MessageBuffer(AsyncMock())
        a = replace(msg('图一'), platform='onebot', images=[{'url':'one'}])
        b = replace(msg('图二', '2'), platform='onebot', images=[{'url':'two'}])
        adapter._bursts.submit(a)
        adapter._bursts.submit(b)
        snapshot = adapter.pending_turn(b)
        self.assertEqual(snapshot.images, [{'url':'one'}, {'url':'two'}])
        self.assertEqual([m.msg_id for m in members(snapshot)], ['1','2'])
        self.assertEqual([p['index'] for p in snapshot.content_parts if p['type']=='image'], [0,1])

    async def test_ilink_snapshot_isolated_by_conversation_and_sender(self):
        adapter = ILinkAdapter.__new__(ILinkAdapter)
        a = replace(msg('一'), platform='ilink')
        b = replace(msg('二', '2'), platform='ilink', images=[{'url':'image'}])
        foreign = replace(msg('不属于这里', '3', 'other'), platform='ilink')
        adapter._pending = [{'message':a}, {'message':foreign}, {'message':b}]
        adapter._normalize = lambda raw:raw['message']
        snapshot = adapter.pending_turn(b)
        self.assertEqual(snapshot.content, '一\n二')
        self.assertEqual([m.msg_id for m in members(snapshot)], ['1','2'])
        self.assertEqual(snapshot.images, b.images)


if __name__ == '__main__': unittest.main()
