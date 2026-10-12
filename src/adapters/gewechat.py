"""GeWe / Gewechat 适配器（iPad 协议）—— 完整群聊支持。

═══════════════════════════════════════════════════════════════════
为什么用这条路径做群聊？
  官方 iLink Bot API 的群聊缺失是**服务端限制**，不是插件限制：
    · 官方 @tencent-weixin/openclaw-weixin 2.4.10 → chatTypes: ["direct"]
    · 社区 fork openclaw-weixin 3.2.3（版本号更高）→ 同样 ["direct"]，零群聊代码
    · 第三方 SDK weixin-ilink → 仅类型字段，无实现
    · 官方仓库 5 个群聊 feature request 全部 open、零维护者回应
  所以群聊只能走协议路径。

接口来源（已按真实文档逐项核对）：
  GeWe API 文档 https://doc.geweapi.com
  Gewechat 开放接口 https://s.apifox.cn/69ba62ca-cb7d-437e-85e4-6f3d3df271b1

═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from ..core.group import (
    GroupMember,
    GroupMemberResolver,
    detect_at_bot,
    is_group_id,
    parse_sender_prefix,
    should_invalidate_members,
)
from ..core.http import explain_error, make_client
from ..core.types import WxMessage
from .base import BaseAdapter, MessageHandler

log = logging.getLogger(__name__)


class GeWeError(RuntimeError):
    """网关调用失败。带上 HTTP 状态码，便于上层区分故障类型。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status

# GeWe 的 MsgType 字符串 → 统一 WxMessage.msg_type
_MSG_TYPE_MAP = {
    "TEXT": "text",
    "IMAGE": "image",
    "VOICE": "voice",
    "VIDEO": "video",
    "FILE": "file",
    "EMOJI": "text",
    "LINK": "text",
    "MINI_PROGRAM": "text",
    "QUOTE": "text",
    "CHAT_RECORD": "file",
    "SYSTEM": "system",
    "GROUP_INVITE": "system",
    "REMOVE_FROM_GROUP": "system",
    "GROUP_DISMISS": "system",
    "GROUP_NAME_CHANGE": "system",
    "REVOKE_MSG": "system",
    "PAT_MSG": "system",
}

# 整数 MsgType（部分回调版本用数字）
_MSG_TYPE_INT_MAP = {1: "text", 3: "image", 34: "voice", 43: "video", 49: "file"}


class GewechatMemberResolver(GroupMemberResolver):
    """群成员解析器 —— 对接 GeWe 的群成员列表接口。

    接口：POST /group/getChatroomMemberList
    返回：{ data: { memberList: [{wxid, nickName, displayName, memberFlag}],
                    chatroomOwner, adminWxid: [] } }
    """

    def __init__(self, adapter: "GewechatAdapter", ttl: float = 1800.0):
        super().__init__(ttl=ttl)
        self.adapter = adapter

    async def _fetch_members(self, chat_id: str) -> dict[str, GroupMember]:
        data = await self.adapter._api("group/getChatroomMemberList", {"chatroomId": chat_id})
        d = data.get("data") or {}
        owner = d.get("chatroomOwner") or ""
        admins = set(d.get("adminWxid") or [])

        members: dict[str, GroupMember] = {}
        for m in d.get("memberList") or []:
            wxid = m.get("wxid")
            if not wxid:
                continue
            members[wxid] = GroupMember(
                wxid=wxid,
                nickname=m.get("nickName") or "",
                display_name=m.get("displayName") or "",
                is_owner=(wxid == owner),
                is_admin=(wxid in admins),
            )
        log.debug("拉取群成员 chat_id=%s 数量=%d", chat_id, len(members))
        return members


