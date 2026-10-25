"""Persona/relationship spaces isolate content, not transport clocks or claims."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from src.config_store import AppConfig
from src.core.conversation import ConversationChanged
from src.core.proactive import ProactivePlanner
from src.core.reply_decision import ReplyDecision
from src.core.turn_coordinator import TurnCoordinator
from src.core.types import WxMessage


def msg(mid='1',text='这是一段真实来话',platform='ilink',owner='wx'):
    return WxMessage(mid,platform,'private','p_'+owner if platform=='onebot' else owner,
                     owner,owner,'bot',text,context_token='test-token' if platform=='ilink' else None)


class ProactiveSpaces(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'proactive.json'
        self.space=['profile-a--neutral'];self.now=[1792036800.]
        self.cfg=AppConfig();self.cfg.bot.proactive_enabled=True
        self.cfg.bot.proactive_min_hours=self.cfg.bot.proactive_max_hours=.25
        self.cfg.bot.proactive_quiet_start=self.cfg.bot.proactive_quiet_end=0
        self.generate=AsyncMock(return_value='自然简短的联系。');self.deliver=AsyncMock()
        self.planner=self.make()

    def make(self):
        p=ProactivePlanner(self.path,lambda p,o:'same-person',lambda:self.cfg,
                           lambda rows,now:rows[0],self.generate,self.deliver,
                           clock=lambda:self.now[0],uniform=lambda a,b:a)
        p.active_space=lambda:self.space[0];p.legacy_space='profile-a--neutral'
        return p

    async def asyncTearDown(self):await self.planner.close();self.tmp.cleanup()

    async def make_due(self):
        self.planner.note_in(msg());self.planner.set_contact('ilink:wx',True)
        await self.planner.tick();self.now[0]+=901

    async def test_tagged_history_and_shared_turns_only_use_current_space(self):
        p=self.planner;p.note_in(msg(text='A空间微信原话'))
        p.note_out('ilink','wx','A空间答复')
        p.note_in(msg('q1','A空间QQ原话','onebot','123'));p.note_out('onebot','p_123','A空间QQ答复')
        self.space[0]='profile-b--close';self.now[0]+=10
        p.note_in(msg('q2','B空间QQ原话','onebot','123'));p.note_out('onebot','p_123','B空间QQ答复')
        text=json.dumps(p.timeline(list(p.accounts.values())),ensure_ascii=False)
        self.assertIn('B空间QQ原话',text);self.assertNotIn('A空间',text)
        turns=p.shared_turns(msg());self.assertEqual(len(turns),1)
        self.assertEqual((turns[0]['user'],turns[0]['assistant']),('B空间QQ原话','B空间QQ答复'))
        self.space[0]='profile-a--neutral'
        self.assertIn('A空间微信原话',json.dumps(p.timeline(list(p.accounts.values())),ensure_ascii=False))
        self.assertNotIn('B空间',json.dumps(p.shared_turns(msg()),ensure_ascii=False))

    async def test_untagged_history_belongs_only_to_legacy_space(self):
        p=self.planner;p.note_in(msg(text='升级前的真实来话'))
        p.accounts['ilink:wx']['history'][0].pop('persona_space');p._save()
        restored=self.make()
        try:
            self.assertIn('升级前',str(restored.timeline(list(restored.accounts.values()))))
            self.space[0]='new-preset--neutral'
            self.assertEqual(restored.timeline(list(restored.accounts.values())),[])
            self.assertEqual(restored.shared_turns(msg(platform='desktop',owner='local')),[])
        finally:await restored.close()

    async def test_transport_clocks_contact_consent_and_reservation_stay_global(self):
        p=self.planner;p.note_in(msg());p.set_contact('ilink:wx',True)
        row=p.accounts['ilink:wx'];row['awaiting']=True
        row['attempts']=[{'id':'accepted-old','ts':self.now[0],'status':'accepted'}]
        self.space[0]='new-preset--neutral';self.now[0]+=20
        p.note_in(msg('2','新空间来话'));p.note_out('ilink','wx','新空间回复')
        self.assertTrue(row['enabled']);self.assertFalse(row['awaiting'])
        self.assertEqual(row['last_in'],self.now[0]);self.assertEqual(row['last_out'],self.now[0])
        self.assertEqual(row['attempts'],[{'id':'accepted-old','ts':self.now[0]-20,'status':'accepted'}])
        self.assertEqual(len(p.groups()),1)

    async def test_space_change_invalidates_unsigned_proactive_draft(self):
        await self.make_due();entered=asyncio.Event();cancelled=asyncio.Event()
        async def slow(*a):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.planner.generate=slow
        task=asyncio.create_task(self.planner.tick());await asyncio.wait_for(entered.wait(),1)
        self.space[0]='other-preset--close'
        await asyncio.wait_for(task,1)
        self.assertTrue(cancelled.is_set());self.deliver.assert_not_awaited()
        self.assertEqual(self.planner.accounts['ilink:wx']['attempts'],[])

    async def test_space_change_after_submission_keeps_claim_and_original_history_tag(self):
        await self.make_due();entered=asyncio.Event();gate=asyncio.Event()
        async def transport(*a):entered.set();await gate.wait()
        self.planner.deliver=transport
        task=asyncio.create_task(self.planner.tick());await asyncio.wait_for(entered.wait(),1)
        row=self.planner.accounts['ilink:wx'];attempt_id=row['attempts'][0]['id']
        self.space[0]='other-preset--close';gate.set();await task
        self.assertEqual(row['attempts'][0]['id'],attempt_id)
        self.assertEqual(row['attempts'][0]['status'],'accepted');self.assertTrue(row['awaiting'])
        self.assertEqual(row['history'][-1]['persona_space'],'profile-a--neutral')
        self.assertEqual(self.planner.timeline([row]),[])

    async def test_explicit_outbound_tag_handles_old_inflight_regular_reply(self):
        p=self.planner;p.note_in(msg());self.space[0]='other-preset--close'
        p.note_out('ilink','wx','已提交的旧空间回复',persona_space='profile-a--neutral')
        self.assertEqual(p.timeline(list(p.accounts.values())),[])
        self.assertEqual(p.accounts['ilink:wx']['last_out'],self.now[0])

    async def test_default_empty_space_preserves_old_standalone_behavior(self):
        p=ProactivePlanner(Path(self.tmp.name)/'standalone.json',lambda p,o:p+o,lambda:self.cfg,
                           lambda *_:None,self.generate,self.deliver)
        try:
            p.note_in(msg());p.accounts['ilink:wx']['history'][0].pop('persona_space')
            self.assertEqual(len(p.timeline(list(p.accounts.values()))),1)
        finally:await p.close()


class CoordinatorSpaces(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'turns.json'
        self.space=['preset-a--neutral'];self.coordinator=TurnCoordinator(self.path,clock=lambda:1000)
        self.coordinator.active_space=lambda:self.space[0];self.coordinator.legacy_space=self.space[0]
    def tearDown(self):self.tmp.cleanup()
    def deferred(self,mid='1',owner='wx'):
        tid=self.coordinator.begin(msg(mid,owner=owner));self.coordinator.decide(tid,ReplyDecision('defer','稍后承接'))
        self.coordinator.turns[tid]['revisit_at']=0
        return tid

    def test_begin_tags_and_due_cancels_other_spaces_without_reviving_on_return(self):
        old=self.deferred();self.assertEqual(self.coordinator.turns[old]['persona_space'],'preset-a--neutral')
        self.space[0]='preset-b--close';new=self.deferred('2','other-user')
        due=self.coordinator.due();self.assertEqual([r['id'] for r in due],[new])
        self.assertEqual(self.coordinator.turns[old]['state'],'context_only')
        self.space[0]='preset-a--neutral';self.assertEqual(self.coordinator.due(),[])
        self.assertEqual(self.coordinator.turns[new]['state'],'context_only')

    def test_legacy_untagged_pending_does_not_leak_to_new_space(self):
        old=self.deferred();self.coordinator.turns[old].pop('persona_space')
        self.coordinator._save();self.space[0]='preset-b--close'
        self.assertEqual(self.coordinator.due(),[])
        self.assertEqual(self.coordinator.turns[old]['state'],'context_only')

    def test_unsent_checks_and_late_decisions_cannot_revive_other_space(self):
        tid=self.coordinator.begin(msg());self.coordinator.decide(tid,ReplyDecision('reply','现在回复'))
        self.space[0]='preset-b--close'
        with self.assertRaises(ConversationChanged):self.coordinator.check(tid)
        with self.assertRaises(ConversationChanged):self.coordinator.reserve(tid,text='旧稿')
        self.coordinator.decide(tid,ReplyDecision('reply','迟到的旧判定'))
        self.assertEqual(self.coordinator.turns[tid]['state'],'context_only')
        self.assertEqual(self.coordinator.turns[tid]['attempts'],[])

    def test_switch_never_modifies_committed_delivery_states_or_credentials(self):
        ids=[]
        for i,state in enumerate(('sending','uncertain','accepted')):
            tid=self.coordinator.begin(msg(str(i),owner='owner'+str(i)))
            self.coordinator.decide(tid,ReplyDecision('reply','回复'))
            self.coordinator.reserve(tid,text='固定正文'+str(i))
            if state!='sending':self.coordinator.finish(tid,state)
            ids.append(tid)
        before={tid:copy.deepcopy(self.coordinator.turns[tid]) for tid in ids}
        self.space[0]='preset-b--close';self.assertEqual(self.coordinator.cancel_other_spaces(),0)
        self.assertEqual({tid:self.coordinator.turns[tid] for tid in ids},before)
        self.assertEqual(self.coordinator.due(),[])

    def test_cancel_is_atomic_if_disk_save_fails(self):
        tid=self.deferred();before=copy.deepcopy(self.coordinator.turns[tid]);self.space[0]='preset-b--close'
        with patch.object(self.coordinator,'_save',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.coordinator.cancel_other_spaces()
        self.assertEqual(self.coordinator.turns[tid],before)

    def test_default_space_preserves_legacy_untagged_pending_behavior(self):
        c=TurnCoordinator(clock=lambda:1000);tid=c.begin(msg())
        c.turns[tid].pop('persona_space');c.decide(tid,ReplyDecision('defer','稍后承接'));c.turns[tid]['revisit_at']=0
        self.assertEqual([r['id'] for r in c.due()],[tid])


if __name__=='__main__':unittest.main()
