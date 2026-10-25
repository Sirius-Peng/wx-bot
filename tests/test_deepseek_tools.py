"""1017 wire contracts using only synthetic httpx transports, no paid API calls."""
import asyncio
from datetime import datetime, timezone
import inspect
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.core.deepseek_tools import (
    DeepSeekSearchFetch, SEARCH_ENDPOINT, FETCH_ENDPOINT, RESPONSE_BYTES,
    MAX_CONTENT_CHARS, MAX_SUMMARY_CHARS, MAX_SNIPPET_CHARS, MAX_FETCH_CHARS,
    parse_search, parse_fetch, public_fetch_url,
)

URL = 'https://example.com/article?id=17&lang=zh'


def payload(**changes):
    entry = {'title': '公开新闻', 'url': URL, 'content': '网页原文说明活动今天开始。',
             'summary': '搜索摘要说明活动今天开始。', 'snippet': '简短片段',
             'date': '2026-10-25T00:00:00Z', 'site_name': '公开站点',
             'authority': 3, 'rerank_score': 2.7}
    entry.update(changes)
    return {'results': [entry]}


class ParserTests(unittest.TestCase):
    def test_fields_and_evidence_are_not_collapsed_into_fake_fulltext(self):
        result = parse_search(payload())
        source = result['sources'][0]
        self.assertEqual(source['content'], '网页原文说明活动今天开始。')
        self.assertEqual(source['summary'], '搜索摘要说明活动今天开始。')
        self.assertEqual(source['snippet'], '简短片段')
        self.assertEqual(source['date'], source['published_at'])
        self.assertEqual((source['site_name'], source['authority'], source['rerank_score']), ('公开站点', 3, 2.7))
        self.assertEqual(source['evidence_kind'], 'content')
        self.assertEqual(result['summary'], '')
        self.assertTrue(result['full_page_available'])

    def test_summary_snippet_title_only_types(self):
        for updates, kind, body, evidence in [
            ({'content': ''}, 'summary', True, True),
            ({'content': '', 'summary': ''}, 'snippet', False, True),
            ({'content': '', 'summary': '', 'snippet': ''}, 'title_only', False, False),
        ]:
            result = parse_search(payload(**updates))
            self.assertTrue(result['ok'])
            self.assertEqual(result['sources'][0]['evidence_kind'], kind)
            self.assertEqual(result['body_available'], body)
            self.assertEqual(result['evidence_available'], evidence)
            self.assertFalse(result['full_page_available'])
            self.assertEqual(result['sources'][0]['content'], '')

    def test_rank_dedupe_limit_and_public_urls_only(self):
        entries = [payload(url=f'https://example.com/{n}', rerank_score=n / 10)['results'][0] for n in range(7)]
        entries += [payload(url='http://127.0.0.1/internal', rerank_score=3)['results'][0], entries[-1].copy()]
        result = parse_search({'results': entries})
        self.assertEqual([source['url'] for source in result['sources']], ['https://example.com/6', 'https://example.com/5', 'https://example.com/4'])

    def test_unknown_scores_are_not_made_up(self):
        for bad in (True, float('nan'), float('inf'), 4, -2, '2.7', None):
            result = parse_search(payload(rerank_score=bad, authority=bad))
            source = result['sources'][0]
            self.assertIsNone(source['rerank_score'])
            if bad != 4 and bad != -2 or isinstance(bad, bool):
                self.assertIsNone(source['authority'])
        self.assertEqual(parse_search(payload(authority=-1))['sources'][0]['authority'], -1)

    def test_total_evidence_has_fixed_bounds_and_content_truncation_is_explicit(self):
        entries = [payload(url=f'https://example.com/{n}', title='t' * 500, content='c' * 10000,
                           summary='s' * 10000, snippet='n' * 10000)['results'][0] for n in range(10)]
        result = parse_search({'results': entries})
        self.assertEqual(len(result['sources']), 3)
        self.assertFalse(result['full_page_available'])
        for source in result['sources']:
            self.assertEqual(len(source['content']), MAX_CONTENT_CHARS)
            self.assertEqual(len(source['summary']), MAX_SUMMARY_CHARS)
            self.assertEqual(len(source['snippet']), MAX_SNIPPET_CHARS)
            self.assertTrue(source['content_truncated'])
            self.assertTrue(source['summary_truncated'])
            self.assertTrue(source['snippet_truncated'])
            self.assertLessEqual(len(source['title']), 200)
        self.assertLessEqual(sum(len(s[k]) for s in result['sources'] for k in ('content', 'summary', 'snippet')), 15600)

    def test_empty_and_invalid_payloads_are_explicit(self):
        self.assertEqual(parse_search({'results': []})['error'], 'no_results')
        for value in (None, [], {}, {'results': None}, {'results': {}}):
            self.assertEqual(parse_search(value)['error'], 'invalid_response')
        self.assertFalse(parse_search({'results': [None, {}, {'url': 'javascript:alert(1)'}]})['ok'])

    def test_fetch_preserves_original_url_and_never_claims_unavailable_metadata(self):
        result = parse_fetch({'title': 'Markdown页面', 'content': '# 标题\n\n真正正文'}, URL)
        source = result['sources'][0]
        self.assertEqual(source['url'], URL)
        self.assertEqual(source['content'], '# 标题\n\n真正正文')
        self.assertEqual(source['published_at'], '')
        self.assertIsNone(source['authority'])
        self.assertIsNone(source['rerank_score'])
        self.assertEqual(source['evidence_kind'], 'content')

    def test_fetch_bound_empty_and_bad_response(self):
        result = parse_fetch({'content': 'x' * (MAX_FETCH_CHARS + 5)}, URL)
        self.assertFalse(result['full_page_available'])
        source = result['sources'][0]
        self.assertEqual(len(source['content']), MAX_FETCH_CHARS)
        self.assertTrue(source['content_truncated'])
        self.assertEqual(parse_fetch({'title': '标题', 'content': '   '}, URL)['error'], 'no_content')
        for value in (None, [], {}, {'content': None}, {'content': []}):
            self.assertEqual(parse_fetch(value, URL)['error'], 'invalid_response')

    def test_reject_nonpublic_and_private_urls_without_rewrite(self):
        blocked = [
            '', None, 'file:///etc/passwd', 'javascript:alert(1)', 'http://127.0.0.1',
            'http://192.168.1.1/a', 'http://10.1.1.1', 'http://169.254.169.254', 'http://[::1]/',
            'http://[::ffff:127.0.0.1]/', 'http://100.79.39.103', 'http://localhost',
            'http://localhost./a', 'https://service.local/a', 'https://service.internal/a',
            'https://service.home/a', 'http://router/a', 'http://2130706433/a', 'http://999.9.9.9/a', 'http://0x7f.0.0.1/a',
            'https://example.com/a?authToken=abc', 'https://example .com/a',
            'https://user:password@example.com/a', 'https://example.com:wrong/a',
            'https://example.com/a?access_token=abc', 'https://example.com/a?X-Amz-Signature=abc',
            'https://example.com/a?api%5Fkey=abc', 'https://example.com/a#token=abc',
            'https://example.com/sk-fixture123456', 'https://example.com/\nsecret',
            'https://example.com/a' + 'x' * 2048, ' https://example.com/a',
        ]
        for value in blocked:
            with self.subTest(value=value):self.assertEqual(public_fetch_url(value), '')
        self.assertEqual(public_fetch_url(URL + '#section'), URL + '#section')
        self.assertEqual(public_fetch_url('https://[2001:4860:4860::8888]/a'), 'https://[2001:4860:4860::8888]/a')


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers = []; self.clients = []; self.requests = []; self.handler = None
        async def dispatch(request):
            self.requests.append(request)
            if self.handler is None:
                return httpx.Response(200, json=payload() if request.url.path.endswith('/search') else {'title': '页面', 'content': '# 正文'})
            result = self.handler(request)
            return await result if inspect.isawaitable(result) else result
        def make(*args, **kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch), follow_redirects=True)
            self.clients.append(client); return client
        self.patch = patch('src.core.deepseek_tools.make_client', side_effect=make)
        self.factory = self.patch.start()

    async def asyncTearDown(self):
        for provider in self.providers:await provider.aclose()
        self.patch.stop()
        self.assertTrue(all(client.is_closed for client in self.clients))

    def provider(self, **kwargs):
        provider = DeepSeekSearchFetch(key='fixture-only-key', **kwargs)
        self.providers.append(provider); return provider

    async def test_search_wire_uses_official_non_llm_api_and_content_parameter(self):
        provider = self.provider(model='legacy-model-for-cache-only')
        self.assertTrue((await provider.search('site:example.com "两个  空格" -site:ads.com'))['ok'])
        request = self.requests[0]
        self.assertEqual(str(request.url), SEARCH_ENDPOINT)
        self.assertEqual(request.headers['authorization'], 'Bearer fixture-only-key')
        self.assertNotIn('x-api-key', request.headers)
        self.assertEqual(json.loads(request.content), {'query': 'site:example.com "两个  空格" -site:ads.com', 'count': 5, 'need_content': True})
        self.assertEqual(len(provider.metrics), 0)
        self.assertEqual(request.extensions['timeout'], {'connect': 4, 'read': 18, 'write': 18, 'pool': 1})
        self.assertTrue(provider.supports_fetch)

    async def test_query_bound_is_unicode_characters_without_truncation(self):
        provider = self.provider()
        query = '中' * 127 + '😀'
        self.assertTrue((await provider.search(query))['ok'])
        self.assertEqual(json.loads(self.requests[-1].content)['query'], query)
        for value in (query + '中', ' ', '', None, 'sk-fixture1234567', '邮箱 a@example.com', 'api_key:secret', 'null\x00input', '\ud800'):
            self.assertEqual((await provider.search(value))['error'], 'invalid_or_private_query')
        self.assertEqual(len(self.requests), 1)

    async def test_signed_private_urls_are_not_smuggled_inside_search_query(self):
        provider = self.provider()
        for query in ('搜索 https://example.com/reset?token=synthetic-secret',
                      'site:https://example.com/a?api_key=synthetic-secret',
                      '看看 https://user:key@example.com/path',
                      '搜索 http://127.0.0.1/private'):
            self.assertEqual((await provider.search(query))['error'], 'invalid_or_private_query')
        self.assertEqual(len(self.requests), 0)
        self.assertTrue((await provider.search('搜索 ' + URL))['ok'])

    async def test_internal_query_spaces_are_not_normalized_away(self):
        provider = self.provider()
        await provider.search(' "two  spaces" ')
        second = await provider.search('"two  spaces"')
        await provider.search('"two spaces"')
        self.assertTrue(second['local_result_reuse'])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([json.loads(r.content)['query'] for r in self.requests], ['"two  spaces"', '"two spaces"'])

    async def test_fetch_only_posts_url_to_official_endpoint_and_reuses_client(self):
        provider = self.provider()
        await provider.search('公开查询')
        result = await provider.fetch(URL)
        self.assertEqual(str(self.requests[1].url), FETCH_ENDPOINT)
        self.assertEqual(json.loads(self.requests[1].content), {'url': URL})
        self.assertEqual(result['sources'][0]['url'], URL)
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(len(self.requests), 2)

    async def test_no_automatic_fetch_after_search(self):
        provider = self.provider()
        self.handler = lambda _: httpx.Response(200, json=payload(content='', summary='', snippet='只有片段'))
        result = await provider.search('公开新闻')
        self.assertEqual(result['sources'][0]['evidence_kind'], 'snippet')
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(result['body_available'])
        self.assertEqual(len(provider._cache), 0)

    async def test_private_url_missing_key_closed_query_never_reach_network(self):
        provider = self.provider()
        for url in ('http://127.0.0.1', URL + '&token=secret', URL + '&x=' + 'a' * 2048):
            self.assertEqual((await provider.fetch(url))['error'], 'invalid_or_private_url')
        provider.key = ''
        self.assertEqual((await provider.search('公开新闻'))['error'], 'missing_deepseek_key')
        self.assertEqual((await provider.fetch(URL))['error'], 'missing_deepseek_key')
        provider.key = 'fixture-only-key'; await provider.aclose()
        self.assertEqual((await provider.search('公开新闻'))['error'], 'search_closed')
        self.assertEqual((await provider.fetch(URL))['error'], 'search_closed')
        self.assertEqual(len(self.requests), 0)

    async def test_successful_cache_ttl_copy_and_operation_isolation(self):
        provider = self.provider(); now = [0.]
        provider._cache_clock = lambda: now[0]
        first = await provider.search('公开新闻')
        first['sources'][0]['content'] = 'caller mutation'
        now[0] = 59.
        second = await provider.search('公开新闻')
        self.assertEqual(second['reuse_source'], 'ttl')
        self.assertEqual(second['retrieved_at'], first['retrieved_at'])
        self.assertNotEqual(second['sources'][0]['content'], 'caller mutation')
        now[0] = 60.
        await provider.search('公开新闻')
        await provider.fetch(URL); await provider.fetch(URL)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(provider.metrics), 0)
        self.assertTrue(all(event['kind'] == 'local_search_cache' for event in provider.cache_events))
        self.assertTrue(all(event['provider_kv_hit'] is None for event in provider.cache_events))

    async def test_cache_namespace_and_size_limits_are_inherited(self):
        provider = self.provider()
        for index in range(35):await provider.search(f'公开新闻{index}')
        self.assertEqual(len(provider._cache), 32)
        self.assertLessEqual(provider._cache_bytes, 1024 * 1024)
        provider.key = 'changed-fixture-key'; await provider.search('公开新闻34')
        provider.model = 'changed-model'; await provider.search('公开新闻34')
        provider.context_timezone = 'UTC'; await provider.search('公开新闻34')
        self.assertEqual(len(self.requests), 38)

    async def test_local_date_invalidates_cached_result(self):
        provider = self.provider(); day = [datetime(2026, 10, 25, tzinfo=timezone.utc)]
        with patch('src.core.web_search.local_datetime', side_effect=lambda _: day[0]):
            await provider.search('公开新闻')
            day[0] = datetime(2026, 10, 26, tzinfo=timezone.utc)
            await provider.search('公开新闻')
        self.assertEqual(len(self.requests), 2)

    async def test_instances_never_share_cache_or_key(self):
        first = self.provider(); second = self.provider()
        await first.search('公开新闻'); await second.search('公开新闻')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.factory.call_count, 2)

    async def test_title_only_empty_invalid_responses_are_not_cached(self):
        provider = self.provider()
        for response in (
            httpx.Response(200, json=payload(content='', summary='', snippet='')),
            httpx.Response(200, json={'results': []}),
            httpx.Response(200, json={}),
            httpx.Response(200, content=b'not json'),
        ):
            self.handler = lambda _, response=response: response
            await provider.search('公开新闻'); await provider.search('公开新闻')
            self.assertEqual(len(provider._cache), 0)
        self.assertEqual(len(self.requests), 8)

    async def test_fetch_empty_does_not_cache_title_or_emit_model_tokens(self):
        provider = self.provider(); observed = []; provider.usage_observer = observed.append
        self.handler = lambda _: httpx.Response(200, json={'title': '标题', 'content': ''})
        first = await provider.fetch(URL); await provider.fetch(URL)
        self.assertEqual(first['error'], 'no_content')
        self.assertEqual(len(provider._cache), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(event['kind'] == 'tool_request' and event['billable_result'] is False for event in observed))
        self.assertEqual(len(provider.metrics), 0)

    async def test_explicit_http_error_codes_have_no_retry_or_legacy_fallback(self):
        provider = self.provider()
        for status in (301, 302, 307, 400, 401, 402, 403, 404, 422):
            before = len(self.requests)
            self.handler = lambda _, status=status: httpx.Response(status, json={'error': 'fixture-only-key'}, headers={'Location': 'https://redirect.invalid'})
            result = await provider.search('公开新闻')
            self.assertEqual(result['error'], 'search_http_' + str(status))
            self.assertEqual(len(self.requests), before + 1)
            self.assertNotIn('fixture-only-key', json.dumps(result))
        self.assertTrue(all(str(request.url) == SEARCH_ENDPOINT for request in self.requests))

    async def test_retry_only_transient_statuses_once_and_records_both_requests(self):
        provider = self.provider(); provider._sleep = AsyncMock()
        for status in (408, 429, 500, 502, 503, 599):
            count = [0]
            def handler(_, status=status):
                count[0] += 1
                return httpx.Response(status, headers={'Retry-After': '99'}) if count[0] == 1 else httpx.Response(200, json=payload())
            self.handler = handler
            self.assertTrue((await provider.search(f'公开新闻{status}'))['ok'])
            self.assertEqual(count[0], 2)
        self.assertEqual([call.args[0] for call in provider._sleep.call_args_list], [1.] * 6)
        self.assertEqual(len(provider.tool_attempts), 12)
        self.assertEqual(len({event['request_id'] for event in provider.tool_attempts}), 12)
        self.assertEqual(sum(event['billable_result'] for event in provider.tool_attempts), 6)
        self.assertEqual(len(provider.metrics), 0)

    async def test_persistent_rate_limit_stops_after_second_attempt_without_cache(self):
        provider = self.provider(); provider._sleep = AsyncMock()
        self.handler = lambda _: httpx.Response(429, headers={'Retry-After': '0'})
        self.assertEqual((await provider.fetch(URL))['error'], 'fetch_http_429')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(provider._cache), 0)
        self.assertEqual([event['status'] for event in provider.tool_attempts], ['error', 'error'])

    async def test_retry_after_bounds_for_numeric_dates_invalid_values(self):
        for value, expected in [('5', 1.), ('-4', 0.), ('0.15', .15), ('nan', .25), ('inf', .25), (None, .25), ('invalid', .25)]:
            self.assertEqual(DeepSeekSearchFetch._retry_delay(value), expected)
        self.assertEqual(DeepSeekSearchFetch._retry_delay('Thu, 01 Jan 1970 00:00:00 GMT'), 0.)
        self.assertEqual(DeepSeekSearchFetch._retry_delay('Tue, 01 Jan 2100 00:00:00 GMT'), 1.)

    async def test_transport_failure_no_extra_request_no_sensitive_exception_text(self):
        provider = self.provider()
        def handler(request):raise httpx.ReadTimeout('fixture-only-key private query', request=request)
        self.handler = handler
        with self.assertLogs('src.core.deepseek_tools', level='INFO') as logs:
            result = await provider.search('公开新闻')
        self.assertEqual(result['error'], 'search_network_error')
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('fixture-only-key', '\n'.join(logs.output))
        self.assertNotIn('private query', '\n'.join(logs.output))
        self.assertEqual(provider.tool_attempts[-1]['error_type'], 'ReadTimeout')
        self.assertIsNone(provider.tool_attempts[-1]['billable_result'])

    async def test_total_twenty_second_deadline_covers_request_and_cleans_up_unknown_result(self):
        provider = self.provider(); entered = asyncio.Event(); cleaned = asyncio.Event()
        async def handler(_):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cleaned.set()
        self.handler = handler
        original_wait_for = asyncio.wait_for
        async def bounded(awaitable, timeout):
            self.assertEqual(timeout, 20)
            return await original_wait_for(awaitable, timeout=.01)
        with patch('src.core.deepseek_tools.asyncio.wait_for', side_effect=bounded):
            self.assertEqual((await provider.search('公开新闻'))['error'], 'search_network_error')
        self.assertTrue(entered.is_set())
        self.assertTrue(cleaned.is_set())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(provider.tool_attempts[-1]['status'], 'cancelled')
        self.assertIsNone(provider.tool_attempts[-1]['billable_result'])
        self.assertEqual(len(provider._cache), 0)

    async def test_cancellation_during_retry_backoff_prevents_second_http_call(self):
        provider = self.provider(); entered = asyncio.Event(); cleanup = asyncio.Event()
        self.handler = lambda _: httpx.Response(429, headers={'Retry-After': '1'})
        async def sleep(_):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cleanup.set()
        provider._sleep = sleep
        task = asyncio.create_task(provider.search('公开新闻')); await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await provider.aclose()
        self.assertTrue(cleanup.is_set())
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(provider.tool_attempts[-1]['billable_result'])
        self.assertEqual(len(provider._cache), 0)

    async def test_response_limit_prevents_parsing_or_caching_large_response(self):
        provider = self.provider()
        self.handler = lambda _: httpx.Response(200, content=b'x' * (RESPONSE_BYTES + 1))
        result = await provider.search('公开新闻')
        self.assertEqual(result['error'], 'response_too_large')
        self.assertIsNone(provider.tool_attempts[-1]['billable_result'])
        self.assertEqual(len(provider._cache), 0)
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_schema_and_json_do_not_claim_a_known_empty_billing_result(self):
        provider = self.provider()
        for response in (httpx.Response(200, json={'unexpected': True}), httpx.Response(200, content=b'not json')):
            self.handler = lambda _, response=response: response
            self.assertEqual((await provider.search('公开新闻'))['error'], 'invalid_response')
            self.assertIsNone(provider.tool_attempts[-1]['billable_result'])
            self.assertIsNone(provider.tool_attempts[-1]['result_count'])
        self.assertEqual(len(self.requests), 2)

    async def test_only_http200_nonempty_results_are_billable_without_token_guessing(self):
        provider = self.provider(); observed = []; provider.usage_observer = observed.append
        await provider.search('公开新闻')
        await provider.fetch(URL)
        self.handler = lambda _: httpx.Response(200, json={'results': []})
        await provider.search('空结果')
        self.assertEqual([event['billable_result'] for event in observed], [True, True, False])
        self.assertEqual([event['result_count'] for event in observed], [1, 1, 0])
        self.assertEqual([event['operation'] for event in observed], ['search', 'fetch', 'search'])
        for event in observed:
            self.assertEqual(event['kind'], 'tool_request')
            self.assertIn('elapsed_ms', event)
            self.assertIn('request_id', event)
            self.assertEqual(event['http_status'], 200)
            for absent in ('prompt_tokens', 'completion_tokens', 'cache_hit_tokens', 'query', 'url', 'key', 'body'):
                self.assertNotIn(absent, event)

    async def test_tool_logs_observer_and_local_cache_never_expose_input(self):
        provider = self.provider(); observed = []; provider.usage_observer = observed.append
        with self.assertLogs('src.core.deepseek_tools', level='INFO') as logs:
            await provider.search('公开独特测试字符串')
            await provider.search('公开独特测试字符串')
        self.assertEqual([event['kind'] for event in observed], ['tool_request', 'local_search_cache'])
        text = '\n'.join(logs.output) + json.dumps(observed)
        for private in ('fixture-only-key', '公开独特测试字符串', URL):self.assertNotIn(private, text)
        self.assertIn('WEB_TOOL_REQUEST ', text)

    async def test_observer_failure_does_not_retry_completed_tool(self):
        provider = self.provider()
        def observer(_):raise RuntimeError('private observer details')
        provider.usage_observer = observer
        with self.assertLogs('src.core.web_search', level='WARNING') as logs:
            self.assertTrue((await provider.search('公开新闻'))['ok'])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('private observer details', '\n'.join(logs.output))

    async def test_single_flight_for_search_and_fetch_return_independent_data(self):
        provider = self.provider()
        for operation in ('search', 'fetch'):
            entered = asyncio.Event(); release = asyncio.Event()
            async def handler(_):
                entered.set(); await release.wait()
                return httpx.Response(200, json=payload() if operation == 'search' else {'content': '# 正文'})
            self.handler = handler
            method = provider.search if operation == 'search' else provider.fetch
            value = '并行公开新闻' if operation == 'search' else URL
            first = asyncio.create_task(method(value)); await entered.wait()
            second = asyncio.create_task(method(value)); await asyncio.sleep(0)
            release.set(); results = await asyncio.gather(first, second)
            self.assertEqual(results[1]['reuse_source'], 'single_flight')
            results[0]['sources'][0]['content'] = 'mutation'
            self.assertNotEqual(results[1]['sources'][0]['content'], 'mutation')
        self.assertEqual(len(self.requests), 2)

    async def test_cancelling_one_waiter_does_not_cancel_live_shared_request(self):
        provider = self.provider(); entered = asyncio.Event(); release = asyncio.Event(); cancelled = asyncio.Event()
        async def handler(_):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:cancelled.set(); raise
            return httpx.Response(200, json=payload())
        self.handler = handler
        first = asyncio.create_task(provider.search('并行公开新闻')); await entered.wait()
        second = asyncio.create_task(provider.search('并行公开新闻')); await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):await first
        self.assertFalse(cancelled.is_set())
        release.set(); self.assertTrue((await second)['ok'])
        self.assertEqual(len(self.requests), 1)

    async def test_last_waiter_cancel_closes_request_and_no_background_retry(self):
        provider = self.provider(); entered = asyncio.Event(); cancelled = asyncio.Event()
        async def handler(_):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.handler = handler
        first = asyncio.create_task(provider.fetch(URL)); await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):await first
        await provider.aclose()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(provider.tool_attempts[-1]['status'], 'cancelled')
        self.assertIsNone(provider.tool_attempts[-1]['billable_result'])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(provider.metrics), 0)

    async def test_close_cancels_all_flights_and_closes_reused_client_idempotently(self):
        provider = self.provider(); entered = asyncio.Event()
        async def handler(_):entered.set(); await asyncio.Event().wait()
        self.handler = handler
        task = asyncio.create_task(provider.search('公开新闻')); await entered.wait()
        await provider.aclose()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(self.clients[0].is_closed)
        self.assertEqual(len(provider._flights), 0)
        await provider.aclose()


if __name__ == '__main__':unittest.main()
