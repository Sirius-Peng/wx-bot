"""Explainable local turn-end estimation; no extra LLM round-trip or fake chat pings."""
import hashlib,re,statistics
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

@dataclass
class WaitPlan:
    delay:float
    deadline:float
    hold:bool
    reason:str

def plan_wait(items,base,max_wait,human=True,pause_timeout=120):
    """items = [(WxMessage, receipt epoch)]. Stable small jitter remains testable."""
    latest,at=items[-1];start=items[0][1]
    if not human or base<=0:return WaitPlan(base,start+max_wait,False,'fixed')
    text=latest.content.strip()
    paused=[i for i,(m,_) in enumerate(items) if PAUSE.fullmatch(m.content.strip())]
    done=[i for i,(m,_) in enumerate(items) if is_done(m.content)]
    if paused and (not done or paused[-1] > done[-1]):
        # A continuation does not revoke "let me finish". Only an explicit
        # completion signal or the original bounded deadline releases it.
        pause_at=items[paused[-1]][1]
        return WaitPlan(pause_timeout,pause_at+pause_timeout,True,'explicit_pause')
    if is_done(text):return WaitPlan(min(.35,base*.18),at+max_wait,False,'explicit_done')
    if done:start=items[done[-1]][1]
    reason='normal';delay=base
    if UNFINISHED.search(text):delay=base*2;reason='unfinished'
    elif latest.images and not text.replace('[图片]','').strip():delay=base*2;reason='image_caption'
    elif len(text)<8 and not re.search(r'[。！？!?]$',text):delay=base*1.4;reason='short_fragment'
    elif re.search(r'[。！？!?]$',text) or len(text)>50:delay=base*.9;reason='complete_sentence'
    if len(items)>1:
        gaps=[max(0,items[i][1]-items[i-1][1]) for i in range(max(1,len(items)-4),len(items))]
        delay=max(delay,min(base*3,statistics.median(gaps)*1.35))
    jitter=(int(hashlib.sha256((latest.sender_id+latest.msg_id).encode()).hexdigest()[:4],16)%17-8)/100
    return WaitPlan(min(10,max(base*.75,delay*(1+jitter))),start+max_wait,False,reason)

def ready(plan,now,last_at,count):
    if plan.hold:return now>=plan.deadline,now>=plan.deadline
    force=now>=plan.deadline or count>=50
    return force or now-last_at>=plan.delay,force
