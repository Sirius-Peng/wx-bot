"""Independent review probes for persona-space transactions (temporary fixtures)."""
import asyncio
import copy
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from src.config_store import AppConfig, ConfigStore
from src.runtime import BotRuntime
from src.adapters.onebot import OneBotAdapter
from src.core.types import WxMessage
from src.core.reply_decision import ReplyDecision


class LateSubmissionSpaceAudit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=ConfigStore(self.root/'config.json');cfg=AppConfig()
        cfg.llm.primary_key='fixture';cfg.llm.search_enabled=False;cfg.bot.memory_enabled=False
        cfg.bot.proactive_enabled=False;cfg.bot.proactive_quiet_start=cfg.bot.proactive_quiet_end=0
        self.store.save(cfg);self.rt=BotRuntime(self.store,self.root)
        self.adapter=OneBotAdapter('http://fixture');self.transport=AsyncMock(return_value='accepted')
        self.adapter.send_text=self.transport
        self.orch=self.rt._build_orchestrator(self.adapter,'qq')
        await self.orch.llm.aclose()
        class Gateway:
            async def stream(self,*a,**kw):yield '原空间的稍后承接。'
            async def aclose(self):pass
        self.orch.llm=Gateway()
        self.orch.reply_decider=type('Decider',(),{})()
        self.orch.reply_decider.decide=AsyncMock(return_value=ReplyDecision('reply','可以接回'))
        self.rt.qq_adapter=self.adapter;self.rt.qq_orch=self.orch
        self.adapter._running=True;self.rt.running=True;self.rt.platforms_running={'qq'}

    async def asyncTearDown(self):
        await self.rt._stop();self.tmp.cleanup()

    async def test_submitted_late_reply_retains_original_history_and_proactive_tag(self):
        original_space=self.rt.persona_spaces.active_key
        m=WxMessage('late-source','onebot','private','p_123','123','测试用户','bot','今天听了一张新唱片',persona_space=original_space)
        self.rt._proactive_note_in(m);await self.orch.ctx.accept_user(m)
        tid=self.rt.conversations.begin(m)
        self.rt.conversations.decide(tid,ReplyDecision('defer','普通分享'))
        self.rt.conversations.turns[tid]['revisit_at']=0
        old_store=self.orch.store;entered=asyncio.Event();release=asyncio.Event()
        async def submit(*a,**kw):entered.set();await release.wait();return 'accepted'
        self.transport.side_effect=submit
        task=asyncio.create_task(self.rt._late_tick())
        try:
            await asyncio.wait_for(entered.wait(),1)
            cfg=copy.deepcopy(self.rt.cfg);cfg.bot.persona_relationship='trusted'
            self.store.save(cfg);self.rt.apply_config(cfg)
            new_store=self.orch.store;release.set();await asyncio.wait_for(task,1)
            self.transport.assert_awaited_once()
            self.assertEqual(self.rt.conversations.turns[tid]['state'],'accepted')
            self.assertEqual(await new_store.get_recent(m.ctx_key),[],
                             'already submitted old-space late reply leaked into new persona history')
            old=await old_store.get_recent(m.ctx_key)
            self.assertTrue(any(t.get('assistant')=='原空间的稍后承接。' for t in old))
            completed=[e for e in self.rt.proactive.accounts['onebot:123']['history'] if e['role']=='assistant']
            self.assertEqual(completed[-1]['persona_space'],original_space)
        finally:
            release.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_submitted_random_contact_retains_original_history_and_proactive_tag(self):
        original_space=self.rt.persona_spaces.active_key
        m=WxMessage('proactive-source','onebot','private','p_123','123','测试用户','bot','今天听了一张新唱片',persona_space=original_space)
        self.rt.cfg.bot.proactive_enabled=True
        self.rt.cfg.bot.proactive_min_hours=self.rt.cfg.bot.proactive_max_hours=.25
        now=[time.time()];planner=self.rt.proactive
        planner.clock=lambda:now[0];planner.uniform=lambda a,b:a;planner.started=now[0]
        self.rt._proactive_note_in(m);planner.set_contact('onebot:123',True)
        planner.generate=AsyncMock(return_value='原空间的主动联系。')
        await planner.tick();now[0]+=901
        old_store=self.orch.store;entered=asyncio.Event();release=asyncio.Event()
        async def submit(*a,**kw):entered.set();await release.wait();return 'accepted'
        self.transport.side_effect=submit
        task=asyncio.create_task(planner.tick())
        try:
            await asyncio.wait_for(entered.wait(),1)
            row=planner.accounts['onebot:123'];attempt_id=row['attempts'][-1]['id']
            cfg=copy.deepcopy(self.rt.cfg);cfg.bot.persona_relationship='trusted'
            self.store.save(cfg);self.rt.apply_config(cfg)
            new_store=self.orch.store;release.set();await asyncio.wait_for(task,1)
            self.transport.assert_awaited_once()
            self.assertEqual(row['attempts'][-1]['id'],attempt_id)
            self.assertEqual(row['attempts'][-1]['status'],'accepted')
            self.assertEqual(await new_store.get_recent(m.ctx_key),[],
                             'already submitted old-space random contact leaked into new persona history')
            old=await old_store.get_recent(m.ctx_key)
            self.assertTrue(any(t.get('assistant')=='原空间的主动联系。' for t in old))
            completed=[e for e in row['history'] if e['role']=='assistant']
            self.assertEqual(completed[-1]['persona_space'],original_space)
            self.assertEqual(planner.timeline([row]),[])
        finally:
            release.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_switched_qq_context_uses_original_filename_and_reopens_after_restart(self):
        original_name=self.orch.store.path.name
        cfg=copy.deepcopy(self.rt.cfg);cfg.bot.persona_relationship='trusted'
        self.store.save(cfg);self.rt.apply_config(cfg)
        m=WxMessage('new-space','onebot','private','p_123','123','测试用户','bot','新空间应在重启后仍然记得',
                    persona_space=self.rt.persona_spaces.active_key)
        await self.orch.ctx.accept_user(m);await self.orch.ctx.append(m,'完整的真实答复')
        expected_path=self.orch.store.path
        self.assertEqual(expected_path.name,original_name,'switching must not change context-qq.json into context-onebot.json')
        await self.rt._stop()
        reloaded=BotRuntime(self.store,self.root);adapter=OneBotAdapter('http://fixture')
        try:
            orch=reloaded._build_orchestrator(adapter,'qq');reloaded.qq_orch=orch;reloaded.qq_adapter=adapter
            self.assertEqual(orch.store.path,expected_path)
            history=await orch.store.get_recent(m.ctx_key)
            self.assertTrue(any(t.get('user')==m.content and t.get('assistant')=='完整的真实答复' for t in history))
        finally:await reloaded._stop()

    async def test_inactive_old_inbox_after_restart_uses_original_context_filename(self):
        original_space=self.rt.persona_spaces.active_key;original_path=self.orch.store.path
        m=WxMessage('old-inbox','onebot','private','p_123','123','测试用户','bot','原人格离线待处理的来话',
                    persona_space=original_space)
        cfg=copy.deepcopy(self.rt.cfg);cfg.bot.persona_relationship='trusted'
        self.store.save(cfg);self.rt.apply_config(cfg);await self.rt._stop()
        reloaded=BotRuntime(self.store,self.root);adapter=OneBotAdapter('http://fixture')
        try:
            orch=reloaded._build_orchestrator(adapter,'qq');reloaded.qq_orch=orch;reloaded.qq_adapter=adapter
            self.assertNotIn(original_space,orch._persona_contexts)
            await orch.handle(m)
            old_context=orch._persona_contexts[original_space]
            self.assertEqual(old_context.store.path,original_path)
            self.assertEqual(await orch.store.get_recent(m.ctx_key),[])
            old=await old_context.store.get_recent(m.ctx_key)
            self.assertEqual(old[0]['user'],m.content);self.assertEqual(old[0]['assistant'],'')
        finally:await reloaded._stop()

    async def test_stop_closes_all_cached_memory_and_context_resources(self):
        old_memory=self.rt._ensure_memory();old_context=self.orch.ctx
        cfg=copy.deepcopy(self.rt.cfg);cfg.bot.persona_relationship='trusted'
        self.store.save(cfg);self.rt.apply_config(cfg)
        current_memory=self.rt._ensure_memory();current_context=self.orch.ctx
        self.assertIsNot(old_memory,current_memory);self.assertIsNot(old_context,current_context)
        for memory in (old_memory,current_memory):
            memory.learner.close=AsyncMock(wraps=memory.learner.close)
            memory.close_usage=AsyncMock(wraps=memory.close_usage)
            memory.llm.aclose=AsyncMock(wraps=memory.llm.aclose)
        for context in (old_context,current_context):context.close=AsyncMock(wraps=context.close)
        await self.rt._stop()
        for memory in (old_memory,current_memory):
            memory.learner.close.assert_awaited_once();memory.close_usage.assert_awaited_once()
            memory.llm.aclose.assert_awaited_once()
            self.assertFalse(memory.learner._jobs);self.assertIsNone(memory.learner._runner)
        for context in (old_context,current_context):context.close.assert_awaited_once()
        self.assertIsNone(self.rt._memory_usage_task)


if __name__=='__main__':unittest.main()
