"""Independent 1017 search/fetch integration checks; synthetic sources only.

No production data, API keys, external HTTP or paid model calls are used here.
The canned assistant outputs verify orchestration, not a model quality score.
"""
from collections import deque
import asyncio
import copy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.core.context import approx_tokens, SEARCH_BUDGET
from src.core.llm import LLMGateway, ProviderCfg
from src.core.web_search import SEARCH_RULES
from src.runtime import BotRuntime


URL = 'https://example.com/public-announcement'
OTHER = 'https://example.org/follow-up'
TITLE = '合成通行公告'
BODY = '周五早高峰开放两条车道，通行前必须预约。周末安排尚未公布，不能据此判断全天开放。'
FINAL = '这次确定的是周五早高峰开放两条车道，而且需要提前预约。周末怎么安排还没有公布，不能当成全天开放。'


def tool(name, args, identifier='review-tool-1'):
    dumped = {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
    return NS(id=identifier, function=NS(**dumped['function']),
              model_dump=lambda **kwargs: copy.deepcopy(dumped))


def response(text='', tools=None):
    return NS(choices=[NS(message=NS(content=text, tool_calls=tools,
                                    reasoning_content=None))],
              usage=NS(prompt_tokens=100, completion_tokens=30,
                       prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20))


def evidence(*, body=BODY, summary='', snippet='周五试运行，需预约。', kind=None):
    source = {'url': URL, 'title': TITLE, 'content': body, 'summary': summary,
              'snippet': snippet, 'published_at': '2026-10-25T00:00:00+00:00',
              'evidence_kind': kind or ('content' if body else 'summary' if summary else 'snippet')}
    return {'ok': True, 'sources': [source], 'summary': '',
            'body_available': bool(body)}


class Search1017IntegrationReview(unittest.IsolatedAsyncioTestCase):
    def gateway(self, responses, result=None):
        provider = NS(supports_fetch=True,
                      search=AsyncMock(return_value=result or evidence()),
                      fetch=AsyncMock(return_value=evidence()))
        gateway = LLMGateway(ProviderCfg('primary', 'https://api.deepseek.com',
                                        'fixture-not-a-real-key', 'deepseek-flash'),
                             web_search=provider)
        create = AsyncMock(side_effect=responses)
        gateway._client = lambda cfg: NS(chat=NS(completions=NS(create=create)))
        return gateway, provider, create

    async def test_content_answer_is_one_bubble_not_a_title_url_wall_or_extra_fetch(self):
        gateway, provider, create = self.gateway([
            response('我查一下', [tool('web_search', {'query': '合成通行安排'})]),
            response(FINAL + ' 来源：' + TITLE + ' ' + URL + '; ' + TITLE + ' ' + URL)])
        outputs = [part async for part in gateway.stream(
            [{'role': 'user', 'content': '联网核实今天的通行安排'}], chat_id='fixture')]
        self.assertEqual(len(outputs), 1)
        self.assertIn('两条车道', outputs[0]); self.assertIn('预约', outputs[0])
        self.assertEqual(outputs[0].count(URL), 1)
        self.assertNotIn('我查一下', outputs[0]); self.assertNotIn(TITLE, outputs[0])
        provider.search.assert_awaited_once(); provider.fetch.assert_not_awaited()
        self.assertEqual(create.await_count, 2, 'Good evidence needs one synthesis, no fixed review.')

    async def test_snippet_only_evidence_is_not_promoted_to_page_body(self):
        data = evidence(body='', snippet='周五试运行，需预约。', kind='snippet')
        gateway, provider, create = self.gateway([
            response('', [tool('web_search', {'query': '合成公告'})]),
            response('目前只有搜索片段能支持周五试运行和预约要求；我还没有拿到文章正文，其他细节仍待核实。')], data)
        answer = await gateway.chat([{'role': 'user', 'content': '查一下合成公告'}], chat_id='fixture')
        wire = create.call_args_list[1].kwargs['messages']
        inserted = json.loads(next(message['content'] for message in wire if message['role'] == 'tool'))
        source = inserted['sources'][0]
        self.assertEqual(source['content'], ''); self.assertEqual(source['evidence_kind'], 'snippet')
        self.assertIn('片段', answer); self.assertIn('没有拿到文章正文', answer)
        self.assertIn('只有摘要/片段不声称已读全文', SEARCH_RULES)
        provider.fetch.assert_not_awaited()

    async def test_long_summary_is_available_evidence_without_claiming_full_article(self):
        data = evidence(body='', summary=BODY, snippet='', kind='summary')
        gateway, provider, create = self.gateway([
            response('', [tool('web_search', {'query': '合成通行公告'})]),
            response('依据搜索返回的长摘要，周五早高峰开放两条车道并需要预约；这不是已核对的完整原文，周末安排仍未知。')], data)
        answer = await gateway.chat([{'role': 'user', 'content': '查合成公告内容'}], chat_id='fixture')
        self.assertIn('长摘要', answer)
        self.assertEqual(create.await_count, 2); provider.fetch.assert_not_awaited()
        source = json.loads(create.call_args.kwargs['messages'][-1]['content'])['sources'][0]
        self.assertEqual(source['summary'], BODY); self.assertEqual(source['content'], '')

    async def test_current_user_url_can_fetch_directly_without_keyword_search(self):
        gateway, provider, create = self.gateway([
            response('', [tool('web_fetch', {'url': URL})]), response(FINAL)])
        output = await gateway.chat([{'role': 'user', 'content': '请读这篇文章并概括：' + URL}], chat_id='fixture')
        provider.fetch.assert_awaited_once_with(URL); provider.search.assert_not_awaited()
        self.assertIn('两条车道', output); self.assertEqual(create.await_count, 2)
        offered = {value['function']['name'] for value in create.call_args_list[0].kwargs['tools']}
        self.assertEqual(offered, {'web_search', 'web_fetch'})

    async def test_historical_link_does_not_authorize_a_new_fetch(self):
        gateway, provider, create = self.gateway([
            response('', [tool('web_fetch', {'url': URL})]), response('当前问题没有提供需要读取的公开链接。')])
        await gateway.chat([{'role': 'user', 'content': '上周提过 ' + URL},
                            {'role': 'assistant', 'content': '知道了'},
                            {'role': 'user', 'content': '今天先聊别的'}], chat_id='fixture')
        provider.fetch.assert_not_awaited()
        tool_result = json.loads(create.call_args.kwargs['messages'][-1]['content'])
        self.assertEqual(tool_result['error'], 'url_not_in_current_turn')

    async def test_search_returned_url_permits_one_follow_up_fetch_and_total_budget_is_two(self):
        gateway, provider, create = self.gateway([
            response('', [tool('web_search', {'query': '合成公告'})]),
            response('', [tool('web_fetch', {'url': URL}, 'review-tool-2')]),
            response('', [tool('web_search', {'query': '额外不应执行'}, 'review-tool-3')]),
            response(FINAL)])
        await gateway.chat([{'role': 'user', 'content': '请查找并阅读合成公告'}], chat_id='fixture')
        self.assertEqual(provider.search.await_count + provider.fetch.await_count, 2)
        provider.fetch.assert_awaited_once_with(URL)
        self.assertEqual(create.call_args_list[2].kwargs['tool_choice'], 'none')

    async def test_parallel_tool_declarations_are_limited_to_two_including_fetch(self):
        gateway, provider, create = self.gateway([
            response('', [tool('web_search', {'query': '合成公告'}),
                          tool('web_fetch', {'url': URL}, 'review-tool-2'),
                          tool('web_fetch', {'url': OTHER}, 'review-tool-3')]), response(FINAL)])
        await gateway.chat([{'role': 'user', 'content': '这两篇说什么 ' + URL + ' ' + OTHER}], chat_id='fixture')
        self.assertEqual(provider.search.await_count + provider.fetch.await_count, 2)
        accepted = [message for message in create.call_args.kwargs['messages'] if message['role'] == 'tool']
        self.assertEqual(len(accepted), 2)

    async def test_large_raw_content_is_within_one_whole_turn_evidence_budget(self):
        data = evidence(body='合成正文。' * 12000, summary='摘要资料。' * 2000)
        gateway, provider, create = self.gateway([
            response('', [tool('web_search', {'query': '公开资料'}, 'x' * 128),
                          tool('web_search', {'query': '相关资料'}, 'y' * 128)]), response(FINAL)], data)
        await gateway.chat([{'role': 'user', 'content': '请查合成公开资料'}], chat_id='fixture')
        work = create.call_args.kwargs['messages']
        boundary = next(index for index, message in enumerate(work)
                        if message.get('content') == SEARCH_RULES)
        used = approx_tokens(json.dumps(work[boundary], ensure_ascii=False))
        used += sum(approx_tokens(json.dumps(message, ensure_ascii=False)) for message in work[boundary + 1:]
                    if message['role'] == 'tool')
        used += approx_tokens(json.dumps(gateway._search_review_message(), ensure_ascii=False))
        self.assertLessEqual(used, SEARCH_BUDGET)
        self.assertEqual(gateway.last_search_budget['limit'], SEARCH_BUDGET)
        self.assertLessEqual(gateway.last_search_budget['used'] + gateway.last_search_budget['reserved_review'], SEARCH_BUDGET)
        payloads = [json.loads(message['content']) for message in work if message['role'] == 'tool']
        for result in payloads:
            if result.get('ok'):
                self.assertTrue(any(source.get('content_truncated') for source in result['sources']))

    async def test_mixed_media_order_and_stable_prefix_survive_fetch(self):
        gateway, provider, create = self.gateway([
            response('', [tool('web_fetch', {'url': URL})]), response(FINAL)])
        parts = [{'type': 'text', 'text': '结合两张图和视频附件总结 ' + URL},
                 {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
                 {'type': 'text', 'text': '[视频附件：没有提供视频画面，不猜测内容]'},
                 {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,BBBB'}}]
        messages = [{'role': 'system', 'content': '稳定人格'}, {'role': 'user', 'content': '上一轮'},
                    {'role': 'assistant', 'content': '上一轮的原始回答'}, {'role': 'user', 'content': parts}]
        original = copy.deepcopy(messages)
        answer = await gateway.chat(messages, chat_id='fixture')
        for sent in create.call_args_list:
            self.assertEqual(sent.kwargs['messages'][:len(original)], original)
        self.assertEqual(messages, original); self.assertIn('预约', answer)
        provider.fetch.assert_awaited_once(); provider.search.assert_not_awaited()

    async def test_cancel_fetch_does_not_synthesize_or_send_a_partial_bubble(self):
        gateway, provider, create = self.gateway([response('', [tool('web_fetch', {'url': URL})])])
        entered = asyncio.Event(); cleaned = asyncio.Event()
        async def pending(url):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cleaned.set()
        provider.fetch.side_effect = pending
        async def consume():
            return [part async for part in gateway.stream(
                [{'role': 'user', 'content': '总结 ' + URL}], chat_id='fixture')]
        task = asyncio.create_task(consume()); await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(cleaned.is_set()); self.assertEqual(create.await_count, 1)


class Search1017AccountingReview(unittest.TestCase):
    def test_raw_tool_requests_never_pollute_model_cache_denominator_or_token_usage(self):
        runtime = BotRuntime.__new__(BotRuntime); runtime._usage_events = deque(maxlen=500)
        runtime._observe_usage({'kind': 'usage', 'request_id': 'model-1', 'task': 'chat',
                                'prompt_tokens': 100, 'cache_hit_tokens': 80,
                                'cache_miss_tokens': 20, 'completion_tokens': 30})
        runtime._observe_usage({'kind': 'request', 'request_id': 'model-1', 'task': 'chat', 'status': 'success'})
        for identifier, operation, code, status, billable in [
                ('tool-1', 'search', 200, 'success', True),
                ('tool-2', 'fetch', 200, 'success', True),
                ('tool-3', 'search', 200, 'success', False),
                ('tool-4', 'fetch', 403, 'error', False),
                ('tool-5', 'fetch', None, 'cancelled', False),
                ('tool-6', 'fetch', 200, 'error', None)]:
            runtime._observe_usage({'kind': 'tool_request', 'request_id': identifier, 'operation': operation,
                                    'http_status': code, 'status': status, 'billable_result': billable,
                                    'prompt_tokens': 999999})
        runtime._observe_usage({'kind': 'local_search_cache', 'task': 'search', 'prompt_tokens': 888888})
        result = runtime.usage_diagnostics()
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['summary']['fields']['prompt_tokens']['observed_sum'], 100)
        self.assertEqual(result['summary']['cache']['overall_hit_ratio'], .8)
        self.assertEqual(result['summary']['cache']['unknown_calls'], 0)
        self.assertEqual(result['tool_requests']['attempts'], 6)
        self.assertEqual(result['tool_requests']['operations']['search']['results_returned'], 1)
        self.assertEqual(result['tool_requests']['operations']['fetch'], {
            'attempts': 4, 'results_returned': 1, 'failed': 2, 'cancelled': 1, 'result_unknown': 1})
        self.assertEqual(result['local_search_cache'], 1)


class Search1017RawAPIReview(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.core.deepseek_tools import DeepSeekSearchFetch
        self.provider = DeepSeekSearchFetch('fixture-not-a-real-key')
        self.requests = []; self.handler = None; self.observed = []
        async def dispatch(request):
            self.requests.append(request)
            if self.handler is not None:
                value = self.handler(request)
                if hasattr(value, '__await__'): return await value
                return value
            if request.url.path.endswith('/fetch'):
                return httpx.Response(200, json={'title': TITLE, 'content': '# 公告\n\n' + BODY})
            return httpx.Response(200, json={'results': [{
                'url': URL, 'title': TITLE, 'content': BODY, 'summary': '', 'snippet': '需预约。',
                'rerank_score': 2.7, 'authority': 3, 'date': None, 'site_name': '合成站点'}]})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        self.factory = patch('src.core.deepseek_tools.make_client', return_value=self.client)
        self.factory.start(); self.provider.usage_observer = self.observed.append

    async def asyncTearDown(self):
        await self.provider.aclose(); self.factory.stop()
        if self.requests:
            self.assertTrue(self.client.is_closed)
        await self.client.aclose()  # An invalid query never asks the factory for it.

    async def test_search_wire_has_only_public_query_and_explicit_body_no_auxiliary_model(self):
        query = '合成公告 site:example.com lang:zh after:2026-10-01 "两  车道"'
        result = await self.provider.search(query)
        request = self.requests[0]; body = json.loads(request.content)
        self.assertEqual(str(request.url), 'https://api.deepseek.com/v1/tools/search')
        self.assertEqual(body, {'query': query, 'count': 5, 'need_content': True})
        self.assertEqual(request.headers['authorization'], 'Bearer fixture-not-a-real-key')
        self.assertNotIn('x-api-key', request.headers)
        self.assertEqual(result['sources'][0]['content'], BODY)
        self.assertEqual(result['sources'][0]['evidence_kind'], 'content')
        self.assertFalse(result['sources'][0]['published_at'])
        self.assertEqual(len(self.provider.metrics), 0)
        self.assertEqual(self.observed[0]['kind'], 'tool_request')
        self.assertTrue(self.observed[0]['billable_result'])
        for field in ('prompt_tokens', 'completion_tokens', 'cache_hit_tokens', 'model', 'query', 'url'):
            self.assertNotIn(field, self.observed[0])

    async def test_fetch_uses_markdown_endpoint_without_a_search_or_model_request(self):
        result = await self.provider.fetch(URL)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(str(self.requests[0].url), 'https://api.deepseek.com/v1/tools/fetch')
        self.assertEqual(json.loads(self.requests[0].content), {'url': URL})
        self.assertEqual(result['sources'][0]['content'], '# 公告\n\n' + BODY)
        self.assertEqual(self.observed[0]['operation'], 'fetch')
        self.assertTrue(self.observed[0]['billable_result']); self.assertFalse(self.provider.metrics)

    async def test_summary_only_and_snippet_only_keep_provenance_not_full_body(self):
        for field, kind in [('summary', 'summary'), ('snippet', 'snippet')]:
            self.handler = lambda _, f=field: httpx.Response(200, json={
                'results': [{'url': URL, 'title': TITLE, 'content': '', f: BODY}]})
            result = await self.provider.search('合成证据 ' + field)
            source = result['sources'][0]
            self.assertEqual(source['evidence_kind'], kind); self.assertEqual(source['content'], '')
            self.assertFalse(result['full_page_available'])

    async def test_snippet_only_is_not_cached_as_successful_article_body(self):
        self.handler = lambda _: httpx.Response(200, json={
            'results': [{'url': URL, 'title': TITLE, 'content': '', 'summary': '', 'snippet': BODY}]})
        await self.provider.search('仅片段'); await self.provider.search('仅片段')
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(self.provider._cache)

    async def test_128_unicode_character_query_boundary_has_no_silent_truncation(self):
        self.assertTrue((await self.provider.search('汉' * 128))['ok'])
        self.assertFalse((await self.provider.search('汉' * 129))['ok'])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0].content)['query'], '汉' * 128)

    async def test_private_or_nonpublic_fetch_urls_never_leave_the_process(self):
        for url in ['file:///etc/passwd', 'http://localhost./config', 'http://127.0.0.1/config',
                    'http://[::1]/', 'http://192.168.1.2/', 'https://user:pass@example.com/',
                    'https://example.com/reset?token=fixture-token',
                    'https://example.com/path?api_key=fixture-token',
                    'https://example.com/path#access_token=fixture-token']:
            self.assertFalse((await self.provider.fetch(url))['ok'], url)
        self.assertFalse(self.requests)

    async def test_private_signed_url_is_not_repackaged_as_search_keywords(self):
        result = await self.provider.search('阅读 https://example.com/reset?token=fixture-secret')
        self.assertFalse(result['ok']); self.assertFalse(self.requests)

    async def test_credentialed_redirect_is_not_followed_or_exposed_in_errors(self):
        self.handler = lambda _: httpx.Response(302, headers={'Location': 'https://other.invalid/'},
                                                json={'secret': 'fixture-not-a-real-key'})
        result = await self.provider.fetch(URL)
        self.assertFalse(result['ok']); self.assertEqual(len(self.requests), 1)
        self.assertNotIn('fixture-not-a-real-key', json.dumps(result))
        self.assertFalse(self.observed[0]['billable_result'])

    async def test_402_does_not_silently_fall_back_to_an_auxiliary_paid_model(self):
        self.handler = lambda _: httpx.Response(402, json={'error': {'message': 'fixture-not-a-real-key'}})
        result = await self.provider.search('公开公告')
        self.assertEqual(result['error'], 'search_http_402')
        self.assertEqual(len(self.requests), 1); self.assertFalse(self.provider.metrics)

    async def test_one_transient_retry_is_counted_separately_not_as_tokens(self):
        responses = [httpx.Response(429, headers={'Retry-After': '0'}),
                     httpx.Response(200, json={'results': [{'url': URL, 'title': TITLE, 'content': BODY}]})]
        self.handler = lambda _: responses.pop(0)
        result = await self.provider.search('公开公告')
        self.assertTrue(result['ok']); self.assertEqual(len(self.requests), 2)
        self.assertEqual([event['http_status'] for event in self.observed], [429, 200])
        self.assertEqual([event['billable_result'] for event in self.observed], [False, True])
        self.assertFalse(self.provider.metrics)

    async def test_empty_200_is_observed_but_not_a_billable_result_or_cached(self):
        self.handler = lambda _: httpx.Response(200, json={'results': []})
        for _ in range(2): self.assertFalse((await self.provider.search('公开公告'))['ok'])
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(event['result_count'] == 0 and event['billable_result'] is False
                            for event in self.observed))

    async def test_ttl_reuse_is_not_a_new_tool_request_or_provider_kv_hit(self):
        first = await self.provider.fetch(URL); second = await self.provider.fetch(URL)
        self.assertEqual(len(self.requests), 1); self.assertEqual(second['retrieved_at'], first['retrieved_at'])
        self.assertEqual([event['kind'] for event in self.observed], ['tool_request', 'local_search_cache'])
        self.assertIsNone(self.observed[1]['provider_kv_hit']); self.assertFalse(self.provider.metrics)

    async def test_cancellation_closes_fetch_and_emits_no_fake_model_usage(self):
        entered = asyncio.Event(); cleaned = asyncio.Event()
        async def hold(_):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cleaned.set()
        self.handler = hold
        task = asyncio.create_task(self.provider.fetch(URL)); await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        await self.provider.aclose()
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.observed[-1]['status'], 'cancelled'); self.assertFalse(self.provider.metrics)


if __name__ == '__main__':
    unittest.main()
