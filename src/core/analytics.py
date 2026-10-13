"""Local frequency analytics over durable JSONL, independent of the 2,000-row UI buffer.

No LLM, network request, raw excerpts or account identifiers in analytic responses.
The bounded streaming scan and tokenizer run in a worker thread, never on reply IO.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta
import json
from io import BytesIO
import logging
import math
from pathlib import Path
import re
import threading
from zoneinfo import ZoneInfo
from .fileio import read_snapshot
from .reality import local_datetime, valid_timezone

MAX_SCAN_BYTES=32*1024*1024
MAX_LINE_BYTES=256*1024
MAX_TEXT_CHARS=8000
MAX_VOCABULARY=30000
STOPWORDS=set('的 了 是 在 和 与 就 都 而 及 或 一个 一些 我们 你们 他们 她们 这个 那个 这些 那些 什么 怎么 为什么 可以 可能 还是 但是 因为 所以 如果 然后 已经 没有 不是 不要 不能 需要 觉得 知道 时候 现在 今天 明天 昨天 真的 一起 一直 一下 一点 有点 这样 那样 这里 那里 自己 对方 其实 应该 不会 不用 好的 好吧 好像 呢 啊 嗯 哈哈 哈哈哈 嘿嘿 谢谢 收到 请问 不过 比较 非常 还有 只是 正在 是否 我 你 他 她 它 这 那 all and the are was were have has with that this from your you for not but can will into about then also just http https www com'.split())
TOPICS={
 '工作与职场':'工作 上班 下班 加班 同事 老板 会议 项目 职场 面试 招聘 工资',
 '学习与成长':'学习 考试 作业 复习 课程 论文 大学 学校 英语 数学 阅读 读书',
 '编程与技术':'编程 代码 开发 程序 软件 网关 模型 人工智能 服务器 python javascript typescript deepseek llm api bug',
 '游戏与动漫':'游戏 鸣潮 弗洛洛 原神 崩铁 动漫 漫画 副本 抽卡 攻略 角色 排位',
 '音乐与艺术':'音乐 歌曲 唱歌 乐曲 演奏 钢琴 吉他 绘画 画画 摄影 电影 乐队 演唱会',
 '饮食与美食':'吃饭 做饭 午饭 晚饭 早餐 美食 火锅 咖啡 奶茶 烘焙 餐厅 菜谱',
 '健康与睡眠':'睡觉 睡眠 失眠 熬夜 健康 生病 感冒 医院 头疼 休息 疲惫',
 '运动与健身':'运动 健身 跑步 散步 游泳 篮球 足球 瑜伽 骑行 锻炼',
 '旅行与出行':'旅行 旅游 出差 酒店 飞机 高铁 火车 机票 景点 地铁 出行 天气',
 '关系与情绪':'朋友 家人 恋爱 陪伴 想念 难过 开心 焦虑 压力 孤独 心情 感情',
 '生活与消费':'生活 购物 买东西 快递 房租 装修 搬家 家务 宠物 猫咪 钱包 预算',
}
TOPIC_WORDS={label:set(words.split()) for label,words in TOPICS.items()}
REDACT=re.compile(r'https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|sk-[\w-]{6,}|Bearer\s+\S+|(?<!\d)\d{6,}(?!\d)|(?:api[_ -]?key|token|password|密码|密钥)\s*[:=：]\s*[\w.\-]+|data:image/\S+',re.I)
PLACEHOLDERS=re.compile(r'\[(?:图片|图像|语音|视频|文件|表情|image|audio|video|file)\d*[^\]]*\]',re.I)
WORD=re.compile(r'(?:[\u4e00-\u9fff]{2,24}|[a-z][a-z0-9+#_-]{1,23})\Z')


def clean_text(text):
    text=re.sub(r'```[\s\S]*?```',' ',text)
    text=REDACT.sub(' ',text)
    text=PLACEHOLDERS.sub(' ',text)
    return re.sub(r'<[^>]{0,200}>',' ',text).lower()


def _signature(path):
    try:
        s=path.stat();return (s.st_ino,s.st_size,s.st_mtime_ns)
    except FileNotFoundError:return None


_TOKENIZER=None
_TOKENIZER_LOCK=threading.Lock()
def _shared_tokenizer():
    # Read-only dictionary is shared, not the conversation aggregates or raw text.
    global _TOKENIZER
    with _TOKENIZER_LOCK:
        if _TOKENIZER is None:
            try:import jieba
            except ImportError:raise RuntimeError('中文分词依赖未安装，请更新运行依赖后重试')
            jieba.setLogLevel(logging.WARNING)
            tokenizer=jieba.Tokenizer()
            for words in TOPIC_WORDS.values():
                for word in words:tokenizer.add_word(word)
            _TOKENIZER=tokenizer
        return _TOKENIZER


class ConversationAnalytics:
    def __init__(self,path):
        self.path=Path(path);self._lock=threading.Lock();self._tokenizer=None
        self._revision=None;self._buckets={};self._meta={};self._zone=None

    def _tokens(self,text):
        if self._tokenizer is None:self._tokenizer=_shared_tokenizer()
        return [word for word in self._tokenizer.cut(text,HMM=False) if WORD.fullmatch(word) and word not in STOPWORDS]

    def _build(self,zone,revision):
        buckets={};vocabulary=set()
        meta={'skipped_lines':0,'oversized_lines':0,'truncated_texts':0,'vocabulary_limited':False,
              'scan_bytes':0,'omitted_bytes':0,'file_bytes':revision[1] if revision else 0}
        if revision is not None:
            snapshot,offset=read_snapshot(self.path,MAX_SCAN_BYTES)
            size=len(snapshot)
            with BytesIO(snapshot) as f:
                start=0
                if offset:
                    chunk=f.readline(min(MAX_LINE_BYTES+1,size-f.tell()))
                    while chunk and not chunk.endswith(b'\n') and f.tell()<size:chunk=f.readline(min(MAX_LINE_BYTES+1,size-f.tell()))
                    start=f.tell()
                meta['omitted_bytes']=offset+start
                while f.tell()<size:
                    remaining=size-f.tell();line=f.readline(min(MAX_LINE_BYTES+1,remaining))
                    if not line:break
                    meta['scan_bytes']+=len(line)
                    if len(line)>MAX_LINE_BYTES:
                        # Consume this entire oversized record without allocating it.
                        while line and not line.endswith(b'\n') and f.tell()<size:line=f.readline(min(MAX_LINE_BYTES+1,size-f.tell()))
                        meta['oversized_lines']+=1;continue
                    if not line.endswith(b'\n'):
                        meta['skipped_lines']+=1;continue
                    try:
                        raw=json.loads(line)
                        if not isinstance(raw,dict) or raw.get('direction') not in ('in','out') or not isinstance(raw.get('content'),str):raise ValueError()
                        ts=float(raw['ts'])
                        if isinstance(raw['ts'],bool) or not math.isfinite(ts) or ts<=0:raise ValueError()
                        day=local_datetime(zone,ts/1000).date().isoformat()
                    except (ValueError,TypeError,KeyError,OverflowError,OSError):
                        meta['skipped_lines']+=1;continue
                    platform={'wechat':'wechat','ilink':'wechat','qq':'qq','onebot':'qq'}.get(raw.get('platform') if isinstance(raw.get('platform'),str) else '', 'unknown')
                    kind=raw.get('chat_type') if raw.get('chat_type') in ('private','group') else 'unknown'
                    key=(day,platform,raw['direction'],kind)
                    bucket=buckets.setdefault(key,{'messages':0,'text_messages':0,'words':Counter(),'word_messages':Counter(),'topics':Counter(),'topic_words':{}})
                    bucket['messages']+=1
                    text=raw['content']
                    if len(text)>MAX_TEXT_CHARS:meta['truncated_texts']+=1
                    tokens=self._tokens(clean_text(text[:MAX_TEXT_CHARS]))
                    if not tokens:continue
                    bucket['text_messages']+=1
                    accepted=[]
                    for word in tokens:
                        if word not in vocabulary:
                            if len(vocabulary)>=MAX_VOCABULARY:meta['vocabulary_limited']=True;continue
                            vocabulary.add(word)
                        accepted.append(word)
                    bucket['words'].update(accepted);bucket['word_messages'].update(set(accepted))
                    found=False;unique=set(tokens)
                    for label,aliases in TOPIC_WORDS.items():
                        hits=unique&aliases
                        if hits:
                            found=True;bucket['topics'][label]+=1
                            bucket['topic_words'].setdefault(label,Counter()).update(hits)
                    if not found:bucket['topics']['未归类']+=1
                meta['scan_bytes']=f.tell()-start
        self._buckets=buckets;self._meta=meta;self._zone=zone
        meta['changed_during_scan']=_signature(self.path)!=revision
        # Keep the indexed signature, not None: deleting a file mid-scan must invalidate next query.
        self._revision=revision

    def query(self,days=30,platform='',direction='in',chat_type='',limit=20,min_count=2,zone='Asia/Shanghai',now=None):
        if days not in (0,7,30,90) or platform not in ('','wechat','qq') or direction not in ('in','out','all') or chat_type not in ('','private','group') or not 1<=limit<=50 or not 1<=min_count<=100:raise ValueError('Invalid analytics filters')
        zone=valid_timezone(zone)
        reference=now.astimezone(ZoneInfo(zone)) if now is not None and now.tzinfo is not None else (now or local_datetime(zone))
        today=reference.date()
        first=today-timedelta(days=days-1) if days else None
        with self._lock:
            revision=_signature(self.path)
            if revision!=self._revision or zone!=self._zone:self._build(zone,revision)
            words=Counter();word_messages=Counter();topics=Counter();topic_words={};total=0;texts=0;dates=set();unknown=0
            for (day,p,d,k),bucket in self._buckets.items():
                date=datetime.fromisoformat(day).date()
                if date>today or (first and date<first) or (platform and p!=platform) or (direction!='all' and d!=direction) or (chat_type and k!=chat_type):continue
                total+=bucket['messages'];texts+=bucket['text_messages'];dates.add(day)
                if p=='unknown':unknown+=bucket['messages']
                words.update(bucket['words']);word_messages.update(bucket['word_messages']);topics.update(bucket['topics'])
                for label,hits in bucket['topic_words'].items():topic_words.setdefault(label,Counter()).update(hits)
            ranked=lambda c:sorted(((w,n) for w,n in c.items() if n>=min_count),key=lambda x:(-x[1],x[0]))[:limit]
            word_list=[{'label':w,'count':n,'messages':word_messages[w]} for w,n in ranked(words)]
            topic_list=[{'label':w,'count':n,'messages':n,'share':round(n/max(1,texts)*100,1),
                         'keywords':[x for x,_ in topic_words.get(w,Counter()).most_common(5)]} for w,n in ranked(topics)]
            return {'words':word_list,'topics':topic_list,
                    'summary':{'messages':total,'text_messages':texts,'token_count':sum(words.values()),'unique_words':len(words),'active_days':len(dates),'unknown_platform_messages':unknown},
                    'filters':{'days':days,'platform':platform,'direction':direction,'chat_type':chat_type,'limit':limit,'min_count':min_count,'timezone':zone},
                    'coverage':dict(self._meta),'method':{'words':'jieba_exact','topics':'keyword_rules','topic_multilabel':True},
                    'generated_at':local_datetime(zone).isoformat(timespec='seconds')}
