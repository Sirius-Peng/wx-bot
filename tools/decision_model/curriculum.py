"""Training-only conversational augmentation; never mutate or tune a held-out split.

This is a compositional, AI-authored development curriculum, NOT human annotation
or a new blind evaluation.  Its 60 authored situations contain different events,
constraints and outcomes, rather than changing the entity in one utterance.  Thirty
discourse moves per situation exercise quotation, scope, emotion, corrections,
withdrawal, Traditional Chinese, mixed language and merged envelopes.  A small
counterfactual section keeps the same utterance but changes its user-history scope.

The source calibration/test files are copied byte for byte and retain their old
status as seen development regression data.  Do not point this command at a blind
acceptance set.  No locked stress fixture is read, and no model is used here.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import tempfile


VERSION = 'wxbot.conversational-curriculum.v1'

# id, prior user setup, new event, unfinished detail, corrected fact, settled outcome.
# Each row is one semantic family; its branches must NEVER be split across datasets.
SITUATIONS = (
    ('work-proof', '我在写一份交付说明，正文和附录分开了。', '同事只收到正文，附录还留在草稿箱', '附件的命名和收件人清单', '应该发给实施组，不是销售组', '附录已经补发并确认收到'),
    ('work-meeting', '今天要协调两个人的空档，会议暂时没有排好。', '其中一个人的日历突然多了半小时空白', '另一位的出差时间和会议目的', '会议改成语音，不需要准备投影', '两边都确认了新的半小时安排'),
    ('work-printer', '我得打印一批双面材料，机器刚换过纸。', '打印机把第二页旋转了半圈', '驱动里双面翻转的两个选项', '纸张尺寸是A5，之前那份A4设置不能沿用', '试印的两页方向已经一致'),
    ('work-note', '我在整理会议笔记，有一处责任人写得模糊。', '两个人都说自己只负责核对数字', '当时的原话和任务交接记录', '负责人是轮值同事，不是整个小组', '责任人和截止时间已补进笔记'),
    ('work-move', '办公桌要换位置，我想少搬一些不常用的东西。', '桌下竟然还有半箱没拆封的线材', '接口类型和现有设备数量', '那一箱属于公共库存，不是我的私人物品', '常用线材留下，其余已经归还公共柜'),
    ('work-break', '午间休息的房间临时被借用，我在找替代地点。', '楼下安静的小角落也在维修', '附近可坐的位置和回来的路程', '维修只到今天下午，明天就能恢复', '我选择在茶水间坐了一会儿'),
    ('study-card', '我把复习卡片按章节分了几叠。', '混进来的一叠卡片没有写章节号', '卡片中的公式和例题出处', '缺的是章节标签，卡片内容本身没错', '无标签卡片已经归到对应章节'),
    ('study-notes', '我在听公开课，想把笔记压缩成一页。', '老师把同一个概念用了两种不同说法', '前后两段的定义和举例', '后一个说法是应用场景，不是新的定义', '两种说法已经合成一条带示例的笔记'),
    ('study-seat', '我借了自习室的位置，预约规则有点绕。', '系统显示位置空着却不能继续预约', '预约日期和连续使用时长', '限制按整天算，不是按我实际坐的小时算', '我改约了下午另一间空教室'),
    ('study-project', '小组作业要交一个演示，我负责把素材拼起来。', '两段录屏的画面比例不一致', '录制分辨率和最终导出的要求', '成片只需要竖版，横版不是本次要求', '录屏已经统一裁成竖版并导出'),
    ('study-book', '我借来的书里夹着几张手写便条。', '便条上的页码比新版书少了一章', '旧版目录与新版目录之间的对应', '便条对应的是上一版，内容不是漏印', '我把对应页码贴在便条背面了'),
    ('study-plan', '我想把每天的学习安排拆成短一些的时间块。', '第一天的任务在二十分钟内就做完了', '剩下任务的难度和明天的空闲', '二十分钟只是热身练习，不是整章内容', '我把剩下时间留给了错题复盘'),
    ('food-rice', '我想试一次少放水的焖饭，材料已经切好了。', '锅底熟了，上面的米还带一点硬芯', '锅的厚度和中途打开锅盖的次数', '米提前泡过了，不需要再按干米加水', '补水焖过以后米粒软硬合适了'),
    ('food-soup', '冰箱剩了几样蔬菜，我打算一起煮成汤。', '其中一把叶菜比我预计的更容易烂', '几种菜的下锅顺序', '最后才下的是叶菜，不是土豆', '汤已经分装好，叶菜没有继续煮'),
    ('food-lunch', '明天的午饭想提前准备，但冰箱位置有限。', '饭盒叠起来会挡住冷藏室的风口', '饭盒尺寸和需要冷藏的其他东西', '只准备一顿就够，不需要同时备三天', '我换了薄饭盒，风口已经空出来'),
    ('food-bread', '我想用小烤箱烤两片面包，先做一次试验。', '边缘上色很快，中间却还是软的', '烤盘高度和加热管距离', '烤箱实际是上下管独立控温', '下调上管温度后面包没有再焦边'),
    ('food-tea', '家里两包茶的外包装很像，我想分开贴标签。', '一包闻起来清淡，另一包明显更浓', '包装上的冲泡温度和存放日期', '两包是不同焙火程度，不是不同产地', '两个茶罐都贴上了各自的标签'),
    ('food-shop', '我在做下周的采购清单，想尽量少浪费。', '清单里的两道菜要用同一种酱料', '现有调料的剩余量和保质日期', '缺的是小瓶酱油，不是需要再买一套调料', '重复的调料已删掉，只保留了小瓶装'),
    ('creative-color', '我给小册子做封面，打算只用两种主色。', '屏幕上协调的颜色印出来却有些灰', '纸张底色和印刷色值', '这次是黑白内页配彩色封面', '我改用更亮的纸，并完成了一张试样'),
    ('creative-photo', '我挑了几张散步时拍的照片，想拼成一页。', '最好看的两张构图方向刚好相反', '标题位置和图片的裁剪余量', '页面是对开折页，不是普通长图', '两张照片分在折线两边，已经排好'),
    ('creative-text', '我想给朋友写一张简短的贺卡，不写套话。', '开头写得很正式，后面却像随手聊天', '卡片的实际用途和我们平时的称呼', '这是一张道谢卡，不是纪念日贺卡', '我保留了日常称呼，把正式开头删掉了'),
    ('creative-track', '我给一小段生活录像配了背景音乐。', '转场恰好落在音乐最突兀的一拍上', '两个转场的长度和音轨空隙', '需要移动的是画面切点，不是整段音轨', '我把切点前移，试听已经顺了'),
    ('creative-clay', '我用剩余黏土捏一个小收纳盘。', '盘沿干得快，底部摸起来却还很软', '盘底厚度和室内通风情况', '它是自然风干材料，不是需要烧制的陶土', '我减慢了通风，底部已经均匀干燥'),
    ('creative-font', '我在做活动海报，想让大字和小字看起来协调。', '标题很醒目，日期却缩小以后看不清', '远距离观看时的字号和行间距', '海报要贴在室内门口，不是手机屏幕', '日期字号已调大，门口试贴可以看清'),
    ('transit-route', '我准备周末去另一个城区，先查了两条路线。', '直达车少换乘，但下车要多走一段', '末班时间和那段步行路线', '是白天出发，不涉及夜间末班车', '我选了直达车，把步行路线也存好了'),
    ('transit-rain', '出门前我把雨具放在门边了。', '临时要带的纸袋放不进原来的背包', '纸袋尺寸和一路上能避雨的位置', '袋子里是资料复印件，不是易碎物品', '我套了防水袋，已经平稳到家'),
    ('transit-platform', '我要在一个不熟悉的站换乘，提前看了示意图。', '现场的出口编号和截图不一样', '站厅层的标识和新开的通道', '变化的是出口编号，列车线路没有改', '我按现场标识找到了正确站台'),
    ('transit-ticket', '我替家里人看两段行程，想让中转别太赶。', '便宜的组合只留下很短的中转空隙', '行李情况和两个站台的距离', '同站换乘不用出站，不是跨城换站', '我选了留足中转时间的那一组'),
    ('transit-bike', '我给自行车装了一个新的前灯。', '灯固定好了，却会被车篮边缘遮住一角', '车把空间和灯的可调角度', '需要调整的是安装位置，不是亮度档位', '前灯已挪到另一侧，照明没有遮挡'),
    ('transit-walk', '我想把回家的步行路线换得安静一点。', '安静的那条路要经过一段正在铺砖的路面', '绕行距离和施工结束的日期', '是临时施工，不是整段道路封闭', '我先走短绕行路线，等施工结束再换'),
    ('social-table', '大家约了一次普通聚餐，座位还没确定。', '两个人都想坐方便离开的那一侧', '到场顺序和每个人预计离开的时间', '提前离开的是我，不是负责订桌的朋友', '我选了边上的座位，大家都已经知道'),
    ('social-gift', '我给朋友挑一件小礼物，想实用一些。', '看中的两个款式一个便携一个容量大', '朋友平时带包的习惯和使用场合', '她常在家用，不需要优先考虑便携', '我选了容量大的款式，包装也弄好了'),
    ('social-invite', '邻居邀请我参加一次小型桌游，我还没答复。', '开始时间正好卡在我原定回家的点上', '实际结束时间和能否中途加入', '我那天有空，只是到得稍微晚一点', '我已经说明会晚到，对方也接受了'),
    ('social-message', '我想回一条比较客气的消息，别显得太生硬。', '打好的回复读起来像是在催人交差', '对方上一条的语气和事情的紧急程度', '对方只是问进度，并没有要求当天交付', '我改成了普通进度说明，已经发出'),
    ('social-loan', '我借给朋友一本书，最近想重新翻一遍。', '朋友说书在另一个住处，下周才能带来', '我真正要看的章节和手边其他版本', '我只需要其中两页，并不急着拿回整本', '朋友拍了对应两页，借书的事先放下了'),
    ('social-picnic', '几个人打算去公园坐一下午，分工还没细化。', '大家都准备带吃的，却没人带垫子', '现场地面情况和每个人已有的东西', '公园有长椅，只是草地坐着更方便', '垫子已有人负责，不需要再重复准备'),
    ('home-lamp', '我把书桌上的灯换到了左侧。', '看书没有影子了，键盘却有些反光', '灯罩角度和桌面材质', '反光来自桌垫，不是显示器', '我调整了灯罩，键盘不再晃眼'),
    ('home-box', '我想把玄关的小物件归到几个盒子里。', '钥匙很顺手，门卡却总被压在底下', '盒子的深度和每天拿取的顺序', '门卡每天都用，不能放到备用物品那盒', '门卡单独放在浅盘里了'),
    ('home-wash', '我准备洗一批换季衣物，已经分了颜色。', '其中一件标签写着不能直接烘干', '面料成分和晾晒空间', '那件需要平铺晾干，不是所有衣服都一样', '特殊面料单独晾好了，其他已经收起'),
    ('home-plant', '窗台上的植物最近长出了一片新叶。', '新叶的方向正好顶住窗框', '花盆位置和白天的光照方向', '要移动的是整盆，不需要剪掉新叶', '我把盆移开了一点，新叶已经展开'),
    ('home-cable', '我想把电视柜后面的线整理清楚。', '拔掉一条旧线后音箱也没了声音', '接口标识和现在实际连接的设备', '那根线仍在传音频，不是废线', '线材已经贴好标签并接回去了'),
    ('home-calendar', '家里墙上的日历和手机日历都记了待办。', '同一件事被我写在了两个不同日期', '通知里的时间和当时改期的消息', '改期后的日期才有效，纸上的还没更新', '两个日历已同步，不用再核对了'),
    ('hobby-puzzle', '我在拼一幅颜色很接近的拼图。', '中间有一块能塞进去，却让边缘歪了', '相邻图案的走势和背面的编号', '那块只是形状相似，不能硬按进去', '我换了一块，边缘已经重新对齐'),
    ('hobby-game', '我在试一个没有教程的小关卡。', '按下开关以后门没开，反而亮了一盏灯', '房间里另外两个机关的状态', '开关控制的是顺序，不是直接开门', '按正确顺序完成后门已经打开'),
    ('hobby-music', '我想练一段简单的节奏，先用很慢的速度。', '单独敲能跟上，一加伴奏就抢拍', '我数拍的方式和伴奏里的重音', '问题在起拍位置，不是速度太快', '我加了预备拍，已经能稳稳跟住'),
    ('hobby-sketch', '我在画杯子和盘子的简单静物。', '杯子画得很立体，盘子却像竖起来的圈', '视线高度和盘沿的椭圆比例', '视角比我最初画的要低一些', '盘沿比例改好了，画面已经协调'),
    ('hobby-run', '我想把散步里的某一段换成轻松慢跑。', '前一小段很轻松，转弯后节奏就乱了', '路面坡度和转弯前的步幅', '这里只是在试路线，不是在追求配速', '我把坡道留给走路，路线已经调整好'),
    ('hobby-listen', '我在整理几段喜欢的电台片段。', '同一个片段被两个不同标题收录了', '音频开头的报时和节目名称', '它们是同一期的不同剪辑，不是两期节目', '重复片段已合并到同一个条目'),
    ('tech-photo', '我把手机照片复制到电脑，打算再做一份备份。', '电脑里多出一批同名但大小不同的文件', '导出选项和原图是否保留', '小文件是缩略图，不是原图损坏', '原图和缩略图已分开放好'),
    ('tech-window', '我调整了电脑外接屏幕的排列。', '鼠标能过去，拖窗口时却像撞到一堵墙', '两块屏幕在系统里的上下对齐方式', '屏幕高度不一样，不应该强行对齐上边缘', '虚拟排列已经调整，窗口能顺畅拖动'),
    ('tech-sync', '我想让两台设备上的待办保持一致。', '一边完成的项目在另一边仍然显示未做', '同步时间和各自登录的账户', '两边其实用了不同账户，不是网络延迟', '我换回同一账户，待办状态已经一致'),
    ('tech-subtitle', '我给一段自己录的视频加了字幕。', '前半段对得上，越到后面越偏', '视频帧率和字幕生成时的时间轴', '字幕需要整体变速，不是只平移几秒', '字幕时间轴已拉伸，末尾也对齐了'),
    ('tech-file', '我在清理旧资料，只想保留最终定稿。', '两个文件都写着最终版，修改时间也很接近', '正文里最后一处改动和导出来源', '要保留的是带签名那份，不是文件名最长那份', '定稿已经单独备份，重复文件已归档'),
    ('tech-reminder', '我给一个周期性小任务设了提醒。', '提醒连续两天出现，第三天却没有响', '重复规则和勿扰时段的交集', '第三天是周末，原规则只覆盖工作日', '我补上周末规则，试响已经正常'),
    ('life-parcel', '我取了两个大小相近的快递，想先把包装收掉。', '其中一个箱子里只有配件，没有主件', '订单拆单信息和另一箱的物流', '主件是分开发货，不是商家漏装', '拆单信息已核对，配件也单独收好了'),
    ('life-drawer', '我整理抽屉时翻到一张以前写的清单。', '清单上有几项看着熟悉，却没写用途', '当时旁边记的日期和对应收据', '这是旅行前的准备单，不是现在的采购单', '旧清单已归档，不会混进当前待办'),
    ('life-key', '我想配一把备用钥匙，先找出了旧钥匙串。', '串上有两把外形差不多的小钥匙', '钥匙齿形和实际对应的锁', '其中一把开的是储物柜，不是家门', '家门钥匙已经标记好，备用也配好了'),
    ('life-coat', '我准备把一件闲置外套送给家里人。', '衣长合适，袖口却比对方习惯的更宽', '里面要穿的衣服和能否轻松改袖口', '只是袖口偏宽，不是整件大了一个尺码', '对方试过觉得合适，衣服已经收好'),
    ('life-weather', '我打算把今天的户外安排挪到更凉快的时候。', '预报下午降温，但同时会起一阵风', '有遮挡的位置和活动需要的东西', '活动是散步，不涉及需要固定的器材', '我改到傍晚出门，已经把路线定下了'),
    ('life-rest', '我想在周末留出一段没有安排的时间。', '临时多出一个不急的小差事', '差事的实际截止日期和可替代方案', '它可以下周再办，不需要占用这次休息', '我把差事移到下周，今天已经空出来了'),
)


def _move_examples(s):
    """All branches share a situation id, but change speech act and proposition scope."""
    key, setup, event, detail, correction, outcome = s
    history = [setup]
    # Each authored move has its own category; none is derived from held-out text.
    rows = (
        ('reply', 'oral_request', f'{event}。我现在卡在这儿了，给我支个招呗', history),
        ('reply', 'choice', f'{event}。是先核对细节还是先换个做法？你给我排个顺序', history),
        ('reply', 'emotion', f'{event}，我越琢磨越烦躁。先别给一大套道理，陪我缓一缓就好', history),
        ('reply', 'correction', f'前面有一点得重新对齐：{correction}。按这个前提再帮我想一下', history + [event]),
        ('reply', 'quoted_boundary', f'记录里那个「不必回复」是别人写给我的，不是我对你说的。{event}，我是在等你的意见', history),
        ('reply', 'irony', f'可真会挑时候啊，{event}。我说的是反话，你帮我看看下一步怎么收拾', history),
        ('reply', 'traditional', f'{event}。我還拿不準，想聽你把利弊講清楚，不是只要一句安慰', history),
        ('reply', 'mixed_language', f'{event}。Need a second opinion: 你会先检查哪一步，理由也说一下', history),
        ('reply', 'merged_envelopes', f'{event}\n我把{detail}补齐了\n就这些，接下来轮到你说说了', history),
        ('reply', 'withdrawal', f'上一条让你照旧处理的意思撤回。现在以「{correction}」为准，重新给我个做法', history + [event]),
        ('defer', 'incomplete', f'{event}，但这还只是开头，后面的经过我还在整理', history),
        ('defer', 'attachment_pending', f'我先把{event}这部分放这里，相关截图没传齐，传齐以后再一起聊', history),
        ('defer', 'two_stage', f'先把背景铺一下：{event}。现在先不接，等我发第二部分', history),
        ('defer', 'quote_not_request', f'纸上原话写着「你选哪个」。这是我在复述材料，{detail}我还要往下贴', history),
        ('defer', 'traditional', f'{event}。這段先讓我完整打出來，後面還有幾個關鍵細節，暫時別接話', history),
        ('defer', 'mixed_language', f'{event}。Hold on, more context coming，下一条才是后半段', history),
        ('defer', 'merged_envelopes', f'先记一下这个经过\n{event}\n关于{detail}还没发完，这条后面有续篇', history),
        ('defer', 'pending_comparison', f'现在只摆出第一种情况：{event}。另一种还在下一条，凑齐了再讨论', history),
        ('defer', 'organizing', f'{event}。我这会儿在把{detail}按顺序排好，排完再接着说', history),
        ('defer', 'explicit_pause', f'我想分两次发完这段，先别搭腔。第一段是{event}，第二段还在输入', history),
        ('context_only', 'settled', f'{outcome}。事情已落定，这条只是交代结果，不需要再接啦', history + [event]),
        ('context_only', 'acknowledgement', f'嗯，刚才那套做法我已经照着做完了，{outcome}，咱们先收个尾', history),
        ('context_only', 'goodbye', f'{outcome}，我去做别的事了，咱们改天聊', history),
        ('context_only', 'cancel_request', f'刚才那个待处理的小请求撤销，不用继续准备答案。{outcome}', history + [event]),
        ('context_only', 'quote_not_pause', f'那句「先等一等」只是旧笔记里的原文。现在{outcome}，这段到此结束就行', history),
        ('context_only', 'traditional', f'{outcome}。謝啦，我這邊已經處理妥當，這條不用再回覆了', history),
        ('context_only', 'mixed_language', f'{outcome}。All sorted, no follow-up needed，我先下线啦', history),
        ('context_only', 'merged_envelopes', f'收到，刚才那件事已经落地\n{outcome}\n没有新的问题，先不接话就好', history),
        ('context_only', 'topic_closed', f'{outcome}。这个话题就先封存吧，接下来我自己安排，不用再补充', history),
        ('context_only', 'gratitude_only', f'谢谢你刚刚耐心听完，{outcome}。我只是说声谢，不另起话头了', history),
    )
    for i, (label, category, current, recent) in enumerate(rows):
        yield _row(f'curriculum:{key}:{i}', key, label, category, current, recent)


def _row(identifier, family, label, category, current, recent):
    return {
        'id': identifier, 'session_id': 'curriculum-session:' + family,
        'family': 'curriculum-family:' + family, 'label': label,
        'protected': label == 'reply', 'slice': category,
        'state': {'current': current, 'recent': list(recent)},
        'provenance': {'kind': 'ai_authored_compositional_synthetic',
                       'human_labelled': False, 'purpose': 'training_only',
                       'curriculum_version': VERSION},
    }


# These paired utterances teach the encoder to read history, rather than assign a
# permanent class to a closing-looking string. They are not new acceptance cases.
COUNTERFACTUALS = (
    ('title', '到此为止。',
     '我在请你写一段四句小诗，下一条发给你的是标题，按标题直接写即可。',
     '刚刚那件事已经处理好了，我下一条只打个收尾，不需要再延伸。'),
    ('translation', '今天就这样。',
     '下一条是要翻译的中文原句，请把它翻成自然英文，而不是当成聊天结束。',
     '我准备关掉聊天去休息了，这里没有别的待回答事项。'),
    ('story', '我先走啦。',
     '我在写一个短场景，下一条是人物的第一句台词，请接着写另一人的回应。',
     '事情都说清楚了，我要离开一会儿，不需要说送别的话。'),
    ('rewrite', '收到。',
     '帮我把下一条的过于生硬的回复改得温和一点，给两种版本就好。',
     '你的步骤已经够清楚了，我这边只确认一下收到了。'),
    ('rhyme', '晚点见。',
     '我需要一句和下一条同韵脚的广告短句，下一条是参考，不是对你道别。',
     '我们这段先聊到这里，我现在去准备东西。'),
    ('explain', '不必再说了。',
     '我想知道下一条台词会给听者什么感觉，帮我分析语气，不执行那句台词。',
     '前面没有留下问题，我只是要把话题正式结束。'),
    ('punctuation', '先停一下',
     '下一条给你的短句需要加标点并解释不同标点带来的语气。',
     '我要补上一段没整理好的背景，整理完再来继续，这里先不用接。'),
    ('continuation', '后面还有一段。',
     '我们在写双人对话，下一条是我写的角色台词，你接着扮演另一个角色说一句。',
     '我正在分段贴自己的经历，还没有贴到结尾。'),
    ('metaphor', '让我想一想。',
     '下一条是我写的比喻，请给我一个保留原意但更有画面感的改写。',
     '我暂时不想听建议，等我理清自己的话，再继续发过来。'),
    ('dialogue', '别急，快讲完了。',
     '下一条是脚本中的台词，我需要你给接话角色写一句自然的回应。',
     '我还在说明同一件事的前因后果，下一条后面还有补充。'),
)


def incremental_rows(seed=321):
    rows = [row for situation in SITUATIONS for row in _move_examples(situation)]
    for key, text, reply_history, other_history in COUNTERFACTUALS:
        other = 'defer' if key in ('punctuation', 'continuation', 'metaphor', 'dialogue') else 'context_only'
        for label, recent in (('reply', reply_history), (other, other_history)):
            rows.append(_row('curriculum:scope:' + key + ':' + label, 'scope-' + key,
                             label, 'history_counterfactual', text, [recent]))
    random.Random(seed).shuffle(rows)
    return rows


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def _normalized(text):
    # Used only to detect overlap, not to create or rewrite a conversation.
    return re.sub(r'\s+', '', text).casefold()


def _state_key(row):
    return json.dumps(row['state'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _encode(rows):
    return ''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in rows).encode('utf-8')


def prepare(source, output, *, seed=321, retain_source_train=7200):
    """Create a NEW sibling data directory; retain held-out source bytes exactly.

    No threshold fitting, test prediction inspection, model import or data download
    happens in this operation. Re-running refuses to overwrite even an empty target.
    """
    source = Path(source).expanduser().resolve(strict=True)
    requested = Path(output).expanduser()
    if requested.is_symlink() or requested.exists():
        raise ValueError('output_exists; preserve every earlier data revision')
    output = requested.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('source_and_output_must_be_separate')
    if isinstance(retain_source_train, bool) or not isinstance(retain_source_train, int) or retain_source_train < 0:
        raise ValueError('invalid_retain_source_train')
    metadata_path = source / 'dataset.json'
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise ValueError('source_dataset_manifest_required')
    source_metadata_raw = metadata_path.read_bytes()
    source_metadata = json.loads(source_metadata_raw)
    declarations = source_metadata.get('splits', {})
    if (source_metadata.get('release_acceptance_eligible') is True
            or source_metadata.get('evaluation_role') == 'locked_blind_acceptance'
            or any(isinstance(value, dict) and value.get('role') == 'locked_blind_acceptance'
                   for value in declarations.values())):
        raise ValueError('source_blind_acceptance_not_allowed')
    raw = {}
    source_rows = {}
    for split in ('train', 'calibration', 'test'):
        path = source / (split + '.jsonl')
        if path.is_symlink() or not path.is_file():
            raise ValueError('invalid_source_split:' + split)
        raw[split] = path.read_bytes()
        source_rows[split] = _read_jsonl(path)
        if not source_rows[split]:
            raise ValueError('empty_source_split:' + split)
        for row in source_rows[split]:
            if row.get('label') not in ('reply', 'defer', 'context_only') or not isinstance(row.get('state', {}).get('current'), str):
                raise ValueError('invalid_source_record:' + split)
    # Make stale input manifests fatal, not a new claimed frozen source version.
    for split in raw:
        declared = source_metadata.get('splits', {}).get(split, {})
        if declared.get('sha256') != _sha(raw[split]) or declared.get('records') != len(source_rows[split]):
            raise ValueError('source_manifest_mismatch:' + split)
    additions = incremental_rows(seed)
    heldout = {_normalized(r['state']['current']) for sp in ('calibration', 'test') for r in source_rows[sp]}
    if any(_normalized(r['state']['current']) in heldout for r in additions):
        raise ValueError('curriculum_overlaps_heldout_source; never retune held-out cases')
    source_ids = {r['id'] for rows in source_rows.values() for r in rows}
    source_families = {r.get('family') for rows in source_rows.values() for r in rows}
    if any(r['id'] in source_ids or r['family'] in source_families for r in additions):
        raise ValueError('curriculum_namespace_collision')
    state_keys = [_state_key(r) for r in additions]
    if len(set(state_keys)) != len(state_keys):
        raise ValueError('duplicate_curriculum_state')
    # Repeat suppression is by the encoded state. Paired history examples are
    # deliberately the same current string with DIFFERENT user-history scope.
    rng = random.Random(seed)
    original = list(source_rows['train'])
    rng.shuffle(original)
    retained = original[:min(retain_source_train, len(original))]
    source_states = {_state_key(r) for r in retained}
    if source_states.intersection(state_keys):
        raise ValueError('curriculum_duplicates_source_train')
    rows = retained + additions
    rng.shuffle(rows)
    train_raw = _encode(rows)
    increment_raw = _encode(additions)
    # The source path is diagnostic only. Hashes, not this path, bind provenance.
    metadata = {
        'schema': 'wxbot.decision-corpus.v2', 'curriculum_version': VERSION,
        'seed': seed, 'provenance': 'AI-authored compositional synthetic; no private chats; NOT human-labelled',
        'purpose': 'development-training augmentation; retained calibration/test are seen development regression, NOT blind acceptance',
        'release_acceptance_eligible': False,
        'split_unit': 'all branches of each new situation/family/session are TRAIN ONLY; no new evaluation split',
        'source': {'directory': str(source), 'dataset_sha256': _sha(source_metadata_raw),
                   'splits': {sp: {'sha256': _sha(b), 'records': len(source_rows[sp])} for sp, b in raw.items()}},
        'increment': {'sha256': _sha(increment_raw), 'records': len(additions),
                      'authored_situations': len(SITUATIONS),
                      'discourse_moves_per_situation': 30,
                      'history_counterfactual_pairs': len(COUNTERFACTUALS),
                      'independent_natural_conversations': 0,
                      'note': 'Compositions share authored situations and discourse moves; record count is NOT independent annotation count.',
                      'labels': dict(Counter(r['label'] for r in additions)),
                      'slices': dict(Counter(r['slice'] for r in additions)),
                      'session_ids': sorted({r['session_id'] for r in additions}),
                      'families': sorted({r['family'] for r in additions}),
                      'all_nonempty_recent': all(bool(r['state']['recent']) for r in additions),
                      'multi_envelope_records': sum('\n' in r['state']['current'] for r in additions)},
        'retained_source_train_records': len(retained),
        'splits': {
            'train': {'sha256': _sha(train_raw), 'records': len(rows),
                      'families': sorted({r['family'] for r in rows})},
            **{sp: {'sha256': _sha(raw[sp]), 'records': len(source_rows[sp]),
                    'families': sorted({r['family'] for r in source_rows[sp]}),
                    'copied_byte_for_byte': True, 'role': 'seen_development_regression'} for sp in ('calibration', 'test')},
        },
        'curriculum_source_sha256': _sha(Path(__file__).read_bytes()),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.' + output.name + '-', dir=output.parent))
    try:
        (temporary / 'train.jsonl').write_bytes(train_raw)
        (temporary / 'increment.jsonl').write_bytes(increment_raw)
        for split in ('calibration', 'test'):
            (temporary / (split + '.jsonl')).write_bytes(raw[split])
        (temporary / 'source-dataset.json').write_bytes(source_metadata_raw)
        (temporary / 'dataset.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if requested.is_symlink() or requested.exists():
            raise ValueError('output_appeared_during_generation')
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    # Reopen the actual result. Source and held-out bytes are never rewritten.
    for split in ('calibration', 'test'):
        if (output / (split + '.jsonl')).read_bytes() != raw[split]:
            raise ValueError('heldout_copy_verification_failed')
    if _sha((output / 'train.jsonl').read_bytes()) != metadata['splits']['train']['sha256']:
        raise ValueError('train_copy_verification_failed')
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Existing development corpus directory, never the blind release set')
    parser.add_argument('--out', required=True, help='New external corpus revision; overwriting is rejected')
    parser.add_argument('--seed', type=int, default=321)
    parser.add_argument('--retain-source-train', type=int, default=7200)
    args = parser.parse_args(argv)
    result = prepare(args.source, args.out, seed=args.seed, retain_source_train=args.retain_source_train)
    print(json.dumps({
        'status': 'CURRICULUM_READY', 'directory': str(Path(args.out).expanduser().resolve()),
        'increment_records': result['increment']['records'], 'train_records': result['splits']['train']['records'],
        'train_sha256': result['splits']['train']['sha256'],
        'calibration_sha256': result['splits']['calibration']['sha256'],
        'test_sha256': result['splits']['test']['sha256'], 'release_acceptance_eligible': False,
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
