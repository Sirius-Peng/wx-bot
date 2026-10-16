"""Read-only, provenance-backed topology. Keyword edges are not causal claims."""
from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path
import time

from .memory import MemoryItem, PersonaMemory, GLOBAL_OWNERS, CATEGORIES

PHASES={'candidate','stable','temporary','dormant','pinned'}
MAX_FILE_BYTES=16*1024*1024


def read_json(path):
    path=Path(path)
    if not path.exists():return {}
    if path.stat().st_size>MAX_FILE_BYTES:raise ValueError('state snapshot exceeds graph read budget')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise ValueError('invalid state snapshot')
    return value


def identifier(kind,value):
    return kind+':'+hashlib.sha256(str(value).encode()).hexdigest()[:24]


def number(value,default=0):
    return float(value) if isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value) else default


class MemoryGraphSnapshot(PersonaMemory):
    """Use existing identity/forget rules without initializing any stateful writer."""
    def __init__(self,path):
        raw=read_json(path)
        self.identities=raw.get('identities',[])
        if not isinstance(self.identities,list):raise ValueError('invalid graph identity snapshot')
        for identity in self.identities:
            if (not isinstance(identity,dict) or not isinstance(identity.get('id'),str)
                    or not isinstance(identity.get('label',''),str)
                    or not self._valid_accounts(identity.get('accounts'))):
                raise ValueError('invalid graph identity snapshot')
        stored=raw.get('suppressions',[])
        durable=read_json(Path(path).with_name('memory-forgotten.json')).get('markers',[])
        if not isinstance(stored,list) or not isinstance(durable,list):
            raise ValueError('invalid graph forget barrier')
        markers=[*stored,*durable]
        for marker in markers:
            if (not isinstance(marker,dict) or not isinstance(marker.get('id'),str)
                    or not marker.get('id') or not self._valid_accounts(marker.get('accounts'))
                    or not all(isinstance(marker.get(k,''),str) for k in ('content','evidence','fact_key'))
                    or not isinstance(marker.get('source_texts',[]),list)
                    or not all(isinstance(text,str) for text in marker.get('source_texts',[]))):
                # A malformed barrier must not make forgotten data visible.
                raise ValueError('invalid graph forget barrier')
        self.suppressions=list({m['id']:m for m in markers}.values())
        self._items={};self.invalid_items=0
        known={f.name for f in fields(MemoryItem)}
        if not isinstance(raw.get('items',[]),list):raise ValueError('invalid graph memory snapshot')
        for row in raw.get('items',[]):
            try:
                values={k:v for k,v in row.items() if k in known}
                for name in ('created_at','updated_at','valid_from','valid_until','expires_at','last_user_evidence_at','last_used_at','established_at','confidence','importance'):
                    if name in values:values[name]=number(values[name])
                item=MemoryItem(**values)
                if not all(isinstance(getattr(item,k),str) for k in ('id','scope','owner_id','platform','content','status','category','evidence','source_chat','fact_key','superseded_by')):raise ValueError('invalid memory strings')
                if item.scope not in ('user','group') or item.category not in CATEGORIES or item.phase not in PHASES or not isinstance(item.pinned,bool):raise ValueError('invalid memory lifecycle')
                if not isinstance(item.keywords,list) or not isinstance(item.evidence_sources,list) or not isinstance(item.evidence_ids,list):raise ValueError('invalid memory source shape')
                item.keywords=[w for w in item.keywords if isinstance(w,str)][:12]
                item.evidence_ids=[sid for sid in item.evidence_ids if isinstance(sid,str)]
                item.evidence_sources=[{**e,'id':e.get('id',''),'observed_at':number(e.get('observed_at'))}
                    for e in item.evidence_sources if isinstance(e,dict) and isinstance(e.get('text'),str)
                    and isinstance(e.get('id',''),str) and isinstance(e.get('episode_id',''),str)][:100]
                if raw.get('version',1)<3:
                    item.pinned=item.source_chat=='manual'
                    item.phase='pinned' if item.pinned else ('candidate' if item.evidence else 'dormant')
                    item.last_user_evidence_at=(item.valid_from or item.updated_at or item.created_at) if item.evidence else 0
                self._items[item.id]=item
            except (TypeError,ValueError,AttributeError):self.invalid_items+=1

    @staticmethod
    def _valid_accounts(accounts):
        return (isinstance(accounts,list) and bool(accounts) and all(
            isinstance(a,dict) and isinstance(a.get('platform'),str)
            and isinstance(a.get('owner_id'),str) for a in accounts))


