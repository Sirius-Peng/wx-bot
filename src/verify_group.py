"""群聊支持验证脚本 —— 逐项验证群聊实现中最容易出错的细节。

运行：
    python -m src.verify_group

验证范围：
  A. 群语义基础（群ID识别、群昵称优先级、发送者前缀剥离）
  B. @ 机器人检测（三种格式 + U+2005 分隔符陷阱）
  C. 群成员解析（缓存、失效、降级）
  D. 完整适配器归一化（两种回调格式）
  E. 去重（官方要求 appid + newMsgId）
  F. 群聊 @ 发送（ats + content 必须同时满足）
  G. 群事件触发缓存失效
"""
from __future__ import annotations

import asyncio
import time

from .adapters.gewechat import GewechatAdapter
from .core.group import (
    WX_AT_SEPARATOR,
    GroupMember,
    GroupMemberResolver,
    detect_at_bot,
    is_group_id,
    parse_sender_prefix,
    should_invalidate_members,
)
from .core.trigger import (
    MODE_MENTION_ONLY,
    MODE_SMART,
    TriggerRouter,
    default_group_config,
)
from .core.types import WxMessage
from .store_memory import MemoryStore

BOT_WXID = "wxid_bot0001"
BOT_NICK = "小助手"
GROUP_ID = "34757816141@chatroom"
ALICE = "wxid_alice001"


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


# ═══════════════════════════════════════════════════════════════════
#  A. 群语义基础
# ═══════════════════════════════════════════════════════════════════

def test_group_id() -> None:
    assert is_group_id(GROUP_ID) is True
    assert is_group_id("wxid_alice001") is False
    assert is_group_id("") is False
    _ok(f"群 ID 识别：{GROUP_ID} → 群聊；wxid_xxx → 私聊")


def test_group_nickname_priority() -> None:
    """群昵称优先于好友昵称 —— 群里展示的是群昵称。"""
    m = GroupMember(wxid="wxid_a", nickname="张三", display_name="产品-张三")
    assert m.name == "产品-张三", "应优先使用群昵称 displayName"
    m2 = GroupMember(wxid="wxid_b", nickname="李四", display_name="")
    assert m2.name == "李四", "无群昵称时回落到好友昵称"
    m3 = GroupMember(wxid="wxid_abcdef123456", nickname="", display_name="")
    assert m3.name == "wxid_abcdef123456", "都为空时回落到 wxid"
    _ok("群昵称优先级：displayName > nickName > wxid")


def test_sender_prefix() -> None:
    """旧版协议把发送者塞在 content 前缀里，必须剥离。"""
    s, c = parse_sender_prefix("wxid_alice001:\n今天天气不错")
    assert s == "wxid_alice001" and c == "今天天气不错", f"实际 ({s}, {c})"
    _ok("发送者前缀剥离：'wxid_alice001:\\n今天天气不错' → 发送者=wxid_alice001, 正文='今天天气不错'")

    # 不能误伤正常文本
    s2, c2 = parse_sender_prefix("注意:\n明天要交周报")
    assert s2 == "" and c2 == "注意:\n明天要交周报", "不应把 '注意:' 误判为发送者"
    _ok("防误伤：'注意:\\n明天要交周报' 保持原样")

    s3, c3 = parse_sender_prefix("没有冒号的消息")
    assert s3 == "" and c3 == "没有冒号的消息"
    _ok("无前缀消息保持原样")


# ═══════════════════════════════════════════════════════════════════
#  B. @ 机器人检测
# ═══════════════════════════════════════════════════════════════════

def test_at_format_1_field() -> None:
    """格式①：结构化字段 at_list（最可靠）"""
    hit, cleaned = detect_at_bot(
        content="帮我查下天气", bot_wxid=BOT_WXID,
        bot_names=[BOT_NICK], at_list=[BOT_WXID],
    )
    assert hit is True and cleaned == "帮我查下天气"
    _ok("格式① 结构化字段 at_list=[wxid_bot0001] → 命中，正文干净")

    # at 了别人，不是机器人
    hit2, _ = detect_at_bot(
        content="你好", bot_wxid=BOT_WXID, at_list=["wxid_other999"],
    )
    assert hit2 is False
    _ok("格式① at_list 里是别人 → 不命中")


