"""Offline search wire contracts, bounded tool loop, clock and persistence."""
import asyncio
import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from src.config_store import AppConfig, ConfigStore
from src.core.reality import clock_context, local_datetime, valid_timezone
from src.core.web_search import DeepSeekWebSearch, configured_search, parse_results, ENDPOINT, query_is_public, public_url
from src.core.llm import LLMGateway, ProviderCfg
from src.core.context import ContextManager
from src.core.types import WxMessage
from src.store_memory import MemoryStore
from src.runtime import BotRuntime

URL='https://example.com/news'
def payload():
    return {'content':[{'type':'web_search_tool_result','content':[{'type':'web_search_result','url':URL,'title':'公开新闻','page_age':'2026-10-13'}]}, {'type':'text','text':'Summary','citations':[{'url':URL,'cited_text':'事实片段'}]}]}
def result():return {'ok':True,'sources':[{'url':URL,'title':'公开新闻','snippet':'事实片段','published_at':'2026-10-13'}]}
def call(name='web_search',args='{"query":"2026年10月13日公开新闻"}',cid='tool1'):
    f=NS(name=name,arguments=args)
    d={'id':cid,'type':'function','function':{'name':name,'arguments':args}}
    return NS(id=cid,function=f,model_dump=lambda **kw:d)
def response(text='答复',tools=None,reasoning=None):
    return NS(choices=[NS(message=NS(content=text,tool_calls=tools,reasoning_content=reasoning))],usage=NS(prompt_tokens=10,completion_tokens=3))

class ClockTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_timezone_and_relative_dates(self):
        stamp=datetime(2026,10,12,16,0,1,tzinfo=timezone.utc).timestamp()
        c=clock_context('Asia/Shanghai',stamp)
        for s in ['2026-10-13T00:00:01+08:00','星期二','Asia/Shanghai','昨天：2026-10-12','明天：2026-10-14','2026-10-12T16:00:01+00:00']:self.assertIn(s,c)
    def test_different_timezone(self):
        stamp=datetime(2026,10,13,tzinfo=timezone.utc).timestamp()
        self.assertIn('2026-10-12T20:00:00-04:00',clock_context('America/New_York',stamp))
    def test_bad_timezone_normalized(self):
        for bad in [None,'','wrong/Timezone','../etc/passwd']:self.assertEqual(valid_timezone(bad),'Asia/Shanghai')
    async def test_every_turn_refreshes_not_message_time(self):
        ctx=ContextManager(MemoryStore(),context_timezone='UTC')
        msg=WxMessage('test','ilink','private','p','p','user','bot','现在几点')
        with patch('src.core.context.clock_context',side_effect=['CLOCK_ONE','CLOCK_TWO']):
            a=await ctx.build(msg);b=await ctx.build(msg)
        self.assertIn('CLOCK_ONE',[m['content'] for m in a]);self.assertIn('CLOCK_TWO',[m['content'] for m in b]);self.assertEqual(await ctx.store.get_recent(msg.ctx_key),[])
    async def test_history_timestamp_uses_same_timezone(self):
        store=MemoryStore();ctx=ContextManager(store,context_timezone='Asia/Shanghai')
        msg=WxMessage('test','ilink','private','p','p','user','bot','你好')
        await store.push(msg.ctx_key,{'user':'昨天','assistant':'回应','ts':datetime(2026,10,12,16,tzinfo=timezone.utc).timestamp()})
        data=json.dumps(await ctx.build(msg),ensure_ascii=False);self.assertIn('2026-10-13T00:00:00+08:00',data)

