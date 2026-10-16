"""Persona profile, retrieval, runtime and corpus regression tests (offline)."""
import copy, importlib, json, os
from collections import Counter
from pathlib import Path
import sys, tempfile, unittest
from unittest.mock import patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from src.config_store import AppConfig,ConfigStore
from src.core.context import ContextManager,build_system_prompt
from src.core.personality import detect_scene,few_shot_messages,profile_prompt,public_profiles
from src.personas.phrolova import EXAMPLES,CORE_PROMPT
from src.core.types import WxMessage
from src.store_memory import MemoryStore
from src.runtime import BotRuntime
from tools.export_persona import training_rows

class PersonaTests(unittest.TestCase):
 def test_new_defaults(self):
  b=AppConfig().bot;self.assertEqual((b.name,b.persona_profile,b.persona_relationship),('弗洛洛','phrolova','neutral'))
 def test_legacy_custom_not_overwritten(self):
  b=ConfigStore(Path('/tmp/not-loaded-persona-config.json'))._from_dict({'bot':{'name':'猫','persona':'小猫'}}).bot
  self.assertEqual((b.name,b.persona,b.persona_profile),('猫','小猫','custom'))
 def test_missing_bot_uses_preset(self):self.assertEqual(ConfigStore(Path('/tmp/not-loaded-persona-config.json'))._from_dict({}).bot.persona_profile,'phrolova')
 def test_invalid_ids(self):
  for x in ['unknown',None,[],{}]:
   b=ConfigStore(Path('/tmp/not-loaded-persona-config.json'))._from_dict({'bot':{'persona_profile':x,'persona_relationship':x}}).bot
   self.assertEqual((b.persona_profile,b.persona_relationship),('custom','neutral'))
 def test_profile_roundtrip(self):
  with tempfile.TemporaryDirectory() as d:
   store=ConfigStore(Path(d)/'config.json');cfg=AppConfig();cfg.bot.persona_relationship='trusted';store.save(cfg)
   self.assertEqual(store.load().bot.persona_relationship,'trusted')
 def test_core_and_relation(self):
  s=build_system_prompt('弗洛洛','少说表情','phrolova','neutral');self.assertIn(CORE_PROMPT,s);self.assertIn('当前关系：普通聊天',s);self.assertIn('少说表情',s)
 def test_custom_no_preset(self):
  self.assertNotIn(CORE_PROMPT,build_system_prompt('猫','像猫','custom'));self.assertEqual(few_shot_messages('custom','你好'),[])
 def test_relationships_distinct(self):
  for mode,text in [('neutral','普通聊天'),('trusted','信任日常'),('rover','漂泊者剧情扮演')]:self.assertIn('当前关系：'+text,profile_prompt('phrolova',mode))
 def test_scene_detection(self):
  for q,s in [('Python报错','technical'),('游戏战斗开场','combat'),('赫卡忒是什么','identity'),('无辜的人','protection'),('未经允许拿东西','boundary'),('一起合作','cooperation'),('失约之后','promise'),('伤痕打乱计划','cooperation'),('我很难过','vulnerability'),('听音乐','music')]:self.assertEqual(detect_scene(q),s)
 def test_retrieval_has_technical_examples(self):
  shots=few_shot_messages('phrolova','电脑报错了');self.assertIn('technical',shots[0]['content']);self.assertTrue(any('电脑' in s['content'] for s in shots))
 def test_fewshot_bounded_and_labelled(self):
  s=few_shot_messages('phrolova','你好',limit=100);self.assertEqual([x['role'] for x in s],['system','user','assistant','user','assistant']);self.assertIn('不是当前用户真实经历',s[0]['content'])
 def test_rover_not_selected_for_neutral(self):
  s=few_shot_messages('phrolova','我是漂泊者，我们在失约之后见面。','neutral');self.assertFalse(any(x['content']=='我是漂泊者，我们在失约之后见面。' for x in s))
 def test_data_distribution_and_provenance(self):
  rows=EXAMPLES
  self.assertEqual(Counter(r['group'] for r in rows),{'daily':10,'intimacy':30,'plot':40,'anchor':15,'combat':5})
  self.assertEqual(len({r['id'] for r in rows}),100);self.assertTrue(all(not r['is_official_quote'] and r['provenance']=='synthetic_adaptation' for r in rows))
 def test_training_format(self):
  rows=list(training_rows());self.assertEqual(len(rows),100)
  self.assertTrue(all([m['role'] for m in r['messages']]==['system','user','assistant'] for r in rows))
 def test_eval_heldout(self):
  rows=[json.loads(s) for s in (ROOT/'personas/phrolova/eval.jsonl').read_text().splitlines()];self.assertEqual(len(rows),24)
  self.assertTrue(all(r['user'] not in {e['user'] for e in EXAMPLES} for r in rows))
 def test_public_metadata(self):
  p=public_profiles()[1];self.assertEqual(p['id'],'phrolova');self.assertEqual(set(p['relationships']),{'neutral','trusted','rover'});self.assertIn('权重未修改',p['method'])
 def test_judge_default_follows_primary(self):
  cfg=AppConfig();cfg.llm.primary_base='http://primary';cfg.llm.primary_key='test-key';cfg.llm.primary_model='model';p=BotRuntime._judge_provider(cfg)
  self.assertEqual((p.base_url,p.api_key,p.model),('http://primary','test-key','model'))
 def test_judge_override(self):
  cfg=AppConfig();cfg.llm.judge_key='judge';cfg.llm.judge_base='http://judge';cfg.llm.judge_model='small';p=BotRuntime._judge_provider(cfg)
  self.assertEqual((p.base_url,p.api_key,p.model),('http://judge','judge','small'))

