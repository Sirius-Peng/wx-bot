"""Identity linking is optional for WeChat-only and QQ-only proactive contacts."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
import httpx
from src.config_store import AppConfig,ConfigStore
from src.core.memory import PersonaMemory
from src.core.proactive import ProactivePlanner
from src.core.types import WxMessage
from src.runtime import BotRuntime
from src.adapters.ilink import ILinkAdapter
from src.adapters.onebot import OneBotAdapter


def incoming(platform,owner='123',mid='1',text='今天听到一首曲子'):
    return WxMessage(mid,platform,'private','p_'+owner if platform=='onebot' else owner,
                     owner,'单端用户','bot',text,context_token='fixture-context' if platform=='ilink' else None)


class SingleChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg=AppConfig();self.cfg.bot.proactive_enabled=True
        self.cfg.bot.proactive_min_hours=self.cfg.bot.proactive_max_hours=.25
        self.cfg.bot.proactive_quiet_start=self.cfg.bot.proactive_quiet_end=0
        self.memory=PersonaMemory(self.root/'memory.json');self.now=[1792036800.]
        self.generate=AsyncMock(return_value='那首曲子，听完了吗？');self.deliver=AsyncMock()
        self.p=ProactivePlanner(self.root/'proactive.json',self.memory.identity_key,lambda:self.cfg,
            lambda rows,now:max(rows,key=lambda r:r['last_in']),self.generate,self.deliver,
            clock=lambda:self.now[0],uniform=lambda a,b:a)
    async def asyncTearDown(self):
        await self.p.close();self.tmp.cleanup()
    async def advance(self):
        await self.p.tick();self.now[0]+=901;await self.p.tick()
    async def standalone(self,platform):
        self.p.note_in(incoming(platform));self.p.set_contact(platform+':123',True)
        await self.advance()
        self.assertEqual(self.memory.identities,[]);self.deliver.assert_awaited_once()
        self.assertEqual(self.deliver.call_args.args[0]['platform'],platform)
        self.assertEqual(list(self.p.plans),[platform+':123'])
        self.assertEqual(len(self.p.status()['contacts'][0]['accounts']),1)
        self.now[0]+=86400;await self.p.tick();self.deliver.assert_awaited_once()
    async def test_wechat_only_sends_without_identity_link(self):await self.standalone('ilink')
    async def test_qq_only_sends_without_identity_link(self):await self.standalone('onebot')
    async def test_same_owner_string_is_not_cross_platform_identity(self):
        self.p.note_in(incoming('ilink',text='微信端独立内容'));self.p.note_in(incoming('onebot',text='QQ端独立内容'))
        self.assertEqual(set(self.p.groups()),{'ilink:123','onebot:123'})
        self.assertEqual(self.p.shared_context(incoming('ilink')),'')
        self.assertEqual(self.p.shared_context(incoming('onebot')),'')
    async def test_selecting_one_unlinked_contact_does_not_enable_other(self):
        self.p.note_in(incoming('ilink'));self.p.note_in(incoming('onebot'))
        self.p.set_contact('onebot:123',True);await self.advance()
        self.deliver.assert_awaited_once();self.assertEqual(self.deliver.call_args.args[0]['platform'],'onebot')
        self.assertFalse(self.p.accounts['ilink:123']['enabled']);self.assertFalse(self.p.accounts['ilink:123']['awaiting'])
    async def test_two_unlinked_contacts_have_independent_plans(self):
        for platform in ('ilink','onebot'):
            self.p.note_in(incoming(platform));self.p.set_contact(platform+':123',True)
        await self.advance();self.assertEqual(self.deliver.await_count,2)
        self.assertEqual(len(self.p.plans),2)
    async def test_prompt_reflects_actual_single_channel_without_other_account_assumption(self):
        for platform,channel,other in [('ilink','微信','QQ'),('onebot','QQ','微信')]:
            with self.subTest(platform=platform):
                self.p.note_in(incoming(platform))
                prompt=json.dumps(self.p.prompt(self.p.rows_for(platform,'123'),self.now[0]),ensure_ascii=False)
                self.assertIn('仅通过'+channel,prompt);self.assertNotIn(other,prompt)
                self.assertIn('不要求对方关联账号',prompt);self.assertIn('当前时间',prompt)
    async def test_unlinked_contact_api_accepts_both_single_channels(self):
        with patch.dict(os.environ,{'WXBOT_DATA_DIR':str(self.root/'api-data'),'WXBOT_ADMIN_TOKEN':'single-test','WXBOT_HOST':'127.0.0.1'}):
            import src.server as server
            server=importlib.reload(server)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',headers={'Authorization':'Bearer single-test'}) as client:
                for platform in ('ilink','onebot'):
                    server.runtime.proactive.note_in(incoming(platform))
                    result=await client.patch('/api/proactive/contact',json={'id':platform+':123','enabled':True})
                    self.assertEqual(result.status_code,200)
                    contact=next(c for c in result.json()['contacts'] if c['id']==platform+':123')
                    self.assertTrue(contact['enabled']);self.assertEqual(len(contact['accounts']),1)
                self.assertEqual(server.runtime._ensure_memory().identities,[])
    async def test_single_enabled_runtime_channel_uses_real_transport_contract(self):
        for platform in ('ilink','onebot'):
            with self.subTest(platform=platform):
                data=self.root/platform;data.mkdir()
                store=ConfigStore(data/'config.json');cfg=self.cfg
                cfg.adapter.wechat_enabled=platform=='ilink';cfg.adapter.qq_enabled=platform=='onebot';store.save(cfg)
                rt=BotRuntime(store,data)
                adapter=ILinkAdapter(str(data/'ilink')) if platform=='ilink' else OneBotAdapter('http://fixture',bot_qq='bot')
                if platform=='ilink':adapter._notify=AsyncMock()
                orch=rt._build_orchestrator(adapter,'wechat' if platform=='ilink' else 'qq')
                if platform=='ilink':rt.wechat_adapter=adapter;rt.wechat_orch=orch
                else:rt.qq_adapter=adapter;rt.qq_orch=orch
                rt.running=True;rt.platforms_running={'wechat' if platform=='ilink' else 'qq'};adapter._running=True
                p=rt.proactive;p.clock=lambda:self.now[0];p.uniform=lambda a,b:a;p.started=self.now[0]
                p.note_in(incoming(platform));p.set_contact(platform+':123',True);p.generate=self.generate
                wire=AsyncMock(return_value={'ret':0,'message_id':'accepted'} if platform=='ilink' else {'status':'ok','data':{'message_id':'accepted'}})
                try:
                    await p.tick();self.now[0]+=901
                    with patch('src.runtime.time.time',side_effect=lambda:self.now[0]),patch.object(adapter,'_post' if platform=='ilink' else '_call',wire):await p.tick()
                    wire.assert_awaited_once();self.assertEqual(rt._ensure_memory().identities,[])
                    self.assertEqual(p.status()['contacts'][0]['last_status'],'accepted')
                    self.assertEqual(len(p.plans),1)
                finally:await rt._stop()

if __name__=='__main__':unittest.main()
