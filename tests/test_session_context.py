"""Stable-prefix and ratio compaction contracts; synthetic inputs, no API calls."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from src.core.context import ContextManager, ContextWindowExceeded, LLMCompactor, messages_tokens
from src.core.llm import LLMGateway, ProviderCfg
from src.core.types import WxMessage
from src.core.web_search import SEARCH_RULES
from src.store_memory import MemoryStore
from src.orchestrator import Orchestrator


def message(text='沿着河边走了一会', mid='1', owner='u'):
    return WxMessage(mid,'onebot','private','p_'+owner,owner,'用户','bot',text)


def marker(text='蓝色的风铃'):
    return {'id':'forgotten','accounts':[{'platform':'onebot','owner_id':'u'}],
            'content':text,'evidence':text,'source_texts':[text]}


class StableSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'context-onebot.json'
        self.store=MemoryStore(path=self.path)
        self.ctx=ContextManager(self.store)
        self.ctx.configure_session(context_window_tokens=1_000_000,compact_ratio=.4,
                                   output_reserve_tokens=1024,provider_fingerprint='fixture-flash')

    async def asyncTearDown(self):
        await self.ctx.close()
        self.tmp.cleanup()

    async def turn(self,text,mid,reply='听到了。',**kwargs):
        msg=message(text,mid)
        await self.ctx.accept_user(msg)
        request=await self.ctx.build(msg,**kwargs)
        await self.ctx.append(msg,reply)
        return request,await self.store.get_session(msg.ctx_key)

    async def test_second_turn_extends_exact_delivered_prefix(self):
        first,session=await self.turn('我喜欢这段旋律','1',memory_context='喜欢绿茶')
        second=await self.ctx.build(message('接着聊那段旋律','2'),memory_context='喜欢钢琴')
        self.assertEqual(second[:len(first)],first)
        self.assertEqual(json.dumps(second[:len(first)],ensure_ascii=False),json.dumps(first,ensure_ascii=False))
        self.assertEqual(second[:len(session['messages'])],session['messages'])
        self.assertEqual(second[len(first)],{'role':'assistant','content':'听到了。'})
        self.assertEqual(second[-1]['content'],'接着聊那段旋律')

    async def test_new_time_and_recall_never_rewrite_old_time(self):
        with patch('src.core.context.clock_context',return_value='[会话时间] 第一天'):
            _,session=await self.turn('第一天的见闻','1')
        with patch('src.core.context.clock_context',return_value='[会话时间] 第二天'):
            result=await self.ctx.build(message('今天接着说','2'),memory_context='新的参考')
        self.assertEqual(result[:len(session['messages'])],session['messages'])
        self.assertIn('[会话时间] 第二天',str(result[len(session['messages']):]))

    async def test_restart_reuses_persisted_exact_messages_and_ids(self):
        _,session=await self.turn('重启前说的话','1')
        loaded=MemoryStore(path=self.path)
        ctx=ContextManager(loaded)
        ctx.configure_session(context_window_tokens=1_000_000,compact_ratio=.4,
                              output_reserve_tokens=1024,provider_fingerprint='fixture-flash')
        try:
            result=await ctx.build(message('重启后的话','2'))
            self.assertEqual(result[:len(session['messages'])],session['messages'])
            self.assertEqual(sum(m.get('content')=='重启前说的话' for m in result),1)
        finally: await ctx.close()
        self.assertEqual(self.path.name,'context-onebot.json')

    async def test_more_than_eight_turns_and_five_thousand_tokens_do_not_compact(self):
        compact=NS(summarize=AsyncMock(return_value='摘要'))
        self.ctx.compactor=compact
        first=None
        for i in range(14):
            request,session=await self.turn('沿途的观察'+str(i)+'字'*620,str(i),'答复'+str(i))
            if first is None: first=request
        result=await self.ctx.build(message('继续这个话题','next'))
        self.assertEqual(result[:len(first)],first)
        self.assertIn('答复0',str(result))
        self.assertGreater(messages_tokens(result),5000)
        self.assertGreater(self.ctx.last_budget['turns'],8)
        compact.summarize.assert_not_awaited()

    async def test_one_million_window_compacts_only_at_forty_percent(self):
        compact=NS(summarize=AsyncMock(return_value='河边的观察仍在继续，未解决的问题保留。'))
        self.ctx.compactor=compact
        # ASCII ~4 characters/token: first request remains below 400,000.
        await self.turn('BEGIN-ORIGINAL '+('x'*1_500_000),'1','继续观察')
        self.assertLess(self.ctx.last_budget['session_tokens_estimated']+1024,400_000)
        compact.summarize.assert_not_awaited()
        current=message('新资料 '+('y'*120_000),'2')
        result=await self.ctx.build(current)
        compact.summarize.assert_awaited_once()
        self.assertIn('BEGIN-ORIGINAL',str(compact.summarize.call_args.args[0]))
        self.assertEqual(result[-1]['content'],current.render())
        self.assertTrue(self.ctx.last_budget['compacted'])
        self.assertEqual(self.ctx.last_budget['compact_threshold'],400_000)
        self.assertIn('河边的观察',str(result))

    async def test_compaction_failure_keeps_exact_old_prefix_and_backs_off(self):
        _,session=await self.turn('原始观察'*160,'1')
        self.ctx.configure_session(context_window_tokens=8000,compact_ratio=.1,output_reserve_tokens=100,
                                   provider_fingerprint='fixture-small')
        # Establish a session under the new provider fingerprint.
        compact=NS(summarize=AsyncMock(side_effect=RuntimeError('fixture')))
        self.ctx.compactor=compact
        first,_=await self.turn('第二段观察'*150,'2')
        retained=await self.store.get_session(message().ctx_key)
        next_request=await self.ctx.build(message('再补充一段','3'))
        self.assertEqual(next_request[:len(retained['messages'])],retained['messages'])
        self.assertIn('原始观察',str(next_request))
        self.assertEqual(compact.summarize.await_count,1)

    async def test_current_and_persona_are_not_truncated_on_overflow(self):
        self.ctx.configure_session(context_window_tokens=2048,compact_ratio=.4,output_reserve_tokens=128)
        current=message('完整当前资料'*2000)
        with self.assertRaises(ContextWindowExceeded): await self.ctx.build(current)
        self.assertEqual(current.content,'完整当前资料'*2000)
        self.assertIsNone(await self.store.get_session(current.ctx_key))

    async def test_actual_usage_can_trigger_earlier_compaction_than_estimate(self):
        compact=NS(summarize=AsyncMock(return_value='已核实的历史摘要'))
        self.ctx.compactor=compact
        msg=message('短输入','1')
        request=await self.ctx.build(msg)
        self.ctx.capture_request(msg,request,{'prompt_tokens':410_000})
        await self.ctx.append(msg,'已回复')
        await self.ctx.build(message('接着聊','2'))
        compact.summarize.assert_awaited_once()

    async def test_foreign_account_does_not_get_stable_prefix(self):
        await self.turn('属于u的蓝色风铃','1')
        result=await self.ctx.build(message('另一位用户','2',owner='other'))
        self.assertNotIn('蓝色风铃',str(result))

    async def test_persona_and_provider_change_rebuild_without_old_dynamic_refs(self):
        await self.turn('真实用户经历','1',memory_context='旧人格动态例子')
        self.ctx.provider_fingerprint='different-model'
        self.ctx.system_prompt='另一人格的完整设定'
        result=await self.ctx.build(message('新模型本轮','2'))
        self.assertEqual(result[0]['content'],'另一人格的完整设定')
        self.assertNotIn('旧人格动态例子',str(result))
        self.assertIn('真实用户经历',str(result))

    async def test_silent_user_experience_is_added_once_without_fake_assistant(self):
        _,session=await self.turn('第一轮已答','1')
        silent=message('安静分享的一句','2')
        await self.ctx.accept_user(silent)
        current=message('下一段内容','3')
        result=await self.ctx.build(current)
        self.assertEqual(result[:len(session['messages'])],session['messages'])
        index=next(i for i,m in enumerate(result) if m.get('content')=='安静分享的一句')
        self.assertNotEqual(result[index+1]['role'],'assistant')
        self.assertEqual(sum(m.get('content')=='安静分享的一句' for m in result),1)

    async def test_aborted_draft_never_becomes_a_session(self):
        old=message('未提交的旧稿','1')
        await self.ctx.accept_user(old)
        await self.ctx.build(old)
        self.ctx.discard(old)
        self.assertIsNone(await self.store.get_session(old.ctx_key))
        result=await self.ctx.build(message('新的补充','2'))
        self.assertIn('未提交的旧稿',str(result))
        self.assertFalse(any(m['role']=='assistant' for m in result))

    async def test_forget_purges_prefix_and_blocks_late_paraphrase_commit(self):
        await self.turn('书桌上有蓝色的风铃','1')
        inflight=message('接着刚才的事','2')
        await self.ctx.accept_user(inflight)
        await self.ctx.build(inflight)
        self.store.suppress(marker())
        await self.ctx.append(inflight,'那件会在风中轻响的物件，确实很特别。')
        history=str(await self.store.get_recent(inflight.ctx_key))
        self.assertNotIn('轻响的物件',history)
        self.assertIsNone(await self.store.get_session(inflight.ctx_key))
        result=await self.ctx.build(message('换个话题','3'))
        self.assertNotIn('蓝色的风铃',str(result))

    async def test_accepted_revision_guards_replayed_delivery_without_pending_draft(self):
        msg=message('已经被平台提交的内容','1')
        await self.ctx.accept_user(msg)
        await self.store.clear_session(msg.ctx_key)
        await self.ctx.append(msg,'旧回复的不同表述')
        self.assertNotIn('旧回复的不同表述',str(await self.store.get_recent(msg.ctx_key)))

    async def test_image_order_and_real_vision_window_are_preserved(self):
        self.ctx.image_context_window_tokens=64000
        self.ctx.image_output_reserve_tokens=512
        msg=message('刚才两张照片','1')
        msg.images=[{'url':'source-1'},{'url':'source-2'}]
        images=[{'type':'image_url','image_url':{'url':'data:image/png;base64,FIXTURE1'}},
                {'type':'image_url','image_url':{'url':'data:image/png;base64,FIXTURE2'}}]
        first=await self.ctx.build(msg,image_inputs=images)
        self.assertEqual(self.ctx.last_budget['context_window'],64000)
        await self.ctx.append(msg,'两张图的可见观察')
        result=await self.ctx.build(message('再说第二张','2'))
        self.assertEqual(result[:len(first)],first)
        data=str(result)
        self.assertLess(data.index('FIXTURE1'),data.index('FIXTURE2'))
        self.assertEqual(self.ctx.last_budget['context_window'],64000)

    async def test_linked_shared_turns_append_without_inserting_into_existing_prefix(self):
        shared=[{'user':'跨端稍早的话','assistant':'跨端答复','ts':1,'source_ids':['cross:1']}]
        self.ctx.shared_history=lambda _:shared
        _,session=await self.turn('当前渠道第一轮','1')
        shared.append({'user':'另一渠道刚来的话','assistant':'','ts':2,'source_ids':['cross:2']})
        result=await self.ctx.build(message('本端下一轮','2'))
        self.assertEqual(result[:len(session['messages'])],session['messages'])
        self.assertEqual(sum(m.get('content')=='跨端稍早的话' for m in result),1)
        self.assertIn('另一渠道刚来的话',str(result[len(session['messages']):]))

    async def test_capture_actual_tool_request_before_delivery(self):
        msg=message('本轮查到的公开资料','1')
        initial=await self.ctx.build(msg)
        actual=initial+[{'role':'assistant','content':None,'tool_calls':[
            {'id':'tool-1','type':'function','function':{'name':'web_search','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'tool-1','content':'本轮正文证据'}]
        self.ctx.capture_request(msg,actual,{'prompt_tokens':1200,'prompt_cache_hit_tokens':0})
        await self.ctx.append(msg,'自然整合后的完整答复')
        result=await self.ctx.build(message('接着刚才的资料','2'))
        self.assertEqual(result[:len(actual)],actual)
        self.assertEqual(result[len(actual)]['content'],'自然整合后的完整答复')
        session=await self.store.get_session(msg.ctx_key)
        self.assertEqual(session['prompt_tokens'],1200)
        self.assertIsNone(self.ctx.last_budget['cache_hit_tokens'])


class GatewayCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_is_task_local_and_missing_cache_usage_is_unknown(self):
        gateway=LLMGateway(ProviderCfg('fixture','https://example.invalid','fixture','fixture'))
        async def request(text):
            with gateway.capture_request() as trace:
                await asyncio.sleep(0)
                gateway._capture_request([{'role':'user','content':text}],NS(prompt_tokens=12))
            return trace
        a,b=await asyncio.gather(request('a'),request('b'))
        self.assertEqual(a['messages'][0]['content'],'a')
        self.assertEqual(b['messages'][0]['content'],'b')
        self.assertIsNone(a['usage']['prompt_cache_hit_tokens'])
        self.assertIsNone(gateway._request_capture.get())
        await gateway.aclose()

    async def test_summary_reads_every_original_turn_and_uses_main_fast_gateway(self):
        background=NS(complete=AsyncMock(return_value='wrong'))
        primary=NS(complete_once=AsyncMock(return_value='所有历史的摘要'))
        compactor=LLMCompactor(background)
        compactor.session_gateway=primary
        turns=[{'user':'最初的重要约定'+str(i)+'中'*150,'assistant':'回应'} for i in range(40)]
        self.assertEqual(await compactor.summarize(turns),'所有历史的摘要')
        prompt=primary.complete_once.call_args.args[0]
        self.assertIn('最初的重要约定0',prompt)
        self.assertIn('最初的重要约定39',prompt)
        background.complete.assert_not_awaited()
        self.assertEqual(primary.complete_once.call_args.kwargs['max_tokens'],1800)

    async def test_slow_cancel_cleanup_does_not_block_next_turn_slot(self):
        obj=object.__new__(Orchestrator)
        obj._retired_work=set()
        changed=[False]
        def current(msg):
            if changed[0]: raise RuntimeError('new input')
        obj._check_current=current
        cleaned=asyncio.Event()
        async def slow():
            try: await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(.2)
                cleaned.set()
                return 'discarded old answer'
        task=asyncio.create_task(obj._current_work(message(),slow()))
        await asyncio.sleep(.01)
        changed[0]=True
        started=time.monotonic()
        with self.assertRaises(RuntimeError): await task
        self.assertLess(time.monotonic()-started,.09)
        await asyncio.wait_for(cleaned.wait(),.4)
        await asyncio.gather(*obj._retired_work)


class LargeDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_current_input_is_preserved_and_skips_duplicate_judge_prefill(self):
        from src.core.reply_decision import ReplyDecider
        gateway=NS(complete_once=AsyncMock())
        current=message('一段完整的观察'*1500)
        original=current.content
        result=await ReplyDecider(gateway).decide(current)
        self.assertEqual((result.action,result.source),('reply','conservative'))
        self.assertEqual(current.content,original)
        gateway.complete_once.assert_not_awaited()


class MediaStoreIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'context-ilink.json'
        self.store=MemoryStore(path=self.path)
        self.ctx=ContextManager(self.store)

    async def asyncTearDown(self):
        await self.ctx.close()
        self.tmp.cleanup()

    async def picture(self):
        current=message('这张图的说明','picture')
        current.images=[{'url':'local-image'}]
        url='data:image/png;base64,'+'PUBLIC_SYNTHETIC_PIXEL'*800
        image={'type':'image_url','image_url':{'url':url}}
        first=await self.ctx.build(current,image_inputs=[image])
        await self.ctx.append(current,'本轮实际回复')
        return current,first,url

    async def test_store_json_uses_refs_and_restart_recovers_exact_prefix(self):
        current,first,url=await self.picture()
        encoded=self.path.read_text()
        self.assertNotIn(url,encoded)
        self.assertIn('wxbot-session-media://',encoded)
        blobs=list(self.path.with_name('context-ilink-media').glob('*.blob'))
        self.assertEqual(len(blobs),1)
        loaded=MemoryStore(path=self.path)
        actual=await loaded.get_session(current.ctx_key)
        self.assertEqual(actual['messages'][:len(first)],first)
        self.assertEqual(blobs[0].read_text(),url)

    async def test_accept_user_does_not_rewrite_image_blob(self):
        current,first,url=await self.picture()
        blob=next(self.path.with_name('context-ilink-media').glob('*.blob'))
        stamp=blob.stat().st_mtime_ns
        await self.ctx.accept_user(message('后续普通文字','next'))
        self.assertEqual(blob.stat().st_mtime_ns,stamp)
        self.assertNotIn(url,self.path.read_text())
        self.assertEqual((await self.store.get_session(current.ctx_key))['messages'][:len(first)],first)

    async def test_missing_media_only_invalidates_prefix_not_raw_experience(self):
        current,first,url=await self.picture()
        next(self.path.with_name('context-ilink-media').glob('*.blob')).unlink()
        loaded=MemoryStore(path=self.path)
        self.assertIsNone(await loaded.get_session(current.ctx_key))
        self.assertIn('本轮实际回复',str(await loaded.get_recent(current.ctx_key)))

    async def test_forgetting_removes_all_media_references_not_physical_backup_blob(self):
        current,first,url=await self.picture()
        self.store.suppress({'id':'media-owner','accounts':[{'platform':'onebot','owner_id':'u'}],
                             'content':'任意明确遗忘内容'})
        self.assertIsNone(await self.store.get_session(current.ctx_key))
        self.assertNotIn('wxbot-session-media://',self.path.read_text())
        self.assertEqual(len(list(self.path.with_name('context-ilink-media').glob('*.blob'))),1)


class ToolReserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_protocol_space_is_reserved_before_the_main_request(self):
        ctx=ContextManager(MemoryStore())
        ctx.configure_session(context_window_tokens=4096,compact_ratio=.4,output_reserve_tokens=128)
        ctx.tool_reserve_tokens=2000
        current=message('z'*10000)
        with self.assertRaises(ContextWindowExceeded): await ctx.build(current)
        self.assertIsNone(await ctx.store.get_session(current.ctx_key))
        await ctx.close()
