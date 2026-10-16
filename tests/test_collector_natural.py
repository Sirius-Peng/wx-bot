"""Deterministic collector/rate-limit tests without live accounts or model calls."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT))
from src.adapters.ilink import ILinkAdapter
from src.core.conversation import ConversationChanged,MessageBuffer,members,merge_messages
from src.core.sender import DeliveryError,HumanLikeSender,TokenBucket
from src.core.turn_taking import SendingCadence,plan_wait,ready
from src.core.types import WxMessage


def message(text='完整的一句话。',mid='1'):
    return WxMessage(mid,'onebot','private','p_u','u','用户','bot',text)


def raw(mid,at=None):
    return {'message_type':1,'message_id':str(mid),'from_user_id':'wx_user','context_token':'token-'+str(mid),
            '_wxbot_received_at':time.time()-1 if at is None else at,
            'item_list':[{'type':1,'text_item':{'text':'第'+str(mid)+'段。'}}]}


async def eventually(predicate,timeout=.8):
    end=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>end:raise AssertionError('test condition timed out')
        await asyncio.sleep(.005)


class PlanTests(unittest.TestCase):
    def test_default_normal_fragment_media_and_done_windows(self):
        self.assertEqual(plan_wait([(message(),0)]).delay,4)
        for text in ('然后','刚刚','这句话还没说完，'):
            self.assertEqual(plan_wait([(message(text),0)]).delay,6)
        picture=message('[图片]说明');picture.images=[{}]
        video=message('[视频]');video.msg_type='video'
        self.assertEqual(plan_wait([(picture,0)]).delay,6)
        self.assertEqual(plan_wait([(video,0)]).delay,6)
        self.assertEqual(plan_wait([(message('说完了'),0)]).delay,.3)

    def test_last_twenty_historical_gaps_raise_window_with_eight_second_cap(self):
        self.assertEqual(plan_wait([(message(),100)],cadence=[5]*20).delay,7.5)
        self.assertEqual(plan_wait([(message(),100)],cadence=[20]*20).delay,8)
        self.assertEqual(plan_wait([(message(),100)],cadence=[20]*20+[1]*20).delay,4)

    def test_long_bursts_never_force_readiness(self):
        for duration in (12,20,30):
            items=[(message('第'+str(i)+'条','m'+str(i)),i*.5) for i in range(duration*2+1)]
            p=plan_wait(items)
            self.assertEqual(ready(p,duration+.1,duration,len(items)),(False,False))
            self.assertEqual(ready(p,duration+8,duration,len(items)),(True,False))

    def test_fifty_is_not_an_utterance_boundary(self):
        self.assertEqual(ready(plan_wait([(message(),0)]),1,.9,80),(False,False))

    def test_pause_expiry_is_not_force_permission(self):
        p=plan_wait([(message('等我说完'),0)],pause_timeout=120)
        self.assertEqual(ready(p,119,0,1),(False,False))
        self.assertEqual(ready(p,120,0,1),(True,False))
        self.assertEqual(ready(p,120,119.9,2),(False,False))

    def test_invalid_optional_cadence_does_not_poison_readiness(self):
        c=SendingCadence({'a':{'at':float('nan')},'b':{'at':1,'gaps':[float('nan'),'bad',5]}})
        self.assertEqual(c.gaps('a'),[])
        self.assertEqual(c.gaps('b'),[5])

    def test_cadence_ignores_duplicate_and_separate_conversation_gaps(self):
        c=SendingCadence()
        c.record('p','1',0);c.record('p','2',5);c.record('p','2',50);c.record('p','3',1900)
        self.assertEqual(c.gaps('p'),[5])
        for i in range(4,30):c.record('p',str(i),1900+i*2)
        self.assertEqual(len(c.gaps('p')),20)
        self.assertEqual(c.gaps('p'),[2]*20)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_all_eighty_fragments_become_one_batch(self):
        calls=[]
        async def callback(m):calls.append(m)
        b=MessageBuffer(callback);b.configure(.01,.01,human=False);b.start()
        try:
            for i in range(80):b.submit(message('第'+str(i)+'条',str(i)))
            await eventually(lambda:bool(calls));await asyncio.sleep(.03)
            self.assertEqual(len(calls),1)
            self.assertEqual(len(members(calls[0])),80)
        finally:await b.close()

    async def test_legacy_force_cannot_disable_new_input_invalidation(self):
        b=MessageBuffer(None);a=message();b.pending[b.key(a)]=[(a,0)]
        b.active[b.key(a)]={'known':{b.identity(a)},'force':True,'typing_at':0}
        b.pending[b.key(a)].append((message('下一段','2'),1))
        with self.assertRaises(ConversationChanged):b.check(a)

    async def test_cadence_survives_empty_pending_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'pending.json';b=MessageBuffer(None);b.configure(4,10,path)
            b.cadence.record('p','1',10);b.cadence.record('p','2',15);b.save()
            c=MessageBuffer(None);c.configure(4,10,path)
            self.assertEqual(c.cadence.gaps('p'),[5])
            self.assertEqual(json.loads(path.read_text())['version'],2)

    async def test_qq_regroups_even_after_old_deadline(self):
        entered=asyncio.Event();release=asyncio.Event();replies=[]
        async def callback(m):
            if len(members(m))==1:
                entered.set();await release.wait();b.check(m)
            else:replies.append(m)
        b=MessageBuffer(callback);b.configure(.01,.01,human=False);b.start()
        try:
            b.submit(message());await entered.wait();b.submit(message('追加内容','2'));release.set()
            await eventually(lambda:bool(replies))
            self.assertEqual(len(members(replies[0])),2)
        finally:await b.close()

    async def test_wechat_eighty_messages_are_one_batch_and_latest_token(self):
        with tempfile.TemporaryDirectory() as d:
            a=ILinkAdapter(d);a.configure_burst(.01,.01,human=False)
            a._pending=[raw(i) for i in range(80)];calls=[]
            async def handle(m):calls.append(m)
            a._handler=handle
            try:
                self.assertTrue(await a._handle_raw(a._pending[0]))
                self.assertEqual(len(calls),1);self.assertEqual(len(members(calls[0])),80)
                self.assertEqual(calls[0].context_token,'token-79');self.assertEqual(a._pending,[])
            finally:await a._client.aclose()

    async def test_wechat_submitted_failure_retries_same_credentials_with_new_input(self):
        with tempfile.TemporaryDirectory() as d:
            a=ILinkAdapter(d);a.configure_burst(.01,.01,human=False);a.bot_id='bot'
            first=raw(1);a._pending=[first];sent=[]
            async def post(endpoint,body):
                sent.append(body['msg'].copy())
                if len(sent)==1:raise OSError('lost acknowledgement')
                return {'ret':0,'message_id':'accepted'}
            a._post=post
            async def handle(m):await a.send_text(m.chat_id,'完整旧稿。')
            a._handler=handle
            try:
                with self.assertRaises(OSError):await a._handle_raw(first)
                a._pending.append(raw(2))
                self.assertTrue(await a._handle_raw(first))
                self.assertEqual(sent[0],sent[1]);self.assertEqual(sent[1]['context_token'],'token-1')
                self.assertEqual(len(a._pending),1)
            finally:await a._client.aclose()


class SenderTests(unittest.IsolatedAsyncioTestCase):
    def sender(self):
        class Adapter:
            platform='onebot'
            def __init__(self):self.sent=[]
            async def send_text(self,chat,text,at=None):self.sent.append(text);return 'out'
        a=Adapter();return a,HumanLikeSender(a)

    async def test_token_wait_cancels_old_draft_before_submission(self):
        a,s=self.sender();s.chat_buckets['p_u'].tokens=0;changed=False;submitted=[]
        def check():
            if changed:raise ConversationChanged('new input')
        task=asyncio.create_task(s.send('p_u','旧稿',check_current=check,on_submit=lambda:submitted.append(1)))
        await asyncio.sleep(.025);changed=True
        with self.assertRaises(ConversationChanged):await asyncio.wait_for(task,.2)
        self.assertEqual(a.sent,[]);self.assertEqual(submitted,[])
        self.assertFalse(s.chat_buckets['p_u']._lock.locked())

    async def test_bucket_lock_wait_cancels_and_does_not_leak_lock(self):
        b=TokenBucket(2);await b._lock.acquire();changed=False
        def check():
            if changed:raise ConversationChanged('new input')
        task=asyncio.create_task(b.acquire(check));await asyncio.sleep(.01);changed=True
        with self.assertRaises(ConversationChanged):await asyncio.wait_for(task,.2)
        self.assertTrue(b._lock.locked());b._lock.release()
        await asyncio.wait_for(b.acquire(),.1)
        self.assertFalse(b._lock.locked())

    async def test_submit_callback_is_last_check_and_not_repeated_after_network_starts(self):
        a,s=self.sender();order=[];changed=False
        def check():
            if changed:raise ConversationChanged('late input')
            order.append('check')
        def submit():order.append('submit')
        async def send(chat,text,at=None):
            nonlocal changed
            order.append('network');changed=True;await asyncio.sleep(.01);return 'out'
        a.send_text=send
        self.assertEqual(await s.send('p_u','正文',check_current=check,on_submit=submit),['out'])
        self.assertEqual(order[-3:],['check','submit','network'])
        self.assertEqual(order.count('submit'),1)

    async def test_normalized_submission_text_is_recorded_before_network(self):
        a,s=self.sender();record=[]
        async def stream():yield '第一句。\n';yield '第二句。'
        await s.stream_send('p_u',stream(),on_submit_text=record.append)
        self.assertEqual(record,['第一句。第二句。'])
        self.assertEqual(record,a.sent)

    async def test_legacy_custom_bucket_with_no_parameters_still_supported(self):
        class Bucket:
            async def acquire(self):return
        a,s=self.sender();s.global_bucket=Bucket();s.chat_buckets['p_u']=Bucket()
        await s.send('p_u','完整文本',check_current=lambda:None)
        self.assertEqual(a.sent,['完整文本'])

    async def test_only_explicit_platform_rate_limit_creates_backoff(self):
        a,s=self.sender()
        class Limited(Exception):status_code=429;retry_after=2
        async def fail(*args,**kwargs):raise Limited('rate limit')
        a.send_text=fail
        with self.assertRaises(DeliveryError):await s.send('p_u','正文')
        self.assertGreater(s._backoff_until['p_u'],time.monotonic()+1)


if __name__=='__main__':unittest.main()
