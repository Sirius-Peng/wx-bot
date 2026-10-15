"""NapCat protocol contract, Windows callback lifecycle, isolated settings saves."""
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from src.config_store import AppConfig, ConfigStore
from src.core.docker import DockerManager, DockerState, _run
from src.core.napcat import NapCatWebUI
from src.runtime import BotRuntime

class DockerOutputTests(unittest.TestCase):
    def test_windows_none_pipes(self):
        with patch('src.core.docker.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=None,stderr=None)):
            self.assertEqual(_run(['docker','logs','napcat']),(0,'',''))
    def test_windows_bytes_encoding(self):
        with patch('src.core.docker.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=b'\xffQQ\n',stderr=b' err ')):
            self.assertEqual(_run(['docker']),(0,'\ufffdQQ','err'))
    def test_windows_text_encoding_args(self):
        with patch('src.core.docker.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as run:
            _run(['docker']);self.assertEqual(run.call_args.kwargs['encoding'],'utf-8');self.assertEqual(run.call_args.kwargs['errors'],'replace')
    def test_mirror_default(self):
        self.assertEqual(AppConfig().adapter.napcat_image_mirror,'docker.m.daocloud.io')
    def test_config_tokens(self):
        _,c=DockerManager().napcat_config_files('web-secret','ws://host.docker.internal:8959/ws/onebot','ob-secret')
        self.assertEqual(c['network']['websocketClients'][0]['token'],'ob-secret')
        self.assertEqual(c['network']['httpServers'][0]['token'],'ob-secret')

class DockerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_both_streams(self):
        d=DockerManager();d.detect=lambda:SimpleNamespace(installed=True,cli_path='docker')
        with patch('src.core.docker._run',return_value=(0,'stdout','stderr')) as run:
            r=await d.napcat_logs(9999);self.assertIn('stdout',r['log']);self.assertIn('stderr',r['log']);self.assertIn('2000',run.call_args.args[0])
    async def test_cached_image_no_pull(self):
        d=DockerManager()
        with patch('src.core.docker._run',return_value=(0,'','')) as run:
            self.assertTrue((await d.ensure_image('docker','alpine:latest'))['cached']);self.assertEqual(run.call_count,1)
    async def test_mirror_first_and_tag(self):
        d=DockerManager()
        with patch('src.core.docker._run',side_effect=[(1,'',''),(0,'',''),(0,'','')]) as run:
            r=await d.ensure_image('docker','alpine:latest');self.assertTrue(r['ok'])
            self.assertEqual(run.call_args_list[1].args[0],['docker','pull','docker.m.daocloud.io/library/alpine:latest'])
            self.assertEqual(run.call_args_list[2].args[0][-1],'alpine:latest')
    async def test_official_fallback(self):
        d=DockerManager()
        with patch('src.core.docker._run',side_effect=[(1,'',''),(1,'','mirror-error'),(0,'','')]):
            r=await d.ensure_image('docker','mlikiowa/napcat-docker:latest');self.assertEqual(r['source'],'mlikiowa/napcat-docker:latest')
    async def test_blank_mirror_official(self):
        d=DockerManager();d.image_mirror=''
        with patch('src.core.docker._run',side_effect=[(1,'',''),(0,'','')]) as run:
            await d.ensure_image('docker','alpine:latest');self.assertEqual(run.call_args.args[0],['docker','pull','alpine:latest'])
    async def test_pull_errors_return_failure(self):
        with patch('src.core.docker._run',return_value=(1,'','failed')):
            self.assertFalse((await DockerManager().ensure_image('docker','alpine:latest'))['ok'])
    async def test_reuse_running_container(self):
        d=DockerManager();d.detect=lambda:SimpleNamespace(installed=True,running=True,cli_path='docker')
        with patch('src.core.docker._run',return_value=(0,'true','')) as run,patch('src.core.docker.port_open',return_value=True):
            await d._ensure_napcat_impl('web','ws://host:9000/ws/onebot',True,'token')
            self.assertEqual(run.call_count,1);self.assertEqual(d.task['stage'],'waiting_login')
    async def test_reuse_stopped_container(self):
        d=DockerManager();d.detect=lambda:SimpleNamespace(installed=True,running=True,cli_path='docker')
        with patch('src.core.docker._run',side_effect=[(0,'false',''),(0,'','')]) as run,patch('src.core.docker.port_open',return_value=True):
            await d._ensure_napcat_impl('web','ws://host:9000/ws/onebot',True,'token')
            self.assertEqual(run.call_args.args[0],['docker','start','napcat'])
    async def test_duplicate_start_single_task(self):
        d=DockerManager();d._ensure_napcat_impl=AsyncMock(return_value=None)
        await d.ensure_napcat('w','ws://h/p');r=await d.ensure_napcat('w','ws://h/p');self.assertTrue(r['pending'])
        await d._napcat_task

class NapCatTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_accounts_new_and_old(self):
        c=NapCatWebUI('token');c._post=AsyncMock(side_effect=[{'code':0,'data':[{'uin':'12345','nickName':'测试'}]},{'code':0,'data':['12345','54321','bad']},{'code':0,'data':'12345'}])
        r=await c.quick_accounts();self.assertEqual(r['accounts'],[{'qq':'12345','nickname':'测试'},{'qq':'54321','nickname':''}]);self.assertEqual(r['autoLoginQq'],'12345')
    async def test_accounts_error_distinguished_from_empty(self):
        c=NapCatWebUI('token');c._post=AsyncMock(return_value={'code':401})
        r=await c.quick_accounts();self.assertFalse(r['ok']);self.assertIn('失败',r['message'])
    async def test_quick_request_pending_not_success(self):
        c=NapCatWebUI('token');c.check_login=AsyncMock(return_value={'loggedIn':False});c.quick_accounts=AsyncMock(return_value={'accounts':[{'qq':'12345'}]});c._post=AsyncMock(return_value={'code':0})
        r=await c.quick_login('12345',True);self.assertTrue(r['pending']);self.assertNotIn('loggedIn',r)
        self.assertEqual(c._post.call_args_list[0].args,('/api/QQLogin/SetQuickLogin',{'uin':'12345'}))
        self.assertEqual(c._post.call_args_list[1].args,('/api/QQLogin/SetQuickLoginQQ',{'uin':'12345'}))
    async def test_quick_does_not_remember_without_opt_in(self):
        c=NapCatWebUI('token');c.check_login=AsyncMock(return_value={});c.quick_accounts=AsyncMock(return_value={'accounts':[{'qq':'12345'}]});c._post=AsyncMock(return_value={'code':0})
        await c.quick_login('12345');self.assertEqual(c._post.call_count,1)
    async def test_unknown_quick_account(self):
        c=NapCatWebUI('token');c.check_login=AsyncMock(return_value={});c.quick_accounts=AsyncMock(return_value={'accounts':[]});c._post=AsyncMock()
        self.assertFalse((await c.quick_login('12345'))['ok']);c._post.assert_not_called()
    async def test_quick_already_online(self):
        c=NapCatWebUI('token');c.check_login=AsyncMock(return_value={'loggedIn':True});c._post=AsyncMock()
        self.assertTrue((await c.quick_login('12345'))['loggedIn']);c._post.assert_not_called()
    async def test_invalid_qq(self):
        self.assertFalse((await NapCatWebUI('token').quick_login('bad'))['ok'])
    async def test_hot_config_preserves_integrations(self):
        c=NapCatWebUI('token');other={'name':'other-app','url':'ws://other:3000/ws','token':'other'}
        before={'network':{'websocketClients':[other],'httpServers':[]},'musicSignUrl':'keep'}
        c._post=AsyncMock(side_effect=[{'code':0,'data':before},{'code':0}]);template=DockerManager().napcat_config_files('web','ws://host:9999/ws/onebot','secret')[1]
        r=await c.sync_onebot(template);self.assertFalse(r['restarted']);after=json.loads(c._post.call_args.args[1]['config'])
        self.assertEqual(after['network']['websocketClients'][0],other);self.assertEqual(after['musicSignUrl'],'keep');self.assertEqual(len(before['network']['websocketClients']),1)
    async def test_hot_config_unchanged(self):
        c=NapCatWebUI('token');template=DockerManager().napcat_config_files('web','ws://host:9999/ws/onebot','secret')[1];c._post=AsyncMock(return_value={'code':0,'data':copy.deepcopy(template)})
        self.assertFalse((await c.sync_onebot(template))['changed']);self.assertEqual(c._post.call_count,1)
    async def test_qrcode_not_ready(self):
        c=NapCatWebUI('token');c._post=AsyncMock(return_value={'code':0,'data':{'qrcode':None}})
        self.assertTrue((await c.qrcode())['pending'])
    async def test_phase_does_not_imply_login(self):
        c=NapCatWebUI('token');c._post=AsyncMock(return_value={'code':0,'data':{'isLogin':False,'loginPhase':'confirmed'}})
        self.assertFalse((await c.check_login())['loggedIn'])
    async def test_refresh_credential_once(self):
        c=NapCatWebUI('token');c.login=AsyncMock(return_value={'ok':True})
        responses=iter([httpx.Response(401),httpx.Response(200,json={'code':0,'data':[]})])
        fake=AsyncMock();fake.post.side_effect=lambda *a,**kw:next(responses);fake.__aenter__.return_value=fake
        with patch('src.core.napcat.make_client',return_value=fake):r=await c._post('/api/test')
        self.assertEqual(r['code'],0);self.assertEqual(c.login.call_count,2);self.assertEqual(fake.post.call_count,2)

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=ConfigStore(Path(self.tmp.name)/'config.json');cfg=AppConfig();cfg.adapter.qq_enabled=True;self.store.save(cfg);self.rt=BotRuntime(self.store,Path(self.tmp.name))
        self.rt.docker.detect=lambda:DockerState(installed=True,running=True)
        self.rt.docker.napcat_status=lambda *a:{'running':True}
        self.rt.probe_qq=AsyncMock(return_value={'loggedIn':True,'httpReachable':True})
    async def asyncTearDown(self):
        await self.rt.close_onebot_ingress();self.tmp.cleanup()
    async def test_connection_before_bot_start(self):
        ws=object();self.rt.attach_onebot_ws(ws);r=await self.rt.qq_status();self.assertTrue(r['reverseWsConnected']);self.assertFalse(r['messageReceiverStarted']);self.assertEqual(r['wsConnections'],1)
    async def test_multiple_connections_disconnect_one(self):
        a,b=object(),object();self.rt.attach_onebot_ws(a);self.rt.attach_onebot_ws(b);self.rt.detach_onebot_ws(a);self.assertTrue((await self.rt.qq_status())['reverseWsConnected']);self.rt.detach_onebot_ws(b);self.assertFalse((await self.rt.qq_status())['reverseWsConnected'])
    async def test_http_events_visible_pre_start(self):
        self.rt.note_onebot_event('http');r=await self.rt.qq_status();self.assertEqual(r['messageTransport'],'http_post');self.assertGreater(r['lastHttpEventAt'],0);self.assertFalse(r['reverseWsConnected'])
    async def test_callback_port_authoritative(self):
        self.rt.set_http_port(8959);self.rt._onebot_ingress=SimpleNamespace(port=43210,close=AsyncMock());self.assertEqual(self.rt._napcat_ws_url(),'ws://host.docker.internal:43210/ws/onebot')
    async def test_runtime_awaits_ready_qr(self):
        c=SimpleNamespace(check_login=AsyncMock(return_value={'loggedIn':False}),qrcode=AsyncMock(side_effect=[{'ok':False,'pending':True},{'ok':True,'image':'data'}]));self.rt._webui_client=lambda:c
        r=await self.rt.qq_login_qrcode();self.assertEqual(r['image'],'data');self.assertEqual(c.qrcode.call_count,2)
    async def test_no_qr_for_already_logged(self):
        c=SimpleNamespace(check_login=AsyncMock(return_value={'loggedIn':True}),qrcode=AsyncMock());self.rt._webui_client=lambda:c
        self.assertTrue((await self.rt.qq_login_qrcode())['loggedIn']);c.qrcode.assert_not_called()

class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'settings-test','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.server=importlib.reload(server);self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',headers={'Authorization':'Bearer settings-test'})
    async def asyncTearDown(self):
        await self.client.aclose();self.env.stop();self.tmp.cleanup()
    async def test_scoped_save_preserves_other_sections(self):
        cfg=self.server.config_store.load();cfg.llm.primary_key='existing-secret';cfg.bot.persona='old';self.server.config_store.save(cfg)
        r=await self.client.patch('/api/config/section/persona',json={'persona':'new'});self.assertEqual(r.status_code,200)
        cfg=self.server.config_store.load();self.assertEqual(cfg.bot.persona,'new');self.assertEqual(cfg.llm.primary_key,'existing-secret');self.assertNotIn('existing-secret',r.text)
    async def test_cross_section_rejected(self):
        self.assertEqual((await self.client.patch('/api/config/section/persona',json={'primary_key':'bad'})).status_code,400)
    async def test_masked_key_preserved(self):
        cfg=self.server.config_store.load();cfg.llm.primary_key='real';self.server.config_store.save(cfg)
        await self.client.patch('/api/config/section/primary',json={'primary_key':'••••••'});self.assertEqual(self.server.config_store.load().llm.primary_key,'real')
    async def test_empty_key_can_be_cleared(self):
        await self.client.patch('/api/config/section/primary',json={'primary_key':''});self.assertEqual(self.server.config_store.load().llm.primary_key,'')
    async def test_invalid_mirror_rejected(self):
        self.assertEqual((await self.client.patch('/api/config/section/channels',json={'napcat_image_mirror':'https://bad/path'})).status_code,400)
    async def test_bool_type_strict(self):
        self.assertEqual((await self.client.patch('/api/config/section/memory',json={'memory_enabled':'false'})).status_code,400)
    async def test_unauthorized_patch(self):
        self.client.headers.clear();self.assertEqual((await self.client.patch('/api/config/section/persona',json={})).status_code,401)
    async def test_http_bad_auth_does_not_track(self):
        cfg=self.server.config_store.load();cfg.adapter.qq_enabled=True;cfg.adapter.onebot_token='ob-secret';self.server.config_store.save(cfg)
        await self.client.post('/onebot/event',json={'post_type':'meta_event'});self.assertEqual(self.server.runtime._qq_event_count,0)
    async def test_http_valid_event_tracks_without_bot(self):
        cfg=self.server.config_store.load();cfg.adapter.qq_enabled=True;cfg.adapter.onebot_token='ob-secret';self.server.config_store.save(cfg)
        r=await self.client.post('/onebot/event',json={'post_type':'meta_event'},headers={'Authorization':'Bearer ob-secret'})
        self.assertEqual(r.json()['retcode'],100);self.assertEqual(self.server.runtime._qq_event_count,1)

class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_listener_auth_and_no_management(self):
        from src.core.onebot_ingress import OneBotIngress
        cfg=AppConfig();cfg.adapter.qq_enabled=True;cfg.adapter.onebot_token='callback-secret'
        ingress=OneBotIngress();events=[]
        async def ws_handler(ws):
            await ws.accept();events.append(await ws.receive_json());await ws.close()
        async def http_handler(request):events.append(await request.json());return {'ok':True}
        try:
            port=await ingress.start(ws_handler,http_handler,lambda:cfg);self.assertGreater(port,0);self.assertEqual(await ingress.start(ws_handler,http_handler,lambda:cfg),port)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}') as c:
                self.assertEqual((await c.get('/api/config')).status_code,404)
                self.assertEqual((await c.post('/onebot/event',json={})).status_code,403)
                self.assertEqual((await c.post('/onebot/event',json={'valid':True},headers={'Authorization':'Bearer callback-secret'})).status_code,200)
            from websockets.asyncio.client import connect
            import inspect
            options={'additional_headers':{'Authorization':'Bearer callback-secret'}}
            if 'proxy' in inspect.signature(connect).parameters:options['proxy']=None
            async with connect(f'ws://127.0.0.1:{port}/ws/onebot',**options) as ws:
                await ws.send(json.dumps({'post_type':'meta_event'}));await ws.wait_closed()
            self.assertEqual(len(events),2)
        finally:await ingress.close()
        self.assertEqual(ingress.port,0)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_health_not_login(self):
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(Path(d)/'config.json');store.save(AppConfig());rt=BotRuntime(store,Path(d))
            fake=SimpleNamespace(_client=SimpleNamespace(aclose=AsyncMock()),_call=AsyncMock(side_effect=[{'status':'ok','retcode':0,'data':{'user_id':12345}},{'status':'ok','data':{'online':False,'good':False}},{'status':'ok','data':{'app_name':'NapCat','app_version':'4'}}]))
            with patch('src.runtime.OneBotAdapter',return_value=fake):r=await rt.probe_qq()
            self.assertTrue(r['httpReachable']);self.assertFalse(r['loggedIn']);self.assertEqual(r['gatewayVersion'],'4')
    async def test_unsupported_health_keeps_login(self):
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(Path(d)/'config.json');store.save(AppConfig());rt=BotRuntime(store,Path(d))
            fake=SimpleNamespace(_client=SimpleNamespace(aclose=AsyncMock()),_call=AsyncMock(side_effect=[{'status':'ok','retcode':0,'data':{'user_id':12345}},{'status':'failed','data':[]},{}]))
            with patch('src.runtime.OneBotAdapter',return_value=fake):r=await rt.probe_qq()
            self.assertTrue(r['loggedIn'])

class ServerWSTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'ws-fixture','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.server=importlib.reload(server);cfg=server.config_store.load();cfg.adapter.qq_enabled=True;cfg.adapter.onebot_token='ob-secret';cfg.adapter.wechat_enabled=False;server.config_store.save(cfg)
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_actual_ws_registered_before_bot_start(self):
        from fastapi.testclient import TestClient
        with TestClient(self.server.app) as c:
            with c.websocket_connect('/ws/onebot',headers={'Authorization':'Bearer ob-secret'}) as ws:
                ws.send_json({'post_type':'meta_event'});ws.send_text('not-json')
                # a status API round trip synchronizes observation with the running server
                with patch.object(self.server.runtime,'qq_status',AsyncMock(return_value={'connected':bool(self.server.runtime.onebot_ws)})):
                    self.assertTrue(c.get('/api/qq/status',headers={'Authorization':'Bearer ws-fixture'}).json()['connected'])
                self.assertEqual(len(self.server.runtime.onebot_ws),1)
            self.assertEqual(len(self.server.runtime.onebot_ws),0)
    def test_bad_ws_token_not_registered(self):
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        with TestClient(self.server.app) as c:
            try:
                with c.websocket_connect('/ws/onebot',headers={'Authorization':'bad'}) as ws:ws.receive_json()
            except WebSocketDisconnect:pass
            self.assertEqual(len(self.server.runtime.onebot_ws),0)

class OfflineSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_copy_round_trip_and_token(self):
        d=DockerManager();d.detect=lambda:SimpleNamespace(installed=True,running=True,cli_path='docker');d.ensure_image=AsyncMock(return_value={'ok':True})
        before={'network':{'websocketClients':[{'name':'third-party','url':'ws://other:333/ws'},{'name':'wxbot-reverse-ws','url':'ws://old:100/ws'}]},'plugins':['keep']}
        written={};commands=[]
        def run(cmd,timeout):
            commands.append(cmd)
            if cmd[1]=='cp':
                if cmd[2].endswith(':/dst/.'):
                    dest=Path(cmd[3]);(dest/'onebot11_12345.json').write_text(written.get('onebot11_12345.json',json.dumps(before)),encoding='utf-8')
                else:
                    for p in Path(cmd[2]).glob('*.json'):written[p.name]=p.read_text(encoding='utf-8')
            if cmd[1]=='inspect':return (0,'false','')
            return (0,'','')
        with patch('src.core.docker._run',side_effect=run):r=await d.sync_napcat_ws_url('ws://host:43210/ws/onebot','secret')
        self.assertTrue(r['ok']);self.assertFalse(r['restarted']);after=json.loads(written['onebot11_12345.json']);self.assertEqual(after['plugins'],['keep']);self.assertEqual(after['network']['websocketClients'][0]['url'],'ws://other:333/ws');self.assertEqual(after['network']['websocketClients'][1]['token'],'secret');self.assertFalse(any('sed' in ' '.join(cmd) for cmd in commands))
    async def test_invalid_json_no_writes(self):
        d=DockerManager();d.detect=lambda:SimpleNamespace(installed=True,running=True,cli_path='docker');d.ensure_image=AsyncMock(return_value={'ok':True});commands=[]
        def run(cmd,timeout):
            commands.append(cmd)
            if cmd[1]=='cp' and cmd[2].endswith(':/dst/.'):(Path(cmd[3])/'onebot11.json').write_text('{broken')
            return (0,'','')
        with patch('src.core.docker._run',side_effect=run):r=await d.sync_napcat_ws_url('ws://host:43210/ws/onebot','secret')
        self.assertFalse(r['ok']);self.assertFalse(any(cmd[1]=='cp' and cmd[3].endswith(':/dst') for cmd in commands));self.assertEqual(commands[-1][1:3],['rm','-f'])


class UIContractTests(unittest.TestCase):
    def test_settings_cards_and_fields_are_contained(self):
        from html.parser import HTMLParser
        class Parser(HTMLParser):
            def __init__(self):super().__init__();self.stack=[];self.fields={};self.buttons=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if 'data-save-section' in attrs:self.buttons.append(attrs['data-save-section'])
                if tag in ('input','select','textarea') and attrs.get('id'):
                    section=next((x.get('data-settings-section') for _,x in reversed(self.stack) if x.get('data-settings-section')),None)
                    page=next((x.get('id') for _,x in reversed(self.stack) if 'page' in x.get('class','').split()),None)
                    self.fields[attrs['id']]=(section,page)
                if tag not in ('input','meta','link','img','br','hr','source'):self.stack.append((tag,attrs))
            def handle_endtag(self,tag):
                for i in range(len(self.stack)-1,-1,-1):
                    if self.stack[i][0]==tag:self.stack=self.stack[:i];break
        parser=Parser();parser.feed((Path(__file__).resolve().parents[1]/'src/web/index.html').read_text(encoding='utf-8'))
        for name in ('primary','fallback','judge','vision'):
            for field in ('base','model','key'):self.assertEqual(parser.fields[name+'-'+field],(name,'page-settings'))
        for field,name in [('bot-name','persona'),('bot-persona','persona'),('typing-indicator','reply'),('memory-interval','memory')]:self.assertEqual(parser.fields[field],(name,'page-settings'))
        self.assertEqual(set(parser.buttons),{'primary','fallback','judge','vision','search','persona','reply','memory','proactive','channels'})
    def test_no_global_form_save_remains(self):
        js=(Path(__file__).resolve().parents[1]/'src/web/app.js').read_text(encoding='utf-8')
        self.assertNotIn("api('/api/config', { method: 'PUT'",js)
        self.assertIn('const values = Object.fromEntries(fields.filter',js)
    def test_mainland_npm_lock_keeps_integrity(self):
        root=Path(__file__).resolve().parents[1];data=json.loads((root/'desktop/package-lock.json').read_text(encoding='utf-8'))
        for value in data['packages'].values():
            if value.get('resolved','').startswith('https://registry.npmmirror.com/'):
                self.assertTrue(value.get('integrity'))

if __name__=='__main__':unittest.main()
