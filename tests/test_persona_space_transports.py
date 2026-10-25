"""Persist ingress space; switching cannot turn old drafts into a new persona."""
import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from src.adapters.desktop import DesktopAdapter
from src.adapters.ilink import ILinkAdapter
from src.adapters.onebot import OneBotAdapter
from src.core.conversation import MessageBuffer, merge_messages, members
from src.core.types import WxMessage

A, B = 'custom--neutral', 'phrolova--trusted'
def message(text='真实来话', mid='1', space=A):
    return WxMessage(mid,'onebot','private','p_123','123','用户','bot',text,persona_space=space)
def raw(mid='1', text='真实来话', space=A):
    value={'message_id':mid,'from_user_id':'wx','to_user_id':'bot','message_type':1,
           'item_list':[{'type':1,'text_item':{'text':text}}]}
    if space is not None:value['_wxbot_persona_space']=space
    return value

class CollectorSpaces(unittest.IsolatedAsyncioTestCase):
    def test_dto_legacy_default_and_persisted_tag(self):
        value=asdict(message());self.assertEqual(WxMessage(**value).persona_space,A)
        value.pop('persona_space');self.assertEqual(WxMessage(**value).persona_space,'')
    def test_mixed_spaces_cannot_merge(self):
        with self.assertRaises(ValueError):merge_messages([message(),message(mid='2',space=B)])
        merged=merge_messages([message(),message('补充','2')])
        self.assertEqual(merged.persona_space,A)
        self.assertEqual([m.persona_space for m in members(merged)],[A,A])
    def test_buffer_keys_and_identity_include_nonempty_space(self):
        a,b=message(),message(space=B)
        self.assertNotEqual(MessageBuffer.key(a),MessageBuffer.key(b))
        self.assertNotEqual(MessageBuffer.identity(a),MessageBuffer.identity(b))
        legacy=message(space='')
        self.assertEqual(MessageBuffer.key(legacy),legacy.ctx_key+':'+legacy.sender_id)
    async def test_inactive_wait_skips_120_second_pause(self):
        seen=[];done=asyncio.Event()
        async def callback(msg):seen.append(msg);done.set()
        buffer=MessageBuffer(callback);buffer.active_space=lambda:B;buffer.legacy_space=A
        buffer.configure(2,10,human=True,pause_timeout=120)
        buffer.submit(message('等我说完'));buffer.start()
        try:
            await asyncio.wait_for(done.wait(),.25)
            self.assertEqual([(m.content,m.persona_space) for m in seen],[('等我说完',A)])
        finally:await buffer.close()
    async def test_active_space_keeps_quiet_window(self):
        callback=AsyncMock();buffer=MessageBuffer(callback);buffer.active_space=lambda:A;buffer.legacy_space=A
        buffer.configure(2,10);buffer.submit(message());buffer.start()
        try:
            await asyncio.sleep(.06);callback.assert_not_awaited()
        finally:await buffer.close()
    async def test_restore_untagged_pending_goes_to_legacy_even_when_binding_is_late(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'pending.json';old=asdict(message('等我说完',space=''));old.pop('persona_space')
            path.write_text(json.dumps({'version':2,'pending':{'old-key':[{'message':old,'at':time.time()}]}}))
            seen=[];done=asyncio.Event()
            async def callback(msg):seen.append(msg);done.set()
            buffer=MessageBuffer(callback);buffer.configure(2,10,path)
            buffer.active_space=lambda:B;buffer.legacy_space=lambda:A;buffer.start()
            try:
                await asyncio.wait_for(done.wait(),.25);self.assertEqual(seen[0].persona_space,A)
            finally:await buffer.close()
    async def test_recovered_mixed_key_splits_before_processing(self):
        seen=[];done=asyncio.Event()
        async def callback(msg):
            seen.append(msg)
            if len(seen)==2:done.set()
        buffer=MessageBuffer(callback);buffer.active_space=lambda:B;buffer.legacy_space=A
        buffer.pending={'old-key':[(message('A'),time.time()),(message('B','2',B),time.time())]};buffer.start()
        try:
            await asyncio.wait_for(done.wait(),.25)
            self.assertEqual({m.persona_space for m in seen},{A,B});self.assertEqual({m.content for m in seen},{'A','B'})
        finally:await buffer.close()
    def test_typing_hint_targets_current_space_only(self):
        buffer=MessageBuffer(AsyncMock());current=[A];buffer.active_space=lambda:current[0];buffer.legacy_space=A
        a,b=message(),message(mid='2',space=B);buffer.submit(a);buffer.submit(b);current[0]=B
        self.assertTrue(buffer.note_typing('p_123','123'))
        self.assertNotIn(buffer.key(a),buffer.typing_hints);self.assertIn(buffer.key(b),buffer.typing_hints)

class ProtocolSpaces(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_stamps_real_ingress_and_partitions_pending(self):
        adapter=OneBotAdapter();current=[A];adapter.active_space=lambda:current[0];adapter.legacy_space=A
        adapter.on_message(AsyncMock());adapter.configure_burst(2,10)
        event={'post_type':'message','message_type':'private','user_id':123,'self_id':999,
               'message_id':1,'message':[{'type':'text','data':{'text':'一'}}], 'persona_space':'untrusted'}
        try:
            await adapter.handle_event(event);current[0]=B
            await adapter.handle_event({**event,'message_id':2,'message':[{'type':'text','data':{'text':'二'}}]})
            rows=[items[0][0] for items in adapter._bursts.pending.values()]
            self.assertEqual({m.persona_space for m in rows},{A,B})
            self.assertEqual(adapter.pending_turn(next(m for m in rows if m.persona_space==B)).content,'二')
        finally:await adapter.stop()
    async def test_ilink_normalize_uses_saved_tag_or_legacy_not_active(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=ILinkAdapter(d);adapter.active_space=lambda:B;adapter.legacy_space=A
            try:
                self.assertEqual(adapter._normalize(raw()).persona_space,A)
                self.assertEqual(adapter._normalize(raw(space=None)).persona_space,A)
                self.assertEqual(adapter._normalize(raw(space=B)).persona_space,B)
            finally:await adapter._client.aclose()
    async def test_ilink_peer_and_snapshot_filter_space(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=ILinkAdapter(d);adapter.active_space=lambda:B;adapter.legacy_space=A
            a,b,legacy=raw(),raw('2','二',B),raw('3','旧',None);adapter._pending=[a,b,legacy]
            try:
                self.assertEqual(adapter._peer_pending(a),[a,legacy])
                snapshot=adapter.pending_turn(adapter._normalize(b))
                self.assertEqual((snapshot.content,snapshot.persona_space),('二',B))
            finally:await adapter._client.aclose()
    async def test_ilink_inactive_wait_does_not_block_current_persona(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=ILinkAdapter(d);adapter.active_space=lambda:B;adapter.legacy_space=A
            adapter.configure_burst(2,10,human=True,pause_timeout=120)
            a,b=raw(text='等我说完'),raw('2','新空间',B);adapter._pending=[a,b]
            try:
                self.assertEqual(await asyncio.wait_for(adapter._batch_raw(a),.2),[a])
                saved=json.loads(adapter._cursor_file.read_text())
                self.assertEqual([r['_wxbot_persona_space'] for r in saved['pending']],[A,B])
            finally:await adapter._client.aclose()
    async def test_ilink_real_poller_stamps_before_persisting_and_overrides_wire_tag(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=ILinkAdapter(d);current=[A];adapter.active_space=lambda:current[0];adapter.legacy_space=A;adapter._running=True
            calls=0
            async def post(*args):
                nonlocal calls
                calls+=1
                if calls==1:return {'msgs':[raw(space='forged')]}
                if calls==2:current[0]=B;return {'msgs':[raw('2',space='forged')]}
                adapter._running=False;return {'msgs':[]}
            adapter._post=post
            try:
                await asyncio.wait_for(adapter._receive_loop(),.5)
                self.assertEqual([r['_wxbot_persona_space'] for r in adapter._pending],[A,B])
                self.assertEqual([r['_wxbot_persona_space'] for r in json.loads(adapter._cursor_file.read_text())['pending']],[A,B])
            finally:await adapter._client.aclose()

class DesktopSpaces(unittest.IsolatedAsyncioTestCase):
    async def test_new_messages_events_and_snapshots_use_current_space(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);current=[A];adapter.active_space=lambda:current[0];adapter.legacy_space=A
            await adapter.submit('A原话','1',A);current[0]=B;await adapter.submit('B原话','2',B)
            self.assertEqual(len(adapter.buffer.pending),2)
            rows=[v[0][0] for v in adapter.buffer.pending.values()]
            self.assertEqual({m.persona_space for m in rows},{A,B})
            self.assertEqual(adapter.pending_turn(next(m for m in rows if m.persona_space==B)).content,'B原话')
            self.assertEqual([e['text'] for e in adapter.snapshot()],['B原话'])
    async def test_submitted_old_reply_keeps_old_space_after_switch(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);current=[A];adapter.active_space=lambda:current[0];adapter.legacy_space=A
            old=replace(message(),platform='desktop',chat_id='local',sender_id='local')
            with adapter.bind_reply_context(old):
                current[0]=B;await adapter.send_text('local','已提交旧答案');await adapter.send_typing('local',False)
            self.assertEqual(adapter.events[0]['persona_space'],A);self.assertEqual(adapter.snapshot(),[])
            current[0]=A;self.assertEqual(adapter.snapshot()[0]['text'],'已提交旧答案')
    async def test_late_draft_and_cancel_keep_original_space_without_binding(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);adapter.active_space=lambda:B;adapter.legacy_space=A
            queue=asyncio.Queue();adapter.listeners.add(queue)
            old=replace(message(),platform='desktop',chat_id='local',sender_id='local')
            await adapter.preview(old,'旧草稿');await adapter.cancel_turn(old)
            self.assertEqual([(await queue.get())['persona_space'] for _ in range(2)],[A,A])
    async def test_stale_reconnect_input_does_not_become_new_persona_input(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);adapter.active_space=lambda:B;adapter.legacy_space=A
            with self.assertRaises(ValueError):await adapter.submit('旧未确认输入','id',A)
            self.assertFalse(adapter.buffer.pending);self.assertFalse(adapter.events)
    async def test_untagged_events_belong_only_to_legacy_and_owner_is_still_checked(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);current=[B];adapter.active_space=lambda:current[0];adapter.legacy_space=A
            adapter.events=[{'type':'completed','seq':1,'text':'旧无tag'},
                {'type':'completed','seq':2,'text':'B记录','persona_space':B,'owner_id':'local'},
                {'type':'completed','seq':3,'text':'别的用户','persona_space':B,'owner_id':'other'}]
            self.assertEqual([e['text'] for e in adapter.snapshot()],['B记录']);current[0]=A
            self.assertEqual([e['text'] for e in adapter.snapshot()],['旧无tag'])
            self.assertEqual(adapter.snapshot()[0]['persona_space'],A)
    async def test_physical_client_id_dedup_survives_switch_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            adapter=DesktopAdapter(d);adapter.active_space=lambda:A;adapter.legacy_space=A
            await adapter.submit('原话','same',A)
            again=DesktopAdapter(d);again.active_space=lambda:B;again.legacy_space=A
            self.assertTrue((await again.submit('原话','same',B))['duplicate'])
            self.assertEqual(len(again.events),1);self.assertEqual(again.snapshot(),[])

if __name__=='__main__':unittest.main()
