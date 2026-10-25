"""Stress harness contracts; these tests do not claim model quality or train a model."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.decision_model.eval_stress import DATASET, DATASET_SHA256, evaluate, message, read_cases, summarize


class StressFixtures(unittest.TestCase):
    def test_dataset_is_fixed_at_120_independent_sessions(self):
        rows = read_cases()
        self.assertEqual(len(rows), 120)
        self.assertEqual(len({row['session_id'] for row in rows}), 120)
        self.assertEqual(len({row['slice'] for row in rows}), 12)
        self.assertTrue(all(n == 10 for n in Counter(row['slice'] for row in rows).values()))
        self.assertTrue(all(row['provenance']['human_labelled'] is False for row in rows))

    def test_manifest_hash_matches_code_and_fixture(self):
        manifest = json.loads(DATASET.with_name('stress-v1.manifest.json').read_text())
        self.assertEqual(manifest['sha256'], DATASET_SHA256)
        self.assertEqual(hashlib.sha256(DATASET.read_bytes()).hexdigest(), DATASET_SHA256)
        self.assertIn('never training or calibration', manifest['purpose'])

    def test_multi_turn_uses_actual_merge_envelopes(self):
        row = next(row for row in read_cases() if row['slice'] == 'multi_turn')
        msg = message(row)
        self.assertEqual(msg.content, row['state']['current'])
        self.assertEqual(len(msg.batch_messages), 2)
        self.assertEqual(msg.batch_messages[0]['content'], row['state']['current'].split('\n')[0])
        self.assertEqual(msg.chat_id, row['session_id'])

    def test_single_case_does_not_fabricate_batch_history(self):
        row = read_cases()[0]
        self.assertEqual(message(row).batch_messages, [])

    def test_counts_abstentions_and_exact_silence_separately(self):
        rows = [{'label': 'reply', 'protected': True}, {'label': 'defer', 'protected': False},
            {'label': 'context_only', 'protected': False}]
        predictions = [{'action': 'reply', 'source': 'conservative'}, {'action': 'context_only'}, {'action': 'context_only'}]
        out = summarize(rows, predictions, accepted=[False, True, True])
        self.assertEqual(out['protected_false_silence'], 0)
        self.assertEqual(out['reply_recall'], 1)
        self.assertEqual(out['coverage'], 2 / 3)
        self.assertEqual(out['silent_precision'], .5)
        self.assertEqual(out['withholding_precision_not_reply'], 1)

    def test_false_silence_counts_as_missed_reply(self):
        out = summarize([{'label': 'reply', 'protected': True}], [{'action': 'defer'}])
        self.assertEqual(out['protected_false_silence'], 1)
        self.assertEqual(out['reply_recall'], 0)
        self.assertEqual(out['silent_precision'], 0)

    def test_no_silence_is_unknown_precision_not_perfect(self):
        out = summarize([{'label': 'reply', 'protected': True}], [{'action': 'reply'}])
        self.assertIsNone(out['silent_precision'])

    def test_length_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize([{'label': 'reply', 'protected': True}], [])
        with self.assertRaises(ValueError):
            summarize([{'label': 'reply', 'protected': True}], [{'action': 'reply'}], accepted=[])

    def test_unknown_prediction_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize([{'label': 'reply', 'protected': True}], [{'action': 'discard'}])


class StressReportPreservation(unittest.IsolatedAsyncioTestCase):
    async def test_existing_report_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'first-observation.json'
            output.write_text('initial untouched observation')
            with self.assertRaisesRegex(ValueError, 'report_exists'):
                await evaluate(Path(folder) / 'missing-model', output)
            self.assertEqual(output.read_text(), 'initial untouched observation')


if __name__ == '__main__':
    unittest.main()
