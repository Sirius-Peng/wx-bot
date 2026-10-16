"""Local, deterministic admission/queue/scheduler contracts; no paid APIs."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from src.core.reply_decision import ReplyDecider,ReplyDecision
from src.core.turn_coordinator import TurnCoordinator,ConversationStateError
from src.core.conversation import ConversationChanged,merge_messages
from src.core.types import WxMessage
from src.config_store import AppConfig,ConfigStore
from src.runtime import BotRuntime
from src.adapters.onebot import OneBotAdapter


def msg(text='最近在听一张新唱片',mid='1',platform='onebot',owner='123'):
    return WxMessage(mid,platform,'private','p_'+owner if platform=='onebot' else owner,owner,'fixture','bot',text,
                     context_token='test-token' if platform=='ilink' else None)


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_without_question_mark_does_not_call_model(self):
        gateway=SimpleNamespace(complete=AsyncMock(return_value='{"action":"context_only"}'))
        decider=ReplyDecider(gateway,bot_name='弗洛洛')
        for text in ('能不能陪陪我','明天几点出发','我很难过','我考上了','弗洛洛','不是那个，是这个','告诉我答案'):
            with self.subTest(text=text):
                decision=await decider.decide(msg(text))
                self.assertEqual(decision.action,'reply');self.assertTrue(decision.protected_ids)
        gateway.complete.assert_not_awaited()

    async def test_one_burst_is_judged_as_one_not_each_fragment(self):
        gateway=SimpleNamespace(complete=AsyncMock(return_value='{"action":"reply"}'))
        batch=merge_messages([msg('最近', '1'),msg('听了一张唱片', '2'),msg('很有意思。','3')])
        result=await ReplyDecider(gateway).decide(batch)
        self.assertEqual(result.action,'reply');gateway.complete.assert_awaited_once()
        self.assertIn('最近\\n听了一张唱片\\n很有意思',gateway.complete.call_args.args[0])

    async def test_bad_model_decision_never_discards_ambiguous_share(self):
        for output in ('', 'NO', '{"action":"drop"}', 'null'):
            gateway=SimpleNamespace(complete=AsyncMock(return_value=output))
            self.assertEqual((await ReplyDecider(gateway).decide(msg())).action,'reply')

    async def test_classifier_timeout_is_bounded_and_replies(self):
        async def blocked(*args,**kwargs):await asyncio.Event().wait()
        result=await asyncio.wait_for(ReplyDecider(SimpleNamespace(complete=blocked),timeout=.02).decide(msg()),.2)
        self.assertEqual(result.action,'reply')

    async def test_revisit_cannot_defer_twice(self):
        gateway=SimpleNamespace(complete=AsyncMock(return_value='{"action":"defer"}'))
        self.assertEqual((await ReplyDecider(gateway).decide(msg(),revisit=True)).action,'context_only')

    async def test_question_then_ack_is_not_discarded(self):
        result=await ReplyDecider().decide(merge_messages([msg('明天几点走','1'),msg('嗯','2')]))
        self.assertEqual(result.action,'reply')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.json';self.now=1000
        self.ledger=TurnCoordinator(self.path,clock=lambda:self.now)
    def tearDown(self):self.tmp.cleanup()

    def deferred(self):
        tid=self.ledger.begin(msg());self.ledger.decide(tid,ReplyDecision('defer','ordinary sharing'));return tid

    def test_duplicate_ingress_does_not_advance_revision(self):
        self.ledger.ingest(msg());before=json.loads(self.path.read_text())
        self.ledger.ingest(msg());self.assertEqual(json.loads(self.path.read_text()),before)

    def test_fixed_origin_and_revisit_60_to180seconds(self):
        tid=self.deferred();row=self.ledger.turns[tid]
        self.assertTrue(1060<=row['revisit_at']<=1180);self.assertEqual(row['message']['platform'],'onebot')
        self.assertFalse(self.ledger.due());self.now=1181;self.assertEqual(len(self.ledger.due()),1)
        self.ledger.reconsidered(tid);self.assertFalse(self.ledger.due())

    def test_expiry_and_disable_never_send(self):
        tid=self.deferred();self.now=1601;self.assertEqual(self.ledger.due(),[])
        self.assertEqual(self.ledger.turns[tid]['state'],'context_only')
        self.ledger.begin(msg(mid='2'));tid=self.ledger.begin(msg(mid='3'))
        self.ledger.decide(tid,ReplyDecision('defer','ordinary'));self.assertEqual(self.ledger.due(enabled=False),[])
        self.assertEqual(self.ledger.turns[tid]['state'],'context_only')

    def test_protected_request_not_given_ordinary_ttl(self):
        tid=self.ledger.begin(msg('帮我看一下'));self.ledger.decide(tid,ReplyDecision('defer','model error',['1']))
        self.now+=10000;self.ledger.due();self.assertEqual(self.ledger.turns[tid]['state'],'replying')

    def test_linked_new_input_supersedes_queue_without_switching_destination(self):
        self.ledger.identity=lambda platform,owner:'person'
        tid=self.deferred();self.ledger.ingest(msg('晚安','2','ilink','wx'))
        self.assertEqual(self.ledger.turns[tid]['state'],'superseded');self.assertEqual(self.ledger.turns[tid]['message']['platform'],'onebot')

    def test_revision_invalidates_old_unsent_work_but_not_submission(self):
        tid=self.ledger.begin(msg());self.ledger.ingest(msg('继续','2'))
        with self.assertRaises(ConversationChanged):self.ledger.check(tid)
        tid=self.ledger.begin(msg('继续','2'));attempt=self.ledger.reserve(tid,text='完整回复')
        self.ledger.ingest(msg('新话','3'));self.ledger.check(tid)
        restored=TurnCoordinator(self.path);self.assertEqual(restored.turns[tid]['state'],'uncertain')
        self.assertEqual(restored.turns[tid]['attempts'][0]['id'],attempt['id']);self.assertEqual(restored.turns[tid]['draft'],'完整回复')
        self.assertFalse(restored.due())

    def test_busy_refcount_for_simultaneous_linked_channels(self):
        self.ledger.identity=lambda p,o:'same'
        a=msg();b=msg(platform='ilink',owner='wx')
        self.ledger.enter(a);self.ledger.enter(b);self.ledger.leave(a)
        self.assertTrue(self.ledger.busy('same'));self.ledger.leave(b);self.assertFalse(self.ledger.busy('same'))

    def test_no_inline_media_or_raw_content_in_second_ledger(self):
        m=msg();m.images=[{'inline':'data:image/png;base64,SECRET'}];m.raw_content='[CQ:image,file=base64://SECRET]'
        self.ledger.begin(m);self.assertNotIn('SECRET',self.path.read_text())

    def test_failed_reservation_does_not_look_submitted(self):
        tid=self.ledger.begin(msg())
        with patch.object(self.ledger,'_save',side_effect=OSError('fixture disk')):
            with self.assertRaises(ConversationStateError):self.ledger.reserve(tid,text='old')
        self.assertEqual(self.ledger.turns[tid]['state'],'judging')
        self.assertEqual(self.ledger.turns[tid]['attempts'],[])

    def test_broken_state_is_preserved_and_optional_work_stops(self):
        self.path.write_text('{invalid',encoding='utf-8');restored=TurnCoordinator(self.path)
        self.assertTrue(restored.load_error);self.assertFalse(restored.due());self.assertEqual(self.path.read_text(),'{invalid')


class LateReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.store=ConfigStore(root/'config.json');cfg=AppConfig();cfg.llm.primary_key='fixture'
        cfg.bot.memory_enabled=False;cfg.bot.proactive_enabled=False
        cfg.bot.proactive_quiet_start=cfg.bot.proactive_quiet_end=0;self.store.save(cfg)
        self.rt=BotRuntime(self.store,root)
        self.adapter=OneBotAdapter('http://fixture');self.transport=AsyncMock(return_value='mid');self.adapter.send_text=self.transport
        self.orch=self.rt._build_orchestrator(self.adapter,'qq')
        await self.orch.llm.aclose()
        self.stream_count=0
        async def stream(*args,**kwargs):self.stream_count+=1;yield '那张唱片，有一段旋律还留在耳边。'
        class Gateway:pass
        self.orch.llm=Gateway();self.orch.llm.stream=stream
        self.orch.reply_decider=SimpleNamespace(decide=AsyncMock(return_value=ReplyDecision('reply','worth continuing')))
        self.rt.qq_adapter=self.adapter;self.rt.qq_orch=self.orch;self.adapter._running=True
        self.rt.running=True;self.rt.platforms_running={'qq'}
    async def asyncTearDown(self):
        await self.rt._stop();self.tmp.cleanup()

    async def defer(self):
        m=msg();self.rt._proactive_note_in(m);await self.orch.ctx.accept_user(m)
        tid=self.rt.conversations.begin(m);self.rt.conversations.decide(tid,ReplyDecision('defer','ordinary'))
        self.rt.conversations.turns[tid]['revisit_at']=0
        return tid

    async def test_late_reply_independent_of_random_contact_once_origin_and_clock(self):
        tid=await self.defer();await self.rt._late_tick();await self.rt._late_tick()
        self.transport.assert_awaited_once();self.assertEqual(self.transport.call_args.args[0],'p_123')
        self.assertEqual(self.rt.conversations.turns[tid]['state'],'accepted')
        row=self.rt.proactive.accounts['onebot:123'];self.assertTrue(row['awaiting']);self.assertGreater(row['last_out'],0)
        history=await self.orch.store.get_recent(msg().ctx_key)
        self.assertEqual(sum(bool(t['user']) for t in history),1);self.assertEqual(sum(bool(t['assistant']) for t in history),1)
        self.assertEqual(self.stream_count,1)

    async def test_quiet_hours_do_not_consume_reconsideration(self):
        tid=await self.defer();self.rt.proactive._quiet=lambda now:True
        await self.rt._late_tick();self.transport.assert_not_awaited();self.assertFalse(self.rt.conversations.turns[tid]['revisited'])

    async def test_switch_off_consumes_queue_not_a_fake_reply(self):
        tid=await self.defer();self.rt.cfg.bot.late_reply_enabled=False;await self.rt._late_tick()
        self.transport.assert_not_awaited();self.assertEqual(self.rt.conversations.turns[tid]['state'],'context_only')

    async def test_cross_channel_input_cancels_during_generation(self):
        self.rt._ensure_memory().link_accounts('wx','123');tid=await self.defer();entered=asyncio.Event()
        async def slow(*args,**kwargs):entered.set();await asyncio.Event().wait();yield 'old'
        self.orch.llm.stream=slow;task=asyncio.create_task(self.rt._late_tick());await asyncio.wait_for(entered.wait(),.3)
        self.rt._proactive_note_in(msg('嗯','2','ilink','wx'));await asyncio.wait_for(task,.3)
        self.transport.assert_not_awaited();self.assertEqual(self.rt.conversations.turns[tid]['state'],'superseded')

    async def test_uncertain_late_submission_never_replays_after_restart(self):
        tid=await self.defer();self.transport.side_effect=TimeoutError('fixture')
        await self.rt._late_tick();await self.rt._late_tick()
        self.transport.assert_awaited_once();self.assertEqual(self.rt.conversations.turns[tid]['state'],'uncertain')
        restored=TurnCoordinator(self.rt.data_dir/'conversation-state.json');self.assertFalse(restored.due())

    async def test_forgetting_during_generation_invalidates_unsubmitted_context(self):
        memory=self.rt._ensure_memory()
        item=memory.add_manual('user','123','preference','用户喜欢钢琴',['唱片'],platform='onebot')
        tid=await self.defer();entered=asyncio.Event()
        async def slow(*args,**kwargs):entered.set();await asyncio.Event().wait();yield 'old memory'
        self.orch.llm.stream=slow;task=asyncio.create_task(self.rt._late_tick())
        await asyncio.wait_for(entered.wait(),.3);memory.delete(item['id']);await asyncio.wait_for(task,.3)
        self.transport.assert_not_awaited();self.assertEqual(self.rt.conversations.turns[tid]['state'],'superseded')


if __name__=='__main__':unittest.main()
