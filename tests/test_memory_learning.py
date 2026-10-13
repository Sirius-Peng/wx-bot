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

VALID='[{"category":"fact","content":"用户是后端工程师","keywords":["职业","后端"]}]'
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
 async def test_single_fact_persists_and_reloads(self):
  o=make_orch(self.mem);await o.handle(msg());await o.wait_for_memory();self.assertEqual(self.model.calls,1);self.assertEqual(self.mem.stats()['total'],1)
  loaded=PersonaMemory(self.path/'memory.json');self.assertEqual(loaded.stats()['total'],1);self.assertIn('后端',loaded.build_context('u','p_u','职业'));assert_private_file(self,self.path/'memory.json')
 async def test_idle_flush_partial_batch(self):
  q=self.mem.learner;q.IDLE_SECONDS=.03;await q.enqueue(msg('1','你好'),'');await q.wait_idle();self.assertEqual(self.model.calls,1)
  await q.enqueue(msg('2','用户仍然在做后端工作'),'')
  for _ in range(100):
   if self.model.calls>=2 and not q.pending:break
   await asyncio.sleep(.01)
  self.assertEqual(self.model.calls,2)
 async def test_explicit_memory_ignores_large_interval(self):
  self.mem.extract_interval=50;q=self.mem.learner;await q.enqueue(msg('first','你好'),'');await q.wait_idle()
  await q.enqueue(msg('2','记住，我喜欢绿茶'),'')
  for _ in range(100):
   if self.model.calls==2:break
   await asyncio.sleep(.005)
  self.assertEqual(self.model.calls,2)
 async def test_busy_queue_keeps_third_user(self):
  gate=asyncio.Event();entered=[]
  class Slow(Model):
   async def complete(self,prompt,**kw):entered.append(prompt);await gate.wait();return VALID
  self.mem.llm=Slow();q=self.mem.learner
  for i in range(3):await q.enqueue(msg(str(i),owner=str(i),chat=str(i)),'')
  for _ in range(100):
   if len(entered)==2:break
   await asyncio.sleep(.005)
  self.assertEqual(len(entered),2);self.assertEqual(q.status()['pending_turns'],3);gate.set();await q.wait_idle();self.assertEqual(len(entered),3);self.assertEqual(self.mem.stats()['total'],3)
 async def test_new_turn_during_extraction_not_lost(self):
  gate=asyncio.Event();entered=asyncio.Event();calls=[]
  class Slow:
   async def complete(self,prompt,**kw):calls.append(prompt);entered.set();await gate.wait();return VALID
  self.mem.llm=Slow();q=self.mem.learner;await q.enqueue(msg(),'');await asyncio.wait_for(entered.wait(),.2);await q.enqueue(msg('2','我喜欢绿茶'),'');gate.set();await q.wait_idle();self.assertEqual(len(calls),2);self.assertEqual(q.status()['pending_turns'],0)
 async def test_restart_preserves_inflight_job(self):
  gate=asyncio.Event();entered=asyncio.Event()
  class Slow:
   async def complete(self,*a,**kw):entered.set();await gate.wait();return VALID
  self.mem.llm=Slow();q=self.mem.learner;await q.enqueue(msg(),'');await asyncio.wait_for(entered.wait(),.2);await q.close()
  loaded=PersonaMemory(self.path/'memory.json',llm=self.model)
  try:loaded.learner.start();await loaded.learner.wait_idle();self.assertEqual(loaded.stats()['total'],1)
  finally:await loaded.learner.close()
 async def test_invalid_json_retained_then_retry(self):
  self.model.output='not JSON';q=self.mem.learner;await q.enqueue(msg(),'');await q.wait_idle();self.assertEqual(q.status()['pending_turns'],1);self.assertIn('invalid JSON',q.status()['last_error']);self.assertEqual(self.mem.stats()['total'],0)
  self.model.output=VALID;q.retry_now();await q.wait_idle();self.assertEqual(self.mem.stats()['total'],1);self.assertEqual(q.status()['last_error'],'')
 async def test_empty_array_success_not_failure(self):
  self.model.output='[]';q=self.mem.learner;await q.enqueue(msg('hello','你好'),'');await q.wait_idle();s=q.status();self.assertEqual((s['empty'],s['failures'],s['pending_turns']),(1,0,0));self.assertEqual(s['state'],'empty')
 async def test_empty_string_is_error(self):
  self.model.output=''
  with self.assertRaises(MemoryExtractionError):await self.mem.observe('p_u','private','u','用户',[{'user':'我是后端工程师'}])
 async def test_invalid_schema_is_error(self):
  for output in ['[{"content":"事实"}]','[{"category":{},"content":"事实","keywords":[]}]','[{"category":"fact","content":"事实","keywords":"词"}]']:
   self.model.output=output
   with self.assertRaises(MemoryExtractionError):await self.mem.observe('p_u','private','u','用户',[{'user':'我是后端工程师'}])
 async def test_disk_failure_does_not_claim_learned(self):
  with patch.object(self.mem,'_save',side_effect=OSError('disk')):
   with self.assertRaises(OSError):await self.mem.observe('p_u','private','u','用户',[{'user':'我是后端工程师'}])
  self.assertEqual(self.mem.stats()['total'],0);self.assertFalse((self.path/'memory.json').exists())
 async def test_enqueue_disk_failure_visible(self):
  with patch.object(self.mem.learner,'_save',side_effect=OSError('disk')):
   with self.assertRaises(OSError):await self.mem.learner.enqueue(msg(),'')
  self.assertEqual(self.model.calls,0);self.assertEqual(self.mem.learner.status()['state'],'save_failed')
 async def test_learning_failure_does_not_block_reply(self):
  self.model.output='invalid';o=make_orch(self.mem);await asyncio.wait_for(o.handle(msg()),.1);await o.wait_for_memory();self.assertEqual(len(await o.store.get_recent(msg().ctx_key)),1);self.assertEqual(self.mem.learner.status()['pending_turns'],1)
 async def test_model_http_error_is_sanitized(self):
  class Problem(Exception):status_code=401
  class Bad:
   async def complete(self,*a,**kw):raise Problem('secret-key-and-user-data')
  self.mem.llm=Bad();q=self.mem.learner;await q.enqueue(msg(),'');await q.wait_idle();self.assertEqual(q.status()['last_error'],'Problem (HTTP 401)')
 async def test_duplicate_inbound_is_not_relearned(self):
  q=self.mem.learner;await q.enqueue(msg(),'');await q.enqueue(msg(),'');await q.wait_idle();await q.enqueue(msg(),'');await q.wait_idle();self.assertEqual(self.model.calls,1)
 async def test_prompt_does_not_include_assistant_or_persona_examples(self):
  q=self.mem.learner;await q.enqueue(msg(),'助手角色台词秘密');await q.wait_idle();body=self.model.prompts[0].split('【对话】')[-1];self.assertIn('我是后端工程师',body);self.assertNotIn('助手角色台词秘密',body)
 async def test_owner_isolation(self):
  q=self.mem.learner;await q.enqueue(msg(),'');await q.wait_idle();self.assertEqual(self.mem.recall('other','other','职业'),[]);self.assertEqual(self.mem.list_items()[0]['owner_id'],'u')
 async def test_disabled_auto_still_injects_manual_memory(self):
  self.mem.enabled=False;self.mem.add_manual('user','u','fact','用户是后端工程师',['职业']);o=make_orch(self.mem);await o.handle(msg());self.assertEqual(self.model.calls,0);self.assertTrue(any('后端工程师' in m['content'] for m in o.llm.messages if m['role']=='system'))
 async def test_disable_while_extracting_keeps_pending(self):
  entered=asyncio.Event();gate=asyncio.Event()
  class Slow:
   async def complete(self,*a,**kw):entered.set();await gate.wait();return VALID
  self.mem.llm=Slow();q=self.mem.learner;await q.enqueue(msg(),'');await entered.wait();self.mem.enabled=False;gate.set()
  for _ in range(100):
   if not q._jobs:break
   await asyncio.sleep(.005)
  self.assertEqual(self.mem.stats()['total'],0);self.assertEqual(q.status()['pending_turns'],1);self.mem.enabled=True;q.retry_now();await q.wait_idle();self.assertEqual(self.mem.stats()['total'],1)
 async def test_clear_cancels_pending_extraction(self):
  entered=asyncio.Event();gate=asyncio.Event()
  class Slow:
   async def complete(self,*a,**kw):entered.set();await gate.wait();return VALID
  self.mem.llm=Slow();q=self.mem.learner;await q.enqueue(msg(),'');await entered.wait();self.mem.clear();gate.set();await asyncio.sleep(.03);self.assertEqual(q.status()['pending_turns'],0);self.assertEqual(self.mem.stats()['total'],0)
 async def test_failed_delivery_still_learns_received_fact(self):
  class BadAdapter(Adapter):
   async def send_text(self,*a,**kw):raise RuntimeError('send fails')
  o=make_orch(self.mem,BadAdapter())
  with self.assertRaises(DeliveryError):await o.handle(msg())
  await o.wait_for_memory();self.assertEqual(self.mem.stats()['total'],1)
 async def test_unconfigured_job_waits_without_losing_source(self):
  self.mem.llm=None;q=self.mem.learner;await q.enqueue(msg(),'');await asyncio.sleep(.03);self.assertEqual(q.status()['state'],'unconfigured');self.assertEqual(q.status()['pending_turns'],1);self.mem.llm=self.model;q.retry_now();await q.wait_idle();self.assertEqual(self.mem.stats()['total'],1)
 async def test_journal_ack_failure_restores_pending(self):
  q=self.mem.learner;calls=0;original=q._save
  def save():
   nonlocal calls;calls+=1
   if calls==4:raise OSError('ack disk failure')
   original()
  with patch.object(q,'_save',side_effect=save):await q.enqueue(msg(),'');await q.wait_idle()
  # At least one of the attempted acknowledgements failed; source remains available for retry.
  self.assertGreaterEqual(q.status()['failures'],1);self.assertEqual(q.status()['pending_turns'],1)
 async def test_journal_is_private(self):
  q=self.mem.learner;await q.enqueue(msg(),'');await q.wait_idle();assert_private_file(self,q.path)

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
