"""Real automatic-learning wiring with temporary state and controlled model outputs."""
import asyncio,importlib,json,os
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from file_privacy import assert_private_file
from src.core.memory import PersonaMemory,MemoryExtractionError
from src.core.memory_learning import error_summary
from src.core.context import ContextManager
from src.core.types import WxMessage
from src.core.sender import HumanLikeSender,DeliveryError
from src.core.trigger import TriggerRouter
from src.orchestrator import Orchestrator
from src.store_memory import MemoryStore
from src.config_store import AppConfig,ConfigStore
from src.runtime import BotRuntime

VALID='[{"category":"fact","content":"用户是后端工程师","keywords":["职业","后端"],"evidence":"我是后端工程师"}]'
class Model:
 def __init__(self,output=VALID):self.output=output;self.calls=0;self.prompts=[]
 async def complete(self,prompt,**kw):self.calls+=1;self.prompts.append(prompt);return self.output
class Adapter:
 platform='fake'
 def on_message(self,h):self.handler=h
 async def send_text(self,*a,**kw):return 'sent'
class Bucket:
 async def acquire(self):pass
class Chat:
 def __init__(self):self.messages=[]
 async def stream(self,messages,**kw):self.messages=messages;yield '好的。'
def msg(mid='1',content='我是后端工程师',owner='u',chat='p_u'):
 return WxMessage(mid,'fake','private',chat,owner,owner,'bot',content)
def make_orch(memory,adapter=None):
 store=MemoryStore();adapter=adapter or Adapter();sender=HumanLikeSender(adapter);sender.global_bucket=Bucket();sender.chat_buckets['p_u']=Bucket()
 return Orchestrator(adapter,store,Chat(),None,ContextManager(store),TriggerRouter(),sender,memory=memory)

class LearningTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.model=Model();self.mem=PersonaMemory(self.path/'memory.json',llm=self.model);self.mem.extract_interval=3
 async def asyncTearDown(self):await self.mem.learner.close();self.tmp.cleanup()
 async def seed(self,owner='u',start=0):
  q=self.mem.learner
  for i,text in enumerate(('我是后端工程师','工作主要在服务端开发','项目已经维护了很多年')):
   await q.enqueue(msg(str(start+i),text,owner=owner,chat='p_'+owner),'')
 async def reflect(self):
  q=self.mem.learner
  for job in q.pending.values():job['due_at']=0
  q.last_reflection.clear();q.start();await q.wait_idle()
 async def test_single_statement_and_remember_do_not_trigger(self):
  q=self.mem.learner
  await q.enqueue(msg('1','记住，我是后端工程师'),'');await q.wait_idle()
  self.assertEqual(self.model.calls,0);self.assertEqual(self.mem.stats()['total'],0);self.assertEqual(q.status()['pending_turns'],1)
 async def test_three_turns_still_wait_idle_two_minutes(self):
  await self.seed();q=self.mem.learner;self.assertEqual(self.model.calls,0)
  self.assertTrue(all(j['due_at'] > __import__('time').time()+118 for j in q.pending.values()))
 async def test_three_turns_form_candidate_then_survive_restart(self):
  await self.seed();await self.reflect();self.assertEqual(self.model.calls,1)
  item=next(iter(self.mem._items.values()));self.assertEqual(item.phase,'candidate');self.assertEqual(self.mem.build_context('u','p_u','职业'),'')
  loaded=PersonaMemory(self.path/'memory.json');self.assertEqual(loaded.stats()['phases']['candidate'],1);assert_private_file(self,self.path/'memory.json')
 async def test_semantic_ack_and_emoji_do_not_count(self):
  q=self.mem.learner
  for i,text in enumerate(('好的','嗯嗯','谢谢','😂')):await q.enqueue(msg(str(i),text),'')
  self.assertEqual(q.status()['pending_turns'],0)
 async def test_fragments_and_callbacks_one_semantic_turn(self):
  m=msg();m.batch_messages=[{'msg_id':str(i)} for i in range(60)];q=self.mem.learner
  await q.enqueue(m,'');await q.enqueue(m,'');self.assertEqual(q.status()['pending_turns'],1)
 async def test_cooldown_survives_restart(self):
  await self.seed();await self.reflect();await self.mem.learner.close();loaded=PersonaMemory(self.path/'memory.json',llm=self.model)
  try:
   for i in range(3):await loaded.learner.enqueue(msg(str(i+10),'关于工程师工作的补充'+str(i)),'')
   self.assertTrue(all(j['due_at'] > __import__('time').time()+298 for j in loaded.learner.pending.values()))
  finally:await loaded.learner.close()
 async def test_no_assistant_in_extraction(self):
  await self.seed();await self.reflect();body=self.model.prompts[0].split('【对话】')[-1];self.assertIn('我是后端工程师',body);self.assertNotIn('助手:',body)
 async def test_invalid_json_retained_and_retry(self):
  self.model.output='not JSON';await self.seed();await self.reflect();self.assertEqual(self.mem.learner.status()['pending_turns'],3);self.assertIn('invalid JSON',self.mem.learner.status()['last_error'])
  self.model.output=VALID;await self.reflect();self.assertEqual(self.mem.stats()['total'],1)
 async def test_empty_output_error_and_empty_array_success(self):
  self.model.output=''
  with self.assertRaises(MemoryExtractionError):await self.mem.observe('p','private','u','u',[{'user':'我是后端工程师'}])
  self.model.output='[]';await self.seed();await self.reflect();self.assertEqual(self.mem.learner.status()['empty'],1)
 async def test_invalid_schema_rejected(self):
  self.model.output='[{"content":"事实"}]'
  with self.assertRaises(MemoryExtractionError):await self.mem.observe('p','private','u','u',[{'user':'内容'}])
 async def test_disk_failure_rolls_back(self):
  with patch.object(self.mem,'_save',side_effect=OSError('disk')):
   with self.assertRaises(OSError):await self.mem.observe('p','private','u','u',[{'user':'我是后端工程师'}])
  self.assertEqual(self.mem.stats()['total'],0)
 async def test_enqueue_failure_visible(self):
  with patch.object(self.mem.learner,'_save',side_effect=OSError('disk')):
   with self.assertRaises(OSError):await self.mem.learner.enqueue(msg(),'')
  self.assertEqual(self.mem.learner.status()['state'],'save_failed')
 async def test_failed_provider_is_sanitized(self):
  class Problem(Exception):status_code=401
  class Bad:
   async def complete(self,*a,**kw):raise Problem('secret and userdata')
  self.mem.llm=Bad();await self.seed();await self.reflect();self.assertEqual(self.mem.learner.status()['last_error'],'Problem (HTTP 401)')
 async def test_disabled_does_not_extract_manual_still_available(self):
  self.mem.enabled=False;self.mem.add_manual('user','u','fact','用户是后端工程师',['职业']);await self.seed();self.assertEqual(self.model.calls,0);self.assertIn('后端',self.mem.build_context('u','p_u','职业'))
 async def test_new_messages_during_extraction_preserved(self):
  entered=asyncio.Event();gate=asyncio.Event()
  class Slow:
   async def complete(self,*a,**k):entered.set();await gate.wait();return VALID
  self.mem.llm=Slow();await self.seed();q=self.mem.learner
  q.retry_now();await asyncio.wait_for(entered.wait(),1);await q.enqueue(msg('new','新增的详细补充'),'');gate.set()
  for _ in range(100):
   if not q._jobs:break
   await asyncio.sleep(.01)
  # A fresh user turn now preempts background reflection; its three original
  # source turns remain pending along with the new supplement, not acknowledged.
  self.assertEqual(q.status()['pending_turns'],4);self.assertEqual(q.info['failures'],0)
 async def test_restart_preserves_pending(self):
  await self.seed();await self.mem.learner.close();loaded=PersonaMemory(self.path/'memory.json',llm=self.model)
  try:
   self.assertEqual(loaded.learner.status()['pending_turns'],3);loaded.learner.retry_now();await loaded.learner.wait_idle();self.assertEqual(loaded.stats()['phases']['candidate'],1)
  finally:await loaded.learner.close()
 async def test_delete_cancels_inflight_and_future_same_fact(self):
  item=self.mem.add_manual('user','u','fact','用户是后端工程师',['职业']);entered=asyncio.Event();gate=asyncio.Event()
  class Slow:
   async def complete(self,*a,**k):entered.set();await gate.wait();return VALID
  self.mem.llm=Slow();await self.seed();self.mem.learner.retry_now();await entered.wait();self.mem.delete(item['id']);gate.set();await asyncio.sleep(.02)
  self.assertEqual(self.mem.stats()['total'],0);self.assertEqual(self.mem.learner.status()['pending_turns'],0)
  self.mem.llm=self.model;result=await self.mem.observe('p','private','u','u',[{'user':'我是后端工程师'}]);self.assertEqual(result,[])
 async def test_journal_private(self):
  await self.seed();assert_private_file(self,self.mem.learner.path)
 async def test_single_fact_delivery_failure_does_not_create_long_memory(self):
  class BadAdapter(Adapter):
   async def send_text(self,*a,**kw):raise RuntimeError('send fails')
  o=make_orch(self.mem,BadAdapter())
  try:
   with self.assertRaises(DeliveryError):await o.handle(msg())
   await o.wait_for_memory();self.assertEqual(self.mem.stats()['total'],0)
  finally:await o.close()

class RuntimeLearningTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.store=ConfigStore(self.path/'config.json');self.cfg=AppConfig();self.cfg.llm.primary_key='fake';self.store.save(self.cfg);self.rt=BotRuntime(self.store,self.path)
 async def asyncTearDown(self):await self.rt.stop();self.tmp.cleanup()
 async def test_list_and_stats_enabled_match(self):
  self.assertTrue(self.rt.memory_list()['stats']['enabled']);self.assertTrue(self.rt.memory_stats()['enabled']);self.assertIn('learning',self.rt.memory_list()['stats'])
 async def test_memory_gateway_shared_not_overwritten_by_second_channel(self):
  a=self.rt._build_orchestrator(Adapter());gateway=self.rt.memory.llm;b=self.rt._build_orchestrator(Adapter());self.assertIs(a.memory,b.memory);self.assertIs(gateway,b.memory.llm);self.assertIsNot(gateway,a.judge.gateway);await a.close();await b.close()
 async def test_memory_gateway_primary_fallback_and_timeout(self):
  p=BotRuntime._memory_provider(self.cfg);self.assertEqual(p.api_key,'fake');self.assertEqual(p.model,self.cfg.llm.primary_model);self.assertEqual(p.timeout,30)
 async def test_memory_gateway_explicit_judge(self):
  self.cfg.llm.judge_key='judge';self.cfg.llm.judge_model='extractor';p=BotRuntime._memory_provider(self.cfg);self.assertEqual((p.api_key,p.model),('judge','extractor'))
 async def test_retry_disabled_no_background_calls(self):
  self.cfg.bot.memory_enabled=False;self.rt.apply_config(self.cfg);self.assertFalse(self.rt.memory_retry_learning()['ok'])
 async def test_authenticated_api_reports_enabled_and_errors(self):
  import httpx
  with patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'memory-test','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'}):
   import src.server as server;server=importlib.reload(server)
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://127.0.0.1') as c:
    self.assertEqual((await c.post('/api/memory/learning/retry')).status_code,401)
    headers={'Authorization':'Bearer memory-test'};data=(await c.get('/api/memory',headers=headers)).json();self.assertTrue(data['stats']['enabled']);self.assertTrue(data['stats']['learning']['configured']);self.assertEqual((await c.post('/api/memory/learning/retry',headers=headers)).status_code,200);self.assertIn('memory-learning-status',(await c.get('/')).text)
   await server.runtime.stop()

if __name__=='__main__':unittest.main()
