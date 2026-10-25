"""Research-only per-tensor Gather compression of an existing MatMul-INT8 run.

Writes a new, explicitly unaccepted candidate; never edits the source package,
changes token IDs, or reads a test/stress split. Run the ordinary Windows CPU
benchmark and locked pipeline evaluation separately before any release decision.
Heavy imports occur only when executing an experiment, not on app/runtime paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import gc
import hashlib
import json
from pathlib import Path
import shutil
import time

from src.core.local_decision import load_manifest
from .pipeline import BASE_REPO, BASE_REV, SOURCES, fit_calibration, read_rows, sha, write

REQUIRED_FILES = frozenset({'model.onnx', 'tokenizer.json', 'policy.json', 'calibration.json'})
ALLOWED_FILES = REQUIRED_FILES | {'LICENSE', 'NOTICE', 'BASE_MODEL_CARD.md'}
EMBEDDING_SHAPE = (256000, 768)
DTYPES = {1: 'FLOAT', 2: 'UINT8', 3: 'INT8', 10: 'FLOAT16'}


def validate_paths(source_run, out, data):
    """Require a fresh sibling run and a manifest-authenticated resource allowlist."""
    source = Path(source_run).expanduser().resolve()
    package = source if (source/'manifest.json').is_file() else source/'package'
    target = Path(out).expanduser().absolute()
    dataset = Path(data).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise ValueError('candidate_output_exists_choose_new_run')
    target = target.resolve()
    if target.is_relative_to(source) or target.is_relative_to(dataset):
        raise ValueError('candidate_output_inside_source_or_data')
    manifest = load_manifest(package, verify=True, require_gate=False)
    if (manifest.get('base') != BASE_REPO or manifest.get('base_revision') != BASE_REV
            or manifest.get('source_revisions') != SOURCES):
        raise ValueError('candidate_source_provenance_mismatch')
    if set(manifest['files']) - ALLOWED_FILES:
        raise ValueError('candidate_source_file_not_allowlisted')
    calibration_path = dataset/'calibration.jsonl'
    if calibration_path.is_symlink() or not calibration_path.is_file():
        raise ValueError('candidate_calibration_path')
    split_hash = sha(calibration_path)
    calibration = json.loads((package/'calibration.json').read_text(encoding='utf-8'))
    if (calibration.get('quantized') is not True or calibration.get('split_sha256') != split_hash
            or calibration.get('fitted_on_independent_split') is not True):
        raise ValueError('candidate_calibration_split_mismatch')
    return source, package, target, dataset, manifest, split_hash


def tensor_digest(tensor):
    descriptor = getattr(tensor, 'DESCRIPTOR', None)
    if descriptor is not None and descriptor.name == 'TensorProto':
        # ORT's temporary external-data round trip writes DEFAULT explicitly on
        # reload, whereas newly quantized tensors often leave that default unset.
        # Normalize ONLY this equivalent storage-field presence; all names,
        # dtypes, dimensions, value bytes and other protobuf fields stay strict.
        if tensor.data_location != 0 or list(tensor.external_data):
            raise ValueError('candidate_external_tensor_not_supported')
        normalized = type(tensor)()
        normalized.CopyFrom(tensor)
        normalized.ClearField('data_location')
        tensor = normalized
    return hashlib.sha256(tensor.SerializeToString(deterministic=True)).hexdigest()


def graph_snapshot(model):
    """Small structural/hash snapshot, without constructing an inference session."""
    graph = model.graph
    tensors = {tensor.name: tensor for tensor in graph.initializer}
    if any(getattr(tensor, 'data_location', 0) != 0 or list(getattr(tensor, 'external_data', ()))
           for tensor in tensors.values()):
        raise ValueError('candidate_external_tensor_not_supported')
    embeddings = [(node, tensors[node.input[0]]) for node in graph.node
                  if node.op_type == 'Gather' and node.input and node.input[0] in tensors
                  and tuple(tensors[node.input[0]].dims) == EMBEDDING_SHAPE]
    if len(embeddings) != 1:
        raise ValueError('candidate_embedding_gather_not_unique')
    gather, embedding = embeddings[0]
    if not gather.name or embedding.data_type not in (1, 10):
        raise ValueError('candidate_source_embedding_type_or_node')
    matmuls = {node.name: tensor_digest(node) for node in graph.node if node.op_type == 'MatMulInteger'}
    if not matmuls or not any(tensor.data_type == 3 for tensor in tensors.values()):
        raise ValueError('candidate_source_not_matmul_int8')
    return {'embedding': {'name': embedding.name, 'node': gather.name,
                          'shape': list(embedding.dims), 'dtype': DTYPES[embedding.data_type],
                          'output': list(gather.output), 'index_inputs': list(gather.input[1:]),
                          'attributes': [tensor_digest(value) for value in gather.attribute],
                          'domain': getattr(gather, 'domain', ''), 'overload': getattr(gather, 'overload', '')},
            'other_nodes': [tensor_digest(node) for node in graph.node if node != gather],
            'other_initializers': {name: tensor_digest(tensor) for name, tensor in tensors.items()
                                   if name != embedding.name},
            'matmul_integer_nodes': matmuls,
            'inputs': [tensor_digest(value) for value in graph.input],
            'outputs': [tensor_digest(value) for value in graph.output]}


def validate_candidate_graph(model, source):
    graph = model.graph
    tensors = {tensor.name: tensor for tensor in graph.initializer}
    embedding = source['embedding']
    if embedding['name'] in tensors:
        raise ValueError('candidate_float_embedding_retained')
    for name, expected in source['other_initializers'].items():
        if name not in tensors or tensor_digest(tensors[name]) != expected:
            raise ValueError('candidate_nonembedding_initializer_changed:'+name)
    matmuls = {node.name: tensor_digest(node) for node in graph.node if node.op_type == 'MatMulInteger'}
    if matmuls != source['matmul_integer_nodes']:
        raise ValueError('candidate_matmul_integer_nodes_changed')
    if ([tensor_digest(value) for value in graph.input] != source['inputs']
            or [tensor_digest(value) for value in graph.output] != source['outputs']):
        raise ValueError('candidate_graph_io_changed')
    gathers = [node for node in graph.node if node.op_type == 'Gather' and node.name == embedding['node']]
    if len(gathers) != 1 or gathers[0].input[0] not in tensors:
        raise ValueError('candidate_quantized_gather_missing')
    gather = gathers[0]
    if (list(gather.input[1:]) != embedding['index_inputs']
            or [tensor_digest(value) for value in gather.attribute] != embedding['attributes']
            or getattr(gather, 'domain', '') != embedding['domain']
            or getattr(gather, 'overload', '') != embedding['overload']
            or len(gather.output) != len(embedding['output'])):
        raise ValueError('candidate_gather_index_or_attributes_changed')
    quantized = tensors[gather.input[0]]
    if tuple(quantized.dims) != EMBEDDING_SHAPE or quantized.data_type != 2:
        raise ValueError('candidate_embedding_not_per_tensor_uint8')
    dequant = [node for node in graph.node if node.op_type == 'DequantizeLinear'
               and node.input and node.input[0] in gather.output and list(node.output) == embedding['output']]
    if len(dequant) != 1 or len(dequant[0].input) != 3:
        raise ValueError('candidate_embedding_dequantization_missing')
    ordinary_nodes = [tensor_digest(node) for node in graph.node if node != gather and node != dequant[0]]
    # ORT re-sorts a graph after inserting the Gather's DequantizeLinear. Only
    # ordering may differ: compare full serialized nodes WITH multiplicities,
    # never a set/name-only comparison. run() additionally checks the resulting
    # complete ONNX graph, so invalid dependency order is still rejected.
    if Counter(ordinary_nodes) != Counter(source['other_nodes']):
        raise ValueError('candidate_nonembedding_nodes_changed')
    scale, zero = (tensors.get(name) for name in dequant[0].input[1:])
    if (scale is None or zero is None or tuple(scale.dims) or tuple(zero.dims)
            or scale.data_type not in (1, 10) or zero.data_type != 2
            or any(attribute.name == 'axis' for attribute in dequant[0].attribute)):
        raise ValueError('candidate_embedding_not_scalar_quantization')
    allowed = set(source['other_initializers']) | {quantized.name, scale.name, zero.name}
    if len(tensors) != len(graph.initializer) or set(tensors) != allowed:
        raise ValueError('candidate_unexpected_initializer')
    return {'dtype': DTYPES[quantized.data_type], 'shape': list(quantized.dims),
            'per_channel': False, 'scale_elements': 1, 'zero_point_elements': 1,
            'gather_node': gather.name, 'float_embedding_removed': True,
            'nonembedding_initializers_unchanged': True, 'matmul_integer_nodes_unchanged': True,
            'graph_io_unchanged': True, 'other_nodes_unchanged': True,
            'other_node_order_changed': ordinary_nodes != source['other_nodes']}


def candidate_manifest(source_manifest, package):
    # Do not carry an old evaluation/pass flag to a differently quantized graph.
    manifest = {key: copy.deepcopy(source_manifest[key]) for key in
                ('schema', 'actions', 'model_id', 'base', 'base_revision', 'source_revisions', 'calibration')}
    if 'training_provenance' in source_manifest:
        manifest['training_provenance'] = copy.deepcopy(source_manifest['training_provenance'])
    manifest['model_id'] += '-gather-u8-pt-v1'
    manifest['quality_gate'] = {'passed': False, 'reason': 'not_evaluated'}
    manifest['files'] = {name: sha(package/name) for name in source_manifest['files']}
    return manifest


def calibrate_candidate(package, data, manifest, split_hash):
    """Old calibration is authenticated before the research-only loader is called."""
    from src.core.local_decision_worker import DecisionModel
    runtime = DecisionModel(package, require_gate=False)
    rows = read_rows(data, 'calibration')
    if not rows or any(row.get('label') not in manifest['actions'] or not isinstance(row.get('state'), dict)
                       for row in rows):
        raise ValueError('candidate_calibration_rows')
    logits = [runtime.logits(row['state']) for row in rows]
    calibration = fit_calibration(rows, logits)
    calibration.update(split_sha256=split_hash, quantized=True)
    write(package/'calibration.json', calibration)
    manifest['files']['calibration.json'] = sha(package/'calibration.json')
    write(package/'manifest.json', manifest)
    return len(rows)


def run(args):
    source, package, out, data, manifest, split_hash = validate_paths(args.source_run, args.out, args.data)
    import onnx
    import onnxruntime as ort
    from onnxruntime.quantization import quantize_dynamic, QuantType
    started = time.monotonic()
    source_manifest_hash = sha(package/'manifest.json')
    model = onnx.load_model(str(package/'model.onnx'), load_external_data=False)
    snapshot = graph_snapshot(model)
    out.mkdir(parents=True, exist_ok=False)
    candidate = out/'package'
    candidate.mkdir()
    for name in sorted(manifest['files']):
        if name != 'model.onnx':
            shutil.copyfile(package/name, candidate/name)
    # Passing a ModelProto keeps ORT's temporary inferred files in its temporary
    # directory, not next to the authenticated source run. Only Gather is touched.
    quantize_dynamic(model, str(candidate/'model.onnx'), op_types_to_quantize=['Gather'],
                     nodes_to_quantize=[snapshot['embedding']['node']], weight_type=QuantType.QInt8,
                     per_channel=False, reduce_range=False)
    del model
    gc.collect()
    transformed = onnx.load_model(str(candidate/'model.onnx'), load_external_data=False)
    structure = validate_candidate_graph(transformed, snapshot)
    if (candidate/'model.onnx').stat().st_size >= (package/'model.onnx').stat().st_size:
        raise ValueError('candidate_model_not_smaller')
    onnx.checker.check_model(transformed)
    del transformed
    gc.collect()
    current_manifest = candidate_manifest(manifest, candidate)
    # The new model hash and still-valid OLD calibration hash are written before
    # DecisionModel verifies the package. Refit then updates calibration + hash.
    write(candidate/'manifest.json', current_manifest)
    load_manifest(candidate, verify=True, require_gate=False)
    records = calibrate_candidate(candidate, data, current_manifest, split_hash)
    if sha(data/'calibration.jsonl') != split_hash:
        raise ValueError('candidate_calibration_changed_during_run')
    if sha(package/'manifest.json') != source_manifest_hash or load_manifest(package, verify=True, require_gate=False) != manifest:
        raise ValueError('candidate_source_changed_during_run')
    load_manifest(candidate, verify=True, require_gate=False)
    for name in manifest['files']:
        if name not in ('model.onnx', 'calibration.json') and sha(candidate/name) != manifest['files'][name]:
            raise ValueError('candidate_resource_changed:'+name)
    report = {'schema': 'wxbot.embedding-quantization.v1', 'source_run': str(source),
              'source_manifest_sha256': source_manifest_hash, 'source_files': manifest['files'],
              'source_model_bytes': (package/'model.onnx').stat().st_size,
              'candidate_model_bytes': (candidate/'model.onnx').stat().st_size,
              'source_package_bytes': sum((package/name).stat().st_size for name in manifest['files']) + (package/'manifest.json').stat().st_size,
              'candidate_package_bytes': sum(file.stat().st_size for file in candidate.iterdir()),
              'candidate_files': current_manifest['files'], 'onnxruntime_version': ort.__version__,
              'quantization': 'Gather-only per-tensor UINT8; existing MatMul INT8 unchanged',
              'embedding': structure, 'source_embedding': snapshot['embedding'],
              'calibration_split_sha256': split_hash, 'calibration_records': records,
              'source_unchanged': True, 'seconds': time.monotonic()-started,
              'quality_gate': {'passed': False, 'reason': 'not_evaluated'}, 'release_accepted': False,
              'required_next': [
                  ['python', '-m', 'tools.decision_model.benchmark', '--model-dir', str(candidate), '--output', str(out/'windows-cpu-benchmark.json')],
                  ['python', '-m', 'tools.decision_model.pipeline', 'evaluate', '--out', str(out), '--data', str(data)]],
              'note': 'Research candidate only. No test or stress rows were read; calibration is not release acceptance.'}
    write(out/'embedding-quantization.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True, help='existing run containing package/ (or the package directory)')
    parser.add_argument('--out', required=True, help='new candidate run; must not already exist')
    parser.add_argument('--data', required=True, help='fixed corpus directory; only calibration.jsonl is read')
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
