"""Image protocol and human turn-taking fixtures; never fetch real user media."""
import asyncio, base64, io, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
import httpx
from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7
from src.core.types import WxMessage
from src.core.media import aes_key,decrypt_image,image_data_url,ImageLoader,MediaError
from src.core.turn_taking import plan_wait,ready
from src.core.conversation import MessageBuffer,merge_messages,ConversationChanged,members
from src.core.llm import LLMGateway,ProviderCfg

def msg(text='你好',mid='1',uid='u',images=None):return WxMessage(mid,'onebot','private',uid,uid,uid,'bot',text,images=images or [])
def png():
    out=io.BytesIO();Image.new('RGB',(20,20),'red').save(out,format='PNG');return out.getvalue()

class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_official_key_encodings(self):
        key=b'0123456789abcdef'
        for ref in [{'key_hex':key.hex()},{'aes_key':base64.b64encode(key).decode()},{'aes_key':base64.b64encode(key.hex().encode()).decode()}]:self.assertEqual(aes_key(ref),key)
    async def test_hex_key_priority(self):self.assertEqual(aes_key({'key_hex':(b'1'*16).hex(),'aes_key':'invalid'}),b'1'*16)
    async def test_invalid_key_rejected(self):
        for ref in [{'key_hex':'invalid'},{'aes_key':'abc'}]:
            with self.assertRaises(MediaError):aes_key(ref)
    async def test_aes_ecb_pkcs7_roundtrip(self):
        key=b'0123456789abcdef';pad=PKCS7(128).padder();data=pad.update(png())+pad.finalize();cipher=Cipher(algorithms.AES(key),modes.ECB()).encryptor();encrypted=cipher.update(data)+cipher.finalize()
        self.assertEqual(decrypt_image(encrypted,{'key_hex':key.hex()}),png())
    async def test_corrupt_image_is_not_fabricated(self):
        with self.assertRaises(MediaError):image_data_url(b'not an image')
    async def test_inline_png_becomes_real_model_image(self):
        data=await ImageLoader().load([{'inline':base64.b64encode(png()).decode()}]);self.assertTrue(data[0]['image_url']['url'].startswith(('data:image/jpeg;base64,','data:image/png;base64,')))
    async def test_private_address_not_implicitly_trusted(self):
        with self.assertRaises(MediaError):await ImageLoader().validate_url('http://127.0.0.1/private')
    async def test_configured_gateway_origin_accepted(self):await ImageLoader(['http://127.0.0.1:3000']).validate_url('http://127.0.0.1:3000/media.png')
    async def test_image_count_limit(self):
        with self.assertRaises(MediaError):await ImageLoader().load([{}]*7)
    async def test_gateway_download_keeps_input_order(self):
        urls=[]
        def fake(request):urls.append(str(request.url));return httpx.Response(200,content=png())
        data=await ImageLoader(['http://127.0.0.1:3000'],httpx.MockTransport(fake)).load([{'url':'http://127.0.0.1:3000/1'},{'url':'http://127.0.0.1:3000/2'}]);self.assertEqual(len(data),2);self.assertEqual(len(urls),2)
    async def test_redirect_to_other_private_host_blocked(self):
        def fake(request):return httpx.Response(302,headers={'location':'http://127.0.0.2/private'})
        with self.assertRaises(MediaError):await ImageLoader(['http://127.0.0.1:3000'],httpx.MockTransport(fake)).load([{'url':'http://127.0.0.1:3000/media'}])
    async def test_vision_routing_no_text_fallback(self):
        primary=ProviderCfg('primary','http://p','fake','p');fallback=ProviderCfg('fallback','http://f','fake','f');vision=ProviderCfg('vision','http://v','fake','v');g=LLMGateway(primary,fallback,vision=vision);calls=[]
        async def fail(cfg,*a):calls.append(cfg.name);raise RuntimeError('fixture')
        with patch.object(g,'_call',side_effect=fail):
            with self.assertRaises(RuntimeError):await g.chat([{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,fixture'}}]}])
        self.assertEqual(calls,['vision'])

class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_pause_and_done(self):
        p=plan_wait([(msg('等我说完'),10)],2,10);self.assertTrue(p.hold);self.assertEqual(p.deadline,130)
        done=plan_wait([(msg('等我说完'),10),(msg('说完了','2'),12)],2,10);self.assertLess(done.delay,.4);self.assertFalse(done.hold)
    async def test_quoted_pause_does_not_hold(self):self.assertFalse(plan_wait([(msg('他说等一下是什么意思'),10)],2,10).hold)
    async def test_incomplete_sentence_waits_longer(self):self.assertGreater(plan_wait([(msg('然后'),10)],2,10).delay,plan_wait([(msg('就是这样。'),10)],2,10).delay)
    async def test_picture_waits_for_caption(self):self.assertGreater(plan_wait([(msg('[图片]',images=[{}]),10)],2,10).delay,3)
    async def test_max_wait_is_bounded(self):self.assertEqual(ready(plan_wait([(msg('然后'),10)],2,10),20,10,1),(True,True))
    async def test_batch_preserves_originals_and_latest_token(self):
        a=msg('第一条');a.context_token='old';b=msg('第二条','2');b.context_token='new';m=merge_messages([a,b]);self.assertEqual(m.context_token,'new');self.assertEqual([x.content for x in members(m)],['第一条','第二条'])
    async def test_quiet_window_resets_and_returns_one_batch(self):
        calls=[]
        async def callback(m):calls.append(m)
        b=MessageBuffer(callback);b.configure(.06,1,human=False);b.start();b.submit(msg('第一条'));await asyncio.sleep(.035);b.submit(msg('第二条','2'));await asyncio.sleep(.045);self.assertEqual(calls,[]);await asyncio.sleep(.06);self.assertEqual(len(calls),1);self.assertIn('第二条',calls[0].content);await b.close()
    async def test_new_input_invalidates_blocked_generation(self):
        entered=asyncio.Event();gate=asyncio.Event();changes=[]
        async def callback(m):
            if len(members(m))==1:
                entered.set();await gate.wait();b.check(m)
            else:changes.append(m)
        b=MessageBuffer(callback);b.configure(.02,1,human=False);b.start();b.submit(msg('第一条'));await entered.wait();b.submit(msg('第二条','2'));gate.set();await asyncio.sleep(.1);self.assertEqual(len(changes),1);self.assertEqual(len(members(changes[0])),2);await b.close()
    async def test_pending_messages_survive_close_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'pending.json';calls=[]
            async def callback(m):calls.append(m)
            b=MessageBuffer(callback);b.configure(.2,1,path,human=False);b.start();b.submit(msg());await b.close()
            c=MessageBuffer(callback);c.configure(.01,1,path,human=False);c.start();await asyncio.sleep(.08);self.assertEqual(len(calls),1);await c.close()
    async def test_waiting_users_do_not_take_processing_slots(self):
        calls=[]
        async def callback(m):calls.append(m.sender_id)
        b=MessageBuffer(callback);b.configure(.02,1,human=True,pause_timeout=1);b.start()
        for i in range(5):b.submit(msg('等我说完',uid='pause'+str(i)))
        b.submit(msg('你好。',uid='ready'));await asyncio.sleep(.12);self.assertEqual(calls,['ready']);await b.close()

if __name__=='__main__':unittest.main()
