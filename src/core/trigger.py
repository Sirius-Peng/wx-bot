"""群聊触发判定。

═══════════════════════════════════════════════════════════════════
两种群聊策略（由 GroupConfig.mode 决定）：

  mention_only（默认）—— 仅 @ 机器人时回复
      私聊：一律回复
      群聊：只有 @ 了机器人才回复，其余消息一概不回
      不启用启发式打分、小模型判定、唤醒词

  smart —— 智能插话（五层漏斗）
      除 @ 外，还会用启发式打分 + 小模型判定主动找时机插话，
      最终约 1~2% 的群消息会被回复

═══════════════════════════════════════════════════════════════════
smart 模式的五层漏斗：

    L0 硬规则   命中即回复（@ / 唤醒词 / 引用机器人）
    L1 黑名单   命中即丢弃（开关 / 静默时段 / 非文本）
    L2 启发式   打分，低于阈值丢弃（拦掉约 80%）
    L3 小模型   免费模型二分类（约 20% 进入，超时视为不回复）
    L4 冷却配额 限流 + 概率闸门（最终约 1-2% 被回复）
"""
from __future__ import annotations

import re
import random
from dataclasses import dataclass, field
from typing import Optional

from .types import WxMessage

QUESTION_WORDS = ["吗", "呢", "怎么", "如何", "为什么", "为啥", "哪个", "什么时候",
                  "能不能", "可以吗", "有没有", "是不是", "?", "？"]
HELP_WORDS = ["求助", "请问", "有人知道", "谁知道", "帮忙", "急", "在线等"]
NOISE_PATTERNS = [r"^[哈呵嘿嘻\s]+$", r"^\[.*\]$", r"^https?://\S+$", r"^\d+$"]

L2_THRESHOLD = 40          # 启发式得分阈值，低于则直接丢弃

# 群聊策略
MODE_MENTION_ONLY = "mention_only"   # 仅 @ 机器人（默认）
MODE_SMART = "smart"                 # 智能插话（五层漏斗）
MODE_KEYWORD_ONLY = "keyword_only"   # 仅关键词

VALID_MODES = {MODE_MENTION_ONLY, MODE_SMART, MODE_KEYWORD_ONLY}


@dataclass
class TriggerDecision:
    should_reply: bool
    layer: str            # 决策来源层：L0/L1/L2/L3/L4
    reason: str
    score: int = 0


@dataclass
class GroupConfig:
    chat_id: str
    enabled: bool = True
    mode: str = MODE_MENTION_ONLY
    wake_words: list[str] = field(default_factory=lambda: ["小助手"])
    cooldown_sec: int = 0
    hourly_limit: int = 60
    reply_prob: float = 1.0
    quiet_hours: list[int] = field(default_factory=list)   # 空 = 不设静默时段


def default_group_config(chat_id: str, mode: str = MODE_MENTION_ONLY) -> GroupConfig:
    """按策略生成合理的默认参数。

    「仅 @」模式下默认不做任何限速与概率控制 —— 用户 @ 了就该回，
    否则会出现"我 @ 了它却不理我"的困惑。想限速可自行在界面里设置。
    """
    if mode == MODE_SMART:
        return GroupConfig(
            chat_id=chat_id, mode=MODE_SMART, wake_words=["小助手"],
            cooldown_sec=60, hourly_limit=8, reply_prob=0.7,
            quiet_hours=[0, 1, 2, 3, 4, 5, 6],
        )
    return GroupConfig(
        chat_id=chat_id, mode=mode, wake_words=["小助手"],
        cooldown_sec=0, hourly_limit=60, reply_prob=1.0, quiet_hours=[],
    )


