"""官方 iLink Bot API 适配器（微信 ClawBot / OpenClaw 微信渠道）。

═══════════════════════════════════════════════════════════════════
本项目对接腾讯开放的 iLink 协议；账号和服务状态以平台实际响应为准。
来源：npm `@tencent-weixin/openclaw-weixin`（作者 Tencent，MIT）
      官方仓库 github.com/Tencent/openclaw-weixin
═══════════════════════════════════════════════════════════════════

⚠️ 能力边界（已通过源码验证，2026-10-12）：
   capabilities.chatTypes = ["direct"]   ← 只支持一对一私聊
   inbound.ts 硬编码 ChatType = "direct"
   CHANGELOG 全量 26 个版本从未提及群聊
   → 官方协议不支持群聊。本项目的微信通道因此只有私聊；
     QQ 群聊走 OneBot 适配器。

⚠️ 三个 id 的含义（对照官方实现 login-qr.js / monitor.js 核验，2026-10-13）：

   bot_token     形如 `<ilink_bot_id>@im.bot:<secret>` —— 机器人自己的身份
   ilink_bot_id  **机器人**的 id（官方代码里叫 accountId），@im.bot 命名空间
   ilink_user_id **扫码那个人**的 id（官方代码里存成 userId），@im.wechat 命名空间

   ⚠️ 踩过的坑：早期把 ilink_user_id 当成"机器人自己的 id"存进 bot_user_id，
   于是用户自己发的消息因为 `sender_id == bot_id` 被判定为"机器人自身消息"，
   表现就是"微信发消息机器人完全不理"。判断自发消息只能用 @im.bot 那个 id。

协议特征：
   · HTTP/JSON，无 SDK 依赖，可直接 fetch
   · 长轮询收消息（服务端 hold 最多 35 秒），类似 Telegram getUpdates
   · context_token 是回复的关键 —— 必须原样带回，否则关联不到会话
   · 媒体文件走 CDN（AES-128-ECB 加密）
   · 支持"正在输入"状态（sendtyping）—— 这是官方协议独有的体验优势
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import os
import random
import struct
import tempfile
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from ..core.http import explain_error, make_client
from ..core.llm import is_transient_llm_error
from ..core.turn_taking import plan_wait,ready
from ..core.types import WxMessage
from ..core.conversation import ConversationChanged,merge_messages,MAX_BATCH
from .base import BaseAdapter, MessageHandler

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "1.3.4"
ILINK_APP_ID = "bot"
MAX_PENDING = 200
MAX_ACTIVE_CHATS = 4

# proto: MessageType
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2

# proto: MessageState
STATE_NEW = 0
STATE_GENERATING = 1
STATE_FINISH = 2

# proto: MessageItemType
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# proto: TypingStatus
TYPING_ON = 1
TYPING_OFF = 2

# token 失效错误码
STALE_TOKEN_ERRCODE = -14


def derive_bot_id(bot_token: Optional[str]) -> str:
    """从 bot_token 推出机器人自己的 id。

    token 形如 `b046afd6fcae@im.bot:0600008f53...`，
    冒号前那一段就是机器人的 id（@im.bot 命名空间）。
    这是官方实现里 `ilink_bot_id` 的等价物，也是旧凭证唯一的补救来源。
    """
    if not bot_token:
        return ""
    head = bot_token.split(":", 1)[0].strip()
    if head.endswith("@im.wechat"):        # 用户命名空间 → 不是机器人 id
        return ""
    return head if "@im.bot" in head else ""


class ILinkAdapter(BaseAdapter):
    """腾讯官方 iLink Bot API 适配器。"""

    platform = "ilink"

    def __init__(self, state_dir: str = "./data/ilink", bot_agent: str = "OpenClaw",
                 base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.bot_agent = bot_agent
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.bot_token: Optional[str] = None
        self.bot_base_url: Optional[str] = None
        # 机器人自己的 id（@im.bot 命名空间）—— 只用于识别"自己发的消息"
        self.bot_id: Optional[str] = None
        # 扫码绑定这个机器人的用户 id（@im.wechat 命名空间）—— 仅作展示/排查
        self.owner_user_id: Optional[str] = None
        self.typing_ticket: Optional[str] = None

        self._get_updates_buf: str = ""
        self._pending: list[dict] = []
        self.last_error = ""
        # 每个会话最近一条入站消息的 context_token —— 回复时必须原样带回
        self._ctx_tokens: dict[str, str] = {}
        # 官方域名是公网地址，走系统代理没问题；make_client 会自动判断
        self._client = make_client(self.base_url, timeout=40.0)
        self._poll_client = None
        self._reply_context: ContextVar = ContextVar("ilink_reply_context", default=None)
        self._outbox: dict[str, dict] = {}
        self._wakeup = asyncio.Event()
        self._capacity_changed = asyncio.Event()
        self._jobs: dict[str, asyncio.Task] = {}
        self._retry_after: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._delivery = {"attempts": 0, "accepted": 0, "failed": 0,
                          "last_status": "idle", "last_ret": None,
                          "last_errcode": None, "last_send_ms": 0, "network_retries": 0, "last_model_error_type": ""}
        self._handler: Optional[MessageHandler] = None
        self.humanized_wait=True;self.pause_timeout=120
        self._collecting=set();self._processing_capacity=asyncio.Semaphore(MAX_ACTIVE_CHATS)
        self._typing_tickets={}
        self._typing_active={}
        self._typing_status={'starts':0,'stops':0,'failed':0,'last_status':'idle','last_error_type':''}
        self.message_merge_window=0.0
        self.message_merge_max_wait=10.0
        self._running = False
        self._startup_task: Optional[asyncio.Task] = None

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        if not self._load_token():
            raise RuntimeError(
                "未找到已保存的凭证，请先调用 login() 扫码登录"
            )
        # 旧凭证里没有 bot_id（或误存成了用户 id）时，从 token 前缀补出来。
        # token 形如 `<ilink_bot_id>@im.bot:<secret>`，这是唯一可靠的来源。
        if not self.bot_id:
            self.bot_id = derive_bot_id(self.bot_token)
            if self.bot_id:
                log.info("从 bot_token 推出机器人 id：%s", self.bot_id)
        self._running = True
        # 在线上报/输入状态配置是辅助信息，不阻塞开始收件。
        self._startup_task = asyncio.create_task(self._startup_metadata())
        log.info("ILinkAdapter started, bot_id=%s owner=%s",
                 self.bot_id or "?", self.owner_user_id or "?")

    async def _startup_metadata(self) -> None:
        await self._notify("notifystart")
        await self._refresh_config()

    async def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        self._capacity_changed.set()
        if self._startup_task is not None:
            self._startup_task.cancel()
            await asyncio.gather(self._startup_task, return_exceptions=True)
            self._startup_task = None
        try:
            await self._notify("notifystop")
        except Exception:
            log.warning("notify stop failed", exc_info=True)
        await self._client.aclose()
        if self._poll_client is not None:
            await self._poll_client.aclose();self._poll_client=None

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def is_healthy(self) -> bool:
        return bool(self.bot_token) and self._running

    # ── 扫码登录 ──────────────────────────────────────────────

    async def login(self, poll_interval: float = 1.0, timeout: float = 300.0) -> dict:
        """扫码登录流程。

        ① GET  get_bot_qrcode  → 拿二维码
        ② 用户用微信扫码
        ③ GET  get_qrcode_status（轮询）→ 拿 bot_token
        ④ 持久化凭证
        """
        r = await self._client.get(
            f"{self.base_url}/ilink/bot/get_bot_qrcode", params={"bot_type": 3}
        )
        r.raise_for_status()
        data = r.json()
        qrcode = data.get("qrcode")
        img = data.get("qrcode_img_content")
        log.info("请用微信扫描二维码完成登录：%s", img)
        print(f"\n请用微信扫码登录（二维码链接）：\n{img}\n")

        waited = 0.0
        while waited < timeout:
            s = await self._client.get(
                f"{self.base_url}/ilink/bot/get_qrcode_status", params={"qrcode": qrcode}
            )
            s.raise_for_status()
            st = s.json()
            status = st.get("status")

            if status == "confirmed":
                self.bot_token = st.get("bot_token")
                self.bot_base_url = st.get("baseurl") or self.base_url
                # 官方实现里：accountId = ilink_bot_id（机器人），
                # userId = ilink_user_id（扫码的人）。两者绝不能混用。
                self.bot_id = (st.get("ilink_bot_id")
                               or derive_bot_id(self.bot_token))
                self.owner_user_id = (st.get("ilink_user_id")
                                      or st.get("bot_user_id") or "")
                self._save_token()
                log.info("登录成功，凭证已保存到 %s（bot_id=%s）",
                         self.state_dir, self.bot_id or "?")
                return st
            if status == "binded_redirect":
                # 服务端表示"已绑定过本端"，本地凭证仍然有效
                log.info("该账号已连接过，无需重复连接")
                self._load_token()
                return st
            if status in ("expired", "cancelled"):
                raise RuntimeError(f"二维码已失效或被取消：{status}")

            await asyncio.sleep(poll_interval)
            waited += poll_interval

        raise TimeoutError("扫码登录超时")

    # ── 消息接收：长轮询主循环 ────────────────────────────────

    async def poll_loop(self) -> None:
        """收件与处理独立运行；每个会话有序，不因生成/重试停止收件。"""
        receiver = asyncio.create_task(self._receive_loop())
        dispatcher = asyncio.create_task(self._dispatch_loop())
        try:
            await asyncio.gather(receiver, dispatcher)
        finally:
            receiver.cancel()
            dispatcher.cancel()
            for job in self._jobs.values():
                job.cancel()
            await asyncio.gather(receiver, dispatcher, *self._jobs.values(),
                                 return_exceptions=True)
            self._jobs.clear()

    @staticmethod
    def _raw_key(raw: dict) -> str:
        mid = raw.get("message_id") or raw.get("client_id")
        if not mid:
            mid = hashlib.sha256(json.dumps({k:v for k,v in raw.items() if not k.startswith("_wxbot_")}, sort_keys=True).encode()).hexdigest()
        return str(raw.get("from_user_id", "")) + ":" + str(mid)

    @staticmethod
    def _check_response(resp: dict, operation: str) -> None:
        if not isinstance(resp, dict):
            raise RuntimeError(f"{operation}: invalid JSON response")
        for field in ("ret", "errcode"):
            value = resp.get(field)
            if value is not None and str(value) != "0":
                raise RuntimeError(f"{operation}: {field}={value}")

    async def _receive_loop(self) -> None:
        while self._running:
            # 有界收件箱：处理跟不上时暂停下一次拉取，而不是丢弃已收到的批次。
            if len(self._pending) >= MAX_PENDING:
                self._capacity_changed.clear()
                await self._capacity_changed.wait()
                continue
            started = time.monotonic()
            try:
                resp = await self._post("ilink/bot/getupdates", {
                    "get_updates_buf": self._get_updates_buf,
                    "base_info": self._base_info(),
                })
                if str(resp.get("errcode")) == str(STALE_TOKEN_ERRCODE):
                    self.last_error = "微信凭证已失效，请重新扫码登录"
                    self._running = False
                    self._wakeup.set()
                    return
                self._check_response(resp, "getupdates")
            except httpx.TimeoutException:
                continue  # 真正的长轮询超时立即重连。
            except Exception:
                log.exception("getupdates 失败，将短暂退避后重试")
                await asyncio.sleep(0.5)
                continue
            msgs = resp.get("msgs") or []
            existing = {self._raw_key(raw) for raw in self._pending}
            for raw in msgs:
                key = self._raw_key(raw)
                if key not in existing:
                    raw["_wxbot_received_at"]=time.time()
                    self._pending.append(raw)
                    existing.add(key)
                    msg = self._normalize(raw)
                    if msg is not None:
                        self.remember_context(msg)
                        self.note_activity(msg)
            if resp.get("get_updates_buf"):
                self._get_updates_buf = resp["get_updates_buf"]
            self._save_cursor()  # 游标和全部未处理消息一并提交，不覆盖在途消息。
            self._wakeup.set()
            # 防止网关异常地立即返回空批次时热循环；正常长轮询不额外等待。
            if not msgs and time.monotonic() - started < 0.05:
                await asyncio.sleep(0.05)
            else:
                await asyncio.sleep(0)

    async def _dispatch_loop(self) -> None:
        while self._running:
            self._wakeup.clear()
            for peer, job in list(self._jobs.items()):
                if job.done():
                    if job.cancelled():
                        raise asyncio.CancelledError()
                    job.result()
                    del self._jobs[peer]
            visited: set[str] = set()
            now = time.monotonic()
            for raw in list(self._pending):
                peer = str(raw.get("from_user_id") or "unknown")
                if peer in visited:
                    continue
                visited.add(peer)  # 重试中的首条消息也占住该会话顺序。
                if (peer in self._jobs or (len(self._jobs) >= (MAX_PENDING if self.message_merge_window else MAX_ACTIVE_CHATS))
                        or self._retry_after.get(peer, 0) > now):
                    continue
                self._collecting.add(peer)
                self._jobs[peer] = asyncio.create_task(self._process_pending(raw, peer))
            delays = [max(0.01, at - time.monotonic())
                      for peer, at in self._retry_after.items()
                      if at > time.monotonic() and peer not in self._jobs]
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=min(delays) if delays else 5.0)
            except asyncio.TimeoutError:
                pass

    def configure_burst(self,window,max_wait,human=True,pause_timeout=120,**kwargs):
        self.humanized_wait=human;self.pause_timeout=pause_timeout
        self.message_merge_window=float(window);self.message_merge_max_wait=max(float(max_wait),float(window))

    def _peer_pending(self,raw):
        return [r for r in self._pending if r.get('from_user_id')==raw.get('from_user_id')
                and r.get('group_id')==raw.get('group_id')]

    async def _batch_raw(self,raw):
        if not self.message_merge_window:return [raw]
        keys=raw.get('_wxbot_batch_keys')
        if keys:return [r for r in self._pending if self._raw_key(r) in keys]
        raw.setdefault('_wxbot_received_at',time.time())
        while True:
            candidates=self._peer_pending(raw)
            for r in candidates:r.setdefault('_wxbot_received_at',time.time())
            now=time.time()
            timed=[(m,r['_wxbot_received_at']) for r in candidates if (m:=self._normalize(r)) is not None]
            if not timed:break
            plan=plan_wait(timed,self.message_merge_window,self.message_merge_max_wait,self.humanized_wait,self.pause_timeout)
            is_ready,force=ready(plan,now,timed[-1][1],len(timed))
            if is_ready:break
            await asyncio.sleep(min(.05,self.message_merge_window))
        force=locals().get('force',False)
        selected=candidates[:MAX_BATCH]
        raw['_wxbot_batch_keys']=[self._raw_key(r) for r in selected]
        raw['_wxbot_batch_known']=[self._raw_key(r) for r in candidates]
        raw['_wxbot_batch_force']=force
        try:self._save_cursor()
        except BaseException:
            for key in ('_wxbot_batch_keys','_wxbot_batch_known','_wxbot_batch_force'):raw.pop(key,None)
            raise
        return selected

    def check_reply_current(self,msg,*,after_failure=False):
        if not self.message_merge_window:return
        raw=next((r for r in self._pending if str(r.get('message_id') or r.get('client_id') or self._raw_key(r))==msg.msg_id
                  and r.get('from_user_id')==msg.sender_id),None)
        if raw and raw.get('_wxbot_batch_keys') and (after_failure or not raw.get('_wxbot_batch_force')):
            known=set(raw['_wxbot_batch_known'])
            if any(self._raw_key(r) not in known for r in self._peer_pending(raw)):
                raise ConversationChanged('New conversation input')

    def check_failed_reply_current(self,msg):
        # A max-wait batch may finish a useful answer, but an error placeholder
        # must never jump ahead of newer media that can make the turn succeed.
        self.check_reply_current(msg,after_failure=True)

    async def _handle_raw(self, raw: dict) -> bool:
        selected=await self._batch_raw(raw)
        self._collecting.discard(str(raw.get("from_user_id") or "unknown"))
        messages=[m for r in selected if (m:=self._normalize(r)) is not None]
        msg=(merge_messages(messages) if self.message_merge_window else messages[0]) if messages else None
        if msg is not None:
            if self._handler is None:return False
            self.remember_context(msg)
            async with self._processing_capacity:
                self.check_reply_current(msg)
                with self.bind_reply_context(msg):await self._handler(msg)
        before=list(self._pending)
        for r in selected:self._pending.remove(r)
        try:self._save_cursor()
        except BaseException:self._pending[:]=before;raise
        self._capacity_changed.set()
        return True

    async def _process_pending(self, raw: dict, peer: str) -> None:
        try:
            if await self._handle_raw(raw):
                self._failures.pop(peer, None)
                self._retry_after.pop(peer, None)
            else:
                self._retry_after[peer] = time.monotonic() + 0.5
        except ConversationChanged:
            # A prepared network draft is never reshaped; only unsent generations regroup.
            for key in ('_wxbot_batch_keys','_wxbot_batch_known','_wxbot_batch_force'):raw.pop(key,None)
            self._save_cursor()
            self._retry_after.pop(peer,None)
        except Exception:
            failures = self._failures.get(peer, 0) + 1
            self._failures[peer] = failures
            self._retry_after[peer] = time.monotonic() + min(8.0, 0.5 * 2 ** min(failures - 1, 4))
            log.exception("处理消息失败，将保留收件与完整回复草稿后重试")
        finally:
            self._collecting.discard(peer)
            self._wakeup.set()

    async def _drain_pending(self) -> bool:
        """保留顺序恢复接口，实际轮询使用独立的有界分会话调度。"""
        while self._pending:
            try:
                if not await self._handle_raw(self._pending[0]):
                    return False
            except Exception:
                log.exception("处理消息失败，将保留待重试")
                return False
        return True

    @contextmanager
    def bind_reply_context(self, msg: WxMessage):
        token = self._reply_context.set(msg)
        try:
            yield
        finally:
            self._reply_context.reset(token)

    def _reply_key(self, msg: WxMessage) -> str:
        mid = msg.msg_id or hashlib.sha256(
            f"{msg.sender_id}:{msg.timestamp}:{msg.content}".encode()).hexdigest()
        return f"{self.bot_id or ''}:{msg.chat_id}:{mid}"

    def get_reply_draft(self, msg: WxMessage) -> Optional[str]:
        draft = self._outbox.get(self._reply_key(msg))
        return draft["text"] if draft else None

    def validate_reply_context(self, msg: WxMessage) -> None:
        if not (msg.context_token or self._ctx_tokens.get(msg.chat_id)):
            self._delivery.update(last_status="missing_context")
            raise RuntimeError("sendmessage: missing context_token")

    def mark_generation_pending(self, error):
        self._delivery.update(last_status='generation_retry_pending',last_model_error_type=type(error).__name__)

    def typing_cleanup_required(self, chat_id):
        return bool(self._typing_active.get(chat_id))

    def delivery_status(self) -> dict:
        return {**self._delivery, "typing":dict(self._typing_status), "pending_inbound": len(self._pending),
                "active_chats": len(self._jobs)-len(self._collecting), "collecting_chats":len(self._collecting),
                "pending_replies": sum(d.get("status") != "api_accepted"
                                       for d in self._outbox.values()),
                "confirmation": "API acceptance only; no client read receipt"}

    # ── 消息归一化 ────────────────────────────────────────────

    def _normalize(self, raw: dict) -> Optional[WxMessage]:
        """把官方协议消息转成统一的 WxMessage。"""
        try:
            # 只处理用户发来的消息（message_type=1），忽略 BOT 自己的回显
            if raw.get("message_type") != MSG_TYPE_USER:
                return None

            items = raw.get("item_list") or []
            texts=[];images=[];parts=[];msg_type='text'
            for it in items:
                t=it.get('type')
                if t==ITEM_TEXT:
                    text=str((it.get('text_item') or {}).get('text') or '');texts.append(text);parts.append({'type':'text','text':text})
                elif t==ITEM_IMAGE:
                    img=it.get('image_item') or {};media=img.get('media') or {}
                    parts.append({'type':'image','index':len(images)})
                    images.append({'url':media.get('full_url',''),'encrypt_query_param':media.get('encrypt_query_param',''),
                                   'aes_key':media.get('aes_key',''),'key_hex':img.get('aeskey','')})
                    texts.append('[图片]');msg_type='image'
                elif t==ITEM_VOICE:
                    voice=it.get('voice_item') or {};text=str(voice.get('text') or voice.get('transcript') or '[语音]');texts.append(text);parts.append({'type':'text','text':text})
                    if not images:msg_type='voice'
                elif t in (ITEM_FILE,ITEM_VIDEO):
                    text='[文件]' if t==ITEM_FILE else '[视频]';texts.append(text);parts.append({'type':'video' if t==ITEM_VIDEO else 'text','text':text})
                    if not images:msg_type='video' if t==ITEM_VIDEO else 'file'
            text=''.join(texts)

            from_user = raw.get("from_user_id") or ""
            group_id = raw.get("group_id")

            return WxMessage(
                msg_id=str(raw.get("message_id") or raw.get("client_id") or self._raw_key(raw)),
                platform=self.platform,
                # 官方协议当前只支持私聊（capabilities.chatTypes=["direct"]）
                chat_type="group" if group_id else "private",
                chat_id=from_user,
                sender_id=from_user,
                sender_name=from_user.split("@")[0] if from_user else "",
                # ⚠️ 必须是机器人自己的 id：用扫码人的 id 会把用户消息误判成自发消息
                bot_id=self.bot_id or "",
                content=text.strip(),
                raw_content=text,
                msg_type=msg_type, images=images, content_parts=parts,  # type: ignore[arg-type]
                timestamp=int(raw.get("create_time_ms") or 0),
                is_at_me=False,     # 私聊无需 @
                context_token=raw.get("context_token"),
                seq=raw.get("seq"),
                session_id=raw.get("session_id"),
                group_id=group_id,
            )
        except Exception:
            log.exception("归一化失败：%s", raw)
            return None

    # ── 消息发送 ──────────────────────────────────────────────

    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str:
        """持久化完整回复，并以固定 client_id 提交；失败不当作已回复。"""
        return await self._send_text(chat_id, text)

    async def send_proactive(self, chat_id: str, text: str, attempt_id: str, context_token: str) -> str:
        return await self._send_text(chat_id, text, proactive=(attempt_id, context_token))

    async def _send_text(self, chat_id: str, text: str, proactive=None) -> str:
        bound = None if proactive else self._reply_context.get()
        key = ("proactive:" + proactive[0] if proactive else
               self._reply_key(bound) if bound is not None else "manual:" + uuid.uuid4().hex)
        draft = self._outbox.get(key)
        if draft is None:
            if bound is not None:self.check_reply_current(bound)
            ctx_token = (proactive[1] if proactive else (bound.context_token if bound is not None else None)) or self._ctx_tokens.get(chat_id)
            if not ctx_token:
                self._delivery.update(last_status="missing_context", failed=self._delivery["failed"] + 1)
                raise RuntimeError("sendmessage: missing context_token")
            draft = {"to": chat_id, "text": text, "context_token": ctx_token,
                     "client_id": "wxbot-" + uuid.uuid4().hex, "status": "pending",
                     "created": time.time()}
            self._outbox[key] = draft
            self._save_outbox()  # 先落盘再发，重试与重启都复用相同正文/ID。
        if draft.get("status") == "api_accepted":
            self._save_outbox()
            return draft.get("server_message_id") or draft["client_id"]
        self._save_outbox()  # 也涵盖上次落盘失败的重试。
        started = time.monotonic()
        self._delivery.update(last_ret=None, last_errcode=None)
        self._delivery["attempts"] += 1
        try:
            resp = await self._post("ilink/bot/sendmessage", {
                "msg": {"from_user_id": "", "to_user_id": draft["to"],
                        "client_id": draft["client_id"], "message_type": MSG_TYPE_BOT,
                        "message_state": STATE_FINISH, "context_token": draft["context_token"],
                        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": draft["text"]}}]},
                "base_info": self._base_info(),
            })
            self._delivery.update(last_ret=resp.get("ret"), last_errcode=resp.get("errcode"))
            self._check_response(resp, "sendmessage")
            if not any(field in resp for field in ("ret", "errcode", "message_id")):
                raise RuntimeError("sendmessage: missing acknowledgement fields")
            draft.update(status="api_accepted", server_message_id=str(resp.get("message_id") or ""))
            self._save_outbox()
            self._delivery.update(last_status="api_accepted", last_model_error_type="", accepted=self._delivery["accepted"] + 1)
            log.info("微信回复接口已接受 client_id=%s server_id_present=%s",
                     draft["client_id"], bool(draft["server_message_id"]))
            return draft["server_message_id"] or draft["client_id"]
        except Exception:
            self._delivery.update(last_status="retry_pending", failed=self._delivery["failed"] + 1)
            if str(self._delivery.get("last_errcode")) == str(STALE_TOKEN_ERRCODE):
                self.last_error = "微信凭证已失效，请重新扫码登录"
                self._running = False
                self._wakeup.set()
            raise
        finally:
            self._delivery["last_send_ms"] = round((time.monotonic() - started) * 1000, 1)

    def _save_outbox(self) -> None:
        # 限制已确认记录数量；任何未确认回复都不被清理。
        acknowledged = [key for key, d in self._outbox.items() if d.get("status") == "api_accepted"]
        for key in acknowledged[:-512]:
            del self._outbox[key]
        self._atomic_json(self.state_dir / "outbox.json", {"version": 1, "replies": self._outbox})

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def send_typing(self, chat_id: str, on: bool = True) -> bool:
        """Peer-specific tickets; auxiliary calls never block the reply pipeline."""
        try:
            cached = self._typing_tickets.get(chat_id)
            if not on:
                # Cancel must never fetch a new ticket or wait for a config request.
                ticket = self._typing_active.get(chat_id) or (cached[0] if cached else '')
                if not ticket:return False
            else:
                if cached is None or cached[1] < time.time():
                    bound = self._reply_context.get()
                    context = (bound.context_token if bound is not None and bound.chat_id==chat_id else '') or self._ctx_tokens.get(chat_id,'')
                    response = await self._post('ilink/bot/getconfig',{'ilink_user_id':chat_id,
                        'context_token':context,'base_info':self._base_info()})
                    self._check_response(response,'getconfig')
                    cached = (response.get('typing_ticket') or '',time.time()+60)
                    self._typing_tickets[chat_id] = cached
                ticket = cached[0]
                if not ticket:
                    self._typing_status.update(last_status='no_ticket')
                    return False
                self._typing_active[chat_id] = ticket  # may be accepted even if the response times out
            response = await self._post('ilink/bot/sendtyping',{'ilink_user_id':chat_id,'typing_ticket':ticket,
                'status':TYPING_ON if on else TYPING_OFF,'base_info':self._base_info()})
            self._check_response(response,'sendtyping')
            self._typing_status['starts' if on else 'stops'] += 1
            self._typing_status.update(last_status='on' if on else 'off',last_error_type='')
            if not on:self._typing_active.pop(chat_id,None)
            return True
        except asyncio.CancelledError:
            self._typing_status.update(last_status='interrupted' if on else 'cleanup_pending',last_error_type='CancelledError')
            raise
        except Exception as error:
            self._typing_tickets.pop(chat_id,None)  # allow a stale ticket to refresh on the next attempt
            self._typing_status['failed'] += 1
            self._typing_status.update(last_status='failed',last_error_type=type(error).__name__)
            log.debug('Typing hint unavailable error_type=%s',type(error).__name__)
            return False

    async def send_image(self, chat_id: str, path_or_url: str) -> str:
        """发送图片（走 CDN，AES-128-ECB 加密）。

        完整流程：生成随机 AES-128 key → 加密文件 → getuploadurl 拿预签名 URL
        → PUT 到 CDN → sendmessage 带 aes_key 与 CDN 引用。
        此处仅留接口，实现见 cdn/ 目录设计。
        """
        raise NotImplementedError("图片上传需实现 CDN AES-128-ECB 加密链路")

    # ── 内部：HTTP 与凭证 ─────────────────────────────────────

    def _base_info(self) -> dict:
        return {"channel_version": CHANNEL_VERSION, "bot_agent": self.bot_agent}

    @staticmethod
    def _random_uin() -> str:
        """X-WECHAT-UIN：随机 uint32 → 十进制字符串 → base64。防重放。"""
        n = random.getrandbits(32)
        return base64.b64encode(str(n).encode()).decode()

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str((1 << 16) | (3 << 8) | 4),
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self._random_uin(),
        }
        if self.bot_token:
            h["Authorization"] = f"Bearer {self.bot_token}"
        return h

    async def _post(self, endpoint: str, body: dict) -> dict:
        base = (self.bot_base_url or self.base_url).rstrip('/')
        url = base + '/' + endpoint
        payload = {**body, 'base_info': self._base_info()}
        polling = endpoint.endswith('/getupdates')
        if polling:
            if self._poll_client is None:self._poll_client = make_client(base,timeout=40)
            client = self._poll_client
            timeout = httpx.Timeout(40,connect=8,pool=2)
        else:
            client = self._client
            control = endpoint.endswith(('/getconfig','/sendtyping','/notifystart','/notifystop'))
            timeout = httpx.Timeout(2.5,connect=1.5,pool=.5) if control else httpx.Timeout(15,connect=3,pool=2)
        try:
            response = await client.post(url,json=payload,headers=self._headers(),timeout=timeout)
        except (httpx.TimeoutException,httpx.NetworkError,httpx.RemoteProtocolError) as error:
            if not endpoint.endswith('/sendmessage'):raise
            # Do not reset the shared pool: other peers may have requests in flight.
            # A fresh, short-lived retry uses the exact persisted client_id/context/text.
            self._delivery['network_retries'] += 1
            log.warning('Wechat send connection retry error_type=%s',type(error).__name__)
            await asyncio.sleep(.25)
            async with make_client(base,timeout=15) as retry_client:
                response = await retry_client.post(url,json=payload,headers=self._headers(),timeout=timeout)
        response.raise_for_status()
        return response.json()

    async def _notify(self, which: str) -> None:
        """上报告知服务端本端启动/停止，便于服务端做在线状态对账。"""
        try:
            await self._post(f"ilink/bot/msg/{which}", {"base_info": self._base_info()})
        except Exception:
            log.debug("%s 上报失败", which, exc_info=True)

    async def _refresh_config(self) -> None:
        """获取 typing_ticket。"""
        try:
            resp = await self._post("ilink/bot/getconfig", {
                "ilink_user_id": self.owner_user_id or "",
                "base_info": self._base_info(),
            })
            self._check_response(resp,'getconfig')
            self.typing_ticket = resp.get("typing_ticket")
            if self.owner_user_id and self.typing_ticket:
                self._typing_tickets[self.owner_user_id]=(self.typing_ticket,time.time()+60)
        except Exception:
            log.debug("getconfig 失败", exc_info=True)

    # ── 凭证与游标持久化 ──────────────────────────────────────

    @property
    def _token_file(self) -> Path:
        return self.state_dir / "account.json"

    @property
    def _cursor_file(self) -> Path:
        return self.state_dir / "cursor.json"

    def _save_token(self) -> None:
        self._token_file.write_text(json.dumps({
            "bot_token": self.bot_token,
            "base_url": self.bot_base_url,
            "bot_id": self.bot_id or derive_bot_id(self.bot_token),
            "owner_user_id": self.owner_user_id,
            # 旧字段，留着只为回读旧文件（语义是 owner，不是 bot）
            "bot_user_id": self.owner_user_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(self._token_file, 0o600)   # 凭证文件收紧权限

    def _load_token(self) -> bool:
        if not self._token_file.exists():
            return False
        try:
            d = json.loads(self._token_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        self.bot_token = d.get("bot_token")
        self.bot_base_url = d.get("base_url")
        self.owner_user_id = d.get("owner_user_id") or d.get("bot_user_id") or ""
        # 旧版本的 bot_user_id 里存的其实是扫码人的 id（@im.wechat），
        # 不能拿它当机器人 id，否则用户消息会被判成自发消息。
        bot_id = d.get("bot_id") or ""
        if bot_id.endswith("@im.wechat"):
            bot_id = ""
        self.bot_id = bot_id or derive_bot_id(self.bot_token)
        if self._cursor_file.exists():
            try:
                checkpoint = json.loads(self._cursor_file.read_text(encoding="utf-8"))
                self._get_updates_buf = checkpoint.get("get_updates_buf", "")
                self._pending = checkpoint.get("pending") or []
            except json.JSONDecodeError:
                pass
        outbox = self.state_dir / "outbox.json"
        if outbox.exists():
            self._outbox = json.loads(outbox.read_text(encoding="utf-8")).get("replies") or {}
        return bool(self.bot_token)

    def _save_cursor(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"get_updates_buf": self._get_updates_buf,
                           "pending": self._pending}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._cursor_file)
        finally:
            Path(tmp).unlink(missing_ok=True)

    # ── context_token 缓存（发送时必填）───────────────────────

    def remember_context(self, msg: WxMessage) -> None:
        """记录每个会话最近的 context_token，供回复时使用。"""
        if msg.context_token:
            self._ctx_tokens[msg.chat_id] = msg.context_token
