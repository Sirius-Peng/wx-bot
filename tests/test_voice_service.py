"""Local-only TTS HTTP contracts and optional WeChat speech scheduling.

Audio is a synthetic PCM fixture, not a generated or trained character voice.
No configured credentials, platform accounts, external model calls or cloud fees.
"""
import asyncio
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import wave

import httpx

from src.config_store import VoiceConfig
from src.core.types import WxMessage
from src.core.voice import AudioResult, VoiceReplies, VoiceSynthesizer


def wav_fixture(seconds=.1):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000)
        audio.writeframes(b'\0\0'*int(24000*seconds))
    return stream.getvalue()


WAV=wav_fixture()

def message(mid='one',text='今天过得怎么样？',platform='ilink',owner='alice'):
    return WxMessage(mid,platform,'private',owner,owner,'Alice','bot',text,
                     context_token='synthetic-context',timestamp=1793073600000)


class VoiceSynthesisContracts(unittest.IsolatedAsyncioTestCase):
    async def make(self,handler,*,follow_redirects=False):
        requests=[];clients=[]
        async def capture(request):
            requests.append(request)
            result=handler(request)
            return await result if asyncio.iscoroutine(result) else result
        def factory(*_args,**_kwargs):
            client=httpx.AsyncClient(transport=httpx.MockTransport(capture),trust_env=False,
                                     follow_redirects=follow_redirects)
            clients.append(client);return client
        self.addAsyncCleanup(self.close_clients,clients)
        return VoiceSynthesizer(client_factory=factory),requests

    @staticmethod
    async def close_clients(clients):
        for client in clients:await client.aclose()

    def configured(self,**values):
        cfg=VoiceConfig(api_key='synthetic-voice-key')
        for field,value in values.items():setattr(cfg,field,value)
        return cfg

    async def test_fish_uses_explicit_engine_voice_id_and_one_wav_request(self):
        synth,requests=await self.make(lambda _:httpx.Response(200,content=WAV,headers={'content-type':'audio/wav'}))
        cfg=self.configured();original=copy.deepcopy(cfg)
        result=await synth.synthesize(cfg,'演出还没有结束。')
        self.assertEqual(len(requests),1)
        request=requests[0];body=json.loads(request.content)
        self.assertEqual(str(request.url),'https://api.fish.audio/v1/tts')
        self.assertEqual(request.headers.get('Authorization'),'Bearer synthetic-voice-key')
        self.assertEqual(request.headers.get('model'),'s2.1-pro')
        self.assertEqual(body['text'],'演出还没有结束。')
        self.assertEqual(body['reference_id'],'5b432f747d00419aba7cb12fa50e4af0')
        self.assertEqual((body['format'],body['sample_rate']),('wav',24000))
        self.assertEqual(result.data,WAV);self.assertEqual(result.mime_type,'audio/wav')
        self.assertTrue(result.filename.endswith('.wav'));self.assertEqual(cfg,original)

    async def test_local_gpt_sovits_uses_reference_on_service_machine_without_cloud_key(self):
        synth,requests=await self.make(lambda _:httpx.Response(200,content=WAV,headers={'content-type':'audio/wav'}))
        cfg=self.configured(provider='gpt_sovits',base_url='http://127.0.0.1:9880',
                            reference_audio=r'D:\voices\phrolova.wav',reference_text='跟上我的节奏。')
        result=await synth.synthesize(cfg,'我在这里。')
        self.assertEqual(result.data,WAV);self.assertEqual(len(requests),1)
        request=requests[0];body=json.loads(request.content)
        self.assertEqual(str(request.url),'http://127.0.0.1:9880/tts')
        self.assertNotIn('authorization',request.headers)
        self.assertEqual(body,{'text':'我在这里。','text_lang':'zh','ref_audio_path':r'D:\voices\phrolova.wav',
                              'prompt_text':'跟上我的节奏。','prompt_lang':'zh','media_type':'wav','streaming_mode':False})

    async def test_unconfigured_service_is_rejected_before_any_provider_request(self):
        synth,requests=await self.make(lambda _:httpx.Response(200,content=WAV,headers={'content-type':'audio/wav'}))
        for cfg in [self.configured(api_key=''),self.configured(provider='gpt_sovits',base_url='http://127.0.0.1:9880',reference_audio='',reference_text='')]:
            with self.subTest(provider=cfg.provider),self.assertRaises(Exception):
                await synth.synthesize(cfg,'测试。')
        self.assertEqual(requests,[])

    async def test_provider_http_failure_is_not_retried_on_another_paid_engine(self):
        synth,requests=await self.make(lambda _:httpx.Response(429,json={'message':'synthetic throttling'}))
        with self.assertRaises(Exception):await synth.synthesize(self.configured(),'短回复。')
        self.assertEqual(len(requests),1);self.assertEqual(requests[0].headers.get('model'),'s2.1-pro')

    async def test_paid_tts_307_and_308_do_not_repeat_post_even_with_redirecting_client(self):
        for status in (307,308):
            def response(request):
                if request.url.path=='/v1/tts':
                    return httpx.Response(status,headers={'location':'/paid-redirect'})
                return httpx.Response(200,content=WAV,headers={'content-type':'audio/wav'})
            synth,requests=await self.make(response,follow_redirects=True)
            with self.subTest(status=status):
                with self.assertRaises(httpx.HTTPStatusError):
                    await synth.synthesize(self.configured(),'不跟随重定向的短回复。')
                self.assertEqual(len(requests),1,'a redirect must not repeat a potentially billed POST')
                self.assertEqual(requests[0].method,'POST')
                self.assertEqual(requests[0].url.path,'/v1/tts')
                self.assertEqual(requests[0].headers.get('model'),'s2.1-pro')

    async def test_direct_unknown_fish_model_is_rejected_before_any_billed_http_request(self):
        synth,requests=await self.make(lambda _:httpx.Response(200,content=WAV))
        for model in ('s2.1-pro-free-typo','future-unknown-engine',''):
            cfg=self.configured();cfg.fish_model=model
            with self.subTest(model=model),self.assertRaises(ValueError):
                await synth.synthesize(cfg,'模型错误时不应合成。')
        self.assertEqual(requests,[])

    async def test_direct_unknown_provider_is_rejected_without_selecting_paid_fish(self):
        synth,requests=await self.make(lambda _:httpx.Response(200,content=WAV))
        for provider in ('unknown','Fish',''):
            cfg=self.configured();cfg.provider=provider
            with self.subTest(provider=provider),self.assertRaises(ValueError):
                await synth.synthesize(cfg,'供应商错误时不应合成。')
        self.assertEqual(requests,[])

    async def test_timeout_is_not_retried_or_returned_as_audio(self):
        async def timeout(request):raise httpx.ReadTimeout('synthetic timeout',request=request)
        synth,requests=await self.make(timeout)
        with self.assertRaises(Exception):await synth.synthesize(self.configured(),'短回复。')
        self.assertEqual(len(requests),1)

    async def test_json_error_body_is_rejected_even_if_server_calls_it_audio(self):
        for content_type in ['application/json','audio/wav']:
            synth,requests=await self.make(lambda _:httpx.Response(200,content=b'{"error":"not audio"}',headers={'content-type':content_type}))
            with self.subTest(content_type=content_type),self.assertRaises(Exception):
                await synth.synthesize(self.configured(),'短回复。')
            self.assertEqual(len(requests),1)

    async def test_empty_invalid_and_oversized_audio_are_rejected(self):
        for data in [b'',b'RIFF-invalid-wave',b'ID3-invalid-mp3',WAV+b'x'*(8*1024*1024)]:
            synth,requests=await self.make(lambda _,body=data:httpx.Response(200,content=body,headers={'content-type':'audio/wav'}))
            with self.subTest(size=len(data)),self.assertRaises(Exception):
                await synth.synthesize(self.configured(),'短回复。')
            self.assertEqual(len(requests),1)

    async def test_audio_longer_than_sixty_seconds_is_rejected(self):
        too_long=wav_fixture(60.1)
        synth,requests=await self.make(lambda _:httpx.Response(200,content=too_long,headers={'content-type':'audio/wav'}))
        with self.assertRaises(Exception):await synth.synthesize(self.configured(),'短回复。')
        self.assertEqual(len(requests),1)


