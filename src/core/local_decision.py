"""Bounded offline decision process. This module deliberately imports no ML runtime."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import math
import re
from pathlib import Path
import sys
import time

ACTIONS = ('reply', 'defer', 'context_only')
MANIFEST_SCHEMA = 'wxbot.local-decision.v1'


def resolve_model_dir(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get('WXBOT_LOCAL_DECISION_DIR'):
        return Path(os.environ['WXBOT_LOCAL_DECISION_DIR']).expanduser().resolve()
    roots = [Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)),
             Path(sys.executable).parent, Path(__file__).resolve().parents[2]]
    for root in roots:
        candidate = root / 'resources' / 'decision-model'
        if (candidate / 'manifest.json').is_file():
            return candidate
        candidate = root / 'decision-model'
        if (candidate / 'manifest.json').is_file():
            return candidate
    return roots[-1] / 'resources' / 'decision-model'


def _validate_quality_gate(manifest):
    """Reject edited pass flags, empty cohorts and metrics below the release contract."""
    gate = manifest.get('quality_gate')
    if not isinstance(gate, dict) or gate.get('passed') is not True:
        raise ValueError('gate_failed')
    checks = gate.get('checks')
    if (not isinstance(checks, dict) or not checks or any(v is not True for v in checks.values())
            or checks.get('independent_split') is not True):
        raise ValueError('gate_checks')
    evaluation = manifest.get('evaluation')
    if (not isinstance(evaluation, dict)
            or json.dumps(evaluation.get('quality_gate'), sort_keys=True) != json.dumps(gate, sort_keys=True)):
        raise ValueError('gate_evaluation')
    metrics = evaluation.get('metrics')
    if not isinstance(metrics, dict):
        raise ValueError('gate_metrics')
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    requirements = {
        'protected': lambda value: number(value) and value >= 100,
        'protected_missed': lambda value: number(value) and value == 0,
        'silent_accepted': lambda value: number(value) and value >= 100,
        'silent_precision': lambda value: number(value) and .99 <= value <= 1,
        'reply_records': lambda value: number(value) and value >= 100,
        'reply_recall': lambda value: number(value) and .995 <= value <= 1,
        'coverage': lambda value: number(value) and .5 <= value <= 1,
    }
    if any(not check(metrics.get(name)) for name, check in requirements.items()):
        raise ValueError('gate_metrics')
    latency = evaluation.get('warm_p95_ms')
    if not number(latency) or not 0 < latency <= 300:
        raise ValueError('gate_latency')
    if not isinstance(evaluation.get('split_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', evaluation['split_sha256']):
        raise ValueError('gate_split')


def load_manifest(folder, *, verify=True, require_gate=True):
    root = Path(folder).resolve()
    if (root / 'manifest.json').is_symlink():
        raise ValueError('manifest_path')
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema') != MANIFEST_SCHEMA or manifest.get('actions') != list(ACTIONS):
        raise ValueError('manifest_schema')
    calibration = manifest.get('calibration')
    if not isinstance(calibration, dict) or calibration.get('fitted_on_independent_split') is not True:
        raise ValueError('uncalibrated')
    if require_gate:
        _validate_quality_gate(manifest)
    files = manifest.get('files')
    if not isinstance(files, dict) or not {'model.onnx', 'tokenizer.json', 'policy.json', 'calibration.json'} <= set(files):
        raise ValueError('manifest_files')
    for relative, expected in files.items():
        if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative or '\0' in relative
                or any(p in ('', '.', '..') for p in relative.split('/'))
                or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
            raise ValueError('manifest_files')
        original = root / relative
        path = original.resolve()
        if (not path.is_relative_to(root) or not path.is_file() or original.is_symlink()
                or any(p.is_symlink() for p in original.parents if p != root and p.is_relative_to(root))
                or path.stat().st_size == 0):
            raise ValueError('manifest_path')
        if verify:
            sha = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    sha.update(chunk)
            if sha.hexdigest() != expected:
                raise ValueError('manifest_hash')
    if verify:
        for path in root.rglob('*'):
            if path.is_symlink() or (path.is_file() and path != root / 'manifest.json' and path.relative_to(root).as_posix() not in files):
                raise ValueError('manifest_untracked_file')
    if require_gate:
        calibration = json.loads((root / 'calibration.json').read_text(encoding='utf-8'))
        if (not isinstance(calibration, dict) or calibration.get('fitted_on_independent_split') is not True
                or calibration.get('quantized') is not True
                or not isinstance(calibration.get('split_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', calibration['split_sha256'])
                or calibration['split_sha256'] == manifest['evaluation']['split_sha256']):
            raise ValueError('uncalibrated')
    return manifest


class LocalDecisionRunner:
    """One warm process; deadline includes queueing. No download, retries or chat logging."""
    def __init__(self, model_dir=None, deadline=.8):
        self.model_dir = resolve_model_dir(model_dir)
        self.deadline = max(.01, min(float(deadline), .8))
        self.ready = False
        self.reason = 'cold'
        self.model_fingerprint = ''
        self._process = None
        self._lock = asyncio.Lock()
        self._counter = 0
        self._warming = False
        self._recovery = None

    def _result(self, reason, started, **extra):
        return {'action': None, 'source': 'local', 'probabilities': {},
                'model_fingerprint': self.model_fingerprint, 'calibrated': False,
                'abstain': True, 'reason': reason,
                'elapsed_ms': (time.monotonic() - started) * 1000, **extra}

    async def warmup(self):
        if self.ready:
            return True
        if self._warming:
            return False
        self._warming = True
        try:
            if not (self.model_dir / 'manifest.json').is_file():
                self.reason = 'missing_manifest'
                return False
            # A 1 GB digest and model construction belong off the application event loop.
            try:
                manifest = await asyncio.to_thread(load_manifest, self.model_dir)
            except Exception as error:
                self.reason = str(error) if isinstance(error, ValueError) else 'invalid_manifest'
                return False
            self.model_fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True,
                ensure_ascii=False).encode()).hexdigest()
            if getattr(sys, 'frozen', False):
                command = [sys.executable, '--local-decision-worker']
            else:
                command = [sys.executable, '-m', 'src.core.local_decision_worker']
            self._process = await asyncio.create_subprocess_exec(*command, '--model-dir', str(self.model_dir),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=65536)
            line = await asyncio.wait_for(self._process.stdout.readline(), 45)
            result = json.loads(line)
            self.ready = result.get('status') == 'ready'
            self.reason = 'ready' if self.ready else result.get('reason', 'worker_failed')
            if not self.ready:
                await self._stop()
            return self.ready
        except asyncio.CancelledError:
            await self._stop()
            raise
        except Exception:
            self.reason = 'worker_failed'
            await self._stop()
            return False
        finally:
            self._warming = False

    async def decide(self, state, recent=()):
        started = time.monotonic()
        if not self.ready or not self._process or self._process.returncode is not None:
            if self.reason in ('cold','timeout','worker_failed') and not self._warming and (self._recovery is None or self._recovery.done()):
                self._recovery=asyncio.create_task(self.warmup())
            return self._result(self.reason if self.reason != 'ready' else 'worker_failed', started)
        if not isinstance(state, dict):
            return self._result('invalid_input', started)
        payload = dict(state)
        if recent and not payload.get('recent'):
            payload['recent'] = list(recent)
        self._counter += 1
        sequence = self._counter
        async def exchange():
            async with self._lock:
                if not self.ready:
                    return self._result('worker_failed', started)
                raw = json.dumps({'id': sequence, 'state': payload}, ensure_ascii=False).encode() + b'\n'
                if len(raw) > 32000:
                    return self._result('input_too_large', started)
                self._process.stdin.write(raw)
                await self._process.stdin.drain()
                value = json.loads(await self._process.stdout.readline())
                if value.get('id') != sequence:
                    raise ValueError('stale_response')
                if value.get('action') not in ACTIONS and not value.get('abstain'):
                    raise ValueError('invalid_action')
                return {**value, 'source': 'local', 'model_fingerprint': self.model_fingerprint,
                        'elapsed_ms': (time.monotonic() - started) * 1000}
        try:
            return await asyncio.wait_for(exchange(), self.deadline)
        except asyncio.CancelledError:
            # A canceled read must not leave an old answer in the next turn's pipe.
            await self._stop()
            self.reason = 'cold'
            raise
        except asyncio.TimeoutError:
            await self._stop()
            self.reason = 'timeout'
            return self._result('timeout', started)
        except Exception:
            await self._stop()
            self.reason = 'worker_failed'
            return self._result('worker_failed', started)

    async def _stop(self):
        self.ready = False
        process, self._process = self._process, None
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def close(self):
        if self._recovery and not self._recovery.done():
            self._recovery.cancel();await asyncio.gather(self._recovery,return_exceptions=True)
        await self._stop()
