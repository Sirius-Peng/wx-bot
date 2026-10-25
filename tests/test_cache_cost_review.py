"""Budget regression and read-only v2.1.7 provenance checks, with synthetic data only.

No user configuration, credentials, chats or actual provider calls are read.
Historical tests skip when the source snapshot has no Git tag database.
"""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from src.core.conversation import merge_messages
from src.core.llm import LLMGateway, ProviderCfg
from src.core.reply_decision import ReplyDecider
from src.core.turn_preparation import TurnPreparation
from src.core.types import WxMessage
from src.store_memory import MemoryStore


def message(text='窗外的雨已经落了一整天。', mid='first', owner='cost-fixture'):
    return WxMessage(mid,'desktop','private',owner,owner,'合成用户','bot',text)


class BurstPaidBudget(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cloud = NS(complete_once=AsyncMock(return_value='C'))
        self.decider = ReplyDecider(self.cloud)
        self.orch = NS(reply_decider=self.decider, memory=None, store=MemoryStore(),
                       ctx=NS(system_prompt='稳定人格'), _memory_revision=lambda _:None)
        self.prep = TurnPreparation(self.orch, debounce=.01)

    async def asyncTearDown(self): await self.prep.close()

    async def preview(self, value):
        self.prep.accept(value)
        await self.prep.entries[self.prep.key(value)].task

    async def final(self, value, recent=()):
        with self.prep.bind(value): return await self.decider.decide(value, recent)

    async def test_gaps_longer_than_default_debounce_spend_once_for_whole_pending_burst(self):
        self.prep.debounce = .8
        originals=[]
        for index, text in enumerate(('窗外的雨下了一整天。','屋檐的水滴连成了一串。','街灯照着空荡的小路。')):
            originals.append(message(text, str(index)))
            # Keep actual inter-fragment gaps longer than the real default .8s,
            # including when an exhausted preview now returns immediately.
            if index: await asyncio.sleep(.81)
            await self.preview(merge_messages(originals))
        complete = merge_messages(originals)
        result = await self.final(complete)
        self.cloud.complete_once.assert_awaited_once()
        self.assertEqual((result.action,result.source), ('reply','conservative'))
        self.assertEqual(self.decider.last_status['reason'], 'burst_budget_exhausted')
        self.assertEqual(complete.content, '\n'.join(item.content for item in originals))

    async def test_stale_context_only_result_is_never_reused_for_new_complete_text(self):
        first=message();await self.preview(first)
        full=merge_messages([first,message('水滴把路面照得发亮。','second')])
        await self.preview(full)
        result=await self.final(full)
        self.assertEqual((result.action,result.source),('reply','conservative'))
        self.cloud.complete_once.assert_awaited_once()
        self.assertNotIn('prepared',self.decider.last_status)

    async def test_new_burst_has_new_quota_and_same_snapshot_handoff_is_one_shot(self):
        first=message();await self.preview(first)
        self.assertEqual((await self.final(first)).source,'cloud')
        self.assertTrue(self.decider.last_status['prepared'])
        self.assertEqual((await self.final(first)).source,'conservative')
        next_turn=message('我走到了河边。','new-burst')
        await self.preview(next_turn)
        self.assertEqual((await self.final(next_turn)).source,'cloud')
        self.assertEqual(self.cloud.complete_once.await_count,2)

    async def test_latest_protected_closing_and_unfinished_rules_run_before_budget(self):
        first=message();await self.preview(first)
        for tail, action in [('请帮我看看这件事。','reply'),('晚安','context_only'),('然后','defer')]:
            full=merge_messages([first,message(tail,tail)])
            await self.preview(full)
            result=await self.final(full)
            self.assertEqual((result.action,result.source),(action,'rule'))
        ack=message('好的','separate-ack')
        self.assertEqual((await self.final(ack)).action,'context_only')
        self.cloud.complete_once.assert_awaited_once()

    async def test_cancel_during_debounce_does_not_consume_paid_quota(self):
        self.prep.debounce=.05
        first=message();self.prep.accept(first)
        old=self.prep.entries[self.prep.key(first)].task
        await asyncio.sleep(.01)
        full=merge_messages([first,message('街灯也亮起来了。','second')])
        await self.preview(full);await asyncio.gather(old,return_exceptions=True)
        self.assertEqual((await self.final(full)).source,'cloud')
        self.cloud.complete_once.assert_awaited_once()
        prompt=self.cloud.complete_once.call_args.args[0]
        self.assertIn('窗外的雨',prompt);self.assertIn('街灯也亮起来了',prompt)

    async def test_invalidation_rejects_result_but_retains_burst_paid_budget(self):
        first=message();await self.preview(first)
        self.prep.invalidate()
        self.orch.ctx.system_prompt='新的人格，不复用旧结果'
        result=await self.final(first)
        self.assertEqual((result.action,result.source),('reply','conservative'))
        self.assertEqual(self.decider.last_status['reason'],'burst_budget_exhausted')
        self.cloud.complete_once.assert_awaited_once()

    async def test_changed_history_does_not_purchase_a_second_judgment(self):
        value=message();await self.preview(value)
        result=await self.final(value,[{'user':'另一段历史'}])
        self.assertEqual(result.source,'conservative')
        self.cloud.complete_once.assert_awaited_once()

    async def test_failure_then_new_fragment_has_no_second_paid_attempt(self):
        self.cloud.complete_once.side_effect=TimeoutError('synthetic provider timeout')
        first=message();await self.preview(first)
        full=merge_messages([first,message('小路上没有行人。','second')])
        await self.preview(full)
        result=await self.final(full)
        self.assertEqual((result.action,result.source),('reply','conservative'))
        self.cloud.complete_once.assert_awaited_once()

    async def test_late_silent_results_after_timeout_never_reopen_handoff(self):
        for answer in ('C','D'):
            with self.subTest(answer=answer):
                self.decider.timeout=.02
                self.prep.debounce=0
                release,finished=asyncio.Event(),asyncio.Event()
                async def ignores_cancel(prompt, **kwargs):
                    try:await release.wait()
                    except asyncio.CancelledError:
                        # Even a provider which actively clears cancellation
                        # cannot publish after the decision has completed.
                        uncancel=getattr(asyncio.current_task(),'uncancel',None)
                        if uncancel:uncancel()
                        await release.wait()
                    finished.set()
                    return answer
                self.cloud.complete_once.side_effect=ignores_cancel
                before=self.cloud.complete_once.await_count
                first=message(mid='late-timeout-'+answer)
                await self.preview(first)
                entry=self.prep.entries[self.prep.key(first)]
                self.assertTrue(entry.cloud_publish_closed)
                self.assertIsNone(entry.cloud_result)
                release.set();await finished.wait();await asyncio.sleep(0)
                self.assertIsNone(entry.cloud_result)
                result=await self.final(first)
                self.assertEqual((result.action,result.source),('reply','conservative'))
                fresh=merge_messages([first,message('请帮我看看。','protected-'+answer)])
                protected=await self.final(fresh)
                self.assertEqual((protected.action,protected.source),('reply','rule'))
                self.assertEqual(self.cloud.complete_once.await_count,before+1)

    async def test_late_silent_results_after_explicit_cancel_never_reopen_handoff(self):
        for answer in ('C','D'):
            with self.subTest(answer=answer):
                self.prep.debounce=0
                started,release,finished=asyncio.Event(),asyncio.Event(),asyncio.Event()
                async def ignores_cancel(prompt, **kwargs):
                    started.set()
                    try:await release.wait()
                    except asyncio.CancelledError:
                        uncancel=getattr(asyncio.current_task(),'uncancel',None)
                        if uncancel:uncancel()
                        await release.wait()
                    finished.set()
                    return answer
                self.cloud.complete_once.side_effect=ignores_cancel
                before=self.cloud.complete_once.await_count
                first=message(mid='late-cancel-'+answer)
                self.prep.accept(first)
                entry=self.prep.entries[self.prep.key(first)]
                await started.wait()
                entry.task.cancel();await asyncio.gather(entry.task,return_exceptions=True)
                self.assertTrue(entry.cloud_publish_closed)
                release.set();await finished.wait();await asyncio.sleep(0)
                self.assertIsNone(entry.cloud_result)
                result=await self.final(first)
                self.assertEqual((result.action,result.source),('reply','conservative'))
                fresh=merge_messages([first,message('街灯也亮起来了。','latest-'+answer)])
                result=await self.final(fresh)
                self.assertEqual((result.action,result.source),('reply','conservative'))
                self.assertEqual(self.decider.last_status['reason'],'burst_budget_exhausted')
                self.assertEqual(self.cloud.complete_once.await_count,before+1)

    async def test_revisit_uses_independent_single_call(self):
        value=message();await self.preview(value)
        with self.prep.bind(value): result=await self.decider.decide(value,revisit=True)
        self.assertEqual(result.source,'cloud')
        self.assertEqual(self.cloud.complete_once.await_count,2)

    async def test_paid_map_bound_does_not_evict_live_quota_and_expires(self):
        now=[0.];self.prep.clock=lambda:now[0];self.prep.ttl=1.;self.prep.burst_limit=2
        first=message(mid='first');second=message(mid='second');third=message(mid='third')
        await self.preview(first);await self.preview(second);await self.preview(third)
        self.assertEqual(len(self.prep.burst_spend),2)
        self.assertEqual(self.cloud.complete_once.await_count,2)
        self.assertEqual((await self.final(first)).source,'conservative')
        now[0]=2.;await self.preview(third)
        self.assertEqual(self.cloud.complete_once.await_count,3)
        self.assertEqual(len(self.prep.burst_spend),1)


def _historical_module(filename):
    root=Path(__file__).resolve().parents[1]
    run=subprocess.run(['git','show','v2.1.7:'+filename],cwd=root,capture_output=True,text=True)
    if run.returncode: raise unittest.SkipTest('v2.1.7 source requires a Git checkout with its preserved tag')
    name='src.core._cost_review_v217_'+Path(filename).stem
    spec=importlib.util.spec_from_loader(name,loader=None)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    exec(compile(run.stdout,'v2.1.7:'+filename,'exec'),module.__dict__)
    return module


def response(text='正常完整回答。',tools=None):
    return NS(choices=[NS(message=NS(content=text,tool_calls=tools,reasoning_content=None))],
              usage=NS(prompt_tokens=100,completion_tokens=10))


def tool():
    value={'id':'synthetic-call','type':'function','function':{'name':'web_search','arguments':'{"query":"公开新闻"}'}}
    return NS(id=value['id'],function=NS(**value['function']),model_dump=lambda **_:copy.deepcopy(value))


class Historical217Costs(unittest.IsolatedAsyncioTestCase):
    async def test_v217_does_not_cloud_judge_protected_or_ack_messages(self):
        old=_historical_module('src/core/reply_decision.py')
        cloud=NS(complete=AsyncMock(return_value='{"action":"reply"}'));decider=old.ReplyDecider(cloud)
        await decider.decide(message('请帮我想想。'))
        await decider.decide(message('好的','ack'))
        cloud.complete.assert_not_awaited()
        await decider.decide(message())
        cloud.complete.assert_awaited_once()

    async def test_v217_time_changes_before_real_history_break_contiguous_prefix(self):
        old=_historical_module('src/core/context.py');store=MemoryStore();ctx=old.ContextManager(store)
        value=message();await store.push(value.ctx_key,{'user':'真实旧历史','assistant':'旧回答','turn_id':'old'})
        with patch.object(old,'clock_context',side_effect=['CLOCK-1','CLOCK-2']):
            first=await ctx.build(value);second=await ctx.build(message(mid='second'))
        history_index=next(i for i,item in enumerate(first) if item.get('content')=='真实旧历史')
        common=0
        for a,b in zip(first,second):
            if a!=b:break
            common+=1
        self.assertLess(common,history_index)
        await ctx.close()

    async def test_v217_valid_search_still_adds_fixed_review_but_v3_does_not(self):
        old=_historical_module('src/core/llm.py')
        substantive='今天公布了新安排，正文说明了实施时间、适用条件以及后续影响。'
        result={'ok':True,'summary':'正文有明确安排和实施日期。',
                'sources':[{'url':'https://example.com/article','title':'公开安排','snippet':'有明确日期的正文事实。'}]}
        for module,count in ((old,3),(NS(LLMGateway=LLMGateway,ProviderCfg=ProviderCfg),2)):
            search=NS(search=AsyncMock(return_value=result))
            gateway=module.LLMGateway(module.ProviderCfg('primary','https://api.deepseek.com','fixture','deepseek-flash'),web_search=search)
            create=AsyncMock(side_effect=[response('',[tool()]),response(substantive),response(substantive)])
            gateway._client=lambda _:NS(chat=NS(completions=NS(create=create)))
            await gateway.chat([{'role':'user','content':'查询公开安排。'}],chat_id='fixture')
            self.assertEqual(create.await_count,count);search.search.assert_awaited_once()

    async def test_v217_never_sets_explicit_non_thinking_on_internal_completion(self):
        old=_historical_module('src/core/llm.py')
        gateway=old.LLMGateway(old.ProviderCfg('judge','https://api.deepseek.com','fixture','deepseek-flash'))
        create=AsyncMock(return_value=response('OK'))
        gateway._client=lambda _:NS(chat=NS(completions=NS(create=create)))
        await gateway.complete('纯合成内部任务',max_tokens=80)
        self.assertNotIn('extra_body',create.call_args.kwargs)
        self.assertNotIn('thinking',create.call_args.kwargs)

    async def test_v217_memory_context_is_bounded_not_whole_library(self):
        old=_historical_module('src/core/memory.py')
        memory=old.PersonaMemory.__new__(old.PersonaMemory)
        memory.recall=lambda *args,**kwargs:[NS(category='fact',confidence=.9,valid_from=1,created_at=1,
            status='active',pinned=True,expires_at=0,content='合成事实'+str(i)+'字'*140) for i in range(50)]
        rendered=memory.build_context('fixture','fixture','合成查询')
        self.assertEqual(old.MAX_INJECT,3)
        self.assertLess(len(rendered),600)
        self.assertLessEqual(rendered.count('- ['),3)


if __name__=='__main__':unittest.main()
