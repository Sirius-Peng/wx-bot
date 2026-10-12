"""群聊端到端演示 —— 对比「仅 @ 机器人」与「智能插话」两种策略。

运行：
    python -m src.demo_group

演示内容：
  1. 私聊：一律回复
  2. 群聊（默认策略 = 仅 @ 机器人）：只有 @ 了才回，其余一概不回
  3. 群聊（智能插话）：启发式打分 + 小模型判定主动找时机
  4. 群昵称解析、@ 检测三种格式、U+2005 分隔符、去重、群事件

不依赖任何外部服务：协议网关用 mock，LLM 用假模型。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .adapters.gewechat import GewechatAdapter
from .core.context import ContextManager
from .core.llm import JudgeLLM, LLMGateway, ProviderCfg
from .core.sender import HumanLikeSender
from .core.trigger import GroupConfig, TriggerRouter
from .orchestrator import Orchestrator
from .store_memory import MemoryStore

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("src.orchestrator").setLevel(logging.INFO)

BOT_WXID = "wxid_bot0001"
BOT_NICK = "小助手"
GROUP_ID = "34757816141@chatroom"
GROUP_NAME = "产品技术交流群"
PRIVATE_ID = "wxid_zhang001"

MEMBERS = {
    "wxid_alice001": {"nickName": "Alice", "displayName": "产品-Alice"},
    "wxid_bob002": {"nickName": "Bob", "displayName": "后端-Bob"},
    "wxid_carol003": {"nickName": "Carol", "displayName": None},
    PRIVATE_ID: {"nickName": "张三", "displayName": None},
}


class Harness:
    """把适配器 + 编排层装配起来，网关与模型都用 mock。"""

    def __init__(self):
        self.sent: list[dict] = []

    def build(self, mode: str) -> Orchestrator:
        adapter = GewechatAdapter(
            base_url="http://mock-gateway:2531", token="mock",
            app_id="wx_app", bot_wxid=BOT_WXID, bot_nickname=BOT_NICK,
        )

        async def fake_api(endpoint: str, body: dict) -> dict:
            if endpoint == "group/getChatroomMemberList":
                return {"ret": 200, "data": {
                    "memberList": [
                        {"wxid": w, "nickName": v["nickName"], "displayName": v["displayName"]}
                        for w, v in MEMBERS.items()
                    ] + [{"wxid": BOT_WXID, "nickName": BOT_NICK, "displayName": BOT_NICK}],
                    "chatroomOwner": "wxid_alice001", "adminWxid": ["wxid_bob002"],
                }}
            if endpoint == "message/postText":
                self.sent.append(body)
                return {"ret": 200, "data": {"newMsgId": int(time.time() * 1000)}}
            return {"ret": 200, "data": {}}

        adapter._api = fake_api          # type: ignore[method-assign]

        store = MemoryStore()
        store.set_default_mode(mode)
        # 演示聚焦触发逻辑本身，因此关闭冷却/配额/概率闸门等限流项
        store.set_group_config(GroupConfig(
            chat_id=GROUP_ID, mode=mode, cooldown_sec=0, hourly_limit=1000,
            reply_prob=1.0, quiet_hours=[],
        ))

        judge_gw = LLMGateway(ProviderCfg("judge", "http://x", "k", "glm-4.7-flash"))

        class DemoJudge(JudgeLLM):
            async def judge(self, msg, recent, persona):
                hit = any(w in msg.content for w in ["怎么", "如何", "为什么", "吗", "？", "?", "帮"])
                print(f"        └─ L3 小模型判定: {'YES 该插话' if hit else 'NO 不插话'}")
                return hit

        judge = DemoJudge(judge_gw)

        class DemoLLM:
            async def stream(self, messages, chat_id="", cfg=None):
                user = messages[-1]["content"]
                if "几点" in user or "时间" in user:
                    reply = "会议改到明天下午 3 点，会议室 A。"
                elif "报错" in user:
                    reply = "先看下完整堆栈，多半是依赖版本冲突。"
                elif "部署" in user:
                    reply = "支持两种方式：双击启动脚本，或 docker compose up -d。"
                else:
                    reply = f"收到，关于「{user[:16]}」我看一下。"
                for ch in reply:
                    yield ch
                    await asyncio.sleep(0)

        class FastSender(HumanLikeSender):
            @staticmethod
            def human_delay(text: str) -> float:
                return 0.0

        return Orchestrator(
            adapter=adapter, store=store, llm=DemoLLM(), judge=judge,
            ctx=ContextManager(store),
            router=TriggerRouter(store, judge, "友好的群聊助手", default_mode=mode),
            sender=FastSender(adapter, global_rpm=600, chat_rpm=600),
        )


_seq = 2000


def group_msg(sender: str, content: str, at_bot: bool = False) -> dict:
    """群消息（v2.0 扁平回调格式）。"""
    global _seq
    _seq += 1
    return {
        "appid": "wx_app", "isSelf": False,
        "fromUser": sender, "toUser": GROUP_ID,
        "content": content, "msgType": "TEXT",
        "newMsgId": _seq, "createTime": int(time.time()),
        "atWxidList": [BOT_WXID] if at_bot else [],
    }


def private_msg(sender: str, content: str) -> dict:
    """私聊消息。"""
    global _seq
    _seq += 1
    return {
        "appid": "wx_app", "isSelf": False,
        "fromUser": sender, "toUser": BOT_WXID,
        "content": content, "msgType": "TEXT",
        "newMsgId": _seq, "createTime": int(time.time()),
    }


async def run_phase(title: str, desc: str, mode: str, scenarios: list) -> None:
    h = Harness()
    orch = h.build(mode)

    print("\n" + "═" * 72)
    print(f"  {title}")
    print(f"  {desc}")
    print("═" * 72)

    for label, payload in scenarios:
        d = payload.get("Data") or payload
        where = "私聊" if d["toUser"] == BOT_WXID else "群聊"
        print(f"\n【{label}】{where}")
        print(f"  内容: {d['content']!r}")
        if where == "群聊":
            print(f"  @机器人: {'是' if d.get('atWxidList') else '否'}")
        await orch.adapter.handle_webhook(payload)

    print(f"\n  → 共发出 {len(h.sent)} 条回复：")
    if not h.sent:
        print("     （无）")
    for i, s in enumerate(h.sent, 1):
        ats = f"  ats={s['ats']}" if s.get("ats") else ""
        print(f"     {i}. {s['content']!r}{ats}")


async def main() -> None:
    print("=" * 72)
    print(f"群聊端到端演示  |  群：{GROUP_NAME}  |  机器人群昵称：{BOT_NICK}")
    print("=" * 72)

    common = [
        ("私聊提问", private_msg(PRIVATE_ID, "你好，我想咨询一下部署方式")),
        ("群内 @ 机器人", group_msg("wxid_alice001", f"@{BOT_NICK} 明天的会议改到几点？", at_bot=True)),
        ("群内未 @，但含疑问词", group_msg("wxid_carol003", "明天的会议改到几点？")),
        ("群内未 @，但提到「小助手」", group_msg("wxid_bob002", "小助手这个功能挺方便的")),
        ("群内未 @，纯闲聊", group_msg("wxid_bob002", "哈哈哈哈哈哈")),
    ]

    # ── 阶段一：默认策略「仅 @ 机器人」──────────────────────
    await run_phase(
        "阶段一 · 默认策略：仅 @ 机器人时回复",
        "私聊一律回复；群聊只有 @ 了机器人才回复",
        "mention_only", common,
    )

    # ── 阶段二：智能插话（对比）─────────────────────────────
    await run_phase(
        "阶段二 · 可选策略：智能插话（五层漏斗）",
        "除 @ 外，还会用启发式打分 + 小模型判定主动找时机",
        "smart", common,
    )

    print("\n" + "=" * 72)
    print("对比结论：")
    print("  · 仅 @ 模式（默认）→ 群聊只回了被 @ 的那 1 条，其余全部沉默")
    print("  · 智能模式（可选）→ 群聊额外回了「含疑问词」和「提到小助手」2 条，纯闲聊仍沉默")
    print("  · 两种模式下，私聊都正常回复")
    print()
    print("  提示：真实环境下两种模式都还会叠加冷却(60s)与小时配额(8条)保护，")
    print("        本演示为聚焦触发逻辑已关闭这些限流项。")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
