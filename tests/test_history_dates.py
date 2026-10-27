"""Absolute-time provenance for dialogue history; deterministic local fixtures only."""
import copy
import json
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from src.core import reality
from src.core.context import ContextManager, LLMCompactor
from src.core.types import WxMessage
from src.store_memory import MemoryStore


def stamp(text, zone='Asia/Shanghai'):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp()


OLD = stamp('2026-10-23T23:59:30')
AFTER_MIDNIGHT = stamp('2026-10-24T00:00:30')
NOW = stamp('2026-10-27T15:00:00')


def message(mid='first', text='昨天我完成了那件事。', at=OLD, owner='person-a'):
    return WxMessage(mid, 'onebot', 'private', 'p_' + owner, owner,
                     '用户', 'bot', text, timestamp=int(at * 1000) if at is not None else 0)


def text_of(messages):
    return json.dumps(messages, ensure_ascii=False)


def assert_unknown(case, text):
    case.assertRegex(text, r'未知|不详|未记录|无法确定|unknown|不确定')


class TimeProvenanceTests(unittest.TestCase):
    def test_timestamp_validation_rejects_nonfinite_booleans_and_out_of_range(self):
        for value in [None, True, False, 0, -1, float('nan'), float('inf'),
                      float('-inf'), 1e100, 10**1000, 'not-a-date', {}, []]:
            with self.subTest(value=repr(value)):
                self.assertIsNone(reality.valid_timestamp(value))
        self.assertEqual(reality.valid_timestamp(OLD), OLD)
        self.assertEqual(reality.valid_timestamp(1.25), 1.25)

    def test_transport_timestamp_stays_distinct_from_days_later_receipt(self):
        msg = message()
        info = reality.message_timing(msg, NOW)
        self.assertEqual(info['user_at'], OLD)
        self.assertEqual(info['user_end_at'], OLD)
        self.assertEqual(info['received_at'], NOW)
        self.assertEqual(info['user_time_source'], 'message')
        self.assertEqual([entry['at'] for entry in info['source_times']], [OLD])
        self.assertTrue(all(isinstance(entry['source_id'], str) and entry['source_id'] for entry in info['source_times']))

    def test_missing_transport_timestamp_uses_receipt_without_claiming_send_time(self):
        info = reality.message_timing(message(at=None), NOW)
        self.assertEqual(info['user_at'], NOW)
        self.assertEqual(info['received_at'], NOW)
        self.assertEqual(info['user_time_source'], 'received')
        self.assertNotIn('昨天=',reality.turn_time_context({**info,'user':'昨天的事'}))

    def test_midnight_batch_preserves_every_source_time_and_range(self):
        first, second = message('one'), message('two', '今天继续说。', AFTER_MIDNIGHT)
        merged = copy.deepcopy(first)
        from dataclasses import asdict
        merged.batch_messages = [asdict(first), asdict(second)]
        merged.content = first.content + '\n' + second.content
        info = reality.message_timing(merged, NOW)
        self.assertEqual(info['user_at'], OLD)
        self.assertEqual(info['user_end_at'], AFTER_MIDNIGHT)
        self.assertEqual(info['user_time_source'], 'message')
        self.assertEqual([entry['at'] for entry in info['source_times']], [OLD, AFTER_MIDNIGHT])
        self.assertEqual(len({entry['source_id'] for entry in info['source_times']}), 2)
        context = reality.turn_time_context({**info, 'user': merged.content}, 'Asia/Shanghai')
        self.assertIn('2026-10-23', context)
        self.assertIn('2026-10-24', context)
        self.assertIn('+08:00', context)

    def test_relative_words_in_midnight_batch_belong_only_to_their_own_message(self):
        from dataclasses import asdict
        first=message('one','明天去看电影。',OLD)
        second=message('two','昨天朋友结婚。',AFTER_MIDNIGHT)
        first.batch_messages=[asdict(first),asdict(second)]
        first.content+='\n'+second.content
        info=reality.message_timing(first,NOW)
        text=reality.turn_time_context({**info,'user':first.content})
        self.assertIn('第1条=2026-10-23',text)
        self.assertIn('第2条=2026-10-24',text)
        self.assertIn('引述或另设日期优先',text)
        self.assertNotIn('昨天=2026-10-22',text)
        self.assertNotIn('明天=2026-10-25',text)
        legacy=copy.deepcopy(info)
        for row in legacy['source_times']:row.pop('relative_terms',None)
        old=reality.turn_time_context({**legacy,'user':first.content})
        self.assertIn('第1条=2026-10-23',old)
        self.assertNotIn('昨天=',old)
        self.assertNotIn('明天=',old)

    def test_relative_words_inside_a_quote_are_not_hard_mapped_to_send_date(self):
        msg=message(text='我翻到10月20日他说“明天见”的消息。')
        info=reality.message_timing(msg,NOW)
        context=reality.turn_time_context({**info,'user':msg.content})
        self.assertIn('2026-10-23',context)
        self.assertIn('引述或另设日期优先',context)
        self.assertNotIn('明天=2026-10-24',context)
        self.assertNotIn(msg.content,context,'program metadata does not repeat or rewrite source words')

    def test_partial_batch_does_not_relabel_all_sources_as_current(self):
        from dataclasses import asdict
        first, second = message('one'), message('two', at=None)
        first.batch_messages = [asdict(first), asdict(second)]
        info = reality.message_timing(first, NOW)
        self.assertEqual(info['user_time_source'], 'partial')
        self.assertIn(OLD, [entry['at'] for entry in info['source_times']])
        self.assertEqual(info['received_at'], NOW)

    def test_time_context_is_absolute_and_timezone_aware(self):
        turn = {'user': '昨天的事', 'assistant': '好的。', 'user_at': OLD, 'assistant_at': AFTER_MIDNIGHT,
                'user_time_source': 'message'}
        before = copy.deepcopy(turn)
        shanghai = reality.turn_time_context(turn, 'Asia/Shanghai')
        utc = reality.turn_time_context(turn, 'UTC')
        self.assertIn('2026-10-23', shanghai)
        self.assertIn('2026-10-24', shanghai)
        self.assertIn('引述或另设日期优先',shanghai)
        self.assertIn('+08:00', shanghai)
        self.assertIn('2026-10-23', utc)
        self.assertTrue('+00:00' in utc or 'UTC' in utc)
        self.assertNotRegex(shanghai, r'\d+\s*天前|几天前|刚刚')
        self.assertEqual(turn, before, 'rendering time references is read-only')

    def test_undated_legacy_turn_is_unknown_not_the_epoch_or_today(self):
        for turn in [{'user': '昨天见到的'}, {'user': '昨天见到的', 'ts': float('nan')},
                     {'user': '昨天见到的', 'ts': True}]:
            with self.subTest(turn=turn):
                text = reality.turn_time_context(turn, 'Asia/Shanghai')
                assert_unknown(self, text)
                self.assertNotIn('1970-', text)
                self.assertNotIn('2026-10-27', text)

    def test_clock_rule_distinguishes_current_and_historical_relative_dates(self):
        text = reality.clock_context('Asia/Shanghai', NOW)
        self.assertIn('2026-10-27', text)
        self.assertIn('2026-10-26', text)
        self.assertRegex(text, r'历史|旧消息|过去消息')
        self.assertRegex(text, r'发送|原话|各自')


class HistoryDateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'context-onebot.json'
        self.store = MemoryStore(path=self.path)
        self.context = self.make_context(self.store)
        self.addAsyncCleanup(self.context.close)
        self.addCleanup(self.tmp.cleanup)

    def make_context(self, store, zone='Asia/Shanghai'):
        context = ContextManager(store, context_timezone=zone)
        context.configure_session(context_window_tokens=1_000_000,
                                  provider_fingerprint='history-date-fixture')
        return context

    async def test_historical_turn_dates_reach_model_without_changing_original_words(self):
        old = message()
        with patch('src.core.context.time.time', return_value=NOW):
            await self.context.accept_user(old)
            await self.context.append(old, '听到了，先歇一会。')
            result = await self.context.build(message('new', '今天接着聊吧。', NOW))
        self.assertIn({'role': 'user', 'content': old.render()}, result)
        self.assertIn({'role': 'assistant', 'content': '听到了，先歇一会。'}, result)
        self.assertIn('2026-10-23', text_of(result))
        self.assertIn('2026-10-27', text_of(result))
        metadata = [item for item in result if item['role'] == 'system']
        self.assertIn('2026-10-23', text_of(metadata))
        self.assertEqual(result[-1]['content'], '今天接着聊吧。')

    async def test_malformed_legacy_timestamps_do_not_crash_sorting_or_rendering(self):
        current = message('current', '继续说吧。', NOW)
        for index, invalid in enumerate([float('nan'), True, 'not-a-date', 1e100, None]):
            await self.store.push(current.ctx_key, {'user': f'无可靠日期的原话{index}',
                'assistant': '', 'ts': invalid, 'turn_id': f'invalid-{index}',
                'source_ids': [f'invalid-{index}']})
        result = await self.context.build(current)
        for index in range(5):
            self.assertIn({'role': 'user', 'content': f'无可靠日期的原话{index}'}, result)
        assert_unknown(self, text_of([item for item in result if item['role'] == 'system']))
        self.assertEqual(result[-1]['content'], current.render())

    async def test_multimodal_current_input_keeps_full_text_pixels_and_date_metadata(self):
        current = message('image', '昨天这张图里有什么？', OLD)
        current.images = [{'type': 'fixture'}]
        current.content_parts = [{'type': 'text', 'text': current.content}, {'type': 'image', 'index': 0}]
        pixel = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,ZmFrZS1waXhlbHM='}}
        result = await self.context.build(current, image_inputs=[pixel])
        self.assertEqual(result[-1]['role'], 'user')
        self.assertIsInstance(result[-1]['content'], list)
        self.assertIn(pixel, result[-1]['content'])
        self.assertIn({'type': 'text', 'text': current.content}, result[-1]['content'])
        self.assertIn('2026-10-23', text_of([item for item in result if item['role'] == 'system']))

    async def test_received_and_sent_times_survive_append_retry_and_reload(self):
        old = message()
        with patch('src.core.context.time.time', return_value=NOW):
            await self.context.accept_user(old)
        accepted = copy.deepcopy((await self.store.get_recent(old.ctx_key))[0])
        self.assertEqual(accepted['user_at'], OLD)
        self.assertEqual(accepted['received_at'], NOW)
        with patch('src.core.context.time.time', return_value=NOW + 9):
            await self.context.append(old, '我在听。')
        delivered = copy.deepcopy((await self.store.get_recent(old.ctx_key))[0])
        self.assertEqual(delivered['user_at'], OLD)
        self.assertEqual(delivered['received_at'], NOW)
        self.assertEqual(delivered['assistant_at'], NOW + 9)
        with patch('src.core.context.time.time', return_value=NOW + 86400):
            await self.context.accept_user(old)
            await self.context.append(old, '我在听。')
        reloaded = MemoryStore(path=self.path)
        restored = (await reloaded.get_recent(old.ctx_key))[0]
        self.assertEqual(restored['user_at'], OLD)
        self.assertEqual(restored['received_at'], NOW)
        self.assertEqual(restored['assistant_at'], NOW + 9)
        self.assertEqual(len(await reloaded.get_recent(old.ctx_key)), 1)

    async def test_legacy_retry_does_not_refresh_old_record_or_invent_receipt_and_delivery(self):
        old=message(at=None)
        await self.store.push(old.ctx_key,{'user':old.content,'assistant':'旧回复。',
            'ts':OLD,'turn_id':self.context.turn_id(old),'source_ids':self.context.source_ids(old)})
        with patch('src.core.context.time.time',return_value=NOW):
            await self.context.accept_user(old)
            await self.context.append(old,'旧回复。')
        restored=(await MemoryStore(path=self.path).get_recent(old.ctx_key))[0]
        self.assertEqual(restored['ts'],OLD)
        self.assertIsNone(restored.get('user_at'))
        self.assertIsNone(restored.get('received_at'))
        self.assertIsNone(restored.get('assistant_at'))
        text=reality.turn_time_context(restored)
        self.assertIn('2026-10-23',text)
        self.assertNotIn('2026-10-27',text)
        assert_unknown(self,text)

    async def test_genuine_source_can_enrich_legacy_time_without_inventing_first_receipt(self):
        old=message()
        await self.store.push(old.ctx_key,{'user':old.content,'assistant':'旧回复。',
            'ts':OLD+10,'turn_id':self.context.turn_id(old),'source_ids':self.context.source_ids(old)})
        with patch('src.core.context.time.time',return_value=NOW):
            await self.context.accept_user(old)
            await self.context.append(old,'旧回复。')
        restored=(await self.store.get_recent(old.ctx_key))[0]
        self.assertEqual(restored['ts'],OLD+10)
        self.assertEqual(restored['user_at'],OLD)
        self.assertEqual(restored['user_time_source'],'message')
        self.assertIsNone(restored.get('received_at'))
        self.assertIsNone(restored.get('assistant_at'))

    async def test_undated_legacy_retry_stays_undated(self):
        old=message(at=None)
        await self.store.push(old.ctx_key,{'user':old.content,'assistant':'旧回复。',
            'turn_id':self.context.turn_id(old),'source_ids':self.context.source_ids(old)})
        with patch('src.core.context.time.time',return_value=NOW):
            await self.context.accept_user(old)
            await self.context.append(old,'旧回复。')
        restored=(await self.store.get_recent(old.ctx_key))[0]
        self.assertFalse(restored.get('ts'))
        self.assertIsNone(restored.get('user_at'))
        self.assertIsNone(restored.get('received_at'))
        self.assertIsNone(restored.get('assistant_at'))
        self.assertNotIn('2026-',reality.turn_time_context(restored))

    async def test_missing_send_time_receipt_anchor_is_fixed_on_first_acceptance(self):
        old = message(at=None)
        with patch('src.core.context.time.time', return_value=NOW):
            await self.context.accept_user(old)
        with patch('src.core.context.time.time', return_value=NOW + 86400):
            await self.context.accept_user(old)
            await self.context.append(old, '好。')
        turn = (await self.store.get_recent(old.ctx_key))[0]
        self.assertEqual(turn['user_at'], NOW)
        self.assertEqual(turn['received_at'], NOW)
        self.assertEqual(turn['user_time_source'], 'received')

    async def test_delivered_prefix_is_byte_identical_across_days_and_restart(self):
        first = message()
        with patch('src.core.context.time.time', return_value=OLD):
            await self.context.accept_user(first)
            request = await self.context.build(first)
            await self.context.append(first, '听到了。')
        session = await self.store.get_session(first.ctx_key)
        old_prefix = copy.deepcopy(session['messages'])
        self.assertEqual(old_prefix[:len(request)], request)
        restored = self.make_context(MemoryStore(path=self.path))
        self.addAsyncCleanup(restored.close)
        with patch('src.core.context.time.time', return_value=NOW):
            result = await restored.build(message('second', '今天呢？', NOW))
        self.assertEqual(result[:len(old_prefix)], old_prefix)
        self.assertEqual(json.dumps(result[:len(old_prefix)], ensure_ascii=False),
                         json.dumps(old_prefix, ensure_ascii=False))
        self.assertIn('2026-10-23', text_of(old_prefix))
        self.assertIn('2026-10-27', text_of(result[len(old_prefix):]))

    async def test_provider_assistant_and_tool_protocol_are_not_rewritten_by_dates(self):
        current = message()
        request = await self.context.build(current)
        tool_call = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call-one', 'type': 'function', 'function': {'name': 'search', 'arguments': '{}'}}]}
        tool_result = {'role': 'tool', 'tool_call_id': 'call-one', 'content': '检索正文'}
        actual = copy.deepcopy(request) + [tool_call, tool_result]
        original = {'role': 'assistant', 'content': '完整回复。', 'reasoning_content': '协议内部推理'}
        self.context.capture_request(current, actual, assistant=original)
        await self.context.append(current, '完整回复。')
        saved = (await self.store.get_session(current.ctx_key))['messages']
        self.assertEqual(saved[:len(actual)], actual)
        assistant = [item for item in saved[len(actual):] if item['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]['content'], original['content'])
        self.assertEqual(assistant[0]['reasoning_content'], original['reasoning_content'])
        self.assertEqual(saved[-1]['role'], 'system', 'delivery date appends after the untouched provider reply')

    async def test_legacy_undated_summary_and_history_do_not_gain_upgrade_date(self):
        msg = message()
        await self.store.push(msg.ctx_key, {'user': '昨天去了图书馆。', 'assistant': '好。',
                                          'turn_id': 'legacy', 'source_ids': ['legacy']})
        await self.store.set_summary(msg.ctx_key, '昨天有一个约定。')
        result = await self.context.build(message('now', '上次聊了什么？', NOW))
        self.assertIn('昨天去了图书馆。', text_of(result))
        summary = next(item['content'] for item in result if '<reference_data kind="summary">' in str(item.get('content')))
        assert_unknown(self, summary)
        system = text_of([item for item in result if item['role'] == 'system'])
        assert_unknown(self, system)

    async def test_timezone_change_rebuilds_once_then_reuses_new_prefix(self):
        first = message()
        await self.context.accept_user(first)
        await self.context.build(first)
        await self.context.append(first, '继续。')
        original = await self.store.get_session(first.ctx_key)
        other = self.make_context(self.store, 'America/New_York')
        self.addAsyncCleanup(other.close)
        next_msg = message('two', '继续说吧。', NOW)
        await other.build(next_msg)
        await other.append(next_msg, '好的。')
        changed = await self.store.get_session(first.ctx_key)
        self.assertNotEqual(changed['fingerprint'], original['fingerprint'])
        self.assertIn('-04:00', text_of(changed['messages']))
        following = await other.build(message('three', '还有吗？', NOW + 1))
        self.assertEqual(following[:len(changed['messages'])], changed['messages'])

    async def test_legacy_session_rebuild_does_not_cross_account_boundaries(self):
        mine = message()
        await self.store.push(mine.ctx_key, {'user': mine.content, 'assistant': '原来的回复',
                                            'ts': OLD, 'turn_id': 'old-turn', 'source_ids': ['old-turn']})
        await self.store.set_session(mine.ctx_key, {'fingerprint': 'legacy-without-time-protocol',
            'messages': [{'role': 'system', 'content': self.context.system_prompt},
                         {'role': 'user', 'content': 'STALE PREFIX WITHOUT DATE'}],
            'covered_source_ids': ['old-turn']}, expected_revision=self.store.revision)
        peer = message('peer', '其他账号的私密原话', NOW, owner='person-b')
        await self.context.build(peer)
        await self.context.append(peer, '其他账号的回答')
        peer_session = copy.deepcopy(await self.store.get_session(peer.ctx_key))
        current = message('new', '继续旧话题', NOW)
        built = await self.context.build(current)
        self.assertNotIn('STALE PREFIX WITHOUT DATE', text_of(built))
        self.assertNotIn('其他账号的私密原话', text_of(built))
        self.assertIn('2026-10-23', text_of(built))
        await self.context.append(current, '新回复')
        rebuilt = await self.store.get_session(mine.ctx_key)
        later = await self.context.build(message('later', '继续', NOW + 1))
        self.assertEqual(later[:len(rebuilt['messages'])], rebuilt['messages'])
        self.assertEqual(await self.store.get_session(peer.ctx_key), peer_session)

    async def test_runtime_timezone_update_reaches_existing_memory_and_new_compactor(self):
        from src.runtime import BotRuntime
        from src.config_store import AppConfig,ConfigStore
        cfg=AppConfig();cfg.adapter.wechat_enabled=False;cfg.adapter.qq_enabled=False
        cfg.llm.search_enabled=False
        root=Path(self.tmp.name)/'runtime'
        store=ConfigStore(root/'config.json');store.save(cfg)
        runtime=BotRuntime(store,data_dir=root)
        self.addAsyncCleanup(runtime.stop)
        memory=runtime._ensure_memory()
        self.assertEqual(memory.context_timezone,'Asia/Shanghai')
        changed=copy.deepcopy(cfg);changed.llm.context_timezone='America/New_York'
        runtime.apply_config(changed)
        self.assertIs(runtime.memory,memory)
        self.assertEqual(memory.context_timezone,'America/New_York')
        compactor=LLMCompactor(SimpleNamespace())
        context=self.make_context(self.store,'America/New_York');context.compactor=compactor
        self.addAsyncCleanup(context.close)
        BotRuntime._configure_context_session(context,changed)
        self.assertEqual(compactor.context_timezone,'America/New_York')

    async def test_compactor_raw_turn_prompt_contains_dates_and_relative_time_rule(self):
        gateway = SimpleNamespace(complete=AsyncMock(return_value='2026-10-23 用户提及前一天的事情。'))
        compactor = LLMCompactor(gateway)
        await compactor.summarize([{'user': '昨天完成了任务。', 'assistant': '好。',
                                   'user_at': OLD, 'assistant_at': AFTER_MIDNIGHT,
                                   'user_time_source': 'message', 'ts': OLD}])
        prompt = gateway.complete.call_args.args[0]
        self.assertIn('2026-10-23', prompt)
        self.assertIn('2026-10-24', prompt)
        self.assertRegex(prompt, r'相对|原话|各自')
        self.assertRegex(prompt, r'发送|日期|时间')


if __name__ == '__main__':
    unittest.main()
