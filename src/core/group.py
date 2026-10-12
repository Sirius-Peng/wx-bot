"""群聊语义处理 —— 各协议适配器共用的群聊核心逻辑。

═══════════════════════════════════════════════════════════════════
为什么单独抽一层？
  群聊消息在不同协议下的格式差异极大，且有几个业界公认的"坑"：
    ① 群消息的 sender 有时不在独立字段，而是藏在 content 前缀里
    ② @ 机器人的判定有三种以上格式（字段 / XML / 文本前缀）
    ③ 微信 @ 后面跟的是 U+2005（four-per-em space），不是普通空格
    ④ 群昵称（displayName）与好友昵称（nickName）是两回事
  把这四个坑集中到一个模块，各适配器只需做格式映射。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

# 微信群 ID 后缀
CHATROOM_SUFFIX = "@chatroom"

# 微信 @ 提及后的分隔符是 U+2005（four-per-em space），不是普通空格
# 这是最容易踩的坑：用普通空格 split 会导致 @ 检测失败
WX_AT_SEPARATOR = "\u2005"

# content 前缀形式的发送者：`wxid_xxxx:\n内容`
_SENDER_PREFIX_RE = re.compile(r"^([A-Za-z0-9_\-]{5,64}):\n")

# MsgSource XML 中的 @ 列表
_ATUSERSOURCE_RE = re.compile(r"<atuserlist>(.*?)</atuserlist>", re.S | re.I)

# @昵称 的匹配（昵称后可跟 U+2005 或普通空格或直接结束）
def _at_pattern(nickname: str) -> re.Pattern:
    return re.compile(r"@" + re.escape(nickname) + r"(?:\u2005|\s|$)")


def is_group_id(chat_id: str) -> bool:
    """判断是否群会话 ID。微信群 ID 统一以 @chatroom 结尾。"""
    return bool(chat_id) and chat_id.endswith(CHATROOM_SUFFIX)


@dataclass
class GroupMember:
    """群成员。注意区分两种昵称。"""
    wxid: str
    nickname: str = ""        # 好友昵称（全局）
    display_name: str = ""    # 群昵称（仅本群生效）—— 展示应优先用它
    is_owner: bool = False
    is_admin: bool = False

    @property
    def name(self) -> str:
        """展示名：群昵称优先，回落到好友昵称，最后回落到 wxid。"""
        return self.display_name or self.nickname or self.wxid


@dataclass
class GroupMemberCache:
    """带 TTL 的群成员缓存。群昵称会变，缓存必须能失效。"""
    members: dict[str, GroupMember] = field(default_factory=dict)
    fetched_at: float = 0.0
    ttl: float = 1800.0        # 30 分钟

    @property
    def expired(self) -> bool:
        return (time.time() - self.fetched_at) > self.ttl


class GroupMemberResolver:
    """群成员名称解析器。

    适配器只需实现 `_fetch_members(chat_id)`，本类负责缓存、失效与降级。
    """

    def __init__(self, ttl: float = 1800.0):
        self.ttl = ttl
        self._cache: dict[str, GroupMemberCache] = {}
        # 单成员临时缓存：拿不到群成员列表时，至少记住已知的昵称
        self._loose: dict[tuple[str, str], str] = {}

    async def resolve(self, chat_id: str, user_id: str) -> str:
        """解析某成员在该群的展示名（群昵称优先）。"""
        cache = await self._ensure(chat_id)
        m = cache.members.get(user_id)
        if m:
            return m.name
        # 降级：用临时缓存的昵称，最后回落到 wxid 短形式
        return self._loose.get((chat_id, user_id)) or _short_wxid(user_id)

    async def resolve_many(self, chat_id: str, user_ids: list[str]) -> dict[str, str]:
        cache = await self._ensure(chat_id)
        return {
            u: (cache.members[u].name if u in cache.members
                else self._loose.get((chat_id, u)) or _short_wxid(u))
            for u in user_ids
        }

    async def get(self, chat_id: str, user_id: str) -> Optional[GroupMember]:
        cache = await self._ensure(chat_id)
        return cache.members.get(user_id)

    def remember(self, chat_id: str, user_id: str, name: str) -> None:
        """适配器从消息里顺带拿到的昵称，回填缓存（省一次接口调用）。"""
        if name and user_id:
            self._loose[(chat_id, user_id)] = name

    def invalidate(self, chat_id: str) -> None:
        """群成员变动（入群/退群/改名）时调用。"""
        self._cache.pop(chat_id, None)

    async def _ensure(self, chat_id: str) -> GroupMemberCache:
        cache = self._cache.get(chat_id)
        if cache is None or cache.expired:
            try:
                members = await self._fetch_members(chat_id)
                cache = GroupMemberCache(members=members, fetched_at=time.time(), ttl=self.ttl)
                self._cache[chat_id] = cache
            except Exception:
                # 拉取失败不能让业务挂掉 —— 降级为临时缓存
                cache = cache or GroupMemberCache(fetched_at=time.time(), ttl=5.0)
        return cache

    async def _fetch_members(self, chat_id: str) -> dict[str, GroupMember]:
        """由具体适配器覆盖。默认返回空（降级到临时缓存）。"""
        return {}


def _short_wxid(wxid: str) -> str:
    """wxid 太长不适合展示，取短形式。"""
    if not wxid:
        return "未知成员"
    return wxid.split("@")[0][:12]


# ═══════════════════════════════════════════════════════════════════
#  @ 机器人检测 —— 三种格式全覆盖
# ═══════════════════════════════════════════════════════════════════

def detect_at_bot(
    content: str,
    bot_wxid: str,
    bot_names: Optional[list[str]] = None,
    at_list: Optional[list[str]] = None,
    msg_source: str = "",
) -> tuple[bool, str]:
    """判断消息是否 @ 了机器人，并返回剥离 @ 后的正文。

    按可靠性从高到低依次尝试三种格式：
      ① 结构化字段 at_list / AtWxidList（最可靠）
      ② MsgSource XML 里的 <atuserlist>
      ③ 正文文本前缀 @机器人昵称（兜底，依赖昵称匹配）

    返回 (是否@了机器人, 清理后的正文)
    """
    bot_names = [n for n in (bot_names or []) if n]
    text = content or ""

    # ① 结构化 @ 列表
    if at_list:
        normalized = {_normalize_wxid(x) for x in at_list if x}
        if bot_wxid and _normalize_wxid(bot_wxid) in normalized:
            return True, _strip_at_prefix(text, bot_names)

    # ② MsgSource XML
    if msg_source:
        m = _ATUSERSOURCE_RE.search(msg_source)
        if m:
            listed = {_normalize_wxid(x) for x in m.group(1).split(",") if x.strip()}
            if bot_wxid and _normalize_wxid(bot_wxid) in listed:
                return True, _strip_at_prefix(text, bot_names)

    # ③ 文本前缀兜底
    for name in bot_names:
        if _at_pattern(name).search(text):
            return True, _strip_at_prefix(text, bot_names)

    return False, text


def _normalize_wxid(raw: str) -> str:
    """不同协议对同一 wxid 的写法略有差异，统一归一化。

    已知变体：
      wxid_abc
      wxid_abc_username      （部分协议加后缀）
      wxid_abc@chatroom      （群 ID 混入）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.endswith("@chatroom"):
        return s
    if s.endswith("_username"):
        s = s[: -len("_username")]
    return s


