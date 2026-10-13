"""A model response must become one complete message, not streaming bubbles."""
import asyncio
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT))
from src.core import sender as sender_module
from src.core.sender import HumanLikeSender
from src.core.context import ContextManager, build_system_prompt
from src.core.trigger import TriggerRouter
from src.core.types import WxMessage
from src.orchestrator import Orchestrator
from src.store_memory import MemoryStore


class Bucket:
    def __init__(self):self.calls=0
    async def acquire(self):self.calls+=1


class Adapter:
    def __init__(self):self.sent=[]
    def on_message(self,handler):self.handler=handler
    async def send_text(self,chat_id,text,at=None):
        self.sent.append({'chat_id':chat_id,'text':text,'at':at})
        return str(len(self.sent))


def make_sender(adapter):
    sender=HumanLikeSender(adapter)
    sender.global_bucket=Bucket()
    sender.chat_buckets['p_u']=Bucket()
    sender.chat_buckets['g_1']=Bucket()
    sender.human_delay=lambda text:0
    return sender


async def stream_parts(parts):
    for part in parts:
        await asyncio.sleep(0)
        yield part


class ReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter=Adapter();self.sender=make_sender(self.adapter)

    async def test_many_chunks_emit_one_complete_message(self):
        pieces=['第一段内容足够长，超过旧版本二十字阈值也不应该提前发送。\n\n',
                '第二段继续说明情况，还会超过旧版本八十字的后续阈值。'*4,
                '\n最后一句也应该保留。']
        reply=await self.sender.stream_send('p_u',stream_parts(pieces))
        self.assertEqual(len(self.adapter.sent),1)
        self.assertEqual(self.adapter.sent[0]['text'],reply)
        self.assertNotIn('\n',reply)
        self.assertTrue(reply.endswith('最后一句也应该保留。'))
        self.assertIn('第二段继续说明情况',reply)

    async def test_no_emission_before_stream_completes(self):
        async def generating():
            yield '这段内容超过二十字且已经有句号，但生成还没有结束。'
            self.assertEqual(self.adapter.sent,[])
            yield '这是生成结束前的最后一段。'
        await self.sender.stream_send('p_u',generating())
        self.assertEqual(len(self.adapter.sent),1)

    async def test_long_direct_send_is_not_split_or_truncated(self):
        text='长'*1601
        ids=await self.sender.send('p_u',text)
        self.assertEqual(ids,['1'])
        self.assertEqual(self.adapter.sent,[{'chat_id':'p_u','text':text,'at':None}])

    async def test_rate_limit_and_mention_apply_once(self):
        reply=await self.sender.stream_send('g_1',stream_parts(['回复内容比较长，要超过旧版本首段的阈值才算有效。','\n\n完整结尾。']),at=['123'])
        self.assertEqual(self.adapter.sent,[{'chat_id':'g_1','text':reply,'at':['123']}])
        self.assertEqual(self.sender.global_bucket.calls,1)
        self.assertEqual(self.sender.chat_buckets['g_1'].calls,1)

    async def test_partial_generation_failure_emits_no_partial_message(self):
        async def broken():
            yield '这是超过旧版本阈值的一段内容，但是接下来的生成会失败。'
            raise RuntimeError('STREAM_FAILED')
        with self.assertRaisesRegex(RuntimeError,'STREAM_FAILED'):
            await self.sender.stream_send('p_u',broken())
        self.assertEqual(self.adapter.sent,[])

    async def test_empty_stream_raises_before_sending(self):
        with self.assertRaises(ValueError):
            await self.sender.stream_send('p_u',stream_parts(['  ','\n\n']))
        self.assertEqual(self.adapter.sent,[])

    async def test_empty_direct_send_remains_silent(self):
        self.assertEqual(await self.sender.send('p_u',' \n\n '),[])
        self.assertEqual(self.adapter.sent,[])

    async def test_legacy_thresholds_no_longer_split_reply(self):
        await self.sender.stream_send('p_u',stream_parts(['你好。','后半句。']),first_threshold=1,next_threshold=1)
        self.assertEqual(len(self.adapter.sent),1)
        self.assertEqual(self.adapter.sent[0]['text'],'你好。后半句。')

    async def test_code_fence_format_survives_chunk_boundaries(self):
        text='示例：\n```python\ndef f():\n    x = 1  \n\n    return x\n```\n运行后就能得到结果。'
        reply=await self.sender.stream_send('p_u',stream_parts([text[i:i+7] for i in range(0,len(text),7)]))
        self.assertEqual(reply,text)
        self.assertEqual(len(self.adapter.sent),1)
        self.assertEqual(self.adapter.sent[0]['text'],text)

    async def test_history_and_wire_content_match(self):
        class Model:
            async def stream(self,messages,**kwargs):
                async for part in stream_parts(['普通回复第一段。\n\n','普通回复第二段。']):yield part
        store=MemoryStore()
        orch=Orchestrator(self.adapter,store,Model(),None,ContextManager(store),TriggerRouter(),self.sender)
        msg=WxMessage('1','onebot','private','p_u','u','用户','bot','你好')
        await orch.handle(msg)
        recent=await store.get_recent(msg.ctx_key)
        self.assertEqual(len(self.adapter.sent),1)
        self.assertEqual(recent[0]['assistant'],self.adapter.sent[0]['text'])
        self.assertNotIn('\n',recent[0]['assistant'])

    async def test_failed_stream_orchestrator_sends_only_one_error_reply(self):
        class Model:
            async def stream(self,*args,**kwargs):
                yield '这是超过旧版本阈值的一段内容，但是生成接下来会失败。'
                raise RuntimeError('STREAM_FAILED')
        store=MemoryStore()
        orch=Orchestrator(self.adapter,store,Model(),None,ContextManager(store),TriggerRouter(),self.sender)
        msg=WxMessage('1','onebot','private','p_u','u','用户','bot','你好')
        await orch.handle(msg)
        self.assertEqual(len(self.adapter.sent),1)
        self.assertIn('稍等一下',self.adapter.sent[0]['text'])
        self.assertNotIn('这是超过',self.adapter.sent[0]['text'])


