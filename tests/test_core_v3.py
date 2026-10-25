"""Contracts for the v3 composition, offline inference boundary and local surface."""
import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock
from src.core.context import ContextManager,approx_tokens,WINDOW_BUDGET,REFERENCE_BUDGET
from src.core.types import WxMessage
from src.store_memory import MemoryStore
from src.core.local_decision import LocalDecisionRunner,load_manifest
from src.core.reply_decision import ReplyDecider
from tools.decision_model.research_decider import ResearchReplyDecider
from src.core.media import ImageLoader
from src.core.llm import LLMGateway
from src.core.companion_models import CompanionModels
from src.adapters.desktop import DesktopAdapter
from src.config_store import ConfigStore,AppConfig

def message(text='这是一段普通的分享',mid='now'):
    return WxMessage(mid,'desktop','private','local','local','我','bot',text)

class Composition(unittest.IsolatedAsyncioTestCase):
    async def test_complete_current_and_persona_are_not_budget_clipped(self):
        persona='完整自定义人格'*2000;current='当前很长的话'*3000
        ctx=ContextManager(MemoryStore(),persona=persona)
        result=await ctx.build(message(current))
        self.assertIn(persona,result[0]['content']);self.assertEqual(result[-1]['content'],current)
    async def test_session_retains_history_until_model_window_ratio(self):
        store=MemoryStore();ctx=ContextManager(store)
        for i in range(50):await store.push(message().ctx_key,{'user':'历史'+str(i)+'中'*180,'assistant':'回'*120,'ts':i})
        ctx.shared_history=lambda _: [{'user':'跨端'*1000,'assistant':'','ts':90}]
        await ctx.build(message(),memory_context='记忆'*200,rag_context='搜索'*10000)
        self.assertGreater(ctx.last_budget['history'],WINDOW_BUDGET)
        self.assertLess(ctx.last_budget['session_tokens_estimated'],ctx.context_window_tokens)
        self.assertLessEqual(ctx.last_budget['references'],REFERENCE_BUDGET)
        self.assertEqual(ctx.last_budget['turns'],51)
    async def test_dynamic_tail_leaves_persona_history_byte_stable(self):
        store=MemoryStore();ctx=ContextManager(store)
        await store.push(message().ctx_key,{'user':'昨天真实来话','assistant':'真实答复','ts':1})
        a=await ctx.build(message(),memory_context='喜欢绿茶');b=await ctx.build(message(),memory_context='喜欢音乐')
        self.assertEqual(a[:3],b[:3]);self.assertEqual(a[-1],b[-1])
    async def test_current_accepted_message_is_not_its_own_last_contact(self):
        ctx=ContextManager(MemoryStore());m=message();await ctx.accept_user(m)
        result=await ctx.build(m)
        self.assertFalse(any('[上次联系]' in str(v['content']) for v in result))
    async def test_shared_source_ids_are_deduplicated(self):
        ctx=ContextManager(MemoryStore());m=message()
        ctx.shared_history=lambda _: [{'user':'重复来话','source_ids':ctx.source_ids(m),'ts':1}]
        result=await ctx.build(m)
        self.assertNotIn('重复来话',str(result))

class DecisionBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_missing_model_never_downloads_or_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            runner=LocalDecisionRunner(d);self.assertFalse(await runner.warmup())
            out=await runner.decide({'current':'分享'});self.assertTrue(out['abstain']);await runner.close()
    async def test_protected_input_does_not_invoke_local_or_cloud(self):
        local=type('Runner',(),{'decide':AsyncMock()})();cloud=type('Gateway',(),{'complete_once':AsyncMock()})()
        d=ResearchReplyDecider(cloud,local_runner=local)
        for text in ['能不能帮我看下','我很难过','不是昨天，是明天','请告诉我原因']:
            self.assertEqual((await d.decide(message(text))).action,'reply')
        local.decide.assert_not_awaited();cloud.complete_once.assert_not_awaited()
    async def test_local_input_is_not_silently_truncated(self):
        local=type('Runner',(),{'decide':AsyncMock(return_value={'abstain':True,'calibrated':False,'reason':'input_too_large'})})()
        text='不要丢失前面的诉求'*500
        await ResearchReplyDecider(local_runner=local).decide(message(text))
        self.assertEqual(local.decide.call_args.args[0]['current'],text)
    async def test_calibrated_runner_policy_is_not_double_thresholded(self):
        value={'action':'context_only','probabilities':{'reply':.02,'defer':.01,'context_only':.97},'abstain':False,'calibrated':True}
        local=type('Runner',(),{'decide':AsyncMock(return_value=value)})()
        self.assertEqual((await ResearchReplyDecider(local_runner=local).decide(message())).action,'context_only')
    def test_unknown_usage_is_not_zero_cache_hits(self):
        self.assertIsNone(LLMGateway._usage(None).cache_hit_tokens)
        self.assertIsNone(LLMGateway._usage(None).cached)
    def test_provider_cache_fields_are_preserved(self):
        raw=type('Usage',(),{'prompt_tokens':20,'completion_tokens':3,'prompt_cache_hit_tokens':16,'prompt_cache_miss_tokens':4})()
        result=LLMGateway._usage(raw)
        self.assertEqual((result.cache_hit_tokens,result.cache_miss_tokens,result.cached),(16,4,True))