class PersonaAsyncTests(unittest.IsolatedAsyncioTestCase):
 async def test_context_real_user_last_and_memory_label(self):
  ctx=ContextManager(MemoryStore(),bot_name='弗洛洛',persona_profile='phrolova')
  msg=WxMessage('1','onebot','private','p_u','u','用户','bot','请帮我检查Python报错')
  messages=await ctx.build(msg,memory_context='用户喜欢阅读')
  self.assertEqual(messages[-1],{'role':'user','content':msg.render()});self.assertTrue(any('不是角色官设' in m['content'] and m['role']!='system' for m in messages));self.assertIn('弗洛洛',messages[0]['content']);self.assertTrue(any('technical' in m['content'] for m in messages if m['role']=='system'))
 async def test_live_mode_update(self):
  class Adapter:
   platform="fake"
   def on_message(self,fn):pass
   async def send_text(self,*a,**k):return "sent"
  with tempfile.TemporaryDirectory() as d:
   store=ConfigStore(Path(d)/'config.json');cfg=AppConfig();store.save(cfg);rt=BotRuntime(store,Path(d));orch=rt._build_orchestrator(Adapter());rt.qq_orch=orch
   cfg=copy.deepcopy(cfg);cfg.bot.persona_relationship='rover';self.assertFalse(rt.apply_config(cfg));self.assertEqual(orch.ctx.persona_relationship,'rover');self.assertIn('当前关系：漂泊者剧情扮演',orch.ctx.system_prompt)
   await orch.llm.aclose();await rt.memory.llm.aclose()
 async def test_profiles_endpoint_authenticated(self):
  import httpx
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'WXBOT_DATA_DIR':d,'WXBOT_ADMIN_TOKEN':'persona-test','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'}):
   import src.server as server;server=importlib.reload(server)
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost') as c:
    self.assertEqual((await c.get('/api/persona/profiles')).status_code,401)
    r=await c.get('/api/persona/profiles',headers={'Authorization':'Bearer persona-test'});self.assertEqual(r.status_code,200);self.assertEqual(r.json()['profiles'][1]['id'],'phrolova')

if __name__=='__main__':unittest.main()
