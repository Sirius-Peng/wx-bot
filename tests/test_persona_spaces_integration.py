"""Temporary fixtures only: persona namespaces survive backup/import without mixing.

Identity links and explicit forgetting are global by design. Facts, raw contexts,
summaries, learning and usage live under the selected persona/relationship pair.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.config_store import AppConfig, ConfigStore
from src.core.data_import import inspect_import, prepare_import, import_status, _publish
from src.core.persona_spaces import PersonaSpaces
from src.core.storage import initialize_storage, create_backup, verify_backup, _atomic_json


OWNER = 'fixture-contact'
CTX = 'ctx:ilink:private:' + OWNER


def write_json(root, name, value):
    path = root/name
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, value)


def fact(identifier, text):
    return {'id': identifier, 'scope': 'user', 'owner_id': OWNER, 'category': 'preference',
            'platform': 'ilink', 'content': text, 'source_chat': 'manual', 'pinned': True, 'phase': 'pinned'}


def context(text):
    return {'version': 3, 'contexts': {CTX: [
        {'user': text, 'assistant': '旧回答'+text, 'turn_id': 'old', 'ts': 1},
        {'user': '新的普通上下文', 'assistant': '普通回答', 'turn_id': 'allowed', 'ts': 2}]},
        'summaries': {CTX: text}, 'summary_sources': {CTX: ['old']}, 'suppressions': []}


def marker(identifier, text='用户喜欢绿茶'):
    return {'id': identifier, 'scope': 'user', 'accounts': [
        {'platform': 'ilink', 'owner_id': OWNER}, {'platform': 'onebot', 'owner_id': 'qq-fixture'}],
        'content': text, 'source_texts': [text], 'created_at': 1}


def seed(root, label, *, legacy=('custom', 'neutral'), active=('phrolova', 'trusted')):
    root.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig()
    cfg.bot.persona_profile, cfg.bot.persona_relationship = legacy
    cfg.bot.name = label+' legacy'
    cfg.bot.persona = label+' 原始人格'
    cfg.llm.primary_key = 'fixture-key'
    cfg.adapter.ilink_state_dir = str(root/'ilink')
    store = ConfigStore(root/'config.json')
    store.save(cfg)
    spaces = PersonaSpaces(root, cfg.bot)
    write_json(root, 'memory.json', {'version': 3, 'items': [fact(label+'-legacy', label+' 根记忆')]})
    write_json(root, 'context-ilink.json', context('用户喜欢绿茶'))
    cfg.bot = copy.deepcopy(cfg.bot)
    cfg.bot.persona_profile, cfg.bot.persona_relationship = active
    cfg.bot.name = label+' active'
    cfg.bot.persona = label+' 另一套人格'
    spaces.activate(cfg.bot)
    folder = spaces.directory()
    write_json(folder, 'memory.json', {'version': 3, 'items': [fact(label+'-nested', label+' 子空间记忆')]})
    write_json(folder, 'context-onebot.json', context('用户喜欢绿茶'))
    write_json(folder, 'memory-learning.json', {'version': 2, 'pending': {}, 'seen': [], 'contexts': []})
    write_json(folder, 'memory-usage.json', {'version': 1, 'items': {label+'-nested': {'hits': 2, 'last_used_at': 2}}})
    write_json(root, 'identity-links.json', {'version': 1, 'identities': [
        {'id': 'person-fixture', 'label': 'fixture', 'accounts': [
            {'platform': 'ilink', 'owner_id': OWNER}, {'platform': 'onebot', 'owner_id': 'qq-fixture'}]}]})
    write_json(root, 'ilink/account.json', {'bot_token': 'fixture-token'})
    (root/'unknown-user-note.bin').write_bytes(b'keep user file')
    store.save(cfg)
    return spaces, folder


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '.backups' not in p.parts and not p.name.startswith('.storage')}


class PersonaSpacesImport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.old = self.base/'旧人格资料'
        self.new = self.base/'现有人格资料'
        self.old_spaces, self.old_active = seed(self.old, 'source')
        self.new_spaces, self.new_active = seed(self.new, 'target', legacy=('phrolova', 'neutral'), active=('custom', 'rover'))

    def tearDown(self):
        self.temp.cleanup()

    def queue(self):
        preview = inspect_import(str(self.old), self.new)
        return prepare_import(str(self.old), self.new, preview['fingerprint'])

    def apply(self):
        initialize_storage(self.new, migrate=False)
        return import_status(self.new)

    def test_backup_covers_registry_namespaces_global_controls_and_credentials(self):
        before = hashes(self.old)
        archive = create_backup(self.old)
        manifest = verify_backup(archive)
        for name in ('persona-state.json', 'persona.json', 'identity-links.json', 'ilink/account.json',
                     'persona-spaces/phrolova--trusted/memory.json',
                     'persona-spaces/phrolova--trusted/memory-learning.json',
                     'persona-spaces/phrolova--trusted/memory-usage.json',
                     'persona-spaces/phrolova--trusted/context-onebot.json'):
            self.assertEqual(manifest['files'][name], before[name])
        self.assertEqual(hashes(self.old), before)

    def test_preview_reports_nested_only_memory_and_contexts(self):
        (self.old/'memory.json').unlink()
        (self.old/'context-ilink.json').unlink()
        preview = inspect_import(str(self.old), self.new)
        self.assertTrue(preview['includes']['memory'])
        self.assertTrue(preview['includes']['contexts'])
        self.assertEqual(preview['includes']['persona_spaces'], 2)
        self.assertNotIn('fixture-key', json.dumps(preview))
        self.assertNotIn('source 子空间记忆', json.dumps(preview))

    def test_import_replaces_whole_registry_without_mixing_target_spaces(self):
        source_hashes = hashes(self.old)
        self.queue()
        self.assertEqual(self.apply()['status'], 'imported')
        cfg = ConfigStore(self.new/'config.json').load()
        spaces = PersonaSpaces(self.new, cfg.bot)
        self.assertEqual(spaces.legacy_key, 'custom--neutral')
        self.assertEqual(spaces.active_key, 'phrolova--trusted')
        self.assertEqual(spaces.directory(), self.new/'persona-spaces/phrolova--trusted')
        self.assertFalse((self.new/'persona-spaces/custom--rover').exists())
        self.assertEqual(json.loads((self.new/'memory.json').read_text())['items'][0]['id'], 'source-legacy')
        self.assertEqual(json.loads((spaces.directory()/'memory.json').read_text())['items'][0]['id'], 'source-nested')
        self.assertEqual((self.new/'ilink/account.json').read_bytes(), (self.old/'ilink/account.json').read_bytes())
        self.assertEqual(hashes(self.old), source_hashes)

    def test_old_directory_without_registry_binds_imported_config_not_local_choice(self):
        import shutil
        shutil.rmtree(self.old/'persona-spaces')
        (self.old/'persona-state.json').unlink()
        (self.old/'persona.json').unlink()
        cfg = ConfigStore(self.old/'config.json').load()
        cfg.bot.persona_profile, cfg.bot.persona_relationship = ('custom', 'trusted')
        ConfigStore(self.old/'config.json').save(cfg)
        self.queue()
        self.assertEqual(self.apply()['status'], 'imported')
        loaded = ConfigStore(self.new/'config.json').load()
        spaces = PersonaSpaces(self.new, loaded.bot)
        self.assertEqual(spaces.legacy_key, 'custom--trusted')
        self.assertEqual(spaces.directory(), self.new)
        self.assertEqual(json.loads((self.new/'memory.json').read_text())['items'][0]['id'], 'source-legacy')

    def test_orphan_spaces_rejected_before_prepare(self):
        (self.old/'persona-state.json').unlink()
        with self.assertRaisesRegex(ValueError, '缺少 persona-state'):
            inspect_import(str(self.old), self.new)
        self.assertEqual(json.loads((self.new/'memory.json').read_text())['items'][0]['id'], 'target-legacy')

    def test_registry_directory_is_rejected_instead_of_published_as_file(self):
        (self.old/'persona-state.json').unlink()
        (self.old/'persona-state.json').mkdir()
        with self.assertRaisesRegex(ValueError, '人格空间'):
            inspect_import(str(self.old), self.new)

    def test_invalid_registry_rejected_without_writes(self):
        before = hashes(self.new)
        for value in [[], {'version': 9}, {'version': 1, 'legacy_space': '../outside', 'active_space': 'custom--neutral'},
                      {'version': 1, 'legacy_space': 'custom--neutral', 'active_space': 'unknown--trusted'}]:
            write_json(self.old, 'persona-state.json', value)
            with self.assertRaisesRegex(ValueError, '人格空间'):
                inspect_import(str(self.old), self.new)
        self.assertEqual(hashes(self.new), before)

    def test_unknown_namespace_and_duplicate_legacy_directory_rejected(self):
        for name in ('unknown--neutral', 'custom--neutral'):
            folder = self.old/'persona-spaces'/name
            folder.mkdir()
            try:
                with self.assertRaisesRegex(ValueError, '人格空间目录'):
                    inspect_import(str(self.old), self.new)
            finally:
                folder.rmdir()

    def test_namespace_file_cannot_masquerade_as_space_directory(self):
        (self.old/'persona-spaces/custom--trusted').write_text('not a directory')
        with self.assertRaisesRegex(ValueError, '人格空间目录'):
            inspect_import(str(self.old), self.new)

    def test_nested_memory_and_context_validation(self):
        target = self.old_active/'memory.json'
        original = target.read_bytes()
        for bad in [[], {'items': {}}, {'items': [{'id': 'missing-content'}]},
                    {'items': [fact('duplicate', 'a'), fact('duplicate', 'b')]}]:
            _atomic_json(target, bad)
            with self.assertRaisesRegex(ValueError, '状态 JSON'):
                inspect_import(str(self.old), self.new)
        target.write_bytes(original)
        write_json(self.old_active, 'context-onebot.json', [])
        with self.assertRaisesRegex(ValueError, '状态 JSON'):
            inspect_import(str(self.old), self.new)

    def test_nested_context_turn_shape_and_encoding_match_runtime(self):
        path = self.old_active/'context-onebot.json'
        for bad in [{'contexts': {CTX: 'not-a-turn-list'}}, {'contexts': {CTX: ['not-a-turn']}},
                    {'contexts': {CTX: [{'user': {'not': 'text'}}]}}, {'summaries': {CTX: []}}]:
            write_json(self.old_active, path.name, bad)
            with self.assertRaisesRegex(ValueError, '状态 JSON'):
                inspect_import(str(self.old), self.new)
        path.write_bytes(json.dumps(context('encoding')).encode('utf-16'))
        with self.assertRaisesRegex(ValueError, '状态 JSON'):
            inspect_import(str(self.old), self.new)

    def test_nested_queue_usage_and_forget_barrier_validation(self):
        for name, value in [('memory-learning.json', {'pending': []}),
                            ('memory-usage.json', {'items': []}),
                            ('memory-forgotten.json', {'markers': ['broken']})]:
            path = self.old_active/name
            before = path.read_bytes() if path.exists() else None
            write_json(self.old_active, name, value)
            try:
                with self.assertRaisesRegex(ValueError, '状态 JSON'):
                    inspect_import(str(self.old), self.new)
            finally:
                if before is None: path.unlink()
                else: path.write_bytes(before)

    def test_nested_persona_snapshot_cannot_claim_another_space(self):
        raw = json.loads((self.old_active/'persona.json').read_text())
        raw['persona_relationship'] = 'rover'
        write_json(self.old_active, 'persona.json', raw)
        with self.assertRaisesRegex(ValueError, '状态 JSON'):
            inspect_import(str(self.old), self.new)

    def test_persona_snapshot_schema_and_typed_version_required_before_publish(self):
        for folder in (self.old, self.old_active):
            path = folder/'persona.json'
            original = path.read_bytes()
            for change in ('missing-schema', 'wrong-schema', 'boolean-version', 'wrong-version'):
                with self.subTest(folder=folder.name, change=change):
                    raw = json.loads(original)
                    if change == 'missing-schema': del raw['schema']
                    elif change == 'wrong-schema': raw['schema'] = 'unknown-user-document'
                    elif change == 'boolean-version': raw['version'] = True
                    else: raw['version'] = 2
                    _atomic_json(path, raw)
                    try:
                        with self.assertRaisesRegex(ValueError, '状态 JSON'):
                            inspect_import(str(self.old), self.new)
                    finally:
                        path.write_bytes(original)

    def test_root_persona_snapshot_must_match_registry_legacy_key(self):
        raw = json.loads((self.old/'persona.json').read_text())
        raw['persona_relationship'] = 'trusted'
        write_json(self.old, 'persona.json', raw)
        with self.assertRaisesRegex(ValueError, '状态 JSON'):
            inspect_import(str(self.old), self.new)

    def test_global_identity_alias_collision_is_rejected(self):
        raw = json.loads((self.old/'identity-links.json').read_text())
        raw['identities'].append({**raw['identities'][0], 'id': 'another-person'})
        write_json(self.old, 'identity-links.json', raw)
        with self.assertRaisesRegex(ValueError, '状态 JSON'):
            inspect_import(str(self.old), self.new)

    def test_global_forgetting_unions_current_and_source_all_spaces(self):
        write_json(self.new, 'memory-forgotten.json', {'version': 1, 'markers': [marker('current-global')]})
        write_json(self.new_active, 'memory-forgotten.json', {'version': 1, 'markers': [marker('current-inactive-copy')]})
        write_json(self.old, 'memory-forgotten.json', {'version': 1, 'markers': [marker('source-global')]})
        raw = json.loads((self.old_active/'memory.json').read_text())
        raw['suppressions'] = [marker('source-nested-copy')]
        write_json(self.old_active, 'memory.json', raw)
        self.queue()
        self.assertEqual(self.apply()['status'], 'imported')
        raw = json.loads((self.new/'memory-forgotten.json').read_text())
        self.assertEqual({m['id'] for m in raw['markers']},
                         {'current-global', 'current-inactive-copy', 'source-global', 'source-nested-copy'})
        for path in (self.new/'context-ilink.json', self.new/'persona-spaces/phrolova--trusted/context-onebot.json'):
            data = json.loads(path.read_text())
            self.assertEqual(data['summaries'], {})
            self.assertEqual(data['summary_sources'], {})
            self.assertEqual([t['user'] for t in data['contexts'][CTX]], ['新的普通上下文'])
            self.assertEqual({m['id'] for m in data['suppressions']}, {m['id'] for m in raw['markers']})

    def test_failed_publish_restores_all_namespace_bytes(self):
        before = hashes(self.new)
        self.queue()
        def fail_once(root, archive, manifest, id=None, source=None):
            if id:
                write_json(root, 'persona-state.json', {'partial': True})
                raise OSError('fixture interrupted persona publication')
            return _publish(root, archive, manifest, id, source)
        with patch('src.core.data_import._publish', side_effect=fail_once):
            self.assertEqual(self.apply()['status'], 'failed')
        self.assertEqual(hashes(self.new), before)

    def test_persona_only_current_root_counts_as_existing_for_legacy_migration(self):
        fresh = self.base/'仅剩人格状态'
        fresh.mkdir()
        write_json(fresh, 'persona-state.json', {'version': 1, 'legacy_space': 'custom--neutral', 'active_space': 'custom--neutral'})
        before = (fresh/'persona-state.json').read_bytes()
        result = initialize_storage(fresh, sources=[self.old], migrate=True)
        self.assertEqual(result['status'], 'existing')
        self.assertFalse((fresh/'memory.json').exists())
        self.assertEqual((fresh/'persona-state.json').read_bytes(), before)

    def test_auto_migration_rejects_bad_nested_state_before_any_publish(self):
        fresh = self.base/'迁移目标'
        write_json(self.old_active, 'memory.json', {'items': [{'id': 'invalid'}]})
        with self.assertRaisesRegex(ValueError, '状态 JSON'):
            initialize_storage(fresh, sources=[self.old], migrate=True)
        self.assertFalse((fresh/'config.json').exists())
        self.assertFalse((fresh/'persona-state.json').exists())

    def test_auto_migration_cannot_ignore_empty_orphan_namespace(self):
        source = self.base/'空孤儿源'
        (source/'persona-spaces').mkdir(parents=True)
        target = self.base/'空孤儿目标'
        with self.assertRaisesRegex(ValueError, '缺少 persona-state'):
            initialize_storage(target, sources=[source], migrate=True)
        self.assertFalse((target/'persona-state.json').exists())


if __name__ == '__main__':
    unittest.main()
