"""Adversarial budgets/deadlines and desktop identity persistence, offline only."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from src.config_store import AppConfig
from src.core.context import ContextManager, SEARCH_BUDGET, WINDOW_BUDGET, approx_tokens
from src.core.conversation import merge_messages
from src.core.llm import LLMGateway, ProviderCfg
from src.core.proactive import ProactivePlanner
from src.core.reply_decision import ReplyDecider
from tools.decision_model.research_decider import ResearchReplyDecider
from src.core.types import WxMessage
from src.core.web_search import SEARCH_RULES
from src.store_memory import MemoryStore


def msg(text='今天路上看到一片云', mid='now', platform='desktop', owner='local'):
    return WxMessage(mid, platform, 'private', 'p_'+owner if platform=='onebot' else owner,
                     owner, '测试用户', 'bot', text)


def call(cid):
    data={'id':cid,'type':'function','function':{'name':'web_search','arguments':'{"query":"公开新闻"}'}}
    return NS(id=cid,function=NS(**data['function']),model_dump=lambda **_:dict(data))


def response(calls=None):
    return NS(choices=[NS(message=NS(content='已经根据正文合并了关键信息。',tool_calls=calls,reasoning_content=None))])


class SearchBudgets(unittest.IsolatedAsyncioTestCase):
    def large(self):
        return {'ok':True,'raw':'不应注入'*10000,'other':'隐藏字段'*10000,'summary':'正文依据，包含清楚的事实。'*2000,
                'sources':[{'url':'https://example.com/'+str(i)+'/'+'a'*1200,'title':'独立来源'+str(i),
                            'snippet':'证据片段。'*1200} for i in range(5)]}

    def test_zero_and_impossibly_small_budgets_abstain_instead_of_minimum_32(self):
        for budget in (0,1,8):
            self.assertIsNone(LLMGateway._fit_search_result(self.large(),budget))

    def test_fit_includes_json_escaping_and_tool_envelope(self):
        result=self.large();result['summary']='"\\\n真实证据"'*3000
        fitted=LLMGateway._fit_search_result(result,500,'tool-1')
        self.assertIsNotNone(fitted)
        self.assertLessEqual(LLMGateway._search_result_cost(fitted,'tool-1'),500)
        self.assertNotIn('raw',fitted);self.assertNotIn('other',fitted)
        self.assertEqual(result['summary'],'"\\\n真实证据"'*3000)

    def test_error_category_is_preserved(self):
        result=LLMGateway._fit_search_result({'ok':False,'error':'search_network_error','sources':[]},100)
        self.assertEqual(result['error'],'search_network_error')

    async def test_two_results_share_budget_with_rules_and_never_restore_discarded_sources(self):
        search=NS(search=AsyncMock(return_value=self.large()))
        gateway=LLMGateway(ProviderCfg('test','https://example.com','fixture','fixture'),web_search=search)
        gateway._response=AsyncMock(side_effect=[response([call('first')]),response([call('second')]),response()])
        gateway._review_search_answer=AsyncMock(return_value='一条总结')
        self.assertEqual(await gateway._web_answer(gateway.primary,[],''),'一条总结')
        work=gateway._review_search_answer.call_args.args[1]
        tools=[m for m in work if m['role']=='tool']
        cost=approx_tokens(json.dumps({'role':'system','content':SEARCH_RULES},ensure_ascii=False))
        cost+=sum(approx_tokens(json.dumps(m,ensure_ascii=False)) for m in tools)
        self.assertEqual(len(tools),2);self.assertLessEqual(cost,SEARCH_BUDGET)
        self.assertLessEqual(cost+approx_tokens(json.dumps(gateway._search_review_message(),ensure_ascii=False)),SEARCH_BUDGET)
        self.assertEqual(gateway.last_search_budget['used'],cost)
        injected={s['url'] for m in tools for s in json.loads(m['content']).get('sources',[])}
        reviewed={s['url'] for s in gateway._review_search_answer.call_args.args[3]}
        self.assertEqual(injected,reviewed)
        self.assertEqual(gateway._response.call_args.kwargs['tool_choice'],'none')

    async def test_parallel_tool_declarations_cannot_create_unbounded_protocol_overhead(self):
        gateway=LLMGateway(ProviderCfg('test','https://example.com','fixture','fixture'),
                           web_search=NS(search=AsyncMock(return_value=self.large())))
        gateway._response=AsyncMock(side_effect=[response([call(str(i)) for i in range(100)]),response()])
        gateway._review_search_answer=AsyncMock(return_value='总结')
        await gateway._web_answer(gateway.primary,[],'')
        work=gateway._review_search_answer.call_args.args[1]
        self.assertEqual(len(next(m for m in work if m.get('tool_calls'))['tool_calls']),2)
        self.assertEqual(len([m for m in work if m['role']=='tool']),2)
        self.assertEqual(gateway.web_search.search.await_count,2)


class HardDecisionDeadlines(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_cancellation_does_not_hold_local_reply_slot(self):
        cleaned=asyncio.Event()
        async def slow(*a,**kw):
            try:await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(.10)
                cleaned.set()
                return {'action':'context_only','calibrated':True}
        started=time.monotonic()
        result=await ResearchReplyDecider(local_runner=NS(decide=slow),local_timeout=.01).decide(msg())
        self.assertEqual(result.action,'reply');self.assertLess(time.monotonic()-started,.08)
        await asyncio.wait_for(cleaned.wait(),.4)

    async def test_local_and_cloud_share_one_deadline_and_one_cloud_attempt(self):
        async def local(*a,**kw):
            await asyncio.sleep(.02)
            return {'action':None,'abstain':True,'calibrated':True,'reason':'low_confidence'}
        async def cloud(*a,**kw):await asyncio.sleep(10)
        gateway=NS(complete_once=AsyncMock(side_effect=cloud),complete=AsyncMock())
        started=time.monotonic()
        result=await ResearchReplyDecider(gateway,local_runner=NS(decide=local),timeout=.05).decide(msg())
        self.assertEqual(result.action,'reply');self.assertLess(time.monotonic()-started,.11)
        gateway.complete_once.assert_awaited_once();gateway.complete.assert_not_awaited()

    async def test_delayed_cloud_cancellation_does_not_extend_total_deadline(self):
        cleaned=asyncio.Event()
        async def cloud(*a,**kw):
            try:await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(.10);cleaned.set();return '{"action":"context_only"}'
        started=time.monotonic()
        result=await ReplyDecider(NS(complete_once=cloud),timeout=.01).decide(msg())
        self.assertEqual(result.action,'reply');self.assertLess(time.monotonic()-started,.08)
        await asyncio.wait_for(cleaned.wait(),.4)

    async def test_traditional_mixed_and_unpunctuated_requests_bypass_models(self):
        local=NS(decide=AsyncMock());cloud=NS(complete_once=AsyncMock())
        decider=ResearchReplyDecider(cloud,local_runner=local)
        for text in ('你觉得这样安排', '给我写一个短故事', '我撐不下去了', '怎樣才能做好',
                     '給我整理一下', 'please 帮我看看', 'can you explain this'):
            with self.subTest(text=text):self.assertEqual((await decider.decide(msg(text))).action,'reply')
        local.decide.assert_not_awaited();cloud.complete_once.assert_not_awaited()

    async def test_protected_earlier_request_survives_natural_closing(self):
        burst=merge_messages([msg('給我整理一下','1'),msg('晚安','2')])
        self.assertEqual((await ReplyDecider().decide(burst)).action,'reply')

    async def test_video_in_later_batch_member_is_not_misclassified_as_plain_text(self):
        video=msg('[视频]','2');video.msg_type='video'
        batch=merge_messages([msg('刚才拍到的','1'),video])
        local=NS(decide=AsyncMock())
        self.assertEqual((await ResearchReplyDecider(local_runner=local).decide(batch)).action,'reply')
        local.decide.assert_not_awaited()

    async def test_rule_diagnostic_replaces_stale_model_failure(self):
        decider=ReplyDecider();await decider.decide(msg())
        self.assertEqual(decider.last_status['source'],'conservative')
        await decider.decide(msg('帮我解释一下'))
        self.assertEqual(decider.last_status['source'],'rule')

    async def test_generalized_correction_distress_and_comparison_protection(self):
        local=NS(decide=AsyncMock());cloud=NS(complete_once=AsyncMock())
        decider=ResearchReplyDecider(cloud,local_runner=local)
        split=merge_messages([msg('其实不是想辞职','split1'),msg('我是想学着跟他说不','split2')])
        self.assertEqual((await decider.decide(split)).action,'reply')
        cases=('剛剛講錯了，集合地點是北門，不是南門',
               '我刚从医院出来，检查结果还没拿，手一直在抖',
               '其实不是想辞职\n我是想学着跟他说不',
               '两张图都齐了\n现在把它们放一起比一下差别',
               '日期记错了，应该是下星期',
               '並非想斷交\n而是想暫時冷靜',
               '腿有些发软，声音也在抖',
               '把前后两版对照一下有什么不同')
        for text in cases:
            with self.subTest(text=text):
                result=await decider.decide(msg(text))
                self.assertEqual(result.action,'reply');self.assertEqual(result.source,'rule')
        local.decide.assert_not_awaited();cloud.complete_once.assert_not_awaited()

    async def test_new_protection_does_not_override_exact_wait_or_treat_quoted_wait_as_command(self):
        local=NS(decide=AsyncMock());cloud=NS(complete_once=AsyncMock())
        decider=ResearchReplyDecider(cloud,local_runner=local)
        batch=merge_messages([msg('日期记错了，应该是下星期','1'),msg('等我说完','2')])
        self.assertEqual((await decider.decide(batch)).action,'context_only')
        for text in ('她写了“等我说完”，这两个版本比一下差别',
                     '他说“不用回”，不是让我离开\n我是想知道该怎么理解'):
            with self.subTest(text=text):self.assertEqual((await decider.decide(msg(text))).action,'reply')
        local.decide.assert_not_awaited();cloud.complete_once.assert_not_awaited()


class UnifiedHistoryAndDesktop(unittest.IsolatedAsyncioTestCase):
    def planner(self,path):
        return ProactivePlanner(path,lambda p,o:'linked-person',lambda:AppConfig(),
                                lambda *_:None,AsyncMock(),AsyncMock(),clock=lambda:1000)

    async def test_desktop_journal_survives_restart_without_poisoning_other_accounts(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'proactive.json';planner=self.planner(path)
            for platform,owner in (('desktop','local'),('ilink','wx'),('onebot','123')):
                planner.note_in(msg(platform=platform,owner=owner))
            restored=self.planner(path)
            self.assertFalse(restored.load_error);self.assertEqual(len(restored.accounts),3)
            self.assertEqual({r['channel'] for r in restored.timeline(list(restored.accounts.values()))},
                             {'桌面伙伴','微信','QQ'})
            self.assertEqual({a['channel'] for a in restored.status()['contacts'][0]['accounts']},
                             {'桌面伙伴','微信','QQ'})
            await planner.close();await restored.close()

    async def test_single_desktop_prompt_does_not_claim_qq_or_wechat_identity(self):
        with tempfile.TemporaryDirectory() as d:
            planner=self.planner(Path(d)/'proactive.json');planner.note_in(msg())
            text=json.dumps(planner.prompt(list(planner.accounts.values()),1000),ensure_ascii=False)
            self.assertIn('仅通过桌面伙伴',text);self.assertNotIn('QQ',text);self.assertNotIn('微信',text)
            await planner.close()

    async def test_eight_cross_channel_exchanges_are_eight_turns_not_sixteen_events(self):
        with tempfile.TemporaryDirectory() as d:
            planner=self.planner(Path(d)/'proactive.json')
            for i in range(8):
                planner.note_in(msg('用户真实来话'+str(i),str(i),'onebot','123'))
                planner.note_out('onebot','p_123','真实答复'+str(i))
            shared=planner.shared_turns(msg())
            self.assertEqual(len(shared),8)
            self.assertTrue(all(t['user'] and t['assistant'] for t in shared))
            ctx=ContextManager(MemoryStore());ctx.shared_history=planner.shared_turns
            messages=await ctx.build(msg())
            self.assertEqual(sum(m['role']=='assistant' for m in messages),8)
            self.assertLessEqual(ctx.last_budget['history'],WINDOW_BUDGET)
            await planner.close()

    async def test_oversized_search_reference_preserves_whole_wrapper_and_useful_prefix(self):
        ctx=ContextManager(MemoryStore(),persona='  完整人格\n ')
        messages=await ctx.build(msg(),rag_context='资料开始。'+'正文证据'*10000)
        reference=next(m['content'] for m in messages if 'kind="search"' in str(m['content']))
        self.assertIn('资料开始。',reference);self.assertTrue(reference.endswith('</reference_data>'))
        self.assertIn('资料超预算',reference);self.assertLessEqual(approx_tokens(reference),SEARCH_BUDGET)
        self.assertIn('  完整人格\n ',messages[0]['content'])
        self.assertEqual(ctx.last_budget['search'],approx_tokens(reference))


if __name__=='__main__':unittest.main()
