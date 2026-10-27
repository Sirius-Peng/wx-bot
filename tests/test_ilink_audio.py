"""WeChat audio attachment transport: synthetic PCM and mocked API/CDN only."""
import asyncio
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from src.adapters.ilink import ILinkAdapter, MAX_AUDIO_BYTES
from src.core.conversation import ConversationChanged
from src.core.types import WxMessage


def sound():
    data=io.BytesIO()
    with wave.open(data,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\x00\x00'*1600)
    return data.getvalue()


def message(mid='one',peer='peer',token='context-original'):
    return WxMessage(mid,'ilink','private',peer,peer,'测试用户','bot','请继续',context_token=token)


class AudioTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.adapters=[];self.requests=[];self.uploads=[];self.sends=[];self.send_responses=[]
        self.upload_status=200;self.upload_header=True;self.full_url=False
        self.adapter=await self.make_adapter();self.audio=sound()

    async def asyncTearDown(self):
        for adapter in self.adapters:await adapter._client.aclose()
        self.temp.cleanup()

    async def make_adapter(self):
        adapter=ILinkAdapter(str(self.path),base_url='https://api.fixture.invalid')
        await adapter._client.aclose()
        adapter._client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        adapter.bot_id='fixture@im.bot';adapter.bot_token='synthetic-token'
        adapter._ctx_tokens['peer']='context-fallback'
        self.adapters.append(adapter)
        return adapter

    def respond(self,request):
        self.requests.append(request)
        if request.url.path.endswith('/getuploadurl'):
            body=json.loads(request.content)
            if self.full_url:return httpx.Response(200,json={'ret':0,'upload_full_url':'https://cdn.fixture.invalid/full-upload?token=synthetic'})
            return httpx.Response(200,json={'ret':0,'upload_param':'param + /? synthetic'})
        if request.url.path.endswith('/upload') or request.url.path=='/full-upload':
            self.uploads.append(request)
            return httpx.Response(self.upload_status,headers={'x-encrypted-param':'download-synthetic'} if self.upload_header else {})
        if request.url.path.endswith('/sendmessage'):
            self.sends.append(json.loads(request.content))
            if self.send_responses:
                value=self.send_responses.pop(0)
                if isinstance(value,Exception):raise value
                if isinstance(value,int):return httpx.Response(value,json={'ret':2})
                return httpx.Response(200,json=value)
            return httpx.Response(200,json={'ret':0,'message_id':'server-audio'})
        raise AssertionError('unexpected external request '+str(request.url))

    async def send(self,adapter=None,msg=None,audio=None,**options):
        adapter=adapter or self.adapter
        with adapter.bind_reply_context(msg or message()):
            return await adapter.send_audio('peer',self.audio if audio is None else audio,**options)

    def entries(self):
        return json.loads((self.path/'audio-outbox.json').read_text())['replies']

    async def test_official_file_upload_fields_and_ciphertext_round_trip(self):
        text_state=copy.deepcopy(self.adapter._delivery)
        self.assertEqual(await self.send(),'server-audio')
        upload_request=next(r for r in self.requests if r.url.path.endswith('/getuploadurl'))
        info=json.loads(upload_request.content)
        self.assertEqual(info['media_type'],3);self.assertTrue(info['no_need_thumb'])
        self.assertEqual(info['rawsize'],len(self.audio));self.assertEqual(info['rawfilemd5'],hashlib.md5(self.audio).hexdigest())
        self.assertEqual(info['filesize'],(len(self.audio)//16+1)*16)
        self.assertEqual(len(bytes.fromhex(info['aeskey'])),16)
        upload=self.uploads[0];self.assertEqual(upload.method,'POST')
        self.assertEqual(upload.headers['content-type'],'application/octet-stream')
        self.assertNotIn('authorization',upload.headers)
        self.assertEqual(upload.url.params['encrypted_query_param'],'param + /? synthetic')
        self.assertEqual(upload.url.params['filekey'],info['filekey'])
        decryptor=Cipher(algorithms.AES(bytes.fromhex(info['aeskey'])),modes.ECB()).decryptor()
        padded=decryptor.update(upload.content)+decryptor.finalize();unpadder=padding.PKCS7(128).unpadder()
        self.assertEqual(unpadder.update(padded)+unpadder.finalize(),self.audio)
        sent=self.sends[0]['msg'];self.assertEqual(sent['context_token'],'context-original')
        self.assertEqual(sent['message_type'],2);self.assertEqual(sent['message_state'],2)
        self.assertEqual(len(sent['item_list']),1);item=sent['item_list'][0]
        self.assertEqual(item['type'],4);self.assertNotIn('voice_item',item)
        file=item['file_item'];self.assertEqual(file['file_name'],'弗洛洛-合成语音.wav');self.assertEqual(file['len'],str(len(self.audio)))
        self.assertEqual(base64.b64decode(file['media']['aes_key']).decode('ascii'),info['aeskey'])
        self.assertEqual(file['media']['encrypt_query_param'],'download-synthetic');self.assertEqual(file['media']['encrypt_type'],1)
        self.assertEqual(self.adapter._delivery,text_state)
        self.assertEqual(self.adapter.delivery_status()['audio']['accepted'],1)
        self.assertEqual(self.adapter.delivery_status()['pending_replies'],0)
        self.assertFalse(list((self.path/'voice-outbox').iterdir()),'accepted audio cache is removed')

    async def test_upload_full_url_has_precedence(self):
        self.full_url=True;await self.send()
        self.assertEqual(str(self.uploads[0].url),'https://cdn.fixture.invalid/full-upload?token=synthetic')

    async def test_retry_reuses_uploaded_item_id_context_and_filename(self):
        self.send_responses=[503,{'ret':0,'message_id':'accepted'}]
        with self.assertRaises(httpx.HTTPStatusError):await self.send()
        first=copy.deepcopy(self.sends[0]);files=list((self.path/'voice-outbox').iterdir());self.assertEqual(len(files),1)
        self.adapter._ctx_tokens['peer']='new-context-that-must-not-replace-draft'
        await self.send(msg=message(token='new-bound-context'),audio=b'not a replacement WAV',filename='other.mp3',mime_type='audio/mpeg')
        self.assertEqual(self.sends[1],first);self.assertEqual(len(self.uploads),1)
        self.assertFalse(files[0].exists())

    async def test_restart_loads_same_pending_media_and_does_not_upload_or_regenerate(self):
        self.send_responses=[503,{'ret':0}]
        with self.assertRaises(httpx.HTTPStatusError):await self.send()
        first=copy.deepcopy(self.sends[0]);other=await self.make_adapter()
        await self.send(adapter=other,audio=b'new bytes ignored because retry is immutable')
        self.assertEqual(self.sends[1],first);self.assertEqual(len(self.uploads),1)
        self.assertEqual(next(iter(self.entries().values()))['status'],'api_accepted')

    async def test_accepted_id_survives_restart_and_sends_nothing_twice(self):
        await self.send();before=len(self.requests);other=await self.make_adapter()
        self.assertEqual(await self.send(adapter=other,audio=b''),'server-audio')
        self.assertEqual(len(self.requests),before)

    async def test_upload_failure_retries_same_ciphertext_filekey_and_key(self):
        self.upload_status=503
        with self.assertRaisesRegex(RuntimeError,'CDN upload rejected'):await self.send()
        first=self.uploads[0].content;entry=next(iter(self.entries().values()));client=entry['client_id']
        self.upload_status=200;await self.send(audio=b'new audio must not replace old')
        self.assertEqual(self.uploads[1].content,first)
        self.assertEqual(self.sends[0]['msg']['client_id'],client)
        urls=[json.loads(r.content) for r in self.requests if r.url.path.endswith('/getuploadurl')]
        self.assertEqual(urls[0],urls[1])

    async def test_missing_cdn_header_does_not_submit_a_message(self):
        self.upload_header=False
        with self.assertRaisesRegex(RuntimeError,'missing download parameter'):await self.send()
        self.assertFalse(self.sends);self.assertEqual(self.adapter._delivery['failed'],0)
        self.assertEqual(next(iter(self.entries().values()))['status'],'pending')

    async def test_empty_acknowledgement_stays_unconfirmed(self):
        self.send_responses=[{}]
        with self.assertRaisesRegex(RuntimeError,'missing acknowledgement'):await self.send()
        self.assertNotEqual(next(iter(self.entries().values()))['status'],'api_accepted')
        self.assertTrue(list((self.path/'voice-outbox').iterdir()))

    async def test_failed_audio_does_not_change_a_delivered_text_or_resend_it(self):
        with self.adapter.bind_reply_context(message()):await self.adapter.send_text('peer','主文字已发。')
        text=copy.deepcopy(self.adapter._outbox);state=copy.deepcopy(self.adapter._delivery)
        self.send_responses=[503]
        with self.assertRaises(httpx.HTTPStatusError):await self.send()
        self.assertEqual(self.adapter._outbox,text);self.assertEqual(self.adapter._delivery,state)
        self.assertEqual(len(self.sends),2);self.assertEqual(self.sends[0]['msg']['item_list'][0]['type'],1)
        self.assertEqual(self.adapter.delivery_status()['pending_replies'],0)
        self.assertEqual(self.adapter.delivery_status()['audio']['failed'],1)

    async def test_missing_context_never_uploads(self):
        self.adapter._ctx_tokens.clear()
        with self.assertRaisesRegex(RuntimeError,'missing context_token'):
            await self.adapter.send_audio('peer',self.audio)
        self.assertFalse(self.requests)

    async def test_mismatched_bound_conversation_is_not_sent(self):
        with self.adapter.bind_reply_context(message(peer='different')):
            with self.assertRaisesRegex(ValueError,'conversation mismatch'):await self.adapter.send_audio('peer',self.audio)
        self.assertFalse(self.requests)

    async def test_invalid_audio_inputs_are_rejected_before_network(self):
        examples=[(b'','a.wav','audio/wav'),(b'RIFF'+b'x'*(MAX_AUDIO_BYTES+1),'a.wav','audio/wav'),
                  (self.audio,'a.exe','audio/wav'),(self.audio,'a.mp3','audio/mpeg'),
                  (self.audio,'a.wav','text/html'),(self.audio,'bad\n.wav','audio/wav')]
        for audio,name,mime in examples:
            with self.subTest(name=name,mime=mime,length=len(audio)):
                with self.assertRaises(ValueError):await self.send(msg=message(name),audio=audio,filename=name,mime_type=mime)
        self.assertFalse(self.requests)

    async def test_cached_file_corruption_is_not_uploaded(self):
        self.upload_status=503
        with self.assertRaises(RuntimeError):await self.send()
        cache=next((self.path/'voice-outbox').iterdir());cache.write_bytes(b'corrupted')
        before=len(self.requests)
        with self.assertRaisesRegex(RuntimeError,'integrity'):await self.send()
        self.assertEqual(len(self.requests),before)

    async def test_pending_blob_is_durable_before_first_upload_request(self):
        previous=self.respond
        def handler(request):
            if request.url.path.endswith('/getuploadurl'):
                entry=next(iter(self.entries().values()))
                self.assertEqual((self.path/'voice-outbox'/entry['source_file']).read_bytes(),self.audio)
                self.assertTrue(entry['client_id']);self.assertTrue(entry['context_token'])
            return previous(request)
        await self.adapter._client.aclose();self.adapter._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.send()

    async def test_queue_save_failure_never_uploads(self):
        with patch.object(self.adapter,'_save_audio_outbox',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):await self.send()
        self.assertFalse(self.requests)
        await self.send();self.assertEqual(len(self.uploads),1)

    async def test_new_input_during_upload_cancels_before_api_submission(self):
        def current():
            if self.uploads:raise ConversationChanged('new input')
        with self.assertRaises(ConversationChanged):await self.send(check_current=current)
        self.assertFalse(self.sends);self.assertEqual(next(iter(self.entries().values()))['status'],'cancelled')
        self.assertFalse(list((self.path/'voice-outbox').iterdir()))
        with self.assertRaises(ConversationChanged):await self.send()

    async def test_after_submission_retry_reuses_original_even_if_current_check_would_fail(self):
        self.send_responses=[503,{'ret':0}]
        with self.assertRaises(httpx.HTTPStatusError):await self.send()
        def changed():raise AssertionError('must not replace or cancel a submitted payload')
        await self.send(check_current=changed)
        self.assertEqual(self.sends[0],self.sends[1])

    async def test_manual_attempt_identifier_is_stable_without_bound_context(self):
        first=await self.adapter.send_audio('peer',self.audio,attempt_id='attempt-one')
        self.assertEqual(await self.adapter.send_audio('peer',b'',attempt_id='attempt-one'),first)
        self.assertEqual(len(self.sends),1)

    async def test_native_voice_payload_is_never_claimed_or_used_for_mp3(self):
        await self.send(audio=b'ID3synthetic-mp3',filename='音频.mp3',mime_type='audio/mpeg')
        item=self.sends[0]['msg']['item_list'][0]
        self.assertEqual(item['type'],4);self.assertEqual(item['file_item']['file_name'],'音频.mp3')
        self.assertNotIn('voice_item',item)

    async def test_network_retry_is_audio_only_and_reuses_exact_payload(self):
        self.send_responses=[httpx.ConnectTimeout('synthetic interruption'),{'ret':0}]
        async def no_wait(_):pass
        def factory(*_args,**_kwargs):return httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        with patch('src.adapters.ilink.make_client',side_effect=factory),patch('src.adapters.ilink.asyncio.sleep',new=no_wait):
            await self.send()
        self.assertEqual(self.sends[0],self.sends[1]);self.assertEqual(self.adapter._audio_delivery['network_retries'],1)
        self.assertEqual(self.adapter._delivery['network_retries'],0)

    async def test_concurrent_bound_calls_share_one_upload_and_send(self):
        first,second=await asyncio.gather(self.send(),self.send())
        self.assertEqual(first,second);self.assertEqual(len(self.uploads),1);self.assertEqual(len(self.sends),1)

    async def test_submit_callback_follows_durable_commit_and_never_precedes_upload(self):
        notified=[]
        def submitted():
            entry=next(iter(self.entries().values()))
            self.assertTrue(entry['send_started']);self.assertEqual(entry['status'],'submitting')
            self.assertEqual(len(self.uploads),1);self.assertEqual(len(self.sends),0)
            notified.append(True)
        await self.send(on_submit=submitted)
        self.assertEqual(notified,[True])

    async def test_cancelled_upload_removes_unsubmitted_cache(self):
        uploading=asyncio.Event()
        async def handler(request):
            if request.url.path.endswith('/upload'):
                uploading.set();await asyncio.Event().wait()
            return self.respond(request)
        await self.adapter._client.aclose();self.adapter._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task=asyncio.create_task(self.send(attempt_id='cancel-before-submit'))
        await asyncio.wait_for(uploading.wait(),1);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(list((self.path/'voice-outbox').iterdir()))
        entry=next(iter(self.entries().values()));self.assertEqual(entry['status'],'cancelled');self.assertFalse(entry['send_started'])
        self.assertEqual(self.adapter._delivery['failed'],0)

    async def test_cancelled_submission_preserves_original_payload_and_cache(self):
        submitting=asyncio.Event()
        async def handler(request):
            if request.url.path.endswith('/sendmessage'):
                submitting.set();await asyncio.Event().wait()
            return self.respond(request)
        await self.adapter._client.aclose();self.adapter._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task=asyncio.create_task(self.send(attempt_id='cancel-after-submit'))
        await asyncio.wait_for(submitting.wait(),1);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(list((self.path/'voice-outbox').iterdir()))
        entry=next(iter(self.entries().values()));self.assertTrue(entry['send_started'])
        self.assertFalse(await self.adapter.discard_audio('cancel-after-submit'))
        await self.adapter._client.aclose();self.adapter._client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        await self.send(audio=b'different bytes ignored',attempt_id='cancel-after-submit')
        self.assertEqual(self.sends[0]['msg']['client_id'],entry['client_id'])

    async def test_explicit_discard_only_cancels_pre_submission_attempt(self):
        self.upload_status=503
        with self.assertRaises(RuntimeError):await self.send(attempt_id='pre-attempt')
        self.assertTrue(await self.adapter.discard_audio('pre-attempt'))
        self.assertFalse(list((self.path/'voice-outbox').iterdir()))
        self.assertFalse(await self.adapter.discard_audio('pre-attempt'))
        self.assertFalse(await self.adapter.discard_audio('unknown'))
        self.assertFalse(await self.adapter.discard_audio(''))

    async def test_pending_queue_honors_total_byte_cap_not_just_item_count(self):
        with patch('src.adapters.ilink.MAX_AUDIO_CACHE_BYTES',len(self.audio)-1):
            with self.assertRaisesRegex(RuntimeError,'queue full'):await self.send()
        self.assertFalse(self.requests)
        self.assertFalse((self.path/'voice-outbox').exists())

    async def test_failure_to_persist_submit_barrier_does_not_claim_network_submission(self):
        save=self.adapter._save_audio_outbox
        def barrier():
            if any(d.get('send_started') for d in self.adapter._audio_outbox.values()):
                raise OSError('synthetic barrier failure')
            save()
        with patch.object(self.adapter,'_save_audio_outbox',side_effect=barrier):
            with self.assertRaises(OSError):await self.send(attempt_id='barrier-failure')
        self.assertFalse(self.sends)
        self.assertFalse(next(iter(self.adapter._audio_outbox.values()))['send_started'])
        self.assertTrue(await self.adapter.discard_audio('barrier-failure'))

    async def test_ack_save_failure_can_be_retried_without_resending_and_is_persisted(self):
        save=self.adapter._save_audio_outbox
        def only_ack():
            if any(d.get('status')=='api_accepted' for d in self.adapter._audio_outbox.values()):
                raise OSError('synthetic ack save failure')
            save()
        with patch.object(self.adapter,'_save_audio_outbox',side_effect=only_ack):
            with self.assertRaises(OSError):await self.send()
        self.assertEqual(len(self.sends),1)
        self.assertTrue(list((self.path/'voice-outbox').iterdir()))
        await self.send(audio=b'')
        self.assertEqual(len(self.sends),1);self.assertFalse(list((self.path/'voice-outbox').iterdir()))
        self.assertEqual(next(iter(self.entries().values()))['status'],'api_accepted')

    async def test_persona_change_cancels_before_optional_audio_upload(self):
        self.adapter.active_space=lambda:'new-space'
        old=message();old.persona_space='old-space'
        with self.assertRaises(ConversationChanged):await self.send(msg=old)
        self.assertFalse(self.requests)

    async def test_linked_cache_directory_is_not_used_for_audio_files(self):
        outside=self.path/'outside';outside.mkdir()
        (self.path/'voice-outbox').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError,'directory link'):await self.send()
        self.assertFalse(list(outside.iterdir()));self.assertFalse(self.requests)

    async def test_manual_attempts_are_isolated_when_the_bot_account_changes(self):
        await self.adapter.send_audio('peer',self.audio,attempt_id='same-attempt')
        first=self.sends[0]['msg']['client_id']
        self.adapter.bot_id='another@im.bot';self.adapter._ctx_tokens['peer']='another-account-context'
        await self.adapter.send_audio('peer',self.audio,attempt_id='same-attempt')
        self.assertEqual(len(self.sends),2)
        self.assertNotEqual(self.sends[1]['msg']['client_id'],first)
        self.assertEqual(self.sends[1]['msg']['context_token'],'another-account-context')

    async def test_discard_remains_atomic_when_old_receipts_are_pruned(self):
        self.upload_status=503
        with self.assertRaises(RuntimeError):await self.send(attempt_id='prune-case')
        for index in range(128):
            self.adapter._audio_outbox['old-'+str(index)]={'status':'api_accepted',
                'source_file':'wxbot-audio-'+format(index,'032x')+'.wav','send_started':True}
        self.assertTrue(await self.adapter.discard_audio('prune-case'))
        self.assertEqual(len(self.entries()),128)
        self.assertFalse(list((self.path/'voice-outbox').iterdir()))
