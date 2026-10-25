"""Cloud-only production wiring: synthetic config, no API or native worker."""
import asyncio
import copy
import importlib
import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.config_store import AppConfig, ConfigStore
from src.core.llm import LLMGateway
from src.runtime import BotRuntime


class FixtureAdapter:
    def __init__(self, platform):
        self.platform = platform
        self.stop = AsyncMock()

    def on_message(self, callback):
        self.callback = callback

    async def send_text(self, *args, **kwargs):
        return 'accepted'


class CloudProviderTests(unittest.TestCase):
    def test_official_deepseek_uses_flash_without_mutating_main(self):
        for base in ('https://api.deepseek.com', 'https://api.deepseek.com/v1/'):
            with self.subTest(base=base):
                cfg = AppConfig()
                cfg.llm.primary_base = base
                cfg.llm.primary_model = 'configured-deep-mode'
                cfg.llm.primary_key = 'fixture-main'
                before = copy.deepcopy(cfg)
                provider = BotRuntime._admission_provider(cfg)
                self.assertEqual((provider.base_url, provider.api_key, provider.model),
                                 (base, 'fixture-main', 'deepseek-flash'))
                self.assertEqual(cfg, before)
                self.assertEqual(provider.timeout, 3)

    def test_custom_provider_keeps_its_model(self):
        for base in ('https://proxy.example/v1', 'https://api.deepseek.com.example/v1'):
            cfg = AppConfig()
            cfg.llm.primary_base = base
            cfg.llm.primary_model = 'provider-specific-model'
            cfg.llm.primary_key = 'fixture-main'
            provider = BotRuntime._admission_provider(cfg)
            self.assertEqual((provider.base_url, provider.api_key, provider.model),
                             (base, 'fixture-main', 'provider-specific-model'))

    def test_existing_separate_provider_is_preserved(self):
        cfg = AppConfig()
        cfg.llm.primary_key = 'fixture-main'
        cfg.llm.judge_base = 'https://judge.example/v1'
        cfg.llm.judge_key = 'fixture-judge'
        cfg.llm.judge_model = 'chosen-judge'
        provider = BotRuntime._admission_provider(cfg)
        self.assertEqual((provider.base_url, provider.api_key, provider.model),
                         ('https://judge.example/v1', 'fixture-judge', 'chosen-judge'))

    def test_old_default_judge_fields_do_not_replace_main_credentials(self):
        cfg = AppConfig()
        cfg.llm.primary_key = 'fixture-main'
        self.assertFalse(cfg.llm.judge_key)
        self.assertNotEqual(cfg.llm.primary_base, cfg.llm.judge_base)
        provider = BotRuntime._admission_provider(cfg)
        self.assertEqual((provider.base_url, provider.api_key, provider.model),
                         (cfg.llm.primary_base, 'fixture-main', 'deepseek-flash'))

    def test_memory_budget_and_selected_model_are_unchanged(self):
        cfg = AppConfig()
        cfg.llm.primary_model = 'explicit-main-model'
        cfg.llm.primary_key = 'fixture-main'
        provider = BotRuntime._memory_provider(cfg)
        self.assertEqual((provider.model, provider.timeout, provider.max_tokens, provider.temperature),
                         ('explicit-main-model', 30, 1200, .2))
        cfg.llm.judge_key = 'fixture-judge'
        cfg.llm.judge_model = 'explicit-memory-model'
        provider = BotRuntime._memory_provider(cfg)
        self.assertEqual((provider.model, provider.api_key), ('explicit-memory-model', 'fixture-judge'))

    def test_runtime_source_does_not_import_or_construct_model_runner(self):
        import src.runtime as module
        source = inspect.getsource(module)
        for token in ('LocalDecisionRunner', '_ensure_decision_runner', 'local_decision import'):
            self.assertNotIn(token, source)


class CloudRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ConfigStore(self.root/'config.json')
        cfg = AppConfig()
        cfg.llm.primary_key = 'fixture-main'
        cfg.llm.primary_model = 'preserved-main-model'
        cfg.llm.search_enabled = False
        cfg.bot.memory_enabled = False
        cfg.adapter.wechat_enabled = cfg.adapter.qq_enabled = False
        self.store.save(cfg)
        self.runtime = BotRuntime(self.store, self.root)

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.temp.cleanup()

    def build(self, platform, slot):
        adapter = FixtureAdapter(platform)
        orch = self.runtime._build_orchestrator(adapter, slot)
        setattr(self.runtime, slot+'_adapter', adapter)
        setattr(self.runtime, slot+'_orch', orch)
        return orch

    async def test_three_channels_get_independent_real_cloud_gateways(self):
        with patch('src.core.local_decision.LocalDecisionRunner', side_effect=AssertionError('retired')):
            orches = [self.build('ilink', 'wechat'), self.build('onebot', 'qq'), self.build('desktop', 'desktop')]
            for orch in orches:
                gateway = orch.reply_decider.gateway
                self.assertIsInstance(gateway, LLMGateway)
                self.assertIsNot(gateway, orch.llm)
                self.assertIsNot(gateway, orch.judge.gateway)
                self.assertIsNot(gateway, orch.ctx.compactor.judge_llm)
                self.assertIsNone(getattr(orch.reply_decider, 'local_runner', None))
                self.assertEqual(gateway.primary.model, 'deepseek-flash')
                self.assertEqual(orch.llm.primary.model, 'preserved-main-model')
                self.assertEqual(orch.judge.gateway.primary.model, 'preserved-main-model')
                self.assertEqual(orch.ctx.compactor.judge_llm.primary.model, 'preserved-main-model')
                self.assertFalse(gateway._clients)
            self.assertEqual(len({id(o.reply_decider.gateway) for o in orches}), 3)

    async def test_start_learning_does_not_create_or_warm_model_worker(self):
        with patch('src.core.local_decision.LocalDecisionRunner', side_effect=AssertionError('retired')):
            self.runtime.start_learning()
            self.runtime.start_learning()
            await asyncio.sleep(0)
            self.assertIsNotNone(self.runtime._memory_usage_task)
            self.assertIsNone(getattr(self.runtime, '_decision_runner', None))
            self.assertIsNone(getattr(self.runtime, '_decision_warmup_task', None))

    async def test_construction_and_learning_work_without_native_model_dependencies(self):
        with patch.dict('sys.modules', {'src.core.local_decision': None,
                                      'onnxruntime': None, 'tokenizers': None}):
            orch = self.build('onebot', 'qq')
            self.runtime.start_learning()
            await asyncio.sleep(0)
            self.assertIsInstance(orch.reply_decider.gateway, LLMGateway)
            self.assertIsNone(getattr(orch.reply_decider, 'local_runner', None))

    async def test_reload_updates_every_channel_admission_in_place(self):
        orches = [self.build('ilink', 'wechat'), self.build('onebot', 'qq'), self.build('desktop', 'desktop')]
        original = [o.reply_decider.gateway for o in orches]
        cfg = copy.deepcopy(self.runtime.cfg)
        cfg.llm.primary_key = 'fixture-updated-main'
        cfg.llm.primary_model = 'updated-main-model'
        self.runtime.apply_config(cfg)
        for orch, gateway in zip(orches, original):
            self.assertIs(orch.reply_decider.gateway, gateway)
            self.assertEqual((gateway.primary.api_key, gateway.primary.model),
                             ('fixture-updated-main', 'deepseek-flash'))
            self.assertEqual(orch.judge.gateway.primary.model, 'updated-main-model')
            self.assertEqual(orch.ctx.compactor.judge_llm.primary.model, 'updated-main-model')
            self.assertIsNone(gateway.fallback)
            self.assertEqual(gateway.chat_mode, 'fast')

    async def test_reload_updates_existing_gateway_and_keeps_main_model(self):
        orch = self.build('onebot', 'qq')
        gateway = orch.reply_decider.gateway
        cfg = copy.deepcopy(self.runtime.cfg)
        cfg.llm.primary_base = 'https://custom.example/v1'
        cfg.llm.primary_key = 'fixture-new-main'
        cfg.llm.primary_model = 'custom-fast-model'
        self.runtime.apply_config(cfg)
        self.assertIs(orch.reply_decider.gateway, gateway)
        self.assertEqual((gateway.primary.base_url, gateway.primary.api_key, gateway.primary.model),
                         ('https://custom.example/v1', 'fixture-new-main', 'custom-fast-model'))
        cfg = copy.deepcopy(cfg)
        cfg.llm.judge_base = 'https://judge.example/v1'
        cfg.llm.judge_key = 'fixture-new-judge'
        cfg.llm.judge_model = 'user-chosen-judge'
        self.runtime.apply_config(cfg)
        self.assertEqual((gateway.primary.base_url, gateway.primary.api_key, gateway.primary.model),
                         ('https://judge.example/v1', 'fixture-new-judge', 'user-chosen-judge'))
        self.assertEqual((orch.llm.primary.api_key, orch.llm.primary.model),
                         ('fixture-new-main', 'custom-fast-model'))
        self.assertEqual(self.runtime.memory.llm.primary.model, 'user-chosen-judge')
        self.assertEqual(orch.judge.gateway.primary.model, 'user-chosen-judge')

    async def test_clearing_judge_key_restores_fast_default(self):
        orch = self.build('onebot', 'qq')
        cfg = copy.deepcopy(self.runtime.cfg)
        cfg.llm.judge_key = 'fixture-judge'
        cfg.llm.judge_model = 'user-model'
        self.runtime.apply_config(cfg)
        self.assertEqual(orch.reply_decider.gateway.primary.model, 'user-model')
        cfg = copy.deepcopy(cfg)
        cfg.llm.judge_key = ''
        self.runtime.apply_config(cfg)
        self.assertEqual(orch.reply_decider.gateway.primary.model, 'deepseek-flash')
        self.assertEqual(orch.reply_decider.gateway.primary.api_key, 'fixture-main')
        self.assertEqual(orch.judge.gateway.primary.model, 'preserved-main-model')

    async def test_stop_closes_cloud_gateways_without_waiting_for_old_worker(self):
        orch = self.build('onebot', 'qq')
        admission_close = AsyncMock(wraps=orch.reply_decider.gateway.aclose)
        judge_close = AsyncMock(wraps=orch.judge.gateway.aclose)
        chat_close = AsyncMock(wraps=orch.llm.aclose)
        orch.reply_decider.gateway.aclose = admission_close
        orch.judge.gateway.aclose = judge_close
        orch.llm.aclose = chat_close
        # An old integration may still attach these private fields. Production
        # teardown no longer touches native worker lifetime or its warmup task.
        self.runtime._decision_runner = object()
        self.runtime._decision_warmup_task = object()
        result = await asyncio.wait_for(self.runtime.stop(), 1)
        self.assertTrue(result['ok'])
        admission_close.assert_awaited_once()
        judge_close.assert_awaited_once()
        chat_close.assert_awaited_once()
        self.assertFalse(self.runtime.running)
        self.assertIsNone(self.runtime.qq_orch)

    async def test_status_reports_cloud_without_worker_probe(self):
        self.runtime._decision_runner = object()
        result = self.runtime.status()
        self.assertEqual(result['decision_backend'], 'cloud')
        self.assertEqual(result['decisions'], [])


class CloudDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'WXBOT_DATA_DIR': self.temp.name,
            'WXBOT_ADMIN_TOKEN': 'cloud-runtime-fixture', 'WXBOT_HOST': '127.0.0.1',
            'WXBOT_SKIP_NAPCAT_SYNC': '1'})
        self.env.start()
        import src.server as server
        self.server = importlib.reload(server)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
            base_url='http://localhost', headers={'Authorization': 'Bearer cloud-runtime-fixture'})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.server.runtime.stop()
        self.env.stop()
        self.temp.cleanup()

    async def test_diagnostics_legacy_field_is_explicitly_disabled(self):
        self.server.runtime._decision_runner = object()
        result = await self.client.get('/api/diagnostics')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['local_decision'],
                         {'ready': False, 'reason': 'disabled', 'backend': 'cloud'})
        self.assertEqual(result.json()['decision_backend'], 'cloud')

    async def test_companion_status_uses_cloud_backend(self):
        result = await self.client.get('/api/companion/status')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['local_decision'], 'disabled')
        self.assertEqual(result.json()['decision_backend'], 'cloud')

    async def test_diagnostics_includes_separate_admission_usage_not_secrets(self):
        rt = self.server.runtime
        adapter = FixtureAdapter('desktop')
        orch = rt._build_orchestrator(adapter, 'desktop')
        rt.desktop_adapter, rt.desktop_orch = adapter, orch
        orch.reply_decider.gateway.metrics.append({'model': 'judge', 'prompt_tokens': 10, 'completion_tokens': 1})
        orch.reply_decider.last_status = {'source': 'cloud', 'elapsed_ms': 400}
        result = await self.client.get('/api/diagnostics')
        channel = result.json()['channels']['desktop']
        self.assertEqual(channel['decision']['source'], 'cloud')
        self.assertEqual(channel['decision_requests'][0]['prompt_tokens'], 10)
        self.assertEqual(channel['requests'], [])
        self.assertNotIn('api_key', result.text)

    async def test_diagnostics_still_requires_admin_auth(self):
        self.client.headers.clear()
        self.assertEqual((await self.client.get('/api/diagnostics')).status_code, 401)
        self.assertEqual((await self.client.get('/api/companion/status')).status_code, 401)


if __name__ == '__main__':
    unittest.main()
