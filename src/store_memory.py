"""Private atomic conversation storage; an omitted path selects in-memory tests."""
from __future__ import annotations

import asyncio
import json,os,tempfile
from pathlib import Path
import time
from collections import defaultdict
from typing import Optional

from .core.types import WxMessage


class MemoryStore:
    """Persist complete turns until successful background compaction."""

    def __init__(self, window_size: int = 40, path=None):
        self.window_size = window_size
        self._seen: dict[str, float] = {}
        self._ctx: dict[str, list[dict]] = defaultdict(list)
        self._summary: dict[str, str] = {}
        self._archive: list[dict] = []
        self.path=Path(path) if path else None
        if self.path and self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            self._ctx.update({k:list(v) for k,v in raw.get('contexts',{}).items()})
            self._summary.update(raw.get('summaries',{}))

    def _save(self):
        if not self.path:return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=self.path.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump({'version':2,'contexts':dict(self._ctx),'summaries':dict(self._summary)},f,ensure_ascii=False)
                f.flush();os.fsync(f.fileno())
            os.chmod(tmp,0o600);os.replace(tmp,self.path)
        finally:Path(tmp).unlink(missing_ok=True)

    async def compact(self,key,prefix,summary):
        current=self._ctx.get(key,[])
        if current[:len(prefix)]!=prefix:return False
        old_summary=self._summary.get(key,'');old_turns=list(current)
        self._ctx[key]=current[len(prefix):];self._summary[key]=summary
        try:self._save()
        except BaseException:self._ctx[key]=old_turns;self._summary[key]=old_summary;raise
        return True

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

    async def forget_seen(self, msg_id: str) -> None:
        self._seen.pop(msg_id, None)

    # ── 上下文 ────────────────────────────────────────────────

    async def get_recent(self, key: str) -> list[dict]:
        """返回上下文问答对（供 ContextManager 装配 messages）。"""
        return list(self._ctx.get(key, []))

    async def push(self, key: str, turn: dict) -> None:
        if turn.get('turn_id') and any(t.get('turn_id')==turn['turn_id'] for t in self._ctx[key]):return
        before=list(self._ctx[key]);self._ctx[key].append(turn)
        try:self._save()
        except BaseException:self._ctx[key]=before;raise

    async def get_summary(self, key: str) -> str:
        return self._summary.get(key, "")

    async def set_summary(self, key: str, text: str) -> None:
        before = self._summary.get(key, "")
        self._summary[key] = text
        try:
            self._save()
        except Exception:
            self._summary[key] = before
            raise

    async def token_count(self, key: str) -> int:
        from .core.context import approx_tokens
        return sum(approx_tokens(t.get("user", "")) + approx_tokens(t.get("assistant", ""))
                   for t in self._ctx.get(key, []))

    async def pop_oldest(self, key: str, keep: int = 6) -> list[dict]:
        turns = self._ctx.get(key, [])
        if len(turns) <= keep:
            return []
        old, self._ctx[key] = turns[:-keep], turns[-keep:]
        self._save()
        return old

    # ── 归档 / 统计 ───────────────────────────────────────────

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
            await asyncio.sleep(300)