def test_at_format_2_msgsource() -> None:
    """格式②：MsgSource XML 里的 <atuserlist>"""
    xml = f"<msgsource><atuserlist>{BOT_WXID},wxid_other</atuserlist></msgsource>"
    hit, cleaned = detect_at_bot(
        content="明天几点开会", bot_wxid=BOT_WXID, msg_source=xml,
    )
    assert hit is True and cleaned == "明天几点开会", f"实际 hit={hit} cleaned={cleaned!r}"
    _ok("格式② MsgSource XML <atuserlist> → 命中")

    xml2 = "<msgsource><atuserlist>wxid_other999</atuserlist></msgsource>"
    hit2, _ = detect_at_bot(content="你好", bot_wxid=BOT_WXID, msg_source=xml2)
    assert hit2 is False
    _ok("格式② XML 里是别人 → 不命中")


def test_at_format_3_text_and_u2005() -> None:
    """格式③：正文文本前缀，且微信用 U+2005 作分隔符。"""
    # 普通空格
    hit, cleaned = detect_at_bot(content=f"@{BOT_NICK} 你好", bot_wxid=BOT_WXID, bot_names=[BOT_NICK])
    assert hit is True and cleaned == "你好", f"实际 ({hit}, {cleaned!r})"
    _ok("格式③ '@小助手 你好'（普通空格）→ 命中，正文='你好'")

    # U+2005 —— 这是真实微信使用的分隔符，用普通空格 split 会失败
    text = f"@{BOT_NICK}{WX_AT_SEPARATOR}帮我算一下"
    hit2, cleaned2 = detect_at_bot(content=text, bot_wxid=BOT_WXID, bot_names=[BOT_NICK])
    assert hit2 is True, "U+2005 分隔符必须能识别"
    assert cleaned2 == "帮我算一下", f"U+2005 剥离失败，实际 {cleaned2!r}"
    _ok(f"格式③ U+2005 分隔符（\\u2005）→ 命中并正确剥离（这是最易踩的坑）")

    # @ 在中间不应被误判为 @ 机器人
    hit3, cleaned3 = detect_at_bot(
        content=f"我觉得 @{BOT_NICK} 说的对", bot_wxid=BOT_WXID, bot_names=[BOT_NICK],
    )
    # 中间 @ 也算提及，正文保留完整（不做前缀剥离）
    assert cleaned3 == f"我觉得 @{BOT_NICK} 说的对", "非前缀 @ 不应剥离正文"
    _ok("非前缀 @ 不剥离正文（避免误伤）")


def test_at_not_hit() -> None:
    hit, cleaned = detect_at_bot(content="大家晚上好", bot_wxid=BOT_WXID, bot_names=[BOT_NICK])
    assert hit is False and cleaned == "大家晚上好"
    _ok("未 @ 机器人 → 不命中，正文原样")


# ═══════════════════════════════════════════════════════════════════
#  C. 群成员解析
# ═══════════════════════════════════════════════════════════════════

async def test_resolver_cache_and_fallback() -> None:
    calls = {"n": 0}

    class FakeResolver(GroupMemberResolver):
        async def _fetch_members(self, chat_id: str):
            calls["n"] += 1
            return {
                ALICE: GroupMember(wxid=ALICE, nickname="Alice", display_name="产品-Alice"),
                "wxid_bob002": GroupMember(wxid="wxid_bob002", nickname="Bob", display_name=""),
            }

    r = FakeResolver(ttl=60)
    assert await r.resolve(GROUP_ID, ALICE) == "产品-Alice"
    assert await r.resolve(GROUP_ID, "wxid_bob002") == "Bob"
    assert calls["n"] == 1, "第二次应命中缓存，不应重复拉取"
    _ok("成员解析：群昵称优先 + 缓存生效（仅拉取 1 次）")

    # 未知成员降级
    unknown = await r.resolve(GROUP_ID, "wxid_unknown999")
    assert unknown.startswith("wxid_unknown"), f"应降级到 wxid 短形式，实际 {unknown}"
    _ok(f"未知成员降级为 wxid 短形式：{unknown}")

    # 失效后重新拉取
    r.invalidate(GROUP_ID)
    await r.resolve(GROUP_ID, ALICE)
    assert calls["n"] == 2, "失效后应重新拉取"
    _ok("invalidate() 后重新拉取成员")

    # 临时回填（从消息里顺带拿到的昵称）
    r.remember(GROUP_ID, "wxid_carol003", "Carol群昵称")
    assert await r.resolve(GROUP_ID, "wxid_carol003") == "Carol群昵称"
    _ok("remember() 回填昵称，省一次接口调用")