def context_rows(root):
    """Only current context stores, never physical logs/backups or media payloads."""
    rows=[];omitted=0
    for filename,platform in (('context-wechat.json','ilink'),('context-qq.json','onebot')):
        raw=read_json(Path(root)/filename)
        contexts=raw.get('contexts',{})
        if not isinstance(contexts,dict):raise ValueError('invalid conversation snapshot')
        for key,turns in contexts.items():
            prefix=f'ctx:{platform}:'
            if not key.startswith(prefix) or not isinstance(turns,list):continue
            chat_type,sep,chat_id=key[len(prefix):].partition(':')
            if not sep or chat_type not in ('private','group'):continue
            for turn in turns[-1000:]:
                if (not isinstance(turn,dict) or not isinstance(turn.get('user',''),str)
                        or not isinstance(turn.get('assistant',''),str)):
                    omitted+=1;continue
                ids=[s for s in turn.get('source_ids',[]) if isinstance(s,str)] if isinstance(turn.get('source_ids',[]),list) else []
                owner=chat_id.removeprefix('p_') if platform=='onebot' else chat_id
                if chat_type=='group':
                    source_prefix=f'{platform}:group:{chat_id}:'
                    owners={s[len(source_prefix):].partition(':')[0] for s in ids if s.startswith(source_prefix)}
                    if len(owners)!=1 or not all(owners):omitted+=1;continue
                    owner=next(iter(owners))
                rows.append({'platform':platform,'chat_type':chat_type,'chat_id':chat_id,'owner_id':owner,
                    'user':str(turn.get('user',''))[:12000],'assistant':str(turn.get('assistant',''))[:12000],
                    'source_ids':ids,'turn_id':str(turn.get('turn_id','')),'ts':number(turn.get('ts'))})
            omitted+=max(0,len(turns)-1000)
    rows.sort(key=lambda r:(r['ts'],r['turn_id']),reverse=True)
    omitted+=max(0,len(rows)-2000)
    return rows[:2000],omitted


