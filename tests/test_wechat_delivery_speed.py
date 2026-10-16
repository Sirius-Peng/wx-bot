"""Local fake iLink assertions for protocol, recovery, ordering and response latency."""
import asyncio,json,os
from pathlib import Path
import sys,tempfile,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from file_privacy import assert_private_file
from src.adapters.ilink import ILinkAdapter,MAX_ACTIVE_CHATS,MAX_PENDING
from src.core.sender import HumanLikeSender,DeliveryError
from src.orchestrator import Orchestrator
from src.core.context import ContextManager
from src.core.types import WxMessage
from src.core.trigger import TriggerRouter
from src.store_memory import MemoryStore
from src.core.llm import LLMGateway,ProviderCfg

class Bucket:
 async def acquire(self):pass
class Model:
 def __init__(self):self.calls=0
 async def stream(self,*a,**kw):
  self.calls+=1
  yield '完整回答。\n\n'
  yield '保留结尾。'
def msg(mid='1',peer='u',token=None):
 return WxMessage(mid,'ilink','private',peer,peer,peer,'bot','你好',context_token=token or 'ctx-'+mid)
def raw(mid='1',peer='u'):
 return {'message_type':1,'message_id':mid,'from_user_id':peer,'context_token':'ctx-'+mid,'item_list':[{'type':1,'text_item':{'text':'你好'}}]}