def _strip_at_prefix(text: str, bot_names: list[str]) -> str:
    """剥离开头的 @机器人昵称，让 LLM 拿到干净正文。

    微信的 @ 分隔符是 U+2005，也可能是普通空格，两种都要处理。
    """
    out = text
    for name in bot_names:
        if not name:
            continue
        # 只剥离开头的 @，中间的 @ 保留（可能是 @ 其他人）
        out = re.sub(r"^\s*@" + re.escape(name) + r"(?:\u2005|\s)?", "", out)
    return out.strip()


# ═══════════════════════════════════════════════════════════════════
#  发送者前缀解析
# ═══════════════════════════════════════════════════════════════════

def parse_sender_prefix(content: str) -> tuple[str, str]:
    """解析 `wxid_xxx:\\n正文` 形式的发送者前缀。

    部分协议不提供独立的 sender 字段，发送者藏在 content 开头。
    这是群聊实现里最常见的 bug 来源 —— 不剥离会导致 LLM 看到
    "wxid_abc123: 你好" 这种脏数据。

    返回 (sender_wxid, 正文)。无前缀时 sender 为空串。
    """
    if not content:
        return "", ""
    m = _SENDER_PREFIX_RE.match(content)
    if not m:
        return "", content
    candidate = m.group(1)
    # 避免误伤 "注意:\nxxx" 这类正常文本 —— 要求像 wxid 或长随机串
    if not (candidate.startswith("wxid_") or len(candidate) >= 12):
        return "", content
    return candidate, content[m.end():]


# ═══════════════════════════════════════════════════════════════════
#  群事件
# ═══════════════════════════════════════════════════════════════════

# 需要让成员缓存失效的群事件 —— 不处理会导致昵称长期错误
MEMBER_CHANGING_EVENTS = {
    "GROUP_INVITE",            # 有人入群
    "REMOVE_FROM_GROUP",       # 被移出群聊
    "GROUP_DISMISS",           # 群解散
    "GROUP_NAME_CHANGE",       # 群名变更
    "SYSTEM",                  # 系统消息（含成员变动）
}


def should_invalidate_members(event_type: str) -> bool:
    return event_type in MEMBER_CHANGING_EVENTS
