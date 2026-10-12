"""LLM 网关 —— 多供应商路由 / 自动降级 / 缓存 / 成本统计。

所有国内主流模型（DeepSeek、通义千问、智谱 GLM）都兼容 OpenAI 协议，
因此只需改 base_url + api_key + model 三个参数即可切换。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

try:
    from openai import AsyncOpenAI
except ImportError:  # 允许无依赖下导入（测试用）
    AsyncOpenAI = None


@dataclass
class ProviderCfg:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    max_tokens: int = 1024
    temperature: float = 0.8


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False


class LLMGateway:
    """统一 LLM 调用入口。

    职责：
      1. 多供应商路由（主模型 / 兜底模型 / 判定模型）
      2. 自动降级（主模型异常 → 兜底模型）
      3. 重试与超时控制
      4. 成本统计（按 chat_id / 按天）
    """

    def __init__(self, primary: ProviderCfg, fallback: Optional[ProviderCfg] = None,
                 usage_sink=None):
        self.primary = primary
        self.fallback = fallback
        self.usage_sink = usage_sink       # 可注入 DB 写入回调
        self._clients: dict[str, "AsyncOpenAI"] = {}

    def _client(self, cfg: ProviderCfg):
        if cfg.name not in self._clients:
            self._clients[cfg.name] = AsyncOpenAI(
                api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.timeout
            )
        return self._clients[cfg.name]

    async def chat(self, messages: list[dict], chat_id: str = "",
                   cfg: Optional[ProviderCfg] = None) -> str:
        """非流式调用，带降级。"""
        cfg = cfg or self.primary
        try:
            return await self._call(cfg, messages, chat_id)
        except Exception as e:
            if self.fallback and cfg is not self.fallback:
                await self._report_error(cfg, e)
                return await self._call(self.fallback, messages, chat_id)
            raise

    async def stream(self, messages: list[dict], chat_id: str = "",
                     cfg: Optional[ProviderCfg] = None) -> AsyncIterator[str]:
        """流式调用。微信无原生流式，由 ReplyPlanner 分段发送。"""
        cfg = cfg or self.primary
        client = self._client(cfg)
        stream = await client.chat.completions.create(
            model=cfg.model, messages=messages, stream=True,
            max_tokens=cfg.max_tokens, temperature=cfg.temperature,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    async def complete(self, prompt: str, max_tokens: int = 256) -> str:
        """单轮补全，用于摘要/判定等内部任务（走便宜模型）。"""
        cfg = self.fallback or self.primary
        cfg_override = ProviderCfg(**{**cfg.__dict__, "max_tokens": max_tokens, "temperature": 0.3})
        return await self.chat([{"role": "user", "content": prompt}], cfg=cfg_override)

    # ── 内部 ──────────────────────────────────────────────────

    async def _call(self, cfg: ProviderCfg, messages: list[dict], chat_id: str) -> str:
        client = self._client(cfg)
        t0 = time.monotonic()
        resp = await client.chat.completions.create(
            model=cfg.model, messages=messages,
            max_tokens=cfg.max_tokens, temperature=cfg.temperature,
        )
        text = resp.choices[0].message.content or ""
        usage = Usage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0),
            completion_tokens=getattr(resp.usage, "completion_tokens", 0),
        )
        if self.usage_sink:
            await self.usage_sink(chat_id, cfg.model, usage, time.monotonic() - t0)
        return text

    async def _report_error(self, cfg: ProviderCfg, err: Exception) -> None:
        # 实际实现写 Prometheus counter / 日志
        print(f"[LLMGateway] provider={cfg.name} failed: {err!r}, falling back")


class JudgeLLM:
    """群聊插话判定器 —— 用免费小模型做二分类，是成本可控的关键。"""

    JUDGE_TEMPLATE = """你是一个群聊机器人的"插话决策器"。

【机器人人设】
{persona}

【群聊最近消息】
{recent}

【当前消息】
{current}

【任务】
判断机器人现在是否应该插话回复：
- 有人在向机器人提问、求助、或明显在等机器人回应 → YES
- 有人点名机器人（即使没 @）→ YES
- 群友之间闲聊、互相聊天、与机器人无关的话题 → NO
- 机器人插话会显得突兀、打断别人对话 → NO
- 机器人刚发过言且话题已结束 → NO

只输出 YES 或 NO，不要任何其他内容。"""

    def __init__(self, gateway: LLMGateway, timeout: float = 3.0):
        self.gateway = gateway
        self.timeout = timeout

    async def judge(self, msg, recent, persona: str) -> bool:
        recent_text = "\n".join(m.render() for m in recent[-8:])
        prompt = self.JUDGE_TEMPLATE.format(
            persona=persona or "友好的群聊助手", recent=recent_text, current=msg.render()
        )
        try:
            out = await asyncio.wait_for(
                self.gateway.complete(prompt, max_tokens=8), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            return False   # 超时保守不回复
        return "YES" in out.upper()
