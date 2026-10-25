"""Persistent-prefix configuration and runtime wiring; no model or API calls."""
import copy
from dataclasses import asdict
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.config_store import AppConfig, ConfigStore
from src.core.context import ContextManager
from src.core.llm import ProviderCfg
from src.core.types import WxMessage
from src.runtime import BotRuntime


class SessionConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ConfigStore(Path(self.temp.name)/'config.json')

    def test_defaults_and_legacy_config(self):
        self.assertEqual(AppConfig().llm.context_window_tokens, 0)
        self.assertEqual(AppConfig().llm.context_compact_ratio, .4)
        cfg=self.store._from_dict({'llm':{'primary_model':'kept'}})
        self.assertEqual((cfg.llm.primary_model,cfg.llm.context_window_tokens,cfg.llm.context_compact_ratio),('kept',0,.4))

    def test_invalid_file_values_return_to_defaults(self):
        store=self.store
        for value in (True,False,None,'65536',65536.0,-1,[],{}):
            with self.subTest(window=value):
                self.assertEqual(store._from_dict({'llm':{'context_window_tokens':value}}).llm.context_window_tokens,0)
        for value in (True,False,None,'0.4',0,.09,.81,1,2**2000,float('nan'),float('inf'),[],{}):
            with self.subTest(ratio=value):
                self.assertEqual(store._from_dict({'llm':{'context_compact_ratio':value}}).llm.context_compact_ratio,.4)

    def test_valid_values_roundtrip_preserving_unknown_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            path.write_text(json.dumps({'llm':{'context_window_tokens':123456,'context_compact_ratio':.3,'future_setting':'retained'},'other':{'x':1}}))
            store=ConfigStore(path);cfg=store.load();store.save(cfg)
            self.assertEqual((store.load().llm.context_window_tokens,store.load().llm.context_compact_ratio),(123456,.3))
            self.assertEqual(json.loads(path.read_text())['llm']['future_setting'],'retained')
            self.assertEqual(json.loads(path.read_text())['other'],{'x':1})

    def test_ratio_boundaries_are_accepted(self):
        store=self.store
        for ratio in (.1,.4,.8):
            self.assertEqual(store._from_dict({'llm':{'context_compact_ratio':ratio}}).llm.context_compact_ratio,ratio)


