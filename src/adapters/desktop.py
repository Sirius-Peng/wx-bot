"""Local companion transport. No synthetic WeChat/QQ recipients or credentials."""
from __future__ import annotations
import asyncio, hashlib, json, time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from .base import BaseAdapter
from ..core.types import WxMessage
from ..core.conversation import MessageBuffer
from ..core.storage import _atomic_json


class DesktopAdapter(BaseAdapter):
    platform='desktop'

    def __init__(self,data_dir,owner_id='local'):
        self.owner_id=owner_id;self.root=Path(data_dir)
        self.path=self.root/'companion-conversation.json'
        self._binding=ContextVar('desktop_turn',default=None)
        self.listeners=set();self.seq=0;self.events=[];self.ids=[];self.running=False
        if self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            self.ids=raw.get('ids',[]);self.events=raw.get('events',[]);self.seq=raw.get('seq',0)
        self.buffer=MessageBuffer(self._dispatch);self._handler=None
        self.buffer.active_space=self.current_persona_space
        self.buffer.legacy_space=self.legacy_persona_space

    def on_message(self,handler):self._handler=handler
    def configure_burst(self,window,max_wait,state_path=None,human=True,pause_timeout=120):
        self.buffer.configure(window,max_wait,self.root/'desktop-bursts.json',human,pause_timeout)
    async def start(self):self.running=True;self.buffer.start()
    async def stop(self):self.running=False;await self.buffer.close()
    async def is_healthy(self):return self.running
    def delivery_available(self):return self.running and bool(self.listeners)
    async def _dispatch(self,msg):
        if self._handler:await self._handler(msg)
    def check_reply_current(self,msg):self.buffer.check(msg)

    def pending_turn(self, msg):
        from ..core.conversation import merge_messages
        messages = [m for m, _ in self.buffer.pending.get(self.buffer.key(msg), [])]
        return merge_messages(messages) if messages else msg

    @staticmethod
    def turn_id(msg):
        ids=[m.get('msg_id','') for m in msg.batch_messages] or [msg.msg_id]
        return msg.msg_id+':'+hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:10]

    @contextmanager
    def bind_reply_context(self,msg):
        marker=self._binding.set(msg)
        try:yield
        finally:self._binding.reset(marker)

    async def send_typing(self,chat_id,on=True):
        msg=self._binding.get()
        if msg is None or chat_id!=self.owner_id:return False
        self.publish('state',self.turn_id(msg),{'state':'thinking' if on else 'idle'})
        return True

    def publish(self,kind,tid,payload,persist=False):
        self.seq+=1
        bound=self._binding.get()
        space=self.message_persona_space(bound) if bound is not None else self.current_persona_space()
        event={'version':1,'type':kind,'turn_id':tid,'seq':self.seq,'owner_id':self.owner_id,
               'persona_space':space,**payload}
        if persist:
            previous=self.events
            self.events=(self.events+[event])[-256:]
            try:_atomic_json(self.path,{'version':1,'ids':self.ids[-4096:],'seq':self.seq,'events':self.events})
            except BaseException:self.events=previous;raise
        for queue in tuple(self.listeners):
            try:queue.put_nowait(event)
            except asyncio.QueueFull:self.listeners.discard(queue)
        return event

    async def submit(self,text,client_id,persona_space=None):
        if not isinstance(client_id,str) or not 1<=len(client_id)<=120:raise ValueError('消息编号无效')
        if not isinstance(text,str) or not text.strip() or len(text)>64000:raise ValueError('请输入不超过64000字的内容')
        space=self.current_persona_space()
        if persona_space is not None and persona_space!=space:raise ValueError('人格已切换，请在当前对话重新发送')
        # A client_id identifies one physical ingress, even across persona changes.
        identity=self.owner_id+':'+client_id
        if identity in self.ids:
            return {'duplicate':True,'client_id':client_id,'persona_space':space}
        msg=WxMessage(msg_id=client_id,platform='desktop',chat_type='private',chat_id=self.owner_id,
            sender_id=self.owner_id,sender_name='我',bot_id='desktop-bot',content=text,timestamp=int(time.time()*1000),
            persona_space=space)
        # Durable pending envelope precedes acknowledgement and activity observers.
        self.buffer.submit(msg);self.ids.append(identity)
        try:self.publish('accepted',client_id,{'client_id':client_id,'text':text,'persona_space':space},persist=True)
        except BaseException:
            self.ids.pop();raise
        self.note_activity(msg)
        return {'client_id':client_id,'accepted':True,'persona_space':space}

    async def preview(self,msg,delta):
        self.check_reply_current(msg)
        self.publish('delta',self.turn_id(msg),{'text':delta,'persona_space':self.message_persona_space(msg)})
    async def cancel_turn(self,msg):
        self.publish('cancelled',self.turn_id(msg),{'persona_space':self.message_persona_space(msg)})
    async def send_text(self,chat_id,text,at=None):
        msg=self._binding.get()
        if chat_id!=self.owner_id:raise ValueError('桌面会话对象不匹配')
        tid=self.turn_id(msg) if msg else 'desktop:'+str(self.seq+1)
        event=self.publish('completed',tid,{'text':text},persist=True)
        return str(event['seq'])
    async def send_image(self,chat_id,path_or_url):raise ValueError('桌面图片输出尚未启用')

    def snapshot(self,after=0):
        space=self.current_persona_space()
        return [{**e,'persona_space':e.get('persona_space') or self.legacy_persona_space()}
                for e in self.events if e['seq']>after and e.get('owner_id','local')==self.owner_id
                and (e.get('persona_space') or self.legacy_persona_space())==space]
