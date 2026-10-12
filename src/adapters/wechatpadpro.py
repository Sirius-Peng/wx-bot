"""WeChatPadPro 适配器（iPad 协议）—— 接替已停止维护的 GeWe/Gewechat。

═══════════════════════════════════════════════════════════════════
为什么换掉 GeWe？

`Devo919/Gewechat` 仓库 README 首行已写明「技术归档」：

    本项目已停止维护。Gewechat 的历史运行服务、Docker 镜像、
    部署方式及技术支持均不再提供……请勿根据旧教程、旧镜像或
    第三方分发内容判断项目仍然可用。

实测也印证了这一点：旧镜像的容器能起来，但 2531 端口**只监听不响应 HTTP**。

WeChatPadPro 是社区当前的替代方案，本文件按其**真实 API** 实现
（对 `wechatpadpro-macos-arm64` 实测，接口与字段均已核对）。

═══════════════════════════════════════════════════════════════════
鉴权模型（与 GeWe 完全不同，最容易踩错）

    ADMIN_KEY ──POST /admin/GenAuthKey1──▶ authKey ──作为所有请求的 ?key= ──▶ 业务接口

  · `ADMIN_KEY` 只是"管理员口令"，**不能**直接当业务接口的 key 用
    （直接用会得到 `{"Code":300,"Text":"请确认授权码是否正确"}`）
  · 业务接口统一用查询参数 `key=<authKey>`，没有 `X-GEWE-TOKEN` 之类的头
  · authKey 有有效期（生成时用 Days 指定），设备列表 /admin/GetAllDevices 可查

═══════════════════════════════════════════════════════════════════
接口清单（均已实测）

  登录   POST /login/GetLoginQrCodePad   body {"Check":bool,"Proxy":str}
         GET  /login/CheckLoginStatus
         GET  /login/GetLoginStatus
  群     GET  /group/GetAllGroupList
         POST /group/GetChatroomMemberDetail  body {"ChatRoomName": "xxx@chatroom"}
  发消息 POST /message/SendTextMessage
         body {"MsgItem":[{"ToUserName":wxid,"TextContent":文本,
                           "MsgType":1,"AtWxIDList":[wxid...]}]}
  回调   POST /webhook/Config   body {"Enabled":true,"URL":..., ...}
         GET  /webhook/List  /webhook/Status  /webhook/ResetConnection

返回统一是 `{"Code":200,"Data":{...},"Text":"..."}`；`Code != 200` 即失败。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

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

#: Pad 协议的 MsgType（数值）→ 统一 WxMessage.msg_type
_MSG_TYPE_NUM = {
    1: "text", 3: "image", 34: "voice", 43: "video", 49: "text",
    47: "text", 48: "text", 42: "text", 10000: "system",
}

#: 字符串形式（部分版本回调用字符串）
_MSG_TYPE_STR = {
    "TEXT": "text", "IMAGE": "image", "VOICE": "voice", "VIDEO": "video",
    "FILE": "file", "EMOJI": "text", "LINK": "text", "SYSTEM": "system",
    "MINI_PROGRAM": "text", "QUOTE": "text", "CHAT_RECORD": "file",
}


class WePadProError(RuntimeError):
    """网关调用失败。带上服务端 Code，便于上层区分故障类型。"""

    def __init__(self, message: str, code: int = 0, status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


def _first(d: dict, *names: str, default: Any = "") -> Any:
    """按候选键名取值。

    协议端各版本字段大小写不统一（`MsgId` / `msgId` / `NewMsgId`…），
    这里统一按候选列表取第一个存在的，避免因大小写差异漏字段。
    """
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


def _to_ms(ts: Any) -> int:
    """时间戳归一化到毫秒。

    ⚠️ 协议回调的时间戳单位不统一：有的给秒、有的给毫秒。
    统一按数量级判断（< 1e11 视为秒）—— GeWe 时代就因为按毫秒处理秒级
    时间戳，导致时间落到 1970 年，静默时段把消息全拦了。
    """
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return int(time.time() * 1000)
    if v <= 0:
        return int(time.time() * 1000)
    return v * 1000 if v < 1e11 else v


class WeChatPadProAdapter(BaseAdapter):
    """WeChatPadPro 协议适配器。私聊 + 群聊全支持。"""

    platform = "wechatpadpro"
    supports_private = True
    supports_group = True

    def __init__(self, base_url: str, auth_key: str, bot_wxid: str = "",
                 bot_nickname: str = "", member_cache_ttl: float = 1800.0):
        self.base_url = base_url.rstrip("/")
        self.auth_key = auth_key
        self.bot_wxid = bot_wxid
        self.bot_nickname = bot_nickname
        # 协议端常在本机，必须绕过系统代理（否则 Clash 之类会返回 502）
        self._client = make_client(self.base_url, timeout=25.0)
        self._handler: Optional[MessageHandler] = None
        self._running = False

        self.members = WePadProMemberResolver(self, ttl=member_cache_ttl)
        # 去重：同一消息会重复推送（重启 / 历史同步 / 重试）
        self._seen: dict[str, float] = {}
        self._dedup_ttl = 600.0

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        st = await self.login_status()
        if not st.get("loggedIn"):
            raise WePadProError(
                st.get("message") or "WeChatPadPro 尚未登录，请先在接入设置里扫码"
            )
        info = await self._call("GET", "/login/GetLoginStatus")
        data = info.get("Data") or {}
        self.bot_wxid = str(_first(data, "wxid", "Wxid", "userName", default=self.bot_wxid))
        self.bot_nickname = str(_first(data, "nickName", "NickName", default=self.bot_nickname))
        self._running = True
        log.info("WeChatPadPro 已连接：%s(%s)", self.bot_nickname, self.bot_wxid)

    async def stop(self) -> None:
        self._running = False
        await self._client.aclose()

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def is_healthy(self) -> bool:
        try:
            return (await self.login_status()).get("loggedIn", False)
        except Exception:
            return False

    # ── HTTP 调用 ─────────────────────────────────────────────

    async def _call(self, method: str, path: str, body: dict | None = None,
                    timeout: float = 25.0) -> dict:
        """调协议端接口，返回解析后的 JSON。

        ⚠️ `key` 走的是**查询参数**，不是请求头（与 GeWe 的 X-GEWE-TOKEN 不同）。
        """
        url = f"{self.base_url}{path}"
        params = {"key": self.auth_key} if self.auth_key else {}
        try:
            if method.upper() == "GET":
                r = await self._client.get(url, params=params, timeout=timeout)
            else:
                r = await self._client.post(url, params=params, json=body or {},
                                            timeout=timeout)
            if r.status_code == 404:
                raise WePadProError(f"协议端没有这个接口：{path}", status=404)
            if r.status_code >= 400:
                raise WePadProError(
                    explain_error(
                        httpx.HTTPStatusError(f"HTTP {r.status_code}",
                                              request=r.request, response=r), url),
                    status=r.status_code)
            return r.json()
        except WePadProError:
            raise
        except Exception as e:
            raise WePadProError(explain_error(e, url)) from e

    async def _call_ok(self, method: str, path: str, body: dict | None = None,
                       timeout: float = 25.0) -> dict:
        """调接口并要求业务码为成功，返回 `Data`。"""
        res = await self._call(method, path, body, timeout)
        code = res.get("Code")
        if code != 200:
            text = res.get("Text") or res.get("text") or ""
            raise WePadProError(f"{text or '协议端返回失败'}", code=code or 0)
        return res.get("Data") or {}

    # ── 登录状态 ──────────────────────────────────────────────

    async def verify_key(self) -> bool:
        """授权码能否用于业务接口。

        ⚠️ 这里**不能**只看业务接口的返回码。实测同一个有效授权码会返回：

            已登录          → {"Code":200,...}
            未登录          → {"Code":300,"Text":"该账号需要重新登录！..."}
            账号态缺失/刚重启 → {"Code":-2, "Text":"<key> 该链接不存在！"}

        也就是说 `-2` **既可能表示 key 无效，也可能只是账号还没登录**。
        把它一律判成"授权码无效"会误伤：用户明明填对了，界面却让他去重新生成。

        真正能判定"key 是否存在"的是管理接口 `/admin/GetAllDevices`：
        它直接列出协议端登记过的授权码，据此比对最可靠。
        这里退化成"宽松判定"——只有明确说 key 无效时才判失败，
        其余情况一律认为 key 可用（登录态由 `login_status()` 单独负责）。
        """
        try:
            await self._call_ok("GET", "/group/GetAllGroupList", timeout=15.0)
            return True
        except WePadProError as e:
            msg = str(e)
            if "无效的key" in msg or "无效的 key" in msg:
                return False
            # -2 / "该链接不存在" / 300 都当作"key 可能有效但账号态缺失"
            return True

    async def login_status(self) -> dict:
        """查登录态。把"未登录" / "授权码错" / "连不上"三种情况区分开。"""
        try:
            res = await self._call("GET", "/login/CheckLoginStatus", timeout=15.0)
        except WePadProError as e:
            return {"loggedIn": False, "reachable": False, "message": str(e),
                    "hint": "协议端好像没在运行，请先点「一键启动微信协议端」。"}

        code = res.get("Code")
        data = res.get("Data") or {}
        text = res.get("Text") or ""

        # 该接口不校验 key，所以单独验一次，避免把"授权码错"误报成"没扫码"
        if code != 200 and not await self.verify_key():
            return {"loggedIn": False, "reachable": True, "authInvalid": True,
                    "message": "授权码无效或已过期",
                    "hint": "请到「接入设置」重新生成授权码。"}

        # 未登录时服务端返回 Code=300 + "该账号需要重新登录"
        if code != 200:
            # ⚠️ 授权码不对时也返回 300，必须与"未登录"区分开，
            # 否则用户会一直以为是没扫码，怎么扫都没用。
            if "授权码" in text or "无效的key" in text or "无效的 key" in text:
                return {"loggedIn": False, "reachable": True,
                        "authInvalid": True,
                        "message": "授权码无效或已过期",
                        "hint": "请到「接入设置」重新生成授权码。"}
            if "不存在状态" in text:
                return {"loggedIn": False, "reachable": True, "message": "尚未扫码登录"}
            if "需要重新登录" in text or "MMLoginStateNoLogin" in text:
                return {"loggedIn": False, "reachable": True, "message": "登录已失效，请重新扫码"}
            return {"loggedIn": False, "reachable": True, "message": text or "未登录"}

        logged = bool(_first(data, "loginState", "LoginState", "isLogin", default=0))
        wxid = str(_first(data, "wxid", "Wxid", default=""))
        nick = str(_first(data, "nickName", "NickName", default=""))
        return {"loggedIn": logged, "reachable": True, "wxid": wxid,
                "nickname": nick, "message": "已登录" if logged else "尚未登录"}

    # ── 入站：协议端回调 ──────────────────────────────────────

    async def handle_webhook(self, payload: dict) -> dict:
        """HTTP 回调入口（协议端推消息过来）。"""
        if not self._dedup(payload):
            return {"Code": 200, "Text": "duplicate ignored"}

        msg = await self._normalize(payload)
        if msg and self._handler:
            try:
                await self._handler(msg)
            except Exception:
                log.exception("处理消息失败 msg_id=%s", msg.msg_id)
        return {"Code": 200, "Text": "ok"}

    def _dedup(self, payload: dict) -> bool:
        """按 (客户端ID + 消息ID) 去重。

        ⚠️ 同一消息会重复推送（服务重启 / 历史同步 / 失败重试），
        不去重会重复回复。
        """
        mid = str(_first(payload, "MsgId", "msgId", "NewMsgId", "newMsgId", default=""))
        cid = str(_first(payload, "ClientMsgId", "clientMsgId", default=""))
        key = f"{cid}|{mid}"
        if not mid and not cid:
            return True                       # 没有 ID 就没法去重，放行
        now = time.time()
        # 顺手清理过期记录，避免无界增长
        if len(self._seen) > 4000:
            self._seen = {k: v for k, v in self._seen.items()
                          if now - v < self._dedup_ttl}
        if key in self._seen:
            return False
        self._seen[key] = now
        return True

    async def _normalize(self, raw: dict) -> Optional[WxMessage]:
        """把协议端回调归一化成统一 WxMessage。"""
        try:
            # 兼容几种常见包裹：直接给消息体 / 套在 Data / 套在 MsgList 里
            data = raw.get("Data") or raw.get("data") or raw
            if isinstance(data, dict) and isinstance(data.get("MsgList"), list):
                data = (data["MsgList"] or [{}])[0]

            from_user = str(_first(data, "FromUserName", "fromUserName", "FromWxid",
                                   "fromWxid", "SenderUserName", default=""))
            to_user = str(_first(data, "ToUserName", "toUserName", "ToWxid",
                                 "toWxid", default=""))
            content = str(_first(data, "Content", "content", "TextContent",
                                 "textContent", default=""))

            # 群消息：会话 ID 是 xxx@chatroom，发送者在 FromUserName
            chat_id = to_user if is_group_id(to_user) else from_user
            chat_type = "group" if is_group_id(chat_id) else "private"

            # 自己发的消息不入站处理（防回环）
            if self.bot_wxid and from_user == self.bot_wxid:
                return None

            # 前缀兜底：部分版本把发送者塞在 content 开头（`wxid_xxx:\n正文`）
            # ⚠️ 不剥离会让 LLM 看到 "wxid_abc: 你好" 这种脏数据。
            # 返回顺序是 (sender_wxid, 正文)。
            is_group = chat_type == "group"
            prefix_sender, content = parse_sender_prefix(content)
            sender_id = prefix_sender or from_user
            if not chat_id or not sender_id:
                return None

            msg_type = _MSG_TYPE_NUM.get(
                int(_first(data, "MsgType", "msgType", default=0) or 0), "")
            if not msg_type:
                msg_type = _MSG_TYPE_STR.get(
                    str(_first(data, "MsgType", "msgType", "Type", default="")).upper(),
                    "text")

            # @ 检测（三种格式依次尝试：结构化列表 → XML → 正文昵称兜底）
            # ⚠️ 微信 @ 后的分隔符是 U+2005，不是普通空格，交给共享模块处理。
            at_list = _first(data, "AtWxIDList", "atWxidList", "AtList",
                             "AtList", default=[]) or []
            if isinstance(at_list, str):
                at_list = [x for x in at_list.split(",") if x]
            mentions = [str(x) for x in at_list]
            msg_source = str(_first(data, "MsgSource", "msgSource", default=""))
            is_at_me, content = detect_at_bot(
                content=content,
                bot_wxid=self.bot_wxid,
                bot_names=[self.bot_nickname] if self.bot_nickname else [],
                at_list=mentions,
                msg_source=msg_source,
            ) if is_group else (False, content)

            context_token = str(_first(data, "ContextToken", "contextToken",
                                       "SyncKey", "syncKey", default=""))

            return WxMessage(
                msg_id=str(_first(data, "MsgId", "msgId", "NewMsgId", "newMsgId",
                                 "ClientMsgId", default="")),
                platform=self.platform,
                chat_type=chat_type,
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name="",          # 由 members 解析器补真实昵称
                bot_id=self.bot_wxid,
                content=content.strip(),
                raw_content=content,
                msg_type=msg_type,       # type: ignore[arg-type]
                mentions=mentions,
                timestamp=_to_ms(_first(data, "CreateTime", "createTime",
                                        "Timestamp", default=0)),
                is_at_me=is_at_me,
                context_token=context_token,
                seq=int(_first(data, "Seq", "seq", default=0) or 0),
                session_id=chat_id,
                group_id=chat_id if chat_type == "group" else None,
            )
        except Exception:
            log.exception("归一化失败：%s", raw)
            return None

    # ── 回调注册 ──────────────────────────────────────────────

    async def setup_webhook(self, url: str, secret: str = "",
                            include_self: bool = False) -> dict:
        """把消息回调指到本程序。

        协议端既支持 Webhook 推送，也支持 `/message/HttpSyncMsg` 轮询。
        这里用 Webhook（实时、省资源），注册失败时上层可回退到轮询。
        """
        return await self._call_ok("POST", "/webhook/Config", {
            "Enabled": True,
            "URL": url,
            "Secret": secret,
            "IncludeSelfMessage": include_self,
            "MessageTypes": ["Text", "Image", "Voice", "Video", "File"],
            "RetryCount": 3,
            "Timeout": 10,
        }, timeout=20.0)

    async def list_webhooks(self) -> list:
        data = await self._call_ok("GET", "/webhook/List", timeout=15.0)
        if isinstance(data, list):
            return data
        return data.get("list") or data.get("configs") or []

    async def poll_messages(self, count: int = 20) -> list[dict]:
        """HTTP 轮询拉消息（Webhook 不可用时的兜底通道）。"""
        data = await self._call_ok("POST", "/message/HttpSyncMsg",
                                   {"Count": count}, timeout=35.0)
        return data.get("MsgList") or data.get("msgList") or []

    # ── 消息发送 ──────────────────────────────────────────────

    async def send_text(self, chat_id: str, text: str,
                        at: list[str] | None = None) -> str:
        """发文本。群聊 @ 走 `AtWxIDList`。

        ⚠️ 协议端要求 @ 时**正文里也要带 `@昵称`**，只传 AtWxIDList 不生效
        （GeWe 时代踩过同一个坑，两者行为一致）。
        """
        item: dict[str, Any] = {
            "ToUserName": chat_id,
            "TextContent": text,
            "MsgType": 1,
        }
        if at:
            item["AtWxIDList"] = [str(x) for x in at]
        data = await self._call_ok("POST", "/message/SendTextMessage",
                                   {"MsgItem": [item]}, timeout=30.0)
        return str(_first(data, "MsgId", "msgId", "NewMsgId", default=""))

    async def send_image(self, chat_id: str, path_or_url: str) -> str:
        """发图片。

        `ImageContent` 要 **base64**（不是 URL）。支持传本地路径或 http(s) 地址；
        传 URL 时这里先下载再编码 —— 协议端不帮我们拉取。
        """
        import base64 as _b64
        from pathlib import Path as _Path

        if path_or_url.startswith(("http://", "https://")):
            try:
                async with make_client(path_or_url, timeout=30.0) as c:
                    r = await c.get(path_or_url)
                    r.raise_for_status()
                    raw = r.content
            except Exception as e:
                raise WePadProError(f"下载图片失败：{explain_error(e, path_or_url)}") from e
        else:
            p = _Path(path_or_url)
            if not p.exists():
                raise WePadProError(f"图片文件不存在：{path_or_url}")
            raw = p.read_bytes()

        data = await self._call_ok("POST", "/message/SendImageMessage", {
            "MsgItem": [{
                "ToUserName": chat_id,
                "MsgType": 2,
                "ImageContent": _b64.b64encode(raw).decode("ascii"),
            }],
        }, timeout=60.0)
        return str(_first(data, "MsgId", "msgId", "NewMsgId", default=""))

    async def send_typing(self, chat_id: str, on: bool = True) -> None:
        """协议端不保证支持"正在输入"，静默跳过。"""
        return None

    # ── 群 ────────────────────────────────────────────────────

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
        raw = await self._call_ok("POST", "/group/GetChatroomMemberDetail",
                                  {"ChatRoomName": chat_id}, timeout=30.0)
        return [m for m in (raw.get("memberList") or raw.get("MemberList") or [])
                if isinstance(m, dict)]

    async def list_groups(self) -> list[dict]:
        data = await self._call_ok("GET", "/group/GetAllGroupList", timeout=30.0)
        return [g for g in (data.get("chatroomList") or data.get("ChatRoomList") or [])
                if isinstance(g, dict)]


class WePadProMemberResolver(GroupMemberResolver):
    """群成员解析器 —— 对接 WeChatPadPro 的群成员接口。

    只实现 `_fetch_members`，缓存 / 失效 / 降级由基类统一处理
    （这样各协议端的行为完全一致，不用各写一套）。
    """

    def __init__(self, adapter: WeChatPadProAdapter, ttl: float = 1800.0):
        super().__init__(ttl=ttl)
        self.adapter = adapter

    async def _fetch_members(self, chat_id: str) -> dict[str, GroupMember]:
        data = await self.adapter._call_ok(
            "POST", "/group/GetChatroomMemberDetail",
            {"ChatRoomName": chat_id}, timeout=30.0,
        )
        raw_list = (data.get("memberList") or data.get("MemberList")
                    or data.get("chatroomMemberList") or [])
        owner = str(_first(data, "chatroomOwner", "ChatRoomOwner", default=""))
        admins = set(data.get("adminWxid") or data.get("AdminWxid") or [])

        members: dict[str, GroupMember] = {}
        for m in raw_list:
            if not isinstance(m, dict):
                continue
            wxid = str(_first(m, "wxid", "Wxid", "userName", default=""))
            if not wxid:
                continue
            members[wxid] = GroupMember(
                wxid=wxid,
                # ⚠️ 群里展示要用**群昵称(displayName)**，不是好友昵称(nickName)
                display_name=str(_first(m, "displayName", "DisplayName", default="")),
                nickname=str(_first(m, "nickName", "NickName", default="")),
                is_owner=(wxid == owner),
                is_admin=(wxid in admins),
            )
        log.debug("拉取群成员 chat_id=%s 数量=%d", chat_id, len(members))
        return members
