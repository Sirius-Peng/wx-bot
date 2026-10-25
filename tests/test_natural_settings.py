"""Natural conversation configuration and admin-only memory controls."""
import importlib
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from src.config_store import AppConfig, ConfigStore


class NaturalConfigTests(unittest.TestCase):
    def test_defaults(self):
        bot = AppConfig().bot
        self.assertEqual(bot.reply_mode, 'natural')
        self.assertTrue(bot.late_reply_enabled)
        self.assertEqual(bot.message_merge_window, 2)

    def test_legacy_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(Path(d)/'config.json')
            for value in ('bad', None, False, ['natural']):
                cfg = store._from_dict({'bot': {'reply_mode':value, 'late_reply_enabled':'yes', 'memory_extract_interval':1}})
                self.assertEqual(cfg.bot.reply_mode, 'natural')
                self.assertFalse(cfg.bot.late_reply_enabled)
                self.assertEqual(cfg.bot.memory_extract_interval,3)
            cfg=store._from_dict({'bot': {'persona':'legacy persona'}})
            self.assertEqual(cfg.bot.persona,'legacy persona')
            self.assertEqual(cfg.bot.persona_profile,'custom')
            self.assertTrue(cfg.bot.late_reply_enabled)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(Path(d)/'config.json');cfg=AppConfig()
            cfg.bot.reply_mode='always';cfg.bot.late_reply_enabled=False;store.save(cfg)
            self.assertEqual(asdict(store.load()),asdict(cfg))


class NaturalAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'natural-fixture','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'})
        self.env.start()
        import src.server as server
        self.server=importlib.reload(server)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://localhost',headers={'Authorization':'Bearer natural-fixture'})

    async def asyncTearDown(self):
        await self.client.aclose();self.env.stop();self.tmp.cleanup()

    async def test_reply_section_isolated_and_validated(self):
        cfg=self.server.config_store.load();cfg.bot.persona='retain';cfg.bot.proactive_enabled=False;self.server.config_store.save(cfg)
        result=await self.client.patch('/api/config/section/reply',json={'reply_mode':'always','late_reply_enabled':False})
        self.assertEqual(result.status_code,200)
        cfg=self.server.config_store.load()
        self.assertEqual(cfg.bot.persona,'retain');self.assertFalse(cfg.bot.proactive_enabled)
        self.assertEqual(cfg.bot.reply_mode,'always');self.assertFalse(cfg.bot.late_reply_enabled)
        for values in ({'reply_mode':'never'},{'late_reply_enabled':'false'},{'proactive_enabled':True}):
            self.assertEqual((await self.client.patch('/api/config/section/reply',json=values)).status_code,400)

    async def test_legacy_whole_save_preserves_new_preferences(self):
        cfg=AppConfig();cfg.bot.reply_mode='always';cfg.bot.late_reply_enabled=False
        raw=asdict(cfg);del raw['bot']['reply_mode'];del raw['bot']['late_reply_enabled']
        result=self.server._merge_config(cfg,raw)
        self.assertEqual(result.bot.reply_mode,'always');self.assertFalse(result.bot.late_reply_enabled)

    async def test_pin_api_strict_and_missing(self):
        memory=SimpleNamespace(set_pinned=Mock(return_value=True))
        with patch.object(self.server.runtime,'_ensure_memory',return_value=memory):
            result=await self.client.patch('/api/memory/item',json={'pinned':True})
            self.assertEqual(result.status_code,200);memory.set_pinned.assert_called_once_with('item',True)
            for values in ({'pinned':'yes'},{'pinned':False,'content':'wrong'},[]):
                self.assertEqual((await self.client.patch('/api/memory/item',json=values)).status_code,400)
            memory.set_pinned.return_value=False
            self.assertEqual((await self.client.patch('/api/memory/missing',json={'pinned':False})).status_code,404)

    async def test_forgotten_explicit_release_and_auth(self):
        marker={'id':'m','content':'old fact','created_at':1}
        memory=SimpleNamespace(suppressions=[marker],release_suppression=Mock(return_value=True))
        with patch.object(self.server.runtime,'_ensure_memory',return_value=memory), patch.object(self.server.runtime,'memory_allow_relearning',memory.release_suppression):
            self.assertEqual((await self.client.get('/api/memory/forgotten')).json()['items'],[marker])
            result=await self.client.delete('/api/memory/forgotten/m')
            self.assertEqual(result.status_code,200);memory.release_suppression.assert_called_once_with('m')
            memory.release_suppression.return_value=False
            self.assertEqual((await self.client.delete('/api/memory/forgotten/m')).status_code,404)
            self.client.headers.clear()
            self.assertEqual((await self.client.get('/api/memory/forgotten')).status_code,401)
            self.assertEqual((await self.client.patch('/api/memory/item',json={'pinned':True})).status_code,401)


if __name__=='__main__':unittest.main()
