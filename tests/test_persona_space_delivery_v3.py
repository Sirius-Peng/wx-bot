"""Live mode changes invalidate drafts, never move a submitted body's history."""
import asyncio,copy,tempfile,unittest
from pathlib import Path
from src.config_store import AppConfig,ConfigStore
from src.runtime import BotRuntime
from src.adapters.desktop import DesktopAdapter
from src.core.types import WxMessage
from src.core.conversation import ConversationChanged

class DeliverySpaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cfg=AppConfig();self.cfg.bot.memory_enabled=False;self.cfg.bot.reply_mode='always';self.cfg.bot.typing_indicator=False
        self.cfg.llm.primary_key='fixture-only';self.cfg.llm.search_enabled=False;self.cfg.companion.enabled=True
        self.store=ConfigStore(self.root/'config.json');self.store.save(self.cfg)
        self.rt=BotRuntime(self.store,self.root);self.adapter=DesktopAdapter(self.root)
    async def asyncTearDown(self):
        await self.rt.stop();self.tmp.cleanup()
    def incoming(self,mid='one',text='你好，帮我看看'):
        values=dict(msg_id=mid,platform='desktop',chat_type='private',chat_id='local',sender_id='local',sender_name='我',bot_id='bot',content=text)
        if 'persona_space' in WxMessage.__dataclass_fields__:values['persona_space']=self.rt.persona_spaces.active_key
        return WxMessage(**values)
    def build(self,gateway):
        orch=self.rt._build_orchestrator(self.adapter,'desktop');orch.llm=gateway
        self.rt.desktop_orch=orch;return orch
    def switch(self):
        cfg=copy.deepcopy(self.rt.cfg);cfg.bot.persona_relationship='trusted';self.store.save(cfg);self.rt.apply_config(cfg)
    async def test_switch_during_generation_invalidates_unsent_draft(self):
        entered=asyncio.Event()
        class Gateway:
            async def stream(self,*a,**k):
                entered.set();await asyncio.Event().wait();yield 'old'
            async def aclose(self):pass
        orch=self.build(Gateway());message=self.incoming();oldctx=orch.ctx
        task=asyncio.create_task(orch.handle(message));await asyncio.wait_for(entered.wait(),2)
        self.switch()
        with self.assertRaises(ConversationChanged):await asyncio.wait_for(task,2)
        self.assertFalse(any(e['type']=='completed' for e in self.adapter.events))
        self.assertEqual(await orch.store.get_recent(message.ctx_key),[])
        old=await oldctx.store.get_recent(message.ctx_key);self.assertEqual(old[0]['assistant'],'')
    async def test_switch_after_submission_keeps_original_body_and_old_context(self):
        entered=asyncio.Event();release=asyncio.Event();sent=[]
        original=self.adapter.send_text
        async def send(chat,text,at=None):
            entered.set();await release.wait();sent.append(text);return await original(chat,text,at)
        self.adapter.send_text=send
        class Gateway:
            async def stream(self,*a,**k):yield '旧空间完整正文。'
            async def aclose(self):pass
        orch=self.build(Gateway());message=self.incoming();oldctx=orch.ctx;oldspace=orch.persona_space
        task=asyncio.create_task(orch.handle(message));await asyncio.wait_for(entered.wait(),2)
        self.switch();release.set();await asyncio.wait_for(task,2)
        self.assertEqual(sent,['旧空间完整正文。'])
        self.assertEqual(await orch.store.get_recent(message.ctx_key),[])
        old=await oldctx.store.get_recent(message.ctx_key);self.assertEqual(old[0]['assistant'],'旧空间完整正文。')
        events=[e for e in self.adapter.events if e['type']=='completed']
        self.assertEqual(len(events),1)
        if 'persona_space' in events[0]:self.assertEqual(events[0]['persona_space'],oldspace)
    async def test_inactive_queued_input_is_kept_only_in_original_context_without_generation(self):
        class Gateway:
            async def stream(self,*a,**k):raise AssertionError('old queued input must not call LLM');yield ''
            async def aclose(self):pass
        orch=self.build(Gateway());message=self.incoming();oldspace=orch.persona_space
        self.switch()
        if 'persona_space' not in WxMessage.__dataclass_fields__:self.skipTest('DTO integration not yet present')
        await orch.handle(message)
        self.assertEqual(await orch.store.get_recent(message.ctx_key),[])
        old=await orch._persona_contexts[oldspace].store.get_recent(message.ctx_key)
        self.assertEqual(old[0]['user'],message.content)
        self.assertEqual(old[0]['assistant'],'')

if __name__=='__main__':unittest.main()