def orch(adapter,model=None,memory=None):
 sender=HumanLikeSender(adapter);sender.global_bucket=Bucket();sender.chat_buckets['u']=Bucket()
 store=MemoryStore()
 return Orchestrator(adapter,store,model or Model(),None,ContextManager(store),TriggerRouter(),sender,memory=memory)

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.d=Path(self.tmp.name);self.a=ILinkAdapter(str(self.d));self.a.bot_id='bot@im.bot';self.a.bot_token='bot@im.bot:fake';self.a._save_token()
 async def asyncTearDown(self):await self.a._client.aclose();self.tmp.cleanup()
 async def test_complete_official_payload(self):
  bodies=[]
  async def post(e,b):bodies.append(b);return {'ret':0}
  self.a._post=post
  with self.a.bind_reply_context(msg()):mid=await self.a.send_text('u','回复')
  self.assertTrue(mid.startswith('wxbot-'));b=bodies[0];self.assertIn('base_info',b);self.assertEqual(b['msg']['from_user_id'],'');self.assertEqual(b['msg']['context_token'],'ctx-1');self.assertEqual(b['msg']['message_state'],2);self.assertTrue(b['msg']['client_id'])
 async def test_both_business_errors_checked(self):
  for response in [{'ret':-2},{'ret':0,'errcode':-7},{'errcode':-14},{}]:
   async def post(*a):return response
   self.a._post=post
   with self.a.bind_reply_context(msg(str(response))):
    with self.assertRaises(RuntimeError):await self.a.send_text('u','回复')
  self.assertEqual(self.a.delivery_status()['accepted'],0)
 async def test_missing_context_never_posts(self):
  async def post(*a):self.fail('orphan request posted')
  self.a._post=post
  with self.assertRaisesRegex(RuntimeError,'missing context'):await self.a.send_text('u','回复')
 async def test_context_preflight_avoids_llm_cost(self):
  model=Model();o=orch(self.a,model);m=msg();m.context_token=None
  with self.assertRaisesRegex(RuntimeError,'missing context'):await o.handle(m)
  self.assertEqual(model.calls,0)
 async def test_delivery_failure_retains_answer_not_fallback(self):
  bodies=[]
  async def reject(e,b):bodies.append(b);return {'ret':-2}
  self.a._post=reject;model=Model();o=orch(self.a,model)
  with self.assertRaises(DeliveryError):await o.handle(msg())
  self.assertEqual(model.calls,1);self.assertEqual(len(bodies),1);self.assertEqual(bodies[0]['msg']['item_list'][0]['text_item']['text'],'完整回答。保留结尾。');history=await o.store.get_recent(msg().ctx_key);self.assertEqual(len(history),1);self.assertEqual(history[0]['user'],'你好');self.assertEqual(history[0]['assistant'],'')
 async def test_retry_reuses_draft_and_client_id(self):
  bodies=[]
  async def post(e,b):bodies.append(b);return {'ret':-2} if len(bodies)==1 else {'ret':0}
  self.a._post=post;model=Model();o=orch(self.a,model)
  with self.assertRaises(DeliveryError):await o.handle(msg())
  await o.handle(msg());self.assertEqual(model.calls,1);self.assertEqual(bodies[0],bodies[1]);self.assertEqual(self.a.delivery_status()['accepted'],1)
 async def test_restart_reuses_unsent_draft(self):
  bodies=[]
  async def reject(e,b):bodies.append(b);raise RuntimeError('transport timeout')
  self.a._post=reject;o=orch(self.a)
  with self.assertRaises(DeliveryError):await o.handle(msg())
  b=ILinkAdapter(str(self.d));b._load_token();m=Model();restored=orch(b,m)
  async def accept(e,p):bodies.append(p);return {'ret':0}
  b._post=accept
  try:await restored.handle(msg())
  finally:await b._client.aclose()
  self.assertEqual(m.calls,0);self.assertEqual(bodies[0],bodies[1]);assert_private_file(self,self.d/'outbox.json')
 async def test_accepted_draft_not_reposted_after_restart(self):
  async def accept(*a):return {'ret':0,'message_id':18446744073709551615}
  self.a._post=accept;await orch(self.a).handle(msg());b=ILinkAdapter(str(self.d));b._load_token();m=Model()
  async def no_post(*a):self.fail('accepted reply resent')
  b._post=no_post
  try:await orch(b,m).handle(msg())
  finally:await b._client.aclose()
  self.assertEqual(m.calls,0)
 async def test_bound_context_not_replaced_by_newer_message(self):
  bodies=[]
  async def post(e,b):bodies.append(b);return {'ret':0}
  self.a._post=post;self.a.remember_context(msg('new'))
  with self.a.bind_reply_context(msg('old')):await self.a.send_text('u','旧消息回答')
  self.assertEqual(bodies[0]['msg']['context_token'],'ctx-old')
 async def test_outbox_written_before_network(self):
  async def post(e,b):
   d=json.loads((self.d/'outbox.json').read_text());self.assertEqual(next(iter(d['replies'].values()))['client_id'],b['msg']['client_id']);return {'ret':0}
  self.a._post=post
  with self.a.bind_reply_context(msg()):await self.a.send_text('u','回复')
 async def test_disk_failure_prevents_network(self):
  async def no_post(*a):self.fail('sent without durable draft')
  self.a._post=no_post
  with patch.object(self.a,'_save_outbox',side_effect=OSError('disk')),self.a.bind_reply_context(msg()):
   with self.assertRaises(OSError):await self.a.send_text('u','回复')
 async def test_http_metadata_and_slash(self):
  captured=[]
  class Client:
   async def post(self,url,**kw):
    captured.append((url,kw));return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'ret':0})
  old=self.a._client;self.a._client=Client();self.a.bot_base_url='http://localhost/'
  try:await self.a._post('ilink/bot/sendmessage',{'msg':{}})
  finally:self.a._client=old
  url,kw=captured[0];self.assertNotIn('//ilink',url);self.assertIn('base_info',kw['json']);self.assertEqual(kw['headers']['iLink-App-Id'],'bot');self.assertEqual(kw['headers']['iLink-App-ClientVersion'],str((1<<16)|(3<<8)|4))
 async def test_ack_checkpoint_failure_keeps_pending(self):
  self.a._pending=[raw()];self.a.on_message(lambda m:asyncio.sleep(0))
  with patch.object(self.a,'_save_cursor',side_effect=OSError('disk')):
   self.assertFalse(await self.a._drain_pending())
  self.assertEqual(len(self.a._pending),1)
 async def test_poll_continues_during_slow_handler(self):
  started=asyncio.Event();release=asyncio.Event();next_poll=asyncio.Event();n=0;self.a._running=True
  async def post(*a):
   nonlocal n;n+=1
   if n==1:return {'ret':0,'msgs':[raw()],'get_updates_buf':'one'}
   next_poll.set();await release.wait();self.a._running=False;self.a._wakeup.set();return {'ret':0,'msgs':[]}
  async def slow(m):started.set();await release.wait()
  self.a._post=post;self.a.on_message(slow);task=asyncio.create_task(self.a.poll_loop())
  try:
   await asyncio.wait_for(started.wait(),0.5);await asyncio.wait_for(next_poll.wait(),0.2)
   self.assertFalse(release.is_set())
  finally:release.set();await asyncio.wait_for(task,1)
 async def test_same_peer_order_other_peer_parallel(self):
  self.a._running=True;self.a._pending=[raw('1'),raw('2'),raw('3','other')];events=[];gate=asyncio.Event();other=asyncio.Event()
  async def handle(m):
   events.append(m.msg_id)
   if m.msg_id=='1':await gate.wait()
   if m.msg_id=='3':other.set()
  self.a.on_message(handle);task=asyncio.create_task(self.a._dispatch_loop())
  try:
   await asyncio.wait_for(other.wait(),0.5);self.assertEqual(events,['1','3']);gate.set()
   for _ in range(100):
    if '2' in events:break
    await asyncio.sleep(0.005)
   self.assertEqual(events,['1','3','2'])
  finally:self.a._running=False;self.a._wakeup.set();await task
 async def test_worker_limit_and_cancel_preserves_all_pending(self):
  self.a._running=True;self.a._pending=[raw(str(i),str(i)) for i in range(8)];entered=[];gate=asyncio.Event()
  async def handle(m):entered.append(m.msg_id);await gate.wait()
  async def post(*a):await gate.wait();return {'ret':0}
  self.a.on_message(handle);self.a._post=post;task=asyncio.create_task(self.a.poll_loop())
  for _ in range(100):
   if len(entered)==4:break
   await asyncio.sleep(0.005)
  self.assertEqual(len(entered),MAX_ACTIVE_CHATS);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertEqual(len(self.a._pending),8);self.assertEqual(self.a._jobs,{})
 async def test_pending_batch_merge_not_overwrite(self):
  self.a._running=True;self.a._pending=[raw('old')];n=0
  async def post(*a):
   nonlocal n;n+=1
   if n==1:return {'ret':0,'msgs':[raw('new'),raw('old')],'get_updates_buf':'cursor'}
   self.a._running=False;return {'ret':0,'msgs':[]}
  self.a._post=post;await self.a._receive_loop();self.assertEqual([r['message_id'] for r in self.a._pending],['old','new'])
 async def test_errcode_poll_does_not_advance(self):
  self.a._running=True
  async def post(*a):self.a._running=False;return {'ret':0,'errcode':-7,'get_updates_buf':'bad','msgs':[raw()]}
  self.a._post=post
  with patch('src.adapters.ilink.asyncio.sleep',return_value=None):await self.a._receive_loop()
  self.assertEqual(self.a._get_updates_buf,'');self.assertEqual(self.a._pending,[])
 async def test_bad_peer_does_not_block_good_peer(self):
  self.a._running=True;self.a._pending=[raw('bad','bad'),raw('good','good')];done=asyncio.Event()
  async def handle(m):
   if m.chat_id=='bad':raise RuntimeError('API rejects')
   done.set()
  self.a.on_message(handle);task=asyncio.create_task(self.a._dispatch_loop())
  try:await asyncio.wait_for(done.wait(),0.5)
  finally:self.a._running=False;self.a._wakeup.set();await task
  self.assertEqual([m['message_id'] for m in self.a._pending],['bad'])
 async def test_backpressure_does_not_fetch_at_limit(self):
  self.a._running=True;self.a._pending=[raw(str(i)) for i in range(MAX_PENDING)]
  async def no_post(*a):self.fail('backpressure ignored')
  self.a._post=no_post;task=asyncio.create_task(self.a._receive_loop());await asyncio.sleep(0.02);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
 async def test_background_memory_does_not_block_next_reply(self):
  gate=asyncio.Event();entered=asyncio.Event()
  class Memory:
   enabled=True;inject=False;extract_interval=1
   async def observe(self,**kw):entered.set();await gate.wait()
  async def post(*a):return {'ret':0}
  self.a._post=post;o=orch(self.a,memory=Memory())
  try:
   await asyncio.wait_for(o.handle(msg()),0.2);await asyncio.wait_for(entered.wait(),0.2)
   await asyncio.wait_for(o.handle(msg('2')),0.2);self.assertEqual(len(o._memory_tasks),2)
   await asyncio.wait_for(o.handle(msg('3')),0.2);self.assertLessEqual(len(o._memory_tasks),2)
  finally:gate.set();await o.wait_for_memory();await o.close()
 async def test_cancel_waiting_context_lock_releases_seen(self):
  o=orch(self.a);lock=o._locks.setdefault(msg().ctx_key,asyncio.Lock());await lock.acquire();task=asyncio.create_task(o.handle(msg()))
  await asyncio.sleep(0);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  lock.release();self.assertNotIn('1',o.store._seen)

 async def test_start_does_not_wait_for_metadata(self):
  blocked=asyncio.Event();entered=asyncio.Event()
  async def post(*a):entered.set();await blocked.wait();return {'ret':0}
  self.a._post=post
  try:
   await asyncio.wait_for(self.a.start(),0.1);await asyncio.wait_for(entered.wait(),0.1);self.assertTrue(self.a._running)
  finally:self.a._startup_task.cancel();await asyncio.gather(self.a._startup_task,return_exceptions=True)
 async def test_success_history_uses_original_draft(self):
  n=0
  async def post(*a):
   nonlocal n;n+=1;return {'ret':-2} if n==1 else {'ret':0}
  self.a._post=post;o=orch(self.a)
  with self.assertRaises(DeliveryError):await o.handle(msg())
  await o.handle(msg());recent=await o.store.get_recent(msg().ctx_key);self.assertEqual(recent[0]['assistant'],'完整回答。保留结尾。');self.assertEqual(len(recent),1)
 async def test_parallel_logs_do_not_mark_failed_peer_replied(self):
  from src.runtime import BotRuntime
  from src.config_store import ConfigStore,AppConfig
  store=ConfigStore(self.d/'config.json');store.save(AppConfig());rt=BotRuntime(store,self.d);o=rt._build_orchestrator(self.a,platform='wechat');o.memory.enabled=False;o.llm=Model();o.sender.global_bucket=Bucket();o.sender.chat_buckets['bad']=Bucket();o.sender.chat_buckets['good']=Bucket();gate=asyncio.Event()
  async def post(e,b):
   if b['msg']['to_user_id']=='bad':await gate.wait();return {'ret':-2}
   gate.set();return {'ret':0}
  self.a._post=post
  try:
   results=await asyncio.gather(o.handle(msg('bad','bad')),o.handle(msg('good','good')),return_exceptions=True)
   self.assertTrue(any(isinstance(e,DeliveryError) for e in results));entries=[e for e in rt.log._buf if e.direction=='in'];self.assertEqual({e.chat_id:e.replied for e in entries},{'bad':False,'good':True})
  finally:await o.close()

class SpeedDefaultsTests(unittest.TestCase):
 def test_no_human_delay(self):self.assertEqual(HumanLikeSender.human_delay('长'*1000),0)
 def test_ilink_separate_rate(self):
  s=HumanLikeSender(SimpleNamespace(platform='ilink'));self.assertEqual(s.global_bucket.rate,1);self.assertEqual(s.chat_buckets['u'].rate,0.5);self.assertEqual(s.chat_buckets['u'].capacity,2)
 def test_qq_private_rate_and_group_boundaries(self):
  s=HumanLikeSender(SimpleNamespace(platform='onebot'));self.assertEqual(s.chat_buckets['p_u'].rate,12/60);self.assertEqual(s.chat_buckets['p_u'].capacity,2);self.assertEqual(s.chat_buckets['g_1'].rate,2/60);self.assertEqual(s.chat_buckets['g_1'].capacity,1);self.assertEqual(s.global_bucket.rate,20/60)
 def test_sdk_retries_disabled(self):
  gw=LLMGateway(ProviderCfg('primary','http://fake','fake','fake'))
  with patch('src.core.llm.AsyncOpenAI',lambda **kw:kw):self.assertEqual(gw._client(gw.primary)['max_retries'],0)

if __name__=='__main__':unittest.main()
