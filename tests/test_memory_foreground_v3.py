"""Foreground work preempts reflection without losing durable user evidence."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from src.core.memory import PersonaMemory
from src.core.memory_learning import MemoryLearningQueue
from src.core.turn_coordinator import TurnCoordinator
from src.core.types import WxMessage

VALID='[{"category":"fact","content":"用户是后端工程师","keywords":["职业"],"evidence":"我是后端工程师"}]'


def msg(mid,content='详细的项目补充',owner='u'):
    return WxMessage(str(mid),'onebot','private','p_'+owner,owner,owner,'bot',content)


class ForegroundLearning(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.model=NS(complete=AsyncMock(return_value=VALID))
        self.memory=PersonaMemory(self.root/'memory.json',llm=self.model)
        self.queue=self.memory.learner

    async def asyncTearDown(self):
        await self.queue.close();self.tmp.cleanup()

    async def seed(self,owner='u'):
        for i,text in enumerate(('我是后端工程师','服务端项目已经做了很久','主要工作使用Python')):
            await self.queue.enqueue(msg(i,text,owner))

    async def start_slow(self,swallow=False):
        entered=asyncio.Event();gate=asyncio.Event();cancelled=asyncio.Event()
        async def complete(*a,**kw):
            entered.set()
            try:await gate.wait()
            except asyncio.CancelledError:
                cancelled.set()
                if not swallow:raise
                await gate.wait()
            return VALID
        self.model.complete.side_effect=complete
        await self.seed();self.queue.retry_now()
        await asyncio.wait_for(entered.wait(),1)
        return gate,cancelled

    async def finished_jobs(self):
        for _ in range(100):
            if not self.queue._jobs:return
            await asyncio.sleep(.005)
        self.fail('reflection cancellation did not settle')

    async def test_busy_callback_blocks_start_without_failure_or_attempt(self):
        busy=[True];self.queue.foreground_busy=lambda:busy[0]
        await self.seed();self.queue.retry_now();await asyncio.sleep(.03)
        self.model.complete.assert_not_awaited()
        self.assertEqual(self.queue.info['attempts'],0);self.assertEqual(self.queue.info['failures'],0)
        self.assertEqual(self.queue.status()['pending_turns'],3)
        busy[0]=False;self.queue._wake.set()
        for _ in range(100):
            if self.queue.info['completed']:break
            await asyncio.sleep(.005)
        self.assertEqual(self.queue.info['completed'],1)

    async def test_yield_cancels_preserves_sources_and_survives_restart_with_idle(self):
        gate,cancelled=await self.start_slow()
        before=copy.deepcopy(self.queue.pending);seen=list(self.queue.seen)
        self.assertEqual(self.queue.yield_to_foreground(),1)
        await asyncio.wait_for(cancelled.wait(),1);await self.finished_jobs()
        self.assertEqual(self.queue.info['failures'],0);self.assertEqual(self.queue.info['completed'],0)
        self.assertEqual(self.queue.seen,seen);self.assertEqual(self.queue.last_reflection,{})
        for key,job in self.queue.pending.items():
            self.assertEqual(job['turns'],before[key]['turns'])
            self.assertEqual(job['failures'],0);self.assertGreater(job['due_at'],time.time()+118)
        raw=json.loads(self.queue.path.read_text())
        self.assertEqual(sum(len(j['turns']) for j in raw['pending'].values()),3)
        await self.queue.close()
        restored=MemoryLearningQueue(self.memory,self.queue.path)
        try:
            restored.start();await asyncio.sleep(.03)
            self.assertEqual(restored.status()['pending_turns'],3)
            self.assertEqual(restored.info['failures'],0);self.assertEqual(restored.info['completed'],0)
            self.assertFalse(restored._jobs)
        finally:await restored.close()

    async def test_same_person_new_input_cancels_old_batch_and_retains_four_turns(self):
        gate,cancelled=await self.start_slow()
        await self.queue.enqueue(msg('new','我还没有说完，先补上这一点'))
        await asyncio.wait_for(cancelled.wait(),1);await self.finished_jobs()
        self.assertEqual(self.queue.status()['pending_turns'],4)
        self.assertEqual(self.queue.info['failures'],0);self.assertEqual(self.queue.info['completed'],0)
        self.assertEqual(self.memory.stats()['total'],0)

    async def test_ack_preempts_without_becoming_new_memory_evidence(self):
        gate,cancelled=await self.start_slow()
        await self.queue.enqueue(msg('ack','好的'))
        await asyncio.wait_for(cancelled.wait(),1);await self.finished_jobs()
        self.assertEqual(self.queue.status()['pending_turns'],3)
        self.assertEqual(self.queue.info['failures'],0)
        self.assertTrue(all(j['due_at']>time.time()+118 for j in self.queue.pending.values()))

    async def test_duplicate_callback_does_not_cancel_or_renew_batch(self):
        gate,cancelled=await self.start_slow()
        due={k:j['due_at'] for k,j in self.queue.pending.items()}
        await self.queue.enqueue(msg('0','我是后端工程师'))
        self.assertFalse(cancelled.is_set());self.assertEqual({k:j['due_at'] for k,j in self.queue.pending.items()},due)

    async def test_delayed_provider_cancellation_cannot_commit_stale_impression(self):
        gate,cancelled=await self.start_slow(swallow=True)
        self.queue.yield_to_foreground();await asyncio.wait_for(cancelled.wait(),1)
        gate.set();await self.finished_jobs()
        self.assertEqual(self.memory.stats()['total'],0)
        self.assertEqual(self.queue.status()['pending_turns'],3)
        self.assertEqual(self.queue.info['failures'],0);self.assertEqual(self.queue.info['completed'],0)

    async def test_yield_does_not_shorten_successful_reflection_cooldown(self):
        gate,cancelled=await self.start_slow()
        anchor=time.time();self.queue.last_reflection['onebot:u']=anchor
        self.queue.yield_to_foreground();await asyncio.wait_for(cancelled.wait(),1);await self.finished_jobs()
        self.assertEqual(self.queue.last_reflection['onebot:u'],anchor)
        self.assertTrue(all(j['due_at']>=anchor+self.queue.COOLDOWN_SECONDS for j in self.queue.pending.values()))

    async def test_foreground_status_failure_does_not_start_background(self):
        def broken():raise RuntimeError('fixture')
        self.queue.foreground_busy=broken
        await self.seed();self.queue.retry_now();await asyncio.sleep(.03)
        self.model.complete.assert_not_awaited();self.assertEqual(self.queue.info['failures'],0)


class CoordinatorForeground(unittest.IsolatedAsyncioTestCase):
    async def test_any_busy_tracks_refcounts_and_late_lock_without_creating_locks(self):
        coordinator=TurnCoordinator();m=msg('1')
        self.assertFalse(coordinator.any_busy());self.assertEqual(coordinator._locks,{})
        coordinator.enter(m);coordinator.enter(m);self.assertTrue(coordinator.any_busy())
        coordinator.leave(m);self.assertTrue(coordinator.any_busy())
        coordinator.leave(m);self.assertFalse(coordinator.any_busy())
        lock=coordinator.lock('late-person');await lock.acquire()
        self.assertTrue(coordinator.any_busy());lock.release();self.assertFalse(coordinator.any_busy())


if __name__=='__main__':unittest.main()
