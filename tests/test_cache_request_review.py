"""Actual SDK serialization audit with synthetic HTTP responses; never network/keys.

A matching messages prefix alone is not an observed server KV hit. This fixture
records the JSON emitted by the real SDK, and supplies explicit synthetic usage.
"""
import asyncio
import copy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from src.core.context import ContextManager
from src.core.llm import LLMGateway, ProviderCfg
from src.core.sender import HumanLikeSender
from src.core.trigger import TriggerRouter
from src.core.types import WxMessage
from src.orchestrator import Orchestrator
from src.store_memory import MemoryStore

try:
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    import openai._base_client as sdk_base
    sdk_http = getattr(sdk_base, 'httpx2', None) or getattr(sdk_base, 'httpx', None)
except ImportError:
    AsyncOpenAI = sdk_http = None


def message(mid='1', owner='audit', text='今天路上遇到一只猫。'):
    return WxMessage(mid, 'onebot', 'private', 'p_' + owner, owner, '合成用户', 'bot', text)


def usage(prompt=100, hit=0, completion=10):
    return {'prompt_tokens': prompt, 'completion_tokens': completion,
            'total_tokens': prompt + completion,
            'prompt_cache_hit_tokens': hit, 'prompt_cache_miss_tokens': prompt-hit}


def completion(text='听着呢。', *, reasoning=None, actual_usage=None):
    response = {'id':'synthetic-completion','object':'chat.completion','created':1,
                'model':'deepseek-flash','choices':[{'index':0,'finish_reason':'stop',
                'message':{'role':'assistant','content':text}}], 'usage':actual_usage or usage()}
    if reasoning is not None:
        response['choices'][0]['message']['reasoning_content']=reasoning
    return response


class Adapter:
    platform='onebot'
    def __init__(self): self.sent=[]
    def on_message(self, handler): self.handler=handler
    async def send_text(self, chat, text, at=None):
        self.sent.append(text)
        return 'synthetic-accepted'


