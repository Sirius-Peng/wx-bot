"""Durable full-prefix sessions; no provider calls or user configuration reads."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.store_memory import MemoryStore


WX = 'ctx:ilink:private:alice'
QQ = 'ctx:onebot:private:p_123'
OTHER = 'ctx:ilink:private:bob'
GROUP = 'ctx:onebot:group:g_456'


def session(text='今天看见了小猫'):
    return {
        'messages': [
            {'role': 'system', 'content': '完整人格，\n稳定前缀'},
            {'role': 'user', 'content': text},
            {'role': 'assistant', 'content': '嗯，我听着。',
             'provider_optional': {'reasoning_content': 'unknown field'}},
        ],
        'covered_source_ids': ['onebot:private:p_123:123:1'],
        'fingerprint': 'provider/persona:sha256',
        'token_estimate': 41,
        'turn_count': 1,
        'generation': 0,
        'future_metadata': {'nested': [{'value': '原样保留', 'flag': True}]},
    }


def marker():
    return {'id': 'forget-occupation',
            'accounts': [{'platform': 'ilink', 'owner_id': 'alice'},
                         {'platform': 'onebot', 'owner_id': '123'}],
            'content': '用户是工程师', 'evidence': '我是工程师',
            'fact_key': 'occupation'}


class SessionStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'context-ilink.json'
        self.store = MemoryStore(window_size=2, path=self.path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_get_absent_does_not_create_file(self):
        self.assertIsNone(await self.store.get_session(WX))
        self.assertFalse(self.path.exists())

    async def test_round_trip_full_dict_and_unknown_message_fields(self):
        value = session()
        self.assertTrue(await self.store.set_session(WX, value, expected_revision=0))
        loaded = MemoryStore(path=self.path)
        self.assertEqual(await loaded.get_session(WX), value)
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk['version'], 3)
        self.assertEqual(disk['sessions'][WX], value)
        self.assertEqual(disk['session_revisions'][WX], 0)
        self.assertEqual(loaded.revision, 0)

    async def test_set_and_get_have_no_nested_aliases(self):
        value = session()
        original = copy.deepcopy(value)
        await self.store.set_session(WX, value)
        value['messages'][0]['content'] = 'input mutated'
        value['future_metadata']['nested'][0]['value'] = 'input mutated'
        read = await self.store.get_session(WX)
        self.assertEqual(read, original)
        read['messages'].append({'role': 'user', 'content': 'output mutated'})
        read['future_metadata']['nested'].clear()
        self.assertEqual(await self.store.get_session(WX), original)

    async def test_long_sessions_and_full_raw_history_not_old_window_clipped(self):
        value = session()
        value['messages'] = [{'role': 'user' if i % 2 else 'assistant', 'content': str(i)}
                             for i in range(300)]
        for i in range(45):
            await self.store.push(WX, {'user': str(i), 'assistant': '', 'turn_id': str(i)})
        await self.store.set_session(WX, value)
        loaded = MemoryStore(window_size=2, path=self.path)
        self.assertEqual(len((await loaded.get_session(WX))['messages']), 300)
        self.assertEqual(len(await loaded.get_recent(WX)), 45)

    async def test_multimodal_tool_messages_keep_exact_shape(self):
        value = session()
        value['messages'] += [
            {'role': 'user', 'content': [{'type': 'text', 'text': '看看这个'},
              {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture', 'detail': 'auto'}}]},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call-1', 'type': 'function', 'function': {'name': 'search', 'arguments': '{}'}}]},
            {'role': 'tool', 'content': '{"summary":"依据"}', 'tool_call_id': 'call-1'},
        ]
        await self.store.set_session(WX, value)
        self.assertEqual(await MemoryStore(path=self.path).get_session(WX), value)

    async def test_normal_session_commit_does_not_advance_global_revision(self):
        await self.store.set_session(WX, session())
        await self.store.set_session(QQ, session())
        self.assertEqual(self.store.revision, 0)

    async def test_mismatched_revision_rejects_without_save_or_replacement(self):
        value = session()
        await self.store.set_session(WX, value)
        with patch.object(self.store, '_save') as save:
            self.assertFalse(await self.store.set_session(WX, session('旧草稿'), expected_revision=99))
            save.assert_not_called()
        self.assertEqual(await self.store.get_session(WX), value)

    async def test_explicit_forgetting_scrubs_both_linked_sessions_only(self):
        for key in (WX, QQ, OTHER):
            await self.store.set_session(key, session())
        old_revision = self.store.revision
        self.store.suppress(marker())
        self.assertGreater(self.store.revision, old_revision)
        self.assertIsNone(await self.store.get_session(WX))
        self.assertIsNone(await self.store.get_session(QQ))
        self.assertEqual(await self.store.get_session(OTHER), session())
        self.assertIsNone(await MemoryStore(path=self.path).get_session(WX))

    async def test_forget_clears_paraphrased_session_without_raw_context(self):
        await self.store.set_session(WX, session('工作中的那些机械设计经历'))
        self.store.suppress(marker())
        self.assertIsNone(await self.store.get_session(WX))

    async def test_group_cached_forbidden_text_is_cleared(self):
        await self.store.set_session(GROUP, session('我是工程师'))
        self.store.suppress(marker())
        self.assertIsNone(await self.store.get_session(GROUP))

    @staticmethod
    def group_session(owner='123', chat='g_456'):
        value=session('你设计高精度机械产品的经历，大家刚才聊到了这段职业背景。')
        value['covered_source_ids']=[f'onebot:group:{chat}:{owner}:old']
        return value

    async def test_group_compacted_owner_is_forgotten_without_literal_match(self):
        await self.store.set_session(GROUP,self.group_session())
        self.store.suppress(marker())
        self.assertIsNone(await self.store.get_session(GROUP))
        self.assertIsNone(await MemoryStore(path=self.path).get_session(GROUP))

    async def test_group_known_other_sender_is_not_forgotten_by_substring(self):
        value=self.group_session(owner='1234')
        await self.store.set_session(GROUP,value)
        self.store.suppress(marker())
        self.assertEqual(await self.store.get_session(GROUP),value)
        self.assertEqual(await MemoryStore(path=self.path).get_session(GROUP),value)

    async def test_group_provenance_allows_colons_in_message_id_only(self):
        value=self.group_session(owner='1234')
        value['covered_source_ids']=['onebot:group:g_456:1234:message:with:colons']
        await self.store.set_session(GROUP,value)
        self.store.suppress(marker())
        self.assertEqual(await self.store.get_session(GROUP),value)

    async def test_group_mixed_target_and_other_source_owners_clears_prefix(self):
        value=self.group_session(owner='1234')
        value['covered_source_ids'].append('onebot:group:g_456:123:another')
        await self.store.set_session(GROUP,value)
        self.store.suppress(marker())
        self.assertIsNone(await self.store.get_session(GROUP))

    async def test_group_missing_or_inconsistent_provenance_is_conservatively_cleared(self):
        for sources in ([],['legacy-message-id'],['onebot:group:g_456:1234:'],
                        ['onebot:group:g_999:1234:old'],['ilink:group:g_456:1234:old'],
                        ['onebot:private:p_1234:1234:old'],['onebot:group:g_456: 1234:old'],
                        ['onebot:group:g_456:1234:old','legacy-unknown']):
            with self.subTest(sources=sources):
                store=MemoryStore()
                value=self.group_session()
                value['covered_source_ids']=sources
                await store.set_session(GROUP,value)
                store.suppress(marker())
                self.assertIsNone(await store.get_session(GROUP))

    async def test_group_other_platform_account_does_not_invalidate_known_group(self):
        value=self.group_session()
        await self.store.set_session(GROUP,value)
        other=marker()
        other['accounts']=[{'platform':'ilink','owner_id':'123'}]
        self.store.suppress(other)
        self.assertEqual(await self.store.get_session(GROUP),value)

    async def test_group_old_import_source_owner_is_scrubbed_by_sync(self):
        await self.store.set_session(GROUP,self.group_session())
        loaded=MemoryStore(path=self.path)
        loaded.sync_suppressions([marker()])
        self.assertIsNone(await loaded.get_session(GROUP))
        self.assertIsNone(await MemoryStore(path=self.path).get_session(GROUP))

    async def test_group_untrusted_load_uses_original_source_owner(self):
        other='ctx:onebot:group:g_789'
        unrelated=self.group_session(owner='1234',chat='g_789')
        self.path.write_text(json.dumps({'version':3,'contexts':{},'suppressions':[marker()],
            'sessions':{GROUP:self.group_session(),other:unrelated}}))
        loaded=MemoryStore(path=self.path)
        self.assertIsNone(await loaded.get_session(GROUP))
        self.assertEqual(await loaded.get_session(other),unrelated)

    async def test_group_missing_provenance_is_untrusted_even_with_revision_stamp(self):
        value=self.group_session()
        value.pop('covered_source_ids')
        self.path.write_text(json.dumps({'version':3,'contexts':{},'revision':4,
            'suppressions':[marker()],'sessions':{GROUP:value},'session_revisions':{GROUP:4}}))
        self.assertIsNone(await MemoryStore(path=self.path).get_session(GROUP))

    async def test_group_known_sync_drops_unknown_legacy_sources_even_with_fresh_stamp(self):
        self.store.suppress(marker())
        value=self.group_session()
        value['covered_source_ids']=['old-unattributed-summary']
        await self.store.set_session(GROUP,value)
        self.store.sync_suppressions([marker()])
        self.assertIsNone(await self.store.get_session(GROUP))

    async def test_group_fresh_safe_known_source_survives_known_sync_and_restart(self):
        self.store.suppress(marker())
        value=self.group_session()
        value['messages']=[{'role':'user','content':'今天想聊猫。'}]
        await self.store.set_session(GROUP,value,expected_revision=self.store.revision)
        self.store.sync_suppressions([marker()])
        self.assertEqual(await self.store.get_session(GROUP),value)
        self.assertEqual(await MemoryStore(path=self.path).get_session(GROUP),value)

    async def test_group_legacy_summaries_require_attributable_unrelated_sources(self):
        other='ctx:onebot:group:g_789'
        unknown='ctx:onebot:group:g_999'
        paraphrase='你有设计高精度机械产品的经历。'
        self.path.write_text(json.dumps({'version':3,'contexts':{},'suppressions':[marker()],
            'summaries':{GROUP:paraphrase,other:'其他人的允许信息',unknown:paraphrase},
            'summary_sources':{GROUP:['onebot:group:g_456:123:old'],
                               other:['onebot:group:g_789:1234:old'],unknown:['manual-context']}}))
        loaded=MemoryStore(path=self.path)
        self.assertEqual(await loaded.get_summary(GROUP),'')
        self.assertEqual(await loaded.get_summary(unknown),'')
        self.assertEqual(await loaded.get_summary(other),'其他人的允许信息')

    async def test_group_original_source_scrubs_assistant_paraphrase_but_keeps_user_words(self):
        await self.store.push(GROUP,{'user':'周末聊过的事情','assistant':'你的机械设计工作很有意思。',
            'source_ids':['onebot:group:g_456:123:old'],'turn_id':'onebot:group:g_456:123:old'})
        self.store.suppress(marker())
        recent=await self.store.get_recent(GROUP)
        self.assertEqual(recent[0]['user'],'周末聊过的事情')
        self.assertEqual(recent[0]['assistant'],'')

    async def test_group_known_unrelated_source_keeps_its_assistant_context(self):
        turn={'user':'周末聊过的事情','assistant':'一起去公园吧。',
              'source_ids':['onebot:group:g_456:1234:old'],'turn_id':'onebot:group:g_456:1234:old'}
        await self.store.push(GROUP,turn)
        await self.store.set_session(GROUP,self.group_session(owner='1234'))
        self.store.suppress(marker())
        self.assertEqual(await self.store.get_recent(GROUP),[turn])
        self.assertIsNotNone(await self.store.get_session(GROUP))

    async def test_global_marker_clears_every_session(self):
        for key in (WX, QQ, OTHER):
            await self.store.set_session(key, session())
        self.store.suppress({'id': 'all', 'accounts': [{'owner_id': '*'}], 'content': '旧事'})
        for key in (WX, QQ, OTHER):
            self.assertIsNone(await self.store.get_session(key))

    async def test_inflight_prefix_cannot_restore_after_forgetting(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed():
            revision = self.store.revision
            started.set()
            await release.wait()
            return await self.store.set_session(WX, session('旧草稿'), expected_revision=revision)
        job = asyncio.create_task(delayed())
        await started.wait()
        self.store.suppress(marker())
        release.set()
        self.assertFalse(await job)
        self.assertIsNone(await self.store.get_session(WX))

    async def test_explicit_clear_advances_revision_even_without_prior_prefix(self):
        previous = self.store.revision
        self.assertIsNone(await self.store.clear_session(WX))
        self.assertGreater(self.store.revision, previous)
        self.assertFalse(await self.store.set_session(WX, session(), expected_revision=previous))

    async def test_clear_persists_and_preserves_other_prefix(self):
        for key in (WX, OTHER):
            await self.store.set_session(key, session())
        await self.store.clear_session(WX)
        loaded = MemoryStore(path=self.path)
        self.assertIsNone(await loaded.get_session(WX))
        self.assertEqual(await loaded.get_session(OTHER), session())
        self.assertEqual(loaded.revision, self.store.revision)

    async def test_set_failure_restores_prior_prefix_and_revision_metadata(self):
        original = session()
        await self.store.set_session(WX, original)
        disk = self.path.read_bytes()
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                await self.store.set_session(WX, session('replacement'))
        self.assertEqual(await self.store.get_session(WX), original)
        self.assertEqual(self.store._session_revisions[WX], 0)
        self.assertEqual(self.path.read_bytes(), disk)

    async def test_first_set_failure_leaves_no_prefix(self):
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.store.set_session(WX, session())
        self.assertIsNone(await self.store.get_session(WX))
        self.assertNotIn(WX, self.store._session_revisions)

    async def test_clear_failure_restores_prefix_and_barrier_revision(self):
        await self.store.set_session(WX, session())
        before = self.store.revision
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.store.clear_session(WX)
        self.assertEqual(await self.store.get_session(WX), session())
        self.assertEqual(self.store.revision, before)

    async def test_suppression_failure_restores_context_summary_session_and_revision(self):
        await self.store.push(WX, {'user': '我是工程师', 'assistant': '明白了', 'turn_id': '1'})
        await self.store.set_summary(WX, '工作经历')
        await self.store.set_session(WX, session())
        before = self.path.read_bytes()
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.store.suppress(marker())
        self.assertEqual(self.store.revision, 0)
        self.assertEqual(await self.store.get_session(WX), session())
        self.assertEqual(await self.store.get_summary(WX), '工作经历')
        self.assertEqual((await self.store.get_recent(WX))[0]['user'], '我是工程师')
        self.assertEqual(self.store._suppression_markers, [])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_sync_failure_rolls_back_all_markers_and_prefixes(self):
        await self.store.set_session(WX, session())
        before = self.path.read_bytes()
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.store.sync_suppressions([marker()])
        self.assertEqual(self.store.revision, 0)
        self.assertEqual(await self.store.get_session(WX), session())
        self.assertEqual(self.store._suppression_markers, [])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_trusted_safe_prefix_survives_repeated_known_barrier_sync(self):
        self.store.suppress(marker())
        await self.store.set_session(WX, session())
        revision = self.store.revision
        loaded = MemoryStore(path=self.path)
        loaded.sync_suppressions([marker()])
        self.assertEqual(loaded.revision, revision)
        self.assertEqual(await loaded.get_session(WX), session())

    async def test_known_explicit_forget_still_clears_new_prefix(self):
        self.store.suppress(marker())
        await self.store.set_session(WX, session())
        revision = self.store.revision
        self.store.suppress(marker())
        self.assertGreater(self.store.revision, revision)
        self.assertIsNone(await self.store.get_session(WX))

    async def test_expanding_existing_marker_accounts_clears_newly_linked_prefix(self):
        first=marker()
        first['accounts']=first['accounts'][:1]
        self.store.sync_suppressions([first])
        await self.store.set_session(QQ, session('旧工作经历的改写'))
        self.store.sync_suppressions([marker()])
        self.assertIsNone(await self.store.get_session(QQ))

    async def test_forbidden_original_and_new_fact_slot_are_not_reintroduced(self):
        self.store.suppress(marker())
        for text in ('我是工程师', '我是教师'):
            with self.subTest(text=text):
                self.assertFalse(await self.store.set_session(WX, session(text)))
        self.assertIsNone(await self.store.get_session(WX))

    async def test_releasing_barrier_does_not_resurrect_old_prefix(self):
        await self.store.set_session(WX, session('我是工程师'))
        self.store.suppress(marker())
        before = self.store.revision
        self.store.sync_suppressions([])
        self.assertGreater(self.store.revision, before)
        self.assertIsNone(await self.store.get_session(WX))
        self.assertTrue(await self.store.set_session(WX, session('我是教师'), expected_revision=self.store.revision))

    async def test_summary_read_is_readonly(self):
        await self.store.set_summary(WX, '旧摘要')
        await self.store.set_session(WX, session())
        self.assertEqual(await self.store.get_summary(WX), '旧摘要')
        self.assertEqual(await self.store.get_session(WX), session())

    async def test_summary_replace_invalidates_only_same_key_prefix(self):
        for key in (WX, OTHER):await self.store.set_session(key, session())
        before = self.store.revision
        await self.store.set_summary(WX, '新摘要')
        self.assertIsNone(await self.store.get_session(WX))
        self.assertEqual(await self.store.get_session(OTHER), session())
        self.assertEqual(self.store.revision, before)

    async def test_summary_failure_restores_prefix_and_sources(self):
        await self.store.set_session(WX, session())
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.store.set_summary(WX, '新摘要')
        self.assertEqual(await self.store.get_session(WX), session())
        self.assertNotIn(WX, self.store._summary_sources)

    async def test_compaction_invalidates_same_key_prefix_without_global_revision(self):
        turn = {'user': '真实来话', 'assistant': '回答', 'turn_id': '1'}
        await self.store.push(WX, turn)
        for key in (WX, OTHER):await self.store.set_session(key, session())
        before = self.store.revision
        self.assertTrue(await self.store.compact(WX, [turn], '摘要'))
        self.assertIsNone(await self.store.get_session(WX))
        self.assertEqual(await self.store.get_session(OTHER), session())
        self.assertEqual(self.store.revision, before)

    async def test_failed_compaction_restores_same_key_prefix(self):
        turn = {'user': '真实来话', 'assistant': '回答', 'turn_id': '1'}
        await self.store.push(WX, turn)
        await self.store.set_session(WX, session())
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.store.compact(WX, [turn], '摘要')
        self.assertEqual(await self.store.get_session(WX), session())
        self.assertEqual(await self.store.get_recent(WX), [turn])

    async def test_rejected_compaction_does_not_drop_prefix(self):
        await self.store.set_session(WX, session())
        self.assertFalse(await self.store.compact(WX, [{'user': 'nonexistent'}], '摘要'))
        self.assertEqual(await self.store.get_session(WX), session())

    async def test_pop_oldest_invalidates_prefix_and_rolls_back_on_failure(self):
        for i in range(3):await self.store.push(WX, {'user': str(i), 'assistant': ''})
        await self.store.set_session(WX, session())
        with patch.object(self.store, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.store.pop_oldest(WX, keep=1)
        self.assertEqual(await self.store.get_session(WX), session())
        self.assertEqual(len(await self.store.get_recent(WX)), 3)
        self.assertEqual(len(await self.store.pop_oldest(WX, keep=1)), 2)
        self.assertIsNone(await self.store.get_session(WX))

    async def test_invalid_session_rejected_without_replacing_valid_one(self):
        await self.store.set_session(WX, session())
        for value in ({}, {'messages': {}}, {'messages': [{}]},
                      {'messages': [{'role': []}]}, {'messages': [{'role': 'untrusted'}]},
                      {'messages': [{'role': 'user', 'content': 12}]},
                      {'messages': [{'role': 'user', 'content': ['not an object']}]},
                      {'messages': [], 'covered_source_ids': ['valid', 1]},
                      {'messages': [], 'fingerprint': {}},
                      {'messages': [], 'token_estimate': -1},
                      {'messages': [], 'turn_count': '1'},
                      {'messages': [], 'generation': True},
                      {'messages': [], 'unknown': float('nan')},
                      {'messages': [], 'unknown': ('tuple',)},
                      {'messages': [], 'unknown': {1: 'non-string-key'}}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):await self.store.set_session(WX, value)
        self.assertEqual(await self.store.get_session(WX), session())

    async def test_invalid_loaded_session_skips_only_bad_records(self):
        self.path.write_text(json.dumps({'version': 3, 'contexts': {WX: [{'user': '真实来话'}]},
            'sessions': {WX: session(), 'broken': {'messages': [{'role': []}]}, 'bad': None},
            'unknown_top_level': {'original': [1, 2]}}))
        loaded = MemoryStore(path=self.path)
        self.assertEqual(await loaded.get_session(WX), session())
        self.assertIsNone(await loaded.get_session('broken'))
        self.assertIsNone(await loaded.get_session('bad'))
        self.assertEqual((await loaded.get_recent(WX))[0]['user'], '真实来话')
        await loaded.set_session(OTHER, session())
        self.assertEqual(json.loads(self.path.read_text())['unknown_top_level'], {'original': [1, 2]})

    async def test_malformed_sessions_table_does_not_break_existing_context(self):
        self.path.write_text(json.dumps({'version': 3, 'contexts': {WX: [{'user': '真实来话'}]}, 'sessions': []}))
        loaded = MemoryStore(path=self.path)
        self.assertIsNone(await loaded.get_session(WX))
        self.assertEqual((await loaded.get_recent(WX))[0]['user'], '真实来话')

    async def test_untrusted_import_with_known_barrier_is_discarded(self):
        self.path.write_text(json.dumps({'version': 3, 'contexts': {}, 'revision': 4,
            'suppressions': [marker()], 'sessions': {WX: session('paraphrased old work'), OTHER: session()}}))
        loaded = MemoryStore(path=self.path)
        self.assertIsNone(await loaded.get_session(WX))
        self.assertEqual(await loaded.get_session(OTHER), session())

    async def test_false_trusted_stamp_still_cannot_bypass_forbidden_text(self):
        self.path.write_text(json.dumps({'version': 3, 'contexts': {}, 'revision': 4,
            'suppressions': [marker()], 'sessions': {WX: session('我是工程师')}, 'session_revisions': {WX: 4}}))
        self.assertIsNone(await MemoryStore(path=self.path).get_session(WX))

    async def test_import_sync_new_marker_clears_cached_old_prefix(self):
        await self.store.set_session(WX, session('paraphrased old work'))
        loaded = MemoryStore(path=self.path)
        loaded.sync_suppressions([marker()])
        self.assertIsNone(await loaded.get_session(WX))
        self.assertIsNone(await MemoryStore(path=self.path).get_session(WX))

    async def test_v1_upgrade_keeps_paths_immutable_backup_and_unknown_user_files(self):
        original = json.dumps({'version': 1, 'contexts': {WX: [{'user': '旧来话'}]},
                               'summaries': {WX: '无来源旧摘要'}, 'custom': {'keep': True}}).encode()
        self.path.write_bytes(original)
        unknown = self.root / 'user-notes.txt'
        unknown.write_bytes(b'preserve user bytes')
        loaded = MemoryStore(path=self.path)
        self.assertIsNone(await loaded.get_session(WX))
        self.assertEqual(await loaded.get_summary(WX), '')
        self.assertEqual((await loaded.get_recent(WX))[0]['user'], '旧来话')
        await loaded.set_session(WX, session())
        self.assertEqual((self.root / '.context-ilink.v1-backup.json').read_bytes(), original)
        self.assertEqual(unknown.read_bytes(), b'preserve user bytes')
        self.assertEqual(json.loads(self.path.read_text())['version'], 3)
        self.assertEqual(json.loads(self.path.read_text())['custom'], {'keep': True})

    async def test_old_v3_without_sessions_loads_and_retains_sourced_summary(self):
        self.path.write_text(json.dumps({'version': 3, 'contexts': {WX: [{'user': '旧来话'}]},
            'summaries': {WX: '可信摘要'}, 'summary_sources': {WX: ['source-1']}}))
        loaded = MemoryStore(path=self.path)
        self.assertIsNone(await loaded.get_session(WX))
        self.assertEqual(await loaded.get_summary(WX), '可信摘要')
        await loaded.set_session(WX, session())
        self.assertEqual(await MemoryStore(path=self.path).get_summary(WX), '可信摘要')


if __name__ == '__main__':
    unittest.main()