class ParserTests(unittest.TestCase):
    def test_sources_join_citations(self):
        r=parse_results(payload());self.assertTrue(r['ok']);self.assertEqual(r['sources'][0]['snippet'],'事实片段')
    def test_no_prose_scraping(self):
        self.assertEqual(parse_results({'content':[{'type':'text','text':'我已联网搜到 '+URL}]})['error'],'native_search_not_triggered')
    def test_error_not_success(self):
        self.assertFalse(parse_results({'content':[{'type':'web_search_tool_result','content':{'type':'web_search_tool_result_error'}}]})['ok'])
    def test_invalid_payload(self):
        for p in [None,{},[],{'content':{}},{'content':[None]}]:self.assertFalse(parse_results(p)['ok'])
    def test_dedupe_and_bound(self):
        p=payload();items=p['content'][0]['content'];items*=2
        items += [{'type':'web_search_result','url':f'https://example.com/{i}','title':'X'*900} for i in range(9)]
        r=parse_results(p);self.assertEqual(len(r['sources']),5);self.assertTrue(all(len(s['title'])<=200 for s in r['sources']))
    def test_public_urls_only(self):
        for u in ['file:///etc/passwd','http://127.0.0.1','http://[::1]/','https://localhost/a','https://user:key@example.com','javascript:alert(1)','http://192.168.1.1','http://service.local']:
            self.assertEqual(public_url(u),'')
    def test_private_query_filter(self):
        for q in [None,'','x'*241,'查找 sk-1234567890','邮箱 a@example.com','手机号 13812345678','密码：abc','data:image/png;base64,ABC']:
            self.assertFalse(query_is_public(q))
        self.assertTrue(query_is_public('上海 2026-10-13 天气'))

class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def request(self,handler,query='今日公开新闻'):
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('src.core.web_search.make_client',return_value=client):r=await DeepSeekWebSearch('dummy-only-key').search(query)
        self.assertTrue(client.is_closed);return r
    async def test_native_contract_no_conversation(self):
        def handler(req):
            self.assertEqual(str(req.url),ENDPOINT);self.assertEqual(req.headers['x-api-key'],'dummy-only-key');self.assertEqual(req.headers['anthropic-version'],'2023-06-01')
            body=json.loads(req.content);self.assertEqual(body['tools'],[{'type':'web_search_20250305','name':'web_search','max_uses':1}]);self.assertEqual(len(body['messages']),1);self.assertNotIn('memory',str(body));self.assertIn('当前时间',body['system'])
            return httpx.Response(200,json=payload())
        self.assertTrue((await self.request(handler))['ok'])
    async def test_missing_key_no_network(self):
        with patch('src.core.web_search.make_client') as make:r=await DeepSeekWebSearch().search('公开新闻')
        make.assert_not_called();self.assertEqual(r['error'],'missing_deepseek_key')
    async def test_invalid_query_no_network(self):
        with patch('src.core.web_search.make_client') as make:r=await DeepSeekWebSearch('dummy').search('API key: secret')
        make.assert_not_called();self.assertFalse(r['ok'])
    async def test_http_errors_and_redirect_no_secret(self):
        for code in [302,401,402,429,500]:
            count=[]
            def handler(req):count.append(req);return httpx.Response(code,json={'error':'dummy-only-key'},headers={'Location':'https://elsewhere.invalid'})
            r=await self.request(handler);self.assertEqual(len(count),1);self.assertNotIn('dummy-only-key',json.dumps(r));self.assertEqual(r['error'],f'search_http_{code}')
    async def test_timeout(self):
        def handler(req):raise httpx.ReadTimeout('dummy-only-key')
        self.assertEqual((await self.request(handler))['error'],'search_network_error')
    async def test_invalid_json(self):
        self.assertEqual((await self.request(lambda req:httpx.Response(200,content='not JSON')))['error'],'invalid_response')
    async def test_large_response(self):
        self.assertEqual((await self.request(lambda req:httpx.Response(200,content=b'a'*(2*1024*1024+1))))['error'],'response_too_large')
    async def test_cancel_propagates_and_closes(self):
        started=asyncio.Event()
        async def handler(req):started.set();await asyncio.sleep(60)
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('src.core.web_search.make_client',return_value=client):
            t=asyncio.create_task(DeepSeekWebSearch('dummy').search('公开新闻'));await started.wait();t.cancel()
            with self.assertRaises(asyncio.CancelledError):await t
        self.assertTrue(client.is_closed)