# ═══════════════════════════════════════════════════════════════════
#  D. 完整适配器归一化
# ═══════════════════════════════════════════════════════════════════

def _make_adapter() -> GewechatAdapter:
    a = GewechatAdapter(
        base_url="http://gw:2531", token="t", app_id="wx_app",
        bot_wxid=BOT_WXID, bot_nickname=BOT_NICK,
    )

    async def fake_api(endpoint: str, body: dict) -> dict:
        if endpoint == "group/getChatroomMemberList":
            return {"ret": 200, "data": {
                "memberList": [
                    {"wxid": ALICE, "nickName": "Alice", "displayName": "产品-Alice"},
                    {"wxid": "wxid_bob002", "nickName": "Bob", "displayName": None},
                ],
                "chatroomOwner": ALICE, "adminWxid": ["wxid_bob002"],
            }}
        if endpoint == "message/postText":
            return {"ret": 200, "data": {"newMsgId": 999, "toWxid": body.get("toWxid")}}
        return {"ret": 200, "data": {}}

    a._api = fake_api          # type: ignore[method-assign]
    return a


async def test_normalize_v2_group() -> None:
    """v2.0 扁平回调格式的群消息。"""
    a = _make_adapter()
    payload = {
        "appid": "wx_app", "wxid": BOT_WXID, "isSelf": False,
        "fromUser": ALICE, "toUser": GROUP_ID,
        "content": f"@{BOT_NICK} 帮我看看这个方案",
        "msgType": "TEXT", "newMsgId": 9154432038,
        "createTime": int(time.time()),
        "atWxidList": [BOT_WXID],
    }
    msg = await a._normalize(payload)
    assert msg is not None
    assert msg.chat_type == "group", "应识别为群聊"
    assert msg.chat_id == GROUP_ID, f"chat_id 应为群 ID，实际 {msg.chat_id}"
    assert msg.sender_id == ALICE
    assert msg.sender_name == "产品-Alice", f"应用群昵称，实际 {msg.sender_name}"
    assert msg.is_at_me is True, "@ 检测应命中"
    assert msg.content == "帮我看看这个方案", f"@ 应被剥离，实际 {msg.content!r}"
    assert msg.group_id == GROUP_ID
    _ok("v2.0 群消息归一化：群ID / 群昵称 / @命中 / 正文剥离 全部正确")


async def test_normalize_legacy_group() -> None:
    """旧版包装回调格式 + 发送者在 content 前缀。"""
    a = _make_adapter()
    payload = {
        "TypeName": "AddMsg",
        "Data": {
            "FromUserName": GROUP_ID,          # 群消息：FromUserName 是群 ID
            "ToUserName": BOT_WXID,
            "Content": f"{ALICE}:\n@{BOT_NICK}\u2005明天的会议改到几点？",
            "MsgType": 1,
            "NewMsgId": 123456789,
            "CreateTime": int(time.time()),
            "AtWxidList": [BOT_WXID],
            "IsSelf": False,
            "PushContent": "产品-Alice",
        },
    }
    msg = await a._normalize(payload)
    assert msg is not None
    assert msg.chat_type == "group"
    assert msg.sender_id == ALICE, f"应从 content 前缀解析出发送者，实际 {msg.sender_id}"
    assert msg.is_at_me is True
    assert msg.content == "明天的会议改到几点？", f"实际 {msg.content!r}"
    _ok("旧版群消息归一化：发送者前缀解析 + U+2005 剥离 + @ 命中")


async def test_normalize_private() -> None:
    a = _make_adapter()
    payload = {
        "appid": "wx_app", "isSelf": False,
        "fromUser": ALICE, "toUser": BOT_WXID,
        "content": "你好", "msgType": "TEXT", "newMsgId": 111,
        "createTime": int(time.time()),
    }
    msg = await a._normalize(payload)
    assert msg is not None and msg.chat_type == "private"
    assert msg.chat_id == ALICE
    assert msg.is_at_me is False, "私聊无需 @"
    _ok("私聊消息仍正常识别（chat_type=private）")


