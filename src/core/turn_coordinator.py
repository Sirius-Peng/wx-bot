"""Durable turn revisions and optional follow-up queue, shared by both transports.

Transport inboxes remain authoritative for raw/media delivery. This ledger stores
references and bounded text only; a sending/uncertain continuation is never replayed.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from .conversation import ConversationChanged, members
from .types import WxMessage

TERMINAL = {'accepted', 'context_only', 'superseded', 'uncertain'}


class ConversationStateError(RuntimeError):
    """No transport may submit until a durable reservation has succeeded."""


def source_id(msg):
    return ':'.join((msg.platform, msg.chat_type, msg.chat_id, msg.sender_id, msg.msg_id))


class TurnCoordinator:
    def __init__(self, path=None, *, identity=None, clock=time.time):
        self.path = Path(path) if path else None
        self.identity = identity or (lambda platform, owner: platform + ':' + owner)
        self.clock = clock
        self.conversations = {}
        self.turns = {}
        self.applied_forgetting=[]
        self.active_space=lambda: ''
        self.legacy_space=''
        self.load_error = False
        self._busy = {}
        self._locks = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                if raw.get('version') != 1 or not isinstance(raw.get('turns'), dict) or not isinstance(raw.get('conversations'), dict):
                    raise ValueError('invalid conversation ledger')
                for row in raw['conversations'].values():
                    if not isinstance(row, dict) or not isinstance(row['seen'], list) or not isinstance(row['revision'], int):
                        raise ValueError('invalid conversation revision')
                for row in raw['turns'].values():
                    if not isinstance(row, dict) or row['state'] not in TERMINAL | {'collecting', 'judging', 'deferred', 'replying', 'sending'}:
                        raise ValueError('invalid turn state')
                    for name in ('created_at', 'updated_at', 'revisit_at', 'expires_at'):
                        if not isinstance(row[name], (float, int)) or not math.isfinite(row[name]):
                            raise ValueError('invalid turn timestamp')
                    WxMessage(**row['message'])
                self.conversations, self.turns = raw['conversations'], raw['turns']
                self.applied_forgetting=raw.get('applied_forgetting',[])
                for row in self.turns.values():
                    if row['state'] == 'sending':
                        row['state'] = 'uncertain'
                        row['reason'] = '提交中断，等待投递凭据核查，不自动重发'
                        if row.get('attempts'):row['attempts'][-1]['status']='uncertain'
                    elif row['state']=='deferred' and row.get('revisited'):
                        row['revisited']=False  # No completed admission result was recorded.
                    elif row['state']=='replying' and row.get('revisited') and not row.get('attempts'):
                        row.update(state='deferred',revisited=False,late_decision='reply')
            except Exception:
                self.load_error = True  # Preserve broken state; optional sends stay stopped.

    @staticmethod
    def key(msg):
        return ':'.join((msg.platform, msg.chat_type, msg.chat_id, msg.sender_id))

    def person(self, msg):
        return self.identity(msg.platform, msg.sender_id) if not msg.is_group else self.key(msg)

    def _space(self):
        return self.active_space()

    def _row_space(self,row):
        return row.get('persona_space',self.legacy_space)

    def lock(self, person):
        return self._locks.setdefault(person, asyncio.Lock())

    def _save(self):
        if self.load_error:
            raise ValueError('conversation state requires recovery')
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'conversations': self.conversations, 'turns': self.turns,
                           'applied_forgetting':self.applied_forgetting}, f, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def ingest(self, msg):
        if self.load_error:
            # Raw protocol inbox still accepts messages; stop optional work only.
            return
        key = self.key(msg)
        row = self.conversations.setdefault(key, {'revision': 0, 'seen': [], 'platform': msg.platform,
                                                   'owner_id': msg.sender_id, 'last_in': 0})
        sid = source_id(msg)
        if sid in row['seen']:
            return
        before = copy.deepcopy((self.conversations, self.turns))
        row['revision'] += 1
        row['last_in'] = self.clock()
        row['seen'] = (row['seen'] + [sid])[-1024:]
        person = self.person(msg)
        for turn in self.turns.values():
            other = WxMessage(**turn['message'])
            if self.person(other) == person and turn['state'] == 'deferred':
                turn.update(state='superseded', reason='新来话接入当前上下文，取消旧补回', updated_at=self.clock())
        try:
            self._save()
        except BaseException:
            self.conversations, self.turns = before
            raise

    def begin(self, msg):
        for original in members(msg):
            self.ingest(original)
        ids = [source_id(m) for m in members(msg)]
        tid = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        if self.load_error:
            return tid
        now = self.clock()
        existing = self.turns.get(tid)
        if existing and existing['state'] in ('sending', 'uncertain', 'accepted'):
            return tid
        dto = asdict(msg)
        # Media refs, never image bytes; avoid recursively storing batch envelopes.
        dto['batch_messages'] = []
        dto['raw_content']=''
        dto['content']=dto['content'][:12000]
        dto['images']=[]
        dto['content_parts']=[]
        row = {'id': tid, 'key': self.key(msg), 'revision': self.conversations[self.key(msg)]['revision'],
               'persona_space':self._space(),
               'message': dto, 'source_ids': ids, 'state': 'judging', 'reason': '', 'protected_ids': [],
               'created_at': existing['created_at'] if existing else now, 'updated_at': now,
               'revisit_at': 0, 'expires_at': now + 600, 'revisited': False, 'attempts': []}
        self.turns[tid] = row
        # Retain active work and recent audit, not an unbounded second chat log.
        terminal = sorted((r for r in self.turns.values() if r['state'] in TERMINAL), key=lambda r:r['updated_at'])
        for old in terminal[:-256]:
            self.turns.pop(old['id'], None)
        self._save()
        return tid

    def decide(self, tid, decision, *, late_enabled=True):
        if self.load_error:
            return
        row = self.turns[tid]
        if row['state'] in ('sending', 'uncertain'):
            return
        if self._row_space(row)!=self._space():
            self.cancel_other_spaces()
            return
        action = decision.action
        if action == 'defer' and (not late_enabled or decision.protected_ids):
            action = 'reply' if decision.protected_ids else 'context_only'
        now = self.clock()
        row.update(state={'reply':'replying', 'defer':'deferred', 'context_only':'context_only'}[action],
                   reason=decision.reason, protected_ids=decision.protected_ids, updated_at=now)
        if action == 'defer':
            row['revisit_at'] = now + 60 + int(tid[:4], 16) % 121
        elif row.get('revisited'):
            row['late_decision']=action
        self._save()

    def check(self, tid):
        if self.load_error:
            return
        row = self.turns.get(tid)
        if row is None or row['state'] in ('superseded', 'context_only'):
            raise ConversationChanged('Conversation decision superseded')
        if row['state'] not in ('sending','uncertain','accepted') and self._row_space(row)!=self._space():
            raise ConversationChanged('Conversation persona space changed')
        if row['state'] not in ('sending', 'uncertain') and self.conversations[row['key']]['revision'] != row['revision']:
            raise ConversationChanged('Conversation revision changed')

    def finish(self, tid, state='accepted', reason=''):
        if self.load_error or tid not in self.turns:
            return
        row = self.turns[tid]
        row.update(state=state, updated_at=self.clock())
        if row['attempts'] and state in ('accepted','uncertain'):
            row['attempts'][-1]['status']=state
        if reason:
            row['reason'] = reason
        self._save()

    def reserve(self, tid, *, kind='late', text=''):
        if self.load_error:
            return None
        self.check(tid)
        row = self.turns[tid]
        before=copy.deepcopy(row)
        attempt = {'id': uuid.uuid4().hex, 'kind': kind, 'ts': self.clock(), 'status': 'sending'}
        row['attempts'].append(attempt)
        if text:row['draft']=text
        row.update(state='sending', updated_at=self.clock())
        try:self._save()  # Before any transport request. Never replay sending/uncertain.
        except BaseException as exc:
            self.turns[tid]=before
            raise ConversationStateError('conversation reservation was not persisted') from exc
        return attempt

    def busy(self, person):
        return person in self._busy or self.lock(person).locked()

    def any_busy(self):
        """Whether foreground processing or an identity's late-send lock is held."""
        return any(count>0 for count in self._busy.values()) or any(lock.locked() for lock in self._locks.values())

    def enter(self, msg):
        person=self.person(msg)
        self._busy[person]=self._busy.get(person,0)+1

    def leave(self, msg):
        person=self.person(msg)
        if self._busy.get(person,0)>1:self._busy[person]-=1
        else:self._busy.pop(person,None)

    def cancel_other_spaces(self):
        """Cancel only unsubmitted work; retain committed delivery credentials."""
        if self.load_error:return 0
        current=self._space()
        before={tid:copy.deepcopy(row) for tid,row in self.turns.items()
                if row['state'] in ('collecting','judging','deferred','replying')
                and self._row_space(row)!=current}
        for tid in before:
            self.turns[tid].update(state='context_only',reason='人格与关系空间已切换，取消旧空间未提交回复',updated_at=self.clock())
        if before:
            try:self._save()
            except BaseException:
                self.turns.update(before)
                raise
        return len(before)

    def due(self, *, enabled=True):
        if self.load_error:
            return []
        self.cancel_other_spaces()
        now = self.clock(); result = []; changed = False
        for row in self.turns.values():
            if row['state'] != 'deferred':
                continue
            if self._row_space(row)!=self._space():continue
            if not enabled or row['expires_at'] < now:
                row.update(state='context_only', reason='稍后接话已关闭或到期', updated_at=now); changed=True
            elif row['revisit_at'] <= now and not row['revisited']:
                result.append(row)
        if changed:
            self._save()
        return result

    def reconsidered(self, tid):
        self.turns[tid]['revisited'] = True
        self._save()

    def forget(self, predicate, *, marker_id=''):
        if self.load_error:
            return
        if marker_id and marker_id in self.applied_forgetting:return
        for row in self.turns.values():
            msg = WxMessage(**row['message'])
            msg.content+='\n'+row.get('draft','')
            if predicate(msg):
                row['message']['content'] = ''
                row['message']['images'] = []
                row['message']['content_parts'] = []
                row.pop('draft',None)
                row.update(state='superseded', reason='用户明确遗忘，旧补回已取消', updated_at=self.clock())
        if marker_id:self.applied_forgetting.append(marker_id)
        self._save()

    def status(self):
        counts = {'waiting': 0, 'silent': 0, 'deferred': 0, 'replying': 0, 'uncertain': 0}
        mapping = {'collecting':'waiting','judging':'waiting','context_only':'silent','deferred':'deferred',
                   'replying':'replying','sending':'replying','uncertain':'uncertain'}
        for row in self.turns.values():
            if row['state'] in mapping:
                counts[mapping[row['state']]] += 1
        recent = sorted(self.turns.values(), key=lambda r:r['updated_at'], reverse=True)[:10]
        return {**counts, 'state_error': self.load_error, 'recent': [
            {'state': r['state'], 'reason': r['reason'], 'channel': r['message']['platform'],
             'chat_id': r['message']['chat_id'], 'updated_at': r['updated_at']} for r in recent]}