class StubSynthesizer:
    def __init__(self,blocked=False,error=None):
        self.calls=[];self.started=asyncio.Event();self.release=asyncio.Event();self.error=error
        if not blocked:self.release.set()
    async def synthesize(self,cfg,text):
        self.calls.append((copy.deepcopy(cfg),text));self.started.set();await self.release.wait()
        if self.error:raise self.error
        return AudioResult(data=WAV,mime_type='audio/wav',filename='phrolova-synthetic.wav')


class StubAdapter:
    platform='ilink'
    def __init__(self):self.sent=[]
    async def send_audio(self,chat_id,data,**kwargs):
        if kwargs.get('check_current'):kwargs['check_current']()
        if kwargs.get('on_submit'):kwargs['on_submit']()
        self.sent.append((chat_id,data,kwargs));return 'accepted-audio'


class VoiceReplyScheduling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'voice-state.json';self.now=1793073600.0
        self.cfg=VoiceConfig(enabled=True,api_key='synthetic-key',reply_rate=1,cooldown_seconds=300)
        self.synth=StubSynthesizer();self.adapter=StubAdapter()
        self.service=VoiceReplies(self.path,lambda:self.cfg,synthesizer=self.synth,clock=lambda:self.now)
        self.addAsyncCleanup(self.service.close)

    async def test_disabled_wrong_channel_and_long_replies_never_call_tts(self):
        self.cfg.enabled=False
        self.assertFalse(self.service.schedule(message(),'一条普通短回复。',self.adapter))
        self.cfg.enabled=True
        self.assertFalse(self.service.schedule(message(platform='onebot'),'短回复。',self.adapter))
        group=message();group.chat_type='group'
        self.assertFalse(self.service.schedule(group,'短回复。',self.adapter))
        self.assertFalse(self.service.schedule(message(),'长'*501,self.adapter))
        await self.service.wait_idle();self.assertEqual(self.synth.calls,[]);self.assertEqual(self.adapter.sent,[])

    async def test_success_is_background_once_and_saved_state_has_no_reply_or_secret(self):
        original=message();self.service.note_in(original)
        self.assertTrue(self.service.schedule(original,'这场演出还没有结束。',self.adapter))
        await self.service.wait_idle()
        self.assertEqual(len(self.synth.calls),1);self.assertEqual(len(self.adapter.sent),1)
        self.assertEqual(self.adapter.sent[0][0],'alice');self.assertEqual(self.adapter.sent[0][1],WAV)
        self.assertEqual(self.adapter.sent[0][2]['mime_type'],'audio/wav')
        self.assertTrue(self.adapter.sent[0][2]['attempt_id'])
        self.assertFalse(self.service.schedule(original,'这场演出还没有结束。',self.adapter))
        await self.service.wait_idle();self.assertEqual(len(self.synth.calls),1)
        raw=self.path.read_text();self.assertNotIn('这场演出还没有结束',raw);self.assertNotIn('synthetic-key',raw)
        self.assertIsInstance(self.service.status(),dict)

    async def test_tts_exception_does_not_escape_main_reply_or_send_configuration_errors(self):
        self.service.synthesizer=StubSynthesizer(error=RuntimeError('synthetic failure'))
        msg=message();self.service.note_in(msg)
        self.service.schedule(msg,'正常文字已经发送。',self.adapter)
        await self.service.wait_idle();self.assertEqual(self.adapter.sent,[])

    async def test_cooldown_blocks_second_voice_but_not_future_eligible_turn(self):
        first=message();self.service.note_in(first)
        self.assertTrue(self.service.schedule(first,'第一条短语音。',self.adapter))
        await self.service.wait_idle()
        self.now+=10;second=message('two','继续说。');self.service.note_in(second)
        self.assertFalse(self.service.schedule(second,'冷却中不合成。',self.adapter))
        self.now+=301;third=message('three','再接着聊。');self.service.note_in(third)
        self.assertTrue(self.service.schedule(third,'冷却结束的短回复。',self.adapter))
        await self.service.wait_idle();self.assertEqual(len(self.synth.calls),2)

    async def test_voice_opt_out_survives_restart_and_explicit_opt_in_restores_it(self):
        self.service.note_in(message('stop','不要语音'))
        self.assertFalse(self.service.schedule(message('stop','不要语音'),'好的。',self.adapter))
        await self.service.close()
        restored=VoiceReplies(self.path,lambda:self.cfg,synthesizer=self.synth,clock=lambda:self.now)
        self.addAsyncCleanup(restored.close)
        ordinary=message('ordinary','聊点别的。');restored.note_in(ordinary)
        self.assertFalse(restored.schedule(ordinary,'普通短回复。',self.adapter))
        resumed=message('resume','用语音回复');restored.note_in(resumed)
        self.assertTrue(restored.schedule(resumed,'跟上我的节奏。',self.adapter))
        await restored.wait_idle();self.assertEqual(len(self.adapter.sent),1)

    async def test_new_input_after_submission_does_not_cancel_or_regenerate_audio(self):
        started=asyncio.Event();release=asyncio.Event()
        class SubmittedAdapter(StubAdapter):
            async def send_audio(adapter,chat_id,data,**kwargs):
                if kwargs.get('check_current'):kwargs['check_current']()
                kwargs['on_submit']();started.set();await release.wait()
                adapter.sent.append((chat_id,data,kwargs));return 'accepted-audio'
        adapter=SubmittedAdapter();old=message();self.service.note_in(old)
        self.assertTrue(self.service.schedule(old,'已进入提交的短回复。',adapter))
        await asyncio.wait_for(started.wait(),1)
        self.service.note_in(message('new','新的来话。'))
        release.set();await self.service.wait_idle()
        self.assertEqual(len(adapter.sent),1);self.assertEqual(len(self.synth.calls),1)

    async def test_accepted_turn_does_not_resynthesize_after_restart_and_cooldown(self):
        original=message();self.service.note_in(original)
        self.assertTrue(self.service.schedule(original,'已经发送的语音。',self.adapter))
        await self.service.wait_idle();await self.service.close();self.now+=1000
        synth=StubSynthesizer()
        restored=VoiceReplies(self.path,lambda:self.cfg,synthesizer=synth,clock=lambda:self.now)
        self.addAsyncCleanup(restored.close);restored.note_in(original)
        self.assertFalse(restored.schedule(original,'已经发送的语音。',self.adapter))
        await restored.wait_idle();self.assertEqual(synth.calls,[])

    async def test_preview_is_allowed_before_enabling_but_rejects_concurrent_paid_requests(self):
        self.cfg.enabled=False;blocked=StubSynthesizer(blocked=True);self.service.synthesizer=blocked
        first=asyncio.create_task(self.service.preview('试听短句。'))
        await asyncio.wait_for(blocked.started.wait(),1)
        with self.assertRaises(Exception):await self.service.preview('不应并发调用。')
        blocked.release.set();result=await first
        self.assertEqual(result.data,WAV);self.assertEqual(len(blocked.calls),1)
        self.assertEqual(self.adapter.sent,[])

    async def test_new_input_cancels_voice_before_transport_submission(self):
        blocked=StubSynthesizer(blocked=True);self.service.synthesizer=blocked
        old=message();self.service.note_in(old);self.service.schedule(old,'待合成短回复。',self.adapter)
        await asyncio.wait_for(blocked.started.wait(),1)
        self.service.note_in(message('new','再补充一点。'))
        blocked.release.set();await self.service.wait_idle();self.assertEqual(self.adapter.sent,[])


if __name__=='__main__':unittest.main()