async def test_self_message_filtered() -> None:
    a = _make_adapter()
    payload = {
        "appid": "wx_app", "isSelf": True,
        "fromUser": BOT_WXID, "toUser": GROUP_ID,
        "content": "我是机器人自己发的", "msgType": "TEXT", "newMsgId": 222,
    }
    assert await a._normalize(payload) is None
    _ok("机器人自身消息被过滤（防回环）")


async def test_timestamp_normalization() -> None:
    """时间戳单位必须归一化 —— 否则静默时段/会话TTL 全部失效。"""
    import datetime
    a = _make_adapter()
    now_s = int(time.time())
    now_ms = now_s * 1000

    # 秒 → 毫秒
    assert GewechatAdapter._to_ms(now_s) == now_ms, "秒级时间戳应 ×1000"
    # 毫秒保持不变
    assert GewechatAdapter._to_ms(now_ms) == now_ms, "毫秒级时间戳不应再乘"
    # 异常输入回落到当前时间
    assert GewechatAdapter._to_ms(None) > now_ms - 5000
    assert GewechatAdapter._to_ms(0) > now_ms - 5000
    _ok("时间戳归一化：秒 ×1000 / 毫秒保持 / 异常回落当前时间")

    # 端到端：归一化后小时数必须正确（这是真实踩过的坑）
    payload = {
        "appid": "wx_app", "isSelf": False, "fromUser": ALICE, "toUser": GROUP_ID,
        "content": "你好", "msgType": "TEXT", "newMsgId": 333,
        "createTime": now_s,           # GeWe 回调给的是秒
    }
    msg = await a._normalize(payload)
    assert msg is not None
    hour = datetime.datetime.fromtimestamp(msg.timestamp / 1000).hour
    expected = datetime.datetime.now().hour
    assert hour == expected, f"小时数应还原为当前小时 {expected}，实际 {hour}（会误触发静默时段）"
    _ok(f"端到端时间还原正确：hour={hour}（不再是 1970 年的 hour=1）")


# ═══════════════════════════════════════════════════════════════════
#  E. 去重
# ═══════════════════════════════════════════════════════════════════

async def test_dedup() -> None:
    a = _make_adapter()
    payload = {"appid": "wx_app", "Data": {"NewMsgId": 555}}
    assert a._dedup(payload) is True, "首次应通过"
    assert a._dedup(payload) is False, "重复应被拦截"
    _ok("去重：同一 newMsgId 第二次被拦截（官方要求的 appid+newMsgId 组合）")

    payload2 = {"appid": "wx_app", "Data": {"NewMsgId": 556}}
    assert a._dedup(payload2) is True
    _ok("不同 newMsgId 正常通过")


# ═══════════════════════════════════════════════════════════════════
#  F. 群聊 @ 发送
# ═══════════════════════════════════════════════════════════════════

async def test_group_send_with_at() -> None:
    """官方约束：ats 传 wxid，同时 content 必须包含 @昵称，缺一不可。"""
    a = _make_adapter()
    captured = {}

    async def fake_api(endpoint: str, body: dict) -> dict:
        if endpoint == "group/getChatroomMemberList":
            return {"ret": 200, "data": {
                "memberList": [{"wxid": ALICE, "nickName": "Alice", "displayName": "产品-Alice"}],
                "chatroomOwner": ALICE, "adminWxid": [],
            }}
        captured.update(body)
        return {"ret": 200, "data": {"newMsgId": 777}}

    a._api = fake_api          # type: ignore[method-assign]

    await a.send_text(GROUP_ID, "这个问题我看下", at=[ALICE])

    assert captured["toWxid"] == GROUP_ID
    assert captured["ats"] == ALICE, f"ats 应为 wxid，实际 {captured.get('ats')}"
    assert "@产品-Alice" in captured["content"], \
        f"content 必须包含 @群昵称，实际 {captured['content']!r}"
    _ok(f"群聊 @ 发送：ats={captured['ats']} + content 含 @群昵称 → {captured['content']!r}")

    # 私聊不应带 ats
    captured.clear()
    await a.send_text(ALICE, "私聊回复", at=[ALICE])
    assert "ats" not in captured, "私聊不应传 ats"
    _ok("私聊发送不带 ats 参数")


# ═══════════════════════════════════════════════════════════════════
#  G. 群事件 → 缓存失效
# ═══════════════════════════════════════════════════════════════════

