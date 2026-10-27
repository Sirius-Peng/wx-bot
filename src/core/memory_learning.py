"""Durable, shared automatic-memory queue. It never delays sending a reply."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import shutil
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from .reality import message_timing

log = logging.getLogger(__name__)


def error_summary(exc: BaseException) -> str:
    # Do not expose provider response bodies, credentials, prompts or user content.
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "safe_reason", "")
    return type(exc).__name__ + (f" (HTTP {status})" if isinstance(status, int) else "") + (": " + detail if detail else "")


class MemoryLearningQueue:
    MAX_ACTIVE = 2
    IDLE_SECONDS = 120.0
    COOLDOWN_SECONDS = 300.0
    EPISODE_GAP = 1800.0
    MIN_TURNS = 3

    def __init__(self, memory, path: Path):
        self.memory = memory
        self.path = Path(path)
        self.pending: dict[str, dict] = {}
        self.seen: list[str] = []
        self.contexts: list[str] = []
        self.episodes: dict[str, dict] = {}
        self.last_reflection: dict[str, float] = {}
        self.recent_sources: dict[str, dict] = {}
        self.info = {"state": "idle", "attempts": 0, "completed": 0, "added": 0,
                     "empty": 0, "failures": 0, "last_error": "",
                     "last_success_at": 0.0, "last_elapsed_ms": 0.0}
        self.timeout = 45.0
        self._wake = asyncio.Event()
        self._runner = None
        self._jobs: dict[str, asyncio.Task] = {}
        self._job_identities: dict[str, str] = {}
        self._closed = False
        self.foreground_busy = lambda: False
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            version = raw.get('version', 1)
            if version < 2:
                backup = self.path.with_name(self.path.stem + f'.v{version}-backup.json')
                if not backup.exists():
                    shutil.copy2(self.path, backup)
                    os.chmod(backup, 0o600)
            self.pending = raw.get("pending") or {}
            self.seen = raw.get("seen") or []
            self.contexts = raw.get("contexts") or []
            self.episodes = raw.get("episodes") or {}
            self.last_reflection = raw.get("last_reflection") or {}
            self.recent_sources = raw.get("recent_sources") or {}
            self.info.update(raw.get("info") or {})
            self.info["state"] = "queued" if self.pending else "idle"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump({"version": 2, "pending": self.pending, "seen": self.seen[-5000:],
                           "contexts": self.contexts[-5000:], "episodes":self.episodes,
                           "last_reflection":self.last_reflection, "recent_sources":self.recent_sources, "info": self.info}, file,
                          ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        finally:
            Path(temp).unlink(missing_ok=True)

    def configured(self) -> bool:
        llm = self.memory.llm
        if llm is None:
            return False
        primary = getattr(llm, "primary", None)
        return bool(primary.api_key) if primary is not None else True

    def status(self) -> dict:
        primary = getattr(self.memory.llm, "primary", None)
        state = self.info["state"]
        if not self.memory.enabled:
            state = "disabled"
        elif not self.configured():
            state = "unconfigured"
        elif self._jobs:
            state = "extracting"
        elif self.pending and state not in ("retry_wait", "save_failed"):
            state = "queued"
        failures = [p.get("last_error", "") for p in self.pending.values() if p.get("last_error")]
        if (failures and not self._jobs and self.memory.enabled and self.configured()
                and any(p["retry_at"] > time.time() for p in self.pending.values())):
            state = "retry_wait"
        return {**self.info, "state": state, "last_error": failures[-1] if failures else self.info["last_error"], "configured": self.configured(),
                "model": getattr(primary, "model", ""), "active": len(self._jobs),
                "pending_batches": len(self.pending),
                "pending_turns": sum(len(p["turns"]) for p in self.pending.values()),
                "interval": max(self.MIN_TURNS, int(getattr(self.memory, "extract_interval", 3))),
                "reflection_cooldown_seconds": self.COOLDOWN_SECONDS,
                "idle_flush_seconds": self.IDLE_SECONDS}

    def start(self) -> None:
        self._closed = False
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run())
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        tasks = [t for t in [self._runner, *self._jobs.values()] if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._runner = None
        self._jobs.clear()
        if self.pending:
            self.info["state"] = "queued"
            self._save()  # Pending source turns survive stop/restart, not just RAM tasks.

    @staticmethod
    def meaningful(text):
        text = re.sub(r'[^\w\u4e00-\u9fff]+', '', text).lower()
        return bool(text) and text not in {'好','好的','嗯','嗯嗯','哦','噢','ok','okay','收到','谢谢','谢了','哈哈','哈哈哈','行','对','是的','晚安','再见','拜拜'}

    def _eligible(self, job):
        return len(job['turns']) >= max(self.MIN_TURNS, int(getattr(self.memory, 'extract_interval', 3)))

    def _person(self, job):
        if hasattr(self.memory, 'identity_key'):
            return self.memory.identity_key(job.get('platform', ''), job['user_id'])
        return job.get('identity', job['user_id'])

    def _last_reflection(self, job):
        person = self._person(job)
        values = [self.last_reflection.get(person, 0), self.last_reflection.get(job.get('identity', ''), 0)]
        if hasattr(self.memory, '_accounts'):
            values += [self.last_reflection.get(a['platform']+':'+a['owner_id'], 0)
                       for a in self.memory._accounts(job.get('platform', ''), job['user_id'])]
        return max(values)

    def _foreground_active(self):
        try:
            return bool(self.foreground_busy())
        except Exception:
            # Missing admission status is not a reason to compete with a reply.
            return True

    def _postpone(self, job):
        job['due_at'] = max(job['due_at'], time.time() + self.IDLE_SECONDS,
                            self._last_reflection(job) + self.COOLDOWN_SECONDS)

    def _yield_jobs(self, person=None, *, persist=True):
        cancelled=0
        for key,task in list(self._jobs.items()):
            job=self.pending.get(key)
            if (job is None or task.done() or task.cancelling() or
                    (person is not None and self._person(job)!=person)):
                continue
            self._postpone(job)
            # observe() already checks this revision after its network await.
            # Invalidate an interrupted snapshot even if a provider delays or
            # suppresses cancellation; it must not commit stale impressions.
            revisions=getattr(self.memory,'_revisions',None)
            if isinstance(revisions,dict):
                identities={self._person(job),self._job_identities.get(key)}-{None}
                for identity in identities:revisions[identity]=revisions.get(identity,0)+1
            task.cancel()
            cancelled+=1
        if cancelled:
            self.info['state']='queued' if self.pending else 'idle'
            self._wake.set()
            if persist:
                try:self._save()
                except Exception as exc:
                    # A background bookkeeping error must not reject the new
                    # foreground message. Original durable sources remain.
                    self.info.update(state='save_failed',last_error=error_summary(exc))
                    log.warning('自动记忆让位状态落盘失败：%s',error_summary(exc))
        return cancelled

    def yield_to_foreground(self):
        """Cancel in-flight reflection, preserving every unacknowledged source.

        This synchronous admission hook never waits for network cleanup, never
        counts a provider failure and never resets the successful-reflection
        cooldown. It can be called directly from protocol ingress.
        """
        return self._yield_jobs()

    async def enqueue(self, msg, reply: str = '') -> None:
        if not self.memory.enabled:
            return
        identity = self.memory.identity_key(msg.platform, msg.sender_id) if hasattr(self.memory, 'identity_key') else f'{msg.platform}:{msg.sender_id}'
        key = f'{identity}:{msg.chat_type}:{msg.chat_id}' if msg.is_group else identity
        envelopes = msg.batch_messages or [dict(msg_id=msg.msg_id)]
        event_ids = [hashlib.sha256(f'{msg.platform}:{msg.chat_type}:{msg.chat_id}:{msg.sender_id}:{e.get("msg_id", msg.msg_id)}'.encode()).hexdigest() for e in envelopes]
        if all(event in self.seen for event in event_ids):
            return
        # Even an acknowledgement can resume a live conversation. Stop this
        # person's old model request before deciding whether the new message
        # contributes a meaningful memory turn. Duplicate callbacks do not yield.
        yielded=self._yield_jobs(identity,persist=False)
        if not self.meaningful(msg.content):
            if yielded:self._save()
            return
        if hasattr(self.memory, 'is_suppressed') and self.memory.is_suppressed(msg.platform, msg.sender_id, msg.content):
            if yielded:self._save()
            return
        if hasattr(self.memory, 'observe_correction'): self.memory.observe_correction(msg)
        if hasattr(self.memory, 'observe_resolution'): self.memory.observe_resolution(msg)
        now = time.time()
        source_time = msg.timestamp / 1000 if isinstance(msg.timestamp, (int, float)) else 0
        observed_at = source_time if 1_000_000_000 <= source_time <= now + 300 else now
        fingerprint = hashlib.sha256(f'{identity}:{re.sub(chr(10), "", msg.content).strip()}'.encode()).hexdigest()
        last = self.recent_sources.get(fingerprint, {})
        # Copied cross-channel messages are one evidence, not independent support.
        if last and last.get('platform') != msg.platform and abs(observed_at - last['at']) <= 10:
            self.seen.extend(event_ids)
            self._save()
            return
        self.recent_sources = {k:v for k,v in self.recent_sources.items() if now-v.get('at', 0) < 86400}
        self.recent_sources[fingerprint] = {'at': observed_at, 'platform':msg.platform}
        episode = self.episodes.get(identity)
        if not episode or observed_at - episode['last_at'] >= self.EPISODE_GAP:
            episode = {'id':uuid.uuid4().hex, 'last_at':observed_at}
            self.episodes[identity] = episode
        episode['last_at'] = max(episode['last_at'], observed_at)
        job = self.pending.setdefault(key, {
            'identity':identity, 'user_id':msg.sender_id, 'user_name':msg.sender_name or msg.sender_id,
            'chat_id':msg.chat_id, 'chat_type':msg.chat_type, 'platform':msg.platform, 'turns':[],
            'due_at':now + self.IDLE_SECONDS, 'retry_at':0.0, 'failures':0})
        event = hashlib.sha256('|'.join(event_ids).encode()).hexdigest()
        # Date annotations are separate from the evidence clock and verbatim
        # source text. They never change consolidation, confidence or retention.
        timing=message_timing(msg,now)
        job['turns'].append({'id':event, 'source_ids':event_ids, 'user':msg.render(), 'assistant':'',
                             'observed_at':observed_at, 'episode_id':episode['id'],
                             **{field:timing[field] for field in ('user_at','user_end_at','received_at',
                                 'user_time_source','source_times')}})
        self.seen.extend(event_ids)
        self.seen = self.seen[-5000:]
        # Every meaningful turn restarts the idle window; neither a first message
        # nor "remember this" jumps ahead of subconscious/background formation.
        job['due_at'] = max(now + self.IDLE_SECONDS,
                            self._last_reflection(job) + self.COOLDOWN_SECONDS)
        self.info['state'] = 'queued'
        try:
            self._save()
        except Exception as exc:
            self.info.update(state='save_failed', last_error=error_summary(exc))
            log.warning('自动记忆排队落盘失败：%s', error_summary(exc))
            raise
        self.start()

    def retry_now(self) -> int:
        if not self.memory.enabled:
            return 0
        for job in self.pending.values():
            if self._eligible(job):
                job["due_at"] = max(0.0, self._last_reflection(job) + self.COOLDOWN_SECONDS)
                job["retry_at"] = 0.0
        if self.pending:
            self.info["state"] = "queued"
        self._save()
        self.start()
        return len(self.pending)

    def clear(self, scope: str = "", owner_id: str = "") -> None:
        if scope and scope != "user":
            return
        keys = [k for k, p in self.pending.items() if not owner_id or p["user_id"] == owner_id]
        for key in keys:
            task = self._jobs.get(key)
            if task is not None:
                task.cancel()
            self.pending.pop(key, None)
        if keys:
            self.info["state"] = "idle" if not self.pending else "queued"
            self._save()
            self._wake.set()

    async def wait_idle(self, timeout: float = 50.0) -> None:
        # Explicit test/debug synchronization, not part of normal reply latency.
        if not self.memory.enabled or not self.configured():
            return
        self.retry_now()
        async def waiting():
            while any(self._eligible(j) for j in self.pending.values()) or self._jobs:
                if not self._jobs and (self.info['state'] == 'retry_wait' or
                    all(self._last_reflection(j) + self.COOLDOWN_SECONDS > time.time()
                        for j in self.pending.values() if self._eligible(j))):
                    return
                await asyncio.sleep(0.01)
        await asyncio.wait_for(waiting(), timeout)

    async def _run(self) -> None:
        try:
            while not self._closed:
                self._wake.clear()
                for key, task in list(self._jobs.items()):
                    if task.done():
                        if not task.cancelled():
                            task.result()
                        del self._jobs[key]
                now = time.time()
                foreground=self._foreground_active()
                if foreground:self.yield_to_foreground()
                if self.memory.enabled and self.configured() and not foreground:
                    for key, job in list(self.pending.items()):
                        if len(self._jobs) >= self.MAX_ACTIVE:
                            break
                        due = max(job['due_at'], job['retry_at'], self._last_reflection(job) + self.COOLDOWN_SECONDS)
                        active_people = {self._person(self.pending[k]) for k in self._jobs if k in self.pending}
                        if (key not in self._jobs and self._eligible(job) and due <= now
                                and self._person(job) not in active_people):
                            self._jobs[key] = asyncio.create_task(self._extract(key, job))
                due_times = [max(.01, max(j["due_at"], j["retry_at"], self._last_reflection(j) + self.COOLDOWN_SECONDS) - time.time())
                             for k, j in self.pending.items() if k not in self._jobs and self._eligible(j)]
                delay = (min([2.0, *due_times])
                         if self.memory.enabled and self.configured() and not foreground and len(self._jobs) < self.MAX_ACTIVE
                         else 2.0)
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._wake.set()

    async def _extract(self, key: str, job: dict) -> None:
        # Admission may become busy after _run schedules this task but before
        # the task gets CPU time. Do not count that as a model attempt/failure.
        if self._foreground_active():
            self._postpone(job)
            try:self._save()
            except Exception as exc:
                self.info.update(state='save_failed',last_error=error_summary(exc))
            return
        turns = list(job["turns"][:6])
        events = {t["id"] for t in turns}
        self.info.update(state="extracting", attempts=self.info["attempts"] + 1)
        self._job_identities[key]=self._person(job)
        started = time.monotonic()
        try:
            self._save()  # Do not send a non-durable batch to the model.
            learned = await asyncio.wait_for(self.memory.observe(
                chat_id=job["chat_id"], chat_type=job["chat_type"],
                user_id=job["user_id"], user_name=job["user_name"], history=turns,
                **({"platform":job.get("platform",key.split(":",1)[0])} if getattr(self.memory,"supports_platform_scope",False) else {})), self.timeout)
            # A provider may swallow cancellation and return an empty result.
            # That is not a completed reflection and must not acknowledge users'
            # pending evidence or advance the successful-reflection cooldown.
            if asyncio.current_task().cancelling():raise asyncio.CancelledError()
            self.last_reflection[self._person(job)] = time.time()
            current = self.pending.get(key)
            before_ack = copy.deepcopy(current)
            if current is not None:
                current.pop("last_error", None)
                current["turns"] = [t for t in current["turns"] if t["id"] not in events]
                current["failures"] = 0
                current["retry_at"] = 0.0
                if not current["turns"]:
                    self.pending.pop(key, None)
                else:
                    current["due_at"] = max(current["due_at"], time.time() + self.IDLE_SECONDS, time.time() + self.COOLDOWN_SECONDS)
            self.info.update(state="learned" if learned else "empty", last_error="",
                             completed=self.info["completed"] + 1,
                             added=self.info["added"] + len(learned),
                             empty=self.info["empty"] + (not learned), last_success_at=time.time())
            try:
                self._save()
            except Exception:
                if before_ack is not None:
                    self.pending[key] = before_ack
                raise
        except asyncio.CancelledError:
            self.info['state']='queued' if self.pending else 'idle'
            raise  # Pending turns have not been removed; resume them next startup.
        except Exception as exc:
            current = self.pending.get(key)
            if not self.memory.enabled:
                self.info["state"] = "disabled"
                return
            if current is not None:
                current["last_error"] = error_summary(exc)
                current["failures"] += 1
                current["retry_at"] = time.time() + min(60, 2 ** min(current["failures"], 6))
            self.info.update(state="retry_wait", last_error=error_summary(exc),
                             failures=self.info["failures"] + 1)
            log.warning("自动记忆抽取/保存失败，将保留批次：%s", error_summary(exc))
            try:
                self._save()
            except Exception as write_error:
                self.info.update(state="save_failed", last_error=error_summary(write_error))
        finally:
            self._job_identities.pop(key,None)
            self.info["last_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            self._wake.set()
