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

    def current_persona_space(self) -> str:
        active = getattr(self, 'active_space', None)
        value = active() if callable(active) else active
        return value if isinstance(value, str) else self.legacy_persona_space()

    def legacy_persona_space(self) -> str:
        value = getattr(self, 'legacy_space', '')
        value = value() if callable(value) else value
        return value if isinstance(value, str) else ''

    def message_persona_space(self, msg: WxMessage) -> str:
        return msg.persona_space or self.legacy_persona_space()

    def stamp_ingress(self, msg: WxMessage) -> WxMessage:
        """Only true ingress gets the active space; restored envelopes keep theirs."""
        if hasattr(self, 'active_space'):
            msg.persona_space = self.current_persona_space()
        return msg

    def on_activity(self, handler) -> None:
        """Observe private ingress before debounce, so proactive work yields immediately."""
        self._activity_handler = handler

    def on_turn_preparation(self, handler) -> None:
        self._turn_preparation_handler = handler

    def pending_turn(self, msg: WxMessage) -> WxMessage:
        """Read-only collector snapshot; concrete transports preserve their order."""
        return msg

    def collection_snapshot(self, msg: WxMessage) -> WxMessage:
        return self.pending_turn(msg)

    def note_activity(self, msg: WxMessage) -> None:
        prefetch=getattr(self,'prefetch_media',None)
        if prefetch is not None and msg.images:
            prefetch(msg)
        prepare = getattr(self, '_turn_preparation_handler', None)
        if prepare is not None:
            try:
                prepare(self.collection_snapshot(msg))
            except Exception as exc:
                import logging
                logging.getLogger(__name__).debug('Turn preparation skipped error_type=%s', type(exc).__name__)
        handler = getattr(self, '_activity_handler', None)
        if handler is not None:
            try:
                handler(msg)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning('Activity observation paused error_type=%s', type(exc).__name__)

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