class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self,replies,search_result=None):
        search=NS(search=AsyncMock(return_value=search_result or result()))
        g=LLMGateway(ProviderCfg('primary','https://api.deepseek.com','dummy','deepseek-flash'),web_search=search)
        client=NS(chat=NS(completions=NS(create=AsyncMock(side_effect=replies))))
        g._client=lambda cfg:client
        # Tool-loop wire contracts are isolated from the separately tested review pass.
        g._review_search_answer=AsyncMock(side_effect=lambda cfg,work,draft,sources,chat_id:g._with_sources(draft,sources))
        return g,search,client.chat.completions.create
    async def test_auto_chat_single_call(self):
        g,s,c=self.gateway([response('你好')]);self.assertEqual([v async for v in g.stream([{'role':'user','content':'你好'}])],['你好']);s.search.assert_not_called();self.assertEqual(c.call_count,1)
    async def test_tool_result_round_trip_reasoning_and_one_bubble(self):
        g,s,c=self.gateway([response('我查一下',[call()], 'thinking'),response('查到公开新闻')]);m=[{'role':'user','content':'查今日新闻'}]
        out=[v async for v in g.stream(m,chat_id='p')];self.assertEqual(len(out),1);self.assertNotIn('我查一下',out[0]);self.assertIn(URL,out[0]);self.assertEqual(len(m),1)
        second=c.call_args_list[1].kwargs['messages'];self.assertEqual(second[-2]['reasoning_content'],'thinking');self.assertEqual(second[-1]['tool_call_id'],'tool1');self.assertIn('事实片段',second[-1]['content'])
    async def test_tool_budget_two_calls(self):
        g,s,c=self.gateway([response('',[call(),call(cid='tool2'),call(cid='tool3')]),response('',[call(cid='tool4')]),response('最终')]);out=[v async for v in g.stream([])]
        self.assertEqual(s.search.call_count,2);self.assertEqual(c.call_count,3);self.assertEqual(c.call_args.kwargs['tool_choice'],'none');self.assertEqual(len(out),1)
    async def test_invalid_arguments_and_unknown_tool(self):
        for t in [call(args='{bad'),call(name='other')]:
            g,s,c=self.gateway([response('',[t]),response('继续')]);await g.chat([],chat_id='p');s.search.assert_not_called()
    async def test_failed_search_answer_not_interrupted(self):
        g,s,c=self.gateway([response('',[call()]),response('本次未查到')],{'ok':False,'error':'search_network_error','sources':[]});self.assertEqual([v async for v in g.stream([])],['本次未查到'])
    async def test_internal_completion_has_no_tools(self):
        g,s,c=self.gateway([response('摘要')]);await g.complete('私人记忆');self.assertNotIn('tools',c.call_args.kwargs);s.search.assert_not_called()
    async def test_images_remain_images(self):
        g,s,c=self.gateway([response('',[call()]),response('图片与查询')]);image={'type':'image_url','image_url':{'url':'data:image/png;base64,AAA'}};m=[{'role':'user','content':[image]}]
        await g.chat(m,chat_id='p');self.assertEqual(c.call_args.kwargs['messages'][0]['content'][0],image)
    async def test_rejected_tools_keeps_normal_reply(self):
        err=httpx.HTTPStatusError('bad tools',request=httpx.Request('POST',URL),response=httpx.Response(400));err.status_code=400
        g,s,c=self.gateway([err,response('本轮未核实')]);self.assertEqual([v async for v in g.stream([])],['本轮未核实']);self.assertNotIn('tools',c.call_args.kwargs)
    async def test_transient_fallback_keeps_result(self):
        err=RuntimeError('provider');g,s,c=self.gateway([response('',[call()]),err,response('备用答复')]);g.fallback=ProviderCfg('fallback','https://model.invalid','dummy','model');out=[v async for v in g.stream([])]
        self.assertEqual(s.search.call_count,1);self.assertIn(URL,out[0]);self.assertEqual(c.call_args.kwargs['model'],'model')
    async def test_cancel_during_search_no_reply(self):
        g,s,c=self.gateway([response('',[call()])]);started=asyncio.Event()
        async def wait(_):started.set();await asyncio.sleep(60)
        s.search.side_effect=wait
        async def consume():return [v async for v in g.stream([])]
        task=asyncio.create_task(consume());await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(c.call_count,1)

