"""Synthetic old/new user directories only; never touch actual user settings."""
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx
from src.config_store import ConfigStore
from src.core.storage import initialize_storage, verify_backup, MARKER, _atomic_json
from src.core.data_import import (inspect_import, prepare_import, cancel_import, import_status,
                                 IMPORT_PENDING, IMPORT_REPORT, _archive, _publish)


def seed(root, persona):
    root.mkdir(parents=True, exist_ok=True)
    payload = {'config.json': json.dumps({'bot':{'name':'fixture', 'persona':persona},
                   'llm':{'primary_key':'fixture-key'}, 'adapter':{'ilink_state_dir':str(root/'ilink')}}),
               'memory.json':json.dumps({'version':1,'items':[{'id':'legacy-fact','scope':'user','owner_id':'fixture-user','category':'preference','content':persona+' memory','keywords':['fixture']}]}), 'context-ilink.json':'{}', 'context-onebot.json':'{}',
               'messages.jsonl':'fixture-message\n', 'onboarding.json':'{"completed":true}',
               'admin-token':'fixture-token', 'ilink/account.json':'{"bot_token":"fixture-bot"}',
               'custom/note.bin':'extra', '.backups/old.zip':'fixture-old-backup',
               'logs/backend.log':'fixture-old-log', 'unfinished.tmp':'user-owned'}
    for name, value in payload.items():
        p=root/name;p.parent.mkdir(parents=True, exist_ok=True);p.write_text(value, encoding='utf-8')
    (root/'empty').mkdir(exist_ok=True)
    (root/MARKER).write_text('{"status":"fresh"}')
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name).resolve()
        self.old=self.base/'旧资料 v1.3.6/data';self.new=self.base/'新版 v2.1.4/user-data'
        self.old_hashes=seed(self.old,'old persona');seed(self.new,'new persona')
    def tearDown(self):self.tmp.cleanup()
    def queue(self):
        preview=inspect_import(str(self.old), self.new)
        return prepare_import(str(self.old), self.new, preview['fingerprint'])
    def apply(self):return initialize_storage(self.new, migrate=False)
    def test_import_unions_forget_barriers_and_invalidates_old_summaries(self):
        current = {'id':'current-forget', 'accounts':[{'platform':'ilink','owner_id':'fixture-user'}],
                   'content':'old persona memory', 'fact_key':'', 'scope':'user'}
        old = {'id':'old-forget', 'accounts':[{'platform':'onebot','owner_id':'123'}], 'content':'旧资料'}
        _atomic_json(self.new/'memory-forgotten.json', {'version':1, 'markers':[current]})
        _atomic_json(self.old/'memory-forgotten.json', {'version':1, 'markers':[old]})
        _atomic_json(self.old/'context-ilink.json', {'version':2, 'summaries':{'ctx':'old persona memory'}, 'contexts':{}})
        self.queue();self.apply()
        markers=json.loads((self.new/'memory-forgotten.json').read_text(encoding='utf-8'))['markers']
        self.assertEqual({m['id'] for m in markers},{'current-forget','old-forget'})
        context=json.loads((self.new/'context-ilink.json').read_text(encoding='utf-8'));self.assertEqual(context['summaries'],{})
        from src.core.memory import PersonaMemory
        memory=PersonaMemory(self.new/'memory.json')
        self.assertTrue(memory.is_suppressed('ilink','fixture-user','old persona memory'))
        self.assertEqual(memory.recall('fixture-user','ctx','old persona memory',platform='ilink'),[])

    def test_internal_controls_case_alias_not_imported(self):
        (self.old/'.STORAGE-IMPORT.pending.json').write_text('internal control')
        self.queue();self.apply();self.assertFalse((self.new/'.STORAGE-IMPORT.pending.json').exists())
    def test_directory_scan_error_not_silently_ignored(self):
        def denied(*args, **kwargs):
            kwargs['onerror'](PermissionError('fixture directory unreadable'))
            return []
        with patch('src.core.data_import.os.walk',side_effect=denied):
            with self.assertRaises(PermissionError):inspect_import(str(self.old),self.new)
    def test_preview_counts_unknown_backups_logs_temp_empty_no_secrets(self):
        r=inspect_import(str(self.old), self.new)
        self.assertEqual(r['files'],12);self.assertGreaterEqual(r['directories'],5)
        self.assertTrue(r['includes']['memory']);self.assertNotIn('fixture-key',json.dumps(r));self.assertNotIn('old persona',json.dumps(r))
    def test_prepare_leaves_active_and_source_unchanged(self):
        before=(self.new/'config.json').read_bytes();r=self.queue()
        self.assertEqual(r['status'],'queued');self.assertEqual((self.new/'config.json').read_bytes(),before)
        self.assertEqual(len(verify_backup(self.new/'.backups'/r['archive'])['files']),12)
        for n,h in self.old_hashes.items():self.assertEqual(hashlib.sha256((self.old/n).read_bytes()).hexdigest(),h)
    def test_apply_replaces_existing_marker_and_all_active_data(self):
        (self.new/'new-only.json').write_text('new only')
        p=self.queue();(self.new/'memory.json').write_text('{"entries":["latest-before-restart"]}')
        self.apply();r=import_status(self.new)
        self.assertEqual(r['status'],'imported');self.assertFalse((self.new/'new-only.json').exists())
        self.assertEqual(ConfigStore(self.new/'config.json').load().bot.persona,'old persona')
        self.assertEqual(ConfigStore(self.new/'config.json').load().bot.persona_profile,'custom')
        self.assertEqual(ConfigStore(self.new/'config.json').load().adapter.ilink_state_dir,str(self.new/'ilink'))
        self.assertTrue((self.new/'empty').is_dir());self.assertEqual((self.new/'custom/note.bin').read_text(),'extra')
        self.assertEqual((self.new/'unfinished.tmp').read_text(),'user-owned')
        self.assertEqual((self.new/'logs/backend.log').read_text(),'fixture-old-log')
        self.assertEqual((self.new/'logs'/('imported-'+p['id'])/'backend.log').read_text(),'fixture-old-log')
        self.assertEqual((self.new/'.backups'/('legacy-'+p['id'])/'old.zip').read_text(),'fixture-old-backup')
        with zipfile.ZipFile(self.new/'.backups'/r['backup']) as z:
            self.assertIn('latest-before-restart',z.read('memory.json').decode());self.assertEqual(z.read('new-only.json'),b'new only')
        for n,h in self.old_hashes.items():self.assertEqual(hashlib.sha256((self.old/n).read_bytes()).hexdigest(),h)
    def test_source_can_disappear_after_prepare(self):
        self.queue();shutil.rmtree(self.old);self.apply()
        self.assertEqual(import_status(self.new)['status'],'imported')
    def test_cancel_then_restart_keeps_new_data(self):
        self.queue();cancel_import(self.new);self.apply()
        self.assertEqual(ConfigStore(self.new/'config.json').load().bot.persona,'new persona')
        self.assertFalse((self.new/IMPORT_PENDING).exists())
    def test_source_changed_requires_new_preview(self):
        r=inspect_import(str(self.old),self.new);(self.old/'memory.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'已变化'):prepare_import(str(self.old),self.new,r['fingerprint'])
        self.assertFalse((self.new/IMPORT_PENDING).exists())
    def test_duplicate_queue_rejected(self):
        self.queue()
        with self.assertRaisesRegex(ValueError,'待导入'):self.queue()
    def test_directory_relationships_rejected(self):
        for p in [self.new,self.new.parent,self.new/'sub']:
            with self.assertRaises(ValueError):inspect_import(str(p),self.new)
    def test_install_directory_missing_config_rejected(self):
        with self.assertRaisesRegex(ValueError,'没有 config'):inspect_import(str(self.old.parent),self.new)
    def test_relative_missing_or_nonstring_rejected(self):
        for p in ['data','',None,[],str(self.base/'missing')]:
            with self.assertRaises(ValueError):inspect_import(p,self.new)
    def test_corrupt_config_rejected(self):
        for text in ['{broken','[]','{}','{"bot":[]}']:
            (self.old/'config.json').write_text(text)
            with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
    def test_links_rejected(self):
        (self.old/'linked').symlink_to(self.new/'config.json')
        with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
    def test_linked_directory_rejected(self):
        (self.old/'linked').symlink_to(self.new,target_is_directory=True)
        with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
    def test_reserved_windows_names_rejected(self):
        (self.old/'CON.txt').write_text('fixture')
        with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
    def test_case_conflict_rejected(self):
        # Case-sensitive filesystems only.
        (self.old/'Aa').write_text('a');(self.old/'aa').write_text('b')
        if len(list(self.old.glob('[Aa][Aa]'))) < 2:self.skipTest('case-insensitive filesystem')
        with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
    def test_limits_fail_before_any_publish(self):
        with patch('src.core.data_import.MAX_FILES',1):
            with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
        with patch('src.core.data_import.MAX_BYTES',1):
            with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
    def test_failed_publish_restores_original_config_memory_unknown(self):
        (self.new/'original-extra').write_text('preserve');before=(self.new/'config.json').read_bytes();self.queue()
        real=_publish
        def fail_once(root,archive,manifest,id=None,source=None):
            if id:
                (root/'custom').mkdir();(root/'custom/partial').write_text('partial')
                raise OSError('fixture publication failure')
            return real(root,archive,manifest,id,source)
        with patch('src.core.data_import._publish',side_effect=fail_once):self.apply()
        self.assertEqual(import_status(self.new)['status'],'failed');self.assertEqual((self.new/'config.json').read_bytes(),before)
        self.assertEqual((self.new/'original-extra').read_text(),'preserve');self.assertFalse((self.new/'custom/partial').exists())
        self.assertFalse((self.new/IMPORT_PENDING).exists())
    def test_backup_failure_does_not_clear_active(self):
        self.queue();before=(self.new/'config.json').read_bytes()
        with patch('src.core.data_import._snapshot',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.apply()
        self.assertEqual((self.new/'config.json').read_bytes(),before);self.assertTrue((self.new/IMPORT_PENDING).exists())
    def test_corrupt_queued_archive_keeps_current_and_reports_failure(self):
        p=self.queue();(self.new/'.backups'/p['archive']).write_bytes(b'broken');self.apply()
        self.assertEqual(import_status(self.new)['status'],'failed');self.assertEqual(ConfigStore(self.new/'config.json').load().bot.persona,'new persona')
    def test_interrupted_publication_resumes_original_backup(self):
        p=self.queue();real=_publish
        with patch('src.core.data_import._publish',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.apply()
        pending=json.loads((self.new/IMPORT_PENDING).read_text());backup=pending['backup']
        self.assertEqual(pending['status'],'applying');self.apply()
        self.assertEqual(import_status(self.new)['backup'],backup)
        with zipfile.ZipFile(self.new/'.backups'/backup) as z:self.assertIn('new persona',z.read('config.json').decode())
    def test_invalid_memory_items_rejected_before_publish(self):
        for value in [[], {'items':{}}, {'items':[{'id':'missing-content'}]}]:
            (self.old/'memory.json').write_text(json.dumps(value))
            with self.assertRaises(ValueError):inspect_import(str(self.old),self.new)
    def test_legacy_v1_memory_loads_effective_items(self):
        from src.core.memory import PersonaMemory
        self.queue();self.apply();memory=PersonaMemory(self.new/'memory.json')
        self.assertEqual(memory._items['legacy-fact'].content,'old persona memory')
        self.assertTrue((self.new/'memory.v1-backup.json').exists())
    def test_idempotent_not_reimport_after_user_edit(self):
        self.queue();self.apply();store=ConfigStore(self.new/'config.json');cfg=store.load();cfg.bot.persona='edited';store.save(cfg)
        self.apply();self.assertEqual(store.load().bot.persona,'edited')
    def test_storage_control_files_read_explicit_utf8(self):
        from src.core.storage import migrated_state_path, PENDING
        original=Path.read_text
        def read(path, *args, **kwargs):
            if path.name in {MARKER, PENDING}:self.assertEqual(kwargs.get('encoding'),'utf-8')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            self.queue();self.apply();self.apply()
            self.assertEqual(migrated_state_path(self.new,self.old/'ilink'),self.new/'ilink')
    def test_temp_root_alias_absolute_wechat_path_remapped(self):
        raw=Path(self.tmp.name)/'旧资料 v1.3.6/data'
        cfg=json.loads((self.old/'config.json').read_text());cfg['adapter']['ilink_state_dir']=str(raw/'ilink');(self.old/'config.json').write_text(json.dumps(cfg))
        self.queue();self.apply();self.assertEqual(ConfigStore(self.new/'config.json').load().adapter.ilink_state_dir,str(self.new/'ilink'))
    def test_invalid_consumed_state_json_rejected(self):
        (self.old/'memory.json').write_text('broken')
        with self.assertRaisesRegex(ValueError,'状态 JSON'):inspect_import(str(self.old),self.new)
    def test_previous_auto_migration_ilink_path_remapped(self):
        previous=self.base/'original-v1/data'
        (self.old/MARKER).write_text(json.dumps({'status':'migrated','source':str(previous)}))
        cfg=json.loads((self.old/'config.json').read_text());cfg['adapter']['ilink_state_dir']=str(previous/'ilink');(self.old/'config.json').write_text(json.dumps(cfg))
        self.queue();self.apply();self.assertEqual(ConfigStore(self.new/'config.json').load().adapter.ilink_state_dir,str(self.new/'ilink'))
    def test_symlinked_target_logs_not_followed(self):
        outside=self.base/'outside';outside.mkdir();(outside/'keep').write_text('unchanged')
        shutil.rmtree(self.new/'logs');(self.new/'logs').symlink_to(outside,target_is_directory=True)
        self.queue();before=(self.new/'config.json').read_bytes();self.apply()
        self.assertEqual((outside/'keep').read_text(),'unchanged');self.assertEqual(list(outside.iterdir()),[outside/'keep'])
        self.assertEqual((self.new/'config.json').read_bytes(),before);self.assertEqual(import_status(self.new)['status'],'failed')
    def test_external_wechat_path_kept(self):
        cfg=json.loads((self.old/'config.json').read_text());cfg['adapter']['ilink_state_dir']=str(self.base/'external');(self.old/'config.json').write_text(json.dumps(cfg))
        self.queue();self.apply();self.assertEqual(ConfigStore(self.new/'config.json').load().adapter.ilink_state_dir,str(self.base/'external'))
    def test_bad_archive_member_no_escape(self):
        p=self.queue();archive=self.new/'.backups'/p['archive']
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('../escape',b'x');z.writestr('__wxbot_backup_manifest__.json',json.dumps({'files':{'../escape':hashlib.sha256(b'x').hexdigest()}}))
        self.apply();self.assertEqual(import_status(self.new)['status'],'failed');self.assertFalse((self.new.parent/'escape').exists())


class ImportAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.new=self.root/'new';self.old=self.root/'old';seed(self.new,'new');seed(self.old,'old')
        self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':str(self.new),'WXBOT_ADMIN_TOKEN':'test-token','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.s=importlib.reload(server);self.c=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.s.app),base_url='http://localhost',headers={'Authorization':'Bearer test-token'})
    async def asyncTearDown(self):await self.c.aclose();self.env.stop();self.tmp.cleanup()
    async def preview(self):return (await self.c.post('/api/data/import/preview',json={'source':str(self.old)})).json()
    async def test_preview_prepare_status_cancel(self):
        p=await self.preview();self.assertTrue(p['ok']);self.assertNotIn('fixture-key',json.dumps(p))
        r=await self.c.post('/api/data/import/prepare',json={'token':p['token'],'confirm_replace':True})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json()['requires_backend_restart'])
        self.assertEqual((await self.c.get('/api/data/status')).json()['import']['status'],'queued')
        self.assertEqual(ConfigStore(self.new/'config.json').load().bot.persona,'new')
        self.assertEqual((await self.c.post('/api/data/import/cancel')).status_code,200)
    async def test_confirmation_required(self):
        p=await self.preview()
        for v in [False,1,'true',None]:
            self.assertEqual((await self.c.post('/api/data/import/prepare',json={'token':p['token'],'confirm_replace':v})).status_code,400)
    async def test_invalid_and_expired_preview(self):
        self.assertEqual((await self.c.post('/api/data/import/prepare',json={'token':[],'confirm_replace':True})).status_code,409)
        p=await self.preview();self.s._import_previews[p['token']]['at']-=901
        self.assertEqual((await self.c.post('/api/data/import/prepare',json={'token':p['token'],'confirm_replace':True})).status_code,409)
    async def test_auth_and_origin_required(self):
        self.c.headers.clear()
        for endpoint in ['preview','prepare','cancel']:
            self.assertEqual((await self.c.post('/api/data/import/'+endpoint,json={})).status_code,401)
        self.c.headers['Authorization']='Bearer test-token';self.c.headers['Origin']='https://external.example'
        self.assertEqual((await self.c.post('/api/data/import/preview',json={'source':str(self.old)})).status_code,403)
    async def test_remote_client_denied(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.s.app,client=('192.0.2.1',1)),base_url='http://localhost',headers={'Authorization':'Bearer test-token'}) as c:
            self.assertEqual((await c.post('/api/data/import/preview',json={'source':str(self.old)})).status_code,403)
    async def test_busy_import(self):
        await self.s._backup_lock.acquire()
        try:self.assertEqual((await self.c.post('/api/data/import/preview',json={'source':str(self.old)})).status_code,409)
        finally:self.s._backup_lock.release()
    async def test_error_no_private_path_or_keys(self):
        with patch.object(self.s,'inspect_import',side_effect=OSError('fixture-key private path')):
            r=await self.c.post('/api/data/import/preview',json={'source':str(self.old)})
        self.assertEqual(r.status_code,400);self.assertNotIn('fixture-key',r.text)
    async def test_missing_or_wrong_json(self):
        for v in [[],None,{'source':5}]:
            self.assertEqual((await self.c.post('/api/data/import/preview',json=v)).status_code,400)
    async def test_import_archive_download(self):
        p=await self.preview();r=(await self.c.post('/api/data/import/prepare',json={'token':p['token'],'confirm_replace':True})).json()
        archive=r['import']['archive'];self.assertEqual((await self.c.get('/api/data/backup/'+archive)).status_code,200)

if __name__ == '__main__':unittest.main()
