"""Disposable quiet-window work. Never accepts, replies or types; one cancellable cloud decision per snapshot."""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import time

from .conversation import members
from .reply_decision import ReplyDecider, BurstDecisionBudgetExceeded
from .turn_coordinator import source_id

log = logging.getLogger(__name__)
FOLLOW_UP = re.compile(r'[那这它他她]?个?(?:后来呢|然后呢|继续|怎么样了|呢|还记得吗)[？?。!！ ]*')


def prior_turns(msg, recent):
    """Admission may already have saved this batch; it is not its own history."""
    ids = {source_id(original) for original in members(msg)}
    return [turn for turn in recent if isinstance(turn, dict)
            and turn.get('turn_id') not in ids
            and not ids.intersection(turn.get('source_ids', []))]


def recall_query(msg, recent):
    query = msg.content
    if FOLLOW_UP.fullmatch(query.strip()) and recent:
        query += ' ' + recent[-1].get('user', '')[-300:]
    return query


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


@dataclass
class PreparedTurn:
    conversation: tuple
    signature: str
    revision: tuple
    expires: float
    task: asyncio.Task | None = None
    cloud_state: str = ''
    cloud_result: str | None = None
    cloud_attempted: bool = False
    cloud_publish_closed: bool = False
    cloud_budget_exhausted: bool = False
    cloud_used: bool = False
    memory_ready: bool = False
    memory_query: str = ''
    memory_context: str = ''
    memory_ids: tuple = ()
    memory_until: float = float('inf')


