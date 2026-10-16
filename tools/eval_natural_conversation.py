"""20 deterministic contract timelines, not human/LLM semantic-quality evaluation.

Baseline modules are loaded from the specified immutable Git commit. Temporary
fixtures contain no user data or external model/network calls. --output is optional.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.core.conversation import merge_messages
from src.core.context import ContextManager
from src.core.memory import PersonaMemory
from src.core.reply_decision import ReplyDecider
from src.core.sender import DeliveryError
from src.core.trigger import TriggerRouter
from src.core.turn_coordinator import TurnCoordinator
from src.core.turn_taking import plan_wait,ready
from src.core.types import WxMessage
from src.orchestrator import Orchestrator
from src.store_memory import MemoryStore

BASELINE='f92fc3f74a6a41c9793886409d8273795cb5ca64'
BASELINE_HASHES={
    'src/core/turn_taking.py':'75b29b2eda0593b472c1afb63bcbddd253b9d037a46920db42f8280c6bd96d8d',
    'src/core/trigger.py':'51b0867b21ae26d1a5adccd454204891baf35079928fa69691124f20aadb3c46',
    'src/core/memory.py':'8fe793dfb8cf532face5de2f361f32ce5851ae27afb28dbb3cef43ded121bb8a',
    'src/orchestrator.py':'f95f65b9f44746e2090b222c39db3400a6e8a4b6fcfcc565d795fc7c146d63d6',
}


def msg(text,mid='1'):
    return WxMessage(str(mid),'onebot','private','p_fixture','fixture','合成用户','bot',text)


def load_baseline(ref,baseline_root=None):
    loaded={};provenance={}
    for name,file,package in [('wait','src/core/turn_taking.py','src.core'),('trigger','src/core/trigger.py','src.core'),
                              ('memory','src/core/memory.py','src.core'),('orchestrator','src/orchestrator.py','src')]:
        if baseline_root:
            source=(Path(baseline_root)/file).read_bytes()
            # Exported native-test trees have no .git; authenticate the original
            # checkpoint by its committed module hashes instead of inventing one.
            if ref != BASELINE or hashlib.sha256(source).hexdigest() != BASELINE_HASHES[file]:
                raise ValueError('baseline tree does not match the verified checkpoint: '+file)
        else:
            source=subprocess.check_output(['git','show',f'{ref}:{file}'],cwd=ROOT)
        qualified=package+'._timeline_baseline_'+name
        module=types.ModuleType(qualified);module.__package__=package;sys.modules[qualified]=module
        exec(compile(source,file+'@'+ref,'exec'),module.__dict__)
        loaded[name]=module
        provenance[file]=hashlib.sha256(source).hexdigest()
    return loaded,provenance


class Gateway:
    def __init__(self,action):self.action=action;self.calls=0
    async def complete(self,*args,**kwargs):self.calls+=1;return json.dumps({'action':self.action})


def metric():return {'interruptions':0,'missed_protected':0,'duplicate_submissions':0,'memory_overreach':0,'unnecessary_bubbles':0}


def result(name,baseline,new,observations,passed=True):
    return {'id':name,'baseline':baseline,'new':new,'observations':observations,'passed':bool(passed)}


def simulate(events,wait_module,base):
    """Zero generation latency isolates turn-end admission; 50ms collector ticks."""
    pending=[];emissions=[];index=0
    for step in range(int((events[-1][0]+130)*20)+1):
        now=round(step/20,6)
        while index<len(events) and events[index][0]<=now:
            at,text=events[index];pending.append((msg(text,index),at));index+=1
        if pending:
            plan=wait_module.plan_wait(pending,base,10)
            admitted,_=wait_module.ready(plan,now,pending[-1][1],len(pending))
            if admitted:
                emissions.append({'at':now,'messages':len(pending)})
                pending=[]
        if index==len(events) and not pending:break
    return emissions


async def delivery_probe(cls,directory,*,new):
    class Adapter:
        platform='onebot'
        def __init__(self):self.calls=0
        def on_message(self,h):self.handler=h
        async def send_text(self,*args,**kwargs):
            self.calls+=1;raise TimeoutError('synthetic ambiguous send')
    class Model:
        async def stream(self,*args,**kwargs):yield '合成回复'
    class Sender:
        def __init__(self,a):self.adapter=a
        async def stream_send(self,chat,stream,**kwargs):
            text=''.join([p async for p in stream])
            try:await self.adapter.send_text(chat,text)
            except TimeoutError as exc:raise DeliveryError('synthetic ambiguous send') from exc
            return text
        async def send(self,*args,**kwargs):return []
    adapter=Adapter();store=MemoryStore();extra={}
    if new:extra={'coordinator':TurnCoordinator(Path(directory)/'turns.json'),'reply_decider':ReplyDecider()}
    orch=cls(adapter,store,Model(),None,ContextManager(store),TriggerRouter(),Sender(adapter),typing_indicator=False,**extra)
    for _ in range(2):
        try:await orch.handle(msg('帮我推荐一首曲子'))
        except DeliveryError:pass
    return adapter.calls


async def evaluate(baseline_ref=BASELINE,baseline_root=None):
    baseline_root=baseline_root or os.environ.get('WXBOT_EVAL_BASELINE_ROOT')
    old,provenance=load_baseline(baseline_ref,baseline_root)
    fixtures=[]
    new_wait=types.SimpleNamespace(plan_wait=plan_wait,ready=ready)
    timelines=[
        ('T01 连续输入12秒',[(i*.5,f'第{i}段。') for i in range(25)]),
        ('T02 连续输入20秒',[(i*.5,f'第{i}段。') for i in range(41)]),
        ('T03 61条输入30秒',[(i*.5,f'第{i}段。') for i in range(61)]),
        ('T04 碎片表达',[(i*1.7,'然后' if i<7 else '这就是全部。') for i in range(8)]),
        ('T05 慢节奏5秒间隔',[(0,'第一段'),(5,'第二段'),(10,'全部内容。')]),
        ('T06 明确等待再结束',[(0,'等我说完'),(2,'我想问个问题'),(10,'关于这首曲子'),(12,'说完了')]),
        ('T07 引用等待不是命令',[(0,'她说“等我说完”，这是什么意思')]),
        ('T08 明确结束快接答',[(0,'你推荐哪首曲子'),(1,'说完了')]),
    ]
    for name,events in timelines:
        before=simulate(events,old['wait'],2);after=simulate(events,new_wait,4)
        bm,nm=metric(),metric();bm['interruptions']=sum(e['at']<events[-1][0] for e in before);nm['interruptions']=sum(e['at']<events[-1][0] for e in after)
        fixtures.append(result(name,bm,nm,{'events':events,'baseline_emissions':before,'new_emissions':after},nm['interruptions']==0 and len(after)==1))
    decisions=[
        ('T09 简单确认','好的',False,'context_only'),
        ('T10 收尾与说明','晚安，我先睡了',False,'context_only'),
        ('T11 无问号问题','怎么理解这首曲子的结构',True,'reply'),
        ('T12 情绪求助','我今天很难过，陪陪我',True,'reply'),
        ('T13 当前更正','更正一下，我不是教师，我是医生',True,'reply'),
        ('T14 普通分享稍后重判','窗外的树叶又落了一地。',False,'defer'),
    ]
    for name,text,protected,expected in decisions:
        old_decision=await old['trigger'].TriggerRouter().decide(msg(text))
        gateway=Gateway('defer' if expected=='defer' else 'context_only')
        decision=await ReplyDecider(gateway,bot_name='弗洛洛').decide(msg(text))
        bm,nm=metric(),metric();bm['missed_protected']=int(protected and not old_decision.should_reply);nm['missed_protected']=int(protected and decision.action!='reply')
        if expected=='context_only':bm['unnecessary_bubbles']=int(old_decision.should_reply);nm['unnecessary_bubbles']=int(decision.action=='reply')
        observations={'text':text,'baseline_action':'reply' if old_decision.should_reply else 'context_only','new_action':decision.action,'classifier_calls':gateway.calls}
        passed=decision.action==expected
        if expected=='defer':
            with tempfile.TemporaryDirectory() as d:
                now=[1000.0];ledger=TurnCoordinator(Path(d)/'turns.json',clock=lambda:now[0]);tid=ledger.begin(msg(text));ledger.decide(tid,decision)
                revisit_at=ledger.turns[tid]['revisit_at'];now[0]=revisit_at
                due=ledger.due();ledger.reconsidered(tid)
                second=await ReplyDecider(Gateway('context_only')).decide(msg(text),revisit=True)
                ledger.finish(tid,'context_only');observations.update(revisit_delay=revisit_at-1000,revisit_action=second.action,second_due=len(ledger.due()))
                passed &= len(due)==1 and 60<=revisit_at-1000<=180 and not ledger.due()
        fixtures.append(result(name,bm,nm,observations,passed))
    with tempfile.TemporaryDirectory() as d:
        before=await delivery_probe(old['orchestrator'].Orchestrator,d,new=False)
        after=await delivery_probe(Orchestrator,d,new=True)
    bm,nm=metric(),metric();bm['duplicate_submissions']=max(0,before-1);nm['duplicate_submissions']=max(0,after-1)
    fixtures.append(result('T15 QQ不确定投递回调重试',bm,nm,{'baseline_submissions':before,'new_submissions':after,'transport':'synthetic timeout; actual old/new orchestration, common deterministic sender stub'},after==1))
    with tempfile.TemporaryDirectory() as d:
        clock=[1800000000.0]
        fact={'category':'preference','content':'用户喜欢钢琴曲','keywords':['钢琴'],'evidence':'我喜欢钢琴曲'}
        class Extractor:
            async def complete(self,*args,**kwargs):return json.dumps([fact],ensure_ascii=False)
        with patch('time.time',side_effect=lambda:clock[0]):
            before=old['memory'].PersonaMemory(Path(d)/'old.json',llm=Extractor());after=PersonaMemory(Path(d)/'new.json',llm=Extractor())
            async def observe(memory,number,at):
                return await memory.observe('p_fixture','private','fixture','合成用户',
                    [{'user':'我喜欢钢琴曲','assistant':'','observed_at':at,'source_ids':['source:'+str(number)],'episode_id':'episode:'+str(number)}],platform='onebot')
            await observe(before,1,clock[0]);await observe(after,1,clock[0])
            old_count=len(before.recall('fixture','p_fixture','钢琴',platform='onebot'));new_count=len(after.recall('fixture','p_fixture','钢琴',platform='onebot'))
            bm,nm=metric(),metric();bm['memory_overreach']=old_count;nm['memory_overreach']=new_count
            fixtures.append(result('T16 单次事实仍是候选',bm,nm,{'baseline_recalled':old_count,'new_recalled':new_count,'new_phase':after.list_items()[0]['phase']},new_count==0))
            clock[0]+=86401;await observe(before,2,clock[0]);await observe(after,2,clock[0])
            recalled=after.recall('fixture','p_fixture','钢琴',platform='onebot')
            fixtures.append(result('T17 跨日两次证据巩固',metric(),metric(),{'baseline_recalled':len(before.recall('fixture','p_fixture','钢琴',platform='onebot')),'new_phase':recalled[0].phase if recalled else None},bool(recalled and recalled[0].phase=='stable')))
            old_style=before.add_manual('user','fixture','style','用户偏好术语化表达',['术语'],platform='onebot')
            new_style=after.add_manual('user','fixture','style','用户偏好术语化表达',['术语'],platform='onebot')
            oq=before.recall('fixture','p_fixture','晚饭吃点什么',platform='onebot');nq=after.recall('fixture','p_fixture','晚饭吃点什么',platform='onebot')
            bm,nm=metric(),metric();bm['memory_overreach']=int(any(i.id==old_style['id'] for i in oq));nm['memory_overreach']=int(any(i.id==new_style['id'] for i in nq))
            fixtures.append(result('T18 无关交流风格不越界注入',bm,nm,{'baseline_style_recalled':bool(bm['memory_overreach']),'new_style_recalled':bool(nm['memory_overreach'])},nm['memory_overreach']==0))
            target=next(i for i in after.recall('fixture','p_fixture','钢琴',platform='onebot') if i.category=='preference')
            snapshot=(target.last_user_evidence_at,target.confidence,target.lifecycle())
            for _ in range(100):before.recall('fixture','p_fixture','钢琴',platform='onebot');after.recall('fixture','p_fixture','钢琴',platform='onebot')
            unchanged=snapshot==(target.last_user_evidence_at,target.confidence,target.lifecycle())
            fixtures.append(result('T19 百次召回不续期',metric(),metric(),{'baseline_usage_hits':next(i.hits for i in before.recall('fixture','p_fixture','钢琴',platform='onebot') if i.category=='preference'),'new_evidence_unchanged':unchanged,'baseline_strength_metric':None,'note':'旧版无生命周期强度字段；只报告实际hits，不将hits虚构为强度'},unchanged))
            clock[0]+=181*86400
            old_stale=[i for i in before.recall('fixture','p_fixture','钢琴',platform='onebot') if i.category=='preference'];new_stale=[i for i in after.recall('fixture','p_fixture','钢琴',platform='onebot') if i.category=='preference']
            bm,nm=metric(),metric();bm['memory_overreach']=len(old_stale);nm['memory_overreach']=len(new_stale)
            fixtures.append(result('T20 稳定认知休眠而手动保持固定',bm,nm,{'baseline_stale_recalled':len(old_stale),'new_stale_recalled':len(new_stale),'new_manual_phase':next(i['phase'] for i in after.list_items(include_history=True) if i['id']==new_style['id'])},not new_stale and next(i['phase'] for i in after.list_items(include_history=True) if i['id']==new_style['id'])=='pinned'))
    totals={version:{key:sum(f[version][key] for f in fixtures) for key in metric()} for version in ('baseline','new')}
    return {'schema':1,'kind':'deterministic contract regression; not human or real-model semantic evaluation','fixture_count':len(fixtures),
            'baseline_ref':baseline_ref,'baseline_source':'verified exported source tree' if baseline_root else 'read-only Git objects','baseline_module_sha256':provenance,'method':'Original baseline wait/trigger/memory/orchestrator modules loaded read-only from Git or SHA256-authenticated exported checkpoint; current source APIs; synthetic models and zero-latency sender/50ms timing harness. No accounts or paid calls.',
            'totals':totals,'passed':all(f['passed'] for f in fixtures),'fixtures':fixtures,
            'limits':['No human realism rating or real-model semantic accuracy is measured.','No actual platform delivery is measured; duplicate metric counts deterministic adapter submissions.','Fixed timing harness does not model internet jitter, IME or CPU load.','Baseline has no decay-strength metric; no inferred strength number is reported.']}


def write_report(report,output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    json_path=output/'natural-conversation-evaluation.json';md_path=output/'natural-conversation-evaluation.md'
    json_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=['# 自然接话与渐进记忆：20组固定合同时间线','',f"基线：`{report['baseline_ref']}`。源码模块从Git或SHA256认证的原版导出目录只读加载，不使用示意数字替代基线执行。",'',
           '**范围：确定性合同回归，不是人工体感评分或真实LLM语义评估。无账号、无外部模型调用。**','',
           '| 观察计数 | 基线 | 新实现 |','|---|---:|---:|']
    labels={'interruptions':'发言尚未结束就接答','missed_protected':'受保护诉求漏答','duplicate_submissions':'重复投递提交','memory_overreach':'不应参与普通召回的记忆','unnecessary_bubbles':'确认/收尾额外气泡'}
    for key,label in labels.items():lines.append(f"| {label} | {report['totals']['baseline'][key]} | {report['totals']['new'][key]} |")
    lines+=['','## 固定样例','', '| 样例 | 新合同 |','|---|---|']
    lines += [f"| {f['id']} | {'通过' if f['passed'] else '失败'} |" for f in report['fixtures']]
    lines+=['','## 方法与边界','',report['method'],'','基线没有衰减强度字段，百次召回只报告实际hits，不把使用次数伪造为强度。指标是此20组固定夹具中的观察次数，不代表真实用户发生率。完整事件、提交时间、来源哈希与观察值见同目录JSON。']
    md_path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # Reopen both artifacts so a successful command also validates the written result.
    assert json.loads(json_path.read_text(encoding='utf-8'))['fixture_count']==20
    assert '20组' in md_path.read_text(encoding='utf-8')
    return json_path,md_path


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output');parser.add_argument('--baseline-ref',default=BASELINE);parser.add_argument('--baseline-root',default=os.environ.get('WXBOT_EVAL_BASELINE_ROOT'),help='read-only exported original checkpoint, authenticated by module SHA256');args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    report=asyncio.run(evaluate(args.baseline_ref,args.baseline_root))
    paths=write_report(report,args.output) if args.output else ()
    print(json.dumps({'passed':report['passed'],'fixtures':report['fixture_count'],'totals':report['totals'],'artifacts':[str(p) for p in paths]},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
