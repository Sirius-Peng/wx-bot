"""官方 iLink Bot API 适配器（微信 ClawBot / OpenClaw 微信渠道）。

═══════════════════════════════════════════════════════════════════
这是腾讯官方开放的协议 —— 不是逆向，合规、零封号风险。
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
import json
import logging
import os
import random
import struct
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from ..core.http import explain_error, make_client
from ..core.types import WxMessage
from .base import BaseAdapter, MessageHandler

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "1.0.2"

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
        # 每个会话最近一条入站消息的 context_token —— 回复时必须原样带回
        self._ctx_tokens: dict[str, str] = {}
        # 官方域名是公网地址，走系统代理没问题；make_client 会自动判断
        self._client = make_client(self.base_url, timeout=40.0)
        self._handler: Optional[MessageHandler] = None
        self._running = False

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
        await self._notify("notifystart")
        await self._refresh_config()
        self._running = True
        log.info("ILinkAdapter started, bot_id=%s owner=%s",
                 self.bot_id or "?", self.owner_user_id or "?")

    async def stop(self) -> None:
        self._running = False
        try:
            await self._notify("notifystop")
        except Exception:
            log.warning("notify stop failed", exc_info=True)
        await self._client.aclose()

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
        """长轮询主循环。服务端会 hold 住连接最多 35 秒，直到有新消息。

        与 Telegram getUpdates 同构：
          get_updates_buf 是游标，必须每轮更新，否则会重复收到消息。
        """
        while self._running:
            try:
                resp = await self._post(
                    "ilink/bot/getupdates",
                    {
                        "get_updates_buf": self._get_updates_buf,
                        "base_info": self._base_info(),
                    },
                )
            except httpx.TimeoutException:
                continue          # 长轮询超时属正常，立即重连
            except Exception:
                log.exception("getupdates 失败，2 秒后重试")
                await asyncio.sleep(2)
                continue

            # token 失效必须立即感知 —— 这是最致命的故障
            if resp.get("errcode") == STALE_TOKEN_ERRCODE:
                log.error("!!! bot_token 已失效，需要重新扫码登录 !!!")
                self._running = False
                return
            if resp.get("ret", 0) != 0:
                log.warning("getupdates ret=%s errmsg=%s", resp.get("ret"), resp.get("errmsg"))
                await asyncio.sleep(1)
                continue

            # 游标必须更新
            if resp.get("get_updates_buf"):
                self._get_updates_buf = resp["get_updates_buf"]
                self._save_cursor()

            msgs = resp.get("msgs") or []
            if not msgs and not resp.get("get_updates_buf"):
                # 服务端没有 hold 住连接（异常网关/限流时会立刻返回空），
                # 必须自己退避，否则会把接口刷成 429
                await asyncio.sleep(1.0)

            for raw in msgs:
                msg = self._normalize(raw)
                if msg is None:
                    continue
                # ★ 必须在下发之前记住 context_token：
                #   官方协议回复时要求原样带回，否则 sendmessage 关联不到会话
                self.remember_context(msg)
                if self._handler:
                    # 单条消息处理异常不能拖垮整个轮询循环
                    try:
                        await self._handler(msg)
                    except Exception:
                        log.exception("处理消息失败 msg_id=%s", msg.msg_id)

    # ── 消息归一化 ────────────────────────────────────────────

    def _normalize(self, raw: dict) -> Optional[WxMessage]:
        """把官方协议消息转成统一的 WxMessage。"""
        try:
            # 只处理用户发来的消息（message_type=1），忽略 BOT 自己的回显
            if raw.get("message_type") != MSG_TYPE_USER:
                return None

            items = raw.get("item_list") or []
            text, msg_type = "", "text"
            for it in items:
                t = it.get("type")
                if t == ITEM_TEXT:
                    text = (it.get("text_item") or {}).get("text", "")
                    msg_type = "text"
                    break
                if t == ITEM_IMAGE:
                    msg_type = "image"
                elif t == ITEM_VOICE:
                    msg_type = "voice"
                    # 语音自带转文字结果
                    text = (it.get("voice_item") or {}).get("text", "")
                elif t == ITEM_FILE:
                    msg_type = "file"
                elif t == ITEM_VIDEO:
                    msg_type = "video"

            from_user = raw.get("from_user_id") or ""
            group_id = raw.get("group_id")

            return WxMessage(
                msg_id=str(raw.get("message_id") or raw.get("client_id") or ""),
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
                msg_type=msg_type,  # type: ignore[arg-type]
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
        """发送文本。

        ⚠️ context_token 必填 —— 必须用该会话最近一条入站消息携带的 token，
        否则消息不会关联到正确的对话窗口。这里由上层通过 ctx_token 传入。
        """
        ctx_token = self._ctx_tokens.get(chat_id)
        if not ctx_token:
            log.warning("缺少 context_token，消息可能无法关联到会话 chat_id=%s", chat_id)

        resp = await self._post("ilink/bot/sendmessage", {
            "msg": {
                "to_user_id": chat_id,
                "message_type": MSG_TYPE_BOT,
                "message_state": STATE_FINISH,
                "context_token": ctx_token,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
            }
        })
        if resp.get("ret", 0) != 0:
            raise RuntimeError(f"发送失败 ret={resp.get('ret')} errmsg={resp.get('errmsg')}")
        return str(resp.get("message_id") or "")

    async def send_typing(self, chat_id: str, on: bool = True) -> None:
        """发送"正在输入"状态。

        这是官方协议独有的体验优势 —— 微信里机器人也能显示"对方正在输入"，
        极大缓解长回答的等待焦虑。typing_ticket 来自 getconfig。
        """
        if not self.typing_ticket:
            return
        try:
            await self._post("ilink/bot/sendtyping", {
                "ilink_user_id": chat_id,
                "typing_ticket": self.typing_ticket,
                "status": TYPING_ON if on else TYPING_OFF,
            })
        except Exception:
            log.debug("sendtyping 失败（不影响主流程）", exc_info=True)

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
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self._random_uin(),
        }
        if self.bot_token:
            h["Authorization"] = f"Bearer {self.bot_token}"
        return h

    async def _post(self, endpoint: str, body: dict) -> dict:
        url = f"{self.bot_base_url or self.base_url}/{endpoint}"
        # 长轮询接口不能用默认短超时
        timeout = 40.0 if "getupdates" in endpoint else 15.0
        r = await self._client.post(url, json=body, headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()

    async def _notify(self, which: str) -> None:
        """上报告知服务端本端启动/停止，便于服务端做在线状态对账。"""
        try:
            await self._post(f"ilink/bot/msg/{which}", {"base_info": self._base_info()})
        except Exception:
            log.debug("%s 上报失败", which, exc_info=True)

    async def _refresh_config(self) -> None:
        """获取 typing_ticket。"""
        try:
            resp = await self._post("ilink/bot/getconfig", {"base_info": self._base_info()})
            self.typing_ticket = resp.get("typing_ticket")
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
                self._get_updates_buf = json.loads(
                    self._cursor_file.read_text(encoding="utf-8")
                ).get("get_updates_buf", "")
            except json.JSONDecodeError:
                pass
        return bool(self.bot_token)

    def _save_cursor(self) -> None:
        self._cursor_file.write_text(
            json.dumps({"get_updates_buf": self._get_updates_buf}), encoding="utf-8"
        )

    # ── context_token 缓存（发送时必填）───────────────────────

    def remember_context(self, msg: WxMessage) -> None:
        """记录每个会话最近的 context_token，供回复时使用。"""
        if msg.context_token:
            self._ctx_tokens[msg.chat_id] = msg.context_token