def test_group_events() -> None:
    for ev in ["GROUP_INVITE", "REMOVE_FROM_GROUP", "GROUP_DISMISS",
               "GROUP_NAME_CHANGE", "SYSTEM"]:
        assert should_invalidate_members(ev) is True, f"{ev} 应触发成员缓存失效"
    assert should_invalidate_members("TEXT") is False
    _ok("群成员变动事件（入群/退群/解散/改名）触发缓存失效；普通文本不触发")


async def test_event_invalidates_cache() -> None:
    a = _make_adapter()
    await a._normalize({
        "appid": "wx_app", "isSelf": False, "fromUser": ALICE, "toUser": GROUP_ID,
        "content": "你好", "msgType": "TEXT", "newMsgId": 1,
    })
    assert GROUP_ID in a.members._cache, "首次应建立缓存"
    await a._normalize({
        "appid": "wx_app", "isSelf": False, "fromUser": ALICE, "toUser": GROUP_ID,
        "content": "欢迎新成员", "msgType": "GROUP_INVITE", "newMsgId": 2,
        "typeName": "GROUP_INVITE",
    })
    assert GROUP_ID not in a.members._cache, "群事件后缓存应被清除"
    _ok("收到 GROUP_INVITE 后成员缓存被自动清除（避免昵称长期错误）")


# ═══════════════════════════════════════════════════════════════════
#  H. 触发策略：私聊一律回复 / 群聊仅 @ 回复
# ═══════════════════════════════════════════════════════════════════

class _AlwaysYesJudge:
    """判定模型恒返回 YES —— 用来验证策略本身，而非模型判断。"""
    async def judge(self, msg, recent, persona):
        return True


def _gmsg(content: str, at_bot: bool = False) -> WxMessage:
    return WxMessage(
        msg_id=f"g{abs(hash(content)) % 10**8}", platform="gewechat",
        chat_type="group", chat_id=GROUP_ID,
        sender_id=ALICE, sender_name="产品-Alice", bot_id=BOT_WXID,
        content=content, is_at_me=at_bot, timestamp=int(time.time() * 1000),
    )


def _pmsg(content: str) -> WxMessage:
    return WxMessage(
        msg_id=f"p{abs(hash(content)) % 10**8}", platform="gewechat",
        chat_type="private", chat_id=ALICE,
        sender_id=ALICE, sender_name="Alice", bot_id=BOT_WXID,
        content=content, is_at_me=False, timestamp=int(time.time() * 1000),
    )


async def _decide(mode: str, msg: WxMessage, cfg=None, store=None):
    store = store or MemoryStore()
    store.set_default_mode(mode)
    r = TriggerRouter(store, _AlwaysYesJudge(), default_mode=mode)
    return await r.decide(msg, [], cfg)


async def test_private_always_replies() -> None:
    """私聊：一律回复，与群聊策略无关。"""
    for mode in (MODE_MENTION_ONLY, MODE_SMART):
        d = await _decide(mode, _pmsg("随便说点什么"))
        assert d.should_reply is True, f"{mode} 模式下私聊应回复"
    _ok("私聊一律回复（两种策略下均成立）")


async def test_mention_only_mode() -> None:
    """群聊 + 仅 @ 策略：只有 @ 才回。"""
    cfg = default_group_config(GROUP_ID, MODE_MENTION_ONLY)

    d = await _decide(MODE_MENTION_ONLY, _gmsg("你好", at_bot=True), cfg)
    assert d.should_reply is True and d.layer == "L0", f"@ 应回复，实际 {d}"
    _ok("群内 @ 机器人 → 回复")

    cases = [
        ("明天几点开会？", "含疑问词"),
        ("小助手这个功能不错", "提到唤醒词"),
        ("哈哈哈哈哈哈", "纯闲聊"),
        ("帮我看看这个报错", "含求助词"),
    ]
    for text, label in cases:
        d = await _decide(MODE_MENTION_ONLY, _gmsg(text), cfg)
        assert d.should_reply is False, f"{label} 未 @ 不应回复，实际 {d}"
    _ok("群内未 @ → 全部不回复（含疑问词/唤醒词/求助词/闲聊）")


