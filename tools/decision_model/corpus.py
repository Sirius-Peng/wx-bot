"""Deterministic AI-authored fixtures, split by utterance family, never private chat logs.

These are synthetic operational examples, NOT human-labelled training data. Every split
uses disjoint sentence templates; generated metrics must keep this limitation visible.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import random

TOPICS = ('这份工作','周末安排','那场音乐会','最近的考试','家里的事情','这次旅行','那部电影','这张照片','这件外套','朋友的近况','今天的天气','新买的相机','刚才的聊天','学校的课程','我的计划','这个故事','以前的回忆','那次争执','晚饭吃什么','这首歌','明天的见面','我的爱好','这个决定','最近的睡眠','新开的餐厅','那本书','拍照地点','准备的礼物','今天的经历','学习的方法')
# A template is the indivisible split unit. Shared entity values cannot transfer its label.
FAMILIES = {
'reply': {
 'train': ('你怎么看{t}','关于{t}你有什么想法','我想听听你对{t}的看法','给我讲讲{t}','我一直在想{t}，你陪我聊会儿','{t}让我有点烦','{t}这件事让我挺开心的','我今天终于解决了{t}','我刚刚看到{t}突然想起你','我们聊聊{t}吧','我需要一点关于{t}的建议','{t}该如何处理','我不太确定{t}是否合适','我很在意{t}，心里乱糟糟的','我想把{t}分享给你','你之前提到{t}，能再说说','关于{t}我说错了，应该是明天','{t}真让人难受','{t}有哪些需要注意的地方','听完{t}你觉得怎么样'),
 'calibration': ('想知道你会怎么评价{t}','能不能一起讨论下{t}','{t}这件事我盼了好久，今天有结果了','我对{t}有点担心，想找你说说','你对{t}会怎么选择','我是不是把{t}想得太复杂了'),
 'test': ('我拿不定{t}的主意，想借一下你的脑袋','{t}带来的好心情，第一个想告诉你','想到{t}就有点心慌，能陪我待会儿','{t}把我整不会了，有招没','说说看，换成你会怎样面对{t}','关于{t}其实不是昨天，是下周','你别只听我说，也谈谈{t}','唉，{t}的结果不太好，我有点失落')},
'defer': {
 'train': ('{t}这件事我还没说完','先等我把{t}说完','关于{t}还有后续','等一下，{t}我分几条发','{t}后面还有一段','让我先说完{t}的经过','{t}先别接话，我继续','说到{t}，其实还有','关于{t}，首先是','{t}先听着，还没完','{t}我慢慢解释，你等一下','{t}的图片还在发送，先等等','还有{t}的另一件事','{t}我还在打字','{t}让我组织一下语言','{t}这只是前半段','{t}的细节我接着发','关于{t}先说到这，马上补充','{t}我打算从头讲，稍等','{t}先不要回答，等我发完'),
 'calibration': ('{t}先放一放回应，我补齐事情','我得把{t}的来龙去脉打出来','{t}的重点还在后头','{t}先听我讲下一句','我还没写好{t}最后那段','{t}我需要再发两句'),
 'test': ('先别急着接{t}，我这一串还没打完','{t}的尾巴还没交代，我接着来','{t}这个话题让我发完整段再聊','容我把{t}剩下的字敲完','关于{t}我会再贴几张图，齐了再说','{t}还差后半截，等下就有','别在{t}中间插话哈，下一条马上','{t}我拆开发，结束会告诉你')},
'context_only': {
 'train': ('{t}我知道了，谢谢','{t}收到啦','{t}明白了','{t}好，就这样','{t}我了解了，不用回','{t}以后再聊，拜拜','{t}这事先这样，晚安','{t}好的，谢谢你','{t}就到这里吧','{t}嗯，懂了','{t}行，收到','{t}我先去忙了，不用回复','{t}没别的了','{t}这件事结束啦','{t}已解决，不需要再答了','{t}好哒，我记下了','{t}感谢，我先走啦','{t}这下清楚了，回头见','{t}嗯嗯，好','{t}就按说好的，拜拜'),
 'calibration': ('{t}嗯，我理解你的意思啦','{t}那就聊到这里，我去休息','{t}谢啦，没啥要补充的','{t}明白你的建议了，不麻烦啦','{t}妥了，你也歇会儿吧','{t}好咧，不用再跟我客气'),
 'test': ('{t}懂你说的了，到这就行','{t}就照这个办，我下线啦','{t}问题已经搞定，不必再应我','{t}收到你的心意了，我睡去啦','{t}嗯哼，这话咱们先打住','{t}谢咯，回聊','{t}你讲的我记住了，别送啦','{t}我去干活，咱们下次聊')}
}


def prepare(folder, *, per_split=(8000, 2000, 2000), seed=215):
    root = Path(folder); root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    split_hashes = {}
    all_texts = set()
    for split, target in zip(('train', 'calibration', 'test'), per_split):
        rows=[]
        for label, groups in FAMILIES.items():
            for family, template in enumerate(groups[split]):
                for topic in TOPICS:
                    # These variants change the discourse surface, never the label or meaning.
                    for surface in ('{}', '嗯，{}', '{}。', '刚才说的{}'):
                        text=surface.format(template.format(t=topic))
                        if text in all_texts:raise ValueError('cross_split_duplicate')
                        all_texts.add(text)
                        rows.append({'id':f'{split}:{label}:{family}:{len(rows)}',
                            'state':{'current':text, 'recent':[]},'label':label,
                            'family':f'{split}:{label}:{family}', 'protected': label=='reply',
                            'provenance':{'kind':'ai_authored_synthetic','human_labelled':False}})
        rng.shuffle(rows)
        # Preserve class balance in smaller smoke runs.
        pools={k:[r for r in rows if r['label']==k] for k in FAMILIES}
        count=min(target//3,min(map(len,pools.values())))
        selected=[r for v in pools.values() for r in v[:count]];rng.shuffle(selected)
        raw=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in selected).encode()
        (root/(split+'.jsonl')).write_bytes(raw)
        split_hashes[split]={'sha256':hashlib.sha256(raw).hexdigest(),'records':len(selected),
            'families':sorted({r['family'] for r in selected})}
    metadata={'schema':'wxbot.decision-corpus.v1','seed':seed,'provenance':'AI-authored synthetic; no private conversations; no human annotation claimed',
              'split_unit':'disjoint utterance family; shared entities allowed','frozen_before_training':True,'splits':split_hashes}
    (root/'dataset.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    return metadata
