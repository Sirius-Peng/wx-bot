"""Authenticated topology API with isolated on-disk fixtures, no real accounts."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from tests.test_memory_graph import item, conversation


class MemoryGraphAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'WXBOT_DATA_DIR':str(self.root),
                         'WXBOT_ADMIN_TOKEN':'topology-test-token', 'WXBOT_HOST':'127.0.0.1',
                         'WXBOT_SKIP_NAPCAT_SYNC':'1', 'WXBOT_MIGRATE_LEGACY':'0'})
        self.env.start()
        import src.server as server
        self.server = importlib.reload(server)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                       base_url='http://localhost',
                                       headers={'Authorization':'Bearer topology-test-token'})
        self.write_memory([item(created_at=time.time(), updated_at=time.time(), last_user_evidence_at=time.time())])
        row = conversation()
        self.root.joinpath('context-wechat.json').write_text(json.dumps({
            'version':3, 'contexts':{'ctx:ilink:private:alice':[
                {k:row[k] for k in ('user','assistant','turn_id','source_ids','ts')}]}}), encoding='utf-8')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.temp.cleanup()

    def write_memory(self, items, **extra):
        value = {'version':3, 'items':items, 'identities':[], 'suppressions':[]}
        value.update(extra)
        self.root.joinpath('memory.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def file_snapshot(self):
        return {p.relative_to(self.root).as_posix():(p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}

    async def test_graph_requires_admin_session_and_matching_origin(self):
        self.client.headers.clear()
        self.assertEqual((await self.client.get('/api/memory/graph')).status_code, 401)
        self.client.headers['Authorization'] = 'Bearer topology-test-token'
        result = await self.client.get('/api/memory/graph', headers={'Origin':'https://unrelated.invalid'})
        self.assertEqual(result.status_code, 403)
        self.assertNotIn('用户喜欢绿茶', result.text)

    async def test_default_snapshot_shape_and_unknown_person_no_private_nodes(self):
        result = await self.client.get('/api/memory/graph')
        self.assertEqual(result.status_code, 200)
        value = result.json()
        self.assertEqual(value['version'], 1)
        self.assertEqual(result.headers.get('cache-control'), 'no-store')
        self.assertIn('nodes', value)
        self.assertIn('edges', value)
        self.assertEqual(value['stats']['memory'], 1)
        self.assertEqual(value['stats']['conversation'], 1)
        self.assertEqual(value['coverage']['limit'], 160)
        self.assertIn('omitted_context_turns', value['coverage'])
        result = await self.client.get('/api/memory/graph', params={'person':'person:unknown'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['nodes'], [])

    async def test_filters_and_validation(self):
        result = await self.client.get('/api/memory/graph', params={
            'platform':'ilink', 'phase':'candidate', 'keyword':'绿茶', 'limit':20, 'include_history':'true'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['filters']['phase'], 'candidate')
        self.assertEqual(result.json()['stats']['memory'], 1)
        for values in ({'platform':'wechat'}, {'phase':'temp'}, {'keyword':'a'*121},
                       {'person':'p'*101}, {'limit':19}, {'limit':301}):
            with self.subTest(values=values):
                self.assertEqual((await self.client.get('/api/memory/graph', params=values)).status_code, 400)
        for values in ({'include_history':'unknown'}, {'limit':'nan'}, {'limit':'abc'}):
            with self.subTest(values=values):
                self.assertEqual((await self.client.get('/api/memory/graph', params=values)).status_code, 422)

    async def test_view_does_not_initialize_learning_or_write_any_source_file(self):
        before = self.file_snapshot()
        with patch.object(self.server.runtime, '_ensure_memory', side_effect=AssertionError('graph must not initialize writer')):
            for _ in range(3):
                result = await self.client.get('/api/memory/graph')
                self.assertEqual(result.status_code, 200)
        self.assertIsNone(self.server.runtime.memory)
        self.assertEqual(self.file_snapshot(), before)
        self.assertFalse(self.root.joinpath('memory-learning.json').exists())

    async def test_forget_markers_filter_stale_disk_context_even_with_history(self):
        marker = {'id':'deleted', 'accounts':[{'platform':'ilink','owner_id':'alice'}],
                  'content':'用户喜欢绿茶', 'evidence':'我喜欢绿茶', 'source_texts':[], 'fact_key':''}
        self.root.joinpath('memory-forgotten.json').write_text(json.dumps({'version':1, 'markers':[marker]}), encoding='utf-8')
        before = self.file_snapshot()
        result = await self.client.get('/api/memory/graph', params={'include_history':'true'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['nodes'], [])
        self.assertEqual(self.file_snapshot(), before)

    async def test_graph_ignores_physical_logs_old_backups_and_unsourced_summaries(self):
        for name in ('messages.jsonl', 'backend.log', '.context-wechat.v2-backup.json', 'context-legacy.json'):
            self.root.joinpath(name).write_text('secret-old-raw-log', encoding='utf-8')
        path = self.root/'context-wechat.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value.update(summaries={'ctx:ilink:private:alice':'secret-old-raw-log'},
                     summary_sources={'ctx:ilink:private:alice':['unverified']})
        path.write_text(json.dumps(value), encoding='utf-8')
        result = await self.client.get('/api/memory/graph')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('secret-old-raw-log', result.text)

    async def test_broken_durable_barrier_fails_closed_without_sensitive_errors(self):
        path = self.root/'memory-forgotten.json'
        path.write_text('{"markers":[{"content":"do-not-echo-private-data"}]}', encoding='utf-8')
        original = path.read_bytes()
        result = await self.client.get('/api/memory/graph')
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('do-not-echo-private-data', result.text)
        self.assertNotIn(str(self.root), result.text)
        self.assertEqual(path.read_bytes(), original)

    async def test_invalid_context_and_oversized_snapshots_fail_closed(self):
        path = self.root/'context-wechat.json'
        path.write_text('{broken-private-context', encoding='utf-8')
        result = await self.client.get('/api/memory/graph')
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('broken-private-context', result.text)
        path.unlink()
        with patch('src.core.memory_graph.MAX_FILE_BYTES', 5):
            self.assertEqual((await self.client.get('/api/memory/graph')).status_code, 503)

    async def test_refresh_reads_new_data_without_restarting_bot(self):
        first = (await self.client.get('/api/memory/graph')).json()
        self.assertEqual(first['stats']['memory'], 1)
        self.write_memory([item('new', content='用户喜欢爵士乐', keywords=['爵士'], last_user_evidence_at=time.time())])
        updated = (await self.client.get('/api/memory/graph')).json()
        self.assertNotIn('用户喜欢绿茶', json.dumps(updated['nodes'], ensure_ascii=False))
        self.assertIn('用户喜欢爵士乐', json.dumps(updated['nodes'], ensure_ascii=False))
        self.assertFalse(self.server.runtime.running)


if __name__ == '__main__':
    unittest.main()