class TurnPreparation:
    def __init__(self, orchestrator, *, ttl=300, limit=64, clock=time.monotonic, debounce=.8):
        self.orch = orchestrator
        self.ttl = min(300., max(.01, float(ttl)))
        self.limit = max(1, min(64, int(limit)))
        self.clock = clock
        self.debounce = max(0., min(1., float(debounce)))
        self.entries = {}
        # Results belong to one exact snapshot; paid quota belongs to the whole
        # pending burst. Invalidating a result must never silently reset spend.
        self.burst_spend = {}
        self.burst_limit = max(64, self.limit * 4)
        self.jobs = set()
        self.epoch = 0
        self.closed = False
        self.current = ContextVar('prepared_turn', default=None)
        self.current_message = ContextVar('prepared_message', default=None)
        decider = orchestrator.reply_decider
        if decider is not None:
            decider.preparation = self

    def provider_revision(self):
        gateway = getattr(self.orch.reply_decider, 'gateway', None)
        cfg = getattr(gateway, 'primary', None)
        # Hot provider/key/model changes must invalidate paid speculative work.
        # Only a digest is retained in the cache; no credentials are logged.
        return digest(vars(cfg)) if cfg is not None else id(gateway)

    def was_used(self):
        entry = self.current.get()
        return bool(self.usable(entry) and entry.cloud_used)

    async def complete_once(self, prompt, max_tokens=8, *, message=None):
        entry = self.current.get()
        state = digest([prompt, max_tokens])
        if (self.usable(entry) and not entry.cloud_used and
                entry.cloud_attempted and entry.cloud_state == state):
            entry.cloud_used = True
            if entry.task is not None and not entry.task.done():
                await asyncio.shield(entry.task)
            if self.usable(entry) and isinstance(entry.cloud_result, str):
                return entry.cloud_result
            if entry.cloud_budget_exhausted:
                raise BurstDecisionBudgetExceeded('burst budget already spent')
            # A timed-out/error speculative request is already the one attempt.
            # Never restart it at admission or extend the total request deadline.
            raise TimeoutError('prepared decision unavailable')
        msg = message if message is not None else self.current_message.get()
        if msg is None:
            # Production decisions always pass their DTO. Missing scope must not
            # become an unmetered second request from an embedded caller.
            raise BurstDecisionBudgetExceeded('missing burst scope')
        self._consume_burst(msg)
        gateway = self.orch.reply_decider.gateway
        complete = getattr(gateway, 'complete_once', None) or gateway.complete
        return await complete(prompt, max_tokens=max_tokens)

    def _prune_spend(self):
        now = self.clock()
        for key, expiry in list(self.burst_spend.items()):
            if expiry <= now: self.burst_spend.pop(key, None)

    def burst_key(self, msg):
        return self.key(msg), source_id(members(msg)[0])

    def _consume_burst(self, msg):
        # Synchronous check-and-charge: no await allows a preview and admission
        # request to race past the same quota. Debounce has already completed.
        self._prune_spend()
        key = self.burst_key(msg)
        if key in self.burst_spend:
            self.burst_spend[key] = self.clock() + self.ttl
            raise BurstDecisionBudgetExceeded('burst budget already spent')
        if len(self.burst_spend) >= self.burst_limit:
            # Do not evict a live spent key and accidentally reopen its quota.
            raise BurstDecisionBudgetExceeded('burst budget capacity reached')
        self.burst_spend[key] = self.clock() + self.ttl

    @staticmethod
    def key(msg):
        return msg.platform, msg.chat_type, msg.chat_id, msg.sender_id

    @staticmethod
    def signature(msg):
        # Includes original IDs, ordered text/image parts, refs and context token;
        # the cache retains only this digest, not transport credentials or images.
        return digest(asdict(msg))

    def revision(self, msg):
        memory = self.orch.memory
        path = getattr(memory, 'path', None)
        stamp = None
        if path is not None:
            try:
                stat = Path(path).stat()
                stamp = stat.st_mtime_ns, stat.st_size
            except OSError:
                stamp = 'unavailable'
        return (self.epoch, self.orch._memory_revision(msg), stamp,
                bool(getattr(memory, 'enabled', False)), bool(getattr(memory, 'inject', True)),
                getattr(self.orch.store, 'revision', 0),
                getattr(self.orch.ctx, 'system_prompt', ''),
                getattr(self.orch.reply_decider, 'mode', ''),
                getattr(self.orch.reply_decider, 'bot_name', ''),
                self.provider_revision())

    def valid(self, entry):
        if entry is None or self.closed or entry.expires <= self.clock():
            return False
        return (entry.revision[0] == self.epoch
                and self.entries.get(entry.conversation) is entry)

    def usable(self, entry):
        msg = self.current_message.get()
        return self.valid(entry) and msg is not None and entry.revision == self.revision(msg)

    def _prune(self):
        self._prune_spend()
        for key, entry in list(self.entries.items()):
            if not self.valid(entry):
                self.entries.pop(key, None)
                if entry.task is not None:
                    entry.task.cancel()

    def accept(self, msg):
        """Called synchronously after the adapter obtains its full pending snapshot."""
        if self.closed or msg.is_group:
            return
        self._prune()
        key = self.key(msg)
        spent = self.burst_key(msg)
        if spent in self.burst_spend:
            self.burst_spend[spent] = self.clock() + self.ttl
        signature, revision = self.signature(msg), self.revision(msg)
        old = self.entries.get(key)
        if old is not None and old.signature == signature and old.revision == revision:
            return
        if old is not None and old.task is not None:
            old.task.cancel()
        self.entries.pop(key, None)
        while len(self.entries) >= self.limit:
            oldest = next(iter(self.entries))
            removed = self.entries.pop(oldest)
            if removed.task is not None:
                removed.task.cancel()
        entry = PreparedTurn(key, signature, revision, self.clock() + self.ttl)
        self.entries[key] = entry
        entry.task = asyncio.create_task(self._prepare(copy.deepcopy(msg), entry))
        self.jobs.add(entry.task)
        def finished(task):
            self.jobs.discard(task)
            if not task.cancelled():
                task.exception()
        entry.task.add_done_callback(finished)

    async def _prepare(self, msg, entry):
        try:
            # Coalesce callbacks in the same receive tick before any model work.
            await asyncio.sleep(0)
            recent = prior_turns(msg, await self.orch.store.get_recent(msg.ctx_key))
            if entry.revision != self.revision(msg):
                return
            memory = self.orch.memory
            if memory and getattr(memory, 'enabled', False) and getattr(memory, 'inject', True):
                query = recall_query(msg, recent)
                options = {'platform': msg.platform} if getattr(memory, 'supports_platform_scope', False) else {}
                entry.memory_context = memory.build_context(
                    user_id=msg.sender_id, chat_id=msg.chat_id, query=query, **options)
                entry.memory_query = query
                # Read-only candidate metadata lets a cache hit account for use
                # only at final injection, and expire at actual lifecycle limits.
                if hasattr(memory, 'recall'):
                    candidates = memory.recall(msg.sender_id, msg.chat_id, query, **options)
                    used = [item for item in candidates if item.content in entry.memory_context]
                    entry.memory_ids = tuple(item.id for item in used)
                    for item in used:
                        deadline = getattr(item, 'deadline', None)
                        for until in ((deadline() if callable(deadline) else 0), getattr(item, 'expires_at', 0)):
                            if until and not getattr(item, 'pinned', False):
                                entry.memory_until = min(entry.memory_until, until)
                entry.memory_ready = True
            decider = self.orch.reply_decider
            if decider is None or decider.gateway is None:
                return
            decision_deadline = time.monotonic() + min(3.0, max(.001, float(decider.timeout)))
            preparation = self
            class Capture:
                async def complete_once(self, prompt, max_tokens=8, *, message=None):
                    entry.cloud_state = digest([prompt, max_tokens])
                    entry.cloud_attempted = True
                    preparation._prune_spend()
                    if preparation.burst_key(msg) in preparation.burst_spend:
                        entry.cloud_budget_exhausted = True
                        raise BurstDecisionBudgetExceeded('burst budget already spent')
                    # Coalesce rapid fragments before spending an API request.
                    # The default .8s plus inference normally fits within the
                    # existing 2/4s quiet window; never submit a reply here.
                    await asyncio.sleep(preparation.debounce)
                    if entry.revision != preparation.revision(msg):
                        raise asyncio.CancelledError()
                    try:
                        preparation._consume_burst(msg)
                    except BurstDecisionBudgetExceeded:
                        entry.cloud_budget_exhausted = True
                        raise
                    gateway = decider.gateway
                    complete = getattr(gateway, 'complete_once', None) or gateway.complete
                    result = await complete(prompt, max_tokens=max_tokens)
                    # A provider may suppress cancellation and return after
                    # _within has already timed out. Once preview admission
                    # ends, a late C/D must never reopen the result for handoff.
                    task = asyncio.current_task()
                    if (entry.cloud_publish_closed or not preparation.valid(entry)
                            or entry.revision != preparation.revision(msg)
                            or time.monotonic() >= decision_deadline
                            or (task is not None and task.cancelling())):
                        raise asyncio.CancelledError()
                    entry.cloud_result = result
                    return result
            await ReplyDecider(Capture(), mode=decider.mode, bot_name=decider.bot_name,
                               timeout=decider.timeout).decide(msg, recent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug('Quiet-window preparation unavailable; normal path retained', exc_info=True)
        finally:
            # Preserve successful results, but permanently close publication on
            # success, timeout, cancellation and every other preview exit.
            entry.cloud_publish_closed = True

    @contextmanager
    def bind(self, msg):
        self._prune()
        entry = self.entries.get(self.key(msg))
        if (not self.valid(entry) or entry.signature != self.signature(msg)
                or entry.revision != self.revision(msg)):
            entry = None
        marker = self.current.set(entry)
        message_marker = self.current_message.set(msg)
        try:
            yield
        finally:
            self.current_message.reset(message_marker)
            self.current.reset(marker)

    def memory_context(self, msg, query):
        entry = self.current.get()
        if (self.valid(entry) and entry.revision == self.revision(msg)
                and entry.memory_ready and entry.memory_query == query
                and entry.memory_until > time.time()):
            return entry.memory_context, entry.memory_ids
        return None

    def invalidate(self):
        self.epoch += 1
        for entry in self.entries.values():
            if entry.task is not None:
                entry.task.cancel()
        self.entries.clear()

    async def close(self):
        self.closed = True
        self.invalidate()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        self.jobs.clear()
        self.burst_spend.clear()
