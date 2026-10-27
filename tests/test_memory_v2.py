"""Local deterministic conversations; no provider keys, real chats or network."""
import asyncio, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from src.core.memory import PersonaMemory, MemoryExtractionError
from src.core.context import ContextManager, LLMCompactor, WINDOW_BUDGET, approx_tokens
from src.core.types import WxMessage
from src.store_memory import MemoryStore
from src.config_store import ConfigStore, AppConfig
from src.runtime import BotRuntime, MessageLog
from file_privacy import assert_private_file

class Model:
    def __init__(self, entries): self.entries=entries;self.prompts=[]
    async def complete(self, prompt, **kwargs): self.prompts.append(prompt);return json.dumps(self.entries,ensure_ascii=False)

def message(platform='ilink', uid='wx_alice', content='我是工程师', mid='1'):
    return WxMessage(mid,platform,'private',uid,uid,'Alice','bot',content)

class MemoryV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'memory.json';self.m=PersonaMemory(self.path)
    async def asyncTearDown(self): await self.m.learner.close();self.tmp.cleanup()
    def add(self, content, platform='ilink', uid='wx_alice', key='occupation', category='fact', keywords=None, expires=0):
        now=time.time();return self.m._merge_or_create('user',uid,category,content,keywords or [],'chat',platform=platform,fact_key=key,expires_at=expires,sources=[{'id':content+':first','episode_id':'first','observed_at':now-86401,'text':content},{'id':content+':second','episode_id':'second','observed_at':now,'text':content}])
    async def test_schema_version_is_three(self):
        self.m.add_manual('user','u','fact','事实');self.assertEqual(json.loads(self.path.read_text())['version'],3)
    async def test_current_job_supersedes_not_erases_old_job(self):
        old=self.add('用户是后端工程师');new=self.add('用户现在是教师')
        self.assertEqual(old.status,'superseded');self.assertEqual(old.superseded_by,new.id);self.assertGreater(old.valid_until,0)
        self.assertEqual([i.id for i in self.m.recall('wx_alice','chat','现在的职业',platform='ilink')],[new.id])
    async def test_historical_query_recalls_old_job(self):
        old=self.add('用户是工程师');self.add('用户现在是老师')
        self.assertIn(old.id,[i.id for i in self.m.recall('wx_alice','chat','以前的职业',platform='ilink')])
        self.assertIn('历史信息，已被更新',self.m.build_context('wx_alice','chat','以前职业','ilink'))
    async def test_shared_keyword_does_not_replace_distinct_facts(self):
        self.add('用户养了一只猫',key='',keywords=['猫']);self.add('用户收藏猫玩偶',key='',keywords=['猫']);self.assertEqual(self.m.stats()['total'],2)
    async def test_mentions_of_teacher_do_not_change_occupation(self):
        self.add('用户是工程师',key='');self.add('用户的朋友是一名老师',key='');self.assertEqual(self.m.stats()['total'],2)
    async def test_generic_preference_slot_does_not_merge_different_preferences(self):
        self.add('喜欢猫',category='preference',key='preference');self.add('喜欢咖啡',category='preference',key='preference');self.assertEqual(self.m.stats()['total'],2)
    async def test_duplicate_reinforces_same_item(self):
        a=self.add('用户是工程师');b=self.add('用户是工程师');self.assertIs(a,b);self.assertEqual(len(self.m._items),1);self.assertEqual(a.confidence,.85);self.assertEqual(len(a.evidence_sources),2)
    async def test_irrelevant_facts_not_injected(self):
        self.add('用户是工程师');self.assertEqual(self.m.build_context('wx_alice','chat','今天天气晴朗','ilink'),'')
    async def test_style_not_injected_without_relevance(self):
        a=self.add('称呼用户为小雨',key='preferred_name',category='style');self.assertNotIn(a,self.m.recall('wx_alice','chat','早安',platform='ilink'))
    async def test_different_users_isolated(self):
        self.add('用户是工程师');self.assertEqual(self.m.recall('bob','chat','职业',platform='ilink'),[])
    async def test_same_number_across_platforms_not_auto_linked(self):
        self.add('用户是工程师',uid='123');self.assertEqual(self.m.recall('123','chat','职业',platform='onebot'),[])
    async def test_link_shares_wechat_fact_to_qq(self):
        a=self.add('用户是工程师');self.m.link_accounts('wx_alice','123');self.assertIn(a,self.m.recall('123','p_123','职业',platform='onebot'))
    async def test_link_shares_qq_preference_to_wechat(self):
        a=self.add('用户喜欢绿茶',platform='onebot',uid='123',key='preference:tea',category='preference',keywords=['茶'])
        self.m.link_accounts('wx_alice','123');self.assertIn(a,self.m.recall('wx_alice','chat','喝什么茶',platform='ilink'))
    async def test_cross_platform_update_replaces_current_but_preserves_origin(self):
        a=self.add('用户是工程师');self.m.link_accounts('wx_alice','123');b=self.add('用户现在是教师',platform='onebot',uid='123')
        self.assertEqual(a.status,'superseded');self.assertEqual(a.platform,'ilink');self.assertEqual(b.owner_id,'123')
        self.assertEqual(self.m.recall('wx_alice','chat','职业',platform='ilink'),[b])
    async def test_link_existing_conflicting_facts_picks_latest(self):
        a=self.add('用户是工程师');b=self.add('用户是教师',platform='onebot',uid='123');self.m.link_accounts('wx_alice','123')
        self.assertEqual(a.status,'superseded');self.assertEqual(b.status,'active')
    async def test_unlink_restores_per_account_versions(self):
        a=self.add('用户是工程师');self.m.link_accounts('wx_alice','123');b=self.add('用户是教师',platform='onebot',uid='123')
        identity=self.m.identities[0];self.assertTrue(self.m.unlink_accounts(identity['id']));self.assertEqual(a.status,'active');self.assertEqual(b.status,'active')
        self.assertEqual(self.m.recall('wx_alice','chat','职业',platform='ilink'),[a])
    async def test_linked_memory_is_injected_into_actual_qq_reply(self):
        from src.orchestrator import Orchestrator
        from src.core.trigger import TriggerRouter
        self.add('用户是工程师');self.m.link_accounts('wx_alice','123');self.m.enabled=False
        class Adapter:
            platform='onebot'
            def on_message(self,h):self.handler=h
        class Chat:
            async def stream(self,messages,**kw):self.messages=messages;yield '记得，你是工程师。'
        class Sender:
            async def stream_send(self,c,stream,**kw):self.reply=''.join([x async for x in stream]);return self.reply
        store=MemoryStore();chat=Chat();sender=Sender();o=Orchestrator(Adapter(),store,chat,None,ContextManager(store),TriggerRouter(),sender,memory=self.m)
        await o.handle(message('onebot','123','你还记得我的职业吗'))
        self.assertTrue(any('用户是工程师' in x['content'] for x in chat.messages if x['role']!='system'));self.assertIn('工程师',sender.reply);await o.close()
    async def test_link_persists_restart(self):
        self.add('用户是工程师');self.m.link_accounts('wx_alice','123');loaded=PersonaMemory(self.path)
        self.assertEqual(loaded.identity_key('ilink','wx_alice'),loaded.identity_key('onebot','123'));self.assertEqual(len(loaded.recall('123','p_123','职业',platform='onebot')),1)
    async def test_link_does_not_share_to_third_user(self):
        self.add('用户是工程师');self.m.link_accounts('wx_alice','123');self.assertEqual(self.m.recall('456','chat','职业',platform='onebot'),[])
    async def test_link_does_not_mix_group_scopes(self):
        a=self.m._merge_or_create('group','g_1','fact','本群规则',['规则'],'g_1',platform='onebot');self.m.link_accounts('wx_alice','123')
        self.assertNotIn(a,self.m.recall('wx_alice','g_1','规则',platform='ilink'))
    async def test_global_manual_memory_shared_by_both_platforms(self):
        self.m.add_manual('user','global','style','回复保持简洁')
        for platform in ('ilink','onebot'):self.assertIn('简洁',self.m.build_context('any','any','简洁',platform))
    async def test_blank_or_global_account_rejected(self):
        for wx,qq in [('', '123'),('wx',''),('global','123'),('wx','*')]:
            with self.assertRaises(ValueError):self.m.link_accounts(wx,qq)
    async def test_link_duplicate_is_idempotent(self):
        first=self.m.link_accounts('wx','123');second=self.m.link_accounts('wx','123');self.assertEqual(first['id'],second['id']);self.assertEqual(len(self.m.identities),1)
    async def test_link_save_failure_restores_in_memory_state(self):
        a=self.add('用户是工程师');self.add('用户是教师',platform='onebot',uid='123')
        with patch.object(self.m,'_save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.m.link_accounts('wx_alice','123')
        self.assertEqual(self.m.identities,[]);self.assertEqual(self.m._items[a.id].status,'active')
    async def test_expired_event_only_recalled_as_history(self):
        a=self.add('上个月安排了音乐会',key='event:concert',category='event',keywords=['音乐会'],expires=time.time()-1)
        self.assertEqual(self.m.recall('wx_alice','chat','音乐会',platform='ilink'),[])
        self.assertIn(a,self.m.recall('wx_alice','chat','以前的音乐会',platform='ilink'));self.assertIn('已过期',self.m.build_context('wx_alice','chat','以前的音乐会','ilink'))
    async def test_ui_history_toggle_lists_both_versions(self):
        self.add('用户是工程师');self.add('用户是老师');self.assertEqual(len(self.m.list_items()),1);self.assertEqual(len(self.m.list_items(include_history=True)),2)
    async def test_corrupt_memory_is_not_silently_overwritten(self):
        self.path.write_text('{corrupt fixture')
        with self.assertRaises(ValueError):PersonaMemory(self.path)
        self.assertEqual(self.path.read_text(),'{corrupt fixture')
    async def test_migration_preserves_original_backup(self):
        raw={'version':1,'items':[]};self.path.write_text(json.dumps(raw));loaded=PersonaMemory(self.path)
        self.assertEqual(json.loads(self.path.with_name('memory.v1-backup.json').read_text()),raw);assert_private_file(self,self.path.with_name('memory.v1-backup.json'))
    async def test_extraction_stores_platform_and_verbatim_evidence(self):
        self.m.llm=Model([{'category':'fact','content':'用户是工程师','keywords':['职业'],'fact_key':'occupation','evidence':'我是工程师'}])
        await self.m.observe('chat','private','wx_alice','Alice',[{'user':'我是工程师'}],platform='ilink');item=next(iter(self.m._items.values()));self.assertEqual(item.platform,'ilink');self.assertEqual(item.evidence,'我是工程师')
    async def test_cross_platform_correction_immediately_disables_old_but_new_value_is_candidate(self):
        old=self.add('用户是工程师');self.m.link_accounts('wx_alice','123')
        self.m.llm=Model([{'category':'fact','content':'用户现在是教师','keywords':['职业'],'fact_key':'occupation','evidence':'我现在是教师'}])
        self.m.observe_correction(message('onebot','123','我现在是教师','2'))
        self.assertEqual(self.m.recall('wx_alice','chat','现在职业',platform='ilink'),[])
        await self.m.observe('p_123','private','123','Alice',[{'user':'我现在是教师'}],platform='onebot')
        self.assertEqual(old.status,'superseded')
        self.assertEqual(self.m.recall('wx_alice','chat','现在职业',platform='ilink'),[])
        self.assertEqual(next(i for i in self.m._items.values() if i.id != old.id).phase,'candidate')
    async def test_fabricated_evidence_rejected(self):
        self.m.llm=Model([{'category':'fact','content':'用户是医生','keywords':[],'evidence':'我是医生'}])
        with self.assertRaises(MemoryExtractionError):await self.m.observe('chat','private','u','Alice',[{'user':'我是工程师'}],platform='ilink')
        self.assertEqual(self.m.stats()['total'],0)
    async def test_late_old_extraction_does_not_overwrite_newer_cross_channel_fact(self):
        self.m.link_accounts('wx_alice','123')
        self.m._merge_or_create('user','123','fact','用户现在是教师',['职业'],'p_123',platform='onebot',fact_key='occupation',observed_at=200)
        old=self.m._merge_or_create('user','wx_alice','fact','用户之前是工程师',['职业'],'wx',platform='ilink',fact_key='occupation',observed_at=100)
        self.assertEqual(old.status,'superseded');self.assertIn('教师',self.m.build_context('wx_alice','chat','以前职业','ilink'))
    async def test_unrelated_user_delete_does_not_drop_inflight_learning(self):
        other=self.add('用户喜欢猫',uid='bob',key='',category='preference');entered=asyncio.Event();gate=asyncio.Event()
        class Slow:
            async def complete(self,*a,**k):entered.set();await gate.wait();return '[{"category":"fact","content":"用户是工程师","keywords":[]}]'
        self.m.llm=Slow();task=asyncio.create_task(self.m.observe('chat','private','wx_alice','Alice',[{'user':'我是工程师'}],platform='ilink'))
        await entered.wait();self.m.delete(other.id);gate.set();self.assertEqual(len(await task),1)
    async def test_delete_while_extraction_waits_does_not_revive_fact(self):
        item=self.add('用户是工程师');entered=asyncio.Event();gate=asyncio.Event()
        class Slow:
            async def complete(self,*a,**k):entered.set();await gate.wait();return '[{"category":"fact","content":"用户是工程师","keywords":[]}]'
        self.m.llm=Slow();task=asyncio.create_task(self.m.observe('chat','private','wx_alice','Alice',[{'user':'我是工程师'}],platform='ilink'))
        await entered.wait();self.m.delete(item.id);gate.set();self.assertEqual(await task,[]);self.assertEqual(self.m.stats()['total'],0)
    async def test_injection_budget_bounded(self):
        for i in range(10):self.add('用户喜欢音乐'+str(i)+'。'*190,key='preference:'+str(i),category='preference',keywords=['音乐'])
        self.assertLessEqual(len(self.m.build_context('wx_alice','chat','音乐','ilink')),860)
    async def test_memory_file_private(self):
        self.m.link_accounts('wx','123');assert_private_file(self,self.path)

class PersistentContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'context.json';self.store=MemoryStore(path=self.path);self.ctx=ContextManager(self.store)
    async def asyncTearDown(self):await self.ctx.close();self.tmp.cleanup()
    async def test_complete_turns_survive_restart(self):
        m=message();await self.ctx.append(m,'记住了');loaded=MemoryStore(path=self.path);self.assertEqual((await loaded.get_recent(m.ctx_key))[0]['assistant'],'记住了');assert_private_file(self,self.path)
    async def test_same_chat_id_different_platforms_not_same_context(self):self.assertNotEqual(message().ctx_key,message('onebot').ctx_key)
    async def test_duplicate_append_is_idempotent(self):
        m=message();await self.ctx.append(m,'你好');await self.ctx.append(m,'你好');self.assertEqual(len(await self.store.get_recent(m.ctx_key)),1)
    async def test_raw_turns_not_dropped_at_old_window_limit(self):
        for i in range(45):await self.store.push('k',{'user':str(i),'assistant':'a'})
        self.assertEqual(len(await MemoryStore(path=self.path).get_recent('k')),45)
    async def test_write_failure_rolls_back(self):
        with patch.object(self.store,'_save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.store.push('k',{'user':'u','assistant':'a'})
        self.assertEqual(await self.store.get_recent('k'),[])
    async def test_compaction_failure_retains_all_originals(self):
        class Broken:
            async def summarize(self,t):raise RuntimeError('provider')
        self.ctx.compactor=Broken()
        for i in range(10):await self.store.push('k',{'user':str(i),'assistant':'a'})
        original=await self.store.get_recent('k');await self.ctx._compact('k');self.assertEqual(await self.store.get_recent('k'),original)
    async def test_compaction_keeps_latest_six_and_prior_summary(self):
        class Summarizer:
            async def summarize(self,t):self.turns=t;return '旧的未完成约定；最近仍在聊工作'
        summarizer=Summarizer();self.ctx.compactor=summarizer;await self.store.set_summary('k','旧的未完成约定')
        for i in range(10):await self.store.push('k',{'user':str(i),'assistant':'a'})
        await self.ctx._compact('k');self.assertEqual(len(await self.store.get_recent('k')),6);self.assertIn('旧的未完成约定',summarizer.turns[0]['user']);self.assertIn('约定',await self.store.get_summary('k'))
    async def test_compact_prefix_guard(self):
        await self.store.push('k',{'user':'real','assistant':'a'});self.assertFalse(await self.store.compact('k',[{'user':'wrong','assistant':'a'}],'bad'));self.assertEqual(await self.store.get_summary('k'),'')
    async def test_compact_disk_failure_retains_original_and_summary(self):
        original={'user':'real','assistant':'a'};await self.store.push('k',original)
        with patch.object(self.store,'_save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.store.compact('k',[original],'s')
        self.assertEqual(await self.store.get_recent('k'),[original]);self.assertEqual(await self.store.get_summary('k'),'')
    async def test_single_oversized_turn_not_all_discarded(self):
        turns=self.ctx._fit_budget([{'user':'中'*15000,'assistant':'回'*10000}],WINDOW_BUDGET)
        self.assertEqual([t['role'] for t in turns],['system','user','assistant'])
        self.assertIn('未知',turns[0]['content'])
        self.assertTrue(all('省略' in t['content'] for t in turns[1:]))
        self.assertLess(sum(approx_tokens(t['content']) for t in turns),WINDOW_BUDGET)
    async def test_context_has_current_time_and_last_contact(self):
        m=message();await self.ctx.append(m,'你好');m.msg_id='new-turn';messages=await self.ctx.build(m)
        self.assertTrue(any('[会话时间]' in x['content'] for x in messages));self.assertTrue(any('[上次联系]' in x['content'] for x in messages));self.assertEqual(messages[-1]['role'],'user')
    async def test_both_runtime_channels_share_one_long_memory_instance(self):
        class Adapter:
            def __init__(self,platform):self.platform=platform
            def on_message(self,h):self.handler=h
            async def send_text(self,*a,**k):return 'sent'
        config=ConfigStore(self.path.with_name('config.json'));config.save(AppConfig());rt=BotRuntime(config,self.path.parent)
        a=rt._build_orchestrator(Adapter('ilink'),'wechat');b=rt._build_orchestrator(Adapter('onebot'),'qq')
        self.assertIs(a.memory,b.memory);self.assertIsNot(a.store,b.store)
        await a.close();await b.close();await rt.stop()
    async def test_logs_expose_actual_sender_id_for_linking(self):
        logs=MessageLog(self.path.with_name('messages.jsonl'));logs.add_in(message(),platform='wechat');self.assertEqual(logs.query()[0]['sender_id'],'wx_alice')


class MemoryIdentityAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import importlib, httpx
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'fixture-token','WXBOT_SKIP_NAPCAT_SYNC':'1','WXBOT_HOST':'127.0.0.1'});self.env.start()
        import src.server as server
        self.server=importlib.reload(server);self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),base_url='http://127.0.0.1');self.headers={'Authorization':'Bearer fixture-token'}
    async def asyncTearDown(self):await self.client.aclose();await self.server.runtime.stop();self.env.stop();self.tmp.cleanup()
    async def test_identity_mutation_requires_auth(self):
        r=await self.client.post('/api/memory/identities',json={'wechat_id':'wx','qq_id':'123'});self.assertEqual(r.status_code,401)
    async def test_link_api_and_list_returns_persistent_identity(self):
        r=await self.client.post('/api/memory/identities',headers=self.headers,json={'wechat_id':'wx','qq_id':'123'});self.assertEqual(r.status_code,200)
        data=(await self.client.get('/api/memory',headers=self.headers)).json();self.assertEqual(data['identities'][0]['accounts'][1]['owner_id'],'123')
    async def test_unlink_api(self):
        data=(await self.client.post('/api/memory/identities',headers=self.headers,json={'wechat_id':'wx','qq_id':'123'})).json()
        r=await self.client.delete('/api/memory/identities/'+data['identity']['id'],headers=self.headers);self.assertTrue(r.json()['ok']);self.assertEqual(self.server.runtime.memory.identities,[])
    async def test_empty_id_is_validation_error(self):
        r=await self.client.post('/api/memory/identities',headers=self.headers,json={'wechat_id':'','qq_id':'123'});self.assertEqual(r.status_code,400)
    async def test_history_query_parameter(self):
        m=self.server.runtime._ensure_memory();m.add_manual('user','u','fact','用户是工程师');m.add_manual('user','u','fact','用户是教师')
        current=(await self.client.get('/api/memory',headers=self.headers)).json();all_items=(await self.client.get('/api/memory?include_history=true',headers=self.headers)).json()
        self.assertEqual(len(current['items']),1);self.assertEqual(len(all_items['items']),2)

if __name__=='__main__':unittest.main()
