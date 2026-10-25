"""Content-first search and mixed-media regressions; synthetic bytes, no paid API."""
import asyncio,base64,io,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
import httpx
from openai import AsyncOpenAI
from PIL import Image
from src.core.web_search import parse_results,DeepSeekWebSearch
from src.core.llm import LLMGateway,ProviderCfg
from src.core.media import ImageLoader,MediaError
from src.core.context import ContextManager
from src.core.types import WxMessage
from src.core.conversation import merge_messages
from src.core.trigger import TriggerRouter
from src.core.sender import HumanLikeSender
from src.adapters.onebot import OneBotAdapter
from src.adapters.ilink import ILinkAdapter
from src.orchestrator import Orchestrator

URL='https://example.com/article?id=a%20b&utm_source=fixture'
CLEAN='https://example.com/article?id=a%20b'
TITLE='合成新闻：某地发布通行安排'
SUMMARY='合成资料：周五开始试运行，早高峰开放两条车道，需要提前预约。周末的安排尚未公布。'
FINAL='重点是周五开始试运行：早高峰开放两条车道，通行前需要预约。周末怎么安排还没公布，不能据此推断全天开放。'
def native():
 return {'content':[{'type':'text','text':'我先检索。'},{'type':'web_search_tool_result','content':[{'type':'web_search_result','url':URL,'title':TITLE}]},{'type':'text','text':SUMMARY,'citations':[{'url':URL,'cited_text':'周五试运行。'},{'url':URL,'cited_text':'需要提前预约。'}]}]}
def reply(text,tools=None):return NS(choices=[NS(message=NS(content=text,tool_calls=tools,reasoning_content=None))],usage=NS(prompt_tokens=5,completion_tokens=8))
def tool():
 data={'id':'search1','type':'function','function':{'name':'web_search','arguments':'{"query":"合成公开新闻"}'}}
 return NS(id='search1',function=NS(**data['function']),model_dump=lambda **kw:data)
def png(color='red'):
 b=io.BytesIO();Image.new('RGB',(12,12),color).save(b,format='PNG');return b.getvalue()
def ref(color='red'):return {'inline':base64.b64encode(png(color)).decode()}
def msg(images=None,parts=None):return WxMessage('m','onebot','private','p_123','123','fixture','999','[图片][视频][图片]',images=images or [],content_parts=parts or [])

class ContentParserTests(unittest.TestCase):
 def test_preserves_post_search_synthesis(self):
  r=parse_results(native());self.assertEqual(r.get('summary'),SUMMARY);self.assertNotIn('我先检索',r['summary'])
 def test_keeps_multiple_quotes_and_exact_nontracking_query(self):
  r=parse_results(native());self.assertEqual(r['sources'][0]['url'],CLEAN);self.assertIn('周五试运行。',r['sources'][0]['snippet']);self.assertIn('需要提前预约。',r['sources'][0]['snippet'])
 def test_prose_without_native_result_is_not_evidence(self):
  r=parse_results({'content':[{'type':'text','text':SUMMARY}]});self.assertFalse(r['ok']);self.assertFalse(r.get('summary'))
 def test_malformed_citation_does_not_crash(self):
  data=native();data['content'][-1]['citations']=17;self.assertTrue(parse_results(data)['ok'])
 def test_tracking_copies_do_not_multiply_results(self):
  data=native();data['content'][1]['content'].append({'type':'web_search_result','url':CLEAN+'&utm_source=other','title':TITLE});self.assertEqual(len(parse_results(data)['sources']),1)

