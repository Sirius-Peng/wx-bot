"""机器人运行时 —— 管理生命周期、状态、消息日志与扫码登录。

这一层把「引擎」和「界面」解耦：
  · Web UI 只调用这里暴露的方法，不直接碰适配器/编排层
  · 未配置或启动失败时不会拖垮 Web 服务，界面始终可访问

═══════════════════════════════════════════════════════════════════
支持的通道（个人微信群聊能力已整体下线，见 README 说明）：

  · 微信 —— 腾讯官方 iLink Bot API（合规、零封号，仅私聊）
  · QQ   —— OneBot 11（NapCat 等实现，私聊 + 群聊）

⚠️ 关于消息接收方式，两条通道模型完全不同，这是最容易漏的地方：

  QQ / OneBot   —— **推**：网关主动把事件推进来
                   （反向 WS `/ws/onebot` 或 HTTP `/onebot/event`，见 server.py）
  iLink         —— **拉**：由本程序启动长轮询循环去 getUpdates
                   详见 `_start_ilink_poller()`，忘了启动它就会"收不到任何消息"
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from .adapters.base import BaseAdapter
from .adapters.ilink import ILinkAdapter
from .adapters.onebot import OneBotAdapter
from .config_store import AppConfig, ConfigStore
from .core.context import ContextManager
from .core.docker import (NAPCAT_CONTAINER, NAPCAT_HTTP_PORT,
                          NAPCAT_WEBUI_PORT, DockerManager)
from .core.http import explain_error, make_client
from .core.llm import JudgeLLM, LLMGateway, ProviderCfg
from .core.memory import CATEGORIES, SCOPE_USER, PersonaMemory
from .core.napcat import NapCatWebUI
from .core.qr import QRDataTooLong, data_url
from .core.sender import HumanLikeSender
from .core.trigger import TriggerRouter
from .core.types import WxMessage
from .orchestrator import Orchestrator
from .store_memory import MemoryStore

log = logging.getLogger(__name__)


def _first_key(d: dict, *names: str):
    """按候选键名取第一个非空值（协议端字段大小写不统一）。"""
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return ""


# ═══════════════════════════════════════════════════════════════════
#  消息日志（供界面展示）
# ═══════════════════════════════════════════════════════════════════

@dataclass
class LogEntry:
    ts: int
    direction: str          # in | out
    chat_type: str
    chat_id: str
    chat_name: str
    sender: str
    content: str
    trigger_layer: str = ""
    trigger_reason: str = ""
    replied: bool = False
    platform: str = ""      # wechat | qq

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "direction": self.direction,
            "chat_type": self.chat_type, "chat_id": self.chat_id,
            "chat_name": self.chat_name, "sender": self.sender,
            "content": self.content,
            "trigger_layer": self.trigger_layer,
            "trigger_reason": self.trigger_reason,
            "replied": self.replied,
            "platform": self.platform,
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts / 1000)),
        }


class MessageLog:
    """内存环形缓冲 + JSONL 落盘。界面读内存，重启后从文件恢复。"""

    def __init__(self, path: Path, maxlen: int = 2000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buf: deque[LogEntry] = deque(maxlen=maxlen)
        self._stats = {"in": 0, "out": 0, "replied": 0, "silent": 0,
                       "groups": set(), "privates": set()}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        # ⚠️ 必须逐行容错：程序被强杀时最后一行常常是半截 JSON，
        # 早期版本一旦解析失败就整段丢弃 —— 用户会看到"消息记录全没了"。
        bad = 0
        for line in self.path.read_text(encoding="utf-8").splitlines()[-2000:]:
            if not line.strip():
                continue
            try:
                e = LogEntry(**json.loads(line))
            except Exception:
                bad += 1
                continue
            self._buf.append(e)
            self._recount(e)      # 重启后统计也要能对上历史数据
        if bad:
            log.warning("消息日志有 %d 行损坏，已跳过", bad)

    def _recount(self, e: LogEntry) -> None:
        """重启后从历史记录重建统计。"""
        if e.direction == "out":
            self._stats["out"] += 1
            return
        self._stats["in"] += 1
        if e.replied:
            self._stats["replied"] += 1
        else:
            self._stats["silent"] += 1
        (self._stats["groups"] if e.chat_type == "group"
         else self._stats["privates"]).add(e.chat_id)

    def _append_file(self, entry: LogEntry) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": entry.ts, "direction": entry.direction,
                    "chat_type": entry.chat_type, "chat_id": entry.chat_id,
                    "chat_name": entry.chat_name, "sender": entry.sender,
                    "content": entry.content,
                    "trigger_layer": entry.trigger_layer,
                    "trigger_reason": entry.trigger_reason,
                    "replied": entry.replied,
                    "platform": entry.platform,
                }, ensure_ascii=False) + "\n")
        except Exception:
            log.debug("消息日志写盘失败", exc_info=True)

    def add_in(self, msg: WxMessage, layer: str = "", reason: str = "",
               replied: bool = False, platform: str = "") -> None:
        e = LogEntry(
            ts=msg.timestamp or int(time.time() * 1000), direction="in",
            chat_type=msg.chat_type, chat_id=msg.chat_id,
            chat_name=msg.sender_name if not msg.is_group else "",
            sender=msg.sender_name or msg.sender_id, content=msg.content,
            trigger_layer=layer, trigger_reason=reason, replied=replied,
            platform=platform or msg.platform,
        )
        self._buf.append(e)
        self._append_file(e)
        self._recount(e)

    def add_out(self, msg: WxMessage, text: str, platform: str = "") -> None:
        e = LogEntry(
            ts=int(time.time() * 1000), direction="out",
            chat_type=msg.chat_type, chat_id=msg.chat_id,
            chat_name="", sender="机器人", content=text,
            platform=platform or msg.platform,
        )
        self._buf.append(e)
        self._append_file(e)
        self._recount(e)

    def query(self, limit: int = 100, chat_id: str = "",
              chat_type: str = "", direction: str = "") -> list[dict]:
        items = list(self._buf)[::-1]
        if chat_id:
            items = [e for e in items if e.chat_id == chat_id]
        if chat_type:
            items = [e for e in items if e.chat_type == chat_type]
        if direction:
            items = [e for e in items if e.direction == direction]
        return [e.to_dict() for e in items[:limit]]

    def stats(self) -> dict:
        return {
            "total_in": self._stats["in"],
            "total_out": self._stats["out"],
            "replied": self._stats["replied"],
            "silent": self._stats["silent"],
            "group_count": len(self._stats["groups"]),
            "private_count": len(self._stats["privates"]),
            "reply_rate": round(
                self._stats["replied"] / max(1, self._stats["in"]) * 100, 1
            ),
        }

    def clear(self) -> None:
        self._buf.clear()
        self._stats = {"in": 0, "out": 0, "replied": 0, "silent": 0,
                       "groups": set(), "privates": set()}
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
#  运行时
# ═══════════════════════════════════════════════════════════════════

class BotRuntime:
    """机器人运行时 —— 微信（官方 iLink）与 QQ（OneBot）可同时运行。"""

    def __init__(self, store: ConfigStore, data_dir: Path):
        self.config_store = store
        self.data_dir = Path(data_dir)
        self.log = MessageLog(self.data_dir / "messages.jsonl")
        self.docker = DockerManager()
        self._napcat_webui_token = ""
        self._napcat_webui: Optional[NapCatWebUI] = None
        self.memory: Optional[PersonaMemory] = None

        self.cfg: AppConfig = store.load()

        # 双通道（独立适配器 + 独立编排器，互不影响）
        self.wechat_adapter: Optional[BaseAdapter] = None
        self.wechat_orch: Optional[Orchestrator] = None
        self.qq_adapter: Optional[BaseAdapter] = None
        self.qq_orch: Optional[Orchestrator] = None

        self.running = False
        self.last_error = ""
        self.started_at = 0.0
        self.platforms_running: set[str] = set()   # {"wechat", "qq"}

        # iLink 是"拉"模型，必须由我们自己跑长轮询
        self._ilink_task: Optional[asyncio.Task] = None

        self._login: dict = {"status": "idle", "qrcode": "", "image": "",
                             "uuid": "", "app_id": "", "message": "",
                             "channel": ""}

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> dict:
        if self.running:
            return {"ok": True, "message": "机器人已在运行"}
        self.cfg = self.config_store.load()
        try:
            self._validate()
        except ValueError as e:
            self.last_error = str(e)
            log.warning("启动验证未通过：%s", e)
            return {"ok": False, "message": str(e)}

        results: list[dict] = []
        a = self.cfg.adapter

        if a.wechat_enabled:
            try:
                self.wechat_adapter = self._build_wechat_adapter()
                self.wechat_orch = self._build_orchestrator(
                    self.wechat_adapter, platform="wechat")
                await self.wechat_adapter.start()
                self._start_ilink_poller()      # ★ 不启动它 = 收不到任何消息
                self.platforms_running.add("wechat")
                results.append({"ok": True, "platform": "wechat",
                                "message": "微信通道已启动"})
                log.info("微信通道已启动")
            except Exception as e:
                log.exception("微信通道启动失败")
                results.append({"ok": False, "platform": "wechat",
                                "message": f"微信通道启动失败：{e}"})

        if a.qq_enabled:
            try:
                self.qq_adapter = self._build_qq_adapter()
                self.qq_orch = self._build_orchestrator(
                    self.qq_adapter, platform="qq")
                await self.qq_adapter.start()
                self.platforms_running.add("qq")
                results.append({"ok": True, "platform": "qq",
                                "message": "QQ 通道已启动"})
                log.info("QQ 通道已启动")
            except Exception as e:
                log.exception("QQ 通道启动失败")
                results.append({"ok": False, "platform": "qq",
                                "message": f"QQ 通道启动失败：{e}"})

        any_ok = any(r["ok"] for r in results)
        self.running = any_ok
        if any_ok:
            self.last_error = ""
            self.started_at = time.time()
            return {"ok": True,
                    "message": "；".join(r["message"] for r in results),
                    "details": results}
        err = "；".join(r["message"] for r in results) or "没有可用的通道"
        self.last_error = err
        return {"ok": False, "message": err, "details": results}

    # ── iLink 长轮询（消息接收的关键）─────────────────────────

    def _start_ilink_poller(self) -> None:
        """启动官方 iLink 的长轮询任务。

        ⚠️ 这是官方通道**唯一的收消息途径**。iLink 是拉模型
        （getUpdates 长轮询），不像 QQ / OneBot 那样由网关推过来。
        早期版本漏了这一步：适配器 start() 只加载凭证、发一次
        notifystart，`poll_loop()` 从未被调用 ——
        表现为"登录成功了，但发消息没有任何反应，也收不到消息"。
        """
        adapter = self.wechat_adapter
        if adapter is None or not hasattr(adapter, "poll_loop"):
            return
        if self._ilink_task and not self._ilink_task.done():
            return          # 已在跑，别起第二个（会重复收消息）

        async def runner() -> None:
            try:
                await adapter.poll_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("iLink 长轮询异常退出")

        self._ilink_task = asyncio.create_task(runner())
        log.info("iLink 长轮询已启动")

    async def _stop_ilink_poller(self) -> None:
        task, self._ilink_task = self._ilink_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("iLink 长轮询已停止")

    async def stop(self) -> dict:
        await self._stop_ilink_poller()
        for name, adapter in (("wechat", self.wechat_adapter),
                              ("qq", self.qq_adapter)):
            if adapter is not None:
                try:
                    await adapter.stop()
                except Exception:
                    log.warning("%s 通道关闭异常", name, exc_info=True)
        self.wechat_adapter = None
        self.wechat_orch = None
        self.qq_adapter = None
        self.qq_orch = None
        self.running = False
        self.platforms_running.clear()
        return {"ok": True, "message": "机器人已停止"}

    async def restart(self) -> dict:
        await self.stop()
        await asyncio.sleep(0.3)
        return await self.start()

    def status(self) -> dict:
        st = {
            "running": self.running,
            "platforms": list(self.platforms_running),
            "uptime": int(time.time() - self.started_at) if self.running else 0,
            "last_error": self.last_error,
            "login": self._login,
            "configured": bool(self.cfg.llm.primary_key),
            "stats": self.log.stats(),
            "bot": {"name": self.cfg.bot.name, "persona": self.cfg.bot.persona},
        }
        for name, adapter in (("wechat", self.wechat_adapter),
                              ("qq", self.qq_adapter)):
            st[f"{name}_running"] = name in self.platforms_running
            if adapter is not None:
                st[f"{name}_platform"] = adapter.platform
        # 官方通道的收消息循环是否在跑 —— 界面/排查都用得上
        st["ilink_polling"] = bool(self._ilink_task and not self._ilink_task.done())
        return st

    # ── 构建 ──────────────────────────────────────────────────

    def _validate(self) -> None:
        a, l = self.cfg.adapter, self.cfg.llm
        if a.wechat_enabled and not self.ilink_status().get("loggedIn"):
            raise ValueError(
                "微信通道还没扫码登录，请到「接入设置」点「获取登录二维码」")
        if a.qq_enabled and not a.onebot_http_url:
            raise ValueError("QQ 通道：未填写 OneBot 网关地址")
        if not l.primary_key:
            raise ValueError("未填写大模型 API Key，请到「模型与行为」完善")

    def _build_wechat_adapter(self) -> BaseAdapter:
        return ILinkAdapter(state_dir=str(self._ilink_state_dir()),
                            bot_agent="WxBot/1.0")

    def _build_qq_adapter(self) -> BaseAdapter:
        a = self.cfg.adapter
        return OneBotAdapter(
            http_url=a.onebot_http_url, access_token=a.onebot_token,
            bot_qq=a.bot_qq, bot_nickname=a.bot_nickname or self.cfg.bot.name,
        )

    def _build_orchestrator(self, adapter: BaseAdapter,
                            platform: str = "") -> Orchestrator:
        cfg = self.cfg
        # 判定模型只用于知识库抽取，不再做"该不该插话"的判定
        judge_gw = LLMGateway(ProviderCfg(
            name="judge", base_url=cfg.llm.judge_base, api_key=cfg.llm.judge_key,
            model=cfg.llm.judge_model, timeout=8.0, max_tokens=600,
        ))
        chat_gw = LLMGateway(
            primary=ProviderCfg(
                name="primary", base_url=cfg.llm.primary_base,
                api_key=cfg.llm.primary_key, model=cfg.llm.primary_model,
                timeout=60.0,
            ),
            fallback=ProviderCfg(
                name="fallback", base_url=cfg.llm.fallback_base,
                api_key=cfg.llm.fallback_key, model=cfg.llm.fallback_model,
            ) if cfg.llm.fallback_key else None,
        )
        judge = JudgeLLM(judge_gw, timeout=8.0)
        store = MemoryStore()

        memory = PersonaMemory(
            self.data_dir / "memory.json", llm=judge_gw,
            enabled=bool(cfg.bot.memory_enabled),
        )
        memory.extract_interval = max(1, int(cfg.bot.memory_extract_interval or 3))
        memory.inject = bool(cfg.bot.memory_inject)
        self.memory = memory

        # ⚠️ bot 名称与人设必须传给 ContextManager，否则界面上改了不生效
        orch = Orchestrator(
            adapter=adapter, store=store, llm=chat_gw, judge=judge,
            ctx=ContextManager(store, bot_name=cfg.bot.name,
                               persona=cfg.bot.persona),
            router=TriggerRouter(bot_name=cfg.bot.name),
            sender=HumanLikeSender(adapter), memory=memory,
            group_reply_at_sender=bool(cfg.bot.group_reply_at_sender),
        )
        self._wire_logging(orch, adapter, platform)
        return orch

    def _wire_logging(self, orch: Orchestrator, adapter: BaseAdapter,
                      platform: str = "") -> None:
        """挂日志钩子，让界面能看到每条消息的判定结果。"""
        last: dict = {"layer": "", "reason": ""}
        plat = platform or adapter.platform

        orig_decide = orch.router.decide

        async def decide_wrapped(msg: WxMessage):
            d = await orig_decide(msg)
            last["layer"], last["reason"] = d.layer, d.reason
            return d

        orch.router.decide = decide_wrapped        # type: ignore[method-assign]

        orig_process = orch._process

        async def process_wrapped(msg: WxMessage) -> None:
            out_before = self.log._stats["out"]
            try:
                await orig_process(msg)
            finally:
                replied = self.log._stats["out"] > out_before
                self.log.add_in(msg, last.get("layer", ""),
                                last.get("reason", ""), replied, platform=plat)

        orch._process = process_wrapped             # type: ignore[method-assign]

        orig_send = adapter.send_text

        async def send_wrapped(chat_id: str, text: str, at=None) -> str:
            mid = await orig_send(chat_id, text, at=at)
            self.log.add_out(WxMessage(
                msg_id="", platform=plat,
                chat_type=self._guess_chat_type(chat_id),
                chat_id=chat_id, sender_id="", sender_name="", bot_id="",
                content=text,
            ), text, platform=plat)
            return mid

        adapter.send_text = send_wrapped            # type: ignore[method-assign]

    @staticmethod
    def _guess_chat_type(chat_id: str) -> str:
        """从会话 ID 反推会话类型（只用于日志展示）。

        QQ 的 OneBot 适配器用 `g_<群号>` / `p_<QQ号>` 前缀，最可靠；
        官方微信目前只有私聊。
        """
        return "group" if chat_id.startswith("g_") else "private"

    # ══ Docker 环境（QQ 一键部署前置条件）══════════════════════

    def docker_status(self) -> dict:
        """Docker 与安装方案快照。前端轮询它来展示进度与指引。"""
        return {
            "docker": self.docker.detect().to_dict(),
            "plan": self.docker.install_plan(),
            "task": dict(self.docker.task),
        }

    async def docker_prepare(self) -> dict:
        """一键准备 Docker：未装则下载安装包，装了没启动则启动。"""
        return await self.docker.prepare()

    # ══ QQ / OneBot 连接测试 ═══════════════════════════════════

    async def test_onebot(self, http_url: str = "", token: str = "") -> dict:
        """测试 OneBot 网关连通性与 Token 有效性。"""
        a = self.cfg.adapter
        base = (http_url or a.onebot_http_url or "").rstrip("/")
        tok = token if token else a.onebot_token
        if not base:
            return {"ok": False, "stage": "config", "message": "请先填写 HTTP 接口地址"}

        probe = OneBotAdapter(http_url=base, access_token=tok)
        try:
            r = await probe._call("get_login_info")
            if r.get("status") == "failed":
                if r.get("retcode") == 403:
                    return {"ok": False, "stage": "auth",
                            "message": "网关可以连接，但 Access Token 不正确",
                            "detail": "网关返回 403",
                            "hint": "请检查 Token 是否与网关配置里的一致。"}
                return {"ok": False, "stage": "connect",
                        "message": f"连不上 OneBot 网关 {base}",
                        "detail": r.get("message", ""),
                        "hint": "请确认网关已启动、HTTP 服务已开启，"
                                "且地址与端口填写正确。"}

            data = r.get("data") or {}
            qq = str(data.get("user_id") or "")
            nick = data.get("nickname") or ""
            if qq:
                cfg = self.config_store.load()
                cfg.adapter.bot_qq = qq
                if nick and not cfg.adapter.bot_nickname:
                    cfg.adapter.bot_nickname = nick
                self.config_store.save(cfg)
                self.cfg = cfg
            return {
                "ok": True, "stage": "ok",
                "message": f"网关连接正常，已登录：{nick}（{qq}）" if qq else "网关连接正常",
                "qq": qq, "nickname": nick,
                "hint": "下一步：在网关里把反向 WebSocket 指向本程序，"
                        "然后就可以在 QQ 里对话了。",
            }
        finally:
            try:
                await probe._client.aclose()
            except Exception:
                pass

    # ══ QQ / NapCat 自动探测 ═══════════════════════════════════

    QQ_PROBE_CANDIDATES = [
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
        "http://localhost:3001",
    ]

    async def detect_napcat(self) -> dict:
        """自动探测本地是否已有 OneBot / NapCat 网关在运行。"""
        found = []
        for url in self.QQ_PROBE_CANDIDATES:
            try:
                async with make_client(url, timeout=3.0) as c:
                    r = await c.post(f"{url}/get_login_info", json={},
                                     headers={"Authorization": "Bearer "})
                    if r.status_code == 200:
                        d = r.json()
                        if d.get("status") == "ok":
                            data = d.get("data") or {}
                            found.append({
                                "url": url,
                                "qq": str(data.get("user_id") or ""),
                                "nickname": data.get("nickname") or "",
                            })
                            break
            except Exception:
                continue

        if found:
            cfg = self.config_store.load()
            cfg.adapter.onebot_http_url = found[0]["url"]
            if found[0]["qq"] and not cfg.adapter.bot_qq:
                cfg.adapter.bot_qq = found[0]["qq"]
            self.config_store.save(cfg)
            self.cfg = cfg

        return {
            "ok": bool(found),
            "found": found,
            "message": (f"发现 {len(found)} 个本地 OneBot 网关" if found else
                        "没有探测到正在运行的 OneBot 网关。"
                        "若尚未部署，请按下方指引启动一个。"),
        }

    # ══ QQ / NapCat 一键部署 ══════════════════════════════════

    def _napcat_ws_url(self) -> str:
        """NapCat 反向连过来的地址。

        容器内访问宿主机必须用 `host.docker.internal`（127.0.0.1 是容器自己）。
        """
        port = int(os.environ.get("WXBOT_PORT", "8000"))
        return f"ws://host.docker.internal:{port}/ws/onebot"

    def _ensure_napcat_webui_token(self) -> str:
        cfg = self.config_store.load()
        tok = getattr(cfg.adapter, "napcat_webui_token", "") or ""
        if not tok:
            tok = secrets.token_hex(16)
            cfg.adapter.napcat_webui_token = tok
            self.config_store.save(cfg)
            self.cfg = cfg
        self._napcat_webui_token = tok
        return tok

    def _webui_client(self) -> NapCatWebUI:
        if self._napcat_webui is None:
            self._napcat_webui = NapCatWebUI(
                token=self._ensure_napcat_webui_token(), port=NAPCAT_WEBUI_PORT)
        return self._napcat_webui

    async def qq_status(self) -> dict:
        """QQ 通道状态：容器 + 登录态。供前端轮询。"""
        cfg = self.config_store.load()
        napcat = self.docker.napcat_status()
        st = {
            "docker": self.docker.detect().to_dict(),
            "napcat": napcat,
            "task": dict(self.docker.task),
            "onebotUrl": cfg.adapter.onebot_http_url,
            "botQq": cfg.adapter.bot_qq,
            "stage": "idle",
            "loggedIn": False,
            "loginMessage": "",
        }
        if napcat.get("running") and cfg.adapter.onebot_http_url:
            try:
                r = await self.test_onebot(cfg.adapter.onebot_http_url,
                                           cfg.adapter.onebot_token)
                st["loggedIn"] = bool(r.get("ok"))
                st["loginMessage"] = r.get("message", "")
                st["stage"] = "ready" if r.get("ok") else "waiting_login"
            except Exception:
                log.debug("查询 QQ 登录态失败", exc_info=True)
        return st

    async def qq_setup(self) -> dict:
        """一键部署 QQ 网关（非阻塞，前端轮询 `/api/qq/status`）。"""
        token = self._ensure_napcat_webui_token()
        ws = self._napcat_ws_url()
        cfg = self.config_store.load()
        if not cfg.adapter.onebot_http_url:
            cfg.adapter.onebot_http_url = f"http://127.0.0.1:{NAPCAT_HTTP_PORT}"
            self.config_store.save(cfg)
            self.cfg = cfg
        return await self.docker.ensure_napcat(token, ws)

    async def qq_login_qrcode(self) -> dict:
        return await self._webui_client().qrcode()

    async def qq_login_check(self) -> dict:
        r = await self._webui_client().check_login()
        if r.get("loggedIn"):
            info = await self.test_onebot()
            if info.get("ok") and info.get("qq"):
                r["qq"] = info["qq"]
                r["nickname"] = info.get("nickname", "")
        return r

    async def qq_stop(self) -> dict:
        return await self.docker.stop_napcat()

    async def qq_logs(self, lines: int = 100) -> dict:
        return await self.docker.napcat_logs(lines)

    # ══ 人格知识库 ════════════════════════════════════════════

    def _ensure_memory(self) -> PersonaMemory:
        """知识库实例在机器人启动时创建；未启动时按需建一个只读实例，
        这样用户不启动机器人也能查看/编辑已有记忆。"""
        if self.memory is None:
            self.memory = PersonaMemory(
                self.data_dir / "memory.json", llm=None,
                enabled=bool(self.cfg.bot.memory_enabled),
            )
        return self.memory

    def memory_list(self, scope: str = "", owner_id: str = "",
                    category: str = "", keyword: str = "") -> dict:
        m = self._ensure_memory()
        return {
            "items": m.list_items(scope, owner_id, category, keyword),
            "stats": m.stats(),
            "categories": CATEGORIES,
        }

    def memory_add(self, scope: str, owner_id: str, category: str,
                   content: str, keywords: list[str] | None = None) -> dict:
        m = self._ensure_memory()
        if not content.strip():
            return {"ok": False, "message": "内容不能为空"}
        item = m.add_manual(
            scope=scope if scope in ("user", "group") else SCOPE_USER,
            owner_id=owner_id,
            category=category if category in CATEGORIES else "fact",
            content=content, keywords=keywords,
        )
        return {"ok": True, "item": item}

    def memory_delete(self, item_id: str) -> dict:
        ok = self._ensure_memory().delete(item_id)
        return {"ok": ok, "message": "已删除" if ok else "未找到该条记忆"}

    def memory_clear(self, scope: str = "", owner_id: str = "") -> dict:
        n = self._ensure_memory().clear(scope, owner_id)
        return {"ok": True, "message": f"已清除 {n} 条记忆"}

    def memory_stats(self) -> dict:
        m = self._ensure_memory()
        s = m.stats()
        s["enabled"] = bool(self.cfg.bot.memory_enabled)
        s["inject"] = bool(self.cfg.bot.memory_inject)
        s["categories"] = CATEGORIES
        return s

    # ══ 官方 iLink 扫码登录（应用内完成，无需命令行）══════════

    def _ilink_state_dir(self) -> Path:
        """官方 iLink 的凭证目录。

        ⚠️ 配置里的默认值是相对路径 `./data/ilink`，但**相对的是进程工作目录**，
        而桌面版会从任意目录启动进程（数据实际在应用数据目录里）。
        所以这里要把它归一到 `self.data_dir` 下，否则会出现
        "明明扫码成功了，重启后又提示未登录"。
        """
        raw = (self.cfg.adapter.ilink_state_dir or "").strip()
        if not raw:
            return self.data_dir / "ilink"
        d = Path(raw).expanduser()
        if d.is_absolute():
            return d
        return self.data_dir / (d.name or "ilink")

    async def ilink_qrcode(self) -> dict:
        """取官方 iLink 登录二维码（返回 SVG data URL）。"""
        base = "https://ilinkai.weixin.qq.com"
        url = f"{base}/ilink/bot/get_bot_qrcode"
        try:
            async with make_client(base, timeout=20.0) as c:
                r = await c.get(url, params={"bot_type": 3})
                if r.status_code >= 400:
                    return {"ok": False, "stage": "http",
                            "message": f"官方接口返回 HTTP {r.status_code}",
                            "hint": "请稍后重试；若持续失败，可能是官方服务临时不可用。"}
                body = r.json()
        except Exception as e:
            return {"ok": False, "stage": "connect",
                    "message": "连不上微信官方接口",
                    "detail": explain_error(e, url),
                    "hint": "请检查本机网络；官方接口在公网，需要能访问 "
                            "ilinkai.weixin.qq.com。"}

        if body.get("ret") not in (0, None):
            return {"ok": False, "stage": "api",
                    "message": f"官方接口返回 ret={body.get('ret')}",
                    "detail": str(body.get("errmsg") or ""),
                    "hint": "请稍后重试。"}

        qid = body.get("qrcode") or ""
        content = (body.get("qrcode_img_content") or "").strip()
        if not qid or not content:
            return {"ok": False, "message": "官方接口没有返回二维码内容"}

        # 二维码内容是一段 URL，离线转成 SVG（不依赖任何第三方服务）
        try:
            image = data_url(content, ec="M", scale=6, border=4)
        except QRDataTooLong:
            return {"ok": False, "message": "二维码内容过长，无法生成"}
        except Exception:
            log.exception("生成官方二维码失败")
            return {"ok": False, "message": "生成二维码失败"}

        self._login = {
            "status": "waiting", "channel": "ilink",
            "qrcode": qid, "image": image, "uuid": "", "app_id": "",
            "message": "请用微信扫码（二维码几分钟内有效）",
        }
        return {"ok": True, **self._login,
                "hint": "这是腾讯官方通道，目前只支持私聊。"}

    async def ilink_login_check(self) -> dict:
        """轮询官方扫码状态；成功后把凭证落到本地。"""
        if self._login.get("channel") != "ilink" or not self._login.get("qrcode"):
            return {"ok": False, "status": "idle", "message": "请先获取二维码"}

        base = "https://ilinkai.weixin.qq.com"
        url = f"{base}/ilink/bot/get_qrcode_status"
        try:
            # 服务端会 hold 住连接最长 35 秒，超时必须比它大
            async with make_client(base, timeout=50.0) as c:
                r = await c.get(url, params={"qrcode": self._login["qrcode"]})
                if r.status_code >= 400:
                    return {"ok": False, "status": "failed",
                            "message": f"查询失败：HTTP {r.status_code}"}
                st = r.json()
        except Exception as e:
            return {"ok": True, "status": self._login.get("status", "waiting"),
                    "message": self._login.get("message") or "等待扫码…",
                    "detail": explain_error(e, url)}

        status = st.get("status") or ""
        if status == "confirmed":
            ok = self._save_ilink_credentials(st)
            self._login.update({"status": "confirmed", "message": "登录成功"})
            return {"ok": True, "status": "confirmed", "message": "登录成功",
                    "saved": ok}
        if status == "binded_redirect":
            self._login.update({"status": "confirmed",
                                "message": "该账号已连接过，凭证可继续使用"})
            return {"ok": True, "status": "confirmed",
                    "message": "该账号已连接过，凭证可继续使用"}
        if status in ("expired", "cancelled"):
            self._login.update({"status": "failed", "message": "二维码已失效，请重新获取"})
            return {"ok": True, "status": "failed", "message": "二维码已失效，请重新获取"}

        return {"ok": True, "status": "waiting", "message": "等待扫码…"}

    def _save_ilink_credentials(self, st: dict) -> bool:
        """把官方返回的凭证写入 state_dir（ILinkAdapter 启动时会读它）。"""
        token = st.get("bot_token")
        if not token:
            log.warning("官方接口未返回 bot_token，凭证未保存")
            return False
        try:
            d = self._ilink_state_dir()
            d.mkdir(parents=True, exist_ok=True)
            f = d / "account.json"
            f.write_text(json.dumps({
                "bot_token": token,
                "base_url": st.get("baseurl") or "https://ilinkai.weixin.qq.com",
                "bot_user_id": st.get("bot_user_id") or st.get("ilink_user_id") or "",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(f, 0o600)
            log.info("官方 iLink 凭证已保存到 %s", f)
            return True
        except Exception:
            log.exception("保存官方凭证失败")
            return False

    def ilink_status(self) -> dict:
        """官方通道是否已有可用凭证（决定"已登录"徽标）。"""
        f = self._ilink_state_dir() / "account.json"
        if not f.exists():
            return {"loggedIn": False}
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {"loggedIn": False}
        return {"loggedIn": bool(d.get("bot_token")),
                "botUserId": d.get("bot_user_id") or ""}

    # ── 扫码登录入口 ──────────────────────────────────────────

    async def login_qrcode(self) -> dict:
        return await self.ilink_qrcode()

    async def login_check(self, captcha: str = "") -> dict:
        return await self.ilink_login_check()
