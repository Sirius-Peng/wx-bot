"""协议适配器统一契约。

所有底层协议（官方 iLink / OneBot 等）都实现这个接口，
上层业务只依赖这个抽象，从而做到"换协议不改业务"。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from ..core.types import WxMessage

MessageHandler = Callable[[WxMessage], Awaitable[None]]


class BaseAdapter(ABC):
    """消息通道适配器基类。"""

    platform: str = "base"

    @abstractmethod
    async def start(self) -> None:
        """启动适配器：建立连接 / 注册回调 / 校验凭据。"""

    @abstractmethod
    async def stop(self) -> None:
        """优雅关闭：注销回调 / 断开连接。"""

    @abstractmethod
    def on_message(self, handler: MessageHandler) -> None:
        """注册消息处理器。适配器收到消息后归一化为 WxMessage 并回调。"""

    @abstractmethod
    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str:
        """发送文本消息，返回 msg_id。at 为需要 @ 的用户 ID 列表。"""

    @abstractmethod
    async def send_image(self, chat_id: str, path_or_url: str) -> str:
        """发送图片消息，返回 msg_id。"""

    @abstractmethod
    async def is_healthy(self) -> bool:
        """健康检查：登录态是否有效。这是最关键的监控项。"""
