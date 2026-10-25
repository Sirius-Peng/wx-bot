"""v3 retention, evidence-only clocks, indexed reads and batched usage contracts."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
ROOT = Path(os.environ.get('WXBOT_TEST_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from src.core.memory import MemoryItem, PersonaMemory, RETENTION
from src.core.types import WxMessage

DAY = 86400
NOW = 1792300000.0

class FastMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'memory.json'
        self.memory = PersonaMemory(self.path)

    async def asyncTearDown(self):
        await self.memory.learner.close()
        await self.memory.close_usage()
        self.tmp.cleanup()

    def item(self, mid='m', tier='normal', age=0, owner='alice', platform='ilink', **kw):
        value = MemoryItem(id=mid, scope='user', owner_id=owner, platform=platform,
                           category='preference', content='用户喜欢绿茶', keywords=['绿茶'],
                           phase='stable', confidence=.85, last_user_evidence_at=NOW-age,
                           importance_tier=tier, **kw)
        self.memory._items[mid] = value
        return value

    def merge(self, source_id='source', at=NOW, tier=None, reason='', text='我喜欢绿茶'):
        return self.memory._merge_or_create('user', 'alice', 'preference', '用户喜欢绿茶', ['绿茶'], 'alice',
            platform='ilink', evidence=text, observed_at=at, importance_tier=tier, importance_reason=reason,
            sources=[dict(id=source_id, episode_id=source_id, observed_at=at, text=text)])

    def test_three_tiers_have_independent_half_lives_and_deadlines(self):
        for tier, (half, days) in RETENTION.items():
            item = self.item(mid=tier, tier=tier)
            self.assertAlmostEqual(item.lifecycle(NOW+half*DAY)[1], .5)
            self.assertNotEqual(item.lifecycle(NOW+days*DAY-.001)[0], 'dormant')
            self.assertEqual(item.lifecycle(NOW+days*DAY), ('dormant', 0.0))
            self.assertEqual(item.to_dict()['retention_days'], days)

    def test_pinned_ignores_retention_but_unpin_keeps_original_clock(self):
        item = self.item(age=31*DAY)
        self.memory.set_pinned(item.id, True)
        self.assertEqual(item.lifecycle(NOW+9999*DAY), ('pinned', 1.0))
        self.memory.set_pinned(item.id, False)
        self.assertEqual(item.last_user_evidence_at, NOW-31*DAY)
        self.assertEqual(item.lifecycle(NOW)[0], 'dormant')

    def test_important_candidate_does_not_become_fact(self):
        item = self.merge(tier='important', reason='我喜欢绿茶')
        self.assertEqual(item.phase, 'candidate')
        self.assertEqual(item.confidence, .5)
        self.assertEqual(item.importance_tier, 'important')
        self.assertEqual(self.memory.recall('alice','alice','绿茶',platform='ilink'), [])

    def test_model_reason_requires_user_evidence(self):
        for tier, reason in [('important','助手认为非常重要'), ([], '我喜欢绿茶'), ('invalid','我喜欢绿茶')]:
            self.memory._items.clear()
            item = self.merge(tier=tier, reason=reason)
            self.assertEqual(item.importance_tier, 'normal')
            self.assertEqual(item.importance_basis, 'default')

    def test_rescore_does_not_extend_or_revive(self):
        item = self.merge(tier='low', reason='我喜欢绿茶')
        original = item.dormant_at
        self.merge(tier='important', reason='我喜欢绿茶')
        self.assertEqual(item.importance_tier, 'important')
        self.assertEqual(item.dormant_at, original)
        self.assertEqual(item.lifecycle(NOW+4*DAY)[0], 'dormant')
        self.assertEqual(item.last_user_evidence_at, NOW)

    def test_new_independent_user_evidence_can_renew(self):
        item = self.merge(tier='low', reason='我喜欢绿茶')
        self.merge('later', NOW+4*DAY, 'important', '我喜欢绿茶')
        self.assertEqual(item.last_user_evidence_at, NOW+4*DAY)
        self.assertEqual(item.dormant_at, NOW+34*DAY)
        self.assertEqual(item.phase, 'stable')

    def test_replayed_evidence_with_new_timestamp_cannot_renew(self):
        item = self.merge(tier='low', reason='我喜欢绿茶')
        deadline = item.dormant_at
        self.merge('source', NOW+20*DAY, 'important', '我喜欢绿茶')
        self.assertEqual(item.last_user_evidence_at, NOW)
        self.assertEqual(item.dormant_at, deadline)

    def test_rescore_can_shorten_but_not_add_evidence(self):
        item = self.merge(tier='important', reason='我喜欢绿茶')
        self.merge(tier='low', reason='我喜欢绿茶')
        self.assertEqual(item.dormant_at, NOW+3*DAY)
        self.assertEqual(len(item.evidence_ids), 1)

    def test_future_event_not_automatically_temporary(self):
        item = self.memory._merge_or_create('user','alice','event','下周参加演出',['演出'],'alice',
            platform='ilink',evidence='下周参加演出',observed_at=NOW,importance_tier='important',
            importance_reason='下周参加演出',expires_at=NOW+10*DAY,
            sources=[dict(id='event',episode_id='one',observed_at=NOW,text='下周参加演出')])
        self.assertEqual(item.phase, 'candidate')
        self.assertEqual(item.lifecycle(NOW+2*DAY)[0], 'candidate')
        self.assertEqual(item.lifecycle(NOW+10*DAY)[0], 'dormant')
        self.assertEqual(item.deadline(), NOW+10*DAY)

    def test_event_deadline_cannot_extend_tier(self):
        item = self.item(tier='low', expires_at=NOW+100*DAY)
        self.assertEqual(item.deadline(), NOW+3*DAY)

    def test_temporary_state_still_24hours_even_important(self):
        item = self.memory._merge_or_create('user','alice','event','用户今天很累',['累'],'alice',
            platform='ilink',evidence='我今天很累',observed_at=NOW,
            importance_tier='important',importance_reason='我今天很累',
            sources=[dict(id='state',episode_id='one',observed_at=NOW,text='我今天很累')])
        self.assertEqual(item.phase, 'temporary')
        self.assertEqual(item.lifecycle(NOW+DAY), ('dormant',0.0))

    def test_matching_event_cancellation_does_not_cancel_unrelated_event(self):
        first = self.item(mid='a', category='event') if False else self.item(mid='a')
        first.category='event'; first.keywords=['演出']
        second = self.item(mid='b');second.category='event';second.keywords=['旅行']
        msg=WxMessage('cancel','ilink','private','alice','alice','Alice','bot','演出取消了')
        self.memory.observe_resolution(msg)
        self.assertGreater(first.expires_at, 0)
        self.assertEqual(second.expires_at, 0)

    async def test_old_weight_and_hits_do_not_change_migration_clock(self):
        self.path.write_text(json.dumps({'version':3,'items':[dict(id='legacy',scope='user',owner_id='alice',
            platform='ilink',category='fact',content='用户是工程师',importance=3,hits=999,
            phase='stable',last_user_evidence_at=NOW-8*DAY)]}))
        loaded=PersonaMemory(self.path)
        try:
            item=loaded._items['legacy']
            self.assertEqual(item.importance_tier, 'normal')
            self.assertEqual(item.importance_basis, 'legacy_default')
            self.assertEqual(item.dormant_at, NOW-DAY)
            self.assertEqual(item.lifecycle(NOW)[0], 'dormant')
        finally: await loaded.learner.close()

    def test_recall_is_read_only_one_hundred_times(self):
        item=self.item();self.memory._save();before=self.path.read_bytes();metadata=copy.deepcopy(item.__dict__)
        with patch('src.core.memory.time.time',return_value=NOW), patch.object(self.memory,'_save',side_effect=AssertionError('write')):
            for _ in range(100): self.assertEqual(self.memory.recall('alice','alice','绿茶',platform='ilink'),[item])
        self.assertEqual(item.__dict__,metadata)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.path.with_name('memory-usage.json').exists())

    def test_daily_one_historical_three_and_small_talk_none(self):
        for n in range(4):self.item(mid=str(n))
        with patch('src.core.memory.time.time',return_value=NOW):
            self.assertEqual(self.memory.build_context('alice','alice','绿茶','ilink').count('- ['),1)
            text=self.memory.build_context('alice','alice','以前的绿茶','ilink')
            self.assertEqual(text.count('- ['),3)
            self.assertLessEqual(len(text),500)
            self.assertEqual(self.memory.build_context('alice','alice','你好','ilink'),'')

    def test_index_excludes_unrelated_accounts_before_suppression_check(self):
        chosen=self.item()
        for n in range(100):self.item(mid='other'+str(n),owner='other'+str(n))
        with patch('src.core.memory.time.time',return_value=NOW), patch.object(self.memory,'is_suppressed',wraps=self.memory.is_suppressed) as blocked:
            self.assertEqual(self.memory.recall('alice','alice','绿茶',platform='ilink'),[chosen])
            self.assertEqual(blocked.call_count,1)

    def test_index_invalidates_after_mutation_and_forget(self):
        a=self.memory.add_manual('user','alice','preference','用户喜欢绿茶',['绿茶'],platform='ilink')
        self.assertTrue(self.memory.recall('alice','alice','绿茶',platform='ilink'))
        self.memory.delete(a['id'])
        self.assertEqual(self.memory.recall('alice','alice','绿茶',platform='ilink'),[])
        self.assertTrue(self.memory.is_suppressed('ilink','alice','我喜欢绿茶'))

    async def test_usage_is_separate_throttled_and_has_no_strength_effect(self):
        item=self.item();self.memory._save();before=self.path.read_bytes();deadline=item.deadline()
        self.memory.record_usage([item.id]);self.assertFalse(self.memory.flush_usage())
        self.assertTrue(self.memory.flush_usage(True));self.assertEqual(item.hits,1)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(item.deadline(),deadline)
        self.memory.record_usage([item.id]);self.assertFalse(self.memory.flush_usage())
        self.assertEqual(json.loads(self.path.with_name('memory-usage.json').read_text())['items'][item.id]['hits'],1)
        self.memory._usage_last_flush-=61;self.assertTrue(self.memory.flush_usage())
        loaded=PersonaMemory(self.path)
        try:self.assertEqual(loaded._items[item.id].hits,2)
        finally:await loaded.learner.close()

    async def test_usage_shutdown_flushes_only_statistics(self):
        item=self.item();self.memory.record_usage([item.id])
        await self.memory.close_usage()
        self.assertTrue(self.path.with_name('memory-usage.json').exists())
        self.assertFalse(self.path.exists())

    def test_explicit_desktop_link_and_unlink_preserve_isolation(self):
        self.memory.add_manual('user','alice','preference','用户喜欢绿茶',['绿茶'],platform='ilink')
        link=self.memory.link_identity_accounts([{'platform':'ilink','owner_id':'alice'},{'platform':'desktop'}])
        self.assertEqual(self.memory.identity_key('desktop','local'),self.memory.identity_key('ilink','alice'))
        self.assertTrue(self.memory.recall('local','local','绿茶',platform='desktop'))
        self.memory.unlink_accounts(link['id'])
        self.assertEqual(self.memory.recall('local','local','绿茶',platform='desktop'),[])

    def test_desktop_forget_barrier_survives_unlink(self):
        link=self.memory.link_identity_accounts([{'platform':'ilink','owner_id':'alice'},{'platform':'desktop'}])
        value=self.memory.add_manual('user','alice','preference','用户喜欢绿茶',['绿茶'],platform='ilink')
        self.memory.delete(value['id']);self.memory.unlink_accounts(link['id'])
        self.assertTrue(self.memory.is_suppressed('desktop','local','用户喜欢绿茶'))

    def test_invalid_link_input_does_not_mutate(self):
        for accounts in [[],[{'platform':'desktop'}], [{'platform':'bad','owner_id':'x'},{'platform':'desktop'}],
                         [{'platform':'desktop'},{'platform':'desktop'}], [{'platform':'ilink','owner_id':'*'},{'platform':'desktop'}]]:
            with self.assertRaises(ValueError):self.memory.link_identity_accounts(accounts)
        self.assertEqual(self.memory.identities,[])

    async def test_existing_background_call_returns_grounded_importance(self):
        calls=[]
        class Model:
            async def complete(self,prompt,**kw):
                calls.append(prompt)
                return json.dumps([dict(category='preference',content='用户喜欢绿茶',keywords=['绿茶'],
                    evidence='我喜欢绿茶',importance_tier='important',importance_reason='我喜欢绿茶')])
        self.memory.llm=Model()
        await self.memory.observe('alice','private','alice','Alice',[dict(user='我喜欢绿茶',source_ids=['raw'],episode_id='one',observed_at=NOW)],platform='ilink')
        item=next(iter(self.memory._items.values()))
        self.assertEqual(item.importance_tier,'important');self.assertEqual(item.phase,'candidate')
        self.assertEqual(len(calls),1);self.assertIn('importance_tier',calls[0])

if __name__ == '__main__':unittest.main()
