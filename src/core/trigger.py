"""回复判定 —— 决定一条消息要不要回。

═══════════════════════════════════════════════════════════════════
规则只有两条，刻意保持简单：

  私聊  →  一律回复
  群聊  →  只有 @ 了机器人才回复，其余一概不回

为什么不做"智能插话"？
  那套启发式打分 + 小模型判定的漏斗看似聪明，实际有三个问题：
    ① 用户 @ 了却因为分数/概率没被回复，会直接认为"机器人坏了"；
    ② 判定要额外调一次模型，既慢又费 token；
    ③ 行为不可预测，用户无法解释"为什么这条回了那条没回"。
  所以只保留最符合直觉的规则：**@ 了必回，没 @ 就不插话**。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TriggerDecision:
    should_reply: bool
    layer: str            # 决策来源：private / group / skip
    reason: str


class TriggerRouter:
    """回复判定器。无外部依赖，纯函数式判断。"""

    def __init__(self, bot_name: str = ""):
        self.bot_name = bot_name

    async def decide(self, msg) -> TriggerDecision:
        # 自己发的消息不回（防回环）
        if msg.sender_id and msg.bot_id and msg.sender_id == msg.bot_id:
            return TriggerDecision(False, "skip", "机器人自身消息")

        # 私聊：一律回复
        if not msg.is_group:
            return TriggerDecision(True, "private", "私聊默认回复")

        # 群聊：只有 @ 了机器人才回复
        if msg.is_at_me:
            return TriggerDecision(True, "group", "@了机器人")

        return TriggerDecision(False, "group", "群里未 @ 机器人")
