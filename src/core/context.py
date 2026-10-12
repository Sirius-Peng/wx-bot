"""上下文管理 —— 四层上下文结构 + 滚动摘要压缩。

    L0 System Prompt   永不变，固定前缀命中缓存（DeepSeek 缓存命中省 98%）
    L1 长期记忆        滚动摘要，超窗口时由 LLM 压缩生成
    L2 短期窗口        最近 N 轮完整问答，按 token 预算动态截断
    L3 当前消息 + RAG  实时拼装
"""
from __future__ import annotations

from typing import Optional

from .types import WxMessage

SYSTEM_PROMPT = """你是微信里的 AI 助手"小助手"。

行为准则：
1. 回复简洁自然，像朋友聊天，不要长篇大论，控制在 200 字以内
2. 群聊场景下，如果不确定该不该插话，就保持沉默
3. 不要暴露你是机器人，除非被直接询问
4. 不讨论政治、不传播谣言、不输出违规内容
5. 不确定的事情如实说不知道，不要编造
"""

WINDOW_BUDGET = 5000      # L2 短期窗口 token 预算
COMPACT_THRESHOLD = 5000  # 超过则触发滚动摘要


def approx_tokens(text: str) -> int:
    """粗略估算 token 数：中文约 1.5 字符/token，英文约 4 字符/token。"""
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cn
    return int(cn / 1.5 + other / 4) + 1


class ContextManager:
    """装配与维护会话上下文。存储抽象注入，可换 Redis / 内存。"""

    def __init__(self, store, compactor=None):
        self.store = store            # 需实现 get_recent / get_summary / push / set_summary
        self.compactor = compactor    # 摘要压缩器，需实现 summarize(turns) -> str

    async def build(self, msg: WxMessage, rag_context: str = "") -> list[dict]:
        """装配发给 LLM 的 messages。"""
        key = msg.ctx_key
        history = await self.store.get_recent(key)
        summary = await self.store.get_summary(key)

        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if summary:
            messages.append({"role": "system", "content": f"[历史对话摘要] {summary}"})

        # 按预算从最新往前装，装不下就丢弃最早的
        messages += self._fit_budget(history, WINDOW_BUDGET)

        if rag_context:
            messages.append({
                "role": "system",
                "content": f"[参考资料]\n{rag_context}\n请基于以上资料回答，资料不足时如实说明。",
            })

        messages.append({"role": "user", "content": msg.render()})
        return messages

    async def append(self, msg: WxMessage, reply: str) -> None:
        """写入本轮问答，必要时触发压缩。"""
        key = msg.ctx_key
        await self.store.push(key, {"user": msg.render(), "assistant": reply})

        if await self.store.token_count(key) > COMPACT_THRESHOLD and self.compactor:
            turns = await self.store.pop_oldest(key, keep=6)
            if turns:
                summary = await self.compactor.summarize(turns)
                old = await self.store.get_summary(key)
                merged = f"{old}\n{summary}".strip() if old else summary
                await self.store.set_summary(key, merged)

    def _fit_budget(self, history: list[dict], budget: int) -> list[dict]:
        """从最新往前累积，直到超出预算。"""
        out: list[dict] = []
        used = 0
        for turn in reversed(history):
            cost = approx_tokens(turn.get("user", "")) + approx_tokens(turn.get("assistant", ""))
            if used + cost > budget:
                break
            out.insert(0, {"role": "user", "content": turn["user"]})
            out.insert(1, {"role": "assistant", "content": turn["assistant"]})
            used += cost
        return out


class LLMCompactor:
    """用免费小模型做滚动摘要，成本几乎为零。"""

    def __init__(self, judge_llm):
        self.judge_llm = judge_llm

    async def summarize(self, turns: list[dict]) -> str:
        text = "\n".join(f"用户: {t['user']}\n助手: {t['assistant']}" for t in turns)
        prompt = (
            "把下面这段对话压缩成不超过 80 字的中文摘要，"
            "只保留关键事实（用户身份、诉求、已确认的结论、待办事项），"
            "不要保留寒暄和重复内容：\n\n" + text
        )
        return await self.judge_llm.complete(prompt, max_tokens=200)
