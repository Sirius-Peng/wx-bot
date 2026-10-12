"""端到端演示：不依赖任何外部服务，用内存存储 + 假适配器跑通完整链路。

运行：
    python -m src.demo

会模拟群聊里的 6 条消息，打印每条消息的触发判定结果与最终回复。
"""
from __future__ import annotations

import asyncio
import logging

from .core.context import ContextManager
from .core.llm import JudgeLLM, LLMGateway, ProviderCfg
from .core.sender import HumanLikeSender
from .core.trigger import GroupConfig, TriggerRouter
from .core.types import WxMessage
from .orchestrator import Orchestrator
from .store_memory import MemoryStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

BOT_ID = "wxid_bot"
GROUP_ID = "12345@chatroom"


class FakeAdapter:
    """假适配器：把发送的消息打印出来，不真的连微信。"""

    platform = "fake"

    def __init__(self):
        self._handler = None

    async def start(self): ...
    async def stop(self): ...
    def on_message(self, h): self._handler = h

    async def send_text(self, chat_id, text, at=None):
        print(f"    🤖 [回复 {chat_id}] {text}")
        return "fake_msg_id"

    async def send_image(self, chat_id, p): return "fake"
    async def get_group_member(self, chat_id, user_id): return {"nick": user_id}
    async def is_healthy(self): return True


class FakeLLM:
    """假的对话模型：根据消息内容返回固定话术。"""

    async def stream(self, messages, chat_id="", cfg=None):
        user = messages[-1]["content"]
        if "天气" in user:
            reply = "今天多云转晴，气温 18-26 度，适合出门。记得带件外套，早晚有点凉。"
        elif "报错" in user or "bug" in user.lower():
            reply = "听起来像是依赖版本冲突。先贴一下完整的报错堆栈和 requirements，我帮你看。"
        else:
            reply = f"收到，关于「{user[:20]}」我看看。"
        for ch in reply:
            yield ch
            await asyncio.sleep(0)


def make_msg(content: str, sender: str = "张三", at_me: bool = False, ts: int = 0) -> WxMessage:
    import time
    return WxMessage(
        msg_id=f"m{ts}{hash(content) & 0xffff}",
        platform="fake", chat_type="group", chat_id=GROUP_ID,
        sender_id=f"wxid_{sender}", sender_name=sender, bot_id=BOT_ID,
        content=content, raw_content=content, mentions=[BOT_ID] if at_me else [],
        timestamp=ts or int(time.time() * 1000), is_at_me=at_me,
    )


async def main():
    store = MemoryStore()
    # 本演示专门展示「智能插话」模式（五层漏斗）
    store.set_default_mode("smart")
    store.set_group_config(GroupConfig(
        chat_id=GROUP_ID, mode="smart", reply_prob=1.0,
        cooldown_sec=0, quiet_hours=[],     # 演示用：关闭冷却与静默时段
    ))

    adapter = FakeAdapter()
    judge_gateway = LLMGateway(ProviderCfg("judge", "http://x", "k", "glm-4.7-flash"))

    # 演示用的判定器：含疑问词就插话（真实实现走小模型）
    class DemoJudge(JudgeLLM):
        async def judge(self, msg, recent, persona):
            hit = any(w in msg.content for w in ["怎么", "如何", "为什么", "吗", "？", "?"])
            print(f"    [L3 判定] {'YES' if hit else 'NO'} ← {msg.content[:24]}")
            return hit

    judge = DemoJudge(judge_gateway)

    class DemoGateway:
        """包一层，让 stream 走 FakeLLM。"""
        def __init__(self): self.fake = FakeLLM()
        def stream(self, messages, chat_id="", cfg=None):
            return self.fake.stream(messages, chat_id, cfg)

    class FastSender(HumanLikeSender):
        """演示用：跳过拟人化延迟，真实环境请使用父类的默认延迟。"""
        @staticmethod
        def human_delay(text: str) -> float:
            return 0.0

    orch = Orchestrator(
        adapter=adapter, store=store, llm=DemoGateway(), judge=judge,
        ctx=ContextManager(store),
        router=TriggerRouter(store, judge, "友好的群聊助手", default_mode="smart"),
        sender=FastSender(adapter, global_rpm=600, chat_rpm=600),
    )

    scenarios = [
        (make_msg("今天中午吃什么？", "李四"), "闲聊提问（消息过短）→ L2 拦下"),
        (make_msg("哈哈哈哈哈哈", "王五"), "纯情绪 → L2 打负分拦下"),
        (make_msg("这个接口怎么老是报错啊", "张三"), "求助 + 疑问 → 通过漏斗回复"),
        (make_msg("小助手 帮我看下天气", "李四"), "命中唤醒词 → L0 必回"),
        (make_msg("好的收到", "王五"), "短确认 → 沉默"),
        (make_msg("大家看下这个方案有问题吗？", "张三", at_me=True), "@机器人 → L0 必回"),
    ]

    print("=" * 70)
    for msg, desc in scenarios:
        print(f"\n【{desc}】{msg.sender_name}: {msg.content}")
        await orch.handle(msg)
        await asyncio.sleep(0.1)
    print("\n" + "=" * 70)
    print(f"归档消息数：{len(store._archive)}")


if __name__ == "__main__":
    asyncio.run(main())
