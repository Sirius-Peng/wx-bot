"""Mock-only policy/candidate contracts; never run ONNX or claim performance."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.core.local_decision import ACTIONS, MANIFEST_SCHEMA, load_manifest
from src.core.local_decision_worker import DecisionModel
from tools.decision_model import tune_runtime


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FirstSessionPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'calibration.json').write_text(json.dumps({
            'temperature': 1., 'thresholds': {a: .9 for a in ACTIONS}, 'min_margin': .2,
        }))
        self.events = []
        events = self.events

        class Options:
            def __init__(self):
                events.append(('options',))

            def add_session_config_entry(self, key, value):
                events.append(('config', key, value))

        class Session:
            def __init__(self, path, *, sess_options, providers):
                events.append(('session', sess_options.intra_op_num_threads,
                               sess_options.inter_op_num_threads, providers))

            def get_inputs(self):
                return []

        self.modules = {
            'numpy': SimpleNamespace(),
            'onnxruntime': SimpleNamespace(SessionOptions=Options, InferenceSession=Session, __version__='1.23.2-mock'),
            'tokenizers': SimpleNamespace(Tokenizer=SimpleNamespace(from_file=lambda path: object())),
        }

    def model(self, policy):
        (self.root/'policy.json').write_text(json.dumps(policy))
        with patch.dict(sys.modules, self.modules), patch(
                'src.core.local_decision_worker.load_manifest', return_value={'files': {'policy.json': sha(self.root/'policy.json')}}):
            return DecisionModel(self.root, require_gate=False)

    def test_explicit_true_sets_ftz_before_first_session(self):
        model = self.model({'threads': 3, 'denormal_as_zero': True})
        self.assertEqual(model.policy['denormal_as_zero'], True)
        self.assertEqual(self.events, [('options',), ('config', 'session.set_denormal_as_zero', '1'),
                                       ('session', 3, 1, ['CPUExecutionProvider'])])

    def test_missing_and_false_leave_default_unchanged_without_config_entry(self):
        for policy in ({'threads': 2}, {'threads': 2, 'denormal_as_zero': False}):
            self.events.clear()
            self.model(policy)
            self.assertEqual(self.events, [('options',), ('session', 2, 1, ['CPUExecutionProvider'])])

    def test_nonboolean_denormal_policy_rejected_before_any_session(self):
        for value in (0, 1, 'true', 'false', None, [], {}):
            self.events.clear()
            with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:denormal_as_zero'):
                self.model({'denormal_as_zero': value})
            self.assertEqual(self.events, [])

    def test_existing_1_to_4_thread_policies_remain_compatible(self):
        for threads in (1, 2, 3, 4):
            self.events.clear()
            self.model({'threads': threads})
            self.assertEqual(self.events[-1][1], threads)

    def test_only_false_spinning_sets_zero_before_session_and_true_preserves_default(self):
        self.model({'threads': 2, 'intra_op_allow_spinning': False})
        self.assertEqual(self.events, [('options',), ('config', 'session.intra_op.allow_spinning', '0'),
                                       ('session', 2, 1, ['CPUExecutionProvider'])])
        self.events.clear()
        model = self.model({'threads': 2, 'intra_op_allow_spinning': True})
        self.assertEqual(self.events, [('options',), ('session', 2, 1, ['CPUExecutionProvider'])])
        self.assertEqual(model.onnxruntime_version, '1.23.2-mock')

    def test_spinning_nonboolean_including_explicit_null_rejected_before_session(self):
        for value in (0, 1, 'true', 'false', None, [], {}):
            self.events.clear()
            with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:intra_op_allow_spinning'):
                self.model({'intra_op_allow_spinning': value})
            self.assertEqual(self.events, [])

    def test_ftz_and_disable_spinning_are_both_set_before_first_session(self):
        self.model({'denormal_as_zero': True, 'intra_op_allow_spinning': False})
        self.assertEqual(self.events, [('options',), ('config', 'session.set_denormal_as_zero', '1'),
                                       ('config', 'session.intra_op.allow_spinning', '0'),
                                       ('session', 2, 1, ['CPUExecutionProvider'])])

    def test_affinity_is_opt_in_and_applied_before_ort_session(self):
        def affinity(enabled):
            self.events.append(('affinity', enabled))
            return {'enabled': enabled, 'applied': enabled, 'actual_mask': '0xff'}
        with patch('src.core.local_decision_worker.prefer_performance_cores', side_effect=affinity):
            model = self.model({'threads': 4, 'prefer_performance_cores': True})
        self.assertEqual(self.events, [('affinity', True), ('options',),
                                       ('session', 4, 1, ['CPUExecutionProvider'])])
        self.assertIs(model.runtime_cpu_affinity['applied'], True)

    def test_nonboolean_affinity_policy_rejected_before_session(self):
        for value in (0, 1, 'true', None, [], {}):
            self.events.clear()
            with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:prefer_performance_cores'):
                self.model({'prefer_performance_cores': value})
            self.assertEqual(self.events, [])


class RuntimeCandidate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source-run'
        self.package = self.source/'package'
        self.package.mkdir(parents=True)
        self.data = self.root/'data'
        self.data.mkdir()
        self.out = self.root/'new-candidate'
        # There are deliberately NO test/train/stress files in this fixture.
        # Reading anything but calibration would fail this successful mock run.
        self.rows = [{'id': 'cal:'+str(i), 'label': ACTIONS[i % 3],
                      'state': {'current': 'calibration fixture '+str(i), 'recent': []}}
                     for i in range(1998)]
        (self.data/'calibration.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in self.rows))
        self.split_hash = sha(self.data/'calibration.jsonl')
        for name, content in {
            'model.onnx': b'authenticated mock model - not executable',
            'tokenizer.json': b'{"mock_tokenizer":true}',
            'LICENSE': b'mock license',
            'NOTICE': b'mock notice',
            'BASE_MODEL_CARD.md': b'mock model card',
        }.items():
            (self.package/name).write_bytes(content)
        (self.package/'policy.json').write_text(json.dumps({
            'threads': 2, 'architecture': 'fixed-choice-v1', 'max_length': 384,
            'sentinel': 'all unrelated fields remain unchanged',
        }))
        (self.package/'calibration.json').write_text(json.dumps({
            'temperature': 1., 'bias': [0., 0., 0.], 'thresholds': {a: .9 for a in ACTIONS},
            'min_margin': .2, 'split_sha256': self.split_hash, 'quantized': True,
            'fitted_on_independent_split': True, 'fit_records': 1998,
        }))
        self.manifest = {
            'schema': MANIFEST_SCHEMA, 'actions': list(ACTIONS),
            'model_id': 'mock-decision-model', 'base': tune_runtime.BASE_REPO,
            'base_revision': tune_runtime.BASE_REV, 'source_revisions': tune_runtime.SOURCES,
            'calibration': {'fitted_on_independent_split': True},
            # Even a pre-existing claimed success MUST NOT transfer to the trial.
            'quality_gate': {'passed': True, 'checks': {'independent_split': True}},
            'evaluation': {'old_result': 'must not transfer'},
            'files': {},
        }
        self.refresh_manifest()
        self.observed_policies = []
        self.inferred_cases = []

    def refresh_manifest(self):
        self.manifest['files'] = {p.name: sha(p) for p in self.package.iterdir()
                                  if p.is_file() and p.name != 'manifest.json'}
        (self.package/'manifest.json').write_text(json.dumps(self.manifest))

    def hashes(self):
        return {p.name: sha(p) for p in self.package.iterdir() if p.is_file()}

    def args(self, **kwargs):
        values = {'source_run': str(self.source), 'out': str(self.out),
                  'data': str(self.data), 'threads': 4, 'denormal_as_zero': True}
        values.update(kwargs)
        return SimpleNamespace(**values)

    def execute(self, args=None):
        observed = self.observed_policies
        cases = self.inferred_cases

        class MockModel:
            def __init__(self, package, *, require_gate):
                self.package = Path(package)
                # Still exercise real file authentication before any fake logits.
                load_manifest(self.package, verify=True, require_gate=require_gate)
                self.policy = json.loads((self.package/'policy.json').read_text())
                observed.append(self.policy)
                self.onnxruntime_version = '1.23.2-mock'
                self.runtime_cpu_affinity = {'enabled': self.policy.get('prefer_performance_cores', False),
                                             'applied': False, 'reason': 'mock-no-native-calls'}
                self.session = SimpleNamespace(get_providers=lambda: ['CPUExecutionProvider'])

            def logits(self, state):
                cases.append(copy.deepcopy(state))
                return [1., 2., 3.]

        def fit(rows, logits):
            self.assertEqual(len(rows), 1998)
            self.assertEqual(len(logits), 1998)
            return {'temperature': 2., 'bias': [0., 0., 0.], 'min_margin': .2,
                    'thresholds': {a: .95 for a in ACTIONS},
                    'fitted_on_independent_split': True, 'fit_records': 1998}

        with patch('src.core.local_decision_worker.DecisionModel', MockModel), \
                patch('tools.decision_model.tune_runtime.fit_calibration', side_effect=fit), \
                contextlib.redirect_stdout(io.StringIO()):
            return tune_runtime.run(args or self.args())

    def test_new_policy_actually_used_for_all_calibration_cases_and_weights_unchanged(self):
        before = self.hashes()
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.hashes(), before)
        policy = self.observed_policies[0]
        self.assertEqual(policy['threads'], 4)
        self.assertIs(policy['denormal_as_zero'], True)
        self.assertEqual(policy['sentinel'], 'all unrelated fields remain unchanged')
        self.assertEqual(self.inferred_cases, [r['state'] for r in self.rows])
        for name in self.manifest['files']:
            if name not in ('policy.json', 'calibration.json'):
                self.assertEqual((self.out/'package'/name).read_bytes(), (self.package/name).read_bytes())
        manifest = load_manifest(self.out/'package', verify=True, require_gate=False)
        self.assertIs(manifest['quality_gate']['passed'], False)
        self.assertNotIn('evaluation', manifest)
        with self.assertRaisesRegex(ValueError, 'gate_failed'):
            load_manifest(self.out/'package')

    def test_reports_bind_source_policy_files_and_exact_calibration_case_ids(self):
        self.execute()
        report = json.loads((self.out/'runtime-tuning.json').read_text())
        logits = json.loads((self.out/'runtime-calibration-logits.json').read_text())
        self.assertEqual(report['source_manifest_sha256'], sha(self.package/'manifest.json'))
        self.assertEqual(report['source_files'], self.manifest['files'])
        self.assertEqual(report['calibration_split_sha256'], self.split_hash)
        self.assertEqual(report['calibration_records'], 1998)
        self.assertEqual(report['calibration_logits_sha256'], sha(self.out/'runtime-calibration-logits.json'))
        self.assertEqual(report['calibration_runtime_sha256'], sha(self.out/'runtime-calibration-environment.json'))
        self.assertEqual(logits['case_ids'], [r['id'] for r in self.rows])
        self.assertEqual(logits['policy_sha256'], report['candidate_files']['policy.json'])
        self.assertIs(report['release_accepted'], False)
        self.assertIs(report['quality_gate']['passed'], False)
        self.assertIn('No test/stress cases read, no speed measured', report['note'])

    def test_authenticated_training_provenance_preserved_without_aliasing_or_old_pass(self):
        provenance = {'training_json_sha256': 'a'*64, 'task_head_sha256': 'b'*64,
                      'splits': {'train': {'sha256': 'c'*64}, 'calibration': {'sha256': 'd'*64}}}
        self.manifest['training_provenance'] = copy.deepcopy(provenance)
        self.refresh_manifest()
        before = self.hashes()
        self.execute()
        candidate = load_manifest(self.out/'package', verify=True, require_gate=False)
        self.assertEqual(candidate['training_provenance'], provenance)
        self.assertEqual(self.hashes(), before)
        self.assertIs(candidate['quality_gate']['passed'], False)
        self.assertNotIn('evaluation', candidate)
        in_memory = tune_runtime.candidate_manifest(self.manifest, self.out/'package')
        in_memory['training_provenance']['splits']['train']['sha256'] = 'e'*64
        self.assertEqual(self.manifest['training_provenance'], provenance)

    def test_historical_model_without_training_provenance_does_not_gain_claim(self):
        self.assertNotIn('training_provenance', self.manifest)
        self.execute()
        candidate = load_manifest(self.out/'package', verify=True, require_gate=False)
        self.assertNotIn('training_provenance', candidate)
        self.assertIs(candidate['quality_gate']['passed'], False)

    def test_explicit_false_is_preserved_as_default_off_candidate(self):
        self.execute(self.args(threads=1, denormal_as_zero=False))
        self.assertIs(self.observed_policies[0]['denormal_as_zero'], False)
        self.assertEqual(self.observed_policies[0]['threads'], 1)

    def test_existing_output_is_not_touched(self):
        self.out.mkdir()
        (self.out/'mine').write_text('retain')
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, 'output_exists'):
            self.execute()
        self.assertEqual((self.out/'mine').read_text(), 'retain')
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.observed_policies, [])

    def test_output_cannot_be_nested_in_source_or_data(self):
        for target in (self.source/'inside', self.data/'inside'):
            with self.assertRaisesRegex(ValueError, 'inside_source_or_data'):
                self.execute(self.args(out=str(target)))
            self.assertFalse(target.exists())

    def test_corrupted_hash_is_rejected_before_output_or_inference(self):
        (self.package/'model.onnx').write_bytes(b'changed after manifest')
        with self.assertRaisesRegex(ValueError, 'manifest_hash'):
            self.execute()
        self.assertFalse(self.out.exists())
        self.assertEqual(self.observed_policies, [])

    def test_wrong_fixed_source_identity_is_rejected(self):
        self.manifest['base_revision'] = 'different-base'
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'source_provenance_mismatch'):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_even_authenticated_extra_resource_is_not_allowlisted(self):
        (self.package/'extra.bin').write_bytes(b'not a model resource')
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'not_allowlisted'):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_data_hash_must_match_original_calibration(self):
        path = self.data/'calibration.jsonl'
        path.write_bytes(path.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError, 'calibration_split_mismatch'):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_exact_1998_original_cases_required_not_a_new_subset(self):
        path = self.data/'calibration.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in self.rows[:-1]))
        calibration = json.loads((self.package/'calibration.json').read_text())
        calibration['split_sha256'] = sha(path)
        (self.package/'calibration.json').write_text(json.dumps(calibration))
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'calibration_records'):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_rejects_nonboolean_flag_without_creating_candidate(self):
        for value in (0, 1, 'true', None):
            with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:denormal_as_zero'):
                self.execute(self.args(denormal_as_zero=value))
        self.assertFalse(self.out.exists())

    def test_threads_are_strict_integers_in_1_to_4(self):
        for value in (0, 5, True, '2', 2.0):
            with self.assertRaisesRegex(ValueError, 'threads_must_be_1_to_4'):
                self.execute(self.args(threads=value))
        self.assertFalse(self.out.exists())

    def test_invalid_source_boolean_is_not_silently_repaired(self):
        policy = json.loads((self.package/'policy.json').read_text())
        policy['denormal_as_zero'] = 'false'
        (self.package/'policy.json').write_text(json.dumps(policy))
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:denormal_as_zero'):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_explicit_cli_spinning_false_is_used_for_new_calibration(self):
        self.execute(self.args(intra_op_allow_spinning=False))
        self.assertIs(self.observed_policies[0]['intra_op_allow_spinning'], False)
        report = json.loads((self.out/'runtime-tuning.json').read_text())
        self.assertIn('intra_op_allow_spinning', report['allowed_policy_changes'])
        self.assertIs(report['candidate_policy']['intra_op_allow_spinning'], False)

    def test_omitted_cli_spinning_preserves_source_value(self):
        policy = json.loads((self.package/'policy.json').read_text())
        policy['intra_op_allow_spinning'] = False
        (self.package/'policy.json').write_text(json.dumps(policy))
        self.refresh_manifest()
        self.execute(self.args(intra_op_allow_spinning=None))
        self.assertIs(self.observed_policies[0]['intra_op_allow_spinning'], False)

    def test_explicit_true_spinning_can_restore_ort_build_default(self):
        self.execute(self.args(intra_op_allow_spinning=True))
        self.assertIs(self.observed_policies[0]['intra_op_allow_spinning'], True)

    def test_invalid_spinning_argument_rejected_before_output(self):
        for value in (0, 1, 'true', [], {}):
            with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:intra_op_allow_spinning'):
                self.execute(self.args(intra_op_allow_spinning=value))
        self.assertFalse(self.out.exists())

    def test_invalid_source_spinning_is_rejected_before_output(self):
        policy = json.loads((self.package/'policy.json').read_text())
        policy['intra_op_allow_spinning'] = None
        (self.package/'policy.json').write_text(json.dumps(policy))
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:intra_op_allow_spinning'):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_cli_distinguishes_explicit_disable_enable_and_omitted(self):
        argv = ['--source-run', 'source', '--out', 'new', '--data', 'cal', '--threads', '2']
        for extra, expected in (([], None), (['--no-intra-op-allow-spinning'], False),
                                (['--intra-op-allow-spinning'], True)):
            with patch('tools.decision_model.tune_runtime.run', return_value=0) as run:
                self.assertEqual(tune_runtime.main(argv+extra), 0)
                self.assertIs(run.call_args.args[0].intra_op_allow_spinning, expected)

    def test_explicit_performance_cores_used_and_reported_without_model_changes(self):
        before = self.hashes()
        self.execute(self.args(prefer_performance_cores=True))
        self.assertEqual(self.hashes(), before)
        self.assertIs(self.observed_policies[0]['prefer_performance_cores'], True)
        report = json.loads((self.out/'runtime-tuning.json').read_text())
        self.assertIn('prefer_performance_cores', report['allowed_policy_changes'])
        runtime = json.loads((self.out/'runtime-calibration-environment.json').read_text())
        self.assertIs(runtime['runtime_policy']['prefer_performance_cores'], True)
        self.assertEqual(runtime['runtime_cpu_affinity']['reason'], 'mock-no-native-calls')

    def test_omitted_performance_cores_preserves_source_preference(self):
        policy = json.loads((self.package/'policy.json').read_text())
        policy['prefer_performance_cores'] = True
        (self.package/'policy.json').write_text(json.dumps(policy))
        self.refresh_manifest()
        self.execute()
        self.assertIs(self.observed_policies[0]['prefer_performance_cores'], True)

    def test_invalid_affinity_flag_or_authenticated_source_is_rejected(self):
        for value in (0, 1, 'true', [], {}):
            with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:prefer_performance_cores'):
                self.execute(self.args(prefer_performance_cores=value))
        policy = json.loads((self.package/'policy.json').read_text())
        policy['prefer_performance_cores'] = None
        (self.package/'policy.json').write_text(json.dumps(policy))
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'invalid_runtime_policy:prefer_performance_cores'):
            self.execute()
        self.assertFalse(self.out.exists())

    def test_cli_performance_cores_default_and_explicit_values(self):
        argv = ['--source-run', 'source', '--out', 'new', '--data', 'cal', '--threads', '4']
        for extra, expected in (([], None), (['--no-prefer-performance-cores'], False),
                                (['--prefer-performance-cores'], True)):
            with patch('tools.decision_model.tune_runtime.run', return_value=0) as run:
                self.assertEqual(tune_runtime.main(argv+extra), 0)
                self.assertIs(run.call_args.args[0].prefer_performance_cores, expected)


if __name__ == '__main__':
    unittest.main()
