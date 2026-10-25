"""Locked, AI-authored Chinese challenge cases; research-only, never a release gate.

Usage (dev ONNX environment, no cloud/Key/download):
  python -m tools.decision_model.eval_stress --model-dir CACHE/run-v3-fixed/package --out ART/STRESS_V1.json

The model is deliberately loaded with require_gate=False ONLY inside this research
tool. The application no longer runs or packages this local policy. This tool does not write
model packages, thresholds, calibration, training input or quality_gate fields.
The cases have now been reviewed for rule fixes: later runs are development
regressions, not a new blind test. Existing reports are never overwritten.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import statistics
import time

from src.core.conversation import merge_messages
from src.core.local_decision import ACTIONS
from src.core.local_decision_worker import DecisionModel
from tools.decision_model.research_decider import ResearchReplyDecider as ReplyDecider
from src.core.types import WxMessage

DATASET_SHA256 = '1a53ce91765c3cb3c1fb89624bf2c85cb71aac1989fd97fbbdba422390a48978'
DATASET = Path(__file__).with_name('stress-v1.jsonl')


def read_cases():
    raw = DATASET.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DATASET_SHA256:
        raise ValueError('locked_stress_dataset_changed; create a new version, do not retune v1')
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line]
    if len(rows) != 120 or len({row['session_id'] for row in rows}) != 120:
        raise ValueError('independent_sessions_required')
    for row in rows:
        if (row['label'] not in ACTIONS or not isinstance(row['protected'], bool)
                or not isinstance(row['state']['current'], str)
                or not isinstance(row['state']['recent'], list)
                or row['provenance']['human_labelled'] is not False):
            raise ValueError('invalid_stress_case')
    return rows


def summarize(rows, decisions, *, accepted=None):
    """Count exact three-way errors AND safety-only (reply vs withholding) errors."""
    if len(rows) != len(decisions) or not rows:
        raise ValueError('invalid_evaluation_length')
    if accepted is None:
        accepted = [True] * len(rows)
    if len(accepted) != len(rows) or any(not isinstance(value, bool) for value in accepted):
        raise ValueError('invalid_acceptance_length_or_type')
    confusion = {a: {b: 0 for b in ACTIONS} for a in ACTIONS}
    protected = protected_missed = silent = silent_wrong = wrong_withholding = 0
    reply_total = reply_missed = correct = 0
    for row, decision in zip(rows, decisions):
        action = decision['action']
        if action not in ACTIONS:
            raise ValueError('invalid_prediction')
        gold = row['label']
        confusion[gold][action] += 1
        correct += gold == action
        if row['protected']:
            protected += 1
            protected_missed += action != 'reply'
        if gold == 'reply':
            reply_total += 1
            reply_missed += action != 'reply'
        if action != 'reply':
            silent += 1
            silent_wrong += action != gold
            wrong_withholding += gold == 'reply'
    times = sorted(d.get('elapsed_ms', 0.) for d in decisions)
    return {
        'records': len(rows), 'accuracy_three_way': correct / len(rows),
        'protected': protected, 'protected_false_silence': protected_missed,
        'silent_accepted': silent, 'silent_wrong_three_way': silent_wrong,
        'silent_precision': 1 - silent_wrong / silent if silent else None,
        'withholding_precision_not_reply': 1 - wrong_withholding / silent if silent else None,
        'reply_records': reply_total, 'reply_missed': reply_missed,
        'reply_recall': 1 - reply_missed / reply_total if reply_total else None,
        'coverage': sum(accepted) / len(rows),
        'source_counts': dict(Counter(d.get('source', 'unknown') for d in decisions)),
        'measured_p50_ms': statistics.median(times),
        'measured_p95_ms': times[int(.95 * (len(times) - 1))],
        'confusion': confusion,
    }


class ResearchRunner:
    """Actual ONNX policy behind ReplyDecider; no gateway and no remote requests."""
    def __init__(self, model):
        self.model = model
        self.calls = 0
        self.accepted = 0

    async def decide(self, state, recent=()):
        self.calls += 1
        result = await asyncio.to_thread(self.model.decide, state)
        self.accepted += not result['abstain']
        return result


def message(row):
    text = row['state']['current']
    parts = text.split('\n') if row['slice'] == 'multi_turn' else [text]
    originals = [WxMessage(msg_id=f"{row['id']}:{i}", platform='onebot', chat_type='private',
        chat_id=row['session_id'], sender_id=row['session_id'], sender_name='stress-fixture',
        bot_id='fixture-bot', content=value) for i, value in enumerate(parts)]
    return merge_messages(originals) if len(originals) > 1 else originals[0]


async def evaluate(model_dir, output):
    output = Path(output)
    if output.exists():
        raise ValueError('report_exists; preserve the earlier observation and choose a new output path')
    rows = read_cases()
    repo = Path(__file__).resolve().parents[2]
    source_paths = ('tools/decision_model/research_decider.py', 'src/core/turn_taking.py',
        'src/core/conversation.py', 'src/core/local_decision_worker.py',
        'tools/decision_model/eval_stress.py')
    source_hashes = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in source_paths}
    started = time.monotonic()
    model = await asyncio.to_thread(DecisionModel, model_dir, require_gate=False)
    cold_ms = (time.monotonic() - started) * 1000
    await asyncio.to_thread(model.logits, {'current': '这是独立评估的预热输入'})
    runner = ResearchRunner(model)
    decider = ReplyDecider(gateway=None, local_runner=runner, mode='natural')
    raw = []
    argmax = []
    effective = []
    pipeline = []
    pipeline_attempts = []
    results = []
    for row in rows:
        before = time.monotonic()
        prediction = await asyncio.to_thread(model.decide, row['state'])
        elapsed = (time.monotonic() - before) * 1000
        raw.append({**prediction, 'elapsed_ms': elapsed})
        argmax.append({'action': prediction['action'], 'source': 'model_argmax', 'elapsed_ms': elapsed})
        effective.append({'action': 'reply' if prediction['abstain'] else prediction['action'],
            'source': 'conservative' if prediction['abstain'] else 'local', 'elapsed_ms': elapsed})
        calls = runner.calls
        before = time.monotonic()
        decision = await decider.decide(message(row), recent=[{'user': text} for text in row['state']['recent']])
        detail = {'action': decision.action, 'source': decision.source, 'reason': decision.reason,
            'elapsed_ms': (time.monotonic() - before) * 1000}
        pipeline.append(detail)
        pipeline_attempts.append(runner.calls > calls)
        results.append({'id': row['id'], 'session_id': row['session_id'], 'slice': row['slice'],
            'expected': row['label'], 'protected': row['protected'],
            'raw_model': raw[-1], 'model_with_conservative_abstention': effective[-1], 'pipeline': detail,
            'pipeline_called_local_model': runner.calls > calls})
    accepted = [not result['abstain'] for result in raw]
    summaries = {
        'raw_model_argmax_without_abstention': summarize(rows, argmax),
        'raw_model_calibrated_with_conservative_abstention': summarize(rows, effective, accepted=accepted),
        'actual_reply_decider_pipeline': summarize(rows, pipeline,
            accepted=[d['source'] != 'conservative' for d in pipeline]),
    }
    ambiguous_count = sum(pipeline_attempts)
    local_count = sum(d['source'] == 'local' for d in pipeline)
    summaries['actual_reply_decider_pipeline'].update({
        'ambiguous_model_calls': ambiguous_count, 'accepted_local_decisions': local_count,
        'ambiguous_local_coverage': local_count / ambiguous_count if ambiguous_count else None,
        'all_input_local_coverage': local_count / len(rows),
        'deterministic_rule_decisions': sum(d['source'] == 'rule' for d in pipeline),
    })
    per_slice = {}
    for name in sorted({row['slice'] for row in rows}):
        indexes = [i for i, row in enumerate(rows) if row['slice'] == name]
        cases = [rows[i] for i in indexes]
        per_slice[name] = {
            'raw_model_calibrated': summarize(cases, [effective[i] for i in indexes], accepted=[accepted[i] for i in indexes]),
            'actual_pipeline': summarize(cases, [pipeline[i] for i in indexes]),
        }
    report = {
        'schema': 'wxbot.decision-stress-evaluation.v1', 'dataset_sha256': DATASET_SHA256,
        'evaluation_usage': 'development_regression_after_rule_review_not_blind_test',
        'provenance': '120 individually AI-authored synthetic sessions; NOT human-labelled; NOT real-user logs',
        'limitations': ['Research-only challenge set created after model training, separate from train/calibration/test templates',
            'This fixed set has been used to review/fix reply-protection rules: this run is development regression, not held-out/blind evidence',
            'Small cohorts do not satisfy production acceptance sample minima; no quality_gate value is written',
            'No cloud review: abstention conservatively replies; cloud configuration and network behavior are not assessed',
            'Ordinary sharing labels encode intended fixture behavior, not universal human preference',
            'Measured latency is variable-length stress latency, not the independent 256-token platform benchmark'],
        'metrics_definition': {
            'silent_precision': 'Exact three-way correctness among immediate non-reply decisions; defer/context_only confusion counts wrong',
            'withholding_precision_not_reply': 'Among non-reply decisions, fraction whose gold label is not reply',
            'reply_recall': 'Gold reply messages that are actually replied to, including conservative fallback',
            'coverage_raw': 'Fraction accepted by the frozen model policy, before conservative fallback',
            'coverage_pipeline': 'Fraction decided by rules or accepted local model; excludes conservative fallbacks',
            'ambiguous_local_coverage': 'Actual accepted local-model decisions divided by messages that reached the local model'},
        'model': {'directory': str(Path(model_dir).resolve()), 'fingerprint': model.fingerprint,
            'release_gate_bypassed_for_research_only': True, 'cold_construction_ms': cold_ms,
            'platform': platform.platform()},
        'source_sha256': source_hashes,
        'sources_unchanged_during_evaluation': all(hashlib.sha256((repo / name).read_bytes()).hexdigest() == value
            for name, value in source_hashes.items()),
        'summary': summaries, 'slices': per_slice, 'results': results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Reopen the claimed report and dataset; accidental partial writes are errors.
    if json.loads(output.read_text(encoding='utf-8'))['dataset_sha256'] != DATASET_SHA256:
        raise ValueError('report_verification_failed')
    read_cases()
    print(json.dumps({'dataset_sha256': DATASET_SHA256, 'summary': summaries, 'report': str(output.resolve())}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    asyncio.run(evaluate(args.model_dir, args.out))


if __name__ == '__main__':
    main()
