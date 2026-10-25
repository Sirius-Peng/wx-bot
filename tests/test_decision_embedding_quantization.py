"""Mock/graph contracts only: no model is loaded or inferred on the developer Mac."""
import copy
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.local_decision import ACTIONS, MANIFEST_SCHEMA, load_manifest
from tools.decision_model import quantize_embeddings as q


class Proto:
    def __init__(self, **values):
        self.__dict__.update(values)

    def SerializeToString(self, deterministic=False):
        return json.dumps(self.__dict__, sort_keys=deterministic, default=lambda x: x.__dict__).encode()


class FakeTensorProto(Proto):
    DESCRIPTOR = types.SimpleNamespace(name='TensorProto')

    def __init__(self, **values):
        self.name = 'weight'
        self.data_type = 3
        self.dims = [2, 2]
        self.raw_data = [0, 1, 2, 3]
        self.data_location = 0
        self.external_data = []
        self._location_present = False
        self.__dict__.update(values)

    def CopyFrom(self, other):
        self.__dict__ = copy.deepcopy(other.__dict__)

    def ClearField(self, field):
        if field != 'data_location':
            raise AssertionError('only data_location may be normalized')
        self.data_location = 0
        self._location_present = False

    def SerializeToString(self, deterministic=False):
        values = self.__dict__.copy()
        if not values.pop('_location_present'):
            values.pop('data_location')
        return json.dumps(values, sort_keys=deterministic).encode()


def tensor(name, dims, dtype=1):
    return Proto(name=name, dims=list(dims), data_type=dtype, data_location=0, external_data=[])


def node(name, op, inputs, outputs):
    return Proto(name=name, op_type=op, input=list(inputs), output=list(outputs), attribute=[])


def graphs():
    embed = tensor('embedding', q.EMBEDDING_SHAPE)
    mm = tensor('mm_int8', [768, 768], 3)
    bias = tensor('bias', [768])
    gather = node('gather', 'Gather', ['embedding', 'input_ids'], ['embedded'])
    matmul = node('mm', 'MatMulInteger', ['hidden', 'mm_int8'], ['result'])
    io = dict(input=[Proto(name='input_ids')], output=[Proto(name='logits')])
    source = Proto(graph=Proto(initializer=[embed, mm, bias], node=[gather, matmul], **io))
    transformed = Proto(graph=Proto(initializer=[copy.deepcopy(mm), copy.deepcopy(bias),
                tensor('embedding_quantized', q.EMBEDDING_SHAPE, 2), tensor('scale', []), tensor('zp', [], 2)],
            node=[node('gather', 'Gather', ['embedding_quantized', 'input_ids'], ['embedded_quantized']),
                  node('dq', 'DequantizeLinear', ['embedded_quantized', 'scale', 'zp'], ['embedded']),
                  copy.deepcopy(matmul)], **copy.deepcopy(io)))
    return source, transformed


class EmbeddingQuantizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.source = self.root/'source'
        self.package = self.source/'package'
        self.package.mkdir(parents=True)
        self.data = self.root/'data'
        self.data.mkdir()
        (self.data/'calibration.jsonl').write_text(json.dumps({'id':'cal', 'label':'reply', 'state':{'current':'你好'}})+'\n', encoding='utf-8')
        self.split_hash = q.sha(self.data/'calibration.jsonl')
        self.oldcal = dict(temperature=1, thresholds={a: .85 for a in ACTIONS}, min_margin=.2,
                           fitted_on_independent_split=True, quantized=True, split_sha256=self.split_hash)
        files = {'model.onnx': b'original-model-uncompressed-fixture', 'tokenizer.json': b'unchanged-tokenizer',
                 'policy.json': b'unchanged-policy', 'calibration.json': json.dumps(self.oldcal).encode(),
                 'LICENSE': b'license'}
        for name, contents in files.items():
            (self.package/name).write_bytes(contents)
        self.manifest = dict(schema=MANIFEST_SCHEMA, actions=list(ACTIONS), model_id='fixture',
            base=q.BASE_REPO, base_revision=q.BASE_REV, source_revisions=q.SOURCES,
            calibration={'fitted_on_independent_split': True},
            quality_gate={'passed': True, 'reason': 'old-not-reusable'}, evaluation={'old': True},
            files={name:q.sha(self.package/name) for name in files})
        self.save_manifest()
        self.out = self.root/'new-candidate'

    def save_manifest(self):
        q.write(self.package/'manifest.json', self.manifest)

    def test_tensor_digest_normalizes_only_default_storage_presence(self):
        implicit = FakeTensorProto()
        explicit = FakeTensorProto(_location_present=True)
        self.assertNotEqual(implicit.SerializeToString(), explicit.SerializeToString())
        self.assertEqual(q.tensor_digest(implicit), q.tensor_digest(explicit))
        self.assertTrue(explicit._location_present, 'normalization must not mutate source')
        for changes in ({'raw_data':[1,1,2,3]}, {'data_type':2},
                        {'dims':[1,4]}, {'name':'another-weight'}, {'doc_string':'different'}):
            with self.subTest(changes=changes):
                changed = FakeTensorProto(**changes)
                self.assertNotEqual(q.tensor_digest(implicit), q.tensor_digest(changed))
        for changes in ({'data_location':1}, {'external_data':[{'location':'weights.data'}]}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, 'candidate_external_tensor_not_supported'):
                    q.tensor_digest(FakeTensorProto(**changes))

    def test_node_and_value_info_serialization_remains_strict(self):
        node_a = Proto(name='node', data_location=0)
        node_b = Proto(name='node')
        self.assertNotEqual(q.tensor_digest(node_a), q.tensor_digest(node_b))

    def test_real_micro_tensorproto_default_presence_when_onnx_available(self):
        try:
            import onnx
        except ImportError:
            self.skipTest('ONNX is a training-only dependency')
        implicit = onnx.helper.make_tensor('weight', onnx.TensorProto.INT8, [2,2], [0,1,2,3], raw=False)
        explicit = onnx.TensorProto()
        explicit.CopyFrom(implicit)
        explicit.data_location = onnx.TensorProto.DEFAULT
        self.assertFalse(implicit.HasField('data_location'))
        self.assertTrue(explicit.HasField('data_location'))
        self.assertEqual(q.tensor_digest(implicit), q.tensor_digest(explicit))
        raw = onnx.helper.make_tensor('weight', onnx.TensorProto.INT8, [2,2], bytes([0,1,2,3]), raw=True)
        changed = onnx.TensorProto()
        changed.CopyFrom(raw)
        changed.raw_data = bytes([1,1,2,3])
        self.assertNotEqual(q.tensor_digest(raw), q.tensor_digest(changed))
        for field, value in [('name','other'), ('data_type',onnx.TensorProto.UINT8)]:
            altered = onnx.TensorProto()
            altered.CopyFrom(raw)
            setattr(altered, field, value)
            self.assertNotEqual(q.tensor_digest(raw), q.tensor_digest(altered))
        altered = onnx.TensorProto()
        altered.CopyFrom(raw)
        altered.dims[:] = [1,4]
        self.assertNotEqual(q.tensor_digest(raw), q.tensor_digest(altered))
        altered.data_location = onnx.TensorProto.EXTERNAL
        with self.assertRaisesRegex(ValueError, 'candidate_external_tensor_not_supported'):
            q.tensor_digest(altered)
        altered.data_location = onnx.TensorProto.DEFAULT
        altered.external_data.add(key='location',value='weights.data')
        with self.assertRaisesRegex(ValueError, 'candidate_external_tensor_not_supported'):
            q.tensor_digest(altered)

    def test_source_hash_and_calibration_are_verified_before_output_created(self):
        q.validate_paths(self.source, self.out, self.data)
        (self.package/'tokenizer.json').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'manifest_hash'):
            q.validate_paths(self.source, self.out, self.data)
        self.assertFalse(self.out.exists())

    def test_new_run_is_required_and_never_nested_into_source_or_data(self):
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError, 'candidate_output_exists'):
            q.validate_paths(self.source, self.out, self.data)
        for target in (self.source/'nested', self.data/'nested'):
            with self.assertRaisesRegex(ValueError, 'candidate_output_inside'):
                q.validate_paths(self.source, target, self.data)

    def test_manifest_allowlist_rejects_extra_resource(self):
        (self.package/'unknown.bin').write_text('unknown user data')
        self.manifest['files']['unknown.bin'] = q.sha(self.package/'unknown.bin')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'candidate_source_file_not_allowlisted'):
            q.validate_paths(self.source, self.out, self.data)

    def test_wrong_calibration_or_base_rejected(self):
        (self.data/'calibration.jsonl').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'candidate_calibration_split_mismatch'):
            q.validate_paths(self.source, self.out, self.data)
        self.manifest['base_revision'] = 'other'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'candidate_source_provenance_mismatch'):
            q.validate_paths(self.source, self.out, self.data)

    def test_candidate_manifest_drops_old_evaluation_and_gate(self):
        self.manifest['training_provenance']={'history_complete':False,'split_sha256':{'train':'c'*64}}
        manifest = q.candidate_manifest(self.manifest, self.package)
        self.assertEqual(manifest['training_provenance'],self.manifest['training_provenance'])
        self.assertIsNot(manifest['training_provenance'],self.manifest['training_provenance'])
        self.assertNotIn('evaluation', manifest)
        self.assertEqual(manifest['quality_gate'], {'passed':False, 'reason':'not_evaluated'})
        self.assertEqual(manifest['files'], self.manifest['files'])
        self.assertEqual(self.manifest['quality_gate']['passed'], True)

    def test_uint8_single_tensor_structure_and_existing_matmul_are_verified(self):
        source, candidate = graphs()
        result = q.validate_candidate_graph(candidate, q.graph_snapshot(source))
        self.assertEqual(result['dtype'], 'UINT8')
        self.assertFalse(result['per_channel'])
        self.assertTrue(result['matmul_integer_nodes_unchanged'])
        self.assertEqual(result['scale_elements'], 1)

    def test_float_copy_per_channel_signed_or_changed_matmul_rejected(self):
        source, original = graphs()
        snapshot = q.graph_snapshot(source)
        cases = []
        candidate = copy.deepcopy(original)
        candidate.graph.initializer.append(copy.deepcopy(source.graph.initializer[0]))
        cases.append((candidate, 'candidate_float_embedding_retained'))
        candidate = copy.deepcopy(original)
        candidate.graph.initializer[-2].dims = [768]
        cases.append((candidate, 'candidate_embedding_not_scalar_quantization'))
        candidate = copy.deepcopy(original)
        candidate.graph.initializer[2].data_type = 3
        cases.append((candidate, 'candidate_embedding_not_per_tensor_uint8'))
        candidate = copy.deepcopy(original)
        candidate.graph.initializer[0].dims = [4, 4]
        cases.append((candidate, 'candidate_nonembedding_initializer_changed'))
        candidate = copy.deepcopy(original)
        candidate.graph.node[-1].input = ['new', 'mm_int8']
        cases.append((candidate, 'candidate_matmul_integer_nodes_changed'))
        candidate = copy.deepcopy(original)
        candidate.graph.input[0].name = 'remapped_ids'
        cases.append((candidate, 'candidate_graph_io_changed'))
        for candidate, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    q.validate_candidate_graph(candidate, snapshot)

    def test_token_indices_other_nodes_and_hidden_float_copy_cannot_be_certified(self):
        source, original = graphs()
        snapshot = q.graph_snapshot(source)
        candidate = copy.deepcopy(original)
        candidate.graph.node[0].input[1] = 'different_token_ids'
        with self.assertRaisesRegex(ValueError, 'candidate_gather_index_or_attributes_changed'):
            q.validate_candidate_graph(candidate, snapshot)
        candidate = copy.deepcopy(original)
        candidate.graph.node.insert(2, node('extra', 'Identity', ['embedded'], ['changed']))
        with self.assertRaisesRegex(ValueError, 'candidate_nonembedding_nodes_changed'):
            q.validate_candidate_graph(candidate, snapshot)
        candidate = copy.deepcopy(original)
        candidate.graph.initializer.append(tensor('renamed_float_embedding', q.EMBEDDING_SHAPE))
        with self.assertRaisesRegex(ValueError, 'candidate_unexpected_initializer'):
            q.validate_candidate_graph(candidate, snapshot)

    def test_ordinary_node_reordering_preserves_complete_serialized_multiset(self):
        source, candidate = graphs()
        first = node('independent-a', 'Identity', ['input_ids'], ['side_a'])
        second = node('independent-b', 'Identity', ['input_ids'], ['side_b'])
        source.graph.node.extend([first, second])
        candidate.graph.node.extend([copy.deepcopy(second), copy.deepcopy(first)])
        result = q.validate_candidate_graph(candidate, q.graph_snapshot(source))
        self.assertTrue(result['other_nodes_unchanged'])
        self.assertTrue(result['other_node_order_changed'])

    def test_node_multiplicity_and_all_proto_fields_remain_strict(self):
        source, original = graphs()
        extra = node('independent', 'Identity', ['input_ids'], ['side'])
        source.graph.node.append(extra)
        original.graph.node.append(copy.deepcopy(extra))
        snapshot = q.graph_snapshot(source)
        candidates = []
        candidate = copy.deepcopy(original)
        candidate.graph.node.append(copy.deepcopy(extra))
        candidates.append(candidate)  # Duplicate node, even though set of hashes is unchanged.
        candidate = copy.deepcopy(original)
        candidate.graph.node.pop()
        candidates.append(candidate)
        for key, value in [('name','renamed'), ('input',['other_ids']), ('output',['other_output']),
                           ('attribute',[Proto(name='changed',value=1)]),
                           ('domain','other.domain'), ('overload','another')]:
            candidate = copy.deepcopy(original)
            setattr(candidate.graph.node[-1],key,value)
            candidates.append(candidate)
        for index, candidate in enumerate(candidates):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError,'candidate_nonembedding_nodes_changed'):
                    q.validate_candidate_graph(candidate,snapshot)

    def test_real_micro_onnx_reordering_still_requires_topological_validity(self):
        try:
            import onnx
        except ImportError:
            self.skipTest('ONNX is a training-only dependency')
        helper = onnx.helper
        tp = onnx.TensorProto
        inputs = [helper.make_tensor_value_info('ids',tp.INT64,[1]),
                  helper.make_tensor_value_info('a',tp.UINT8,[1,2])]
        outputs = [helper.make_tensor_value_info('embedded',tp.FLOAT,[1,2]),
                   helper.make_tensor_value_info('result',tp.INT32,[1,2]),
                   helper.make_tensor_value_info('side',tp.UINT8,[1,2])]
        mm_weight = helper.make_tensor('mm_int8',tp.INT8,[2,2],[0,1,2,3])
        mm = helper.make_node('MatMulInteger',['a','mm_int8'],['result'],name='mm')
        side = helper.make_node('Identity',['a'],['side'],name='side')
        gather = helper.make_node('Gather',['embedding','ids'],['embedded'],name='gather')
        source = helper.make_model(helper.make_graph([gather,mm,side],'source',inputs,outputs,
            [helper.make_tensor('embedding',tp.FLOAT,[2,2],[0.,1.,2.,3.]),mm_weight]),
            opset_imports=[helper.make_opsetid('',18)])
        quant_gather = helper.make_node('Gather',['embedding_quantized','ids'],['embedded_q'],name='gather')
        dq = helper.make_node('DequantizeLinear',['embedded_q','scale','zp'],['embedded'],name='dq')
        candidate = helper.make_model(helper.make_graph([side,mm,quant_gather,dq],'candidate',inputs,outputs,
            [copy.deepcopy(mm_weight),helper.make_tensor('embedding_quantized',tp.UINT8,[2,2],[0,1,2,3]),
             helper.make_tensor('scale',tp.FLOAT,[],[1.]),helper.make_tensor('zp',tp.UINT8,[],[0])]),
            opset_imports=[helper.make_opsetid('',18)])
        with patch.object(q,'EMBEDDING_SHAPE',(2,2)):
            snapshot = q.graph_snapshot(source)
            result = q.validate_candidate_graph(candidate,snapshot)
            self.assertTrue(result['other_node_order_changed'])
            onnx.checker.check_model(candidate)
            invalid = copy.deepcopy(candidate)
            nodes = list(invalid.graph.node)
            del invalid.graph.node[:]
            invalid.graph.node.extend([nodes[3],nodes[0],nodes[1],nodes[2]])
            # The complete-node multiset is still exact, but run() additionally
            # calls this checker: DQ before its producer must never be accepted.
            q.validate_candidate_graph(invalid,snapshot)
            with self.assertRaises(onnx.checker.ValidationError):
                onnx.checker.check_model(invalid)

    def test_source_requires_unique_embedding_full_vocab_and_int8_matmul(self):
        for mutate, reason in [
            (lambda m: setattr(m.graph.initializer[0], 'dims', [100,768]), 'candidate_embedding_gather_not_unique'),
            (lambda m: setattr(m.graph.initializer[0], 'data_location', 1), 'candidate_external_tensor_not_supported'),
            (lambda m: setattr(m.graph.node[-1], 'op_type', 'MatMul'), 'candidate_source_not_matmul_int8')]:
            source, _ = graphs()
            mutate(source)
            with self.assertRaisesRegex(ValueError, reason):
                q.graph_snapshot(source)

    def test_recalibration_loader_sees_new_model_and_old_calibration_hash(self):
        target = self.root/'cal-only'
        shutil = __import__('shutil')
        shutil.copytree(self.package, target)
        (target/'model.onnx').write_bytes(b'new quantized model')
        manifest = q.candidate_manifest(self.manifest, target)
        q.write(target/'manifest.json', manifest)
        seen = []
        class Runtime:
            def __init__(self, package, require_gate):
                self.assertions = load_manifest(package, require_gate=require_gate)
                seen.append(self.assertions)
            def logits(self, state):
                return [1.,0.,0.]
        with patch('src.core.local_decision_worker.DecisionModel', Runtime), patch.object(q, 'fit_calibration', return_value={**self.oldcal,'temperature':2}):
            self.assertEqual(q.calibrate_candidate(target, self.data, manifest, self.split_hash), 1)
        self.assertEqual(seen[0]['files']['calibration.json'], self.manifest['files']['calibration.json'])
        self.assertNotEqual(seen[0]['files']['model.onnx'], self.manifest['files']['model.onnx'])
        self.assertNotEqual(manifest['files']['calibration.json'], self.manifest['files']['calibration.json'])
        self.assertFalse(manifest['quality_gate']['passed'])
        with self.assertRaisesRegex(ValueError, 'gate_failed'):
            load_manifest(target)

    def test_mock_experiment_copies_only_allowlist_and_keeps_gate_closed(self):
        source_graph, candidate_graph = graphs()
        calls = []
        def fake_quantize(model, target, **options):
            calls.append(options)
            Path(target).write_bytes(b'new quantized model')
        onnx = types.ModuleType('onnx')
        def fake_load_model(path, **kwargs):
            # Production resolves the source path (/var -> /private/var on macOS).
            # Select the authenticated fixture by its bytes, not a platform path
            # spelling or the order in which the suite created temporary dirs.
            contents = Path(path).read_bytes()
            if contents == b'original-model-uncompressed-fixture':
                return source_graph
            if contents == b'new quantized model':
                return candidate_graph
            raise AssertionError('unexpected mocked ONNX fixture: '+str(path))
        onnx.load_model = fake_load_model
        onnx.checker = types.SimpleNamespace(check_model=lambda _:None)
        ort = types.ModuleType('onnxruntime')
        ort.__version__ = 'test-1.23.2'
        quant = types.ModuleType('onnxruntime.quantization')
        quant.quantize_dynamic = fake_quantize
        quant.QuantType = types.SimpleNamespace(QInt8='QInt8')
        before = {p.name:p.read_bytes() for p in self.package.iterdir()}
        with patch.dict(sys.modules, {'onnx':onnx,'onnxruntime':ort,'onnxruntime.quantization':quant}), \
             patch.object(q, 'calibrate_candidate', return_value=1):
            with patch('builtins.print'):
                self.assertEqual(q.run(types.SimpleNamespace(source_run=self.source,out=self.out,data=self.data)), 0)
        self.assertEqual(calls, [dict(op_types_to_quantize=['Gather'], nodes_to_quantize=['gather'],
                        weight_type='QInt8',per_channel=False,reduce_range=False)])
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.package.iterdir()})
        report = json.loads((self.out/'embedding-quantization.json').read_text())
        self.assertEqual(report['embedding']['dtype'], 'UINT8')
        self.assertFalse(report['release_accepted'])
        self.assertEqual(report['calibration_split_sha256'], self.split_hash)
        self.assertFalse((self.data/'test.jsonl').exists())
        self.assertEqual(set(p.name for p in (self.out/'package').iterdir()), set(self.manifest['files'])|{'manifest.json'})
        with self.assertRaisesRegex(ValueError, 'gate_failed'):
            load_manifest(self.out/'package')

    def test_help_does_not_import_onnx_or_start_model(self):
        result = subprocess.run([sys.executable,'-m','tools.decision_model.quantize_embeddings','--help'],
                                text=True,capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--source-run', result.stdout)
        self.assertIn('--data', result.stdout)


if __name__ == '__main__':
    unittest.main()
