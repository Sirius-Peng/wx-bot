"""Local, interruptible turn-end estimation shared by QQ and WeChat collectors."""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass

PAUSE=re.compile(r'(?:(?:你|请|先|再|我|稍微)[，, ]*)*(?:等一下|等一等|等等|稍等|等我一下|等我说完|等我发完|别回复|不要回复|别回答|不要回答|还没说完|还没发完|先别回)(?:[，, ]*(?:我还没说完|我还没发完|我还在打字|等我说完|等我发完|先别回复))?[。.!！… ]*')
DONE=re.compile(r'(?:好了[，, ]*)?(?:说完了|发完了|就这些|你说吧|可以回答了|可以回复了|你可以回答了|你可以回复了|请回答|请回复|立即回答|立即回复)[。.!！?？ ]*')
UNFINISHED=re.compile(r'(?:然后|因为|但是|还有|比如|就是说|我想说的是|我想问的是|先说一下|关于|以及|另外|而且|所以|主要是|首先|其次|我想问个问题|我想说件事|给你看看)[，,：:…\. ]*$|[，,：:…]$')


def is_done(text):
    text=text.strip()
    if DONE.fullmatch(text):return True
    # A clear closing sentence is accepted, but quoted/embedded phrases are not.
    parts=[part.strip() for part in re.split(r'[。!！?？;；\n]',text) if part.strip()]
    return bool(parts and DONE.fullmatch(parts[-1]))


class SendingCadence:
    """Last 20 valid receipt gaps per conversation, not just the current burst.

    IDs prevent retries from being measured as fresh input. Gaps after a 30 minute
    absence are a new conversation, not evidence that the user types very slowly.
    Only receipt metadata is persisted; this record never copies message content.
    """
    def __init__(self,data=None):
        self.entries={}
        if not isinstance(data,dict):return
        for key,row in data.items():
            if not isinstance(row,dict):continue
            at=row.get('at')
            if not isinstance(at,(int,float)) or not math.isfinite(at):continue
            values=row.get('gaps',[])
            gaps=[float(g) for g in values if isinstance(g,(int,float)) and math.isfinite(g) and 0<g<30*60] if isinstance(values,list) else []
            seen=row.get('seen',[])
            self.entries[str(key)]={'at':at,'gaps':gaps[-20:],
                'seen':[str(s) for s in seen][-256:] if isinstance(seen,list) else []}

    def record(self,key,identity,at):
        previous=self.entries.get(key,{})
        seen=list(previous.get('seen',[]))
        if identity in seen:return
        gaps=list(previous.get('gaps',[]))[-20:]
        last=previous.get('at')
        if last is not None and 0 < at-last < 30*60:gaps.append(at-last)
        self.entries[key]={'at':max(at,last if last is not None else at),'gaps':gaps[-20:],'seen':(seen+[identity])[-256:]}
        # Bound metadata for long-lived installations without evicting recent peers.
        if len(self.entries)>1024:
            oldest=min(self.entries,key=lambda k:self.entries[k].get('at',0))
            self.entries.pop(oldest,None)

    def gaps(self,key):
        return [float(g) for g in self.entries.get(key,{}).get('gaps',[]) if 0<float(g)<30*60][-20:]


@dataclass
class WaitPlan:
    delay:float
    deadline:float
    hold:bool
    reason:str


def plan_wait(items,base=4,max_wait=10,human=True,pause_timeout=120,*,cadence=None):
    """items = [(WxMessage, receipt epoch)]. Max wait is an evaluation, not a send.

    The configured window remains scalable for tests and existing custom settings;
    the default 4 second window becomes 6 seconds for fragments/attachment captions.
    Historical cadence can only increase a normal window (up to 8 seconds).
    """
    latest,at=items[-1];start=items[0][1]
    if not human or base<=0:return WaitPlan(max(0,base),start+max_wait,False,'fixed')
    text=latest.content.strip()
    paused=[i for i,(m,_) in enumerate(items) if PAUSE.fullmatch(m.content.strip())]
    done=[i for i,(m,_) in enumerate(items) if is_done(m.content)]
    if paused and (not done or paused[-1] > done[-1]):
        pause_at=items[paused[-1]][1]
        return WaitPlan(min(8,base*1.5),pause_at+pause_timeout,True,'explicit_pause')
    if is_done(text):return WaitPlan(min(.3,base*.15),at+max_wait,False,'explicit_done')
    reason='normal';delay=base
    if UNFINISHED.search(text):delay=base*1.5;reason='unfinished'
    elif (latest.images or latest.msg_type=='video' or any(p.get('type')=='video' for p in latest.content_parts)):
        delay=base*1.5;reason='image_caption' if latest.images else 'video_caption'
    elif len(text)<8 and not re.search(r'[。！？!?]$',text):delay=base*1.5;reason='short_fragment'
    gaps=(list(cadence) if cadence is not None else
          [items[i][1]-items[i-1][1] for i in range(1,len(items))])
    gaps=[g for g in gaps if 0<g<30*60][-20:]
    if gaps:delay=max(delay,min(8,statistics.median(gaps)*1.5))
    return WaitPlan(min(8,delay),start+max_wait,False,reason)


def ready(plan,now,last_at,count):
    # A pause deadline merely admits the turn to semantic re-evaluation. Neither
    # the old 10 second deadline nor 50 envelopes grants permission to interrupt.
    if plan.hold:return now>=plan.deadline and now-last_at>=plan.delay,False
    return now-last_at>=plan.delay,False
