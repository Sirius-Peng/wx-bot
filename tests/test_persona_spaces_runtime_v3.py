"""Runtime-level persona/relationship isolation using temporary user data only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.config_store import AppConfig, ConfigStore
from src.core.storage import _atomic_json, verify_backup
from src.runtime import BotRuntime


OWNER = 'runtime-fixture-wechat'
QQ_OWNER = 'runtime-fixture-qq'


def pinned(identifier='upgrade-original', content='用户喜欢茉莉茶'):
    return {'id': identifier, 'scope': 'user', 'owner_id': OWNER, 'category': 'preference',
            'content': content, 'platform': 'ilink', 'phase': 'pinned', 'pinned': True,
            'source_chat': 'manual', 'confidence': 1.0}


class PersonaSpacesRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()/'user-data'
        self.root.mkdir()
        self.env = patch.dict(os.environ, {'WXBOT_SKIP_NAPCAT_SYNC': '1'})
        self.env.start()
        self.store = ConfigStore(self.root/'config.json')
        cfg = AppConfig()
        cfg.bot.persona_profile, cfg.bot.persona_relationship = ('phrolova', 'trusted')
        cfg.bot.name, cfg.bot.persona = ('升级前名字', '升级前自定义补充人格')
        cfg.adapter.wechat_enabled = cfg.adapter.qq_enabled = False
        cfg.adapter.ilink_state_dir = str(self.root.parent/'unchanged-custom-ilink')
        self.store.save(cfg)
        self.original_payload = json.dumps({'version': 3, 'items': [pinned()], 'identities': []}).encode()
        (self.root/'memory.json').write_bytes(self.original_payload)
        _atomic_json(self.root/'ilink-account-fixture.json', {'token': 'fixture-only'})
        self.runtimes = []
        self.rt = self.new_runtime()

    def new_runtime(self):
        rt = BotRuntime(ConfigStore(self.root/'config.json'), self.root)
        self.runtimes.append(rt)
        return rt

    async def asyncTearDown(self):
        for rt in self.runtimes:
            await rt.stop()
        self.env.stop()
        self.temp.cleanup()

    def switch(self, profile, relationship, *, name=None, persona=None, persist=True):
        cfg = copy.deepcopy(self.rt.cfg)
        cfg.bot.persona_profile = profile
        cfg.bot.persona_relationship = relationship
        if name is not None: cfg.bot.name = name
        if persona is not None: cfg.bot.persona = persona
        # Public settings persists config before apply_config; mirror that contract
        # so a restart exercises both persisted selection and the routing registry.
        if persist: self.store.save(cfg)
        self.rt.apply_config(cfg)
        return self.rt._ensure_memory()

    def contents(self):
        return {row['content'] for row in self.rt.memory_list()['items']}

    def add(self, text, *, platform='ilink', owner=OWNER):
        result = self.rt.memory_add('user', owner, 'preference', text, platform=platform)
        self.assertTrue(result['ok'])
        return result['item']

    def assert_persona_metadata(self, name, persona):
        """Runtime, compatible config mirror and active snapshot describe one role."""
        stored = self.store.load().bot
        snapshot = json.loads((self.rt.persona_spaces.directory()/'persona.json').read_text())
        for bot in (self.rt.cfg.bot, stored):
            self.assertEqual((bot.name, bot.persona), (name, persona))
            self.assertEqual(snapshot['persona_profile'], bot.persona_profile)
            self.assertEqual(snapshot['persona_relationship'], bot.persona_relationship)
        self.assertEqual((snapshot['name'], snapshot['persona']), (name, persona))

    async def test_only_relationship_change_restores_its_name_persona_and_config_mirror(self):
        self.switch('phrolova', 'trusted', name='信任模式甲', persona='甲的独立人格文字')
        self.switch('phrolova', 'neutral', name='普通模式乙', persona='乙的独立人格文字')
        self.assert_persona_metadata('普通模式乙', '乙的独立人格文字')
        self.switch('phrolova', 'trusted')
        self.assert_persona_metadata('信任模式甲', '甲的独立人格文字')
        self.assertEqual(self.contents(), {'用户喜欢茉莉茶'})
        self.switch('phrolova', 'neutral')
        self.assert_persona_metadata('普通模式乙', '乙的独立人格文字')
        self.assertEqual(self.contents(), set())

    async def test_only_preset_change_restores_each_saved_persona_text(self):
        self.switch('phrolova', 'trusted', name='弗洛洛设定甲', persona='甲：克制而安静')
        self.switch('custom', 'trusted', name='自定义设定乙', persona='乙：轻快而直接')
        self.switch('phrolova', 'trusted')
        self.assert_persona_metadata('弗洛洛设定甲', '甲：克制而安静')
        self.switch('custom', 'trusted')
        self.assert_persona_metadata('自定义设定乙', '乙：轻快而直接')

    async def test_explicit_text_edit_during_mode_switch_is_not_replaced_by_old_snapshot(self):
        self.switch('phrolova', 'trusted', name='甲原名', persona='甲原人格')
        self.switch('custom', 'neutral', name='乙原名', persona='乙原人格')
        other_path = self.root/'persona-spaces/custom--neutral/persona.json'
        other_before = other_path.read_bytes()
        self.switch('phrolova', 'trusted', name='甲新名字', persona='这次明确保存的新人格')
        self.assert_persona_metadata('甲新名字', '这次明确保存的新人格')
        self.assertEqual(other_path.read_bytes(), other_before)
        self.switch('custom', 'neutral')
        self.assert_persona_metadata('乙原名', '乙原人格')
        self.switch('phrolova', 'trusted')
        self.assert_persona_metadata('甲新名字', '这次明确保存的新人格')

    async def test_restart_between_config_save_and_apply_recovers_target_settings_without_losing_edits(self):
        self.switch('phrolova', 'trusted', name='甲保存名', persona='甲保存人格')
        self.switch('custom', 'neutral', name='乙保存名', persona='乙保存人格')
        for explicit in (False, True):
            with self.subTest(explicit_text_edit=explicit):
                self.switch('custom', 'neutral')
                pending = copy.deepcopy(self.rt.cfg)
                pending.bot.persona_profile, pending.bot.persona_relationship = ('phrolova', 'trusted')
                if explicit:
                    pending.bot.name, pending.bot.persona = ('重启前明确改名', '重启前明确编辑的人格')
                self.store.save(pending)
                # Simulate process exit after config save, before apply_config.
                self.assertEqual(json.loads((self.root/'persona-state.json').read_text())['active_space'], 'custom--neutral')
                await self.rt.stop()
                self.rt = self.new_runtime()
                self.assertEqual(self.rt.persona_spaces.active_key, 'phrolova--trusted')
                expected = ('重启前明确改名', '重启前明确编辑的人格') if explicit else ('甲保存名', '甲保存人格')
                self.assert_persona_metadata(*expected)
                self.assertEqual(self.rt._ensure_memory().path, self.root/'memory.json')
                self.assertEqual(self.contents(), {'用户喜欢茉莉茶'})

    async def test_upgrade_root_belongs_to_current_combination_without_moving_files(self):
        memory = self.rt._ensure_memory()
        self.assertEqual(self.rt.persona_spaces.legacy_key, 'phrolova--trusted')
        self.assertEqual(memory.path, self.root/'memory.json')
        self.assertIn('用户喜欢茉莉茶', self.contents())
        info = self.rt.memory_list()['space']
        self.assertEqual(info['id'], 'phrolova--trusted')
        self.assertEqual(info['memory_file'], 'memory.json')
        self.assertEqual((self.root/'memory.json').read_bytes(), self.original_payload)
        state = json.loads((self.root/'persona-state.json').read_text())
        archive = self.root/state['upgrade_backup']
        manifest = verify_backup(archive)
        self.assertEqual(manifest['files']['memory.json'], hashlib.sha256(self.original_payload).hexdigest())

    async def test_change_relationship_starts_empty_and_restores_original_memory(self):
        original = self.rt._ensure_memory()
        new = self.switch('phrolova', 'neutral')
        self.assertEqual(new.path, self.root/'persona-spaces/phrolova--neutral/memory.json')
        self.assertEqual(self.contents(), set())
        self.add('用户喜欢冬天散步')
        restored = self.switch('phrolova', 'trusted')
        self.assertIs(restored, original)
        self.assertEqual(restored.path, self.root/'memory.json')
        self.assertEqual(self.contents(), {'用户喜欢茉莉茶'})
        self.switch('phrolova', 'neutral')
        self.assertEqual(self.contents(), {'用户喜欢冬天散步'})

    async def test_change_preset_starts_empty_even_with_same_relationship(self):
        self.rt._ensure_memory()
        memory = self.switch('custom', 'trusted', name='另一位角色', persona='另一位角色的人格')
        self.assertEqual(self.contents(), set())
        self.assertEqual(memory.path, self.root/'persona-spaces/custom--trusted/memory.json')
        self.add('用户喜欢山间清晨')
        self.switch('phrolova', 'trusted')
        self.assertEqual(self.contents(), {'用户喜欢茉莉茶'})
        self.switch('custom', 'trusted')
        self.assertEqual(self.contents(), {'用户喜欢山间清晨'})

    async def test_global_identity_links_follow_switch_without_copying_facts(self):
        first = self.rt._ensure_memory()
        identity = first.link_accounts(OWNER, QQ_OWNER, '同一个用户')
        memory = self.switch('custom', 'neutral')
        self.assertEqual(memory.identity_key('ilink', OWNER), 'person:'+identity['id'])
        self.assertEqual(memory.identity_key('onebot', QQ_OWNER), 'person:'+identity['id'])
        self.assertEqual(self.contents(), set())
        self.assertEqual(self.rt.memory_list()['identities'], [identity])
        self.add('用户喜欢手写信', platform='onebot', owner=QQ_OWNER)
        self.switch('phrolova', 'trusted')
        self.assertEqual(self.contents(), {'用户喜欢茉莉茶'})
        self.assertEqual(json.loads((self.root/'identity-links.json').read_text())['identities'], [identity])

    async def test_explicit_forgetting_purges_cached_and_disk_only_spaces_before_release(self):
        first = self.rt._ensure_memory()
        identity = first.link_accounts(OWNER, QQ_OWNER, '关联')
        self.switch('phrolova', 'neutral')
        self.add('用户喜欢茉莉茶')
        self.switch('custom', 'neutral')
        self.add('用户喜欢茉莉茶', platform='onebot', owner=QQ_OWNER)
        uncached = self.root/'persona-spaces/custom--rover'
        uncached.mkdir(parents=True)
        _atomic_json(uncached/'memory.json', {'version': 3, 'items': [pinned('disk-only')]})
        self.switch('phrolova', 'trusted')
        self.assertTrue(self.rt.memory_delete('upgrade-original')['ok'])
        barriers = json.loads((self.root/'memory-forgotten.json').read_text())['markers']
        self.assertEqual(len(barriers), 1)
        for profile, relationship in [('phrolova', 'neutral'), ('custom', 'neutral'), ('custom', 'rover')]:
            memory = self.switch(profile, relationship)
            self.assertEqual(self.contents(), set())
            self.assertTrue(memory.is_suppressed('ilink', OWNER, '用户喜欢茉莉茶'))
            self.assertTrue(memory.is_suppressed('onebot', QQ_OWNER, '用户喜欢茉莉茶'))
        self.assertTrue(self.rt.memory_allow_relearning(barriers[0]['id']))
        for profile, relationship in [('phrolova', 'trusted'), ('phrolova', 'neutral'), ('custom', 'neutral'), ('custom', 'rover')]:
            memory = self.switch(profile, relationship)
            self.assertEqual(self.contents(), set())
            self.assertFalse(memory.is_suppressed('ilink', OWNER, '用户喜欢茉莉茶'))
        self.assertEqual(self.rt.memory_list()['identities'], [identity])

    async def test_restarting_keeps_active_directory_and_original_legacy_binding(self):
        self.rt._ensure_memory()
        self.switch('custom', 'rover')
        self.add('用户收藏旧地图')
        await self.rt.stop()
        self.rt = self.new_runtime()
        memory = self.rt._ensure_memory()
        self.assertEqual(memory.path, self.root/'persona-spaces/custom--rover/memory.json')
        self.assertEqual(self.rt.memory_list()['space']['legacy_id'], 'phrolova--trusted')
        self.assertEqual(self.contents(), {'用户收藏旧地图'})
        self.switch('phrolova', 'trusted')
        self.assertEqual(self.contents(), {'用户喜欢茉莉茶'})

    async def test_persona_metadata_separate_from_knowledge_and_credentials_unchanged(self):
        self.rt._ensure_memory()
        source_before = (self.root/'memory.json').read_bytes()
        credential_before = (self.root/'ilink-account-fixture.json').read_bytes()
        self.switch('custom', 'neutral', name='自定义甲', persona='人格甲，不是一条用户事实')
        self.add('用户喜欢雕塑')
        target = self.root/'persona-spaces/custom--neutral'
        memory_raw = json.loads((target/'memory.json').read_text())
        persona_raw = json.loads((target/'persona.json').read_text())
        self.assertEqual(persona_raw['name'], '自定义甲')
        self.assertEqual(persona_raw['persona'], '人格甲，不是一条用户事实')
        self.assertEqual(persona_raw['persona_profile'], 'custom')
        self.assertEqual(persona_raw['persona_relationship'], 'neutral')
        self.assertFalse({'name', 'persona', 'persona_profile', 'persona_relationship'} & memory_raw.keys())
        self.assertNotIn('人格甲，不是一条用户事实', json.dumps(memory_raw, ensure_ascii=False))
        self.assertEqual((self.root/'memory.json').read_bytes(), source_before)
        self.assertEqual((self.root/'ilink-account-fixture.json').read_bytes(), credential_before)
        self.assertEqual(self.rt.cfg.adapter.ilink_state_dir, str(self.root.parent/'unchanged-custom-ilink'))

    async def test_invalid_registry_is_not_replaced_or_guessed_on_restart(self):
        await self.rt.stop()
        invalid = b'{"version":1,"legacy_space":"../outside","active_space":"custom--neutral"}'
        (self.root/'persona-state.json').write_bytes(invalid)
        before = (self.root/'memory.json').read_bytes()
        with self.assertRaises(ValueError):
            self.new_runtime()
        self.assertEqual((self.root/'persona-state.json').read_bytes(), invalid)
        self.assertEqual((self.root/'memory.json').read_bytes(), before)

    async def test_unknown_existing_persona_file_remains_unchanged(self):
        unknown = b'{"notes":"unknown user document; do not overwrite"}'
        target = self.root/'persona-spaces/custom--neutral'
        target.mkdir(parents=True)
        (target/'persona.json').write_bytes(unknown)
        previous = self.rt.persona_spaces.active_key
        cfg = copy.deepcopy(self.rt.cfg)
        cfg.bot.persona_profile, cfg.bot.persona_relationship = ('custom', 'neutral')
        with self.assertRaisesRegex(ValueError, '未知.*用户内容'):
            self.rt.apply_config(cfg)
        self.assertEqual((target/'persona.json').read_bytes(), unknown)
        self.assertEqual(self.rt.persona_spaces.active_key, previous)


if __name__ == '__main__':
    unittest.main()