class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_key_reuse_official_only(self):
        cfg=AppConfig().llm;cfg.primary_key='dummy'
        self.assertEqual(configured_search(cfg).key,'dummy')
        for base in ['https://api.deepseek.com.evil/v1','http://api.deepseek.com','https://api.deepseek.com:444','https://other.invalid/v1','bad[']:
            cfg.primary_base=base;self.assertEqual(configured_search(cfg).key,'')
        cfg.search_key='own';self.assertEqual(configured_search(cfg).key,'own')
    def test_disable(self):
        cfg=AppConfig().llm;cfg.search_enabled=False;self.assertIsNone(configured_search(cfg))
    def test_persist_mask_migrate(self):
        with tempfile.TemporaryDirectory() as d:
            s=ConfigStore(Path(d)/'config.json');cfg=s._from_dict({'llm':{'primary_key':'old'}});self.assertTrue(cfg.llm.search_enabled);self.assertEqual(cfg.llm.context_timezone,'Asia/Shanghai')
            cfg.llm.search_key='do-not-echo';cfg.llm.context_timezone='UTC';s.save(cfg);self.assertEqual(s.load().llm.context_timezone,'UTC');self.assertNotIn('do-not-echo',json.dumps(s.masked(cfg)))
    def test_bad_config_normalized(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=ConfigStore(Path(d)/'config.json')._from_dict({'llm':{'context_timezone':None,'search_enabled':'false','search_model':None,'search_key':4}})
            self.assertEqual(cfg.llm.context_timezone,'Asia/Shanghai');self.assertFalse(cfg.llm.search_enabled);self.assertEqual(cfg.llm.search_model,'deepseek-flash');self.assertEqual(cfg.llm.search_key,'')
    def test_frozen_includes_timezone_database(self):
        root=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]))
        for name in ['desktop/scripts/build-backend.sh','desktop/scripts/build-backend.ps1','requirements.txt']:self.assertIn('tzdata',(root/name).read_text())
    async def test_hot_updates_both_channels_not_memory(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=AppConfig();s=ConfigStore(Path(d)/'config.json');rt=BotRuntime(s,Path(d));rt.cfg=cfg;rt._wire_logging=lambda *args:None
            adapter=NS(platform='ilink',on_message=lambda f:None);a=rt._build_orchestrator(adapter,'ilink');b=rt._build_orchestrator(NS(platform='onebot',on_message=lambda f:None),'onebot');rt.wechat_orch=a;rt.qq_orch=b
            cfg.llm.context_timezone='UTC';cfg.llm.search_enabled=False;rt.apply_config(cfg)
            for orch in [a,b]:self.assertEqual(orch.ctx.context_timezone,'UTC');self.assertIsNone(orch.llm.web_search)
            self.assertIsNone(rt.memory.llm.web_search)

class SearchAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'test-auth','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.s=importlib.reload(server);self.c=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.s.app),base_url='http://localhost',headers={'Authorization':'Bearer test-auth'})
    async def asyncTearDown(self):await self.c.aclose();self.env.stop();self.tmp.cleanup()
    async def test_save_only_search(self):
        r=await self.c.patch('/api/config/section/search',json={'search_enabled':False,'search_key':'dummy-secret','context_timezone':'UTC'});self.assertEqual(r.status_code,200);self.assertNotIn('dummy-secret',r.text);self.assertEqual(self.s.config_store.load().llm.primary_model,'deepseek-flash')
    async def test_invalid_settings(self):
        for body in [{'context_timezone':'wrong/Zone'},{'search_enabled':'yes'},{'primary_key':'cross-section'},{'search_model':''}]:self.assertEqual((await self.c.patch('/api/config/section/search',json=body)).status_code,400)
    async def test_mask_keeps_key(self):
        await self.c.patch('/api/config/section/search',json={'search_key':'dummy-secret'});cfg=(await self.c.get('/api/config')).json()
        await self.c.patch('/api/config/section/search',json={'search_key':cfg['llm']['search_key']});self.assertEqual(self.s.config_store.load().llm.search_key,'dummy-secret')
    async def test_requires_auth(self):
        self.c.headers.clear();self.assertEqual((await self.c.patch('/api/config/section/search',json={})).status_code,401)

    async def test_whole_config_mask_and_optional_key_clear(self):
        await self.c.patch('/api/config/section/search',json={'search_key':'dummy-secret'})
        cfg=(await self.c.get('/api/config')).json()
        self.assertEqual((await self.c.put('/api/config',json=cfg)).status_code,200)
        self.assertEqual(self.s.config_store.load().llm.search_key,'dummy-secret')
        cfg['llm']['search_key']='';await self.c.put('/api/config',json=cfg)
        self.assertEqual(self.s.config_store.load().llm.search_key,'')
    async def test_legacy_config_keeps_search_fields(self):
        await self.c.patch('/api/config/section/search',json={'search_enabled':False,'search_key':'dummy-secret','context_timezone':'UTC'})
        await self.c.put('/api/config',json={'llm':{'primary_model':'old-client-model'}})
        cfg=self.s.config_store.load();self.assertFalse(cfg.llm.search_enabled);self.assertEqual(cfg.llm.search_key,'dummy-secret');self.assertEqual(cfg.llm.context_timezone,'UTC')
