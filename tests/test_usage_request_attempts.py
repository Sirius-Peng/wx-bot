"""SDK-level attempts/cancellation/accounting contracts; all HTTP is synthetic.

No real credentials, user chat, remote hosts, or provider API calls are accessed.
"""
import asyncio
import copy
import inspect
import json
import unittest
from unittest.mock import AsyncMock

import tests.test_cache_request_review as fixtures
from src.core.llm import LLMGateway, ProviderCfg
from src.core.usage_accounting import summary


MESSAGES=[{'role':'system','content':'固定合成人格'}, {'role':'user','content':'合成审查输入'}]
NO_USAGE=object()


def response(text='第一句。\n第二句。', raw_usage=NO_USAGE):
    data=fixtures.completion(text)
    if raw_usage is NO_USAGE:data.pop('usage')
    else:data['usage']=raw_usage
    return data


def event(text=None, raw_usage=NO_USAGE):
    value={'id':'synthetic-stream','object':'chat.completion.chunk','created':1,
           'model':'deepseek-flash','choices':[]}
    if text is not None:value['choices']=[{'index':0,'delta':{'content':text},'finish_reason':None}]
    if raw_usage is not NO_USAGE:value['usage']=raw_usage
    return ('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode()


@unittest.skipUnless(fixtures.AsyncOpenAI is not None and fixtures.sdk_http is not None,
                     'real OpenAI SDK transport not installed')
class UsageRequestAttempts(unittest.IsolatedAsyncioTestCase):
    async def gateway(self, callback=None, *, sink=None, observer=None, fallback=False):
        self.packets=[]
        clients=[]
        async def handler(request):
            body=json.loads(request.content)
            self.packets.append(copy.deepcopy(body))
            result=callback(body,len(self.packets)) if callback else response(raw_usage=fixtures.usage(120,80))
            if inspect.isawaitable(result):result=await result
            if isinstance(result,fixtures.sdk_http.Response):return result
            if isinstance(result,tuple):
                status,data=result
                return fixtures.sdk_http.Response(status,json=data)
            return fixtures.sdk_http.Response(200,json=result)
        def make_client(_cfg):
            http=fixtures.DefaultAsyncHttpxClient(transport=fixtures.sdk_http.MockTransport(handler),trust_env=False)
            client=fixtures.AsyncOpenAI(api_key='synthetic-fixture-not-a-real-key',
                base_url='https://api.deepseek.com',max_retries=0,http_client=http)
            clients.append(client)
            return client
        gateway=LLMGateway(ProviderCfg('primary','https://api.deepseek.com','synthetic-fixture-not-a-real-key','primary-fixture'),
            fallback=ProviderCfg('fallback','https://api.deepseek.com','synthetic-fixture-not-a-real-key','fallback-fixture') if fallback else None,
            usage_sink=sink)
        gateway.usage_observer=observer
        gateway._new_client=make_client
        async def close():
            await gateway.aclose()
            for client in clients:await client.close()
        self.addAsyncCleanup(close)
        return gateway

    def stream(self, *parts):
        class SyntheticStream(fixtures.sdk_http.AsyncByteStream):
            async def __aiter__(self):
                for part in parts:
                    if isinstance(part,BaseException):raise part
                    if callable(part):
                        await part()
                    else:yield part
            async def aclose(self):pass
        return fixtures.sdk_http.Response(200,headers={'content-type':'text/event-stream'},stream=SyntheticStream())

    async def collect(self,gateway):
        return ''.join([part async for part in gateway.stream(MESSAGES,chat_id='synthetic-chat')])

    def assert_summary_unknown(self,gateway,attempts):
        result=summary(gateway.metrics,attempts=attempts)
        self.assertEqual(result['attempts'],attempts)
        self.assertEqual(result['usage_unknown_calls'],attempts)
        self.assertIsNone(result['cache']['overall_hit_ratio'])
        for name in ('prompt_tokens','completion_tokens','cache_hit_tokens','cache_miss_tokens','reasoning_tokens'):
            self.assertIsNone(result['fields'][name]['observed_sum'])
            self.assertEqual(result['fields'][name]['unknown_calls'],attempts)

    async def test_real_sdk_retry_has_two_distinct_attempts_and_one_linked_usage(self):
        def script(_body,index):
            if index==1:return 503,{'error':{'message':'synthetic unavailable','type':'server_error'}}
            return response(raw_usage=fixtures.usage(120,80))
        observed=[]
        gateway=await self.gateway(script,observer=lambda item:observed.append(item))
        self.assertEqual(await gateway.chat(MESSAGES,chat_id='synthetic-chat'),'第一句。\n第二句。')
        self.assertEqual(len(self.packets),2)
        self.assertEqual(self.packets[0],self.packets[1])
        self.assertEqual([a['status'] for a in gateway.attempts],['error','success'])
        self.assertEqual([a['usage_reported'] for a in gateway.attempts],[False,True])
        ids=[a['request_id'] for a in gateway.attempts]
        self.assertEqual(len(set(ids)),2)
        self.assertEqual(gateway.metrics[0]['request_id'],ids[1])
        self.assertEqual(len(gateway.metrics),1)
        self.assertEqual(sum(item['kind']=='request' for item in observed),2)
        self.assertEqual(sum(item['kind']=='usage' for item in observed),1)
        result=summary(gateway.metrics,attempts=len(gateway.attempts))
        self.assertEqual((result['cache']['known_calls'],result['cache']['unknown_calls']),(1,1))
        self.assertEqual(result['cache']['hit_ratio'],80/120)
        self.assertIsNone(result['cache']['overall_hit_ratio'])

    async def test_real_sdk_nontransient_error_is_one_unknown_attempt(self):
        gateway=await self.gateway(lambda *_:(400,{'error':{'message':'synthetic bad request','type':'invalid_request_error'}}))
        with self.assertRaises(Exception):await gateway.chat(MESSAGES,chat_id='synthetic-chat')
        self.assertEqual(len(self.packets),1)
        self.assertEqual(len(gateway.metrics),0)
        self.assertEqual(gateway.attempts[0]['status'],'error')
        self.assertFalse(gateway.attempts[0]['usage_reported'])
        self.assertNotIn('_started',gateway.attempts[0])
        self.assert_summary_unknown(gateway,1)

    async def test_retry_exhaustion_counts_both_attempts_not_only_last_error(self):
        gateway=await self.gateway(lambda *_:(503,{'error':{'message':'synthetic unavailable','type':'server_error'}}))
        with self.assertRaises(Exception):await gateway.chat(MESSAGES,chat_id='synthetic-chat')
        self.assertEqual(len(self.packets),2)
        self.assertEqual([a['status'] for a in gateway.attempts],['error','error'])
        self.assert_summary_unknown(gateway,2)

    async def test_fallback_request_gets_its_own_id_without_hiding_primary_failures(self):
        def script(body,_index):
            if body['model']=='primary-fixture':
                return 503,{'error':{'message':'synthetic unavailable','type':'server_error'}}
            return response('合成兜底成稿。',fixtures.usage(70,60))
        gateway=await self.gateway(script,fallback=True)
        self.assertEqual(await gateway.chat(MESSAGES,chat_id='synthetic-chat'),'合成兜底成稿。')
        self.assertEqual([a['model'] for a in gateway.attempts],['primary-fixture','primary-fixture','fallback-fixture'])
        self.assertEqual(len({a['request_id'] for a in gateway.attempts}),3)
        self.assertEqual(len(self.packets),3)
        self.assertEqual(len(gateway.metrics),1)
        self.assertEqual(gateway.metrics[0]['request_id'],gateway.attempts[-1]['request_id'])
        self.assertIsNone(summary(gateway.metrics,attempts=3)['cache']['overall_hit_ratio'])

    async def test_complete_once_never_retries_or_falls_back(self):
        gateway=await self.gateway(lambda *_:(503,{'error':{'message':'synthetic unavailable','type':'server_error'}}),fallback=True)
        with self.assertRaises(Exception):await gateway.complete_once('合成轻量判定',max_tokens=8)
        self.assertEqual(len(self.packets),1)
        self.assertEqual(len(gateway.attempts),1)
        self.assert_summary_unknown(gateway,1)

    async def test_cancellation_before_response_is_a_cancelled_unknown_attempt(self):
        entered=asyncio.Event()
        async def script(_body,_index):
            entered.set()
            await asyncio.Event().wait()
        gateway=await self.gateway(script,fallback=True)
        task=asyncio.create_task(gateway.chat(MESSAGES,chat_id='synthetic-chat'))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(self.packets),1)
        self.assertEqual(gateway.attempts[0]['status'],'cancelled')
        self.assertEqual(gateway.attempts[0]['error_type'],'CancelledError')
        self.assert_summary_unknown(gateway,1)

    async def test_stream_cancellation_before_headers_does_not_trigger_retry(self):
        entered=asyncio.Event()
        async def script(_body,_index):
            entered.set()
            await asyncio.Event().wait()
        gateway=await self.gateway(script,fallback=True)
        task=asyncio.create_task(self.collect(gateway))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(self.packets),1)
        self.assertTrue(gateway.attempts[0]['stream'])
        self.assertEqual(gateway.attempts[0]['status'],'cancelled')
        self.assert_summary_unknown(gateway,1)

    async def test_midstream_cancellation_does_not_capture_partial_assistant(self):
        entered=asyncio.Event()
        async def block():
            entered.set()
            await asyncio.Event().wait()
        gateway=await self.gateway(lambda *_:self.stream(event('尚未完成的草稿'),block),fallback=True)
        with gateway.capture_request() as trace:
            task=asyncio.create_task(self.collect(gateway))
            await asyncio.wait_for(entered.wait(),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(self.packets),1)
        self.assertEqual(gateway.attempts[0]['status'],'cancelled')
        self.assertIsNone(trace['assistant'])
        self.assertIsNone(trace['usage']['prompt_tokens'])
        self.assert_summary_unknown(gateway,1)

    async def test_closing_unconsumed_stream_finalizes_cancelled_attempt(self):
        async def block():await asyncio.Event().wait()
        gateway=await self.gateway(lambda *_:self.stream(event('尚未完成的草稿'),block))
        with gateway.capture_request() as trace:
            stream=gateway.stream(MESSAGES,chat_id='synthetic-chat')
            self.assertEqual(await anext(stream),'尚未完成的草稿')
            await stream.aclose()
        self.assertEqual(len(self.packets),1)
        self.assertEqual(gateway.attempts[0]['status'],'cancelled')
        self.assertIsNone(trace['assistant'])
        self.assert_summary_unknown(gateway,1)

    async def test_stream_transport_error_before_first_delta_retries_only_once(self):
        def script(_body,index):
            if index==1:return self.stream(fixtures.sdk_http.ReadError('synthetic interrupted stream'))
            return self.stream(event('完整成稿。'),event(raw_usage=fixtures.usage(100,90)),b'data: [DONE]\n\n')
        gateway=await self.gateway(script)
        with gateway.capture_request() as trace:
            self.assertEqual(await self.collect(gateway),'完整成稿。')
        self.assertEqual(len(self.packets),2)
        self.assertEqual([a['status'] for a in gateway.attempts],['error','success'])
        self.assertEqual(len(gateway.metrics),1)
        self.assertEqual(gateway.metrics[0]['request_id'],gateway.attempts[1]['request_id'])
        self.assertEqual(trace['assistant'],{'role':'assistant','content':'完整成稿。'})

    async def test_stream_error_after_delta_does_not_start_second_paid_answer(self):
        gateway=await self.gateway(lambda *_:self.stream(event('部分草稿'),fixtures.sdk_http.ReadError('synthetic stream error')),fallback=True)
        with gateway.capture_request() as trace:
            with self.assertRaises(Exception):await self.collect(gateway)
        self.assertEqual(len(self.packets),1)
        self.assertEqual(gateway.attempts[0]['status'],'error')
        self.assertIsNone(trace['assistant'])
        self.assert_summary_unknown(gateway,1)

    async def test_failed_stream_still_keeps_reported_usage_when_provider_supplied_it(self):
        gateway=await self.gateway(lambda *_:self.stream(event('部分草稿'),event(raw_usage=fixtures.usage(100,80)),
            fixtures.sdk_http.ReadError('synthetic stream error')))
        with self.assertRaises(Exception):await self.collect(gateway)
        self.assertEqual(len(self.packets),1)
        self.assertEqual(gateway.attempts[0]['status'],'error')
        self.assertTrue(gateway.attempts[0]['usage_reported'])
        self.assertEqual(gateway.metrics[0]['cache_hit_tokens'],80)
        self.assertEqual(gateway.metrics[0]['request_id'],gateway.attempts[0]['request_id'])

    async def test_usage_sink_fault_cannot_retry_or_fall_back_after_model_success(self):
        sink=AsyncMock(side_effect=RuntimeError('synthetic accounting failure'))
        gateway=await self.gateway(sink=sink,fallback=True)
        self.assertEqual(await gateway.chat(MESSAGES,chat_id='synthetic-chat'),'第一句。\n第二句。')
        self.assertEqual(len(self.packets),1)
        self.assertEqual(gateway.attempts[0]['status'],'success')
        self.assertEqual(len(gateway.metrics),1)
        sink.assert_awaited_once()

    async def test_usage_and_request_observer_faults_cannot_regenerate_success(self):
        observed=[]
        def fail(item):
            observed.append(item['kind'])
            raise RuntimeError('synthetic observer failure')
        gateway=await self.gateway(observer=fail,fallback=True)
        self.assertEqual(await gateway.chat(MESSAGES,chat_id='synthetic-chat'),'第一句。\n第二句。')
        self.assertEqual(len(self.packets),1)
        self.assertEqual(sorted(observed),['request','usage'])
        self.assertEqual(gateway.attempts[0]['status'],'success')

    async def test_stream_sink_and_observer_faults_keep_one_raw_output(self):
        sink=AsyncMock(side_effect=RuntimeError('synthetic accounting failure'))
        def fail(_):raise RuntimeError('synthetic observer failure')
        gateway=await self.gateway(lambda *_:self.stream(event('第一句。\n'),event('第二句。'),
            event(raw_usage=fixtures.usage(100,90)),b'data: [DONE]\n\n'),sink=sink,observer=fail,fallback=True)
        with gateway.capture_request() as trace:
            self.assertEqual(await self.collect(gateway),'第一句。\n第二句。')
        self.assertEqual(len(self.packets),1)
        self.assertEqual(gateway.attempts[0]['status'],'success')
        self.assertEqual(trace['assistant'],{'role':'assistant','content':'第一句。\n第二句。'})
        sink.assert_awaited_once()

    async def test_complete_once_sink_fault_does_not_launch_second_decision(self):
        gateway=await self.gateway(sink=AsyncMock(side_effect=RuntimeError('synthetic accounting failure')),fallback=True)
        self.assertEqual(await gateway.complete_once('合成轻量判定',max_tokens=8),'第一句。\n第二句。')
        self.assertEqual(len(self.packets),1)
        self.assertEqual(len(gateway.metrics),1)
        self.assertEqual(gateway.attempts[0]['status'],'success')

    async def test_missing_nonstream_usage_and_capture_counts_stay_unknown(self):
        gateway=await self.gateway(lambda *_:response())
        with gateway.capture_request() as trace:
            await gateway.chat(MESSAGES,chat_id='synthetic-chat')
        self.assertFalse(gateway.attempts[0]['usage_reported'])
        self.assertEqual(trace['assistant'],{'role':'assistant','content':'第一句。\n第二句。'})
        self.assert_summary_unknown(gateway,1)
        for name in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens','reasoning_tokens'):
            self.assertIsNone(trace['usage'][name])

    async def test_empty_usage_envelope_does_not_invent_zero_tokens(self):
        gateway=await self.gateway(lambda *_:response(raw_usage={}))
        await gateway.chat(MESSAGES,chat_id='synthetic-chat')
        self.assertTrue(gateway.attempts[0]['usage_reported'])
        self.assert_summary_unknown(gateway,1)

    async def test_explicit_zero_usage_remains_known_zero_not_unknown(self):
        gateway=await self.gateway(lambda *_:response(raw_usage=fixtures.usage(0,0,0)))
        await gateway.chat(MESSAGES,chat_id='synthetic-chat')
        self.assertTrue(gateway.attempts[0]['usage_reported'])
        result=summary(gateway.metrics,attempts=1)
        for name in ('prompt_tokens','completion_tokens','cache_hit_tokens','cache_miss_tokens'):
            self.assertEqual(result['fields'][name]['observed_sum'],0)
            self.assertEqual(result['fields'][name]['known_calls'],1)
        self.assertIsNone(result['cache']['hit_ratio'])

    async def test_omitted_stream_usage_keeps_attempt_unknown_without_fake_metric(self):
        gateway=await self.gateway(lambda *_:self.stream(event('完整成稿。'),b'data: [DONE]\n\n'))
        with gateway.capture_request() as trace:
            self.assertEqual(await self.collect(gateway),'完整成稿。')
        self.assertEqual(gateway.attempts[0]['status'],'success')
        self.assertFalse(gateway.attempts[0]['usage_reported'])
        self.assertEqual(len(gateway.metrics),0)
        self.assertIsNone(trace['usage']['cache_hit_tokens'])
        self.assert_summary_unknown(gateway,1)

    async def test_repeated_stream_usage_for_one_request_must_not_double_count(self):
        raw=fixtures.usage(100,80)
        gateway=await self.gateway(lambda *_:self.stream(event('完整成稿。'),event(raw_usage=raw),
            event(raw_usage=raw),b'data: [DONE]\n\n'))
        self.assertEqual(await self.collect(gateway),'完整成稿。')
        self.assertEqual(len(self.packets),1)
        self.assertEqual(len(gateway.attempts),1)
        self.assertEqual(len(gateway.metrics),1,'Repeated usage chunks describe one request, not two paid calls')
        self.assertEqual(summary(gateway.metrics,attempts=1)['fields']['prompt_tokens']['observed_sum'],100)


if __name__=='__main__':unittest.main()
