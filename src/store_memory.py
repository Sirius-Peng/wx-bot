"""Private atomic conversation storage; an omitted path selects in-memory tests."""
from __future__ import annotations

import asyncio
import json,os,tempfile,copy,re,shutil,logging
from pathlib import Path
import time
from collections import defaultdict
from typing import Optional

from .core.types import WxMessage
from .core.session_media import SessionMedia


class MemoryStore:
    """Persist complete turns until successful background compaction."""

    def __init__(self, window_size: int = 40, path=None):
        self.window_size = window_size
        self._seen: dict[str, float] = {}
        self._ctx: dict[str, list[dict]] = defaultdict(list)
        self._summary: dict[str, str] = {}
        self._summary_sources: dict[str, list] = {}
        self._suppression_markers: list[dict] = []
        self._sessions: dict[str, dict] = {}
        self._session_revisions: dict[str, int] = {}
        self._extra: dict = {}
        self.revision = 0
        self._archive: list[dict] = []
        self.path=Path(path) if path else None
        self._session_media=SessionMedia(self.path)
        if self.path and self.path.exists():
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            self._extra = {k:copy.deepcopy(v) for k,v in raw.items() if k not in {
                'version', 'contexts', 'summaries', 'summary_sources', 'suppressions',
                'revision', 'sessions', 'session_revisions'}}
            revision = raw.get('revision', 0)
            self.revision = revision if type(revision) is int and revision >= 0 else 0
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
                self._summary.update({k:v for k,v in raw.get('summaries',{}).items()
                    if self._summary_sources.get(k) and not any(
                        self._source_scope(marker,k,self._summary_sources.get(k)) != 'unrelated'
                        for marker in self._suppression_markers)})
            sessions = raw.get('sessions', {})
            stamps = raw.get('session_revisions', {})
            if isinstance(sessions, dict):
                for key, session in sessions.items():
                    if not isinstance(key, str) or not self._valid_session(session):continue
                    stamp = stamps.get(key) if isinstance(stamps, dict) else None
                    trusted = type(stamp) is int and stamp == self.revision
                    # Older imported prefixes have no forgetting provenance. A
                    # known barrier must not be bypassed by loading that prefix.
                    scopes=[self._source_scope(m,key,session.get('covered_source_ids'))
                            for m in self._suppression_markers]
                    affected = any(self._marker_applies(m,key) or scope!='unrelated'
                                   for m,scope in zip(self._suppression_markers,scopes))
                    if (self._session_blocked(key,session) or 'unknown' in scopes or
                            (affected and not trusted)):continue
                    self._sessions[key] = copy.deepcopy(session)
                    if type(stamp) is int and stamp >= 0:self._session_revisions[key] = stamp

    def _save(self):
        if not self.path:return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=self.path.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump({**self._extra,'version':3,'contexts':dict(self._ctx),'summaries':dict(self._summary),
                           'summary_sources':self._summary_sources,'suppressions':self._suppression_markers,
                           'revision':self.revision,'sessions':self._sessions,
                           'session_revisions':self._session_revisions},f,ensure_ascii=False)
                f.flush();os.fsync(f.fileno())
            os.chmod(tmp,0o600);os.replace(tmp,self.path)
        finally:Path(tmp).unlink(missing_ok=True)

    async def compact(self,key,prefix,summary):
        current=self._ctx.get(key,[])
        if current[:len(prefix)]!=prefix:return False
        old_summary=self._summary.get(key,'');old_turns=list(current)
        self._ctx[key]=current[len(prefix):];self._summary[key]=summary
        old_sources=self._summary_sources.get(key, [])
        old_session=self._sessions.pop(key,None)
        old_session_revision=self._session_revisions.pop(key,None)
        self._summary_sources[key]=list(dict.fromkeys([*old_sources, *[t.get('turn_id', '') for t in prefix if t.get('turn_id')]])) or ['manual-context']
        try:self._save()
        except BaseException:
            self._ctx[key]=old_turns;self._summary[key]=old_summary;self._summary_sources[key]=old_sources
            self._restore_session(key,old_session,old_session_revision)
            raise
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
    def _valid_session(session):
        """Validate the envelope, not provider-specific optional message fields."""
        if not isinstance(session, dict) or not isinstance(session.get('messages'), list):return False
        for message in session['messages']:
            if not isinstance(message, dict) or not isinstance(message.get('role'),str) or message['role'] not in {
                    'system','developer','user','assistant','tool','function'}:return False
            content=message.get('content')
            if content is not None and not isinstance(content,(str,list)):return False
            if isinstance(content,list) and any(not isinstance(part,dict) for part in content):return False
        sources=session.get('covered_source_ids', [])
        if not isinstance(sources,list) or any(not isinstance(s,str) for s in sources):return False
        if 'fingerprint' in session and not isinstance(session['fingerprint'],str):return False
        for field in ('token_estimate','turn_count','generation'):
            if field in session and (type(session[field]) is not int or session[field]<0):return False
        try:
            # Reject values that change shape on disk (tuples/non-string keys),
            # cycles and non-finite numbers while retaining unknown JSON fields.
            return json.loads(json.dumps(session,ensure_ascii=False,allow_nan=False)) == session
        except (TypeError,ValueError,OverflowError,RecursionError):return False

    def _session_blocked(self,key,session):
        if not self._suppression_markers:return False
        for message in session['messages']:
            content=message.get('content')
            if isinstance(content,str) and self._blocked(key,content):return True
            if self._blocked(key,json.dumps(message,ensure_ascii=False)):return True
        return False

    def _restore_session(self,key,session,revision):
        if session is None:self._sessions.pop(key,None)
        else:self._sessions[key]=session
        if revision is None:self._session_revisions.pop(key,None)
        else:self._session_revisions[key]=revision

    async def get_session(self,key: str) -> dict | None:
        session=self._sessions.get(key)
        if session is None:return None
        try:return self._session_media.thaw(session)
        except (ValueError,OSError,UnicodeError):
            # A missing/corrupt imported pixel blob is not permission to invent
            # an image input. Raw text history remains available for rebuilding.
            self._sessions.pop(key,None)
            self._session_revisions.pop(key,None)
            logging.getLogger(__name__).warning('Session media unavailable; rebuild from raw conversation')
            return None

    async def set_session(self,key: str,session: dict,expected_revision=None) -> bool:
        """Persist one complete prefix only if no forgetting barrier overtook it."""
        if expected_revision is not None and expected_revision != self.revision:return False
        if not isinstance(key,str):raise ValueError('invalid conversation session key')
        if not self._valid_session(session):raise ValueError('invalid conversation session')
        value=self._session_media.freeze(session)
        if self._session_blocked(key,value):return False
        before=self._sessions.get(key)
        previous_revision=self._session_revisions.get(key)
        self._sessions[key]=value
        self._session_revisions[key]=self.revision
        try:self._save()
        except BaseException:
            self._restore_session(key,before,previous_revision)
            raise
        return True

    async def clear_session(self,key: str) -> None:
        # Advance even when the prefix is absent: an in-flight build may exist.
        previous=self.revision
        before=self._sessions.pop(key,None)
        stamp=self._session_revisions.pop(key,None)
        self.revision+=1
        try:self._save()
        except BaseException:
            self.revision=previous
            self._restore_session(key,before,stamp)
            raise

    @staticmethod
    def _marker_applies(marker, key):
        return any(a.get('owner_id') in ('', 'global', '*', 'all') or
                   ((not a.get('platform') or f":{a.get('platform')}:" in key) and
                    (key.endswith(':' + str(a.get('owner_id', ''))) or
                     key.endswith(':p_' + str(a.get('owner_id', '')))))
                   for a in marker.get('accounts', []))

    @staticmethod
    def _source_scope(marker,key,source_ids):
        """Classify group provenance as affected, unrelated, or unknown.

        Source IDs use the canonical platform:kind:chat:sender:message structure.
        They are not substring identifiers: sender 123 is unrelated to 1234,
        and a sender from another chat/platform cannot attest to this group.
        Missing or partial legacy provenance cannot prove a lossy summary safe.
        """
        parts=key.split(':',3)
        if len(parts)!=4 or parts[0]!='ctx' or parts[2]!='group':return 'unrelated'
        _,platform,_,chat=parts
        accounts=[a for a in marker.get('accounts',[]) if
                  a.get('owner_id') in ('','global','*','all') or
                  not a.get('platform') or a['platform']==platform]
        if not accounts:return 'unrelated'
        if any(a.get('owner_id') in ('','global','*','all') for a in accounts):return 'affected'
        if not isinstance(source_ids,list) or not source_ids:return 'unknown'
        owners=set()
        for source in source_ids:
            if not isinstance(source,str):return 'unknown'
            fields=source.split(':',4)
            if (len(fields)!=5 or fields[:3]!=[platform,'group',chat] or
                    not fields[3] or any(c.isspace() for c in fields[3]) or
                    not fields[4].strip()):return 'unknown'
            owners.add(fields[3])
        return 'affected' if any(str(a.get('owner_id','')) in owners for a in accounts) else 'unrelated'

    def _blocked(self, key, text):
        from .core.memory import PersonaMemory
        normalized = PersonaMemory._normalized(text)
        for marker in self._suppression_markers:
            relevant = self._marker_applies(marker, key)
            from .core.memory import PersonaMemory
            slot = PersonaMemory._fact_slot('fact', str(text))
            if relevant and slot and marker.get('fact_key') == slot: return True
            # Group memories carry a group scope; user evidence in groups uses
            # exact source text instead of accidentally scrubbing another person.
            for value in [marker.get('content', ''), marker.get('evidence', ''), *marker.get('source_texts', [])]:
                token = PersonaMemory._normalized(value)
                if token and token in normalized and (relevant or ':group:' in key): return True
        return False

    def _control_snapshot(self):
        return (copy.deepcopy(self._ctx),dict(self._summary),copy.deepcopy(self._summary_sources),
                copy.deepcopy(self._suppression_markers),copy.deepcopy(self._sessions),
                dict(self._session_revisions),self.revision)

    def _restore_control(self, previous):
        (self._ctx,self._summary,self._summary_sources,self._suppression_markers,
         self._sessions,self._session_revisions,self.revision)=previous

    def _apply_suppression(self,marker,*,synced=False):
        # A retained ID with expanded linked accounts is a new barrier for that
        # scope, not proof that its newly affected prefix was already scrubbed.
        known = any(m.get('id') == marker.get('id') and m == marker for m in self._suppression_markers)
        self._suppression_markers = list({m["id"]:m for m in [*self._suppression_markers, marker]}.values())
        changed = False
        changed_keys=set()
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
            sourced=any(self._source_scope(marker,key,
                turn.get('source_ids',[turn['turn_id']] if turn.get('turn_id') else []))!='unrelated' for turn in turns)
            affected = self._marker_applies(marker, key) or sourced or (':group:' in key and (removed or assistant_leak))
            # Assistant prose can paraphrase facts with no exact source span.
            # Preserve allowed original user experience, never trust an old reply
            # merely because it used different wording for the forgotten fact.
            if affected and (not known or removed or assistant_leak):
                kept = [{**t, 'assistant':''} for t in kept if t.get('user')]
            if kept != turns:changed_keys.add(key)
            changed = changed or kept != turns
            self._ctx[key] = kept
        discard=[]
        for key,session in self._sessions.items():
            blocked=self._session_blocked(key,session)
            source_scope=self._source_scope(marker,key,session.get('covered_source_ids'))
            affected=(self._marker_applies(marker,key) or
                      source_scope!='unrelated' or
                      (':group:' in key and blocked))
            trusted=self._session_revisions.get(key)==self.revision
            if affected and (not synced or not known or not trusted or blocked or
                             source_scope=='unknown' or key in changed_keys):
                discard.append(key)
        if synced and known and not changed and not discard:return False
        # The revision changes before deleting any cached prefix. A build already
        # in flight cannot restore it with its former expected_revision.
        self.revision += 1
        for key in discard:
            self._sessions.pop(key,None)
            self._session_revisions.pop(key,None)
        # Summaries are lossy: remove rather than ask another model to "forget"
        # an unknowable substring. In-flight compaction is revision-guarded.
        self._summary.clear()
        self._summary_sources.clear()
        return True

    def suppress(self, marker):
        previous=self._control_snapshot()
        try:
            self._apply_suppression(marker)
            self._save()
        except BaseException:
            self._restore_control(previous)
            raise

    def sync_suppressions(self, markers):
        previous=self._control_snapshot()
        active_ids = {m.get('id') for m in markers}
        before=list(self._suppression_markers)
        self._suppression_markers = [m for m in before if m.get('id') in active_ids]
        try:
            # Releasing a barrier permits new evidence, not old in-flight writes.
            if len(before)!=len(self._suppression_markers):self.revision+=1
            for marker in markers:
                self._apply_suppression(marker,synced=True)
            self._save()
        except BaseException:
            self._restore_control(previous)
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
        before_sources=self._summary_sources.get(key)
        session=self._sessions.pop(key,None)
        stamp=self._session_revisions.pop(key,None)
        self._summary[key] = text
        self._summary_sources[key] = [t.get('turn_id', 'manual-context') for t in self._ctx.get(key, [])] or ['manual-context']
        try:
            self._save()
        except BaseException:
            self._summary[key] = before
            if before_sources is None:self._summary_sources.pop(key,None)
            else:self._summary_sources[key]=before_sources
            self._restore_session(key,session,stamp)
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
        session=self._sessions.pop(key,None)
        stamp=self._session_revisions.pop(key,None)
        try:self._save()
        except BaseException:
            self._ctx[key]=turns
            self._restore_session(key,session,stamp)
            raise
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
