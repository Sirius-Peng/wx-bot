"""Opt-in paid DeepSeek benchmark using the unchanged production decision path.

Only the hash-locked, public synthetic stress fixture is sent. No persona, memory,
real chat or config is submitted. The key comes from stdin or an explicit local
config, never argv/output. This tool neither updates settings nor approves models.
Run on the target Windows machine; use --help for the reproducible CLI.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time
from urllib.parse import urlsplit

from src.core.llm import LLMGateway, ProviderCfg
from src.core.reply_decision import ReplyDecider
from .eval_stress import DATASET_SHA256, message, read_cases, summarize

PRICE_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
PRICE_AS_OF = '2026-10-20'
RATES_CNY_PER_MILLION = {
    'off_peak': {'hit': .02, 'miss': 1., 'output': 4.},
    'peak': {'hit': .04, 'miss': 2., 'output': 8.},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sdk_versions():
    versions = {}
    for name in ('openai', 'httpx', 'httpx2'):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def normalize_usage(raw):
    hit = count(field(raw, 'prompt_cache_hit_tokens'))
    hit_source = 'prompt_cache_hit_tokens' if hit is not None else None
    if hit is None:
        hit = count(field(field(raw, 'prompt_tokens_details'), 'cached_tokens'))
        if hit is not None:
            hit_source = 'prompt_tokens_details.cached_tokens'
    return {
        'prompt_tokens': count(field(raw, 'prompt_tokens')),
        'completion_tokens': count(field(raw, 'completion_tokens')),
        'total_tokens': count(field(raw, 'total_tokens')),
        'prompt_cache_hit_tokens': hit,
        'prompt_cache_miss_tokens': count(field(raw, 'prompt_cache_miss_tokens')),
        'reasoning_tokens': count(field(field(raw, 'completion_tokens_details'), 'reasoning_tokens')),
        'cache_hit_field': hit_source,
    }


def latency(values):
    values = sorted(float(v) for v in values if isinstance(v, (int, float))
                    and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
    if not values:
        return {'samples': 0, 'mean_ms': None, 'p50_ms': None, 'p95_ms': None, 'max_ms': None}
    return {'samples': len(values), 'mean_ms': statistics.mean(values),
            'p50_ms': statistics.median(values), 'p95_ms': values[int(.95*(len(values)-1))],
            'max_ms': values[-1]}


def usage_summary(calls):
    totals = {}
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens',
            'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'reasoning_tokens')
    for key in keys:
        known = [c['usage'][key] for c in calls if c.get('usage', {}).get(key) is not None]
        totals[key] = {'observed_sum': sum(known), 'known_calls': len(known),
                      'unknown_calls': len(calls)-len(known),
                      'mean_per_known_call': statistics.mean(known) if known else None}
    priced = [c for c in calls if all(c.get('usage', {}).get(key) is not None
              for key in ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'completion_tokens'))]
    estimates = {band: sum((c['usage']['prompt_cache_hit_tokens']*r['hit']
        + c['usage']['prompt_cache_miss_tokens']*r['miss']
        + c['usage']['completion_tokens']*r['output'])/1_000_000 for c in priced)
        for band, r in RATES_CNY_PER_MILLION.items()}
    paired = [c for c in calls if all(c.get('usage', {}).get(key) is not None
              for key in ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'))]
    hit = sum(c['usage']['prompt_cache_hit_tokens'] for c in paired)
    miss = sum(c['usage']['prompt_cache_miss_tokens'] for c in paired)
    return {'api_calls': len(calls), 'fields': totals,
            'cache_hit_share_of_observed_input': hit/(hit+miss) if hit+miss else None,
            'paired_cache_calls': len(paired), 'unknown_cache_calls': len(calls)-len(paired),
            'priced_calls': len(priced), 'unpriced_calls': len(calls)-len(priced),
            'estimated_cny_for_priced_calls': estimates,
            'rates_cny_per_million': RATES_CNY_PER_MILLION,
            'price_as_of': PRICE_AS_OF, 'price_source': PRICE_SOURCE,
            'note': 'Observed SDK usage only; unknown is not zero. Unpriced calls may still be billed. '
                    'Two price bands are estimates, not account charges or a holiday/time-band determination.'}


class MeasuredGateway(LLMGateway):
    """Same complete_once payload/client/options; raw usage, no DB or masked zeros."""
    def __init__(self, key):
        super().__init__(ProviderCfg(name='deepseek-decision-benchmark',
            base_url='https://api.deepseek.com', api_key=key, model='deepseek-flash', timeout=10.))
        self.calls = []
        self.case_id = None

    async def complete_once(self, prompt, max_tokens=80):
        cfg = self.primary
        record = {'case_id': self.case_id, 'started_utc': utc_now(), 'status': 'inflight',
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'prompt_chars': len(prompt), 'max_tokens': max_tokens, 'usage': normalize_usage(None),
            'model_requested': cfg.model, 'model_returned': None, 'finish_reason': None,
            'stream': False, 'ttft_ms': None, 'elapsed_ms': None}
        self.calls.append(record)
        start = time.monotonic()
        try:
            # Deliberately identical to LLMGateway.complete_once: no JSON mode,
            # streaming, system prefix, retry, alternate provider or prompt edits.
            response = await self._client(cfg).chat.completions.create(model=cfg.model,
                messages=[{'role': 'user', 'content': prompt}], max_tokens=max_tokens,
                temperature=.1, **self._mode_options(cfg, internal=True))
            record.update(status='response', usage=normalize_usage(getattr(response, 'usage', None)),
                          model_returned=getattr(response, 'model', None))
            choice = response.choices[0]
            record['finish_reason'] = getattr(choice, 'finish_reason', None)
            record['content'] = choice.message.content or ''
            return record['content']
        except asyncio.CancelledError:
            record['status'] = 'cancelled_deadline'
            raise
        except Exception as error:
            # Never serialize provider error bodies, headers, config or key values.
            record.update(status='error', error_type=type(error).__name__,
                          http_status=getattr(error, 'status_code', None))
            raise
        finally:
            record['elapsed_ms'] = (time.monotonic()-start)*1000
            record['finished_utc'] = utc_now()


def compare_local(path, rows, calls):
    if path is None:
        return None
    report = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if report.get('dataset_sha256') != DATASET_SHA256:
        raise ValueError('local_comparison_fixture_mismatch')
    results = report.get('results', [])
    if len(results) != len(rows) or {x['id'] for x in results} != {r['id'] for r in rows}:
        raise ValueError('local_comparison_case_mismatch')
    by_id = {r['id']: r for r in rows}
    for value in results:
        row = by_id[value['id']]
        if any(value.get(key) != expected for key, expected in (
                ('session_id', row['session_id']), ('expected', row['label']),
                ('protected', row['protected']), ('slice', row['slice']))):
            raise ValueError('local_comparison_case_fields_mismatch')
    requested = {c['case_id'].split('/pass-')[0] for c in calls}
    return {'path_sha256': file_sha(path), 'platform': report['model']['platform'],
            'all_turns': latency([x['pipeline']['elapsed_ms'] for x in results]),
            'same_cloud_requested_cases': latency([x['pipeline']['elapsed_ms'] for x in results if x['id'] in requested]),
            'metrics': report['summary']['actual_reply_decider_pipeline'],
            'api_billed_tokens': 0,
            'note': 'Earlier same-fixture local-only pipeline on the displayed platform: rules + ONNX + conservative abstention; '
                    'no cloud review. Local tokenization/computation is not zero, but API billing is zero. '
                    'Not the artificial 256-token benchmark and not an actually timed hybrid strategy.'}


def observed_timeout(call):
    # A controller deadline is evidence even if HTTP cancellation cleanup has
    # not yet reached the gateway's except/finally blocks.
    return bool(call.get('deadline_observed')) or call['status'] == 'cancelled_deadline' or (
        call['status'] == 'error' and 'Timeout' in call.get('error_type', ''))


async def evaluate(key, out, *, repeats=1, local_report=None, gateway=None):
    if not isinstance(key, str) or not key.strip():
        raise ValueError('empty_api_key')
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats not in (1, 2, 3):
        raise ValueError('repeats_must_be_1_to_3')
    rows = read_cases()
    out = Path(out)
    if out.exists():
        raise ValueError('benchmark_output_exists')
    # Validate a supplied comparison before incurring paid requests.
    compare_local(local_report, rows, [])
    out.mkdir(parents=True, exist_ok=False)
    gateway = gateway or MeasuredGateway(key.strip())
    decider = ReplyDecider(gateway=gateway, mode='natural', timeout=3.)
    root = Path(__file__).resolve().parents[2]
    source_names = ('src/core/reply_decision.py', 'src/core/llm.py', 'tools/decision_model/benchmark_cloud.py')
    hashes = {n: file_sha(root/n) for n in source_names}
    started = utc_now()
    results = []
    aborted = None
    try:
        for rep in range(repeats):
            for row in rows:
                case = row['id']+'/pass-'+str(rep+1)
                gateway.case_id = case
                before_calls = len(gateway.calls)
                before = time.monotonic()
                decision = await decider.decide(message(row), recent=[{'user': x} for x in row['state']['recent']])
                elapsed = (time.monotonic()-before)*1000
                # Give bounded-deadline cancellation one loop turn to reach the
                # measurement finally-block; this is outside measured latency.
                await asyncio.sleep(0)
                result = {'id': row['id'], 'case_id': case, 'pass': rep+1, 'expected': row['label'],
                    'protected': row['protected'], 'slice': row['slice'],
                    'action': decision.action, 'source': decision.source, 'reason': decision.reason,
                    'elapsed_ms': elapsed,
                    'api_requested': len(gateway.calls) > before_calls}
                if result['source'] == 'conservative' and elapsed >= 3000.:
                    for call in gateway.calls[before_calls:]:
                        if call['status'] != 'response':
                            call['deadline_observed'] = True
                results.append(result)
                # Reopenable per-turn evidence survives an interrupted experiment.
                with (out/'results.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'result': result, 'calls': gateway.calls[before_calls:]}, ensure_ascii=False)+'\n')
                if any(c.get('http_status') in (401, 402, 403) for c in gateway.calls[before_calls:]):
                    aborted = 'provider_auth_or_balance_unavailable'
                    break
                if len(results) % 20 == 0:
                    print('CLOUD_PROGRESS '+json.dumps({'turns': len(results), 'api_calls': len(gateway.calls)}), flush=True)
            if aborted:
                break
    finally:
        await gateway.aclose()
    if {n: file_sha(root/n) for n in hashes} != hashes:
        raise ValueError('benchmark_source_changed')
    by_id = {row['id']: row for row in rows}
    expanded = [by_id[r['id']] for r in results]
    metrics = summarize(expanded, results, accepted=[r['source'] != 'conservative' for r in results])
    api_cases = [r for r in results if r['api_requested']]
    successful = [c for c in gateway.calls if c['status'] == 'response']
    report = {'schema': 'wxbot.cloud-decision-benchmark.v1', 'started_utc': started, 'finished_utc': utc_now(),
        'platform': platform.platform(), 'python': platform.python_version(), 'sdk_versions': sdk_versions(),
        'model_requested': 'deepseek-flash', 'models_returned': dict(Counter(c['model_returned'] for c in successful)),
        'dataset_sha256': DATASET_SHA256, 'source_sha256': hashes, 'repeats': repeats,
        'complete': aborted is None, 'abort_reason': aborted,
        'protocol': {'deadline_seconds': 3., 'thinking': 'disabled', 'temperature': .1,
                     'max_tokens': 8, 'stream': False, 'retries': 0, 'backend': 'cloud',
                     'fixture': '120 public AI-authored seen synthetic sessions; not blind acceptance',
                     'private_chat_or_persona_sent': False, 'cache_control': 'best effort, not forced cold/warm'},
        'metrics': metrics, 'all_turn_latency': latency([r['elapsed_ms'] for r in results]),
        'api_turn_latency': latency([r['elapsed_ms'] for r in api_cases]),
        'api_response_latency': latency([c.get('elapsed_ms') for c in successful]),
        'first_api_request': gateway.calls[0] if gateway.calls else None,
        'later_api_requests': latency([c.get('elapsed_ms') for c in gateway.calls[1:]]),
        'status_counts': dict(Counter(c['status'] for c in gateway.calls)),
        'timeout_or_cancelled_calls': sum(observed_timeout(c) for c in gateway.calls),
        'pending_cleanup_calls': sum(c['status'] == 'inflight' for c in gateway.calls),
        'parse_failure_after_response': sum(r['source'] == 'conservative' and
            any(c['case_id'] == r['case_id'] and c['status'] == 'response' for c in gateway.calls) for r in results),
        'length_limited_responses': sum(c.get('finish_reason') == 'length' for c in gateway.calls),
        'usage': usage_summary(gateway.calls),
        'rule_only_turns': sum(not r['api_requested'] for r in results),
        'local_comparison': compare_local(local_report, rows, gateway.calls),
        'results': results, 'calls': gateway.calls,
        'note': 'Actual full-response latency, not TTFT. Timeouts retain unknown usage and possible billing. '
                'Observed small synthetic benchmark, no release gate/settings/model changes.'}
    report['by_pass'] = {}
    if report['local_comparison'] is not None:
        report['local_comparison']['comparable'] = aborted is None
    for rep in range(1, repeats+1):
        selected = [r for r in results if r['pass'] == rep]
        selected_calls = [c for c in gateway.calls if c['case_id'].endswith('/pass-'+str(rep))]
        report['by_pass'][str(rep)] = {
            'turn_latency': latency([r['elapsed_ms'] for r in selected]),
            'api_turn_latency': latency([r['elapsed_ms'] for r in selected if r['api_requested']]),
            'status_counts': dict(Counter(c['status'] for c in selected_calls)),
            'usage': usage_summary(selected_calls),
        }
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    json.loads((out/'report.json').read_text(encoding='utf-8'))
    print('CLOUD_COMPLETE '+json.dumps({k: report[k] for k in ('platform', 'all_turn_latency',
        'api_turn_latency', 'status_counts', 'timeout_or_cancelled_calls', 'usage')}, ensure_ascii=False), flush=True)
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', required=True, help='NEW experiment directory; never overwrites reports')
    p.add_argument('--repeats', type=int, choices=(1, 2, 3), default=1)
    p.add_argument('--local-report', help='Existing Windows eval_stress same120 comparison (no local re-inference)')
    secret = p.add_mutually_exclusive_group(required=True)
    secret.add_argument('--key-stdin', action='store_true', help='Read only the key from stdin, never echo it')
    secret.add_argument('--config', help='Read primary_key ONLY from this user-selected local config; no writes')
    a = p.parse_args(argv)
    if a.key_stdin:
        key = sys.stdin.readline().strip()
    else:
        cfg = json.loads(Path(a.config).read_text(encoding='utf-8-sig')).get('llm', {})
        if urlsplit(cfg.get('primary_base', '')).hostname != 'api.deepseek.com':
            raise ValueError('config_is_not_official_deepseek')
        key = cfg.get('primary_key', '')
    report = asyncio.run(evaluate(key, a.out, repeats=a.repeats, local_report=a.local_report))
    return 0 if report['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