class SessionPolicyTests(unittest.TestCase):
    def options(self,cfg,provider=None):
        context=SimpleNamespace(configure_session=Mock())
        BotRuntime._configure_context_session(context,cfg,provider)
        context.configure_session.assert_called_once()
        return context.configure_session.call_args.kwargs

    def test_known_official_models_have_one_million_window(self):
        cfg=AppConfig()
        for base in ('https://api.deepseek.com','https://api.deepseek.com/v1/'):
            for model in ('deepseek-flash','deepseek-v4-pro'):
                cfg.llm.primary_base,cfg.llm.primary_model=base,model
                self.assertEqual(BotRuntime._context_window_tokens(cfg),1_000_000)

    def test_unknown_models_and_proxies_keep_conservative_window(self):
        cfg=AppConfig()
        for base,model in [('https://proxy.example/v1','deepseek-flash'),
                           ('https://api.deepseek.com.example','deepseek-flash'),
                           ('https://api.deepseek.com:8443','deepseek-flash'),
                           ('http://api.deepseek.com','deepseek-flash'),
                           ('https://user@api.deepseek.com','deepseek-flash'),
                           ('https://[malformed','deepseek-flash'),
                           ('https://api.deepseek.com','unknown-model')]:
            cfg.llm.primary_base,cfg.llm.primary_model=base,model
            self.assertEqual(BotRuntime._context_window_tokens(cfg),32_768)

    def test_explicit_window_wins_for_known_and_unknown_models(self):
        cfg=AppConfig();cfg.llm.context_window_tokens=65536
        self.assertEqual(BotRuntime._context_window_tokens(cfg),65536)
        provider=ProviderCfg('vision','https://vision.example','fixture','unknown')
        self.assertEqual(BotRuntime._context_window_tokens(cfg,provider),65536)

    def test_actual_selected_provider_can_use_conservative_window(self):
        cfg=AppConfig()
        provider=ProviderCfg('vision','https://vision.example','fixture','unknown',max_tokens=800)
        options=self.options(cfg,provider)
        self.assertEqual(options['context_window_tokens'],32_768)
        self.assertEqual(options['output_reserve_tokens'],800)

    def test_constructor_and_reload_default_provider_have_identical_fingerprint(self):
        cfg=AppConfig();cfg.llm.primary_key='private-fixture-value'
        first=self.options(cfg)
        provider=ProviderCfg('primary',cfg.llm.primary_base,cfg.llm.primary_key,cfg.llm.primary_model,timeout=60)
        second=self.options(cfg,provider)
        self.assertEqual(first,second)
        self.assertEqual(first['compact_ratio'],.4)
        self.assertEqual(first['output_reserve_tokens'],1024)
        self.assertRegex(first['provider_fingerprint'],r'^[0-9a-f]{64}$')
        self.assertNotIn('private-fixture-value',json.dumps(first))

    def test_primary_mode_vision_and_search_changes_invalidate_fingerprint(self):
        cfg=AppConfig();cfg.llm.primary_key='fixture-main';cfg.llm.vision_model='vision-one'
        first=self.options(cfg)['provider_fingerprint']
        changes=[('llm','primary_base','https://other.example/v1'),('llm','primary_model','another-model'),
                 ('llm','primary_key','different-fixture'),('bot','chat_mode','deep'),
                 ('llm','vision_base','https://vision.example'),('llm','vision_model','vision-two'),
                 ('llm','vision_key','different-vision-fixture'),('llm','search_enabled',False),
                 ('llm','search_model','different-search-model'),('llm','search_key','different-search-fixture')]
        for section,field,value in changes:
            changed=copy.deepcopy(cfg);setattr(getattr(changed,section),field,value)
            with self.subTest(field=field):
                self.assertNotEqual(self.options(changed)['provider_fingerprint'],first)

    def test_unrelated_setting_save_does_not_invalidate_provider_fingerprint(self):
        cfg=AppConfig();first=self.options(cfg)['provider_fingerprint']
        cfg.bot.memory_enabled=False;cfg.llm.judge_model='another-background-model'
        self.assertEqual(self.options(cfg)['provider_fingerprint'],first)

    def test_vision_context_uses_its_actual_provider_window(self):
        cfg=AppConfig();cfg.llm.vision_model='unknown-vision';cfg.llm.vision_base='https://vision.example/v1'
        context=SimpleNamespace(configure_session=Mock())
        BotRuntime._configure_context_session(context,cfg)
        self.assertEqual(context.configure_session.call_args.kwargs['context_window_tokens'],1_000_000)
        self.assertEqual(context.image_context_window_tokens,32_768)
        self.assertEqual(context.image_output_reserve_tokens,1024)
        cfg.llm.context_window_tokens=65536
        BotRuntime._configure_context_session(context,cfg)
        self.assertEqual(context.image_context_window_tokens,65536)

    def test_images_without_separate_vision_follow_the_selected_provider(self):
        cfg=AppConfig();context=SimpleNamespace(configure_session=Mock())
        BotRuntime._configure_context_session(context,cfg)
        self.assertEqual(context.image_context_window_tokens,1_000_000)
        provider=ProviderCfg('selected','https://other.example','fixture','unknown',max_tokens=800)
        BotRuntime._configure_context_session(context,cfg,provider)
        self.assertEqual((context.image_context_window_tokens,context.image_output_reserve_tokens),(32_768,800))

    def test_official_flash_image_limits_include_body_envelope_headroom(self):
        cfg=AppConfig();context=SimpleNamespace(configure_session=Mock())
        for base in ('https://api.deepseek.com','https://api.deepseek.com/v1/','https://api.deepseek.com:443'):
            cfg.llm.primary_base=base
            BotRuntime._configure_context_session(context,cfg)
            self.assertEqual((context.image_token_reserve,context.max_request_bytes,context.max_images),
                             (1024,46*1024*1024,600))
        cfg.llm.context_window_tokens=65536
        BotRuntime._configure_context_session(context,cfg)
        self.assertEqual((context.image_context_window_tokens,context.image_token_reserve),(65536,1024))

    def test_actual_vision_provider_determines_image_protocol_limits(self):
        cfg=AppConfig();context=SimpleNamespace(configure_session=Mock())
        cfg.llm.vision_base='https://vision.example/v1';cfg.llm.vision_model='unknown-vision'
        BotRuntime._configure_context_session(context,cfg)
        self.assertEqual((context.image_token_reserve,context.max_request_bytes,context.max_images),(4096,0,0))
        cfg.llm.primary_base='https://main.example/v1'
        cfg.llm.vision_base='https://api.deepseek.com';cfg.llm.vision_model='deepseek-flash'
        BotRuntime._configure_context_session(context,cfg)
        self.assertEqual((context.image_token_reserve,context.max_request_bytes,context.max_images),
                         (1024,46*1024*1024,600))

    def test_unknown_models_endpoints_and_proxies_do_not_inherit_flash_limits(self):
        cfg=AppConfig();context=SimpleNamespace(configure_session=Mock())
        for base,model in [('https://api.deepseek.com','deepseek-v4-pro'),
                           ('https://proxy.example/v1','deepseek-flash'),
                           ('https://api.deepseek.com.example','deepseek-flash'),
                           ('https://api.deepseek.com:8443','deepseek-flash'),
                           ('http://api.deepseek.com','deepseek-flash'),
                           ('https://user@api.deepseek.com','deepseek-flash'),
                           ('https://[malformed','deepseek-flash')]:
            with self.subTest(base=base,model=model):
                provider=ProviderCfg('selected',base,'fixture',model)
                BotRuntime._configure_context_session(context,cfg,provider)
                self.assertEqual((context.image_token_reserve,context.max_request_bytes,context.max_images),(4096,0,0))


