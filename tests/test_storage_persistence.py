"""Temporary local fixtures: migration, update/uninstall/reinstall and backups."""
import asyncio
import concurrent.futures
from types import SimpleNamespace
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
from src.core.storage import (resolve_data_dir,initialize_storage,create_backup,verify_backup,
                             migrated_state_path,legacy_data_dirs,MARKER,PENDING,MANIFEST,_snapshot,_lock)
from src.runtime import BotRuntime


def seed(root,value='original'):
    root.mkdir(parents=True,exist_ok=True)
    files={'config.json':json.dumps({'llm':{'primary_key':'fixture-key'},'bot':{'persona':value,'persona_profile':'custom'},'adapter':{'ilink_state_dir':str(root/'ilink')}}),
           'memory.json':json.dumps({'entries':[{'text':value}]}), 'context-ilink.json':'[]', 'context-onebot.json':'[]',
           'ilink/account.json':'{"bot_token":"fixture-session"}', 'ilink/incoming.json':'[]',
           'ilink/outbox.json':'[]', 'qq-bursts.json':'{}', 'messages.jsonl':'{"text":"fixture-chat"}\n',
           'onboarding.json':'{"completed":true}', 'admin-token':'fixture-admin'}
    for name,data in files.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}


class PathTests(unittest.TestCase):
    def test_mac_stable_not_profile_or_install(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_data_dir('darwin',d,{}),Path(d).resolve()/'Library/Application Support/WxBot/user-data')
    def test_windows_local_not_roaming(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_data_dir('win32',d,{'LOCALAPPDATA':d+'/local','APPDATA':d+'/roaming'}),Path(d).resolve()/'local/WxBot/user-data')
    def test_windows_home_fallback(self):
        with tempfile.TemporaryDirectory() as d:self.assertEqual(resolve_data_dir('win32',d,{}),Path(d).resolve()/'AppData/Local/WxBot/user-data')
    def test_linux_xdg(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_data_dir('linux',d,{'XDG_DATA_HOME':d+'/share'}),Path(d).resolve()/'share/wxbot/user-data')
            self.assertEqual(resolve_data_dir('linux',d,{'XDG_DATA_HOME':'relative'}),Path(d).resolve()/'.local/share/wxbot/user-data')
    def test_overrides_and_priority(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_data_dir('darwin',d,{'WXBOT_DATA_DIR':d+'/custom','WXBOT_USER_DATA_DIR':d+'/profile'}),Path(d).resolve()/'custom')
            self.assertEqual(resolve_data_dir('darwin',d,{'WXBOT_USER_DATA_DIR':d+'/profile'}),Path(d).resolve()/'profile/data')
    def test_working_directory_independent(self):
        with tempfile.TemporaryDirectory() as d:
            old=Path.cwd()
            try:
                before=resolve_data_dir('darwin',d,{});os.chdir(d);self.assertEqual(before,resolve_data_dir('darwin',d,{}))
            finally:os.chdir(old)

    def test_explicit_legacy_sources_are_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            sources=legacy_data_dirs(env={'WXBOT_LEGACY_DATA_DIRS':json.dumps([d])},home=d)
            self.assertEqual(sources,[Path(d).resolve()])
    def test_invalid_legacy_source_list_rejected(self):
        for value in ['{}','[4]']:
            with self.assertRaises(ValueError):legacy_data_dirs(env={'WXBOT_LEGACY_DATA_DIRS':value})


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name).resolve();self.old=self.base/'install-v1/data';self.new=self.base/'user-home/WxBot/user-data';self.hashes=seed(self.old)
    def tearDown(self):self.tmp.cleanup()
    def init(self,**kw):return initialize_storage(self.new,sources=kw.pop('sources',[self.old]),migrate=True,**kw)
    def test_complete_migration_original_untouched(self):
        r=self.init();self.assertEqual(r['status'],'migrated');self.assertEqual(r['imported_files'],11)
        for name,digest in self.hashes.items():
            self.assertEqual(hashlib.sha256((self.new/name).read_bytes()).hexdigest(),digest)
            self.assertEqual(hashlib.sha256((self.old/name).read_bytes()).hexdigest(),digest)
        self.assertEqual(len(verify_backup(self.new/'.backups'/r['archive'])['files']),11)
    def test_existing_new_data_wins_no_mixing(self):
        seed(self.new,'newer');r=self.init();self.assertEqual(r['status'],'existing');self.assertIn('newer',(self.new/'memory.json').read_text());self.assertEqual(list((self.new/'.backups').glob('*.zip')) if (self.new/'.backups').exists() else [],[])
    def test_idempotent_deleted_memory_not_resurrected(self):
        self.init();(self.new/'memory.json').unlink();self.assertEqual(self.init()['status'],'migrated');self.assertFalse((self.new/'memory.json').exists());self.assertEqual(len(list((self.new/'.backups').glob('*.zip'))),1)
    def test_multiple_sources_archived_not_mixed(self):
        other=self.base/'other-profile/data';seed(other,'OTHER');r=self.init(sources=[self.old,other]);self.assertEqual(len(r['other_backups']),1);self.assertIn('original',(self.new/'memory.json').read_text());self.assertNotIn('OTHER',(self.new/'memory.json').read_text());verify_backup(self.new/'.backups'/r['other_backups'][0])
    def test_custom_directory_no_implicit_import(self):
        with patch.dict(os.environ,{'WXBOT_DATA_DIR':str(self.new)},clear=True):r=initialize_storage(self.new,sources=[self.old])
        self.assertEqual(r['status'],'custom');self.assertFalse((self.new/'config.json').exists())
    def test_no_source_fresh(self):
        r=self.init(sources=[]);self.assertEqual(r['status'],'fresh');self.assertFalse((self.new/'config.json').exists())
    def test_resume_after_partial_publish(self):
        self.new.mkdir(parents=True);archive=_snapshot(self.old,self.new/'.backups','migration')
        (self.new/PENDING).write_text(json.dumps({'archive':archive.name,'source':str(self.old)}))
        shutil.copy2(self.old/'config.json',self.new/'config.json');r=self.init();self.assertEqual(r['imported_files'],11);self.assertFalse((self.new/PENDING).exists());self.assertTrue((self.new/'ilink/account.json').exists())
    def test_resume_conflict_retains_newer_file(self):
        self.new.mkdir(parents=True);archive=_snapshot(self.old,self.new/'.backups','migration');(self.new/PENDING).write_text(json.dumps({'archive':archive.name,'source':str(self.old)}));(self.new/'config.json').write_text('newer')
        with self.assertRaises(RuntimeError):self.init()
        self.assertEqual((self.new/'config.json').read_text(),'newer');self.assertFalse((self.new/'memory.json').exists())
    def test_corrupt_backup_does_not_publish(self):
        self.new.mkdir(parents=True);archive=_snapshot(self.old,self.new/'.backups','migration');archive.write_bytes(b'broken');(self.new/PENDING).write_text(json.dumps({'archive':archive.name,'source':str(self.old)}))
        with self.assertRaises(zipfile.BadZipFile):self.init()
        self.assertFalse((self.new/'config.json').exists())
    def test_migration_error_keeps_source(self):
        with patch('src.core.storage._snapshot',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.init()
        self.assertTrue((self.old/'memory.json').exists());self.assertFalse((self.new/MARKER).exists());self.assertFalse((self.new/'config.json').exists())
    def test_relative_and_absolute_ilink_migrated(self):
        self.init();store=ConfigStore(self.new/'config.json');rt=BotRuntime(store,self.new);self.assertEqual(rt._ilink_state_dir(),self.new/'ilink')
        rt.cfg.adapter.ilink_state_dir='./data/ilink';self.assertEqual(rt._ilink_state_dir(),self.new/'ilink')
    def test_custom_external_ilink_not_rewritten(self):
        self.init();custom=self.base/'custom-wechat';self.assertEqual(migrated_state_path(self.new,custom),custom)
    def test_upgrade_uninstall_reinstall_same_bytes(self):
        self.init();shutil.rmtree(self.base/'install-v1')
        for version in ['install-v2','install-reinstalled']:
            app=self.base/version;app.mkdir();(app/'app.exe').write_text('binary');shutil.rmtree(app)
            self.init();self.assertEqual(ConfigStore(self.new/'config.json').load().bot.persona,'original')
            self.assertEqual((self.new/'ilink/account.json').read_text(),'{"bot_token":"fixture-session"}')
            for name,digest in self.hashes.items():self.assertEqual(hashlib.sha256((self.new/name).read_bytes()).hexdigest(),digest)
    def test_live_backend_log_does_not_block_migration(self):
        (self.old/'logs').mkdir();(self.old/'logs/backend.log').write_text('old log');(self.new/'logs').mkdir(parents=True);(self.new/'logs/backend.log').write_text('new live log')
        self.init();self.assertEqual((self.new/'logs/backend.log').read_text(),'new live log');self.assertTrue((self.new/'memory.json').exists())
    def test_linked_file_stops_instead_of_copying_external(self):
        outside=self.base/'private';outside.write_text('private');(self.old/'external').symlink_to(outside)
        with self.assertRaises(ValueError):self.init()
        self.assertFalse((self.new/'config.json').exists())
    def test_target_inside_source_ignored(self):
        target=self.old/'nested';r=initialize_storage(target,sources=[self.old],migrate=True);self.assertEqual(r['status'],'fresh')

    def test_parallel_first_start_single_migration(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            reports=list(pool.map(lambda _:self.init(),range(2)))
        self.assertTrue(all(r['status']=='migrated' for r in reports));self.assertEqual(len(list((self.new/'.backups').glob('*.zip'))),1)
    def test_windows_existing_lock_not_written_before_acquire(self):
        self.new.mkdir(parents=True);lock=self.new/'.storage.lock';lock.write_bytes(b'X');calls=[]
        fake=SimpleNamespace(LK_NBLCK=1,LK_UNLCK=2,locking=lambda fd,mode,n:calls.append((mode,n)))
        with patch('src.core.storage.os.name','nt'),patch.dict('sys.modules',{'msvcrt':fake}):
            with _lock(self.new):pass
        self.assertEqual(lock.read_bytes(),b'X');self.assertEqual(calls,[(1,1),(2,1)])


class BackupTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'data';self.hashes=seed(self.root)
    def tearDown(self):self.tmp.cleanup()
    def test_backup_verify_restore_bytes(self):
        archive=create_backup(self.root);manifest=verify_backup(archive);self.assertEqual(manifest['files'],self.hashes)
        restore=self.root.parent/'restored'
        with zipfile.ZipFile(archive) as z:z.extractall(restore)
        for name,digest in self.hashes.items():self.assertEqual(hashlib.sha256((restore/name).read_bytes()).hexdigest(),digest)
    def test_backup_not_recursive_or_temp(self):
        (self.root/'broken.tmp').write_text('partial');create_backup(self.root);second=create_backup(self.root)
        names=verify_backup(second)['files'];self.assertFalse(any('.backups' in n or '.storage' in n or n.endswith('.tmp') for n in names));self.assertEqual(len(names),11)
    def test_unique_archive_names(self):self.assertNotEqual(create_backup(self.root),create_backup(self.root))
    def test_secret_permissions_posix(self):
        if os.name=='nt':return
        archive=create_backup(self.root);self.assertEqual(archive.stat().st_mode & 0o777,0o600)
    def test_external_ilink_in_backup(self):
        external=self.root.parent/'external';external.mkdir();(external/'account.json').write_text('fixture')
        m=verify_backup(create_backup(self.root,extras={'ilink':external}));self.assertIn('external/ilink/account.json',m['files'])
    def test_failure_no_partial_named_archive(self):
        with patch('src.core.storage.verify_backup',side_effect=ValueError('corrupt')):
            with self.assertRaises(ValueError):create_backup(self.root)
        self.assertEqual(list((self.root/'.backups').iterdir()),[]);self.assertTrue((self.root/'memory.json').exists())
    def test_content_tamper_detected(self):
        archive=create_backup(self.root)
        with zipfile.ZipFile(archive,'a') as z:z.writestr('memory.json','tampered')
        with self.assertRaises(ValueError):verify_backup(archive)


class DataAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();seed(self.root)
        self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':str(self.root),'WXBOT_ADMIN_TOKEN':'test-token','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.s=importlib.reload(server);self.c=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.s.app),base_url='http://localhost',headers={'Authorization':'Bearer test-token'})
    async def asyncTearDown(self):await self.c.aclose();self.env.stop();self.tmp.cleanup()
    async def test_status_no_secrets(self):
        r=await self.c.get('/api/data/status');self.assertEqual(r.json()['data_dir'],str(self.root));self.assertNotIn('fixture-key',r.text);self.assertNotIn('fixture-session',r.text)
    async def test_backup_download_verified(self):
        r=await self.c.post('/api/data/backup');self.assertTrue(r.json()['ok']);data=await self.c.get(r.json()['download_url']);self.assertEqual(data.status_code,200);self.assertEqual(data.headers['cache-control'],'no-store');self.assertEqual(data.content,Path(r.json()['path']).read_bytes());verify_backup(Path(r.json()['path']))
    async def test_auth_for_all_data_endpoints(self):
        self.c.headers.clear()
        for method,path in [('GET','/api/data/status'),('POST','/api/data/backup'),('GET','/api/data/backup/wxbot-backup-20261013T000000Z-1234abcd.zip')]:self.assertEqual((await self.c.request(method,path)).status_code,401)
    async def test_busy_backup(self):
        await self.s._backup_lock.acquire()
        try:self.assertEqual((await self.c.post('/api/data/backup')).status_code,409)
        finally:self.s._backup_lock.release()
    async def test_error_no_secret_and_original_retained(self):
        before=(self.root/'config.json').read_bytes()
        with patch.object(self.s,'create_backup',side_effect=OSError('fixture-key')):r=await self.c.post('/api/data/backup')
        self.assertEqual(r.status_code,500);self.assertNotIn('fixture-key',r.text);self.assertEqual((self.root/'config.json').read_bytes(),before)
    async def test_bad_or_missing_names(self):
        for name in ['config.json','wxbot-backup-20261013T000000Z-1234abcd.zip','../config.json']:
            self.assertEqual((await self.c.get('/api/data/backup/'+name)).status_code,404)
    async def test_download_symlink_rejected(self):
        directory=self.root/'.backups';directory.mkdir();name='wxbot-backup-20261013T000000Z-1234abcd.zip';(directory/name).symlink_to(self.root/'config.json')
        self.assertEqual((await self.c.get('/api/data/backup/'+name)).status_code,404)
