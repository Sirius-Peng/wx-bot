"""Offline regression coverage for the project review fixes."""
from __future__ import annotations
from file_privacy import assert_private_file
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('WXBOT_TEST_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from src.config_store import AppConfig, ConfigStore
from src.runtime import BotRuntime
from src.core.memory import PersonaMemory
from src.core.llm import LLMGateway, ProviderCfg
from src.core.sender import split_by_semantics
from src.core.types import WxMessage
from src.adapters.ilink import ILinkAdapter
from src.orchestrator import Orchestrator
from src.core.context import ContextManager
from src.core.trigger import TriggerRouter
from src.store_memory import MemoryStore


class Adapter:
    platform = 'fake'
    def on_message(self, handler): self.handler = handler
    async def start(self): pass
    async def stop(self): pass
    async def send_text(self, *args, **kwargs): return 'sent'


class Model:
    def __init__(self): self.messages = []; self.calls = 0
    async def complete(self, *args, **kwargs):
        self.calls += 1
        return '[{"category":"fact","content":"用户是后端工程师","keywords":["后端"]}]'
    async def stream(self, messages, **kwargs):
        self.messages = messages
        yield '测试回复'


class Sender:
    async def stream_send(self, chat_id, stream, **kwargs):
        return ''.join([x async for x in stream])
    async def send(self, *args, **kwargs): return ['sent']


def message(mid='1'):
    return WxMessage(mid, 'onebot', 'private', 'p_u', 'u', '用户', 'bot', '你好')


def raw_message(mid='1'):
    return {'message_type': 1, 'message_id': mid, 'from_user_id': 'u',
            'context_token': 'token-'+mid,
            'item_list': [{'type':1, 'text_item':{'text':'你好'}}]}


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.store = ConfigStore(self.data/'config.json')
        self.cfg = AppConfig(); self.store.save(self.cfg)
        self.rt = BotRuntime(self.store, self.data)
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients: await c._client.aclose()
        self.tmp.cleanup()

    async def test_extraction_calls_model_and_learns(self):
        llm = Model(); mem = PersonaMemory(self.data/'extract.json',llm=llm)
        learned = await mem.observe('p_u','private','u','用户',[{'user':'我是后端工程师','assistant':'你好'}])
        self.assertEqual(llm.calls,1)
        self.assertEqual(len(learned),1)
        self.assertEqual(learned[0].owner_id,'u')

    async def test_shared_memory_survives_both_channels_and_management(self):
        before = self.rt._ensure_memory()
        w = self.rt._build_orchestrator(Adapter(),platform='wechat')
        q = self.rt._build_orchestrator(Adapter(),platform='qq')
        self.assertIs(w.memory,q.memory)
        self.assertIs(w.memory,before)
        w.memory.add_manual('user','u','fact','微信记忆',['微信'])
        q.memory.add_manual('user','u','preference','QQ记忆',['QQ'])
        self.assertEqual(len(PersonaMemory(self.data/'memory.json').list_items()),2)
        mid = self.rt.memory_list()['items'][0]['id']
        self.rt.memory_delete(mid)
        self.assertEqual(len(w.memory.list_items()),1)
        w.memory.recall('u','p_u','记忆')
        self.assertEqual(len(PersonaMemory(self.data/'memory.json').list_items()),1)

    async def test_injection_off_does_not_disable_learning(self):
        self.cfg.bot.memory_inject=False
        self.cfg.bot.memory_extract_interval=1
        self.store.save(self.cfg); self.rt.cfg=self.cfg
        orch=self.rt._build_orchestrator(Adapter())
        orch.memory.add_manual('user','u','fact','独有记忆',['记忆'])
        llm=Model(); orch.llm=llm; orch.sender=Sender(); orch.memory.llm=llm
        await orch.handle(message())
        await orch.wait_for_memory()
        self.assertFalse(any('独有记忆' in m['content'] for m in llm.messages))
        self.assertEqual(llm.calls,1)

    async def test_live_settings_and_model_configuration(self):
        orch=self.rt._build_orchestrator(Adapter())
        self.rt.qq_orch=orch; self.rt.platforms_running.add('qq');self.rt.running=True
        self.rt._active_adapter_config=copy.deepcopy(vars(self.cfg.adapter))
        changed=copy.deepcopy(self.cfg)
        changed.bot.name='新名称';changed.bot.persona='新设定'
        changed.bot.memory_enabled=False;changed.bot.memory_inject=False
        changed.bot.group_reply_at_sender=False;changed.bot.memory_extract_interval=7
        changed.llm.primary_key='new-key';changed.llm.primary_base='http://new'
        changed.llm.fallback_key='backup';changed.llm.judge_key='new-judge'
        needs_restart=self.rt.apply_config(changed)
        self.assertFalse(needs_restart)
        self.assertIn('新名称',orch.ctx.system_prompt);self.assertIn('新设定',orch.ctx.system_prompt)
        self.assertFalse(orch.group_reply_at_sender);self.assertFalse(orch.memory.enabled)
        self.assertFalse(orch.memory.inject);self.assertEqual(orch.memory.extract_interval,7)
        self.assertEqual(orch.llm.primary.api_key,'new-key')
        self.assertEqual(orch.llm.primary.base_url,'http://new')
        self.assertEqual(orch.llm.fallback.api_key,'backup')
        self.assertEqual(orch.memory.llm.primary.api_key,'new-judge')
        changed.adapter.qq_enabled=True
        self.assertTrue(self.rt.apply_config(changed))

    async def test_gateway_client_rekeys_after_credentials_change(self):
        class Client:
            def __init__(self,**kwargs): self.options=kwargs
        gw=LLMGateway(ProviderCfg('primary','http://first','old','m'))
        with patch('src.core.llm.AsyncOpenAI',Client):
            first=gw._client(gw.primary)
            gw.primary=ProviderCfg('primary','http://second','new','m')
            second=gw._client(gw.primary)
        self.assertIsNot(first,second)
        self.assertEqual(second.options['api_key'],'new')

    async def test_gateway_falls_back_before_first_token(self):
        calls=[]
        class Stream:
            def __aiter__(self): return self
            async def __anext__(self):
                if getattr(self,'done',False):raise StopAsyncIteration
                self.done=True
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='备用回复'))])
            async def close(self): self.closed=True
        stream=Stream()
        class Completions:
            def __init__(self,name):self.name=name
            async def create(self,**kwargs):
                calls.append(self.name)
                if self.name=='primary':raise RuntimeError('PRIMARY_DOWN')
                return stream
        gw=LLMGateway(ProviderCfg('primary','http://fake','k','m'),ProviderCfg('fallback','http://fake','k','m'))
        gw._client=lambda cfg:SimpleNamespace(chat=SimpleNamespace(completions=Completions(cfg.name)))
        text=''.join([x async for x in gw.stream([{'role':'user','content':'hi'}])])
        self.assertEqual(text,'备用回复');self.assertEqual(calls,['primary','fallback'])
        self.assertTrue(stream.closed)

    async def test_gateway_does_not_mix_models_after_output(self):
        calls=[]
        class Stream:
            def __aiter__(self):return self
            async def __anext__(self):
                if getattr(self,'done',False):raise RuntimeError('MID_STREAM')
                self.done=True
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='开头'))])
            async def close(self):pass
        class Completions:
            async def create(self,**kwargs):calls.append(kwargs['model']);return Stream()
        gw=LLMGateway(ProviderCfg('primary','http://fake','k','primary'),ProviderCfg('fallback','http://fake','k','backup'))
        gw._client=lambda cfg:SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        chunks=[]
        with self.assertRaisesRegex(RuntimeError,'MID_STREAM'):
            async for delta in gw.stream([]):chunks.append(delta)
        self.assertEqual(chunks,['开头']);self.assertEqual(calls,['primary'])

    async def test_stale_token_clears_wechat_and_keeps_qq(self):
        adapter=ILinkAdapter(str(self.data/'stale'));self.clients.append(adapter)
        adapter._running=True
        async def stale(*a):return {'errcode':-14}
        adapter._post=stale
        self.rt.wechat_adapter=adapter;self.rt.running=True
        self.rt.platforms_running={'wechat','qq'}
        self.rt._start_ilink_poller();await self.rt._ilink_task
        st=self.rt.status()
        self.assertTrue(st['running']);self.assertTrue(st['qq_running'])
        self.assertFalse(st['wechat_running']);self.assertFalse(st['ilink_polling'])
        self.assertIn('失效',st['last_error'])

    async def test_stale_token_stops_single_channel(self):
        adapter=ILinkAdapter(str(self.data/'stale'));self.clients.append(adapter)
        adapter._running=True
        async def stale(*a):return {'errcode':-14}
        adapter._post=stale
        self.rt.wechat_adapter=adapter;self.rt.running=True;self.rt.platforms_running={'wechat'}
        self.rt._start_ilink_poller();await self.rt._ilink_task
        self.assertFalse(self.rt.status()['running'])

    async def test_stopped_wechat_can_restart_while_qq_runs(self):
        self.cfg.adapter.qq_enabled=True
        # Credentials validation belongs to other tests; no real service here.
        self.rt._validate=lambda:None
        self.rt.running=True;self.rt.platforms_running={'qq'}
        self.rt._build_wechat_adapter=lambda:Adapter()
        self.rt._start_ilink_poller=lambda:None
        r=await self.rt.start()
        self.assertTrue(r['ok']);self.assertIn('wechat',self.rt.platforms_running)

    async def test_cancelled_batch_recovers_before_next_poll(self):
        state=self.data/'recover';state.mkdir()
        (state/'account.json').write_text(json.dumps({'bot_token':'bot@im.bot:k'}))
        first=ILinkAdapter(str(state));self.clients.append(first);first._running=True
        async def updates(*a):return {'get_updates_buf':'next','msgs':[raw_message('1'),raw_message('2')]}
        async def cancelled(msg):raise asyncio.CancelledError()
        first._post=updates;first.on_message(cancelled)
        with self.assertRaises(asyncio.CancelledError):await first.poll_loop()
        persisted=json.loads((state/'cursor.json').read_text())
        self.assertEqual(persisted['get_updates_buf'],'next');self.assertEqual(len(persisted['pending']),2)
        restored=ILinkAdapter(str(state));self.clients.append(restored)
        self.assertTrue(restored._load_token());restored._running=True
        received=[]
        async def handle(msg):received.append((msg.msg_id,msg.context_token))
        async def no_more(*a):
            for _ in range(100):
                if len(received) == 2:break
                await asyncio.sleep(0.01)
            self.assertEqual(len(received),2)
            return {'errcode':-14}
        restored.on_message(handle);restored._post=no_more
        await restored.poll_loop()
        self.assertEqual(received,[('1','token-1'),('2','token-2')])
        self.assertEqual(json.loads((state/'cursor.json').read_text())['pending'],[])

    async def test_failed_handler_remains_pending(self):
        adapter=ILinkAdapter(str(self.data/'retry'));self.clients.append(adapter)
        adapter._pending=[raw_message()];adapter._get_updates_buf='next';adapter._save_cursor()
        async def bad(msg):raise RuntimeError('RETRY_ME')
        adapter.on_message(bad)
        self.assertFalse(await adapter._drain_pending())
        self.assertEqual(len(json.loads(adapter._cursor_file.read_text())['pending']),1)
        received=[]
        async def good(msg):received.append(msg.msg_id)
        adapter.on_message(good)
        self.assertTrue(await adapter._drain_pending());self.assertEqual(received,['1'])

    async def test_orchestrator_releases_failed_dedup(self):
        store=MemoryStore();orch=Orchestrator(Adapter(),store,Model(),None,ContextManager(store),TriggerRouter(),Sender())
        async def fail(msg):raise RuntimeError('SEND_FAILED')
        orch._process=fail
        with self.assertRaisesRegex(RuntimeError,'SEND_FAILED'):await orch.handle(message())
        self.assertNotIn('1',store._seen)
        calls=[]
        async def good(msg):calls.append(msg.msg_id)
        orch._process=good
        await orch.handle(message());await orch.handle(message())
        self.assertEqual(calls,['1'])

    async def test_atomic_checkpoint_failure_preserves_old_file(self):
        adapter=ILinkAdapter(str(self.data/'atomic'));self.clients.append(adapter)
        adapter._get_updates_buf='old';adapter._save_cursor()
        adapter._get_updates_buf='new';adapter._pending=[raw_message()]
        with patch('src.adapters.ilink.os.replace',side_effect=OSError('disk')):
            with self.assertRaises(OSError):adapter._save_cursor()
        self.assertEqual(json.loads(adapter._cursor_file.read_text())['get_updates_buf'],'old')
        self.assertEqual(list(adapter.state_dir.glob('*.tmp')),[])


    async def test_stop_cancels_inflight_and_releases_dedup(self):
        orch=self.rt._build_orchestrator(Adapter())
        self.rt.qq_orch=orch;self.rt.qq_adapter=Adapter()
        self.rt.running=True;self.rt.platforms_running={'qq'}
        entered=asyncio.Event()
        async def waiting(msg):
            entered.set()
            await asyncio.sleep(60)
        orch._process=waiting
        task=asyncio.create_task(orch.handle(message()))
        await entered.wait()
        try:
            await self.rt.stop()
            self.assertTrue(task.done())
            self.assertTrue(task.cancelled())
            self.assertNotIn('1',orch.store._seen)
            self.assertFalse(self.rt.running)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_old_cursor_format_remains_compatible(self):
        state=self.data/'legacy';state.mkdir()
        (state/'account.json').write_text(json.dumps({'bot_token':'bot@im.bot:k'}))
        (state/'cursor.json').write_text(json.dumps({'get_updates_buf':'legacy-cursor'}))
        adapter=ILinkAdapter(str(state));self.clients.append(adapter)
        self.assertTrue(adapter._load_token())
        self.assertEqual(adapter._get_updates_buf,'legacy-cursor')
        self.assertEqual(adapter._pending,[])


