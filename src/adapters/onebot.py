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
import json
import logging
import time
from typing import Any, Optional

import httpx

from ..core.http import explain_error, make_client
from ..core.types import WxMessage
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

    async def stop(self) -> None:
        self._running = False
        await self._client.aclose()

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
                try:
                    await self._handler(msg)
                except Exception:
                    log.exception("处理 QQ 消息失败")
            return

        # 群成员变动 → 失效群名片缓存
        if post_type == "notice":
            ntype = event.get("notice_type")
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
                msg_type="text",
                timestamp=int(e.get("time") or time.time()) * 1000,
                is_at_me=is_at_me,
                group_id=group_id or None,
            )
        except Exception:
            log.exception("归一化 OneBot 事件失败：%s", e)
            return None

    @staticmethod
    def _extract_content(message: Any, self_id: str) -> tuple[str, bool]:
        """从消息中取出纯文本，并判断是否 @ 了机器人。

        OneBot 的 message 字段可能是：
          · 字符串（CQ 码格式）："你好[CQ:at,qq=123]"
          · 数组（消息段）：[{"type":"text","data":{"text":"你好"}},
                            {"type":"at","data":{"qq":"123"}}]
        两种都要处理。
        """
        is_at_me = False

        # ── 数组格式 ──
        if isinstance(message, list):
            parts: list[str] = []
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                stype = seg.get("type")
                data = seg.get("data") or {}
                if stype == "text":
                    parts.append(str(data.get("text") or ""))
                elif stype == "at":
                    qq = str(data.get("qq") or "")
                    if qq == self_id:
                        is_at_me = True
                    # @ 本身不进入正文，避免污染给模型的内容
                elif stype == "image":
                    parts.append("[图片]")
                elif stype == "face":
                    parts.append("[表情]")
                elif stype == "reply":
                    pass          # 引用不进入正文
            return "".join(parts).strip(), is_at_me

        # ── 字符串 / CQ 码格式 ──
        text = str(message or "")
        if f"[CQ:at,qq={self_id}]" in text:
            is_at_me = True
        # 剥离所有 CQ 码，留下纯文本
        import re
        cleaned = re.sub(r"\[CQ:[^\]]+\]", "", text)
        return cleaned.strip(), is_at_me

    # ── 出站：发送 ────────────────────────────────────────────

    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str:
        """发送文本。群聊时可在开头 @ 指定成员。"""
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
