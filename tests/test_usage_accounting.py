"""Real reported counts, weighted cache denominators and task-local attribution."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from src.core.llm import LLMGateway, ProviderCfg, Usage
from src.core.usage_accounting import (TOKEN_FIELDS, normalize_usage, normalize_anthropic_usage,
                                       request_task, summary, task_scope)
from src.store_memory import MemoryStore


class UsageNormalizationTests(unittest.TestCase):
    def test_missing_usage_preserves_every_unknown_count(self):
        for raw in (None,{},NS()):
            value=normalize_usage(raw)
            for field in TOKEN_FIELDS:self.assertIsNone(value[field])
            self.assertIsNone(value['cached'])
            self.assertIsNone(value['cache_hit_source'])
            self.assertFalse(value['cache_inconsistent'])

    def test_default_usage_dataclass_does_not_claim_zero_tokens(self):
        value=Usage()
        for field in TOKEN_FIELDS:self.assertIsNone(getattr(value,field))

    def test_dictionary_and_sdk_object_have_identical_normalization(self):
        value={'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,
               'prompt_cache_hit_tokens':80,'prompt_cache_miss_tokens':20,
               'completion_tokens_details':{'reasoning_tokens':3}}
        sdk=NS(**{**value,'completion_tokens_details':NS(reasoning_tokens=3)})
        self.assertEqual(normalize_usage(value),normalize_usage(sdk))
        result=normalize_usage(value)
        self.assertEqual([result[k] for k in TOKEN_FIELDS],[100,10,110,80,20,3])
        self.assertTrue(result['cached'])

    def test_measured_zero_is_not_missing(self):
        result=normalize_usage({'prompt_tokens':0,'completion_tokens':0,'total_tokens':0,
            'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':0,
            'completion_tokens_details':{'reasoning_tokens':0}})
        self.assertTrue(all(result[key]==0 for key in TOKEN_FIELDS))
        self.assertIs(result['cached'],False)

    def test_invalid_counts_are_not_cast_to_valid_numbers(self):
        for invalid in (True,False,-1,'10',1.0,float('nan'),float('inf'),[],{}):
            with self.subTest(invalid=repr(invalid)):
                result=normalize_usage({'prompt_tokens':invalid,'completion_tokens':invalid,
                    'total_tokens':invalid,'prompt_cache_hit_tokens':invalid,'prompt_cache_miss_tokens':invalid,
                    'completion_tokens_details':{'reasoning_tokens':invalid}})
                self.assertTrue(all(result[key] is None for key in TOKEN_FIELDS))

    def test_openai_nested_cached_tokens_support_dict_and_object(self):
        for nested in ({'cached_tokens':60},NS(cached_tokens=60)):
            value=normalize_usage({'prompt_tokens':100,'prompt_tokens_details':nested})
            self.assertEqual(value['cache_hit_tokens'],60)
            self.assertEqual(value['cache_hit_source'],'prompt_tokens_details.cached_tokens')
            self.assertIsNone(value['cache_miss_tokens'])

    def test_direct_zero_has_precedence_over_nested_cache_count(self):
        value=normalize_usage({'prompt_cache_hit_tokens':0,'prompt_tokens_details':{'cached_tokens':80}})
        self.assertEqual(value['cache_hit_tokens'],0)
        self.assertEqual(value['cache_hit_source'],'prompt_cache_hit_tokens')

    def test_invalid_direct_field_can_use_a_valid_compatible_field(self):
        value=normalize_usage({'prompt_cache_hit_tokens':False,'prompt_tokens_details':{'cached_tokens':80}})
        self.assertEqual(value['cache_hit_tokens'],80)
        self.assertEqual(value['cache_hit_source'],'prompt_tokens_details.cached_tokens')

    def test_missing_total_and_miss_are_not_fabricated_from_other_counts(self):
        value=normalize_usage({'prompt_tokens':100,'completion_tokens':10,'prompt_cache_hit_tokens':80})
        self.assertIsNone(value['total_tokens'])
        self.assertIsNone(value['cache_miss_tokens'])

    def test_normalized_usage_can_be_normalized_again_without_losing_sources(self):
        value=normalize_usage({'prompt_tokens':100,'prompt_tokens_details':{'cached_tokens':80}})
        self.assertEqual(normalize_usage(value),value)
        self.assertEqual(normalize_usage(LLMGateway._usage(value)),value)

    def test_inconsistent_cache_fields_are_preserved_but_flagged(self):
        for raw in ({'prompt_tokens':10,'prompt_cache_hit_tokens':11},
                    {'prompt_tokens':10,'prompt_cache_miss_tokens':11},
                    {'prompt_tokens':10,'prompt_cache_hit_tokens':8,'prompt_cache_miss_tokens':8}):
            with self.subTest(raw=raw):
                value=normalize_usage(raw)
                self.assertTrue(value['cache_inconsistent'])
                self.assertEqual(value['prompt_tokens'],10)

    def test_arbitrary_metadata_and_provider_cached_boolean_are_not_counts(self):
        value=normalize_usage({'cached':True,'api_key':'fixture-secret','unknown':123})
        self.assertIsNone(value['cached'])
        self.assertNotIn('api_key',value)
        self.assertNotIn('unknown',value)


class UsageSummaryTests(unittest.TestCase):
    def test_empty_report_does_not_display_zero_input_or_a_cache_rate(self):
        value=summary([])
        self.assertEqual(value['attempts'],0)
        for field in TOKEN_FIELDS:
            self.assertIsNone(value['fields'][field]['observed_sum'])
            self.assertIsNone(value['fields'][field]['mean_per_known_call'])
        self.assertIsNone(value['cache']['hit_ratio'])
        self.assertFalse(value['cache']['complete'])

    def test_unknown_usage_is_counted_without_adding_fake_zero_tokens(self):
        value=summary([{'task':'chat'},None,{'task':'decision','usage':None}])
        self.assertEqual(value['attempts'],3)
        self.assertEqual(value['usage_unknown_calls'],3)
        self.assertEqual(value['fields']['prompt_tokens']['unknown_calls'],3)
        self.assertIsNone(value['fields']['prompt_tokens']['observed_sum'])
        self.assertEqual(value['cache']['unknown_calls'],3)
        self.assertIsNone(value['cache']['hit_ratio'])

    def test_cache_ratio_is_input_token_weighted_not_average_cached_booleans(self):
        value=summary([{'task':'chat','prompt_tokens':100,'cache_hit_tokens':100,'cached':True},
                       {'task':'chat','prompt_tokens':9900,'cache_hit_tokens':0,'cached':False}])
        self.assertEqual(value['cache']['hit_ratio'],.01)
        self.assertEqual(value['cache']['overall_hit_ratio'],.01)
        self.assertEqual(value['cache']['input_tokens'],10000)
        self.assertEqual(value['cache']['hit_tokens'],100)
        self.assertTrue(value['cache']['complete'])

    def test_partial_report_never_labels_observed_subset_as_overall_rate(self):
        value=summary([{'task':'chat','prompt_tokens':100,'cache_hit_tokens':100},
                       {'task':'chat','usage':None}])
        self.assertEqual(value['cache']['hit_ratio'],1)
        self.assertIsNone(value['cache']['overall_hit_ratio'])
        self.assertFalse(value['cache']['complete'])
        self.assertEqual(value['cache']['unknown_calls'],1)
        self.assertEqual(value['fields']['prompt_tokens']['observed_sum'],100)
        self.assertEqual(value['fields']['prompt_tokens']['mean_per_known_call'],100)

    def test_hit_without_prompt_or_miss_does_not_make_a_fake_hundred_percent(self):
        value=summary([{'cache_hit_tokens':100}])
        self.assertIsNone(value['cache']['hit_ratio'])
        self.assertIsNone(value['cache']['overall_hit_ratio'])
        self.assertEqual(value['fields']['cache_hit_tokens']['observed_sum'],100)

    def test_missing_hit_does_not_make_a_fake_zero_percent(self):
        value=summary([{'prompt_tokens':100,'cached':False}])
        self.assertIsNone(value['cache']['hit_ratio'])

    def test_measured_zero_hit_has_valid_denominator_but_zero_input_has_no_ratio(self):
        value=summary([{'prompt_tokens':100,'cache_hit_tokens':0}])
        self.assertEqual(value['cache']['overall_hit_ratio'],0)
        zero=summary([{'prompt_tokens':0,'cache_hit_tokens':0,'cache_miss_tokens':0}])
        self.assertIsNone(zero['cache']['hit_ratio'])

    def test_explicit_hit_miss_pair_is_valid_when_prompt_count_is_unreported(self):
        value=summary([{'cache_hit_tokens':80,'cache_miss_tokens':20}])
        self.assertEqual(value['cache']['overall_hit_ratio'],.8)
        self.assertIsNone(value['fields']['prompt_tokens']['observed_sum'])
        self.assertEqual(value['cache']['denominator_sources']['reported_hit_and_miss'],1)

    def test_inconsistent_reports_are_not_clamped_into_plausible_ratios(self):
        value=summary([{'prompt_tokens':10,'cache_hit_tokens':20},
                       {'prompt_tokens':100,'cache_hit_tokens':40,'cache_miss_tokens':60}])
        self.assertEqual(value['cache']['hit_ratio'],.4)
        self.assertIsNone(value['cache']['overall_hit_ratio'])
        self.assertEqual(value['cache']['inconsistent_calls'],1)
        self.assertEqual(value['cache']['unknown_calls'],0)
        self.assertEqual(value['fields']['cache_hit_tokens']['observed_sum'],60)

    def test_tasks_keep_chat_decision_memory_search_and_compaction_separate(self):
        records=[{'task':task,'prompt_tokens':index+1,'completion_tokens':2,'reasoning_tokens':0}
                 for index,task in enumerate(('chat','decision','memory','search','compaction'))]
        value=summary(records)
        self.assertEqual(set(value['by_task']),{'chat','decision','memory','search','compaction'})
        self.assertEqual(value['fields']['prompt_tokens']['observed_sum'],15)
        for index,task in enumerate(('chat','decision','memory','search','compaction')):
            self.assertEqual(value['by_task'][task]['fields']['prompt_tokens']['observed_sum'],index+1)
            self.assertEqual(value['by_task'][task]['fields']['reasoning_tokens']['observed_sum'],0)

    def test_attempts_without_any_metric_record_remain_unknown(self):
        value=summary([{'task':'chat','prompt_tokens':100,'cache_hit_tokens':80}],attempts=3)
        self.assertEqual(value['attempts'],3)
        self.assertEqual(value['recorded_attempts'],1)
        self.assertEqual(value['unrecorded_attempts'],2)
        self.assertEqual(value['fields']['prompt_tokens']['unknown_calls'],2)
        self.assertEqual(value['cache']['unknown_calls'],2)
        self.assertIsNone(value['cache']['overall_hit_ratio'])
        self.assertEqual(value['by_task']['unknown']['attempts'],2)

    def test_invalid_attempt_totals_are_rejected(self):
        for attempts in (-1,True,1.0,'1',0):
            with self.subTest(attempts=attempts):
                with self.assertRaises(ValueError):summary([{}],attempts=attempts)

    def test_nested_usage_and_sdk_metric_objects_are_supported(self):
        value=summary([{'task':'search','usage':{'prompt_tokens':100,'prompt_tokens_details':{'cached_tokens':40}}},
                       NS(task='decision',usage=NS(prompt_tokens=100,prompt_cache_hit_tokens=60))])
        self.assertEqual(value['cache']['overall_hit_ratio'],.5)

    def test_generator_metrics_are_consumed_once_and_unknown_tasks_are_not_guessed(self):
        value=summary(({'task':name,'prompt_tokens':1} for name in (None,'custom','chat')))
        self.assertEqual(value['attempts'],3)
        self.assertEqual(value['by_task']['unknown']['attempts'],2)
        self.assertEqual(value['by_task']['chat']['attempts'],1)

    def test_module_does_not_estimate_currency_or_a_provider_invoice(self):
        value=summary([{'model':'arbitrary-model','prompt_tokens':10,'completion_tokens':2}])
        self.assertNotIn('cost',value);self.assertNotIn('price',value);self.assertNotIn('currency',value)
        self.assertEqual(value['scope'],'supplied_request_records_only')


class AnthropicUsageTests(unittest.TestCase):
    def test_complete_components_sum_total_input_without_double_counting_cache(self):
        value=normalize_anthropic_usage({'input_tokens':10,'cache_creation_input_tokens':20,
                                        'cache_read_input_tokens':70,'output_tokens':5})
        self.assertEqual(value['prompt_tokens'],100)
        self.assertEqual(value['completion_tokens'],5)
        self.assertEqual(value['input_tokens_observed'],10)
        self.assertEqual(value['cache_creation_input_tokens'],20)
        self.assertEqual(value['cache_read_input_tokens'],70)
        self.assertEqual(value['cache_miss_tokens'],30)
        self.assertIsNone(value['total_tokens'])
        self.assertIn('cache_creation',value['prompt_tokens_source'])
        result=summary([{'task':'search',**value}])
        self.assertEqual(result['cache']['overall_hit_ratio'],.7)
        self.assertEqual(result['fields']['input_tokens_observed']['observed_sum'],10)

    def test_partial_cache_components_keep_prompt_unknown_and_native_counts_visible(self):
        for absent in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens'):
            raw={'input_tokens':10,'cache_creation_input_tokens':20,'cache_read_input_tokens':70,'output_tokens':5}
            del raw[absent];value=normalize_anthropic_usage(raw)
            self.assertIsNone(value['prompt_tokens'])
            result=summary([{'task':'search',**value}])
            self.assertIsNone(result['cache']['overall_hit_ratio'])
            self.assertEqual(result['fields']['completion_tokens']['observed_sum'],5)
            self.assertEqual(result['usage_unknown_calls'],0)

    def test_reported_zero_cache_components_are_required_instead_of_defaulted(self):
        value=normalize_anthropic_usage({'input_tokens':100,'output_tokens':10})
        self.assertIsNone(value['prompt_tokens'])
        self.assertIsNone(value['cache_hit_tokens'])
        complete=normalize_anthropic_usage({'input_tokens':100,'output_tokens':10,
                                           'cache_creation_input_tokens':0,'cache_read_input_tokens':0})
        self.assertEqual(complete['prompt_tokens'],100)
        self.assertEqual(summary([complete])['cache']['overall_hit_ratio'],0)

    def test_explicit_protocol_is_not_inferred_from_ambiguous_input_tokens(self):
        raw={'input_tokens':10,'cache_creation_input_tokens':20,'cache_read_input_tokens':70,'output_tokens':5}
        self.assertIsNone(summary([raw])['fields']['prompt_tokens']['observed_sum'])
        explicit=summary([{'task':'search','usage_protocol':'anthropic','usage':raw}])
        self.assertEqual(explicit['fields']['prompt_tokens']['observed_sum'],100)

    def test_object_zero_invalid_and_idempotent_normalization(self):
        raw=NS(input_tokens=10,cache_creation_input_tokens=0,cache_read_input_tokens=90,output_tokens=0)
        value=normalize_anthropic_usage(raw)
        self.assertEqual(normalize_anthropic_usage(value),value)
        self.assertEqual(value['output_tokens_observed'],0)
        for invalid in (True,-1,'10',1.0):
            result=normalize_anthropic_usage({'input_tokens':invalid,'output_tokens':invalid})
            self.assertIsNone(result['input_tokens_observed'])
            self.assertIsNone(result['completion_tokens'])

    def test_native_missing_usage_stays_fully_unknown(self):
        value=normalize_anthropic_usage(None)
        self.assertTrue(all(value[k] is None for k in TOKEN_FIELDS))
        self.assertEqual(summary([value])['usage_unknown_calls'],1)

    def test_native_usage_dataclass_and_capture_do_not_lose_decomposition(self):
        raw={'protocol':'anthropic','input_tokens':10,'cache_creation_input_tokens':20,
             'cache_read_input_tokens':70,'output_tokens':5}
        value=LLMGateway._usage(raw)
        self.assertEqual((value.prompt_tokens,value.input_tokens_observed,value.cache_creation_input_tokens),(100,10,20))
        self.assertEqual(value.protocol,'anthropic')
        self.assertEqual(summary([asdict(value)])['cache']['overall_hit_ratio'],.7)


class GatewayAccountingTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self, name='primary', sink=None):
        return LLMGateway(ProviderCfg(name,'https://example.invalid','fixture','fixture'),usage_sink=sink)

    async def test_record_usage_preserves_missing_values_and_existing_sink_signature(self):
        sink=AsyncMock();gateway=self.gateway(sink=sink)
        await gateway._record_usage(gateway.primary,'chat-fixture',None,.25)
        row=gateway.metrics[-1]
        self.assertIsNone(row['prompt_tokens']);self.assertIsNone(row['completion_tokens'])
        self.assertIsNone(row['cached']);self.assertEqual(row['task'],'chat')
        sink.assert_awaited_once()
        args=sink.call_args.args
        self.assertEqual((args[0],args[1],args[3]),('chat-fixture','fixture',.25))
        self.assertIsInstance(args[2],Usage);self.assertIsNone(args[2].prompt_tokens)

    async def test_record_dictionary_usage_retains_reasoning_and_real_zero(self):
        gateway=self.gateway('admission')
        raw={'prompt_tokens':100,'completion_tokens':5,'total_tokens':105,
             'prompt_tokens_details':{'cached_tokens':80},'completion_tokens_details':{'reasoning_tokens':0}}
        await gateway._record_usage(gateway.primary,'',raw,.2,120)
        row=gateway.metrics[-1]
        self.assertEqual((row['prompt_tokens'],row['cache_hit_tokens'],row['reasoning_tokens'],row['ttft_ms']),(100,80,0,120))
        self.assertEqual(row['task'],'decision')
        self.assertEqual(summary(gateway.metrics)['cache']['overall_hit_ratio'],.8)

    async def test_capture_retains_openai_cache_fields_and_legacy_aliases(self):
        gateway=self.gateway()
        with gateway.capture_request() as trace:
            gateway._capture_request([{'role':'user','content':'合成来话'}],
                {'prompt_tokens':100,'prompt_tokens_details':{'cached_tokens':80},
                 'completion_tokens_details':{'reasoning_tokens':0}})
        self.assertEqual(trace['usage']['cache_hit_tokens'],80)
        self.assertEqual(trace['usage']['prompt_cache_hit_tokens'],80)
        self.assertIsNone(trace['usage']['prompt_cache_miss_tokens'])
        self.assertIsNone(trace['usage']['completion_tokens'])
        self.assertEqual(trace['usage']['reasoning_tokens'],0)

    async def test_capture_is_still_task_local(self):
        gateway=self.gateway()
        async def capture(count):
            with gateway.capture_request() as trace:
                await asyncio.sleep(0)
                gateway._capture_request([],{'prompt_tokens':count})
            return trace['usage']['prompt_tokens']
        self.assertEqual(await asyncio.gather(capture(10),capture(20)),[10,20])

    async def test_scoped_main_gateway_compaction_does_not_relabel_parallel_chat(self):
        gateway=self.gateway()
        async def record(task,chat):
            with task_scope(task):
                await asyncio.sleep(0)
                await gateway._record_usage(gateway.primary,chat,{'prompt_tokens':10},.1)
        await asyncio.gather(record('compaction',''),record('chat','conversation'))
        self.assertEqual({row['task'] for row in gateway.metrics},{'chat','compaction'})
        self.assertEqual(request_task('primary',''),'internal')

    async def test_task_scopes_restore_after_nested_exception(self):
        with task_scope('memory'):
            self.assertEqual(request_task('primary',''),'memory')
            with self.assertRaises(RuntimeError):
                with task_scope('compaction'):
                    self.assertEqual(request_task('primary',''),'compaction')
                    raise RuntimeError('fixture')
            self.assertEqual(request_task('primary',''),'memory')
        self.assertEqual(request_task('primary',''),'internal')

    async def test_provider_names_supply_only_known_task_defaults(self):
        for name,expected in [('admission','decision'),('judge','decision'),('memory','memory'),
                              ('search','search'),('compaction','compaction'),('proactive','proactive')]:
            self.assertEqual(request_task(name,''),expected)
        self.assertEqual(request_task('primary','chat-id'),'chat')
        self.assertEqual(request_task('unknown-provider',''),'internal')

    async def test_invalid_task_scopes_are_rejected(self):
        for value in (None,[],True,'not-a-task'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    with task_scope(value):pass

    async def test_existing_memory_sink_accepts_optional_counts_without_fake_sum(self):
        gateway=self.gateway(sink=MemoryStore().record_usage)
        await gateway._record_usage(gateway.primary,'chat',None,.1)
        self.assertIsNone(gateway.metrics[-1]['prompt_tokens'])

    async def test_structured_usage_log_contains_only_metadata_not_key_chat_or_prompt(self):
        gateway=self.gateway();gateway.primary.api_key='fixture-secret-key'
        with self.assertLogs('src.core.llm',level='INFO') as logs:
            await gateway._record_usage(gateway.primary,'private-chat-id',
                {'prompt_tokens':10,'completion_tokens':2,'prompt':'private-input','api_key':'fixture-secret-key'},.1)
        text='\n'.join(logs.output)
        self.assertIn('LLM_USAGE ',text)
        self.assertIn('"prompt_tokens":10',text)
        for forbidden in ('fixture-secret-key','private-chat-id','private-input'):
            self.assertNotIn(forbidden,text)

    async def test_observer_receives_a_copy_and_cannot_mutate_gateway_metrics(self):
        gateway=self.gateway();observed=[]
        def observe(metric):
            observed.append(dict(metric));metric['prompt_tokens']=999
        gateway.usage_observer=observe
        await gateway._record_usage(gateway.primary,'chat',{'prompt_tokens':10},.1)
        self.assertEqual(observed[0]['prompt_tokens'],10)
        self.assertEqual(gateway.metrics[-1]['prompt_tokens'],10)

    async def test_failing_observer_does_not_make_completed_model_usage_fail(self):
        gateway=self.gateway()
        def fail(metric):raise RuntimeError('private-secret-fixture')
        gateway.usage_observer=fail
        with self.assertLogs('src.core.llm',level='WARNING') as logs:
            await gateway._record_usage(gateway.primary,'chat',{'prompt_tokens':10},.1)
        self.assertEqual(gateway.metrics[-1]['prompt_tokens'],10)
        self.assertIn('error_type=RuntimeError','\n'.join(logs.output))
        self.assertNotIn('private-secret-fixture','\n'.join(logs.output))

    async def test_search_stage_scopes_keep_their_actual_task_labels(self):
        gateway=self.gateway()
        for task in ('search_plan','search_synthesis','search_review'):
            with task_scope(task):
                await gateway._record_usage(gateway.primary,'chat',{'prompt_tokens':1},.1)
        self.assertEqual(set(summary(gateway.metrics)['by_task']),{'search_plan','search_synthesis','search_review'})


if __name__=='__main__':unittest.main()
