import json,os,unittest
from pathlib import Path
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]))
class WindowsReleaseTests(unittest.TestCase):
 def test_version_alignment(self):
  p=json.loads((ROOT/'desktop/package.json').read_text());lock=json.loads((ROOT/'desktop/package-lock.json').read_text());self.assertEqual(p['version'],'2.1.5');self.assertEqual(p['version'],lock['version']);self.assertEqual(p['version'],lock['packages']['']['version'])
 def test_windows_onedir_for_startup(self):
  s=(ROOT/'desktop/scripts/build-backend.ps1').read_text(encoding='utf-8-sig');self.assertIn("'--onedir'",s);self.assertNotIn("'--onefile'",s);self.assertIn('wxbot-server\\wxbot-server.exe',s)
 def test_verifier_covers_dictionary_and_timezone(self):
  s=(ROOT/'desktop/scripts/verify-windows.cjs').read_text();self.assertIn('tree(source)',s);self.assertIn("'jieba','dict.txt'",s);self.assertIn("'tzdata','zoneinfo','Asia','Shanghai'",s)
 def test_existing_onefile_path_remains_fallback(self):
  s=(ROOT/'desktop/src/main/paths.js').read_text();self.assertIn("path.join(base, 'wxbot-server', exe)",s);self.assertIn('path.join(base, exe)',s)
 def test_default_quit_guard_and_tree_cleanup(self):
  s=(ROOT/'desktop/src/main/index.js').read_text();self.assertIn('installQuitGuard(app, quitApp)',s);self.assertIn('backend._killTree(backend.proc)',s)
 def test_native_win_build_retains_resources(self):
  s=(ROOT/'desktop/scripts/build-backend.ps1').read_text(encoding='utf-8-sig');self.assertIn("'tzdata','jieba'",s);self.assertIn('PYTHONUTF8',s);self.assertIn('tuna.tsinghua.edu.cn',s)
