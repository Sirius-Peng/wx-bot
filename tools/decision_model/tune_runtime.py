"""Create an authenticated, UNACCEPTED runtime-policy research candidate.

Only a new policy and calibration may change. Model/tokenizer bytes are copied
unchanged, all package files are hash-verified, and the old evaluation/pass status
is discarded. Only the original 1998 calibration cases are read. Run each trial
in a fresh process: ORT's denormal setting is applied on the first session.

This command does not measure or claim a speedup, read test/stress cases, train,
download, or change any quality threshold. Evaluate actual Windows performance
and a genuinely independent locked acceptance set before considering release.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import platform
import shutil
import time

from src.core.local_decision import load_manifest
from .pipeline import BASE_REPO, BASE_REV, SOURCES, fit_calibration, read_rows, sha, write


REQUIRED_FILES = frozenset({'model.onnx', 'tokenizer.json', 'policy.json', 'calibration.json'})
ALLOWED_FILES = REQUIRED_FILES | {'LICENSE', 'NOTICE', 'BASE_MODEL_CARD.md'}
CALIBRATION_RECORDS = 1998


def validate_paths(source_run, out, data):
    source = Path(source_run).expanduser().resolve(strict=True)
    package = source if (source/'manifest.json').is_file() else source/'package'
    requested = Path(out).expanduser().absolute()
    dataset = Path(data).expanduser().resolve(strict=True)
    if requested.exists() or requested.is_symlink():
        raise ValueError('runtime_candidate_output_exists_choose_new_run')
    target = requested.resolve()
    if target.is_relative_to(source) or target.is_relative_to(dataset):
        raise ValueError('runtime_candidate_inside_source_or_data')
    manifest = load_manifest(package, verify=True, require_gate=False)
    if (manifest.get('base') != BASE_REPO or manifest.get('base_revision') != BASE_REV
            or manifest.get('source_revisions') != SOURCES):
        raise ValueError('runtime_candidate_source_provenance_mismatch')
    if set(manifest['files']) - ALLOWED_FILES:
        raise ValueError('runtime_candidate_source_file_not_allowlisted')
    source_policy = json.loads((package/'policy.json').read_text(encoding='utf-8'))
    if not isinstance(source_policy, dict):
        raise ValueError('runtime_candidate_source_policy_invalid')
    if not isinstance(source_policy.get('denormal_as_zero', False), bool):
        raise ValueError('invalid_runtime_policy:denormal_as_zero')
    if ('intra_op_allow_spinning' in source_policy
            and not isinstance(source_policy['intra_op_allow_spinning'], bool)):
        raise ValueError('invalid_runtime_policy:intra_op_allow_spinning')
    if not isinstance(source_policy.get('prefer_performance_cores', False), bool):
        raise ValueError('invalid_runtime_policy:prefer_performance_cores')
    calibration_path = dataset/'calibration.jsonl'
    if calibration_path.is_symlink() or not calibration_path.is_file():
        raise ValueError('runtime_candidate_calibration_path')
    split_hash = sha(calibration_path)
    old_calibration = json.loads((package/'calibration.json').read_text(encoding='utf-8'))
    if (old_calibration.get('quantized') is not True or old_calibration.get('split_sha256') != split_hash
            or old_calibration.get('fitted_on_independent_split') is not True):
        raise ValueError('runtime_candidate_calibration_split_mismatch')
    rows = read_rows(dataset, 'calibration')
    if (len(rows) != CALIBRATION_RECORDS or old_calibration.get('fit_records') != CALIBRATION_RECORDS
            or any(not isinstance(r, dict) or r.get('label') not in manifest['actions']
                   or not isinstance(r.get('state'), dict) or not isinstance(r.get('id'), str)
                   or not r['id'] for r in rows)
            or len({r['id'] for r in rows}) != len(rows)):
        raise ValueError('runtime_candidate_calibration_records')
    return source, package, target, dataset, manifest, source_policy, split_hash, rows


def candidate_manifest(source_manifest, candidate):
    # Deliberately whitelist metadata too: never inherit evaluation, latency or a
    # claimed accepted quality gate from a different runtime policy.
    manifest = {key: copy.deepcopy(source_manifest[key]) for key in
                ('schema', 'actions', 'model_id', 'base', 'base_revision', 'source_revisions', 'calibration')}
    # Runtime-only policy trials retain authenticated training attribution, but
    # missing historical provenance is never manufactured from a later dataset.
    if 'training_provenance' in source_manifest:
        manifest['training_provenance'] = copy.deepcopy(source_manifest['training_provenance'])
    if not isinstance(manifest['model_id'], str):
        raise ValueError('runtime_candidate_model_id')
    manifest['model_id'] += '-runtime-trial-v1'
    manifest['quality_gate'] = {'passed': False, 'reason': 'not_evaluated_after_runtime_policy_change'}
    manifest['files'] = {name: sha(candidate/name) for name in source_manifest['files']}
    return manifest


def calibrate_candidate(candidate, manifest, rows, split_hash):
    # Loader verifies candidate policy/model hashes before creating ORT's FIRST
    # session. This new policy is active for every calibration inference.
    from src.core.local_decision_worker import DecisionModel
    runtime = DecisionModel(candidate, require_gate=False)
    from .benchmark import runtime_metadata
    write(candidate.parent/'runtime-calibration-environment.json', {
        **runtime_metadata(runtime), 'providers': runtime.session.get_providers(),
        'platform': platform.platform(),
        'policy_sha256': manifest['files']['policy.json'],
    })
    logits = [runtime.logits(row['state']) for row in rows]
    calibration = fit_calibration(rows, logits)
    calibration.update(split_sha256=split_hash, quantized=True)
    write(candidate/'calibration.json', calibration)
    manifest['files']['calibration.json'] = sha(candidate/'calibration.json')
    write(candidate/'manifest.json', manifest)
    return logits


def run(args):
    threads = args.threads
    enabled = args.denormal_as_zero
    spinning = getattr(args, 'intra_op_allow_spinning', None)
    performance_cores = getattr(args, 'prefer_performance_cores', None)
    if isinstance(threads, bool) or not isinstance(threads, int) or threads not in (1, 2, 3, 4):
        raise ValueError('runtime_candidate_threads_must_be_1_to_4')
    if not isinstance(enabled, bool):
        raise ValueError('invalid_runtime_policy:denormal_as_zero')
    if spinning is not None and not isinstance(spinning, bool):
        raise ValueError('invalid_runtime_policy:intra_op_allow_spinning')
    if performance_cores is not None and not isinstance(performance_cores, bool):
        raise ValueError('invalid_runtime_policy:prefer_performance_cores')
    source, package, out, data, source_manifest, source_policy, split_hash, rows = validate_paths(
        args.source_run, args.out, args.data)
    started = time.monotonic()
    source_manifest_hash = sha(package/'manifest.json')
    new_policy = copy.deepcopy(source_policy)
    new_policy['threads'] = threads
    new_policy['denormal_as_zero'] = enabled
    if spinning is not None:
        new_policy['intra_op_allow_spinning'] = spinning
    if performance_cores is not None:
        new_policy['prefer_performance_cores'] = performance_cores
    out.mkdir(parents=True, exist_ok=False)
    candidate = out/'package'
    candidate.mkdir()
    # Only the manifest's authenticated flat resource allowlist is copied. No
    # external weights, alternate tokenizer, source logs or untracked file enters.
    for name in sorted(source_manifest['files']):
        shutil.copyfile(package/name, candidate/name)
    write(candidate/'policy.json', new_policy)
    manifest = candidate_manifest(source_manifest, candidate)
    write(candidate/'manifest.json', manifest)
    load_manifest(candidate, verify=True, require_gate=False)
    logits = calibrate_candidate(candidate, manifest, rows, split_hash)
    if sha(data/'calibration.jsonl') != split_hash:
        raise ValueError('runtime_candidate_calibration_changed_during_run')
    if (sha(package/'manifest.json') != source_manifest_hash
            or load_manifest(package, verify=True, require_gate=False) != source_manifest):
        raise ValueError('runtime_candidate_source_changed_during_run')
    load_manifest(candidate, verify=True, require_gate=False)
    for name, expected in source_manifest['files'].items():
        if name not in ('policy.json', 'calibration.json') and sha(candidate/name) != expected:
            raise ValueError('runtime_candidate_unchanged_resource_modified:'+name)
    effective_policy = json.loads((candidate/'policy.json').read_text(encoding='utf-8'))
    if effective_policy != new_policy:
        raise ValueError('runtime_candidate_policy_changed_during_run')
    write(out/'runtime-calibration-logits.json', {
        'schema': 'wxbot.runtime-calibration-logits.v1', 'split_sha256': split_hash,
        'policy_sha256': manifest['files']['policy.json'],
        'case_ids': [r['id'] for r in rows], 'logits': logits,
        'note': 'Calibration only, never test or stress. No release or speed claim.',
    })
    report = {
        'schema': 'wxbot.runtime-tuning.v1', 'source_run': str(source),
        'source_manifest_sha256': source_manifest_hash,
        'source_files': source_manifest['files'], 'candidate_files': manifest['files'],
        'source_policy': source_policy, 'candidate_policy': effective_policy,
        'source_policy_sha256': source_manifest['files']['policy.json'],
        'candidate_policy_sha256': manifest['files']['policy.json'],
        'allowed_policy_changes': ['threads', 'denormal_as_zero', 'intra_op_allow_spinning', 'prefer_performance_cores'],
        'model_bytes_unchanged': True, 'tokenizer_bytes_unchanged': True,
        'source_unchanged': True, 'platform': platform.platform(),
        'calibration_split_sha256': split_hash, 'calibration_records': len(rows),
        'calibration_label_counts': dict(Counter(r['label'] for r in rows)),
        'calibration_logits_sha256': sha(out/'runtime-calibration-logits.json'),
        'calibration_runtime_sha256': sha(out/'runtime-calibration-environment.json'),
        'seconds': time.monotonic()-started,
        'quality_gate': manifest['quality_gate'], 'release_accepted': False,
        'required_next': [
            ['python', '-m', 'tools.decision_model.benchmark', '--model-dir', str(candidate),
             '--output', str(out/'windows-cpu-benchmark.json')],
            ['python', '-m', 'tools.decision_model.pipeline', 'evaluate', '--out', str(out), '--data', str(data)],
        ],
        'note': 'UNACCEPTED runtime-policy research candidate. Fresh process required for each trial. '
                'No test/stress cases read, no speed measured, and calibration is not blind acceptance.',
    }
    write(out/'runtime-tuning.json', report)
    # Reopen the actual report and manifest; changing policy cannot carry an old
    # pass flag through either the package or the human-readable experiment log.
    actual = json.loads((out/'runtime-tuning.json').read_text(encoding='utf-8'))
    checked = load_manifest(candidate, verify=True, require_gate=False)
    if actual != report or checked['quality_gate']['passed'] is not False or 'evaluation' in checked:
        raise ValueError('runtime_candidate_report_verification_failed')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True, help='Authenticated existing run or package directory')
    parser.add_argument('--out', required=True, help='NEW candidate run, never overwrite a source or earlier trial')
    parser.add_argument('--data', required=True, help='Only the original 1998 calibration rows are read')
    parser.add_argument('--threads', type=int, choices=(1, 2, 3, 4), required=True)
    parser.add_argument('--denormal-as-zero', action='store_true', help='Explicitly enable first-session ORT FTZ; default remains false')
    parser.add_argument('--intra-op-allow-spinning', action=argparse.BooleanOptionalAction, default=None,
                        help='False explicitly disables ORT intra-op spinning; True uses the build default. Omitted preserves source policy.')
    parser.add_argument('--prefer-performance-cores', action=argparse.BooleanOptionalAction, default=None,
                        help='Opt-in Windows worker-only highest-performance-core affinity; omitted preserves source policy.')
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
