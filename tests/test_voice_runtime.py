"""Voice after reliable text: synthetic WAV, mock model/API/CDN, no paid calls."""
import asyncio
from dataclasses import asdict
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import httpx

from src.adapters.ilink import ILinkAdapter
from src.config_store import AppConfig,ConfigStore
from src.core.types import WxMessage
from src.core.voice import AudioResult,VoiceSynthesizer
from src.runtime import BotRuntime


def wav():
    out=io.BytesIO()
    with wave.open(out,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000);audio.writeframes(b'\0\0'*2400)
    return out.getvalue()

class FakeLLM:
    def __init__(self):self.calls=0
    async def stream(self,*args,**kwargs):self.calls+=1;yield '演出还没有结束。'
    async def aclose(self):pass

class VoiceEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);store=ConfigStore(root/'config.json');cfg=AppConfig()
        cfg.adapter.wechat_enabled=cfg.adapter.qq_enabled=False
        cfg.bot.memory_enabled=False;cfg.bot.reply_mode='always';cfg.bot.typing_indicator=False
        cfg.llm.search_enabled=False;cfg.voice.enabled=True;cfg.voice.api_key='synthetic-fish-key'
        cfg.voice.reply_rate=1;cfg.voice.cooldown_seconds=0;store.save(cfg)
        self.runtime=BotRuntime(store,root);self.addAsyncCleanup(self.runtime.stop)
        self.adapter=ILinkAdapter(str(root/'ilink'),base_url='https://ilink.fixture.invalid')
        self.adapter.bot_id='synthetic@im.bot';self.adapter.bot_token='synthetic-wechat-token'
        await self.adapter._client.aclose();self.events=[];self.uploads=[];self.tts_calls=[]
        def transport(request):
            if request.url.path.endswith('sendmessage'):
                body=json.loads(request.content);item=body['msg']['item_list'][0]
                self.events.append(('text' if item['type']==1 else 'audio',body))
                return httpx.Response(200,json={'ret':0,'message_id':'synthetic-'+str(len(self.events))})
            if request.url.path.endswith('getuploadurl'):
                return httpx.Response(200,json={'ret':0,'upload_full_url':'https://cdn.fixture.invalid/upload'})
            if request.url.path.endswith('/upload'):
                self.uploads.append(request.content)
                return httpx.Response(200,headers={'x-encrypted-param':'synthetic-encrypted-ref'})
            raise AssertionError('unexpected endpoint')
        self.adapter._client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        self.addAsyncCleanup(self.adapter._client.aclose)
        self.orch=self.runtime._build_orchestrator(self.adapter,'wechat')
        self.runtime.wechat_adapter=self.adapter;self.runtime.wechat_orch=self.orch
        self.gateway=FakeLLM();original=self.orch.llm;self.orch.llm=self.gateway
        await original.aclose()
        def tts_factory(*args,**kwargs):
            def serve(request):self.tts_calls.append(json.loads(request.content));return httpx.Response(200,content=wav())
            return httpx.AsyncClient(transport=httpx.MockTransport(serve))
        self.runtime.voice.synthesizer=VoiceSynthesizer(client_factory=tts_factory)

    def msg(self):
        return WxMessage('message-one','ilink','private','alice','alice','Alice','synthetic@im.bot',
                         '用语音回复吧',context_token='synthetic-context',persona_space=self.orch.persona_space)

    async def test_one_model_reply_then_one_audio_with_same_text_and_no_duplicate_history(self):
        msg=self.msg();await self.orch.handle(msg)
        await self.runtime.voice.wait_idle()
        self.assertEqual([kind for kind,_ in self.events],['text','audio'])
        self.assertEqual(self.gateway.calls,1);self.assertEqual(len(self.tts_calls),1)
        text=self.events[0][1]['msg']['item_list'][0]['text_item']['text']
        self.assertEqual(self.tts_calls[0]['text'],text)
        self.assertEqual(self.events[1][1]['msg']['context_token'],'synthetic-context')
        self.assertEqual(self.events[1][1]['msg']['item_list'][0]['type'],4)
        self.assertTrue(self.uploads[0]);self.assertNotEqual(self.uploads[0],wav())
        history=await self.orch.store.get_recent(msg.ctx_key)
        self.assertEqual(len(history),1);self.assertEqual(history[0]['assistant'],text)
        await self.orch.handle(msg);await self.runtime.voice.wait_idle()
        self.assertEqual(len(self.events),2);self.assertEqual(len(self.tts_calls),1)

    async def test_tts_failure_never_retries_accepted_text_or_sends_chat_configuration_error(self):
        class Failed:
            async def synthesize(self,*args):raise RuntimeError('synthetic provider error')
        self.runtime.voice.synthesizer=Failed()
        await self.orch.handle(self.msg());await self.runtime.voice.wait_idle()
        self.assertEqual([kind for kind,_ in self.events],['text'])
        self.assertEqual((await self.orch.store.get_recent(self.msg().ctx_key))[0]['assistant'],'演出还没有结束。')
        self.assertEqual(self.runtime.conversations.status()['uncertain'],0)

    async def test_new_input_before_background_job_starts_does_not_leak_a_job_or_bill(self):
        msg=self.msg();self.runtime.voice.note_in(msg)
        self.assertTrue(self.runtime.voice.schedule(msg,'这场演出还没有结束。',self.adapter))
        newer=self.msg();newer.msg_id='new'
        self.runtime.voice.note_in(newer)
        await self.runtime.voice.wait_idle()
        self.assertEqual(self.runtime.voice.status()['pending'],0)
        self.assertEqual(self.tts_calls,[]);self.assertEqual(self.events,[])

    async def test_muted_contacts_are_not_evicted_by_metadata_cache_cleanup(self):
        voice=self.runtime.voice
        voice.contacts={str(i):{'muted':True,'last_sent':0} for i in range(513)}
        voice._save()
        self.assertEqual(len(voice.contacts),513)
        self.assertTrue(json.loads(voice.path.read_text())['contacts']['0']['muted'])


class VoiceAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.temp.name,
            'WXBOT_ADMIN_TOKEN':'voice-settings-fixture','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'})
        self.env.start();import src.server as server;self.server=importlib.reload(server)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',
            headers={'Authorization':'Bearer voice-settings-fixture'})
    async def asyncTearDown(self):
        await self.client.aclose();await self.server.runtime.stop();self.env.stop();self.temp.cleanup()
    async def save(self,**values):return await self.client.patch('/api/config/section/voice',json=values)

    async def test_independent_save_mask_keep_key_and_clear_disable(self):
        self.assertEqual((await self.save(api_key='independent-fish-key',enabled=True)).status_code,200)
        result=await self.client.get('/api/config');self.assertNotIn('independent-fish-key',result.text)
        self.assertEqual((await self.save(api_key='',reply_rate=.4)).status_code,200)
        self.assertEqual(self.server.config_store.load().voice.api_key,'independent-fish-key')
        self.assertEqual((await self.save(clear_api_key=True)).status_code,200)
        cfg=self.server.config_store.load();self.assertEqual(cfg.voice.api_key,'');self.assertFalse(cfg.voice.enabled)

    async def test_bad_values_cross_section_and_free_model_typo_cannot_make_paid_calls(self):
        for values in [{'primary_key':'wrong-card'},{'fish_model':'s2.1-pro-free-typo'}, {'reply_rate':2},
                       {'max_chars':True},{'base_url':{}},{'base_url':'https://u:p@host'},{'enabled':True}]:
            with self.subTest(values=values):self.assertEqual((await self.save(**values)).status_code,400)
        self.assertFalse(self.server.config_store.load().voice.enabled)

    async def test_legacy_config_put_cannot_reset_voice_preferences(self):
        await self.save(api_key='independent-fish-key',reply_rate=.4)
        raw=asdict(self.server.config_store.load());raw.pop('voice')
        self.assertEqual((await self.client.put('/api/config',json=raw)).status_code,200)
        voice=self.server.config_store.load().voice
        self.assertEqual(voice.api_key,'independent-fish-key');self.assertEqual(voice.reply_rate,.4)

    async def test_preview_is_authenticated_audio_only_and_never_a_chat_message(self):
        calls=[]
        class Synth:
            async def synthesize(self,cfg,text):calls.append(text);return AudioResult(wav())
        self.server.runtime.voice.synthesizer=Synth()
        self.client.headers.clear()
        self.assertEqual((await self.client.post('/api/voice/preview',json={'text':'演出还没有结束。'})).status_code,401)
        self.assertEqual(calls,[])
        self.client.headers['Authorization']='Bearer voice-settings-fixture'
        response=await self.client.post('/api/voice/preview',json={'text':'演出还没有结束。'})
        self.assertEqual(response.status_code,200);self.assertEqual(response.content,wav())
        self.assertEqual(response.headers['cache-control'],'no-store');self.assertEqual(len(calls),1)
        status=(await self.client.get('/api/voice/status')).json();self.assertEqual(status['delivery'],'audio_file')
        self.assertFalse(status['client_playback_verified'])

    async def test_foreign_origin_cannot_spend_preview_credit(self):
        response=await self.client.post('/api/voice/preview',json={'text':'演出还没有结束。'},headers={'Origin':'https://foreign.invalid'})
        self.assertEqual(response.status_code,403)

if __name__=='__main__':unittest.main()