class CompanionImport(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.source=self.root/'中文 模型';self.source.mkdir()
        self.manager=CompanionModels(self.root/'data')
        (self.source/'m.moc3').write_bytes(b'test');(self.source/'图 像.png').write_bytes(b'test')
        self.raw={'Version':3,'FileReferences':{'Moc':'m.moc3','Textures':['图 像.png']}}
        (self.source/'不同名称.model3.json').write_text(json.dumps(self.raw),encoding='utf-8')
    def tearDown(self):self.tmp.cleanup()
    def test_import_preserves_independent_manifest_name(self):
        result=self.manager.import_model(self.source)
        self.assertEqual(result['manifest'],'不同名称.model3.json')
        self.assertEqual(self.manager.resource(result['id'],'图 像.png').read_bytes(),b'test')
    def test_model_survives_removal_of_original_source(self):
        result=self.manager.import_model(self.source)
        import shutil;shutil.rmtree(self.source)
        self.assertTrue(self.manager.resource(result['id'],'m.moc3').is_file())
    def test_remote_and_parent_paths_are_rejected(self):
        for ref in ['../secret','https://example.com/a','/etc/passwd','a\\b']:
            self.raw['FileReferences']['Moc']=ref
            (self.source/'不同名称.model3.json').write_text(json.dumps(self.raw))
            with self.assertRaises(ValueError):self.manager.import_model(self.source)
    def test_unlisted_file_cannot_be_served(self):
        result=self.manager.import_model(self.source)
        with self.assertRaises(ValueError):self.manager.resource(result['id'],'import.json')
    def test_model_id_cannot_escape_root(self):
        with self.assertRaises(ValueError):self.manager.describe('../a')
    def test_multiple_manifests_require_explicit_selection(self):
        (self.source/'第二个.model3.json').write_text(json.dumps(self.raw))
        result=self.manager.import_model(self.source);self.assertTrue(result['needs_selection']);self.assertEqual(len(result['manifests']),2)
    def test_symlink_resource_is_rejected(self):
        (self.source/'m.moc3').unlink();(self.root/'outside').write_bytes(b'outside');(self.source/'m.moc3').symlink_to(self.root/'outside')
        with self.assertRaises(ValueError):self.manager.import_model(self.source)

class DesktopTransport(unittest.IsolatedAsyncioTestCase):
    async def test_admission_is_durable_and_duplicate_does_not_reenter(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);adapter.configure_burst(2,10)
            result=await adapter.submit('真实来话','id1');self.assertTrue(result['accepted'])
            again=DesktopAdapter(d);again.configure_burst(2,10)
            self.assertTrue((await again.submit('真实来话','id1'))['duplicate'])
            self.assertEqual(len(again.buffer.pending),1)
    async def test_completed_replays_after_reconnect_but_draft_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);adapter.publish('delta','t',{'text':'草稿'})
            await adapter.send_text('local','完整正文')
            again=DesktopAdapter(d);self.assertEqual([e['type'] for e in again.snapshot()],['completed'])
    async def test_desktop_never_targets_a_real_channel_recipient(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d)
            with self.assertRaises(ValueError):await adapter.send_text('p_123','wrong')
    def test_config_unknown_fields_and_paths_survive(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json';raw={'plugin':{'secret':'keep'},'bot':{'unknown':'keep','persona':'完整人格'},'adapter':{'ilink_state_dir':'/custom/login'}}
            p.write_text(json.dumps(raw));store=ConfigStore(p);cfg=store.load();store.save(cfg);saved=json.loads(p.read_text())
            self.assertEqual(saved['plugin'],raw['plugin']);self.assertEqual(saved['bot']['unknown'],'keep')
            self.assertEqual(saved['adapter']['ilink_state_dir'],'/custom/login')
            self.assertNotIn('plugin',store.masked(cfg))
    def test_corrupt_config_is_preserved_for_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json';p.write_text('broken')
            with self.assertRaises(ValueError):ConfigStore(p).load()
            self.assertEqual(p.read_text(),'broken')

if __name__=='__main__':unittest.main()
