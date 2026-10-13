"""Durable, shared automatic-memory queue. It never delays sending a reply."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

log = logging.getLogger(__name__)
EXPLICIT = re.compile(r"记住|记得|以后叫我|称呼我|我的名字|我叫|我是|我喜欢|我不喜欢|我住|生日")


def error_summary(exc: BaseException) -> str:
    # Do not expose provider response bodies, credentials, prompts or user content.
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "safe_reason", "")
    return type(exc).__name__ + (f" (HTTP {status})" if isinstance(status, int) else "") + (": " + detail if detail else "")


class MemoryLearningQueue:
    MAX_ACTIVE = 2
    IDLE_SECONDS = 2.0

    def __init__(self, memory, path: Path):
        self.memory = memory
        self.path = Path(path)
        self.pending: dict[str, dict] = {}
        self.seen: list[str] = []
        self.contexts: list[str] = []
        self.info = {"state": "idle", "attempts": 0, "completed": 0, "added": 0,
                     "empty": 0, "failures": 0, "last_error": "",
                     "last_success_at": 0.0, "last_elapsed_ms": 0.0}
        self.timeout = 45.0
        self._wake = asyncio.Event()
        self._runner = None
        self._jobs: dict[str, asyncio.Task] = {}
        self._closed = False
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.pending = raw.get("pending") or {}
            self.seen = raw.get("seen") or []
            self.contexts = raw.get("contexts") or []
            self.info.update(raw.get("info") or {})
            self.info["state"] = "queued" if self.pending else "idle"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump({"version": 1, "pending": self.pending, "seen": self.seen[-5000:],
                           "contexts": self.contexts[-5000:], "info": self.info}, file,
                          ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
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
                "interval": max(1, int(getattr(self.memory, "extract_interval", 3))),
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

    async def enqueue(self, msg, reply: str) -> None:
        if not self.memory.enabled or not msg.content.strip():
            return
        key = f"{msg.platform}:{msg.chat_type}:{msg.chat_id}:{msg.sender_id}"
        message_id = str(msg.msg_id) if msg.msg_id else uuid.uuid4().hex
        event = hashlib.sha256(f"{key}:{message_id}".encode()).hexdigest()
        if event in self.seen:
            return
        now = time.time()
        first = key not in self.contexts
        if first:
            self.contexts.append(key)
        job = self.pending.setdefault(key, {
            "user_id": msg.sender_id, "user_name": msg.sender_name or msg.sender_id,
            "chat_id": msg.chat_id, "chat_type": msg.chat_type, "turns": [],
            "due_at": now + self.IDLE_SECONDS, "retry_at": 0.0, "failures": 0,
        })
        # Extract only this user's actual incoming text, never persona examples/replies.
        job["turns"].append({"id": event, "user": msg.render(), "assistant": ""})
        self.seen.append(event)
        self.seen = self.seen[-5000:]
        interval = max(1, int(getattr(self.memory, "extract_interval", 3)))
        if first or EXPLICIT.search(msg.content) or len(job["turns"]) >= interval:
            job["due_at"] = now
        else:
            job["due_at"] = min(job["due_at"], now + self.IDLE_SECONDS)
        self.info["state"] = "queued"
        try:
            self._save()
        except Exception as exc:
            self.info.update(state="save_failed", last_error=error_summary(exc))
            log.warning("自动记忆排队落盘失败：%s", error_summary(exc))
            raise
        self.start()

    def retry_now(self) -> int:
        if not self.memory.enabled:
            return 0
        for job in self.pending.values():
            job["due_at"] = 0.0
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
            while self.pending or self._jobs:
                if self.info["state"] == "retry_wait" and not self._jobs:
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
                if self.memory.enabled and self.configured():
                    for key, job in list(self.pending.items()):
                        if len(self._jobs) >= self.MAX_ACTIVE:
                            break
                        due = max(job["due_at"], job["retry_at"])
                        if key not in self._jobs and due <= now:
                            self._jobs[key] = asyncio.create_task(self._extract(key, job))
                due_times = [max(.01, max(j["due_at"], j["retry_at"]) - time.time())
                             for k, j in self.pending.items() if k not in self._jobs]
                delay = (min([2.0, *due_times])
                         if self.memory.enabled and self.configured() and len(self._jobs) < self.MAX_ACTIVE
                         else 2.0)
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._wake.set()

    async def _extract(self, key: str, job: dict) -> None:
        turns = list(job["turns"][:6])
        events = {t["id"] for t in turns}
        self.info.update(state="extracting", attempts=self.info["attempts"] + 1)
        started = time.monotonic()
        try:
            self._save()  # Do not send a non-durable batch to the model.
            learned = await asyncio.wait_for(self.memory.observe(
                chat_id=job["chat_id"], chat_type=job["chat_type"],
                user_id=job["user_id"], user_name=job["user_name"], history=turns), self.timeout)
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
                    current["due_at"] = min(current["due_at"], time.time() + self.IDLE_SECONDS)
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
            self.info["last_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            self._wake.set()