class FixtureAdapter:
    def __init__(self,platform):
        self.platform=platform;self.stop=AsyncMock()
    def on_message(self,callback):self.callback=callback
    async def send_text(self,*args,**kwargs):return 'accepted'


class SessionRuntimeWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=ConfigStore(self.root/'config.json');cfg=AppConfig()
        cfg.llm.primary_key='fixture-main';cfg.llm.search_enabled=False
        cfg.bot.memory_enabled=False;cfg.companion.enabled=True
        cfg.adapter.wechat_enabled=cfg.adapter.qq_enabled=False
        self.store.save(cfg)
        # Record the wiring contract independently of session implementation.
        # Delegate when present so the real context's teardown remains exercised.
        original=getattr(ContextManager,'configure_session',None)
        def record(context,**options):
            context._wiring_options=options
            if original is not None:return original(context,**options)
        self.patch=patch.object(ContextManager,'configure_session',new=record,create=True)
        self.patch.start();self.runtime=BotRuntime(self.store,self.root)

    async def asyncTearDown(self):
        await self.runtime.stop();self.patch.stop();self.temp.cleanup()

    def build(self,platform='onebot',slot='qq'):
        adapter=FixtureAdapter(platform);orch=self.runtime._build_orchestrator(adapter,slot)
        setattr(self.runtime,slot+'_adapter',adapter);setattr(self.runtime,slot+'_orch',orch)
        return orch

    async def test_all_channels_receive_auto_window_ratio_and_reserve(self):
        for platform,slot in [('ilink','wechat'),('onebot','qq'),('desktop','desktop')]:
            orch=self.build(platform,slot);options=orch.ctx._wiring_options
            self.assertEqual((options['context_window_tokens'],options['compact_ratio'],options['output_reserve_tokens']),
                             (1_000_000,.4,1024))
            self.assertIs(orch.ctx.compactor.session_gateway,orch.llm)
            self.assertIsNot(orch.ctx.compactor.session_gateway,orch.judge.gateway)
            self.assertEqual((orch.ctx.image_token_reserve,orch.ctx.max_request_bytes,orch.ctx.max_images),
                             (1024,46*1024*1024,600))

    async def test_reload_applies_window_and_ratio_to_each_existing_context(self):
        orches=[self.build('ilink','wechat'),self.build('onebot','qq'),self.build('desktop','desktop')]
        before=[o.ctx._wiring_options['provider_fingerprint'] for o in orches]
        cfg=copy.deepcopy(self.runtime.cfg);cfg.llm.context_window_tokens=65536;cfg.llm.context_compact_ratio=.25
        self.runtime.apply_config(cfg)
        for orch,previous in zip(orches,before):
            self.assertEqual((orch.ctx._wiring_options['context_window_tokens'],orch.ctx._wiring_options['compact_ratio']),(65536,.25))
            self.assertEqual(orch.ctx._wiring_options['provider_fingerprint'],previous)
            self.assertIs(orch.ctx.compactor.session_gateway,orch.llm)

    async def test_reload_updates_vision_context_window_and_fingerprint(self):
        orch=self.build();before=orch.ctx._wiring_options['provider_fingerprint']
        cfg=copy.deepcopy(self.runtime.cfg);cfg.llm.vision_base='https://vision.example/v1';cfg.llm.vision_model='vision-model'
        self.runtime.apply_config(cfg)
        self.assertEqual(orch.ctx.image_context_window_tokens,32_768)
        self.assertEqual(orch.ctx.image_output_reserve_tokens,1024)
        self.assertNotEqual(orch.ctx._wiring_options['provider_fingerprint'],before)
        self.assertEqual((orch.ctx.image_token_reserve,orch.ctx.max_request_bytes,orch.ctx.max_images),(4096,0,0))
        cfg=copy.deepcopy(cfg);cfg.llm.vision_model=''
        self.runtime.apply_config(cfg)
        self.assertEqual((orch.ctx.image_token_reserve,orch.ctx.max_request_bytes,orch.ctx.max_images),
                         (1024,46*1024*1024,600))

    async def test_unrelated_save_keeps_fingerprint_while_model_mode_and_key_invalidate(self):
        orch=self.build();previous=orch.ctx._wiring_options['provider_fingerprint']
        cfg=copy.deepcopy(self.runtime.cfg);cfg.bot.late_reply_enabled=not cfg.bot.late_reply_enabled
        self.runtime.apply_config(cfg)
        self.assertEqual(orch.ctx._wiring_options['provider_fingerprint'],previous)
        for section,field,value in [('llm','primary_model','other'),('bot','chat_mode','deep'),('llm','primary_key','new-fixture')]:
            cfg=copy.deepcopy(self.runtime.cfg);setattr(getattr(cfg,section),field,value)
            self.runtime.apply_config(cfg)
            current=orch.ctx._wiring_options['provider_fingerprint']
            self.assertNotEqual(current,previous);previous=current

    async def test_switch_and_restore_persona_contexts_receive_current_policy(self):
        orch=self.build();original=orch.ctx
        cfg=copy.deepcopy(self.runtime.cfg);cfg.bot.persona_relationship='trusted'
        cfg.llm.context_window_tokens=98304;cfg.llm.context_compact_ratio=.3
        self.runtime.apply_config(cfg)
        self.assertIsNot(orch.ctx,original)
        self.assertEqual(orch.ctx._wiring_options['context_window_tokens'],98304)
        cfg=copy.deepcopy(self.runtime.cfg);cfg.bot.persona_relationship='neutral';cfg.llm.context_compact_ratio=.5
        self.runtime.apply_config(cfg)
        self.assertIs(orch.ctx,original)
        self.assertEqual(original._wiring_options['compact_ratio'],.5)
        self.assertIs(original.compactor.session_gateway,orch.llm)

    async def test_inactive_context_creation_gets_session_policy_without_changing_path(self):
        orch=self.build();inactive='custom--trusted'
        message=WxMessage('inactive-fixture','onebot','private','p_u','u','用户','bot','保存原来话',persona_space=inactive)
        await self.runtime._accept_inactive_turn(orch,message)
        context=orch._persona_contexts[inactive]
        self.assertEqual(context._wiring_options['context_window_tokens'],1_000_000)
        self.assertEqual(context.store.path.name,orch.context_filename)
        self.assertEqual(context.store.path.parent,self.runtime.persona_spaces.directory(inactive))
        self.assertIs(context.compactor.session_gateway,orch.llm)


class SessionSettingsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.temp.name,'WXBOT_ADMIN_TOKEN':'session-settings-fixture',
            'WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'})
        self.env.start();import src.server as server;self.server=importlib.reload(server)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',
            headers={'Authorization':'Bearer session-settings-fixture'})

    async def asyncTearDown(self):
        await self.client.aclose();await self.server.runtime.stop();self.env.stop();self.temp.cleanup()

    async def test_primary_card_saves_session_settings_without_other_cards(self):
        before=self.server.config_store.load();before.bot.persona='unchanged-persona';before.llm.judge_model='unchanged-judge'
        self.server.config_store.save(before)
        result=await self.client.patch('/api/config/section/primary',json={'context_window_tokens':65536,'context_compact_ratio':.3})
        self.assertEqual(result.status_code,200)
        cfg=self.server.config_store.load()
        self.assertEqual((cfg.llm.context_window_tokens,cfg.llm.context_compact_ratio),(65536,.3))
        self.assertEqual((cfg.bot.persona,cfg.llm.judge_model),('unchanged-persona','unchanged-judge'))

    async def test_patch_rejects_invalid_values(self):
        for values in ({'context_window_tokens':True},{'context_window_tokens':-1},{'context_window_tokens':65536.5},
                       {'context_window_tokens':'65536'},{'context_compact_ratio':True},
                       {'context_compact_ratio':.09},{'context_compact_ratio':.81},{'context_compact_ratio':'0.4'}):
            with self.subTest(values=values):
                result=await self.client.patch('/api/config/section/primary',json=values)
                self.assertEqual(result.status_code,400)

    async def test_session_fields_cannot_be_saved_by_another_card(self):
        self.assertEqual((await self.client.patch('/api/config/section/search',json={'context_window_tokens':10000})).status_code,400)

    async def test_legacy_whole_config_save_keeps_new_preferences(self):
        cfg=self.server.config_store.load();cfg.llm.context_window_tokens=131072;cfg.llm.context_compact_ratio=.6
        self.server.config_store.save(cfg)
        raw=asdict(cfg);del raw['llm']['context_window_tokens'];del raw['llm']['context_compact_ratio']
        result=await self.client.put('/api/config',json=raw)
        self.assertEqual(result.status_code,200)
        reloaded=self.server.config_store.load()
        self.assertEqual((reloaded.llm.context_window_tokens,reloaded.llm.context_compact_ratio),(131072,.6))

    async def test_whole_config_api_also_rejects_invalid_session_values(self):
        raw=asdict(AppConfig());raw['llm']['context_compact_ratio']=40
        self.assertEqual((await self.client.put('/api/config',json=raw)).status_code,400)

    async def test_admin_auth_is_required(self):
        self.client.headers.clear()
        result=await self.client.patch('/api/config/section/primary',json={'context_window_tokens':65536})
        self.assertEqual(result.status_code,401)


if __name__=='__main__':unittest.main()