class TriggerRouter:
    """触发判定器。所有外部依赖（存储 / LLM 判定）通过构造注入，便于测试。"""

    def __init__(self, store, judge_llm, persona: str = "",
                 default_mode: str = MODE_MENTION_ONLY):
        self.store = store          # 提供 cooldown / quota 读写
        self.judge_llm = judge_llm  # 小模型判定器，需实现 judge(msg, recent, persona) -> bool
        self.persona = persona
        # 未单独配置的群使用这个策略
        self.default_mode = default_mode if default_mode in VALID_MODES else MODE_MENTION_ONLY

    async def decide(self, msg: WxMessage, recent: list[WxMessage],
                     cfg: Optional[GroupConfig] = None) -> TriggerDecision:
        # ═══ 私聊：一律回复 ═══════════════════════════════════════
        if not msg.is_group:
            return TriggerDecision(True, "private", "私聊默认回复")

        cfg = cfg or default_group_config(msg.chat_id, self.default_mode)

        # ── 安全开关：任何模式都先生效 ──────────────────────────
        if not cfg.enabled:
            return TriggerDecision(False, "L1", "群未开启机器人")
        if msg.sender_id == msg.bot_id:
            return TriggerDecision(False, "L1", "机器人自身消息")

        # ═══ L0：@ 机器人 —— 最高优先级，两种模式都必回 ═════════
        if msg.is_at_me:
            blocked = await self._rate_limited(msg, cfg)
            return blocked or TriggerDecision(True, "L0", "@了机器人")

        # ═══ 仅 @ 模式：非 @ 消息一律不回复，到此为止 ═══════════
        if cfg.mode == MODE_MENTION_ONLY:
            return TriggerDecision(False, "L0", "群策略=仅响应@机器人")

        # ── 非文本消息（智能模式下也不处理）────────────────────
        if msg.msg_type != "text":
            return TriggerDecision(False, "L1", f"非文本消息({msg.msg_type})")

        # ═══ 以下为 smart 模式的五层漏斗 ═══════════════════════
        # 仅关键词模式：只认唤醒词
        if cfg.mode == MODE_KEYWORD_ONLY:
            if any(w and w in msg.content for w in cfg.wake_words):
                blocked = await self._rate_limited(msg, cfg)
                return blocked or TriggerDecision(True, "L0", "命中关键词")
            return TriggerDecision(False, "L0", "群策略=仅响应关键词")

        # 引用机器人消息 → 视为对话延续
        if msg.quote_msg_id and self._is_bot_msg(msg.quote_msg_id):
            blocked = await self._rate_limited(msg, cfg)
            return blocked or TriggerDecision(True, "L0", "引用机器人消息")
        # 唤醒词
        if any(w and w in msg.content for w in cfg.wake_words):
            blocked = await self._rate_limited(msg, cfg)
            return blocked or TriggerDecision(True, "L0", "命中唤醒词")
        # 静默时段
        if self._in_quiet_hours(msg, cfg):
            return TriggerDecision(False, "L1", "静默时段")

        # ── L2 启发式打分 ─────────────────────────────────────
        score = self._heuristic_score(msg, recent)
        if score < L2_THRESHOLD:
            return TriggerDecision(False, "L2", f"启发式得分{score}低于阈值", score)

        # ── L3 小模型判定（超时视为不回复，保守） ───────────────
        try:
            ok = await self.judge_llm.judge(msg, recent, self.persona)
        except Exception:
            return TriggerDecision(False, "L3", "判定模型超时/异常，保守不回复", score)
        if not ok:
            return TriggerDecision(False, "L3", "模型判定不插话", score)

        # ── L4 冷却与配额 + 概率闸门 ──────────────────────────
        blocked = await self._rate_limited(msg, cfg, score)
        if blocked:
            return blocked
        if random.random() > cfg.reply_prob:
            return TriggerDecision(False, "L4", "概率闸门未通过", score)

        return TriggerDecision(True, "L4", "通过全部漏斗", score)

    # ── 内部方法 ──────────────────────────────────────────────

    async def _rate_limited(self, msg: WxMessage, cfg: GroupConfig,
                            score: int = 0) -> Optional[TriggerDecision]:
        """统一限流检查。返回 None 表示未触发限流。

        @ 回复、唤醒词、引用、关键词、漏斗放行 —— 所有"要回复"的路径
        都必须过这一关，否则某个入口绕过去就会变成刷屏漏洞。
        """
        if await self.store.in_cooldown(msg.chat_id):
            return TriggerDecision(False, "L4", "群冷却中", score)
        if await self.store.over_hourly_quota(msg.chat_id, cfg.hourly_limit):
            return TriggerDecision(False, "L4", "小时配额已满", score)
        return None

    def _heuristic_score(self, msg: WxMessage, recent: list[WxMessage]) -> int:
        s, text = 0, msg.content.strip()

        if any(w in text for w in QUESTION_WORDS):
            s += 30
        if any(w in text for w in HELP_WORDS):
            s += 20
        if 10 <= len(text) <= 200:
            s += 15
        if any(re.match(p, text) for p in NOISE_PATTERNS):
            s -= 100

        # 最近 3 条内有机器人发言 → 正在对话中，追问概率高
        recent = [m for m in recent if isinstance(m, WxMessage)]
        if any(m.sender_id == msg.bot_id for m in recent[-3:]):
            s += 25

        # 软呼叫：消息里出现机器人名字（未用 @）
        if "小助手" in text or "bot" in text.lower():
            s += 40

        return s

    def _in_quiet_hours(self, msg: WxMessage, cfg: GroupConfig) -> bool:
        import datetime
        hour = datetime.datetime.fromtimestamp(msg.timestamp / 1000).hour
        return hour in cfg.quiet_hours

    def _is_bot_msg(self, msg_id: str) -> bool:
        # 实际实现查 Redis/DB 的发送记录
        return False
