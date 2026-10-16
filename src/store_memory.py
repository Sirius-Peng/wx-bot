"""Private atomic conversation storage; an omitted path selects in-memory tests."""
from __future__ import annotations

import asyncio
import json,os,tempfile,copy,re,shutil
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
        self._summary_sources: dict[str, list] = {}
        self._suppression_markers: list[dict] = []
        self.revision = 0
        self._archive: list[dict] = []
        self.path=Path(path) if path else None
        if self.path and self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            version = raw.get('version', 1)
            if version < 3:
                # Hidden prefix keeps runtime's context-*.json scans from treating
                # an immutable migration backup as another live context file.
                backup = self.path.with_name('.' + self.path.stem + f'.v{version}-backup.json')
                if not backup.exists():
                    shutil.copy2(self.path, backup)
                    os.chmod(backup, 0o600)
            self._ctx.update({k:list(v) for k,v in raw.get('contexts',{}).items()})
            self._suppression_markers = raw.get('suppressions', [])
            self._summary_sources.update(raw.get('summary_sources', {}))
            # Old summaries have no original-source provenance and cannot be safely
            # amended after forgetting; rebuild them from allowed raw turns.
            if raw.get('version', 1) >= 3:
                self._summary.update({k:v for k,v in raw.get('summaries',{}).items() if self._summary_sources.get(k)})

    def _save(self):
        if not self.path:return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=self.path.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump({'version':3,'contexts':dict(self._ctx),'summaries':dict(self._summary), 'summary_sources':self._summary_sources,'suppressions':self._suppression_markers},f,ensure_ascii=False)
                f.flush();os.fsync(f.fileno())
            os.chmod(tmp,0o600);os.replace(tmp,self.path)
        finally:Path(tmp).unlink(missing_ok=True)

    async def compact(self,key,prefix,summary):
        current=self._ctx.get(key,[])
        if current[:len(prefix)]!=prefix:return False
        old_summary=self._summary.get(key,'');old_turns=list(current)
        self._ctx[key]=current[len(prefix):];self._summary[key]=summary
        old_sources=self._summary_sources.get(key, [])
        self._summary_sources[key]=list(dict.fromkeys([*old_sources, *[t.get('turn_id', '') for t in prefix if t.get('turn_id')]])) or ['manual-context']
        try:self._save()
        except BaseException:self._ctx[key]=old_turns;self._summary[key]=old_summary;self._summary_sources[key]=old_sources;raise
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

    @staticmethod
    def _marker_applies(marker, key):
        return any(a.get('owner_id') in ('', 'global', '*', 'all') or
                   ((not a.get('platform') or f":{a.get('platform')}:" in key) and
                    (key.endswith(':' + str(a.get('owner_id', ''))) or
                     key.endswith(':p_' + str(a.get('owner_id', '')))))
                   for a in marker.get('accounts', []))

    def _blocked(self, key, text):
        normalized = re.sub(r'[^\w\u4e00-\u9fff]+', '', str(text)).lower()
        for marker in self._suppression_markers:
            relevant = self._marker_applies(marker, key)
            from .core.memory import PersonaMemory
            slot = PersonaMemory._fact_slot('fact', str(text))
            if relevant and slot and marker.get('fact_key') == slot: return True
            # Group memories carry a group scope; user evidence in groups uses
            # exact source text instead of accidentally scrubbing another person.
            for value in [marker.get('content', ''), marker.get('evidence', ''), *marker.get('source_texts', [])]:
                token = re.sub(r'[^\w\u4e00-\u9fff]+', '', value).lower()
                if token and token in normalized and (relevant or ':group:' in key): return True
        return False

    def suppress(self, marker):
        known = any(m.get('id') == marker.get('id') for m in self._suppression_markers)
        previous = (copy.deepcopy(self._ctx), dict(self._summary), copy.deepcopy(self._summary_sources), list(self._suppression_markers))
        self._suppression_markers = list({m["id"]:m for m in [*self._suppression_markers, marker]}.values())
        changed = False
        for key, turns in list(self._ctx.items()):
            kept = []
            removed = False
            assistant_leak = False
            for turn in turns:
                if self._blocked(key, turn.get('user', '')):
                    removed = True
                    continue
                if self._blocked(key, turn.get('assistant', '')):
                    assistant_leak = True
                    turn = {**turn, 'assistant':''}
                kept.append(turn)
            affected = self._marker_applies(marker, key) or (':group:' in key and (removed or assistant_leak))
            # Assistant prose can paraphrase facts with no exact source span.
            # Preserve allowed original user experience, never trust an old reply
            # merely because it used different wording for the forgotten fact.
            if affected and (not known or removed or assistant_leak):
                kept = [{**t, 'assistant':''} for t in kept if t.get('user')]
            changed = changed or kept != turns
            self._ctx[key] = kept
        if known and not changed: return
        self.revision += 1
        # Summaries are lossy: remove rather than ask another model to "forget"
        # an unknowable substring. In-flight compaction is revision-guarded.
        self._summary.clear()
        self._summary_sources.clear()
        try: self._save()
        except BaseException:
            self._ctx, self._summary, self._summary_sources, self._suppression_markers = previous
            raise

    def sync_suppressions(self, markers):
        previous = list(self._suppression_markers)
        active_ids = {m.get('id') for m in markers}
        self._suppression_markers = [m for m in previous if m.get('id') in active_ids]
        try:
            for marker in markers:
                self.suppress(marker)
            self._save()
        except BaseException:
            self._suppression_markers = previous
            raise

    async def push(self, key: str, turn: dict) -> None:
        if self._blocked(key, turn.get('user', '') + '\n' + turn.get('assistant', '')): return
        before = copy.deepcopy(self._ctx[key])
        turns = self._ctx[key]
        turn = dict(turn)
        turn_id = turn.get('turn_id')
        existing = next((t for t in turns if turn_id and t.get('turn_id') == turn_id), None)
        if existing is not None:
            # Accepted user-only experience is completed, not duplicated. A retry
            # with an empty assistant must not erase an already delivered reply.
            if not turn.get('assistant') and existing.get('assistant'):
                turn['assistant'] = existing['assistant']
            existing.update(turn)
        else:
            turns.append(turn)
        try: self._save()
        except BaseException:
            self._ctx[key] = before
            raise

    async def get_summary(self, key: str) -> str:
        return self._summary.get(key, "")

    async def set_summary(self, key: str, text: str) -> None:
        before = self._summary.get(key, "")
        self._summary[key] = text
        self._summary_sources[key] = [t.get('turn_id', 'manual-context') for t in self._ctx.get(key, [])] or ['manual-context']
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
