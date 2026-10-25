"""Native search cost controls: synthetic HTTP, no real keys or paid calls."""
import asyncio
from datetime import datetime, timezone
import inspect
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.core.usage_accounting import summary
from src.core.web_search import (DeepSeekWebSearch, NATIVE_SEARCH_SYSTEM,
                                  SEARCH_CACHE_BYTES, SEARCH_CACHE_ENTRIES)


def payload(*, usage=True, body=True, result=True, truncated=False):
    blocks=[]
    if result:
        blocks.append({'type':'web_search_tool_result','content':[
            {'type':'web_search_result','url':'https://example.com/article','title':'公开事件'}]})
    if body:
        blocks.append({'type':'text','text':'正文依据：今日开始试运行，需要预约。','citations':[
            {'url':'https://example.com/article','cited_text':'今日试运行，需要预约。'}]})
    value={'content':blocks}
    if usage:value['usage']={'input_tokens':10,'cache_creation_input_tokens':20,'cache_read_input_tokens':70,'output_tokens':5}
    if truncated:value['stop_reason']='max_tokens'
    return value


class SearchCostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers=[];self.clients=[];self.requests=[];self.handler=None
        async def dispatch(request):
            self.requests.append(request)
            if self.handler is None:return httpx.Response(200,json=payload())
            result=self.handler(request)
            return await result if inspect.isawaitable(result) else result
        def make(*args,**kwargs):
            client=httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
            self.clients.append(client);return client
        self.factory=patch('src.core.web_search.make_client',side_effect=make)
        self.make=self.factory.start()

    async def asyncTearDown(self):
        for provider in self.providers:await provider.aclose()
        self.factory.stop()
        self.assertTrue(all(client.is_closed for client in self.clients))

    def provider(self,**kwargs):
        provider=DeepSeekWebSearch(key='fixture-only-key',**kwargs)
        self.providers.append(provider);return provider

    async def test_thinking_disabled_budget_and_system_prefix_stays_stable(self):
        provider=self.provider()
        with patch('src.core.web_search.clock_context',side_effect=['CLOCK_ONE','RETRIEVED_ONE','CLOCK_TWO','RETRIEVED_TWO']):
            await provider.search('第一条公开新闻');await provider.search('第二条公开新闻')
        first,second=[json.loads(request.content) for request in self.requests]
        self.assertEqual(first['thinking'],{'type':'disabled'})
        self.assertEqual(first['max_tokens'],1024)
        self.assertEqual(first['system'],NATIVE_SEARCH_SYSTEM)
        self.assertEqual(second['system'],first['system'])
        self.assertNotIn('CLOCK_ONE',first['system'])
        self.assertIn('CLOCK_ONE',str(first['messages']))
        self.assertIn('CLOCK_TWO',str(second['messages']))

    async def test_exact_normalized_query_reuses_success_and_preserves_original_timestamp(self):
        provider=self.provider()
        first=await provider.search('今日  公开新闻')
        first['sources'][0]['snippet']='caller mutation'
        second=await provider.search(' 今日\t公开新闻 ')
        self.assertEqual(len(self.requests),1)
        self.assertTrue(second['local_result_reuse'])
        self.assertEqual(second['reuse_source'],'ttl')
        self.assertEqual(second['retrieved_at'],first['retrieved_at'])
        self.assertNotEqual(second['sources'][0]['snippet'],'caller mutation')
        self.assertEqual((len(provider.metrics),len(provider.attempts)),(1,1))
        self.assertEqual(provider.cache_events[-1]['kind'],'local_search_cache')
        self.assertIsNone(provider.cache_events[-1]['provider_kv_hit'])

    async def test_cache_expires_at_sixty_seconds_not_sixty_seconds_after_reuse(self):
        provider=self.provider();now=[10.]
        with patch.object(provider,'_cache_clock',side_effect=lambda:now[0]):
            await provider.search('今日公开新闻')
            now[0]=69.;self.assertTrue((await provider.search('今日公开新闻'))['local_result_reuse'])
            now[0]=70.;await provider.search('今日公开新闻')
        self.assertEqual(len(self.requests),2)

    async def test_local_date_rollover_invalidates_even_before_ttl(self):
        provider=self.provider();day=[datetime(2026,10,22,tzinfo=timezone.utc)]
        with patch('src.core.web_search.local_datetime',side_effect=lambda _:day[0]):
            await provider.search('今日公开新闻')
            day[0]=datetime(2026,10,23,tzinfo=timezone.utc)
            await provider.search('今日公开新闻')
        self.assertEqual(len(self.requests),2)

    async def test_provider_model_key_and_timezone_are_part_of_cache_identity(self):
        provider=self.provider()
        await provider.search('今日公开新闻')
        provider.model='another-model';await provider.search('今日公开新闻')
        provider.key='different-fixture-key';await provider.search('今日公开新闻')
        provider.context_timezone='UTC';await provider.search('今日公开新闻')
        self.assertEqual(len(self.requests),4)
        self.assertEqual(self.make.call_count,1)
        self.assertEqual(self.requests[-1].headers['x-api-key'],'different-fixture-key')

    async def test_instances_never_share_result_cache_or_credentials(self):
        first=self.provider();second=self.provider()
        await first.search('今日公开新闻');await second.search('今日公开新闻')
        self.assertEqual(len(self.requests),2)
        self.assertEqual(self.make.call_count,2)

    async def test_single_flight_joins_duplicate_queries_but_returns_independent_results(self):
        provider=self.provider();entered=asyncio.Event();release=asyncio.Event()
        async def handler(_):entered.set();await release.wait();return httpx.Response(200,json=payload())
        self.handler=handler
        first=asyncio.create_task(provider.search('今日公开新闻'));await entered.wait()
        others=[asyncio.create_task(provider.search('今日公开新闻')) for _ in range(2)]
        await asyncio.sleep(0);release.set()
        results=await asyncio.gather(first,*others)
        self.assertEqual(len(self.requests),1)
        self.assertEqual((len(provider.metrics),len(provider.attempts)),(1,1))
        self.assertEqual(sum(r.get('reuse_source')=='single_flight' for r in results),2)
        results[0]['sources'][0]['snippet']='mutated'
        self.assertNotEqual(results[1]['sources'][0]['snippet'],'mutated')

    async def test_cancel_one_waiter_does_not_cancel_another_waiters_request(self):
        provider=self.provider();entered=asyncio.Event();release=asyncio.Event();cancelled=asyncio.Event()
        async def handler(_):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:cancelled.set();raise
            return httpx.Response(200,json=payload())
        self.handler=handler
        first=asyncio.create_task(provider.search('今日公开新闻'));await entered.wait()
        second=asyncio.create_task(provider.search('今日公开新闻'));await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):await first
        self.assertFalse(cancelled.is_set())
        release.set();self.assertTrue((await second)['ok'])
        self.assertEqual(len(self.requests),1)
        self.assertEqual(provider.attempts[-1]['status'],'success')

    async def test_last_waiter_cancellation_cancels_underlying_request_and_counts_unknown_usage(self):
        provider=self.provider();entered=asyncio.Event();cancelled=asyncio.Event()
        async def handler(_):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.handler=handler
        first=asyncio.create_task(provider.search('今日公开新闻'));await entered.wait()
        second=asyncio.create_task(provider.search('今日公开新闻'));await asyncio.sleep(0)
        first.cancel();second.cancel()
        results=await asyncio.gather(first,second,return_exceptions=True)
        self.assertTrue(all(isinstance(result,asyncio.CancelledError) for result in results))
        await provider.aclose()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(provider.attempts[-1]['status'],'cancelled')
        self.assertIsNone(provider.metrics[-1]['prompt_tokens'])
        self.assertEqual(summary(provider.metrics)['usage_unknown_calls'],1)

    async def test_aclose_cancels_flights_closes_shared_client_and_prevents_new_calls(self):
        provider=self.provider();entered=asyncio.Event()
        async def handler(_):entered.set();await asyncio.Event().wait()
        self.handler=handler
        task=asyncio.create_task(provider.search('今日公开新闻'));await entered.wait()
        await provider.aclose()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(self.clients[0].is_closed)
        self.assertEqual((await provider.search('今日公开新闻'))['error'],'search_closed')
        self.assertEqual(len(self.requests),1)
        await provider.aclose()

    async def test_http_failure_is_not_cached_or_retried_and_does_not_expose_body(self):
        provider=self.provider();self.handler=lambda _:httpx.Response(429,json={'error':'fixture-only-key'})
        first=await provider.search('今日公开新闻');second=await provider.search('今日公开新闻')
        self.assertEqual(first['error'],'search_http_429')
        self.assertEqual(first,second);self.assertEqual(len(self.requests),2)
        self.assertNotIn('fixture-only-key',json.dumps(first))
        self.assertTrue(all(a['status']=='error' for a in provider.attempts))
        self.assertTrue(all(m['prompt_tokens'] is None for m in provider.metrics))

    async def test_titles_without_summary_or_snippets_are_not_cached(self):
        provider=self.provider();self.handler=lambda _:httpx.Response(200,json=payload(body=False))
        self.assertTrue((await provider.search('今日公开新闻'))['ok'])
        await provider.search('今日公开新闻')
        self.assertEqual(len(self.requests),2)
        self.assertEqual(len(provider._cache),0)

    async def test_no_results_and_invalid_json_are_not_cached(self):
        provider=self.provider();self.handler=lambda _:httpx.Response(200,json=payload(result=False))
        self.assertFalse((await provider.search('今日公开新闻'))['ok'])
        await provider.search('今日公开新闻')
        self.assertEqual(len(self.requests),2)
        self.handler=lambda _:httpx.Response(200,content='invalid json fixture')
        self.assertEqual((await provider.search('其他公开新闻'))['error'],'invalid_response')
        self.assertEqual(provider.attempts[-1]['status'],'error')

    async def test_actual_native_usage_logs_and_observer_pair_request_id_without_private_input(self):
        provider=self.provider();observed=[];provider.usage_observer=observed.append
        with self.assertLogs('src.core.web_search',level='INFO') as logs:
            await provider.search('公开测试术语独一无二')
        usage=provider.metrics[-1];attempt=provider.attempts[-1]
        self.assertEqual((usage['prompt_tokens'],usage['completion_tokens'],usage['cache_hit_tokens']),(100,5,70))
        self.assertEqual(usage['request_id'],attempt['request_id'])
        self.assertTrue(attempt['usage_reported'])
        self.assertEqual({event['kind'] for event in observed},{'request','usage'})
        self.assertLessEqual(attempt['started_at'],attempt['finished_at'])
        text='\n'.join(logs.output)
        self.assertIn('LLM_USAGE ',text)
        for forbidden in ('fixture-only-key','公开测试术语独一无二'):
            self.assertNotIn(forbidden,text)

    async def test_absent_usage_remains_unknown_and_local_reuse_is_not_provider_kv(self):
        provider=self.provider();observed=[];provider.usage_observer=observed.append
        self.handler=lambda _:httpx.Response(200,json=payload(usage=False))
        await provider.search('今日公开新闻');await provider.search('今日公开新闻')
        self.assertIsNone(provider.metrics[-1]['prompt_tokens'])
        self.assertFalse(provider.attempts[-1]['usage_reported'])
        self.assertEqual(sum(event['kind']=='usage' for event in observed),1)
        self.assertEqual(sum(event['kind']=='local_search_cache' for event in observed),1)
        self.assertIsNone(provider.cache_events[-1]['provider_kv_hit'])

    async def test_observer_failure_does_not_retry_a_completed_search(self):
        provider=self.provider()
        def observer(_):raise RuntimeError('sensitive-fixture-body')
        provider.usage_observer=observer
        with self.assertLogs('src.core.web_search',level='WARNING') as logs:
            self.assertTrue((await provider.search('今日公开新闻'))['ok'])
        self.assertEqual(len(self.requests),1)
        self.assertNotIn('sensitive-fixture-body','\n'.join(logs.output))

    async def test_max_tokens_truncation_is_explicit_not_a_claim_of_complete_page(self):
        provider=self.provider();self.handler=lambda _:httpx.Response(200,json=payload(truncated=True))
        result=await provider.search('今日公开新闻')
        self.assertTrue(result['truncated'])
        self.assertIn('截断',result['summary'])
        self.assertIn('不代表完整网页',result['summary'])
        self.assertTrue(result['body_available'])

    async def test_truncation_notice_itself_is_not_treated_as_cached_article_body(self):
        provider=self.provider();self.handler=lambda _:httpx.Response(200,json=payload(body=False,truncated=True))
        result=await provider.search('今日公开新闻');await provider.search('今日公开新闻')
        self.assertFalse(result['body_available'])
        self.assertEqual(result['error'],'search_summary_truncated')
        self.assertEqual(len(self.requests),2)

    async def test_entry_limit_evicts_oldest_successful_result(self):
        provider=self.provider()
        for index in range(SEARCH_CACHE_ENTRIES+1):await provider.search('公开新闻'+str(index))
        self.assertEqual(len(provider._cache),SEARCH_CACHE_ENTRIES)
        self.assertLessEqual(provider._cache_bytes,SEARCH_CACHE_BYTES)
        await provider.search('公开新闻0')
        self.assertEqual(len(self.requests),SEARCH_CACHE_ENTRIES+2)

    async def test_byte_limit_and_oversized_result_do_not_expand_cache(self):
        provider=self.provider();large={'ok':True,'sources':[],'summary':'A'*400000,'retrieved_at':'ORIGINAL'}
        with patch.object(provider,'_request',new=AsyncMock(return_value=large)) as request:
            for index in range(3):await provider.search('公开新闻'+str(index))
            self.assertEqual(len(provider._cache),2)
            self.assertLessEqual(provider._cache_bytes,SEARCH_CACHE_BYTES)
            await provider.search('公开新闻0');self.assertEqual(request.await_count,4)
        huge={**large,'summary':'A'*(SEARCH_CACHE_BYTES+1)}
        with patch.object(provider,'_request',new=AsyncMock(return_value=huge)) as request:
            await provider.search('超大公开新闻');await provider.search('超大公开新闻')
            self.assertEqual(request.await_count,2)

    async def test_http_client_is_reused_until_explicit_close(self):
        provider=self.provider()
        await provider.search('第一条公开新闻');await provider.search('第二条公开新闻')
        self.assertEqual(self.make.call_count,1)
        self.assertFalse(self.clients[0].is_closed)
        await provider.aclose();self.assertTrue(self.clients[0].is_closed)

    async def test_private_query_missing_key_and_closed_provider_create_no_http_attempts(self):
        provider=self.provider()
        self.assertEqual((await provider.search('密码：private'))['error'],'invalid_or_private_query')
        provider.key='';self.assertEqual((await provider.search('今日公开新闻'))['error'],'missing_deepseek_key')
        self.assertEqual(len(self.requests),0);self.assertEqual(len(provider.attempts),0)
        self.assertEqual(self.make.call_count,0)


if __name__=='__main__':unittest.main()
