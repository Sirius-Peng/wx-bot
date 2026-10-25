"""Admin-only diagnostic endpoint forwards observed usage, not settings or bills."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from src.core.usage_accounting import summary


class UsageDiagnosticsAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.temp.name,
            'WXBOT_ADMIN_TOKEN':'usage-api-fixture','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'})
        self.env.start()
        import src.server as server
        self.server=importlib.reload(server)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',
            headers={'Authorization':'Bearer usage-api-fixture'})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.server.runtime.stop()
        self.env.stop()
        self.temp.cleanup()

    async def test_returns_exact_summary_with_unknown_counts_preserved(self):
        report={'scope':'recent_runtime_events','observed_events':2,'requests':3,
            'summary':summary([{'task':'chat','prompt_tokens':1000,'completion_tokens':40,
                               'prompt_cache_hit_tokens':900,'prompt_cache_miss_tokens':100},
                              {'task':'search','completion_tokens':20}],attempts=3)}
        with patch.object(self.server.runtime,'usage_diagnostics',Mock(return_value=report),create=True) as diagnostic:
            response=await self.client.get('/api/diagnostics')
        self.assertEqual(response.status_code,200)
        diagnostic.assert_called_once_with()
        self.assertEqual(response.json()['usage'],report)
        cache=response.json()['usage']['summary']['cache']
        self.assertEqual(cache['hit_ratio'],.9)
        self.assertIsNone(cache['overall_hit_ratio'])
        self.assertEqual((cache['known_calls'],cache['unknown_calls']),(1,2))
        self.assertEqual(response.json()['decision_backend'],'cloud')
        self.assertEqual(response.json()['local_decision'],{'ready':False,'reason':'disabled','backend':'cloud'})

    async def test_empty_runtime_is_not_reported_as_zero_percent_hit(self):
        report={'scope':'recent_runtime_events','observed_events':0,'requests':0,'summary':summary([])}
        with patch.object(self.server.runtime,'usage_diagnostics',return_value=report,create=True):
            response=await self.client.get('/api/diagnostics')
        data=response.json()['usage']
        self.assertEqual(data['observed_events'],0)
        self.assertIsNone(data['summary']['cache']['hit_ratio'])
        self.assertIsNone(data['summary']['fields']['prompt_tokens']['observed_sum'])

    async def test_read_does_not_modify_model_config_or_create_chat_records(self):
        path=Path(self.temp.name)/'config.json'
        self.server.config_store.save(self.server.config_store.load())
        before=path.read_bytes()
        report={'scope':'recent_runtime_events','observed_events':0,'requests':0,'summary':summary([])}
        with patch.object(self.server.runtime,'usage_diagnostics',return_value=report,create=True):
            self.assertEqual((await self.client.get('/api/diagnostics')).status_code,200)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(Path(self.temp.name).glob('context-*.json')),[])

    async def test_unauthenticated_request_never_reads_runtime_diagnostics(self):
        self.client.headers.clear()
        with patch.object(self.server.runtime,'usage_diagnostics',return_value={},create=True) as diagnostic:
            response=await self.client.get('/api/diagnostics')
        self.assertEqual(response.status_code,401)
        diagnostic.assert_not_called()


if __name__=='__main__':unittest.main()
