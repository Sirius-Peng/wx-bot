"""混合路由 —— 官方协议与协议方案的分工调度。

核心思路（本方案的关键结论）：
    官方 iLink Bot API 合规、零封号，但 capabilities.chatTypes = ["direct"]，只支持私聊；
    群聊官方不支持，只能走 iPad 协议。

所以按会话类型分流：
    私聊  → ILinkAdapter   （官方，合规，零封号）
    群聊  → GewechatAdapter（协议，有封禁风险，但能力完整）

这样把"合规风险"压缩到只存在于群聊场景，私聊完全干净。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.types import WxMessage
from .base import BaseAdapter

log = logging.getLogger(__name__)


class HybridAdapterRouter(BaseAdapter):
    """按会话类型把收发请求分发到不同适配器。

    对外表现为一个普通适配器，内部持有多个后端。
    编排层（Orchestrator）完全无感知。
    """

    platform = "hybrid"
    supports_private = True
    supports_group = True

    def __init__(self, private_adapter: Optional[BaseAdapter] = None,
                 group_adapter: Optional[BaseAdapter] = None):
        self.private_adapter = private_adapter
        self.group_adapter = group_adapter
        self._handler = None
        # 记录每个会话由哪个后端接管，发送时才能路由回正确的通道
        self._owner: dict[str, BaseAdapter] = {}

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        if self.private_adapter:
            await self.private_adapter.start()
        if self.group_adapter:
            await self.group_adapter.start()

    async def stop(self) -> None:
        for a in (self.private_adapter, self.group_adapter):
            if a:
                try:
                    await a.stop()
                except Exception:
                    log.warning("关闭适配器失败", exc_info=True)

    def on_message(self, handler) -> None:
        """把同一个 handler 注册到所有后端，并在入站时记录归属。"""
        self._handler = handler
        for a in (self.private_adapter, self.group_adapter):
            if a:
                a.on_message(self._wrap(a))

    def _wrap(self, adapter: BaseAdapter):
        async def _dispatch(msg: WxMessage) -> None:
            self._owner[msg.chat_id] = adapter
            # 后端能力校验：官方协议收到群消息属于异常，直接丢弃并告警
            if msg.is_group and not adapter.supports_group:
                log.warning(
                    "适配器 %s 不支持群聊，丢弃群消息 chat_id=%s",
                    adapter.platform, msg.chat_id,
                )
                return
            if self._handler:
                await self._handler(msg)
        return _dispatch

    # ── 出站：按会话归属路由 ──────────────────────────────────

    def _pick(self, chat_id: str, is_group: bool) -> BaseAdapter:
        if chat_id in self._owner:
            return self._owner[chat_id]
        # 无归属记录时按会话类型兜底
        if is_group and self.group_adapter:
            return self.group_adapter
        return self.private_adapter or self.group_adapter  # type: ignore[return-value]

    async def send_text(self, chat_id: str, text: str, at: list[str] | None = None) -> str:
        is_group = "@chatroom" in chat_id or chat_id.endswith("@chatroom")
        return await self._pick(chat_id, is_group).send_text(chat_id, text, at=at)

    async def send_image(self, chat_id: str, path_or_url: str) -> str:
        is_group = "@chatroom" in chat_id
        return await self._pick(chat_id, is_group).send_image(chat_id, path_or_url)

    async def get_group_member(self, chat_id: str, user_id: str) -> dict:
        if self.group_adapter:
            return await self.group_adapter.get_group_member(chat_id, user_id)
        return {}

    async def is_healthy(self) -> bool:
        checks = []
        if self.private_adapter:
            checks.append(await self.private_adapter.is_healthy())
        if self.group_adapter:
            checks.append(await self.group_adapter.is_healthy())
        return all(checks) if checks else False
