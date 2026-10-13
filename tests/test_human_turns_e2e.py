"""Ordered real image bytes, async turn boundaries and native transport payloads."""
import asyncio,base64,io,json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
import httpx
from openai import AsyncOpenAI
from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7
from src.adapters.onebot import OneBotAdapter
from src.adapters.ilink import ILinkAdapter
from src.core.types import WxMessage
from src.core.turn_taking import plan_wait,is_done
from src.core.conversation import merge_messages,MessageBuffer,ConversationChanged,message_parts
from src.core.media import image_data_url,ImageLoader,MediaError
from src.core.llm import LLMGateway,ProviderCfg
from src.core.context import ContextManager
from src.core.sender import HumanLikeSender
from src.core.trigger import TriggerRouter,TriggerDecision
from src.orchestrator import Orchestrator
from src.store_memory import MemoryStore


def png(color='red',mode='RGB'):
    out=io.BytesIO();Image.new(mode,(12,12),color).save(out,format='PNG');return out.getvalue()
def message(text='你好',mid='1',images=None,parts=None):
    return WxMessage(mid,'onebot','private','p_123','123','测试用户','999',text,images=images or [],content_parts=parts or [])
def event(mid,segments):return {'post_type':'message','message_type':'private','user_id':123,'self_id':999,'message_id':mid,'message':segments}
def raw(mid,items,token='token1'):return {'message_type':1,'message_id':mid,'from_user_id':'wx_fixture','context_token':token,'item_list':items}
def image_seg(color='red'):return {'type':'image','data':{'file':'base64://'+base64.b64encode(png(color)).decode()}}
def text_seg(text):return {'type':'text','data':{'text':text}}
async def eventually(predicate,timeout=2):
    end=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>end:raise AssertionError('fixture condition timed out')
        await asyncio.sleep(.005)
class Chat:
    async def stream(self,*a,**k):yield '完整回复。'
class Sender:
    def __init__(self):self.sent=[]
    async def send(self,chat,text,**k):self.sent.append(text)
    async def stream_send(self,chat,stream,**k):reply=''.join([p async for p in stream]);await self.send(chat,reply);return reply
class Adapter:
    platform='onebot'
    def __init__(self):self.typing=[];self.on_event=asyncio.Event()
    def on_message(self,h):self.handler=h
    async def send_typing(self,c,on):self.typing.append(on);self.on_event.set();return True

def orch(adapter,chat=None,sender=None,router=None,typing=True):
    store=MemoryStore();return Orchestrator(adapter,store,chat or Chat(),None,ContextManager(store),router or TriggerRouter(),sender or Sender(),typing_indicator=typing)

class OrderedImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_ilink_keeps_two_images_and_interleaved_captions(self):
        with tempfile.TemporaryDirectory() as d:
            a=ILinkAdapter(d)
            try:
                m=a._normalize(raw('1',[{'type':2,'image_item':{'media':{'full_url':'https://example.com/1'}}},{'type':1,'text_item':{'text':'第一张说明'}},{'type':2,'image_item':{'media':{'full_url':'https://example.com/2'}}},{'type':1,'text_item':{'text':'第二张说明'}}]))
                self.assertEqual([p['type'] for p in m.content_parts],['image','text','image','text']);self.assertEqual(m.content_parts[2]['index'],1)
            finally:await a._client.aclose()
    async def test_onebot_array_and_cq_have_same_order(self):
        a=OneBotAdapter('http://gateway',bot_qq='999')
        try:
            array=a._normalize(event('1',[{'type':'image','data':{'url':'https://example.com/1'}},text_seg('图一'),{'type':'image','data':{'url':'https://example.com/2'}},text_seg('图二')]))
            cq=a._normalize(event('2','[CQ:image,url=https://example.com/1]图一[CQ:image,url=https://example.com/2]图二'))
            self.assertEqual(array.content_parts,cq.content_parts);self.assertEqual(cq.content,'[图片]图一[图片]图二')
        finally:await a._client.aclose()
    async def test_cq_escaped_url_and_at_preserved(self):
        a=OneBotAdapter('http://gateway',bot_qq='999')
        try:
            m=a._normalize(event('1','[CQ:at,qq=999][CQ:image,url=https://example.com/i?a=1&amp;b=2]说明'))
            self.assertTrue(m.is_at_me);self.assertEqual(m.images[0]['url'],'https://example.com/i?a=1&b=2');self.assertEqual(m.content_parts[0]['type'],'image')
        finally:await a._client.aclose()
    async def test_merge_offsets_image_indices_and_preserves_captions(self):
        a=message('[图片]第一张',images=[{'url':'a'}]);b=message('第二张[图片]','2',images=[{'url':'b'}]);m=merge_messages([a,b])
        self.assertEqual([p['index'] for p in m.content_parts if p['type']=='image'],[0,1]);self.assertEqual(len(m.batch_messages),2)
    async def test_context_delivers_correct_interleaved_multimodal_parts(self):
        m=merge_messages([message('[图片]第一张',images=[{}]),message('[图片]第二张','2',images=[{}])]);inputs=[{'type':'image_url','image_url':{'url':'image1'}},{'type':'image_url','image_url':{'url':'image2'}}]
        data=(await ContextManager(MemoryStore()).build(m,image_inputs=inputs))[-1]['content'];sequence=[p['image_url']['url'] if p['type']=='image_url' else p['text'] for p in data]
        self.assertLess(sequence.index('image1'),sequence.index('第一张'));self.assertLess(sequence.index('第一张'),sequence.index('image2'));self.assertLess(sequence.index('image2'),sequence.index('第二张'))
    async def test_legacy_pending_envelope_without_parts_reconstructs(self):
        m=message('[图片]说明',images=[{}]);self.assertEqual(message_parts(m),[{'type':'image','index':0},{'type':'text','text':'说明'}])
    async def test_png_stays_lossless_and_transparent_background_white(self):
        url=image_data_url(png((0,0,0,0),'RGBA'));self.assertTrue(url.startswith('data:image/png;'))
        pixels=Image.open(io.BytesIO(base64.b64decode(url.split(',')[1])));self.assertEqual(pixels.getpixel((0,0)),(255,255,255))
    async def test_image_failure_cancels_sibling_downloads(self):
        started=asyncio.Event();cancelled=asyncio.Event();loader=ImageLoader()
        async def fetch(ref):
            if ref['id']==1:await started.wait();raise MediaError('fixture')
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        with patch.object(loader,'fetch',side_effect=fetch):
            with self.assertRaises(MediaError):await loader.load([{'id':1},{'id':2}])
        self.assertTrue(cancelled.is_set())
    async def test_qq_lookup_failure_is_media_error_not_model_failure(self):
        a=OneBotAdapter('http://gateway')
        try:
            with patch.object(a,'_call',side_effect=RuntimeError('fixture')):
                with self.assertRaises(MediaError):await a.resolve_images([{'file_id':'fixture'}])
        finally:await a._client.aclose()

class NaturalTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_pause_survives_multiple_continuations(self):
        items=[(message('等我说完'),10),(message('第一段','2'),11),(message('第二段','3'),12)]
        p=plan_wait(items,2,10,pause_timeout=120);self.assertTrue(p.hold);self.assertEqual(p.deadline,130)
    async def test_original_pause_deadline_does_not_slide_forever(self):
        p=plan_wait([(message('等我说完'),10),(message('再补充','2'),129)],2,10,pause_timeout=120);self.assertEqual(p.deadline,130)
    async def test_done_closing_sentence_and_quoted_phrase(self):
        self.assertTrue(is_done('这张是昨天拍的。说完了。'));self.assertFalse(is_done('他说“说完了”是什么意思？'));self.assertFalse(is_done('说完了。再补充一条'))
    async def test_done_releases_pause_before_hard_deadline(self):
        p=plan_wait([(message('等我说完'),10),(message('我的说明。说完了。','2'),12)],2,10,pause_timeout=120);self.assertFalse(p.hold);self.assertLess(p.delay,.4)
    async def test_queue_does_not_answer_partial_text_during_explicit_pause(self):
        calls=[]
        async def callback(m):calls.append(m)
        b=MessageBuffer(callback);b.configure(.02,.06,human=True,pause_timeout=.6);b.start()
        try:
            b.submit(message('等我说完'));b.submit(message('第一段','2'));await asyncio.sleep(.11);self.assertEqual(calls,[])
            b.submit(message('说完了','3'));await eventually(lambda:bool(calls));self.assertEqual(len(calls),1);self.assertEqual(len(calls[0].batch_messages),3)
        finally:await b.close()
    async def test_pause_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            calls=[]
            async def callback(m):calls.append(m)
            path=Path(d)/'pending.json';b=MessageBuffer(callback);b.configure(.02,.06,path,human=True,pause_timeout=.6);b.start();b.submit(message('等我说完'));b.submit(message('第一段','2'));await b.close()
            c=MessageBuffer(callback);c.configure(.02,.06,path,human=True,pause_timeout=.6);c.start()
            try:await asyncio.sleep(.1);self.assertEqual(calls,[]);c.submit(message('说完了','3'));await eventually(lambda:bool(calls))
            finally:await c.close()
    async def test_media_work_cancels_when_user_continues(self):
        a=Adapter();changed=False;entered=asyncio.Event();cancelled=asyncio.Event()
        def check(m):
            if changed:raise ConversationChanged('fixture new input')
        a.check_reply_current=check;o=orch(a)
        async def media():
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        task=asyncio.create_task(o._current_work(message(),media()));await entered.wait();changed=True
        with self.assertRaises(ConversationChanged):await asyncio.wait_for(task,.2)
        self.assertTrue(cancelled.is_set());await o.close()
    async def test_cancelled_image_generation_sends_no_error_placeholder(self):
        a=Adapter();changed=False;entered=asyncio.Event()
        def check(m):
            if changed:raise ConversationChanged('fixture')
        a.check_reply_current=check;sender=Sender();o=orch(a,sender=sender)
        async def load(refs):entered.set();await asyncio.Event().wait()
        with patch.object(o.images,'load',side_effect=load):
            task=asyncio.create_task(o.handle(message('[图片]',images=[{'url':'fixture'}])));await entered.wait();changed=True
            with self.assertRaises(ConversationChanged):await asyncio.wait_for(task,.2)
        self.assertEqual(sender.sent,[]);await o.close()
    async def test_peer_typing_notice_extends_silence_without_fake_messages(self):
        calls=[]
        async def callback(m):calls.append(m)
        b=MessageBuffer(callback);b.configure(.02,.5,human=False);b.start();b.submit(message())
        try:
            self.assertTrue(b.note_typing('p_123','123'));await asyncio.sleep(.09);self.assertEqual(calls,[])
            b.note_typing('p_123','123',False);await eventually(lambda:bool(calls));self.assertEqual(len(calls),1)
        finally:await b.close()
    async def test_peer_typing_admission_is_bounded_by_max_wait(self):
        calls=[]
        async def callback(m):calls.append(m)
        b=MessageBuffer(callback);b.configure(.02,.06,human=False);b.start();b.submit(message());b.note_typing('p_123','123')
        try:await eventually(lambda:bool(calls),.3);self.assertEqual(len(calls),1)
        finally:await b.close()
    async def test_input_status_for_another_user_does_not_block(self):
        b=MessageBuffer(None);b.submit(message());self.assertFalse(b.note_typing('p_456','456'));self.assertEqual(b.typing_hints,{})
    async def test_onebot_notice_updates_only_its_existing_turn(self):
        a=OneBotAdapter('http://gateway',bot_qq='999');a._bursts.submit(message())
        try:
            await a.handle_event({'post_type':'notice','notice_type':'notify','sub_type':'input_status','user_id':123,'group_id':0,'event_type':1})
            self.assertIn('ctx:onebot:private:p_123:123',a._bursts.typing_hints)
        finally:await a._client.aclose()
    async def test_typing_begins_only_after_admitted_reply_not_ignored_input(self):
        a=Adapter()
        class Router:
            async def decide(self,m):await asyncio.sleep(.5);return TriggerDecision(False,'skip','fixture')
        o=orch(a,router=Router());await o.handle(message());self.assertEqual(a.typing,[]);await o.close()
    async def test_typing_stops_on_cancel(self):
        a=Adapter()
        class Slow:
            async def stream(self,*a,**k):await asyncio.Event().wait();yield 'never'
        o=orch(a,chat=Slow());task=asyncio.create_task(o.handle(message()));await asyncio.wait_for(a.on_event.wait(),1);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(a.typing,[True,False]);await o.close()
    async def test_disabled_typing_has_no_auxiliary_requests(self):
        a=Adapter()
        class Slow:
            async def stream(self,*a,**k):await asyncio.sleep(.5);yield '完成'
        o=orch(a,chat=Slow(),typing=False);await o.handle(message());self.assertEqual(a.typing,[]);await o.close()
    async def test_qq_typing_private_only_and_unsupported_backoff(self):
        a=OneBotAdapter('http://gateway')
        try:
            with patch.object(a,'_call',return_value={'status':'ok','retcode':0}) as call:
                self.assertTrue(await a.send_typing('p_123'));self.assertFalse(await a.send_typing('g_456'));call.assert_awaited_once_with('set_input_status',{'user_id':'123','event_type':1})
            with patch.object(a,'_call',side_effect=RuntimeError('unsupported')) as call:
                self.assertFalse(await a.send_typing('p_123'));self.assertFalse(await a.send_typing('p_123'));self.assertEqual(call.await_count,1)
        finally:await a._client.aclose()