class SegmentTests(unittest.TestCase):
    def test_long_single_sentence(self):
        for text in ['x'*1601,'中'*1601,'a'*900+'。'+'b'*900,'code\n'+('x y '*600)]:
            parts=split_by_semantics(text,800)
            self.assertTrue(all(0<len(p)<=800 for p in parts))
            self.assertEqual(''.join(parts),text.strip())
    def test_invalid_length(self):
        with self.assertRaises(ValueError):split_by_semantics('hello',0)
    def test_empty_and_short(self):
        self.assertEqual(split_by_semantics('   '),[])
        self.assertEqual(split_by_semantics(' hello '),['hello'])


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'admin-test-secret','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'})
        self.env.start()
        import src.server as server
        self.server=importlib.reload(server)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app,client=('127.0.0.1',123)),base_url='http://127.0.0.1:8000')
    async def asyncTearDown(self):
        await self.client.aclose();self.env.stop();self.tmp.cleanup()
    async def test_unauthenticated_read_write_and_login(self):
        self.assertEqual((await self.client.get('/api/config')).status_code,401)
        self.assertEqual((await self.client.put('/api/config',json={})).status_code,401)
        self.assertEqual((await self.client.post('/api/auth/login',json={'token':'wrong'})).status_code,401)
        self.assertEqual((await self.client.post('/api/auth/login',json={'token':'admin-test-secret'})).status_code,200)
        r=await self.client.put('/api/config',json={'adapter':{'wechat_enabled':False},'bot':{'name':'已认证'}})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.server.config_store.load().bot.name,'已认证')
    async def test_bearer_and_cross_origin(self):
        headers={'Authorization':'Bearer admin-test-secret'}
        self.assertEqual((await self.client.get('/api/config',headers=headers)).status_code,200)
        headers['Origin']='https://other.example'
        self.assertEqual((await self.client.put('/api/config',json={},headers=headers)).status_code,403)
        self.assertEqual((await self.client.post('/api/auth/login',json={'token':'admin-test-secret'},headers={'Origin':'https://other.example'})).status_code,403)
    async def test_loopback_page_bootstraps_but_rebinding_does_not(self):
        page=await self.client.get('/')
        self.assertIn('HttpOnly',page.headers.get('set-cookie',''))
        self.assertIn('SameSite=strict',page.headers.get('set-cookie',''))
        self.assertEqual((await self.client.get('/api/config')).status_code,200)
        self.client.cookies.clear()
        page=await self.client.get('/',headers={'Host':'attacker.example'})
        self.assertNotIn('set-cookie',page.headers)
        self.assertEqual((await self.client.get('/api/config')).status_code,401)
        page=await self.client.get('/',headers={'Sec-Fetch-Site':'cross-site'})
        self.assertNotIn('set-cookie',page.headers)
    async def test_remote_or_wildcard_page_requires_login(self):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app,client=('192.0.2.2',123)),base_url='http://127.0.0.1:8000') as c:
            self.assertNotIn('set-cookie',(await c.get('/')).headers)
        self.server.admin_auth.bind_host='0.0.0.0'
        self.assertNotIn('set-cookie',(await self.client.get('/')).headers)
    async def test_onebot_uses_separate_token(self):
        cfg=self.server.config_store.load();cfg.adapter.qq_enabled=True;cfg.adapter.onebot_token='onebot-test';self.server.config_store.save(cfg)
        self.server.runtime.qq_adapter=SimpleNamespace(platform='onebot',handle_event=lambda e:asyncio.sleep(0))
        r=await self.client.post('/onebot/event',json={'post_type':'meta_event'},headers={'Authorization':'Bearer onebot-test'})
        self.assertEqual(r.json()['retcode'],0)
        r=await self.client.post('/onebot/event',json={},headers={'Authorization':'Bearer admin-test-secret'})
        self.assertEqual(r.json()['retcode'],403)
        r=await self.client.get('/api/config',headers={'Authorization':'Bearer onebot-test'})
        self.assertEqual(r.status_code,401)
    async def test_config_endpoint_updates_running_behavior(self):
        orch=self.server.runtime._build_orchestrator(Adapter());self.server.runtime.qq_orch=orch
        self.server.runtime.running=True
        self.server.runtime._active_adapter_config=vars(self.server.runtime.cfg.adapter).copy()
        cfg=copy.deepcopy(self.server.config_store.masked(self.server.runtime.cfg));cfg['bot']['name']='API新名称'
        r=await self.client.put('/api/config',json=cfg,headers={'Authorization':'Bearer admin-test-secret'})
        self.assertEqual(r.status_code,200);self.assertFalse(r.json()['requires_restart'])
        self.assertIn('API新名称',orch.ctx.system_prompt)
        cfg['adapter']['qq_enabled']=True
        r=await self.client.put('/api/config',json=cfg,headers={'Authorization':'Bearer admin-test-secret'})
        self.assertTrue(r.json()['requires_restart'])
    async def test_generated_token_permissions_and_cookie_tampering(self):
        from src.core.admin_auth import AdminAuth,COOKIE
        with patch.dict(os.environ,{'WXBOT_ADMIN_TOKEN':''}):
            auth=AdminAuth(Path(self.tmp.name),'127.0.0.1')
            again=AdminAuth(Path(self.tmp.name),'127.0.0.1')
        self.assertEqual(auth.token,again.token)
        assert_private_file(self,Path(self.tmp.name)/'admin-token')
        self.client.cookies.set(COOKIE,'9999999999.fake.bad')
        self.assertEqual((await self.client.get('/api/config')).status_code,401)


if __name__=='__main__':unittest.main()
