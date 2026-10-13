"""First-run state, authenticated setup completion and primary-provider validation."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from src.config_store import AppConfig, ConfigStore
from src.core.onboarding import OnboardingStore, model_fingerprint, test_primary_model
from src.runtime import BotRuntime

class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=OnboardingStore(Path(self.tmp.name)/'onboarding.json');self.cfg=AppConfig()
    def tearDown(self):self.tmp.cleanup()
    def test_fresh_auto_open(self):self.assertTrue(self.store.public(self.cfg)['shouldOpen'])
    def test_existing_config_not_forced_to_guide(self):
        self.cfg.llm.primary_key='test-secret';self.assertTrue(self.store.public(self.cfg)['completed']);self.assertFalse(self.store.public(self.cfg)['shouldOpen'])
    def test_progress_persisted_across_reload(self):
        s=self.store.load(self.cfg);s['step']=3;self.store.save(s);self.assertEqual(OnboardingStore(self.store.path).load(self.cfg)['step'],3)
    def test_dismissed_not_completed(self):
        s=self.store.load(self.cfg);s['dismissed']=True;self.store.save(s);p=self.store.public(self.cfg);self.assertFalse(p['completed']);self.assertFalse(p['shouldOpen'])
    def test_corrupt_progress_keeps_guide(self):
        self.store.path.write_text('{broken');self.assertTrue(self.store.public(self.cfg)['shouldOpen'])
    def test_receipt_invalidated_by_key_model_or_base(self):
        self.cfg.llm.primary_key='dummy';s=self.store.load(self.cfg);s['model_receipt']=model_fingerprint(self.cfg.llm);self.store.save(s);self.assertTrue(self.store.public(self.cfg)['modelVerified'])
        for key in ['primary_key','primary_model','primary_base']:
            cfg=AppConfig();cfg.llm.primary_key='dummy';setattr(cfg.llm,key,'changed');self.assertFalse(self.store.public(cfg)['modelVerified'])
    def test_public_no_secret_or_fingerprint(self):
        self.cfg.llm.primary_key='never-echo-secret';s=self.store.load(self.cfg);s['model_receipt']=model_fingerprint(self.cfg.llm);self.store.save(s);p=json.dumps(self.store.public(self.cfg));self.assertNotIn('never-echo-secret',p);self.assertNotIn(s['model_receipt'],p)
    def test_bad_step_normalized(self):
        s=self.store.load(self.cfg);s['step']=99;self.store.save(s);self.assertEqual(self.store.load(self.cfg)['step'],1)

class ModelTests(unittest.IsolatedAsyncioTestCase):
    def cfg(self):c=AppConfig();c.llm.primary_key='unit-only-secret';return c
    def client(self,response):
        c=AsyncMock();c.__aenter__.return_value=c;c.post.return_value=response;return c
    async def test_missing_key_no_network(self):
        with patch('src.core.onboarding.make_client') as client:r=await test_primary_model(AppConfig())
        self.assertFalse(r['ok']);client.assert_not_called()
    async def test_real_contract_primary_only(self):
        c=self.client(httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]}))
        with patch('src.core.onboarding.make_client',return_value=c):r=await test_primary_model(self.cfg())
        self.assertTrue(r['ok']);self.assertEqual(c.post.call_count,1);self.assertEqual(c.post.call_args.args[0],'https://api.deepseek.com/chat/completions')
        body=c.post.call_args.kwargs['json'];self.assertEqual(body['model'],'deepseek-flash');self.assertEqual(body['max_tokens'],32);self.assertFalse(c.post.call_args.kwargs['follow_redirects']);self.assertNotIn('unit-only-secret',json.dumps(r))
    async def test_error_messages_sanitized(self):
        for code in [401,402,403,404,429,500,302]:
            c=self.client(httpx.Response(code,json={'error':'unit-only-secret'}))
            with patch('src.core.onboarding.make_client',return_value=c):r=await test_primary_model(self.cfg())
            self.assertFalse(r['ok']);self.assertNotIn('unit-only-secret',json.dumps(r))
    async def test_no_text_not_verified(self):
        c=self.client(httpx.Response(200,json={'choices':[{'message':{'content':''}}]}))
        with patch('src.core.onboarding.make_client',return_value=c):r=await test_primary_model(self.cfg())
        self.assertFalse(r['ok'])
    async def test_timeout(self):
        c=self.client(None);c.post.side_effect=httpx.ReadTimeout('secret')
        with patch('src.core.onboarding.make_client',return_value=c):r=await test_primary_model(self.cfg())
        self.assertEqual(r['code'],'timeout')
    async def test_invalid_url_rejected(self):
        for url in ['bad','file:///tmp/data','https://user:pass@host','https://api.deepseek.com/?key=secret']:
            cfg=self.cfg();cfg.llm.primary_base=url
            with patch('src.core.onboarding.make_client') as client:r=await test_primary_model(cfg)
            self.assertFalse(r['ok']);client.assert_not_called()
    async def test_custom_provider_no_deepseek_fields(self):
        cfg=self.cfg();cfg.llm.primary_base='http://127.0.0.1:9000/v1';cfg.llm.primary_model='local'
        c=self.client(httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]}))
        with patch('src.core.onboarding.make_client',return_value=c):await test_primary_model(cfg)
        self.assertNotIn('thinking',c.post.call_args.kwargs['json']);self.assertEqual(c.post.call_args.args[0],'http://127.0.0.1:9000/v1/chat/completions')

class SetupAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'guide-auth','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.s=importlib.reload(server);self.c=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',headers={'Authorization':'Bearer guide-auth'})
    async def asyncTearDown(self):await self.c.aclose();self.env.stop();self.tmp.cleanup()
    def configured(self):
        cfg=self.s.config_store.load();cfg.llm.primary_key='synthetic-key';cfg.adapter.qq_enabled=True;cfg.adapter.wechat_enabled=False;self.s.config_store.save(cfg);self.s.runtime.cfg=cfg
        state=self.s.onboarding_store.load(cfg);state['completed']=False;state['model_receipt']=model_fingerprint(cfg.llm);self.s.onboarding_store.save(state);return cfg
    async def test_auth_required(self):
        self.c.headers.clear()
        for method,path in [('GET','/api/onboarding'),('PATCH','/api/onboarding'),('POST','/api/llm/test'),('POST','/api/onboarding/complete')]:self.assertEqual((await self.c.request(method,path,json={})).status_code,401)
    async def test_progress_no_false_completion(self):
        r=await self.c.patch('/api/onboarding',json={'step':4,'dismissed':True});self.assertEqual(r.status_code,200);self.assertFalse(r.json()['completed']);self.assertFalse(r.json()['shouldOpen'])
        self.assertEqual((await self.c.patch('/api/onboarding',json={'completed':True})).status_code,400)
    async def test_invalid_progress(self):
        for body in [{'step':0},{'step':True},{'step':6},{'dismissed':'yes'},[]]:self.assertEqual((await self.c.patch('/api/onboarding',json=body)).status_code,400)
    async def test_model_receipt_success(self):
        self.configured()
        with patch.object(self.s,'test_primary_model',AsyncMock(return_value={'ok':True,'message':'verified'})):r=await self.c.post('/api/llm/test')
        self.assertTrue(r.json()['ok']);self.assertTrue((await self.c.get('/api/onboarding')).json()['modelVerified'])
    async def test_model_test_does_not_auto_run_on_status(self):
        with patch.object(self.s,'test_primary_model',AsyncMock()) as run:
            await self.c.get('/api/onboarding');run.assert_not_called()
    async def test_busy_test_single_request(self):
        await self.s._model_test_lock.acquire()
        try:self.assertEqual((await self.c.post('/api/llm/test')).json()['code'],'busy')
        finally:self.s._model_test_lock.release()
    async def test_config_changed_during_test(self):
        cfg=self.configured()
        async def call(_):cfg.llm.primary_key='new-synthetic';self.s.config_store.save(cfg);return {'ok':True}
        with patch.object(self.s,'test_primary_model',side_effect=call):r=await self.c.post('/api/llm/test')
        self.assertFalse(r.json()['ok']);self.assertFalse((await self.c.get('/api/onboarding')).json()['modelVerified'])
    async def test_missing_config_not_complete(self):
        r=await self.c.post('/api/onboarding/complete',json={'start':True});self.assertFalse(r.json()['ok']);self.assertFalse((await self.c.get('/api/onboarding')).json()['completed'])
    async def test_offline_qq_not_complete(self):
        self.configured()
        with patch.object(self.s.runtime,'probe_qq',AsyncMock(return_value={'loggedIn':False})):r=await self.c.post('/api/onboarding/complete',json={})
        self.assertFalse(r.json()['ok']);self.assertFalse((await self.c.get('/api/onboarding')).json()['completed'])
    async def test_complete_starts_and_persists(self):
        self.configured()
        with patch.object(self.s.runtime,'probe_qq',AsyncMock(return_value={'loggedIn':True})),patch.object(self.s.runtime,'start',AsyncMock(return_value={'ok':True})) as start:r=await self.c.post('/api/onboarding/complete',json={'start':True})
        self.assertTrue(r.json()['ok']);start.assert_awaited_once();p=(await self.c.get('/api/onboarding')).json();self.assertTrue(p['completed']);self.assertFalse(p['shouldOpen'])
    async def test_start_failure_not_complete(self):
        self.configured()
        with patch.object(self.s.runtime,'probe_qq',AsyncMock(return_value={'loggedIn':True})),patch.object(self.s.runtime,'start',AsyncMock(return_value={'ok':False,'message':'test-start-failed'})):r=await self.c.post('/api/onboarding/complete',json={})
        self.assertFalse(r.json()['ok']);self.assertFalse((await self.c.get('/api/onboarding')).json()['completed'])
    async def test_unlogged_wechat_not_complete(self):
        cfg=self.configured();cfg.adapter.qq_enabled=False;cfg.adapter.wechat_enabled=True;self.s.config_store.save(cfg)
        r=await self.c.post('/api/onboarding/complete',json={});self.assertFalse(r.json()['ok']);self.assertIn('微信',r.json()['message'])
    async def test_completed_receipt_rechecked(self):
        cfg=self.configured();cfg.llm.primary_model='changed';self.s.config_store.save(cfg)
        with patch.object(self.s.runtime,'probe_qq',AsyncMock(return_value={'loggedIn':True})):r=await self.c.post('/api/onboarding/complete',json={})
        self.assertFalse(r.json()['ok']);self.assertIn('测试当前模型',r.json()['message'])

    async def test_incomplete_guide_not_auto_started_after_backend_restart(self):
        self.configured()
        with patch.object(self.s.runtime,'start_learning') as learning,patch.object(self.s.runtime,'start',AsyncMock()) as start:
            async with self.s.lifespan(self.s.app):pass
            learning.assert_not_called();start.assert_not_called()
    async def test_existing_install_auto_start_preserved(self):
        cfg=self.s.config_store.load();cfg.llm.primary_key='legacy-synthetic';cfg.adapter.wechat_enabled=False;cfg.adapter.qq_enabled=True;self.s.config_store.save(cfg)
        with patch.object(self.s.runtime,'start_learning') as learning,patch.object(self.s.runtime,'start',AsyncMock(return_value={'ok':True})) as start:
            async with self.s.lifespan(self.s.app):pass
            learning.assert_called_once();start.assert_awaited_once()

class ILinkGuideTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_qr_response_cannot_save_new_session(self):
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(Path(d)/'config.json');store.save(AppConfig());rt=BotRuntime(store,Path(d));rt._login={'channel':'ilink','qrcode':'old'}
            c=AsyncMock();c.__aenter__.return_value=c
            async def get(*a,**k):rt._login['qrcode']='new';return httpx.Response(200,json={'status':'confirmed'})
            c.get.side_effect=get
            with patch('src.runtime.make_client',return_value=c),patch.object(rt,'_save_ilink_credentials') as save:r=await rt.ilink_login_check()
            self.assertEqual(r['status'],'superseded');save.assert_not_called()
    async def test_credential_write_failure_not_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(Path(d)/'config.json');store.save(AppConfig());rt=BotRuntime(store,Path(d));rt._login={'channel':'ilink','qrcode':'test'}
            c=AsyncMock();c.__aenter__.return_value=c;c.get.return_value=httpx.Response(200,json={'status':'confirmed'})
            with patch('src.runtime.make_client',return_value=c),patch.object(rt,'_save_ilink_credentials',return_value=False):r=await rt.ilink_login_check()
            self.assertEqual(r['status'],'failed');self.assertFalse(r['ok'])

class GuideUIContractTests(unittest.TestCase):
    def test_home_qq_has_single_button_and_details_remain(self):
        root=Path(__file__).resolve().parents[1];html=(root/'src/web/index.html').read_text(encoding='utf-8')
        card=html.split('id="home-qq-card"')[1].split('<div class="stat-grid">')[0];self.assertEqual(card.count('<button'),1)
        for id in ['btn-home-qq','btn-setup-qq','qq-choice-modal','qq-login-modal','page-guide','guide-model-key','btn-guide-wechat']:self.assertEqual(html.count('id="'+id+'"'),1)
        self.assertLess(html.index('/static/guide.js'),html.index('/static/app.js'))
    def test_tutorial_uses_official_links_and_correct_model(self):
        root=Path(__file__).resolve().parents[1];html=(root/'src/web/index.html').read_text(encoding='utf-8');self.assertIn('https://platform.deepseek.com/api_keys',html);self.assertIn('deepseek-flash',html);self.assertIn('少量 API 用量',html)

if __name__=='__main__':unittest.main()
