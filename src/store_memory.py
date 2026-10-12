"""内存版存储实现 —— 用于本地跑通链路与单元测试。

生产环境请替换为 RedisStore（热数据）+ PgStore（温/冷数据），
接口签名保持一致即可无缝切换。
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Optional

from .core.trigger import MODE_MENTION_ONLY, GroupConfig, default_group_config
from .core.types import WxMessage


class MemoryStore:
    """线程/协程内可用的极简存储。仅供开发与测试。"""

    def __init__(self, window_size: int = 40):
        self.window_size = window_size
        self._seen: dict[str, float] = {}
        self._ctx: dict[str, list[dict]] = defaultdict(list)
        self._recent_msgs: dict[str, list[WxMessage]] = defaultdict(list)
        self._summary: dict[str, str] = {}
        self._cooldown: dict[str, float] = {}
        self._cooldown_sec: dict[str, int] = {}
        self._hourly: dict[str, int] = defaultdict(int)
        self._configs: dict[str, GroupConfig] = {}
        self._archive: list[dict] = []
        # 未单独配置的群使用的默认策略
        self.default_mode: str = MODE_MENTION_ONLY

    def set_default_mode(self, mode: str) -> None:
        self.default_mode = mode

    # ── 幂等 ──────────────────────────────────────────────────

    async def mark_seen(self, msg_id: str) -> bool:
        """返回 True 表示首次见到；False 表示重复。"""
        if not msg_id:
            return True
        now = time.time()
        # 顺带清理 1 小时前的记录
        self._seen = {k: v for k, v in self._seen.items() if now - v < 3600}
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = now
        return True

    # ── 上下文 ────────────────────────────────────────────────

    async def get_recent(self, key: str) -> list[dict]:
        """返回上下文问答对（供 ContextManager 装配 messages）。"""
        return list(self._ctx.get(key, []))

    async def get_recent_msgs(self, chat_id: str, limit: int = 10) -> list[WxMessage]:
        """返回最近的原始消息（供 TriggerRouter 判断是否处于对话中）。"""
        return list(self._recent_msgs.get(chat_id, []))[-limit:]

    async def remember_msg(self, msg: WxMessage) -> None:
        """把入站原始消息追加到滑动窗口。"""
        bucket = self._recent_msgs[msg.chat_id]
        bucket.append(msg)
        if len(bucket) > self.window_size:
            del bucket[: len(bucket) - self.window_size]

    async def push(self, key: str, turn: dict) -> None:
        self._ctx[key].append(turn)
        if len(self._ctx[key]) > self.window_size:
            self._ctx[key] = self._ctx[key][-self.window_size:]

    async def get_summary(self, key: str) -> str:
        return self._summary.get(key, "")

    async def set_summary(self, key: str, text: str) -> None:
        self._summary[key] = text

    async def token_count(self, key: str) -> int:
        from .core.context import approx_tokens
        return sum(approx_tokens(t.get("user", "")) + approx_tokens(t.get("assistant", ""))
                   for t in self._ctx.get(key, []))

    async def pop_oldest(self, key: str, keep: int = 6) -> list[dict]:
        turns = self._ctx.get(key, [])
        if len(turns) <= keep:
            return []
        old, self._ctx[key] = turns[:-keep], turns[-keep:]
        return old

    # ── 触发控制 ──────────────────────────────────────────────

    async def in_cooldown(self, chat_id: str) -> bool:
        """冷却是否生效。cooldown_sec = 0 表示不限速。"""
        window = self._cooldown_sec.get(chat_id, 0)
        if window <= 0:
            return False
        last = self._cooldown.get(chat_id, 0)
        return time.time() - last < window

    async def set_cooldown(self, chat_id: str, sec: int) -> None:
        self._cooldown[chat_id] = time.time()
        self._cooldown_sec[chat_id] = sec

    async def over_hourly_quota(self, chat_id: str, limit: int) -> bool:
        return self._hourly.get(chat_id, 0) >= limit

    async def bump_hourly(self, chat_id: str) -> None:
        self._hourly[chat_id] += 1

    # ── 配置 / 归档 / 统计 ────────────────────────────────────

    async def get_group_config(self, chat_id: str) -> GroupConfig:
        """取群配置。未配置过的群使用全局默认策略。"""
        return self._configs.get(chat_id) or default_group_config(chat_id, self.default_mode)

    def set_group_config(self, cfg: GroupConfig) -> None:
        self._configs[cfg.chat_id] = cfg

    async def archive(self, msg: WxMessage, direction: str = "in") -> None:
        self._archive.append({
            "msg_id": msg.msg_id, "chat_id": msg.chat_id, "direction": direction,
            "sender": msg.sender_id, "content": msg.content, "ts": msg.timestamp,
        })

    async def archive_reply(self, msg: WxMessage, reply: str) -> None:
        self._archive.append({
            "msg_id": f"out:{msg.msg_id}", "chat_id": msg.chat_id, "direction": "out",
            "sender": msg.bot_id, "content": reply, "ts": int(time.time() * 1000),
        })

    async def record_usage(self, chat_id: str, model: str, usage, latency: float) -> None:
        # 实际实现写入 llm_usage 表
        pass

    async def flush_loop(self) -> None:
        """占位：生产环境定期把 Redis 热数据刷盘到 PostgreSQL。"""
        while True:
            await __import__("asyncio").sleep(300)
