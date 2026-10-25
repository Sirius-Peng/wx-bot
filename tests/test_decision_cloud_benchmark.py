"""Cloud benchmark contracts using public synthetic DTOs and mocked SDK calls only.

No user configuration, API key, network connection, ONNX inference or remote host
is accessed. Passing tests exercise instrumentation, not model speed or quality.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.core.llm import LLMGateway
from src.core.types import WxMessage
from tools.decision_model import benchmark_cloud as module


def response(content='{"action":"reply"}', *, usage=None, finish='stop'):
    return SimpleNamespace(usage=usage, model='unit-test-flash-version', choices=[
        SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])


def sdk_client(create=None):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=create or AsyncMock(return_value=response()))), close=AsyncMock())


def case(identifier, text, label='reply', *, protected=False, recent=None):
    return {'id': identifier, 'session_id': 'session-'+identifier, 'slice': 'unit-fixture',
            'state': {'current': text, 'recent': recent or []}, 'label': label,
            'protected': protected}


def unit_message(row):
    return WxMessage(msg_id=row['id'], platform='onebot', chat_type='private',
        chat_id=row['session_id'], sender_id=row['session_id'], sender_name='unit-fixture',
        bot_id='unit-bot', content=row['state']['current'])


class SdkVersionContracts(unittest.TestCase):
    def test_versions_record_each_runtime_package(self):
        with patch.object(module, 'version', side_effect=lambda name: {
                'openai': 'unit-openai', 'httpx': 'unit-httpx', 'httpx2': 'unit-httpx2'}[name]):
            self.assertEqual(module.sdk_versions(), {'openai': 'unit-openai',
                'httpx': 'unit-httpx', 'httpx2': 'unit-httpx2'})

    def test_uninstalled_package_version_is_unknown_not_zero(self):
        def version(name):
            if name == 'httpx2':
                raise module.PackageNotFoundError(name)
            return 'unit-'+name
        with patch.object(module, 'version', side_effect=version):
            result = module.sdk_versions()
        self.assertIsNone(result['httpx2'])
        self.assertEqual(result['openai'], 'unit-openai')


class UsageNormalization(unittest.TestCase):
    def test_missing_usage_remains_unknown_instead_of_zero(self):
        normalized = module.normalize_usage(None)
        for name in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                     'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'reasoning_tokens'):
            self.assertIsNone(normalized[name])
        self.assertIsNone(normalized['cache_hit_field'])

    def test_attribute_and_dictionary_usage_agree(self):
        values = {'prompt_tokens': 125, 'completion_tokens': 7, 'total_tokens': 132,
                  'prompt_cache_hit_tokens': 100, 'prompt_cache_miss_tokens': 25,
                  'completion_tokens_details': {'reasoning_tokens': 0}}
        obj = SimpleNamespace(**{**values, 'completion_tokens_details':
                                 SimpleNamespace(reasoning_tokens=0)})
        self.assertEqual(module.normalize_usage(values), module.normalize_usage(obj))
        self.assertEqual(module.normalize_usage(obj)['cache_hit_field'], 'prompt_cache_hit_tokens')

    def test_zero_is_real_measurement_not_missing(self):
        result = module.normalize_usage({'prompt_tokens': 0, 'completion_tokens': 0,
            'total_tokens': 0, 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 0,
            'completion_tokens_details': {'reasoning_tokens': 0}})
        self.assertTrue(all(result[k] == 0 for k in result if k != 'cache_hit_field'))

    def test_rejects_boolean_negative_float_string_and_nonfinite_counts(self):
        for invalid in (True, False, -1, 1.0, '10', float('nan'), float('inf')):
            with self.subTest(value=repr(invalid)):
                result = module.normalize_usage({'prompt_tokens': invalid,
                    'completion_tokens': invalid, 'total_tokens': invalid,
                    'prompt_cache_hit_tokens': invalid, 'prompt_cache_miss_tokens': invalid,
                    'completion_tokens_details': {'reasoning_tokens': invalid}})
                self.assertTrue(all(result[k] is None for k in result))

    def test_cached_tokens_fallback_is_explicit(self):
        result = module.normalize_usage({'prompt_tokens_details': {'cached_tokens': 18}})
        self.assertEqual(result['prompt_cache_hit_tokens'], 18)
        self.assertEqual(result['cache_hit_field'], 'prompt_tokens_details.cached_tokens')
        self.assertIsNone(result['prompt_cache_miss_tokens'])

    def test_deepseek_field_takes_precedence_even_when_zero(self):
        result = module.normalize_usage({'prompt_cache_hit_tokens': 0,
                                         'prompt_tokens_details': {'cached_tokens': 18}})
        self.assertEqual(result['prompt_cache_hit_tokens'], 0)
        self.assertEqual(result['cache_hit_field'], 'prompt_cache_hit_tokens')

    def test_invalid_direct_cache_count_can_use_valid_nested_field(self):
        result = module.normalize_usage({'prompt_cache_hit_tokens': True,
                                         'prompt_tokens_details': {'cached_tokens': 12}})
        self.assertEqual(result['prompt_cache_hit_tokens'], 12)


class UsageAndLatencySummary(unittest.TestCase):
    def test_empty_usage_means_no_requests_and_no_unknown_requests(self):
        result = module.usage_summary([])
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(result['priced_calls'], 0)
        self.assertEqual(result['unpriced_calls'], 0)
        self.assertIsNone(result['cache_hit_share_of_observed_input'])
        self.assertIsNone(result['fields']['prompt_tokens']['mean_per_known_call'])

    def test_unknown_usage_retains_unknown_denominator_and_unpriced_call(self):
        known = module.normalize_usage({'prompt_tokens': 120, 'completion_tokens': 10,
            'total_tokens': 130, 'prompt_cache_hit_tokens': 100, 'prompt_cache_miss_tokens': 20})
        result = module.usage_summary([{'usage': known}, {'usage': module.normalize_usage(None)}])
        self.assertEqual(result['api_calls'], 2)
        self.assertEqual(result['fields']['prompt_tokens'], {'observed_sum': 120,
            'known_calls': 1, 'unknown_calls': 1, 'mean_per_known_call': 120})
        self.assertEqual(result['priced_calls'], 1)
        self.assertEqual(result['unpriced_calls'], 1)
        self.assertAlmostEqual(result['cache_hit_share_of_observed_input'], 100/120)
        self.assertIn('may still be billed', result['note'])

    def test_all_unknown_usage_is_not_presented_as_free(self):
        result = module.usage_summary([{'usage': module.normalize_usage(None)}])
        self.assertEqual(result['priced_calls'], 0)
        self.assertEqual(result['unpriced_calls'], 1)
        for values in result['fields'].values():
            self.assertEqual(values['known_calls'], 0)
            self.assertEqual(values['unknown_calls'], 1)
            self.assertIsNone(values['mean_per_known_call'])

    def test_cny_prices_are_per_million_not_per_token_or_usd(self):
        known = module.normalize_usage({'prompt_tokens': 2_000_000,
            'completion_tokens': 1_000_000, 'total_tokens': 3_000_000,
            'prompt_cache_hit_tokens': 1_000_000, 'prompt_cache_miss_tokens': 1_000_000})
        result = module.usage_summary([{'usage': known}])
        self.assertEqual(result['rates_cny_per_million'], {
            'off_peak': {'hit': .02, 'miss': 1., 'output': 4.},
            'peak': {'hit': .04, 'miss': 2., 'output': 8.}})
        self.assertAlmostEqual(result['estimated_cny_for_priced_calls']['off_peak'], 5.02)
        self.assertAlmostEqual(result['estimated_cny_for_priced_calls']['peak'], 10.04)
        self.assertEqual(result['price_as_of'], '2026-10-20')
        self.assertIn('/zh-cn/', result['price_source'])

    def test_missing_cache_split_is_unpriced_despite_known_total_input(self):
        known = module.normalize_usage({'prompt_tokens': 200, 'completion_tokens': 10})
        result = module.usage_summary([{'usage': known}])
        self.assertEqual(result['priced_calls'], 0)
        self.assertEqual(result['unpriced_calls'], 1)
        self.assertIsNone(result['cache_hit_share_of_observed_input'])

    def test_cache_share_requires_hit_and_miss_from_same_call(self):
        paired = module.normalize_usage({'prompt_cache_hit_tokens': 40,
                                        'prompt_cache_miss_tokens': 60})
        hit_only = module.normalize_usage({'prompt_cache_hit_tokens': 9000})
        miss_only = module.normalize_usage({'prompt_cache_miss_tokens': 8000})
        calls = [{'usage': usage} for usage in (paired, hit_only, miss_only)]
        result = module.usage_summary(calls)
        self.assertEqual(result['paired_cache_calls'], 1)
        self.assertEqual(result['unknown_cache_calls'], 2)
        self.assertAlmostEqual(result['cache_hit_share_of_observed_input'], .4)
        self.assertEqual(result['fields']['prompt_cache_hit_tokens']['observed_sum'], 9040)
        self.assertEqual(result['fields']['prompt_cache_miss_tokens']['observed_sum'], 8060)
        unpaired = module.usage_summary([{'usage': hit_only}])
        self.assertEqual(unpaired['paired_cache_calls'], 0)
        self.assertEqual(unpaired['unknown_cache_calls'], 1)
        self.assertIsNone(unpaired['cache_hit_share_of_observed_input'])

    def test_latency_empty_and_invalid_values_are_unknown(self):
        self.assertEqual(module.latency([None, True, False, -1, float('nan'), float('inf'), '10']),
            {'samples': 0, 'mean_ms': None, 'p50_ms': None, 'p95_ms': None, 'max_ms': None})

    def test_latency_uses_same_p95_index_as_existing_local_report(self):
        result = module.latency(list(reversed(range(1, 101))))
        self.assertEqual(result['samples'], 100)
        self.assertEqual(result['mean_ms'], 50.5)
        self.assertEqual(result['p50_ms'], 50.5)
        self.assertEqual(result['p95_ms'], 95)
        self.assertEqual(result['max_ms'], 100)
        self.assertEqual(module.latency([3])['p95_ms'], 3)


class GatewayContracts(unittest.IsolatedAsyncioTestCase):
    async def test_request_matches_production_payload_and_preserves_raw_usage(self):
        fake = sdk_client(AsyncMock(return_value=response(usage=SimpleNamespace(
            prompt_tokens=123, completion_tokens=8, prompt_cache_hit_tokens=100,
            prompt_cache_miss_tokens=23))))
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.case_id = 'unit/pass-1'
        with patch.object(gateway, '_client', return_value=fake):
            text = await gateway.complete_once('public unit prompt', max_tokens=40)
        self.assertEqual(text, '{"action":"reply"}')
        fake.chat.completions.create.assert_awaited_once_with(model='deepseek-flash',
            messages=[{'role': 'user', 'content': 'public unit prompt'}], max_tokens=40,
            temperature=.1, extra_body={'thinking': {'type': 'disabled'}})
        actual = gateway.calls[0]
        self.assertEqual(actual['status'], 'response')
        self.assertEqual(actual['case_id'], 'unit/pass-1')
        self.assertEqual(actual['usage']['prompt_tokens'], 123)
        self.assertIsNone(actual['usage']['total_tokens'])
        self.assertEqual(actual['model_returned'], 'unit-test-flash-version')
        self.assertFalse(actual['stream'])
        self.assertIsNone(actual['ttft_ms'])
        self.assertEqual(actual['prompt_sha256'], hashlib.sha256(b'public unit prompt').hexdigest())
        self.assertNotIn('public unit prompt', json.dumps(actual))
        self.assertNotIn('unit-test-not-a-key', json.dumps(actual))
        self.assertGreaterEqual(actual['elapsed_ms'], 0)

    async def test_production_and_measured_create_arguments_are_identical(self):
        primary = module.MeasuredGateway('unit-test-not-a-key')
        production = LLMGateway(primary.primary)
        left, right = sdk_client(), sdk_client()
        with patch.object(primary, '_client', return_value=left), \
                patch.object(production, '_client', return_value=right):
            await primary.complete_once('same unit prompt', max_tokens=40)
            await production.complete_once('same unit prompt', max_tokens=40)
        self.assertEqual(left.chat.completions.create.call_args,
                         right.chat.completions.create.call_args)

    async def test_sdk_client_disables_hidden_retries(self):
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        with patch('src.core.llm.AsyncOpenAI') as factory:
            gateway._new_client(gateway.primary)
        self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
        self.assertEqual(factory.call_args.kwargs['base_url'], 'https://api.deepseek.com')
        self.assertEqual(gateway.primary.timeout, 10.)

    async def test_exception_record_never_serializes_body_headers_or_key(self):
        secret = 'not-a-real-private-key-test-sentinel'
        class ProviderFailure(Exception):
            status_code = 429
        fake = sdk_client(AsyncMock(side_effect=ProviderFailure(
            'Authorization: Bearer '+secret+'; private server response')))
        gateway = module.MeasuredGateway(secret)
        with patch.object(gateway, '_client', return_value=fake):
            with self.assertRaises(ProviderFailure):
                await gateway.complete_once('public prompt', max_tokens=40)
        record = gateway.calls[0]
        self.assertEqual(record['status'], 'error')
        self.assertEqual(record['error_type'], 'ProviderFailure')
        self.assertEqual(record['http_status'], 429)
        self.assertIsNone(record['usage']['prompt_tokens'])
        self.assertNotIn(secret, json.dumps(record))
        self.assertNotIn('private server response', json.dumps(record))
        self.assertNotIn('Authorization', json.dumps(record))

    async def test_cancellation_preserves_unknown_usage_and_reraises(self):
        ready = asyncio.Event()
        async def blocked(**kwargs):
            ready.set()
            await asyncio.Event().wait()
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        fake = sdk_client(AsyncMock(side_effect=blocked))
        with patch.object(gateway, '_client', return_value=fake):
            task = asyncio.create_task(gateway.complete_once('public prompt', max_tokens=40))
            await ready.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        record = gateway.calls[0]
        self.assertEqual(record['status'], 'cancelled_deadline')
        self.assertIsNone(record['usage']['prompt_tokens'])
        self.assertIsNone(record['usage']['completion_tokens'])
        self.assertIn('finished_utc', record)

    async def test_empty_content_and_length_finish_are_recorded_without_invention(self):
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        fake = sdk_client(AsyncMock(return_value=response(None, finish='length')))
        with patch.object(gateway, '_client', return_value=fake):
            self.assertEqual(await gateway.complete_once('public prompt', max_tokens=40), '')
        self.assertEqual(gateway.calls[0]['finish_reason'], 'length')
        self.assertIsNone(gateway.calls[0]['usage']['completion_tokens'])


class LocalComparison(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'local.json'
        self.rows = [case('a', 'hello'), case('b', 'world')]
        self.report = {'dataset_sha256': module.DATASET_SHA256,
            'model': {'platform': 'Windows-unit-fixture'},
            'summary': {'actual_reply_decider_pipeline': {'records': 2}},
            'results': [{'id': r['id'], 'session_id': r['session_id'],
                'expected': r['label'], 'protected': r['protected'], 'slice': r['slice'],
                'pipeline': {'elapsed_ms': float(i+1)}} for i, r in enumerate(self.rows)]}

    def save(self):
        self.path.write_text(json.dumps(self.report), encoding='utf-8')

    def test_absent_local_report_is_explicitly_none(self):
        self.assertIsNone(module.compare_local(None, self.rows, []))

    def test_locked_fixture_hash_required(self):
        self.report['dataset_sha256'] = '0'*64
        self.save()
        with self.assertRaisesRegex(ValueError, 'local_comparison_fixture_mismatch'):
            module.compare_local(self.path, self.rows, [])

    def test_case_set_and_cardinality_both_required(self):
        self.report['results'][1]['id'] = 'unrelated'
        self.save()
        with self.assertRaisesRegex(ValueError, 'local_comparison_case_mismatch'):
            module.compare_local(self.path, self.rows, [])
        self.report['results'] = self.report['results'][:1]
        self.save()
        with self.assertRaisesRegex(ValueError, 'local_comparison_case_mismatch'):
            module.compare_local(self.path, self.rows, [])

    def test_case_metadata_is_bound_not_just_reused_case_ids(self):
        original = self.report['results'][0].copy()
        for key, wrong in (('session_id', 'different-session'), ('expected', 'defer'),
                           ('protected', True), ('slice', 'different-slice')):
            with self.subTest(field=key):
                self.report['results'][0] = {**original, key: wrong}
                self.save()
                with self.assertRaisesRegex(ValueError, 'local_comparison_case_fields_mismatch'):
                    module.compare_local(self.path, self.rows, [])
        self.report['results'][0] = original

    def test_local_report_accepts_windows_utf8_bom(self):
        self.path.write_text(json.dumps(self.report), encoding='utf-8-sig')
        result = module.compare_local(self.path, self.rows, [])
        self.assertEqual(result['all_turns']['samples'], 2)

    def test_same_cloud_requested_subset_excludes_rule_only_cases(self):
        self.save()
        result = module.compare_local(self.path, self.rows,
            [{'case_id': 'b/pass-1'}, {'case_id': 'b/pass-2'}])
        self.assertEqual(result['all_turns']['samples'], 2)
        self.assertEqual(result['same_cloud_requested_cases']['samples'], 1)
        self.assertEqual(result['same_cloud_requested_cases']['p50_ms'], 2.)
        self.assertEqual(result['api_billed_tokens'], 0)
        self.assertEqual(result['path_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result['platform'], 'Windows-unit-fixture')
        self.assertIn('not zero', result['note'])


class EvaluateContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    async def test_existing_output_rejected_before_gateway_construction(self):
        out = self.root/'existing'
        out.mkdir()
        with patch.object(module, 'read_cases', return_value=[case('a', 'hello')]), \
                patch.object(module, 'MeasuredGateway') as constructor:
            with self.assertRaisesRegex(ValueError, 'benchmark_output_exists'):
                await module.evaluate('unit-test-not-a-key', out)
        constructor.assert_not_called()

    async def test_empty_key_and_invalid_repeats_rejected_without_creating_output(self):
        with patch.object(module, 'read_cases') as read, \
                patch.object(module, 'MeasuredGateway') as constructor:
            for value in ('', '  ', None, 123):
                with self.subTest(key_type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError, 'empty_api_key'):
                        await module.evaluate(value, self.root/'new')
            for value in (0, 4, -1, True, False, '1', 1.0, 2.0):
                with self.subTest(repeats=repr(value)):
                    with self.assertRaisesRegex(ValueError, 'repeats_must_be_1_to_3'):
                        await module.evaluate('unit-test-not-a-key', self.root/'new', repeats=value)
        read.assert_not_called()
        constructor.assert_not_called()
        self.assertFalse((self.root/'new').exists())

    async def test_mock_run_uses_real_rules_and_only_one_ambiguous_request(self):
        rows = [case('rule', '帮我选一个', protected=True),
                case('ambiguous', '今天路上开了小花', recent=['昨天去散步']),
                case('ack', '收到', label='context_only')]
        fake = sdk_client(AsyncMock(return_value=response('{"action":"defer"}', usage=SimpleNamespace(
            prompt_tokens=220, completion_tokens=8, total_tokens=228,
            prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=220))))
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.aclose = AsyncMock()
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'message', side_effect=unit_message), \
                patch.object(gateway, '_client', return_value=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            report = await module.evaluate('unit-test-not-a-key', self.root/'experiment', gateway=gateway)
        fake.chat.completions.create.assert_awaited_once()
        self.assertEqual(report['rule_only_turns'], 2)
        self.assertEqual(report['usage']['api_calls'], 1)
        self.assertEqual(report['all_turn_latency']['samples'], 3)
        self.assertEqual(report['api_turn_latency']['samples'], 1)
        self.assertEqual(report['results'][0]['source'], 'rule')
        self.assertEqual(report['results'][1]['source'], 'cloud')
        self.assertEqual(report['results'][1]['action'], 'defer')
        self.assertEqual(report['results'][2]['action'], 'context_only')
        self.assertEqual(report['calls'][0]['case_id'], 'ambiguous/pass-1')
        self.assertEqual(fake.chat.completions.create.call_args.kwargs['max_tokens'], 8)
        prompt = fake.chat.completions.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('昨天去散步', prompt)
        self.assertNotIn('人格', prompt)
        self.assertEqual(report['protocol']['deadline_seconds'], 3.)
        self.assertEqual(report['protocol']['retries'], 0)
        gateway.aclose.assert_awaited_once()
        saved = json.loads((self.root/'experiment/report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['dataset_sha256'], module.DATASET_SHA256)
        lines = (self.root/'experiment/results.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(len(json.loads(lines[0])['calls']), 0)
        self.assertNotIn('unit-test-not-a-key', json.dumps(saved))

    async def test_invalid_cloud_json_is_counted_as_parse_failure_not_silent_success(self):
        rows = [case('bad-json', '今天沿着小路走了一会')]
        fake = sdk_client(AsyncMock(return_value=response('not JSON')))
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.aclose = AsyncMock()
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'message', side_effect=unit_message), \
                patch.object(gateway, '_client', return_value=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            report = await module.evaluate('unit-test-not-a-key', self.root/'parse-failure', gateway=gateway)
        self.assertEqual(report['parse_failure_after_response'], 1)
        self.assertEqual(report['results'][0]['source'], 'conservative')
        self.assertEqual(report['results'][0]['action'], 'reply')
        self.assertEqual(report['usage']['unpriced_calls'], 1)
        gateway.aclose.assert_awaited_once()

    async def test_rule_only_run_performs_zero_requests_and_has_unknown_cloud_latency(self):
        rows = [case('rule', '请解释一下', protected=True), case('ack', '好的', label='context_only')]
        fake = sdk_client()
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.aclose = AsyncMock()
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'message', side_effect=unit_message), \
                patch.object(gateway, '_client', return_value=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            report = await module.evaluate('unit-test-not-a-key', self.root/'rule-only', gateway=gateway)
        fake.chat.completions.create.assert_not_called()
        self.assertEqual(report['usage']['api_calls'], 0)
        self.assertEqual(report['rule_only_turns'], 2)
        self.assertEqual(report['api_turn_latency']['samples'], 0)
        self.assertIsNone(report['api_turn_latency']['p95_ms'])
        self.assertIsNone(report['first_api_request'])
        self.assertIsNone(report['later_api_requests']['p95_ms'])

    async def test_local_fixture_mismatch_blocks_requests_and_output_creation(self):
        rows = [case('x', '今天走了好久')]
        local = self.root/'bad-local.json'
        local.write_text(json.dumps({'dataset_sha256': 'wrong'}), encoding='utf-8')
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'MeasuredGateway') as constructor:
            with self.assertRaisesRegex(ValueError, 'local_comparison_fixture_mismatch'):
                await module.evaluate('unit-test-not-a-key', self.root/'blocked', local_report=local)
        constructor.assert_not_called()
        self.assertFalse((self.root/'blocked').exists())

    async def test_per_pass_usage_and_latency_do_not_mix_cached_or_unknown_calls(self):
        rows = [case('rule', '请帮忙', protected=True),
                case('ambiguous', '院子里飘来一阵花香')]
        outputs = [
            response(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=5,
                total_tokens=105, prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=100)),
            response(usage=None),
            response(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=7,
                total_tokens=107, prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20)),
        ]
        fake = sdk_client(AsyncMock(side_effect=outputs))
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.aclose = AsyncMock()
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'message', side_effect=unit_message), \
                patch.object(gateway, '_client', return_value=fake), \
                patch.object(module, 'sdk_versions', return_value={'openai': 'mock-only'}), \
                contextlib.redirect_stdout(io.StringIO()):
            report = await module.evaluate('unit-test-not-a-key', self.root/'by-pass',
                                           repeats=3, gateway=gateway)
        self.assertTrue(report['complete'])
        self.assertIsNone(report['abort_reason'])
        self.assertEqual(report['sdk_versions'], {'openai': 'mock-only'})
        self.assertEqual(set(report['by_pass']), {'1', '2', '3'})
        for number, result in report['by_pass'].items():
            self.assertEqual(result['turn_latency']['samples'], 2)
            self.assertEqual(result['api_turn_latency']['samples'], 1)
            self.assertEqual(result['status_counts'], {'response': 1})
            self.assertEqual(result['usage']['api_calls'], 1)
        first, second, third = [report['by_pass'][str(i)]['usage'] for i in (1, 2, 3)]
        self.assertEqual(first['fields']['prompt_tokens']['observed_sum'], 100)
        self.assertEqual(first['cache_hit_share_of_observed_input'], 0.)
        self.assertEqual(first['priced_calls'], 1)
        self.assertEqual(second['fields']['prompt_tokens']['known_calls'], 0)
        self.assertEqual(second['fields']['prompt_tokens']['unknown_calls'], 1)
        self.assertIsNone(second['fields']['prompt_tokens']['mean_per_known_call'])
        self.assertIsNone(second['cache_hit_share_of_observed_input'])
        self.assertEqual(second['unpriced_calls'], 1)
        self.assertAlmostEqual(third['cache_hit_share_of_observed_input'], .8)
        self.assertEqual(third['fields']['completion_tokens']['observed_sum'], 7)
        self.assertEqual(report['usage']['fields']['prompt_tokens']['observed_sum'], 200)
        self.assertEqual(report['usage']['fields']['prompt_tokens']['unknown_calls'], 1)
        self.assertEqual(report['usage']['priced_calls'], 2)
        self.assertEqual(report['usage']['unpriced_calls'], 1)
        self.assertEqual(report['all_turn_latency']['samples'], 6)
        self.assertEqual(report['api_turn_latency']['samples'], 3)

    async def test_fatal_provider_status_stops_remaining_cases_and_passes(self):
        rows = [case('rule', '请帮忙', protected=True),
                case('fatal', '花园里开了几朵小花'),
                case('never', '街边的树开始落叶')]
        class ProviderFailure(Exception):
            def __init__(self, status):
                super().__init__('unit confidential error body must not be serialized')
                self.status_code = status
        for status in (401, 402, 403):
            with self.subTest(status=status):
                gateway = module.MeasuredGateway('unit-test-not-a-key')
                gateway.aclose = AsyncMock()
                fake = sdk_client(AsyncMock(side_effect=ProviderFailure(status)))
                with patch.object(module, 'read_cases', return_value=rows), \
                        patch.object(module, 'message', side_effect=unit_message), \
                        patch.object(gateway, '_client', return_value=fake), \
                        contextlib.redirect_stdout(io.StringIO()):
                    report = await module.evaluate('unit-test-not-a-key', self.root/str(status),
                                                   repeats=3, gateway=gateway)
                fake.chat.completions.create.assert_awaited_once()
                gateway.aclose.assert_awaited_once()
                self.assertFalse(report['complete'])
                self.assertEqual(report['abort_reason'], 'provider_auth_or_balance_unavailable')
                self.assertEqual([r['id'] for r in report['results']], ['rule', 'fatal'])
                self.assertEqual(report['metrics']['records'], 2)
                self.assertEqual(report['all_turn_latency']['samples'], 2)
                self.assertEqual(report['usage']['api_calls'], 1)
                self.assertEqual(report['usage']['priced_calls'], 0)
                self.assertEqual(report['usage']['unpriced_calls'], 1)
                self.assertEqual(report['status_counts'], {'error': 1})
                self.assertEqual(report['results'][-1]['action'], 'reply')
                self.assertEqual(report['results'][-1]['source'], 'conservative')
                self.assertEqual(report['parse_failure_after_response'], 0)
                self.assertEqual(report['by_pass']['1']['turn_latency']['samples'], 2)
                self.assertEqual(report['by_pass']['1']['api_turn_latency']['samples'], 1)
                for number in ('2', '3'):
                    self.assertEqual(report['by_pass'][number]['turn_latency']['samples'], 0)
                    self.assertEqual(report['by_pass'][number]['usage']['api_calls'], 0)
                    self.assertIsNone(report['by_pass'][number]['api_turn_latency']['p95_ms'])
                saved = json.loads((self.root/str(status)/'report.json').read_text(encoding='utf-8'))
                self.assertFalse(saved['complete'])
                self.assertNotIn('unit confidential error body', json.dumps(saved))

    async def test_nonfatal_rate_limit_does_not_abort_or_retry_same_request(self):
        rows = [case('rate-limited', '路边的花开得很热闹'),
                case('next', '树叶也开始变黄')]
        class RateLimited(Exception):
            status_code = 429
        fake = sdk_client(AsyncMock(side_effect=[RateLimited('mock'), response()]))
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.aclose = AsyncMock()
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'message', side_effect=unit_message), \
                patch.object(gateway, '_client', return_value=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            report = await module.evaluate('unit-test-not-a-key', self.root/'429', gateway=gateway)
        self.assertTrue(report['complete'])
        self.assertEqual(fake.chat.completions.create.await_count, 2)
        self.assertEqual([c['case_id'] for c in report['calls']], ['rate-limited/pass-1', 'next/pass-1'])
        self.assertEqual(report['status_counts'], {'error': 1, 'response': 1})
        self.assertEqual(report['results'][0]['source'], 'conservative')
        self.assertEqual(report['results'][1]['source'], 'cloud')

    async def test_slow_cancel_cleanup_is_unknown_and_counted_as_observed_deadline(self):
        rows = [case('first', '路边的树又长高了一点'),
                case('pending', '院子里还飘着花香')]
        release_cleanup = asyncio.Event()
        class DelayedGateway:
            def __init__(self):
                self.calls = []
                self.case_id = None
                self.pending_task = None
                self.aclose = AsyncMock()
            async def complete_once(self, prompt, max_tokens=80):
                record = {'case_id': self.case_id, 'status': 'response' if not self.calls else 'inflight',
                    'usage': module.normalize_usage(None), 'model_returned': 'mock-only',
                    'finish_reason': 'stop' if not self.calls else None}
                self.calls.append(record)
                if len(self.calls) == 1:
                    record['elapsed_ms'] = .1
                    return '{"action":"reply"}'
                self.pending_task = asyncio.current_task()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Deliberately longer than the one sleep(0) bookkeeping turn.
                    await release_cleanup.wait()
                    raise
                finally:
                    record.update(status='cancelled_deadline', elapsed_ms=1.)
        gateway = DelayedGateway()
        original_decider = module.ReplyDecider
        def short_deadline(*args, **kwargs):
            return original_decider(*args, **{**kwargs, 'timeout': .001})
        # Patch only this tool's clock, not asyncio's global monotonic clock. The
        # decision itself really times out, but the reported wall-clock fixture
        # models a 3-second production deadline without waiting three seconds.
        fake_clock = SimpleNamespace(monotonic=unittest.mock.Mock(
            side_effect=[0., .01, 1., 4.01]))
        try:
            with patch.object(module, 'read_cases', return_value=rows), \
                    patch.object(module, 'message', side_effect=unit_message), \
                    patch.object(module, 'ReplyDecider', side_effect=short_deadline), \
                    patch.object(module, 'time', fake_clock), \
                    contextlib.redirect_stdout(io.StringIO()):
                report = await module.evaluate('unit-test-not-a-key', self.root/'pending-cleanup',
                                               gateway=gateway)
            self.assertEqual(report['results'][-1]['source'], 'conservative')
            self.assertGreaterEqual(report['results'][-1]['elapsed_ms'], 3000.)
            self.assertEqual(report['calls'][-1]['status'], 'inflight')
            self.assertNotIn('elapsed_ms', report['calls'][-1])
            self.assertTrue(report['calls'][-1]['deadline_observed'])
            self.assertEqual(report['pending_cleanup_calls'], 1)
            self.assertEqual(report['timeout_or_cancelled_calls'], 1)
            self.assertIsNone(report['calls'][-1]['usage']['prompt_tokens'])
            self.assertEqual(report['usage']['fields']['prompt_tokens']['unknown_calls'], 2)
            self.assertEqual(report['usage']['unpriced_calls'], 2)
            self.assertEqual(report['later_api_requests']['samples'], 0)
            self.assertIsNone(report['later_api_requests']['p95_ms'])
            saved = json.loads((self.root/'pending-cleanup/report.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['pending_cleanup_calls'], 1)
            self.assertEqual(saved['calls'][-1]['status'], 'inflight')
        finally:
            release_cleanup.set()
            if gateway.pending_task is not None:
                await asyncio.gather(gateway.pending_task, return_exceptions=True)

    async def test_aborted_run_marks_full_local_collection_not_comparable(self):
        rows = [case('fatal', '路边的花又开了一点'), case('never', '云朵慢慢飘走了')]
        local = {'dataset_sha256': module.DATASET_SHA256,
            'model': {'platform': 'Windows-unit-fixture'},
            'summary': {'actual_reply_decider_pipeline': {'records': 2}},
            'results': [{'id': row['id'], 'session_id': row['session_id'],
                'expected': row['label'], 'protected': row['protected'], 'slice': row['slice'],
                'pipeline': {'elapsed_ms': 2.}} for row in rows]}
        path = self.root/'local-all.json'
        path.write_text(json.dumps(local), encoding='utf-8')
        class ProviderFailure(Exception):
            status_code = 402
        fake = sdk_client(AsyncMock(side_effect=ProviderFailure('mock-only')))
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.aclose = AsyncMock()
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'message', side_effect=unit_message), \
                patch.object(gateway, '_client', return_value=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            report = await module.evaluate('unit-test-not-a-key', self.root/'incomplete-vs-local',
                                           repeats=3, local_report=path, gateway=gateway)
        self.assertFalse(report['complete'])
        self.assertEqual(report['all_turn_latency']['samples'], 1)
        self.assertEqual(report['local_comparison']['all_turns']['samples'], 2)
        self.assertIs(report['local_comparison']['comparable'], False)
        saved = json.loads((self.root/'incomplete-vs-local/report.json').read_text(encoding='utf-8'))
        self.assertIs(saved['local_comparison']['comparable'], False)

    async def test_repeat_case_ids_and_token_totals_are_not_deduplicated(self):
        rows = [case('same', '今天看见一片落叶')]
        fake = sdk_client(AsyncMock(return_value=response(usage=SimpleNamespace(
            prompt_tokens=100, completion_tokens=5, total_tokens=105,
            prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20))))
        gateway = module.MeasuredGateway('unit-test-not-a-key')
        gateway.aclose = AsyncMock()
        with patch.object(module, 'read_cases', return_value=rows), \
                patch.object(module, 'message', side_effect=unit_message), \
                patch.object(gateway, '_client', return_value=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            report = await module.evaluate('unit-test-not-a-key', self.root/'repeated', repeats=3, gateway=gateway)
        self.assertEqual([c['case_id'] for c in report['calls']], ['same/pass-1', 'same/pass-2', 'same/pass-3'])
        self.assertEqual(report['usage']['api_calls'], 3)
        self.assertEqual(report['usage']['fields']['prompt_tokens']['observed_sum'], 300)
        self.assertEqual(report['metrics']['records'], 3)
        self.assertEqual(report['later_api_requests']['samples'], 2)


if __name__ == '__main__':
    unittest.main()
