"""Offline analytics over synthetic JSONL; no conversation or LLM access."""
import asyncio
from datetime import datetime,timezone
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from src.core.analytics import ConversationAnalytics,clean_text
from src.runtime import MessageLog

NOW=datetime(2026,10,13,12,tzinfo=timezone.utc)
def row(text='音乐 咖啡',platform='ilink',direction='in',day=13,kind='private',ts=None):
    return {'ts':ts if ts is not None else int(datetime(2026,10,day,12,tzinfo=timezone.utc).timestamp()*1000),'direction':direction,'platform':platform,'chat_type':kind,'content':text}

class AnalyticsTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'messages.jsonl';self.engine=ConversationAnalytics(self.path)
    def tearDown(self):self.tmp.cleanup()
    def write(self,rows):self.path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    def query(self,**kw):
        kw.setdefault('min_count',1);return self.engine.query(now=NOW,**kw)
    def test_empty_and_missing(self):
        r=self.query();self.assertEqual(r['words'],[]);self.assertEqual(r['summary']['messages'],0)
    def test_occurrences_and_message_frequency_differ(self):
        self.write([row('音乐 音乐 咖啡'),row('音乐')]);r=self.query();words={w['label']:w for w in r['words']};self.assertEqual(words['音乐']['count'],3);self.assertEqual(words['音乐']['messages'],2)
    def test_default_incoming_excludes_bot(self):
        self.write([row('音乐'),row('机器人 独有内容',direction='out')]);r=self.query();self.assertEqual(r['summary']['messages'],1);self.assertNotIn('机器人',str(r['words']))
    def test_direction_both_and_out(self):
        self.write([row(),row(direction='out')]);self.assertEqual(self.query(direction='all')['summary']['messages'],2);self.assertEqual(self.query(direction='out')['summary']['messages'],1)
    def test_platform_aliases_and_unknown(self):
        self.write([row(platform=p) for p in ['ilink','wechat','qq','onebot','']]);self.assertEqual(self.query(platform='wechat')['summary']['messages'],2);self.assertEqual(self.query(platform='qq')['summary']['messages'],2);self.assertEqual(self.query()['summary']['unknown_platform_messages'],1)
    def test_group_filter(self):
        self.write([row(kind='private'),row(kind='group')]);self.assertEqual(self.query(chat_type='group')['summary']['messages'],1)
    def test_date_window_uses_calendar_days(self):
        self.write([row(day=6),row(day=7),row(day=13)]);self.assertEqual(self.query(days=7)['summary']['messages'],2);self.assertEqual(self.query(days=0)['summary']['messages'],3)
    def test_timezone_boundary(self):
        ts=int(datetime(2026,10,6,23,tzinfo=timezone.utc).timestamp()*1000);self.write([row(ts=ts)]);self.assertEqual(self.query(days=7,zone='Asia/Shanghai')['summary']['messages'],1);self.assertEqual(self.query(days=7,zone='UTC')['summary']['messages'],0)
    def test_future_not_included(self):
        self.write([row(day=14)]);self.assertEqual(self.query(days=0)['summary']['messages'],0)
    def test_english_case_fold_and_stopwords(self):
        self.write([row('Python PYTHON python 我们 今天 这个 所以 的 了')]);r=self.query();self.assertEqual(r['words'],[{'label':'python','count':3,'messages':1}])
    def test_topics_are_multilabel_once_per_message(self):
        self.write([row('音乐 音乐 咖啡 编程 Python')]);topics={x['label']:x for x in self.query()['topics']};self.assertEqual(topics['音乐与艺术']['count'],1);self.assertEqual(topics['饮食与美食']['count'],1);self.assertEqual(topics['编程与技术']['share'],100);self.assertIn('python',topics['编程与技术']['keywords'])
    def test_not_claiming_semantic_topics(self):self.assertEqual(self.query()['method']['topics'],'keyword_rules')
    def test_unknown_topic_visible(self):
        self.write([row('星云 晶体')]);self.assertEqual(self.query()['topics'][0]['label'],'未归类')
    def test_redaction_and_placeholders(self):
        text='音乐 https://private.invalid/path test@example.com 13812345678 sk-abcdefgh123 Bearer ABCsecret api_key=topsecret password:passsecret [图片1] [语音]'
        self.write([row(text)]);data=json.dumps(self.query(),ensure_ascii=False)
        for secret in ['private.invalid','example.com','13812345678','abcdefgh','abcsecret','topsecret','passsecret']:self.assertNotIn(secret,data)
        self.assertIn('音乐',data)
    def test_code_fence_removed(self):
        self.write([row('音乐 ```python\nnevercountme()\n```')]);self.assertNotIn('nevercountme',str(self.query()['words']))
    def test_image_only_not_effective_text(self):
        self.write([row('[图片1]'),row('音乐')]);r=self.query();self.assertEqual(r['summary']['messages'],2);self.assertEqual(r['summary']['text_messages'],1)
    def test_reads_beyond_recent_buffer(self):
        self.write([row('早期主题') for _ in range(100)]+[row('音乐') for _ in range(2100)]);r=self.query();self.assertEqual(r['summary']['messages'],2200);self.assertTrue(any(w['label']=='早期' for w in r['words']))
    def test_restart_uses_durable_log(self):
        self.write([row('音乐')]);a=self.query();b=ConversationAnalytics(self.path).query(now=NOW,min_count=1);self.assertEqual(a['words'],b['words'])
    def test_append_invalidates_cache(self):
        self.write([row('音乐')]);self.query()
        with self.path.open('a') as f:f.write(json.dumps(row('音乐'))+'\n')
        self.assertEqual(self.query()['words'][0]['count'],2)
    def test_same_signature_reuses_segmentation(self):
        self.write([row()]);self.query()
        with patch.object(self.engine,'_tokens',side_effect=AssertionError('re-tokenized')):self.query(platform='wechat')
    def test_clear_removes_statistics(self):
        self.write([row()]);self.query();MessageLog(self.path).clear();r=self.query();self.assertEqual(r['summary']['messages'],0);self.assertEqual(r['words'],[])
    def test_atomic_replacement_invalidates(self):
        self.write([row('音乐')]);self.query();other=self.path.with_suffix('.new');other.write_text(json.dumps(row('跑步'))+'\n');other.replace(self.path);self.assertEqual(self.query()['words'][0]['label'],'跑步')
    def test_bad_and_incomplete_lines_skip(self):
        self.path.write_text('broken\n'+json.dumps(row('音乐'))+'\n{"unfinished":');r=self.query();self.assertEqual(r['summary']['messages'],1);self.assertEqual(r['coverage']['skipped_lines'],2)
    def test_trailing_line_becomes_complete(self):
        self.path.write_text(json.dumps(row('音乐')));self.assertEqual(self.query()['summary']['messages'],0)
        with self.path.open('a') as f:f.write('\n')
        self.assertEqual(self.query()['summary']['messages'],1)
    def test_invalid_fields_skipped(self):
        self.write([None,{},row(ts=float('nan')),row(ts=True),row(ts=-1),{**row(),'content':[]},row(direction='unknown')]);self.assertEqual(self.query()['coverage']['skipped_lines'],7)
    def test_bounded_scan_reports_omission(self):
        self.write([row('咖啡') for _ in range(30)])
        with patch('src.core.analytics.MAX_SCAN_BYTES',1024):r=self.query()
        self.assertGreater(r['coverage']['omitted_bytes'],0);self.assertLess(r['summary']['messages'],30)
    def test_huge_line_does_not_block_following_record(self):
        self.write([row('x'*1000),row('音乐')])
        with patch('src.core.analytics.MAX_LINE_BYTES',200):r=self.query()
        self.assertEqual(r['coverage']['oversized_lines'],1);self.assertEqual(r['summary']['messages'],1)
    def test_long_text_limit_reported(self):
        self.write([row('音乐 '*10)])
        with patch('src.core.analytics.MAX_TEXT_CHARS',8):r=self.query()
        self.assertEqual(r['coverage']['truncated_texts'],1)
    def test_vocabulary_limit_reported(self):
        self.write([row('音乐 咖啡 编程')])
        with patch('src.core.analytics.MAX_VOCABULARY',1):r=self.query()
        self.assertEqual(r['summary']['unique_words'],1);self.assertTrue(r['coverage']['vocabulary_limited'])
    def test_top_limit_minimum_and_stable_ties(self):
        self.write([row('音乐 咖啡'),row('音乐 咖啡'),row('编程')]);r=self.engine.query(now=NOW,min_count=2,limit=1);self.assertEqual(len(r['words']),1);self.assertEqual(r['words'][0]['label'],'咖啡')
    def test_invalid_filters(self):
        for kw in [{'days':1},{'platform':'bad'},{'direction':'bad'},{'chat_type':'bad'},{'limit':0},{'min_count':0}]:
            with self.assertRaises(ValueError):self.query(**kw)
    def test_no_account_ids_or_raw_excerpts_in_response(self):
        r=row();r.update(sender_id='private-account',chat_id='private-chat',sender='private-sender');self.write([r]);data=json.dumps(self.query());self.assertNotIn('private-account',data);self.assertNotIn('private-chat',data);self.assertNotIn('private-sender',data)
    def test_response_mutation_does_not_corrupt_cache(self):
        self.write([row('音乐')]);a=self.query();a['words'][0]['count']=999;a['coverage']['skipped_lines']=999;b=self.query();self.assertEqual(b['words'][0]['count'],1);self.assertEqual(b['coverage']['skipped_lines'],0)
    def test_packaging_includes_dictionary(self):
        root=Path(os.environ.get('WXBOT_TEST_ROOT',Path(__file__).resolve().parents[1]))
        for f in ['requirements.txt','desktop/scripts/build-backend.sh','desktop/scripts/build-backend.ps1']:self.assertIn('jieba',(root/f).read_text())

    def test_deleted_mid_scan_does_not_cache_old_data(self):
        self.write([row('音乐')]);original=self.engine._tokens
        def tokens(text):self.path.unlink();return original(text)
        with patch.object(self.engine,'_tokens',side_effect=tokens):a=self.query()
        self.assertTrue(a['coverage']['changed_during_scan']);self.assertEqual(self.query()['summary']['messages'],0)
    def test_truncated_mid_scan_terminates_and_refreshes(self):
        self.write([row('音乐') for _ in range(200)]);original=self.engine._tokens;first=True
        def tokens(text):
            nonlocal first
            if first:self.path.write_text('');first=False
            return original(text)
        with patch.object(self.engine,'_tokens',side_effect=tokens):a=self.query()
        self.assertTrue(a['coverage']['changed_during_scan']);self.assertEqual(self.query()['summary']['messages'],0)
    def test_appended_mid_scan_refreshes_next_query(self):
        self.write([row('音乐')]);original=self.engine._tokens;first=True
        def tokens(text):
            nonlocal first
            if first:
                with self.path.open('a') as f:f.write(json.dumps(row('音乐'))+'\n')
                first=False
            return original(text)
        with patch.object(self.engine,'_tokens',side_effect=tokens):a=self.query()
        self.assertEqual(a['summary']['messages'],1);self.assertTrue(a['coverage']['changed_during_scan']);self.assertEqual(self.query()['summary']['messages'],2)

    def test_replaced_mid_scan_refreshes_next_query(self):
        self.write([row('音乐')]);original=self.engine._tokens
        def tokens(text):
            other=self.path.with_suffix('.new');other.write_text(json.dumps(row('跑步'))+'\n');other.replace(self.path);return original(text)
        with patch.object(self.engine,'_tokens',side_effect=tokens):a=self.query()
        self.assertTrue(a['coverage']['changed_during_scan']);self.assertEqual(self.query()['words'][0]['label'],'跑步')

    def test_reference_instant_converted_to_conversation_zone(self):
        instant=datetime(2026,10,13,23,tzinfo=timezone.utc)
        self.write([row(day=14)])
        r=self.engine.query(zone='Asia/Shanghai',now=instant,min_count=1)
        self.assertEqual(r['summary']['messages'],1)

class StatisticsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'WXBOT_DATA_DIR':self.tmp.name,'WXBOT_ADMIN_TOKEN':'stats-test','WXBOT_HOST':'127.0.0.1','WXBOT_SKIP_NAPCAT_SYNC':'1'});self.env.start()
        import src.server as server
        self.s=importlib.reload(server);self.c=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.s.app),base_url='http://localhost',headers={'Authorization':'Bearer stats-test'})
    async def asyncTearDown(self):await self.c.aclose();self.env.stop();self.tmp.cleanup()
    async def test_default_scope_and_words(self):
        ts=int(datetime.now(timezone.utc).timestamp()*1000);self.s.runtime.log.path.write_text(json.dumps(row('音乐 音乐',ts=ts))+'\n');r=await self.c.get('/api/statistics');self.assertEqual(r.status_code,200);self.assertEqual(r.json()['words'][0]['count'],2);self.assertEqual(r.json()['filters']['direction'],'in')
    async def test_auth_required(self):
        self.c.headers.clear();self.assertEqual((await self.c.get('/api/statistics')).status_code,401)
    async def test_bad_filters(self):
        for q in ['days=-1','days=1','platform=bad','direction=bad','chat_type=bad','limit=51','min_count=0','limit=oops']:
            self.assertIn((await self.c.get('/api/statistics?'+q)).status_code,[400,422])
    async def test_worker_thread_not_event_loop(self):
        import threading
        main=threading.get_ident();thread=[]
        def query(*a):thread.append(threading.get_ident());return {'ok':True}
        with patch.object(self.s.runtime.analytics,'query',side_effect=query):r=await self.c.get('/api/statistics')
        self.assertEqual(r.status_code,200);self.assertNotEqual(thread[0],main)
    async def test_error_sanitized(self):
        with patch.object(self.s.runtime.analytics,'query',side_effect=OSError('private-secret')):r=await self.c.get('/api/statistics')
        self.assertEqual(r.status_code,503);self.assertNotIn('private-secret',r.text)
    async def test_clear_sync(self):
        ts=int(datetime.now(timezone.utc).timestamp()*1000);self.s.runtime.log.path.write_text(json.dumps(row('音乐 音乐',ts=ts))+'\n');await self.c.get('/api/statistics');await self.c.delete('/api/messages');self.assertEqual((await self.c.get('/api/statistics')).json()['summary']['messages'],0)
