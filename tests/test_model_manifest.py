"""Manifest integrity only; tiny fixtures do not claim model training or acceptance."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.core.local_decision import load_manifest


class ModelManifest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('model.onnx', 'tokenizer.json', 'policy.json'):
            (self.root / name).write_text('fixture-only-not-a-model')
        (self.root / 'calibration.json').write_text(json.dumps({
            'fitted_on_independent_split': True, 'quantized': True, 'split_sha256': '1' * 64}))
        gate = {'passed': True, 'checks': {'independent_split': True, 'protected_zero_false_silence': True}}
        self.manifest = {'schema': 'wxbot.local-decision.v1', 'actions': ['reply', 'defer', 'context_only'],
            'calibration': {'fitted_on_independent_split': True}, 'quality_gate': gate,
            'evaluation': {'quality_gate': copy.deepcopy(gate), 'split_sha256': '2' * 64, 'warm_p95_ms': 250,
                'metrics': {'protected': 100, 'protected_missed': 0, 'silent_accepted': 100,
                    'silent_precision': .99, 'reply_records': 100, 'reply_recall': .995, 'coverage': .5}},
            'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}}
        self.save()

    def save(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.temp.cleanup()

    def rejected(self, mutate, message):
        mutate(self.manifest)
        self.save()
        with self.assertRaisesRegex(ValueError, message):
            load_manifest(self.root)

    def test_valid_fixture(self):
        self.assertEqual(load_manifest(self.root)['actions'], ['reply', 'defer', 'context_only'])

    def test_truthy_string_pass_is_not_a_gate(self):
        self.rejected(lambda m: m['quality_gate'].update(passed='true'), 'gate_failed')

    def test_empty_checks_are_not_a_gate(self):
        self.rejected(lambda m: m['quality_gate'].update(checks={}), 'gate_checks')

    def test_check_integer_is_not_boolean_true(self):
        self.rejected(lambda m: m['quality_gate']['checks'].update(independent_split=1), 'gate_checks')

    def test_evaluation_must_match_gate(self):
        self.rejected(lambda m: m['evaluation']['quality_gate'].update(passed=False), 'gate_evaluation')

    def test_evaluation_integer_cannot_impersonate_boolean(self):
        self.rejected(lambda m: m['evaluation']['quality_gate'].update(passed=1), 'gate_evaluation')

    def test_empty_silence_cohort_cannot_pass(self):
        self.rejected(lambda m: m['evaluation']['metrics'].update(silent_accepted=0), 'gate_metrics')

    def test_reply_recall_is_rechecked(self):
        self.rejected(lambda m: m['evaluation']['metrics'].update(reply_recall=.994), 'gate_metrics')

    def test_reply_cohort_minimum_is_rechecked(self):
        self.rejected(lambda m: m['evaluation']['metrics'].update(reply_records=99), 'gate_metrics')

    def test_coverage_is_rechecked(self):
        self.rejected(lambda m: m['evaluation']['metrics'].update(coverage=.49), 'gate_metrics')

    def test_bool_is_not_a_metric(self):
        self.rejected(lambda m: m['evaluation']['metrics'].update(protected_missed=False), 'gate_metrics')

    def test_latency_is_rechecked(self):
        self.rejected(lambda m: m['evaluation'].update(warm_p95_ms=301), 'gate_latency')

    def test_source_test_split_hash_is_required(self):
        self.rejected(lambda m: m['evaluation'].update(split_sha256='unknown'), 'gate_split')

    def test_calibration_and_test_cannot_be_same_split(self):
        self.rejected(lambda m: m['evaluation'].update(split_sha256='1' * 64), 'uncalibrated')

    def test_actual_calibration_must_be_quantized(self):
        data = json.loads((self.root / 'calibration.json').read_text()); data['quantized'] = False
        (self.root / 'calibration.json').write_text(json.dumps(data))
        self.manifest['files']['calibration.json'] = hashlib.sha256((self.root / 'calibration.json').read_bytes()).hexdigest()
        self.save()
        with self.assertRaisesRegex(ValueError, 'uncalibrated'):
            load_manifest(self.root)

    def test_file_hash_is_rechecked(self):
        (self.root / 'model.onnx').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'manifest_hash'):
            load_manifest(self.root)

    def test_path_escape_is_rejected(self):
        self.rejected(lambda m: m['files'].update({'../outside': '0' * 64}), 'manifest_files')

    def test_windows_escape_is_rejected_on_all_platforms(self):
        self.rejected(lambda m: m['files'].update({'C:\\outside': '0' * 64}), 'manifest_files')

    def test_symlink_is_rejected(self):
        (self.root / 'model.onnx').unlink()
        (self.root / 'model.onnx').symlink_to(self.root / 'tokenizer.json')
        with self.assertRaisesRegex(ValueError, 'manifest_path'):
            load_manifest(self.root)

    def test_unlisted_file_is_rejected(self):
        (self.root / 'old-model.bin').write_bytes(b'old model')
        with self.assertRaisesRegex(ValueError, 'manifest_untracked_file'):
            load_manifest(self.root)

    def test_training_can_load_unreleased_model_explicitly(self):
        self.manifest['quality_gate'] = {'passed': False, 'reason': 'not_evaluated'}
        del self.manifest['evaluation']
        self.save()
        self.assertEqual(load_manifest(self.root, require_gate=False)['quality_gate']['passed'], False)


if __name__ == '__main__':
    unittest.main()
