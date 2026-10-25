"""Observed API usage only: missing counts are unknown, never free tokens.

Cache ratios are weighted by measured input tokens, not by request-level booleans.
No model prices, currency conversion, provider inference or bill estimates live
here. A bounded metrics window is not an account invoice or lifetime ledger.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar


TOKEN_FIELDS = ('prompt_tokens','completion_tokens','total_tokens','cache_hit_tokens',
                'cache_miss_tokens','reasoning_tokens')
ANTHROPIC_FIELDS = ('input_tokens_observed','cache_creation_input_tokens',
                    'cache_read_input_tokens','output_tokens_observed')
ALL_TOKEN_FIELDS = TOKEN_FIELDS+ANTHROPIC_FIELDS
TASKS = frozenset({'chat','decision','memory','search','search_plan','search_synthesis','search_review',
                   'compaction','proactive','late_reply','internal','unknown'})
_task = ContextVar('llm_usage_task',default=None)
_MISSING = object()


def field(value, name, default=None):
    return value.get(name,default) if isinstance(value,dict) else getattr(value,name,default)


def token_count(value):
    return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None


def _first_count(raw, paths):
    for path in paths:
        value=raw
        for name in path.split('.'):
            value=field(value,name)
        count=token_count(value)
        if count is not None:return count,path
    return None,None


def normalize_usage(raw):
    """Normalize OpenAI-compatible dicts/objects without inventing absent data.

    The reported total/miss counts remain unknown when absent; prompt-hit is not
    silently written back as a measured provider miss count. Cache denominator
    derivation happens separately, with explicit consistency/coverage checks.
    """
    hit,hit_source=_first_count(raw,('prompt_cache_hit_tokens','prompt_tokens_details.cached_tokens','cache_hit_tokens'))
    miss,miss_source=_first_count(raw,('prompt_cache_miss_tokens','cache_miss_tokens'))
    prompt=token_count(field(raw,'prompt_tokens'))
    completion=token_count(field(raw,'completion_tokens'))
    reasoning,_=_first_count(raw,('completion_tokens_details.reasoning_tokens','reasoning_tokens'))
    if hit_source=='cache_hit_tokens':
        prior=field(raw,'cache_hit_source')
        if prior in ('prompt_cache_hit_tokens','prompt_tokens_details.cached_tokens','cache_hit_tokens'):
            hit_source=prior
    if miss_source=='cache_miss_tokens':
        prior=field(raw,'cache_miss_source')
        if prior in ('prompt_cache_miss_tokens','cache_miss_tokens'):miss_source=prior
    inconsistent=bool((prompt is not None and hit is not None and hit>prompt)
        or (prompt is not None and miss is not None and miss>prompt)
        or (prompt is not None and hit is not None and miss is not None and hit+miss!=prompt))
    return {'prompt_tokens':prompt,'completion_tokens':completion,
        'total_tokens':token_count(field(raw,'total_tokens')),'cache_hit_tokens':hit,
        'cache_miss_tokens':miss,'reasoning_tokens':reasoning,
        'cached':hit>0 if hit is not None else None,
        'cache_hit_source':hit_source,'cache_miss_source':miss_source,
        'cache_inconsistent':inconsistent}


def normalize_anthropic_usage(raw):
    """Explicit native Messages protocol, not an inferred OpenAI input alias.

    Anthropic's input_tokens excludes cache creation/read. Preserve those
    original observations separately; a combined prompt total requires all
    three components, including explicitly measured zeroes. No absent cache
    field is silently treated as zero.
    """
    result=normalize_usage(None)
    base,_=_first_count(raw,('input_tokens_observed','input_tokens'))
    output,_=_first_count(raw,('output_tokens_observed','output_tokens'))
    creation=token_count(field(raw,'cache_creation_input_tokens'))
    read=token_count(field(raw,'cache_read_input_tokens'))
    miss=base+creation if base is not None and creation is not None else None
    prompt=miss+read if miss is not None and read is not None else None
    reasoning,_=_first_count(raw,('output_tokens_details.reasoning_tokens','reasoning_tokens'))
    result.update(protocol='anthropic',input_tokens_observed=base,
        cache_creation_input_tokens=creation,cache_read_input_tokens=read,
        output_tokens_observed=output,prompt_tokens=prompt,completion_tokens=output,
        total_tokens=token_count(field(raw,'total_tokens')),reasoning_tokens=reasoning,
        cache_hit_tokens=read,cache_miss_tokens=miss,cached=read>0 if read is not None else None,
        cache_hit_source='anthropic.cache_read_input_tokens' if read is not None else None,
        cache_miss_source='anthropic.input_tokens+cache_creation_input_tokens' if miss is not None else None,
        prompt_tokens_source='anthropic.input_tokens+cache_creation_input_tokens+cache_read_input_tokens' if prompt is not None else None)
    return result


@contextmanager
def task_scope(name):
    """Task-local label for a shared gateway, including concurrent compaction."""
    if not isinstance(name,str) or name not in TASKS:raise ValueError('Unknown usage task')
    marker=_task.set(name)
    try:yield
    finally:_task.reset(marker)


def request_task(provider_name='',chat_id=''):
    scoped=_task.get()
    if scoped is not None:return scoped
    provider_task={'admission':'decision','judge':'decision','decision':'decision',
                   'memory':'memory','search':'search','compaction':'compaction',
                   'proactive':'proactive'}.get(provider_name)
    return provider_task or ('chat' if chat_id else 'internal')


def _cache_pair(usage):
    if usage['cache_inconsistent']:return None
    hit=usage['cache_hit_tokens'];prompt=usage['prompt_tokens'];miss=usage['cache_miss_tokens']
    if hit is None:return None
    if prompt is not None:return hit,prompt,'reported_prompt'
    if miss is not None:return hit,hit+miss,'reported_hit_and_miss'
    return None


def _mean(total,count):
    if not count:return None
    try:return total/count
    except OverflowError:return None


def _aggregate(records,missing=0):
    calls=len(records)+missing
    fields={}
    for key in ALL_TOKEN_FIELDS:
        known=[usage[key] for _,usage in records if usage.get(key) is not None]
        fields[key]={'observed_sum':sum(known) if known else None,'known_calls':len(known),
            'unknown_calls':calls-len(known),
            'mean_per_known_call':_mean(sum(known),len(known))}
    pairs=[pair for _,usage in records if (pair:=_cache_pair(usage)) is not None]
    hits=sum(pair[0] for pair in pairs);inputs=sum(pair[1] for pair in pairs)
    inconsistent=sum(usage['cache_inconsistent'] for _,usage in records)
    complete=bool(calls and len(pairs)==calls)
    ratio=hits/inputs if inputs else None
    unknown_usage=sum(all(usage.get(key) is None for key in ALL_TOKEN_FIELDS) for _,usage in records)+missing
    return {'attempts':calls,'recorded_attempts':len(records),'unrecorded_attempts':missing,
        'usage_unknown_calls':unknown_usage,'fields':fields,
        'cache':{'hit_ratio':ratio,'overall_hit_ratio':ratio if complete else None,
            'hit_tokens':hits if pairs else None,'input_tokens':inputs if pairs else None,
            'known_calls':len(pairs),'unknown_calls':calls-len(pairs)-inconsistent,
            'inconsistent_calls':inconsistent,'complete':complete,
            'denominator_sources':{source:sum(pair[2]==source for pair in pairs)
                                   for source in ('reported_prompt','reported_hit_and_miss')},
            'scope':'reported_input_tokens_only'}}


def summary(metrics, *, attempts=None):
    """Summarize the supplied request records, optionally counting unlogged tries.

    hit_ratio covers only consistent, observed input denominators. A partial
    report never labels that observed subset as the overall rate: the separate
    overall_hit_ratio stays unknown until all attempts have usable cache data.
    Unknown counts include explicit attempts that have no usage record at all.
    """
    records=[]
    for metric in metrics or ():
        raw=field(metric,'usage',_MISSING)
        raw=metric if raw is _MISSING else raw
        protocol=field(raw,'protocol') or field(metric,'usage_protocol')
        usage=normalize_anthropic_usage(raw) if protocol=='anthropic' else normalize_usage(raw)
        name=field(metric,'task','unknown')
        records.append((name if isinstance(name,str) and name in TASKS else 'unknown',usage))
    if attempts is None:attempts=len(records)
    if isinstance(attempts,bool) or not isinstance(attempts,int) or attempts<len(records):
        raise ValueError('Attempt count must include every supplied record')
    missing=attempts-len(records)
    result=_aggregate(records,missing)
    groups={name:[record for record in records if record[0]==name] for name in sorted({r[0] for r in records})}
    if missing and 'unknown' not in groups:groups['unknown']=[]
    result['by_task']={name:_aggregate(values,missing if name=='unknown' else 0) for name,values in groups.items()}
    result['scope']='supplied_request_records_only'
    return result
