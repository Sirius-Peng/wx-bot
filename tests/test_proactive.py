"""Deterministic cross-platform proactive scheduling; no live accounts/model calls."""
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(os.environ.get('WXBOT_TEST_ROOT', Path(__file__).resolve().parents[1])); sys.path.insert(0, str(ROOT))
import httpx
from src.config_store import AppConfig, ConfigStore
from src.core.proactive import ProactivePlanner
from src.core.memory import PersonaMemory
from src.core.types import WxMessage
from src.core.context import ContextManager
from src.store_memory import MemoryStore
from src.runtime import BotRuntime
from src.adapters.ilink import ILinkAdapter
from src.adapters.onebot import OneBotAdapter


def msg(platform='ilink', owner='wx-user', mid='1', text='最近在听钢琴曲'):
    return WxMessage(mid, platform, 'private', 'p_'+owner if platform == 'onebot' else owner,
                     owner, '测试联系人', 'bot', text, context_token='fixture-context' if platform=='ilink' else None)


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'proactive.json'
        self.cfg = AppConfig(); self.cfg.bot.proactive_enabled = True
        self.cfg.bot.proactive_quiet_start = self.cfg.bot.proactive_quiet_end = 0
        self.cfg.bot.proactive_min_hours = 1; self.cfg.bot.proactive_max_hours = 2
        self.now = 1792036800.0
        self.memory = PersonaMemory(Path(self.tmp.name)/'memory.json')
        self.memory.link_accounts('wx-user','123')
        self.generate = AsyncMock(return_value='那首曲子，后来听完了吗？')
        self.deliver = AsyncMock()
        self.planner = self.make()
    def make(self):
        return ProactivePlanner(self.path, self.memory.identity_key, lambda:self.cfg,
            lambda rows,now:max(rows,key=lambda r:r['last_in']), self.generate, self.deliver,
            clock=lambda:self.now, uniform=lambda a,b:(a+b)/2)
    async def asyncTearDown(self):
        await self.planner.close(); self.tmp.cleanup()
    def inbound(self, platform='ilink', owner='wx-user', mid='1', text='最近在听钢琴曲'):
        self.planner.note_in(msg(platform,owner,mid,text))
    def allow(self):self.planner.set_contact('ilink:wx-user',True)
    async def due(self):
        await self.planner.tick(); self.now += 5500
    async def test_linked_accounts_have_one_plan_and_latest_channel(self):
        self.inbound(); self.now+=30; self.inbound('onebot','123'); self.allow()
        await self.due(); self.assertEqual(len(self.planner.plans),1)
        await asyncio.gather(self.planner.tick(),self.planner.tick())
        self.deliver.assert_awaited_once(); self.assertEqual(self.deliver.call_args.args[0]['platform'],'onebot')
        self.assertTrue(all(r['awaiting'] for r in self.planner.accounts.values()))
        self.now+=86400; await self.planner.tick(); self.deliver.assert_awaited_once()
    async def test_global_and_contact_opt_in_are_both_required(self):
        self.inbound(); await self.due(); await self.planner.tick(); self.generate.assert_not_awaited()
        self.allow(); self.cfg.bot.proactive_enabled=False
        await self.due(); await self.planner.tick(); self.generate.assert_not_awaited()
    async def test_either_channel_resets_shared_idle_clock(self):
        self.inbound(); self.allow(); await self.due()
        self.inbound('onebot','123','new','我又回来了')
        await self.planner.tick(); self.deliver.assert_not_awaited()
        self.assertGreater(next(iter(self.planner.plans.values()))['due'],self.now)
    async def test_ordinary_outbound_also_resets_clock(self):
        self.inbound(); self.allow(); await self.due()
        self.planner.note_out('ilink','wx-user','嗯，我在。'); await self.planner.tick()
        self.deliver.assert_not_awaited(); self.assertGreater(next(iter(self.planner.plans.values()))['due'],self.now)
    async def test_inbound_during_generation_cancels_without_bubble(self):
        self.inbound(); self.inbound('onebot','123'); self.allow(); await self.due()
        started=asyncio.Event(); cancelled=asyncio.Event()
        async def slow(*a):
            started.set()
            try: await asyncio.Event().wait()
            finally:cancelled.set()
        self.planner.generate=slow; task=asyncio.create_task(self.planner.tick())
        await started.wait(); self.inbound('onebot','123','2','我还没说完'); await task
        self.assertTrue(cancelled.is_set()); self.deliver.assert_not_awaited()
    async def test_disable_during_generation_cancels(self):
        self.inbound(); self.allow(); await self.due(); started=asyncio.Event()
        async def slow(*a):started.set(); await asyncio.Event().wait()
        self.planner.generate=slow;task=asyncio.create_task(self.planner.tick());await started.wait()
        self.cfg.bot.proactive_enabled=False;await task;self.deliver.assert_not_awaited()
    async def test_link_change_during_generation_cancels(self):
        self.inbound();self.inbound('onebot','123');self.allow();await self.due();started=asyncio.Event()
        async def slow(*a):started.set();await asyncio.Event().wait()
        self.planner.generate=slow;task=asyncio.create_task(self.planner.tick());await started.wait()
        self.memory.unlink_accounts(self.memory.identities[0]['id']);await task;self.deliver.assert_not_awaited()
    async def test_shared_daily_budget_across_channel_switch(self):
        self.cfg.bot.proactive_daily_limit=1
        self.inbound();self.inbound('onebot','123');self.allow();await self.due();await self.planner.tick()
        self.now+=60;self.inbound('onebot','123','2');await self.due();await self.planner.tick()
        self.deliver.assert_awaited_once()
    async def test_quiet_hours_avoid_generation_and_opening_burst(self):
        self.inbound();self.allow();await self.due()
        from src.core.reality import local_datetime
        hour=local_datetime(self.cfg.llm.context_timezone,self.now).hour
        self.cfg.bot.proactive_quiet_start=hour;self.cfg.bot.proactive_quiet_end=(hour+2)%24
        # Updated configuration starts a fresh plan; force it due inside quiet window.
        await self.planner.tick();next(iter(self.planner.plans.values()))['due']=self.now
        await self.planner.tick();self.generate.assert_not_awaited()
        self.assertGreater(next(iter(self.planner.plans.values()))['due'],self.now)
    async def test_daytime_quiet_window_and_same_hour_off(self):
        from src.core.reality import local_datetime
        hour=local_datetime(self.cfg.llm.context_timezone,self.now).hour
        self.cfg.bot.proactive_quiet_start=hour;self.cfg.bot.proactive_quiet_end=hour
        self.assertFalse(self.planner._quiet(self.now))
        self.cfg.bot.proactive_quiet_end=(hour+1)%24;self.assertTrue(self.planner._quiet(self.now))
    async def test_uncertain_delivery_never_fails_over_or_retries_after_restart(self):
        self.inbound();self.inbound('onebot','123');self.allow();await self.due()
        self.deliver.side_effect=TimeoutError('fixture')
        await self.planner.tick();self.deliver.assert_awaited_once()
        self.assertEqual(self.planner.status()['contacts'][0]['last_status'],'uncertain')
        self.planner=self.make();self.now+=10000;await self.planner.tick();self.now+=10000;await self.planner.tick()
        self.deliver.assert_awaited_once()
    async def test_attempt_is_durable_before_transport(self):
        self.inbound();self.allow();await self.due()
        async def deliver(row,text,aid):
            state=json.loads(self.path.read_text());r=state['accounts']['ilink:wx-user']
            self.assertTrue(r['awaiting']);self.assertEqual(r['attempts'][-1],{'id':aid,'ts':self.now,'status':'sending'})
        self.planner.deliver=deliver;await self.planner.tick()
        self.assertEqual(self.planner.status()['contacts'][0]['last_status'],'accepted')
    async def test_restart_does_not_catch_up_expired_plan(self):
        self.inbound();self.allow();await self.due();self.planner=self.make()
        await self.planner.tick();self.deliver.assert_not_awaited()
        self.assertGreater(next(iter(self.planner.plans.values()))['due'],self.now)
    async def test_future_plan_survives_restart(self):
        self.inbound();self.allow();await self.planner.tick();due=next(iter(self.planner.plans.values()))['due']
        self.now+=20;self.planner=self.make();await self.planner.tick()
        self.assertEqual(next(iter(self.planner.plans.values()))['due'],due)
    async def test_linking_existing_independent_plans_is_conservative(self):
        self.memory.unlink_accounts(self.memory.identities[0]['id']);self.inbound();self.inbound('onebot','123')
        self.allow();self.planner.set_contact('onebot:123',True);await self.planner.tick()
        self.assertEqual(len(self.planner.plans),2)
        self.memory.link_accounts('wx-user','123');await self.planner.tick()
        self.assertEqual(len(self.planner.plans),1);self.deliver.assert_not_awaited()
    async def test_unlink_keeps_unanswered_attempt_blocked_on_both(self):
        self.inbound();self.inbound('onebot','123');self.allow();await self.due();await self.planner.tick()
        self.memory.unlink_accounts(self.memory.identities[0]['id']);await self.due();await self.planner.tick()
        self.deliver.assert_awaited_once()
    async def test_cancelled_model_task_does_not_kill_scheduler(self):
        self.inbound();self.allow();await self.due()
        async def cancelled(*a):raise asyncio.CancelledError()
        self.planner.generate=cancelled
        await self.planner.tick();self.deliver.assert_not_awaited()
        self.assertGreater(next(iter(self.planner.plans.values()))['due'],self.now)
    async def test_partial_state_corruption_is_not_silently_reinitialized(self):
        self.inbound();state=json.loads(self.path.read_text());state['accounts']['ilink:wx-user']['last_in']='bad'
        self.path.write_text(json.dumps(state));before=self.path.read_bytes();self.planner=self.make()
        self.assertTrue(self.planner.load_error);await self.planner.tick()
        self.assertEqual(self.path.read_bytes(),before);self.deliver.assert_not_awaited()

    async def test_stop_request_is_shared_and_requires_explicit_reenable(self):
        self.inbound();self.inbound('onebot','123');self.allow()
        self.inbound('onebot','123','stop','不要主动给我发消息')
        self.assertTrue(all(r['muted'] for r in self.planner.accounts.values()))
        self.inbound(mid='after',text='你好');await self.due();await self.planner.tick();self.generate.assert_not_awaited()
        self.allow();self.assertFalse(any(r['muted'] for r in self.planner.accounts.values()))
    async def test_duplicate_ingress_does_not_unblock_or_rearm(self):
        self.inbound();self.allow();await self.due();await self.planner.tick()
        before=copy.deepcopy(self.planner.accounts);self.inbound()
        self.assertEqual(self.planner.accounts,before)
    async def test_group_self_and_invalid_chat_are_never_targets(self):
        m=msg();m.chat_type='group';self.planner.note_in(m)
        m=msg();m.sender_id=m.bot_id;self.planner.note_in(m)
        m=msg();m.chat_id='somebody-else';self.planner.note_in(m)
        self.assertEqual(self.planner.accounts,{})
    async def test_skip_and_generation_failure_send_no_error_and_replan(self):
        self.inbound();self.allow();await self.due();self.generate.return_value='<SKIP>'
        await self.planner.tick();self.deliver.assert_not_awaited();self.assertFalse(self.planner.accounts['ilink:wx-user']['awaiting'])
        self.now+=5500;self.generate.side_effect=RuntimeError('fixture');await self.planner.tick()
        self.deliver.assert_not_awaited();self.assertGreater(next(iter(self.planner.plans.values()))['due'],self.now)
    async def test_unavailable_channel_defers_without_model_call(self):
        self.inbound();self.allow();await self.due();self.planner.choose_channel=lambda *a:None
        await self.planner.tick();self.generate.assert_not_awaited()
    async def test_no_identity_or_group_context_leak(self):
        self.inbound();self.inbound('onebot','123','2','两端同一个话题');self.inbound('onebot','999','other','另一个人的秘密')
        text=self.planner.shared_context(msg());self.assertIn('两端同一个话题',text);self.assertNotIn('另一个人的秘密',text)
        group=msg();group.chat_type='group';self.assertEqual(self.planner.shared_context(group),'')
    async def test_prompt_has_clock_timeline_and_no_fake_new_user(self):
        self.inbound();self.inbound('onebot','123','2','昨天的音乐');self.allow();await self.due();await self.planner.tick()
        prompt=json.dumps(self.generate.call_args.args[1],ensure_ascii=False)
        for text in ('当前时间','昨天的音乐','微信','QQ','不催回复','<SKIP>'):self.assertIn(text,prompt)
    async def test_corrupt_state_is_preserved_and_sends_nothing(self):
        self.path.write_text('{broken',encoding='utf-8');self.planner=self.make();self.inbound();await self.planner.tick()
        self.assertTrue(self.planner.load_error);self.assertEqual(self.path.read_text(),'{broken');self.generate.assert_not_awaited()
    async def test_write_failure_blocks_future_sends(self):
        self.inbound();self.allow();await self.due()
        with patch('src.core.proactive.os.replace',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.inbound(mid='write-fail')
        self.assertTrue(self.planner.load_error);await self.planner.tick();self.generate.assert_not_awaited()
    async def test_single_loop_stops_and_cancels_generation(self):
        self.inbound();self.allow();await self.due();started=asyncio.Event();cancelled=asyncio.Event()
        async def slow(*a):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.planner.generate=slow
        # start() intentionally defers expired plans; keep a due plan current.
        self.planner.start();task=self.planner.task;self.planner.start();self.assertIs(self.planner.task,task)
        await asyncio.sleep(.02);next(iter(self.planner.plans.values()))['due']=self.now
        task.cancel();await self.planner.close()
        self.planner.started=self.now-10000
        task=asyncio.create_task(self.planner.tick());await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(cancelled.is_set());self.deliver.assert_not_awaited()
    async def test_daily_budget_uses_configured_timezone(self):
        self.cfg.llm.context_timezone='UTC';self.cfg.bot.proactive_daily_limit=1
        self.inbound();self.allow();await self.due();await self.planner.tick()
        self.now+=86400;self.inbound(mid='tomorrow');await self.due();await self.planner.tick()
        self.assertEqual(self.deliver.await_count,2)
    async def test_disabled_contact_keeps_other_people_isolated(self):
        self.inbound();self.allow();self.inbound('onebot','999','other');await self.due();await self.planner.tick()
        self.deliver.assert_awaited_once();self.assertEqual(self.deliver.call_args.args[0]['owner_id'],'wx-user')


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=ConfigStore(self.root/'config.json');cfg=AppConfig();cfg.bot.proactive_enabled=True;cfg.bot.proactive_quiet_start=cfg.bot.proactive_quiet_end=0
        self.store.save(cfg);self.rt=BotRuntime(self.store,self.root)
        self.wx=ILinkAdapter(str(self.root/'ilink'));self.wx._notify=AsyncMock();self.qq=OneBotAdapter('http://fixture',bot_qq='bot')
        self.rt.wechat_adapter=self.wx;self.rt.qq_adapter=self.qq
        self.rt.wechat_orch=self.rt._build_orchestrator(self.wx,'wechat');self.rt.qq_orch=self.rt._build_orchestrator(self.qq,'qq')
        self.rt._ensure_memory().link_accounts('wx-user','123')
        self.rt.running=True;self.rt.platforms_running={'wechat','qq'};self.wx._running=self.qq._running=True
    async def asyncTearDown(self):
        await self.rt._stop();self.tmp.cleanup()
    def records(self):
        self.rt.proactive.note_in(msg());self.rt.proactive.note_in(msg('onebot','123','2'))
        return self.rt.proactive.rows_for('ilink','wx-user')
    async def test_latest_channel_and_wechat_freshness(self):
        rows=self.records();now=self.rt.proactive.clock()
        wx=next(r for r in rows if r['platform']=='ilink');qq=next(r for r in rows if r['platform']=='onebot')
        wx['last_in']=now+1;self.assertIs(self.rt._proactive_channel(rows,now),wx)
        wx['last_in']=now-86401;self.assertIs(self.rt._proactive_channel(rows,now),qq)
        self.rt.platforms_running={'wechat'};self.assertIsNone(self.rt._proactive_channel(rows,now))
    async def test_pending_on_either_channel_prevents_push(self):
        rows=self.records();self.wx._pending=[{'from_user_id':'wx-user'}]
        self.assertIsNone(self.rt._proactive_channel(rows,self.rt.proactive.clock()))
        self.wx._pending=[];self.qq._bursts.pending={'fixture':[(msg('onebot','123'),0)]}
        self.assertIsNone(self.rt._proactive_channel(rows,self.rt.proactive.clock()))
        self.qq._bursts.pending={}
        typing_key=msg('onebot','123').ctx_key+':123'
        self.qq._bursts.typing_hints[typing_key]={'until':self.rt.proactive.clock()+4}
        self.assertIsNone(self.rt._proactive_channel(rows,self.rt.proactive.clock()))
        self.qq._bursts.typing_hints={}
        key=msg().ctx_key;lock=asyncio.Lock();await lock.acquire();self.rt.wechat_orch._locks[key]=lock
        self.assertIsNone(self.rt._proactive_channel(rows,self.rt.proactive.clock()));lock.release()
    async def test_qq_raw_ingress_observed_before_debounce(self):
        self.qq.configure_burst(2,10)
        await self.qq.handle_event({'post_type':'message','message_type':'private','user_id':123,'self_id':999,'message_id':'raw', 'message':[{'type':'text','data':{'text':'先等我说完'}}]})
        self.assertIn('onebot:123',self.rt.proactive.accounts)
        self.assertEqual(self.rt.proactive.accounts['onebot:123']['history'][-1]['text'],'先等我说完')
        self.assertEqual(self.rt.log.stats()['total_in'],0)
    async def test_wechat_raw_ingress_observed_before_dispatch(self):
        async def post(endpoint,payload):
            if endpoint.endswith('getupdates'):
                self.wx._running=False
                return {'ret':0,'get_updates_buf':'fixture-cursor','msgs':[{'message_type':1,'message_id':'raw-wx','from_user_id':'wx-user','context_token':'fixture-context','item_list':[{'type':1,'text_item':{'text':'还没说完'}}]}]}
        with patch.object(self.wx,'_post',side_effect=post):await self.wx._receive_loop()
        self.assertIn('ilink:wx-user',self.rt.proactive.accounts);self.assertEqual(self.rt.log.stats()['total_in'],0)
        self.wx._pending=[]
    async def test_wechat_proactive_payload_uses_fixed_id_and_correct_token(self):
        with patch.object(self.wx,'_post',new=AsyncMock(return_value={'ret':0,'message_id':'accepted'})) as post:
            await self.wx.send_proactive('wx-user','音乐还没结束。','fixed-id','fixture-context')
            await self.wx.send_proactive('wx-user','不应替换正文','fixed-id','other-token')
        post.assert_awaited_once();body=post.call_args.args[1]['msg']
        self.assertEqual(body['to_user_id'],'wx-user');self.assertEqual(body['context_token'],'fixture-context')
        self.assertEqual(body['item_list'][0]['text_item']['text'],'音乐还没结束。')
        self.assertIn('proactive:fixed-id',self.wx._outbox)
    async def test_normal_accepted_send_not_retried_if_activity_disk_fails(self):
        self.records()
        with patch.object(self.qq,'_call',new=AsyncMock(return_value={'status':'ok','data':{'message_id':'accepted'}})) as call, patch.object(self.rt.proactive,'note_out',side_effect=OSError('fixture')):
            result=await self.qq.send_text('p_123','完整回复。')
        self.assertTrue(result);call.assert_awaited_once()
    async def test_activity_failure_does_not_break_raw_ingress(self):
        self.qq.on_activity(lambda msg:(_ for _ in ()).throw(OSError('fixture')))
        self.qq.note_activity(msg('onebot','123'))
    async def test_generate_uses_persona_shared_memory_and_no_web_search(self):
        rows=self.records();gateway=SimpleNamespace(chat=AsyncMock(return_value='听听这段旋律？'),aclose=AsyncMock())
        with patch('src.runtime.LLMGateway',return_value=gateway) as create:
            text=await self.rt._proactive_generate(rows[0],self.rt.proactive.prompt(rows,self.rt.proactive.clock()))
        self.assertEqual(text,'听听这段旋律？');prompt=json.dumps(gateway.chat.call_args.args[0],ensure_ascii=False)
        self.assertIn('弗洛洛',prompt);self.assertIn('当前时间',prompt);self.assertNotIn('web_search',create.call_args.kwargs);gateway.aclose.assert_awaited_once()
    async def test_actual_qq_delivery_once_and_assistant_only_context(self):
        rows=self.records();row=next(r for r in rows if r['platform']=='onebot')
        row['last_in']+=1;key=self.rt._ensure_memory().identity_key(row['platform'],row['owner_id'])
        self.rt.proactive.plans[key]={'signature':self.rt.proactive._signature(rows),'due':self.rt.proactive.clock()}
        with patch.object(self.qq,'_call',new=AsyncMock(return_value={'status':'ok','data':{'message_id':'accepted'}})) as call:
            await self.rt._proactive_deliver(row,'那首曲子，听完了吗？','fixture-attempt')
        call.assert_awaited_once();self.assertEqual(call.call_args.args[0],'send_private_msg')
        self.assertEqual(call.call_args.args[1]['user_id'],123)
        turns=await self.rt.qq_orch.store.get_recent('ctx:onebot:private:p_123')
        self.assertEqual(turns[-1]['user'],'');self.assertTrue(turns[-1]['proactive'])
        history=self.rt.qq_orch.ctx._fit_budget(turns,5000)
        self.assertEqual(history[1:],[{'role':'assistant','content':'那首曲子，听完了吗？'}])
        self.assertEqual(history[0]['role'],'system')
        self.assertIn('助手发送时间',history[0]['content'])
        self.assertEqual(turns[-1]['assistant_at'],turns[-1]['ts'])
        self.assertEqual(self.rt.log.stats()['total_out'],1)
    async def test_planner_to_qq_transport_and_wechat_followup_end_to_end(self):
        import time
        now = [time.time()]
        p = self.rt.proactive
        p.clock = lambda: now[0]
        p.uniform = lambda a,b:a
        p.started = now[0]
        self.rt.cfg.bot.proactive_min_hours = self.rt.cfg.bot.proactive_max_hours = .25
        self.records(); now[0] += 1; p.note_in(msg('onebot','123','latest-qq'))
        p.set_contact('ilink:wx-user',True)
        generated = AsyncMock(return_value='那段旋律，后来听完了吗？')
        with patch.object(self.rt,'_proactive_generate',generated):
            # Planner holds the bound callback established at construction.
            p.generate=generated
            await p.tick();now[0]+=901
            with patch('src.runtime.time.time',side_effect=lambda:now[0]), patch.object(self.qq,'_call',new=AsyncMock(return_value={'status':'ok','data':{'message_id':'accepted'}})) as call:
                await p.tick();await p.tick()
        call.assert_awaited_once();generated.assert_awaited_once()
        self.assertEqual(len(p.plans),1)
        self.assertTrue(all(r['awaiting'] for r in p.accounts.values()))
        self.assertIn('那段旋律',p.shared_context(msg(text='刚才的音乐')))
        history=await self.rt.qq_orch.store.get_recent('ctx:onebot:private:p_123')
        self.assertEqual(history[-1]['assistant'],'那段旋律，后来听完了吗？')
        p.note_in(msg(mid='wx-followup',text='听完了，很喜欢'));await p.tick()
        self.assertFalse(any(r['awaiting'] for r in p.accounts.values()))
        self.assertGreater(next(iter(p.plans.values()))['due'],now[0])

    async def test_shared_context_is_actually_injected_into_normal_reply(self):
        self.records();self.rt.proactive.note_out('onebot','p_123','跨端继续同一段音乐')
        prompt=await self.rt.wechat_orch.ctx.build(msg(text='刚才你说什么'))
        self.assertIn('跨端继续同一段音乐',json.dumps(prompt,ensure_ascii=False))


class ConfigTests(unittest.TestCase):
    def test_defaults_opt_in_and_numeric_normalization(self):
        self.assertFalse(AppConfig().bot.proactive_enabled)
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(Path(d)/'config.json')
            cfg=store._from_dict({'bot':{'proactive_enabled':'true','proactive_min_hours':float('nan'),'proactive_max_hours':0,'proactive_quiet_end':99,'proactive_daily_limit':True}})
            self.assertFalse(cfg.bot.proactive_enabled);self.assertEqual(cfg.bot.proactive_min_hours,3)
            self.assertEqual(cfg.bot.proactive_max_hours,3);self.assertEqual(cfg.bot.proactive_quiet_end,23);self.assertEqual(cfg.bot.proactive_daily_limit,2)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'proactive-test','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.server=importlib.reload(server)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',headers={'Authorization':'Bearer proactive-test'})
    async def asyncTearDown(self):
        await self.client.aclose();self.env.stop();self.tmp.cleanup()
    async def test_proactive_section_hot_save_is_isolated(self):
        result=await self.client.patch('/api/config/section/proactive',json={'proactive_enabled':True,'proactive_min_hours':2,'proactive_max_hours':6})
        self.assertEqual(result.status_code,200);self.assertFalse(result.json()['requires_restart'])
        cfg=self.server.config_store.load();self.assertEqual(cfg.bot.proactive_max_hours,6);self.assertEqual(cfg.bot.persona,AppConfig().bot.persona)
        self.assertEqual((await self.client.patch('/api/config/section/persona',json={'proactive_enabled':False})).status_code,400)
        self.assertEqual((await self.client.patch('/api/config/section/proactive',json={'proactive_enabled':'true'})).status_code,400)
    async def test_contact_api_known_private_only_and_identity_shared(self):
        p=self.server.runtime.proactive;p.note_in(msg());p.note_in(msg('onebot','123'))
        self.server.runtime._ensure_memory().link_accounts('wx-user','123')
        result=await self.client.patch('/api/proactive/contact',json={'id':'ilink:wx-user','enabled':True})
        self.assertEqual(result.status_code,200);self.assertEqual(len(result.json()['contacts']),1)
        self.assertTrue(all(r['enabled'] for r in p.accounts.values()))
        for invalid in ({'id':'not-known','enabled':True},{'id':'ilink:wx-user','enabled':'true'}):
            self.assertIn((await self.client.patch('/api/proactive/contact',json=invalid)).status_code,(400,404))
    async def test_status_excludes_context_tokens_and_conversation(self):
        self.server.runtime.proactive.note_in(msg(text='private-history'))
        result=await self.client.get('/api/proactive');self.assertEqual(result.status_code,200)
        self.assertNotIn('fixture-context',result.text);self.assertNotIn('private-history',result.text)
        self.client.headers.clear();self.assertEqual((await self.client.get('/api/proactive')).status_code,401)
    async def test_older_whole_config_client_preserves_new_settings(self):
        from dataclasses import asdict
        cfg=self.server.config_store.load();cfg.bot.proactive_enabled=True;cfg.bot.proactive_min_hours=7
        incoming=asdict(cfg)
        for key in list(incoming['bot']):
            if key.startswith('proactive_'):del incoming['bot'][key]
        merged=self.server._merge_config(cfg,incoming)
        self.assertTrue(merged.bot.proactive_enabled);self.assertEqual(merged.bot.proactive_min_hours,7)


if __name__=='__main__':unittest.main()
