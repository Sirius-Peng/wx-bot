"""Quiet-window batching; original messages survive cancellation and restart."""
from __future__ import annotations
import asyncio,json,logging,os,tempfile,time
from dataclasses import asdict,replace
from pathlib import Path
from .types import WxMessage
from .turn_taking import plan_wait,ready

log=logging.getLogger(__name__)
MAX_BATCH=50
class ConversationChanged(RuntimeError):
    """New input arrived before transport submission; rebuild without sending."""

def members(msg):
    return [WxMessage(**m) for m in msg.batch_messages] if msg.batch_messages else [msg]

def message_parts(msg):
    """Old pending envelopes still work without a content_parts field."""
    if msg.content_parts:
        return msg.content_parts
    if not msg.images:
        return [{'type':'text', 'text':msg.content}]
    # Reconstruct placeholders for old DTOs; append only unreferenced images.
    chunks = msg.content.split('[图片]')
    parts=[]
    for index, text in enumerate(chunks):
        if text: parts.append({'type':'text','text':text})
        if index < len(chunks)-1 and index < len(msg.images):
            parts.append({'type':'image','index':index})
    used=min(len(chunks)-1,len(msg.images))
    parts += [{'type':'image','index':i} for i in range(used,len(msg.images))]
    return parts

def has_video(msg):
    return msg.msg_type=='video' or any(p.get('type')=='video' or
        (p.get('type')=='text' and p.get('text')=='[视频]') for p in message_parts(msg))

def merge_messages(messages):
    first=messages[0]
    token=next((m.context_token for m in reversed(messages) if m.context_token),None)
    parts=[];offset=0
    for index,msg in enumerate(messages):
        if index:parts.append({'type':'text','text':'\n'})
        for part in message_parts(msg):
            parts.append({**part,'index':part['index']+offset} if part.get('type')=='image' else dict(part))
        offset+=len(msg.images)
    return replace(first,content="\n".join(m.content for m in messages),
                   images=[image for m in messages for image in m.images],content_parts=parts,
                   context_token=token,is_at_me=any(m.is_at_me for m in messages),
                   batch_messages=[asdict(m) for m in messages])

class MessageBuffer:
    """OneBot collector: ACK after durable enqueue, not after an LLM request."""
    def __init__(self,callback):
        self.callback=callback;self.window=0.0;self.max_wait=10.0
        self.human=True;self.pause_timeout=120
        self.path=None;self.pending={};self.workers={};self.active={};self.typing_hints={};self.running=False
        self.capacity=asyncio.Semaphore(4)
    @staticmethod
    def key(msg):return msg.ctx_key+":"+msg.sender_id
    @staticmethod
    def identity(msg):return msg.platform+":"+msg.ctx_key+":"+msg.sender_id+":"+msg.msg_id
    def configure(self,window,max_wait,path=None,human=True,pause_timeout=120):
        self.human=human;self.pause_timeout=pause_timeout
        self.window=max(0.0,float(window));self.max_wait=max(self.window,float(max_wait))
        if path and self.path is None:
            self.path=Path(path)
            if self.path.exists():
                data=json.loads(self.path.read_text(encoding='utf-8'))
                self.pending={k:[(WxMessage(**e['message']),e['at']) for e in v] for k,v in data.get('pending',{}).items()}
    def save(self):
        if not self.path:return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        data={'version':1,'pending':{k:[{'message':asdict(m),'at':at} for m,at in v] for k,v in self.pending.items() if v}}
        fd,tmp=tempfile.mkstemp(dir=self.path.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
            os.chmod(tmp,0o600);os.replace(tmp,self.path)
        finally:Path(tmp).unlink(missing_ok=True)
    def start(self):
        self.running=True
        for key in self.pending:self._start(key)
    def _start(self,key):
        if self.running and key not in self.workers:
            task=asyncio.create_task(self._drain(key));self.workers[key]=task
            def finished(done,k=key):
                self.workers.pop(k,None)
                if not done.cancelled():done.exception()
                if self.running and self.pending.get(k):self._start(k)
            task.add_done_callback(finished)
    def submit(self,msg):
        key=self.key(msg);items=self.pending.setdefault(key,[])
        if any(self.identity(m)==self.identity(msg) for m,_ in items):return
        if sum(map(len,self.pending.values()))>=200:raise RuntimeError('Conversation inbox is full')
        items.append((msg,time.time()))
        try:self.save()
        except BaseException:items.pop();raise
        self._start(key)
    def note_typing(self,chat_id,sender_id,on=True,platform='onebot',chat_type='private'):
        key=f'ctx:{platform}:{chat_type}:{chat_id}:{sender_id}'
        if not self.pending.get(key):return False
        if on:
            now=time.time();self.typing_hints[key]={'at':now,'until':now+4}
        else:self.typing_hints.pop(key,None)
        return True

    def check(self,msg,*,after_failure=False):
        active=self.active.get(self.key(msg))
        if active and (after_failure or not active['force']):
            hint=self.typing_hints.get(self.key(msg),{})
            if hint.get('until',0)>time.time() and hint.get('at',0)>active.get('typing_at',0):
                raise ConversationChanged('Peer is continuing input')
            if any(self.identity(m) not in active['known'] for m,_ in self.pending.get(self.key(msg),[])):
                raise ConversationChanged('New conversation input')
    async def _drain(self,key):
        while self.running and self.pending.get(key):
            items=self.pending[key]
            while self.running:
                plan=plan_wait(items,self.window,self.max_wait,self.human,self.pause_timeout)
                is_ready,force=ready(plan,time.time(),items[-1][1],len(items))
                if is_ready and (force or self.typing_hints.get(key,{}).get('until',0)<=time.time()):break
                await asyncio.sleep(.05)
            if not self.running:break
            selected=list(items[:MAX_BATCH]);msg=merge_messages([m for m,_ in selected])
            self.active[key]={'known':{self.identity(m) for m,_ in items},'force':force,'typing_at':self.typing_hints.get(key,{}).get('at',0)}
            try:
                async with self.capacity:
                    self.check(msg)
                    await self.callback(msg)
                del items[:len(selected)]
                try:self.save()
                except BaseException:items[:0]=selected;raise
            except ConversationChanged:pass
            except asyncio.CancelledError:raise
            except Exception:
                log.exception('QQ batch pending retry');await asyncio.sleep(.5)
            finally:self.active.pop(key,None)
        if not self.pending.get(key):
            self.pending.pop(key,None);self.typing_hints.pop(key,None)
    async def close(self):
        self.running=False
        for task in self.workers.values():task.cancel()
        await asyncio.gather(*self.workers.values(),return_exceptions=True)
        self.workers.clear();self.typing_hints.clear();self.save()
