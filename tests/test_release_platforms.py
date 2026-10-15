"""Release manifests describe only the platforms actually built for that version."""
import os,sys,unittest
from pathlib import Path
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from tools.check_docs import expected_assets
class ReleasePlatformsTests(unittest.TestCase):
    def test_mac_only_has_two_assets(self):
        self.assertEqual(expected_assets('2.1.5',['macos-arm64-dmg']),{'SHA256SUMS.txt','WxBot-2.1.5-macOS-arm64.dmg'})
    def test_all_platforms_have_four_assets(self):
        self.assertEqual(len(expected_assets('2.1.4',['windows-x64-setup','windows-x64-portable','macos-arm64-dmg'])),4)
    def test_empty_and_invalid_platforms_rejected(self):
        for value in (None,[],['windows-arm64'],['macos-arm64-dmg','macos-arm64-dmg'],[None],{}):
            with self.subTest(value=value),self.assertRaises(ValueError):expected_assets('2.1.5',value)
