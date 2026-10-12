"""协议适配器统一契约。

所有底层协议（iPad 协议 / PC Hook / 企微官方回调）都必须实现这个接口，
上层业务只依赖这个抽象，从而实现"换协议不改业务"。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from ..core.types import WxMessage

MessageHandler = Callable[[WxMessage], Awaitable[None]]


class BaseAdapter(ABC):
    """微信协议适配器基类。

    每个适配器必须声明自己的能力边界（supports_private / supports_group），
    编排层据此把不同会话类型路由到不同适配器 —— 这是混合架构的基础。
    """

    platform: str = "base"

    # ── 能力声明 ──────────────────────────────────────────────
    # 官方 iLink Bot API：仅私聊（chatTypes: ["direct"]）
    # iPad 协议：私聊 + 群聊
    supports_private: bool = True
    supports_group: bool = False

    @abstractmethod
    async def start(self) -> None:
        """启动适配器：建立连接 / 注册回调 / 校验 URL。"""

    @abstractmethod
    async def stop(self) -> None:
        """优雅关闭：注销回调 / 断开连接。"""

    @abstractmethod
    def on_message(self, handler: MessageHandler) -> None:
        """注册消息处理器。适配器收到消息后归一化为 WxMessage 并回调。"""

    @abstractmethod
    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str:
        """发送文本消息，返回 msg_id。at 为需要 @ 的 wxid 列表。"""

    @abstractmethod
    async def send_image(self, chat_id: str, path_or_url: str) -> str:
        """发送图片消息，返回 msg_id。"""

    @abstractmethod
    async def get_group_member(self, chat_id: str, user_id: str) -> dict:
        """获取群成员信息（昵称等），用于群聊上下文标注。"""

    @abstractmethod
    async def is_healthy(self) -> bool:
        """健康检查：登录态是否有效。这是最关键的监控项。"""