class SearchReviewTests(unittest.IsolatedAsyncioTestCase):
 def gateway(self,answer=FINAL,draft=None,search=None):
  g=LLMGateway(ProviderCfg('primary','https://model.invalid','fixture','model'),web_search=NS(search=AsyncMock(return_value=search or parse_results(native()))))
  create=AsyncMock(side_effect=[reply('',[tool()]),reply(draft or (TITLE+' '+CLEAN)),answer if isinstance(answer,Exception) else reply(answer)])
  g._client=lambda c:NS(chat=NS(completions=NS(create=create)));return g,create
 async def test_three_stages_one_bubble_content_before_one_source(self):
  g,c=self.gateway();out=[x async for x in g.stream([{'role':'user','content':'帮我总结通行安排'}],chat_id='p')]
  self.assertEqual(len(out),1);self.assertIn('两条车道',out[0]);self.assertIn('预约',out[0]);self.assertEqual(out[0].count('http'),1);self.assertNotIn(TITLE,out[0]);self.assertEqual(c.call_count,3)
  data=c.call_args_list[1].kwargs['messages'];self.assertIn(SUMMARY,next(x['content'] for x in data if x['role']=='tool'))
  review=c.call_args_list[2].kwargs;self.assertEqual(review['tool_choice'],'none');self.assertTrue(review['tools']);self.assertIn('联网回答复核',review['messages'][-1]['content'])
 async def test_review_error_keeps_substantive_answer_not_error(self):
  g,c=self.gateway(RuntimeError('fixture-secret'),draft=FINAL);out=await g.chat([],chat_id='p');self.assertIn('预约',out);self.assertNotIn('fixture-secret',out)
 async def test_repeated_title_wall_uses_retained_synthesis(self):
  wall=TITLE+' '+CLEAN+'；'+TITLE+' '+CLEAN;g,c=self.gateway(wall,draft=wall);out=await g.chat([],chat_id='p');self.assertIn('早高峰',out);self.assertEqual(out.count('http'),1)
 async def test_titles_only_never_invent_article_detail(self):
  r={'ok':True,'summary':'','sources':[{'url':CLEAN,'title':TITLE,'snippet':''}]};g,c=self.gateway(search=r,draft='未经证实的细节');out=await g.chat([],chat_id='p');self.assertIn('标题',out);self.assertNotIn('未经证实',out);self.assertEqual(c.call_count,2)
 async def test_code_does_not_append_three_titles_even_if_missing_urls(self):
  sources=[{'url':'https://example.com/'+str(i),'title':TITLE} for i in range(3)];out=LLMGateway._with_sources(FINAL,sources);self.assertEqual(out.count('http'),1);self.assertNotIn(TITLE,out)
 async def test_review_cancellation_propagates(self):
  g,c=self.gateway();entered=asyncio.Event()
  async def blocked(*a,**k):entered.set();await asyncio.Event().wait()
  g._call=blocked;t=asyncio.create_task(g.chat([],chat_id='p'));await asyncio.wait_for(entered.wait(),1);t.cancel()
  with self.assertRaises(asyncio.CancelledError):await t
 async def test_native_search_to_review_retains_page_content(self):
  client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=native())))
  g,c=self.gateway();g.web_search=DeepSeekWebSearch('fixture-not-real')
  with patch('src.core.web_search.make_client',return_value=client):out=await g.chat([],chat_id='p')
  self.assertIn('预约',out);self.assertEqual(c.call_count,3);self.assertFalse(client.is_closed)
  await g.aclose();self.assertTrue(client.is_closed)

 async def test_code_urls_survive_source_compaction(self):
  text='可以用下面的示例调用。\n```python\nget("https://api.example.com/v1")\n```'
  self.assertIn('https://api.example.com/v1',LLMGateway._with_sources(text,[{'url':CLEAN,'title':TITLE}]))
 async def test_explicit_no_links_is_respected(self):
  g,c=self.gateway();out=await g.chat([{'role':'user','content':'只要总结，不要贴链接'}],chat_id='p');self.assertIn('预约',out);self.assertNotIn('http',out)
 async def test_requested_sources_are_not_limited_to_one(self):
  r=parse_results(native());r['sources'] += [{'url':'https://other.example.com/another','title':'另一事件','snippet':'合成资料'}]
  g,c=self.gateway(search=r);out=await g.chat([{'role':'user','content':'请提供来源链接'}],chat_id='p');self.assertEqual(out.count('http'),2)

