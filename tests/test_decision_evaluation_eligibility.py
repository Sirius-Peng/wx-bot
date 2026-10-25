"""Gate wiring with a fake model; no quality claim or training invocation."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tools.decision_model import pipeline


class FakeModel:
    def __init__(self, *args, **kwargs):
        self.calibration = json.loads((Path(args[0])/'calibration.json').read_text())

    def logits(self, state, order=None):
        label = state['current'].split('|')[0]
        return [10. if a == label else 0. for a in pipeline.ACTIONS]


class EvaluationEligibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'corpus'
        self.data.mkdir()
        self.run = self.root / 'run'
        (self.run / 'package').mkdir(parents=True)
        (self.run / 'package/manifest.json').write_text('{}')
        self.dataset = {
            'release_acceptance_eligible': True,
            'evaluation_role': 'locked_blind_acceptance',
            'frozen_before_training': True, 'split_unit': 'independent_session',
            'provenance': 'FAKE model-contract unit test; not an actual acceptance dataset',
            'splits': {},
        }
        for split in ('train', 'calibration', 'test'):
            rows = [{'id': f'{split}:{label}:{i}', 'session_id': f'{split}:{i}',
                     'label': label, 'protected': label == 'reply',
                     'state': {'current': f'{label}|{split}|{i}', 'recent': []}}
                    for label in pipeline.ACTIONS for i in range(120)]
            self.save_split(split, rows)
        self.dataset['splits']['test'].update(role='locked_blind_acceptance', used_for_development=False)
        calibration={'temperature':1.,'thresholds':{a:.85 for a in pipeline.ACTIONS},'min_margin':.2,
                     'split_sha256':self.dataset['splits']['calibration']['sha256']}
        (self.run/'package/calibration.json').write_text(json.dumps(calibration))
        manifest={'training_provenance':{
            'schema':'wxbot.decision-training-provenance.v1','training_metadata_sha256':'a'*64,
            'checkpoint_sha256':'b'*64,'history_complete':True,
            'split_sha256':{sp:self.dataset['splits'][sp]['sha256'] for sp in ('train','calibration')}}}
        (self.run/'package/manifest.json').write_text(json.dumps(manifest))

    def save_split(self, split, rows):
        raw = ''.join(json.dumps(row) + '\n' for row in rows).encode()
        (self.data / (split + '.jsonl')).write_bytes(raw)
        self.dataset['splits'][split] = {
            'records': len(rows), 'sha256': hashlib.sha256(raw).hexdigest(),
            'session_ids': sorted({row['session_id'] for row in rows if row.get('session_id')}),
        }

    def read_split(self, split):
        return [json.loads(line) for line in (self.data / (split + '.jsonl')).read_text().splitlines()]

    def evaluate(self):
        (self.data / 'dataset.json').write_text(json.dumps(self.dataset))
        args = SimpleNamespace(data=str(self.data), out=str(self.run))
        with patch('src.core.local_decision_worker.DecisionModel', FakeModel), \
                patch('tools.decision_model.benchmark.benchmark', return_value={
                    'benchmarks': [{'input_tokens': 256, 'p95_ms': 1.}]}), contextlib.redirect_stdout(io.StringIO()):
            result = pipeline.evaluate(args)
        report = json.loads((self.run / 'evaluation.json').read_text())
        manifest = json.loads((self.run / 'package/manifest.json').read_text())
        self.assertEqual(report['quality_gate'], manifest['quality_gate'])
        return result, report

    def test_valid_attestation_and_actual_disjoint_sessions_can_clear_independence_wiring(self):
        code, report = self.evaluate()
        self.assertEqual(code, 0)
        self.assertTrue(report['release_acceptance_eligible'])
        self.assertTrue(report['quality_gate']['checks']['independent_split'])
        self.assertEqual(report['acceptance_ineligibility_reasons'], [])
        self.assertIn('does not imply human annotation', report['provenance'])

    def test_old_disjoint_family_development_data_cannot_pass_even_with_perfect_fake_model(self):
        self.dataset.pop('release_acceptance_eligible')
        self.dataset.pop('evaluation_role')
        self.dataset['split_unit'] = 'disjoint utterance family; shared entities allowed'
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertFalse(report['quality_gate']['checks']['independent_split'])
        self.assertEqual(report['metrics']['accuracy'], 1)
        self.assertIn('NOT blind release acceptance', report['provenance'])

    def test_truthy_flag_or_narrative_claim_cannot_replace_exact_declarations(self):
        self.dataset['release_acceptance_eligible'] = 'true'
        self.dataset['split_unit'] = 'independent according to the author'
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('release_acceptance_not_declared', report['acceptance_ineligibility_reasons'])
        self.assertIn('split_unit_not_independent_session', report['acceptance_ineligibility_reasons'])

    def test_reused_development_test_never_becomes_blind_by_set_flag(self):
        self.dataset['splits']['test']['used_for_development'] = True
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('test_not_unused_locked_acceptance', report['acceptance_ineligibility_reasons'])

    def test_same_session_in_two_splits_blocks_independence(self):
        rows = self.read_split('train')
        rows[0]['session_id'] = 'test:0'
        self.save_split('train', rows)
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('train_test_session_overlap', report['acceptance_ineligibility_reasons'])

    def test_retagged_duplicate_state_cannot_manufacture_new_conversation(self):
        rows = self.read_split('train')
        rows[0]['state'] = self.read_split('test')[0]['state']
        self.save_split('train', rows)
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('train_test_state_overlap', report['acceptance_ineligibility_reasons'])

    def test_declared_session_list_must_equal_actual_sessions_not_just_be_disjoint(self):
        self.dataset['splits']['train']['session_ids'] = ['invented-session']
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('train_invalid_or_unbound_sessions', report['acceptance_ineligibility_reasons'])

    def test_actual_missing_session_ids_block_independence(self):
        rows = self.read_split('calibration')
        rows[0].pop('session_id')
        self.save_split('calibration', rows)
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('calibration_invalid_or_unbound_sessions', report['acceptance_ineligibility_reasons'])

    def test_byte_hash_mismatch_blocks_independence_even_when_rows_parse(self):
        path = self.data / 'test.jsonl'
        path.write_bytes(path.read_bytes() + b'\n')
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('test_hash_or_record_mismatch', report['acceptance_ineligibility_reasons'])

    def test_actual_calibration_cannot_be_replaced_by_declared_clean_split(self):
        path=self.run/'package/calibration.json'
        calibration=json.loads(path.read_text())
        calibration['split_sha256']=self.dataset['splits']['test']['sha256']
        path.write_text(json.dumps(calibration))
        code,report=self.evaluate()
        self.assertEqual(code,2)
        self.assertIn('actual_calibration_split_mismatch',report['acceptance_ineligibility_reasons'])

    def test_missing_or_different_training_provenance_blocks_independence(self):
        path=self.run/'package/manifest.json'
        original=json.loads(path.read_text())
        for patch_value in ({}, {'split_sha256':{'train':'c'*64,'calibration':'d'*64}}, {'history_complete':False}):
            with self.subTest(patch_value=patch_value):
                changed=json.loads(json.dumps(original))
                if not patch_value:changed.pop('training_provenance')
                else:changed['training_provenance'].update(patch_value)
                path.write_text(json.dumps(changed))
                code,report=self.evaluate()
                self.assertEqual(code,2)
                self.assertFalse(report['release_acceptance_eligible'])

    def test_not_frozen_before_training_blocks_independence(self):
        self.dataset['frozen_before_training'] = False
        code, report = self.evaluate()
        self.assertEqual(code, 2)
        self.assertIn('not_frozen_before_training', report['acceptance_ineligibility_reasons'])


if __name__ == '__main__':
    unittest.main()