@unittest.skipUnless(AsyncOpenAI is not None and sdk_http is not None, 'real OpenAI SDK transport not installed')
class SDKRequestReview(unittest.IsolatedAsyncioTestCase):
    async def gateway(self, callback=None, *, search=False):
        self.payloads=[]
        clients=[]
        def handler(request):
            # Only test-owned fake credentials and synthetic bodies enter here.
            body=json.loads(request.content)
            self.payloads.append(copy.deepcopy(body))
            reply=callback(body,len(self.payloads)) if callback else completion()
            if isinstance(reply,tuple):
                code,payload=reply
                return sdk_http.Response(code,json=payload)
            if body.get('stream'):
                delta={'content':reply['choices'][0]['message']['content']}
                if 'reasoning_content' in reply['choices'][0]['message']:
                    delta['reasoning_content']=reply['choices'][0]['message']['reasoning_content']
                chunks=[{'id':'synthetic','object':'chat.completion.chunk','created':1,
                         'model':'deepseek-flash','choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                        {'id':'synthetic','object':'chat.completion.chunk','created':1,
                         'model':'deepseek-flash','choices':[], 'usage':reply['usage']}]
                body=''.join('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
                return sdk_http.Response(200,text=body,headers={'content-type':'text/event-stream'})
            return sdk_http.Response(200,json=reply)
        def make_client(_cfg):
            http=DefaultAsyncHttpxClient(transport=sdk_http.MockTransport(handler),trust_env=False)
            client=AsyncOpenAI(api_key='synthetic-fixture-not-a-real-key',base_url='https://api.deepseek.com',
                               max_retries=0,http_client=http)
            clients.append(client)
            return client
        gateway=LLMGateway(ProviderCfg('audit','https://api.deepseek.com','synthetic-fixture-not-a-real-key','deepseek-flash'),
                           web_search=NS(search=AsyncMock()) if search else None)
        gateway._new_client=make_client
        async def close():
            await gateway.aclose()
            for client in clients: await client.close()
        self.addAsyncCleanup(close)
        return gateway

    async def orchestrator(self,gateway):
        store=MemoryStore()
        context=ContextManager(store)
        context.configure_session(context_window_tokens=1_000_000)
        adapter=Adapter()
        orch=Orchestrator(adapter,store,gateway,None,context,TriggerRouter(),HumanLikeSender(adapter),
                          typing_indicator=False)
        self.addAsyncCleanup(orch.close)
        return orch,store,context,adapter

    async def test_capture_is_visible_across_actual_sdk_child_task(self):
        gateway=await self.gateway(lambda *_:completion(actual_usage=usage(130,100)))
        expected=[{'role':'system','content':'固定合成人格'},{'role':'user','content':'合成当前来话'}]
        with gateway.capture_request() as trace:
            result=await asyncio.create_task(gateway.chat(expected,chat_id='synthetic'))
        self.assertEqual(result,'听着呢。')
        self.assertEqual(trace['messages'],self.payloads[0]['messages'])
        self.assertEqual(trace['messages'],expected)
        self.assertEqual(trace['usage']['prompt_tokens'],130)
        self.assertEqual(trace['usage']['prompt_cache_hit_tokens'],100)
        self.assertEqual(self.payloads[0]['thinking'],{'type':'disabled'})

    async def test_stream_capture_survives_orchestrator_anext_child_tasks(self):
        gateway=await self.gateway(lambda *_:completion(actual_usage=usage(140,110)))
        orch,store,context,adapter=await self.orchestrator(gateway)
        current=message()
        await orch.handle(current)
        persisted=await store.get_session(current.ctx_key)
        actual=self.payloads[0]['messages']
        self.assertEqual(persisted['messages'][:len(actual)],actual)
        self.assertEqual(persisted['messages'][len(actual)],{'role':'assistant','content':'听着呢。'})
        self.assertEqual(len(persisted['messages']),len(actual)+2)
        self.assertEqual(persisted['messages'][-1]['role'],'system')
        self.assertIn('投递时间',persisted['messages'][-1]['content'])
        self.assertEqual(persisted['prompt_tokens'],140)
        self.assertEqual(context.last_budget['cache_hit_tokens'],110)
        self.assertEqual(adapter.sent,['听着呢。'])
        self.assertTrue(self.payloads[0]['stream'])
        self.assertEqual(self.payloads[0]['stream_options'],{'include_usage':True})

    async def test_parallel_capture_records_are_account_isolated(self):
        gateway=await self.gateway()
        async def one(text):
            with gateway.capture_request() as trace:
                await asyncio.create_task(gateway.chat([{'role':'user','content':text}],chat_id=text))
            return trace
        a,b=await asyncio.gather(one('账号甲的合成输入'),one('账号乙的合成输入'))
        self.assertEqual(a['messages'],[{'role':'user','content':'账号甲的合成输入'}])
        self.assertEqual(b['messages'],[{'role':'user','content':'账号乙的合成输入'}])
        self.assertIsNot(a,b)

    async def test_real_sdk_plain_chat_reuses_exact_previous_input_prefix(self):
        gateway=await self.gateway()
        orch,store,context,adapter=await self.orchestrator(gateway)
        await orch.handle(message('1'))
        await orch.handle(message('2',text='它戴着蓝色的项圈。'))
        first,second=self.payloads
        self.assertEqual(second['messages'][:len(first['messages'])],first['messages'])
        for name in ('model','thinking','stream','temperature','max_tokens'):
            self.assertEqual(second.get(name),first.get(name))
        self.assertEqual(len(adapter.sent),2)

    async def test_retry_keeps_same_sdk_body_and_only_records_successful_usage(self):
        def reply(_body,index):
            if index==1:
                return 503,{'error':{'message':'synthetic transient','type':'server_error'}}
            return completion(actual_usage=usage(120,80))
        gateway=await self.gateway(reply)
        with gateway.capture_request() as trace:
            await gateway.chat([{'role':'user','content':'合成重试输入'}],chat_id='synthetic')
        self.assertEqual(len(self.payloads),2)
        self.assertEqual(self.payloads[0],self.payloads[1])
        self.assertEqual(trace['messages'],self.payloads[1]['messages'])
        self.assertEqual(len(gateway.metrics),1)
        self.assertEqual(gateway.metrics[0]['cache_miss_tokens'],40)

    async def test_usage_totals_include_cached_prefix_not_only_new_tail(self):
        def reply(_body,index):
            return completion(actual_usage=usage(1000*index,1000*index-100))
        gateway=await self.gateway(reply)
        for index in range(1,4):
            await gateway.chat([{'role':'user','content':'合成输入'+str(index)}],chat_id='synthetic')
        self.assertEqual(sum(row['prompt_tokens'] for row in gateway.metrics),6000)
        self.assertEqual(sum(row['cache_hit_tokens'] for row in gateway.metrics),5700)
        self.assertEqual(sum(row['cache_miss_tokens'] for row in gateway.metrics),300)
        # These fixture values are evidence of usage handling, not live API hits.

    async def test_deep_tools_final_assistant_reasoning_survives_next_turn(self):
        def reply(body,_index):
            needs_reasoning=body.get('thinking',{}).get('type')=='enabled' and bool(body.get('tools'))
            missing=any(m.get('role')=='assistant' and not isinstance(m.get('reasoning_content'),str)
                        for m in body['messages'])
            if needs_reasoning and missing:
                return 400,{'error':{'message':'synthetic fixture: missing historical reasoning_content',
                                    'type':'invalid_request_error'}}
            return completion('这只猫很特别。',reasoning='合成的模型协议字段，不是真实推理记录。')
        gateway=await self.gateway(reply,search=True)
        gateway.chat_mode='deep'
        orch,store,context,adapter=await self.orchestrator(gateway)
        await orch.handle(message('1'))
        await orch.handle(message('2',text='项圈上写着它的名字。'))
        self.assertEqual(len(self.payloads),2,
            'A missing final reasoning_content must not cause a third no-tools fallback request')
        self.assertTrue(all(body.get('tools') for body in self.payloads))
        previous=[m for m in self.payloads[1]['messages'] if m.get('role')=='assistant']
        self.assertTrue(previous)
        self.assertTrue(all(isinstance(m.get('reasoning_content'),str) for m in previous))
        self.assertEqual(len(adapter.sent),2)


if __name__=='__main__':unittest.main()
