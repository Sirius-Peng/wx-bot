"""Windows packaging and authenticated lifecycle contract, without user accounts."""
import asyncio,importlib,json,os
from pathlib import Path
import sys,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))

class WindowsBuildTests(unittest.TestCase):
 def test_win_build_includes_backend_and_arch_guard(self):
  p=json.loads((ROOT/'desktop/package.json').read_text());self.assertIn('build:backend:win',p['scripts']['pack:win']);self.assertIn('verify:win',p['scripts']['pack:win']);self.assertIn('--x64',p['scripts']['pack:win'])
 def test_unattended_windows_wrapper(self):
  s=(ROOT/'desktop/scripts/build-backend.bat').read_text();self.assertIn('build-backend.ps1',s);self.assertNotIn('pause',s.lower());self.assertIn('exit /b %errorlevel%',s)
 def test_native_python_validation(self):
  s=(ROOT/'desktop/scripts/build-backend.ps1').read_text(encoding='utf-8-sig');self.assertIn('struct.calcsize(chr(80)) == 8',s);self.assertIn('>= (3,10)',s);self.assertIn('if ($LASTEXITCODE -ne 0)',s)
 def test_arch_in_installer_names(self):
  s=(ROOT/'desktop/electron-builder.yml').read_text();self.assertIn('Windows-${arch}-Setup',s);self.assertIn('deleteAppDataOnUninstall: false',s)
 def test_utf8_spawn_environment(self):
  s=(ROOT/'desktop/src/main/backend.js').read_text();self.assertIn("PYTHONUTF8: '1'",s);self.assertIn("PYTHONIOENCODING: 'utf-8'",s)
 def test_targeted_process_tree_cleanup(self):
  s=(ROOT/'desktop/src/main/backend.js').read_text();self.assertIn("['/PID', String(proc.pid), '/T', '/F']",s);self.assertIn('/api/app/shutdown',s);self.assertNotIn('/IM',s)

class ShutdownTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  import httpx
  self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'shutdown-test','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
  import src.server as server;self.server=importlib.reload(server);self.instance=SimpleNamespace(should_exit=False);self.server.app.state.desktop_server=self.instance
  self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app,client=('127.0.0.1',10)),base_url='http://127.0.0.1')
 async def asyncTearDown(self):await self.client.aclose();await self.server.runtime.stop();self.env.stop();self.tmp.cleanup()
 async def test_shutdown_requires_authentication(self):self.assertEqual((await self.client.post('/api/app/shutdown')).status_code,401);self.assertFalse(self.instance.should_exit)
 async def test_authenticated_local_shutdown(self):
  r=await self.client.post('/api/app/shutdown',headers={'Authorization':'Bearer shutdown-test'});self.assertEqual(r.status_code,200);await asyncio.sleep(.06);self.assertTrue(self.instance.should_exit)
 async def test_remote_origin_denied(self):
  self.assertEqual((await self.client.post('/api/app/shutdown',headers={'Authorization':'Bearer shutdown-test','Origin':'https://other.example'})).status_code,403);self.assertFalse(self.instance.should_exit)
 async def test_non_loopback_client_denied(self):
  import httpx
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app,client=('192.0.2.2',10)),base_url='http://127.0.0.1') as c:
   self.assertEqual((await c.post('/api/app/shutdown',headers={'Authorization':'Bearer shutdown-test'})).status_code,403)
