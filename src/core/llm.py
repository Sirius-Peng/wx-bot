"""LLM 网关 —— 多供应商路由 / 自动降级 / 缓存 / 成本统计。

所有国内主流模型（DeepSeek、通义千问、智谱 GLM）都兼容 OpenAI 协议，
因此只需改 base_url + api_key + model 三个参数即可切换。
"""
from __future__ import annotations

import asyncio
import logging
import time
import json
import httpx
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

try:
    from openai import AsyncOpenAI, Timeout as SDKTimeout
except ImportError:  # 允许无依赖下导入（测试用）
    AsyncOpenAI = None
    SDKTimeout = httpx.Timeout


log = logging.getLogger(__name__)

def is_transient_llm_error(error: BaseException) -> bool:
    """Classify SDK-native httpx2 as well as httpx without exposing exception bodies."""
    if isinstance(error, (httpx.TransportError, TimeoutError)):
        return True
    if type(error).__name__ in {'APIConnectionError', 'APITimeoutError', 'ConnectError', 'ConnectTimeout',
                               'ReadTimeout', 'ReadError', 'WriteError', 'RemoteProtocolError', 'PoolTimeout'}:
        return True
    status = getattr(error, 'status_code', None)
    return isinstance(status, int) and (status == 429 or 500 <= status <= 599)


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
                 usage_sink=None, vision: Optional[ProviderCfg] = None, web_search=None):
        self.web_search = web_search
        self.vision = vision
        self.primary = primary
        self.fallback = fallback
        self.usage_sink = usage_sink       # 可注入 DB 写入回调
        self._clients: dict[tuple, "AsyncOpenAI"] = {}

    def _new_client(self, cfg: ProviderCfg):
        # Use the SDK's public Timeout: current OpenAI SDK uses httpx2 internally.
        return AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url,
            timeout=SDKTimeout(cfg.timeout, connect=min(8.0,cfg.timeout), pool=min(3.0,cfg.timeout)),
            max_retries=0)

    def _client(self, cfg: ProviderCfg):
        key = (cfg.name, cfg.base_url, cfg.api_key, cfg.timeout)
        if key not in self._clients:
            self._clients[key] = self._new_client(cfg)
        return self._clients[key]

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        await asyncio.gather(*(c.close() for c in clients.values()),
                             return_exceptions=True)

    async def chat(self, messages: list[dict], chat_id: str = "",
                   cfg: Optional[ProviderCfg] = None) -> str:
        """非流式调用，带降级。"""
        has_images = any(isinstance(m.get("content"), list) and any(p.get("type")=="image_url" for p in m["content"] if isinstance(p,dict)) for m in messages)
        use_web = cfg is None and bool(chat_id) and self.web_search is not None
        cfg = cfg or (self.vision if has_images and self.vision else self.primary)
        if use_web: return await self._web_answer(cfg, messages, chat_id, has_images)
        try:
            return await self._call(cfg, messages, chat_id)
        except Exception as e:
            if not has_images and self.fallback and cfg is not self.fallback:
                await self._report_error(cfg, e)
                return await self._call(self.fallback, messages, chat_id)
            raise

    async def stream(self, messages: list[dict], chat_id: str = "",
                     cfg: Optional[ProviderCfg] = None) -> AsyncIterator[str]:
        """One bounded retry before any token; never concatenate a second generation."""
        has_images = any(isinstance(m.get('content'),list) and any(p.get('type')=='image_url'
                         for p in m['content'] if isinstance(p,dict)) for m in messages)
        if cfg is None and self.web_search is not None:
            selected = self.vision if has_images and self.vision else self.primary
            yield await self._web_answer(selected, messages, chat_id, has_images)
            return
        cfg = cfg or (self.vision if has_images and self.vision else self.primary)
        emitted = False
        try:
            for attempt in range(2):
                stream = None; retry_client = None
                try:
                    client = self._client(cfg) if attempt == 0 else self._new_client(cfg)
                    if attempt: retry_client = client
                    stream = await client.chat.completions.create(model=cfg.model, messages=messages,
                        stream=True, max_tokens=cfg.max_tokens, temperature=cfg.temperature)
                    async for chunk in stream:
                        delta = chunk.choices[0].delta.content if chunk.choices else None
                        if delta:
                            emitted = True
                            yield delta
                    return
                except Exception as error:
                    if attempt == 0 and not emitted and is_transient_llm_error(error):
                        log.warning('Model connection retry provider=%s error_type=%s',cfg.name,type(error).__name__)
                    else:
                        raise
                finally:
                    if stream is not None and hasattr(stream,'close'): await stream.close()
                    if retry_client is not None: await retry_client.close()
                await asyncio.sleep(.25)
        except Exception as error:
            fallback = None if has_images else self.fallback
            if not emitted and fallback and cfg is not fallback:
                await self._report_error(cfg, error)
                async for delta in self.stream(messages, chat_id, cfg=fallback):yield delta
            else:
                raise

    async def complete(self, prompt: str, max_tokens: int = 256) -> str:
        """单轮补全，用于摘要/判定等内部任务（走便宜模型）。"""
        cfg = self.fallback or self.primary
        cfg_override = ProviderCfg(**{**cfg.__dict__, "max_tokens": max_tokens, "temperature": 0.3})
        return await self.chat([{"role": "user", "content": prompt}], cfg=cfg_override)

    # ── 内部 ──────────────────────────────────────────────────

    async def _response(self, cfg, messages, chat_id, **extra):
        t0 = time.monotonic()
        for attempt in range(2):
            client = self._client(cfg) if attempt == 0 else self._new_client(cfg)
            try:
                resp = await client.chat.completions.create(model=cfg.model, messages=messages,
                    max_tokens=cfg.max_tokens, temperature=cfg.temperature, **extra)
                break
            except Exception as error:
                if attempt or not is_transient_llm_error(error):raise
                log.warning('Model connection retry provider=%s error_type=%s',cfg.name,type(error).__name__)
            finally:
                if attempt:await client.close()
            await asyncio.sleep(.25)
        usage = Usage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0),
            completion_tokens=getattr(resp.usage, "completion_tokens", 0),
        )
        if self.usage_sink:
            await self.usage_sink(chat_id, cfg.model, usage, time.monotonic() - t0)
        return resp

    async def _call(self, cfg, messages, chat_id):
        resp = await self._response(cfg, messages, chat_id)
        return resp.choices[0].message.content or ""

    async def _web_answer(self, cfg, messages, chat_id, has_images=False):
        from .web_search import SEARCH_TOOL, SEARCH_RULES
        # Request-local snapshots: hot configuration never mixes credentials mid-turn.
        search = self.web_search
        work = [*messages, {"role":"system", "content":SEARCH_RULES}]
        sources = []; calls_used = 0
        for round_index in range(3):
            extra = {"tools":[SEARCH_TOOL], "tool_choice":"auto" if round_index < 2 else "none"}
            try:
                resp = await self._response(cfg, work, chat_id, **extra)
            except Exception as error:
                if getattr(error,'status_code',None) in (400,404,422):
                    work.append({'role':'system','content':'本轮联网工具调用未成功。对实时问题如实说明本次未能核实；不要声称已搜索。'})
                    return self._with_sources(await self._call(cfg,work,chat_id),sources)
                if has_images or not self.fallback or cfg is self.fallback: raise
                await self._report_error(cfg, error)
                cfg = self.fallback
                resp = await self._response(cfg, work, chat_id, **extra)
            message = resp.choices[0].message
            tool_calls = getattr(message, 'tool_calls', None) or []
            if not tool_calls:
                return self._with_sources(message.content or "",sources)
            if round_index == 2: raise RuntimeError('Model ignored tool_choice=none')
            # Include reasoning_content for DeepSeek thinking/tool-use compatibility.
            assistant = {'role':'assistant','content':message.content,
                         'tool_calls':[t.model_dump(exclude_none=True) for t in tool_calls]}
            reasoning = getattr(message, 'reasoning_content', None)
            if reasoning is not None: assistant['reasoning_content'] = reasoning
            work.append(assistant)
            for tool in tool_calls:
                result = {'ok':False,'error':'tool_budget_exhausted','sources':[]}
                if tool.function.name == 'web_search' and calls_used < 2:
                    calls_used += 1
                    try:
                        args = json.loads(tool.function.arguments)
                        result = await search.search(args.get('query') if isinstance(args,dict) else None)
                    except (ValueError, TypeError): result = {'ok':False,'error':'invalid_query','sources':[]}
                for source in result.get('sources',[]):
                    if source['url'] not in {s['url'] for s in sources}:sources.append(source)
                work.append({'role':'tool','tool_call_id':tool.id,
                             'content':json.dumps(result,ensure_ascii=False)})
        raise RuntimeError('Search turn budget exhausted')

    @staticmethod
    def _with_sources(text, sources):
        # Keep provenance even if a provider omitted URLs; one complete bubble.
        missing = [s for s in sources if s['url'] not in text][:3]
        if missing: text += " 参考来源：" + "；".join(s['title'] + " " + s['url'] for s in missing)
        return text

    async def _report_error(self, cfg: ProviderCfg, err: Exception) -> None:
        # 实际实现写 Prometheus counter / 日志
        log.warning('Model fallback provider=%s error_type=%s',cfg.name,type(err).__name__)


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
