"""Fault injection reproducing the attached log's network outage, without user credentials."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import httpx
from src.adapters.ilink import ILinkAdapter
from src.core.llm import LLMGateway,ProviderCfg,is_transient_llm_error
from src.core.types import WxMessage
from src.core.context import ContextManager
from src.core.sender import HumanLikeSender,DeliveryError
from src.core.trigger import TriggerRouter
from src.store_memory import MemoryStore
from src.orchestrator import Orchestrator


def message(mid='fixture'):
    return WxMessage(mid,'ilink','private','peer','peer','fixture','bot','请回答原问题',context_token='context-fixture')

class Stream:
    def __init__(self,values=None,error=None):self.values=iter(values or []);self.error=error;self.closed=False
    def __aiter__(self):return self
    async def __anext__(self):
        try:text=next(self.values)
        except StopIteration:
            if self.error:error,self.error=self.error,None;raise error
            raise StopAsyncIteration
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])
    async def close(self):self.closed=True


def sdk(create):return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=AsyncMock())

class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self):return LLMGateway(ProviderCfg('primary','https://model.invalid','fixture-key','fixture-model'))
    async def test_sdk_native_timeout_connect_bound(self):
        g=self.gateway();c=g._client(g.primary)
        try:self.assertEqual(c.timeout.connect,8);self.assertEqual(c.timeout.pool,3);self.assertEqual(c.max_retries,0)
        finally:await g.aclose()
    async def test_connection_retry_before_tokens_uses_temporary_client(self):
        g=self.gateway();first=sdk(AsyncMock(side_effect=httpx.ConnectError('fixture')));retry=sdk(AsyncMock(return_value=Stream(['完整','回答'])));g._client=lambda _:first;g._new_client=lambda _:retry
        text=''.join([s async for s in g.stream([])]);self.assertEqual(text,'完整回答');retry.close.assert_awaited_once();first.close.assert_not_awaited()
    async def test_retry_bounded_to_two_requests(self):
        g=self.gateway();a=sdk(AsyncMock(side_effect=httpx.ConnectTimeout('fixture')));b=sdk(AsyncMock(side_effect=httpx.ConnectTimeout('fixture')));g._client=lambda _:a;g._new_client=lambda _:b
        with self.assertRaises(httpx.ConnectTimeout):[s async for s in g.stream([])]
        self.assertEqual(a.chat.completions.create.await_count+b.chat.completions.create.await_count,2)
    async def test_partial_stream_never_retried_or_concatenated(self):
        g=self.gateway();stream=Stream(['半段'],httpx.ReadError('fixture'));a=sdk(AsyncMock(return_value=stream));g._client=lambda _:a;g._new_client=AsyncMock()
        values=[]
        with self.assertRaises(httpx.ReadError):
            async for s in g.stream([]):values.append(s)
        self.assertEqual(values,['半段']);g._new_client.assert_not_called();self.assertTrue(stream.closed)
    async def test_non_network_error_not_retried(self):
        g=self.gateway();a=sdk(AsyncMock(side_effect=ValueError('fixture')));g._client=lambda _:a;g._new_client=AsyncMock()
        with self.assertRaises(ValueError):[s async for s in g.stream([])]
        g._new_client.assert_not_called()
    async def test_non_stream_memory_call_bounded_retry(self):
        g=self.gateway();response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='OK'))],usage=None)
        a=sdk(AsyncMock(side_effect=httpx.ConnectTimeout('fixture')));b=sdk(AsyncMock(return_value=response));g._client=lambda _:a;g._new_client=lambda _:b
        self.assertEqual(await g.chat([]),'OK');b.close.assert_awaited_once()
    def test_classifier_sdk_httpx2_and_status(self):
        from openai import APIConnectionError
        self.assertTrue(is_transient_llm_error(APIConnectionError(request=httpx.Request('POST','https://model.invalid'))))
        self.assertTrue(is_transient_llm_error(SimpleNamespace(status_code=503)));self.assertFalse(is_transient_llm_error(SimpleNamespace(status_code=401)))
        self.assertFalse(is_transient_llm_error(RuntimeError('ordinary failure')))

class WechatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.a=ILinkAdapter(self.tmp.name);self.a.bot_token='fixture-token';self.a.bot_id='fixture@im.bot';self.a._ctx_tokens['peer']='context-fixture'
    async def asyncTearDown(self):
        await self.a._client.aclose()
        if self.a._poll_client is not None:await self.a._poll_client.aclose()
        self.tmp.cleanup()
    def orch(self,model,typing=False):
        store=MemoryStore();return Orchestrator(self.a,store,model,None,ContextManager(store),TriggerRouter(),HumanLikeSender(self.a),typing_indicator=typing)
    async def test_send_connect_timeout_recovers_same_payload_and_id(self):
        sent=[]
        def fail(request):sent.append(json.loads(request.content));raise httpx.ConnectTimeout('fixture')
        def accept(request):sent.append(json.loads(request.content));return httpx.Response(200,json={'ret':0,'message_id':'server-fixture'})
        await self.a._client.aclose();self.a._client=httpx.AsyncClient(transport=httpx.MockTransport(fail))
        retry=httpx.AsyncClient(transport=httpx.MockTransport(accept))
        with patch('src.adapters.ilink.make_client',return_value=retry),self.a.bind_reply_context(message()):
            self.assertEqual(await self.a.send_text('peer','完整回答'),'server-fixture')
        self.assertEqual(sent[0]['msg'],sent[1]['msg']);self.assertEqual(self.a.delivery_status()['network_retries'],1);self.assertTrue(retry.is_closed);self.assertFalse(self.a._client.is_closed)
        with self.a.bind_reply_context(message()):await self.a.send_text('peer','不得重新生成')
        self.assertEqual(len(sent),2)
    async def test_send_read_timeout_unknown_acceptance_reuses_client_id(self):
        payloads=[]
        def lost(request):payloads.append(json.loads(request.content)['msg']);raise httpx.ReadTimeout('fixture response lost')
        def accepted(request):payloads.append(json.loads(request.content)['msg']);return httpx.Response(200,json={'ret':0})
        await self.a._client.aclose();self.a._client=httpx.AsyncClient(transport=httpx.MockTransport(lost))
        retry=httpx.AsyncClient(transport=httpx.MockTransport(accepted))
        with patch('src.adapters.ilink.make_client',return_value=retry),self.a.bind_reply_context(message()):await self.a.send_text('peer','回答')
        self.assertEqual(payloads[0]['client_id'],payloads[1]['client_id']);self.assertEqual(payloads[0]['context_token'],payloads[1]['context_token'])
    async def test_persistent_send_failure_keeps_draft_on_disk(self):
        def failed(request):raise httpx.ConnectTimeout('fixture')
        await self.a._client.aclose();self.a._client=httpx.AsyncClient(transport=httpx.MockTransport(failed));retry=httpx.AsyncClient(transport=httpx.MockTransport(failed))
        with patch('src.adapters.ilink.make_client',return_value=retry),self.a.bind_reply_context(message()):
            with self.assertRaises(httpx.ConnectTimeout):await self.a.send_text('peer','原答案')
        self.assertEqual(self.a.get_reply_draft(message()),'原答案');self.assertEqual(self.a.delivery_status()['last_status'],'retry_pending');self.assertIn('原答案',(Path(self.tmp.name)/'outbox.json').read_text())
    async def test_business_error_not_retried_inline(self):
        calls=[]
        def rejected(request):calls.append(request);return httpx.Response(200,json={'ret':-7})
        await self.a._client.aclose();self.a._client=httpx.AsyncClient(transport=httpx.MockTransport(rejected))
        with patch('src.adapters.ilink.make_client') as factory,self.a.bind_reply_context(message()):
            with self.assertRaises(RuntimeError):await self.a.send_text('peer','答案')
            factory.assert_not_called()
        self.assertEqual(len(calls),1)
    async def test_poll_has_independent_pool_and_timeout(self):
        command=AsyncMock();command.post.return_value=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'ret':0})
        original=self.a._client;self.a._client=command
        poll=AsyncMock();poll.post.return_value=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'ret':0,'msgs':[]})
        with patch('src.adapters.ilink.make_client',return_value=poll):await self.a._post('ilink/bot/getupdates',{})
        await self.a._post('ilink/bot/getconfig',{'ilink_user_id':'peer'})
        self.assertEqual(command.post.await_count,1);self.assertEqual(poll.post.await_count,1);self.assertEqual(poll.post.call_args.kwargs['timeout'].read,40);self.assertEqual(command.post.call_args.kwargs['timeout'].connect,1.5)
        self.a._client=original
    async def test_send_connect_timeout_budget_not_overridden_by_float(self):
        fake=AsyncMock();fake.post.return_value=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'ret':0});original=self.a._client;self.a._client=fake
        try:await self.a._post('ilink/bot/sendmessage',{})
        finally:self.a._client=original
        timeout=fake.post.call_args.kwargs['timeout'];self.assertEqual(timeout.connect,3);self.assertEqual(timeout.read,15)
    async def test_typing_slow_config_over_one_second_allowed(self):
        calls=[]
        async def post(endpoint,body):
            calls.append((endpoint,body))
            if endpoint.endswith('/getconfig'):await asyncio.sleep(1.05);return {'ret':0,'typing_ticket':'peer-ticket'}
            return {'ret':0}
        self.a._post=post;o=self.orch(object(),typing=True)
        with self.a.bind_reply_context(message()):self.assertTrue(await o._typing_once(message(),True))
        self.assertEqual(calls[0][1]['context_token'],'context-fixture');self.assertEqual(calls[1][1]['status'],1);self.assertEqual(self.a.delivery_status()['typing']['starts'],1)
        self.assertTrue(await self.a.send_typing('peer',False));self.assertEqual(calls[-1][1]['status'],2);await o.close()
    async def test_typing_ticket_scoped_by_peer(self):
        calls=[]
        async def post(endpoint,body):calls.append((endpoint,body));return {'ret':0,'typing_ticket':body.get('ilink_user_id','')+'-ticket'}
        self.a._post=post;self.a._ctx_tokens['other']='other-context'
        with self.a.bind_reply_context(message()):await self.a.send_typing('peer');await self.a.send_typing('other')
        configs=[b for e,b in calls if e.endswith('/getconfig')];self.assertEqual([c['context_token'] for c in configs],['context-fixture','other-context'])
        self.assertEqual([b['typing_ticket'] for e,b in calls if e.endswith('/sendtyping')],['peer-ticket','other-ticket'])
    async def test_stop_typing_does_not_getconfig(self):
        self.a._post=AsyncMock(return_value={'ret':0});self.assertFalse(await self.a.send_typing('peer',False));self.a._post.assert_not_called()
    async def test_typing_bad_ticket_refreshes_next_attempt(self):
        self.a._typing_tickets['peer']=('stale-ticket',time.time()+60);self.a._post=AsyncMock(side_effect=[{'ret':-1},{'ret':0,'typing_ticket':'fresh'},{'ret':0}])
        self.assertFalse(await self.a.send_typing('peer'));self.assertTrue(await self.a.send_typing('peer'));self.assertIn('/getconfig',self.a._post.call_args_list[1].args[0])
    async def test_startup_config_seeds_owner_ticket(self):
        self.a.owner_user_id='peer';self.a._post=AsyncMock(return_value={'ret':0,'typing_ticket':'seeded'});await self.a._refresh_config();self.a._post.reset_mock();await self.a.send_typing('peer');self.assertEqual(self.a._post.call_args.args[0],'ilink/bot/sendtyping')
    async def test_typing_failure_never_delays_actual_reply(self):
        calls=[]
        async def post(endpoint,body):
            calls.append(endpoint)
            if endpoint.endswith('/getconfig'):await asyncio.Event().wait()
            return {'ret':0}
        class Model:
            async def stream(self,*a,**k):await asyncio.sleep(.6);yield '完整回答'
        self.a._post=post;o=self.orch(Model(),True);started=time.monotonic();await o.handle(message());self.assertLess(time.monotonic()-started,1);self.assertEqual(calls.count('ilink/bot/sendmessage'),1);await o.close()
    async def test_ambiguous_typing_on_cancel_still_sends_off(self):
        on_started=asyncio.Event();statuses=[]
        async def post(endpoint,body):
            if endpoint.endswith('/sendtyping'):
                statuses.append(body['status'])
                if body['status']==1:on_started.set();await asyncio.Event().wait()
            return {'ret':0}
        class Model:
            async def stream(self,*a,**k):await on_started.wait();yield '完整回答'
        self.a._typing_tickets['peer']=('ticket',time.time()+60);self.a._post=post;o=self.orch(Model(),True);await asyncio.wait_for(o.handle(message()),2);await asyncio.gather(*o._typing_cleanup_tasks);self.assertEqual(statuses,[1,2]);self.assertFalse(self.a.typing_cleanup_required('peer'));await o.close()
    async def test_model_outage_keeps_incoming_without_placeholder_draft(self):
        class Model:
            def __init__(self):self.calls=0
            async def stream(self,*a,**k):
                self.calls+=1
                if self.calls==1:raise httpx.ConnectTimeout('fixture outage')
                yield '原问题的完整答案'
        calls=[]
        async def post(endpoint,body):calls.append(body['msg']['item_list'][0]['text_item']['text']);return {'ret':0}
        self.a._post=post;m=Model();o=self.orch(m);raw={'message_type':1,'message_id':'fixture','from_user_id':'peer','context_token':'context-fixture','item_list':[{'type':1,'text_item':{'text':'原问题'}}]};self.a._pending=[raw]
        await self.a._process_pending(raw,'peer');self.assertEqual(len(self.a._pending),1);self.assertEqual(calls,[]);self.assertIsNone(self.a.get_reply_draft(message()));self.assertEqual(self.a.delivery_status()['last_status'],'generation_retry_pending')
        await self.a._handle_raw(raw);self.assertEqual(calls,['原问题的完整答案']);self.assertEqual(len(self.a._pending),0);self.assertEqual(m.calls,2);await o.close()


class EncodingTests(unittest.TestCase):
    def test_frozen_entry_forces_utf8_before_server_main(self):
        import importlib.util,os,sys
        entry=Path(__file__).resolve().parents[1]/'desktop/backend/entry.py'
        spec=importlib.util.spec_from_file_location('fixture_backend_entry',entry);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import src.server as server
        stream1=SimpleNamespace(reconfigure=lambda **kw:calls.append(kw));stream2=SimpleNamespace(reconfigure=lambda **kw:calls.append(kw));calls=[]
        with patch.object(sys,'stdout',stream1),patch.object(sys,'stderr',stream2),patch.object(server,'main'),patch.dict(os.environ,{},clear=False):module.main()
        self.assertEqual(calls,[{'encoding':'utf-8','errors':'replace'}]*2)

if __name__=='__main__':unittest.main()