class ReplyFormattingTests(unittest.TestCase):
    def normalize(self,text):return sender_module.normalize_reply_text(text)

    def test_plain_chinese_and_crlf(self):
        self.assertEqual(self.normalize(' 你好。\r\n\r\n我在的。\r\n 最后一句。 '),'你好。我在的。最后一句。')

    def test_whitespace_only_blank_lines_are_removed(self):
        self.assertEqual(self.normalize('你好。\n    \n\t\n我在。'),'你好。我在。')

    def test_english_words_keep_spaces(self):
        self.assertEqual(self.normalize('Hello\nworld.\n\nNext sentence.'),'Hello world. Next sentence.')

    def test_lists_remain_readable(self):
        self.assertEqual(self.normalize('步骤如下：\n\n1. 第一步\n2. 第二步\n\n完成。'),'步骤如下：\n1. 第一步\n2. 第二步\n完成。')

    def test_tables_headings_and_quotes_remain_multiline(self):
        text='# 标题\n> 引用\n| 名称 | 数值 |\n| --- | --- |\n| A | 1 |'
        self.assertEqual(self.normalize(text),text)

    def test_tilde_and_nested_fence_preserve_code(self):
        for text in ['~~~js\n  const x = 1;\n\n~~~','````text\n```\n  inner\n```\n````']:
            self.assertEqual(self.normalize(text),text)

    def test_normalization_is_idempotent(self):
        for text in ['你好\n\n朋友','Hello\nworld','- a\n- b','```\n  x\n\n```']:
            normalized=self.normalize(text)
            self.assertEqual(self.normalize(normalized),normalized)

    def test_system_prompt_prefers_single_paragraph(self):
        self.assertIn('普通聊天回复使用一个自然段',build_system_prompt())


if __name__=='__main__':unittest.main()