class GewechatAdapter(BaseAdapter):
    """GeWe / Gewechat 协议适配器。私聊 + 群聊全支持。"""

    platform = "gewechat"
    supports_private = True
    supports_group = True

    def __init__(self, base_url: str, token: str, app_id: str, bot_wxid: str,
                 bot_nickname: str = "", member_cache_ttl: float = 1800.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.app_id = app_id
        self.bot_wxid = bot_wxid
        self.bot_nickname = bot_nickname
        # ⚠️ 用 make_client 而非直接 httpx.AsyncClient：
        # 网关常在本机（127.0.0.1:2531），而 httpx 默认会把本机请求也走系统代理，
        # 导致返回 502。make_client 会自动对内网地址绕过代理。
        self._client = make_client(self.base_url, timeout=20.0)
        self._handler: Optional[MessageHandler] = None
        self._running = False

        # 群成员名称解析（群昵称优先）
        self.members = GewechatMemberResolver(self, ttl=member_cache_ttl)
        # 去重：官方文档要求 key = appid + newMsgId
        self._seen: dict[str, float] = {}

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        await self._check_login()
        self._running = True
        log.info("GewechatAdapter started, bot=%s", self.bot_wxid)

    async def stop(self) -> None:
        self._running = False
        await self._client.aclose()

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def is_healthy(self) -> bool:
        try:
            await self._check_login()
            return True
        except Exception:
            return False

    # ── 入站：网关回调 ────────────────────────────────────────

    async def handle_webhook(self, payload: dict) -> dict:
        """HTTP 回调入口。

        ⚠️ 同一消息会重复回调（服务重启 / 历史同步 / 失败重试），
        官方文档明确要求去重，去重 Key = appid + newMsgId。
        """
        if not self._dedup(payload):
            return {"ret": 200, "msg": "duplicate ignored"}

        msg = await self._normalize(payload)
        if msg and self._handler:
            await self._handler(msg)
        return {"ret": 200, "msg": "ok"}

    def _dedup(self, payload: dict) -> bool:
        """返回 True 表示首次收到。"""
        d = payload.get("Data") or payload
        appid = payload.get("appid") or payload.get("Appid") or self.app_id
        new_id = d.get("NewMsgId") or d.get("newMsgId") or d.get("MsgId") or d.get("msgId")
        if not new_id:
            return True
        key = f"{appid}:{new_id}"
        now = time.time()
        # 顺带清理 1 小时前的记录，防止内存无限增长
        if len(self._seen) > 5000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 3600}
        if key in self._seen:
            return False
        self._seen[key] = now
        return True

    async def _normalize(self, payload: dict) -> Optional[WxMessage]:
        """把 GeWe 回调归一化为统一 WxMessage。

        兼容两种回调结构：
          v2.0 扁平结构：{appid, wxid, content, fromUser, toUser, msgType, newMsgId, isSelf}
          旧版包装结构：{TypeName, Data: {FromUserName, ToUserName, Content, MsgType, ...}}
        """
        try:
            is_v2 = "msgType" in payload or "fromUser" in payload
            d = payload if is_v2 else (payload.get("Data") or {})

            # 统一取值
            if is_v2:
                from_user = d.get("fromUser") or ""
                to_user = d.get("toUser") or ""
                content = d.get("content") or ""
                raw_type = d.get("msgType") or "TEXT"
                msg_id = str(d.get("newMsgId") or d.get("msgId") or "")
                ts = self._to_ms(d.get("createTime"))
                is_self = bool(d.get("isSelf"))
                sender_field = ""
                at_list = d.get("atWxidList") or d.get("AtWxidList")
                msg_source = d.get("msgSource") or ""
                push_content = d.get("pushContent") or ""
                event_type = d.get("typeName") or d.get("TypeName") or ""
            else:
                from_user = d.get("FromUserName") or ""
                to_user = d.get("ToUserName") or ""
                content = d.get("Content") or ""
                raw_type = d.get("MsgType") or 1
                msg_id = str(d.get("NewMsgId") or d.get("MsgId") or "")
                ts = self._to_ms(d.get("CreateTime"))
                is_self = bool(d.get("IsSelf"))
                sender_field = d.get("SenderUserName") or ""
                at_list = d.get("AtWxidList")
                msg_source = d.get("MsgSource") or ""
                push_content = d.get("PushContent") or ""
                event_type = payload.get("TypeName") or ""

            if is_self:
                return None     # 自己发的消息，防回环

            # ── 会话与发送者判定 ──────────────────────────
            # ⚠️ 两种回调格式的字段语义**不同**，这是最容易搞错的地方：
            #   v2.0   : fromUser = 发送者，toUser = 会话（群聊时是群 ID）
            #   旧版    : FromUserName = 会话（群聊时是群 ID），SenderUserName = 发送者
            if is_v2:
                sender_hint = from_user
                conv = to_user if is_group_id(to_user) else from_user
            else:
                conv = from_user
                sender_hint = sender_field

            is_group = is_group_id(conv)
            chat_id = conv

            # 前缀兜底：旧版协议常把发送者塞在 content 开头
            prefix_sender, content = parse_sender_prefix(content)
            sender_id = sender_hint or prefix_sender or ("" if is_group else conv)

            if not chat_id or not sender_id:
                return None

            # ── @ 检测（三种格式）─────────────────────────
            is_at_me, content = detect_at_bot(
                content=content,
                bot_wxid=self.bot_wxid,
                bot_names=[self.bot_nickname] if self.bot_nickname else [],
                at_list=at_list,
                msg_source=msg_source,
            )

            # ── 昵称解析（群昵称优先）─────────────────────
            sender_name = sender_id
            if is_group:
                # 回调里若自带昵称，回填缓存省一次接口调用
                if push_content:
                    self.members.remember(chat_id, sender_id, push_content)
                sender_name = await self.members.resolve(chat_id, sender_id)

            # ── 群成员变动事件 → 失效缓存 ─────────────────
            if is_group and should_invalidate_members(str(event_type)):
                self.members.invalidate(chat_id)
                log.info("群成员变动(%s)，已失效成员缓存 chat_id=%s", event_type, chat_id)

            return WxMessage(
                msg_id=msg_id,
                platform=self.platform,
                chat_type="group" if is_group else "private",
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
                bot_id=self.bot_wxid,
                content=content.strip(),
                raw_content=d.get("Content") or content,
                msg_type=self._map_type(raw_type),
                mentions=[str(x) for x in (at_list or [])],
                quote_msg_id=d.get("QuoteMsgId") or d.get("quoteMsgId"),
                timestamp=ts or int(time.time() * 1000),
                is_at_me=is_at_me,
                group_id=chat_id if is_group else None,
            )
        except Exception:
            log.exception("归一化失败：%s", payload)
            return None

    @staticmethod
    def _map_type(raw) -> str:
        if isinstance(raw, int):
            return _MSG_TYPE_INT_MAP.get(raw, "text")
        return _MSG_TYPE_MAP.get(str(raw).upper(), "text")

    @staticmethod
    def _to_ms(raw) -> int:
        """把时间戳统一归一化为毫秒。

        ⚠️ 不同协议/版本的时间戳单位不一致：GeWe 回调的 createTime /
        CreateTime 都是**秒**，而部分协议给的是毫秒。
        如果一律按毫秒处理，时间会落到 1970 年，导致静默时段、
        会话 TTL、冷却计时等所有基于时间的逻辑全部失效。

        这里按数量级自动判定：< 1e11 视为秒，否则视为毫秒。
        """
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return int(time.time() * 1000)
        if v <= 0:
            return int(time.time() * 1000)
        return v * 1000 if v < 100_000_000_000 else v

    # ── 出站：发送 ────────────────────────────────────────────

    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str:
        """发送文本。

        ⚠️ 群聊 @ 成员的关键约束（官方文档原文）：
            "在群内发送消息@某人时，content 中需包含 @xxx"
            即：ats 传 wxid，同时 content 里必须出现 @昵称，两者缺一不可。
            只传 ats 不写 @昵称 → 微信客户端不会渲染出 @ 效果。
        """
        body: dict = {"appId": self.app_id, "toWxid": chat_id, "content": text}

        if at and is_group_id(chat_id):
            names = [await self.members.resolve(chat_id, w) for w in at]
            prefix = "".join(f"@{n} " for n in names if n)
            body["content"] = f"{prefix}{text}"
            body["ats"] = ",".join(at)
        elif at:
            log.debug("私聊消息忽略 at 参数 chat_id=%s", chat_id)

        resp = await self._api("message/postText", body)
        return str((resp.get("data") or {}).get("newMsgId") or "")

    async def send_image(self, chat_id: str, path_or_url: str) -> str:
        resp = await self._api("message/postImage", {
            "appId": self.app_id, "toWxid": chat_id, "imgUrl": path_or_url,
        })
        return str((resp.get("data") or {}).get("newMsgId") or "")

    # ── 群成员 ────────────────────────────────────────────────

    async def get_group_member(self, chat_id: str, user_id: str) -> dict:
        m = await self.members.get(chat_id, user_id)
        if not m:
            return {}
        return {
            "wxid": m.wxid, "nickName": m.nickname,
            "displayName": m.display_name, "name": m.name,
            "isOwner": m.is_owner, "isAdmin": m.is_admin,
        }

    async def list_group_members(self, chat_id: str) -> list[dict]:
        """获取全部群成员（含群昵称）。"""
        cache = await self.members._ensure(chat_id)
        return [
            {"wxid": m.wxid, "name": m.name, "nickName": m.nickname,
             "displayName": m.display_name, "isOwner": m.is_owner, "isAdmin": m.is_admin}
            for m in cache.members.values()
        ]

    # ── 群操作（完整能力）─────────────────────────────────────

    async def create_group(self, wxids: list[str]) -> str:
        """创建群聊。返回新群 ID。"""
        resp = await self._api("group/createChatroom", {
            "appId": self.app_id, "wxids": ",".join(wxids),
        })
        return str((resp.get("data") or {}).get("chatroomId") or "")

    async def invite_to_group(self, chat_id: str, wxids: list[str]) -> None:
        await self._api("group/inviteMember", {
            "appId": self.app_id, "chatroomId": chat_id, "wxids": ",".join(wxids),
        })
        self.members.invalidate(chat_id)

    async def remove_from_group(self, chat_id: str, wxids: list[str]) -> None:
        await self._api("group/removeMember", {
            "appId": self.app_id, "chatroomId": chat_id, "wxids": ",".join(wxids),
        })
        self.members.invalidate(chat_id)

    async def rename_group(self, chat_id: str, name: str) -> None:
        await self._api("group/modifyChatroomName", {
            "appId": self.app_id, "chatroomId": chat_id, "chatroomName": name,
        })

    async def set_my_group_nickname(self, chat_id: str, nickname: str) -> None:
        """设置机器人在本群的群昵称 —— 影响 @ 检测的昵称匹配。"""
        await self._api("group/modifyChatroomNickNameForSelf", {
            "appId": self.app_id, "chatroomId": chat_id, "nickName": nickname,
        })
        self.members.invalidate(chat_id)

    async def quit_group(self, chat_id: str) -> None:
        await self._api("group/quitChatroom", {"appId": self.app_id, "chatroomId": chat_id})
        self.members.invalidate(chat_id)

    async def disband_group(self, chat_id: str) -> None:
        await self._api("group/disbandChatroom", {"appId": self.app_id, "chatroomId": chat_id})
        self.members.invalidate(chat_id)

    # ── 内部：HTTP ────────────────────────────────────────────

    async def _api(self, endpoint: str, body: dict) -> dict:
        """统一调用 GeWe API。路径规则：{base}/v2/api/{endpoint}"""
        url = f"{self.base_url}/v2/api/{endpoint}"
        try:
            r = await self._client.post(url, json=body, headers={"X-GEWE-TOKEN": self.token})
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 带上状态码，让上层能区分"网关活着但报错"和"根本连不上"
            raise GeWeError(
                f"{explain_error(e, url)}（HTTP {e.response.status_code}）",
                status=e.response.status_code,
            ) from e
        except httpx.HTTPError as e:
            raise GeWeError(explain_error(e, url)) from e

        data = r.json()
        if data.get("ret") not in (200, 0):
            raise GeWeError(
                f"网关返回错误：ret={data.get('ret')} msg={data.get('msg')}"
            )
        return data

    async def _check_login(self) -> None:
        await self._api("login/checkLogin", {"appId": self.app_id})
