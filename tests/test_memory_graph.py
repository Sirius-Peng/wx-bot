"""Read-only graph provenance, lifecycle, scoping and forget-barrier regressions."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.core.memory import MemoryItem
from src.core.memory_graph import MemoryGraphSnapshot, build_memory_graph, context_rows, identifier, read_json

NOW = 1_792_100_000.0


def item(mid='m1', owner='alice', platform='ilink', **values):
    defaults = dict(id=mid, scope='user', owner_id=owner, category='preference',
                    content='用户喜欢绿茶', keywords=['绿茶'], platform=platform,
                    phase='candidate', created_at=NOW-50, updated_at=NOW,
                    last_user_evidence_at=NOW, source_chat=owner)
    defaults.update(values)
    return asdict(MemoryItem(**defaults))


def conversation(mid='u1', owner='alice', platform='ilink', **values):
    chat = 'p_'+owner if platform == 'onebot' else owner
    sid = f'{platform}:private:{chat}:{owner}:{mid}'
    defaults = dict(platform=platform, chat_type='private', chat_id=chat,
                    owner_id=owner, user='我喜欢绿茶', assistant='听起来不错。',
                    source_ids=[sid], turn_id=sid, ts=NOW)
    defaults.update(values)
    return defaults


class MemoryGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'memory.json'

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, items=None, identities=None, suppressions=None, **extra):
        raw = dict(version=3, items=items or [], identities=identities or [], suppressions=suppressions or [])
        raw.update(extra)
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
        return MemoryGraphSnapshot(self.path)

    def graph(self, memory, rows=None, **filters):
        return build_memory_graph(memory, rows or [], now=NOW, **filters)

    @staticmethod
    def nodes(graph, kind):
        return [n for n in graph['nodes'] if n['type'] == kind]

    def linked(self):
        return [dict(id='linked', label='同一位朋友', accounts=[dict(platform='ilink', owner_id='alice'),
                                                           dict(platform='onebot', owner_id='123')])]

    def barrier(self, **values):
        defaults = dict(id='forgot', accounts=[dict(platform='ilink', owner_id='alice')],
                        content='用户喜欢绿茶', evidence='我喜欢绿茶', fact_key='', source_texts=[])
        defaults.update(values)
        return defaults

    def test_empty_graph_needs_no_data_directory_or_writers(self):
        absent = self.root/'absent'/'memory.json'
        graph = self.graph(MemoryGraphSnapshot(absent))
        self.assertEqual(graph['nodes'], [])
        self.assertEqual(graph['edges'], [])
        self.assertFalse(absent.parent.exists())

    def test_100_views_do_not_change_hits_strength_files_or_timestamps(self):
        memory = self.snapshot([item(hits=42)])
        before = copy.deepcopy(memory._items)
        files = {p.name:(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.root.iterdir()}
        with patch.object(memory, 'recall', side_effect=AssertionError('view must not recall')):
            first = self.graph(memory)
            for _ in range(100):
                self.assertEqual(self.graph(memory), first)
        self.assertEqual(memory._items, before)
        self.assertEqual({p.name:(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.root.iterdir()}, files)

    def test_saved_hash_source_links_to_real_user_context(self):
        row = conversation()
        evidence = dict(id=hashlib.sha256(row['source_ids'][0].encode()).hexdigest(),
                        episode_id='ep1', text='我喜欢绿茶', observed_at=NOW)
        graph = self.graph(self.snapshot([item(evidence_sources=[evidence])]), [row])
        nodes = self.nodes(graph, 'conversation')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['origin'], 'recent_context')
        self.assertEqual(nodes[0]['assistant'], row['assistant'])
        self.assertEqual([e['kind'] for e in graph['edges']].count('evidence'), 1)

    def test_compacted_source_is_labelled_excerpt_not_invented_message(self):
        evidence = dict(id='old-source', episode_id='ep1', text='我喜欢绿茶', observed_at=NOW-86400)
        graph = self.graph(self.snapshot([item(evidence_sources=[evidence])]))
        source = self.nodes(graph, 'conversation')[0]
        self.assertEqual(source['origin'], 'evidence_excerpt')
        self.assertEqual(source['body'], evidence['text'])
        self.assertEqual(source['assistant'], '')
        self.assertIn('来源摘录', source['label'])

    def test_same_source_id_with_nonmatching_user_span_cannot_make_evidence_edge(self):
        row = conversation(user='今天看了电影')
        evidence = dict(id=row['source_ids'][0], episode_id='ep', text='我喜欢绿茶', observed_at=NOW)
        graph = self.graph(self.snapshot([item(evidence_sources=[evidence])]), [row])
        targets = {e['source'] for e in graph['edges'] if e['kind'] == 'evidence'}
        real_node = next(n for n in self.nodes(graph, 'conversation') if n['origin'] == 'recent_context')
        self.assertNotIn(real_node['id'], targets)
        self.assertTrue(any(n['origin'] == 'evidence_excerpt' for n in self.nodes(graph, 'conversation')))

    def test_assistant_only_proactive_text_never_becomes_user_evidence(self):
        graph = self.graph(self.snapshot(), [conversation(user='', assistant='你喜欢绿茶。')])
        self.assertEqual(graph['nodes'], [])
        self.assertEqual(graph['edges'], [])

    def test_user_only_silent_turn_stays_experience_not_automatic_memory(self):
        graph = self.graph(self.snapshot(), [conversation(assistant='')])
        self.assertEqual(len(self.nodes(graph, 'conversation')), 1)
        self.assertEqual(self.nodes(graph, 'memory'), [])
        self.assertEqual(self.nodes(graph, 'conversation')[0]['assistant'], '')

    def test_unlinked_equal_account_strings_stay_separate(self):
        graph = self.graph(self.snapshot([item(), item('m2', platform='onebot')]),
                           [conversation(), conversation(platform='onebot')])
        self.assertEqual(len(self.nodes(graph, 'person')), 2)
        self.assertEqual(len({n['person_id'] for n in self.nodes(graph, 'memory')}), 2)
        self.assertEqual(len(self.nodes(graph, 'topic')), 2)

    def test_explicitly_linked_accounts_share_person_but_keep_channel_and_context(self):
        memory = self.snapshot([item(), item('m2', owner='123', platform='onebot')], identities=self.linked())
        graph = self.graph(memory, [conversation(turn_id='legacy'), conversation(owner='123', platform='onebot', turn_id='legacy')])
        self.assertEqual(len(self.nodes(graph, 'person')), 1)
        self.assertEqual(len(self.nodes(graph, 'conversation')), 2)
        self.assertEqual({n['platform'] for n in self.nodes(graph, 'conversation')}, {'ilink', 'onebot'})
        self.assertEqual(len(self.nodes(graph, 'person')[0]['accounts']), 2)

    def test_platform_and_person_filters_do_not_return_other_people_content(self):
        memory = self.snapshot([item(), item('bob', owner='bob', content='另一人的私事'), item('qq', platform='onebot')])
        graph = self.graph(memory, [conversation(), conversation(owner='bob', user='别人的对话')], platform='ilink')
        self.assertTrue(all(n.get('platform') != 'onebot' for n in graph['nodes']))
        alice = next(p['id'] for p in graph['people'] if p['label'] == 'alice')
        graph = self.graph(memory, [conversation(), conversation(owner='bob', user='别人的对话')], person=alice)
        self.assertNotIn('另一人的私事', json.dumps(graph['nodes'], ensure_ascii=False))
        self.assertNotIn('别人的对话', json.dumps(graph['nodes'], ensure_ascii=False))

    def test_dormant_and_superseded_hidden_unless_history_requested(self):
        memory = self.snapshot([item('old', last_user_evidence_at=NOW-15*86400),
                                item('retired', status='superseded'), item('live')])
        graph = self.graph(memory)
        self.assertEqual([n['memory_id'] for n in self.nodes(graph, 'memory')], ['live'])
        historical = self.graph(memory, include_history=True)
        self.assertEqual(len(self.nodes(historical, 'memory')), 3)
        dormant = self.graph(memory, include_history=True, phase='dormant')
        self.assertEqual([n['memory_id'] for n in self.nodes(dormant, 'memory')], ['old'])

    def test_history_displays_only_explicit_version_edges(self):
        graph = self.graph(self.snapshot([item('old', status='superseded', superseded_by='new'), item('new')]), include_history=True)
        versions = [e for e in graph['edges'] if e['kind'] == 'version']
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]['source'], identifier('memory', 'old'))
        self.assertEqual(versions[0]['target'], identifier('memory', 'new'))
        self.assertFalse(versions[0]['derived'])

    def test_version_pointer_cannot_link_different_people(self):
        graph = self.graph(self.snapshot([item('old', status='superseded', superseded_by='bob'),
                                          item('bob', owner='bob')]), include_history=True)
        self.assertFalse(any(e['kind'] == 'version' for e in graph['edges']))

    def test_temporary_expiry_and_pinned_lifecycle_respected(self):
        memory = self.snapshot([item('expired', phase='temporary', expires_at=NOW-1),
                                item('fixed', pinned=True, phase='pinned', last_user_evidence_at=0)])
        graph = self.graph(memory)
        self.assertEqual([n['memory_id'] for n in self.nodes(graph, 'memory')], ['fixed'])
        self.assertEqual(self.nodes(graph, 'memory')[0]['strength'], 1)

    def test_keyword_edges_only_derived_associations_not_causal(self):
        graph = self.graph(self.snapshot([item(), item('m2', content='用户收藏绿茶茶具')]))
        self.assertEqual(len(self.nodes(graph, 'topic')), 1)
        self.assertTrue(all(e['derived'] == (e['kind'] == 'keyword') for e in graph['edges']))
        self.assertFalse(any(e['kind'] in ('causes', 'inferred', 'inference') for e in graph['edges']))
        self.assertIn('不是模型内部思维', graph['notice'])

    def test_manual_memory_has_no_fabricated_source(self):
        graph = self.graph(self.snapshot([item(pinned=True, phase='pinned', source_chat='manual')]))
        memory = self.nodes(graph, 'memory')[0]
        self.assertEqual(memory['origin'], 'manual')
        self.assertIn('手动添加', memory['source_note'])
        self.assertEqual(self.nodes(graph, 'conversation'), [])
        self.assertFalse(any(e['kind'] == 'evidence' for e in graph['edges']))

    def test_source_and_keyword_fanout_and_total_nodes_bounded(self):
        sources = [dict(id=f'u{i}', episode_id=f'ep{i}', text=f'用户依据{i}', observed_at=NOW-i) for i in range(10)]
        graph = self.graph(self.snapshot([item(evidence_sources=sources, keywords=[f'词{i}' for i in range(12)])]), limit=20)
        self.assertLessEqual(len(self.nodes(graph, 'conversation')), 3)
        self.assertLessEqual(len(self.nodes(graph, 'topic')), 4)
        crowded = self.graph(self.snapshot([item(str(i)) for i in range(60)]), limit=20)
        self.assertLessEqual(len(crowded['nodes']), 20)
        self.assertTrue(crowded['coverage']['truncated'])
        ids = {n['id'] for n in crowded['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in crowded['edges']))

    def test_forgotten_item_and_old_context_do_not_reappear_including_history(self):
        memory = self.snapshot([item()], suppressions=[self.barrier()])
        graph = self.graph(memory, [conversation()], include_history=True, keyword='绿茶')
        self.assertNotIn('绿茶', json.dumps(graph['nodes'], ensure_ascii=False))
        self.assertEqual(graph['nodes'], [])

    def test_durable_forget_markers_override_stale_memory_snapshot(self):
        self.root.joinpath('memory-forgotten.json').write_text(json.dumps({'version':1, 'markers':[self.barrier()]}), encoding='utf-8')
        graph = self.graph(self.snapshot([item()]), [conversation()], include_history=True)
        self.assertEqual(graph['nodes'], [])

    def test_linked_channel_forgetting_blocks_both_channels_but_not_stranger(self):
        memory = self.snapshot([item(), item('qq', owner='123', platform='onebot'), item('other', owner='bob')],
                               identities=self.linked(), suppressions=[self.barrier()])
        graph = self.graph(memory, [conversation(), conversation(owner='123', platform='onebot'), conversation(owner='bob')], include_history=True)
        self.assertEqual([n['memory_id'] for n in self.nodes(graph, 'memory')], ['other'])
        self.assertEqual({n['person_id'] for n in self.nodes(graph, 'conversation')}, {identifier('person', 'ilink:bob')})

    def test_keyword_subset_of_forgotten_source_cannot_leak(self):
        barrier = self.barrier(content='需隐藏的信息', evidence='', source_texts=['项目代号秘密花园'])
        memory = self.snapshot([item(content='用户讨论一个项目', keywords=['秘密花园'])], suppressions=[barrier])
        graph = self.graph(memory)
        self.assertNotIn('秘密花园', json.dumps(graph['nodes'], ensure_ascii=False))
        self.assertEqual(self.nodes(graph, 'memory')[0]['keywords'], [])

    def test_assistant_paraphrases_hidden_with_person_barrier(self):
        memory = self.snapshot(suppressions=[self.barrier()])
        graph = self.graph(memory, [conversation(user='周末去哪玩', assistant='到你最喜欢的那家茶店吧')])
        self.assertEqual(self.nodes(graph, 'conversation')[0]['assistant'], '')
        self.assertEqual(self.nodes(graph, 'conversation')[0]['body'], '周末去哪玩')

    def test_group_assistant_cannot_leak_another_members_forgotten_fact(self):
        memory = self.snapshot(suppressions=[self.barrier()])
        graph = self.graph(memory, [conversation(owner='bob', platform='onebot', chat_type='group', chat_id='g',
                                               user='聊聊明天的安排', assistant='Alice说过他有一些私人安排')])
        self.assertEqual(self.nodes(graph, 'conversation')[0]['assistant'], '')

    def test_missing_platform_legacy_barrier_still_hides_assistant(self):
        memory = self.snapshot(suppressions=[self.barrier(accounts=[dict(platform='', owner_id='alice')])])
        graph = self.graph(memory, [conversation(user='周末去哪里', assistant='回到你的旧工作地点看看')])
        self.assertEqual(self.nodes(graph, 'conversation')[0]['assistant'], '')

    def test_malformed_barrier_fails_closed_instead_of_exposing_stale_data(self):
        for bad in ([{}], [{'id':'broken', 'accounts':'bad'}], 'bad'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.snapshot([item()], suppressions=bad)

    def test_invalid_lifecycle_record_is_omitted_and_reported(self):
        memory = self.snapshot([item(), item('bad', evidence_ids='wrong'), item('bad2', phase='fiction')])
        graph = self.graph(memory)
        self.assertEqual(graph['coverage']['invalid_items'], 2)
        self.assertEqual(graph['coverage']['memory_shown'], 1)

    def test_legacy_read_does_not_migrate_write_backup_or_renew_evidence(self):
        memory = self.snapshot([item(last_user_evidence_at=0, evidence='我喜欢绿茶', updated_at=NOW-20*86400, created_at=NOW-20*86400)], version=2)
        before = self.path.read_bytes()
        graph = self.graph(memory, include_history=True)
        self.assertEqual(self.nodes(graph, 'memory')[0]['phase'], 'dormant')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['memory.json'])

    def test_snapshot_read_budget(self):
        self.path.write_text('{"long":"content"}', encoding='utf-8')
        with patch('src.core.memory_graph.MAX_FILE_BYTES', 2):
            with self.assertRaises(ValueError):
                read_json(self.path)

    def test_context_rows_uses_only_current_named_stores_not_logs_summaries_backups(self):
        raw = dict(version=3, contexts={'ctx:ilink:private:alice':[dict(user='真实近期来话', assistant='', turn_id='one', ts=NOW)]},
                   summaries={'ctx:ilink:private:alice':'旧摘要中的秘密'}, summary_sources={'ctx:ilink:private:alice':['old']})
        self.root.joinpath('context-wechat.json').write_text(json.dumps(raw), encoding='utf-8')
        for filename in ('context-legacy.json', '.context-wechat.v2-backup.json', 'messages.jsonl', 'backend.log'):
            self.root.joinpath(filename).write_text('旧日志中的秘密', encoding='utf-8')
        rows, omitted = context_rows(self.root)
        self.assertEqual(omitted, 0)
        self.assertEqual([r['user'] for r in rows], ['真实近期来话'])
        self.assertNotIn('秘密', json.dumps(rows, ensure_ascii=False))

    def test_group_context_requires_unambiguous_real_sender_not_group_owner_guess(self):
        raw = dict(contexts={'ctx:onebot:group:789':[
            dict(user='[Alice]: 一条来话', assistant='', turn_id='a', source_ids=['onebot:group:789:123:m1'], ts=NOW),
            dict(user='来源未知', assistant='', source_ids=[], ts=NOW),
            dict(user='混合来源', assistant='', source_ids=['onebot:group:789:123:m2', 'onebot:group:789:456:m3'], ts=NOW),
        ]})
        self.root.joinpath('context-qq.json').write_text(json.dumps(raw), encoding='utf-8')
        rows, omitted = context_rows(self.root)
        self.assertEqual(omitted, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['owner_id'], '123')
        self.assertEqual(rows[0]['chat_id'], '789')

    def test_malformed_context_text_is_not_stringified_into_user_evidence(self):
        self.root.joinpath('context-wechat.json').write_text(json.dumps({'contexts':{'ctx:ilink:private:alice':[
            dict(user={'api_key':'not user prose'}, assistant=''), dict(user='normal', assistant='')
        ]}}), encoding='utf-8')
        rows, omitted = context_rows(self.root)
        self.assertEqual(omitted, 1)
        self.assertEqual([r['user'] for r in rows], ['normal'])


if __name__ == '__main__':
    unittest.main()
