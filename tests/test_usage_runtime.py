"""Runtime accounting and search pools; synthetic metadata, no API/key access."""
from collections import deque
from pathlib import Path
from types import SimpleNamespace as NS
import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.config_store import ConfigStore,AppConfig
from src.core.llm import LLMGateway,ProviderCfg
from src.core.web_search import DeepSeekWebSearch
from src.runtime import BotRuntime


class RuntimeUsage(unittest.TestCase):
    def runtime(self):
        runtime=BotRuntime.__new__(BotRuntime)
        runtime._usage_events=deque(maxlen=500)
        return runtime

    def test_request_and_usage_pair_once_in_either_order(self):
        for usage_first in (False,True):
            runtime=self.runtime()
            request={'kind':'request','request_id':'one','task':'chat','status':'success'}
            usage={'kind':'usage','request_id':'one','task':'chat','prompt_tokens':100,
                   'completion_tokens':20,'cache_hit_tokens':80,'cache_miss_tokens':20}
            for event in ([usage,request] if usage_first else [request,usage]):runtime._observe_usage(event)
            result=runtime.usage_diagnostics()
            self.assertEqual((result['observed_events'],result['requests']),(2,1))
            self.assertEqual(result['summary']['attempts'],1)
            self.assertEqual(result['summary']['fields']['prompt_tokens']['observed_sum'],100)
            self.assertEqual(result['summary']['cache']['overall_hit_ratio'],.8)

    def test_unreported_failure_is_unknown_not_free_or_a_cache_miss(self):
        runtime=self.runtime()
        runtime._observe_usage({'kind':'request','request_id':'fail','task':'decision','status':'cancelled'})
        runtime._observe_usage({'kind':'request','request_id':'ok','task':'chat','status':'success'})
        runtime._observe_usage({'kind':'usage','request_id':'ok','task':'chat','prompt_tokens':100,
                               'cache_hit_tokens':80,'cache_miss_tokens':20})
        result=runtime.usage_diagnostics();summary=result['summary']
        self.assertEqual(summary['attempts'],2)
        self.assertEqual((summary['cache']['hit_ratio'],summary['cache']['overall_hit_ratio']),(.8,None))
        self.assertEqual(summary['cache']['unknown_calls'],1)
        self.assertEqual(summary['by_task']['decision']['usage_unknown_calls'],1)
        self.assertEqual(result['request_states'],{'cancelled':1,'success':1})

    def test_native_search_does_not_double_count_cache_read(self):
        runtime=self.runtime()
        runtime._observe_usage({'kind':'usage','request_id':'search','task':'search','protocol':'anthropic',
                               'input_tokens_observed':10,'cache_creation_input_tokens':20,
                               'cache_read_input_tokens':70,'output_tokens_observed':5})
        runtime._observe_usage({'kind':'request','request_id':'search','task':'search','status':'success'})
        runtime._observe_usage({'kind':'local_search_cache','task':'search','prompt_tokens':900})
        result=runtime.usage_diagnostics()
        self.assertEqual(result['requests'],1);self.assertEqual(result['local_search_cache'],1)
        self.assertEqual(result['summary']['fields']['prompt_tokens']['observed_sum'],100)
        self.assertEqual(result['summary']['fields']['completion_tokens']['observed_sum'],5)
        self.assertEqual(result['summary']['cache']['hit_ratio'],.7)

    def test_window_boundary_orphan_usage_and_attempt_are_retained(self):
        runtime=self.runtime()
        for n in range(502):runtime._observe_usage({'kind':'request','request_id':str(n),'task':'chat','status':'error'})
        self.assertEqual(len(runtime._usage_events),500)
        runtime._observe_usage({'kind':'usage','request_id':'orphan','task':'memory','prompt_tokens':3})
        result=runtime.usage_diagnostics()
        self.assertEqual((result['requests'],result['observed_events']),(500,500))
        self.assertEqual(result['summary']['by_task']['memory']['attempts'],1)

    def test_observer_filters_content_and_secrets(self):
        runtime=self.runtime()
        runtime._observe_usage({'kind':'usage','request_id':'safe','task':'chat','prompt_tokens':3,
                               'messages':[{'role':'user','content':'synthetic chat'}],
                               'api_key':'synthetic secret','chat_id':'private account'})
        event=runtime._usage_events[0]
        self.assertNotIn('messages',event);self.assertNotIn('api_key',event);self.assertNotIn('chat_id',event)

    def test_empty_window_is_unknown(self):
        result=self.runtime().usage_diagnostics()
        self.assertEqual(result['requests'],0)
        self.assertIsNone(result['summary']['cache']['hit_ratio'])
        self.assertIsNone(result['summary']['fields']['completion_tokens']['observed_sum'])


class SearchPoolLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.runtime=BotRuntime(ConfigStore(Path(self.temp.name)/'config.json'),Path(self.temp.name))
        self.cfg=AppConfig();self.cfg.llm.primary_base='https://api.deepseek.com'
        self.cfg.llm.primary_key='synthetic-not-real';self.cfg.llm.search_enabled=True
        self.gateway=LLMGateway(ProviderCfg('primary','https://api.deepseek.com','synthetic-not-real','deepseek-flash'))

    async def asyncTearDown(self):
        await self.gateway.aclose();await self.runtime.stop();self.temp.cleanup()

    async def test_unchanged_settings_keep_search_cache_and_client(self):
        self.runtime._refresh_search(self.gateway,self.cfg)
        original=self.gateway.web_search
        client=NS(aclose=AsyncMock());original._client=client
        original._remember(('fixture',),{'ok':True,'summary':'synthetic body','sources':[]})
        self.cfg.bot.name='not a search setting'
        self.runtime._refresh_search(self.gateway,self.cfg)
        self.assertIs(self.gateway.web_search,original)
        self.assertIs(original._client,client)
        self.assertEqual(len(original._cache),1)
        client.aclose.assert_not_awaited()
        original._observe({'kind':'usage','task':'search','prompt_tokens':7})
        self.assertEqual(self.runtime.usage_diagnostics()['summary']['fields']['prompt_tokens']['observed_sum'],7)

    async def test_changed_key_retires_old_pool_without_midrequest_close(self):
        self.runtime._refresh_search(self.gateway,self.cfg)
        original=self.gateway.web_search;client=NS(aclose=AsyncMock());original._client=client
        self.cfg.llm.primary_key='synthetic-new-key'
        self.runtime._refresh_search(self.gateway,self.cfg)
        self.assertIsNot(self.gateway.web_search,original)
        client.aclose.assert_not_awaited()
        await self.runtime._close_retired_search()
        client.aclose.assert_awaited_once()
        self.assertEqual(self.runtime._retired_search,{})
        self.assertFalse(self.runtime._search_cleanup_tasks)

    async def test_disabled_search_remains_disabled_and_retires_existing_pool(self):
        self.runtime._refresh_search(self.gateway,self.cfg)
        original=self.gateway.web_search
        self.cfg.llm.search_enabled=False
        self.runtime._refresh_search(self.gateway,self.cfg)
        self.assertIsNone(self.gateway.web_search)
        await self.runtime._close_retired_search()
        self.assertTrue(original._closed)

    async def test_short_lived_proactive_gateway_usage_is_retained(self):
        async def fake_chat(gateway,*args,**kwargs):
            self.assertIsNotNone(gateway.usage_observer)
            gateway.usage_observer({'kind':'usage','request_id':'proactive','task':'proactive',
                                    'prompt_tokens':50,'completion_tokens':5})
            return 'synthetic response'
        memory=NS(enabled=False,inject=False)
        with patch.object(self.runtime,'_ensure_memory',return_value=memory),patch.object(LLMGateway,'chat',fake_chat):
            await self.runtime._proactive_generate({'owner_id':'fixture','chat_id':'fixture','platform':'onebot'},[])
        summary=self.runtime.usage_diagnostics()['summary']
        self.assertEqual(summary['by_task']['proactive']['attempts'],1)
        self.assertEqual(summary['fields']['completion_tokens']['observed_sum'],5)

    async def test_memory_gateway_and_foreground_gateways_share_observer(self):
        memory=self.runtime._ensure_memory()
        self.assertIsNotNone(memory.llm.usage_observer)
        memory.llm.usage_observer({'kind':'usage','request_id':'memory','task':'memory','prompt_tokens':4})
        self.assertEqual(self.runtime.usage_diagnostics()['summary']['by_task']['memory']['attempts'],1)


if __name__=='__main__':unittest.main()
