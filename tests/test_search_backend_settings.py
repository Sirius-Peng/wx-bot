"""Search/Fetch selection, configuration compatibility and independent save contract.

Only synthetic keys, temporary user data and local ASGI requests are used.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from src.config_store import AppConfig, ConfigStore
from src.core.web_search import DeepSeekWebSearch, configured_search


class SearchBackendConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ConfigStore(Path(self.tmp.name) / 'config.json')

    def test_new_and_legacy_missing_backend_use_tools(self):
        self.assertEqual(AppConfig().llm.search_backend, 'tools')
        cfg = self.store._from_dict({'llm': {'search_model': 'user-native-model', 'primary_model': 'user-primary'}})
        self.assertEqual(cfg.llm.search_backend, 'tools')
        self.assertEqual(cfg.llm.search_model, 'user-native-model')
        self.assertEqual(cfg.llm.primary_model, 'user-primary')

    def test_invalid_values_normalize_without_crashing(self):
        for value in ('other', '', None, True, 2, ['tools'], {'backend': 'tools'}):
            with self.subTest(value=value):
                cfg = self.store._from_dict({'llm': {'search_backend': value}})
                self.assertEqual(cfg.llm.search_backend, 'tools')

    def test_roundtrip_preserves_native_and_credentials(self):
        cfg = AppConfig()
        cfg.llm.search_backend = 'native'
        cfg.llm.search_model = 'custom-native'
        cfg.llm.search_key = 'fixture-secret-only'
        cfg.llm.primary_model = 'unchanged-main'
        self.store.save(cfg)
        self.assertEqual(asdict(self.store.load()), asdict(cfg))
        masked = self.store.masked(cfg)
        self.assertEqual(masked['llm']['search_backend'], 'native')
        self.assertNotIn('fixture-secret-only', json.dumps(masked))

    def test_tools_provider_selected_lazily_and_legacy_signature_supported(self):
        constructor = Mock(return_value=SimpleNamespace(key='fixture'))
        module = SimpleNamespace(DeepSeekSearchFetch=constructor)
        cfg = AppConfig().llm
        cfg.primary_key = 'fixture-primary'
        cfg.search_model = 'retained-native-setting'
        with patch.dict(sys.modules, {'src.core.deepseek_tools': module}):
            self.assertIs(configured_search(cfg), constructor.return_value)
        constructor.assert_called_once_with('fixture-primary', 'retained-native-setting', 'Asia/Shanghai')
        del cfg.search_backend
        constructor.reset_mock()
        with patch.dict(sys.modules, {'src.core.deepseek_tools': module}):
            configured_search(cfg)
        constructor.assert_called_once()

    def test_native_is_explicit_and_never_instantiates_tools(self):
        cfg = AppConfig().llm
        cfg.search_backend = 'native'
        cfg.primary_key = 'fixture-primary'
        constructor = Mock()
        with patch.dict(sys.modules, {'src.core.deepseek_tools': SimpleNamespace(DeepSeekSearchFetch=constructor)}):
            provider = configured_search(cfg)
        self.assertIsInstance(provider, DeepSeekWebSearch)
        self.assertEqual(provider.key, 'fixture-primary')
        constructor.assert_not_called()

    def test_programmatic_invalid_backend_never_enables_legacy_implicitly(self):
        cfg = AppConfig().llm
        cfg.search_backend = 'invalid'
        constructor = Mock()
        with patch.dict(sys.modules, {'src.core.deepseek_tools': SimpleNamespace(DeepSeekSearchFetch=constructor)}):
            configured_search(cfg)
        constructor.assert_called_once()

    def test_disabled_does_not_construct_provider(self):
        cfg = AppConfig().llm
        cfg.search_enabled = False
        constructor = Mock()
        with patch.dict(sys.modules, {'src.core.deepseek_tools': SimpleNamespace(DeepSeekSearchFetch=constructor)}):
            self.assertIsNone(configured_search(cfg))
        constructor.assert_not_called()

    def test_other_provider_credentials_never_reused(self):
        cfg = AppConfig().llm
        cfg.primary_key = 'fixture-not-deepseek'
        constructor = Mock()
        with patch.dict(sys.modules, {'src.core.deepseek_tools': SimpleNamespace(DeepSeekSearchFetch=constructor)}):
            for base in ('http://api.deepseek.com', 'https://api.deepseek.com.evil', 'https://api.deepseek.com:444', 'https://user:pass@api.deepseek.com', 'https://other.invalid/v1'):
                cfg.primary_base = base
                configured_search(cfg)
                self.assertEqual(constructor.call_args.args[0], '')
            cfg.search_key = 'fixture-independent'
            configured_search(cfg)
            self.assertEqual(constructor.call_args.args[0], 'fixture-independent')


class SearchBackendAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'WXBOT_DATA_DIR': self.tmp.name, 'WXBOT_ADMIN_TOKEN': 'search-backend-fixture', 'WXBOT_HOST': '127.0.0.1', 'WXBOT_SKIP_NAPCAT_SYNC': '1'})
        self.env.start()
        import src.server as server
        self.server = importlib.reload(server)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url='http://localhost', headers={'Authorization': 'Bearer search-backend-fixture'})
        self.apply = patch.object(self.server.runtime, 'apply_config', return_value=False)
        self.apply.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.apply.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def test_search_card_save_isolated_and_masks_keys(self):
        cfg = AppConfig()
        cfg.bot.persona = 'retain-persona'
        cfg.llm.primary_model = 'retain-primary'
        cfg.llm.search_model = 'retain-native-model'
        cfg.llm.primary_key = 'fixture-main-secret'
        self.server.config_store.save(cfg)
        result = await self.client.patch('/api/config/section/search', json={'search_backend': 'native', 'search_key': 'fixture-independent-secret'})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('fixture-independent-secret', result.text)
        self.assertNotIn('fixture-main-secret', result.text)
        saved = self.server.config_store.load()
        self.assertEqual(saved.llm.search_backend, 'native')
        self.assertEqual(saved.llm.search_model, 'retain-native-model')
        self.assertEqual(saved.llm.primary_model, 'retain-primary')
        self.assertEqual(saved.bot.persona, 'retain-persona')
        result = await self.client.patch('/api/config/section/search', json={'search_backend': 'tools'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.server.config_store.load().llm.search_model, 'retain-native-model')

    async def test_invalid_backend_rejected_in_section_and_whole_save(self):
        for value in ('unknown', '', None, True, [], {}):
            for method, url, body in (
                (self.client.patch, '/api/config/section/search', {'search_backend': value}),
                (self.client.put, '/api/config', {'llm': {'search_backend': value}})):
                with self.subTest(value=value, url=url):
                    self.assertEqual((await method(url, json=body)).status_code, 400)

    async def test_legacy_whole_save_preserves_explicit_native(self):
        cfg = AppConfig()
        cfg.llm.search_backend = 'native'
        cfg.llm.search_model = 'retain-search-model'
        self.server.config_store.save(cfg)
        raw = asdict(cfg)
        del raw['llm']['search_backend']
        result = await self.client.put('/api/config', json=raw)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.server.config_store.load().llm.search_backend, 'native')

    async def test_backend_cannot_be_saved_from_other_card(self):
        result = await self.client.patch('/api/config/section/primary', json={'search_backend': 'native'})
        self.assertEqual(result.status_code, 400)

    async def test_missing_field_config_exposed_as_tools(self):
        cfg = self.server.config_store._from_dict({'llm': {'primary_model': 'retain-main'}})
        self.server.config_store.save(cfg)
        result = await self.client.get('/api/config')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['llm']['search_backend'], 'tools')
        self.assertEqual(result.json()['llm']['primary_model'], 'retain-main')


if __name__ == '__main__':
    unittest.main()
