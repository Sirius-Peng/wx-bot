"""Absolute source dates in background memory and linked-channel history.

All data, clocks and providers are synthetic; no configured accounts or API calls.
"""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]));sys.path.insert(0,str(ROOT))
from src.config_store import AppConfig
from src.core.memory import PersonaMemory
from src.core.proactive import ProactivePlanner
from src.core.types import WxMessage
from src.core.memory_learning import MemoryLearningQueue


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


class HistoricalMemoryDates(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.model=AsyncMock();self.model.complete.return_value='[]'
        self.memory=PersonaMemory(Path(self.tmp.name)/'memory.json',llm=self.model)
        self.memory.context_timezone='Asia/Shanghai'

    async def asyncTearDown(self):
        await self.memory.learner.close();self.tmp.cleanup()

    async def observe(self,history):
        return await self.memory.observe('alice','private','alice','Alice',history,platform='ilink')

    async def test_each_source_retains_its_own_absolute_date_and_timezone(self):
        history=[{'user':'我今天去看演出','observed_at':stamp('2026-10-20T23:50:00+08:00')},
                 {'user':'我昨天回来啦','observed_at':stamp('2026-10-25T00:10:00+08:00')}]
        original=copy.deepcopy(history)
        with patch('src.core.memory.time.time',return_value=stamp('2026-10-27T12:00:00+08:00')):
            await self.observe(history)
        prompt=self.model.complete.call_args.args[0]
        self.assertIn('2026-10-20T23:50:00+08:00',prompt)
        self.assertIn('2026-10-25T00:10:00+08:00',prompt)
        self.assertIn('Asia/Shanghai',prompt)
        self.assertIn('原发言',prompt)
        self.assertEqual(history,original)
        self.model.complete.assert_awaited_once()

    async def test_configured_timezone_not_host_timezone_controls_source_labels(self):
        self.memory.context_timezone='America/New_York'
        await self.observe([{'user':'今天的约定','observed_at':stamp('2026-10-26T00:30:00+00:00')}])
        prompt=self.model.complete.call_args.args[0]
        self.assertIn('2026-10-25T20:30:00-04:00',prompt)
        self.assertIn('America/New_York',prompt)

    async def test_undated_old_sources_are_explicitly_unknown_not_dated_today(self):
        for value in [None,0,-1,float('nan'),float('inf'),True,'2026-10-20',10**100]:
            with self.subTest(value=value):
                self.model.complete.reset_mock()
                await self.observe([{'user':'昨天的事','observed_at':value}])
                prompt=self.model.complete.call_args.args[0]
                marker=prompt[prompt.index('【对话】'):]
                self.assertIn('时间未知',marker)
                self.assertNotIn('1970-',marker)
                self.assertNotIn('当前时间：',marker)

    async def test_date_wrappers_never_become_evidence_or_renew_old_sources(self):
        old=stamp('2026-10-20T09:00:00+08:00')
        self.model.complete.return_value=json.dumps([{
            'category':'preference','content':'用户喜欢绿茶','keywords':['绿茶'],
            'evidence':'我喜欢绿茶','fact_key':'preference:tea'}],ensure_ascii=False)
        with patch('src.core.memory.time.time',return_value=stamp('2026-10-27T12:00:00+08:00')):
            items=await self.observe([{'id':'old','episode_id':'old-session','source_ids':['old'],
                'user':'我喜欢绿茶','observed_at':old}])
        self.assertEqual(len(items),1)
        item=items[0]
        self.assertEqual(item.evidence,'我喜欢绿茶')
        self.assertEqual(item.evidence_sources[0]['text'],'我喜欢绿茶')
        self.assertEqual(item.evidence_sources[0]['observed_at'],old)
        self.assertEqual(item.last_user_evidence_at,old)
        self.assertEqual(item.phase,'candidate')


    async def test_learning_queue_preserves_receipt_source_without_changing_evidence_clock(self):
        now=stamp('2026-10-27T12:00:00+08:00');old=stamp('2026-10-20T23:50:00+08:00')
        with patch('src.core.memory_learning.time.time',return_value=now), patch.object(self.memory.learner,'start'):
            for mid,at in [('known',int(old*1000)),('missing',0)]:
                message=WxMessage(mid,'ilink','private','alice','alice','Alice','bot',
                    '我今天要去看一场钢琴演出',timestamp=at)
                await self.memory.learner.enqueue(message)
        job=next(iter(self.memory.learner.pending.values()));known,received=job['turns']
        self.assertEqual(known['observed_at'],old)
        self.assertEqual(known['user_at'],old)
        self.assertEqual(known['user_time_source'],'message')
        self.assertEqual(received['observed_at'],now)
        self.assertEqual(received['received_at'],now)
        self.assertEqual(received['user_time_source'],'received')
        self.assertEqual(received['source_times'][0]['at'],None)
        loaded=MemoryLearningQueue(self.memory,self.memory.learner.path)
        self.assertEqual(next(iter(loaded.pending.values()))['turns'],job['turns'])
        await self.observe([received])
        prompt=self.model.complete.call_args.args[0]
        self.assertIn('用户来话接收时间（原发送时间未知）',prompt)
        self.assertEqual(prompt.count('我今天要去看一场钢琴演出'),1)
        await loaded.close()

    async def test_partial_midnight_batch_keeps_indexed_dates_and_verbatim_body(self):
        now=stamp('2026-10-27T12:00:00+08:00')
        before=stamp('2026-10-20T23:59:00+08:00');after=stamp('2026-10-21T00:01:00+08:00')
        text='我今天刚到剧场\n今天终于看完了\n后来我去喝茶'
        message=WxMessage('first','ilink','private','alice','alice','Alice','bot',text,
            timestamp=int(before*1000),batch_messages=[
                {'msg_id':'first','timestamp':int(before*1000)},
                {'msg_id':'second','timestamp':int(after*1000)},
                {'msg_id':'third','timestamp':0}])
        with patch('src.core.memory_learning.time.time',return_value=now), patch.object(self.memory.learner,'start'):
            await self.memory.learner.enqueue(message)
        turn=next(iter(self.memory.learner.pending.values()))['turns'][0]
        self.assertEqual(turn['observed_at'],before)
        self.assertEqual(turn['user_time_source'],'partial')
        self.assertEqual([row['at'] for row in turn['source_times']],[before,after,None])
        await self.observe([turn]);prompt=self.model.complete.call_args.args[0]
        self.assertIn('第1条=2026-10-20',prompt)
        self.assertIn('第2条=2026-10-21',prompt)
        self.assertIn('第3条=日期未知',prompt)
        self.assertEqual(prompt.count(text),1)
        self.assertEqual(turn['user'],text)

    async def test_recall_date_is_evidence_record_day_not_event_date(self):
        self.memory.context_timezone='America/New_York'
        now=stamp('2026-10-26T00:30:00+00:00')
        with patch('src.core.memory.time.time',return_value=now):
            self.memory.add_manual('user','alice','event','用户于2026-10-20看过演出',['演出'],platform='ilink')
        text=self.memory.build_context('alice','alice','演出',platform='ilink')
        self.assertIn('获知/证据记录日：2026-10-25（非事件发生日）',text)
        self.assertIn('用户于2026-10-20看过演出',text)


class LinkedHistoryDates(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.cfg=AppConfig()
        self.now=stamp('2026-10-27T12:00:00+08:00')
        self.model=AsyncMock();self.deliver=AsyncMock()
        self.path=Path(self.tmp.name)/'proactive.json'
        self.planner=self.make()

    def make(self):
        return ProactivePlanner(self.path,lambda platform,owner:'same-person',lambda:self.cfg,
            lambda rows,now:rows[0],self.model,self.deliver,clock=lambda:self.now)

    async def asyncTearDown(self):
        await self.planner.close();self.tmp.cleanup()

    def message(self,at=0,platform='onebot',mid='m1'):
        owner='123' if platform=='onebot' else 'alice'
        return WxMessage(mid,platform,'private','p_'+owner if platform=='onebot' else owner,
            owner,'测试','bot','昨天去看演出了',timestamp=at)

    async def test_delayed_ingress_keeps_original_date_but_activity_uses_receipt(self):
        old=stamp('2026-10-20T23:50:00+08:00')
        self.planner.note_in(self.message(int(old*1000)))
        row=self.planner.accounts['onebot:123'];event=row['history'][0]
        self.assertEqual(row['last_in'],self.now)
        self.assertEqual(event['ts'],old)
        self.assertEqual(event['user_at'],old)
        self.assertEqual(event['received_at'],self.now)
        self.assertEqual(event['user_time_source'],'message')
        timeline=self.planner.timeline([row])
        self.assertEqual(timeline[0]['time'],'2026-10-20T23:50:00+08:00')
        self.assertEqual(timeline[0]['timezone'],'Asia/Shanghai')

    async def test_invalid_or_future_source_time_is_labelled_as_received(self):
        for n,value in enumerate([0,-1,True,float('nan'),float('inf'),10**100,int((self.now+301)*1000)]):
            with self.subTest(value=value):
                self.planner.note_in(self.message(value,mid='invalid'+str(n)))
                event=self.planner.accounts['onebot:123']['history'][-1]
                self.assertEqual(event['ts'],self.now)
                self.assertEqual(event['user_at'],self.now)
                self.assertEqual(event['user_time_source'],'received')

    async def test_cross_day_reply_does_not_move_user_message_to_reply_day(self):
        old=stamp('2026-10-20T23:50:00+08:00');sent=stamp('2026-10-21T00:10:00+08:00')
        self.planner.note_in(self.message(int(old*1000)))
        self.now=sent;self.planner.note_out('onebot','p_123','嗯，那场演出。')
        turns=self.planner.shared_turns(self.message(platform='ilink'))
        self.assertEqual(len(turns),1)
        self.assertEqual(turns[0]['ts'],old)
        self.assertEqual(turns[0]['user_at'],old)
        self.assertEqual(turns[0]['assistant_at'],sent)
        self.assertEqual(turns[0]['user_time_source'],'message')
        restored=self.make();self.assertFalse(restored.load_error)
        self.assertEqual(restored.shared_turns(self.message(platform='ilink')),turns)
        await restored.close()

    async def test_orphan_assistant_retains_its_own_time(self):
        self.planner.note_in(self.message(int((self.now-86400)*1000)))
        row=self.planner.accounts['onebot:123'];row['history'].clear()
        self.planner.note_out('onebot','p_123','前几天的想法')
        turns=self.planner.shared_turns(self.message(platform='ilink'))
        self.assertEqual(len(turns),1)
        self.assertEqual(turns[0]['user'],'')
        self.assertEqual(turns[0]['assistant_at'],self.now)
        self.assertEqual(turns[0]['ts'],self.now)

    async def test_legacy_user_timestamp_survives_pairing_and_zero_is_unknown(self):
        self.planner.note_in(self.message())
        row=self.planner.accounts['onebot:123']
        row['history']=[{'role':'user','text':'旧用户原话','ts':0},
                        {'role':'assistant','text':'较晚的回复','ts':self.now}]
        turns=self.planner.shared_turns(self.message(platform='ilink'))
        self.assertEqual(turns[0]['ts'],0)
        self.assertEqual(turns[0]['user_at'],0)
        self.assertEqual(turns[0]['assistant_at'],self.now)
        self.assertEqual(self.planner.timeline([row])[0]['time'],'时间未知')

    async def test_proactive_prompt_carries_original_dates_without_rewriting_now(self):
        old=stamp('2026-10-20T23:50:00+08:00')
        self.planner.note_in(self.message(int(old*1000)))
        prompt=json.dumps(self.planner.prompt(list(self.planner.accounts.values()),self.now),ensure_ascii=False)
        self.assertIn('2026-10-20T23:50:00+08:00',prompt)
        self.assertIn('2026-10-27',prompt)
        self.assertIn('原发言',prompt)
