"""Optional WeChat TTS configuration stays separate, bounded and secret-masked."""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from src.config_store import AppConfig, ConfigStore, VoiceConfig


class VoiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'config.json';self.store=ConfigStore(self.path)

    def test_defaults_select_existing_fish_voice_without_enabling_unsaved_service(self):
        value=AppConfig().voice
        self.assertIsInstance(value,VoiceConfig)
        self.assertFalse(value.enabled)
        self.assertEqual((value.provider,value.base_url,value.fish_model),('fish','https://api.fish.audio','s2.1-pro'))
        self.assertEqual(value.model_id,'5b432f747d00419aba7cb12fa50e4af0')
        self.assertEqual((value.reply_rate,value.cooldown_seconds,value.max_chars,value.timeout_seconds),(.25,300,160,20))

    def test_legacy_configuration_gains_defaults_without_rewriting_persona_or_model(self):
        raw={'llm':{'primary_model':'my-model','primary_key':'my-key'},'bot':{'persona':'原人格'},'other':{'keep':True}}
        loaded=self.store._from_dict(raw)
        self.assertFalse(loaded.voice.enabled)
        self.assertEqual((loaded.llm.primary_model,loaded.llm.primary_key,loaded.bot.persona),('my-model','my-key','原人格'))
        self.assertEqual(raw,{'llm':{'primary_model':'my-model','primary_key':'my-key'},'bot':{'persona':'原人格'},'other':{'keep':True}})

    def test_voice_and_unknown_fields_roundtrip_independently(self):
        self.path.write_text(json.dumps({'voice':{'enabled':True,'provider':'gpt_sovits','base_url':'http://127.0.0.1:9880',
            'reference_audio':r'D:\角色\参考.wav','reference_text':'别急，演出还没有结束。','future':{'kept':1}},'future_root':[1,2]}))
        cfg=self.store.load();self.store.save(cfg)
        value=json.loads(self.path.read_text())
        self.assertEqual(value['voice']['reference_audio'],r'D:\角色\参考.wav')
        self.assertEqual(value['voice']['future'],{'kept':1});self.assertEqual(value['future_root'],[1,2])
        self.assertTrue(self.store.load().voice.enabled)

    def test_asdict_based_old_api_save_preserves_future_voice_fields(self):
        self.path.write_text(json.dumps({'voice':{'future_key':'retained'},'future_root':{'k':'v'}}))
        first=self.store.load();second=self.store._from_dict(asdict(first));self.store.save(second)
        saved=json.loads(self.path.read_text())
        self.assertEqual(saved['voice']['future_key'],'retained');self.assertEqual(saved['future_root'],{'k':'v'})

    def test_masked_output_does_not_leak_voice_key_or_unknown_secret(self):
        secret='voice-private-credential-abcdef'
        cfg=self.store._from_dict({'voice':{'api_key':secret,'unknown_secret':'other-secret'}})
        result=self.store.masked(cfg)
        self.assertEqual(result['voice']['api_key'],'•'*8+'cdef')
        self.assertNotIn(secret,json.dumps(result));self.assertNotIn('other-secret',json.dumps(result))
        self.assertEqual(cfg.voice.api_key,secret)

    def test_voice_key_does_not_share_llm_key(self):
        cfg=AppConfig();cfg.voice.api_key='voice-only';cfg.llm.primary_key='chat-only'
        self.store.save(cfg);loaded=self.store.load()
        self.assertEqual((loaded.voice.api_key,loaded.llm.primary_key),('voice-only','chat-only'))
        self.assertEqual(asdict(loaded.voice)['api_key'],'voice-only')

    def test_invalid_numbers_and_boolean_numbers_use_defaults(self):
        defaults=VoiceConfig()
        for field in ['reply_rate','cooldown_seconds','max_chars','timeout_seconds']:
            for value in [None,True,False,'5',[],{},float('nan'),float('inf'),float('-inf'),10**1000,-100]:
                with self.subTest(field=field,value=repr(value)):
                    self.assertEqual(getattr(self.store._from_dict({'voice':{field:value}}).voice,field),getattr(defaults,field))
        self.assertEqual(self.store._from_dict({'voice':{'max_chars':100.5}}).voice.max_chars,160)

    def test_boundary_numbers_and_explicit_zero_are_kept(self):
        for field,values in {'reply_rate':[0,1],'cooldown_seconds':[0,3600],'max_chars':[20,500],'timeout_seconds':[3,60]}.items():
            for value in values:
                with self.subTest(field=field,value=value):
                    self.assertEqual(getattr(self.store._from_dict({'voice':{field:value}}).voice,field),value)

    def test_provider_engine_language_and_types_are_normalized(self):
        cfg=self.store._from_dict({'voice':{'enabled':'true','provider':'other','fish_model':'implicit-unknown',
            'prompt_language':None,'text_language':{},'api_key':[],'reference_text':False}})
        self.assertFalse(cfg.voice.enabled);self.assertEqual(cfg.voice.provider,'other')
        self.assertEqual(cfg.voice.fish_model,'implicit-unknown')
        self.assertEqual((cfg.voice.prompt_language,cfg.voice.text_language),('zh','zh'))
        self.assertEqual((cfg.voice.api_key,cfg.voice.reference_text),('',''))
        for engine in ['s1','s2-pro','s2.1-pro','s2.1-pro-free','drama-3']:
            self.assertEqual(self.store._from_dict({'voice':{'fish_model':engine}}).voice.fish_model,engine)

    def test_explicit_invalid_fish_model_disables_instead_of_paid_fallback(self):
        from src.core.voice import validate_voice_config
        for engine in ['s2.1-pro-free-typo','',None,True,[],{},'x'*257]:
            with self.subTest(engine=repr(engine)):
                raw={'voice':{'enabled':True,'provider':'fish','api_key':'synthetic-key',
                              'model_id':'voice-id','fish_model':engine}}
                cfg=self.store._from_dict(raw)
                self.assertFalse(cfg.voice.enabled)
                self.assertNotEqual(cfg.voice.fish_model,'s2.1-pro')
                with self.assertRaises(ValueError):validate_voice_config(cfg.voice,require_ready=True)
                self.store.save(cfg)
                restored=self.store.load()
                self.assertFalse(restored.voice.enabled)
                self.assertNotEqual(restored.voice.fish_model,'s2.1-pro')

    def test_explicit_unknown_provider_never_becomes_enabled_fish(self):
        from src.core.voice import validate_voice_config
        for provider in ['fsh','',None,True,[],{},'x'*257]:
            with self.subTest(provider=repr(provider)):
                cfg=self.store._from_dict({'voice':{'enabled':True,'provider':provider,
                    'api_key':'synthetic-key','model_id':'voice-id','fish_model':'s2.1-pro'}})
                self.assertFalse(cfg.voice.enabled)
                self.assertNotEqual(cfg.voice.provider,'fish')
                with self.assertRaises(ValueError):validate_voice_config(cfg.voice,require_ready=True)

    def test_missing_selection_fields_keep_default_but_free_engine_is_not_changed(self):
        defaults=self.store._from_dict({'voice':{'api_key':'synthetic-key'}}).voice
        self.assertEqual((defaults.provider,defaults.fish_model),('fish','s2.1-pro'))
        self.assertFalse(defaults.enabled)
        configured=self.store._from_dict({'voice':{'enabled':True,'provider':'fish',
            'api_key':'synthetic-key','model_id':'voice-id','fish_model':'s2.1-pro-free'}}).voice
        self.assertTrue(configured.enabled)
        self.assertEqual(configured.fish_model,'s2.1-pro-free')

    def test_voice_raw_input_is_not_mutated(self):
        raw={'voice':{'enabled':True,'provider':'fish','api_key':'  key  ','future':{'a':[1]}}}
        before=copy.deepcopy(raw);cfg=self.store._from_dict(raw)
        self.assertEqual(raw,before);self.assertEqual(cfg.voice.api_key,'key')


if __name__=='__main__':unittest.main()
