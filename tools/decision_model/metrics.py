"""Dependency-free metric/gate implementation shared by training and packaging tests."""
from __future__ import annotations
import math
from src.core.local_decision import ACTIONS


def score(rows, probabilities, thresholds=None, margin=.2):
    if len(rows)!=len(probabilities):raise ValueError('evaluation_length_mismatch')
    thresholds = thresholds or {a:.85 for a in ACTIONS}
    confusion={a:{b:0 for b in ACTIONS} for a in ACTIONS}
    protected=protected_missed=accepted=accepted_silent=wrong_silent=correct=0
    reply_total=reply_missed=0
    bin_counts=[0]*15;bin_conf=[0.]*15;bin_correct=[0]*15
    for row,p in zip(rows,probabilities):
        if set(p)!=set(ACTIONS) or any(not math.isfinite(v) for v in p.values()) or abs(sum(p.values())-1)>1e-5:
            raise ValueError('invalid_probability')
        action=max(p,key=p.get);gold=row['label'];confusion[gold][action]+=1;correct+=action==gold
        values=sorted(p.values(),reverse=True)
        direct=values[0]>=thresholds.get(action,1.) and values[0]-values[1]>=margin
        accepted+=direct
        if gold=='reply':
            reply_total+=1;reply_missed+=direct and action!='reply'
        # defer and context_only both withhold an immediate reply; both require precision.
        if direct and action!='reply':
            accepted_silent+=1;wrong_silent+=action!=gold
        if row.get('protected'):
            protected+=1;protected_missed+=direct and action!='reply'
        b=min(14,int(values[0]*15));bin_counts[b]+=1;bin_conf[b]+=values[0];bin_correct[b]+=action==gold
    n=len(rows)
    if not n:raise ValueError('empty_evaluation')
    fs=[]
    for a in ACTIONS:
        tp=confusion[a][a];fp=sum(confusion[b][a] for b in ACTIONS if b!=a);fn=sum(confusion[a][b] for b in ACTIONS if b!=a)
        fs.append(2*tp/max(1,2*tp+fp+fn))
    ece=sum(abs(bin_conf[i]-bin_correct[i])/n for i in range(15))
    return {'records':n,'accuracy':correct/n,'macro_f1':sum(fs)/3,'ece':ece,'coverage':accepted/n,
            'reply_records':reply_total,'reply_missed':int(reply_missed),
            'reply_recall':1-reply_missed/reply_total if reply_total else None,
            'protected':protected,'protected_missed':int(protected_missed),
            'silent_accepted':accepted_silent,'silent_wrong':wrong_silent,
            'silent_precision':1-wrong_silent/accepted_silent if accepted_silent else None,'confusion':confusion}


def quality_gate(metrics, *, latency_p95_ms=None, flips=None, independent=True):
    checks={
        'independent_split':bool(independent),
        'protected_zero_false_silence':metrics.get('protected',0)>=100 and metrics.get('protected_missed')==0,
        'silence_precision_99':metrics.get('silent_accepted',0)>=100 and (metrics.get('silent_precision') or 0)>=.99,
        'reply_recall_995':metrics.get('reply_records',0)>=100 and (metrics.get('reply_recall') or 0)>=.995,
        'ordinary_local_coverage_50':metrics.get('coverage',0)>=.50,
        'warm_p95_300ms':latency_p95_ms is not None and latency_p95_ms<=300,
        'option_order_flip_01':flips is not None and flips<=.01,
    }
    return {'passed':all(checks.values()),'checks':checks}
