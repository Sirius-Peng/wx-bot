"""Integration regressions found during review of natural reply scheduling."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.config_store import AppConfig, ConfigStore
from src.core.context import ContextManager
from src.core.reply_decision import ReplyDecider, ReplyDecision
from src.core.sender import HumanLikeSender, DeliveryError
from src.core.trigger import TriggerRouter
from src.core.turn_coordinator import TurnCoordinator
from src.core.types import WxMessage
from src.orchestrator import Orchestrator
from src.runtime import BotRuntime
from src.store_memory import MemoryStore


def message(mid='1', text='帮我推荐一首曲子', platform='onebot', owner='u'):
    return WxMessage(mid, platform, 'private', 'p_'+owner if platform=='onebot' else owner,
                     owner, 'user', 'bot', text, context_token='fixture-token' if platform=='ilink' else None)


class FailingAdapter:
    platform='onebot'
    def __init__(self):self.calls=0
    def on_message(self, handler):self.handler=handler
    async def send_text(self,*args,**kwargs):
        self.calls+=1
        raise TimeoutError('accepted remotely, local response lost')


class ChatModel:
    def __init__(self):self.calls=0
    async def stream(self,*args,**kwargs):
        self.calls+=1
        yield '这首曲子，也许合适。'


class NaturalReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_unknown_delivery_retry_does_not_regenerate_or_resubmit(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter=FailingAdapter();model=ChatModel();store=MemoryStore()
            orch=Orchestrator(adapter,store,model,None,ContextManager(store),TriggerRouter(),
                              HumanLikeSender(adapter),coordinator=TurnCoordinator(Path(directory)/'turns.json'),
                              reply_decider=ReplyDecider(),typing_indicator=False)
            with self.assertRaises(DeliveryError):await orch.handle(message())
            self.assertEqual(adapter.calls,1)
            await orch.handle(message())
            self.assertEqual(adapter.calls,1,'QQ timeout is an uncertain submission, not permission to send again')
            self.assertEqual(model.calls,1,'uncertain reply body must not be replaced with a newly generated answer')

    async def test_ledger_keeps_references_not_inline_image_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            secret='aW1hZ2UtYnl0ZXMtdGhhdC1iZWxvbmctb25seS10by1yYXctaW5ib3g='
            msg=message(text='帮我看看这张图')
            msg.images=[{'inline':secret}]
            msg.raw_content='[CQ:image,file=base64://'+secret+']'
            path=Path(directory)/'turns.json'
            ledger=TurnCoordinator(path);ledger.begin(msg)
            self.assertNotIn(secret,path.read_text(encoding='utf-8'))

    async def test_restarting_during_unsent_reconsideration_recovers_work(self):
        with tempfile.TemporaryDirectory() as directory:
            now=[1000.0];path=Path(directory)/'turns.json'
            ledger=TurnCoordinator(path,clock=lambda:now[0]);tid=ledger.begin(message(text='窗外下雨了'))
            ledger.decide(tid,ReplyDecision('defer','sharing'));now[0]=1200
            ledger.reconsidered(tid)
            # Process exits before the classifier or network submission returns.
            reopened=TurnCoordinator(path,clock=lambda:now[0])
            self.assertEqual([r['id'] for r in reopened.due()],[tid])

    async def test_restarting_during_unsent_late_generation_recovers_work(self):
        with tempfile.TemporaryDirectory() as directory:
            now=[1000.0];path=Path(directory)/'turns.json'
            ledger=TurnCoordinator(path,clock=lambda:now[0]);tid=ledger.begin(message(text='窗外下雨了'))
            ledger.decide(tid,ReplyDecision('defer','sharing'));now[0]=1200
            ledger.reconsidered(tid);ledger.decide(tid,ReplyDecision('reply','worth following up'))
            reopened=TurnCoordinator(path,clock=lambda:now[0])
            self.assertEqual([r['id'] for r in reopened.due()],[tid])

    async def test_quoted_no_reply_does_not_silence_actual_question(self):
        gateway=SimpleNamespace(complete=AsyncMock(return_value='{"action":"context_only"}'))
        decision=await ReplyDecider(gateway).decide(message(text='她跟我说“不用回”，我应该怎么理解'))
        self.assertEqual(decision.action,'reply');gateway.complete.assert_not_awaited()

    async def test_explicit_ending_with_explanation_is_not_queued_again(self):
        gateway=SimpleNamespace(complete=AsyncMock(return_value='{"action":"defer"}'))
        for text in ('晚安，我先睡了','不用回，我只是记一下今天的想法','今天先这样吧，我去忙了'):
            with self.subTest(text=text):
                decision=await ReplyDecider(gateway).decide(message(text=text))
                self.assertEqual(decision.action,'context_only')
        gateway.complete.assert_not_awaited()

    async def test_linked_other_channel_activity_cancels_unsent_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ConfigStore(Path(directory)/'config.json');cfg=AppConfig()
            cfg.bot.proactive_quiet_start=cfg.bot.proactive_quiet_end=0;store.save(cfg)
            runtime=BotRuntime(store,Path(directory));runtime.running=True
            memory=runtime._ensure_memory();memory.link_accounts('wx','qq')
            original=message('late','窗外下雨了','ilink','wx');other=message('new','嗯','onebot','qq')
            runtime._proactive_note_in(original);runtime._proactive_note_in(message('before','你好','onebot','qq'))
            tid=runtime.conversations.begin(original);runtime.conversations.decide(tid,ReplyDecision('defer','sharing'))
            runtime.conversations.turns[tid]['revisit_at']=0
            started=asyncio.Event();release=asyncio.Event();delivered=AsyncMock()
            class SlowModel:
                async def stream(self,*a,**kw):
                    started.set();await release.wait();yield '雨声，像很轻的前奏。'
            wxadapter=SimpleNamespace(_running=True,_pending=[],_jobs={},send_proactive=delivered)
            wxstore=MemoryStore()
            wxorch=SimpleNamespace(_locks={},adapter=wxadapter,store=wxstore,
                ctx=ContextManager(wxstore),reply_decider=SimpleNamespace(decide=AsyncMock(return_value=ReplyDecision('reply','sharing'))),
                llm=SlowModel(),sender=HumanLikeSender(wxadapter))
            runtime.wechat_adapter=wxadapter;runtime.wechat_orch=wxorch
            task=asyncio.create_task(runtime._late_tick())
            try:
                await asyncio.wait_for(started.wait(),1)
                # This other-end acknowledgement can finish before a 50ms polling cycle.
                runtime._proactive_note_in(other)
                release.set();await asyncio.wait_for(task,1)
                delivered.assert_not_awaited()
            finally:
                release.set()
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                await memory.llm.aclose()


if __name__=='__main__':unittest.main()
