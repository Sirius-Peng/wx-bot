"""CPU ONNX worker; no torch, transformers, network client or model auto-download."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from .local_decision import ACTIONS, load_manifest
from .local_decision_cpu import prefer_performance_cores


def state_text(state):
    current = state.get('current', state.get('text', state.get('content', '')))
    if not isinstance(current, str):
        raise ValueError('invalid_input')
    recent = state.get('recent') or []
    if not isinstance(recent, (list, tuple)):
        raise ValueError('invalid_input')
    previous = [x if isinstance(x, str) else str(x.get('user', '')) for x in recent[-3:] if isinstance(x, (str, dict))]
    # Preserve the full current input. Oversized token sequences abstain, never truncate its request.
    return '最近用户原话：' + '\n'.join(x[-160:] for x in previous) + '\n当前整段：' + current


def encode_sequence(tokenizer, state, policy, *, order=None):
    mask = policy['special_tokens']['mask']
    cls = policy['special_tokens']['cls']
    sep = policy['special_tokens']['sep']
    mask_text = policy.get('mask_text', '[MASK]')
    order = list(range(3)) if order is None else list(order)
    def tokens(text):
        return tokenizer.encode(str(text).replace(mask_text, ' '), add_special_tokens=False).ids
    if policy.get('architecture')=='fixed-choice-v1':
        ids=[cls]+tokens(state_text(state))+[sep]
        if len(ids)>policy.get('max_length',384):raise ValueError('input_too_large')
        return ids,[0,0,0]
    ids = [cls] + tokens('choice question: ' + policy['instructions']) + [sep]
    markers = []
    for i in order:
        markers.append(len(ids))
        ids += [mask] + tokens(' ' + policy['options'][ACTIONS[i]])
    ids += [sep] + tokens(state_text(state)) + [sep]
    if len(ids) > policy.get('max_length', 384):
        raise ValueError('input_too_large')
    return ids, markers


def calibrated_probabilities(logits, calibration):
    if not isinstance(logits,(tuple,list)) or len(logits)!=3:raise ValueError('invalid_logits')
    temperature = float(calibration['temperature'])
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError('uncalibrated')
    biases = calibration.get('bias', [0., 0., 0.])
    if not isinstance(biases,(tuple,list)) or len(biases)!=3:raise ValueError('uncalibrated')
    values = [float(x) / temperature + float(b) for x, b in zip(logits, biases)]
    if len(values) != 3 or not all(math.isfinite(x) for x in values):
        raise ValueError('invalid_logits')
    maximum = max(values)
    exponent = [math.exp(x - maximum) for x in values]
    total = sum(exponent)
    return {name: value / total for name, value in zip(ACTIONS, exponent)}


class DecisionModel:
    def __init__(self, model_dir, *, require_gate=True):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        self.onnxruntime_version = getattr(ort, '__version__', None)
        self.root = Path(model_dir)
        manifest = load_manifest(self.root, require_gate=require_gate)
        self.fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.policy = json.loads((self.root / 'policy.json').read_text(encoding='utf-8'))
        denormal_as_zero = self.policy.get('denormal_as_zero', False)
        if not isinstance(denormal_as_zero, bool):
            raise ValueError('invalid_runtime_policy:denormal_as_zero')
        if ('intra_op_allow_spinning' in self.policy
                and not isinstance(self.policy['intra_op_allow_spinning'], bool)):
            raise ValueError('invalid_runtime_policy:intra_op_allow_spinning')
        performance_cores = self.policy.get('prefer_performance_cores', False)
        if not isinstance(performance_cores, bool):
            raise ValueError('invalid_runtime_policy:prefer_performance_cores')
        self.calibration = json.loads((self.root / 'calibration.json').read_text(encoding='utf-8'))
        calibrated_probabilities([0.,0.,0.],self.calibration)
        thresholds=self.calibration.get('thresholds',{})
        if not isinstance(thresholds,dict) or set(thresholds)!=set(ACTIONS):raise ValueError('uncalibrated')
        for value in [*thresholds.values(),self.calibration.get('min_margin',.2)]:
            if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or not 0<=value<=1.000001:raise ValueError('uncalibrated')
        self.runtime_cpu_affinity = prefer_performance_cores(performance_cores)
        self.tokenizer = Tokenizer.from_file(str(self.root / 'tokenizer.json'))
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, max(1, int(self.policy.get('threads', 2))))
        options.inter_op_num_threads = 1
        # ORT applies this process-wide setting when creating its first session.
        # Only an explicitly enabled, hash-authenticated policy may change it.
        if denormal_as_zero:
            options.add_session_config_entry('session.set_denormal_as_zero', '1')
        # ORT v1.23.2 supports this key. Its default depends on the ORT build,
        # so missing/True preserve that default; only False explicitly disables.
        if self.policy.get('intra_op_allow_spinning') is False:
            options.add_session_config_entry('session.intra_op.allow_spinning', '0')
        self.session = ort.InferenceSession(str(self.root / 'model.onnx'), sess_options=options,
                                           providers=['CPUExecutionProvider'])
        self._inputs = {v.name for v in self.session.get_inputs()}

    def logits(self, state, order=None):
        ids, markers = encode_sequence(self.tokenizer, state, self.policy, order=order)
        np = self.np
        inputs = {'input_ids': np.asarray([ids], dtype=np.int64),
                  'attention_mask': np.ones((1, len(ids)), dtype=np.int64),
                  'marker_pos': np.asarray([markers], dtype=np.int64),
                  'marker_mask': np.ones((1, 3), dtype=bool),
                  'qtype': np.zeros((1,), dtype=np.int64)}
        out = self.session.run(None, {k: v for k, v in inputs.items() if k in self._inputs})[0][0].tolist()
        if order is not None and self.policy.get('architecture')!='fixed-choice-v1':
            restored = [0.] * 3
            for i, original in enumerate(order):
                restored[original] = out[i]
            out = restored
        return out

    def decide(self, state):
        started = time.monotonic()
        probabilities = calibrated_probabilities(self.logits(state), self.calibration)
        action = max(probabilities, key=probabilities.get)
        values = sorted(probabilities.values(), reverse=True)
        threshold = float(self.calibration.get('thresholds', {}).get(action, 1.0))
        abstain = values[0] < threshold or values[0] - values[1] < self.calibration.get('min_margin', .2)
        return {'action': action, 'probabilities': probabilities, 'calibrated': True,
                'abstain': abstain, 'reason': 'low_confidence' if abstain else 'accepted',
                'model_fingerprint': self.fingerprint, 'source': 'local',
                'elapsed_ms': (time.monotonic() - started) * 1000}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-dir', required=True)
    args = parser.parse_args(argv)
    try:
        model = DecisionModel(args.model_dir)
        model.logits({'current': '你好'})
        print(json.dumps({'status': 'ready'}), flush=True)
    except Exception as error:
        reason = 'dependencies_missing' if isinstance(error, ImportError) else 'worker_failed'
        print(json.dumps({'status': 'unavailable', 'reason': reason}), flush=True)
        return 2
    for line in sys.stdin.buffer:
        request_id = None
        try:
            if len(line) > 32000:
                raise ValueError('input_too_large')
            request = json.loads(line)
            request_id = request['id']
            result = model.decide(request['state'])
        except Exception as error:
            result = {'action': None, 'calibrated': False, 'abstain': True,
                      'probabilities': {}, 'reason': str(error) if isinstance(error, ValueError) else 'inference_failed'}
        print(json.dumps({'id': request_id, **result}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
