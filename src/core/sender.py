"""拟人化发送器 —— 限流 + 随机延迟 + 语义分段。

微信风控特征非常明确：固定间隔、高频、大段文本。
这个模块的职责就是把机器人的行为"伪装"成真人打字节奏。
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
MAX_SEG_LEN = 800      # 单条消息最大长度，超出按语义切分

# 句子边界（中英文标点）
BOUNDARY = re.compile(r"[。！？；\n]|[.!?;]\s")


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
    text = text.strip()
    if len(text) <= max_len:
        return [text] if text else []

    segs, cur = [], ""
    for piece in re.split(r"(?<=[。！？；\n])", text):
        if len(cur) + len(piece) > max_len and cur:
            segs.append(cur.strip())
            cur = piece
        else:
            cur += piece
    if cur.strip():
        segs.append(cur.strip())
    return segs


class HumanLikeSender:
    """带限流与拟人化延迟的发送器。"""

    def __init__(self, adapter, global_rpm: int = GLOBAL_RPM, chat_rpm: int = CHAT_RPM):
        self.adapter = adapter
        self.global_bucket = TokenBucket(global_rpm)
        self.chat_buckets: dict[str, TokenBucket] = defaultdict(lambda: TokenBucket(chat_rpm))

    @staticmethod
    def human_delay(text: str) -> float:
        """模拟真人打字：基础 1.5s + 每字 0.03s + 0~2.5s 随机抖动。"""
        return 1.5 + 0.03 * len(text) + random.uniform(0, 2.5)

    async def send(self, chat_id: str, text: str, at: list[str] | None = None) -> list[str]:
        """发送文本，自动分段 + 限流 + 拟人延迟。返回各段 msg_id。"""
        if not text.strip():
            return []

        msg_ids: list[str] = []
        segs = split_by_semantics(text)
        for i, seg in enumerate(segs):
            await self.global_bucket.acquire()
            await self.chat_buckets[chat_id].acquire()
            await asyncio.sleep(self.human_delay(seg))
            # 只有第一段带 @，避免每段都 @ 打扰用户
            mention = at if i == 0 else None
            msg_id = await self.adapter.send_text(chat_id, seg, at=mention)
            msg_ids.append(msg_id)
        return msg_ids

    async def stream_send(self, chat_id: str, stream, first_threshold: int = 20,
                          next_threshold: int = 80, at: list[str] | None = None) -> str:
        """流式分段发送：首段阈值低（快速给反馈），后续按句子边界切。

        微信没有原生流式消息，这是最接近流式体验的落地方式。

        `at` 只在**第一段**生效 —— 群聊里 @ 提问者一次就够，
        每段都 @ 会刷屏打扰群成员。
        """
        buffer, full, first = "", "", True
        async for chunk in stream:
            buffer += chunk
            full += chunk
            threshold = first_threshold if first else next_threshold
            if len(buffer) >= threshold and BOUNDARY.search(buffer):
                await self.send(chat_id, buffer.strip(), at=at if first else None)
                buffer, first = "", False
        if buffer.strip():
            await self.send(chat_id, buffer.strip(), at=at if first else None)
        return full
