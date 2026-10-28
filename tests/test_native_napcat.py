"""Offline native-manager contracts; never start a real QQ account or cloud API."""
import asyncio, hashlib, json, os, socket, tempfile, threading, unittest, zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from src.core import native_napcat as n
from src.config_store import AppConfig, ConfigStore
from src.runtime import BotRuntime

class FakeProcess:
    def __init__(self):self.running=False;self.calls=[];self.pid=None
    async def start(self,*args):self.calls.append(args);self.running=True;self.pid=9001;return self.pid
    async def stop(self):self.running=False;self.pid=None

class NativeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.bundle=self.root/'bundle';self.bundle.mkdir()
        self.zip=self.bundle/n.ARCHIVE;self.required={}
        with zipfile.ZipFile(self.zip,'w') as z:
            for name in n.REQUIRED:
                value=('synthetic:'+name).encode();z.writestr(name,value)
                self.required[name]={'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()}
        self.patches=patch.multiple(n,ARCHIVE_SIZE=self.zip.stat().st_size,ARCHIVE_SHA256=hashlib.sha256(self.zip.read_bytes()).hexdigest(),REQUIRED_FILES=self.required,FILES_MANIFEST_SHA256=hashlib.sha256(json.dumps({k:{'size':v['bytes'],'sha256':v['sha256']} for k,v in self.required.items()},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest())
        self.patches.start();self.machine=patch.object(n.platform,'machine',return_value='AMD64');self.machine.start()
        self.process=FakeProcess();self.m=n.NativeNapCatManager(self.root/'data',bundle_dir=self.bundle,process=self.process,platform_name='win32')
        self.web=SimpleNamespace(login=AsyncMock(return_value={'ok':True}),sync_onebot=AsyncMock(return_value={'ok':True,'changed':True}))
        self.client=patch.object(n,'NapCatWebUI',return_value=self.web);self.client.start()
    async def asyncTearDown(self):
        await self.m.close();self.client.stop();self.machine.stop();self.patches.stop();self.tmp.cleanup()
    async def ready(self):
        result=await self.m.ensure('web-secret','ws://127.0.0.1:12345/ws/onebot','ob-secret');self.assertTrue(result['pending']);await self.m._job;self.assertEqual(self.m.task['stage'],'waiting_login')
    async def test_offline_extract_start_and_auth_ready(self):
        await self.ready();self.assertTrue(self.m.installed());self.assertTrue(self.m._verified_install())
        argv,cwd,env,log=self.process.calls[0];self.assertEqual(argv,[str(self.m.runtime/'node.exe'),str(self.m.runtime/'index.js')]);self.assertEqual(cwd,self.m.work)
        self.web.login.assert_awaited_once();self.assertTrue(self.m.autostart);self.assertTrue(self.m.status()['running'])
    async def test_duplicate_start_one_process(self):
        await asyncio.gather(self.m.ensure('w','ws://127.0.0.1:1','o'),self.m.ensure('w','ws://127.0.0.1:1','o'));await self.m._job
        self.assertEqual(len(self.process.calls),1);await self.m.ensure('w','ws://127.0.0.1:1','o');self.assertEqual(len(self.process.calls),1)
    async def test_profile_and_work_isolated_no_installed_QQ_required(self):
        await self.ready();env=self.process.calls[0][2]
        self.assertEqual(env['USERPROFILE'],str(self.m.profile));self.assertEqual(env['APPDATA'],str(self.m.profile/'AppData/Roaming'));self.assertEqual(env['NAPCAT_WORKDIR'],str(self.m.work))
        self.assertEqual(env['NAPCAT_DISABLE_MULTI_PROCESS'],'1');self.assertNotIn('NODE_OPTIONS',env);self.assertFalse(self.m.status()['needsQQ'])
    async def test_host_login_and_webui_overrides_not_inherited(self):
        with patch.dict(os.environ,{'NAPCAT_QUICK_ACCOUNT':'external-user','NAPCAT_QUICK_PASSWORD':'secret','NAPCAT_WEBUI_TOKEN':'external-key','NODE_OPTIONS':'--inspect'}):
            env=self.m._environment()
        self.assertNotIn('NAPCAT_QUICK_ACCOUNT',env);self.assertNotIn('NAPCAT_QUICK_PASSWORD',env);self.assertNotIn('NAPCAT_WEBUI_TOKEN',env);self.assertNotIn('NODE_OPTIONS',env)
    async def test_http_webui_loopback_tokens_and_ws(self):
        await self.ready();path=self.m.work/'config';web=json.loads((path/'webui.json').read_text());ob=json.loads((path/'onebot11.json').read_text())
        self.assertEqual(web['host'],'127.0.0.1');self.assertEqual(web['token'],'web-secret');h=ob['network']['httpServers'][0];self.assertEqual(h['host'],'127.0.0.1');self.assertFalse(h['enableCors']);self.assertEqual(h['token'],'ob-secret');self.assertEqual(ob['network']['websocketClients'][0]['url'],'ws://127.0.0.1:12345/ws/onebot')
    async def test_unknown_data_and_login_profile_survive_stop_restart(self):
        await self.ready();unknown=self.m.profile/'unknown-user-file';unknown.write_text('retain');await self.m.close();self.assertTrue(self.m.autostart)
        await self.ready();self.assertEqual(unknown.read_text(),'retain');await self.m.stop();self.assertFalse(self.m.autostart)
    async def test_occupied_port_not_adopted_or_stopped(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));sock.listen();self.m.http_port=sock.getsockname()[1];old=self.m.http_port
            ports=[];await self.m.ensure('w','ws://127.0.0.1:1','o',on_ports=lambda *args:ports.append(args));await self.m._job
            self.assertNotEqual(self.m.http_port,old);self.assertEqual(ports[0][0],self.m.http_port);await self.m.stop();self.assertEqual(sock.getsockname()[1],old)
    async def test_start_failure_does_not_enable_autostart(self):
        self.process.start=AsyncMock(side_effect=OSError('fixture startup failure'));await self.m.ensure('w','ws://127.0.0.1:1','o');await self.m._job
        self.assertEqual(self.m.task['stage'],'error');self.assertFalse(self.m.autostart);self.assertFalse(self.process.running)
    async def test_tampered_pinned_archive_never_starts(self):
        self.zip.write_bytes(b'bad');await self.m.ensure('w','ws://127.0.0.1:1','o');await self.m._job
        self.assertEqual(self.m.task['stage'],'error');self.assertEqual(self.process.calls,[])
    async def test_damaged_installed_runtime_not_trusted(self):
        await self.ready();file=self.m.runtime/'index.js';file.write_bytes(b'X'*self.required['index.js']['bytes']);self.assertFalse(self.m._verified_install())
    async def test_all_files_and_manifest_are_authenticated(self):
        await self.ready();marker=self.m.runtime/'.wxbot-runtime.json'
        extra=self.m.runtime/'extra.dll';extra.write_bytes(b'executable');self.assertFalse(self.m._verified_install());extra.unlink()
        data=json.loads(marker.read_text());data['files']={};marker.write_text(json.dumps(data));self.assertFalse(self.m._verified_install())
    async def test_only_native_beacon_log_allowed_and_preserved_after_stop(self):
        await self.ready();beacon=self.m.runtime/'beacon_report.log';beacon.write_text('native diagnostic')
        self.assertTrue(self.m._verified_install());other=self.m.runtime/'arbitrary.log';other.write_text('no');self.assertFalse(self.m._verified_install());other.unlink()
        await self.m.stop();self.assertFalse(beacon.exists());self.assertIn('native diagnostic',(self.m.work/'native-beacon-report.log').read_text());self.assertTrue(self.m._verified_install())
    async def test_unknown_existing_runtime_is_not_overwritten(self):
        self.m.runtime.mkdir(parents=True);unknown=self.m.runtime/'custom';unknown.write_text('retain');await self.m.ensure('w','ws://127.0.0.1:1','o');await self.m._job
        self.assertEqual(self.m.task['stage'],'error');self.assertEqual(unknown.read_text(),'retain');self.assertFalse(self.process.running)
    async def test_sync_preserves_third_party_networks_and_unknown_keys(self):
        await self.ready();file=self.m.work/'config/onebot11_12345.json';before={'custom':42,'network':{'websocketClients':[{'name':'other','url':'ws://other'}]}};file.write_text(json.dumps(before))
        result=await self.m.sync_config('web-secret','ws://127.0.0.1:43210/ws/onebot','new-token');self.assertTrue(result['ok']);after=json.loads(file.read_text());self.assertEqual(after['custom'],42);self.assertEqual(after['network']['websocketClients'][0],before['network']['websocketClients'][0]);self.assertEqual(after['network']['websocketClients'][1]['token'],'new-token')
    async def test_invalid_config_does_not_get_silently_replaced(self):
        await self.ready();file=self.m.work/'config/onebot11_12345.json';file.write_text('{bad');before=(self.m.work/'config/onebot11.json').read_bytes()
        with self.assertRaises(ValueError):await self.m.sync_config('w','ws://127.0.0.1:1','o')
        self.assertEqual(file.read_text(),'{bad');self.assertEqual((self.m.work/'config/onebot11.json').read_bytes(),before)
    async def test_unsupported_platform_has_no_docker_fallback(self):
        self.m.supported=False;result=await self.m.ensure('w','ws://127.0.0.1:1','o');self.assertFalse(result['ok']);self.assertEqual(result['stage'],'unsupported');self.assertEqual(self.process.calls,[])
    async def test_stop_during_start_never_commits_a_running_process(self):
        gate=asyncio.Event();self.process.start=AsyncMock(side_effect=lambda *a:gate.wait())
        await self.m.ensure('w','ws://127.0.0.1:1','o');await self.m.stop();self.assertFalse(self.process.running);self.assertEqual(self.m.task['stage'],'stopped')
    async def test_redaction_before_process_log_and_read_api(self):
        await self.ready();text=self.m._redact('web-secret ob-secret https://example.com/?token=secret Credential: abc123')
        self.assertNotIn('web-secret',text);self.assertNotIn('ob-secret',text);self.assertNotIn('abc123',text);self.assertNotIn('example.com',text)
        file=self.m.root/'logs/process.log';file.parent.mkdir(parents=True,exist_ok=True);file.write_text('web-secret https://example.com/?token=secret');self.assertNotIn('web-secret',(await self.m.logs())['log'])

class ExtractionTests(unittest.TestCase):
    def test_manifest_constants_match_pinned_build_manifest(self):
        manifest=json.loads((Path(__file__).parents[1]/'desktop/napcat-runtime.json').read_text());self.assertEqual(manifest['required_files'],n.REQUIRED_FILES);self.assertEqual(manifest['asset']['sha256'],n.ARCHIVE_SHA256);self.assertEqual(manifest['files_manifest_sha256'],n.FILES_MANIFEST_SHA256)
    def test_zip_traversal_absolute_drive_symlink_and_duplicate_rejected(self):
        for name,mode in [('../escape',0),('/escape',0),('C:/escape',0),('link',0o120777),('node.exe',0)]:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);archive=root/'fixture.zip'
                with zipfile.ZipFile(archive,'w') as z:
                    z.writestr('node.exe',b'fixture');info=zipfile.ZipInfo(name);info.external_attr=mode<<16;z.writestr(info,b'bad')
                with patch.multiple(n,ARCHIVE_SIZE=archive.stat().st_size,ARCHIVE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest()):
                    with self.assertRaises(ValueError):n.extract_runtime(archive,root/'runtime',threading.Event())
                self.assertFalse((root/'runtime').exists());self.assertFalse((root.parent/'escape').exists())

class RuntimeNativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.store=ConfigStore(self.path/'config.json');self.store.save(AppConfig());self.rt=BotRuntime(self.store,self.path)
    async def asyncTearDown(self):await self.rt.close_gateway();self.tmp.cleanup()
    async def test_external_setup_sync_stop_never_calls_docker_or_native(self):
        cfg=self.store.load();cfg.adapter.napcat_mode='external';self.store.save(cfg);self.rt.cfg=cfg
        with patch.object(self.rt.native_napcat,'ensure',AsyncMock()) as ensure,patch.dict(os.environ,{'WXBOT_SKIP_NAPCAT_SYNC':'0'}):
            self.assertFalse((await self.rt.qq_setup())['ok']);self.assertTrue((await self.rt.qq_sync_ws_url())['skipped']);self.assertTrue((await self.rt.qq_stop())['ok']);ensure.assert_not_awaited()
    async def test_legacy_qq_config_preserved_as_external(self):
        raw={'adapter':{'onebot_token':'old','napcat_webui_token':'web','onebot_http_url':'http://127.0.0.1:3000'}};cfg=self.store._from_dict(raw);self.assertEqual(cfg.adapter.napcat_mode,'external');self.assertEqual(cfg.adapter.onebot_token,'old')
    async def test_new_install_native_and_bad_choice_fail_closed(self):
        self.assertEqual(AppConfig().adapter.napcat_mode,'native');self.assertEqual(self.store._from_dict({'adapter':{'napcat_mode':'bogus'}}).adapter.napcat_mode,'external')
    async def test_actual_callback_loopback_and_external_status_explicit(self):
        self.rt.set_http_port(9959);self.assertEqual(self.rt._napcat_ws_url(),'ws://127.0.0.1:9959/ws/onebot');self.rt.probe_qq=AsyncMock(return_value={'loggedIn':False,'httpReachable':False})
        result=await self.rt.qq_status();self.assertEqual(result['manager']['mode'],'native');self.assertFalse(result['manager']['requiresDocker'])
    async def test_deprecated_docker_api_does_not_launch_docker(self):
        self.assertFalse(self.rt.docker_status()['required']);self.assertTrue((await self.rt.docker_prepare())['deprecated']);self.assertFalse(hasattr(self.rt,'docker'))
    async def test_no_webui_access_when_native_not_owned_or_external(self):
        self.rt._webui_client=AsyncMock(side_effect=AssertionError('external WebUI must not be accessed'))
        self.assertFalse((await self.rt.qq_login_qrcode())['ok']);self.assertFalse((await self.rt.qq_quick_accounts())['ok'])
        self.assertFalse((await self.rt.qq_quick_login('12345'))['ok']);self.assertFalse((await self.rt.qq_login_check())['loggedIn'])
        cfg=self.store.load();cfg.adapter.napcat_mode='external';self.store.save(cfg)
        self.assertEqual((await self.rt.qq_login_qrcode())['stage'],'external');self.rt._webui_client.assert_not_called()
    async def test_switch_external_and_disable_finish_owned_cleanup(self):
        self.rt.native_napcat.process=SimpleNamespace(running=True,stop=AsyncMock())
        cfg=self.store.load();cfg.adapter.napcat_mode='external'
        await self.rt.reconcile_gateway(cfg);self.rt.native_napcat.process.stop.assert_awaited_once()
        self.assertEqual(self.rt.native_napcat.task['stage'],'stopped')
    async def test_changed_security_config_invalidates_old_start_callback(self):
        old=self.store.load();expected=(old.adapter.napcat_mode,old.adapter.qq_enabled,old.adapter.napcat_webui_token,old.adapter.onebot_token)
        old.adapter.onebot_token='new';self.store.save(old)
        with self.assertRaisesRegex(RuntimeError,'配置已经改变'):self.rt._native_ports(33333,33334,expected)
        self.assertEqual(self.store.load().adapter.onebot_http_url,'http://127.0.0.1:3000')
    async def test_gateway_ports_saved_and_adapter_endpoint_updated(self):
        self.rt.qq_adapter=SimpleNamespace(http_url='http://127.0.0.1:3000',access_token='')
        self.rt._native_ports(33333,33334);self.assertEqual(self.store.load().adapter.onebot_http_url,'http://127.0.0.1:33333');self.assertEqual(self.rt.qq_adapter.http_url,'http://127.0.0.1:33333')

class NativeSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import importlib,httpx
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'native-settings-fixture','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.s=importlib.reload(server);cfg=self.s.config_store.load();cfg.adapter.qq_enabled=True;cfg.adapter.napcat_mode='native';self.s.config_store.save(cfg);self.s.runtime.cfg=cfg
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.s.app),base_url='http://127.0.0.1',headers={'Authorization':'Bearer native-settings-fixture'})
    async def asyncTearDown(self):
        await self.client.aclose();await self.s.runtime.close_gateway();self.env.stop();self.tmp.cleanup()
    async def test_external_save_waits_for_owned_cleanup(self):
        owned=FakeProcess();owned.running=True;self.s.runtime.native_napcat.process=owned
        response=await self.client.patch('/api/config/section/channels',json={'napcat_mode':'external'});self.assertEqual(response.status_code,200);self.assertFalse(owned.running);self.assertEqual(self.s.config_store.load().adapter.napcat_mode,'external')
    async def test_invalid_mode_does_not_change_saved_config(self):
        response=await self.client.patch('/api/config/section/channels',json={'napcat_mode':'docker'});self.assertEqual(response.status_code,400);self.assertEqual(self.s.config_store.load().adapter.napcat_mode,'native')
    async def test_older_whole_config_client_preserves_native_mode(self):
        response=await self.client.put('/api/config',json={'adapter':{'qq_enabled':True,'onebot_token':'new-fixture-token'}});self.assertEqual(response.status_code,200);self.assertEqual(self.s.config_store.load().adapter.napcat_mode,'native')
    async def test_status_has_explicit_manager_without_docker_requirement(self):
        self.s.runtime.probe_qq=AsyncMock(return_value={'loggedIn':False,'httpReachable':False})
        response=await self.client.get('/api/qq/status');self.assertEqual(response.status_code,200);self.assertEqual(response.json()['manager']['mode'],'native');self.assertFalse(response.json()['manager']['requiresDocker'])

if __name__=='__main__':unittest.main()
