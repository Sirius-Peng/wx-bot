"""Fixed timeline evaluation uses real historical modules, no network or user data."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('natural_timeline_evaluation',ROOT/'tools/eval_natural_conversation.py')
evaluation=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluation)


class TimelineEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_fixed_timelines_and_reopened_reports(self):
        if not os.environ.get('WXBOT_EVAL_BASELINE_ROOT') and (not shutil.which('git') or subprocess.run(['git','cat-file','-e',evaluation.BASELINE],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode):
            self.skipTest('historical Git source absent: set WXBOT_EVAL_BASELINE_ROOT to the SHA256-verified checkpoint tree for native exported tests')
        with patch('logging.Logger.error'),patch('logging.Logger.exception'):
            report=await evaluation.evaluate()
        self.assertEqual(report['fixture_count'],20)
        self.assertTrue(report['passed'])
        self.assertEqual(len({fixture['id'] for fixture in report['fixtures']}),20)
        self.assertEqual(len(report['baseline_module_sha256']),4)
        self.assertEqual(report['totals']['new'],dict.fromkeys(evaluation.metric(),0))
        self.assertGreater(report['totals']['baseline']['interruptions'],0)
        self.assertGreater(report['totals']['baseline']['duplicate_submissions'],0)
        self.assertGreater(report['totals']['baseline']['memory_overreach'],0)
        with tempfile.TemporaryDirectory() as directory:
            json_path,md_path=evaluation.write_report(report,directory)
            self.assertTrue(json.loads(json_path.read_text(encoding='utf-8'))['passed'])
            self.assertIn('不是人工体感评分',md_path.read_text(encoding='utf-8'))
            self.assertIn('不把使用次数伪造为强度',md_path.read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
