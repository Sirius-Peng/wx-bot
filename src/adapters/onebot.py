"""OneBot 11 适配器 —— 一个适配器支持所有 QQ 机器人实现。

═══════════════════════════════════════════════════════════════════
为什么只写一个适配器就够？

QQ 生态与微信最大的不同：**存在统一的标准协议 OneBot 11**。
NapCat、Lagrange、LLOneBot 等都实现了它，因此对接标准协议即可通吃，
用户换实现不用改代码。

（微信那边每个实现各写各的：GeWe 一套 REST、iLink 一套 HTTP、WCF 一套 gRPC，
所以只能一个适配器对一种协议。）

协议文档：https://11.onebot.dev

═══════════════════════════════════════════════════════════════════
通信方式选择：**反向 WebSocket（收） + HTTP API（发）**

  · 反向 WS：网关主动连到我们的 /ws/onebot，无需暴露额外端口，
    网关自带重连；同机部署延迟最低
  · HTTP API：发送时调网关的 HTTP 接口，比在 WS 上做 echo 关联更简单可靠

同时兼容 HTTP POST 上报（网关推事件到 /onebot/event），
方便用户按自己的部署习惯选择。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import re
import html
import json
import logging
import time
from typing import Any, Optional

import httpx

from ..core.http import explain_error, make_client
from ..core.types import WxMessage
from ..core.conversation import MessageBuffer
from ..core.media import MediaError
from .base import BaseAdapter, MessageHandler

log = logging.getLogger(__name__)

# chat_id 约定：私聊 p_<qq>，群聊 g_<群号>
# 这样从 chat_id 本身就能看出会话类型，发送时无需额外查表
PRIVATE_PREFIX = "p_"
GROUP_PREFIX = "g_"


def make_chat_id(chat_type: str, target: str | int) -> str:
    return (GROUP_PREFIX if chat_type == "group" else PRIVATE_PREFIX) + str(target)


def parse_chat_id(chat_id: str) -> tuple[str, str]:
    """返回 (chat_type, 原始 id)。"""
    if chat_id.startswith(GROUP_PREFIX):
        return "group", chat_id[len(GROUP_PREFIX):]
    if chat_id.startswith(PRIVATE_PREFIX):
        return "private", chat_id[len(PRIVATE_PREFIX):]
    # 没带前缀时按群号/QQ 号的常见长度无法可靠区分，默认当私聊
    return "private", chat_id


class OneBotError(RuntimeError):
    """OneBot API 调用失败。"""

    def __init__(self, message: str, retcode: int = 0):
        super().__init__(message)
        self.retcode = retcode


class OneBotAdapter(BaseAdapter):
    """OneBot 11 协议适配器（QQ）。私聊 + 群聊全支持。"""

    platform = "onebot"

    def __init__(self, http_url: str = "http://127.0.0.1:3000",
                 access_token: str = "", bot_qq: str = "",
                 bot_nickname: str = ""):
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token
        self.bot_qq = str(bot_qq)
        self.bot_nickname = bot_nickname

        self._client = make_client(self.http_url, timeout=20.0)
        self._handler: Optional[MessageHandler] = None
        self._running = False
        self._reply_context=ContextVar("onebot_reply_context",default=None)
        self._bursts=MessageBuffer(self._deliver)

        # 反向 WS 连接（网关连过来的）
        self._ws: Any = None
        # 最近一次收到事件的时间（毫秒）—— 用来判断"网关到底有没有在推消息"
        self.last_event_at: int = 0
        self.event_count: int = 0
        # 群名片缓存：群号 -> {qq: 群名片}
        self._card_cache: dict[str, dict[str, str]] = {}

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        """探测网关是否可用，并取回登录信息。"""
        info = await self._call("get_login_info")
        if info.get("status") == "ok":
            data = info.get("data") or {}
            self.bot_qq = str(data.get("user_id") or self.bot_qq)
            self.bot_nickname = data.get("nickname") or self.bot_nickname
            log.info("OneBot 已连接：%s(%s)", self.bot_nickname, self.bot_qq)
        else:
            raise OneBotError(
                f"连接 OneBot 网关失败：{info.get('message') or info.get('wording') or '未知错误'}\n"
                f"请确认网关已启动，且 HTTP 地址与 Access Token 填写正确。"
            )
        self._running = True
        self._bursts.start()

    async def stop(self) -> None:
        await self._bursts.close()
        self._running = False
        await self._client.aclose()

    def configure_burst(self,window,max_wait,state_path=None,human=True,pause_timeout=120):
        self._bursts.configure(window,max_wait,state_path,human,pause_timeout)

    async def _deliver(self,msg):
        if self._handler is None:raise RuntimeError('No message handler')
        await self._handler(msg)

    @contextmanager
    def bind_reply_context(self,msg):
        token=self._reply_context.set(msg)
        try:yield
        finally:self._reply_context.reset(token)

    def check_reply_current(self,msg):self._bursts.check(msg)

    def check_failed_reply_current(self,msg):self._bursts.check(msg,after_failure=True)

    async def resolve_images(self,refs):
        semaphore=asyncio.Semaphore(2)
        async def resolve(ref):
            if ref.get('url') or ref.get('inline'):return ref
            try:
                async with semaphore:
                    response=await asyncio.wait_for(self._call('get_image',{'file':ref.get('file_id','')}),timeout=10)
                url=(response.get('data') or {}).get('url')
                if not url:raise MediaError('Gateway did not return an image URL')
                return {'url':url}
            except Exception as exc:raise MediaError('Gateway image lookup failed') from exc
        tasks=[asyncio.create_task(resolve(ref)) for ref in refs]
        try:return await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def is_healthy(self) -> bool:
        try:
            r = await self._call("get_login_info")
            return r.get("status") == "ok"
        except Exception:
            return False

    # ── 入站：事件处理 ────────────────────────────────────────

    def attach_ws(self, ws: Any) -> None:
        """记录反向 WS 连接（由 Web 层在建立连接时调用）。"""
        self._ws = ws
        log.info("OneBot 反向 WebSocket 已连接")

    def detach_ws(self, ws: Any) -> None:
        if self._ws is ws:
            self._ws = None
            log.info("OneBot 反向 WebSocket 已断开")

    @property
    def reverse_ws_connected(self) -> bool:
        """反向 WS 是否在线。

        ⚠️ 这个值和"HTTP 接口能通"是两回事：
        HTTP 通只说明网关活着，**不代表它把消息推过来了**。
        反向 WS 没连上时，QQ 消息一条都收不到，界面必须把这点暴露出来。
        """
        return self._ws is not None

    async def handle_event(self, event: dict) -> None:
        """处理一条 OneBot 事件（来自 WS 或 HTTP POST）。

        只关心消息事件；通知/请求/元事件仅记录日志。
        """
        self.last_event_at = int(time.time() * 1000)
        self.event_count += 1
        post_type = event.get("post_type")

        if post_type == "message":
            msg = self._normalize(event)
            if msg and self._handler:
                self.note_activity(msg)
                if self._bursts.window or self._bursts.pending:
                    self._bursts.submit(msg);return
                try:
                    await self._handler(msg)
                except Exception:
                    log.exception("处理 QQ 消息失败")
            return

        # 群成员变动 → 失效群名片缓存
        if post_type == "notice":
            ntype = event.get("notice_type")
            if ntype=='notify' and event.get('sub_type')=='input_status':
                uid=str(event.get('user_id') or '')
                gid=str(event.get('group_id') or '')
                group=bool(gid and gid!='0')
                if uid and uid!=self.bot_qq:
                    kind='group' if group else 'private'
                    self._bursts.note_typing(make_chat_id(kind,gid if group else uid),uid,
                        on=str(event.get('event_type'))=='1',platform=self.platform,chat_type=kind)
                return
            if ntype in ("group_increase", "group_decrease", "group_card", "group_admin"):
                gid = str(event.get("group_id") or "")
                if gid:
                    self._card_cache.pop(gid, None)
                    log.debug("群 %s 成员变动(%s)，已失效名片缓存", gid, ntype)
            return

        # 元事件：心跳/生命周期
        if post_type == "meta_event":
            if event.get("meta_event_type") == "lifecycle":
                log.info("OneBot 生命周期：%s", event.get("sub_type"))

    def _normalize(self, e: dict) -> Optional[WxMessage]:
        """把 OneBot 消息事件转成统一 WxMessage。"""
        try:
            mtype = e.get("message_type")           # private | group
            sub = e.get("sub_type") or ""
            user_id = str(e.get("user_id") or "")
            self_id = str(e.get("self_id") or self.bot_qq)

            # 忽略自己发的消息（防回环）
            if user_id and user_id == self_id:
                return None

            # 忽略系统提示类群消息（如"管理员已禁止匿名"）
            if mtype == "group" and sub == "notice":
                return None

            group_id = str(e.get("group_id") or "")
            is_group = mtype == "group" and bool(group_id)

            chat_id = make_chat_id("group" if is_group else "private",
                                   group_id if is_group else user_id)

            sender = e.get("sender") or {}
            if is_group:
                # 群名片优先于昵称（与微信的群昵称同理）
                sender_name = sender.get("card") or sender.get("nickname") or user_id
                self._card_cache.setdefault(group_id, {})[user_id] = sender_name
            else:
                sender_name = sender.get("nickname") or user_id

            content, is_at_me = self._extract_content(e.get("message"), self_id)
            images=self._extract_images(e.get('message'))
            if images and '[图片]' not in content:content=(content+'[图片]').strip()

            return WxMessage(
                msg_id=str(e.get("message_id") or ""),
                platform=self.platform,
                chat_type="group" if is_group else "private",
                chat_id=chat_id,
                sender_id=user_id,
                sender_name=sender_name,
                bot_id=self_id,
                content=content,
                raw_content=str(e.get("raw_message") or ""),
                msg_type="image" if images else ("video" if any(s.get('type')=='video' for s in self._segments(e.get('message'))) else "text"), images=images, content_parts=self._extract_parts(e.get("message")),
                timestamp=int(e.get("time") or time.time()) * 1000,
                is_at_me=is_at_me,
                group_id=group_id or None,
            )
        except Exception:
            log.exception("归一化 OneBot 事件失败：%s", e)
            return None

    @staticmethod
    def _segments(message):
        if isinstance(message,list):
            return [seg for seg in message if isinstance(seg,dict)]
        text=str(message or '');segments=[];end=0
        for match in re.finditer(r'\[CQ:([a-zA-Z_]+)(?:,([^\]]*))?\]',text):
            if match.start()>end:
                segments.append({'type':'text','data':{'text':html.unescape(text[end:match.start()])}})
            data={}
            for item in (match.group(2) or '').split(','):
                key,sep,value=item.partition('=')
                if sep:data[key]=html.unescape(value)
            segments.append({'type':match.group(1),'data':data});end=match.end()
        if end<len(text):segments.append({'type':'text','data':{'text':html.unescape(text[end:])}})
        return segments

    @staticmethod
    def _extract_images(message):
        images=[]
        for seg in OneBotAdapter._segments(message):
            if seg.get('type')!='image':continue
            data=seg.get('data') or {};file=str(data.get('file') or '');url=str(data.get('url') or '')
            if file.startswith('base64://'):images.append({'inline':file[9:]})
            elif url or file.startswith(('https://','http://')):images.append({'url':url or file})
            else:images.append({'file_id':file})
        return images

    @staticmethod
    def _extract_parts(message):
        parts=[];image_index=0
        for seg in OneBotAdapter._segments(message):
            kind=seg.get('type');data=seg.get('data') or {}
            if kind=='text':parts.append({'type':'text','text':str(data.get('text') or '')})
            elif kind=='image':parts.append({'type':'image','index':image_index});image_index+=1
            elif kind in ('face','record','video','file'):
                parts.append({'type':'video' if kind=='video' else 'text','text':{'face':'[表情]','record':'[语音]','video':'[视频]','file':'[文件]'}[kind]})
        return parts

    @staticmethod
    def _extract_content(message: Any, self_id: str) -> tuple[str, bool]:
        parts=OneBotAdapter._extract_parts(message)
        text=''.join('[图片]' if part['type']=='image' else part.get('text','') for part in parts)
        at=any(seg.get('type')=='at' and str((seg.get('data') or {}).get('qq'))==self_id for seg in OneBotAdapter._segments(message))
        return text.strip(),at

    async def send_typing(self,chat_id,on=True):
        # NapCat's documented extension sets a private typing hint (event_type=1).
        # Sending a message/native expiry clears it; no undocumented stop enum.
        kind,uid=parse_chat_id(chat_id)
        if kind!='private' or not on or time.monotonic()<getattr(self,'_typing_retry_at',0):return False
        try:
            response=await self._call('set_input_status',{'user_id':str(uid),'event_type':1})
            if response.get('status')=='ok' and response.get('retcode',0)==0:return True
        except Exception:pass
        self._typing_retry_at=time.monotonic()+300
        return False

    # ── 出站：发送 ────────────────────────────────────────────

    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str:
        """发送文本。群聊时可在开头 @ 指定成员。"""
        msg=self._reply_context.get()
        if msg is not None:self.check_reply_current(msg)
        chat_type, target = parse_chat_id(chat_id)

        # 组装消息段
        segments: list[dict] = []
        if at and chat_type == "group":
            for qq in at:
                segments.append({"type": "at", "data": {"qq": str(qq)}})
            segments.append({"type": "text", "data": {"text": " " + text}})
        else:
            segments.append({"type": "text", "data": {"text": text}})

        action = "send_group_msg" if chat_type == "group" else "send_private_msg"
        key = "group_id" if chat_type == "group" else "user_id"
        resp = await self._call(action, {key: int(target) if target.isdigit() else target,
                                         "message": segments})
        if resp.get("status") not in ("ok", "async"):
            raise OneBotError(
                f"发送失败：{resp.get('message') or resp.get('wording') or resp.get('retcode')}",
                retcode=int(resp.get("retcode") or 0),
            )
        return str((resp.get("data") or {}).get("message_id") or "")

    async def send_image(self, chat_id: str, path_or_url: str) -> str:
        chat_type, target = parse_chat_id(chat_id)
        action = "send_group_msg" if chat_type == "group" else "send_private_msg"
        key = "group_id" if chat_type == "group" else "user_id"
        resp = await self._call(action, {
            key: int(target) if target.isdigit() else target,
            "message": [{"type": "image", "data": {"file": path_or_url}}],
        })
        return str((resp.get("data") or {}).get("message_id") or "")

    # ── 群成员 ────────────────────────────────────────────────

    async def get_group_member(self, chat_id: str, user_id: str) -> dict:
        chat_type, target = parse_chat_id(chat_id)
        if chat_type != "group":
            return {}
        resp = await self._call("get_group_member_info", {
            "group_id": int(target) if target.isdigit() else target,
            "user_id": int(user_id) if user_id.isdigit() else user_id,
            "no_cache": False,
        })
        d = resp.get("data") or {}
        return {
            "user_id": str(d.get("user_id") or user_id),
            "nickname": d.get("nickname") or "",
            "card": d.get("card") or "",
            "name": d.get("card") or d.get("nickname") or user_id,
            "role": d.get("role") or "member",
        }

    async def list_group_members(self, chat_id: str) -> list[dict]:
        chat_type, target = parse_chat_id(chat_id)
        if chat_type != "group":
            return []
        resp = await self._call("get_group_member_list", {
            "group_id": int(target) if target.isdigit() else target,
        })
        out = []
        for m in resp.get("data") or []:
            uid = str(m.get("user_id") or "")
            out.append({
                "user_id": uid,
                "nickname": m.get("nickname") or "",
                "card": m.get("card") or "",
                "name": m.get("card") or m.get("nickname") or uid,
                "role": m.get("role") or "member",
            })
        return out

    async def get_group_list(self) -> list[dict]:
        resp = await self._call("get_group_list")
        return [
            {"group_id": str(g.get("group_id") or ""),
             "group_name": g.get("group_name") or "",
             "member_count": g.get("member_count") or 0}
            for g in resp.get("data") or []
        ]

    # ── 内部：API 调用 ────────────────────────────────────────

    async def _call(self, action: str, params: Optional[dict] = None) -> dict:
        """调用 OneBot HTTP API。

        响应格式：{status, retcode, data, echo}
          status: ok | async | failed
          retcode: 0 表示成功，非 0 为错误码
        """
        url = f"{self.http_url}/{action}"
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            r = await self._client.post(url, json=params or {}, headers=headers)
            if r.status_code == 403:
                return {"status": "failed", "retcode": 403,
                        "message": "Access Token 不正确（网关返回 403）"}
            if r.status_code >= 400:
                return {"status": "failed", "retcode": r.status_code,
                        "message": f"网关返回 HTTP {r.status_code}"}
            return r.json()
        except Exception as e:
            return {"status": "failed", "retcode": -1,
                    "message": explain_error(e, url)}
