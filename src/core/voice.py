"""Optional, bounded TTS accompaniment after a successful WeChat text reply.

No extra LLM call, no training runtime, no replacement of reliable text delivery.
Voice means a playable synthesized audio attachment, not an unverified voice bubble.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import wave

from .http import make_client
from .conversation import ConversationChanged

log = logging.getLogger(__name__)
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 60
FISH_MODELS = {'s1','s2-pro','s2.1-pro','s2.1-pro-free','drama-3'}
MUTE = re.compile(r'^(?:请|以后|暂时|先)?(?:别|不要|不用)(?:再)?(?:给我)?(?:发|用)?语音(?:回复|了)?[。！!\s]*$|^(?:只用)?文字(?:回复)?就好[。！!\s]*$')
REQUEST = re.compile(r'^(?:请|以后|可以)?(?:用语音(?:回复|说|回答)|给我发(?:一段|个)?语音|念给我听|读给我听)')


@dataclass(frozen=True)
class AudioResult:
    data: bytes
    mime_type: str = 'audio/wav'
    filename: str = '弗洛洛-合成语音.wav'


def validate_voice_config(cfg, *, require_ready=False):
    """Reject ambiguous endpoints/models before any billed provider call."""
    if cfg.provider not in ('fish','gpt_sovits'):
        raise ValueError('请选择 Fish Audio 或本地 GPT-SoVITS')
    try:
        parsed=urlsplit(cfg.base_url)
        valid=(parsed.scheme in ('http','https') and bool(parsed.hostname) and not parsed.username
               and not parsed.password and not parsed.query and not parsed.fragment)
        parsed.port
    except (ValueError,TypeError):valid=False
    if not valid:raise ValueError('语音服务地址需为 HTTP/HTTPS 地址，不含账号、查询参数或片段')
    for field,lo,hi in [('reply_rate',0,1),('cooldown_seconds',0,3600),('max_chars',20,500),('timeout_seconds',3,60)]:
        value=getattr(cfg,field)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
            raise ValueError('语音数值设置超出范围')
    if cfg.provider=='fish' and cfg.fish_model not in FISH_MODELS:
        raise ValueError('Fish 模型名称不在支持列表中，请明确选择，避免隐式切换计费模型')
    if require_ready:
        if cfg.provider=='fish' and (not cfg.api_key.strip() or not cfg.model_id.strip()):
            raise ValueError('请先保存 Fish Audio 的独立 API Key 和音色 ID')
        if cfg.provider=='gpt_sovits' and (not cfg.reference_audio.strip() or not cfg.reference_text.strip()):
            raise ValueError('请先填写 GPT-SoVITS 服务端参考音频路径和对应文本')


def validate_wav(data):
    if not isinstance(data,bytes) or not data or len(data)>MAX_AUDIO_BYTES:
        raise ValueError('语音音频为空或超过 8 MiB')
    try:
        with wave.open(io.BytesIO(data),'rb') as source:
            frames=source.getnframes();rate=source.getframerate()
            if (source.getcomptype()!='NONE' or source.getnchannels() not in (1,2)
                    or source.getsampwidth() not in (1,2,3,4) or not 8000<=rate<=96000
                    or frames<=0 or frames/rate>MAX_SECONDS):
                raise ValueError('语音格式需要不超过60秒的PCM WAV')
            expected=frames*source.getnchannels()*source.getsampwidth()
            if len(source.readframes(frames))!=expected:raise ValueError('语音音频未完整返回')
    except (wave.Error,EOFError,OSError) as error:
        raise ValueError('语音服务未返回可播放的 PCM WAV 音频') from error
    return data


class VoiceSynthesizer:
    def __init__(self,client_factory=make_client):self.client_factory=client_factory

    async def synthesize(self,cfg,text):
        validate_voice_config(cfg,require_ready=True)
        if not isinstance(text,str) or not text.strip() or len(text)>cfg.max_chars:
            raise ValueError('语音文本长度不符合设置，请使用短回复')
        base=cfg.base_url.rstrip('/')
        if cfg.provider=='fish':
            endpoint=base if base.endswith('/v1/tts') else base+'/tts' if base.endswith('/v1') else base+'/v1/tts'
            headers={'Authorization':'Bearer '+cfg.api_key,'model':cfg.fish_model,'Accept':'audio/wav'}
            payload={'text':text,'reference_id':cfg.model_id,'format':'wav','sample_rate':24000}
        else:
            endpoint=base if base.endswith('/tts') else base+'/tts'
            headers={'Accept':'audio/wav'}
            payload={'text':text,'text_lang':cfg.text_language,'ref_audio_path':cfg.reference_audio,
                     'prompt_text':cfg.reference_text,'prompt_lang':cfg.prompt_language,
                     'media_type':'wav','streaming_mode':False}
        # A single request only. A timeout/429 must not bill an automatic retry.
        async with asyncio.timeout(cfg.timeout_seconds):
            async with self.client_factory(endpoint,timeout=cfg.timeout_seconds) as client:
                async with client.stream('POST',endpoint,json=payload,headers=headers,follow_redirects=False) as response:
                    response.raise_for_status()
                    size=0;chunks=[]
                    length=response.headers.get('content-length','')
                    if length.isdigit() and int(length)>MAX_AUDIO_BYTES:raise ValueError('语音文件超过 8 MiB')
                    async for chunk in response.aiter_bytes():
                        size+=len(chunk)
                        if size>MAX_AUDIO_BYTES:raise ValueError('语音文件超过 8 MiB')
                        chunks.append(chunk)
        return AudioResult(validate_wav(b''.join(chunks)))


class VoiceReplies:
    def __init__(self,path,config,*,synthesizer=None,clock=time.time):
        self.path=Path(path);self.config=config;self.synthesizer=synthesizer or VoiceSynthesizer();self.clock=clock
        self.contacts={};self.attempts={};self.jobs={};self.revisions={};self.seen={}
        self._capacity=asyncio.Semaphore(2);self._preview_busy=False;self.load_error=False
        self.last_status='idle';self.last_error_type='';self._closed=False;self._backoff_until=0
        if self.path.exists():
            try:
                raw=json.loads(self.path.read_text(encoding='utf-8'))
                if raw.get('version')!=1 or not isinstance(raw['contacts'],dict) or not isinstance(raw['attempts'],dict):
                    raise ValueError('invalid voice ledger')
                self.contacts=raw['contacts'];self.attempts=raw['attempts']
                for row in self.contacts.values():
                    if (not isinstance(row,dict) or not isinstance(row.get('muted',False),bool)
                            or isinstance(row.get('last_sent',0),bool) or not isinstance(row.get('last_sent',0),(int,float))
                            or not math.isfinite(row.get('last_sent',0))):raise ValueError('invalid voice contact')
                for row in self.attempts.values():
                    if not isinstance(row,dict) or row.get('status') not in ('synthesizing','sending','api_accepted','failed','cancelled','uncertain'):
                        raise ValueError('invalid voice attempt')
                # Never auto-regenerate/replay interrupted optional audio.
            except (OSError,ValueError,KeyError,TypeError):
                self.load_error=True;self.contacts={};self.attempts={};self.last_status='state_error'

    @staticmethod
    def key(msg):return hashlib.sha256(f'{msg.platform}:{msg.bot_id}:{msg.chat_type}:{msg.chat_id}:{msg.sender_id}'.encode()).hexdigest()
    @staticmethod
    def turn_id(msg):return hashlib.sha256(f'{VoiceReplies.key(msg)}:{msg.msg_id}:{msg.persona_space}'.encode()).hexdigest()
    def signature(self):return hashlib.sha256(json.dumps(asdict(self.config()),sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def _save(self):
        if self.load_error:raise ValueError('voice ledger requires recovery')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        finished=[key for key,row in self.attempts.items() if row['status'] not in ('synthesizing','sending')]
        for key in finished[:-512]:self.attempts.pop(key,None)
        # Bounded metadata only; neither reply text nor credentials is written here.
        while len(self.contacts)>512:
            key=next((key for key,row in self.contacts.items() if key not in self.jobs and not row.get('muted')),None)
            if key is None:break
            self.contacts.pop(key)
        fd,tmp=tempfile.mkstemp(dir=self.path.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as file:
                json.dump({'version':1,'contacts':self.contacts,'attempts':self.attempts},file,ensure_ascii=False)
                file.flush();os.fsync(file.fileno())
            os.chmod(tmp,0o600);os.replace(tmp,self.path)
        finally:Path(tmp).unlink(missing_ok=True)

    def _try_save(self):
        try:self._save()
        except (OSError,ValueError):self.load_error=True;self.last_status='state_error'

    def note_in(self,msg):
        if msg.platform!='ilink' or msg.is_group:return
        key=self.key(msg);seen=self.seen.setdefault(key,[])
        if msg.msg_id and msg.msg_id in seen:return
        seen.append(msg.msg_id);del seen[:-128]
        while len(self.seen)>512:self.seen.pop(next(iter(self.seen)))
        self.revisions[key]=self.revisions.get(key,0)+1
        while len(self.revisions)>1024:
            stale=next((k for k in self.revisions if k not in self.jobs and k!=key),None)
            if stale is None:break
            self.revisions.pop(stale,None)
        job=self.jobs.get(key)
        if job and not job['submitted']:job['task'].cancel()
        if not self.config().enabled:return
        row=self.contacts.setdefault(key,{'muted':False,'last_sent':0})
        if MUTE.fullmatch(msg.content.strip()):row['muted']=True;self._try_save()
        elif REQUEST.match(msg.content.strip()) and row.get('muted'):row['muted']=False;self._try_save()

    def schedule(self,msg,reply,adapter,*,sender=None,check_current=None):
        cfg=self.config()
        if self._closed or not cfg.enabled or self.load_error or msg.platform!='ilink' or msg.is_group or not hasattr(adapter,'send_audio'):
            return False
        if not isinstance(reply,str) or not 2<=len(reply)<=cfg.max_chars or re.search(r'```|https?://|^\s*[#|]',reply):return False
        key=self.key(msg);tid=self.turn_id(msg);now=self.clock()
        if now<self._backoff_until:return False
        row=self.contacts.get(key,{'muted':False,'last_sent':0})
        if row.get('muted') or MUTE.fullmatch(msg.content.strip()) or tid in self.attempts:return False
        forced=bool(REQUEST.match(msg.content.strip()))
        if not forced and now-max(row.get('last_sent',0),row.get('last_attempt',0))<cfg.cooldown_seconds:return False
        chance=int(hashlib.sha256((tid+reply).encode()).hexdigest()[:16],16)/2**64
        if not forced and chance>=cfg.reply_rate:return False
        if key in self.jobs or len(self.jobs)>=4:return False
        try:validate_voice_config(cfg,require_ready=True)
        except ValueError:self.last_status='not_configured';return False
        cfg=type(cfg)(**asdict(cfg))
        self.contacts.setdefault(key,{'muted':False,'last_sent':0})
        self.attempts[tid]={'status':'synthesizing','at':now,'contact':key}
        try:self._save()
        except (ValueError,OSError):self.last_status='state_error';self.load_error=True;return False
        entry={'submitted':False,'revision':self.revisions.get(key,0),'signature':self.signature()}
        self.jobs[key]=entry
        def guard():
            if entry['submitted']:return
            if entry['revision']!=self.revisions.get(key,0) or entry['signature']!=self.signature():raise ConversationChanged('New input/settings invalidated optional audio')
            if check_current:check_current()
        def submitted():entry['submitted']=True;self.attempts[tid]['status']='sending';self._try_save()
        async def run():
            try:
                async with self._capacity:
                    guard();self.last_status='synthesizing'
                    self.contacts[key]['last_attempt']=self.clock()
                    self._save()  # Reserve ordinary cooldown before a potentially billed call.
                    result=await self.synthesizer.synthesize(cfg,reply)
                    guard()
                    if sender is not None:
                        await sender.global_bucket.acquire();guard()
                        await sender.chat_buckets[msg.chat_id].acquire();guard()
                    self.last_status='sending'
                    await adapter.send_audio(msg.chat_id,result.data,filename=result.filename,mime_type=result.mime_type,
                                             attempt_id=tid,check_current=guard,on_submit=submitted)
                    self.attempts[tid]['status']='api_accepted';self.contacts[key]['last_sent']=self.clock()
                    self.last_status='api_accepted';self.last_error_type=''
            except asyncio.CancelledError:
                self.attempts[tid]['status']='uncertain' if entry['submitted'] else 'cancelled';self.last_status=self.attempts[tid]['status']
            except ConversationChanged:
                self.attempts[tid]['status']='cancelled';self.last_status='cancelled'
            except Exception as error:
                if not entry['submitted']:self._backoff_until=self.clock()+60
                self.attempts[tid]['status']='uncertain' if entry['submitted'] else 'failed'
                self.last_status=self.attempts[tid]['status'];self.last_error_type=type(error).__name__
                log.warning('Optional voice ended error_type=%s; text reply remains accepted',type(error).__name__)
            finally:
                if not entry['submitted'] and hasattr(adapter,'discard_audio'):
                    try:await adapter.discard_audio(tid)
                    except Exception:pass
                self._try_save()
                if self.jobs.get(key) is entry:self.jobs.pop(key,None)
        entry['task']=asyncio.create_task(run())
        def finished(task):
            # Cancellation can happen before Python enters the coroutine's try/finally.
            if self.jobs.get(key) is entry:
                self.attempts[tid]['status']='uncertain' if entry['submitted'] else 'cancelled'
                self.jobs.pop(key,None);self._try_save()
            if not task.cancelled():task.exception()
        entry['task'].add_done_callback(finished)
        return True

    def cancel_pending(self):
        for entry in list(self.jobs.values()):
            if not entry['submitted']:entry['task'].cancel()

    async def wait_idle(self):
        await asyncio.gather(*(job['task'] for job in list(self.jobs.values())),return_exceptions=True)

    def start(self):self._closed=False

    async def close(self):
        self._closed=True
        for job in list(self.jobs.values()):job['task'].cancel()
        await self.wait_idle()
        # start() can reopen the optional scheduler when the bot is restarted.

    async def preview(self,text):
        if self._preview_busy:raise ValueError('已有试听正在生成，请稍候')
        if not isinstance(text,str) or len(text)>min(160,self.config().max_chars):raise ValueError('试听请使用不超过160字的短文本')
        self._preview_busy=True
        try:return await self.synthesizer.synthesize(self.config(),text)
        finally:self._preview_busy=False

    def status(self):
        cfg=self.config()
        try:validate_voice_config(cfg,require_ready=True);configured=True
        except (ValueError,TypeError,AttributeError):configured=False
        return {'enabled':cfg.enabled,'provider':cfg.provider,'configured':configured,
                'delivery':'audio_file','state':self.last_status,'state_error':self.load_error,
                'last_error_type':self.last_error_type,'pending':len(self.jobs),
                'accepted':sum(row['status']=='api_accepted' for row in self.attempts.values()),
                'backoff_seconds':max(0,round(self._backoff_until-self.clock())),
                'client_playback_verified':False}
