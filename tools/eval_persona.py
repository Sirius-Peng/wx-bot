"""Run artificial held-out cases against a configured model; never export API keys."""
import argparse,asyncio,datetime,json,os
from pathlib import Path
import sys
ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from src.config_store import ConfigStore
from src.core.context import ContextManager
from src.core.llm import LLMGateway,ProviderCfg
from src.core.sender import normalize_reply_text
from src.core.types import WxMessage
from src.store_memory import MemoryStore

def automatic_flags(case,text):
 flags=[]
 if not text.strip():flags.append('empty')
 if case['scene']!='technical' and len(text)>200:flags.append('ordinary_reply_over_200_chars')
 if text.count('……')+text.count('...')>2:flags.append('excess_ellipses')
 if case['id']=='eval-04' and '406' not in text:flags.append('incorrect_arithmetic')
 if case['relationship']=='neutral' and any(t in text for t in ('你这个骗子','你拯救了全世界，却唯独忘了我','你曾将刀刺向我')):flags.append('unjustified_user_as_rover')
 if any(t in text for t in ('断绝所有朋友','不许离开我')):flags.append('relationship_pressure')
 return flags

async def run(args):
 cfg=ConfigStore(Path(args.config)).load();l=cfg.llm
 if not l.primary_key:raise SystemExit('MODEL_EVAL: missing configured primary key')
 provider=ProviderCfg('persona-eval',l.primary_base,l.primary_key,l.primary_model,timeout=40,max_tokens=600,temperature=0.8)
 gateway=LLMGateway(provider);results=[];cases=[json.loads(s) for s in (ROOT/'personas/phrolova/eval.jsonl').read_text().splitlines()];semaphore=asyncio.Semaphore(2)
 async def one(case):
  async with semaphore:
   ctx=ContextManager(MemoryStore(),bot_name='弗洛洛',persona_profile='phrolova',persona_relationship=case['relationship'])
   msg=WxMessage(case['id'],'eval','private','eval-chat','eval-user','测试者','弗洛洛',case['user']);messages=await ctx.build(msg)
   row={**case,'model':provider.model,'temperature':provider.temperature,'manual_score':None}
   try:
    reply=await gateway.chat(messages);wire=normalize_reply_text(reply);row.update(output=reply,wire_output=wire,automatic_flags=automatic_flags(case,wire),status='ok')
   except Exception as e:row.update(status='error',error_type=type(e).__name__,status_code=getattr(e,'status_code',None))
   results.append(row);print(case['id']+': '+row['status'],flush=True)
 try:await asyncio.gather(*(one(c) for c in cases))
 finally:await gateway.aclose()
 results.sort(key=lambda r:r['id']);report={'method':'heldout-model-observation-not-weight-training','profile_version':'1.0.0','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'model':provider.model,'temperature':0.8,'cases':len(cases),'ok':sum(r['status']=='ok' for r in results),'automatic_flags':sum(len(r.get('automatic_flags',[])) for r in results),'manual_review_required':True,'results':results}
 path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(f"MODEL_EVAL: cases={report['cases']}; responses={report['ok']}; automatic_flags={report['automatic_flags']}; manual_review=REQUIRED")
 if report['ok']!=len(cases):raise SystemExit(1)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--live',action='store_true',required=True,help='Explicitly send artificial cases to configured model');p.add_argument('--config',required=True);p.add_argument('--output',required=True);asyncio.run(run(p.parse_args()))
