"""统一消息 DTO —— 所有协议适配器归一化到此结构。

这是"换协议不改业务"的关键：无论底层是官方 iLink 还是 OneBot，
上层的回复判定 / 上下文管理 / LLM 调用都只认这个结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

ChatType = Literal["private", "group"]
MsgType = Literal["text", "image", "voice", "file", "video", "system"]
Direction = Literal["in", "out"]


@dataclass
class WxMessage:
    msg_id: str                          # 全局唯一，用于幂等去重
    platform: str                        # ilink | onebot
    chat_type: ChatType                  # private / group
    chat_id: str                         # 私聊=对方 ID；群聊=群 ID
    sender_id: str                       # 发送者 ID
    sender_name: str                     # 发送者昵称（群聊上下文必须）
    bot_id: str                          # 机器人自己的 ID，用于判断 @自己、防回环
    content: str                         # 已剥离 @ 前缀的正文
    raw_content: str = ""                # 原始文本（保留 @ 信息）
    msg_type: MsgType = "text"
    quote_msg_id: Optional[str] = None   # 引用回复的目标 msg_id
    timestamp: int = 0                   # 毫秒时间戳
    is_at_me: bool = False               # 是否 @ 了机器人

    # ── 官方 iLink Bot API 专有字段 ────────────────────────────
    # 官方协议回复时必须原样带回 context_token，否则消息关联不到正确会话
    context_token: Optional[str] = None
    seq: Optional[int] = None            # iLink 消息序号
    session_id: Optional[str] = None
    group_id: Optional[str] = None       # 群号（OneBot 归一化时填入）

    images: list[dict] = field(default_factory=list)  # refs only, never base64 pixels
    content_parts: list[dict] = field(default_factory=list)  # text/image-index in original order
    batch_messages: list[dict] = field(default_factory=list)  # original envelopes

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def ctx_key(self) -> str:
        """上下文存储 key：群聊按群隔离，私聊按会话隔离。"""
        return f"ctx:{self.platform}:{self.chat_type}:{self.chat_id}"

    def render(self) -> str:
        """渲染成给 LLM 的文本。群聊必须标注发言人，否则模型分不清谁在说话。"""
        if self.is_group:
            return f"[{self.sender_name}]: {self.content}"
        return self.content