class ProtocolPipelineTests(unittest.IsolatedAsyncioTestCase):
    def llm(self,requests):
        def model(request):
            body=json.loads(request.content);requests.append(body)
            chunks=[{'choices':[{'index':0,'delta':{'content':piece}}]} for piece in ('第一张是红色。\n','第二张是蓝色。')]
            sse=''.join('data: '+json.dumps(c,ensure_ascii=False)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
            return httpx.Response(200,headers={'content-type':'text/event-stream'},text=sse)
        sdk=AsyncOpenAI(api_key='fixture-key',base_url='http://model/v1',http_client=httpx.AsyncClient(transport=httpx.MockTransport(model)))
        g=LLMGateway(ProviderCfg('primary','http://model/v1','fixture-key','vision-fixture'));g._client=lambda c:sdk
        return g,sdk
    async def test_qq_three_bursts_two_actual_images_one_reply(self):
        requests=[];sent=[];g,sdk=self.llm(requests);a=OneBotAdapter('http://gateway',bot_qq='999')
        def transport(request):
            body=json.loads(request.content)
            if request.url.path=='/send_private_msg':sent.append(body)
            return httpx.Response(200,json={'status':'ok','retcode':0,'data':{'message_id':100}})
        await a._client.aclose();a._client=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://gateway')
        store=MemoryStore();o=Orchestrator(a,store,g,None,ContextManager(store),TriggerRouter(),HumanLikeSender(a));a.configure_burst(.04,1,human=False);a._bursts.start()
        try:
            await a.handle_event(event('1',[image_seg('red')]));await a.handle_event(event('2',[text_seg('第一张是红色，对吗？'),image_seg('blue')]));await a.handle_event(event('3',[text_seg('第二张是什么颜色？')]))
            await eventually(lambda:len(store._archive)>=4)
            self.assertEqual(len(requests),1);self.assertEqual(len(sent),1)
            parts=requests[0]['messages'][-1]['content'];images=[p for p in parts if p['type']=='image_url'];self.assertEqual(len(images),2)
            for part,color in zip(images,[(255,0,0),(0,0,255)]):
                data=base64.b64decode(part['image_url']['url'].split(',')[1]);self.assertEqual(Image.open(io.BytesIO(data)).getpixel((0,0)),color)
            captions=[p.get('text','') for p in parts];self.assertLess(captions.index('第一张是红色，对吗？'),next(i for i,p in enumerate(parts) if p is images[1]))
            self.assertNotIn('\n',sent[0]['message'][0]['data']['text']);self.assertEqual(len(await store.get_recent('ctx:onebot:private:p_123')),1)
        finally:await a.stop();await o.close();await sdk.close()
    async def test_ilink_encrypted_picture_caption_latest_token_one_reply(self):
        with tempfile.TemporaryDirectory() as d:
            requests=[];sent=[];g,sdk=self.llm(requests);a=ILinkAdapter(d,base_url='http://media');a.bot_id='bot@im.bot';a.configure_burst(.03,1,human=False)
            key=b'0123456789abcdef';pad=PKCS7(128).padder();padded=pad.update(png())+pad.finalize();cipher=Cipher(algorithms.AES(key),modes.ECB()).encryptor();encrypted=cipher.update(padded)+cipher.finalize()
            image={'type':2,'image_item':{'aeskey':key.hex(),'media':{'full_url':'http://media/picture'}}}
            a._pending=[raw('1',[image],token='old'),raw('2',[{'type':1,'text_item':{'text':'请一起看看这张照片。'}}],token='latest')]
            async def post(endpoint,body):
                if endpoint=='ilink/bot/sendmessage':sent.append(body)
                return {'ret':0,'message_id':'out-fixture'}
            a._post=post;store=MemoryStore();o=Orchestrator(a,store,g,None,ContextManager(store),TriggerRouter(),HumanLikeSender(a));o.images=ImageLoader(['http://media'],httpx.MockTransport(lambda r:httpx.Response(200,content=encrypted)))
            try:
                self.assertTrue(await a._handle_raw(a._pending[0]));self.assertEqual(a._pending,[]);self.assertEqual(len(sent),1);self.assertEqual(sent[0]['msg']['context_token'],'latest')
                parts=requests[0]['messages'][-1]['content'];image_part=next(p for p in parts if p['type']=='image_url');data=base64.b64decode(image_part['image_url']['url'].split(',')[1]);self.assertEqual(Image.open(io.BytesIO(data)).getpixel((0,0)),(255,0,0));self.assertEqual(len(store._archive),3)
            finally:await a._client.aclose();await o.close();await sdk.close()

if __name__=='__main__':unittest.main()