class PartialMediaTests(unittest.IsolatedAsyncioTestCase):
 async def test_bad_image_does_not_erase_two_good_images(self):
  loader=ImageLoader();data=await loader.load_partial([ref(),{'inline':'not-base64!'},ref('blue')]);self.assertEqual([bool(x) for x in data],[True,False,True])
  parts=(await ContextManager(__import__('src.store_memory',fromlist=['MemoryStore']).MemoryStore()).build(msg([{}, {}, {}]),image_inputs=data))[-1]['content']
  self.assertIn('[图片3]',[p.get('text') for p in parts]);self.assertEqual(sum(p['type']=='image_url' for p in parts),2)
 async def test_first_six_only_preserve_overflow_placeholders(self):
  loader=ImageLoader();loader.fetch=AsyncMock(return_value=png());data=await loader.load_partial([{}]*8);self.assertEqual(loader.fetch.await_count,1);self.assertEqual(len(data),8);self.assertEqual(data[-2:],[None,None])
 async def test_resolver_failure_does_not_cancel_good_input(self):
  async def resolve(r):
   if r.get('bad'):raise MediaError('fixture')
   return r
  data=await ImageLoader().load_partial([{'bad':True},ref()],resolver=resolve);self.assertIsNone(data[0]);self.assertIsNotNone(data[1])
 async def test_timeout_keeps_finished_and_cancels_late_input(self):
  loader=ImageLoader();late=asyncio.Event();original_wait=asyncio.wait
  async def fetch(r):
   if r.get('slow'):
    try:await asyncio.Event().wait()
    finally:late.set()
   return png()
  async def short(tasks,**kwargs):return await original_wait(tasks,timeout=.03)
  loader.fetch=fetch
  with patch('src.core.media.asyncio.wait',side_effect=short):data=await loader.load_partial([{}, {'slow':True}])
  self.assertTrue(data[0]);self.assertIsNone(data[1]);self.assertTrue(late.is_set())
 async def test_cancel_cleans_child_downloads(self):
  loader=ImageLoader();entered=asyncio.Event();closed=asyncio.Event()
  async def fetch(r):
   entered.set()
   try:await asyncio.Event().wait()
   finally:closed.set()
  loader.fetch=fetch;task=asyncio.create_task(loader.load_partial([{}]));await entered.wait();task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertTrue(closed.is_set())
 async def test_both_adapters_keep_video_separate_from_images(self):
  qq=OneBotAdapter('http://gateway',bot_qq='999')
  with tempfile.TemporaryDirectory() as d:
   wx=ILinkAdapter(d)
   try:
    a=qq._normalize({'message_type':'private','user_id':123,'self_id':999,'message_id':1,'message':[{'type':'image','data':{'url':'https://example.com/i'}},{'type':'video','data':{'url':'https://example.com/v.mp4'}}]})
    b=wx._normalize({'message_type':1,'from_user_id':'u','message_id':1,'item_list':[{'type':2,'image_item':{'media':{'full_url':'https://example.com/i'}}},{'type':5,'video_item':{'media':{'full_url':'https://example.com/v.mp4'}}}]})
    for m in [a,b]:
     self.assertEqual(len(m.images),1);self.assertEqual(m.content_parts[1]['type'],'video');data=await ContextManager(__import__('src.store_memory',fromlist=['MemoryStore']).MemoryStore()).build(m,image_inputs=[{'type':'image_url','image_url':{'url':'actual-image'}}]);self.assertIn('没有向模型提供视频画面',str(data));self.assertNotIn('v.mp4',str(data))
   finally:await qq._client.aclose();await wx._client.aclose()
 async def test_user_facing_model_error_never_blames_configuration(self):
  class Adapter:
   platform='onebot'
   def on_message(self,h):pass
  class Broken:
   async def stream(self,*a,**k):raise ValueError('fixture-secret');yield ''
  class Sender:
   sent=[]
   async def send(self,c,t,**k):self.sent.append(t)
   async def stream_send(self,c,s,**k):return ''.join([p async for p in s])
  from src.store_memory import MemoryStore
  store=MemoryStore();sender=Sender();o=Orchestrator(Adapter(),store,Broken(),None,ContextManager(store),TriggerRouter(),sender,typing_indicator=False)
  await o.handle(msg([ref()]));self.assertEqual(len(sender.sent),1)
  for s in ['模型与行为','配置','API','fixture-secret']:self.assertNotIn(s,sender.sent[0])
  await o.close()