async def test_smart_mode_still_works() -> None:
    """群聊 + 智能策略：未 @ 也可能被漏斗捞起来。"""
    cfg = default_group_config(GROUP_ID, MODE_SMART)
    cfg.cooldown_sec = 0
    cfg.hourly_limit = 1000
    cfg.reply_prob = 1.0
    cfg.quiet_hours = []

    d = await _decide(MODE_SMART, _gmsg("你好", at_bot=True), cfg)
    assert d.should_reply is True
    _ok("智能模式：@ 机器人 → 回复")

    # 注意：消息要够长才能拿到 L2 的长度加分（阈值 40 分），
    # 「明天几点开会？」只有 7 字，拿不到长度分会被 L2 拦下。
    d = await _decide(MODE_SMART, _gmsg("明天下午的评审会议改到几点了？"), cfg)
    assert d.should_reply is True, f"含疑问词应被漏斗放行，实际 {d}"
    _ok(f"智能模式：未 @ 但含疑问词 → 回复（走 {d.layer} 层）")

    d = await _decide(MODE_SMART, _gmsg("哈哈哈哈哈哈"), cfg)
    assert d.should_reply is False
    _ok("智能模式：纯闲聊 → 仍沉默")


async def test_switches_and_limits() -> None:
    """安全开关与限流对所有回复路径都生效。"""
    cfg = default_group_config(GROUP_ID, MODE_MENTION_ONLY)
    cfg.enabled = False
    d = await _decide(MODE_MENTION_ONLY, _gmsg("你好", at_bot=True), cfg)
    assert d.should_reply is False and "未开启" in d.reason
    _ok("群开关关闭 → 即使 @ 也不回复")

    # 冷却生效时 @ 也不回
    store = MemoryStore()
    store.set_default_mode(MODE_MENTION_ONLY)
    cfg2 = default_group_config(GROUP_ID, MODE_MENTION_ONLY)
    cfg2.cooldown_sec = 60
    await store.set_cooldown(GROUP_ID, 60)
    d = await _decide(MODE_MENTION_ONLY, _gmsg("你好", at_bot=True), cfg2, store)
    assert d.should_reply is False and d.layer == "L4", f"冷却中应拒绝，实际 {d}"
    _ok("冷却生效中 → 即使 @ 也拒绝（防止被连续 @ 刷屏）")

    # 冷却设为 0 表示不限速
    store2 = MemoryStore()
    cfg3 = default_group_config(GROUP_ID, MODE_MENTION_ONLY)
    assert cfg3.cooldown_sec == 0, "仅 @ 模式下默认不应限速"
    d = await _decide(MODE_MENTION_ONLY, _gmsg("你好", at_bot=True), cfg3, store2)
    assert d.should_reply is True
    _ok("仅 @ 模式默认不限速（cooldown_sec=0），@ 了必回")


async def test_self_message_filtered_in_trigger() -> None:
    """机器人自己的消息不会被判定为需要回复。"""
    m = _gmsg("我是机器人")
    m.sender_id = BOT_WXID
    d = await _decide(MODE_MENTION_ONLY, m)
    assert d.should_reply is False
    _ok("机器人自身消息被过滤（防回环）")


# ═══════════════════════════════════════════════════════════════════

async def main() -> None:
    print("=" * 70)
    print("群聊支持验证")
    print("=" * 70)

    print("\n[A] 群语义基础")
    test_group_id()
    test_group_nickname_priority()
    test_sender_prefix()

    print("\n[B] @ 机器人检测（三种格式）")
    test_at_format_1_field()
    test_at_format_2_msgsource()
    test_at_format_3_text_and_u2005()
    test_at_not_hit()

    print("\n[C] 群成员解析")
    await test_resolver_cache_and_fallback()

    print("\n[D] 完整适配器归一化")
    await test_normalize_v2_group()
    await test_normalize_legacy_group()
    await test_normalize_private()
    await test_self_message_filtered()
    await test_timestamp_normalization()

    print("\n[E] 去重")
    await test_dedup()

    print("\n[F] 群聊 @ 发送")
    await test_group_send_with_at()

    print("\n[G] 群事件 → 缓存失效")
    test_group_events()
    await test_event_invalidates_cache()

    print("\n[H] 触发策略：私聊一律回复 / 群聊仅 @ 回复")
    await test_private_always_replies()
    await test_mention_only_mode()
    await test_smart_mode_still_works()
    await test_switches_and_limits()
    await test_self_message_filtered_in_trigger()

    print("\n" + "=" * 70)
    print("全部群聊验证通过")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