def build_memory_graph(memory,conversations,*,platform='',person='',phase='',keyword='',include_history=False,limit=160,now=None):
    now=time.time() if now is None else now
    limit=max(20,min(300,int(limit)))
    nodes={};edges={};people={};truncated=False;query=keyword.strip().casefold()
    phase_labels={'candidate':'形成中','stable':'已熟悉','temporary':'临时状态','dormant':'已淡化','pinned':'已固定'}

    def person_info(p,owner,scope='user'):
        key=('group:'+p+':'+owner if scope=='group' else 'global' if owner in GLOBAL_OWNERS else memory.identity_key(p,owner))
        pid=identifier('person',key)
        linked=next((i for i in memory.identities if any(a.get('platform')==p and a.get('owner_id')==owner for a in i.get('accounts',[]))),None) if scope=='user' and owner not in GLOBAL_OWNERS else None
        accounts=(linked['accounts'] if linked else [{'platform':p,'owner_id':owner}])
        label=(linked.get('label') if linked else '') or ('通用手动记忆' if key=='global' else ('群聊 '+owner if scope=='group' else owner))
        if memory.is_suppressed(p,owner,label):label=owner
        info={'id':pid,'type':'person','label':label[:60],'body':label[:300],
              'accounts':[{'platform':str(a.get('platform','')),'owner_id':str(a.get('owner_id',''))[:120]} for a in accounts],
              'scope':scope,'channels':sorted({a.get('platform','') for a in accounts})}
        people[pid]=info
        return pid,info

    def add(node):
        nonlocal truncated
        if node['id'] in nodes:return True
        if len(nodes)>=limit:truncated=True;return False
        nodes[node['id']]=node;return True

    def edge(a,b,kind,label,derived=False):
        if a not in nodes or b not in nodes:return
        eid=identifier('edge',a+'|'+b+'|'+kind)
        if len(edges)<limit*6:edges[eid]={'id':eid,'source':a,'target':b,'kind':kind,'label':label,'derived':derived}

    def blocked(p,owner,text):
        return memory.is_suppressed(p,owner,text)

    def related_barrier(marker,p,owner):
        identity=memory.identity_key(p,owner)
        return any(a.get('owner_id') in GLOBAL_OWNERS or
                   (a.get('owner_id')==owner and (not a.get('platform') or not p or a.get('platform')==p)) or
                   memory.identity_key(a.get('platform',''),a.get('owner_id',''))==identity
                   for a in marker.get('accounts',[]))

    def token_blocked(p,owner,text):
        if blocked(p,owner,text):return True
        token=memory._normalized(text)
        for marker in memory.suppressions:
            related=related_barrier(marker,p,owner)
            if related and token and any(token in memory._normalized(v) for v in (marker.get('content',''),marker.get('evidence',''),*marker.get('source_texts',[]))):return True
        return False

    def has_barrier(p,owner):
        return any(related_barrier(m,p,owner) for m in memory.suppressions)

    # Matched raw evidence can be located without displaying logs or guessing
    # from timestamps. A retained evidence excerpt is explicit when context was compacted.
    source_index={};available=[]
    for row in conversations:
        p=row.get('platform','');owner=row.get('owner_id','');user=str(row.get('user',''))
        if p not in ('ilink','onebot') or not owner or (platform and p!=platform) or blocked(p,owner,user):continue
        pid,info=person_info(p,owner)
        if person and pid!=person:continue
        if not user.strip():continue  # assistant-only proactive messages are never evidence
        # An assistant reply in a group may paraphrase another member's forgotten
        # facts. No per-sentence provenance exists, so hide it conservatively.
        hide_assistant=has_barrier(p,owner) or (row.get('chat_type')=='group' and bool(memory.suppressions))
        safe={**row,'person_id':pid,'assistant':'' if hide_assistant or blocked(p,owner,str(row.get('assistant',''))) else str(row.get('assistant',''))}
        cid=identifier('conversation',pid+'|'+p+'|'+str(row.get('chat_type','private'))+'|'+str(row.get('chat_id',''))+'|'+str(row.get('turn_id') or row.get('source_ids') or user))
        safe['id']=cid;available.append(safe)
        for sid in row.get('source_ids',[]):
            source_index[(pid,hashlib.sha256(sid.encode()).hexdigest())]=safe
            source_index[(pid,sid)]=safe

    items=[]
    for item in list(memory._items.values()):
        p=item.platform;owner=item.owner_id
        if platform and p and p!=platform:continue
        if blocked(p,owner,item.content) or memory.is_suppressed(p,owner,item.content,item.fact_key):continue
        if item.evidence and blocked(p,owner,item.evidence):continue
        current,strength=item.lifecycle(now)
        if not include_history and (current=='dormant' or item.status!='active'):continue
        if phase and phase!=current:continue
        pid,info=person_info(p,owner,item.scope)
        if person and pid!=person:continue
        sources=[e for e in item.evidence_sources if not blocked(p,owner,e['text'])]
        keywords=[k[:60] for k in item.keywords if k.strip() and not token_blocked(p,owner,k)]
        if query and query not in ' '.join([item.content,*keywords,*[e['text'] for e in sources]]).casefold():continue
        items.append((item,current,strength,pid,info,sources,keywords))
    items.sort(key=lambda t:(-number(t[0].last_user_evidence_at or t[0].updated_at),t[0].id))
    matched=len(items)

    def conversation_node(row,*,excerpt=False):
        return {'id':row['id'],'type':'conversation','label':('来源摘录' if excerpt else '对话')+' · '+str(row.get('user','')).replace('\n',' ')[:30],
            'body':str(row.get('user',''))[:1200],'assistant':str(row.get('assistant',''))[:1200],
            'ts':number(row.get('ts')),'platform':row.get('platform',''),'chat_type':row.get('chat_type','private'),
            'chat_id':str(row.get('chat_id',''))[:120],'origin':'evidence_excerpt' if excerpt else 'recent_context',
            'person_id':row['person_id'],'source_count':len(row.get('source_ids',[]))}

    # Reserve room for actual conversation sources; keywords cannot crowd them out.
    shown_items=[]
    for item,current,strength,pid,info,sources,keywords in items:
        if pid not in nodes and len(nodes)>limit-2:truncated=True;continue
        if not add(info):continue
        mid=identifier('memory',item.id)
        node={'id':mid,'type':'memory','label':item.content[:34],'body':item.content[:600],
            'memory_id':item.id,'person_id':pid,'platform':item.platform,'category':item.category,
            'phase':current,'phaseLabel':phase_labels.get(current,current),'status':item.status,
            'strength':round(strength,6),'pinned':item.pinned,'keywords':keywords,
            'evidence_count':len(set(item.evidence_ids)|{str(e.get('id','')) for e in sources}),
            'ts':number(item.last_user_evidence_at or item.created_at),
            'origin':'manual' if item.source_chat=='manual' else 'automatic',
            'source_note':'手动添加，不代表从对话推导' if item.source_chat=='manual' else '有逐字用户依据' if sources else '旧数据未保留可验证的逐字来源'}
        if not add(node):continue
        edge(pid,mid,'knows','关于此人')
        shown_items.append((item,mid,pid,keywords))
        seen=set()
        for evidence in sorted(sources,key=lambda e:number(e.get('observed_at')),reverse=True):
            row=source_index.get((pid,str(evidence.get('id',''))))
            if row and evidence['text'] not in row.get('user',''):row=None
            if row is None:
                eid=identifier('conversation',pid+'|evidence|'+str(evidence.get('episode_id') or evidence.get('id'))+'|'+evidence['text'])
                row={'id':eid,'person_id':pid,'user':evidence['text'],'assistant':'','ts':evidence.get('observed_at'),
                     'platform':item.platform,'chat_id':item.source_chat,'source_ids':[evidence.get('id','')]}
                excerpt=True
            else:excerpt=False
            if row['id'] in seen:continue
            seen.add(row['id'])
            if add(conversation_node(row,excerpt=excerpt)):
                edge(pid,row['id'],'interaction','交流经历')
                edge(row['id'],mid,'evidence','用户原话依据')
            if len(seen)>=3:break

    # Show recent standalone conversations too, rather than pretending every
    # statement has already become a memory. Cap their share for readability.
    for row in available[:80]:
        if query and query not in (row['user']+' '+row['assistant']).casefold():continue
        if phase:continue
        pid=row['person_id']
        if pid not in nodes and len(nodes)>limit-2:truncated=True;continue
        if not add(people[pid]):continue
        if add(conversation_node(row)):edge(pid,row['id'],'interaction','交流经历（不等于长期记忆）')
    for item,mid,pid,keywords in shown_items:
        for word in keywords[:4]:
            tid=identifier('topic',pid+'|'+word.casefold())
            if add({'id':tid,'type':'topic','label':word,'body':word,'person_id':pid,
                    'origin':'keyword','source_note':'关键词字面关联，不代表因果、真实推理路径或新的事实'}):
                edge(mid,tid,'keyword','共享关键词',True)
        successor=identifier('memory',item.superseded_by) if item.superseded_by else ''
        if (successor in nodes and nodes[successor].get('person_id')==pid
                and nodes[successor].get('category')==item.category):
            edge(mid,successor,'version','事实版本更新')
    # Remove parents for which the node cap did not permit any visible child.
    connected={e[k] for e in edges.values() for k in ('source','target')}
    nodes={k:v for k,v in nodes.items() if v['type']!='person' or k in connected}
    people_options=[{'id':p['id'],'label':p['label'],'channels':p['channels']} for p in people.values()]
    people_options.sort(key=lambda p:(p['label'],p['id']))
    return {'version':1,'nodes':list(nodes.values()),'edges':list(edges.values()),'people':people_options,
        'stats':{kind:sum(n['type']==kind for n in nodes.values()) for kind in ('person','memory','conversation','topic')},
        'coverage':{'memory_total':len(memory._items),'memory_matched':matched,
                    'memory_shown':sum(n['type']=='memory' for n in nodes.values()),'recent_conversations_available':len(available),
                    'truncated':truncated,'invalid_items':getattr(memory,'invalid_items',0),'limit':limit,
                    'scope':'近期上下文与保留的记忆依据，不是全部聊天日志；最多展示每条记忆三个来源和四个关键词。'},
        'filters':{'platform':platform,'person':person,'phase':phase,'keyword':keyword,'include_history':include_history},
        'generated_at':now,'notice':'实线表示所属、交流或已保存的用户依据；虚线仅表示关键词关联。此图不是模型内部思维或因果推理的读取。'}
