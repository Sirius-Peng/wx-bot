"""完整回复发送器 —— 限流 + 延迟 + 普通聊天格式整理。

模型可以流式生成，但聊天端只发送完整的一条回复，不将 token 流拆成气泡。
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from collections import defaultdict

# ── 风控红线 ──────────────────────────────────────────────
GLOBAL_RPM = 20        # 单账号全局上限：20 条/分钟
CHAT_RPM = 2           # 单会话上限：2 条/分钟（最关键的红线）
MAX_SEG_LEN = 800      # 仅供旧分段工具使用；默认回复不按此长度拆分

# 保留确有用途的代码、列表、引用、标题和表格格式。
STRUCTURED_LINE = re.compile(r"^(?:\s*(?:[-*+•]\s+|\d+[.)]\s+|\d+、|[一二三四五六七八九十]+、|#{1,6}\s+|>\s*|\|)| {4}|\t)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class TokenBucket:
    """令牌桶限流器。"""

    def __init__(self, rate_per_min: float, capacity: float | None = None):
        self.rate = rate_per_min / 60.0          # 每秒补充速率
        self.capacity = capacity or max(1.0, rate_per_min / 6)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def split_by_semantics(text: str, max_len: int = MAX_SEG_LEN) -> list[str]:
    """按标点语义切分长文本，避免把句子拦腰截断。"""
    if max_len < 1:
        raise ValueError("max_len must be positive")
    text = text.strip()
    if len(text) <= max_len:
        return [text] if text else []

    segs, cur = [], ""
    for piece in re.split(r"(?<=[。！？；\n])", text):
        if cur and len(cur) + len(piece) > max_len:
            segs.append(cur)
            cur = ""
        while len(piece) > max_len:
            segs.append(piece[:max_len])
            piece = piece[max_len:]
        cur += piece
    if cur:
        segs.append(cur)
    return [s for s in segs if s.strip()]


def normalize_reply_text(text: str) -> str:
    """合并普通段落的软换行；代码和结构化内容保留必要换行。

    不修改代码块正文，不按长度截断文本，不把英文单词拼在一起。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    prose: list[str] = []
    fence_char = ""
    fence_length = 0

    def flush_prose() -> None:
        if prose:
            paragraph = " ".join(prose)
            # 中文句子间的换行合并后无需额外空格；英文仍保留单词分隔。
            paragraph = re.sub(r"(?<=[\u4e00-\u9fff，。！？；：、]) (?=[\u4e00-\u9fff])",
                               "", paragraph)
            parts.append(paragraph)
            prose.clear()

    for line in text.strip("\n").split("\n"):
        if fence_char:
            parts.append(line)  # 代码正文的缩进、空行和尾部空格都不改。
            if re.fullmatch(r" {0,3}" + re.escape(fence_char) +
                            "{" + str(fence_length) + r",}\s*", line):
                fence_char = ""
            continue
        fence = FENCE.match(line)
        if fence:
            flush_prose()
            marker = fence.group(1)
            fence_char, fence_length = marker[0], len(marker)
            parts.append(line.rstrip())
        elif line.strip() and STRUCTURED_LINE.match(line):
            flush_prose()
            parts.append(line.rstrip())
        elif line.strip():
            prose.append(line.strip())
        # 普通文本的空行不产生新消息或多余段落。
    flush_prose()
    return "\n".join(parts)


from .conversation import ConversationChanged

class DeliveryError(RuntimeError):
    """The model reply exists but transport acceptance failed; retain it for retry."""


class HumanLikeSender:
    """带限流与拟人化延迟的发送器。"""

    def __init__(self, adapter, global_rpm: int | None = None, chat_rpm: int | None = None):
        self.adapter = adapter
        # 官方机器人通道用独立的有界限流；不继承旧个人号的30秒等待。
        ilink = getattr(adapter, "platform", "") == "ilink"
        global_rpm = global_rpm if global_rpm is not None else (60 if ilink else GLOBAL_RPM)
        chat_rpm = chat_rpm if chat_rpm is not None else (30 if ilink else CHAT_RPM)
        self.global_bucket = TokenBucket(global_rpm, capacity=2 if ilink else None)
        self.chat_buckets: dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(chat_rpm, capacity=2 if ilink else None))

    @staticmethod
    def human_delay(text: str) -> float:
        """兼容旧延迟钩子；默认即时提交，不人为模拟打字。"""
        return 0.0

    async def send(self, chat_id: str, text: str, at: list[str] | None = None) -> list[str]:
        """限流后一次发送完整文本；不按句子、换行或 800 字阈值拆消息。"""
        reply = normalize_reply_text(text)
        if not reply.strip():
            return []
        await self.global_bucket.acquire()
        await self.chat_buckets[chat_id].acquire()
        await asyncio.sleep(self.human_delay(reply))
        try:
            msg_id = await self.adapter.send_text(chat_id, reply, at=at)
        except ConversationChanged:
            raise
        except Exception as exc:
            raise DeliveryError("完整回复尚未被接口接受") from exc
        return [msg_id]

    async def stream_send(self, chat_id: str, stream, first_threshold: int = 20,
                          next_threshold: int = 80, at: list[str] | None = None) -> str:
        """接收完整模型流，再一次发送；归档文本与用户收到的内容一致。

        first_threshold / next_threshold 仅保留旧调用兼容，不再触发提前发送。
        生成中途失败时尚未发出任何内容，交由编排层发送一条失败提示。
        """
        chunks = [chunk async for chunk in stream]
        reply = normalize_reply_text("".join(chunks))
        if not reply.strip():
            raise ValueError("模型返回空回复")
        await self.send(chat_id, reply, at=at)
        return reply