class MixedProtocolSDKTests(unittest.IsolatedAsyncioTestCase):
 async def pipeline(self,platform,web=False):
  from src.store_memory import MemoryStore
  requests=[];sent=[]
  def completion(text):return {'id':'fixture','object':'chat.completion','created':1,'model':'fixture','choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],'usage':{'prompt_tokens':2,'completion_tokens':4,'total_tokens':6}}
  def model(req):
   b=json.loads(req.content);requests.append(b);self.assertEqual(b['model'],'deepseek-v4.1-flash');images=[p for m in b['messages'] if isinstance(m.get('content'),list) for p in m['content'] if p.get('type')=='image_url']
   if len(images)>1:return httpx.Response(400,json={'error':{'message':'single image per call','type':'invalid_request_error','code':'image_batch'}})
   if images:
    self.assertNotIn('tools',b);data=base64.b64decode(images[0]['image_url']['url'].split(',')[1]);color=Image.open(io.BytesIO(data)).getpixel((0,0));return httpx.Response(200,json=completion('红色画面' if color==(255,0,0) else '蓝色画面'))
   text=json.dumps(b,ensure_ascii=False);self.assertIn('红色画面',text);self.assertIn('蓝色画面',text);self.assertIn('视频仍只是附件标记',text)
   return httpx.Response(200,json=completion('第一张是红色，第二张是蓝色。视频这次只读到了附件标记，你想让我看哪一段？'))
  sdk=AsyncOpenAI(api_key='fixture-key',base_url='http://model/v1',http_client=httpx.AsyncClient(transport=httpx.MockTransport(model)),max_retries=0)
  g=LLMGateway(ProviderCfg('primary','http://model/v1','fixture-key','deepseek-v4.1-flash')); g._client=lambda c:sdk
  if web:g.web_search=NS(search=AsyncMock(side_effect=AssertionError('image-only turn must not search')))
  with tempfile.TemporaryDirectory() as d:
   if platform=='onebot':
    a=OneBotAdapter('http://gateway',bot_qq='999')
    async def call(action,body):self.assertEqual(action,'send_private_msg');sent.append(body);return {'status':'ok','retcode':0,'data':{'message_id':9}}
    a._call=call
    segments=[{'type':'image','data':{'file':'base64://'+ref()['inline']}},{'type':'video','data':{'file':'video-fixture','url':'https://example.com/private.mp4'}},{'type':'image','data':{'file':'base64://'+ref('blue')['inline']}}]
    m=a._normalize({'message_type':'private','user_id':123,'self_id':999,'message_id':1,'message':segments})
   else:
    a=ILinkAdapter(d);a.bot_id='fixture-bot'
    async def post(endpoint,body):sent.append(body);return {'ret':0,'message_id':'out'}
    a._post=post;m=a._normalize({'message_type':1,'from_user_id':'wx-user','message_id':1,'context_token':'fixture-token','item_list':[{'type':2,'image_item':{}},{'type':5,'video_item':{'media':{'full_url':'https://example.com/private.mp4'}}},{'type':2,'image_item':{}}]});m.images=[ref(),ref('blue')]
   store=MemoryStore();o=Orchestrator(a,store,g,None,ContextManager(store),TriggerRouter(),HumanLikeSender(a),typing_indicator=False)
   try:
    await o.handle(m);self.assertEqual(len(sent),1);self.assertEqual(len(requests),5 if web else 4);self.assertIn('蓝色',json.dumps(sent,ensure_ascii=False));self.assertNotIn('模型与行为',json.dumps(sent,ensure_ascii=False));self.assertNotIn('private.mp4',json.dumps(requests));self.assertEqual(len(await store.get_recent(m.ctx_key)),1)
   finally:await o.close();await a._client.aclose();await sdk.close()
 async def test_qq_images_plus_video_rejected_batch_recovers_once(self):await self.pipeline('onebot')
 async def test_wechat_images_plus_video_rejected_batch_recovers_once(self):await self.pipeline('ilink',web=True)

class LateWechatMediaTests(unittest.IsolatedAsyncioTestCase):
 async def test_video_and_mixed_markers_wait_for_caption(self):
  from src.core.turn_taking import plan_wait
  a=msg([{}]);b=msg();b.msg_type='video';b.content='[视频]';b.content_parts=[{'type':'video','text':'[视频]'}]
  for m in [a,b]:self.assertEqual(plan_wait([(m,0)],4,10).delay,6)
 async def test_legacy_force_never_allows_a_stale_reply(self):
  from src.core.conversation import ConversationChanged
  with tempfile.TemporaryDirectory() as d:
   a=ILinkAdapter(d);a.configure_burst(.02,.02,human=False)
   raw={'message_type':1,'from_user_id':'u','message_id':'1','item_list':[],'_wxbot_batch_keys':['u:1'],'_wxbot_batch_known':['u:1'],'_wxbot_batch_force':True}
   a._pending=[raw,{'from_user_id':'u','message_id':'2'}];m=a._normalize(raw)
   try:
    with self.assertRaises(ConversationChanged):a.check_reply_current(m)
    with self.assertRaises(ConversationChanged):a.check_failed_reply_current(m)
   finally:await a._client.aclose()
 async def test_wechat_late_video_regroups_without_error_then_second_reply(self):
  import time
  from src.store_memory import MemoryStore
  entered=asyncio.Event();sent=[]
  class Flaky:
   count=0
   async def stream(self,*args,**kwargs):
    self.count+=1
    if self.count==1:entered.set();raise ValueError('fixture-image-request')
    yield '这张照片是红色的。视频只收到了附件标记。'
  with tempfile.TemporaryDirectory() as d:
   a=ILinkAdapter(d,base_url='http://media');a.bot_id='fixture-bot';a.configure_burst(.02,.02,human=False)
   first={'message_type':1,'from_user_id':'u','message_id':'1','context_token':'first','_wxbot_received_at':time.time()-1,'item_list':[{'type':2,'image_item':{'media':{'full_url':'http://media/picture'}}}]}
   a._pending=[first]
   async def post(endpoint,body):sent.append(body);return {'ret':0,'message_id':'out'}
   a._post=post;store=MemoryStore();chat=Flaky();o=Orchestrator(a,store,chat,None,ContextManager(store),TriggerRouter(),HumanLikeSender(a),typing_indicator=False)
   o.images=ImageLoader(['http://media'],httpx.MockTransport(lambda r:httpx.Response(200,content=png())))
   task=asyncio.create_task(a._process_pending(first,'u'))
   try:
    await asyncio.wait_for(entered.wait(),1);await asyncio.sleep(.04)
    a._pending.append({'message_type':1,'from_user_id':'u','message_id':'2','context_token':'latest','_wxbot_received_at':time.time(),'item_list':[{'type':5,'video_item':{}}]})
    await asyncio.wait_for(task,1)
    if a._pending:await asyncio.wait_for(a._process_pending(a._pending[0],'u'),1)
    self.assertEqual(len(sent),1);self.assertEqual(sent[0]['msg']['context_token'],'latest');self.assertIn('红色',json.dumps(sent,ensure_ascii=False));self.assertNotIn('模型与行为',json.dumps(sent,ensure_ascii=False));self.assertEqual(a._pending,[]);self.assertEqual(chat.count,2)
   finally:
    task.cancel();await asyncio.gather(task,return_exceptions=True);await o.close();await a._client.aclose()

if __name__=='__main__':unittest.main()
